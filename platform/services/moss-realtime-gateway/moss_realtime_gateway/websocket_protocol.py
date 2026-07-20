from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from moss_realtime_gateway.backends import CanaryPcmPayload
from moss_realtime_gateway.config import GatewaySettings
from moss_realtime_gateway.low_latency_bridge import CanaryStageTiming
from moss_realtime_gateway.runtime import GatewayError, GatewayRuntime, Session

_MAX_TEXT_FRAME_CHARACTERS = 16_384
_DEFAULT_USER_TEXT = "请使用普通话进行自然、清晰、稳定的辩论发言。"


class _StrictMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StartMessage(_StrictMessage):
    type: Literal["start"]
    seq: Literal[0]
    session_id: str = Field(min_length=1, max_length=128)
    voice: str = Field(min_length=1, max_length=64)
    user_text: str | None = Field(default=None, max_length=2_000)
    canary_observe: bool = False


class DeltaMessage(_StrictMessage):
    type: Literal["text_delta"]
    seq: Annotated[int, Field(strict=True, ge=1)]
    text: str = Field(min_length=1, max_length=4_096)


class FinalMessage(_StrictMessage):
    type: Literal["final"]
    seq: Annotated[int, Field(strict=True, ge=1)]


class AbortMessage(_StrictMessage):
    type: Literal["abort"]
    reason: str = Field(default="client_abort", max_length=256)


class PingMessage(_StrictMessage):
    type: Literal["ping"]
    id: str | int | None = None


class HealthMessage(_StrictMessage):
    type: Literal["health"]


ClientMessage = StartMessage | DeltaMessage | FinalMessage | AbortMessage | PingMessage | HealthMessage
_MESSAGE_MODELS: dict[str, type[_StrictMessage]] = {
    "start": StartMessage,
    "text_delta": DeltaMessage,
    "final": FinalMessage,
    "abort": AbortMessage,
    "ping": PingMessage,
    "health": HealthMessage,
}


@dataclass
class ProtocolViolation(Exception):
    code: str
    detail: str


@dataclass
class DisconnectNotice:
    code: int


@dataclass
class OutboundFrame:
    kind: Literal["json", "canary", "pcm", "close"]
    payload: Any
    sent: asyncio.Future[None] | None = None


class WebSocketSender:
    def __init__(self, websocket: WebSocket) -> None:
        self.websocket = websocket
        self.queue: asyncio.Queue[OutboundFrame] = asyncio.Queue(maxsize=32)
        self.enqueue_lock = asyncio.Lock()
        self.writer_task = asyncio.create_task(self._writer())

    async def _writer(self) -> None:
        frame: OutboundFrame | None = None
        try:
            while True:
                frame = await self.queue.get()
                if frame.kind in {"json", "canary"}:
                    await self.websocket.send_json(frame.payload)
                elif frame.kind == "pcm":
                    await self.websocket.send_bytes(frame.payload)
                else:
                    code, reason = frame.payload
                    await self.websocket.close(code=code, reason=reason)
                if frame.sent is not None and not frame.sent.done():
                    frame.sent.set_result(None)
                if frame.kind == "close":
                    return
        except BaseException as exc:
            if frame is not None and frame.sent is not None and not frame.sent.done():
                frame.sent.set_exception(exc)
            while True:
                try:
                    frame = self.queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if frame.sent is not None and not frame.sent.done():
                    frame.sent.set_exception(exc)
            raise

    async def _enqueue(self, frame: OutboundFrame) -> None:
        if self.writer_task.done():
            await self.writer_task
        async with self.enqueue_lock:
            await self.queue.put(frame)

    async def json(self, payload: dict[str, Any]) -> None:
        sent = asyncio.get_running_loop().create_future()
        await self._enqueue(OutboundFrame("json", payload, sent))
        await sent

    async def pcm(self, payload: bytes) -> None:
        await self._enqueue(OutboundFrame("pcm", payload))

    async def canary(self, payload: dict[str, Any]) -> None:
        await self._enqueue(OutboundFrame("canary", payload))

    async def drop_pending_pcm(self) -> None:
        async with self.enqueue_lock:
            retained: list[OutboundFrame] = []
            while True:
                try:
                    frame = self.queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if frame.kind not in {"canary", "pcm"}:
                    retained.append(frame)
            for frame in retained:
                self.queue.put_nowait(frame)

    async def close(self, *, code: int, reason: str = "") -> None:
        if self.writer_task.done():
            return
        sent = asyncio.get_running_loop().create_future()
        await self._enqueue(OutboundFrame("close", (code, reason), sent))
        await asyncio.gather(sent, return_exceptions=True)

    async def shutdown(self) -> None:
        if not self.writer_task.done():
            self.writer_task.cancel()
        await asyncio.gather(self.writer_task, return_exceptions=True)


def _contains_thinking(value: Any) -> bool:
    if isinstance(value, dict):
        return "thinking" in value or any(_contains_thinking(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_thinking(item) for item in value)
    return False


async def _receive_payload(websocket: WebSocket) -> dict[str, Any]:
    message = await websocket.receive()
    if message["type"] == "websocket.disconnect":
        raise WebSocketDisconnect(message.get("code", 1000), message.get("reason"))
    if message.get("bytes") is not None:
        raise ProtocolViolation("binary_upstream_not_allowed", "client messages must be JSON text frames")
    raw = message.get("text")
    if not isinstance(raw, str):
        raise ProtocolViolation("invalid_frame", "client messages must be JSON text frames")
    if len(raw) > _MAX_TEXT_FRAME_CHARACTERS:
        raise ProtocolViolation("text_too_long", "WebSocket text frame exceeds the fixed safety limit")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProtocolViolation("invalid_json", "message is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ProtocolViolation("invalid_message", "message must be a JSON object")
    if _contains_thinking(payload):
        raise ProtocolViolation("thinking_not_allowed", "thinking content must never be sent to TTS")
    return payload


def _parse_message(payload: dict[str, Any]) -> ClientMessage:
    message_type = payload.get("type")
    if not isinstance(message_type, str) or message_type not in _MESSAGE_MODELS:
        raise ProtocolViolation("unknown_message", "unsupported WebSocket message type")
    if message_type == "text_delta" and isinstance(payload.get("text"), str) and len(payload["text"]) > 4_096:
        raise ProtocolViolation("text_too_long", "text_delta text exceeds 4096 characters")
    try:
        return _MESSAGE_MODELS[message_type].model_validate(payload)
    except ValidationError as exc:
        raise ProtocolViolation("invalid_message", f"invalid {message_type} message") from exc


async def _audio_pump(runtime: GatewayRuntime, session: Session, sender: WebSocketSender) -> None:
    chunk_index = 0
    while True:
        payload = await runtime.audio_item(session)
        if payload is None:
            return
        if isinstance(payload, CanaryStageTiming):
            await sender.canary(
                {
                    "type": "canary.stage",
                    "session_id": session.session_id,
                    "stage": payload.stage,
                    "stage_index": payload.stage_index,
                    "duration_ms": payload.duration_ms,
                    "turn_elapsed_ms": payload.turn_elapsed_ms,
                    "source_stage": payload.source_stage,
                    "source_stage_index": payload.source_stage_index,
                }
            )
            continue
        if isinstance(payload, CanaryPcmPayload):
            await sender.canary(
                {
                    "type": "canary.pcm",
                    "session_id": session.session_id,
                    "chunk_index": chunk_index,
                    "source_stage": payload.source_stage,
                    "source_stage_index": payload.source_stage_index,
                    "source_stage_duration_ms": payload.source_stage_duration_ms,
                    "decoder_stage_index": payload.decoder_stage_index,
                    "decoder_yield_ms": payload.decoder_yield_ms,
                    "turn_elapsed_ms": payload.turn_elapsed_ms,
                }
            )
            await sender.pcm(payload.pcm16)
        else:
            await sender.pcm(payload)
        chunk_index += 1


async def _await_audio(audio_task: asyncio.Task[None] | None) -> None:
    if audio_task is not None:
        await asyncio.gather(audio_task, return_exceptions=True)


async def _cancel_audio(audio_task: asyncio.Task[None] | None) -> None:
    if audio_task is not None and not audio_task.done():
        audio_task.cancel()
    if audio_task is not None:
        await asyncio.gather(audio_task, return_exceptions=True)


def _released_payload(session_id: str, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "released",
        "session_id": session_id,
        "status": result["status"],
        "released": result["released"],
    }


async def _command_receiver(
    websocket: WebSocket,
    sender: WebSocketSender,
    runtime: GatewayRuntime,
    commands: asyncio.Queue[ClientMessage | ProtocolViolation | DisconnectNotice],
    terminating: asyncio.Event,
    ensure_abort: Callable[[str], asyncio.Task[dict[str, Any]]],
) -> None:
    try:
        while True:
            try:
                message = _parse_message(await _receive_payload(websocket))
            except ProtocolViolation as exc:
                ensure_abort("websocket_protocol_error")
                await commands.put(exc)
                return
            if isinstance(message, PingMessage):
                await sender.json({"type": "pong", "id": message.id})
                continue
            if isinstance(message, HealthMessage):
                await sender.json({"type": "health", **runtime.health()})
                continue
            if isinstance(message, AbortMessage):
                terminating.set()
                ensure_abort(message.reason or "client_abort")
                await commands.put(message)
                return
            if terminating.is_set():
                violation = ProtocolViolation(
                    "session_terminating",
                    "no delta, start, or repeated final is valid after final",
                )
                ensure_abort("websocket_protocol_error")
                await commands.put(violation)
                return
            if isinstance(message, FinalMessage):
                terminating.set()
            await commands.put(message)
    except WebSocketDisconnect as exc:
        ensure_abort("websocket_disconnect")
        await commands.put(DisconnectNotice(exc.code))
    except RuntimeError:
        ensure_abort("websocket_disconnect")
        await commands.put(DisconnectNotice(1006))


def add_websocket_protocol(application: FastAPI, runtime: GatewayRuntime, settings: GatewaySettings) -> None:
    @application.websocket("/tts/session/ws")
    async def realtime_tts(websocket: WebSocket) -> None:
        supplied_key = websocket.headers.get("X-MOSS-Gateway-Key")
        if settings.api_key and not (supplied_key and secrets.compare_digest(supplied_key, settings.api_key)):
            await websocket.send_denial_response(
                JSONResponse({"detail": "gateway authentication failed"}, status_code=401)
            )
            return

        await websocket.accept()
        sender = WebSocketSender(websocket)
        session_id: str | None = None
        session: Session | None = None
        audio_task: asyncio.Task[None] | None = None
        receiver_task: asyncio.Task[None] | None = None
        abort_task: asyncio.Task[dict[str, Any]] | None = None
        released = False
        try:
            start_message: StartMessage | None = None
            while start_message is None:
                message = _parse_message(await _receive_payload(websocket))
                if isinstance(message, PingMessage):
                    await sender.json({"type": "pong", "id": message.id})
                elif isinstance(message, HealthMessage):
                    await sender.json({"type": "health", **runtime.health()})
                elif isinstance(message, StartMessage):
                    start_message = message
                else:
                    raise ProtocolViolation("start_required", "start must precede synthesis messages")

            session_id = start_message.session_id
            start_result = await runtime.start_session(
                session_id=session_id,
                prompt_audio=start_message.voice,
                user_text=start_message.user_text or _DEFAULT_USER_TEXT,
                assistant_text="",
                canary_observe=start_message.canary_observe,
            )
            session = runtime.claim_audio(session_id)
            await sender.json(
                {
                    "type": "ready",
                    "session_id": session_id,
                    "voice": start_message.voice,
                    "next_seq": 1,
                    "audio": {"codec": "pcm_s16le", "sample_rate": settings.sample_rate, "channels": 1},
                    "canary_observe": bool(start_result.get("canary_observe")),
                }
            )
            audio_task = asyncio.create_task(_audio_pump(runtime, session, sender))
            commands: asyncio.Queue[ClientMessage | ProtocolViolation | DisconnectNotice] = asyncio.Queue()
            terminating = asyncio.Event()

            def ensure_abort(reason: str) -> asyncio.Task[dict[str, Any]]:
                nonlocal abort_task
                if abort_task is None:
                    abort_task = asyncio.create_task(runtime.abort_session(session_id, reason=reason))
                return abort_task

            receiver_task = asyncio.create_task(
                _command_receiver(websocket, sender, runtime, commands, terminating, ensure_abort)
            )
            expected_seq = 1

            while True:
                message = await commands.get()
                if isinstance(message, DisconnectNotice):
                    if abort_task is not None:
                        await asyncio.gather(abort_task, return_exceptions=True)
                    return
                if isinstance(message, ProtocolViolation):
                    raise message
                if isinstance(message, DeltaMessage):
                    if message.seq != expected_seq:
                        raise ProtocolViolation(
                            "invalid_seq",
                            f"expected delta seq {expected_seq}, received {message.seq}",
                        )
                    await runtime.push_text(session_id, message.text, is_final=False)
                    if abort_task is not None:
                        result = await abort_task
                        await _cancel_audio(audio_task)
                        await sender.drop_pending_pcm()
                        await sender.json({"type": "audio_reset", "session_id": session_id})
                        await sender.json(_released_payload(session_id, result))
                        released = True
                        await sender.close(code=1000)
                        return
                    await sender.json({"type": "ack", "seq": message.seq})
                    expected_seq += 1
                    continue
                if isinstance(message, FinalMessage):
                    if message.seq != expected_seq:
                        raise ProtocolViolation(
                            "invalid_seq",
                            f"expected final seq {expected_seq}, received {message.seq}",
                        )
                    final_result = await runtime.push_text(session_id, "", is_final=True)
                    result = await abort_task if abort_task is not None else final_result
                    if result["status"] != "aborted":
                        await sender.json({"type": "ack", "seq": message.seq})
                elif isinstance(message, AbortMessage):
                    result = await abort_task if abort_task is not None else await runtime.abort_session(
                        session_id,
                        reason=message.reason or "client_abort",
                    )
                else:
                    raise ProtocolViolation("session_started", "start is valid exactly once per WebSocket")
                if result["status"] == "aborted":
                    await _cancel_audio(audio_task)
                    await sender.drop_pending_pcm()
                    await sender.json({"type": "audio_reset", "session_id": session_id})
                else:
                    await _await_audio(audio_task)
                    await sender.json({"type": "audio_end", "session_id": session_id})
                await sender.json(_released_payload(session_id, result))
                released = True
                await sender.close(code=1000)
                return
        except WebSocketDisconnect:
            return
        except ProtocolViolation as exc:
            result: dict[str, Any] | None = None
            if session_id is not None and session is not None and not released:
                try:
                    if abort_task is None:
                        abort_task = asyncio.create_task(
                            runtime.abort_session(session_id, reason="websocket_protocol_error")
                        )
                    result = await abort_task
                except GatewayError:
                    result = None
                await _cancel_audio(audio_task)
            try:
                if result is not None:
                    await sender.drop_pending_pcm()
                await sender.json({"type": "error", "code": exc.code, "detail": exc.detail})
                if result is not None:
                    await sender.json({"type": "audio_reset", "session_id": session_id})
                    await sender.json(_released_payload(session_id, result))
                    released = True
                await sender.close(code=1008, reason=exc.code)
            except (RuntimeError, WebSocketDisconnect):
                pass
        except (GatewayError, ValueError) as exc:
            result = None
            if session_id is not None and session is not None and not released:
                try:
                    if abort_task is None:
                        abort_task = asyncio.create_task(
                            runtime.abort_session(session_id, reason="websocket_gateway_error")
                        )
                    result = await abort_task
                except GatewayError:
                    result = None
                await _cancel_audio(audio_task)
            try:
                if result is not None:
                    await sender.drop_pending_pcm()
                await sender.json(
                    {"type": "error", "code": "gateway_error", "detail": f"{type(exc).__name__}: {exc}"}
                )
                if result is not None:
                    await sender.json({"type": "audio_reset", "session_id": session_id})
                    await sender.json(_released_payload(session_id, result))
                    released = True
                await sender.close(code=1011, reason="gateway_error")
            except (RuntimeError, WebSocketDisconnect):
                pass
        finally:
            if receiver_task is not None and not receiver_task.done():
                receiver_task.cancel()
                await asyncio.gather(receiver_task, return_exceptions=True)
            if session_id is not None and session is not None and not released:
                try:
                    if abort_task is None:
                        abort_task = asyncio.create_task(
                            runtime.abort_session(session_id, reason="websocket_disconnect")
                        )
                    await asyncio.shield(abort_task)
                except GatewayError:
                    pass
                await _cancel_audio(audio_task)
                await sender.drop_pending_pcm()
            await sender.shutdown()
