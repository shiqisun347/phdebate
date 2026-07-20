from __future__ import annotations

import asyncio
import json
import logging
import queue
import re
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from moss_realtime_gateway.backends import BackendTurn, CanaryPcmPayload, SynthesisBackend
from moss_realtime_gateway.config import GatewaySettings, PromptRegistry
from moss_realtime_gateway.low_latency_bridge import CanaryStageTiming

logger = logging.getLogger(__name__)
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class GatewayError(RuntimeError):
    pass


class GatewayNotReady(GatewayError):
    pass


class SessionConflict(GatewayError):
    pass


class SessionMissing(GatewayError):
    pass


class SessionOrphaned(GatewayError):
    pass


class FailFastCallback(Protocol):
    async def __call__(self, payload: dict[str, Any]) -> None: ...


class HttpFailFastCallback:
    def __init__(self, url: str, timeout_seconds: float) -> None:
        self.url = url
        self.timeout_seconds = timeout_seconds

    async def __call__(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()

        def send() -> None:
            request = urllib.request.Request(
                self.url,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                response.read(1)

        try:
            await asyncio.to_thread(send)
        except Exception as exc:
            logger.error("fail-fast callback failed: %s", type(exc).__name__)


@dataclass
class Command:
    kind: str
    text: str = ""
    acknowledged: threading.Event = field(default_factory=threading.Event)
    error: BaseException | None = None


@dataclass
class Session:
    session_id: str
    prompt_path: Path
    user_text: str
    initial_text: str
    audio_queue: queue.Queue[Any]
    commands: queue.Queue[Command] = field(default_factory=queue.Queue)
    started: threading.Event = field(default_factory=threading.Event)
    worker_exited: threading.Event = field(default_factory=threading.Event)
    abort_requested: threading.Event = field(default_factory=threading.Event)
    audio_ended: threading.Event = field(default_factory=threading.Event)
    lock: threading.RLock = field(default_factory=threading.RLock)
    worker: threading.Thread | None = None
    backend_turn: BackendTurn | None = None
    start_error: BaseException | None = None
    status: str = "starting"
    terminal_reason: str = ""
    terminal_kind: str = ""
    terminal_command: Command | None = None
    text_characters: int = 0
    audio_claimed: bool = False
    orphan_reason: str = ""
    created_monotonic: float = field(default_factory=time.monotonic)
    last_command_monotonic: float = field(default_factory=time.monotonic)
    canary_observe: bool = False

    def clear_audio(self, *, terminal: bool) -> None:
        while True:
            try:
                self.audio_queue.get_nowait()
            except queue.Empty:
                break
        if terminal:
            self.audio_ended.set()


class GatewayRuntime:
    _COMPLETED_SESSION_RETENTION = 64
    _MAX_SESSION_TEXT_CHARACTERS = 20_000

    def __init__(
        self,
        settings: GatewaySettings,
        backend: SynthesisBackend,
        *,
        fail_fast_callback: FailFastCallback | None = None,
    ) -> None:
        settings.validate()
        self.settings = settings
        self.backend = backend
        self.prompts = PromptRegistry(settings)
        self.fail_fast_callback = fail_fast_callback or (
            HttpFailFastCallback(settings.fail_fast_url, settings.fail_fast_timeout_seconds)
            if settings.fail_fast_url
            else None
        )
        self._lock = threading.RLock()
        self._sessions: dict[str, Session] = {}
        self._active_session_id: str | None = None
        self._startup_complete = False
        self._startup_error = ""
        self._orphaned: dict[str, str] = {}
        self._shutting_down = False

    @property
    def ready(self) -> bool:
        with self._lock:
            return self._startup_complete and not self._startup_error and not self._orphaned and not self._shutting_down

    async def startup(self) -> None:
        try:
            prompt_paths = self.prompts.validate_all()
            await asyncio.wait_for(asyncio.to_thread(self.backend.startup), self.settings.startup_timeout_seconds)
            await asyncio.wait_for(
                asyncio.to_thread(self.backend.warmup, prompt_paths, self.settings.warmup_text),
                self.settings.startup_timeout_seconds,
            )
        except Exception as exc:
            self._startup_error = f"{type(exc).__name__}: {exc}"
            logger.exception("gateway startup failed: %s", type(exc).__name__)
            return
        self._startup_complete = True

    async def shutdown(self) -> None:
        self._shutting_down = True
        with self._lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            if not session.worker_exited.is_set():
                try:
                    await self.abort_session(session.session_id, reason="gateway_shutdown")
                except GatewayError:
                    pass
        await asyncio.to_thread(self.backend.shutdown)

    def health(self) -> dict[str, Any]:
        with self._lock:
            active = self._active_session_id
            orphaned = dict(self._orphaned)
        ready = self.ready
        active_count = 1 if active else 0
        orphan_count = len(orphaned)
        return {
            "ok": ready,
            "status": "ready" if self.ready else "not_ready",
            "backend": self.backend.name,
            "upstream_revision": self.backend.upstream_revision,
            "model_warmed": self._startup_complete,
            "active": active_count,
            "pending": 0,
            "capacity": 1,
            "orphan_count": orphan_count,
            "warmed_up": self._startup_complete,
            "startup_error_type": self._startup_error.split(":", 1)[0] if self._startup_error else None,
            "active_session": bool(active),
            "orphaned_sessions": orphan_count,
            "shutting_down": self._shutting_down,
            "gpu_total_memory_gb": getattr(self.backend, "gpu_total_memory_gb", None),
            "gpu_free_memory_at_start_gb": getattr(self.backend, "gpu_free_memory_at_start_gb", None),
            "sub24gb_diagnostic": bool(self.settings.allow_sub24gb_diagnostic),
            "diagnostic_codec_device": self.settings.diagnostic_codec_device or None,
            "diagnostic_codec_encoder_offload": bool(
                self.settings.diagnostic_codec_encoder_offload
            ),
            "placement": getattr(self.backend, "placement", None),
            "canary_stage_observability": bool(self.settings.canary_stage_observability),
        }

    async def start_session(
        self,
        *,
        session_id: str,
        prompt_audio: str,
        user_text: str,
        assistant_text: str,
        canary_observe: bool = False,
    ) -> dict[str, Any]:
        if not self.ready:
            raise GatewayNotReady("gateway is not ready")
        if not SESSION_ID_PATTERN.fullmatch(session_id):
            raise GatewayError("invalid session_id")
        if len(assistant_text) > self._MAX_SESSION_TEXT_CHARACTERS:
            raise GatewayError("session text exceeds the fixed safety limit")
        prompt_path = self.prompts.resolve(prompt_audio)
        resolved_canary_observe = bool(
            canary_observe and self.settings.canary_stage_observability
        )
        existing_session = False
        with self._lock:
            self._prune_completed_sessions_locked()
            existing = self._sessions.get(session_id)
            if existing and not existing.worker_exited.is_set():
                if (
                    existing.prompt_path != prompt_path
                    or existing.user_text != user_text
                    or existing.initial_text != assistant_text
                    or existing.canary_observe != resolved_canary_observe
                ):
                    raise SessionConflict("active session_id was reused with different parameters")
                session = existing
                existing_session = True
            else:
                if self._active_session_id:
                    raise SessionConflict("endpoint already has one active session")
                session = Session(
                    session_id=session_id,
                    prompt_path=prompt_path,
                    user_text=user_text,
                    initial_text=assistant_text,
                    # One endpoint owns one bounded-lifetime turn. A lossless
                    # backlog keeps synthesis ACK independent from network pace.
                    audio_queue=queue.Queue(),
                    text_characters=len(assistant_text),
                    canary_observe=resolved_canary_observe,
                )
                self._sessions[session_id] = session
                self._active_session_id = session_id
                session.worker = threading.Thread(
                    target=self._worker_main,
                    args=(session,),
                    name=f"moss-gateway-{session_id}",
                    daemon=True,
                )
                session.worker.start()
        if not await asyncio.to_thread(session.started.wait, self.settings.control_ack_timeout_seconds):
            await self._mark_orphan(session, "start_ack_timeout")
            raise SessionOrphaned("session start exceeded acknowledgement grace")
        if session.start_error is not None:
            await self._wait_worker_exit(session, self.settings.terminal_grace_seconds, "start_cleanup_timeout")
            raise GatewayError(f"session start failed: {type(session.start_error).__name__}")
        if existing_session and session.status == "orphaned":
            raise SessionOrphaned("session is orphaned")
        return self._session_response(session)

    def _prune_completed_sessions_locked(self) -> None:
        completed = sorted(
            (
                session
                for session in self._sessions.values()
                if session.worker_exited.is_set() and session.session_id != self._active_session_id
            ),
            key=lambda session: session.created_monotonic,
            reverse=True,
        )
        for session in completed[self._COMPLETED_SESSION_RETENTION :]:
            self._sessions.pop(session.session_id, None)

    async def push_text(self, session_id: str, text: str, *, is_final: bool) -> dict[str, Any]:
        session = self._require_session(session_id)
        if session.status == "orphaned":
            raise SessionOrphaned("session is orphaned")
        if session.worker_exited.is_set():
            if is_final:
                return self._session_response(session)
            raise SessionMissing("session worker has already exited")
        command = Command(kind="final" if is_final else "push", text=text)
        session.last_command_monotonic = time.monotonic()
        if is_final:
            command, joined = self._register_terminal(session, command)
            if joined:
                await self._wait_worker_exit(
                    session,
                    self.settings.final_generation_timeout_seconds,
                    "final_join_worker_exit_timeout",
                )
                return self._session_response(session)
            session.commands.put(command)
        else:
            with session.lock:
                if session.terminal_command is not None:
                    raise SessionConflict("session is already terminating")
                if session.text_characters + len(text) > self._MAX_SESSION_TEXT_CHARACTERS:
                    raise GatewayError("session text exceeds the fixed safety limit")
                session.text_characters += len(text)
                session.commands.put(command)
        timeout = (
            self.settings.final_generation_timeout_seconds
            if is_final
            else self.settings.control_ack_timeout_seconds
        )
        if is_final:
            await self._run_terminal_command(session, command, timeout, "final_worker_exit_timeout")
        else:
            await self._wait_command(session, command, timeout)
        return self._session_response(session)

    async def close_session(self, session_id: str) -> dict[str, Any]:
        session = self._require_session(session_id)
        if session.status == "orphaned":
            raise SessionOrphaned("session is orphaned")
        if session.worker_exited.is_set():
            return self._session_response(session)
        command = Command(kind="close")
        command, joined = self._register_terminal(session, command)
        if joined:
            await self._wait_worker_exit(
                session,
                self.settings.terminal_grace_seconds,
                "close_join_worker_exit_timeout",
            )
            return self._session_response(session)
        session.commands.put(command)
        await self._run_terminal_command(
            session,
            command,
            self.settings.terminal_grace_seconds,
            "close_worker_exit_timeout",
        )
        return self._session_response(session)

    async def abort_session(self, session_id: str, *, reason: str = "client_abort") -> dict[str, Any]:
        session = self._require_session(session_id)
        if session.status == "orphaned":
            raise SessionOrphaned("session is orphaned")
        if session.worker_exited.is_set():
            session.clear_audio(terminal=True)
            return self._session_response(session)
        session.abort_requested.set()
        session.terminal_reason = reason
        session.clear_audio(terminal=True)
        with session.lock:
            turn = session.backend_turn
        if turn is not None:
            try:
                await asyncio.to_thread(turn.abort)
            except Exception:
                logger.exception("backend abort signal failed session=%s", session.session_id)
        command = Command(kind="abort")
        command, joined = self._register_terminal(session, command)
        if joined:
            await self._wait_worker_exit(
                session,
                self.settings.terminal_grace_seconds,
                "abort_join_worker_exit_timeout",
            )
            return self._session_response(session)
        session.commands.put(command)
        await self._run_terminal_command(
            session,
            command,
            self.settings.terminal_grace_seconds,
            "abort_worker_exit_timeout",
        )
        return self._session_response(session)

    def claim_audio(self, session_id: str) -> Session:
        session = self._require_session(session_id)
        with session.lock:
            if session.audio_claimed:
                raise SessionConflict("audio stream is already claimed")
            session.audio_claimed = True
        return session

    async def audio_item(self, session: Session) -> Any | None:
        while True:
            if session.audio_ended.is_set() and session.audio_queue.empty():
                return None
            try:
                return await asyncio.to_thread(session.audio_queue.get, True, 0.1)
            except queue.Empty:
                continue

    async def audio_disconnected(self, session: Session) -> None:
        if not session.worker_exited.is_set() and session.status != "orphaned":
            try:
                await self.abort_session(session.session_id, reason="audio_disconnect")
            except GatewayError:
                pass

    def _require_session(self, session_id: str) -> Session:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise SessionMissing("session not found")
        return session

    async def _wait_command(self, session: Session, command: Command, timeout: float) -> None:
        if not await asyncio.to_thread(command.acknowledged.wait, timeout):
            await self._mark_orphan(session, f"{command.kind}_ack_timeout")
            raise SessionOrphaned(f"{command.kind} exceeded acknowledgement grace")
        if command.error is not None:
            raise GatewayError(f"{command.kind} failed: {type(command.error).__name__}")

    def _register_terminal(self, session: Session, command: Command) -> tuple[Command, bool]:
        with session.lock:
            if session.terminal_command is not None:
                return session.terminal_command, True
            if session.text_characters + len(command.text) > self._MAX_SESSION_TEXT_CHARACTERS:
                raise GatewayError("session text exceeds the fixed safety limit")
            session.text_characters += len(command.text)
            session.terminal_kind = command.kind
            session.terminal_command = command
            return command, False

    async def _run_terminal_command(
        self,
        session: Session,
        command: Command,
        ack_timeout: float,
        exit_orphan_reason: str,
    ) -> None:
        command_error: GatewayError | None = None
        try:
            await self._wait_command(session, command, ack_timeout)
        except SessionOrphaned:
            raise
        except GatewayError as exc:
            command_error = exc
        await self._wait_worker_exit(session, self.settings.terminal_grace_seconds, exit_orphan_reason)
        if command_error is not None:
            raise command_error

    async def _wait_worker_exit(self, session: Session, timeout: float, orphan_reason: str) -> None:
        if not await asyncio.to_thread(session.worker_exited.wait, timeout):
            await self._mark_orphan(session, orphan_reason)
            raise SessionOrphaned("worker did not exit within grace")
        if session.worker is not None:
            await asyncio.to_thread(session.worker.join, timeout)
            if session.worker.is_alive():
                await self._mark_orphan(session, orphan_reason)
                raise SessionOrphaned("worker thread is still alive after acknowledgement")

    async def _mark_orphan(self, session: Session, reason: str) -> None:
        with session.lock:
            session.status = "orphaned"
            session.orphan_reason = reason
            session.abort_requested.set()
            session.clear_audio(terminal=True)
        with self._lock:
            self._orphaned[session.session_id] = reason
        logger.critical("MOSS gateway worker orphaned session=%s reason=%s", session.session_id, reason)
        if self.fail_fast_callback is not None:
            await self.fail_fast_callback(
                {
                    "event": "moss_gateway_orphaned",
                    "session_id": session.session_id,
                    "reason": reason,
                    "upstream_revision": self.backend.upstream_revision,
                }
            )

    def _worker_main(self, session: Session) -> None:
        turn: BackendTurn | None = None
        terminal_command: Command | None = None
        aborted = False
        try:
            turn = self.backend.open_turn(
                prompt_path=session.prompt_path,
                user_text=session.user_text,
                initial_text=session.initial_text,
            )
            with session.lock:
                session.backend_turn = turn
            if session.abort_requested.is_set() or session.status == "orphaned":
                turn.abort()
                session.started.set()
                return
            session.status = "active"
            self._emit(session, getattr(turn, "initial_audio", lambda: [])())
            session.started.set()
            while True:
                command = session.commands.get()
                try:
                    if command.kind == "push":
                        self._emit(session, turn.push_text(command.text))
                        command.acknowledged.set()
                        continue
                    if command.kind in {"final", "close"}:
                        if command.text:
                            self._emit(session, turn.push_text(command.text))
                        self._emit(session, turn.finish())
                        session.terminal_reason = command.kind
                        terminal_command = command
                        break
                    if command.kind == "abort":
                        aborted = True
                        turn.abort()
                        session.terminal_reason = session.terminal_reason or "abort"
                        terminal_command = command
                        break
                    raise RuntimeError(f"unknown worker command: {command.kind}")
                except BaseException as exc:
                    command.error = exc
                    terminal_command = command
                    break
        except BaseException as exc:
            session.start_error = exc
            session.status = "failed"
            session.started.set()
        finally:
            session.started.set()
            if turn is not None:
                if session.abort_requested.is_set() and not aborted:
                    try:
                        turn.abort()
                    except Exception:
                        logger.exception("backend abort cleanup failed session=%s", session.session_id)
                try:
                    turn.close()
                except Exception as exc:
                    if terminal_command is not None and terminal_command.error is None:
                        terminal_command.error = exc
                finally:
                    with session.lock:
                        session.backend_turn = None
            if session.abort_requested.is_set() or session.start_error is not None:
                session.clear_audio(terminal=False)
            session.audio_ended.set()
            if session.status != "orphaned":
                terminal_failed = terminal_command is not None and terminal_command.error is not None
                session.status = (
                    "aborted"
                    if session.abort_requested.is_set()
                    else "failed"
                    if session.start_error is not None or terminal_failed
                    else "closed"
                )
            if terminal_command is not None:
                terminal_command.acknowledged.set()
            session.worker_exited.set()
            with self._lock:
                if self._active_session_id == session.session_id:
                    self._active_session_id = None

    def _emit(self, session: Session, chunks: Any) -> None:
        iterator = iter(chunks)
        try:
            while not session.abort_requested.is_set():
                try:
                    chunk = next(iterator)
                except StopIteration:
                    break
                if isinstance(chunk, CanaryStageTiming):
                    if session.canary_observe:
                        session.audio_queue.put_nowait(chunk)
                    continue
                if isinstance(chunk, CanaryPcmPayload):
                    payload = chunk.pcm16
                    queued_item: Any = chunk if session.canary_observe else payload
                else:
                    payload = bytes(chunk)
                    queued_item = payload
                if not payload:
                    continue
                if len(payload) % 2:
                    raise RuntimeError("backend returned unaligned PCM16")
                session.audio_queue.put_nowait(queued_item)
        finally:
            if session.abort_requested.is_set():
                close_iterator = getattr(iterator, "close", None)
                if close_iterator is not None:
                    close_iterator()

    @staticmethod
    def _session_response(session: Session) -> dict[str, Any]:
        released = session.worker_exited.is_set() and session.status in {"aborted", "closed", "failed"}
        return {
            "ok": True,
            "session_id": session.session_id,
            "status": session.status,
            "worker_exited": session.worker_exited.is_set(),
            "released": released,
            "canary_observe": session.canary_observe,
        }
