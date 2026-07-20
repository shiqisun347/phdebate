from __future__ import annotations

import asyncio
import json
import logging
import re
import struct
import threading
import uuid
from datetime import datetime, timezone
from time import monotonic

import websockets
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.core.config import settings
from app.core.database import SessionLocal, TransactionLockTimeout
from app.core.deps import SESSION_COOKIE
from app.core.security import as_utc, token_hash
from app.models.entities import Match, Room, RoomSeat, Speech, TranscriptSegment, User, UserSession
from app.services.provider_config import runtime_provider_config
from app.services.public_snapshot import public_snapshot_cache
from app.services.realtime import audio_stream_aborts, room_hub
from app.services.room_service import (
    anonymous_realtime_event,
    append_event,
    can_control,
    can_view_room,
    load_room,
    serialize_room,
    use_public_projection,
    user_seat,
)
from app.services.speech_quality import (
    MIN_ASR_VOICED_SAMPLES,
    PcmVoiceActivity,
    normalize_transcript,
    payload_confidence,
    transcript_rejection_reason,
)

router = APIRouter(tags=["realtime"])
logger = logging.getLogger(__name__)
_active_asr_streams: set[str] = set()
_active_asr_streams_lock = threading.Lock()
ACTIVE_PRESENCE_STATUSES = {"lobby", "preparing", "running", "paused", "judging"}
ASR_PROTOCOL_FIELDS = {"protocol_version", "encoding", "channels", "sample_rate"}
ASR_AUTHENTICATION_FIELDS = {"type", "lease"} | ASR_PROTOCOL_FIELDS
ASR_PROTOCOL_V1 = {
    "protocol_version": 1,
    "encoding": "pcm_s16le",
    "channels": 1,
    "sample_rate": 16_000,
}
AUDIO_STREAM_GENERATION_RE = re.compile(r"^[a-f0-9]{32}$")
AUDIO_STREAM_PROTOCOL_VERSION = 1
AUDIO_STREAM_FRAME_SAMPLES = 960
AUDIO_STREAM_SAMPLE_WIDTH = 2
AUDIO_STREAM_WAV_HEADER_BYTES = 44
AUDIO_STREAM_REPLAY_SECONDS = 0.4
AUDIO_STREAM_CLIENT_HIGH_WATER_MS = 1_200
AUDIO_STREAM_INITIAL_BURST_FRAMES = 20
AUDIO_STREAM_FEEDBACK_BURST_FRAMES = 8
AUDIO_STREAM_BINARY_HEADER = struct.Struct(">2sBBIIQI")
TRANSIENT_DATABASE_ERRORS = (OperationalError, TransactionLockTimeout)


def _asr_protocol_supported(authentication: dict) -> bool:
    if set(authentication) - ASR_AUTHENTICATION_FIELDS:
        return False
    present = ASR_PROTOCOL_FIELDS.intersection(authentication)
    if not present:
        return True
    if present != ASR_PROTOCOL_FIELDS:
        return False
    return (
        type(authentication.get("protocol_version")) is int
        and authentication["protocol_version"] == ASR_PROTOCOL_V1["protocol_version"]
        and authentication.get("encoding") == ASR_PROTOCOL_V1["encoding"]
        and type(authentication.get("channels")) is int
        and authentication["channels"] == ASR_PROTOCOL_V1["channels"]
        and type(authentication.get("sample_rate")) is int
        and authentication["sample_rate"] == ASR_PROTOCOL_V1["sample_rate"]
    )


def _claim_asr_stream(speech_id: str) -> bool:
    with _active_asr_streams_lock:
        if speech_id in _active_asr_streams:
            return False
        _active_asr_streams.add(speech_id)
        return True


def _release_asr_stream(speech_id: str) -> None:
    with _active_asr_streams_lock:
        _active_asr_streams.discard(speech_id)


def _websocket_origin_allowed(websocket: WebSocket) -> bool:
    origin = websocket.headers.get("origin")
    if not origin:
        return True
    normalized = origin.rstrip("/").lower()
    return normalized in {item.rstrip("/").lower() for item in settings.origins}


def persist_asr_final(
    speech_id: str,
    text: str,
    *,
    confidence: float | None = None,
    voice_detected: bool = True,
) -> bool:
    normalized = normalize_transcript(text)
    if not voice_detected or transcript_rejection_reason(
        normalized,
        confidence=confidence,
        require_substantive=True,
    ):
        return False
    with SessionLocal() as db:
        speech = db.get(Speech, speech_id)
        if not speech or speech.status != "speaking":
            return False
        speech.content = f"{speech.content}{normalized}"
        db.add(TranscriptSegment(speech_id=speech.id, text=normalized, is_final=True))
        db.commit()
        return True


def _websocket_user(websocket: WebSocket) -> User | None:
    token = websocket.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    with SessionLocal() as db:
        session = db.scalar(select(UserSession).where(UserSession.token_hash == token_hash(token)))
        if not session or as_utc(session.expires_at) <= datetime.now(timezone.utc):
            return None
        user = db.get(User, session.user_id)
        if not user or not user.is_active:
            return None
        db.expunge(user)
        return user


def _websocket_session_active(websocket: WebSocket, user: User | None) -> bool:
    if not user:
        return True
    token = websocket.cookies.get(SESSION_COOKIE)
    if not token:
        return False
    with SessionLocal() as db:
        session = db.scalar(select(UserSession).where(UserSession.token_hash == token_hash(token)))
        current = db.get(User, user.id)
        return bool(
            session
            and session.user_id == user.id
            and as_utc(session.expires_at) > datetime.now(timezone.utc)
            and current
            and current.is_active
        )


def _asr_stream_active(
    websocket: WebSocket,
    user: User,
    *,
    room_id: str,
    seat_key: str,
    speech_id: str,
    control_lease: str,
) -> bool:
    token = websocket.cookies.get(SESSION_COOKIE)
    if not token:
        return False
    with SessionLocal() as db:
        session = db.scalar(select(UserSession).where(UserSession.token_hash == token_hash(token)))
        current_user = db.get(User, user.id)
        room = db.get(Room, room_id)
        seat = db.scalar(
            select(RoomSeat).where(
                RoomSeat.room_id == room_id,
                RoomSeat.seat_key == seat_key,
                RoomSeat.user_id == user.id,
            )
        )
        speech = db.get(Speech, speech_id)
        return bool(
            session
            and session.user_id == user.id
            and as_utc(session.expires_at) > datetime.now(timezone.utc)
            and current_user
            and current_user.is_active
            and room
            and can_view_room(db, room, current_user)
            and seat
            and seat.occupant_type == "human"
            and seat.control_lease == control_lease
            and speech
            and speech.status == "speaking"
        )


@router.websocket("/ws/rooms/{code}")
async def room_websocket(websocket: WebSocket, code: str) -> None:
    if not _websocket_origin_allowed(websocket):
        await websocket.close(code=4403)
        return
    user = _websocket_user(websocket)
    joined_presence: tuple[str, str] | None = None
    joined_as_spectator = False
    presence_connection_id = uuid.uuid4().hex
    presence_connected_seq: int | None = None
    public_initial_message: str | None = None
    await websocket.accept()
    send_lock = asyncio.Lock()

    async def send_json(payload: dict) -> None:
        try:
            async with send_lock:
                await websocket.send_json(payload)
        except (OSError, RuntimeError) as exc:
            raise WebSocketDisconnect(code=1006) from exc

    async def send_text(payload: str) -> None:
        try:
            async with send_lock:
                await websocket.send_text(payload)
        except (OSError, RuntimeError) as exc:
            raise WebSocketDisconnect(code=1006) from exc

    async def close_socket(code: int) -> None:
        try:
            await websocket.close(code=code)
        except (OSError, RuntimeError, WebSocketDisconnect):
            pass

    async def release_presence() -> None:
        nonlocal joined_presence
        if not user or not joined_presence:
            return
        seat_key, user_id = joined_presence
        joined_presence = None
        if not await room_hub.presence_leave(code, seat_key, user_id, connection_id=presence_connection_id):
            return
        for attempt in range(2):
            try:
                with SessionLocal() as db:
                    room = load_room(db, code, lock=True)
                    seat = db.scalar(
                        select(RoomSeat).where(
                            RoomSeat.room_id == room.id,
                            RoomSeat.seat_key == seat_key,
                            RoomSeat.user_id == user_id,
                        )
                    )
                    if not seat or not seat.connected:
                        db.rollback()
                        return
                    seat.connected = False
                    seat.disconnected_at = datetime.now(timezone.utc)
                    active_presence = room.status in ACTIVE_PRESENCE_STATUSES
                    if active_presence:
                        append_event(
                            db,
                            room,
                            "presence.disconnected",
                            {"seat_key": seat.seat_key},
                            actor_user_id=user.id,
                        )
                    db.commit()
                    if active_presence:
                        await room_hub.publish(
                            code,
                            {"type": "presence.disconnected", "room_code": code, "seq": room.seq},
                        )
                    return
            except HTTPException as exc:
                if exc.status_code != 404:
                    logger.exception("failed to release websocket presence for room %s", code)
                return
            except TRANSIENT_DATABASE_ERRORS as exc:
                if attempt == 0:
                    await asyncio.sleep(0.05)
                    continue
                logger.warning(
                    "database lock prevented websocket presence release for room %s error=%s",
                    code,
                    type(exc).__name__,
                )
                return
            except Exception:
                logger.exception("failed to release websocket presence for room %s", code)
                return

    async def release_spectator() -> None:
        nonlocal joined_as_spectator
        if not joined_as_spectator:
            return
        joined_as_spectator = False
        await room_hub.spectator_leave(code, connection_id=presence_connection_id)

    async def reserve_spectator() -> bool:
        nonlocal joined_as_spectator
        joined_as_spectator = await room_hub.spectator_join(code, connection_id=presence_connection_id)
        if not joined_as_spectator:
            await close_socket(4429)
        return joined_as_spectator

    if not user:
        # The public snapshot cache is keyed only by room code and can contain
        # QA rooms whose visibility intentionally mirrors production.  Check
        # authoritative access before reading the cache so test transcripts
        # and presence never become an anonymous WebSocket projection.
        try:
            with SessionLocal() as db:
                room = load_room(db, code)
                if not can_view_room(db, room, None):
                    await close_socket(4401)
                    return
                if not await reserve_spectator():
                    return
        except HTTPException:
            await close_socket(4404)
            return
        try:
            public_initial_message = await public_snapshot_cache.initial_message(code)
        except Exception:
            await release_spectator()
            await close_socket(4404)
            return
        if public_initial_message is None:
            await release_spectator()
            await close_socket(4401)
            return
    else:
        try:
            with SessionLocal() as db:
                room = load_room(db, code)
                if not can_view_room(db, room, user):
                    await close_socket(4403)
                    return
                seat = user_seat(room, user)
                is_spectator = seat is None and not can_control(db, room, user)
                active_seat_key = seat.seat_key if seat and room.status in ACTIVE_PRESENCE_STATUSES else None
        except HTTPException:
            await close_socket(4404)
            return
        except Exception:
            logger.exception("failed to load websocket room %s", code)
            await close_socket(1011)
            return

        if is_spectator and not await reserve_spectator():
            return

        if active_seat_key:
            await room_hub.presence_join(
                code,
                active_seat_key,
                user.id,
                connection_id=presence_connection_id,
            )
            # Register the in-memory join immediately. If the authoritative row
            # lock below times out, the common cleanup path can never leak this
            # presence count.
            joined_presence = (active_seat_key, user.id)
            try:
                # Every worker reconciles the authoritative row. The row lock
                # and connected guard still produce exactly one event, while a
                # non-first socket can repair a failed first worker commit.
                with SessionLocal() as db:
                    room = load_room(db, code, lock=True)
                    seat = user_seat(room, user)
                    if not seat or seat.seat_key != active_seat_key or room.status not in ACTIVE_PRESENCE_STATUSES:
                        await room_hub.presence_leave(
                            code, active_seat_key, user.id, connection_id=presence_connection_id
                        )
                        joined_presence = None
                    else:
                        was_connected = seat.connected
                        seat.connected = True
                        seat.disconnected_at = None
                        if not was_connected:
                            append_event(
                                db,
                                room,
                                "presence.connected",
                                {"seat_key": seat.seat_key},
                                actor_user_id=user.id,
                            )
                        db.commit()
                        if not was_connected:
                            presence_connected_seq = room.seq
            except TRANSIENT_DATABASE_ERRORS as exc:
                await room_hub.presence_leave(code, active_seat_key, user.id, connection_id=presence_connection_id)
                joined_presence = None
                logger.warning(
                    "database lock delayed websocket presence join for room %s; client may retry error=%s",
                    code,
                    type(exc).__name__,
                )
                await close_socket(1013)
                return
            except HTTPException:
                await room_hub.presence_leave(code, active_seat_key, user.id, connection_id=presence_connection_id)
                joined_presence = None
                await close_socket(4404)
                return
            except Exception:
                await room_hub.presence_leave(code, active_seat_key, user.id, connection_id=presence_connection_id)
                joined_presence = None
                logger.exception("failed to persist websocket presence join for room %s", code)
                await close_socket(1011)
                return

        try:
            with SessionLocal() as db:
                loaded = load_room(db, code)
                if not can_view_room(db, loaded, user):
                    await release_presence()
                    await release_spectator()
                    await close_socket(4403)
                    return
                snapshot = serialize_room(
                    db,
                    loaded,
                    user,
                    public=use_public_projection(db, loaded, user),
                )
        except HTTPException:
            await release_presence()
            await release_spectator()
            await close_socket(4404)
            return
        except Exception:
            logger.exception("failed to serialize websocket room %s", code)
            await release_presence()
            await release_spectator()
            await close_socket(1011)
            return
    try:
        if presence_connected_seq is not None:
            await room_hub.publish(code, {"type": "presence.connected", "room_code": code, "seq": presence_connected_seq})
        if public_initial_message is not None:
            await send_text(public_initial_message)
            initial_snapshot_seq = int(json.loads(public_initial_message)["room"]["seq"])
        else:
            await send_json({"type": "snapshot", "room": snapshot})
            initial_snapshot_seq = int(snapshot["seq"])
    except WebSocketDisconnect:
        await release_presence()
        await release_spectator()
        return

    async def synchronize_presence() -> list[tuple[str, int]]:
        nonlocal joined_presence
        if not user:
            return []
        with SessionLocal() as read_db:
            current_room = load_room(read_db, code)
            current_seat = user_seat(current_room, user)
            should_spectate = current_seat is None and not can_control(read_db, current_room, user)
            desired = (current_seat.seat_key, user.id) if current_seat and current_room.status in ACTIVE_PRESENCE_STATUSES else None
        if should_spectate and not joined_as_spectator:
            if not await reserve_spectator():
                raise WebSocketDisconnect(code=4429)
        elif not should_spectate and joined_as_spectator:
            await release_spectator()
        if desired == joined_presence:
            return []
        events: list[tuple[str, int]] = []
        if joined_presence:
            old_seat_key, old_user_id = joined_presence
            if await room_hub.presence_leave(
                code, old_seat_key, old_user_id, connection_id=presence_connection_id
            ):
                with SessionLocal() as db:
                    current_room = load_room(db, code, lock=True)
                    old_seat = db.scalar(
                        select(RoomSeat).where(
                            RoomSeat.room_id == current_room.id,
                            RoomSeat.seat_key == old_seat_key,
                            RoomSeat.user_id == old_user_id,
                        )
                    )
                    if old_seat and old_seat.connected:
                        old_seat.connected = False
                        old_seat.disconnected_at = datetime.now(timezone.utc)
                        if current_room.status in ACTIVE_PRESENCE_STATUSES:
                            append_event(
                                db,
                                current_room,
                                "presence.disconnected",
                                {"seat_key": old_seat_key},
                                actor_user_id=old_user_id,
                            )
                        db.commit()
                        if current_room.status in ACTIVE_PRESENCE_STATUSES:
                            events.append(("presence.disconnected", current_room.seq))
            joined_presence = None
        if desired:
            seat_key, user_id = desired
            await room_hub.presence_join(code, seat_key, user_id, connection_id=presence_connection_id)
            joined_presence = desired
            with SessionLocal() as db:
                current_room = load_room(db, code, lock=True)
                seat = db.scalar(
                    select(RoomSeat).where(
                        RoomSeat.room_id == current_room.id,
                        RoomSeat.seat_key == seat_key,
                        RoomSeat.user_id == user_id,
                    )
                )
                if seat and current_room.status in ACTIVE_PRESENCE_STATUSES:
                    joined_presence = desired
                    was_connected = seat.connected
                    seat.connected = True
                    seat.disconnected_at = None
                    if not was_connected:
                        append_event(
                            db,
                            current_room,
                            "presence.connected",
                            {"seat_key": seat_key},
                            actor_user_id=user_id,
                        )
                    db.commit()
                    if not was_connected:
                        events.append(("presence.connected", current_room.seq))
                else:
                    await room_hub.presence_leave(code, seat_key, user_id, connection_id=presence_connection_id)
                    joined_presence = None
        return events

    async def sender() -> None:
        nonlocal initial_snapshot_seq
        async for message in room_hub.stream(code, initial_sync=True):
            if user and joined_presence:
                await room_hub.presence_refresh(
                    code,
                    joined_presence[0],
                    joined_presence[1],
                    connection_id=presence_connection_id,
                )
            if joined_as_spectator and not await room_hub.spectator_refresh(
                code, connection_id=presence_connection_id
            ):
                await close_socket(4429)
                return
            if not _websocket_session_active(websocket, user):
                await close_socket(4401)
                return
            if user:
                with SessionLocal() as db:
                    room = load_room(db, code)
                    if not can_view_room(db, room, user):
                        await close_socket(4403)
                        return
                presence_events = await synchronize_presence()
                # A socket may have connected before the user claimed a seat.
                # Persist the dynamically-bound presence first, then build the
                # snapshot so the claim event cannot expose my_seat together
                # with a stale connected=false value.
                with SessionLocal() as db:
                    room = load_room(db, code)
                    projection = serialize_room(db, room, user, public=use_public_projection(db, room, user))
                for event_type, seq in presence_events:
                    await room_hub.publish(code, {"type": event_type, "room_code": code, "seq": seq})
            else:
                with SessionLocal() as db:
                    room = load_room(db, code)
                    if not can_view_room(db, room, None):
                        await close_socket(4401)
                        return
                if message.get("type") == "_sync":
                    projection = await public_snapshot_cache.get_if_newer(code, known_seq=initial_snapshot_seq)
                else:
                    expected_seq = message.get("seq") if isinstance(message.get("seq"), int) else None
                    projection = await public_snapshot_cache.get(code, expected_seq=expected_seq)
                if projection is None:
                    await close_socket(4401)
                    return
            if message.get("type") == "_sync":
                # This second snapshot closes the initial snapshot/subscription
                # race.  It intentionally has no live event: consumers should
                # reconcile state without replaying an action that may already
                # be represented by the first snapshot.
                projection_seq = int(projection.get("seq", 0))
                if projection_seq > initial_snapshot_seq:
                    await send_json({"type": "snapshot", "room": projection})
                    initial_snapshot_seq = projection_seq
                continue
            outgoing_event = message if user else anonymous_realtime_event(message)
            await send_json({"type": "snapshot", "event": outgoing_event, "room": projection})

    async def receiver() -> None:
        while True:
            try:
                data = await websocket.receive_json()
            except (OSError, RuntimeError) as exc:
                raise WebSocketDisconnect(code=1006) from exc
            if data.get("type") == "ping":
                if not _websocket_session_active(websocket, user):
                    await close_socket(4401)
                    return
                with SessionLocal() as db:
                    room = load_room(db, code)
                    if not can_view_room(db, room, user):
                        await close_socket(4403 if user else 4401)
                        return
                if joined_as_spectator and not await room_hub.spectator_refresh(
                    code, connection_id=presence_connection_id
                ):
                    await close_socket(4429)
                    return
                await send_json({"type": "pong"})

    tasks = [asyncio.create_task(sender()), asyncio.create_task(receiver())]

    def clear_handler_cancellation() -> None:
        current = asyncio.current_task()
        if current is None:
            return
        while current.cancelling():
            current.uncancel()

    try:
        done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for completed in done:
            try:
                completed.result()
            except WebSocketDisconnect:
                pass
            except TRANSIENT_DATABASE_ERRORS as exc:
                logger.warning(
                    "database lock interrupted websocket synchronization for room %s; client may retry error=%s",
                    code,
                    type(exc).__name__,
                )
                await close_socket(1013)
            except Exception:
                logger.exception("room websocket task failed for room %s", code)
                await close_socket(1011)
    except WebSocketDisconnect:
        pass
    except asyncio.CancelledError:
        # Starlette cancels the ASGI handler when a client context closes. Keep
        # that expected shutdown from cancelling the cleanup task as well.
        clear_handler_cancellation()
    finally:

        async def cleanup() -> None:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await release_presence()
            await release_spectator()

        cleanup_task = asyncio.create_task(cleanup())
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                clear_handler_cancellation()
        cleanup_task.result()


@router.websocket("/ws/rooms/{code}/audio")
async def room_audio_websocket(websocket: WebSocket, code: str) -> None:
    """Stream fixed 40 ms PCM frames from the engine's shared generation file."""

    if not _websocket_origin_allowed(websocket):
        await websocket.close(code=4403)
        return
    user = _websocket_user(websocket)
    with SessionLocal() as db:
        try:
            room = load_room(db, code)
        except HTTPException:
            await websocket.close(code=4404)
            return
        if not can_view_room(db, room, user):
            await websocket.close(code=4401 if not user else 4403)
            return
    await websocket.accept()
    send_lock = asyncio.Lock()

    async def send_json(payload: dict) -> None:
        try:
            async with send_lock:
                await asyncio.wait_for(websocket.send_json(payload), timeout=2)
        except (asyncio.TimeoutError, OSError, RuntimeError) as exc:
            raise WebSocketDisconnect(code=1013) from exc

    async def send_bytes(payload: bytes) -> None:
        try:
            async with send_lock:
                await asyncio.wait_for(websocket.send_bytes(payload), timeout=2)
        except (asyncio.TimeoutError, OSError, RuntimeError) as exc:
            raise WebSocketDisconnect(code=1013) from exc

    try:
        subscription = await asyncio.wait_for(websocket.receive_json(), timeout=5)
    except (asyncio.TimeoutError, OSError, RuntimeError, WebSocketDisconnect):
        await websocket.close(code=4409)
        return
    if set(subscription) - {"type", "protocol_version", "speech_id", "generation", "after_seq", "flow_control"}:
        await websocket.close(code=4400)
        return
    if (
        subscription.get("type") != "subscribe"
        or subscription.get("protocol_version") != AUDIO_STREAM_PROTOCOL_VERSION
        or not isinstance(subscription.get("speech_id"), str)
        or not isinstance(subscription.get("generation"), str)
        or not AUDIO_STREAM_GENERATION_RE.fullmatch(subscription["generation"])
        or type(subscription.get("after_seq", -1)) is not int
        or int(subscription.get("after_seq", -1)) < -1
        or int(subscription.get("after_seq", -1)) > 2_000_000
        or type(subscription.get("flow_control", False)) is not bool
    ):
        await websocket.close(code=4409)
        return

    speech_id = subscription["speech_id"]
    generation = subscription["generation"]
    after_seq = int(subscription.get("after_seq", -1))
    flow_control = bool(subscription.get("flow_control", False))
    with SessionLocal() as db:
        room = load_room(db, code)
        current_user = db.get(User, user.id) if user else None
        speech = db.get(Speech, speech_id)
        if (
            not can_view_room(db, room, current_user)
            or not speech
            or speech.room_id != room.id
            or speech.stream_generation != generation
            or speech.stream_sample_rate <= 0
            or speech.status not in {"synthesizing", "playing"}
        ):
            await websocket.close(code=4409)
            return
        sample_rate = speech.stream_sample_rate
        playback_started_at = speech.playback_started_at.isoformat() if speech.playback_started_at else None

    room_directory = (settings.media_path / code).resolve()
    part = room_directory / f".{speech_id}.{generation}.wav.part"
    final = room_directory / f"{speech_id}.wav"
    if part.is_symlink() or final.is_symlink():
        await websocket.close(code=4409)
        return
    source = part if part.is_file() else final if final.is_file() else None
    if source is None or source.resolve().parent != room_directory:
        await websocket.close(code=4409)
        return
    try:
        handle = source.open("rb")
    except FileNotFoundError:
        if not final.is_file() or final.resolve().parent != room_directory:
            await websocket.close(code=4409)
            return
        handle = final.open("rb")

    frame_samples = max(1, round(sample_rate * 0.04))
    frame_bytes = frame_samples * AUDIO_STREAM_SAMPLE_WIDTH
    requested_start_seq = after_seq + 1
    try:
        available_bytes = max(0, source.stat().st_size - AUDIO_STREAM_WAV_HEADER_BYTES)
    except FileNotFoundError:
        available_bytes = 0
    available_samples = available_bytes // AUDIO_STREAM_SAMPLE_WIDTH
    available_frame_count = (available_samples + frame_samples - 1) // frame_samples
    replay_frame_count = max(1, round(AUDIO_STREAM_REPLAY_SECONDS / 0.04))
    start_seq = requested_start_seq
    if requested_start_seq >= available_frame_count:
        start_seq = max(0, available_frame_count - replay_frame_count)
    cursor = AUDIO_STREAM_WAV_HEADER_BYTES + start_seq * frame_bytes
    generation_id = int(generation[:8], 16)
    flow_seen = False
    client_buffered_ms = 0
    frames_since_flow = 0
    flow_updated = asyncio.Event()
    abort_waiter = audio_stream_aborts.register(code, speech_id, generation)

    async def access_active() -> tuple[bool, int]:
        if user and not _websocket_session_active(websocket, user):
            return False, 4401
        with SessionLocal() as db:
            try:
                active_room = load_room(db, code)
            except HTTPException:
                return False, 4404
            active_user = db.get(User, user.id) if user else None
            if not can_view_room(db, active_room, active_user):
                return False, 4403
            active_speech = db.get(Speech, speech_id)
            if not active_speech or active_speech.room_id != active_room.id:
                return False, 4409
            if active_speech.stream_generation != generation or active_speech.status not in {"synthesizing", "playing"}:
                return False, 4410
        return True, 1000

    async def sender() -> None:
        nonlocal cursor, frames_since_flow
        seq = start_seq
        pts_samples = start_seq * frame_samples
        pending = bytearray()
        last_access_check = monotonic()

        async def send_abort() -> None:
            await send_json(
                {
                    "type": "audio.abort",
                    "speech_id": speech_id,
                    "generation": generation,
                    "final_seq": seq,
                    "total_samples": pts_samples,
                }
            )

        async def wait_for_client_capacity() -> bool:
            nonlocal last_access_check
            if abort_waiter.event.is_set():
                return False
            if not flow_control:
                return True
            while True:
                if abort_waiter.event.is_set():
                    return False
                burst_limit = AUDIO_STREAM_FEEDBACK_BURST_FRAMES if flow_seen else AUDIO_STREAM_INITIAL_BURST_FRAMES
                if frames_since_flow < burst_limit and (not flow_seen or client_buffered_ms < AUDIO_STREAM_CLIENT_HIGH_WATER_MS):
                    return True
                flow_updated.clear()
                burst_limit = AUDIO_STREAM_FEEDBACK_BURST_FRAMES if flow_seen else AUDIO_STREAM_INITIAL_BURST_FRAMES
                if frames_since_flow < burst_limit and (not flow_seen or client_buffered_ms < AUDIO_STREAM_CLIENT_HIGH_WATER_MS):
                    continue
                flow_wait = asyncio.create_task(flow_updated.wait())
                abort_wait = asyncio.create_task(abort_waiter.event.wait())
                done, pending_tasks = await asyncio.wait(
                    {flow_wait, abort_wait},
                    timeout=0.5,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending_tasks:
                    task.cancel()
                if pending_tasks:
                    await asyncio.gather(*pending_tasks, return_exceptions=True)
                if abort_wait in done:
                    return False
                if not done:
                    if monotonic() - last_access_check >= 1:
                        allowed, close_code = await access_active()
                        last_access_check = monotonic()
                        if not allowed:
                            await websocket.close(code=close_code)
                            return False

        await send_json(
            {
                "type": "audio.start",
                "protocol_version": AUDIO_STREAM_PROTOCOL_VERSION,
                "speech_id": speech_id,
                "generation": generation,
                "encoding": "pcm_s16le",
                "sample_rate": sample_rate,
                "channels": 1,
                "frame_samples": frame_samples,
                "start_seq": start_seq,
                "start_pts_samples": pts_samples,
                "requested_start_seq": requested_start_seq,
                "available_samples": available_samples,
                "clamped": start_seq != requested_start_seq,
                "playback_started_at": playback_started_at,
                "replay": start_seq > 0,
            }
        )
        while True:
            if abort_waiter.event.is_set():
                await send_abort()
                return
            if monotonic() - last_access_check >= 1:
                allowed, close_code = await access_active()
                last_access_check = monotonic()
                if not allowed:
                    await websocket.close(code=close_code)
                    return
            handle.seek(cursor)
            chunk = handle.read(32_768)
            if chunk:
                cursor += len(chunk)
                pending.extend(chunk)
                while len(pending) >= frame_bytes:
                    if not await wait_for_client_capacity():
                        if abort_waiter.event.is_set():
                            await send_abort()
                        return
                    pcm = bytes(pending[:frame_bytes])
                    del pending[:frame_bytes]
                    header = AUDIO_STREAM_BINARY_HEADER.pack(
                        b"JX",
                        AUDIO_STREAM_PROTOCOL_VERSION,
                        1 if start_seq > 0 else 0,
                        generation_id,
                        seq,
                        pts_samples,
                        frame_samples,
                    )
                    await send_bytes(header + pcm)
                    frames_since_flow += 1
                    seq += 1
                    pts_samples += frame_samples
                continue
            if final.is_file() or not part.exists():
                if pending:
                    usable = len(pending) - (len(pending) % AUDIO_STREAM_SAMPLE_WIDTH)
                    if usable:
                        if not await wait_for_client_capacity():
                            if abort_waiter.event.is_set():
                                await send_abort()
                            return
                        sample_count = usable // AUDIO_STREAM_SAMPLE_WIDTH
                        header = AUDIO_STREAM_BINARY_HEADER.pack(
                            b"JX",
                            AUDIO_STREAM_PROTOCOL_VERSION,
                            1 if start_seq > 0 else 0,
                            generation_id,
                            seq,
                            pts_samples,
                            sample_count,
                        )
                        await send_bytes(header + bytes(pending[:usable]))
                        frames_since_flow += 1
                        pts_samples += sample_count
                await send_json(
                    {
                        "type": "audio.final" if final.is_file() else "audio.abort",
                        "speech_id": speech_id,
                        "generation": generation,
                        "final_seq": seq,
                        "total_samples": pts_samples,
                    }
                )
                return
            await asyncio.sleep(settings.lighttts_stream_poll_seconds)

    async def receiver() -> None:
        nonlocal flow_seen, client_buffered_ms, frames_since_flow
        while True:
            try:
                packet = await websocket.receive()
            except (OSError, RuntimeError) as exc:
                raise WebSocketDisconnect(code=1006) from exc
            if packet.get("bytes") is not None:
                await websocket.close(code=1003)
                return
            text = packet.get("text")
            if text is None:
                raise WebSocketDisconnect(code=1000)
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                await websocket.close(code=4400)
                return
            if data == {"type": "ping"}:
                await send_json({"type": "pong"})
                continue
            if (
                flow_control
                and isinstance(data, dict)
                and set(data) == {"type", "protocol_version", "generation", "buffered_ms", "played_ms"}
                and data.get("type") == "flow"
                and data.get("protocol_version") == AUDIO_STREAM_PROTOCOL_VERSION
                and data.get("generation") == generation
                and type(data.get("buffered_ms")) is int
                and 0 <= data["buffered_ms"] <= 30_000
                and type(data.get("played_ms")) is int
                and 0 <= data["played_ms"] <= 3_600_000
            ):
                flow_seen = True
                client_buffered_ms = data["buffered_ms"]
                frames_since_flow = 0
                flow_updated.set()
                continue
            await websocket.close(code=4400)
            return

    tasks = [asyncio.create_task(sender()), asyncio.create_task(receiver())]
    try:
        done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for completed in done:
            completed.result()
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    finally:
        handle.close()
        audio_stream_aborts.unregister(code, speech_id, generation, abort_waiter)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@router.websocket("/ws/rooms/{code}/asr")
async def asr_websocket(websocket: WebSocket, code: str) -> None:
    if not _websocket_origin_allowed(websocket):
        await websocket.close(code=4403)
        return
    user = _websocket_user(websocket)
    if not user:
        await websocket.close(code=4401)
        return
    with SessionLocal() as db:
        room = load_room(db, code)
        if not can_view_room(db, room, user):
            await websocket.close(code=4403)
            return
        seat = user_seat(room, user)
        if not seat:
            await websocket.close(code=4403)
            return
        seat_key = seat.seat_key
        speech = db.scalar(
            select(Speech).where(
                Speech.room_id == room.id,
                Speech.seat_key == seat_key,
                Speech.status == "speaking",
            )
        )
        if not speech:
            await websocket.close(code=4403)
            return
        speech_id = speech.id
        room_id = room.id
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        asr_config = runtime_provider_config(match.service_snapshot if match else None, "funasr")
    await websocket.accept()

    async def send_json(payload: dict) -> None:
        try:
            await websocket.send_json(payload)
        except (OSError, RuntimeError) as exc:
            raise WebSocketDisconnect(code=1006) from exc

    async def close_socket(close_code: int = 1000) -> None:
        try:
            await websocket.close(code=close_code)
        except (OSError, RuntimeError, WebSocketDisconnect):
            pass

    try:
        authentication = await asyncio.wait_for(websocket.receive_json(), timeout=5)
    except (asyncio.TimeoutError, WebSocketDisconnect, OSError, RuntimeError, ValueError):
        await close_socket(4409)
        return
    if not isinstance(authentication, dict):
        await close_socket(4409)
        return
    control_lease = str(authentication.get("lease") or "").strip()
    if (
        authentication.get("type") != "authenticate"
        or not control_lease
        or len(control_lease) > 64
        or not _asr_stream_active(
            websocket,
            user,
            room_id=room_id,
            seat_key=seat_key,
            speech_id=speech_id,
            control_lease=control_lease,
        )
    ):
        await close_socket(4409)
        return
    if not _asr_protocol_supported(authentication):
        try:
            await send_json(
                {
                    "type": "error",
                    "code": "unsupported_asr_protocol",
                    "message": "语音识别协议不兼容，录音仍会正常保存。",
                }
            )
        except WebSocketDisconnect:
            pass
        await close_socket(4400)
        return
    if not asr_config.get("enabled") or not str(asr_config.get("endpoint") or "").strip():
        try:
            await send_json({"type": "error", "message": "语音识别服务当前未启用，请使用文字补录。"})
        except WebSocketDisconnect:
            pass
        await close_socket(1013)
        return
    if not _claim_asr_stream(speech_id):
        await close_socket(4409)
        return
    asr_endpoint = str(asr_config["endpoint"])
    asr_settings = asr_config.get("settings") if isinstance(asr_config.get("settings"), dict) else {}
    try:
        final_wait_seconds = max(5.0, min(60.0, float(asr_settings.get("final_wait_seconds", 30.0))))
    except (TypeError, ValueError):
        final_wait_seconds = 30.0
    try:
        await send_json({"type": "ready", "speech_id": speech_id})
        async with websockets.connect(asr_endpoint, max_size=8 * 1024 * 1024) as upstream:
            # The deployed local FunASR adapter uses a small command protocol,
            # not the upstream FunASR JSON handshake.
            await upstream.send("START")
            await upstream.send("LANGUAGE:zh-CN")

            final_received = asyncio.Event()
            voice_activity = PcmVoiceActivity(minimum_voiced_samples=MIN_ASR_VOICED_SAMPLES)

            async def browser_to_asr() -> None:
                last_validation = monotonic()
                while True:
                    try:
                        message = await websocket.receive()
                    except (OSError, RuntimeError) as exc:
                        raise WebSocketDisconnect(code=1006) from exc
                    current_time = monotonic()
                    if current_time - last_validation >= 1:
                        if not _asr_stream_active(
                            websocket,
                            user,
                            room_id=room_id,
                            seat_key=seat_key,
                            speech_id=speech_id,
                            control_lease=control_lease,
                        ):
                            await close_socket(4409)
                            return
                        last_validation = current_time
                    if message.get("bytes") is not None:
                        audio_bytes = message["bytes"]
                        voice_activity.observe(audio_bytes)
                        await upstream.send(audio_bytes)
                    elif message.get("text"):
                        control = json.loads(message["text"])
                        if control.get("type") == "finish":
                            await upstream.send("STOP")
                            # FunASR's 2-pass final sentence arrives after the
                            # browser signals the end of speech.  Keep the
                            # upstream reader alive briefly instead of winning
                            # FIRST_COMPLETED and cancelling it immediately.
                            try:
                                await asyncio.wait_for(final_received.wait(), timeout=final_wait_seconds)
                            except asyncio.TimeoutError:
                                pass
                            return

            async def asr_to_browser() -> None:
                last_validation = monotonic()
                async for raw in upstream:
                    try:
                        payload = json.loads(raw)
                    except (TypeError, json.JSONDecodeError):
                        continue
                    sentences = payload.get("sentences") if isinstance(payload.get("sentences"), list) else []
                    sentence_text = "".join(str(item.get("text") or "") for item in sentences if isinstance(item, dict))
                    text = str(payload.get("text") or payload.get("partial") or sentence_text).strip()
                    if text:
                        is_final = bool(payload.get("is_final")) or payload.get("mode") in {"2pass-offline", "offline"}
                        confidence = payload_confidence(payload)
                        current_time = monotonic()
                        if is_final or current_time - last_validation >= 1:
                            if not _asr_stream_active(
                                websocket,
                                user,
                                room_id=room_id,
                                seat_key=seat_key,
                                speech_id=speech_id,
                                control_lease=control_lease,
                            ):
                                await close_socket(4409)
                                return
                            last_validation = current_time
                        rejection_reason = transcript_rejection_reason(
                            text,
                            confidence=confidence,
                            require_substantive=True,
                        )
                        if is_final and (not voice_activity.has_voice or rejection_reason):
                            outgoing = {
                                "type": "asr_rejected",
                                "room_code": code,
                                "reason": "silence" if not voice_activity.has_voice else rejection_reason,
                                "seat_key": seat_key,
                                "speech_id": speech_id,
                            }
                            await send_json(outgoing)
                            final_received.set()
                            return
                        if not is_final and (not voice_activity.has_voice or rejection_reason):
                            continue
                        if is_final:
                            persist_asr_final(
                                speech_id,
                                text,
                                confidence=confidence,
                                voice_detected=voice_activity.has_voice,
                            )
                        outgoing = {
                            "type": "asr",
                            "room_code": code,
                            "text": text,
                            "is_final": is_final,
                            "seat_key": seat_key,
                            "speech_id": speech_id,
                        }
                        await send_json(outgoing)
                        await room_hub.publish(code, outgoing)
                        if is_final:
                            final_received.set()
                            return
                if not final_received.is_set():
                    raise RuntimeError("ASR upstream closed before a final result")

            tasks = [asyncio.create_task(browser_to_asr()), asyncio.create_task(asr_to_browser())]
            done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in tasks:
                task.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for task, result in zip(tasks, results):
                if task in done and isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                    raise result
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    except Exception:
        logger.exception("ASR bridge failed for room %s", code)
        try:
            await send_json({"type": "error", "message": "语音识别服务暂时不可用，录音仍可继续。"})
        except WebSocketDisconnect:
            logger.debug("ASR error could not be delivered to the browser", exc_info=True)
    finally:
        _release_asr_stream(speech_id)
        await close_socket()
