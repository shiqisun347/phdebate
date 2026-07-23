from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from time import perf_counter
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx


def readiness_url(endpoint: str) -> str:
    parsed = urlsplit(endpoint.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("LightTTS endpoint must be HTTP(S)")
    return urlunsplit((parsed.scheme, parsed.netloc, "/health/ready", "", ""))


async def probe_readiness(
    endpoint: str,
    *,
    timeout_seconds: float = 2.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Read the lifecycle-aware LightTTS readiness contract.

    A reachable TCP port or liveness response is deliberately insufficient:
    an abort-pending request, orphan, stalled first PCM, or dead model worker
    must fail this probe before the platform allocates another GPU job.
    """

    started = perf_counter()
    try:
        url = readiness_url(endpoint)
    except ValueError as exc:
        return {"ok": False, "service": "LightTTS", "message": str(exc)}
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
        ) as client:
            response = await client.get(url)
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("readiness payload must be an object")
    except (httpx.HTTPError, ValueError) as exc:
        return {
            "ok": False,
            "service": "LightTTS",
            "message": type(exc).__name__,
            "latency_ms": round((perf_counter() - started) * 1000, 2),
        }
    keys = (
        "active",
        "pending",
        "oldest_wait_seconds",
        "first_pcm_stalled",
        "whole_session_stalled",
        "abort_pending",
        "orphan_count",
        "orphan_total",
        "stages",
        "last_progress_age_seconds",
        "components",
    )
    result = {
        "ok": response.status_code == 200 and payload.get("ok") is True,
        "service": "LightTTS",
        "status_code": response.status_code,
        "latency_ms": round((perf_counter() - started) * 1000, 2),
    }
    result.update({key: payload[key] for key in keys if key in payload})
    if not result["ok"]:
        result["message"] = "lifecycle readiness failed"
    return result


async def require_readiness(
    endpoint: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    snapshot = await probe_readiness(endpoint, transport=transport)
    if not snapshot["ok"]:
        from app.services.providers import ProviderError

        raise ProviderError(
            "LightTTS 未真正就绪，已拒绝新的语音任务。",
            code="lighttts_not_ready",
            retryable=True,
            retry_after_seconds=2,
        )
    return snapshot


class VoiceSession(Protocol):
    async def prepare(self) -> None: ...

    async def push_text(self, text: str) -> None: ...

    async def finish(self) -> str: ...

    async def abort(self, reason: str = "") -> None: ...


class ProviderVoiceSession:
    """Normalize provider sessions without changing their lifecycle behavior."""

    def __init__(self, session: Any) -> None:
        self._session = session
        self.abort_reason = ""

    async def prepare(self) -> None:
        prepare = getattr(self._session, "prepare", None)
        if prepare is not None:
            await prepare()

    async def push_text(self, text: str) -> None:
        await self._session.push_text(text)

    async def finish(self) -> str:
        return str(await self._session.finish())

    async def abort(self, reason: str = "") -> None:
        self.abort_reason = reason.strip()
        await self._session.abort()


class LightTTSRuntime:
    """Unified async session entry point for the configured realtime backend."""

    async def open_session(
        self,
        *,
        room_code: str,
        speech_id: str,
        voice_id: str,
        provider_config: dict[str, Any] | None = None,
        should_cancel: Callable[[], bool | Awaitable[bool]] | None = None,
        deadline_monotonic: float | None = None,
        on_stream_event: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> VoiceSession:
        # Lazy import prevents providers.py -> voice_runtime.text from cycling
        # back through the provider singletons during module initialization.
        from app.services.providers import realtime_tts

        session = realtime_tts.open_incremental_session(
            room_code=room_code,
            speech_id=speech_id,
            voice=voice_id,
            provider_config=provider_config,
            should_cancel=should_cancel,
            deadline_monotonic=deadline_monotonic,
            on_stream_event=on_stream_event,
        )
        wrapped = ProviderVoiceSession(session)
        # Admission, endpoint readiness, the bidirectional MOSS connection and
        # its protocol-level ready frame must all finish before the Agent is
        # allowed to emit its first readable character. Otherwise a second
        # room can expose text immediately and then wait behind a long GPU
        # speech even though synthesis latency itself is healthy.
        try:
            await wrapped.prepare()
        except BaseException:
            # Preparation may already own the global and endpoint scheduler
            # slots. Always drive the provider abort path before propagating a
            # pause, termination, timeout, or handshake failure; otherwise one
            # cancelled room can strand the only MOSS slot for every room.
            try:
                await asyncio.shield(wrapped.abort(reason="prepare_failed"))
            except BaseException:
                # Preserve the authoritative preparation failure. Provider
                # abort paths are best-effort but internally bounded and keep
                # observing their own worker completion.
                pass
            raise
        return wrapped


lighttts = LightTTSRuntime()
