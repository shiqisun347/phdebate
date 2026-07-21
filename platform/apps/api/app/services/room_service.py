from __future__ import annotations

import math
import random
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.database import acquire_transaction_locks
from app.models.entities import (
    AgentProfile,
    CaptionSegment,
    Competition,
    JudgeScorecard,
    LeaderboardEntry,
    Match,
    MatchEvent,
    Room,
    RoomCodeReservation,
    RoomSeat,
    Season,
    Speech,
    TranscriptSegment,
    User,
)
from app.services.seasons import serialize_season
from fastapi import HTTPException
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

AI_NAMES = ["陈思远", "乾元", "明川", "知微", "景行", "若谷", "清和", "见山"]
ANONYMOUS_SPECTATOR_STATUSES = frozenset(
    {"preparing", "running", "paused", "judging", "completed", "review_required", "terminated"}
)


def now() -> datetime:
    return datetime.now(timezone.utc)


def reset_connected_presence(db: Session) -> int:
    active_room_ids = select(Room.id).where(Room.status.in_(["lobby", "preparing", "running", "paused", "judging"]))
    result = db.execute(
        update(RoomSeat)
        .where(
            RoomSeat.room_id.in_(active_room_ids),
            RoomSeat.occupant_type == "human",
            RoomSeat.connected.is_(True),
        )
        .values(connected=False, disconnected_at=now())
    )
    return result.rowcount or 0


def room_code(db: Session) -> str:
    for _ in range(100):
        code = f"{secrets.randbelow(900000) + 100000:06d}"
        try:
            with db.begin_nested():
                db.add(RoomCodeReservation(code=code))
                db.flush()
            return code
        except IntegrityError:
            continue
    raise RuntimeError("无法生成唯一房间号")


def seat_keys(competition: Competition) -> list[tuple[str, str, int]]:
    per_side = competition.seat_count // 2
    return [(f"{side}_{position}", side, position) for side in ("aff", "neg") for position in range(1, per_side + 1)]


def load_room(db: Session, code: str, *, lock: bool = False) -> Room:
    if lock:
        acquire_transaction_locks(db, f"0:room:{code}")
    stmt = select(Room).options(selectinload(Room.seats), selectinload(Room.competition)).where(Room.code == code)
    if lock:
        stmt = stmt.with_for_update()
    room = db.scalar(stmt)
    if not room:
        raise HTTPException(status_code=404, detail="房间不存在。")
    return room


def stage(room: Room) -> dict[str, Any] | None:
    if room.current_stage_index < 0 or room.current_stage_index >= len(room.template_snapshot or []):
        return None
    return room.template_snapshot[room.current_stage_index]


def remaining_seconds(room: Room) -> int | None:
    if room.status in {"completed", "review_required", "terminated", "cancelled"}:
        return 0
    if room.status == "paused":
        return room.paused_remaining_seconds
    current = stage(room)
    if current and current.get("host_announcement_pending"):
        return max(1, int(current.get("host_target_duration_seconds") or current.get("duration", 30)))
    if current and current.get("ai_preparing"):
        frozen = current.get("preparing_stage_remaining_seconds")
        return max(0, int(frozen)) if frozen is not None else None
    if not room.stage_deadline_at:
        return None
    deadline = room.stage_deadline_at
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    return max(0, int((deadline - now()).total_seconds()))


def free_turn_deadline(room: Room, current: dict[str, Any] | None = None) -> datetime | None:
    current = current or stage(room)
    if not current or current.get("kind") != "free":
        return None
    started_at = room.stage_started_at
    raw_started_at = current.get("turn_started_at")
    if raw_started_at:
        try:
            started_at = datetime.fromisoformat(str(raw_started_at))
        except ValueError:
            started_at = room.stage_started_at
    if not started_at:
        return None
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    deadline = started_at + timedelta(seconds=max(1, int(current.get("turn_duration", 45))))
    if room.stage_deadline_at:
        stage_deadline = room.stage_deadline_at
        if stage_deadline.tzinfo is None:
            stage_deadline = stage_deadline.replace(tzinfo=timezone.utc)
        deadline = min(deadline, stage_deadline)
    return deadline


def free_turn_remaining_seconds(room: Room, current: dict[str, Any] | None = None) -> int | None:
    if room.status in {"completed", "review_required", "terminated", "cancelled"}:
        return 0
    current = current or stage(room)
    if room.status == "paused" and current and current.get("kind") == "free":
        paused = current.get("paused_turn_remaining_seconds")
        return max(0, int(paused)) if paused is not None else None
    if current and current.get("kind") == "free" and current.get("ai_preparing"):
        frozen = current.get("preparing_turn_remaining_seconds")
        return max(0, int(frozen)) if frozen is not None else None
    deadline = free_turn_deadline(room, current)
    return max(0, int((deadline - now()).total_seconds())) if deadline else None


def can_control(db: Session, room: Room, user: User) -> bool:
    del db
    if user.role == "system_admin":
        return True
    return room.owner_id == user.id


def transfer_room_owner(
    db: Session,
    room: Room,
    successor: RoomSeat,
    *,
    actor_user_id: str | None = None,
    reason: str,
    idempotency_key: str | None = None,
) -> bool:
    """Move room control to an authoritative human seat.

    The debate flow is automated, but failure recovery still requires an
    online room controller.  Keeping ownership attached to a disconnected
    participant after their seat is replaced by AI leaves every remaining
    human unable to pause, retry, or terminate the match.  This helper keeps
    the ownership mutation and its append-only audit event inseparable.
    """

    if successor.occupant_type != "human" or not successor.user_id:
        raise ValueError("room ownership can only be transferred to a human seat")
    if successor.user_id == room.owner_id:
        return False
    previous_owner_id = room.owner_id
    room.owner_id = successor.user_id
    append_event(
        db,
        room,
        "room.owner_transferred",
        {
            "previous_owner_id": previous_owner_id,
            "new_owner_id": successor.user_id,
            "seat_key": successor.seat_key,
            "real_name": successor.display_name,
            "reason": reason,
        },
        actor_user_id=actor_user_id,
        idempotency_key=idempotency_key,
    )
    return True


def can_view_room(db: Session, room: Room, user: User | None) -> bool:
    if room.is_test_data:
        # QA rooms may deliberately use public visibility so browser and load
        # tests exercise the production spectator path.  That must not turn
        # synthetic students, transcripts or recordings into public data.
        # Test data remains available only to its participants, room owner and
        # system administrators through the same authenticated surfaces.
        if not user:
            return False
        # Synthetic browser/load-test users need to be able to join a public
        # synthetic lobby before they have a seat.  Restrict that exception to
        # accounts explicitly marked as test data; ordinary authenticated
        # students must never gain access to QA rooms merely by knowing the
        # six-digit code.
        if room.status == "lobby" and room.visibility == "public" and user.is_test_account:
            return True
        return can_control(db, room, user) or user_seat(room, user) is not None
    if room.visibility == "public":
        # A six-digit room code is a locator, not an authentication secret.
        # Waiting rooms expose real student names and readiness state, so they
        # require a login even when the eventual match will be public.  Once a
        # match starts, the same room becomes an anonymous spectator surface.
        return user is not None or room.status in ANONYMOUS_SPECTATOR_STATUSES
    if not user:
        return False
    return can_control(db, room, user) or user_seat(room, user) is not None


def use_public_projection(db: Session, room: Room, user: User | None) -> bool:
    if not user:
        return True
    return not can_control(db, room, user) and user_seat(room, user) is None


def user_seat(room: Room, user: User | None) -> RoomSeat | None:
    if not user:
        return None
    return next((seat for seat in room.seats if seat.user_id == user.id), None)


def speaking_permission(
    room: Room,
    user: User | None,
    *,
    ignore_active_speech: bool = False,
) -> tuple[bool, str]:
    if not user:
        return False, "登录后才能参赛"
    seat = user_seat(room, user)
    if not seat:
        return False, "你不是本房间辩手"
    if seat.occupant_type == "ai_substitute":
        return False, "你的席位已由 AI 接替，等待管理员恢复真人控制"
    if seat.occupant_type != "human":
        return False, "AI 席位由系统控制"
    if room.status == "paused":
        return False, "比赛已暂停"
    if room.status == "judging":
        return False, "裁判正在评议"
    if room.status != "running":
        return False, "比赛尚未开始"
    current = stage(room)
    if not current:
        return False, "当前没有发言环节"
    if current.get("host_announcement_pending"):
        return False, "主持人正在播报下一环节"
    active = active_speech(db=None, room=room)
    if active and not ignore_active_speech:
        return False, "该席位正在另一设备发言" if active.seat_key == seat.seat_key else "其他辩手正在发言"
    if current.get("kind") == "speech" and current.get("seat") != seat.seat_key:
        return False, f"当前轮到 {seat_label(current.get('seat', ''))}"
    if current.get("kind") == "free":
        if current.get("intermission_deadline_at"):
            return False, "正在等待下一方的三秒发言申请窗口结束"
        if current.get("side") != seat.side:
            return False, f"自由辩论当前轮到{'正方' if current.get('side') == 'aff' else '反方'}"
        if current.get("force_ai_fallback"):
            return False, "本轮无人申请，系统正在安排 AI 接替"
        selected = current.get("selected_human_seat")
        if selected and selected != seat.seat_key:
            return False, f"本轮已按申请顺序选中 {seat_label(str(selected))}"
    if current.get("kind") not in {"speech", "free"}:
        return False, "当前是自动流程环节"
    return True, "轮到你发言"


def active_speech(db: Session | None, room: Room) -> Speech | None:
    if db is None:
        return getattr(room, "_active_speech", None)
    match = db.scalar(select(Match).where(Match.room_id == room.id))
    if not match:
        return None
    return db.scalar(
        select(Speech)
        .where(Speech.match_id == match.id, Speech.status.in_(["speaking", "synthesizing", "playing"]))
        .order_by(Speech.created_at.desc())
    )


def serialize_user(user: User) -> dict[str, Any]:
    return {
        "id": user.id,
        "account": user.account,
        "real_name": user.real_name,
        "role": user.role,
        "is_active": user.is_active,
        "is_test_account": user.is_test_account,
    }


def serialize_scorecard(
    scorecard: JudgeScorecard | None,
    *,
    expose_internal_details: bool = False,
    allowed_seat_keys: set[str] | None = None,
) -> dict[str, Any] | None:
    if not scorecard:
        return None
    approved = scorecard.status == "approved"
    if approved or expose_internal_details:
        winner = scorecard.winner
        affirmative_score: float | None = scorecard.affirmative_score
        negative_score: float | None = scorecard.negative_score
        reasoning = scorecard.reasoning
    else:
        # Provider failures can contain endpoints and operational details. A
        # pending scorecard is not a published result, so only administrators
        # may inspect its provisional values and raw failure reason.
        winner = None
        affirmative_score = None
        negative_score = None
        reasoning = "裁判结果等待管理员复核。"
    individual_scores: dict[str, float] = {}
    if approved or expose_internal_details:
        for seat_key, raw_score in (scorecard.individual_scores or {}).items():
            if allowed_seat_keys is not None and seat_key not in allowed_seat_keys:
                continue
            if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)):
                continue
            score = float(raw_score)
            if math.isfinite(score) and 0 <= score <= 100:
                individual_scores[seat_key] = round(score, 2)
    return {
        "id": scorecard.id,
        "status": scorecard.status,
        "winner": winner,
        "affirmative_score": affirmative_score,
        "negative_score": negative_score,
        "individual_scores": individual_scores,
        "reasoning": reasoning,
    }


def serialize_competition(competition: Competition, *, topics: list | None = None, live_count: int = 0) -> dict[str, Any]:
    return {
        "id": competition.id,
        "slug": competition.slug,
        "name": competition.name,
        "tagline": competition.tagline,
        "description": competition.description,
        "rules": competition.rules,
        "format": competition.format,
        "seat_count": competition.seat_count,
        "ranked": competition.ranked,
        "allow_custom_topic": competition.allow_custom_topic,
        "accent": competition.accent,
        "is_active": competition.is_active,
        "season": serialize_season(competition.season) if competition.season else None,
        "live_count": live_count,
        "topics": [{"id": topic.id, "title": topic.title, "is_active": topic.is_active} for topic in topics or []],
    }


PUBLIC_ROOM_HIDDEN_EVENT_TYPES = frozenset(
    {
        "presence.connected",
        "presence.disconnected",
        "presence.expired",
        "presence.changed",
        "seat.control_acquired",
        "seat.control_taken_over",
        "speech.correction_requested",
        "speech.correction_cancelled",
        "speech.correction_rejected",
        "speech.corrected",
    }
)

PUBLIC_MATCH_TIMELINE_EVENT_TYPES = frozenset(
    {
        "room.created",
        "room.locked",
        "room.cancelled",
        "room.owner_transferred",
        "match.started",
        "match.completed",
        "match.result",
        "stage.started",
        "stage.completed",
        "stage.advanced",
        "speech.started",
        "speech.completed",
        "speech.interrupted",
        "speech.timed_out",
        "speech.late_finalized",
        "free.side_changed",
        "free.turn_timed_out",
        "control.pause",
        "control.resume",
        "control.terminate",
        "judge.review_required",
        "judge.interrupted",
        "judge.reviewed",
        "judge.corrected",
    }
)


def serialize_room(db: Session, room: Room, user: User | None = None, *, public: bool = False) -> dict[str, Any]:
    current = stage(room)
    seat = user_seat(room, user)
    active = active_speech(db, room)
    setattr(room, "_active_speech", active)
    allowed, reason = speaking_permission(room, user)
    turn_remaining_seconds = free_turn_remaining_seconds(room, current)
    events = db.scalars(select(MatchEvent).where(MatchEvent.room_id == room.id).order_by(MatchEvent.seq.desc()).limit(40)).all()
    recent_speeches = db.scalars(select(Speech).where(Speech.room_id == room.id).order_by(Speech.created_at.desc()).limit(20)).all()
    active_caption_segments = []
    if active:
        active_caption_segments = list(
            reversed(
                list(
                    db.scalars(
                        select(CaptionSegment)
                        .where(CaptionSegment.speech_id == active.id)
                        .order_by(CaptionSegment.ordinal.desc(), CaptionSegment.id.desc())
                        .limit(40)
                    ).all()
                )
            )
        )
        # A speech may have started just before migration 0031 or an old ASR
        # client may not yet have written the display projection. Preserve a
        # reconnect-safe fallback without ever treating AI text-generation
        # offsets as research transcript timing.
        if not active_caption_segments:
            active_caption_segments = list(
                reversed(
                    list(
                        db.scalars(
                            select(TranscriptSegment)
                            .where(TranscriptSegment.speech_id == active.id)
                            .order_by(TranscriptSegment.start_ms.desc(), TranscriptSegment.id.desc())
                            .limit(40)
                        ).all()
                    )
                )
            )
    expose_internal_details = bool(user and user.role == "system_admin")
    serialized_events = []
    for item in reversed(events):
        if public and item.event_type in PUBLIC_ROOM_HIDDEN_EVENT_TYPES:
            continue
        if expose_internal_details:
            payload = item.payload
        elif public or user is None:
            payload = anonymous_event_payload(item.event_type, item.payload)
        else:
            payload = public_event_payload(item.event_type, item.payload)
        serialized_events.append({"seq": item.seq, "type": item.event_type, "payload": payload, "created_at": item.created_at.isoformat()})
    restore_requests: list[dict[str, Any]] = []
    if user:
        # Imported lazily because the restoration service also uses the room
        # event and permission helpers defined in this module.
        from app.services.seat_restore import visible_restore_requests

        restore_requests = visible_restore_requests(db, room, user)
    from app.services.free_turn_queue import queue_projection

    free_turn_queue = queue_projection(db, room, user)
    return {
        "id": room.id,
        "code": room.code,
        "topic": room.topic,
        "status": room.status,
        "visibility": room.visibility,
        "is_test_data": room.is_test_data,
        "seq": room.seq,
        "competition": serialize_competition(room.competition),
        "season": serialize_season(room.season) if room.season else None,
        "owner": {"id": room.owner.id, "real_name": room.owner.real_name} if not public else {"real_name": room.owner.real_name},
        "seats": [
            {
                "seat_key": item.seat_key,
                "side": item.side,
                "position": item.position,
                "label": seat_label(item.seat_key),
                "occupant_type": item.occupant_type,
                "display_name": item.display_name,
                "is_ready": item.is_ready,
                "connected": item.connected,
                "is_me": bool(user and item.user_id == user.id),
                "is_owner": bool(item.user_id and item.user_id == room.owner_id),
            }
            for item in sorted(room.seats, key=lambda x: (x.side, x.position))
        ],
        "current_stage": current,
        "current_stage_index": room.current_stage_index,
        "remaining_seconds": remaining_seconds(room),
        "turn_remaining_seconds": turn_remaining_seconds,
        "active_speech": (
            {
                "id": active.id,
                "seat_key": active.seat_key,
                "speaker_type": active.speaker_type,
                "status": active.status,
                "content": active.content,
                "playback_started_at": active.playback_started_at.isoformat() if active.playback_started_at else None,
                "stream_generation": active.stream_generation,
                "stream_sample_rate": active.stream_sample_rate,
            }
            if active
            else None
        ),
        "caption_segments": [
            {
                "segment_id": item.id,
                "speech_id": item.speech_id,
                "text": item.text,
                "start_ms": (
                    item.presentation_offset_ms if isinstance(item, CaptionSegment) else item.start_ms
                ),
                "end_ms": (
                    item.presentation_offset_ms if isinstance(item, CaptionSegment) else item.end_ms
                ),
                "is_final": item.is_final,
                "timing_basis": item.timing_basis if isinstance(item, CaptionSegment) else "asr",
                "source": item.source if isinstance(item, CaptionSegment) else "asr",
                "updated_at": (
                    item.updated_at
                    if isinstance(item, CaptionSegment)
                    else active.created_at + timedelta(milliseconds=max(item.start_ms, item.end_ms))
                ).isoformat(),
            }
            for item in active_caption_segments
        ],
        "my_seat": seat.seat_key if seat else None,
        "can_speak": allowed,
        "speak_reason": reason,
        "can_control": bool(user and can_control(db, room, user)),
        "seat_restore_requests": restore_requests,
        "free_turn_queue": free_turn_queue,
        "failure_reason": (
            room.failure_reason if expose_internal_details else "服务暂时异常，比赛已安全暂停。" if room.failure_reason else ""
        ),
        "recent_events": serialized_events,
        "speeches": [
            {
                "id": item.id,
                "seat_key": item.seat_key,
                "speaker": f"上一段发言 · {seat_label(item.seat_key)}",
                "stage_key": item.stage_key,
                "content": item.content,
                "audio_url": item.audio_url,
                "duration_seconds": item.duration_seconds,
                "playback_started_at": item.playback_started_at.isoformat() if item.playback_started_at else None,
                "playback_ends_at": item.playback_ends_at.isoformat() if item.playback_ends_at else None,
                "stream_generation": item.stream_generation,
                "stream_sample_rate": item.stream_sample_rate,
                "status": item.status,
                "created_at": item.created_at.isoformat(),
            }
            for item in reversed(recent_speeches)
        ],
    }


def public_event_payload(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    if event_type == "provider.failed":
        return {"provider": payload.get("provider"), "message": "服务暂时异常"}
    if event_type == "judge.corrected":

        def public_result(value: Any) -> dict[str, Any]:
            source = value if isinstance(value, dict) else {}
            return {key: source[key] for key in ("winner", "affirmative_score", "negative_score") if key in source}

        return {"old": public_result(payload.get("old")), "new": public_result(payload.get("new"))}
    safe_keys = {
        "competition",
        "topic",
        "owner_seat",
        "seat_key",
        "real_name",
        "ready",
        "ai_filled",
        "stage",
        "stage_index",
        "deadline_at",
        "speech_id",
        "speaker_type",
        "content",
        "audio_url",
        "duration_seconds",
        "side",
        "winner",
        "scorecard_id",
        "stage_key",
        "text",
        "generation",
        "transport",
        "track_sid",
        "tts_session_id",
        "voice_id",
        "synthesis_mode",
        "interrupt_id",
        "flush_guard_ms",
        "agent_first_readable_delta_at",
        "server_first_capture_at",
        "sample_rate",
        "channels",
        "sample_width",
    }
    result = {key: value for key, value in payload.items() if key in safe_keys}
    if (
        event_type.startswith(("stage.", "control.", "speech.", "audio.rtc."))
        or event_type in {"room.cancelled", "judge.interrupted"}
    ) and "reason" in payload:
        result["reason"] = payload["reason"]
    return result


# The anonymous stage needs only presentation state. Runtime bookkeeping such as
# turn_started_at and paused/preparation counters is represented by the
# authoritative remaining_seconds fields on the room projection instead.
ANONYMOUS_STAGE_FIELDS = frozenset({"key", "name", "kind", "side", "seat", "cue", "ai_preparing"})

# Continuous anonymous playback is intentionally driven by the room snapshot:
# active_speech.{id,status,stream_generation,stream_sample_rate,playback_started_at}
# identifies and resumes the PCM stream, while speeches.{audio_url,
# duration_seconds,playback_started_at,playback_ends_at} provides the compatible
# file fallback. Events only need the fields below to render captions/stages,
# start an announcement cue, and flush the currently identified generation.
ANONYMOUS_EVENT_FIELDS = frozenset(
    {
        "competition",
        "topic",
        "owner_seat",
        "seat_key",
        "real_name",
        "ready",
        "ai_filled",
        "stage",
        "stage_index",
        "deadline_at",
        "speech_id",
        "speaker_type",
        "content",
        "audio_url",
        "duration_seconds",
        "side",
        "winner",
        "scorecard_id",
        "stage_key",
        "text",
        "generation",
    }
)


def anonymous_event_payload(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Return the minimum event projection needed by a read-only spectator."""

    if event_type == "provider.failed":
        return {"message": "服务暂时异常"}
    if event_type == "judge.corrected":

        def public_result(value: Any) -> dict[str, Any]:
            source = value if isinstance(value, dict) else {}
            return {key: source[key] for key in ("winner", "affirmative_score", "negative_score") if key in source}

        return {"old": public_result(payload.get("old")), "new": public_result(payload.get("new"))}

    result = {key: value for key, value in payload.items() if key in ANONYMOUS_EVENT_FIELDS}
    if isinstance(result.get("stage"), dict):
        stage_payload = result["stage"]
        result["stage"] = {key: stage_payload[key] for key in ANONYMOUS_STAGE_FIELDS if key in stage_payload}
    # These reasons are explicit competition actions or public state-machine
    # transitions, not provider exception strings. They remain useful in a
    # spectator timeline without exposing Agent/TTS diagnostics.
    if (event_type.startswith(("stage.", "control.")) or event_type == "room.cancelled") and isinstance(
        payload.get("reason"), str
    ):
        result["reason"] = payload["reason"]
    return result


def anonymous_realtime_event(message: dict[str, Any]) -> dict[str, Any]:
    """Strip room-hub diagnostics before forwarding an event to an anonymous WS."""

    event_type = message.get("type")
    result: dict[str, Any] = {"type": event_type} if isinstance(event_type, str) else {}
    if type(message.get("seq")) is int:
        result["seq"] = message["seq"]
    if event_type in {"asr", "caption.segment"}:
        if isinstance(message.get("text"), str):
            result["text"] = message["text"]
        if isinstance(message.get("is_final"), bool):
            result["is_final"] = message["is_final"]
        for key in ("speech_id", "seat_key", "segment_id"):
            if isinstance(message.get(key), str):
                result[key] = message[key]
        for key in ("start_ms", "end_ms"):
            if type(message.get(key)) is int:
                result[key] = message[key]
        if message.get("timing_basis") in {"agent_text", "asr"}:
            result["timing_basis"] = message["timing_basis"]
    if event_type in {"audio.rtc.interrupt", "audio.realtime.aborted", "audio.stream.aborted"} and isinstance(
        message.get("generation"), str
    ):
        result["generation"] = message["generation"]
    return result


def append_event(
    db: Session,
    room: Room,
    event_type: str,
    payload: dict[str, Any],
    *,
    actor_user_id: str | None = None,
    idempotency_key: str | None = None,
) -> MatchEvent:
    if idempotency_key:
        existing = db.scalar(select(MatchEvent).where(MatchEvent.idempotency_key == idempotency_key))
        if existing:
            return existing
    room.seq += 1
    match = db.scalar(select(Match).where(Match.room_id == room.id))
    event = MatchEvent(
        room_id=room.id,
        match_id=match.id if match else None,
        seq=room.seq,
        event_type=event_type,
        actor_user_id=actor_user_id,
        idempotency_key=idempotency_key,
        payload=payload,
    )
    db.add(event)
    return event


def seat_label(key: str) -> str:
    try:
        side, position = key.split("_", 1)
        return f"{'正方' if side == 'aff' else '反方'}{position}辩"
    except ValueError:
        return key


def choose_agent_profiles(db: Session, count: int) -> list[AgentProfile]:
    profiles = list(db.scalars(select(AgentProfile).where(AgentProfile.is_active.is_(True)).order_by(AgentProfile.created_at)).all())
    if not profiles:
        return []
    random.shuffle(profiles)
    return [profiles[index % len(profiles)] for index in range(count)]


def leaderboard(
    db: Session,
    competition_id: str | None = None,
    season_id: str | None = None,
    limit: int = 100,
    *,
    include_test_accounts: bool = False,
) -> list[dict[str, Any]]:
    def rank_time(value: datetime | None) -> float:
        if value is None:
            return 0.0
        normalized = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return normalized.timestamp()

    if competition_id and season_id is None:
        season_id = db.scalar(select(Competition.season_id).where(Competition.id == competition_id))
    if season_id is None:
        season_id = db.scalar(select(Season.id).where(Season.is_active.is_(True)).order_by(Season.starts_at.desc(), Season.id).limit(1))
    if season_id is None:
        return []
    stmt = select(LeaderboardEntry).options(selectinload(LeaderboardEntry.user))
    if not include_test_accounts:
        # Public rankings are an identity surface as well as a score table.
        # Keep moderation effective without destroying the append-only score
        # history: disabled accounts disappear immediately and return with the
        # same historical aggregate if an administrator reactivates them.
        stmt = stmt.join(User, User.id == LeaderboardEntry.user_id).where(
            User.is_test_account.is_(False),
            User.is_active.is_(True),
        )
    if competition_id:
        stmt = stmt.where(LeaderboardEntry.competition_id == competition_id)
    rows = list(db.scalars(stmt.where(LeaderboardEntry.season_id == season_id)).all())
    if competition_id:
        ranked_rows = [
            {
                "user_id": row.user_id,
                "real_name": row.user.real_name,
                "points": row.points,
                "wins": row.wins,
                "draws": row.draws,
                "losses": row.losses,
                "matches": row.matches,
                "average_score": row.average_score,
                "last_match_at": row.last_match_at,
            }
            for row in rows
        ]
    else:
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            item = grouped.setdefault(
                row.user_id,
                {
                    "user_id": row.user_id,
                    "real_name": row.user.real_name,
                    "points": 0,
                    "wins": 0,
                    "draws": 0,
                    "losses": 0,
                    "matches": 0,
                    "weighted_score": 0.0,
                    "last_match_at": None,
                },
            )
            item["points"] += row.points
            item["wins"] += row.wins
            item["draws"] += row.draws
            item["losses"] += row.losses
            item["matches"] += row.matches
            item["weighted_score"] += row.average_score * row.matches
            if row.last_match_at and rank_time(row.last_match_at) > rank_time(item["last_match_at"]):
                item["last_match_at"] = row.last_match_at
        ranked_rows = [
            item
            | {
                "average_score": item["weighted_score"] / item["matches"] if item["matches"] else 0.0,
            }
            for item in grouped.values()
        ]
    ranked_rows.sort(
        key=lambda row: (
            row["points"],
            row["average_score"],
            row["matches"],
            rank_time(row["last_match_at"]),
            row["real_name"],
        ),
        reverse=True,
    )
    return [
        {
            "rank": index,
            "user_id": row["user_id"],
            "real_name": row["real_name"],
            "points": row["points"],
            "wins": row["wins"],
            "draws": row["draws"],
            "losses": row["losses"],
            "matches": row["matches"],
            "average_score": round(row["average_score"], 2),
            "last_match_at": row["last_match_at"].isoformat() if row["last_match_at"] else None,
            "season_id": season_id,
        }
        for index, row in enumerate(ranked_rows[:limit], start=1)
    ]


def rebuild_leaderboard_entries(
    db: Session,
    *,
    competition_id: str,
    season_id: str | None,
    user_ids: set[str],
) -> None:
    """Rebuild materialized leaderboard rows from authoritative final results.

    RatingChange remains an append-only audit trail. This function is used when
    a historical room is reclassified as QA data, so all affected participants
    lose only the aggregate contribution from excluded rooms.
    """

    if not user_ids:
        return
    db.flush()
    season_filter = Match.season_id.is_(None) if season_id is None else Match.season_id == season_id
    db.execute(
        delete(LeaderboardEntry).where(
            LeaderboardEntry.competition_id == competition_id,
            LeaderboardEntry.season_id.is_(None) if season_id is None else LeaderboardEntry.season_id == season_id,
            LeaderboardEntry.user_id.in_(user_ids),
        )
    )
    rows = db.execute(
        select(
            RoomSeat.user_id,
            RoomSeat.side,
            Match.winner,
            JudgeScorecard.affirmative_score,
            JudgeScorecard.negative_score,
            Room.completed_at,
        )
        .join(Room, Room.id == RoomSeat.room_id)
        .join(Match, Match.room_id == Room.id)
        .join(JudgeScorecard, JudgeScorecard.match_id == Match.id)
        .where(
            RoomSeat.user_id.in_(user_ids),
            RoomSeat.occupant_type.in_(["human", "ai_substitute"]),
            Room.competition_id == competition_id,
            Room.is_test_data.is_(False),
            Match.status == "completed",
            season_filter,
            JudgeScorecard.status == "approved",
        )
        .order_by(Room.completed_at, Match.id, RoomSeat.user_id)
    ).all()
    aggregates: dict[str, dict[str, Any]] = {}
    for user_id, side, winner, affirmative_score, negative_score, completed_at in rows:
        if not user_id:
            continue
        outcome = "draw" if winner == "draw" else "win" if winner == side else "loss"
        points = 3 if outcome == "win" else 1 if outcome == "draw" else 0
        score = affirmative_score if side == "aff" else negative_score
        item = aggregates.setdefault(
            user_id,
            {"points": 0, "wins": 0, "draws": 0, "losses": 0, "matches": 0, "score_total": 0.0, "last": None},
        )
        item["points"] += points
        item["wins"] += int(outcome == "win")
        item["draws"] += int(outcome == "draw")
        item["losses"] += int(outcome == "loss")
        item["matches"] += 1
        item["score_total"] += float(score)
        if completed_at and (item["last"] is None or completed_at > item["last"]):
            item["last"] = completed_at
    for user_id, item in aggregates.items():
        db.add(
            LeaderboardEntry(
                competition_id=competition_id,
                season_id=season_id,
                user_id=user_id,
                points=item["points"],
                wins=item["wins"],
                draws=item["draws"],
                losses=item["losses"],
                matches=item["matches"],
                average_score=item["score_total"] / item["matches"],
                last_match_at=item["last"],
            )
        )
