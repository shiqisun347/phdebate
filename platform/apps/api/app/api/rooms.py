from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import acquire_transaction_locks, get_db
from app.core.deps import authenticated_session, current_user, optional_user, verify_csrf
from app.models.entities import (
    Competition,
    CompetitionTopic,
    FreeTurnRequest,
    JudgeProfile,
    JudgeScorecard,
    Match,
    MatchEvent,
    MatchParticipant,
    RatingChange,
    Room,
    RoomSeat,
    Season,
    Speech,
    SpeechCorrectionRequest,
    TranscriptSegment,
    User,
    UserSession,
    VoiceTelemetry,
)
from app.schemas.requests import (
    ControlLeaseRequest,
    ControlRequest,
    CreateRoomRequest,
    FinishSpeechRequest,
    ReadyRequest,
    SeatRequest,
    SpeechCorrectionCreate,
    VoiceTelemetryReport,
)
from app.services.free_turn_queue import (
    cancel_free_turn,
    expire_room_requests,
    intermission_remaining_ms,
    request_free_turn,
)
from app.services.livekit_audio import (
    create_livekit_token,
    livekit_audio_enabled,
    livekit_room_name,
)
from app.services.match_archive import enqueue_match_archive
from app.services.match_engine import match_engine
from app.services.provider_config import build_service_snapshot
from app.services.providers import moss_tts_realtime
from app.services.realtime import (
    SPECTATOR_TICKET_COOKIE,
    SpectatorAdmissionUnavailable,
    room_hub,
    spectator_connection_id,
    valid_spectator_ticket,
)
from app.services.room_capacity import ACTIVE_PARTICIPANT_STATUSES, MAX_ACTIVE_ROOMS
from app.services.room_service import (
    PUBLIC_MATCH_TIMELINE_EVENT_TYPES,
    active_speech,
    anonymous_event_payload,
    append_event,
    can_control,
    can_view_room,
    choose_agent_profiles,
    free_turn_deadline,
    free_turn_duration,
    free_turn_remaining_seconds,
    load_room,
    now,
    participant_disconnect_pause_context,
    public_event_payload,
    remaining_seconds,
    room_code,
    seat_keys,
    serialize_room,
    serialize_scorecard,
    speaking_permission,
    stage,
    transfer_room_owner,
    use_public_projection,
    user_seat,
)
from app.services.seasons import season_is_open
from app.services.speech_correction import (
    can_request_correction,
    cancel_correction_request,
    create_correction_request,
    serialize_correction_request,
    visible_correction_requests,
)
from app.services.speech_pagination import SpeechCursorError, paginate_match_speeches
from app.services.speech_quality import normalize_transcript, transcript_rejection_reason
from app.services.transcript_collab import create_transcript_collab_token
from app.services.voice_telemetry import record_browser_report

router = APIRouter(prefix="/api/rooms", tags=["rooms"])
# The test suite deliberately creates many historical fixtures in one shared
# SQLite database. Individual capacity tests enable the production gate
# explicitly; production can never disable it through configuration.
ROOM_CAPACITY_ENFORCED = settings.app_env != "test"
# TestClient fixtures deliberately do not hold lobby/debate WebSockets open.
# Freeze the deployment-mode decision at process import, just like capacity
# control, so tests can model production provider settings without creating a
# false participant disconnect. Every real development/production process
# imports this as ``True``.
RECOVERY_PRESENCE_ENFORCED = settings.app_env != "test"
_RTC_DEVICE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


async def _require_realtime_voice_ready_for_start() -> None:
    """Keep a cold or rejected production voice runtime out of the match path.

    Rooms may still be created and arranged while MOSS is starting.  Starting a
    match, however, locks seats and creates the authoritative match snapshot, so
    reject the action before those mutations when no warmed endpoint exists.
    The engine's provider-failure pause remains the second line of defence for
    failures that happen after this short readiness check.
    """

    if not (settings.app_env == "production" and settings.realtime_voice_backend == "moss_realtime" and settings.moss_tts_realtime_enabled):
        return
    # HTTP readiness alone is not enough: an endpoint can report a warmed
    # model while its authenticated bidirectional WebSocket is rejected or
    # still cold.  Starting a match in that state produces the confusing
    # "preparing" wait followed by an avoidable provider pause.  Require the
    # exact connection that the first speech will reuse to be established.
    snapshot = await moss_tts_realtime.prewarm()
    required = max(1, int(snapshot.get("required_endpoints") or 1))
    if snapshot.get("ok") is True and int(snapshot.get("idle_ws_ready_endpoints") or 0) >= required:
        return
    raise HTTPException(
        status_code=503,
        detail="实时语音连接正在预热，比赛尚未锁定。席位和准备状态已保留，请稍后重试。",
        headers={"Retry-After": "5"},
    )


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


def _require_active_room_capacity(db: Session) -> None:
    if not ROOM_CAPACITY_ENFORCED:
        return
    # Every room belongs to a competition, so the first competition row is a
    # stable PostgreSQL mutex for the global admission count. SQLite already
    # holds its process-wide writer lock through acquire_transaction_locks().
    db.scalar(select(Competition.id).order_by(Competition.id).limit(1).with_for_update())
    active_rooms = int(db.scalar(select(func.count(Room.id)).where(Room.status.in_(ACTIVE_PARTICIPANT_STATUSES))) or 0)
    if active_rooms >= MAX_ACTIVE_ROOMS:
        raise HTTPException(
            status_code=409,
            detail=f"当前同时开放的比赛已达到 {MAX_ACTIVE_ROOMS} 场上限，请等待一场比赛结束或关闭后再创建。",
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


def _competition_template_snapshot(competition: Competition) -> list[dict]:
    template = competition.automation_template
    if not template or not template.is_active or not template.stages:
        raise HTTPException(status_code=409, detail="该赛事的自动比赛流程暂不可用，请联系管理员处理。")
    return [dict(stage) for stage in template.stages]


def _synchronize_transcript_segments(db: Session, speech: Speech, content: str) -> None:
    existing = list(db.scalars(select(TranscriptSegment).where(TranscriptSegment.speech_id == speech.id)).all())
    if existing and normalize_transcript(speech.content) == content:
        return
    db.execute(delete(TranscriptSegment).where(TranscriptSegment.speech_id == speech.id))
    db.add(TranscriptSegment(speech_id=speech.id, text=content, is_final=True))


async def upload_speech_audio(*_args, **_kwargs) -> dict:
    """Compatibility symbol for retired callers; no route is registered.

    New matches persist transcripts only.  Keeping this fail-closed callable
    avoids breaking internal imports while ensuring no request can write a
    recording or expose an archive URL.
    """

    raise HTTPException(status_code=410, detail="比赛音频归档已停用；系统只保存文字记录。")


async def _publish(db: Session, room: Room, event_type: str) -> None:
    """Release the synchronous SQLAlchemy connection before awaiting Redis.

    These routes use a synchronous Session inside async handlers.  A read
    performed after ``commit()`` opens a new transaction; awaiting Redis while
    that transaction is still checked out lets a request burst exhaust the
    whole connection pool and stall the event loop.  Every caller publishes
    only after committing its mutation, so rolling back the trailing read
    transaction is safe and keeps the Session reusable for response
    serialization after the await.
    """

    code = room.code
    seq = room.seq
    if db.new or db.dirty or db.deleted:
        raise RuntimeError("room publish requires a clean committed database session")
    db.rollback()
    await room_hub.publish(code, {"type": event_type, "room_code": code, "seq": seq})


@router.post("")
async def create_room(
    payload: CreateRoomRequest,
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    user: User = Depends(verify_csrf),
    db: Session = Depends(get_db),
) -> dict:
    acquire_transaction_locks(db, "0:active-room-capacity", f"1:user:{user.id}")
    creation_key, creation_fingerprint = _creation_identity(user, payload, idempotency_key)
    if creation_key:
        existing = db.scalar(select(Room).where(Room.creation_key == creation_key))
        if existing:
            if existing.creation_fingerprint != creation_fingerprint:
                raise HTTPException(status_code=409, detail="同一个幂等键不能用于不同的建房参数。")
            return {"room": serialize_room(db, load_room(db, existing.code), user), "replayed": True}
    _require_active_room_capacity(db)
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
    template_snapshot = _competition_template_snapshot(competition)
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
        template_snapshot=template_snapshot,
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
    await _publish(db, room, "room.created")
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
    if not source_seat or source_seat.occupant_type != "human":
        raise HTTPException(status_code=403, detail="只有本场真人参赛者可以发起再次比赛。")

    acquire_transaction_locks(db, "0:active-room-capacity", f"1:user:{user.id}")
    creation_key, creation_fingerprint = _rematch_identity(user, source_room, idempotency_key)
    if creation_key:
        existing = db.scalar(select(Room).where(Room.creation_key == creation_key))
        if existing:
            if existing.creation_fingerprint != creation_fingerprint:
                raise HTTPException(status_code=409, detail="同一个幂等键不能用于不同的再次比赛请求。")
            return {"room": serialize_room(db, load_room(db, existing.code), user), "replayed": True}

    _require_active_room_capacity(db)

    _lock_participants(db, [user.id])
    _assert_available_for_room(db, user.id)
    competition = db.scalar(select(Competition).where(Competition.id == source_room.competition_id, Competition.is_active.is_(True)))
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
    template_snapshot = _competition_template_snapshot(competition)

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
        template_snapshot=template_snapshot,
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
    await _publish(db, room, "room.created")
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
    request: Request,
    device_id: str | None = Header(default=None, alias="X-Device-ID"),
    user: User | None = Depends(optional_user),
    db: Session = Depends(get_db),
) -> dict:
    room = load_room(db, code)
    if not can_view_room(db, room, user):
        raise HTTPException(status_code=401 if not user else 403, detail="无权查看该房间。")
    if not user:
        ticket = request.cookies.get(SPECTATOR_TICKET_COOKIE)
        if not valid_spectator_ticket(ticket):
            raise HTTPException(status_code=403, detail="请先进入观战页面并建立实时连接。")
        try:
            authorized = await room_hub.spectator_authorized(
                code,
                connection_id=spectator_connection_id(code, ticket),
            )
        except SpectatorAdmissionUnavailable as exc:
            raise HTTPException(status_code=503, detail="观战容量服务暂不可用，请稍后重试。") from exc
        if not authorized:
            raise HTTPException(status_code=403, detail="观战连接已失效，请重新进入观战页面。")
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


@router.post("/{code}/voice-telemetry", status_code=202)
async def report_voice_telemetry(
    code: str,
    payload: VoiceTelemetryReport,
    request: Request,
    user: User | None = Depends(optional_user),
    db: Session = Depends(get_db),
) -> dict:
    """Accept bounded browser playout diagnostics without exposing a transcript.

    Logged-in reporters must pass the normal CSRF check. Anonymous spectators
    must hold the short-lived admission ticket *and* still own the admitted
    realtime connection, matching the RTC-token authorization boundary.
    """

    room = load_room(db, code)
    if not can_view_room(db, room, user):
        raise HTTPException(status_code=401 if not user else 403, detail="无权查看该房间。")
    if user:
        verify_csrf(
            request,
            user,
            request.headers.get("X-CSRF-Token"),
            db,
        )
    else:
        ticket = request.cookies.get(SPECTATOR_TICKET_COOKIE)
        if not valid_spectator_ticket(ticket):
            raise HTTPException(status_code=403, detail="观战连接已失效，请重新进入观战页面。")
        try:
            authorized = await room_hub.spectator_authorized(
                code,
                connection_id=spectator_connection_id(code, ticket),
            )
        except SpectatorAdmissionUnavailable as exc:
            raise HTTPException(status_code=503, detail="观战容量服务暂不可用，请稍后重试。") from exc
        if not authorized:
            raise HTTPException(status_code=403, detail="观战连接已失效，请重新进入观战页面。")

    room = load_room(db, code, lock=True)
    speech = db.scalar(select(Speech).where(Speech.id == payload.speech_id, Speech.room_id == room.id).with_for_update())
    if not speech or speech.speaker_type != "ai":
        raise HTTPException(status_code=404, detail="语音发言不存在。")
    telemetry = db.scalar(select(VoiceTelemetry).where(VoiceTelemetry.speech_id == speech.id))
    authoritative_generation = (telemetry.generation if telemetry else "") or speech.stream_generation
    if authoritative_generation and payload.generation != authoritative_generation:
        raise HTTPException(status_code=409, detail="语音代次已失效。")
    try:
        record_browser_report(
            db,
            room,
            speech,
            generation=payload.generation,
            event=payload.event,
            metrics=payload.metrics,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail="语音代次已失效。") from exc
    db.commit()
    return {"accepted": True}


@router.get("/{code}/result")
def room_result(
    code: str,
    speech_page: int = Query(default=1, ge=1, le=100000),
    speech_page_size: int = Query(default=100, ge=1, le=200),
    speech_cursor: str | None = Query(default=None, min_length=10, max_length=1000),
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
    public_projection = use_public_projection(db, room, user)
    if public_projection and match.status != "completed":
        raise HTTPException(
            status_code=409,
            detail="比赛尚未结束，请前往观战页面查看实时内容。",
        )
    scorecard = db.scalar(select(JudgeScorecard).where(JudgeScorecard.match_id == match.id))
    try:
        speech_result = paginate_match_speeches(
            db,
            match.id,
            page=speech_page,
            page_size=speech_page_size,
            cursor=speech_cursor,
        )
    except SpeechCursorError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    stage_names = {str(item.get("key")): str(item.get("name") or item.get("key")) for item in room.template_snapshot or []}
    changes = db.scalars(
        select(RatingChange).where(RatingChange.match_id == match.id).order_by(RatingChange.created_at, RatingChange.id)
    ).all()
    rating_names = {
        item.id: item.real_name for item in db.scalars(select(User).where(User.id.in_({change.user_id for change in changes}))).all()
    }
    event_filters = [MatchEvent.room_id == room.id]
    if public_projection:
        event_filters.append(MatchEvent.event_type.in_(PUBLIC_MATCH_TIMELINE_EVENT_TYPES))
    event_total = db.scalar(select(func.count(MatchEvent.id)).where(*event_filters)) or 0
    event_query = select(MatchEvent).where(*event_filters)
    if event_before_seq is not None:
        event_query = event_query.where(MatchEvent.seq < event_before_seq)
    else:
        event_query = event_query.offset((event_page - 1) * event_page_size)
    events = list(reversed(db.scalars(event_query.order_by(MatchEvent.seq.desc()).limit(event_page_size)).all()))
    next_before_seq = min((item.seq for item in events), default=None)
    has_more_events = bool(
        next_before_seq and db.scalar(select(MatchEvent.id).where(*event_filters, MatchEvent.seq < next_before_seq).limit(1))
    )
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
                "content": "" if public_projection else item.content,
                "audio_url": item.audio_url if settings.match_audio_archive_enabled else "",
                "duration_seconds": item.duration_seconds,
                "created_at": item.created_at.isoformat(),
                "can_request_correction": can_request_correction(db, room, item, user),
            }
            for item in speech_result.rows
        ],
        "speech_pagination": {
            "page": speech_result.page,
            "page_size": speech_result.page_size,
            "total": speech_result.total,
            "pages": speech_result.pages,
            "next_cursor": speech_result.next_cursor,
            "has_more": speech_result.has_more,
        },
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
                    if public_projection or user is None
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
    await _publish(db, room, "seat.claimed")
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
    await _publish(db, room, "seat.released")
    return {"room": serialize_room(db, load_room(db, code), user)}


@router.post("/{code}/seats/{seat_key}/remove")
async def remove_lobby_participant(
    code: str,
    seat_key: str,
    payload: ControlRequest,
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    user: User = Depends(verify_csrf),
    auth_session: UserSession = Depends(authenticated_session),
    db: Session = Depends(get_db),
) -> dict:
    room = load_room(db, code, lock=True)
    if not can_control(db, room, user):
        raise HTTPException(status_code=403, detail="只有房主或系统管理员可以移出席位。")
    if room.status != "lobby":
        raise HTTPException(
            status_code=409,
            detail="比赛开始后不能移出真人席位；真人断线满 60 秒后系统会自动暂停，请由房主或管理员处理。",
        )
    controller_seat = user_seat(room, user)
    if controller_seat and user.role != "system_admin":
        _require_lobby_device_control(controller_seat, auth_session)
    operation_key = _operation_key(f"seat-remove:{seat_key}", room, user, idempotency_key)
    if operation_key:
        previous = db.scalar(select(MatchEvent).where(MatchEvent.idempotency_key == operation_key))
        if previous:
            return {"room": serialize_room(db, room, user), "replayed": True}
    seat = next((item for item in room.seats if item.seat_key == seat_key), None)
    if not seat:
        raise HTTPException(status_code=404, detail="席位不存在。")
    if seat.user_id == room.owner_id:
        raise HTTPException(status_code=409, detail="不能移出房主席位；如需结束组局请关闭房间。")
    if seat.occupant_type != "human" or not seat.user_id:
        raise HTTPException(status_code=409, detail="该席位当前没有可移出的真人辩手。")
    removed_user_id = seat.user_id
    removed_name = seat.display_name
    _lock_participants(db, [removed_user_id])
    seat.occupant_type = "open"
    seat.user_id = None
    seat.display_name = "待加入"
    seat.is_ready = False
    seat.connected = False
    seat.disconnected_at = None
    seat.control_lease = ""
    seat.control_session_id = None
    seat.agent_profile_id = None
    append_event(
        db,
        room,
        "seat.removed_by_owner",
        {"seat_key": seat.seat_key, "real_name": removed_name, "reason": payload.reason},
        actor_user_id=user.id,
        idempotency_key=operation_key,
    )
    db.commit()
    await _publish(db, room, "seat.removed_by_owner")
    return {"room": serialize_room(db, load_room(db, code), user), "replayed": False}


@router.post("/{code}/abandon-seat")
async def abandon_started_seat(
    code: str,
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    user: User = Depends(verify_csrf),
    db: Session = Depends(get_db),
) -> dict:
    room = load_room(db, code, lock=True)
    del idempotency_key, user, room
    raise HTTPException(
        status_code=410,
        detail="系统已取消 AI 自动接管真人席位；如需离开，请联系房主暂停或终止比赛。",
    )


@router.get("/{code}/speech-correction-requests")
def list_speech_correction_requests(
    code: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    room = load_room(db, code)
    if not can_view_room(db, room, user):
        raise HTTPException(status_code=403, detail="无权查看该房间。")
    return {"items": visible_correction_requests(db, room, user)}


@router.post("/{code}/transcript-collab-token")
def transcript_collab_token(
    code: str,
    response: Response,
    user: User = Depends(verify_csrf),
    db: Session = Depends(get_db),
) -> dict:
    room = load_room(db, code)
    if not can_view_room(db, room, user):
        raise HTTPException(status_code=403, detail="无权查看该房间。")
    is_match_participant = db.scalar(
        select(MatchParticipant.id).where(
            MatchParticipant.room_id == room.id,
            MatchParticipant.user_id == user.id,
        )
    )
    if not can_control(db, room, user) and user_seat(room, user) is None and not is_match_participant:
        raise HTTPException(status_code=403, detail="观众不可查看文字稿。")
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Pragma"] = "no-cache"
    return create_transcript_collab_token(db, room, user)


@router.post("/{code}/speeches/{speech_id}/correction-requests")
async def request_speech_correction(
    code: str,
    speech_id: str,
    payload: SpeechCorrectionCreate,
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    user: User = Depends(verify_csrf),
    db: Session = Depends(get_db),
) -> dict:
    room = load_room(db, code, lock=True)
    request, replayed = create_correction_request(
        db,
        room,
        speech_id,
        user,
        proposed_content=payload.proposed_content,
        reason=payload.reason,
        provided_idempotency_key=idempotency_key,
    )
    db.commit()
    if not replayed:
        await _publish(db, room, "speech.correction_requested")
    return {
        "request": serialize_correction_request(request),
        "room": serialize_room(db, load_room(db, code), user),
        "replayed": replayed,
    }


@router.post("/{code}/speech-correction-requests/{request_id}/cancel")
async def cancel_speech_correction(
    code: str,
    request_id: str,
    user: User = Depends(verify_csrf),
    db: Session = Depends(get_db),
) -> dict:
    room = load_room(db, code, lock=True)
    request = db.get(SpeechCorrectionRequest, request_id)
    if not request:
        raise HTTPException(status_code=404, detail="修正申请不存在。")
    replayed = cancel_correction_request(db, room, request, user)
    db.commit()
    if not replayed:
        await _publish(db, room, "speech.correction_cancelled")
    return {"request": serialize_correction_request(request), "replayed": replayed}


@router.post("/{code}/free-turn-requests")
async def create_free_turn_request(
    code: str,
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    user: User = Depends(verify_csrf),
    db: Session = Depends(get_db),
) -> dict:
    room = load_room(db, code, lock=True)
    item, replayed = request_free_turn(db, room, user, provided_idempotency_key=idempotency_key)
    db.commit()
    if not replayed:
        await match_engine.invalidate_free_agent_speculation(code)
        await _publish(db, room, "free.turn_requested")
    return {"request_id": item.id, "status": item.status, "replayed": replayed, "room": serialize_room(db, load_room(db, code), user)}


@router.post("/{code}/free-turn-requests/{request_id}/cancel")
async def cancel_free_turn_request(
    code: str,
    request_id: str,
    user: User = Depends(verify_csrf),
    db: Session = Depends(get_db),
) -> dict:
    room = load_room(db, code, lock=True)
    item = db.get(FreeTurnRequest, request_id)
    if not item:
        raise HTTPException(status_code=404, detail="自由辩论申请不存在。")
    replayed = cancel_free_turn(db, room, item, user)
    db.commit()
    if not replayed:
        await _publish(db, room, "free.turn_request_cancelled")
    return {"request_id": item.id, "status": item.status, "replayed": replayed, "room": serialize_room(db, load_room(db, code), user)}


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
    await _publish(db, room, "seat.ready_changed")
    return {"room": serialize_room(db, load_room(db, code), user)}


@router.post("/{code}/transfer-owner")
async def transfer_owner(
    code: str,
    payload: SeatRequest,
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    user: User = Depends(verify_csrf),
    db: Session = Depends(get_db),
) -> dict:
    """Hand room recovery controls to another human participant.

    Normal matches advance automatically, but a present participant must be
    able to repair an exceptional pause when the original creator leaves.
    Only the current owner (or a system administrator) can make an explicit
    handoff; engine-driven disconnect recovery uses the same event contract.
    """

    room = load_room(db, code, lock=True)
    operation_key = _operation_key("owner-transfer", room, user, idempotency_key)
    if operation_key:
        previous = db.scalar(select(MatchEvent).where(MatchEvent.idempotency_key == operation_key))
        if previous:
            if previous.payload.get("seat_key") != payload.seat_key:
                raise HTTPException(status_code=409, detail="同一个幂等键不能用于不同的目标席位。")
            return {"room": serialize_room(db, room, user), "replayed": True}
    if not can_control(db, room, user):
        raise HTTPException(status_code=403, detail="只有当前房主或系统管理员可以移交房间控制权。")
    if room.status not in ACTIVE_PARTICIPANT_STATUSES:
        raise HTTPException(status_code=409, detail="比赛已经结束，不能再移交房间控制权。")
    successor = next((item for item in room.seats if item.seat_key == payload.seat_key), None)
    if not successor:
        raise HTTPException(status_code=404, detail="目标席位不存在。")
    if successor.occupant_type != "human" or not successor.user_id:
        raise HTTPException(status_code=409, detail="控制权只能移交给当前真人辩手。")
    if not successor.connected:
        raise HTTPException(status_code=409, detail="目标辩手当前不在线，不能接管房间控制权。")
    participant = db.get(User, successor.user_id)
    if not participant or not participant.is_active:
        raise HTTPException(status_code=409, detail="目标辩手账号当前不可用。")
    if successor.user_id == room.owner_id:
        return {"room": serialize_room(db, room, user), "replayed": True}
    _lock_participants(db, [room.owner_id, successor.user_id])
    transfer_room_owner(
        db,
        room,
        successor,
        actor_user_id=user.id,
        reason="manual_handoff",
        idempotency_key=operation_key,
    )
    db.commit()
    await _publish(db, room, "room.owner_transferred")
    return {"room": serialize_room(db, load_room(db, code), user), "replayed": False}


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
    await _publish(db, room, "room.cancelled")
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
    # Unit/integration tests use synchronous TestClient calls without holding
    # four lobby WebSockets open.  Every real deployment must require the
    # authoritative realtime presence before locking seats.
    disconnected = [seat.display_name for seat in humans if not seat.connected] if settings.app_env != "test" else []
    if disconnected:
        raise HTTPException(
            status_code=409,
            detail=f"以下辩手当前未连接比赛房间：{'、'.join(disconnected)}。请全部进入房间后再开始比赛。",
        )
    if settings.host_cues_preset_only:
        missing_cues = match_engine.missing_preset_cues(db, room)
        if missing_cues:
            visible = "、".join(missing_cues[:5])
            suffix = f"等 {len(missing_cues)} 个阶段" if len(missing_cues) > 5 else ""
            raise HTTPException(
                status_code=409,
                detail=f"系统阶段提示音尚未完整配置：{visible}{suffix}。请联系管理员补齐统一预设音频后再开赛。",
            )
    # Provider warming can be comparatively expensive.  Only probe it after
    # every user-repairable lobby condition has passed, so a stale page gets a
    # deterministic 409 instead of an unrelated service-readiness response.
    await _require_realtime_voice_ready_for_start()
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
    db.flush()
    for seat in humans:
        if seat.user_id:
            db.add(
                MatchParticipant(
                    match_id=match.id,
                    room_id=room.id,
                    seat_key=seat.seat_key,
                    user_id=seat.user_id,
                    display_name=seat.display_name,
                )
            )
    room.status = "preparing"
    room.started_at = now()
    # Lobby occupancy and in-match disconnection use different clocks.  Real
    # deployments reach this point only when every human is online; the
    # conditional remains for synchronous test fixtures that do not keep room
    # WebSockets open and gives those fixtures the normal 60-second boundary.
    for seat in humans:
        seat.disconnected_at = None if seat.connected else room.started_at
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
    await _publish(db, room, "room.locked")
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
        raise HTTPException(status_code=409, detail="该席位不是可由真人控制的参赛席位。")
    if room.status in {"review_required", "completed", "terminated", "cancelled"}:
        raise HTTPException(status_code=409, detail="比赛已经结束，无需接管席位。")
    fingerprint = _lease_fingerprint(lease)
    same_session = bool(seat.control_lease and seat.control_session_id and seat.control_session_id == auth_session.id)
    previous_session = db.get(UserSession, seat.control_session_id) if seat.control_session_id else None
    previous_session_active = bool(
        previous_session
        and previous_session.user_id == user.id
        and previous_session.expires_at
        and (
            previous_session.expires_at.replace(tzinfo=now().tzinfo)
            if previous_session.expires_at.tzinfo is None
            else previous_session.expires_at
        )
        > now()
    )
    # Possession of the same high-entropy lease proves this is the same
    # browser device even when its login session cookie was rotated between
    # the lobby and stage. Rebind that session without presenting a false
    # cross-device takeover warning. A genuinely different device still has a
    # different lease and follows the explicit confirmation path below.
    if seat.control_lease == lease:
        same_device_recovery = seat.control_session_id != auth_session.id
        if same_device_recovery:
            seat.control_session_id = auth_session.id
            db.commit()
        return {
            "ok": True,
            "lease_fingerprint": fingerprint,
            "seq": room.seq,
            "replayed": True,
            "same_session_recovery": same_session,
            "same_device_recovery": same_device_recovery,
        }
    active = active_speech(db, room)
    # A login session proves who the user is, not which browser/device is in
    # control. Multiple tabs and copied cookies can share the same session, so
    # only the exact high-entropy lease above is safe to replay silently.
    # Every different lease follows the explicit takeover path, and an active
    # human speech can never be transferred mid-sentence.
    if active and active.seat_key == seat.seat_key and seat.control_lease:
        raise HTTPException(status_code=409, detail="该席位正在另一设备发言，结束后才能接管。")
    if seat.control_lease and not force and previous_session_active:
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
    await _publish(db, room, event_type)
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
        # This branch is resuming this exact speech, so validate every other
        # authoritative turn rule without rejecting it as its own active row.
        allowed, reason = speaking_permission(room, user, ignore_active_speech=True)
        if not allowed:
            raise HTTPException(status_code=403, detail=reason)
        current = stage(room)
        if not current or existing_active.stage_key != current.get("key"):
            raise HTTPException(status_code=409, detail="进行中的发言不属于当前阶段，请刷新页面或由房主处理异常步骤。")
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
    current = match_engine.start_human_stage_clock(room, current, started_at=now())
    append_event(
        db,
        room,
        "speech.started",
        {
            "speech_id": speech.id,
            "seat_key": seat.seat_key,
            "speaker_type": "human",
            "deadline_at": room.stage_deadline_at.isoformat() if room.stage_deadline_at else None,
        },
        actor_user_id=user.id,
        idempotency_key=operation_key,
    )
    db.commit()
    await _publish(db, room, "speech.started")
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
    if room.status in {"terminated", "cancelled"}:
        # A timed-out speech may still be finalized after its stage advances,
        # including the short result/review window needed by a weak-network
        # browser to preserve its local recording. Explicit termination is the
        # immutable audit boundary.
        raise HTTPException(status_code=409, detail="比赛已经终止或取消，不能再提交发言。")
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
        archive_match_id = match.id if room.status in {"completed", "review_required"} else None
        db.commit()
        if archive_match_id:
            # The result may have been archived before the weak-network client
            # recovered its locally retained final transcript. Rebuild that
            # exact match instead of discarding the student's evidence.
            enqueue_match_archive(archive_match_id)
        await _publish(db, room, "speech.late_finalized")
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
    if current and current.get("kind") == "free":
        match_engine.schedule_free_agent_speculation(code)
    await _publish(db, room, "speech.completed")
    return {"room": serialize_room(db, load_room(db, code), user), "speech_id": speech.id}


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
    participant_timeout_context, participant_disconnect_pause = participant_disconnect_pause_context(db, room)
    # Recovery actions must be fail-closed even during the first 60 seconds of
    # a disconnect.  Previously the API populated this list only after the
    # engine had persisted ``participant.disconnect_timeout``.  A caller could
    # therefore bypass the disabled UI and resume/retry a manually or
    # service-paused room while a human was already offline, then wait for a
    # second automatic pause.  TestClient fixtures do not hold room sockets;
    # retain their explicit timeout-context path while every real environment
    # enforces current authoritative presence immediately.
    unavailable_humans = []
    if RECOVERY_PRESENCE_ENFORCED or participant_timeout_context:
        for seat in room.seats:
            if seat.occupant_type != "human" or not seat.user_id:
                continue
            participant = db.get(User, seat.user_id)
            if not seat.connected or participant is None or not participant.is_active:
                unavailable_humans.append(seat.display_name)
    speaking_human = db.scalar(
        select(Speech).where(
            Speech.room_id == room.id,
            Speech.speaker_type == "human",
            Speech.status == "speaking",
        )
    )
    if action in {"pause", "safe-pause"}:
        if room.status not in {"preparing", "running", "judging"}:
            raise HTTPException(status_code=409, detail="当前状态不能暂停。")
        if room.status == "preparing" and action == "safe-pause":
            raise HTTPException(status_code=409, detail="比赛仍在开赛准备中，当前没有需要紧急处置的真人发言。")
        if speaking_human and action == "pause":
            raise HTTPException(status_code=409, detail="真人正在发言，请先结束发言再暂停比赛。")
        if action == "safe-pause" and not speaking_human:
            raise HTTPException(status_code=409, detail="当前没有需要紧急处置的真人发言。")
        current = stage(room)
        stage_remaining = remaining_seconds(room)
        host_announcement_ms = None
        human_start_remaining_ms = None
        if current and current.get("awaiting_human_start"):
            try:
                human_start_deadline = datetime.fromisoformat(str(current.get("human_start_deadline_at")))
                if human_start_deadline.tzinfo is None:
                    human_start_deadline = human_start_deadline.replace(tzinfo=now().tzinfo)
                human_start_remaining_ms = max(0, round((human_start_deadline - now()).total_seconds() * 1000))
            except (TypeError, ValueError):
                human_start_remaining_ms = settings.human_start_timeout_seconds * 1000
        if current and current.get("host_announcement_pending"):
            try:
                host_deadline = datetime.fromisoformat(str(current.get("host_announcement_deadline_at")))
                if host_deadline.tzinfo is None:
                    host_deadline = host_deadline.replace(tzinfo=now().tzinfo)
                host_announcement_ms = max(0, round((host_deadline - now()).total_seconds() * 1000))
            except (TypeError, ValueError):
                host_announcement_ms = 0
        turn_remaining = free_turn_remaining_seconds(room, current) if current and current.get("kind") == "free" else None
        intermission_ms = intermission_remaining_ms(room, current) if current and current.get("kind") == "free" else None
        if speaking_human:
            match_engine.close_active_speeches(db, room, status="interrupted", reason="emergency_human_pause")
        match_engine.interrupt_inflight_ai_speeches(db, room, reason="manual_pause")
        match_engine.interrupt_inflight_judging(db, room, reason="manual_pause")
        # Before the first stage is entered there is no countdown to freeze.
        # Persisting a synthetic one-second remainder would make a later
        # resume look like a running stage even though current_stage_index is
        # still -1.  The resume branch restores this exact state to
        # ``preparing`` so cue readiness and the opening stage remain the sole
        # authoritative start boundary.
        room.paused_remaining_seconds = (
            None if room.status == "preparing" else 1 if stage_remaining is None else stage_remaining
        )
        current = stage(room)
        updated_current = dict(current) if current else None
        if updated_current and host_announcement_ms is not None:
            updated_current["paused_host_announcement_remaining_ms"] = host_announcement_ms
            updated_current.pop("host_announcement_deadline_at", None)
        if updated_current and human_start_remaining_ms is not None:
            updated_current["paused_human_start_remaining_ms"] = human_start_remaining_ms
            updated_current.pop("human_start_deadline_at", None)
        if updated_current and updated_current.get("kind") == "free":
            updated_current["paused_turn_remaining_seconds"] = turn_remaining or 0
            if intermission_ms is not None:
                updated_current["paused_intermission_remaining_ms"] = intermission_ms
                updated_current.pop("intermission_deadline_at", None)
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
        match_engine.arm_interrupted_human_restart(
            room,
            speaking_human,
            stage_remaining=stage_remaining,
            turn_remaining=turn_remaining,
        )
        room.status = "paused"
        room.stage_deadline_at = None
    elif action == "resume":
        if room.status != "paused":
            raise HTTPException(status_code=409, detail="比赛未暂停。")
        if unavailable_humans:
            raise HTTPException(
                status_code=409,
                detail=f"以下真人辩手尚未重新连接或账号不可用：{'、'.join(unavailable_humans)}。全部恢复后再继续比赛。",
            )
        current = stage(room)
        human_start_timeout_pause = bool(current and current.get("human_start_timeout_paused"))
        failed_speech = (
            db.scalar(
                select(Speech.id).where(
                    Speech.room_id == room.id,
                    Speech.stage_key == str(current.get("key")),
                    Speech.status == "failed",
                )
            )
            if current
            else None
        )
        if (room.failure_reason and not participant_disconnect_pause and not human_start_timeout_pause) or failed_speech:
            raise HTTPException(status_code=409, detail="当前因服务异常暂停，请使用重试当前步骤。")
        resumed_at = now()
        # A manual pause or disconnect can occur while the room is preparing
        # cues and before the first stage is entered. Resuming that state as
        # ``running`` would leave ``current_stage_index == -1`` with no stage;
        # the engine would then treat the match as finished. Return to the
        # idempotent preparation path so cue preparation and opening continue
        # normally after either recovery path.
        if room.current_stage_index < 0 and current is None:
            room.status = "preparing"
            room.stage_started_at = None
            room.stage_deadline_at = None
            room.paused_remaining_seconds = None
            room.failure_reason = ""
        else:
            resumes_judging = bool(
                (current and current.get("kind") == "judging")
                or (room.current_stage_index >= len(room.template_snapshot or []) and room.current_stage_index >= 0)
            )
            room.status = "judging" if resumes_judging else "running"
            resume_seconds = room.paused_remaining_seconds if room.paused_remaining_seconds is not None else 1
            room.stage_deadline_at = (
                None
                if resumes_judging or (current and current.get("awaiting_human_start"))
                else resumed_at + timedelta(seconds=max(0, resume_seconds))
            )
            if current and current.get("host_announcement_pending"):
                updated_current = dict(current)
                host_remaining_ms = max(0, int(updated_current.pop("paused_host_announcement_remaining_ms", 0)))
                host_deadline = resumed_at + timedelta(milliseconds=host_remaining_ms)
                updated_current["host_announcement_deadline_at"] = host_deadline.isoformat()
                snapshot = list(room.template_snapshot)
                snapshot[room.current_stage_index] = updated_current
                room.template_snapshot = snapshot
                room.stage_deadline_at = host_deadline
                current = updated_current
            elif current and current.get("kind") == "free":
                updated_current = dict(current)
                paused_intermission_ms = updated_current.pop("paused_intermission_remaining_ms", None)
                if paused_intermission_ms is not None:
                    updated_current["intermission_deadline_at"] = (
                        resumed_at + timedelta(milliseconds=max(0, int(paused_intermission_ms)))
                    ).isoformat()
                    updated_current.pop("paused_turn_remaining_seconds", None)
                elif updated_current.get("awaiting_human_start"):
                    updated_current.pop("paused_turn_remaining_seconds", None)
                    updated_current.pop("turn_started_at", None)
                    paused_human_start_ms = updated_current.pop("paused_human_start_remaining_ms", None)
                    if updated_current.pop("human_start_timeout_paused", False):
                        paused_human_start_ms = settings.human_start_timeout_seconds * 1000
                    updated_current["human_start_deadline_at"] = (
                        resumed_at
                        + timedelta(
                            milliseconds=max(
                                0,
                                int(
                                    paused_human_start_ms
                                    if paused_human_start_ms is not None
                                    else settings.human_start_timeout_seconds * 1000
                                ),
                            )
                        )
                    ).isoformat()
                    room.stage_deadline_at = None
                else:
                    turn_duration = free_turn_duration(updated_current)
                    paused_turn_remaining = max(
                        0,
                        min(
                            int(updated_current.pop("paused_turn_remaining_seconds", turn_duration)),
                            turn_duration,
                        ),
                    )
                    elapsed = max(0, turn_duration - paused_turn_remaining)
                    updated_current["turn_started_at"] = (resumed_at - timedelta(seconds=elapsed)).isoformat()
                snapshot = list(room.template_snapshot)
                snapshot[room.current_stage_index] = updated_current
                room.template_snapshot = snapshot
                current = updated_current
            elif current and current.get("awaiting_human_start"):
                updated_current = dict(current)
                paused_human_start_ms = updated_current.pop("paused_human_start_remaining_ms", None)
                if updated_current.pop("human_start_timeout_paused", False):
                    paused_human_start_ms = settings.human_start_timeout_seconds * 1000
                updated_current["human_start_deadline_at"] = (
                    resumed_at
                    + timedelta(
                        milliseconds=max(
                            0,
                            int(
                                paused_human_start_ms
                                if paused_human_start_ms is not None
                                else settings.human_start_timeout_seconds * 1000
                            ),
                        )
                    )
                ).isoformat()
                snapshot = list(room.template_snapshot)
                snapshot[room.current_stage_index] = updated_current
                room.template_snapshot = snapshot
                room.stage_deadline_at = None
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
        if speaking_human:
            raise HTTPException(
                status_code=409,
                detail="真人正在发言，不能直接跳过并丢弃发言；请先正常结束发言，或使用紧急暂停保留剩余时间。",
            )
        if unavailable_humans:
            raise HTTPException(
                status_code=409,
                detail=f"以下真人辩手尚未重新连接或账号不可用：{'、'.join(unavailable_humans)}。真人断线期间不能跳过阶段。",
            )
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
            append_event(
                db,
                room,
                "judge.review_required",
                {"message": scorecard.reasoning, "scorecard_id": scorecard.id},
                actor_user_id=user.id,
            )
        else:
            current_stage_key = str(current.get("key")) if current else ""
            if current_stage_key:
                for failed in db.scalars(
                    select(Speech).where(
                        Speech.room_id == room.id,
                        Speech.stage_key == current_stage_key,
                        Speech.status == "failed",
                    )
                ).all():
                    failed.status = "failed_skipped"
            match_engine.close_active_speeches(db, room, status="interrupted", reason="manual_skip")
            match_engine._advance(db, room, reason="manual_skip")
            room.failure_reason = ""
    elif action == "reset-speech":
        if room.status != "running":
            raise HTTPException(status_code=409, detail="只有运行中的 AI 发言可以重置。")
        current = stage(room)
        if not current or current.get("kind") not in {"speech", "free"}:
            raise HTTPException(status_code=409, detail="当前阶段没有可重置的 AI 发言。")
        reset_ids = match_engine.reset_active_ai_speech(db, room, reason="owner_reset")
        if not reset_ids:
            raise HTTPException(status_code=409, detail="当前没有正在进行的 AI 发言。")
        reset_at = now()
        if current.get("kind") == "free":
            updated_current = dict(stage(room) or current)
            updated_current.pop("turn_started_at", None)
            updated_current.pop("awaiting_human_start", None)
            updated_current.pop("intermission_deadline_at", None)
            snapshot = list(room.template_snapshot)
            snapshot[room.current_stage_index] = updated_current
            room.template_snapshot = snapshot
            room.stage_started_at = None
            room.stage_deadline_at = None
        else:
            duration = max(1, int(current.get("duration") or 30))
            room.stage_started_at = reset_at
            room.stage_deadline_at = reset_at + timedelta(seconds=duration)
        room.failure_reason = ""
    elif action == "retry":
        if room.status != "paused":
            raise HTTPException(status_code=409, detail="只有异常暂停状态可以重试。")
        if unavailable_humans:
            raise HTTPException(
                status_code=409,
                detail=f"以下真人辩手尚未重新连接或账号不可用：{'、'.join(unavailable_humans)}。全部恢复后再重试当前步骤。",
            )
        if participant_disconnect_pause:
            raise HTTPException(status_code=409, detail="当前因真人断线暂停；全部真人重新连接后请使用继续比赛。")
        retry_stage = stage(room)
        if retry_stage and retry_stage.get("human_start_timeout_paused"):
            raise HTTPException(status_code=409, detail="当前因真人长时间未开始发言而暂停；确认选手准备后请使用继续比赛。")
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        if settings.app_env == "production" and match and not match.service_snapshot:
            raise HTTPException(
                status_code=409,
                detail="该旧比赛缺少服务配置快照，不能安全重试；请结束本场并创建新比赛。",
            )
        current = stage(room)
        # Retry is the room-wide recovery boundary for unresolved provider
        # attempts, including legacy/preparation failures whose stage key may
        # not match the current template snapshot. Normal resume is scoped to
        # the current stage, while an explicit retry seals every unresolved
        # failed row before provider work starts again.
        failed = db.scalars(select(Speech).where(Speech.room_id == room.id, Speech.status == "failed")).all()
        if not room.failure_reason and not failed:
            raise HTTPException(status_code=409, detail="当前是人工暂停，请使用恢复比赛。")
        await _require_realtime_voice_ready_for_start()
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
            retries_judging = bool(
                (current and current.get("kind") == "judging")
                or (room.current_stage_index >= len(room.template_snapshot or []) and room.current_stage_index >= 0)
            )
            room.status = "judging" if retries_judging else "running"
            # Preserve the exact frozen countdown on a retry.  Using ``or 30``
            # here turned a stage that had already reached 00:00 into a fresh
            # 30-second stage after a provider retry, which looked like the
            # match had jumped backwards.  ``None`` only occurs for a legacy
            # paused row without a snapshot; use the smallest safe value for
            # that case, while an explicit zero remains an immediate expiry.
            paused_seconds = room.paused_remaining_seconds
            room.stage_deadline_at = (
                None if retries_judging else retried_at + timedelta(seconds=max(0, paused_seconds if paused_seconds is not None else 1))
            )
        current = stage(room)
        if current and current.get("kind") == "free":
            updated_current = dict(current)
            if updated_current.get("awaiting_human_start"):
                updated_current.pop("paused_turn_remaining_seconds", None)
                updated_current.pop("turn_started_at", None)
                updated_current.pop("human_start_timeout_paused", None)
                updated_current.pop("paused_human_start_remaining_ms", None)
                updated_current["human_start_deadline_at"] = (
                    retried_at + timedelta(seconds=settings.human_start_timeout_seconds)
                ).isoformat()
                room.stage_deadline_at = None
            else:
                turn_duration = free_turn_duration(updated_current)
                paused_turn_remaining = max(
                    1,
                    min(
                        int(updated_current.pop("paused_turn_remaining_seconds", turn_duration)),
                        turn_duration,
                    ),
                )
                elapsed = max(0, turn_duration - paused_turn_remaining)
                updated_current["turn_started_at"] = (retried_at - timedelta(seconds=elapsed)).isoformat()
            snapshot = list(room.template_snapshot)
            snapshot[room.current_stage_index] = updated_current
            room.template_snapshot = snapshot
        room.paused_remaining_seconds = None
    elif action == "terminate":
        if room.status not in {"preparing", "running", "paused", "judging"}:
            raise HTTPException(status_code=409, detail="当前状态不能终止比赛。")
        match_engine.close_active_speeches(db, room, status="interrupted", reason="match_terminated")
        match_engine.interrupt_inflight_judging(db, room, reason="match_terminated")
        room.status = "terminated"
        room.completed_at = now()
        room.stage_deadline_at = None
        room.paused_remaining_seconds = None
        expire_room_requests(db, room, "match_terminated")
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
    if action in {"pause", "safe-pause", "reset-speech", "skip", "terminate"}:
        await match_engine.invalidate_free_agent_speculation(code)
    elif action == "resume":
        match_engine.schedule_free_agent_speculation(code)
    if archive_match_id:
        enqueue_match_archive(archive_match_id)
    await _publish(db, room, f"control.{action}")
    return {"room": serialize_room(db, load_room(db, code), user)}
