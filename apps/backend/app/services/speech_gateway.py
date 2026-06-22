from __future__ import annotations

import asyncio
import base64
import inspect
import io
import json
import os
import re
import time
import uuid
import wave
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Dict, Optional
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl

import httpx
import websockets

from app.services.integration_config import integration_config
from app.services.xfyun_gateway import ASRResult, TTSResult, XfyunGatewayError, XfyunTTSGateway
from app.services.xfyun_rtasr import select_asr_gateway as select_xfyun_asr_gateway


class SpeechGatewayError(Exception):
    def __init__(self, message: str, code: Optional[Any] = None, provider: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.provider = provider


@dataclass(frozen=True)
class SpeechGatewaySelection:
    gateway: Any
    provider: str
    options: Dict[str, Any]
    preset: Optional[Dict[str, Any]] = None


ConnectFactory = Callable[..., Any]
ALICLOUD_ASR_AUDIO_FRAME_BYTES = 96 * 1024
ALICLOUD_ASR_AUDIO_SEND_RATE_BYTES_PER_SEC = 512 * 1024
ALICLOUD_ASR_MAX_AUDIO_SEND_RATE_BYTES_PER_SEC = 1536 * 1024
FUNASR_DEFAULT_FRAME_BYTES = 1920


_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)]\(([^)]+)\)")
_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_UPPER_ABBR_RE = re.compile(r"\b[A-Z]{2,8}\b")
_COMMON_TTS_TERMS = (
    (re.compile(r"\bQwen[-\s]?TTS\b", re.IGNORECASE), "千问语音合成"),
    (re.compile(r"\bQwen[-\s]?ASR\b", re.IGNORECASE), "千问语音识别"),
    (re.compile(r"\bQwen\b", re.IGNORECASE), "千问"),
    (re.compile(r"\bDashScope\b", re.IGNORECASE), "百炼"),
    (re.compile(r"\bModel\s*Studio\b", re.IGNORECASE), "百炼模型服务"),
    (re.compile(r"\bAPI\s*Key\b", re.IGNORECASE), "接口密钥"),
    (re.compile(r"\bTTS\b", re.IGNORECASE), "语音合成"),
    (re.compile(r"\bASR\b", re.IGNORECASE), "语音识别"),
    (re.compile(r"\bAI\b", re.IGNORECASE), "人工智能"),
    (re.compile(r"\bAgent\b", re.IGNORECASE), "智能体"),
    (re.compile(r"\bWebSocket\b", re.IGNORECASE), "网络连接"),
)


def normalize_tts_text(text: str) -> str:
    """Clean model text before TTS so symbols and markdown do not get spoken aloud."""
    content = str(text or "").strip()
    if not content:
        return ""
    content = _CODE_FENCE_RE.sub(" ", content)
    content = _HTML_TAG_RE.sub(" ", content)
    content = _MARKDOWN_LINK_RE.sub(r"\1", content)
    content = _INLINE_CODE_RE.sub(r"\1", content)
    content = _URL_RE.sub("链接", content)
    content = _EMAIL_RE.sub("邮箱地址", content)
    for pattern, replacement in _COMMON_TTS_TERMS:
        content = pattern.sub(replacement, content)
    content = content.replace("\r", "\n")
    content = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", content)
    content = re.sub(r"(?m)^\s{0,3}>\s*", "", content)
    content = re.sub(r"(?m)^\s*(?:[-*+]|[0-9]+[.)、])\s+", "", content)
    content = re.sub(r"[*_~`#|]+", "", content)
    content = re.sub(r"[\"“”‘’]+", "", content)
    content = re.sub(r"[<>{}\[\]]+", "", content)
    content = re.sub(r"[=]{2,}", "，", content)
    content = re.sub(r"[-—–]{2,}", "，", content)
    content = re.sub(r"\.{3,}|…{1,}", "。", content)
    content = re.sub(r"[!！]{2,}", "！", content)
    content = re.sub(r"[?？]{2,}", "？", content)
    content = re.sub(r"[,，]{2,}", "，", content)
    content = re.sub(r"[;；]{2,}", "；", content)
    content = re.sub(r"[、/\\]+", "、", content)
    content = _UPPER_ABBR_RE.sub(lambda m: " ".join(m.group(0)), content)
    content = re.sub(r"[\U00010000-\U0010ffff]", "", content)
    content = re.sub(r"\s*\n+\s*", "，", content)
    content = re.sub(r"\s+", " ", content)
    content = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", content)
    content = re.sub(r"\s+([，。！？；：、])", r"\1", content)
    content = re.sub(r"([，。！？；：、])\s+", r"\1", content)
    content = re.sub(r"[，、；：]+([。！？])", r"\1", content)
    content = re.sub(r"^[，。！？；：、\s]+", "", content)
    return content.strip()


def select_asr_gateway(connect: Optional[ConnectFactory] = None) -> SpeechGatewaySelection:
    section = integration_config.active_section("asr")
    provider = str(section.get("provider") or "alicloud")
    if not section.get("enabled"):
        raise SpeechGatewayError("ASR 未启用。", code="asr_disabled", provider=provider)
    if provider == "xfyun":
        url = str(section.get("endpoint") or "").strip()
        if not url:
            raise SpeechGatewayError("讯飞 ASR 缺少 WebSocket 地址。", code="missing_config", provider=provider)
        return SpeechGatewaySelection(
            gateway=select_xfyun_asr_gateway(url, connect=connect),
            provider=provider,
            options={"audio_format": "audio/L16;rate=16000", "encoding": "raw", "lang": section.get("lang") or "autodialect"},
        )
    if provider == "local_qwen":
        return SpeechGatewaySelection(
            gateway=LocalQwenASRGateway(section=section),
            provider="local_qwen",
            options={"audio_format": "audio/L16;rate=16000", "encoding": "raw", "lang": section.get("lang") or "zh"},
        )
    if provider == "funasr":
        settings = dict(section.get("settings") or {})
        return SpeechGatewaySelection(
            gateway=FunASRASRGateway(section=section, connect=connect),
            provider="funasr",
            options={
                "audio_format": "audio/L16;rate=16000",
                "encoding": "raw",
                "lang": settings.get("language") or section.get("lang") or "中文",
                "realtime_pacing": True,
            },
        )
    return SpeechGatewaySelection(
        gateway=AlicloudASRGateway(section=section, connect=connect),
        provider="alicloud",
        options={},
    )


def select_tts_gateway(
    *,
    voice_preset_id: str = "",
    speaker: Optional[Dict[str, Any]] = None,
    connect: Optional[ConnectFactory] = None,
) -> SpeechGatewaySelection:
    section = integration_config.active_section("tts")
    provider = str(section.get("provider") or "alicloud")
    if not section.get("enabled"):
        raise SpeechGatewayError("TTS 未启用。", code="tts_disabled", provider=provider)
    if provider == "xfyun":
        url = str(section.get("endpoint") or "").strip()
        if not url:
            raise SpeechGatewayError("讯飞 TTS 缺少 WebSocket 地址。", code="missing_config", provider=provider)
        preset = _resolve_voice_preset(voice_preset_id, speaker, provider)
        options = {"voice": (preset or {}).get("voice") or section.get("voice") or "x6_lingfeiyi_pro"}
        return SpeechGatewaySelection(gateway=XfyunTTSGateway(url=url, connect=connect), provider=provider, options=options, preset=preset)

    preset = _resolve_voice_preset(voice_preset_id, speaker, provider)
    if provider == "local_qwen":
        return SpeechGatewaySelection(
            gateway=LocalQwenTTSGateway(section=section, preset=preset),
            provider="local_qwen",
            options={},
            preset=preset,
        )
    return SpeechGatewaySelection(
        gateway=AlicloudTTSGateway(section=section, preset=preset, connect=connect),
        provider="alicloud",
        options={},
        preset=preset,
    )


def _resolve_voice_preset(voice_preset_id: str, speaker: Optional[Dict[str, Any]], provider: str) -> Optional[Dict[str, Any]]:
    candidate_id = str(voice_preset_id or (speaker or {}).get("tts_voice_preset_id") or "").strip()
    if candidate_id:
        preset = integration_config.voice_preset(candidate_id)
        if preset and preset.get("enabled") and preset.get("provider") == provider:
            return preset
    return integration_config.default_voice_preset(provider)


def _prepare_local_qwen_asr_audio(audio: bytes, options: Dict[str, Any]) -> tuple[str, bytes, str]:
    audio_format = str(options.get("audio_format") or "audio/L16;rate=16000")
    encoding = str(options.get("encoding") or "").lower()
    normalized = audio_format.lower()
    if "l16" in normalized or "pcm" in normalized or encoding == "raw":
        sample_rate = _pcm_sample_rate(audio_format)
        return ("audio.wav", _pcm16le_to_wav(audio, sample_rate=sample_rate), "audio/wav")
    if "webm" in normalized:
        return ("audio.webm", audio, audio_format)
    if "mpeg" in normalized or "mp3" in normalized:
        return ("audio.mp3", audio, audio_format)
    if "wav" in normalized or "wave" in normalized:
        return ("audio.wav", audio, audio_format)
    return ("audio.bin", audio, audio_format or "application/octet-stream")


def _pcm_sample_rate(audio_format: str) -> int:
    match = re.search(r"(?:rate|sample_rate)=(\d+)", audio_format, re.IGNORECASE)
    if not match:
        return 16000
    try:
        value = int(match.group(1))
    except ValueError:
        return 16000
    return value if 8000 <= value <= 192000 else 16000


def _pcm16le_to_wav(audio: bytes, *, sample_rate: int) -> bytes:
    if len(audio) % 2:
        audio = audio[:-1]
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(audio)
    return buffer.getvalue()


class LocalQwenTTSGateway:
    def __init__(self, section: Dict[str, Any], preset: Optional[Dict[str, Any]] = None) -> None:
        self.section = section
        self.preset = dict(preset or {})

    async def synthesize(self, text: str, **options: Any) -> TTSResult:
        audio_parts = []
        mime_type = self.stream_mime_type(**options)
        latency_ms = 0
        chunk_count = 0
        async for event in self.synthesize_stream(text, **options):
            if event["type"] == "chunk":
                audio_parts.append(event["audio"])
                chunk_count += 1
            elif event["type"] == "done":
                mime_type = event["mime_type"]
                latency_ms = event["latency_ms"]
                chunk_count = event["chunk_count"]
        return TTSResult(audio=b"".join(audio_parts), mime_type=mime_type, latency_ms=latency_ms, chunk_count=chunk_count)

    def stream_mime_type(self, **options: Any) -> str:
        session = self._session_options(options)
        return _tts_mime_type(session["response_format"])

    async def synthesize_stream(self, text: str, **options: Any) -> AsyncIterator[Dict[str, Any]]:
        content = normalize_tts_text(text)
        if not content:
            raise SpeechGatewayError("TTS 合成文本不能为空。", code="empty_text", provider="local_qwen")
        session = self._session_options(options)
        endpoint = _join_http_url(str(self.section.get("endpoint") or ""), "/v1/audio/speech")
        base_payload = {
            "model": session["model"],
            "input": content,
            "voice": session["voice"],
            "response_format": session["response_format"],
            "sample_rate": session["sample_rate"],
            "speed": session["speech_rate"],
        }
        payloads = _local_qwen_tts_payload_variants(base_payload, session)
        started = time.perf_counter()
        last_error: Optional[SpeechGatewayError] = None
        for payload_idx, payload in enumerate(payloads):
            chunk_count = 0
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=8.0)) as client:
                    async with client.stream("POST", endpoint, json=payload) as response:
                        if response.status_code >= 400:
                            detail = await response.aread()
                            last_error = SpeechGatewayError(
                                f"本地 Qwen TTS 返回错误：{response.status_code} {detail[:200].decode('utf-8', 'ignore')}",
                                code=response.status_code,
                                provider="local_qwen",
                            )
                            if payload_idx == 0 and len(payloads) > 1:
                                continue
                            raise last_error
                        async for chunk in response.aiter_bytes():
                            if not chunk:
                                continue
                            chunk_count += 1
                            yield {"type": "chunk", "audio": chunk, "index": chunk_count}
                yield {
                    "type": "done",
                    "mime_type": self.stream_mime_type(**options),
                    "latency_ms": int((time.perf_counter() - started) * 1000),
                    "chunk_count": chunk_count,
                }
                return
            except SpeechGatewayError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise SpeechGatewayError(f"本地 Qwen TTS 调用失败：{exc}", provider="local_qwen") from exc
        if last_error:
            raise last_error
        yield {
            "type": "done",
            "mime_type": self.stream_mime_type(**options),
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "chunk_count": 0,
        }

    def _session_options(self, options: Dict[str, Any]) -> Dict[str, Any]:
        settings = dict(self.section.get("settings") or {})
        preset = dict(self.preset or {})
        voice = options.get("voice") or preset.get("voice") or self.section.get("voice") or "dylan"
        session = {
            "voice": _local_qwen_voice(str(voice)),
            "response_format": options.get("response_format") or preset.get("response_format") or settings.get("response_format") or "mp3",
            "sample_rate": int(options.get("sample_rate") or preset.get("sample_rate") or settings.get("sample_rate") or 24000),
            "model": options.get("model") or preset.get("model") or settings.get("model") or "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
            "speech_rate": float(options.get("speech_rate") or preset.get("speech_rate") or settings.get("speech_rate") or 1.0),
        }
        for key in ("seed", "temperature", "top_p", "volume", "pitch_rate", "instructions"):
            value = options.get(key)
            if value is None or value == "":
                value = preset.get(key)
            if value is None or value == "":
                value = settings.get(key)
            if value is not None and value != "":
                session[key] = value
        return session


class LocalQwenASRGateway:
    def __init__(self, section: Dict[str, Any]) -> None:
        self.section = section

    async def recognize(self, audio: bytes, **options: Any) -> ASRResult:
        if not audio:
            raise SpeechGatewayError("ASR 识别音频不能为空。", code="empty_audio", provider="local_qwen")
        settings = dict(self.section.get("settings") or {})
        endpoint = _join_http_url(str(self.section.get("endpoint") or ""), "/v1/audio/transcriptions")
        model = str(options.get("model") or settings.get("model") or "Qwen/Qwen3-ASR-1.7B")
        language = str(options.get("lang") or options.get("language") or settings.get("language") or self.section.get("lang") or "zh")
        filename, payload, mime_type = _prepare_local_qwen_asr_audio(audio, options)
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=8.0)) as client:
                response = await client.post(
                    endpoint,
                    data={"model": model, "language": language, "response_format": "json"},
                    files={"file": (filename, payload, mime_type)},
                )
            if response.status_code >= 400:
                raise SpeechGatewayError(
                    f"本地 Qwen ASR 返回错误：{response.status_code} {response.text[:200]}",
                    code=response.status_code,
                    provider="local_qwen",
                )
            text = _extract_local_qwen_asr_text(response)
        except SpeechGatewayError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise SpeechGatewayError(f"本地 Qwen ASR 调用失败：{exc}", provider="local_qwen") from exc
        return ASRResult(text=text, latency_ms=int((time.perf_counter() - started) * 1000), chunk_count=1)

    async def open_stream(
        self,
        *,
        on_partial: Optional[Any] = None,
        on_final: Optional[Any] = None,
        on_error: Optional[Any] = None,
        **options: Any,
    ) -> "LocalQwenASRStreamSession":
        return LocalQwenASRStreamSession(self, on_partial=on_partial, on_final=on_final, on_error=on_error, options=options)


class LocalQwenASRStreamSession:
    def __init__(
        self,
        gateway: LocalQwenASRGateway,
        *,
        on_partial: Optional[Any] = None,
        on_final: Optional[Any] = None,
        on_error: Optional[Any] = None,
        options: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.gateway = gateway
        self.on_partial = on_partial
        self.on_final = on_final
        self.on_error = on_error
        self.options = dict(options or {})
        self.parts: list[bytes] = []
        self.closed = False

    async def send_audio(self, audio: bytes) -> None:
        if self.closed:
            raise SpeechGatewayError("ASR 流式会话已经结束。", code="stream_closed", provider="local_qwen")
        if audio:
            self.parts.append(bytes(audio))

    async def finish(self) -> ASRResult:
        self.closed = True
        try:
            result = await self.gateway.recognize(b"".join(self.parts), **self.options)
            await _call_callback(self.on_final, result.text, result.latency_ms, len(self.parts))
            return ASRResult(text=result.text, latency_ms=result.latency_ms, chunk_count=len(self.parts))
        except Exception as exc:
            await _call_callback(self.on_error, exc)
            raise

    async def close(self) -> None:
        self.closed = True


class FunASRASRGateway:
    def __init__(self, section: Dict[str, Any], connect: Optional[ConnectFactory] = None) -> None:
        self.section = section
        self.connect = connect or websockets.connect

    async def recognize(self, audio: bytes, **options: Any) -> ASRResult:
        if not audio:
            raise SpeechGatewayError("ASR 识别音频不能为空。", code="empty_audio", provider="funasr")
        settings = dict(self.section.get("settings") or {})
        archive_timeout = options.get("archive_final_timeout") or settings.get("archive_final_timeout") or 90
        session = await self.open_stream(**{**options, "realtime_pacing": False, "final_timeout": archive_timeout})
        await session.send_audio(audio)
        return await session.finish()

    async def open_stream(
        self,
        *,
        on_partial: Optional[Any] = None,
        on_final: Optional[Any] = None,
        on_error: Optional[Any] = None,
        **options: Any,
    ) -> "FunASRASRStreamSession":
        endpoint = str(self.section.get("endpoint") or "").strip()
        if not endpoint:
            raise SpeechGatewayError("FunASR 缺少 WebSocket 地址。", code="missing_config", provider="funasr")
        session = FunASRASRStreamSession(
            section=self.section,
            endpoint=endpoint,
            connect=self.connect,
            on_partial=on_partial,
            on_final=on_final,
            on_error=on_error,
            options=options,
        )
        await session.start()
        return session


class FunASRASRStreamSession:
    def __init__(
        self,
        *,
        section: Dict[str, Any],
        endpoint: str,
        connect: ConnectFactory,
        on_partial: Optional[Any] = None,
        on_final: Optional[Any] = None,
        on_error: Optional[Any] = None,
        options: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.section = section
        self.endpoint = endpoint
        self.connect = connect
        self.on_partial = on_partial
        self.on_final = on_final
        self.on_error = on_error
        self.options = dict(options or {})
        self.settings = dict(section.get("settings") or {})
        self.started = time.perf_counter()
        self.chunk_count = 0
        self._audio: "asyncio.Queue[Optional[bytes]]" = asyncio.Queue()
        self._task: Optional[asyncio.Task[ASRResult]] = None
        self._closed = False
        self._sender_finished = False
        self._final_emitted = False
        self._queued_audio_bytes = 0
        self._committed_text = ""
        self._online_text = ""
        self._best_text = ""

    async def start(self) -> None:
        if not self._task:
            self._task = asyncio.create_task(self._run())

    async def send_audio(self, audio: bytes) -> None:
        if self._closed:
            raise SpeechGatewayError("ASR 流式会话已经结束。", code="stream_closed", provider="funasr")
        if not audio:
            return
        frame_bytes = self._frame_bytes()
        self._queued_audio_bytes += len(audio)
        for offset in range(0, len(audio), frame_bytes):
            frame = audio[offset : offset + frame_bytes]
            if frame:
                await self._audio.put(frame)

    async def finish(self) -> ASRResult:
        if not self._closed:
            self._closed = True
            await self._audio.put(None)
        if not self._task:
            raise SpeechGatewayError("ASR 流式会话尚未启动。", code="stream_not_started", provider="funasr")
        timeout = self._final_timeout_seconds()
        if self._realtime_pacing_enabled():
            timeout += self._queued_audio_bytes / max(1.0, self._bytes_per_second()) + 1.0
        try:
            return await asyncio.wait_for(self._task, timeout=timeout)
        except asyncio.TimeoutError as exc:
            if self._best_text:
                if self._task and not self._task.done():
                    self._task.cancel()
                latency_ms = int((time.perf_counter() - self.started) * 1000)
                await self._emit_final_once(self._best_text, latency_ms)
                return ASRResult(text=self._best_text, latency_ms=latency_ms, chunk_count=self.chunk_count)
            error = SpeechGatewayError("FunASR final 响应超时。", code="final_timeout", provider="funasr")
            await _call_callback(self.on_error, error)
            if self._task and not self._task.done():
                self._task.cancel()
            raise error from exc

    async def close(self) -> None:
        self._closed = True
        if self._task and not self._task.done():
            self._task.cancel()

    async def _run(self) -> ASRResult:
        try:
            async with await _ws_connect_funasr(self.connect, self.endpoint, open_timeout=self._open_timeout_seconds()) as websocket:
                await websocket.send("START")
                language = self._language()
                if language:
                    await websocket.send(f"LANGUAGE:{language}")
                hotwords = self._hotwords()
                if hotwords:
                    await websocket.send(f"HOTWORDS:{hotwords}")
                sender = asyncio.create_task(self._send_frames(websocket))
                try:
                    await self._receive_messages(websocket)
                finally:
                    if not sender.done():
                        sender.cancel()
        except SpeechGatewayError as exc:
            await _call_callback(self.on_error, exc)
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            error = SpeechGatewayError(f"FunASR 调用失败：{exc}", provider="funasr")
            await _call_callback(self.on_error, error)
            raise error from exc
        latency_ms = int((time.perf_counter() - self.started) * 1000)
        text = self._best_text
        if not text:
            error = SpeechGatewayError("FunASR 未返回转写文本。", code="empty_result", provider="funasr")
            await _call_callback(self.on_error, error)
            raise error
        await self._emit_final_once(text, latency_ms)
        return ASRResult(text=text, latency_ms=latency_ms, chunk_count=self.chunk_count)

    async def _send_frames(self, websocket: Any) -> None:
        frame_delay = self._frame_delay_seconds()
        while True:
            chunk = await self._audio.get()
            if chunk is None:
                self._sender_finished = True
                await websocket.send("STOP")
                return
            await websocket.send(chunk)
            self.chunk_count += 1
            if self._realtime_pacing_enabled() and frame_delay > 0:
                await asyncio.sleep(frame_delay)

    async def _receive_messages(self, websocket: Any) -> None:
        while True:
            timeout = self._end_wait_timeout_seconds() if self._sender_finished else None
            try:
                if timeout is None:
                    raw = await websocket.recv()
                else:
                    raw = await asyncio.wait_for(websocket.recv(), timeout=timeout)
            except asyncio.TimeoutError:
                if self._sender_finished:
                    return
                continue
            except Exception:
                if self._sender_finished and self._best_text:
                    return
                raise
            message = json.loads(raw.decode("utf-8", "ignore") if isinstance(raw, bytes) else raw)
            done = await self._handle_message(message)
            if done:
                return

    async def _handle_message(self, message: Dict[str, Any]) -> bool:
        event = str(message.get("event") or "")
        if event == "error" or message.get("error"):
            raise SpeechGatewayError(str(message.get("message") or message.get("error") or "FunASR 返回错误。"), provider="funasr")
        if event in {"started", "language_set", "hotwords_set"}:
            return False
        if event == "stopped":
            return bool(self._sender_finished)
        sentences = message.get("sentences")
        if isinstance(sentences, list):
            self._committed_text = "".join(
                str(item.get("text") or "") for item in sentences if isinstance(item, dict)
            )
            self._online_text = str(message.get("partial") or "")
        else:
            text = str(message.get("text") or "")
            if text:
                self._online_text = text
        current = (self._committed_text + self._online_text).strip()
        if current:
            self._best_text = current
            latency_ms = int((time.perf_counter() - self.started) * 1000)
            await _call_callback(self.on_partial, current, latency_ms, self.chunk_count)
        return bool(message.get("is_final"))

    async def _emit_final_once(self, text: str, latency_ms: int) -> None:
        if self._final_emitted:
            return
        self._final_emitted = True
        await _call_callback(self.on_final, text, latency_ms, self.chunk_count)

    def _language(self) -> str:
        return str(self.options.get("lang") or self.options.get("language") or self.settings.get("language") or self.section.get("lang") or "中文").strip()

    def _hotwords(self) -> str:
        value = self.options.get("hotwords", self.settings.get("hotwords", ""))
        if isinstance(value, (list, tuple)):
            return ",".join(str(item).strip() for item in value if str(item).strip())
        return str(value or "").strip()

    def _chunk_size(self) -> list[int]:
        raw = self.options.get("chunk_size", self.settings.get("chunk_size", [5, 10, 5]))
        if isinstance(raw, str):
            parts = [part.strip() for part in raw.split(",")]
        elif isinstance(raw, (list, tuple)):
            parts = list(raw)
        else:
            parts = [5, 10, 5]
        values: list[int] = []
        for item in parts[:3]:
            try:
                values.append(int(item))
            except (TypeError, ValueError):
                values.append([5, 10, 5][len(values)])
        while len(values) < 3:
            values.append([5, 10, 5][len(values)])
        return [max(1, value) for value in values[:3]]

    def _sample_rate(self) -> int:
        return int(self.options.get("sample_rate") or self.settings.get("sample_rate") or _pcm_sample_rate(str(self.options.get("audio_format") or "")))

    def _chunk_interval(self) -> int:
        try:
            return max(1, int(self.options.get("chunk_interval") or self.settings.get("chunk_interval") or 10))
        except (TypeError, ValueError):
            return 10

    def _frame_bytes(self) -> int:
        try:
            frame_ms = float(self.options.get("frame_ms") or self.settings.get("frame_ms") or 100)
        except (TypeError, ValueError):
            frame_ms = 100
        frame_ms = max(20.0, min(500.0, frame_ms))
        value = int(frame_ms / 1000 * self._sample_rate() * 2)
        return max(320, value or FUNASR_DEFAULT_FRAME_BYTES)

    def _frame_delay_seconds(self) -> float:
        return self._frame_bytes() / self._bytes_per_second()

    def _bytes_per_second(self) -> float:
        return float(max(1, self._sample_rate() * 2))

    def _realtime_pacing_enabled(self) -> bool:
        raw = self.options.get("realtime_pacing", self.settings.get("realtime_pacing", True))
        if isinstance(raw, str):
            return raw.strip().lower() not in {"0", "false", "no", "off"}
        return bool(raw)

    def _open_timeout_seconds(self) -> float:
        try:
            return max(1.0, float(self.options.get("open_timeout") or self.settings.get("open_timeout") or 8.0))
        except (TypeError, ValueError):
            return 8.0

    def _final_timeout_seconds(self) -> float:
        try:
            return max(1.0, float(self.options.get("final_timeout") or self.settings.get("final_timeout") or 8.0))
        except (TypeError, ValueError):
            return 8.0

    def _end_wait_timeout_seconds(self) -> float:
        try:
            return max(0.2, float(self.options.get("end_wait_timeout") or self.settings.get("end_wait_timeout") or 1.2))
        except (TypeError, ValueError):
            return 1.2


class AlicloudTTSGateway:
    def __init__(self, section: Dict[str, Any], preset: Optional[Dict[str, Any]] = None, connect: Optional[ConnectFactory] = None) -> None:
        self.section = section
        self.preset = dict(preset or {})
        self.connect = connect or websockets.connect

    async def synthesize(self, text: str, **options: Any) -> TTSResult:
        audio_parts = []
        mime_type = "audio/mpeg"
        latency_ms = 0
        chunk_count = 0
        async for event in self.synthesize_stream(text, **options):
            if event["type"] == "chunk":
                audio_parts.append(event["audio"])
                chunk_count += 1
            elif event["type"] == "done":
                mime_type = event["mime_type"]
                latency_ms = event["latency_ms"]
                chunk_count = event["chunk_count"]
        return TTSResult(audio=b"".join(audio_parts), mime_type=mime_type, latency_ms=latency_ms, chunk_count=chunk_count)

    def stream_mime_type(self, **options: Any) -> str:
        session = self._session_options(options)
        return _tts_mime_type(session["response_format"])

    async def synthesize_stream(self, text: str, **options: Any) -> AsyncIterator[Dict[str, Any]]:
        content = normalize_tts_text(text)
        if not content:
            raise SpeechGatewayError("TTS 合成文本不能为空。", code="empty_text", provider="alicloud")
        api_key = _alicloud_api_key(self.section)
        if not api_key:
            raise SpeechGatewayError("阿里云 TTS 缺少 DashScope API Key。", code="missing_api_key", provider="alicloud")

        session = self._session_options(options)
        endpoint = _endpoint_with_model(str(self.section.get("endpoint") or ""), session["model"])
        headers = _alicloud_headers(api_key, self.section)
        started = time.perf_counter()
        chunk_count = 0
        mime_type = _tts_mime_type(session["response_format"])
        try:
            async with await _ws_connect(self.connect, endpoint, headers, open_timeout=8, close_timeout=3) as websocket:
                await websocket.send(json.dumps({"event_id": _event_id(), "type": "session.update", "session": session}, ensure_ascii=False))
                await websocket.send(json.dumps({"event_id": _event_id(), "type": "input_text_buffer.append", "text": content}, ensure_ascii=False))
                await websocket.send(json.dumps({"event_id": _event_id(), "type": "input_text_buffer.commit"}, ensure_ascii=False))
                await websocket.send(json.dumps({"event_id": _event_id(), "type": "session.finish"}, ensure_ascii=False))
                async for raw in websocket:
                    if isinstance(raw, bytes):
                        chunk_count += 1
                        yield {"type": "chunk", "audio": raw, "index": chunk_count}
                        continue
                    message = json.loads(raw)
                    event_type = str(message.get("type") or "")
                    if event_type == "error":
                        error = message.get("error") or message
                        raise SpeechGatewayError(str(error.get("message") or "阿里云 TTS 返回错误。"), code=error.get("code"), provider="alicloud")
                    if event_type == "response.audio.delta":
                        delta = message.get("delta") or message.get("audio")
                        if delta:
                            chunk_count += 1
                            yield {"type": "chunk", "audio": base64.b64decode(delta), "index": chunk_count}
                    if event_type == "session.finished":
                        break
        except SpeechGatewayError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise SpeechGatewayError(f"阿里云 TTS 调用失败：{exc}", provider="alicloud") from exc
        yield {
            "type": "done",
            "mime_type": mime_type,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "chunk_count": chunk_count,
        }

    def _session_options(self, options: Dict[str, Any]) -> Dict[str, Any]:
        settings = dict(self.section.get("settings") or {})
        preset = dict(self.preset or {})
        session = {
            "voice": options.get("voice") or preset.get("voice") or self.section.get("voice") or "Ethan",
            "mode": options.get("mode") or preset.get("mode") or settings.get("mode") or "server_commit",
            "language_type": options.get("language_type") or preset.get("language_type") or settings.get("language_type") or "Chinese",
            "response_format": options.get("response_format") or preset.get("response_format") or settings.get("response_format") or "mp3",
            "sample_rate": int(options.get("sample_rate") or preset.get("sample_rate") or settings.get("sample_rate") or 24000),
            "model": options.get("model") or preset.get("model") or settings.get("model") or "qwen3-tts-flash-realtime",
        }
        for key in ("speech_rate", "volume", "pitch_rate"):
            value = options.get(key, preset.get(key, settings.get(key)))
            if value not in {None, ""}:
                session[key] = value
        if "instruct" in str(session["model"]).lower():
            for key in ("instructions", "optimize_instructions"):
                value = options.get(key, preset.get(key, settings.get(key)))
                if value not in {None, ""}:
                    session[key] = value
        return session


class AlicloudASRGateway:
    def __init__(self, section: Dict[str, Any], connect: Optional[ConnectFactory] = None) -> None:
        self.section = section
        self.connect = connect or websockets.connect

    async def recognize(self, audio: bytes, **options: Any) -> ASRResult:
        session = await self.open_stream(**options)
        await session.send_audio(audio)
        return await session.finish()

    async def open_stream(
        self,
        *,
        on_partial: Optional[Any] = None,
        on_final: Optional[Any] = None,
        on_error: Optional[Any] = None,
        **options: Any,
    ) -> "AlicloudASRStreamSession":
        api_key = _alicloud_api_key(self.section)
        if not api_key:
            raise SpeechGatewayError("阿里云 ASR 缺少 DashScope API Key。", code="missing_api_key", provider="alicloud")
        session = AlicloudASRStreamSession(
            section=self.section,
            api_key=api_key,
            connect=self.connect,
            on_partial=on_partial,
            on_final=on_final,
            on_error=on_error,
            options=options,
        )
        await session.start()
        return session


class AlicloudASRStreamSession:
    def __init__(
        self,
        *,
        section: Dict[str, Any],
        api_key: str,
        connect: ConnectFactory,
        on_partial: Optional[Any] = None,
        on_final: Optional[Any] = None,
        on_error: Optional[Any] = None,
        options: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.section = section
        self.api_key = api_key
        self.connect = connect
        self.on_partial = on_partial
        self.on_final = on_final
        self.on_error = on_error
        self.options = dict(options or {})
        self.started = time.perf_counter()
        self.chunk_count = 0
        self.final_timeout = float(self.options.get("final_timeout") or 12)
        self._queued_audio_bytes = 0
        self._audio: "asyncio.Queue[Optional[bytes]]" = asyncio.Queue()
        self._task: Optional[asyncio.Task[ASRResult]] = None
        self._closed = False

    async def start(self) -> None:
        if not self._task:
            self._task = asyncio.create_task(self._run())

    async def send_audio(self, audio: bytes) -> None:
        if self._closed:
            raise SpeechGatewayError("ASR 流式会话已经结束。", code="stream_closed", provider="alicloud")
        if audio:
            try:
                configured = int(self.options.get("frame_bytes") or ALICLOUD_ASR_AUDIO_FRAME_BYTES)
            except (TypeError, ValueError):
                configured = ALICLOUD_ASR_AUDIO_FRAME_BYTES
            frame_bytes = max(1, min(configured, ALICLOUD_ASR_AUDIO_FRAME_BYTES))
            self._queued_audio_bytes += len(audio)
            for offset in range(0, len(audio), frame_bytes):
                await self._audio.put(audio[offset : offset + frame_bytes])

    async def finish(self) -> ASRResult:
        if not self._closed:
            self._closed = True
            await self._audio.put(None)
        if not self._task:
            raise SpeechGatewayError("ASR 流式会话尚未启动。", code="stream_not_started", provider="alicloud")
        try:
            send_budget = self._queued_audio_bytes / self._audio_send_rate_bytes_per_sec()
            return await asyncio.wait_for(self._task, timeout=self.final_timeout + send_budget + 2)
        except asyncio.TimeoutError as exc:
            error = SpeechGatewayError("阿里云 ASR final 响应超时。", code="final_timeout", provider="alicloud")
            await _call_callback(self.on_error, error)
            if self._task and not self._task.done():
                self._task.cancel()
            raise error from exc

    async def close(self) -> None:
        self._closed = True
        if self._task and not self._task.done():
            self._task.cancel()

    async def _run(self) -> ASRResult:
        settings = dict(self.section.get("settings") or {})
        model = str(settings.get("model") or "qwen3-asr-flash-realtime")
        endpoint = _endpoint_with_model(str(self.section.get("endpoint") or ""), model)
        headers = _alicloud_headers(self.api_key, self.section)
        # 实时转写按 server-VAD 切成多段，每段一个 `.completed`。回调约定是"累计全文"
        # （与讯飞 IAT 网关一致）：转写层会用回调文本整段覆盖字幕，若每次只上报当前
        # 分段，字幕就会只剩最后一段（历史 bug：一整句发言最终只显示最后一个"嗯"）。
        # 因此把已定稿分段并入 text_parts，并始终上报"已定稿全文 + 当前在写分段"。
        committed: list[str] = []
        current_partial = ""
        try:
            async with await _ws_connect(self.connect, endpoint, headers, open_timeout=8, close_timeout=3) as websocket:
                await websocket.send(json.dumps({"event_id": _event_id(), "type": "session.update", "session": _asr_session(settings)}, ensure_ascii=False))
                sender = asyncio.create_task(self._send_frames(websocket))
                try:
                    async for raw in websocket:
                        if isinstance(raw, bytes):
                            continue
                        message = json.loads(raw)
                        event_type = str(message.get("type") or "")
                        if event_type == "error":
                            error = message.get("error") or message
                            raise SpeechGatewayError(str(error.get("message") or "阿里云 ASR 返回错误。"), code=error.get("code"), provider="alicloud")
                        text = _extract_text(message)
                        latency = int((time.perf_counter() - self.started) * 1000)
                        if event_type.endswith(".completed"):
                            if text:
                                committed.append(text)
                            current_partial = ""
                            await _call_callback(self.on_partial, "".join(committed), latency, self.chunk_count)
                        elif event_type.endswith(".text") and text:
                            current_partial = text
                            await _call_callback(self.on_partial, "".join(committed) + current_partial, latency, self.chunk_count)
                        if event_type == "session.finished":
                            break
                finally:
                    if not sender.done():
                        sender.cancel()
        except SpeechGatewayError as exc:
            await _call_callback(self.on_error, exc)
            raise
        except Exception as exc:  # noqa: BLE001
            error = SpeechGatewayError(f"阿里云 ASR 调用失败：{exc}", provider="alicloud")
            await _call_callback(self.on_error, error)
            raise error from exc
        full_text = "".join(committed)
        latency_ms = int((time.perf_counter() - self.started) * 1000)
        await _call_callback(self.on_final, full_text, latency_ms, self.chunk_count)
        return ASRResult(text=full_text, latency_ms=latency_ms, chunk_count=self.chunk_count)

    async def _send_frames(self, websocket: Any) -> None:
        started = time.perf_counter()
        sent_bytes = 0
        rate = self._audio_send_rate_bytes_per_sec()
        while True:
            chunk = await self._audio.get()
            if chunk is None:
                await websocket.send(json.dumps({"event_id": _event_id(), "type": "session.finish"}, ensure_ascii=False))
                return
            await websocket.send(
                json.dumps(
                    {"event_id": _event_id(), "type": "input_audio_buffer.append", "audio": base64.b64encode(chunk).decode("ascii")},
                    ensure_ascii=False,
                )
            )
            self.chunk_count += 1
            sent_bytes += len(chunk)
            target_elapsed = sent_bytes / rate
            delay = started + target_elapsed - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)

    def _audio_send_rate_bytes_per_sec(self) -> float:
        try:
            configured = float(self.options.get("send_rate_bytes_per_sec") or self.options.get("audio_send_rate_bytes_per_sec") or ALICLOUD_ASR_AUDIO_SEND_RATE_BYTES_PER_SEC)
        except (TypeError, ValueError):
            configured = ALICLOUD_ASR_AUDIO_SEND_RATE_BYTES_PER_SEC
        return max(1.0, min(configured, float(ALICLOUD_ASR_MAX_AUDIO_SEND_RATE_BYTES_PER_SEC)))


def _asr_session(settings: Dict[str, Any]) -> Dict[str, Any]:
    turn_detection = settings.get("turn_detection")
    if not isinstance(turn_detection, dict):
        turn_detection = {"type": "server_vad", "threshold": 0.0, "silence_duration_ms": 400}
    return {
        "input_audio_format": settings.get("input_audio_format") or "pcm",
        "sample_rate": int(settings.get("sample_rate") or 16000),
        "input_audio_transcription": {"language": settings.get("language") or "zh"},
        "turn_detection": turn_detection,
    }


def _endpoint_with_model(endpoint: str, model: str) -> str:
    value = endpoint.strip() or "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
    parts = urlsplit(value)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["model"] = model
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _alicloud_api_key(section: Dict[str, Any]) -> str:
    secrets = section.get("secrets") or {}
    return str((secrets.get("alicloud") or {}).get("api_key") or os.getenv("DASHSCOPE_API_KEY", "")).strip()


def _alicloud_headers(api_key: str, section: Dict[str, Any]) -> Dict[str, str]:
    headers = {"Authorization": f"Bearer {api_key}", "user-agent": "phdebate-speech/1.0"}
    workspace_id = str(((section.get("secrets") or {}).get("alicloud") or {}).get("workspace_id") or "").strip()
    if workspace_id:
        headers["X-DashScope-WorkSpace"] = workspace_id
    return headers


async def _ws_connect(connect: ConnectFactory, url: str, headers: Dict[str, str], **kwargs: Any) -> Any:
    header_keys = _header_arg_order(connect)
    last_error: Optional[TypeError] = None
    for header_key in header_keys:
        try:
            result = connect(url, **{header_key: headers}, **kwargs)
            if inspect.isawaitable(result):
                return await result
            return result
        except TypeError as exc:
            last_error = exc
            if header_key not in str(exc):
                raise
    if last_error:
        raise last_error
    result = connect(url, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


async def _ws_connect_funasr(connect: ConnectFactory, url: str, **kwargs: Any) -> Any:
    attempts = (
        {"subprotocols": ["binary"], "ping_interval": None, **kwargs},
        {"ping_interval": None, **kwargs},
        kwargs,
    )
    last_error: Optional[TypeError] = None
    for call_kwargs in attempts:
        try:
            result = connect(url, **call_kwargs)
            if inspect.isawaitable(result):
                return await result
            return result
        except TypeError as exc:
            last_error = exc
            if "subprotocols" not in str(exc) and "ping_interval" not in str(exc) and "open_timeout" not in str(exc):
                raise
    if last_error:
        raise last_error
    result = connect(url)
    if inspect.isawaitable(result):
        return await result
    return result


def _header_arg_order(connect: ConnectFactory) -> tuple[str, str]:
    try:
        parameters = inspect.signature(connect).parameters
    except (TypeError, ValueError):
        return ("additional_headers", "extra_headers")
    if "additional_headers" in parameters:
        return ("additional_headers", "extra_headers")
    if "extra_headers" in parameters:
        return ("extra_headers", "additional_headers")
    return ("additional_headers", "extra_headers")


async def _call_callback(callback: Optional[Any], *args: Any) -> None:
    if callback is None:
        return
    result = callback(*args)
    if inspect.isawaitable(result):
        await result


def _extract_text(message: Dict[str, Any]) -> str:
    for key in ("transcript", "text"):
        value = message.get(key)
        if isinstance(value, str) and value:
            return value
    for key in ("delta", "item", "conversation", "response"):
        value = message.get(key)
        if isinstance(value, dict):
            text = _extract_text(value)
            if text:
                return text
    content = message.get("content")
    if isinstance(content, list):
        return "".join(_extract_text(item) for item in content if isinstance(item, dict))
    return ""


def _join_http_url(base: str, path: str) -> str:
    value = (base or "").strip().rstrip("/") or "http://127.0.0.1:12302"
    if value.endswith(path):
        return value
    return f"{value}{path}"


def _extract_local_qwen_asr_text(response: httpx.Response) -> str:
    content_type = response.headers.get("content-type", "")
    if "application/json" in content_type:
        data = response.json()
        if isinstance(data, dict):
            for key in ("text", "transcript", "content"):
                value = data.get(key)
                if isinstance(value, str):
                    return value
            choices = data.get("choices")
            if isinstance(choices, list):
                return "".join(_extract_text(item) for item in choices if isinstance(item, dict))
    return response.text.strip()


def _local_qwen_voice(voice: str) -> str:
    value = voice.strip().lower()
    aliases = {
        "neil": "dylan",
        "ethan": "dylan",
        "cherry": "vivian",
        "serena": "serena",
    }
    return aliases.get(value, value or "dylan")


def _env_truthy(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _local_qwen_tts_extra_payload(session: Dict[str, Any]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    for key in ("seed", "temperature", "top_p"):
        value = session.get(key)
        if value is not None and value != "":
            payload[key] = value
    if _env_truthy("PHDEBATE_LOCAL_QWEN_TTS_EXTENDED_PARAMS"):
        for key in ("volume", "pitch_rate", "instructions"):
            value = session.get(key)
            if value is not None and value != "":
                payload[key] = value
    return payload


def _local_qwen_tts_payload_variants(base_payload: Dict[str, Any], session: Dict[str, Any]) -> list[Dict[str, Any]]:
    variants: list[Dict[str, Any]] = []
    extra = _local_qwen_tts_extra_payload(session)
    if extra:
        variants.append({**base_payload, **extra})
        seed = extra.get("seed")
        if seed is not None and set(extra.keys()) != {"seed"}:
            variants.append({**base_payload, "seed": seed})
    variants.append(base_payload)

    unique: list[Dict[str, Any]] = []
    seen: set[str] = set()
    for payload in variants:
        key = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        unique.append(payload)
    return unique


def _tts_mime_type(format_name: str) -> str:
    value = str(format_name or "mp3").lower()
    if value == "mp3":
        return "audio/mpeg"
    if value == "wav":
        return "audio/wav"
    if value == "opus":
        return "audio/opus"
    return "audio/L16"


def _event_id() -> str:
    return f"event_{uuid.uuid4().hex}"
