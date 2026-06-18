from __future__ import annotations

import asyncio
import inspect
import json
import os
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Dict, Optional

import websockets

from app.services.xfyun_adapter import (
    XfyunCredentials,
    build_iat_end_frame,
    build_iat_frame,
    build_super_tts_frame,
    build_tts_frame,
    credentials_from_env,
    extract_iat_text,
    extract_super_tts_audio,
    extract_tts_audio,
    iat_finished,
    super_tts_error,
    super_tts_finished,
    tts_finished,
    xfyun_signed_url,
)


def _is_super_tts_url(url: str) -> bool:
    """超拟人 / super smart-tts 使用 /v1/private/ 资源路径，与老版 TTS schema 不同。"""
    return "/v1/private/" in (url or "") or os.getenv("XFYUN_TTS_SCHEMA", "").strip().lower() == "super"


class XfyunGatewayError(Exception):
    def __init__(self, message: str, code: Optional[int] = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass(frozen=True)
class TTSResult:
    audio: bytes
    mime_type: str
    latency_ms: int
    chunk_count: int


@dataclass(frozen=True)
class ASRResult:
    text: str
    latency_ms: int
    chunk_count: int


ConnectFactory = Callable[..., Any]
ASRTextCallback = Callable[[str, int, int], Any]
ASRErrorCallback = Callable[[XfyunGatewayError], Any]


class XfyunASRGateway:
    def __init__(
        self,
        credentials: Optional[XfyunCredentials] = None,
        url: Optional[str] = None,
        connect: Optional[ConnectFactory] = None,
    ) -> None:
        self.credentials = credentials or credentials_from_env()
        self.url = url
        self.connect = connect or websockets.connect

    async def recognize(self, audio: bytes, **options: Any) -> ASRResult:
        if not self.credentials:
            raise XfyunGatewayError("讯飞 ASR 缺少 XFYUN_APP_ID / XFYUN_API_KEY / XFYUN_API_SECRET。")
        url = self.url or options.pop("url", None)
        if not url:
            raise XfyunGatewayError("讯飞 ASR 缺少 XFYUN_ASR_URL。")
        if not audio:
            raise XfyunGatewayError("ASR 试识别音频不能为空。")

        open_timeout = _float_option(options, "open_timeout", "XFYUN_ASR_OPEN_TIMEOUT_S", 8.0)
        close_timeout = _float_option(options, "close_timeout", "XFYUN_ASR_CLOSE_TIMEOUT_S", 3.0)
        final_timeout = _float_option(options, "final_timeout", "XFYUN_ASR_FINAL_TIMEOUT_S", 12.0)
        signed_url = xfyun_signed_url(url, self.credentials)
        started = time.perf_counter()
        chunk_count = 0
        text_parts = []
        async with self.connect(signed_url, open_timeout=open_timeout, close_timeout=close_timeout) as websocket:
            for frame in _iat_frames(audio, self.credentials, options):
                await websocket.send(json.dumps(frame, ensure_ascii=False))
                if frame.get("data", {}).get("audio"):
                    chunk_count += 1
            while True:
                try:
                    raw = await _receive_websocket_message(websocket, final_timeout)
                except StopAsyncIteration:
                    break
                message = json.loads(raw)
                code = int(message.get("code", 0))
                if code != 0:
                    raise XfyunGatewayError(str(message.get("message") or "讯飞 ASR 返回错误。"), code=code)
                text = extract_iat_text(message)
                if text:
                    text_parts.append(text)
                if iat_finished(message):
                    break
        latency_ms = int((time.perf_counter() - started) * 1000)
        return ASRResult(text="".join(text_parts), latency_ms=latency_ms, chunk_count=chunk_count)

    async def open_stream(
        self,
        *,
        on_partial: Optional[ASRTextCallback] = None,
        on_final: Optional[ASRTextCallback] = None,
        on_error: Optional[ASRErrorCallback] = None,
        **options: Any,
    ) -> "XfyunASRStreamSession":
        if not self.credentials:
            raise XfyunGatewayError("讯飞 ASR 缺少 XFYUN_APP_ID / XFYUN_API_KEY / XFYUN_API_SECRET。")
        url = self.url or options.pop("url", None)
        if not url:
            raise XfyunGatewayError("讯飞 ASR 缺少 XFYUN_ASR_URL。")
        session = XfyunASRStreamSession(
            credentials=self.credentials,
            url=url,
            connect=self.connect,
            on_partial=on_partial,
            on_final=on_final,
            on_error=on_error,
            options=options,
        )
        await session.start()
        return session


class XfyunASRStreamSession:
    def __init__(
        self,
        *,
        credentials: XfyunCredentials,
        url: str,
        connect: ConnectFactory,
        on_partial: Optional[ASRTextCallback] = None,
        on_final: Optional[ASRTextCallback] = None,
        on_error: Optional[ASRErrorCallback] = None,
        options: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.credentials = credentials
        self.url = url
        self.connect = connect
        self.on_partial = on_partial
        self.on_final = on_final
        self.on_error = on_error
        self.options = dict(options or {})
        self.started = time.perf_counter()
        self.chunk_count = 0
        self.open_timeout = _float_option(self.options, "open_timeout", "XFYUN_ASR_OPEN_TIMEOUT_S", 8.0)
        self.close_timeout = _float_option(self.options, "close_timeout", "XFYUN_ASR_CLOSE_TIMEOUT_S", 3.0)
        self.final_timeout = _float_option(self.options, "final_timeout", "XFYUN_ASR_FINAL_TIMEOUT_S", 12.0)
        self._audio: asyncio.Queue[Optional[bytes]] = asyncio.Queue()
        self._task: Optional[asyncio.Task[ASRResult]] = None
        self._end_sent = asyncio.Event()
        self._closed = False

    async def start(self) -> None:
        if self._task:
            return
        self._task = asyncio.create_task(self._run())

    async def send_audio(self, audio: bytes) -> None:
        if self._closed:
            raise XfyunGatewayError("ASR 流式会话已经结束。")
        if not audio:
            return
        await self._audio.put(audio)

    async def finish(self) -> ASRResult:
        if not self._closed:
            self._closed = True
            await self._audio.put(None)
        if not self._task:
            raise XfyunGatewayError("ASR 流式会话尚未启动。")
        try:
            return await asyncio.wait_for(self._task, timeout=max(self.final_timeout + 1, self.final_timeout))
        except asyncio.TimeoutError as exc:
            error = XfyunGatewayError("讯飞 ASR final 响应超时。")
            await _call_callback(self.on_error, error)
            if self._task and not self._task.done():
                self._task.cancel()
            raise error from exc

    async def close(self) -> None:
        self._closed = True
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                return

    async def _run(self) -> ASRResult:
        text_parts = []
        signed_url = xfyun_signed_url(self.url, self.credentials)
        sender: Optional[asyncio.Task[None]] = None
        try:
            async with self.connect(signed_url, open_timeout=self.open_timeout, close_timeout=self.close_timeout) as websocket:
                sender = asyncio.create_task(self._send_frames(websocket))
                while True:
                    try:
                        raw = await self._receive_next_stream_message(websocket)
                    except StopAsyncIteration:
                        break
                    message = json.loads(raw)
                    code = int(message.get("code", 0))
                    if code != 0:
                        raise XfyunGatewayError(str(message.get("message") or "讯飞 ASR 返回错误。"), code=code)
                    text = extract_iat_text(message)
                    latency_ms = int((time.perf_counter() - self.started) * 1000)
                    if text:
                        text_parts.append(text)
                        await _call_callback(self.on_partial, "".join(text_parts), latency_ms, self.chunk_count)
                    if iat_finished(message):
                        break
                if sender:
                    await sender
        except XfyunGatewayError as exc:
            if sender:
                sender.cancel()
            await _call_callback(self.on_error, exc)
            raise
        latency_ms = int((time.perf_counter() - self.started) * 1000)
        result = ASRResult(text="".join(text_parts), latency_ms=latency_ms, chunk_count=self.chunk_count)
        await _call_callback(self.on_final, result.text, result.latency_ms, result.chunk_count)
        return result

    async def _send_frames(self, websocket: Any) -> None:
        options = dict(self.options)
        audio_format = str(options.pop("audio_format", "audio/L16;rate=16000"))
        encoding = str(options.pop("encoding", "raw"))
        first = True
        while True:
            chunk = await self._audio.get()
            if chunk is None:
                await websocket.send(json.dumps(build_iat_end_frame(), ensure_ascii=False))
                self._end_sent.set()
                return
            status = 0 if first else 1
            first = False
            frame = build_iat_frame(
                chunk,
                self.credentials,
                status=status,
                audio_format=audio_format,
                encoding=encoding,
                **options,
            )
            await websocket.send(json.dumps(frame, ensure_ascii=False))
            self.chunk_count += 1

    async def _receive_next_stream_message(self, websocket: Any) -> str:
        receive_task = asyncio.create_task(_receive_websocket_message(websocket))
        if self._end_sent.is_set():
            try:
                return await asyncio.wait_for(receive_task, self.final_timeout)
            except asyncio.TimeoutError as exc:
                receive_task.cancel()
                raise XfyunGatewayError("讯飞 ASR final 响应超时。") from exc

        end_task = asyncio.create_task(self._end_sent.wait())
        done, pending = await asyncio.wait({receive_task, end_task}, return_when=asyncio.FIRST_COMPLETED)
        if end_task in done and receive_task not in done:
            try:
                return await asyncio.wait_for(receive_task, self.final_timeout)
            except asyncio.TimeoutError as exc:
                receive_task.cancel()
                raise XfyunGatewayError("讯飞 ASR final 响应超时。") from exc
        for task in pending:
            task.cancel()
        return await receive_task


class XfyunTTSGateway:
    def __init__(
        self,
        credentials: Optional[XfyunCredentials] = None,
        url: Optional[str] = None,
        connect: Optional[ConnectFactory] = None,
    ) -> None:
        self.credentials = credentials or credentials_from_env()
        self.url = url
        self.connect = connect or websockets.connect

    async def synthesize(self, text: str, **options: Any) -> TTSResult:
        if not self.credentials:
            raise XfyunGatewayError("讯飞 TTS 缺少 XFYUN_APP_ID / XFYUN_API_KEY / XFYUN_API_SECRET。")
        url = self.url or options.pop("url", None)
        if not url:
            raise XfyunGatewayError("讯飞 TTS 缺少 XFYUN_TTS_URL。")
        content = text.strip()
        if not content:
            raise XfyunGatewayError("TTS 试合成文本不能为空。")

        use_super = _is_super_tts_url(url)
        try:
            return await self._synthesize_once(content, url, use_super, options)
        except XfyunGatewayError as exc:
            raise _diagnose_tts_error(exc, url, use_super, options) from exc

    async def synthesize_stream(self, text: str, **options: Any) -> AsyncIterator[Dict[str, Any]]:
        """流式合成：每收到一段音频即产出 {'type':'chunk', 'audio': bytes, 'index': int}，
        结束时产出 {'type':'done', 'mime_type', 'latency_ms', 'chunk_count'}。"""
        if not self.credentials:
            raise XfyunGatewayError("讯飞 TTS 缺少 XFYUN_APP_ID / XFYUN_API_KEY / XFYUN_API_SECRET。")
        url = self.url or options.pop("url", None)
        if not url:
            raise XfyunGatewayError("讯飞 TTS 缺少 XFYUN_TTS_URL。")
        content = text.strip()
        if not content:
            raise XfyunGatewayError("TTS 试合成文本不能为空。")

        use_super = _is_super_tts_url(url)
        try:
            async for event in self._synthesize_stream_once(content, url, use_super, options):
                yield event
        except XfyunGatewayError as exc:
            raise _diagnose_tts_error(exc, url, use_super, options) from exc

    async def _synthesize_once(self, content: str, url: str, use_super: bool, options: Dict[str, Any]) -> TTSResult:
        frame, aue = _build_tts_frame(content, self.credentials, use_super, options)
        signed_url = xfyun_signed_url(url, self.credentials)
        started = time.perf_counter()
        audio_parts = []
        chunk_count = 0
        async with self.connect(signed_url, open_timeout=8, close_timeout=3) as websocket:
            await websocket.send(json.dumps(frame, ensure_ascii=False))
            async for raw in websocket:
                chunk, finished = _parse_tts_message(json.loads(raw), use_super)
                if chunk:
                    audio_parts.append(chunk)
                    chunk_count += 1
                if finished:
                    break
        return TTSResult(
            audio=b"".join(audio_parts),
            mime_type=_tts_mime_type(aue),
            latency_ms=int((time.perf_counter() - started) * 1000),
            chunk_count=chunk_count,
        )

    async def _synthesize_stream_once(
        self,
        content: str,
        url: str,
        use_super: bool,
        options: Dict[str, Any],
    ) -> AsyncIterator[Dict[str, Any]]:
        frame, aue = _build_tts_frame(content, self.credentials, use_super, options)
        signed_url = xfyun_signed_url(url, self.credentials)
        started = time.perf_counter()
        chunk_count = 0
        async with self.connect(signed_url, open_timeout=8, close_timeout=3) as websocket:
            await websocket.send(json.dumps(frame, ensure_ascii=False))
            async for raw in websocket:
                chunk, finished = _parse_tts_message(json.loads(raw), use_super)
                if chunk:
                    chunk_count += 1
                    yield {"type": "chunk", "audio": chunk, "index": chunk_count}
                if finished:
                    break
        yield {
            "type": "done",
            "mime_type": _tts_mime_type(aue),
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "chunk_count": chunk_count,
        }


def _build_tts_frame(content: str, credentials: XfyunCredentials, use_super: bool, options: Dict[str, Any]) -> tuple[Dict[str, Any], str]:
    frame_options = {key: value for key, value in options.items() if not key.startswith("_")}
    if use_super:
        frame = build_super_tts_frame(content, credentials, **frame_options)
        return frame, str(frame["parameter"]["tts"]["audio"].get("encoding") or "")
    frame = build_tts_frame(content, credentials, **frame_options)
    return frame, str(frame["business"].get("aue") or "")


def _parse_tts_message(message: Dict[str, Any], use_super: bool) -> tuple[bytes, bool]:
    if use_super:
        error = super_tts_error(message)
        if error:
            raise XfyunGatewayError(str(error.get("message")), code=int(error.get("code") or 0))
        return extract_super_tts_audio(message), super_tts_finished(message)
    code = int(message.get("code", 0))
    if code != 0:
        raise XfyunGatewayError(str(message.get("message") or "讯飞 TTS 返回错误。"), code=code)
    return extract_tts_audio(message), tts_finished(message)


def _diagnose_tts_error(exc: XfyunGatewayError, url: str, use_super: bool, options: Dict[str, Any]) -> XfyunGatewayError:
    if not use_super or not _is_tts_license_error(exc):
        return exc
    voice = str(options.get("voice") or os.getenv("XFYUN_TTS_VOICE", "") or "x6_lingfeiyi_pro").strip()
    return XfyunGatewayError(
        f"{exc.message}；讯飞超拟人 TTS License 校验失败。"
        f"当前请求未切换发音人，仍使用 voice={voice}。"
        "请在讯飞控制台核对：当前 APPID/APIKey 是否绑定该超拟人 TTS 服务资源、"
        "该发音人是否已开通，以及字符/并发额度是否已用尽。",
        code=exc.code,
    )


def _is_tts_license_error(exc: XfyunGatewayError) -> bool:
    message = exc.message.lower()
    return "licccheck" in message or "licc limit" in message or "unauthenticated" in message


def _tts_mime_type(aue: str) -> str:
    value = aue.lower()
    if value in {"lame", "mp3"}:
        return "audio/mpeg"
    if value in {"raw", "pcm"}:
        return "audio/L16"
    if value == "speex":
        return "audio/speex"
    return "application/octet-stream"


def _iat_frames(audio: bytes, credentials: XfyunCredentials, options: Dict[str, Any]):
    chunk_size = int(options.pop("chunk_size", 1280))
    audio_format = str(options.pop("audio_format", "audio/L16;rate=16000"))
    encoding = str(options.pop("encoding", "raw"))
    chunks = [audio[index:index + chunk_size] for index in range(0, len(audio), chunk_size)]
    for index, chunk in enumerate(chunks):
        status = 0 if index == 0 else 1
        yield build_iat_frame(
            chunk,
            credentials,
            status=status,
            audio_format=audio_format,
            encoding=encoding,
            **options,
        )
    yield build_iat_end_frame()


async def _call_callback(callback: Optional[Callable[..., Any]], *args: Any) -> None:
    if not callback:
        return
    result = callback(*args)
    if inspect.isawaitable(result):
        await result


def _float_option(options: Dict[str, Any], option_name: str, env_name: str, default: float) -> float:
    raw = options.pop(option_name, None)
    if raw is None:
        raw = os.getenv(env_name, "")
    if raw in {None, ""}:
        return default
    return float(raw)


async def _receive_websocket_message(websocket: Any, timeout: Optional[float] = None) -> str:
    async def read_next() -> str:
        if hasattr(websocket, "recv"):
            return await websocket.recv()
        return await websocket.__anext__()

    try:
        if timeout is None:
            return await read_next()
        return await asyncio.wait_for(read_next(), timeout)
    except asyncio.TimeoutError as exc:
        raise XfyunGatewayError("讯飞 ASR 响应超时。") from exc
