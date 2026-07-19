from __future__ import annotations

import asyncio
import hashlib
import json
import re
import subprocess
import uuid
import wave
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, UploadFile
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import acquire_transaction_locks, get_db
from app.core.deps import authenticated_session, current_user, optional_user, verify_csrf
from app.models.entities import (
    Competition,
    CompetitionTopic,
    JudgeProfile,
    JudgeScorecard,
    Match,
    MatchEvent,
    RatingChange,
    Room,
    RoomSeat,
    Season,
    SeatRestoreRequest,
    Speech,
    TranscriptSegment,
    User,
    UserSession,
)
from app.schemas.requests import ControlLeaseRequest, ControlRequest, CreateRoomRequest, FinishSpeechRequest, ReadyRequest, SeatRequest
from app.services.livekit_audio import (
    create_livekit_token,
    livekit_audio_enabled,
    livekit_room_name,
)
from app.services.match_archive import enqueue_match_archive
from app.services.match_engine import match_engine
from app.services.provider_config import build_service_snapshot
from app.services.realtime import room_hub
from app.services.room_service import (
    active_speech,
    anonymous_event_payload,
    append_event,
    can_control,
    can_view_room,
    choose_agent_profiles,
    free_turn_deadline,
    free_turn_remaining_seconds,
    load_room,
    now,
    public_event_payload,
    remaining_seconds,
    room_code,
    seat_keys,
    serialize_room,
    serialize_scorecard,
    speaking_permission,
    stage,
    use_public_projection,
    user_seat,
)
from app.services.seasons import season_is_open
from app.services.seat_restore import (
    cancel_restore_request,
    create_restore_request,
    expire_pending_restore_requests,
    review_restore_request,
    serialize_restore_request,
)
from app.services.speech_quality import PcmVoiceActivity, normalize_transcript, transcript_rejection_reason

router = APIRouter(prefix="/api/rooms", tags=["rooms"])
MAX_AUDIO_BYTES = 50 * 1024 * 1024
MIN_AUDIO_BYTES = 128
MAX_AUDIO_VAD_WINDOWS = 64
AUDIO_VAD_WINDOW_FRAMES = 8192
DECODED_AUDIO_SAMPLE_RATE = 16_000
MAX_DECODED_AUDIO_SECONDS = 600
MEDIA_DECODE_TIMEOUT_SECONDS = 60
ACTIVE_PARTICIPANT_STATUSES = {"lobby", "preparing", "running", "paused", "judging"}
_AUDIO_VALIDATION_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="audio-validation")
_RTC_DEVICE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _lock_participants(db: Session, user_ids: list[str]) -> None:
    if user_ids:
        normalized = sorted(set(user_ids))
        acquire_transaction_locks(db, *(f"1:user:{user_id}" for user_id in normalized))
        list(db.scalars(select(User.id).where(User.id.in_(normalized)).order_by(User.id).with_for_update()).all())


def _active_human_assignment(db: Session, user_id: str, *, exclude_room_id: str | None = None):
    statement = (
        select(Room.code, Room.status, RoomSeat.seat_key)
        .join(RoomSeat, RoomSeat.room_id == Room.id)
        .where(
            RoomSeat.user_id == user_id,
            RoomSeat.occupant_type == "human",
            Room.status.in_(ACTIVE_PARTICIPANT_STATUSES),
        )
        .order_by(Room.updated_at.desc())
        .limit(1)
    )
    if exclude_room_id:
        statement = statement.where(Room.id != exclude_room_id)
    return db.execute(statement).first()


def _assert_available_for_room(db: Session, user_id: str, *, exclude_room_id: str | None = None) -> None:
    assignment = _active_human_assignment(db, user_id, exclude_room_id=exclude_room_id)
    if assignment:
        raise HTTPException(
            status_code=409,
            detail=f"你已在房间 #{assignment.code} 参赛，请先返回该比赛或释放席位。",
        )


def _operation_key(action: str, room: Room, user: User, provided: str | None) -> str | None:
    if not provided:
        return None
    digest = hashlib.sha256(f"{action}:{room.id}:{user.id}:{provided[:128]}".encode()).hexdigest()
    return f"{action}:{digest}"


def _lease_fingerprint(lease: str) -> str:
    return hashlib.sha256(lease.encode()).hexdigest()[:16]


def _require_lobby_device_control(seat: RoomSeat, auth_session: UserSession) -> None:
    """Bind legacy lobby actions to one login session and reject stale devices."""
    if seat.control_session_id and seat.control_session_id != auth_session.id:
        raise HTTPException(status_code=409, detail="该席位已由另一设备控制，如需切换请确认接管。")
    if not seat.control_session_id:
        seat.control_session_id = auth_session.id


def _creation_identity(user: User, payload: CreateRoomRequest, provided: str | None) -> tuple[str | None, str | None]:
    if not provided:
        return None, None
    supplied = provided[:128]
    key_digest = hashlib.sha256(f"room-create:{user.id}:{supplied}".encode()).hexdigest()
    canonical = json.dumps(payload.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"room-create:{key_digest}", hashlib.sha256(canonical.encode()).hexdigest()


def _rematch_identity(user: User, source_room: Room, provided: str | None) -> tuple[str | None, str | None]:
    if not provided:
        return None, None
    supplied = provided[:128]
    key_digest = hashlib.sha256(f"room-rematch:{source_room.id}:{user.id}:{supplied}".encode()).hexdigest()
    fingerprint = hashlib.sha256(f"{source_room.id}:{user.id}".encode()).hexdigest()
    return f"room-rematch:{key_digest}", fingerprint


def _audio_suffix(header: bytes) -> str | None:
    if header.startswith(b"RIFF") and header[8:12] == b"WAVE":
        return ".wav"
    if header.startswith(b"\x1aE\xdf\xa3"):
        return ".webm"
    if header.startswith(b"OggS"):
        return ".ogg"
    if header.startswith(b"ID3") or header[:2] in {b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"}:
        return ".mp3"
    if len(header) >= 12 and header[4:8] == b"ftyp":
        return ".m4a"
    return None


def _validate_uploaded_audio(path: Path, suffix: str, size: int) -> float:
    if size < MIN_AUDIO_BYTES:
        raise HTTPException(status_code=422, detail="音频为空或过短，未保存该文件。")
    if suffix != ".wav":
        try:
            decoded = subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-nostdin",
                    "-i",
                    str(path),
                    "-map",
                    "0:a:0",
                    "-vn",
                    "-sn",
                    "-dn",
                    "-ac",
                    "1",
                    "-ar",
                    str(DECODED_AUDIO_SAMPLE_RATE),
                    "-t",
                    str(MAX_DECODED_AUDIO_SECONDS),
                    "-f",
                    "s16le",
                    "pipe:1",
                ],
                check=False,
                capture_output=True,
                timeout=MEDIA_DECODE_TIMEOUT_SECONDS,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise HTTPException(status_code=422, detail="音频无法解码，未保存该文件。") from exc
        pcm = decoded.stdout[: len(decoded.stdout) - (len(decoded.stdout) % 2)]
        if decoded.returncode != 0 or not pcm:
            raise HTTPException(status_code=422, detail="音频无法解码，未保存该文件。")
        activity = PcmVoiceActivity()
        frame_count = len(pcm) // 2
        if frame_count <= AUDIO_VAD_WINDOW_FRAMES * MAX_AUDIO_VAD_WINDOWS:
            positions = range(0, frame_count, AUDIO_VAD_WINDOW_FRAMES)
        else:
            last_start = max(0, frame_count - AUDIO_VAD_WINDOW_FRAMES)
            positions = sorted({round(index * last_start / (MAX_AUDIO_VAD_WINDOWS - 1)) for index in range(MAX_AUDIO_VAD_WINDOWS)})
        for position in positions:
            start = position * 2
            activity.observe(pcm[start : start + AUDIO_VAD_WINDOW_FRAMES * 2])
            if activity.has_voice:
                break
        if not activity.has_voice:
            raise HTTPException(status_code=422, detail="音频中未检测到清晰语音，未保存该文件。")
        duration = frame_count / DECODED_AUDIO_SAMPLE_RATE
        return round(max(0, min(duration, MAX_DECODED_AUDIO_SECONDS)), 3)
    try:
        with wave.open(str(path), "rb") as source:
            frame_count = source.getnframes()
            frame_rate = source.getframerate()
            sample_width = source.getsampwidth()
            channels = source.getnchannels()
            if frame_count <= 0 or frame_rate <= 0:
                raise HTTPException(status_code=422, detail="音频没有可播放帧，未保存该文件。")
            if frame_count * channels * sample_width > size:
                raise HTTPException(status_code=422, detail="WAV 音频帧数据不完整，未保存该文件。")
            duration = frame_count / frame_rate
            if sample_width == 2 and channels >= 1:
                activity = PcmVoiceActivity()
                if frame_count <= AUDIO_VAD_WINDOW_FRAMES * MAX_AUDIO_VAD_WINDOWS:
                    positions = range(0, frame_count, AUDIO_VAD_WINDOW_FRAMES)
                else:
                    last_start = max(0, frame_count - AUDIO_VAD_WINDOW_FRAMES)
                    positions = sorted({round(index * last_start / (MAX_AUDIO_VAD_WINDOWS - 1)) for index in range(MAX_AUDIO_VAD_WINDOWS)})
                for position in positions:
                    source.setpos(position)
                    frames = source.readframes(AUDIO_VAD_WINDOW_FRAMES)
                    if not frames:
                        continue
                    activity.observe(frames)
                    if activity.has_voice:
                        break
                if not activity.has_voice:
                    raise HTTPException(status_code=422, detail="音频中未检测到清晰语音，未保存该文件。")
            return round(max(0, min(duration, 600)), 3)
    except HTTPException:
        raise
    except (OSError, wave.Error) as exc:
        raise HTTPException(status_code=422, detail="WAV 音频无法解码，未保存该文件。") from exc


async def _validate_uploaded_audio_off_loop(path: Path, suffix: str, size: int) -> float:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_AUDIO_VALIDATION_EXECUTOR, _validate_uploaded_audio, path, suffix, size)


def _synchronize_transcript_segments(db: Session, speech: Speech, content: str) -> None:
    existing = list(db.scalars(select(TranscriptSegment).where(TranscriptSegment.speech_id == speech.id)).all())
    if existing and normalize_transcript(speech.content) == content:
        return
    db.execute(delete(TranscriptSegment).where(TranscriptSegment.speech_id == speech.id))
    db.add(TranscriptSegment(speech_id=speech.id, text=content, is_final=True))


async def _publish(room: Room, event_type: str) -> None:
    await room_hub.publish(room.code, {"type": event_type, "room_code": room.code, "seq": room.seq})


@router.post("")
async def create_room(
    payload: CreateRoomRequest,
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    user: User = Depends(verify_csrf),
    db: Session = Depends(get_db),
) -> dict:
    acquire_transaction_locks(db, f"1:user:{user.id}")
    creation_key, creation_fingerprint = _creation_identity(user, payload, idempotency_key)
    if creation_key:
        existing = db.scalar(select(Room).where(Room.creation_key == creation_key))
        if existing:
            if existing.creation_fingerprint != creation_fingerprint:
                raise HTTPException(status_code=409, detail="同一个幂等键不能用于不同的建房参数。")
            return {"room": serialize_room(db, load_room(db, existing.code), user), "replayed": True}
    _lock_participants(db, [user.id])
    _assert_available_for_room(db, user.id)
    competition = db.scalar(select(Competition).where(Competition.slug == payload.competition_slug, Competition.is_active.is_(True)))
    if not competition:
        raise HTTPException(status_code=404, detail="赛事不存在或已停用。")
    season = db.get(Season, competition.season_id) if competition.season_id else None
    if competition.ranked and not season_is_open(season):
        raise HTTPException(status_code=409, detail="该积分赛事当前没有开放中的赛季，请稍后参赛。")
    valid_seats = {item[0] for item in seat_keys(competition)}
    if payload.seat_key not in valid_seats:
        raise HTTPException(status_code=422, detail="无效的席位。")
    if payload.custom_topic:
        if not competition.allow_custom_topic:
            raise HTTPException(status_code=422, detail="该赛事不允许自定义辩题。")
        topic = payload.custom_topic.strip()
    elif payload.topic_id:
        topic_row = db.scalar(
            select(CompetitionTopic).where(
                CompetitionTopic.id == payload.topic_id,
                CompetitionTopic.competition_id == competition.id,
                CompetitionTopic.is_active.is_(True),
            )
        )
        if not topic_row:
            raise HTTPException(status_code=422, detail="题目不存在。")
        topic = topic_row.title
    else:
        topic_row = db.scalar(
            select(CompetitionTopic)
            .where(CompetitionTopic.competition_id == competition.id, CompetitionTopic.is_active.is_(True))
            .order_by(CompetitionTopic.created_at)
            .limit(1)
        )
        if not topic_row:
            raise HTTPException(status_code=422, detail="该赛事还没有可用题目。")
        topic = topic_row.title

    room = Room(
        code=room_code(db),
        creation_key=creation_key,
        creation_fingerprint=creation_fingerprint,
        created_by_user_id=user.id,
        competition_id=competition.id,
        season_id=competition.season_id,
        owner_id=user.id,
        topic=topic,
        visibility=payload.visibility,
        is_test_data=user.is_test_account,
        template_snapshot=[dict(item) for item in competition.automation_template.stages],
    )
    db.add(room)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        existing = db.scalar(select(Room).where(Room.creation_key == creation_key)) if creation_key else None
        if existing and existing.creation_fingerprint == creation_fingerprint:
            return {"room": serialize_room(db, load_room(db, existing.code), user), "replayed": True}
        raise HTTPException(status_code=409, detail="房间创建冲突，请重试。") from exc
    for key, side, position in seat_keys(competition):
        claimed = key == payload.seat_key
        db.add(
            RoomSeat(
                room_id=room.id,
                seat_key=key,
                side=side,
                position=position,
                occupant_type="human" if claimed else "open",
                user_id=user.id if claimed else None,
                display_name=user.real_name if claimed else "待加入",
                connected=False,
                disconnected_at=now() if claimed else None,
            )
        )
    append_event(
        db, room, "room.created", {"competition": competition.slug, "topic": topic, "owner_seat": payload.seat_key}, actor_user_id=user.id
    )
    db.commit()
    room = load_room(db, room.code)
    await _publish(room, "room.created")
    return {"room": serialize_room(db, room, user, public=use_public_projection(db, room, user))}


@router.get("/search")
def search_room(code: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    room = load_room(db, code)
    if not can_view_room(db, room, user):
        raise HTTPException(status_code=403, detail="该房间为私密房间。")
    return {"room": serialize_room(db, room, user, public=use_public_projection(db, room, user))}


@router.post("/{code}/rematch")
async def rematch_room(
    code: str,
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    user: User = Depends(verify_csrf),
    db: Session = Depends(get_db),
) -> dict:
    source_room = load_room(db, code)
    if source_room.status not in {"completed", "review_required", "terminated"}:
        raise HTTPException(status_code=409, detail="当前比赛尚未结束，不能发起再次比赛。")
    source_seat = user_seat(source_room, user)
    if not source_seat or source_seat.occupant_type not in {"human", "ai_substitute"}:
        raise HTTPException(status_code=403, detail="只有本场真人参赛者可以发起再次比赛。")

    acquire_transaction_locks(db, f"1:user:{user.id}")
    creation_key, creation_fingerprint = _rematch_identity(user, source_room, idempotency_key)
    if creation_key:
        existing = db.scalar(select(Room).where(Room.creation_key == creation_key))
        if existing:
            if existing.creation_fingerprint != creation_fingerprint:
                raise HTTPException(status_code=409, detail="同一个幂等键不能用于不同的再次比赛请求。")
            return {"room": serialize_room(db, load_room(db, existing.code), user), "replayed": True}

    _lock_participants(db, [user.id])
    _assert_available_for_room(db, user.id)
    competition = db.scalar(
        select(Competition).where(Competition.id == source_room.competition_id, Competition.is_active.is_(True))
    )
    if not competition:
        raise HTTPException(status_code=409, detail="原赛事已停用，暂时不能再次比赛。")
    season = db.get(Season, competition.season_id) if competition.season_id else None
    if competition.ranked and not season_is_open(season):
        raise HTTPException(status_code=409, detail="该积分赛事当前没有开放中的赛季，暂时不能再次比赛。")
    if not competition.allow_custom_topic:
        active_topic = db.scalar(
            select(CompetitionTopic.id).where(
                CompetitionTopic.competition_id == competition.id,
                CompetitionTopic.title == source_room.topic,
                CompetitionTopic.is_active.is_(True),
            )
        )
        if not active_topic:
            raise HTTPException(status_code=409, detail="原辩题已从当前赛事题库下架，请从赛事大厅选择新题目。")
    valid_seats = {item[0] for item in seat_keys(competition)}
    if source_seat.seat_key not in valid_seats:
        raise HTTPException(status_code=409, detail="赛事席位规则已经变化，请从赛事大厅重新创建比赛。")

    room = Room(
        code=room_code(db),
        creation_key=creation_key,
        creation_fingerprint=creation_fingerprint,
        created_by_user_id=user.id,
        competition_id=competition.id,
        season_id=competition.season_id,
        owner_id=user.id,
        topic=source_room.topic,
        visibility=source_room.visibility,
        is_test_data=user.is_test_account,
        template_snapshot=[dict(item) for item in competition.automation_template.stages],
    )
    db.add(room)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        existing = db.scalar(select(Room).where(Room.creation_key == creation_key)) if creation_key else None
        if existing and existing.creation_fingerprint == creation_fingerprint:
            return {"room": serialize_room(db, load_room(db, existing.code), user), "replayed": True}
        raise HTTPException(status_code=409, detail="再次比赛创建冲突，请重试。") from exc

    for key, side, position in seat_keys(competition):
        claimed = key == source_seat.seat_key
        db.add(
            RoomSeat(
                room_id=room.id,
                seat_key=key,
                side=side,
                position=position,
                occupant_type="human" if claimed else "open",
                user_id=user.id if claimed else None,
                display_name=user.real_name if claimed else "待加入",
                connected=False,
                disconnected_at=now() if claimed else None,
            )
        )
    append_event(
        db,
        room,
        "room.created",
        {
            "competition": competition.slug,
            "topic": source_room.topic,
            "owner_seat": source_seat.seat_key,
            "rematch_of": source_room.code,
        },
        actor_user_id=user.id,
    )
    db.commit()
    room = load_room(db, room.code)
    await _publish(room, "room.created")
    return {"room": serialize_room(db, room, user), "replayed": False}


@router.get("/{code}")
def get_room(code: str, user: User | None = Depends(optional_user), db: Session = Depends(get_db)) -> dict:
    room = load_room(db, code)
    if not can_view_room(db, room, user):
        raise HTTPException(status_code=401 if not user else 403, detail="无权查看该房间。")
    return {"room": serialize_room(db, room, user, public=use_public_projection(db, room, user))}


@router.post("/{code}/rtc-token")
async def rtc_token(
    code: str,
    device_id: str | None = Header(default=None, alias="X-Device-ID"),
    user: User | None = Depends(optional_user),
    db: Session = Depends(get_db),
) -> dict:
    room = load_room(db, code)
    if not can_view_room(db, room, user):
        raise HTTPException(status_code=401 if not user else 403, detail="无权查看该房间。")
    if not livekit_audio_enabled():
        return {
            "enabled": False,
            "backend": settings.webrtc_audio_backend,
            "url": None,
            "token": None,
            "room_name": None,
            "expires_in": 0,
        }
    normalized_device_id = device_id.strip() if device_id else uuid.uuid4().hex
    if not _RTC_DEVICE_ID.fullmatch(normalized_device_id):
        raise HTTPException(status_code=422, detail="设备标识格式无效。")
    identity_prefix = f"user:{user.id}" if user else f"guest:{uuid.uuid4().hex}"
    identity = f"{identity_prefix}:device:{normalized_device_id}"
    room_name = livekit_room_name(room.code)
    try:
        token = create_livekit_token(
            identity=identity,
            room_name=room_name,
            can_publish=False,
            can_subscribe=True,
            ttl_seconds=settings.livekit_token_ttl_seconds,
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail="实时音频连接暂不可用。") from exc
    return {
        "enabled": True,
        "backend": "livekit",
        "url": settings.livekit_browser_url,
        "token": token,
        "room_name": room_name,
        "expires_in": settings.livekit_token_ttl_seconds,
    }


@router.get("/{code}/result")
def room_result(
    code: str,
    event_page: int = Query(default=1, ge=1, le=100000),
    event_page_size: int = Query(default=100, ge=1, le=200),
    event_before_seq: int | None = Query(default=None, ge=1),
    user: User | None = Depends(optional_user),
    db: Session = Depends(get_db),
) -> dict:
    room = load_room(db, code)
    if not can_view_room(db, room, user):
        raise HTTPException(status_code=401 if not user else 403, detail="无权查看该房间。")
    match = db.scalar(select(Match).where(Match.room_id == room.id))
    if not match:
        raise HTTPException(status_code=404, detail="该房间还没有比赛记录。")
    scorecard = db.scalar(select(JudgeScorecard).where(JudgeScorecard.match_id == match.id))
    speeches = db.scalars(select(Speech).where(Speech.match_id == match.id).order_by(Speech.created_at, Speech.id)).all()
    stage_names = {str(item.get("key")): str(item.get("name") or item.get("key")) for item in room.template_snapshot or []}
    changes = db.scalars(
        select(RatingChange).where(RatingChange.match_id == match.id).order_by(RatingChange.created_at, RatingChange.id)
    ).all()
    rating_names = {
        item.id: item.real_name for item in db.scalars(select(User).where(User.id.in_({change.user_id for change in changes}))).all()
    }
    event_total = db.scalar(select(func.count(MatchEvent.id)).where(MatchEvent.room_id == room.id)) or 0
    event_query = select(MatchEvent).where(MatchEvent.room_id == room.id)
    if event_before_seq is not None:
        event_query = event_query.where(MatchEvent.seq < event_before_seq)
    else:
        event_query = event_query.offset((event_page - 1) * event_page_size)
    events = list(reversed(db.scalars(event_query.order_by(MatchEvent.seq.desc()).limit(event_page_size)).all()))
    next_before_seq = min((item.seq for item in events), default=None)
    has_more_events = bool(
        next_before_seq and db.scalar(select(MatchEvent.id).where(MatchEvent.room_id == room.id, MatchEvent.seq < next_before_seq).limit(1))
    )
    public_projection = use_public_projection(db, room, user)
    expose_internal_details = bool(user and user.role == "system_admin")
    return {
        "room": serialize_room(db, room, user, public=public_projection),
        "match": {"id": match.id, "status": match.status, "winner": match.winner, "reason": match.result_reason},
        "scorecard": serialize_scorecard(
            scorecard,
            expose_internal_details=expose_internal_details,
            allowed_seat_keys={seat.seat_key for seat in room.seats},
        ),
        "speeches": [
            {
                "id": item.id,
                "seat_key": item.seat_key,
                "stage_key": item.stage_key,
                "stage_name": stage_names.get(item.stage_key, item.stage_key),
                "speaker_type": item.speaker_type,
                "status": item.status,
                "content": item.content,
                "audio_url": item.audio_url,
                "duration_seconds": item.duration_seconds,
                "created_at": item.created_at.isoformat(),
            }
            for item in speeches
        ],
        "rating_changes": [
            ({"user_id": item.user_id} if not public_projection else {})
            | {
                "display_name": rating_names.get(item.user_id, "参赛选手"),
                "points_delta": item.points_delta,
                "score": item.score,
                "reason": item.reason,
                "source": item.source,
            }
            for item in changes
        ],
        "events": [
            {
                "seq": item.seq,
                "type": item.event_type,
                "payload": (
                    item.payload
                    if expose_internal_details
                    else anonymous_event_payload(item.event_type, item.payload)
                    if user is None
                    else public_event_payload(item.event_type, item.payload)
                ),
                "created_at": item.created_at.isoformat(),
            }
            for item in events
        ],
        "event_pagination": {
            "page": event_page,
            "page_size": event_page_size,
            "total": event_total,
            "pages": max(1, (event_total + event_page_size - 1) // event_page_size),
            "next_before_seq": next_before_seq,
            "has_more": has_more_events,
        },
    }


@router.post("/{code}/claim-seat")
async def claim_seat(code: str, payload: SeatRequest, user: User = Depends(verify_csrf), db: Session = Depends(get_db)) -> dict:
    room = load_room(db, code, lock=True)
    if not can_view_room(db, room, user):
        raise HTTPException(status_code=403, detail="无权加入该房间。")
    if room.status != "lobby":
        raise HTTPException(status_code=409, detail="比赛开始后不能更换席位。")
    if room.competition.ranked and not season_is_open(room.season):
        raise HTTPException(status_code=409, detail="该积分房间绑定的赛季已关闭，不能再认领席位。")
    existing = user_seat(room, user)
    if existing:
        if existing.seat_key == payload.seat_key:
            return {"room": serialize_room(db, room, user), "replayed": True}
        raise HTTPException(status_code=409, detail=f"你已经认领了 {existing.seat_key}。")
    _lock_participants(db, [user.id])
    _assert_available_for_room(db, user.id, exclude_room_id=room.id)
    seat = next((item for item in room.seats if item.seat_key == payload.seat_key), None)
    if not seat:
        raise HTTPException(status_code=404, detail="席位不存在。")
    if seat.occupant_type != "open":
        raise HTTPException(status_code=409, detail="该席位已被占用。")
    seat.occupant_type = "human"
    seat.user_id = user.id
    seat.display_name = user.real_name
    seat.connected = False
    seat.disconnected_at = now()
    seat.is_ready = False
    if user.is_test_account:
        room.is_test_data = True
    append_event(db, room, "seat.claimed", {"seat_key": seat.seat_key, "real_name": user.real_name}, actor_user_id=user.id)
    db.commit()
    await _publish(room, "seat.claimed")
    return {"room": serialize_room(db, load_room(db, code), user)}


@router.post("/{code}/release-seat")
async def release_seat(
    code: str,
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    user: User = Depends(verify_csrf),
    auth_session: UserSession = Depends(authenticated_session),
    db: Session = Depends(get_db),
) -> dict:
    room = load_room(db, code, lock=True)
    if room.status != "lobby":
        raise HTTPException(status_code=409, detail="比赛开始后不能释放席位。")
    operation_key = _operation_key("seat-release", room, user, idempotency_key)
    if operation_key:
        previous = db.scalar(select(MatchEvent).where(MatchEvent.idempotency_key == operation_key))
        if previous:
            return {"room": serialize_room(db, room, user), "replayed": True}
    seat = user_seat(room, user)
    if not seat:
        raise HTTPException(status_code=404, detail="你没有认领席位。")
    _require_lobby_device_control(seat, auth_session)
    if room.owner_id == user.id:
        raise HTTPException(status_code=409, detail="房主不能释放席位，可终止房间。")
    _lock_participants(db, [user.id])
    key = seat.seat_key
    seat.occupant_type = "open"
    seat.user_id = None
    seat.display_name = "待加入"
    seat.is_ready = False
    seat.connected = False
    seat.disconnected_at = None
    seat.control_lease = ""
    seat.control_session_id = None
    seat.agent_profile_id = None
    append_event(db, room, "seat.released", {"seat_key": key}, actor_user_id=user.id, idempotency_key=operation_key)
    db.commit()
    await _publish(room, "seat.released")
    return {"room": serialize_room(db, load_room(db, code), user)}


@router.post("/{code}/abandon-seat")
async def abandon_started_seat(
    code: str,
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    user: User = Depends(verify_csrf),
    db: Session = Depends(get_db),
) -> dict:
    room = load_room(db, code, lock=True)
    operation_key = _operation_key("seat-abandon", room, user, idempotency_key)
    if operation_key:
        previous = db.scalar(select(MatchEvent).where(MatchEvent.idempotency_key == operation_key))
        if previous:
            return {"room": serialize_room(db, room, user), "replayed": True}
    if room.status not in {"preparing", "running", "paused", "judging"}:
        raise HTTPException(status_code=409, detail="仅已开始且未结束的比赛可以由 AI 接替席位。")
    seat = user_seat(room, user)
    if not seat or seat.occupant_type != "human":
        raise HTTPException(status_code=409, detail="你当前没有可退出的真人席位。")
    if room.owner_id == user.id:
        raise HTTPException(status_code=409, detail="房主需要继续管理比赛；如需结束请使用比赛控制。")
    active = db.scalar(
        select(Speech.id).where(
            Speech.room_id == room.id,
            Speech.seat_key == seat.seat_key,
            Speech.status.in_(["speaking", "synthesizing", "playing"]),
        )
    )
    if active:
        raise HTTPException(status_code=409, detail="当前发言尚未完成，请先结束并提交后再退出本场。")
    _lock_participants(db, [user.id])
    profiles = choose_agent_profiles(db, len(room.seats))
    profile = profiles[(max(1, seat.position) - 1) % len(profiles)] if profiles else None
    original_name = seat.display_name.removeprefix("AI 接替·")
    seat.occupant_type = "ai_substitute"
    seat.display_name = f"AI 接替·{original_name}"
    seat.agent_profile_id = profile.id if profile else None
    seat.is_ready = True
    seat.connected = False
    seat.disconnected_at = now()
    seat.control_lease = ""
    seat.control_session_id = None
    append_event(
        db,
        room,
        "seat.abandoned",
        {"seat_key": seat.seat_key, "user_id": user.id, "replacement": "ai_substitute"},
        actor_user_id=user.id,
        idempotency_key=operation_key,
    )
    db.commit()
    await _publish(room, "seat.abandoned")
    return {"room": serialize_room(db, load_room(db, code), user)}


@router.post("/{code}/seat-restore-requests")
async def request_seat_restore(
    code: str,
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    user: User = Depends(verify_csrf),
    db: Session = Depends(get_db),
) -> dict:
    room = load_room(db, code, lock=True)
    request, replayed = create_restore_request(db, room, user, idempotency_key)
    db.commit()
    loaded = load_room(db, code)
    refreshed = db.get(SeatRestoreRequest, request.id)
    await _publish(loaded, "seat.restore_requested")
    return {
        "request": serialize_restore_request(refreshed, loaded, refreshed.seat, user),
        "room": serialize_room(db, loaded, user),
        "replayed": replayed,
    }


@router.post("/{code}/seat-restore-requests/{request_id}/cancel")
async def cancel_seat_restore(
    code: str,
    request_id: str,
    user: User = Depends(verify_csrf),
    db: Session = Depends(get_db),
) -> dict:
    room = load_room(db, code, lock=True)
    request = db.get(SeatRestoreRequest, request_id)
    if not request:
        raise HTTPException(status_code=404, detail="恢复申请不存在。")
    replayed = cancel_restore_request(db, room, request, user)
    db.commit()
    loaded = load_room(db, code)
    await _publish(loaded, "seat.restore_cancelled")
    return {"room": serialize_room(db, loaded, user), "replayed": replayed}


@router.post("/{code}/seat-restore-requests/{request_id}/approve")
async def approve_seat_restore(
    code: str,
    request_id: str,
    payload: ControlRequest,
    user: User = Depends(verify_csrf),
    db: Session = Depends(get_db),
) -> dict:
    room = load_room(db, code, lock=True)
    request = db.get(SeatRestoreRequest, request_id)
    if not request:
        raise HTTPException(status_code=404, detail="恢复申请不存在。")
    replayed = review_restore_request(db, room, request, user, approve=True, reason=payload.reason)
    db.commit()
    loaded = load_room(db, code)
    await _publish(loaded, "seat.human_restored")
    return {"room": serialize_room(db, loaded, user), "replayed": replayed}


@router.post("/{code}/seat-restore-requests/{request_id}/reject")
async def reject_seat_restore(
    code: str,
    request_id: str,
    payload: ControlRequest,
    user: User = Depends(verify_csrf),
    db: Session = Depends(get_db),
) -> dict:
    room = load_room(db, code, lock=True)
    request = db.get(SeatRestoreRequest, request_id)
    if not request:
        raise HTTPException(status_code=404, detail="恢复申请不存在。")
    replayed = review_restore_request(db, room, request, user, approve=False, reason=payload.reason)
    db.commit()
    loaded = load_room(db, code)
    await _publish(loaded, "seat.restore_rejected")
    return {"room": serialize_room(db, loaded, user), "replayed": replayed}


@router.post("/{code}/ready")
async def ready(
    code: str,
    payload: ReadyRequest,
    user: User = Depends(verify_csrf),
    auth_session: UserSession = Depends(authenticated_session),
    db: Session = Depends(get_db),
) -> dict:
    room = load_room(db, code, lock=True)
    if room.status != "lobby":
        raise HTTPException(status_code=409, detail="比赛已经开始。")
    if payload.ready and room.competition.ranked and not season_is_open(room.season):
        raise HTTPException(status_code=409, detail="该积分房间绑定的赛季已关闭，不能再确认准备。")
    seat = user_seat(room, user)
    if not seat:
        raise HTTPException(status_code=403, detail="请先认领席位。")
    _require_lobby_device_control(seat, auth_session)
    if seat.is_ready == payload.ready:
        return {"room": serialize_room(db, room, user), "replayed": True}
    seat.is_ready = payload.ready
    append_event(db, room, "seat.ready_changed", {"seat_key": seat.seat_key, "ready": seat.is_ready}, actor_user_id=user.id)
    db.commit()
    await _publish(room, "seat.ready_changed")
    return {"room": serialize_room(db, load_room(db, code), user)}


@router.post("/{code}/cancel")
async def cancel_room(
    code: str,
    user: User = Depends(verify_csrf),
    auth_session: UserSession = Depends(authenticated_session),
    db: Session = Depends(get_db),
) -> dict:
    room = load_room(db, code, lock=True)
    if not can_control(db, room, user):
        raise HTTPException(status_code=403, detail="只有房主或系统管理员可以关闭房间。")
    if room.status == "cancelled":
        return {"room": serialize_room(db, room, user), "replayed": True}
    if room.status != "lobby":
        raise HTTPException(status_code=409, detail="比赛开始后不能关闭房间，请使用比赛控制台终止。")
    owner_seat = user_seat(room, user)
    if owner_seat and user.role != "system_admin":
        _require_lobby_device_control(owner_seat, auth_session)
    _lock_participants(db, [seat.user_id for seat in room.seats if seat.occupant_type == "human" and seat.user_id])
    room.status = "cancelled"
    room.completed_at = now()
    append_event(db, room, "room.cancelled", {}, actor_user_id=user.id, idempotency_key=f"{room.id}:cancel")
    db.commit()
    await _publish(room, "room.cancelled")
    return {"room": serialize_room(db, load_room(db, code), user)}


@router.post("/{code}/start")
async def start_room(
    code: str,
    user: User = Depends(verify_csrf),
    auth_session: UserSession = Depends(authenticated_session),
    db: Session = Depends(get_db),
) -> dict:
    room = load_room(db, code, lock=True)
    if not can_control(db, room, user):
        raise HTTPException(status_code=403, detail="只有房主或系统管理员可以开始比赛。")
    previous_start = db.scalar(select(MatchEvent).where(MatchEvent.idempotency_key == f"{room.id}:start"))
    if room.status != "lobby":
        if previous_start:
            return {"room": serialize_room(db, room, user), "replayed": True}
        raise HTTPException(status_code=409, detail="比赛已经开始。")
    owner_seat = user_seat(room, user)
    if owner_seat and user.role != "system_admin":
        _require_lobby_device_control(owner_seat, auth_session)
    humans = [seat for seat in room.seats if seat.occupant_type == "human"]
    _lock_participants(db, [seat.user_id for seat in humans if seat.user_id])
    for seat in humans:
        if seat.user_id:
            assignment = _active_human_assignment(db, seat.user_id, exclude_room_id=room.id)
            if assignment:
                raise HTTPException(
                    status_code=409,
                    detail=f"辩手 {seat.display_name} 同时占用房间 #{assignment.code}，请先释放冲突席位。",
                )
    if room.competition.ranked:
        if not room.season_id:
            raise HTTPException(status_code=409, detail="该积分房间缺少赛季快照，不能开始比赛。")
        if not season_is_open(room.season):
            raise HTTPException(status_code=409, detail="该房间绑定的赛季已关闭，请重新创建房间。")
    not_ready = [seat.display_name for seat in humans if not seat.is_ready]
    if not_ready:
        raise HTTPException(status_code=409, detail=f"以下辩手尚未准备：{'、'.join(not_ready)}")
    open_seats = [seat for seat in room.seats if seat.occupant_type == "open"]
    profiles = choose_agent_profiles(db, len(open_seats))
    for index, seat in enumerate(open_seats):
        profile = profiles[index] if profiles else None
        seat.occupant_type = "ai"
        seat.display_name = profile.name if profile else f"AI 辩手 {index + 1}"
        seat.agent_profile_id = profile.id if profile else None
        seat.is_ready = True
        seat.connected = True
    judge_profile = db.scalar(
        select(JudgeProfile).where(JudgeProfile.is_active.is_(True)).order_by(JudgeProfile.created_at, JudgeProfile.id).with_for_update()
    )
    judge_snapshot = (
        {
            "id": judge_profile.id,
            "name": judge_profile.name,
            "endpoint": judge_profile.endpoint,
            "model_name": judge_profile.model_name,
            "system_prompt": judge_profile.system_prompt,
            "timeout_seconds": judge_profile.timeout_seconds,
        }
        if judge_profile
        else (
            {
                "name": "环境变量裁判",
                "endpoint": settings.judge_api_url,
                "model_name": "",
                "system_prompt": "",
                "timeout_seconds": 120,
            }
            if settings.judge_api_url
            else {}
        )
    )
    service_snapshot = build_service_snapshot(db)
    match = Match(
        room_id=room.id,
        competition_id=room.competition_id,
        season_id=room.season_id,
        status="running",
        judge_profile_id=judge_profile.id if judge_profile else None,
        judge_snapshot=judge_snapshot,
        service_snapshot=service_snapshot,
    )
    db.add(match)
    room.status = "preparing"
    room.started_at = now()
    append_event(
        db,
        room,
        "room.locked",
        {
            "ai_filled": len(open_seats),
            "judge_configured": bool(judge_snapshot),
            "services": {kind: bool(item.get("enabled")) for kind, item in service_snapshot.items()},
        },
        actor_user_id=user.id,
        idempotency_key=f"{room.id}:start",
    )
    db.commit()
    await _publish(room, "room.locked")
    return {"room": serialize_room(db, load_room(db, code), user)}


@router.post("/{code}/control-lease")
async def acquire_control_lease(
    code: str,
    payload: ControlLeaseRequest | None = None,
    control_lease: str = Header(alias="X-Control-Lease"),
    user: User = Depends(verify_csrf),
    auth_session: UserSession = Depends(authenticated_session),
    db: Session = Depends(get_db),
) -> dict:
    lease = control_lease.strip()
    force = bool(payload and payload.force)
    if not lease or len(lease) > 64:
        raise HTTPException(status_code=422, detail="设备控制标识无效。")
    room = load_room(db, code, lock=True)
    seat = user_seat(room, user)
    if not seat:
        raise HTTPException(status_code=403, detail="你不是本房间辩手。")
    if seat.occupant_type != "human":
        raise HTTPException(status_code=409, detail="该席位已由 AI 接替，当前只能观战。")
    if room.status in {"review_required", "completed", "terminated", "cancelled"}:
        raise HTTPException(status_code=409, detail="比赛已经结束，无需接管席位。")
    fingerprint = _lease_fingerprint(lease)
    same_session = bool(
        seat.control_lease
        and seat.control_session_id
        and seat.control_session_id == auth_session.id
    )
    if seat.control_lease == lease and (same_session or not seat.control_session_id):
        if not seat.control_session_id:
            seat.control_session_id = auth_session.id
            db.commit()
        return {
            "ok": True,
            "lease_fingerprint": fingerprint,
            "seq": room.seq,
            "replayed": True,
            "same_session_recovery": same_session,
        }
    active = active_speech(db, room)
    if active and active.seat_key == seat.seat_key and seat.control_lease and not same_session:
        raise HTTPException(status_code=409, detail="该席位正在另一设备发言，结束后才能接管。")
    if seat.control_lease and not force and not same_session:
        raise HTTPException(status_code=409, detail="该席位已由另一设备控制，如需切换请确认接管。")
    event_type = "seat.control_taken_over" if seat.control_lease else "seat.control_acquired"
    seat.control_lease = lease
    seat.control_session_id = auth_session.id
    append_event(
        db,
        room,
        event_type,
        {
            "seat_key": seat.seat_key,
            "lease_fingerprint": fingerprint,
            "forced": force,
            "same_session_recovery": same_session,
        },
        actor_user_id=user.id,
    )
    db.commit()
    await _publish(room, event_type)
    return {
        "ok": True,
        "lease_fingerprint": fingerprint,
        "seq": room.seq,
        "replayed": False,
        "same_session_recovery": same_session,
    }


@router.post("/{code}/speech/start")
async def start_speech(
    code: str,
    control_lease: str | None = Header(default=None, alias="X-Control-Lease"),
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    user: User = Depends(verify_csrf),
    db: Session = Depends(get_db),
) -> dict:
    room = load_room(db, code, lock=True)
    operation_key = _operation_key("speech-start", room, user, idempotency_key)
    if operation_key:
        previous = db.scalar(select(MatchEvent).where(MatchEvent.idempotency_key == operation_key))
        if previous:
            return {
                "speech_id": previous.payload.get("speech_id"),
                "room": serialize_room(db, room, user),
                "replayed": True,
            }
    seat = user_seat(room, user)
    existing_active = active_speech(db, room)
    if existing_active and seat and existing_active.seat_key == seat.seat_key and existing_active.speaker_type == "human":
        if not control_lease or not seat.control_lease or seat.control_lease != control_lease:
            raise HTTPException(status_code=409, detail="该席位已在其他设备上接管。")
        return {
            "speech_id": existing_active.id,
            "room": serialize_room(db, room, user),
            "resumed": True,
        }
    allowed, reason = speaking_permission(room, user)
    if not allowed:
        raise HTTPException(status_code=403, detail=reason)
    current = stage(room)
    match = db.scalar(select(Match).where(Match.room_id == room.id))
    if not match or not seat or not current:
        raise HTTPException(status_code=409, detail="比赛状态不完整。")
    existing = db.scalar(select(Speech).where(Speech.match_id == match.id, Speech.status.in_(["speaking", "synthesizing", "playing"])))
    if existing:
        raise HTTPException(status_code=409, detail="已有辩手正在发言。")
    if not control_lease or not seat.control_lease or seat.control_lease != control_lease:
        raise HTTPException(status_code=409, detail="该席位尚未绑定当前设备，或已被其他设备接管。")
    speech = Speech(
        match_id=match.id,
        room_id=room.id,
        seat_key=seat.seat_key,
        stage_key=current["key"],
        speaker_type="human",
        status="speaking",
    )
    db.add(speech)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="已有辩手正在发言。") from exc
    append_event(
        db,
        room,
        "speech.started",
        {"speech_id": speech.id, "seat_key": seat.seat_key, "speaker_type": "human"},
        actor_user_id=user.id,
        idempotency_key=operation_key,
    )
    db.commit()
    await _publish(room, "speech.started")
    return {"speech_id": speech.id, "room": serialize_room(db, load_room(db, code), user)}


@router.post("/{code}/speech/finish")
async def finish_speech(
    code: str,
    payload: FinishSpeechRequest,
    control_lease: str | None = Header(default=None, alias="X-Control-Lease"),
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    user: User = Depends(verify_csrf),
    db: Session = Depends(get_db),
) -> dict:
    room = load_room(db, code, lock=True)
    seat = user_seat(room, user)
    if not seat:
        raise HTTPException(status_code=403, detail="你不是本房间辩手。")
    normalized_content = normalize_transcript(payload.content)
    if reason := transcript_rejection_reason(normalized_content, require_substantive=True):
        messages = {
            "empty": "发言文字不能为空，请补充本次有效发言。",
            "too_short": "发言文字过短，请核对并补充本次有效发言。",
            "no_speech_characters": "发言文字缺少可识别内容，请重新填写。",
            "control_characters": "发言文字包含无效控制字符，请重新填写。",
            "excessive_symbols": "发言文字包含过多无效符号，请重新填写。",
            "repeated_characters": "发言文字疑似无效重复内容，请重新填写。",
        }
        raise HTTPException(status_code=422, detail=messages.get(reason, "发言文字未通过质量检查，请重新填写。"))
    operation_key = _operation_key("speech-finish", room, user, idempotency_key)
    if operation_key:
        previous = db.scalar(select(MatchEvent).where(MatchEvent.idempotency_key == operation_key))
        if previous:
            if previous.payload.get("speech_id") != payload.speech_id or previous.payload.get("content", "") != normalized_content:
                raise HTTPException(status_code=409, detail="同一个幂等键不能用于不同的发言内容。")
            return {"room": serialize_room(db, room, user), "speech_id": previous.payload.get("speech_id"), "replayed": True}
    if not control_lease or not seat.control_lease or control_lease != seat.control_lease:
        raise HTTPException(status_code=409, detail="该席位已在其他设备上接管。")
    match = db.scalar(select(Match).where(Match.room_id == room.id))
    speech = db.get(Speech, payload.speech_id) if match else None
    if not speech or speech.match_id != match.id or speech.room_id != room.id or speech.seat_key != seat.seat_key:
        raise HTTPException(status_code=409, detail="发言标识与当前房间、席位或比赛不匹配。")
    if speech.status == "timed_out":
        _synchronize_transcript_segments(db, speech, normalized_content)
        speech.content = normalized_content
        speech.status = "completed"
        append_event(
            db,
            room,
            "speech.late_finalized",
            {"speech_id": speech.id, "seat_key": seat.seat_key, "content": speech.content},
            actor_user_id=user.id,
            idempotency_key=operation_key,
        )
        db.commit()
        await _publish(room, "speech.late_finalized")
        return {
            "room": serialize_room(db, load_room(db, code), user),
            "speech_id": speech.id,
            "timed_out": True,
        }
    if speech.status != "speaking":
        raise HTTPException(status_code=409, detail="指定发言当前不能完成或已被处理。")
    _synchronize_transcript_segments(db, speech, normalized_content)
    speech.content = normalized_content
    speech.status = "completed"
    append_event(
        db,
        room,
        "speech.completed",
        {"speech_id": speech.id, "seat_key": seat.seat_key, "content": speech.content},
        actor_user_id=user.id,
        idempotency_key=operation_key,
    )
    current = stage(room)
    match_engine._advance(db, room, reason="free_turn_completed" if current and current.get("kind") == "free" else "speech_completed")
    db.commit()
    await _publish(room, "speech.completed")
    return {"room": serialize_room(db, load_room(db, code), user), "speech_id": speech.id}


@router.post("/{code}/speech/{speech_id}/audio")
async def upload_speech_audio(
    code: str,
    speech_id: str,
    audio: UploadFile = File(...),
    user: User = Depends(verify_csrf),
    db: Session = Depends(get_db),
) -> dict:
    room = load_room(db, code, lock=True)
    seat = user_seat(room, user)
    speech = db.get(Speech, speech_id)
    if not seat or not speech or speech.room_id != room.id or speech.seat_key != seat.seat_key:
        raise HTTPException(status_code=403, detail="无权上传该发言音频。")
    if speech.speaker_type != "human" or speech.status != "completed":
        raise HTTPException(status_code=409, detail="只有已完成的真人发言可以上传音频。")
    if speech.audio_url:
        return {"audio_url": speech.audio_url, "replayed": True}
    room_id = room.id
    room_code_value = room.code
    seat_key = speech.seat_key
    # Release the room row lock and database connection before reading a file
    # that may be as large as 50 MiB.  Holding it across ``await audio.read`` can
    # exhaust a synchronous SQLAlchemy pool and freeze all API requests once
    # enough rooms upload concurrently.
    db.commit()
    # The ASGI upload middleware already holds the per-speech cross-process
    # lease before multipart parsing begins. Recheck authorization and
    # idempotency after releasing the initial room transaction.
    db.expire_all()
    room = load_room(db, code, lock=True)
    seat = user_seat(room, user)
    speech = db.get(Speech, speech_id)
    if (
        room.id != room_id
        or not seat
        or not speech
        or speech.room_id != room.id
        or speech.seat_key != seat_key
        or speech.seat_key != seat.seat_key
    ):
        raise HTTPException(status_code=403, detail="无权上传该发言音频。")
    if speech.speaker_type != "human" or speech.status != "completed":
        raise HTTPException(status_code=409, detail="只有已完成的真人发言可以上传音频。")
    if speech.audio_url:
        db.rollback()
        return {"audio_url": speech.audio_url, "replayed": True}
    db.commit()

    first_chunk = await audio.read(1024 * 1024)
    if not first_chunk:
        raise HTTPException(status_code=422, detail="音频为空，未保存该文件。")
    suffix = _audio_suffix(first_chunk[:16])
    if not suffix:
        raise HTTPException(status_code=415, detail="无法识别音频格式，请上传 WAV、WebM、MP3、Ogg 或 M4A。")
    target_dir = settings.media_path / room_code_value
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{speech.id}{suffix}"
    temporary = target_dir / f".{speech.id}.{uuid.uuid4().hex}.part"
    size = 0
    try:
        with temporary.open("wb") as output:
            chunk = first_chunk
            while chunk:
                size += len(chunk)
                if size > MAX_AUDIO_BYTES:
                    raise HTTPException(status_code=413, detail="音频文件不能超过 50 MiB。")
                output.write(chunk)
                chunk = await audio.read(1024 * 1024)
        duration_seconds = await _validate_uploaded_audio_off_loop(temporary, suffix, size)

        db.expire_all()
        room = load_room(db, code, lock=True)
        seat = user_seat(room, user)
        speech = db.get(Speech, speech_id)
        if (
            room.id != room_id
            or not seat
            or not speech
            or speech.room_id != room.id
            or speech.seat_key != seat_key
            or speech.seat_key != seat.seat_key
        ):
            raise HTTPException(status_code=403, detail="无权上传该发言音频。")
        if speech.speaker_type != "human" or speech.status != "completed":
            raise HTTPException(status_code=409, detail="只有已完成的真人发言可以上传音频。")
        if speech.audio_url:
            db.rollback()
            return {"audio_url": speech.audio_url, "replayed": True}

        temporary.replace(target)
        speech.audio_url = f"/media/{room.code}/{target.name}"
        if duration_seconds > 0:
            speech.duration_seconds = duration_seconds
        append_event(
            db,
            room,
            "speech.audio.ready",
            {
                "speech_id": speech.id,
                "seat_key": speech.seat_key,
                "audio_url": speech.audio_url,
                "duration_seconds": speech.duration_seconds,
            },
            actor_user_id=user.id,
        )
        db.commit()
        await _publish(room, "speech.audio.ready")
        return {"audio_url": speech.audio_url}
    finally:
        temporary.unlink(missing_ok=True)


@router.post("/{code}/control/{action}")
async def control(
    code: str,
    action: str,
    payload: ControlRequest,
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    user: User = Depends(verify_csrf),
    db: Session = Depends(get_db),
) -> dict:
    room = load_room(db, code, lock=True)
    if not can_control(db, room, user):
        raise HTTPException(status_code=403, detail="只有房主或系统管理员可以控制比赛。")
    operation_key = _operation_key(f"control-{action}", room, user, idempotency_key)
    if operation_key:
        previous = db.scalar(select(MatchEvent).where(MatchEvent.idempotency_key == operation_key))
        if previous:
            if previous.payload.get("reason", "") != payload.reason:
                raise HTTPException(status_code=409, detail="同一个幂等键不能用于不同的控制参数。")
            return {"room": serialize_room(db, room, user), "replayed": True}
    archive_match_id: str | None = None
    if action == "pause":
        if room.status not in {"running", "judging"}:
            raise HTTPException(status_code=409, detail="当前状态不能暂停。")
        speaking_human = db.scalar(
            select(Speech.id).where(
                Speech.room_id == room.id,
                Speech.speaker_type == "human",
                Speech.status == "speaking",
            )
        )
        if speaking_human:
            raise HTTPException(status_code=409, detail="真人正在发言，请先结束发言再暂停比赛。")
        current = stage(room)
        stage_remaining = remaining_seconds(room)
        turn_remaining = free_turn_remaining_seconds(room, current) if current and current.get("kind") == "free" else None
        match_engine.interrupt_inflight_ai_speeches(db, room, reason="manual_pause")
        match_engine.interrupt_inflight_judging(db, room, reason="manual_pause")
        room.paused_remaining_seconds = 1 if stage_remaining is None else stage_remaining
        current = stage(room)
        updated_current = dict(current) if current else None
        if updated_current and updated_current.get("kind") == "free":
            updated_current["paused_turn_remaining_seconds"] = turn_remaining or 0
        playing = db.scalar(select(Speech).where(Speech.room_id == room.id, Speech.status == "playing"))
        if updated_current and playing and playing.playback_started_at:
            playback_started_at = playing.playback_started_at
            if playback_started_at.tzinfo is None:
                playback_started_at = playback_started_at.replace(tzinfo=now().tzinfo)
            updated_current["paused_playback_elapsed_seconds"] = round(
                max(0.0, min(playing.duration_seconds, (now() - playback_started_at).total_seconds())),
                3,
            )
        if updated_current is not None and updated_current != current:
            snapshot = list(room.template_snapshot)
            snapshot[room.current_stage_index] = updated_current
            room.template_snapshot = snapshot
        room.status = "paused"
        room.stage_deadline_at = None
    elif action == "resume":
        if room.status != "paused":
            raise HTTPException(status_code=409, detail="比赛未暂停。")
        failed_speech = db.scalar(select(Speech.id).where(Speech.room_id == room.id, Speech.status == "failed"))
        if room.failure_reason or failed_speech:
            raise HTTPException(status_code=409, detail="当前因服务异常暂停，请使用重试当前步骤。")
        resumed_at = now()
        room.status = "running"
        resume_seconds = room.paused_remaining_seconds if room.paused_remaining_seconds is not None else 1
        room.stage_deadline_at = resumed_at + timedelta(seconds=max(0, resume_seconds))
        current = stage(room)
        if current and current.get("kind") == "free":
            updated_current = dict(current)
            paused_turn_remaining = max(
                0,
                min(
                    int(updated_current.pop("paused_turn_remaining_seconds", updated_current.get("turn_duration", 45))),
                    max(1, int(updated_current.get("turn_duration", 45))),
                ),
            )
            elapsed = max(0, int(updated_current.get("turn_duration", 45)) - paused_turn_remaining)
            updated_current["turn_started_at"] = (resumed_at - timedelta(seconds=elapsed)).isoformat()
            snapshot = list(room.template_snapshot)
            snapshot[room.current_stage_index] = updated_current
            room.template_snapshot = snapshot
            current = updated_current
        playing = db.scalar(select(Speech).where(Speech.room_id == room.id, Speech.status == "playing"))
        if playing:
            playback_elapsed = float(current.get("paused_playback_elapsed_seconds", 0)) if current else 0.0
            playback_elapsed = max(0.0, min(playing.duration_seconds, playback_elapsed))
            playback_started_at = resumed_at - timedelta(seconds=playback_elapsed)
            playing.playback_started_at = playback_started_at
            playback_ends_at = resumed_at + timedelta(seconds=max(0.1, playing.duration_seconds - playback_elapsed))
            if current and current.get("kind") == "free":
                turn_deadline = free_turn_deadline(room, current)
                if turn_deadline:
                    playback_ends_at = min(playback_ends_at, turn_deadline)
            else:
                room.stage_deadline_at = playback_ends_at
            playing.playback_ends_at = playback_ends_at
        if current and "paused_playback_elapsed_seconds" in current:
            updated_current = dict(current)
            updated_current.pop("paused_playback_elapsed_seconds", None)
            snapshot = list(room.template_snapshot)
            snapshot[room.current_stage_index] = updated_current
            room.template_snapshot = snapshot
        room.paused_remaining_seconds = None
        room.failure_reason = ""
    elif action == "skip":
        if room.status not in {"running", "paused", "judging"}:
            raise HTTPException(status_code=409, detail="当前状态不能跳过。")
        current = stage(room)
        if room.status == "judging" or (current and current.get("kind") == "judging"):
            match = db.scalar(select(Match).where(Match.room_id == room.id))
            if not match:
                raise HTTPException(status_code=409, detail="比赛记录不存在，无法进入人工复核。")
            scorecard = db.scalar(select(JudgeScorecard).where(JudgeScorecard.match_id == match.id))
            if not scorecard:
                scorecard = JudgeScorecard(match_id=match.id, status="review_required", reasoning=payload.reason or "管理员跳过自动裁判。")
                db.add(scorecard)
                db.flush()
            else:
                scorecard.status = "review_required"
                scorecard.reasoning = payload.reason or "管理员跳过自动裁判。"
            match.status = "review_required"
            archive_match_id = match.id
            room.status = "review_required"
            room.completed_at = now()
            room.stage_deadline_at = None
            room.paused_remaining_seconds = None
            room.failure_reason = scorecard.reasoning
            expire_pending_restore_requests(db, room, "比赛已结束并进入人工复核")
            append_event(
                db,
                room,
                "judge.review_required",
                {"message": scorecard.reasoning, "scorecard_id": scorecard.id},
                actor_user_id=user.id,
            )
        else:
            match_engine.close_active_speeches(db, room, status="interrupted", reason="manual_skip")
            match_engine._advance(db, room, reason="manual_skip")
            room.failure_reason = ""
    elif action == "retry":
        if room.status != "paused":
            raise HTTPException(status_code=409, detail="只有异常暂停状态可以重试。")
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        if settings.app_env == "production" and match and not match.service_snapshot:
            raise HTTPException(
                status_code=409,
                detail="该旧比赛缺少服务配置快照，不能安全重试；请结束本场并创建新比赛。",
            )
        failed = db.scalars(select(Speech).where(Speech.room_id == room.id, Speech.status == "failed")).all()
        if not room.failure_reason and not failed:
            raise HTTPException(status_code=409, detail="当前是人工暂停，请使用恢复比赛。")
        for item in failed:
            item.status = "failed_retried"
        room.failure_reason = ""
        retried_at = now()
        if room.current_stage_index < 0:
            room.status = "preparing"
            room.stage_started_at = None
            room.stage_deadline_at = None
            room.paused_remaining_seconds = None
        else:
            room.status = "running"
            room.stage_deadline_at = retried_at + timedelta(seconds=room.paused_remaining_seconds or 30)
        current = stage(room)
        if current and current.get("kind") == "free":
            updated_current = dict(current)
            paused_turn_remaining = max(
                1,
                min(
                    int(updated_current.pop("paused_turn_remaining_seconds", updated_current.get("turn_duration", 45))),
                    max(1, int(updated_current.get("turn_duration", 45))),
                ),
            )
            elapsed = max(0, int(updated_current.get("turn_duration", 45)) - paused_turn_remaining)
            updated_current["turn_started_at"] = (retried_at - timedelta(seconds=elapsed)).isoformat()
            snapshot = list(room.template_snapshot)
            snapshot[room.current_stage_index] = updated_current
            room.template_snapshot = snapshot
        room.paused_remaining_seconds = None
    elif action == "terminate":
        if room.status not in {"preparing", "running", "paused", "judging"}:
            raise HTTPException(status_code=409, detail="当前状态不能终止比赛。")
        match_engine.close_active_speeches(db, room, status="interrupted", reason="match_terminated")
        room.status = "terminated"
        room.completed_at = now()
        room.stage_deadline_at = None
        room.paused_remaining_seconds = None
        expire_pending_restore_requests(db, room, "比赛已终止")
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        if match:
            match.status = "terminated"
            archive_match_id = match.id
    else:
        raise HTTPException(status_code=404, detail="未知控制操作。")
    append_event(
        db,
        room,
        f"control.{action}",
        {"reason": payload.reason},
        actor_user_id=user.id,
        idempotency_key=operation_key,
    )
    db.commit()
    if archive_match_id:
        enqueue_match_archive(archive_match_id)
    await _publish(room, f"control.{action}")
    return {"room": serialize_room(db, load_room(db, code), user)}
