from __future__ import annotations

import asyncio
import inspect
import io
import json
import logging
import math
import os
import re
import shutil
import sys
import time
import uuid
import wave
from array import array
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
import websockets
from app.core.config import settings
from app.core.secret_crypto import decrypt_secret
from app.services.provider_config import runtime_provider_config
from app.services.voice_runtime.admission import (
    LightTTSAdmissionCancelled,
    LightTTSAdmissionLease,
    LightTTSAdmissionQueueFull,
    LightTTSAdmissionQueueTimeout,
    LightTTSAdmissionUnavailable,
    lighttts_admission_gate,
)
from app.services.voice_runtime.archive import wav_duration
from app.services.voice_runtime.lighttts import require_readiness as require_lighttts_readiness
from app.services.voice_runtime.livekit import livekit_audio_enabled, livekit_audio_registry
from app.services.voice_runtime.text import (
    extract_agent_body_delta as _extract_agent_body_delta,
)
from app.services.voice_runtime.text import (
    extract_agent_final as _extract_agent_final,
)
from app.services.voice_runtime.text import (
    extract_text as _extract_text,
)
from app.services.voice_runtime.voices import prompt_path as voice_prompt_path
from app.services.voice_runtime.voices import prompt_text as voice_prompt_text

logger = logging.getLogger(__name__)


class ProviderError(RuntimeError):
    """A provider failure with stable, machine-readable recovery metadata.

    ``str(exc)`` intentionally remains the original public/admin message so
    existing callers and audit logs stay backwards compatible.  The extra
    fields let orchestration code make bounded recovery decisions without
    parsing localized error text or exposing an upstream exception.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "provider_error",
        retryable: bool = False,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.retry_after_seconds = max(0.0, float(retry_after_seconds)) if retry_after_seconds is not None else None


class ProviderCancelled(ProviderError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="provider_cancelled")


class DebateAgentProvider:
    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        ws_connect: Callable[..., Any] | None = None,
    ) -> None:
        self.transport = transport
        self.ws_connect = ws_connect or websockets.connect
        self._client: httpx.AsyncClient | None = None
        self._client_loop: asyncio.AbstractEventLoop | None = None
        self._client_lock: asyncio.Lock | None = None

    async def _http_client(self) -> httpx.AsyncClient:
        """Return one keep-alive pool for the API process event loop."""

        loop = asyncio.get_running_loop()
        if self._client_loop is not loop:
            previous = self._client
            self._client = None
            self._client_loop = loop
            self._client_lock = asyncio.Lock()
            if previous is not None and not previous.is_closed:
                try:
                    await previous.aclose()
                except RuntimeError:
                    logger.warning("Agent HTTP pool belonged to a closed event loop; discarding it")
        lock = self._client_lock
        if lock is None:
            raise RuntimeError("Agent HTTP client lock initialization failed")
        async with lock:
            if self._client is None or self._client.is_closed:
                self._client = httpx.AsyncClient(
                    timeout=None,
                    transport=self.transport,
                    limits=httpx.Limits(
                        max_connections=max(16, settings.engine_max_concurrent_rooms * 2),
                        max_keepalive_connections=max(8, settings.engine_max_concurrent_rooms),
                        keepalive_expiry=120,
                    ),
                )
            return self._client

    async def aclose(self) -> None:
        client = self._client
        self._client = None
        self._client_loop = None
        self._client_lock = None
        if client is not None and not client.is_closed:
            await client.aclose()

    @staticmethod
    def _headers(runtime: dict[str, Any]) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        secret = decrypt_secret(str(runtime.get("secret_ciphertext") or ""))
        if secret:
            headers["X-Debate-Agent-Key"] = secret
        return headers

    async def interrupt(
        self,
        task_id: str,
        endpoint: str | None = None,
        *,
        provider_config: dict[str, Any] | None = None,
    ) -> bool:
        """Best-effort remote cancellation for an in-flight Agent task."""

        if settings.agent_mock or not task_id.strip():
            return False
        runtime = (
            runtime_provider_config({"agent": provider_config}, "agent")
            if provider_config is not None
            else runtime_provider_config(None, "agent")
        )
        url = endpoint or str(runtime.get("endpoint") or settings.agent_api_url)
        parts = urlsplit(url)
        path = parts.path.rstrip("/")
        if path.endswith("/debate"):
            path = path[: -len("/debate")]
        interrupt_url = urlunsplit((parts.scheme, parts.netloc, f"{path}/tasks/{quote(task_id, safe='')}/interrupt", "", ""))
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            return False
        try:
            client = await self._http_client()
            response = await client.post(
                interrupt_url,
                headers=self._headers(runtime),
                timeout=httpx.Timeout(2, connect=2, read=2, write=2, pool=2),
            )
            if response.status_code == 404:
                return False
            response.raise_for_status()
            body = response.json()
            return isinstance(body, dict) and bool(body.get("ok"))
        except (httpx.HTTPError, ValueError):
            logger.warning("Agent interrupt request failed task=%s", task_id, exc_info=True)
            return False

    async def generate(
        self,
        payload: dict[str, Any],
        endpoint: str | None = None,
        *,
        provider_config: dict[str, Any] | None = None,
    ) -> str:
        content = ""
        final_seen = False
        async for event in self.generate_stream(
            payload,
            endpoint,
            provider_config=provider_config,
        ):
            if event["type"] == "final":
                content = str(event.get("content") or "").strip()
                final_seen = True
            elif not final_seen:
                content += str(event.get("delta") or "")
        if not content.strip():
            raise ProviderError("辩手 Agent 返回空内容。")
        return content.strip()

    async def generate_stream(
        self,
        payload: dict[str, Any],
        endpoint: str | None = None,
        *,
        provider_config: dict[str, Any] | None = None,
    ) -> AsyncIterator[dict[str, str]]:
        if settings.agent_mock:
            await asyncio.sleep(0.05)
            side = "正方" if payload.get("holder") == "正方" else "反方"
            content = f"作为{side}辩手，我方将从事实、价值与长期影响三个层面展开论证。核心在于厘清判断标准，并回应对方论点中的前提缺口。"
            yield {"type": "delta", "delta": content}
            yield {"type": "final", "content": content}
            return
        runtime = (
            runtime_provider_config({"agent": provider_config}, "agent")
            if provider_config is not None
            else runtime_provider_config(None, "agent")
        )
        if provider_config is not None and not runtime.get("enabled"):
            raise ProviderError("辩手 Agent 服务已停用。")
        url = endpoint or str(runtime.get("endpoint") or settings.agent_api_url)
        if not url:
            raise ProviderError("未配置辩手 Agent 地址。")
        provider_settings = runtime.get("settings") if isinstance(runtime.get("settings"), dict) else {}
        try:
            timeout_seconds = max(10, min(300, int(provider_settings.get("timeout_seconds", settings.agent_timeout_seconds))))
        except (TypeError, ValueError):
            timeout_seconds = settings.agent_timeout_seconds
        request_payload = dict(payload)
        output = dict(request_payload.get("output") or {})
        output["stream"] = bool(provider_settings.get("stream", output.get("stream", True)))
        request_payload["output"] = output
        headers = self._headers(runtime)
        try:
            timeout = httpx.Timeout(10, read=timeout_seconds, write=30, pool=10)
            client = await self._http_client()
            async with client.stream("POST", url, json=request_payload, headers=headers, timeout=timeout) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type", "")
                # The requested mode is not proof of the response framing.  A
                # gateway may legitimately return an already-completed,
                # idempotent task as one JSON response even when ``stream`` was
                # requested.  Treating that JSON body as SSE consumes it via
                # ``aiter_lines()`` and the later ``aread()`` then raises
                # httpx.StreamConsumed, which used to make speculative Agent
                # prefetch fail and add a full generation wait to the next
                # stage.  Only the response Content-Type is authoritative for
                # selecting the incremental parser.
                if "text/event-stream" in content_type:
                    content = ""
                    final_emitted = False
                    async for line in response.aiter_lines():
                        line = line.strip()
                        if not line or line.startswith(":"):
                            continue
                        if line.startswith("data:"):
                            line = line[5:].strip()
                        if line == "[DONE]":
                            break
                        try:
                            event = json.loads(line)
                            if isinstance(event, dict) and isinstance(event.get("error"), dict):
                                error = event["error"]
                                code = str(error.get("code") or "agent_stream_failed")
                                message = str(error.get("message") or "辩手 Agent 流式生成失败。")
                                raise ProviderError(
                                    f"辩手 Agent 流式生成失败：{message}",
                                    code=code,
                                    retryable=code not in {"output_truncated", "output_too_large"},
                                )
                            if isinstance(event, dict) and str(event.get("type") or "").lower() == "final":
                                final_content = _extract_agent_final(event)
                                if final_content.strip():
                                    content = final_content
                                    final_emitted = True
                                    yield {"type": "final", "content": final_content.strip()}
                            else:
                                delta = _extract_agent_body_delta(event)
                                if delta:
                                    content += delta
                                    yield {"type": "delta", "delta": delta}
                        except json.JSONDecodeError:
                            logger.debug("ignoring non-JSON Agent SSE line")
                    if content.strip():
                        if not final_emitted:
                            yield {"type": "final", "content": content.strip()}
                        return
                raw = await response.aread()
                try:
                    text = _extract_text(json.loads(raw))
                except json.JSONDecodeError:
                    text = raw.decode("utf-8", "ignore")
                if not text.strip():
                    raise ProviderError("辩手 Agent 返回空内容。")
                yield {"type": "final", "content": text.strip()}
                return
        except httpx.HTTPError as exc:
            raise ProviderError(f"辩手 Agent 调用失败：{exc}") from exc


StreamEventCallback = Callable[[dict[str, Any]], Awaitable[None]]


class LightTTSProvider:
    MAX_CHUNK_CHARACTERS = 30
    MAX_CHUNKS = 128
    SEGMENT_PAUSE_SECONDS = 0.12
    SEGMENT_MIN_INSERTED_PAUSE_SECONDS = 0.08
    SEGMENT_EDGE_SILENCE_KEEP_SECONDS = 0.02
    SEGMENT_EDGE_FADE_SECONDS = 0.005
    SEGMENT_SILENCE_WINDOW_SECONDS = 0.01
    SEGMENT_SILENCE_RMS_DBFS = -60.0
    SEGMENT_SILENCE_PEAK_DBFS = -50.0
    PART_STALE_SECONDS = 1800
    MIN_AUDIO_DURATION_SECONDS = 0.25
    MIN_SECONDS_PER_VISIBLE_CHARACTER = 0.08
    MAX_REQUEST_ATTEMPTS = 3
    RETRY_BASE_SECONDS = 0.5
    TRANSIENT_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        stream_connect: Callable[..., Any] | None = None,
    ) -> None:
        self.transport = transport
        self.stream_connect = stream_connect or websockets.connect
        try:
            self._scheduler_loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            self._scheduler_loop = None
        self._semaphore: asyncio.Semaphore | None = asyncio.Semaphore(settings.lighttts_max_active) if self._scheduler_loop else None
        self._background_semaphore: asyncio.Semaphore | None = asyncio.Semaphore(1) if self._scheduler_loop else None
        self._draining_jobs: set[asyncio.Task[str]] = set()

    def _ensure_scheduler(self) -> None:
        loop = asyncio.get_running_loop()
        if self._scheduler_loop is loop:
            return
        self._scheduler_loop = loop
        self._semaphore = asyncio.Semaphore(settings.lighttts_max_active)
        self._background_semaphore = asyncio.Semaphore(1)

    def _track_draining_job(self, job: asyncio.Task[str]) -> None:
        self._draining_jobs.add(job)

        def finished(task: asyncio.Task[str]) -> None:
            self._draining_jobs.discard(task)
            try:
                task.exception()
            except (asyncio.CancelledError, Exception):
                # The original waiter already received cancellation.  Consume
                # the drain result so it cannot become an unhandled exception.
                pass

        job.add_done_callback(finished)

    async def drain_cancelled_jobs(self, timeout_seconds: float) -> int:
        """Wait at most ``timeout_seconds`` for cancelled callers' GPU work."""
        pending = {job for job in self._draining_jobs if not job.done()}
        if not pending:
            return 0
        _done, still_pending = await asyncio.wait(pending, timeout=max(0.0, timeout_seconds))
        return len(still_pending)

    def prompt_path(self, voice: str) -> Path:
        return voice_prompt_path(voice)

    @staticmethod
    def prompt_text(voice: str) -> str:
        return voice_prompt_text(voice)

    @staticmethod
    def _bistream_endpoint(endpoint: str) -> str:
        parsed = urlsplit(endpoint)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        path = parsed.path.rstrip("/")
        if path.endswith("/inference_zero_shot"):
            path = f"{path.removesuffix('/inference_zero_shot')}/inference_zero_shot_bistream"
        else:
            path = f"{path}/inference_zero_shot_bistream"
        return urlunsplit((scheme, parsed.netloc, path, "", ""))

    @staticmethod
    def _visible_characters(text: str) -> int:
        return len(re.sub(r"\s+", "", text))

    @classmethod
    def _split_text(cls, text: str, *, max_characters: int | None = None) -> list[str]:
        """Split long speech near punctuation before LightTTS rejects it.

        The deployed LightTTS service returns HTTP 417 when a single request is
        likely to produce incomplete audio. Keep chunks conservatively below
        that limit while preserving every non-whitespace character in order.
        """

        limit = max(1, max_characters or cls.MAX_CHUNK_CHARACTERS)
        remaining = re.sub(r"\s+", " ", text).strip()
        chunks: list[str] = []
        preferred_breaks = set("。！？!?；;，,、：:")
        while remaining:
            if not re.search(r"[\w\u4e00-\u9fff]", remaining):
                if chunks:
                    chunks[-1] += remaining
                    break
                raise ProviderError("LightTTS 文本缺少可发音内容。")
            if cls._visible_characters(remaining) <= limit:
                chunks.append(remaining)
                break
            visible = 0
            window_end = 0
            preferred_end = 0
            for index, character in enumerate(remaining):
                if not character.isspace():
                    visible += 1
                window_end = index + 1
                if character in preferred_breaks and visible >= max(1, limit // 2):
                    preferred_end = window_end
                if visible >= limit:
                    break
            cut_at = preferred_end or window_end
            chunk = remaining[:cut_at].strip()
            if not chunk:
                raise ProviderError("LightTTS 文本无法安全分段。")
            chunks.append(chunk)
            if len(chunks) >= cls.MAX_CHUNKS:
                raise ProviderError("LightTTS 文本过长，分段数量超过安全上限。")
            remaining = remaining[cut_at:].strip()
        return chunks

    @staticmethod
    def _pcm16_samples(frames: bytes) -> array:
        samples = array("h")
        samples.frombytes(frames)
        if sys.byteorder == "big":
            samples.byteswap()
        return samples

    @staticmethod
    def _pcm16_frames(samples: array) -> bytes:
        output = array("h", samples)
        if sys.byteorder == "big":
            output.byteswap()
        return output.tobytes()

    @classmethod
    def _edge_silence_frames(cls, samples: array, sample_rate: int, *, leading: bool) -> int:
        """Return only contiguous, near-digital silence at one segment edge."""

        if not samples:
            return 0
        window_frames = max(1, round(sample_rate * cls.SEGMENT_SILENCE_WINDOW_SECONDS))
        rms_limit = 32768.0 * (10 ** (cls.SEGMENT_SILENCE_RMS_DBFS / 20.0))
        peak_limit = 32768.0 * (10 ** (cls.SEGMENT_SILENCE_PEAK_DBFS / 20.0))
        silent_frames = 0
        if leading:
            offsets = range(0, len(samples), window_frames)
        else:
            offsets = range(len(samples), 0, -window_frames)
        for offset in offsets:
            if leading:
                chunk = samples[offset : min(len(samples), offset + window_frames)]
            else:
                chunk = samples[max(0, offset - window_frames) : offset]
            if not chunk:
                break
            peak = max(abs(value) for value in chunk)
            rms = math.sqrt(sum(value * value for value in chunk) / len(chunk))
            if peak > peak_limit or rms > rms_limit:
                break
            silent_frames += len(chunk)
        # An all-silent response cannot establish a safe speech boundary. Leave
        # it untouched and let the existing duration/content gates decide.
        return 0 if silent_frames >= len(samples) else silent_frames

    @staticmethod
    def _fade_pcm16_edge(samples: array, frame_count: int, *, fade_in: bool) -> None:
        count = min(max(0, frame_count), len(samples))
        if count <= 0:
            return
        if count == 1:
            samples[0 if fade_in else -1] = 0
            return
        start = 0 if fade_in else len(samples) - count
        for index in range(count):
            progress = index / (count - 1)
            factor = 0.5 * (1 - math.cos(math.pi * progress)) if fade_in else 0.5 * (1 + math.cos(math.pi * progress))
            position = start + index
            samples[position] = round(samples[position] * factor)

    @classmethod
    def _process_pcm16_mono_segment(
        cls,
        frames: bytes,
        sample_rate: int,
        *,
        trim_leading_edge: bool,
        trim_trailing_edge: bool,
    ) -> tuple[bytes, int, int]:
        original = cls._pcm16_samples(frames)
        keep_frames = max(0, round(sample_rate * cls.SEGMENT_EDGE_SILENCE_KEEP_SECONDS))
        fade_frames = max(1, round(sample_rate * cls.SEGMENT_EDGE_FADE_SECONDS))
        leading_silence = cls._edge_silence_frames(original, sample_rate, leading=True)
        trailing_silence = cls._edge_silence_frames(original, sample_rate, leading=False)
        trim_leading = max(0, leading_silence - keep_frames) if trim_leading_edge else 0
        trim_trailing = max(0, trailing_silence - keep_frames) if trim_trailing_edge else 0
        end = len(original) - trim_trailing if trim_trailing else len(original)
        samples = array("h", original[trim_leading:end])
        if trim_leading_edge:
            cls._fade_pcm16_edge(samples, fade_frames, fade_in=True)
        if trim_trailing_edge:
            cls._fade_pcm16_edge(samples, fade_frames, fade_in=False)
        retained_leading = min(leading_silence, keep_frames) if trim_leading_edge else leading_silence
        retained_trailing = min(trailing_silence, keep_frames) if trim_trailing_edge else trailing_silence
        return cls._pcm16_frames(samples), retained_leading, retained_trailing

    @classmethod
    def _merge_wav_segments(cls, segments: list[Path], target: Path) -> None:
        if not segments:
            raise ProviderError("LightTTS 未生成可合并的音频分段。")
        if len(segments) == 1:
            shutil.copyfile(segments[0], target)
            return
        expected: tuple[int, int, int, str] | None = None
        compname = "not compressed"
        for segment in segments:
            with wave.open(str(segment), "rb") as source:
                current = (source.getnchannels(), source.getsampwidth(), source.getframerate(), source.getcomptype())
                if expected is None:
                    expected = current
                    compname = source.getcompname()
                elif current != expected:
                    raise ProviderError("LightTTS 分段音频参数不一致，无法安全合并。")
        if expected is None:
            raise ProviderError("LightTTS 未生成可合并的音频分段。")
        with wave.open(str(target), "wb") as output:
            output.setnchannels(expected[0])
            output.setsampwidth(expected[1])
            output.setframerate(expected[2])
            output.setcomptype(expected[3], compname)
            if expected[0] == 1 and expected[1] == 2 and expected[3] == "NONE":
                target_pause_frames = max(0, round(expected[2] * cls.SEGMENT_PAUSE_SECONDS))
                minimum_pause_frames = max(0, round(expected[2] * cls.SEGMENT_MIN_INSERTED_PAUSE_SECONDS))
                previous_trailing_silence = 0
                last_index = len(segments) - 1
                for index, segment in enumerate(segments):
                    with wave.open(str(segment), "rb") as source:
                        frames = source.readframes(source.getnframes())
                    processed, retained_leading, retained_trailing = cls._process_pcm16_mono_segment(
                        frames,
                        expected[2],
                        trim_leading_edge=index > 0,
                        trim_trailing_edge=index < last_index,
                    )
                    if index:
                        native_pause_frames = previous_trailing_silence + retained_leading
                        inserted_pause_frames = max(
                            minimum_pause_frames,
                            target_pause_frames - native_pause_frames,
                        )
                        output.writeframes(b"\x00\x00" * inserted_pause_frames)
                    output.writeframes(processed)
                    previous_trailing_silence = retained_trailing
            else:
                silence_frames = int(expected[2] * cls.SEGMENT_PAUSE_SECONDS)
                silence = b"\x00" * silence_frames * expected[0] * expected[1]
                for index, segment in enumerate(segments):
                    if index:
                        output.writeframes(silence)
                    with wave.open(str(segment), "rb") as source:
                        output.writeframes(source.readframes(source.getnframes()))

    @classmethod
    def _remove_stale_parts(cls, target_dir: Path, speech_id: str) -> None:
        cutoff = time.time() - cls.PART_STALE_SECONDS
        stale_prefix = f".{speech_id}."
        try:
            entries = list(target_dir.iterdir())
        except FileNotFoundError:
            return
        for stale in entries:
            if stale.is_symlink() or not stale.is_file() or not stale.name.startswith(stale_prefix) or not stale.name.endswith(".part"):
                continue
            try:
                if stale.stat().st_mtime <= cutoff:
                    stale.unlink(missing_ok=True)
            except (FileNotFoundError, OSError):
                continue

    @classmethod
    def _minimum_duration(cls, text: str) -> float:
        """Reject grossly truncated HTTP 200 responses and stale cache files.

        Saved production samples speak roughly 3.5--4.6 visible characters per
        second.  This threshold deliberately allows up to 12.5 characters per
        second, so it is not a quality or pacing judgement; it only catches an
        output that is far too short to contain the requested chunk.  Keeping
        the check proportional without the former four-second cap also prevents
        a short prefix of a long speech from becoming a reusable cache entry.
        """

        visible_characters = cls._visible_characters(text)
        return max(
            cls.MIN_AUDIO_DURATION_SECONDS,
            visible_characters * cls.MIN_SECONDS_PER_VISIBLE_CHARACTER,
        )

    @staticmethod
    def _wav_duration(path: Path) -> float:
        return wav_duration(path)

    @staticmethod
    def _write_audio(target: Path, audio: bytes) -> None:
        # Maintenance and verification jobs may remove an otherwise empty room
        # directory while inference is still in flight. Recreate it immediately
        # before the atomic temporary write instead of turning a valid TTS
        # response into an engine-level FileNotFoundError.
        target.parent.mkdir(parents=True, exist_ok=True)
        if audio.startswith(b"RIFF"):
            content = audio
        else:
            buffer = io.BytesIO()
            with wave.open(buffer, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(24000)
                wav.writeframes(audio)
            content = buffer.getvalue()
        with target.open("wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())

    async def publish_wav_to_livekit(
        self,
        *,
        room_code: str,
        speech_id: str,
        should_cancel: Callable[[], bool | Awaitable[bool]] | None,
        on_stream_event: StreamEventCallback,
    ) -> str:
        """Publish a completed WAV through the bounded LiveKit PCM clock.

        This path deliberately keeps the production bi-stream switch off. The
        proven one-shot HTTP synthesis completes first, then its PCM is sent
        through the already bounded and revocable LiveKit publisher.
        """

        if not livekit_audio_enabled():
            raise ProviderError("LiveKit 实时音频尚未启用。")
        target = settings.media_path / room_code / f"{speech_id}.wav"
        if not target.is_file() or target.is_symlink():
            raise ProviderError("LightTTS 最终 WAV 不存在，无法发布到 LiveKit。")
        if await self._cancel_requested(should_cancel):
            raise ProviderCancelled("LightTTS LiveKit 发布任务已取消。")

        generation = uuid.uuid4().hex
        generation_started = False
        event_emitted = False
        track_sid = ""
        try:
            with wave.open(str(target), "rb") as audio:
                sample_rate = audio.getframerate()
                if audio.getnchannels() != 1 or audio.getsampwidth() != 2 or not 8_000 <= sample_rate <= 48_000:
                    raise ProviderError("LiveKit 降级发布仅接受 8–48kHz 单声道 PCM16 WAV。")
                track_sid = await livekit_audio_registry.start_generation(
                    room_code,
                    speech_id,
                    generation,
                    sample_rate,
                )
                generation_started = True
                read_frames = max(1, sample_rate // 10)
                while True:
                    if await self._cancel_requested(should_cancel):
                        raise ProviderCancelled("LightTTS LiveKit 发布任务已取消。")
                    pcm16 = audio.readframes(read_frames)
                    if not pcm16:
                        break
                    first_capture_at = await livekit_audio_registry.write_pcm(room_code, generation, pcm16)
                    if first_capture_at is None:
                        raise ProviderCancelled("LightTTS LiveKit generation 已被撤销。")
                    if not event_emitted:
                        await on_stream_event(
                            {
                                "type": "audio.stream.started",
                                "room_code": room_code,
                                "speech_id": speech_id,
                                "generation": generation,
                                "stream_url": None,
                                "sample_rate": sample_rate,
                                "channels": 1,
                                "sample_width": 2,
                                "transport": "livekit",
                                "track_sid": track_sid or None,
                                "server_first_capture_at": first_capture_at,
                            }
                        )
                        event_emitted = True
            if not event_emitted:
                raise ProviderError("LightTTS 最终 WAV 不含可发布音频。")
            if await self._cancel_requested(should_cancel):
                raise ProviderCancelled("LightTTS LiveKit 发布任务已取消。")
            await livekit_audio_registry.finish_generation(room_code, generation)
            return generation
        except BaseException as exc:
            if generation_started:
                await livekit_audio_registry.abort_generation(room_code, generation)
            if event_emitted:
                try:
                    await on_stream_event(
                        {
                            "type": "audio.stream.aborted",
                            "room_code": room_code,
                            "speech_id": speech_id,
                            "generation": generation,
                            "transport": "livekit",
                            "track_sid": track_sid or None,
                        }
                    )
                except Exception:
                    logger.exception("failed to publish LiveKit WAV abort room=%s speech=%s", room_code, speech_id)
            if isinstance(exc, (wave.Error, OSError)):
                raise ProviderError(f"LightTTS 最终 WAV 无法读取：{exc}") from exc
            raise

    async def _stream_bistream_candidate(
        self,
        *,
        endpoint: str,
        chunks: Iterable[str] | AsyncIterator[str],
        prompt_path: Path,
        prompt_text: str,
        temporary: Path,
        generation: str,
        room_code: str,
        speech_id: str,
        should_cancel: Callable[[], bool | Awaitable[bool]] | None,
        job_deadline: float,
        on_stream_event: StreamEventCallback,
    ) -> str:
        """Tail one upstream bi-stream session into an atomically published WAV.

        The growing ``.part`` file is intentionally readable only through the
        authorized raw-PCM endpoint.  The normal ``/media`` route still exposes
        only the final immutable WAV after the header is patched and replaced.
        """

        stream_url = f"/ws/rooms/{room_code}/audio"
        started = False
        total_bytes = 0
        rtc_enabled = livekit_audio_enabled()
        rtc_generation_started = False
        rtc_track_sid = ""
        temporary.parent.mkdir(parents=True, exist_ok=True)
        raw = temporary.open("w+b")
        wav = wave.open(raw, "wb")
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(settings.lighttts_stream_sample_rate)
        if rtc_enabled:
            await livekit_audio_registry.ensure_room(room_code)

        async def emit(event_type: str, **payload: Any) -> None:
            await on_stream_event(
                {
                    "type": event_type,
                    "room_code": room_code,
                    "speech_id": speech_id,
                    "generation": generation,
                    **payload,
                }
            )

        try:
            remaining = job_deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise ProviderCancelled("LightTTS 全任务已超时。")
            async with self.stream_connect(
                self._bistream_endpoint(endpoint),
                max_size=None,
                open_timeout=max(0.1, min(10.0, remaining)),
                close_timeout=5,
                ping_interval=20,
                ping_timeout=20,
            ) as websocket:
                await websocket.send(json.dumps({"prompt_text": prompt_text, "tts_model_name": "default"}))
                await websocket.send(prompt_path.read_bytes())

                async def send_text() -> None:
                    if isinstance(chunks, AsyncIterator):
                        async for chunk in chunks:
                            if await self._cancel_requested(should_cancel):
                                raise ProviderCancelled("LightTTS 运行任务已取消。")
                            await websocket.send(json.dumps({"tts_text": chunk}, ensure_ascii=False))
                    else:
                        for chunk in chunks:
                            if await self._cancel_requested(should_cancel):
                                raise ProviderCancelled("LightTTS 运行任务已取消。")
                            await websocket.send(json.dumps({"tts_text": chunk}, ensure_ascii=False))
                    await websocket.send(json.dumps({"finish": True}))

                sender = asyncio.create_task(send_text())
                try:
                    while True:
                        if await self._cancel_requested(should_cancel):
                            raise ProviderCancelled("LightTTS 运行任务已取消。")
                        if asyncio.get_running_loop().time() >= job_deadline:
                            raise ProviderCancelled("LightTTS 全任务已超时。")
                        try:
                            message = await asyncio.wait_for(websocket.recv(), timeout=0.25)
                        except asyncio.TimeoutError:
                            if sender.done():
                                sender.result()
                            continue
                        except websockets.exceptions.ConnectionClosedOK:
                            break
                        if message is None:
                            break
                        if isinstance(message, str):
                            try:
                                upstream = json.loads(message)
                            except json.JSONDecodeError as exc:
                                raise ProviderError("LightTTS 流式服务返回了无效控制消息。") from exc
                            if isinstance(upstream, dict) and upstream.get("error"):
                                raise ProviderError(f"LightTTS 流式服务失败：{upstream['error']}")
                            continue
                        audio = bytes(message)
                        if not audio:
                            continue
                        if len(audio) % 2:
                            raise ProviderError("LightTTS 流式服务返回了未对齐的 PCM16 数据。")
                        wav.writeframesraw(audio)
                        raw.flush()
                        total_bytes += len(audio)
                        rtc_first_capture_at: str | None = None
                        if rtc_enabled:
                            if not rtc_generation_started:
                                rtc_track_sid = await livekit_audio_registry.start_generation(
                                    room_code,
                                    speech_id,
                                    generation,
                                    settings.lighttts_stream_sample_rate,
                                )
                                rtc_generation_started = True
                            rtc_first_capture_at = await livekit_audio_registry.write_pcm(room_code, generation, audio)
                        if not started:
                            if rtc_enabled and not rtc_first_capture_at:
                                continue
                            started = True
                            await emit(
                                "audio.stream.started",
                                stream_url=None if rtc_enabled else stream_url,
                                sample_rate=settings.lighttts_stream_sample_rate,
                                channels=1,
                                sample_width=2,
                                transport="livekit" if rtc_enabled else "websocket_pcm",
                                track_sid=rtc_track_sid or None,
                                server_first_capture_at=rtc_first_capture_at,
                            )
                    await sender
                finally:
                    if not sender.done():
                        sender.cancel()
                        await asyncio.gather(sender, return_exceptions=True)
            if not started or total_bytes <= 0:
                raise ProviderError("LightTTS 流式服务未返回音频。")
            if await self._cancel_requested(should_cancel):
                raise ProviderCancelled("LightTTS 运行任务已取消。")
            if rtc_generation_started:
                await livekit_audio_registry.finish_generation(room_code, generation)
            wav.close()
            raw.flush()
            os.fsync(raw.fileno())
            raw.close()
            os.chmod(temporary, 0o600)
            return generation
        except BaseException:
            if rtc_generation_started:
                await livekit_audio_registry.abort_generation(room_code, generation)
            try:
                wav.close()
            except Exception:
                pass
            try:
                raw.close()
            except Exception:
                pass
            if started:
                try:
                    await emit(
                        "audio.stream.aborted",
                        transport="livekit" if rtc_enabled else "websocket_pcm",
                        track_sid=rtc_track_sid or None,
                    )
                except Exception:
                    logger.exception("failed to publish LightTTS stream abort room=%s speech=%s", room_code, speech_id)
            temporary.unlink(missing_ok=True)
            raise

    @staticmethod
    async def _cancel_requested(should_cancel: Callable[[], bool | Awaitable[bool]] | None) -> bool:
        if not should_cancel:
            return False
        cancelled = should_cancel()
        return bool(await cancelled) if inspect.isawaitable(cancelled) else bool(cancelled)

    async def _post_with_cancellation(
        self,
        client: httpx.AsyncClient,
        endpoint: str,
        *,
        data: dict[str, str],
        candidate: Path,
        should_cancel: Callable[[], bool | Awaitable[bool]] | None,
        job_deadline: float,
        read_timeout: float,
    ) -> httpx.Response:
        remaining = job_deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise ProviderCancelled("LightTTS 全任务已超时。")
        request_timeout = httpx.Timeout(
            max(0.1, min(10.0, remaining)),
            read=max(0.1, min(read_timeout, remaining)),
            write=max(0.1, min(60.0, remaining)),
            pool=max(0.1, min(10.0, remaining)),
        )
        with candidate.open("rb") as prompt_file:
            request = asyncio.create_task(
                client.post(
                    endpoint,
                    data=data,
                    files={"prompt_wav": (candidate.name, prompt_file, "audio/wav")},
                    timeout=request_timeout,
                )
            )
            cancellation_requested = False
            try:
                while True:
                    done, _pending = await asyncio.wait({request}, timeout=0.25)
                    if done:
                        try:
                            response = await request
                        except Exception:
                            if cancellation_requested:
                                raise ProviderCancelled("LightTTS 运行任务已取消。") from None
                            raise
                        if cancellation_requested:
                            raise ProviderCancelled("LightTTS 运行任务已取消。")
                        return response
                    if not cancellation_requested and await self._cancel_requested(should_cancel):
                        # Closing the HTTP client does not guarantee that the GPU
                        # server stops inference. Keep the semaphore occupied
                        # until the response finishes, then discard it, so actual
                        # LightTTS concurrency never exceeds the configured cap.
                        cancellation_requested = True
            except BaseException:
                if not request.done():
                    request.cancel()
                    await asyncio.gather(request, return_exceptions=True)
                raise

    async def _post_with_retry(
        self,
        client: httpx.AsyncClient,
        endpoint: str,
        *,
        data: dict[str, str],
        candidate: Path,
        should_cancel: Callable[[], bool | Awaitable[bool]] | None,
        job_deadline: float,
        read_timeout: float,
    ) -> httpx.Response:
        for attempt in range(1, self.MAX_REQUEST_ATTEMPTS + 1):
            if asyncio.get_running_loop().time() >= job_deadline:
                raise ProviderCancelled("LightTTS 全任务已超时。")
            try:
                response = await self._post_with_cancellation(
                    client,
                    endpoint,
                    data=data,
                    candidate=candidate,
                    should_cancel=should_cancel,
                    job_deadline=job_deadline,
                    read_timeout=read_timeout,
                )
            except httpx.TransportError as exc:
                if asyncio.get_running_loop().time() >= job_deadline:
                    raise ProviderCancelled("LightTTS 全任务已超时。") from exc
                if attempt >= self.MAX_REQUEST_ATTEMPTS:
                    raise
                logger.warning(
                    "LightTTS transport retry attempt=%s/%s error_type=%s",
                    attempt,
                    self.MAX_REQUEST_ATTEMPTS,
                    type(exc).__name__,
                )
            else:
                if response.status_code not in self.TRANSIENT_STATUS_CODES or attempt >= self.MAX_REQUEST_ATTEMPTS:
                    return response
                logger.warning(
                    "LightTTS response retry attempt=%s/%s status=%s",
                    attempt,
                    self.MAX_REQUEST_ATTEMPTS,
                    response.status_code,
                )
            delay = self.RETRY_BASE_SECONDS * 2 ** (attempt - 1)
            deadline = asyncio.get_running_loop().time() + delay
            while asyncio.get_running_loop().time() < min(deadline, job_deadline):
                if await self._cancel_requested(should_cancel):
                    raise ProviderCancelled("LightTTS 重试任务已取消。")
                await asyncio.sleep(min(0.1, max(0.0, min(deadline, job_deadline) - asyncio.get_running_loop().time())))
            if asyncio.get_running_loop().time() >= job_deadline:
                raise ProviderCancelled("LightTTS 全任务已超时。")
        raise RuntimeError("LightTTS retry loop exited unexpectedly")

    async def _acquire_semaphore(
        self,
        semaphore: asyncio.Semaphore,
        should_cancel: Callable[[], bool | Awaitable[bool]] | None,
    ) -> None:
        """Acquire a scheduler slot while allowing stale queued jobs to leave early."""
        if await self._cancel_requested(should_cancel):
            raise ProviderCancelled("LightTTS 排队任务已取消。")
        acquire = asyncio.create_task(semaphore.acquire())
        acquired = False
        try:
            while True:
                done, _pending = await asyncio.wait({acquire}, timeout=0.1)
                if done:
                    await acquire
                    acquired = True
                    break
                if await self._cancel_requested(should_cancel):
                    raise ProviderCancelled("LightTTS 排队任务已取消。")
            if await self._cancel_requested(should_cancel):
                raise ProviderCancelled("LightTTS 排队任务已取消。")
        except BaseException:
            if acquired:
                semaphore.release()
            elif not acquire.done():
                acquire.cancel()
                await asyncio.gather(acquire, return_exceptions=True)
            raise

    async def synthesize(
        self,
        text: str,
        *,
        room_code: str,
        speech_id: str,
        voice: str = "debate_voice_1",
        provider_config: dict[str, Any] | None = None,
        should_cancel: Callable[[], bool | Awaitable[bool]] | None = None,
        background: bool = False,
        deadline_monotonic: float | None = None,
        on_stream_event: StreamEventCallback | None = None,
    ) -> str:
        caller_cancelled = asyncio.Event()

        async def combined_cancel_requested() -> bool:
            return caller_cancelled.is_set() or await self._cancel_requested(should_cancel)

        job = asyncio.create_task(
            self._synthesize_job(
                text,
                room_code=room_code,
                speech_id=speech_id,
                voice=voice,
                provider_config=provider_config,
                should_cancel=combined_cancel_requested,
                background=background,
                deadline_monotonic=deadline_monotonic,
                on_stream_event=on_stream_event,
            )
        )
        try:
            return await asyncio.shield(job)
        except asyncio.CancelledError:
            caller_cancelled.set()
            self._track_draining_job(job)
            raise

    def open_incremental_session(
        self,
        *,
        room_code: str,
        speech_id: str,
        voice: str = "debate_voice_1",
        provider_config: dict[str, Any] | None = None,
        should_cancel: Callable[[], bool | Awaitable[bool]] | None = None,
        deadline_monotonic: float | None = None,
        on_stream_event: StreamEventCallback,
    ) -> LightTTSIncrementalSession:
        """Open one continuous upstream session fed by stable Agent clauses.

        The returned object is deliberately separate from ``synthesize``: all
        clauses share one websocket and one codec context, so a caller cannot
        accidentally create a new voice state for every Agent delta.
        """

        if not settings.lighttts_streaming_enabled or not settings.lighttts_bistream_enabled:
            raise ProviderError("LightTTS 增量会话需要同时启用已验证的 bi-stream 后端。")
        return LightTTSIncrementalSession(
            self,
            room_code=room_code,
            speech_id=speech_id,
            voice=voice,
            provider_config=provider_config,
            should_cancel=should_cancel,
            deadline_monotonic=deadline_monotonic,
            on_stream_event=on_stream_event,
        )

    async def _synthesize_incremental_job(
        self,
        chunks: AsyncIterator[str],
        text_parts: list[str],
        *,
        room_code: str,
        speech_id: str,
        voice: str,
        provider_config: dict[str, Any] | None,
        should_cancel: Callable[[], bool | Awaitable[bool]] | None,
        deadline_monotonic: float | None,
        on_stream_event: StreamEventCallback,
    ) -> str:
        """Run a dynamically-fed bi-stream session under normal admission.

        This path is intentionally websocket-only. Falling back to one HTTP
        request per clause would reintroduce the exact gaps and voice resets the
        realtime pipeline is designed to remove.
        """

        loop = asyncio.get_running_loop()
        provider_deadline = loop.time() + settings.lighttts_job_timeout_seconds
        job_deadline = min(provider_deadline, deadline_monotonic) if deadline_monotonic is not None else provider_deadline
        if loop.time() >= job_deadline:
            raise ProviderError("LightTTS 全任务超时，未发布音频。", code="lighttts_job_timeout")
        runtime = runtime_provider_config({"lighttts": provider_config} if provider_config is not None else None, "lighttts")
        if not runtime.get("enabled"):
            raise ProviderError("LightTTS 服务已停用。")
        endpoint = str(runtime.get("endpoint") or "").strip()
        if not endpoint:
            raise ProviderError("未配置 LightTTS 服务地址。")
        if settings.app_env == "production":
            await require_lighttts_readiness(endpoint)
        provider_settings = runtime.get("settings") if isinstance(runtime.get("settings"), dict) else {}
        try:
            speed = max(0.5, min(2.0, float(provider_settings.get("speed", 1.0))))
        except (TypeError, ValueError):
            speed = 1.0
        if speed != 1.0:
            raise ProviderError("LightTTS 增量会话暂不允许变速，以避免音色和时序漂移。")
        prompt_path = self.prompt_path(voice)
        if not prompt_path.exists() or prompt_path.is_symlink():
            raise ProviderError(f"LightTTS prompt 音频不存在：{prompt_path}")
        prompt_text = self.prompt_text(voice if prompt_path.stem == voice else prompt_path.stem)
        target_dir = settings.media_path / room_code
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{speech_id}.wav"
        if target.exists() and not target.is_symlink():
            target.unlink(missing_ok=True)
        self._remove_stale_parts(target_dir, speech_id)

        admission: LightTTSAdmissionLease | None = None
        global_admission_attempted = False
        generation = uuid.uuid4().hex
        temporary = target_dir / f".{speech_id}.{generation}.wav.part"
        self._ensure_scheduler()
        live_semaphore = self._semaphore
        if live_semaphore is None:
            raise RuntimeError("LightTTS scheduler initialization failed")

        async def deadline_or_caller_cancelled() -> bool:
            return loop.time() >= job_deadline or await self._cancel_requested(should_cancel)

        effective_should_cancel: Callable[[], bool | Awaitable[bool]] = deadline_or_caller_cancelled
        try:
            if settings.lighttts_global_gate_enabled:
                try:
                    global_admission_attempted = True
                    admission = await lighttts_admission_gate.acquire(should_cancel, deadline_monotonic=job_deadline)
                except LightTTSAdmissionCancelled as exc:
                    raise ProviderCancelled("LightTTS 排队任务已取消。") from exc
                except LightTTSAdmissionQueueFull as exc:
                    raise ProviderError(
                        "LightTTS 全局队列已满，请稍后重试。",
                        code="lighttts_queue_full",
                        retryable=True,
                        retry_after_seconds=2,
                    ) from exc
                except LightTTSAdmissionQueueTimeout as exc:
                    if loop.time() >= job_deadline:
                        raise ProviderError("LightTTS 全任务超时，未发布音频。", code="lighttts_job_timeout") from exc
                    raise ProviderError(
                        "LightTTS 全局排队超时，请稍后重试。",
                        code="lighttts_queue_timeout",
                        retryable=True,
                        retry_after_seconds=5,
                    ) from exc
                except LightTTSAdmissionUnavailable as exc:
                    raise ProviderError(
                        "LightTTS 全局调度服务不可用。",
                        code="lighttts_admission_unavailable",
                        retryable=True,
                        retry_after_seconds=5,
                    ) from exc
            if admission is not None:

                async def admission_cancel_requested() -> bool:
                    return await admission.cancellation_requested(deadline_or_caller_cancelled)

                effective_should_cancel = admission_cancel_requested
            await self._acquire_semaphore(live_semaphore, effective_should_cancel)
            try:
                await self._stream_bistream_candidate(
                    endpoint=endpoint,
                    chunks=chunks,
                    prompt_path=prompt_path,
                    prompt_text=prompt_text,
                    temporary=temporary,
                    generation=generation,
                    room_code=room_code,
                    speech_id=speech_id,
                    should_cancel=effective_should_cancel,
                    job_deadline=job_deadline,
                    on_stream_event=on_stream_event,
                )
                full_text = "".join(text_parts).strip()
                if not full_text:
                    raise ProviderError("TTS 文本为空。")
                if self._wav_duration(temporary) < self._minimum_duration(full_text):
                    await on_stream_event(
                        {
                            "type": "audio.stream.aborted",
                            "room_code": room_code,
                            "speech_id": speech_id,
                            "generation": generation,
                        }
                    )
                    raise ProviderError("LightTTS 返回的音频无效或过短。")
                if await self._cancel_requested(effective_should_cancel):
                    raise ProviderCancelled("LightTTS 运行任务已取消。")
                os.chmod(temporary, 0o600)
                os.replace(temporary, target)
            finally:
                live_semaphore.release()
        except ProviderCancelled as exc:
            if loop.time() >= job_deadline:
                lighttts_admission_gate.record_timeout()
                raise ProviderError("LightTTS 全任务超时，未发布音频。") from exc
            if admission is not None or not settings.lighttts_global_gate_enabled or not global_admission_attempted:
                lighttts_admission_gate.record_cancel()
            raise
        finally:
            temporary.unlink(missing_ok=True)
            if admission is not None:
                await admission.release()
        return f"/media/{room_code}/{target.name}"

    async def _synthesize_job(
        self,
        text: str,
        *,
        room_code: str,
        speech_id: str,
        voice: str = "debate_voice_1",
        provider_config: dict[str, Any] | None = None,
        should_cancel: Callable[[], bool | Awaitable[bool]] | None = None,
        background: bool = False,
        deadline_monotonic: float | None = None,
        on_stream_event: StreamEventCallback | None = None,
    ) -> str:
        loop = asyncio.get_running_loop()
        provider_deadline = loop.time() + settings.lighttts_job_timeout_seconds
        job_deadline = min(provider_deadline, deadline_monotonic) if deadline_monotonic is not None else provider_deadline
        if loop.time() >= job_deadline:
            raise ProviderError(
                "LightTTS 全任务超时，未发布音频。",
                code="lighttts_job_timeout",
            )
        if not text.strip():
            raise ProviderError("TTS 文本为空。")
        runtime = runtime_provider_config({"lighttts": provider_config} if provider_config is not None else None, "lighttts")
        if not runtime.get("enabled"):
            raise ProviderError("LightTTS 服务已停用。")
        endpoint = str(runtime.get("endpoint") or "").strip()
        if not endpoint:
            raise ProviderError("未配置 LightTTS 服务地址。")
        provider_settings = runtime.get("settings") if isinstance(runtime.get("settings"), dict) else {}
        try:
            read_timeout = max(30, min(300, int(provider_settings.get("read_timeout_seconds", 180))))
            speed = max(0.5, min(2.0, float(provider_settings.get("speed", 1.0))))
        except (TypeError, ValueError):
            read_timeout, speed = 180, 1.0
        prompt_path = self.prompt_path(voice)
        default_prompt_path = Path(settings.lighttts_prompt_wav_path)
        if not prompt_path.exists() or not default_prompt_path.exists():
            raise ProviderError(f"LightTTS prompt 音频不存在：{prompt_path}")
        target_dir = settings.media_path / room_code
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{speech_id}.wav"
        request_data = {
            "tts_model_name": "default",
            "speed": str(round(speed, 2)),
            "stream": "false",
        }
        chunks = self._split_text(text)
        minimum_duration = self._minimum_duration(text)
        if target.is_file() and not target.is_symlink() and self._wav_duration(target) >= minimum_duration:
            if await self._cancel_requested(should_cancel):
                raise ProviderCancelled("LightTTS 排队任务已取消。")
            return f"/media/{room_code}/{target.name}"
        if settings.app_env == "production":
            await require_lighttts_readiness(endpoint)
        # A short or invalid target is never a valid cache entry.  Remove it
        # before retrying so a deadline cannot resurrect stale audio.
        if target.exists() and not target.is_symlink():
            target.unlink(missing_ok=True)
        background_acquired = False
        admission: LightTTSAdmissionLease | None = None
        global_admission_attempted = False
        unsafe_shutdown_cancellation = False
        try:
            self._ensure_scheduler()
            live_semaphore = self._semaphore
            background_semaphore = self._background_semaphore
            if live_semaphore is None or background_semaphore is None:
                raise RuntimeError("LightTTS scheduler initialization failed")

            async def deadline_or_caller_cancelled() -> bool:
                return loop.time() >= job_deadline or await self._cancel_requested(should_cancel)

            effective_should_cancel = deadline_or_caller_cancelled
            if background:
                # Keep extra cue-prefetch jobs out of the shared Redis queue.
                # Otherwise one API process can consume every pending slot with
                # background work before a live speech even reaches admission.
                await self._acquire_semaphore(background_semaphore, effective_should_cancel)
                background_acquired = True
            if settings.lighttts_global_gate_enabled:
                try:
                    global_admission_attempted = True
                    admission = await lighttts_admission_gate.acquire(should_cancel, deadline_monotonic=job_deadline)
                except LightTTSAdmissionCancelled as exc:
                    raise ProviderCancelled("LightTTS 排队任务已取消。") from exc
                except LightTTSAdmissionQueueFull as exc:
                    raise ProviderError(
                        "LightTTS 全局队列已满，请稍后重试。",
                        code="lighttts_queue_full",
                        retryable=True,
                        retry_after_seconds=2,
                    ) from exc
                except LightTTSAdmissionQueueTimeout as exc:
                    if loop.time() >= job_deadline:
                        raise ProviderError(
                            "LightTTS 全任务超时，未发布音频。",
                            code="lighttts_job_timeout",
                        ) from exc
                    raise ProviderError(
                        "LightTTS 全局排队超时，请稍后重试。",
                        code="lighttts_queue_timeout",
                        retryable=True,
                        retry_after_seconds=5,
                    ) from exc
                except LightTTSAdmissionUnavailable as exc:
                    raise ProviderError(
                        "LightTTS 全局调度服务不可用。",
                        code="lighttts_admission_unavailable",
                        retryable=True,
                        retry_after_seconds=5,
                    ) from exc
            if admission is not None:

                async def admission_cancel_requested() -> bool:
                    return await admission.cancellation_requested(deadline_or_caller_cancelled)

                effective_should_cancel = admission_cancel_requested
            await self._acquire_semaphore(live_semaphore, effective_should_cancel)
            try:
                try:
                    async with httpx.AsyncClient(
                        timeout=httpx.Timeout(10, read=read_timeout, write=60, pool=10),
                        transport=self.transport,
                    ) as client:
                        primary_prompt_voice = voice if prompt_path != default_prompt_path else default_prompt_path.stem
                        candidates = [(prompt_path, self.prompt_text(primary_prompt_voice))]
                        if prompt_path != default_prompt_path:
                            candidates.append((default_prompt_path, self.prompt_text(default_prompt_path.stem)))
                        if target.is_file() and not target.is_symlink() and self._wav_duration(target) >= minimum_duration:
                            return f"/media/{room_code}/{target.name}"
                        # A different process can still be writing the same
                        # speech while this process waits for its live slot.
                        # Reap only files older than every supported job, never
                        # a fresh part that may belong to an active writer.
                        self._remove_stale_parts(target_dir, speech_id)
                        for candidate, candidate_prompt_text in candidates:
                            if (
                                settings.lighttts_streaming_enabled
                                and settings.lighttts_bistream_enabled
                                and on_stream_event is not None
                                and speed == 1.0
                            ):
                                generation = uuid.uuid4().hex
                                temporary = target_dir / f".{speech_id}.{generation}.wav.part"
                                try:
                                    await self._stream_bistream_candidate(
                                        endpoint=endpoint,
                                        chunks=list(chunks),
                                        prompt_path=candidate,
                                        prompt_text=candidate_prompt_text,
                                        temporary=temporary,
                                        generation=generation,
                                        room_code=room_code,
                                        speech_id=speech_id,
                                        should_cancel=effective_should_cancel,
                                        job_deadline=job_deadline,
                                        on_stream_event=on_stream_event,
                                    )
                                    if self._wav_duration(temporary) < minimum_duration:
                                        await on_stream_event(
                                            {
                                                "type": "audio.stream.aborted",
                                                "room_code": room_code,
                                                "speech_id": speech_id,
                                                "generation": generation,
                                            }
                                        )
                                        temporary.unlink(missing_ok=True)
                                        continue
                                    if await self._cancel_requested(effective_should_cancel):
                                        raise ProviderCancelled("LightTTS 运行任务已取消。")
                                    os.replace(temporary, target)
                                    break
                                except ProviderCancelled:
                                    raise
                                except (ProviderError, OSError, websockets.exceptions.WebSocketException) as exc:
                                    logger.warning(
                                        "LightTTS bi-stream candidate failed room=%s speech=%s voice=%s error=%s",
                                        room_code,
                                        speech_id,
                                        candidate.stem,
                                        type(exc).__name__,
                                    )
                                    temporary.unlink(missing_ok=True)
                                    continue
                            temporary = target_dir / f".{speech_id}.{uuid.uuid4().hex}.wav.part"
                            segment_paths: list[Path] = []
                            try:
                                candidate_chunks = list(chunks)
                                index = 0
                                candidate_valid = True
                                while index < len(candidate_chunks):
                                    chunk = candidate_chunks[index]
                                    try:
                                        response = await self._post_with_retry(
                                            client,
                                            endpoint,
                                            data=request_data | {"tts_text": chunk, "prompt_text": candidate_prompt_text},
                                            candidate=candidate,
                                            should_cancel=effective_should_cancel,
                                            job_deadline=job_deadline,
                                            read_timeout=read_timeout,
                                        )
                                    except asyncio.CancelledError:
                                        unsafe_shutdown_cancellation = True
                                        raise
                                    if response.status_code == 417 and self._visible_characters(chunk) > 1:
                                        smaller = self._split_text(
                                            chunk,
                                            max_characters=max(1, self._visible_characters(chunk) // 2),
                                        )
                                        if len(smaller) <= 1 or len(candidate_chunks) - 1 + len(smaller) > self.MAX_CHUNKS:
                                            response.raise_for_status()
                                        candidate_chunks[index : index + 1] = smaller
                                        continue
                                    response.raise_for_status()
                                    if not response.content:
                                        candidate_valid = False
                                        break
                                    if await self._cancel_requested(effective_should_cancel):
                                        raise ProviderCancelled("LightTTS 运行任务已取消。")
                                    segment = target_dir / f".{speech_id}.{uuid.uuid4().hex}.segment.wav.part"
                                    self._write_audio(segment, response.content)
                                    segment_paths.append(segment)
                                    if self._wav_duration(segment) < self._minimum_duration(chunk):
                                        candidate_valid = False
                                        break
                                    index += 1
                                if not candidate_valid:
                                    continue
                                self._merge_wav_segments(segment_paths, temporary)
                                if self._wav_duration(temporary) < minimum_duration:
                                    continue
                                if await self._cancel_requested(effective_should_cancel):
                                    raise ProviderCancelled("LightTTS 运行任务已取消。")
                                os.chmod(temporary, 0o600)
                                # Atomic replace is the publish commit point.
                                # Cancellation is checked immediately before it;
                                # once visible, this valid final must never be
                                # removed by a late caller/deadline transition.
                                os.replace(temporary, target)
                                break
                            finally:
                                temporary.unlink(missing_ok=True)
                                for segment in segment_paths:
                                    segment.unlink(missing_ok=True)
                        else:
                            raise ProviderError("LightTTS 返回的音频无效或过短。")
                except httpx.HTTPError as exc:
                    if loop.time() >= job_deadline:
                        raise ProviderCancelled("LightTTS 全任务已超时。") from exc
                    raise ProviderError(f"LightTTS 调用失败：{exc}") from exc
                except ProviderCancelled as exc:
                    if admission is not None and admission.lost:
                        raise ProviderError("LightTTS 全局调度租约丢失，已安全终止本次任务。") from exc
                    raise
            finally:
                live_semaphore.release()
        except ProviderCancelled as exc:
            if loop.time() >= job_deadline:
                lighttts_admission_gate.record_timeout()
                raise ProviderError("LightTTS 全任务超时，未发布音频。") from exc
            # Queue cancellation is already counted by the Redis gate before
            # a lease exists.  Count cancellation here only after admission,
            # or for the default local-only scheduler, to avoid double-counting.
            if admission is not None or not settings.lighttts_global_gate_enabled or not global_admission_attempted:
                lighttts_admission_gate.record_cancel()
            raise
        finally:
            if admission is not None:
                if unsafe_shutdown_cancellation:
                    await admission.abandon()
                else:
                    await admission.release()
            if background_acquired:
                background_semaphore.release()
        return f"/media/{room_code}/{target.name}"


class LightTTSIncrementalSession:
    """Queue stable clauses into one continuous LightTTS websocket."""

    _END = object()

    def __init__(
        self,
        provider: LightTTSProvider,
        *,
        room_code: str,
        speech_id: str,
        voice: str,
        provider_config: dict[str, Any] | None,
        should_cancel: Callable[[], bool | Awaitable[bool]] | None,
        deadline_monotonic: float | None,
        on_stream_event: StreamEventCallback,
    ) -> None:
        self.provider = provider
        self.room_code = room_code
        self.speech_id = speech_id
        self.voice = voice
        self.provider_config = provider_config
        self.should_cancel = should_cancel
        self.deadline_monotonic = deadline_monotonic
        self.on_stream_event = on_stream_event
        self._queue: asyncio.Queue[str | object] = asyncio.Queue(maxsize=32)
        self._text_parts: list[str] = []
        self._task: asyncio.Task[str] | None = None
        self._cancelled = asyncio.Event()
        self._closed = False
        # Reserve and readiness-check an endpoint while the Agent is producing
        # its first readable phrase.  The worker still waits for the first text
        # chunk before opening a GPU turn, so no empty synthesis is created.
        self._ensure_started()

    async def _cancel_requested(self) -> bool:
        return self._cancelled.is_set() or await self.provider._cancel_requested(self.should_cancel)

    async def _chunks(self) -> AsyncIterator[str]:
        while True:
            item = await self._queue.get()
            if item is self._END:
                return
            yield str(item)

    def _ensure_started(self) -> asyncio.Task[str]:
        if self._task is None:
            self._task = asyncio.create_task(
                self.provider._synthesize_incremental_job(
                    self._chunks(),
                    self._text_parts,
                    room_code=self.room_code,
                    speech_id=self.speech_id,
                    voice=self.voice,
                    provider_config=self.provider_config,
                    should_cancel=self._cancel_requested,
                    deadline_monotonic=self.deadline_monotonic,
                    on_stream_event=self.on_stream_event,
                )
            )
        return self._task

    def _raise_worker_failure(self) -> None:
        if self._task is not None and self._task.done():
            self._task.result()

    async def push_text(self, text: str) -> None:
        # Whitespace at an incremental boundary can be semantically required
        # for English terms embedded in a Chinese debate.  Reject an all-space
        # frame, but otherwise preserve the exact source slice selected by the
        # voice pipeline.
        chunk = text
        if not chunk.strip():
            return
        if self._closed:
            raise RealtimeVoiceSessionClosed("LightTTS 增量会话已结束。")
        self._raise_worker_failure()
        self._text_parts.append(chunk)
        task = self._ensure_started()
        put = asyncio.create_task(self._queue.put(chunk))
        done, _pending = await asyncio.wait({put, task}, return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            if not put.done():
                put.cancel()
                await asyncio.gather(put, return_exceptions=True)
            task.result()
        else:
            await put

    async def finish(self) -> str:
        if self._closed:
            raise RealtimeVoiceSessionClosed("LightTTS 增量会话已结束。")
        self._closed = True
        if not self._text_parts:
            raise ProviderError("TTS 文本为空。")
        task = self._ensure_started()
        put = asyncio.create_task(self._queue.put(self._END))
        try:
            done, _pending = await asyncio.wait({put, task}, return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                if not put.done():
                    put.cancel()
                    await asyncio.gather(put, return_exceptions=True)
                return task.result()
            await put
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            self._cancelled.set()
            raise

    async def abort(self) -> None:
        if self._closed and self._task is None:
            return
        self._closed = True
        self._cancelled.set()
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        if self._task is None:
            return
        if not self._task.done():
            try:
                self._queue.put_nowait(self._END)
            except asyncio.QueueFull:
                pass
        await asyncio.gather(self._task, return_exceptions=True)


class RealtimeVoiceSessionClosed(ProviderError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="realtime_voice_session_closed")


@dataclass
class _MossIdleWebSocket:
    endpoint: str
    connection: Any
    websocket: Any
    loop: asyncio.AbstractEventLoop
    created_at: float


class MossTTSRealtimeProvider:
    """Client for the native incremental MOSS-TTS-Realtime session API.

    The dedicated service owns the model, codec and prompt assets.  One API
    synthesis job maps to one upstream turn and one continuously growing PCM
    response, so text chunks never create independent voice contexts.
    """

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        ws_connect: Callable[..., Any] | None = None,
    ) -> None:
        self.transport = transport
        self.ws_connect = ws_connect or websockets.connect
        self._client: httpx.AsyncClient | None = None
        self._client_loop: asyncio.AbstractEventLoop | None = None
        self._client_lock: asyncio.Lock | None = None
        self._pool_loop: asyncio.AbstractEventLoop | None = None
        self._pool_key: tuple[tuple[str, ...], int, int] | None = None
        self._global_semaphore: asyncio.Semaphore | None = None
        self._endpoint_semaphores: list[tuple[str, asyncio.Semaphore]] = []
        self._endpoint_cursor = 0
        self._idle_ws_loop: asyncio.AbstractEventLoop | None = None
        self._idle_ws_key: tuple[tuple[str, ...], str] | None = None
        self._idle_ws_lock: asyncio.Lock | None = None
        self._idle_websockets: dict[str, _MossIdleWebSocket] = {}
        self._idle_ws_refill_tasks: dict[str, asyncio.Task[None]] = {}
        self._idle_ws_background_tasks: set[asyncio.Task[Any]] = set()
        self._idle_ws_closing = False

    async def _http_client(self) -> httpx.AsyncClient:
        """Keep one authenticated HTTP/1.1 pool warm for all turns in this process."""

        loop = asyncio.get_running_loop()
        if self._client_loop is not loop:
            previous = self._client
            self._client = None
            self._client_loop = loop
            self._client_lock = asyncio.Lock()
            if previous is not None and not previous.is_closed:
                try:
                    await previous.aclose()
                except RuntimeError:
                    logger.warning("MOSS HTTP pool belonged to a closed event loop; discarding it")
        lock = self._client_lock
        if lock is None:
            raise RuntimeError("MOSS HTTP client lock initialization failed")
        async with lock:
            if self._client is None or self._client.is_closed:
                headers = {}
                if settings.moss_tts_realtime_api_key:
                    headers["X-MOSS-Gateway-Key"] = settings.moss_tts_realtime_api_key
                self._client = httpx.AsyncClient(
                    timeout=None,
                    transport=self.transport,
                    headers=headers,
                    limits=httpx.Limits(
                        max_connections=max(6, settings.moss_tts_realtime_max_active * 3),
                        max_keepalive_connections=max(3, settings.moss_tts_realtime_max_active * 2),
                        keepalive_expiry=300,
                    ),
                )
            return self._client

    async def aclose(self) -> None:
        current_loop = asyncio.get_running_loop()
        idle_items = list(self._idle_websockets.values())
        refill_tasks = [*self._idle_ws_refill_tasks.values(), *self._idle_ws_background_tasks]
        self._idle_ws_closing = True
        self._idle_websockets = {}
        self._idle_ws_refill_tasks = {}
        self._idle_ws_background_tasks = set()
        self._idle_ws_lock = None
        self._idle_ws_key = None
        self._idle_ws_loop = None
        local_tasks: list[asyncio.Task[Any]] = []
        for task in refill_tasks:
            task_loop = task.get_loop()
            if task_loop is current_loop:
                task.cancel()
                local_tasks.append(task)
            elif not task_loop.is_closed():
                task_loop.call_soon_threadsafe(task.cancel)
        if local_tasks:
            await asyncio.gather(*local_tasks, return_exceptions=True)
        for item in idle_items:
            if item.loop is current_loop:
                await self._close_idle_websocket(item)
            else:
                self._abort_cross_loop_websocket(item.websocket)
        client = self._client
        self._client = None
        self._client_loop = None
        self._client_lock = None
        if client is not None and not client.is_closed:
            await client.aclose()

    @asynccontextmanager
    async def _client_context(self) -> AsyncIterator[httpx.AsyncClient]:
        yield await self._http_client()

    @classmethod
    def _candidate_urls(cls) -> tuple[str, ...]:
        candidates = [*settings.moss_tts_realtime_urls]
        if settings.moss_tts_realtime_url.strip():
            candidates.append(settings.moss_tts_realtime_url)
        normalized: list[str] = []
        for candidate in candidates:
            endpoint = cls._base_url(str(candidate))
            if endpoint not in normalized:
                normalized.append(endpoint)
        return tuple(normalized)

    def _ensure_pool(self) -> tuple[asyncio.Semaphore, list[tuple[str, asyncio.Semaphore]]]:
        loop = asyncio.get_running_loop()
        endpoints = self._candidate_urls()
        if not endpoints:
            raise ProviderError("未配置 MOSS-TTS-Realtime 服务地址。")
        total_capacity = min(
            settings.moss_tts_realtime_max_active,
            len(endpoints) * settings.moss_tts_realtime_max_active_per_endpoint,
        )
        key = (endpoints, total_capacity, settings.moss_tts_realtime_max_active_per_endpoint)
        if self._pool_loop is not loop or self._pool_key != key:
            self._pool_loop = loop
            self._pool_key = key
            self._global_semaphore = asyncio.Semaphore(total_capacity)
            self._endpoint_semaphores = [
                (endpoint, asyncio.Semaphore(settings.moss_tts_realtime_max_active_per_endpoint)) for endpoint in endpoints
            ]
            self._endpoint_cursor = 0
        if self._global_semaphore is None:
            raise RuntimeError("MOSS-TTS-Realtime scheduler initialization failed")
        return self._global_semaphore, self._endpoint_semaphores

    @staticmethod
    def _base_url(endpoint: str) -> str:
        raw = endpoint.strip()
        parts = urlsplit(raw)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise ProviderError("MOSS-TTS-Realtime 地址无效。")
        path = parts.path.rstrip("/")
        suffix = "/tts/session/start"
        if path.endswith(suffix):
            path = path[: -len(suffix)]
        return urlunsplit((parts.scheme, parts.netloc, path, "", "")).rstrip("/")

    @staticmethod
    def _websocket_url(base_url: str) -> str:
        parts = urlsplit(base_url)
        scheme = "wss" if parts.scheme == "https" else "ws"
        return urlunsplit((scheme, parts.netloc, f"{parts.path.rstrip('/')}/tts/session/ws", "", ""))

    def _new_websocket_connection(self, base_url: str) -> Any:
        headers = {}
        if settings.moss_tts_realtime_api_key:
            headers["X-MOSS-Gateway-Key"] = settings.moss_tts_realtime_api_key
        return self.ws_connect(
            self._websocket_url(base_url),
            additional_headers=headers,
            open_timeout=settings.moss_tts_realtime_connect_timeout_seconds,
            close_timeout=settings.moss_tts_realtime_close_timeout_seconds,
            ping_interval=20,
            ping_timeout=20,
            compression=None,
            max_size=None,
        )

    @staticmethod
    def _abort_cross_loop_websocket(websocket: Any) -> None:
        transport = getattr(websocket, "transport", None)
        abort = getattr(transport, "abort", None)
        if callable(abort):
            try:
                abort()
            except Exception:
                pass

    async def _close_idle_websocket(self, idle: _MossIdleWebSocket) -> None:
        try:
            await asyncio.wait_for(
                idle.connection.__aexit__(None, None, None),
                timeout=settings.moss_tts_realtime_close_timeout_seconds,
            )
        except Exception:
            logger.debug("failed to close idle MOSS websocket endpoint=%s", idle.endpoint, exc_info=True)

    @staticmethod
    def _websocket_is_open(websocket: Any) -> bool:
        if getattr(websocket, "closed", False) is True:
            return False
        if getattr(websocket, "close_code", None) is not None:
            return False
        state = getattr(websocket, "state", None)
        if state is None:
            return True
        name = getattr(state, "name", None)
        if name is not None:
            return name == "OPEN"
        return state == 1

    async def _ensure_idle_ws_state(self, endpoints: tuple[str, ...]) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        key = (endpoints, settings.moss_tts_realtime_api_key)
        if self._idle_ws_loop is loop and self._idle_ws_key == key and self._idle_ws_lock is not None:
            return self._idle_ws_lock

        old_loop = self._idle_ws_loop
        old_items = list(self._idle_websockets.values())
        old_tasks = [*self._idle_ws_refill_tasks.values(), *self._idle_ws_background_tasks]
        self._idle_ws_loop = loop
        self._idle_ws_key = key
        self._idle_ws_lock = asyncio.Lock()
        self._idle_websockets = {}
        self._idle_ws_refill_tasks = {}
        self._idle_ws_background_tasks = set()
        self._idle_ws_closing = False

        if old_loop is loop:
            for task in old_tasks:
                task.cancel()
            if old_tasks:
                await asyncio.gather(*old_tasks, return_exceptions=True)
            for item in old_items:
                await self._close_idle_websocket(item)
        else:
            if old_loop is not None and not old_loop.is_closed():
                for task in old_tasks:
                    old_loop.call_soon_threadsafe(task.cancel)
            for item in old_items:
                self._abort_cross_loop_websocket(item.websocket)
        return self._idle_ws_lock

    async def _recv_idle_control(self, websocket: Any) -> dict[str, Any]:
        try:
            raw = await asyncio.wait_for(
                websocket.recv(),
                timeout=settings.moss_tts_realtime_connect_timeout_seconds,
            )
        except Exception as exc:
            raise ProviderError(
                "MOSS-TTS-Realtime 空闲 WebSocket 验证失败。",
                code="moss_tts_ws_prewarm_failed",
                retryable=True,
                retry_after_seconds=2,
            ) from exc
        if not isinstance(raw, str):
            raise ProviderError(
                "MOSS-TTS-Realtime 空闲 WebSocket 返回了非控制帧。",
                code="moss_tts_ws_prewarm_failed",
            )
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProviderError(
                "MOSS-TTS-Realtime 空闲 WebSocket 返回了无效 JSON。",
                code="moss_tts_ws_prewarm_failed",
            ) from exc
        if not isinstance(payload, dict):
            raise ProviderError(
                "MOSS-TTS-Realtime 空闲 WebSocket 控制帧无效。",
                code="moss_tts_ws_prewarm_failed",
            )
        return payload

    async def _verify_idle_websocket(self, websocket: Any, *, include_health: bool) -> None:
        probe_id = uuid.uuid4().hex
        await websocket.send(json.dumps({"type": "ping", "id": probe_id}))
        pong = await self._recv_idle_control(websocket)
        if pong != {"type": "pong", "id": probe_id}:
            raise ProviderError(
                "MOSS-TTS-Realtime 空闲 WebSocket pong 契约无效。",
                code="moss_tts_ws_prewarm_failed",
            )
        if not include_health:
            return
        await websocket.send(json.dumps({"type": "health"}))
        health = await self._recv_idle_control(websocket)
        if health.get("type") != "health" or health.get("ok") is not True:
            raise ProviderError(
                "MOSS-TTS-Realtime 空闲 WebSocket health 契约无效。",
                code="moss_tts_ws_prewarm_failed",
            )

    async def _create_and_store_idle_websocket(self, endpoint: str) -> None:
        loop = asyncio.get_running_loop()
        connection = self._new_websocket_connection(endpoint)
        websocket: Any = None
        idle: _MossIdleWebSocket | None = None
        try:
            websocket = await asyncio.wait_for(
                connection.__aenter__(),
                timeout=settings.moss_tts_realtime_connect_timeout_seconds,
            )
            await self._verify_idle_websocket(websocket, include_health=True)
            idle = _MossIdleWebSocket(endpoint, connection, websocket, loop, loop.time())
            lock = self._idle_ws_lock
            if (
                lock is None
                or self._idle_ws_loop is not loop
                or self._idle_ws_closing
                or self._idle_ws_key is None
                or endpoint not in self._idle_ws_key[0]
            ):
                await self._close_idle_websocket(idle)
                return
            async with lock:
                if endpoint in self._idle_websockets:
                    await self._close_idle_websocket(idle)
                else:
                    self._idle_websockets[endpoint] = idle
        except BaseException:
            if websocket is not None and idle is None:
                await self._close_idle_websocket(_MossIdleWebSocket(endpoint, connection, websocket, loop, loop.time()))
            raise

    async def _ensure_idle_websocket(self, endpoint: str) -> bool:
        endpoints = self._candidate_urls()
        lock = await self._ensure_idle_ws_state(endpoints)
        async with lock:
            current = self._idle_websockets.get(endpoint)
            if current is not None and self._websocket_is_open(current.websocket):
                return True
            if current is not None:
                self._idle_websockets.pop(endpoint, None)
            task = self._idle_ws_refill_tasks.get(endpoint)
            if task is None:
                task = asyncio.create_task(self._create_and_store_idle_websocket(endpoint))
                self._idle_ws_refill_tasks[endpoint] = task

                def refill_done(done: asyncio.Task[None], *, selected_endpoint: str = endpoint) -> None:
                    if self._idle_ws_refill_tasks.get(selected_endpoint) is done:
                        self._idle_ws_refill_tasks.pop(selected_endpoint, None)
                    if not done.cancelled():
                        exception = done.exception()
                        if exception is not None:
                            logger.warning(
                                "MOSS idle websocket refill failed endpoint=%s error=%s",
                                selected_endpoint,
                                exception,
                            )

                task.add_done_callback(refill_done)
        if current is not None:
            await self._close_idle_websocket(current)
        await task
        return endpoint in self._idle_websockets

    def _schedule_idle_websocket_refill(self, endpoint: str) -> None:
        if self._idle_ws_closing or settings.moss_tts_realtime_transport != "websocket":
            return
        task = asyncio.create_task(self._ensure_idle_websocket(endpoint))
        self._idle_ws_background_tasks.add(task)

        def completed(done: asyncio.Task[Any]) -> None:
            self._idle_ws_background_tasks.discard(done)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(completed)

    async def _checkout_idle_websocket(self, endpoint: str) -> _MossIdleWebSocket | None:
        endpoints = self._candidate_urls()
        lock = await self._ensure_idle_ws_state(endpoints)
        async with lock:
            idle = self._idle_websockets.pop(endpoint, None)
        if idle is None:
            return None
        expired = (
            idle.loop is not asyncio.get_running_loop()
            or asyncio.get_running_loop().time() - idle.created_at >= settings.moss_tts_realtime_idle_ws_ttl_seconds
        )
        if expired or not self._websocket_is_open(idle.websocket):
            if idle.loop is asyncio.get_running_loop():
                await self._close_idle_websocket(idle)
            else:
                self._abort_cross_loop_websocket(idle.websocket)
            return None
        try:
            await self._verify_idle_websocket(idle.websocket, include_health=False)
        except ProviderError:
            await self._close_idle_websocket(idle)
            return None
        return idle

    @staticmethod
    async def _probe_readiness(
        client: httpx.AsyncClient,
        base_url: str,
        *,
        deadline: float,
    ) -> dict[str, Any]:
        remaining = deadline - asyncio.get_running_loop().time()
        timeout = min(settings.moss_tts_realtime_readiness_timeout_seconds, remaining)
        if timeout <= 0:
            raise ProviderError("MOSS-TTS-Realtime 全任务超时。", code="moss_tts_job_timeout")
        try:
            response = await asyncio.wait_for(client.get(f"{base_url}/health/ready"), timeout=timeout)
            response.raise_for_status()
            payload = response.json()
        except (asyncio.TimeoutError, httpx.HTTPError, ValueError) as exc:
            raise ProviderError(
                "MOSS-TTS-Realtime endpoint 未真正就绪。",
                code="moss_tts_not_ready",
                retryable=True,
                retry_after_seconds=2,
            ) from exc
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise ProviderError(
                "MOSS-TTS-Realtime endpoint 未真正就绪。",
                code="moss_tts_not_ready",
                retryable=True,
                retry_after_seconds=2,
            )
        if payload.get("model_warmed") is not True or int(payload.get("orphan_count") or 0) != 0:
            raise ProviderError(
                "MOSS-TTS-Realtime endpoint 尚未暖机或存在孤儿任务。",
                code="moss_tts_not_ready",
                retryable=True,
                retry_after_seconds=2,
            )
        return payload

    async def readiness_snapshot(self) -> dict[str, Any]:
        endpoints = self._candidate_urls()
        if not settings.moss_tts_realtime_enabled or not endpoints:
            return {"ok": False, "service": "MOSS-TTS-Realtime", "enabled": False, "endpoints": []}
        client = await self._http_client()
        loop = asyncio.get_running_loop()
        snapshots: list[dict[str, Any]] = []
        for index, endpoint in enumerate(endpoints):
            try:
                payload = await self._probe_readiness(
                    client,
                    endpoint,
                    deadline=loop.time() + settings.moss_tts_realtime_readiness_timeout_seconds,
                )
                snapshots.append(
                    {
                        "index": index,
                        "ok": True,
                        "model_warmed": True,
                        "active": payload.get("active"),
                        "pending": payload.get("pending"),
                        "orphan_count": payload.get("orphan_count", 0),
                    }
                )
            except ProviderError as exc:
                snapshots.append({"index": index, "ok": False, "code": exc.code})
        ready = sum(bool(item["ok"]) for item in snapshots)
        required = math.ceil(settings.moss_tts_realtime_max_active / settings.moss_tts_realtime_max_active_per_endpoint)
        return {
            "ok": ready >= required,
            "service": "MOSS-TTS-Realtime",
            "enabled": True,
            "ready_endpoints": ready,
            "required_endpoints": required,
            "endpoints": snapshots,
        }

    async def prewarm(self) -> dict[str, Any]:
        """Verify readiness and leave one authenticated, unstarted WS per endpoint."""

        snapshot = await self.readiness_snapshot()
        snapshot["transport"] = settings.moss_tts_realtime_transport
        snapshot["idle_ws_ready_endpoints"] = 0
        if not snapshot.get("enabled") or settings.moss_tts_realtime_transport != "websocket":
            return snapshot

        endpoints = self._candidate_urls()
        await self._ensure_idle_ws_state(endpoints)
        endpoint_snapshots = snapshot.get("endpoints")
        if not isinstance(endpoint_snapshots, list):
            return snapshot
        ready_endpoints = [
            endpoint
            for index, endpoint in enumerate(endpoints)
            if index < len(endpoint_snapshots) and endpoint_snapshots[index].get("ok") is True
        ]
        results = await asyncio.gather(
            *(self._ensure_idle_websocket(endpoint) for endpoint in ready_endpoints),
            return_exceptions=True,
        )
        idle_ready = 0
        result_by_endpoint = dict(zip(ready_endpoints, results, strict=True))
        for index, endpoint in enumerate(endpoints):
            item = endpoint_snapshots[index]
            result = result_by_endpoint.get(endpoint)
            item["idle_ws_ready"] = result is True
            if result is True:
                idle_ready += 1
            elif isinstance(result, ProviderError):
                item["idle_ws_code"] = result.code
            elif isinstance(result, BaseException):
                item["idle_ws_code"] = "moss_tts_ws_prewarm_failed"
        snapshot["idle_ws_ready_endpoints"] = idle_ready
        required = int(snapshot.get("required_endpoints") or 0)
        snapshot["ok"] = bool(snapshot.get("ok")) and idle_ready >= required
        return snapshot

    @staticmethod
    def _prompt_file(voice: str) -> str:
        allowed = {f"debate_voice_{index}" for index in range(1, 9)}
        if voice not in allowed:
            raise ProviderError("MOSS-TTS-Realtime 音色 ID 不在固定 8 音色目录中。")
        configured = settings.moss_tts_prompt_files.get(voice)
        if configured:
            return configured
        raise ProviderError(f"MOSS-TTS-Realtime 缺少音色配置：{voice}")

    async def _acquire(
        self,
        semaphore: asyncio.Semaphore,
        should_cancel: Callable[[], bool | Awaitable[bool]] | None,
        deadline: float,
    ) -> None:
        acquire = asyncio.create_task(semaphore.acquire())
        try:
            while not acquire.done():
                if await LightTTSProvider._cancel_requested(should_cancel):
                    raise ProviderCancelled("MOSS-TTS-Realtime 排队任务已取消。")
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise ProviderError("MOSS-TTS-Realtime 全任务超时。", code="moss_tts_job_timeout")
                done, _pending = await asyncio.wait({acquire}, timeout=min(0.1, remaining))
                if done:
                    break
            await acquire
        except BaseException:
            if not acquire.done():
                acquire.cancel()
                await asyncio.gather(acquire, return_exceptions=True)
            elif not acquire.cancelled() and acquire.exception() is None:
                semaphore.release()
            raise

    async def _acquire_endpoint(
        self,
        endpoints: list[tuple[str, asyncio.Semaphore]],
        should_cancel: Callable[[], bool | Awaitable[bool]] | None,
        deadline: float,
    ) -> tuple[str, asyncio.Semaphore]:
        if not endpoints:
            raise ProviderError("未配置 MOSS-TTS-Realtime 服务地址。")
        start = self._endpoint_cursor % len(endpoints)
        ordered = endpoints[start:] + endpoints[:start]
        pending = {asyncio.create_task(semaphore.acquire()): (endpoint, semaphore) for endpoint, semaphore in ordered}
        try:
            while pending:
                if await LightTTSProvider._cancel_requested(should_cancel):
                    raise ProviderCancelled("MOSS-TTS-Realtime 排队任务已取消。")
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise ProviderError("MOSS-TTS-Realtime 全任务超时。", code="moss_tts_job_timeout")
                done, _still_pending = await asyncio.wait(
                    pending,
                    timeout=min(0.1, remaining),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    continue
                chosen_task = next(task for task in pending if task in done)
                endpoint, semaphore = pending[chosen_task]
                for task in done:
                    extra_endpoint, extra_semaphore = pending.pop(task)
                    await task
                    if task is not chosen_task:
                        extra_semaphore.release()
                    else:
                        endpoint, semaphore = extra_endpoint, extra_semaphore
                self._endpoint_cursor = (endpoints.index((endpoint, semaphore)) + 1) % len(endpoints)
                return endpoint, semaphore
            raise RuntimeError("MOSS-TTS-Realtime endpoint scheduler exited unexpectedly")
        finally:
            remaining_tasks = list(pending.items())
            for task, _slot in remaining_tasks:
                if not task.done():
                    task.cancel()
            if remaining_tasks:
                await asyncio.gather(*(task for task, _slot in remaining_tasks), return_exceptions=True)
            for task, (_endpoint, semaphore) in remaining_tasks:
                if not task.cancelled() and task.exception() is None:
                    semaphore.release()

    async def _acquire_ready_endpoint(
        self,
        client: httpx.AsyncClient,
        endpoints: list[tuple[str, asyncio.Semaphore]],
        should_cancel: Callable[[], bool | Awaitable[bool]] | None,
        deadline: float,
    ) -> tuple[str, asyncio.Semaphore]:
        candidates = list(endpoints)
        failures: list[str] = []
        while candidates:
            endpoint, semaphore = await self._acquire_endpoint(candidates, should_cancel, deadline)
            try:
                payload = await self._probe_readiness(client, endpoint, deadline=deadline)
                # The semaphore only represents work scheduled by this API
                # process.  After an API restart, a lost WS release frame, or
                # a gateway-side cleanup race, the GPU endpoint can still own
                # an upstream turn while our local semaphore looks available.
                # Never start a second turn until authoritative gateway health
                # says the endpoint has physical capacity again.
                if int(payload.get("active") or 0) >= settings.moss_tts_realtime_max_active_per_endpoint:
                    raise ProviderError(
                        "MOSS-TTS-Realtime endpoint 仍在释放上一任务。",
                        code="moss_tts_busy",
                        retryable=True,
                        retry_after_seconds=1,
                    )
                return endpoint, semaphore
            except ProviderError as exc:
                semaphore.release()
                failures.append(exc.code)
                candidates = [item for item in candidates if item[0] != endpoint]
        error = ProviderError(
            "没有可用且已暖机的 MOSS-TTS-Realtime endpoint。",
            code="moss_tts_not_ready",
            retryable=True,
            retry_after_seconds=2,
        )
        if failures:
            raise error from ProviderError(",".join(failures))
        raise error

    async def _confirm_release(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        *,
        deadline: float,
    ) -> bool:
        try:
            payload = await self._probe_readiness(client, base_url, deadline=deadline)
        except ProviderError:
            return False
        return int(payload.get("active") or 0) == 0 and int(payload.get("orphan_count") or 0) == 0

    @staticmethod
    async def _post_json(
        client: httpx.AsyncClient,
        url: str,
        payload: dict[str, Any],
        *,
        deadline: float,
    ) -> dict[str, Any]:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise ProviderError("MOSS-TTS-Realtime 全任务超时。", code="moss_tts_job_timeout")
        try:
            response = await asyncio.wait_for(client.post(url, json=payload), timeout=remaining)
            response.raise_for_status()
            body = response.json()
        except asyncio.TimeoutError as exc:
            raise ProviderError("MOSS-TTS-Realtime 全任务超时。", code="moss_tts_job_timeout") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError(f"MOSS-TTS-Realtime 调用失败：{exc}") from exc
        if not isinstance(body, dict) or body.get("ok") is False:
            raise ProviderError("MOSS-TTS-Realtime 返回了无效控制响应。")
        return body

    async def _read_audio(
        self,
        client: httpx.AsyncClient,
        url: str,
        temporary: Path,
        *,
        room_code: str,
        speech_id: str,
        generation: str,
        should_cancel: Callable[[], bool | Awaitable[bool]] | None,
        deadline: float,
        first_pcm_deadline: float,
        on_stream_event: StreamEventCallback,
        headers_ready: asyncio.Event,
        publish_live: bool = True,
    ) -> tuple[int, int]:
        stream_url = f"/ws/rooms/{room_code}/audio"
        started = False
        first_pcm_at: str | None = None
        total_bytes = 0
        rtc_enabled = publish_live and livekit_audio_enabled()
        rtc_generation_started = False
        rtc_track_sid = ""
        rtc_start_buffer = bytearray()
        rtc_start_buffer_flushed = False
        pending_chunk: asyncio.Task[bytes] | None = None
        raw = None
        wav = None

        async def emit(event_type: str, **payload: Any) -> None:
            await on_stream_event(
                {
                    "type": event_type,
                    "room_code": room_code,
                    "speech_id": speech_id,
                    "generation": generation,
                    **payload,
                }
            )

        async def publish_rtc_pcm(audio: bytes, *, final: bool = False) -> str | None:
            nonlocal rtc_generation_started, rtc_track_sid, rtc_start_buffer_flushed
            if not rtc_enabled:
                return None
            if not rtc_generation_started:
                rtc_track_sid = await livekit_audio_registry.start_generation(
                    room_code,
                    speech_id,
                    generation,
                    sample_rate,
                    start_buffer_ms=settings.moss_tts_livekit_start_buffer_ms,
                )
                rtc_generation_started = True
            if not rtc_start_buffer_flushed:
                rtc_start_buffer.extend(audio)
                threshold_bytes = sample_rate * settings.moss_tts_livekit_start_buffer_ms // 1_000 * 2
                if len(rtc_start_buffer) < threshold_bytes and not final:
                    return None
                audio = bytes(rtc_start_buffer)
                rtc_start_buffer.clear()
                rtc_start_buffer_flushed = True
            if not audio:
                return None
            return await livekit_audio_registry.write_pcm(room_code, generation, audio)

        try:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise ProviderError("MOSS-TTS-Realtime 全任务超时。", code="moss_tts_job_timeout")
            async with client.stream("GET", url, timeout=None) as response:
                response.raise_for_status()
                try:
                    sample_rate = int(response.headers.get("X-Audio-Sample-Rate", "24000"))
                    channels = int(response.headers.get("X-Audio-Channels", "1"))
                except ValueError as exc:
                    raise ProviderError("MOSS-TTS-Realtime 返回了无效音频参数。") from exc
                codec = response.headers.get("X-Audio-Codec", "pcm_s16le").lower()
                if channels != 1 or codec != "pcm_s16le" or not 8_000 <= sample_rate <= 48_000:
                    raise ProviderError("MOSS-TTS-Realtime 音频格式必须是单声道 PCM16。")

                temporary.parent.mkdir(parents=True, exist_ok=True)
                raw = temporary.open("w+b")
                wav = wave.open(raw, "wb")
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(sample_rate)
                if rtc_enabled:
                    await livekit_audio_registry.ensure_room(room_code)
                headers_ready.set()

                iterator = response.aiter_bytes().__aiter__()
                while True:
                    if pending_chunk is None:
                        pending_chunk = asyncio.create_task(anext(iterator))
                    done, _pending = await asyncio.wait({pending_chunk}, timeout=0.1)
                    if not done:
                        if await LightTTSProvider._cancel_requested(should_cancel):
                            raise ProviderCancelled("MOSS-TTS-Realtime 运行任务已取消。")
                        if not started and asyncio.get_running_loop().time() >= first_pcm_deadline:
                            raise ProviderError(
                                "MOSS-TTS-Realtime 首个 PCM 超时。",
                                code="moss_tts_first_pcm_timeout",
                                retryable=True,
                                retry_after_seconds=2,
                            )
                        if asyncio.get_running_loop().time() >= deadline:
                            raise ProviderError("MOSS-TTS-Realtime 全任务超时。", code="moss_tts_job_timeout")
                        continue
                    try:
                        audio = pending_chunk.result()
                    except StopAsyncIteration:
                        pending_chunk = None
                        break
                    pending_chunk = None
                    if not audio:
                        continue
                    if len(audio) % 2:
                        raise ProviderError("MOSS-TTS-Realtime 返回了未对齐的 PCM16 数据。")
                    wav.writeframesraw(audio)
                    raw.flush()
                    total_bytes += len(audio)
                    first_pcm_at = first_pcm_at or datetime.now(timezone.utc).isoformat()
                    rtc_first_capture_at = await publish_rtc_pcm(audio) if rtc_enabled else None
                    if not started:
                        if rtc_enabled and not rtc_first_capture_at:
                            continue
                        started = True
                        await emit(
                            "audio.stream.started",
                            stream_url=None if rtc_enabled else stream_url,
                            sample_rate=sample_rate,
                            channels=1,
                            sample_width=2,
                            transport="livekit" if rtc_enabled else "websocket_pcm",
                            track_sid=rtc_track_sid or None,
                            tts_first_pcm_at=first_pcm_at,
                            server_first_capture_at=rtc_first_capture_at,
                        )

                if total_bytes <= 0:
                    raise ProviderError("MOSS-TTS-Realtime 未返回音频。")
                if rtc_generation_started:
                    rtc_first_capture_at = await publish_rtc_pcm(b"", final=True)
                    finished_capture_at = await livekit_audio_registry.finish_generation(room_code, generation)
                    rtc_first_capture_at = rtc_first_capture_at or finished_capture_at
                    if not started and rtc_first_capture_at:
                        started = True
                        await emit(
                            "audio.stream.started",
                            stream_url=None,
                            sample_rate=sample_rate,
                            channels=1,
                            sample_width=2,
                            transport="livekit",
                            track_sid=rtc_track_sid or None,
                            tts_first_pcm_at=first_pcm_at,
                            server_first_capture_at=rtc_first_capture_at,
                        )
                if not started:
                    raise ProviderError("MOSS-TTS-Realtime 未返回音频。")
                wav.close()
                wav = None
                raw.flush()
                os.fsync(raw.fileno())
                raw.close()
                raw = None
                os.chmod(temporary, 0o600)
                return sample_rate, total_bytes
        except BaseException:
            headers_ready.set()
            if rtc_generation_started:
                await livekit_audio_registry.abort_generation(room_code, generation)
            if pending_chunk is not None and not pending_chunk.done():
                pending_chunk.cancel()
                await asyncio.gather(pending_chunk, return_exceptions=True)
            if wav is not None:
                try:
                    wav.close()
                except Exception:
                    pass
            if raw is not None:
                try:
                    raw.close()
                except Exception:
                    pass
            if started:
                try:
                    await emit(
                        "audio.stream.aborted",
                        transport="livekit" if rtc_enabled else "websocket_pcm",
                        track_sid=rtc_track_sid or None,
                    )
                except Exception:
                    logger.exception("failed to publish MOSS stream abort room=%s speech=%s", room_code, speech_id)
            temporary.unlink(missing_ok=True)
            raise

    def open_incremental_session(
        self,
        *,
        room_code: str,
        speech_id: str,
        voice: str = "debate_voice_1",
        provider_config: dict[str, Any] | None = None,
        should_cancel: Callable[[], bool | Awaitable[bool]] | None = None,
        deadline_monotonic: float | None = None,
        on_stream_event: StreamEventCallback,
        publish_live: bool = True,
    ) -> MossTTSRealtimeIncrementalSession:
        if not settings.moss_tts_realtime_enabled:
            raise ProviderError("MOSS-TTS-Realtime 候选后端尚未启用。")
        if not settings.moss_tts_realtime_url.strip() and not settings.moss_tts_realtime_urls:
            raise ProviderError("未配置 MOSS-TTS-Realtime 服务地址。")
        return MossTTSRealtimeIncrementalSession(
            self,
            room_code=room_code,
            speech_id=speech_id,
            voice=voice,
            provider_config=provider_config,
            should_cancel=should_cancel,
            deadline_monotonic=deadline_monotonic,
            on_stream_event=on_stream_event,
            publish_live=publish_live,
        )

    async def synthesize(
        self,
        text: str,
        *,
        room_code: str,
        speech_id: str,
        voice: str = "debate_voice_1",
        should_cancel: Callable[[], bool | Awaitable[bool]] | None = None,
        deadline_monotonic: float | None = None,
        publish_live: bool = False,
    ) -> str:
        """Generate one immutable WAV through the same native MOSS session.

        Fixed cues are prepared before a match starts, so their PCM must not be
        published to the room's live track while it is being cached.
        """

        async def ignore_stream_event(_event: dict[str, Any]) -> None:
            return None

        session = self.open_incremental_session(
            room_code=room_code,
            speech_id=speech_id,
            voice=voice,
            should_cancel=should_cancel,
            deadline_monotonic=deadline_monotonic,
            on_stream_event=ignore_stream_event,
            publish_live=publish_live,
        )
        await session.push_text(text)
        return await session.finish()

    async def _synthesize_incremental_ws_job(
        self,
        chunks: AsyncIterator[str],
        text_parts: list[str],
        *,
        room_code: str,
        speech_id: str,
        voice: str,
        should_cancel: Callable[[], bool | Awaitable[bool]] | None,
        deadline_monotonic: float | None,
        on_stream_event: StreamEventCallback,
        publish_live: bool,
        on_session_ready: Callable[[], None | Awaitable[None]] | None = None,
    ) -> str:
        """Run one MOSS turn over one persistent bidirectional WebSocket."""

        loop = asyncio.get_running_loop()
        hard_deadline = deadline_monotonic
        admission_deadline = loop.time() + settings.moss_tts_realtime_queue_timeout_seconds
        deadline = min(admission_deadline, hard_deadline) if hard_deadline is not None else admission_deadline
        # Validate that the selected voice is part of the fixed allowlist.  The
        # gateway resolves the voice id to its own local prompt path; the API
        # must never send a host filesystem path over the formal WS protocol.
        self._prompt_file(voice)
        target_dir = settings.media_path / room_code
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{speech_id}.wav"
        if target.exists() and not target.is_symlink():
            target.unlink(missing_ok=True)
        LightTTSProvider._remove_stale_parts(target_dir, speech_id)
        generation = uuid.uuid4().hex
        temporary = target_dir / f".{speech_id}.{generation}.wav.part"
        upstream_session_id = f"{room_code}-{speech_id}-{generation}"
        await self._ensure_idle_ws_state(self._candidate_urls())
        global_semaphore, endpoint_semaphores = self._ensure_pool()
        global_acquired = False
        endpoint_semaphore: asyncio.Semaphore | None = None
        selected_endpoint: str | None = None
        endpoint_isolated = False
        upstream_release_acknowledged = False
        receiver: asyncio.Task[tuple[int, int]] | None = None
        websocket: Any = None
        aborting = False
        first_pcm_deadline: float | None = None
        ready_future: asyncio.Future[tuple[int, int]] = loop.create_future()
        first_pcm_future: asyncio.Future[None] = loop.create_future()
        pending_ack_future: asyncio.Future[None] | None = None
        pending_ack_seq: int | None = None

        async def cancellation_requested() -> bool:
            return await LightTTSProvider._cancel_requested(should_cancel)

        async def emit(event_type: str, **payload: Any) -> None:
            await on_stream_event(
                {
                    "type": event_type,
                    "room_code": room_code,
                    "speech_id": speech_id,
                    "generation": generation,
                    "tts_session_id": upstream_session_id,
                    "voice_id": voice,
                    "synthesis_mode": "single_session_incremental",
                    **payload,
                }
            )

        async def receive_messages() -> tuple[int, int]:
            nonlocal pending_ack_future
            nonlocal pending_ack_seq
            rtc_enabled = publish_live and livekit_audio_enabled()
            rtc_generation_started = False
            rtc_track_sid = ""
            rtc_start_buffer = bytearray()
            rtc_start_buffer_flushed = False
            started = False
            upstream_pcm_received = False
            total_bytes = 0
            sample_rate = 24_000
            raw = None
            wav = None
            aborted_event_emitted = False
            audio_ended = False
            audio_reset = False

            async def abort_media() -> None:
                nonlocal aborted_event_emitted
                if rtc_generation_started:
                    await livekit_audio_registry.abort_generation(room_code, generation)
                if started and not aborted_event_emitted:
                    aborted_event_emitted = True
                    try:
                        await emit(
                            "audio.stream.aborted",
                            transport="livekit" if rtc_enabled else "websocket_pcm",
                            track_sid=rtc_track_sid or None,
                        )
                    except Exception:
                        logger.exception("failed to publish MOSS WS stream abort room=%s speech=%s", room_code, speech_id)

            async def publish_rtc_pcm(audio: bytes, *, final: bool = False) -> str | None:
                nonlocal rtc_generation_started, rtc_track_sid, rtc_start_buffer_flushed
                if not rtc_enabled:
                    return None
                if not rtc_generation_started:
                    rtc_track_sid = await livekit_audio_registry.start_generation(
                        room_code,
                        speech_id,
                        generation,
                        sample_rate,
                        start_buffer_ms=settings.moss_tts_livekit_start_buffer_ms,
                    )
                    rtc_generation_started = True
                if not rtc_start_buffer_flushed:
                    rtc_start_buffer.extend(audio)
                    threshold_bytes = sample_rate * settings.moss_tts_livekit_start_buffer_ms // 1_000 * 2
                    if len(rtc_start_buffer) < threshold_bytes and not final:
                        return None
                    audio = bytes(rtc_start_buffer)
                    rtc_start_buffer.clear()
                    rtc_start_buffer_flushed = True
                if not audio:
                    return None
                return await livekit_audio_registry.write_pcm(room_code, generation, audio)

            def close_partial() -> None:
                nonlocal raw, wav
                if wav is not None:
                    try:
                        wav.close()
                    except Exception:
                        pass
                    wav = None
                if raw is not None:
                    try:
                        raw.close()
                    except Exception:
                        pass
                    raw = None

            try:
                while True:
                    if loop.time() >= deadline:
                        raise ProviderError("MOSS-TTS-Realtime 全任务超时。", code="moss_tts_job_timeout")
                    try:
                        message = await asyncio.wait_for(websocket.recv(), timeout=0.05)
                    except asyncio.TimeoutError:
                        if first_pcm_deadline is not None and not upstream_pcm_received and loop.time() >= first_pcm_deadline:
                            raise ProviderError(
                                "MOSS-TTS-Realtime 首个 PCM 超时。",
                                code="moss_tts_first_pcm_timeout",
                                retryable=True,
                                retry_after_seconds=2,
                            )
                        continue
                    except Exception as exc:
                        raise ProviderError(
                            "MOSS-TTS-Realtime WebSocket 在释放确认前断开。",
                            code="moss_tts_ws_disconnected",
                            retryable=True,
                            retry_after_seconds=2,
                        ) from exc

                    if isinstance(message, bytes):
                        if not ready_future.done() or first_pcm_deadline is None:
                            raise ProviderError(
                                "MOSS-TTS-Realtime 在 ready/正文 delta 前返回了音频。",
                                code="moss_tts_ws_protocol_error",
                            )
                        if len(message) % 2:
                            raise ProviderError(
                                "MOSS-TTS-Realtime 返回了未对齐的 PCM16 数据。",
                                code="moss_tts_ws_protocol_error",
                            )
                        if wav is None or raw is None:
                            raise ProviderError(
                                "MOSS-TTS-Realtime PCM writer 尚未就绪。",
                                code="moss_tts_ws_protocol_error",
                            )
                        upstream_pcm_received = True
                        wav.writeframesraw(message)
                        raw.flush()
                        total_bytes += len(message)
                        rtc_first_capture_at = await publish_rtc_pcm(message) if rtc_enabled else None
                        if not started:
                            if rtc_enabled and not rtc_first_capture_at:
                                continue
                            started = True
                            if not first_pcm_future.done():
                                first_pcm_future.set_result(None)
                            await emit(
                                "audio.stream.started",
                                stream_url=None if rtc_enabled else f"/ws/rooms/{room_code}/audio",
                                sample_rate=sample_rate,
                                channels=1,
                                sample_width=2,
                                transport="livekit" if rtc_enabled else "websocket_pcm",
                                track_sid=rtc_track_sid or None,
                                server_first_capture_at=rtc_first_capture_at,
                            )
                        continue

                    if not isinstance(message, str):
                        raise ProviderError(
                            "MOSS-TTS-Realtime WebSocket 返回了未知帧类型。",
                            code="moss_tts_ws_protocol_error",
                        )
                    try:
                        payload = json.loads(message)
                    except json.JSONDecodeError as exc:
                        raise ProviderError(
                            "MOSS-TTS-Realtime WebSocket 返回了无效 JSON。",
                            code="moss_tts_ws_protocol_error",
                        ) from exc
                    if not isinstance(payload, dict):
                        raise ProviderError(
                            "MOSS-TTS-Realtime WebSocket 控制帧格式无效。",
                            code="moss_tts_ws_protocol_error",
                        )
                    if payload.get("session_id") not in {None, upstream_session_id}:
                        raise ProviderError(
                            "MOSS-TTS-Realtime WebSocket session_id 不匹配。",
                            code="moss_tts_ws_protocol_error",
                        )
                    event_type = str(payload.get("type") or "").lower()
                    if event_type == "ready":
                        if ready_future.done() or pending_ack_future is not None:
                            raise ProviderError(
                                "MOSS-TTS-Realtime WebSocket ready 顺序无效。",
                                code="moss_tts_ws_protocol_error",
                            )
                        audio = payload.get("audio")
                        if payload.get("voice") != voice or payload.get("next_seq") != 1 or not isinstance(audio, dict):
                            raise ProviderError(
                                "MOSS-TTS-Realtime WebSocket ready 契约无效。",
                                code="moss_tts_ws_protocol_error",
                            )
                        try:
                            sample_rate = int(audio.get("sample_rate") or 0)
                            channels = int(audio.get("channels") or 0)
                        except (TypeError, ValueError) as exc:
                            raise ProviderError(
                                "MOSS-TTS-Realtime WebSocket 音频参数无效。",
                                code="moss_tts_ws_protocol_error",
                            ) from exc
                        codec = str(audio.get("codec") or "").lower()
                        if channels != 1 or codec != "pcm_s16le" or not 8_000 <= sample_rate <= 48_000:
                            raise ProviderError(
                                "MOSS-TTS-Realtime WebSocket 必须 ready 并返回单声道 PCM16。",
                                code="moss_tts_ws_protocol_error",
                            )
                        temporary.parent.mkdir(parents=True, exist_ok=True)
                        raw = temporary.open("w+b")
                        wav = wave.open(raw, "wb")
                        wav.setnchannels(1)
                        wav.setsampwidth(2)
                        wav.setframerate(sample_rate)
                        if rtc_enabled:
                            await livekit_audio_registry.ensure_room(room_code)
                        ready_future.set_result((sample_rate, 1))
                        continue
                    if event_type == "ack":
                        sequence = payload.get("seq")
                        if (
                            isinstance(sequence, bool)
                            or not isinstance(sequence, int)
                            or pending_ack_future is None
                            or pending_ack_seq != sequence
                            or pending_ack_future.done()
                        ):
                            raise ProviderError(
                                "MOSS-TTS-Realtime WebSocket ACK 乱序或重复。",
                                code="moss_tts_ws_protocol_error",
                            )
                        pending_ack_future.set_result(None)
                        pending_ack_future = None
                        pending_ack_seq = None
                        continue
                    if event_type == "error":
                        raise ProviderError(
                            "MOSS-TTS-Realtime WebSocket 上游返回错误。",
                            code="moss_tts_ws_upstream_error",
                            retryable=True,
                            retry_after_seconds=2,
                        )
                    if event_type == "audio_end":
                        if aborting or audio_ended or pending_ack_future is not None:
                            raise ProviderError(
                                "MOSS-TTS-Realtime WebSocket audio_end 顺序无效。",
                                code="moss_tts_ws_protocol_error",
                            )
                        audio_ended = True
                        continue
                    if event_type == "audio_reset":
                        if not aborting or audio_reset or pending_ack_future is not None:
                            raise ProviderError(
                                "MOSS-TTS-Realtime WebSocket audio_reset 顺序无效。",
                                code="moss_tts_ws_protocol_error",
                            )
                        audio_reset = True
                        continue
                    if event_type == "released":
                        if payload.get("released") is not True or pending_ack_future is not None:
                            raise ProviderError(
                                "MOSS-TTS-Realtime WebSocket 释放确认无效或顺序错误。",
                                code="moss_tts_ws_protocol_error",
                            )
                        status = str(payload.get("status") or "")
                        if aborting:
                            if status != "aborted" or not audio_reset:
                                raise ProviderError(
                                    "MOSS-TTS-Realtime WebSocket 中止释放确认无效。",
                                    code="moss_tts_ws_protocol_error",
                                )
                            await abort_media()
                            close_partial()
                            temporary.unlink(missing_ok=True)
                            return sample_rate, total_bytes
                        if status == "aborted" or not audio_ended:
                            raise ProviderError(
                                "MOSS-TTS-Realtime WebSocket 在 audio_end 前释放。",
                                code="moss_tts_ws_protocol_error",
                            )
                        if total_bytes <= 0:
                            raise ProviderError("MOSS-TTS-Realtime 未返回音频。", code="moss_tts_ws_protocol_error")
                        if rtc_generation_started:
                            rtc_first_capture_at = await publish_rtc_pcm(b"", final=True)
                            finished_capture_at = await livekit_audio_registry.finish_generation(room_code, generation)
                            rtc_first_capture_at = rtc_first_capture_at or finished_capture_at
                            if not started and rtc_first_capture_at:
                                started = True
                                if not first_pcm_future.done():
                                    first_pcm_future.set_result(None)
                                await emit(
                                    "audio.stream.started",
                                    stream_url=None,
                                    sample_rate=sample_rate,
                                    channels=1,
                                    sample_width=2,
                                    transport="livekit",
                                    track_sid=rtc_track_sid or None,
                                    server_first_capture_at=rtc_first_capture_at,
                                )
                        if not started:
                            raise ProviderError(
                                "MOSS-TTS-Realtime 未返回音频。",
                                code="moss_tts_ws_protocol_error",
                            )
                        if wav is None or raw is None:
                            raise ProviderError(
                                "MOSS-TTS-Realtime PCM writer 提前关闭。",
                                code="moss_tts_ws_protocol_error",
                            )
                        wav.close()
                        wav = None
                        raw.flush()
                        os.fsync(raw.fileno())
                        raw.close()
                        raw = None
                        os.chmod(temporary, 0o600)
                        return sample_rate, total_bytes
                    raise ProviderError(
                        "MOSS-TTS-Realtime WebSocket 返回了未知控制事件。",
                        code="moss_tts_ws_protocol_error",
                    )
            except BaseException:
                await abort_media()
                close_partial()
                temporary.unlink(missing_ok=True)
                raise

        async def wait_for_future(
            future: asyncio.Future[Any],
            *,
            timeout_seconds: float,
            timeout_code: str,
        ) -> Any:
            control_deadline = min(deadline, loop.time() + timeout_seconds)
            while True:
                if receiver is not None and receiver.done():
                    receiver.result()
                if await cancellation_requested():
                    raise ProviderCancelled("MOSS-TTS-Realtime 运行任务已取消。")
                remaining = control_deadline - loop.time()
                if remaining <= 0:
                    raise ProviderError(
                        "MOSS-TTS-Realtime WebSocket 控制响应超时。",
                        code=timeout_code,
                        retryable=True,
                        retry_after_seconds=2,
                    )
                wait_set: set[asyncio.Future[Any] | asyncio.Task[Any]] = {future}
                if receiver is not None:
                    wait_set.add(receiver)
                done, _pending = await asyncio.wait(wait_set, timeout=min(0.05, remaining), return_when=asyncio.FIRST_COMPLETED)
                if receiver is not None and receiver in done:
                    receiver.result()
                if future in done:
                    return future.result()

        async def send_with_ack(
            payload: dict[str, Any],
            sequence: int,
            *,
            timeout_seconds: float | None = None,
            timeout_code: str = "moss_tts_ws_ack_timeout",
        ) -> None:
            nonlocal pending_ack_future, pending_ack_seq
            if pending_ack_future is not None:
                raise ProviderError(
                    "MOSS-TTS-Realtime WebSocket 同时存在多个未确认 delta。",
                    code="moss_tts_ws_protocol_error",
                )
            pending_ack_seq = sequence
            pending_ack_future = loop.create_future()
            await websocket.send(json.dumps({**payload, "seq": sequence}, ensure_ascii=False))
            await wait_for_future(
                pending_ack_future,
                timeout_seconds=(settings.moss_tts_realtime_ack_timeout_seconds if timeout_seconds is None else timeout_seconds),
                timeout_code=timeout_code,
            )

        async def next_chunk() -> str | None:
            task = asyncio.create_task(anext(chunks))
            try:
                while True:
                    if receiver is not None and receiver.done():
                        receiver.result()
                    if await cancellation_requested():
                        raise ProviderCancelled("MOSS-TTS-Realtime 运行任务已取消。")
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        raise ProviderError("MOSS-TTS-Realtime 全任务超时。", code="moss_tts_job_timeout")
                    wait_set: set[asyncio.Task[Any]] = {task}
                    if receiver is not None:
                        wait_set.add(receiver)
                    done, _pending = await asyncio.wait(
                        wait_set,
                        timeout=min(0.05, remaining),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if receiver is not None and receiver in done:
                        receiver.result()
                    if task in done:
                        try:
                            return str(task.result())
                        except StopAsyncIteration:
                            return None
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

        isolation_codes = {
            "moss_tts_first_pcm_timeout",
            "moss_tts_ws_ack_timeout",
            "moss_tts_ws_disconnected",
            "moss_tts_ws_protocol_error",
            "moss_tts_ws_ready_timeout",
            "moss_tts_ws_release_timeout",
            "moss_tts_ws_upstream_error",
        }

        async def abort_ws_after_failure(exc: BaseException) -> None:
            nonlocal aborting, endpoint_isolated, pending_ack_future, pending_ack_seq, upstream_release_acknowledged
            endpoint_isolated = isinstance(exc, ProviderError) and exc.code in isolation_codes
            if websocket is None or upstream_release_acknowledged:
                return
            aborting = True
            if pending_ack_future is not None and not pending_ack_future.done():
                pending_ack_future.cancel()
            pending_ack_future = None
            pending_ack_seq = None
            try:
                await websocket.send(
                    json.dumps(
                        {
                            "type": "abort",
                            "reason": "v2_cancelled" if isinstance(exc, ProviderCancelled) else "v2_failed",
                        },
                        ensure_ascii=False,
                    )
                )
                if receiver is not None and not receiver.done():
                    await asyncio.wait_for(
                        asyncio.shield(receiver),
                        timeout=settings.moss_tts_realtime_close_timeout_seconds,
                    )
                    upstream_release_acknowledged = True
            except Exception:
                logger.warning("MOSS WS abort release acknowledgement failed session=%s", upstream_session_id)

        try:
            await self._acquire(global_semaphore, should_cancel, deadline)
            global_acquired = True
            client = await self._http_client()
            base_url, endpoint_semaphore = await self._acquire_ready_endpoint(
                client,
                endpoint_semaphores,
                should_cancel,
                deadline,
            )
            selected_endpoint = base_url
            try:
                idle = await self._checkout_idle_websocket(base_url)
                connection = idle.connection if idle is not None else self._new_websocket_connection(base_url)
                websocket_connection = (
                    idle.websocket
                    if idle is not None
                    else await asyncio.wait_for(
                        connection.__aenter__(),
                        timeout=settings.moss_tts_realtime_connect_timeout_seconds,
                    )
                )
                try:
                    websocket = websocket_connection
                    try:
                        receiver = asyncio.create_task(receive_messages())
                        await websocket.send(
                            json.dumps(
                                {
                                    "type": "start",
                                    "seq": 0,
                                    "session_id": upstream_session_id,
                                    "voice": voice,
                                    "user_text": settings.moss_tts_realtime_speech_instruction,
                                },
                                ensure_ascii=False,
                            )
                        )
                        await wait_for_future(
                            ready_future,
                            timeout_seconds=settings.moss_tts_realtime_connect_timeout_seconds,
                            timeout_code="moss_tts_ws_ready_timeout",
                        )
                        if on_session_ready is not None:
                            ready_result = on_session_ready()
                            if inspect.isawaitable(ready_result):
                                await ready_result
                        active_deadline = loop.time() + settings.moss_tts_realtime_job_timeout_seconds
                        deadline = min(active_deadline, hard_deadline) if hard_deadline is not None else active_deadline
                        first_chunk = await next_chunk()
                        if first_chunk is None:
                            raise ProviderError("TTS 文本为空。")
                        sequence = 1
                        first_pcm_deadline = min(
                            deadline,
                            loop.time() + settings.moss_tts_realtime_first_pcm_timeout_seconds,
                        )
                        await send_with_ack({"type": "text_delta", "text": first_chunk}, sequence)
                        if not livekit_audio_enabled():
                            await wait_for_future(
                                first_pcm_future,
                                timeout_seconds=settings.moss_tts_realtime_first_pcm_timeout_seconds,
                                timeout_code="moss_tts_first_pcm_timeout",
                            )
                        while True:
                            chunk = await next_chunk()
                            if chunk is None:
                                break
                            sequence += 1
                            await send_with_ack({"type": "text_delta", "text": chunk}, sequence)
                        if await cancellation_requested():
                            raise ProviderCancelled("MOSS-TTS-Realtime 运行任务已取消。")
                        sequence += 1
                        # A delta ACK only confirms that one text fragment was
                        # accepted.  The final ACK is deliberately different:
                        # the gateway sends it after the model has completed
                        # the whole continuous utterance.  Applying the short
                        # control ACK timeout here aborts healthy 30-90 second
                        # speeches mid-stream and can leave GPU work orphaned.
                        await send_with_ack(
                            {"type": "final"},
                            sequence,
                            timeout_seconds=max(0.001, deadline - loop.time()),
                            timeout_code="moss_tts_job_timeout",
                        )
                        try:
                            await asyncio.wait_for(
                                asyncio.shield(receiver),
                                timeout=min(
                                    settings.moss_tts_realtime_close_timeout_seconds,
                                    max(0.0, deadline - loop.time()),
                                ),
                            )
                        except asyncio.TimeoutError as exc:
                            raise ProviderError(
                                "MOSS-TTS-Realtime WebSocket 未确认会话释放。",
                                code="moss_tts_ws_release_timeout",
                                retryable=True,
                                retry_after_seconds=2,
                            ) from exc
                        upstream_release_acknowledged = True
                    except BaseException as exc:
                        await abort_ws_after_failure(exc)
                        raise
                finally:
                    await asyncio.wait_for(
                        connection.__aexit__(None, None, None),
                        timeout=settings.moss_tts_realtime_close_timeout_seconds,
                    )
            except (ProviderError, ProviderCancelled):
                raise
            except Exception as exc:
                raise ProviderError(
                    "MOSS-TTS-Realtime WebSocket 连接失败或异常断开。",
                    code="moss_tts_ws_disconnected",
                    retryable=True,
                    retry_after_seconds=2,
                ) from exc

            full_text = "".join(text_parts).strip()
            if not full_text:
                raise ProviderError("TTS 文本为空。")
            if LightTTSProvider._wav_duration(temporary) < LightTTSProvider._minimum_duration(full_text):
                raise ProviderError("MOSS-TTS-Realtime 返回的音频无效或过短。")
            if await cancellation_requested():
                raise ProviderCancelled("MOSS-TTS-Realtime 运行任务已取消。")
            os.replace(temporary, target)
            return f"/media/{room_code}/{target.name}"
        except BaseException as exc:
            if isinstance(exc, ProviderError) and exc.code in isolation_codes:
                endpoint_isolated = True
            elif websocket is None and endpoint_semaphore is not None:
                endpoint_isolated = True
            raise
        finally:
            if receiver is not None and not receiver.done():
                receiver.cancel()
                await asyncio.gather(receiver, return_exceptions=True)
            temporary.unlink(missing_ok=True)
            recovered_release = False
            if endpoint_semaphore is not None and selected_endpoint is not None and not upstream_release_acknowledged:
                # A room may be terminated while the gateway is between its
                # abort and released frames. If the control WS disappears, the
                # endpoint used to remain semaphore-isolated forever even
                # after /health/ready reported active=0 and orphan_count=0.
                # Reconcile the ambiguity before returning the slot.  This is
                # required for every failure class, not only cancellations: a
                # lost delta/release ACK previously leaked the sole endpoint
                # semaphore forever, so all later speeches waited for the full
                # 240-second job deadline even after gateway health was idle.
                recovery_deadline = loop.time() + max(
                    settings.moss_tts_realtime_readiness_timeout_seconds,
                    settings.moss_tts_realtime_close_timeout_seconds,
                )
                try:
                    recovery_client = await self._http_client()
                    recovered_release = await self._confirm_release(
                        recovery_client,
                        selected_endpoint,
                        deadline=recovery_deadline,
                    )
                except BaseException:
                    recovered_release = False
            release_verified = upstream_release_acknowledged or recovered_release
            if endpoint_semaphore is not None:
                # This semaphore is a scheduler slot, not a permanent circuit
                # breaker.  Always return it.  The next acquisition probes
                # /health/ready and refuses endpoints whose active/orphan state
                # is unsafe, so a gateway fault fails fast instead of silently
                # wedging every room behind an unreleasable local lock.
                endpoint_semaphore.release()
                if selected_endpoint is not None and release_verified:
                    self._schedule_idle_websocket_refill(selected_endpoint)
                if recovered_release:
                    logger.warning("MOSS WS endpoint recovered from missing release ACK speech=%s", speech_id)
                elif not release_verified:
                    logger.error(
                        "MOSS WS release unverified; endpoint will require a fresh readiness/capacity probe speech=%s isolated=%s",
                        speech_id,
                        endpoint_isolated,
                    )
            if global_acquired:
                global_semaphore.release()

    async def _synthesize_incremental_job(
        self,
        chunks: AsyncIterator[str],
        text_parts: list[str],
        **kwargs: Any,
    ) -> str:
        if settings.moss_tts_realtime_transport == "http":
            return await self._synthesize_incremental_http_job(chunks, text_parts, **kwargs)
        return await self._synthesize_incremental_ws_job(chunks, text_parts, **kwargs)

    async def _synthesize_incremental_http_job(
        self,
        chunks: AsyncIterator[str],
        text_parts: list[str],
        *,
        room_code: str,
        speech_id: str,
        voice: str,
        should_cancel: Callable[[], bool | Awaitable[bool]] | None,
        deadline_monotonic: float | None,
        on_stream_event: StreamEventCallback,
        publish_live: bool,
        on_session_ready: Callable[[], None | Awaitable[None]] | None = None,
    ) -> str:
        loop = asyncio.get_running_loop()
        hard_deadline = deadline_monotonic
        admission_deadline = loop.time() + settings.moss_tts_realtime_queue_timeout_seconds
        deadline = min(admission_deadline, hard_deadline) if hard_deadline is not None else admission_deadline
        prompt_file = self._prompt_file(voice)
        target_dir = settings.media_path / room_code
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{speech_id}.wav"
        if target.exists() and not target.is_symlink():
            target.unlink(missing_ok=True)
        LightTTSProvider._remove_stale_parts(target_dir, speech_id)
        generation = uuid.uuid4().hex
        temporary = target_dir / f".{speech_id}.{generation}.wav.part"
        upstream_session_id = f"{room_code}-{speech_id}-{generation}"
        global_semaphore, endpoint_semaphores = self._ensure_pool()
        global_acquired = False
        endpoint_semaphore: asyncio.Semaphore | None = None
        reader: asyncio.Task[tuple[int, int]] | None = None
        reader_completed = False
        headers_ready = asyncio.Event()
        session_started = False
        upstream_release_acknowledged = False
        try:
            await self._acquire(global_semaphore, should_cancel, deadline)
            global_acquired = True
            client = await self._http_client()
            base_url, endpoint_semaphore = await self._acquire_ready_endpoint(
                client,
                endpoint_semaphores,
                should_cancel,
                deadline,
            )
            # HTTP cannot pre-open the complete bidirectional text/audio pair,
            # but it still reserves scarce endpoint capacity before Agent text
            # is allowed to appear.
            if on_session_ready is not None:
                ready_result = on_session_ready()
                if inspect.isawaitable(ready_result):
                    await ready_result
            active_deadline = loop.time() + settings.moss_tts_realtime_job_timeout_seconds
            deadline = min(active_deadline, hard_deadline) if hard_deadline is not None else active_deadline
            try:
                first_chunk = await anext(chunks)
            except StopAsyncIteration as exc:
                raise ProviderError("TTS 文本为空。") from exc

            async with self._client_context() as client:
                close_acknowledged = False
                try:
                    first_text_sent_at = loop.time()
                    first_pcm_deadline = min(
                        deadline,
                        first_text_sent_at + settings.moss_tts_realtime_first_pcm_timeout_seconds,
                    )
                    await self._post_json(
                        client,
                        f"{base_url}/tts/session/start",
                        {
                            "session_id": upstream_session_id,
                            "assistant_text": "",
                            "user_text": settings.moss_tts_realtime_speech_instruction,
                            "prompt_audio": prompt_file,
                            "user_audio": None,
                            "new_turn": True,
                        },
                        deadline=deadline,
                    )
                    session_started = True
                    reader = asyncio.create_task(
                        self._read_audio(
                            client,
                            f"{base_url}/tts/session/{upstream_session_id}/audio",
                            temporary,
                            room_code=room_code,
                            speech_id=speech_id,
                            generation=generation,
                            should_cancel=should_cancel,
                            deadline=deadline,
                            first_pcm_deadline=first_pcm_deadline,
                            on_stream_event=on_stream_event,
                            headers_ready=headers_ready,
                            publish_live=publish_live,
                        )
                    )
                    ready_wait = asyncio.create_task(headers_ready.wait())
                    try:
                        remaining = max(0.0, min(deadline, first_pcm_deadline) - loop.time())
                        done, _pending = await asyncio.wait(
                            {ready_wait, reader},
                            timeout=min(settings.moss_tts_realtime_connect_timeout_seconds, remaining),
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if reader in done:
                            reader.result()
                        if ready_wait not in done:
                            if loop.time() >= first_pcm_deadline:
                                raise ProviderError(
                                    "MOSS-TTS-Realtime 首个 PCM 超时。",
                                    code="moss_tts_first_pcm_timeout",
                                    retryable=True,
                                    retry_after_seconds=2,
                                )
                            raise ProviderError("MOSS-TTS-Realtime 音频流连接超时。")
                        await ready_wait
                    finally:
                        if not ready_wait.done():
                            ready_wait.cancel()
                            await asyncio.gather(ready_wait, return_exceptions=True)

                    await self._post_json(
                        client,
                        f"{base_url}/tts/session/push",
                        {"session_id": upstream_session_id, "text": first_chunk, "is_final": False},
                        deadline=deadline,
                    )

                    async for chunk in chunks:
                        if await LightTTSProvider._cancel_requested(should_cancel):
                            raise ProviderCancelled("MOSS-TTS-Realtime 运行任务已取消。")
                        await self._post_json(
                            client,
                            f"{base_url}/tts/session/push",
                            {"session_id": upstream_session_id, "text": chunk, "is_final": False},
                            deadline=deadline,
                        )
                    await self._post_json(
                        client,
                        f"{base_url}/tts/session/push",
                        {"session_id": upstream_session_id, "text": "", "is_final": True},
                        deadline=deadline,
                    )
                    await asyncio.shield(reader)
                    reader_completed = True
                    full_text = "".join(text_parts).strip()
                    if not full_text:
                        raise ProviderError("TTS 文本为空。")
                    if LightTTSProvider._wav_duration(temporary) < LightTTSProvider._minimum_duration(full_text):
                        raise ProviderError("MOSS-TTS-Realtime 返回的音频无效或过短。")
                    if await LightTTSProvider._cancel_requested(should_cancel):
                        raise ProviderCancelled("MOSS-TTS-Realtime 运行任务已取消。")
                    try:
                        close_response = await asyncio.wait_for(
                            client.post(
                                f"{base_url}/tts/session/close",
                                json={"session_id": upstream_session_id},
                            ),
                            timeout=settings.moss_tts_realtime_close_timeout_seconds,
                        )
                        close_response.raise_for_status()
                        close_body = close_response.json()
                        if not isinstance(close_body, dict) or close_body.get("ok") is not True or close_body.get("released") is not True:
                            raise ProviderError("MOSS-TTS-Realtime 未确认会话释放。")
                        release_deadline = min(
                            deadline,
                            loop.time() + settings.moss_tts_realtime_close_timeout_seconds,
                        )
                        if not await self._confirm_release(client, base_url, deadline=release_deadline):
                            raise ProviderError("MOSS-TTS-Realtime 释放后仍有活动或孤儿任务。")
                        close_acknowledged = True
                        upstream_release_acknowledged = True
                    except (asyncio.TimeoutError, httpx.HTTPError, ValueError) as exc:
                        raise ProviderError("MOSS-TTS-Realtime 未确认会话释放。") from exc
                    os.replace(temporary, target)
                finally:
                    if session_started and not close_acknowledged:
                        try:
                            if await LightTTSProvider._cancel_requested(should_cancel):
                                abort_response = await asyncio.wait_for(
                                    client.post(
                                        f"{base_url}/tts/session/abort",
                                        json={"session_id": upstream_session_id, "reason": "v2_cancelled"},
                                    ),
                                    timeout=settings.moss_tts_realtime_close_timeout_seconds,
                                )
                                abort_response.raise_for_status()
                                abort_body = abort_response.json()
                                upstream_release_acknowledged = bool(
                                    isinstance(abort_body, dict) and abort_body.get("ok") is True and abort_body.get("released") is True
                                )
                                if upstream_release_acknowledged:
                                    release_deadline = min(
                                        deadline,
                                        loop.time() + settings.moss_tts_realtime_close_timeout_seconds,
                                    )
                                    upstream_release_acknowledged = await self._confirm_release(
                                        client,
                                        base_url,
                                        deadline=release_deadline,
                                    )
                            if upstream_release_acknowledged:
                                close_acknowledged = True
                            else:
                                close_response = await asyncio.wait_for(
                                    client.post(
                                        f"{base_url}/tts/session/close",
                                        json={"session_id": upstream_session_id},
                                    ),
                                    timeout=settings.moss_tts_realtime_close_timeout_seconds,
                                )
                                close_response.raise_for_status()
                                close_body = close_response.json()
                                upstream_release_acknowledged = bool(
                                    isinstance(close_body, dict) and close_body.get("ok") is True and close_body.get("released") is True
                                )
                                if upstream_release_acknowledged:
                                    release_deadline = min(
                                        deadline,
                                        loop.time() + settings.moss_tts_realtime_close_timeout_seconds,
                                    )
                                    upstream_release_acknowledged = await self._confirm_release(
                                        client,
                                        base_url,
                                        deadline=release_deadline,
                                    )
                                close_acknowledged = upstream_release_acknowledged
                        except Exception:
                            logger.warning("MOSS session close acknowledgement failed session=%s", upstream_session_id)
            return f"/media/{room_code}/{target.name}"
        except BaseException:
            if reader_completed:
                try:
                    await on_stream_event(
                        {
                            "type": "audio.stream.aborted",
                            "room_code": room_code,
                            "speech_id": speech_id,
                            "generation": generation,
                            "transport": "livekit" if publish_live and livekit_audio_enabled() else "websocket_pcm",
                        }
                    )
                except Exception:
                    logger.exception("failed to publish MOSS stream abort room=%s speech=%s", room_code, speech_id)
            raise
        finally:
            if reader is not None and not reader.done():
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
            temporary.unlink(missing_ok=True)
            if endpoint_semaphore is not None and (not session_started or upstream_release_acknowledged):
                endpoint_semaphore.release()
            elif endpoint_semaphore is not None:
                logger.error("MOSS endpoint quarantined after missing release ACK speech=%s", speech_id)
            if global_acquired:
                global_semaphore.release()


class MossTTSRealtimeIncrementalSession:
    _END = object()

    def __init__(
        self,
        provider: MossTTSRealtimeProvider,
        *,
        room_code: str,
        speech_id: str,
        voice: str,
        provider_config: dict[str, Any] | None,
        should_cancel: Callable[[], bool | Awaitable[bool]] | None,
        deadline_monotonic: float | None,
        on_stream_event: StreamEventCallback,
        publish_live: bool,
    ) -> None:
        self.provider = provider
        self.room_code = room_code
        self.speech_id = speech_id
        self.voice = voice
        self.provider_config = provider_config
        self.should_cancel = should_cancel
        self.deadline_monotonic = deadline_monotonic
        self.on_stream_event = on_stream_event
        self.publish_live = publish_live
        self._queue: asyncio.Queue[str | object] = asyncio.Queue(maxsize=64)
        self._text_parts: list[str] = []
        self._task: asyncio.Task[str] | None = None
        self._cancelled = asyncio.Event()
        self._ready = asyncio.Event()
        self._closed = False

    async def _cancel_requested(self) -> bool:
        return self._cancelled.is_set() or await LightTTSProvider._cancel_requested(self.should_cancel)

    async def _chunks(self) -> AsyncIterator[str]:
        while True:
            item = await self._queue.get()
            if item is self._END:
                return
            yield str(item)

    def _ensure_started(self) -> asyncio.Task[str]:
        if self._task is None:
            self._task = asyncio.create_task(
                self.provider._synthesize_incremental_job(
                    self._chunks(),
                    self._text_parts,
                    room_code=self.room_code,
                    speech_id=self.speech_id,
                    voice=self.voice,
                    should_cancel=self._cancel_requested,
                    deadline_monotonic=self.deadline_monotonic,
                    on_stream_event=self.on_stream_event,
                    publish_live=self.publish_live,
                    on_session_ready=self._ready.set,
                )
            )
            # The room can be paused/terminated between the first queued text
            # chunk and the caller reaching finish()/abort().  Observe the
            # exception immediately so asyncio does not emit "Task exception
            # was never retrieved"; task.result() still raises the same error
            # to any later push/finish caller.
            self._task.add_done_callback(self._observe_background_completion)
        return self._task

    @staticmethod
    def _observe_background_completion(task: asyncio.Task[str]) -> None:
        if task.cancelled():
            return
        try:
            task.exception()
        except asyncio.CancelledError:
            return

    def _raise_worker_failure(self) -> None:
        if self._task is not None and self._task.done():
            self._task.result()

    async def prepare(self) -> None:
        """Reserve capacity and finish the upstream handshake without text.

        The worker remains blocked on ``_chunks`` after signalling readiness,
        so every later delta still enters one native MOSS context and one
        continuous browser audio track.
        """

        if self._closed:
            raise RealtimeVoiceSessionClosed("MOSS-TTS-Realtime 增量会话已结束。")
        self._raise_worker_failure()
        if self._ready.is_set():
            return
        task = self._ensure_started()
        ready_wait = asyncio.create_task(self._ready.wait())
        try:
            done, _pending = await asyncio.wait(
                {ready_wait, task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if task in done:
                task.result()
                if not self._ready.is_set():
                    raise ProviderError("MOSS-TTS-Realtime 会话未就绪即结束。")
            await ready_wait
            self._raise_worker_failure()
        finally:
            if not ready_wait.done():
                ready_wait.cancel()
                await asyncio.gather(ready_wait, return_exceptions=True)

    async def push_text(self, text: str) -> None:
        # Preserve exact inter-clause whitespace.  The upstream bridge owns one
        # continuous text context, so trimming every delta would merge words at
        # a chunk boundary even though the final transcript remained correct.
        chunk = text
        if not chunk.strip():
            return
        if self._closed:
            raise RealtimeVoiceSessionClosed("MOSS-TTS-Realtime 增量会话已结束。")
        await self.prepare()
        self._raise_worker_failure()
        self._text_parts.append(chunk)
        task = self._ensure_started()
        put = asyncio.create_task(self._queue.put(chunk))
        done, _pending = await asyncio.wait({put, task}, return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            if not put.done():
                put.cancel()
                await asyncio.gather(put, return_exceptions=True)
            task.result()
        else:
            await put

    async def finish(self) -> str:
        if self._closed:
            raise RealtimeVoiceSessionClosed("MOSS-TTS-Realtime 增量会话已结束。")
        self._closed = True
        if not self._text_parts:
            await self.abort(reason="empty_session")
            raise ProviderError("TTS 文本为空。")
        task = self._ensure_started()
        put = asyncio.create_task(self._queue.put(self._END))
        try:
            done, _pending = await asyncio.wait({put, task}, return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                if not put.done():
                    put.cancel()
                    await asyncio.gather(put, return_exceptions=True)
                return task.result()
            await put
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            self._cancelled.set()
            raise

    async def abort(self, reason: str = "") -> None:
        self._closed = True
        self._cancelled.set()
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        if self._task is None:
            return
        if not self._task.done():
            try:
                self._queue.put_nowait(self._END)
            except asyncio.QueueFull:
                pass
        await asyncio.gather(self._task, return_exceptions=True)


class RealtimeTTSProviderRouter:
    def __init__(self, lighttts_provider: LightTTSProvider, moss_provider: MossTTSRealtimeProvider) -> None:
        self.lighttts_provider = lighttts_provider
        self.moss_provider = moss_provider

    def enabled(self) -> bool:
        if settings.realtime_voice_backend == "moss_realtime":
            return bool(settings.moss_tts_realtime_enabled and (settings.moss_tts_realtime_url.strip() or settings.moss_tts_realtime_urls))
        return bool(settings.lighttts_streaming_enabled and settings.lighttts_bistream_enabled)

    def provider_name(self) -> str:
        return "moss_tts_realtime" if settings.realtime_voice_backend == "moss_realtime" else "lighttts"

    def open_incremental_session(self, **kwargs: Any) -> LightTTSIncrementalSession | MossTTSRealtimeIncrementalSession:
        if settings.realtime_voice_backend == "moss_realtime":
            return self.moss_provider.open_incremental_session(**kwargs)
        return self.lighttts_provider.open_incremental_session(**kwargs)


class JudgeProvider:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.transport = transport

    async def judge(
        self,
        topic: str,
        speeches: list[dict[str, Any]],
        *,
        profile: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if settings.agent_mock:
            return self._mock_score(speeches)
        profile = profile or {}
        endpoint = str(profile.get("endpoint") or settings.judge_api_url).strip()
        if not endpoint:
            raise ProviderError("未配置 AI 裁判服务。")
        model_name = str(profile.get("model_name") or "").strip()
        system_prompt = str(profile.get("system_prompt") or "").strip()
        try:
            timeout_seconds = max(10, min(300, int(profile.get("timeout_seconds") or 120)))
        except (TypeError, ValueError):
            timeout_seconds = 120
        payload = {
            "task_type": "judge",
            "debate_topic": topic,
            "debate_history": speeches,
            "output": {"stream": False, "language": "zh-CN"},
        }
        if model_name:
            payload["model_name"] = model_name
        if system_prompt:
            payload["system_prompt"] = system_prompt
        try:
            timeout = httpx.Timeout(10, read=timeout_seconds, write=30, pool=10)
            async with httpx.AsyncClient(timeout=timeout, transport=self.transport) as client:
                response = await client.post(endpoint, json=payload)
                response.raise_for_status()
                data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError(f"AI 裁判调用失败：{exc}") from exc
        return self._normalize_score(data)

    def _mock_score(self, speeches: list[dict[str, Any]]) -> dict[str, Any]:
        aff = sum(len(item.get("content", "")) for item in speeches if item.get("seat_key", "").startswith("aff"))
        neg = sum(len(item.get("content", "")) for item in speeches if item.get("seat_key", "").startswith("neg"))
        total = max(1, aff + neg)
        aff_score = round(70 + 20 * aff / total, 2)
        neg_score = round(70 + 20 * neg / total, 2)
        winner = "draw" if abs(aff_score - neg_score) < 0.5 else ("aff" if aff_score > neg_score else "neg")
        return {
            "winner": winner,
            "affirmative_score": aff_score,
            "negative_score": neg_score,
            "individual_scores": {},
            "reasoning": "测试裁判依据双方有效发言完整度给出结果。",
        }

    def _normalize_score(self, raw: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise ProviderError("AI 裁判返回格式无效。")
        result = raw.get("output") if isinstance(raw.get("output"), dict) else raw
        winner_raw = str(result.get("winner", "")).lower()
        winner_map = {
            "aff": "aff",
            "affirmative": "aff",
            "正方": "aff",
            "neg": "neg",
            "negative": "neg",
            "反方": "neg",
            "draw": "draw",
            "tie": "draw",
            "平局": "draw",
        }
        winner = winner_map.get(winner_raw)
        if not winner:
            raise ProviderError("AI 裁判返回了无法识别的胜方。")

        def score(*keys: str) -> float:
            value: Any = None
            for key in keys:
                if key in result:
                    value = result[key]
                    break
            if value is None:
                raise ProviderError("AI 裁判缺少队伍分数。")
            try:
                number = float(value)
            except (TypeError, ValueError) as exc:
                raise ProviderError("AI 裁判返回了无效分数。") from exc
            if not math.isfinite(number) or not 0 <= number <= 100:
                raise ProviderError("AI 裁判分数必须在 0–100 之间。")
            return round(number, 2)

        individual_scores = result.get("individual_scores", {})
        if not isinstance(individual_scores, dict) or len(individual_scores) > 100:
            raise ProviderError("AI 裁判个人评分格式无效。")
        if len(json.dumps(individual_scores, ensure_ascii=False, default=str)) > 100_000:
            raise ProviderError("AI 裁判个人评分内容过大。")
        reasoning = str(result.get("reasoning", result.get("reason", ""))).strip()
        if not reasoning:
            raise ProviderError("AI 裁判未返回判定理由。")
        if len(reasoning) > 10_000:
            raise ProviderError("AI 裁判判定理由过长。")
        return {
            "winner": winner,
            "affirmative_score": score("affirmative_score", "aff_score"),
            "negative_score": score("negative_score", "neg_score"),
            "individual_scores": individual_scores,
            "reasoning": reasoning,
        }


def agent_payload(
    *,
    topic: str,
    debater_name: str,
    seat_key: str,
    current_stage: str,
    next_stage: str,
    history: list[dict[str, Any]],
    max_token: int,
    model_name: str | None = None,
    match_id: str | None = None,
    room_code: str | None = None,
    task_id: str | None = None,
    agent_profile: str | None = None,
) -> dict[str, Any]:
    side, position = seat_key.split("_", 1)
    result = {
        "model_name": model_name or settings.agent_model_name,
        "debater_name": debater_name,
        "debate_position": f"{position}辩",
        "debate_topic": topic,
        "current_stage": current_stage,
        "next_stage": next_stage,
        "holder": "正方" if side == "aff" else "反方",
        "debate_history": history,
        "task_type": "debate",
        "max_token": max_token,
        "output": {"stream": True, "language": "zh-CN"},
    }
    if match_id:
        result["match_id"] = match_id
    if room_code:
        result["room_code"] = room_code
    if task_id:
        result["task_id"] = task_id
    if agent_profile:
        result["agent_profile"] = agent_profile
    return result


debate_agent = DebateAgentProvider()
lighttts = LightTTSProvider()
moss_tts_realtime = MossTTSRealtimeProvider()
realtime_tts = RealtimeTTSProviderRouter(lighttts, moss_tts_realtime)
judge_provider = JudgeProvider()
