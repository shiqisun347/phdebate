from __future__ import annotations

import asyncio
import base64
import inspect
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Dict, Optional
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl

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
            await self._audio.put(audio)

    async def finish(self) -> ASRResult:
        if not self._closed:
            self._closed = True
            await self._audio.put(None)
        if not self._task:
            raise SpeechGatewayError("ASR 流式会话尚未启动。", code="stream_not_started", provider="alicloud")
        try:
            return await asyncio.wait_for(self._task, timeout=self.final_timeout + 1)
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
