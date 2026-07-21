from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from app.core.database import acquire_transaction_locks
from app.models.entities import (
    Match,
    MatchEvent,
    MatchParticipant,
    Room,
    Speech,
    SpeechCorrectionRequest,
    TranscriptSegment,
    User,
)
from app.services.match_archive import enqueue_match_archive
from app.services.room_service import append_event, now
from app.services.speech_quality import normalize_transcript, transcript_rejection_reason
from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

TERMINAL_ARCHIVE_STATUSES = {"completed", "review_required"}


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _quality_error(content: str) -> str | None:
    reason = transcript_rejection_reason(content, require_substantive=True)
    return {
        "empty": "修正后的发言文字不能为空。",
        "too_short": "修正后的发言文字过短。",
        "no_speech_characters": "修正后的发言文字缺少可识别内容。",
        "control_characters": "修正后的发言文字包含无效控制字符。",
        "excessive_symbols": "修正后的发言文字包含过多无效符号。",
        "repeated_characters": "修正后的发言文字疑似无效重复内容。",
    }.get(reason)


def _request_fingerprint(proposed_content: str, reason: str) -> str:
    canonical = json.dumps(
        {"proposed_content": proposed_content, "reason": reason},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _idempotency_key(room: Room, speech_id: str, user: User, supplied: str | None) -> str | None:
    if not supplied:
        return None
    digest = hashlib.sha256(
        f"speech-correction:{room.id}:{speech_id}:{user.id}:{supplied[:128]}".encode()
    ).hexdigest()
    return f"speech-correction:{digest}"


def speech_owner_id(db: Session, speech: Speech) -> str | None:
    """Resolve immutable speaker identity from the append-only match log.

    RoomSeat is current room state and can legitimately change after AI
    substitution, restoration or administrative repair.  It must never be
    used to decide ownership of an already-finished human speech.
    """

    event_owner = db.scalar(
        select(MatchEvent.actor_user_id)
        .where(
            MatchEvent.room_id == speech.room_id,
            MatchEvent.event_type.in_(["speech.started", "speech.completed", "speech.late_finalized"]),
            MatchEvent.actor_user_id.is_not(None),
            MatchEvent.payload["speech_id"].as_string() == speech.id,
        )
        .order_by(MatchEvent.seq)
        .limit(1)
    )
    if event_owner:
        return event_owner
    return db.scalar(
        select(MatchParticipant.user_id).where(
            MatchParticipant.match_id == speech.match_id,
            MatchParticipant.seat_key == speech.seat_key,
        )
    )


def can_request_correction(db: Session, room: Room, speech: Speech, user: User | None) -> bool:
    return bool(
        user
        and speech.speaker_type == "human"
        and speech.status == "completed"
        and room.status not in {"cancelled", "terminated"}
        and speech_owner_id(db, speech) == user.id
    )


def serialize_correction_request(item: SpeechCorrectionRequest, *, expose_internal: bool = False) -> dict:
    payload = {
        "id": item.id,
        "speech_id": item.speech_id,
        "room_id": item.room_id,
        "original_content": item.original_content,
        "proposed_content": item.proposed_content,
        "reason": item.reason,
        "status": item.status,
        "review_reason": item.review_reason,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
        "resolved_at": item.resolved_at.isoformat() if item.resolved_at else None,
    }
    if expose_internal:
        payload |= {
            "requester_user_id": item.requester_user_id,
            "reviewed_by_user_id": item.reviewed_by_user_id,
            "original_segments": item.original_segments,
        }
    return payload


def create_correction_request(
    db: Session,
    room: Room,
    speech_id: str,
    requester: User,
    *,
    proposed_content: str,
    reason: str,
    provided_idempotency_key: str | None,
) -> tuple[SpeechCorrectionRequest, bool]:
    acquire_transaction_locks(db, f"1:user:{requester.id}", f"2:room:{room.id}")
    speech = db.get(Speech, speech_id)
    if not speech or speech.room_id != room.id:
        raise HTTPException(status_code=404, detail="发言记录不存在。")
    if speech.speaker_type != "human":
        raise HTTPException(status_code=403, detail="只能申请修正自己的真人发言。")
    owner_id = speech_owner_id(db, speech)
    if not owner_id:
        raise HTTPException(status_code=409, detail="该历史发言缺少可验证的提交身份，请联系管理员登记数据问题。")
    if owner_id != requester.id:
        raise HTTPException(status_code=403, detail="只能申请修正自己的真人发言。")
    if speech.status != "completed":
        raise HTTPException(status_code=409, detail="只有已经提交完成的真人发言可以申请修正。")
    if room.status in {"cancelled", "terminated"}:
        raise HTTPException(status_code=409, detail="比赛已经取消或终止，原始记录保持只读；请联系管理员登记数据问题。")

    normalized = normalize_transcript(proposed_content)
    quality_error = _quality_error(normalized)
    if quality_error:
        raise HTTPException(status_code=422, detail=quality_error)
    normalized_reason = " ".join(reason.strip().split())
    fingerprint = _request_fingerprint(normalized, normalized_reason)
    operation_key = _idempotency_key(room, speech.id, requester, provided_idempotency_key)
    if operation_key:
        replay = db.scalar(select(SpeechCorrectionRequest).where(SpeechCorrectionRequest.idempotency_key == operation_key))
        if replay:
            if replay.request_fingerprint != fingerprint:
                raise HTTPException(status_code=409, detail="同一个幂等键不能用于不同的修正内容。")
            return replay, True

    original = normalize_transcript(speech.content)
    if normalized == original:
        raise HTTPException(status_code=409, detail="修正内容与当前发言完全相同，无需提交。")

    pending = db.scalar(
        select(SpeechCorrectionRequest)
        .where(
            SpeechCorrectionRequest.speech_id == speech.id,
            SpeechCorrectionRequest.status == "pending",
        )
        .order_by(SpeechCorrectionRequest.created_at.desc())
        .limit(1)
    )
    if pending:
        if pending.request_fingerprint != fingerprint:
            raise HTTPException(status_code=409, detail="该发言已有待复核申请，请先撤销或等待管理员处理。")
        return pending, True

    segments = list(
        db.scalars(
            select(TranscriptSegment)
            .where(TranscriptSegment.speech_id == speech.id)
            .order_by(TranscriptSegment.start_ms, TranscriptSegment.id)
        ).all()
    )
    request = SpeechCorrectionRequest(
        speech_id=speech.id,
        room_id=room.id,
        requester_user_id=requester.id,
        original_content=speech.content,
        proposed_content=normalized,
        original_segments=[
            {
                "start_ms": item.start_ms,
                "end_ms": item.end_ms,
                "text": item.text,
                "is_final": item.is_final,
            }
            for item in segments
        ],
        reason=normalized_reason,
        idempotency_key=operation_key,
        request_fingerprint=fingerprint,
    )
    db.add(request)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="该发言已有待复核申请，请刷新后查看。") from exc
    append_event(
        db,
        room,
        "speech.correction_requested",
        {"request_id": request.id, "speech_id": speech.id, "seat_key": speech.seat_key},
        actor_user_id=requester.id,
    )
    return request, False


def cancel_correction_request(
    db: Session,
    room: Room,
    request: SpeechCorrectionRequest,
    requester: User,
) -> bool:
    acquire_transaction_locks(db, f"1:user:{requester.id}", f"2:room:{room.id}")
    if request.room_id != room.id or request.requester_user_id != requester.id:
        raise HTTPException(status_code=403, detail="只能撤销自己的发言修正申请。")
    if request.status == "cancelled":
        return True
    if request.status != "pending":
        raise HTTPException(status_code=409, detail="该修正申请已经处理，不能撤销。")
    request.status = "cancelled"
    request.review_reason = "参赛者主动撤销"
    request.resolved_at = now()
    speech = db.get(Speech, request.speech_id)
    append_event(
        db,
        room,
        "speech.correction_cancelled",
        {"request_id": request.id, "speech_id": request.speech_id, "seat_key": speech.seat_key if speech else ""},
        actor_user_id=requester.id,
    )
    return False


def review_correction_request(
    db: Session,
    room: Room,
    request: SpeechCorrectionRequest,
    reviewer: User,
    *,
    approve: bool,
    reason: str,
    expected_updated_at: datetime,
) -> tuple[bool, str | None]:
    if reviewer.role != "system_admin":
        raise HTTPException(status_code=403, detail="仅系统管理员可以复核发言修正。")
    acquire_transaction_locks(db, f"1:user:{request.requester_user_id}", f"2:room:{room.id}")
    if request.room_id != room.id:
        raise HTTPException(status_code=404, detail="修正申请不存在。")
    if _as_utc(request.updated_at) != _as_utc(expected_updated_at):
        raise HTTPException(status_code=409, detail="该修正申请已被更新，请刷新后重试。")
    if request.status != "pending":
        raise HTTPException(status_code=409, detail="该修正申请已经处理。")
    speech = db.get(Speech, request.speech_id)
    if not speech or speech.room_id != room.id:
        raise HTTPException(status_code=409, detail="原发言记录不存在，无法处理修正。")
    match = db.get(Match, speech.match_id)
    if not match:
        raise HTTPException(status_code=409, detail="原发言缺少比赛记录，无法处理修正。")

    if approve and (room.status in {"cancelled", "terminated"} or match.status == "terminated"):
        raise HTTPException(status_code=409, detail="比赛已经取消或终止，原始记录保持只读，不能批准改写。")

    request.reviewed_by_user_id = reviewer.id
    request.review_reason = " ".join(reason.strip().split())
    request.resolved_at = now()
    archive_match_id: str | None = None
    if not approve:
        request.status = "rejected"
        event_type = "speech.correction_rejected"
    else:
        if speech.content != request.original_content:
            raise HTTPException(status_code=409, detail="原发言已发生变化，请让参赛者基于最新文本重新申请。")
        old_segments = list(
            db.scalars(select(TranscriptSegment).where(TranscriptSegment.speech_id == speech.id)).all()
        )
        end_ms = max(
            [item.end_ms for item in old_segments]
            + [round(max(0, speech.duration_seconds) * 1000)],
        )
        db.execute(delete(TranscriptSegment).where(TranscriptSegment.speech_id == speech.id))
        db.add(
            TranscriptSegment(
                speech_id=speech.id,
                start_ms=0,
                end_ms=end_ms,
                text=request.proposed_content,
                is_final=True,
            )
        )
        speech.content = request.proposed_content
        request.status = "approved"
        event_type = "speech.corrected"
        if match.status in TERMINAL_ARCHIVE_STATUSES:
            archive_match_id = match.id

    append_event(
        db,
        room,
        event_type,
        {"request_id": request.id, "speech_id": speech.id, "seat_key": speech.seat_key},
        actor_user_id=reviewer.id,
    )
    return False, archive_match_id


def visible_correction_requests(db: Session, room: Room, user: User) -> list[dict]:
    statement = select(SpeechCorrectionRequest).where(SpeechCorrectionRequest.room_id == room.id)
    if user.role != "system_admin":
        statement = statement.where(SpeechCorrectionRequest.requester_user_id == user.id)
    rows = db.scalars(statement.order_by(SpeechCorrectionRequest.created_at.desc())).all()
    return [serialize_correction_request(item, expose_internal=user.role == "system_admin") for item in rows]


def enqueue_correction_archive(match_id: str | None) -> None:
    if match_id:
        enqueue_match_archive(match_id)
