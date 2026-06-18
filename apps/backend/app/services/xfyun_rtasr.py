"""讯飞实时语音转写（RTASR 大模型版 / 极速版）适配层。

与老版 IAT（host/date/authorization 鉴权）不同，本接口使用
`wss://office-api-ast-dx.iflyaisol.com/ast/communicate/v1` 端点，鉴权为
appId/accessKeyId/utc/signature（HMAC-SHA1）查询参数；音频为 16k/16bit 原始
PCM，按 1280 字节/40ms 推送；结束发送 `{"end": true}`；结果在
`data.cn.st.rt[].ws[].cw[].w`，`data.cn.st.type` 为 0=最终 / 1=中间。

协议已对官方端点做过真实握手验证（action=started → result → 正常关闭）。
真实语音转写文本质量需现场用麦克风音频核验。
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import hashlib
import hmac
import json
import os
import time
import uuid as uuid_lib
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlencode

import websockets

from app.services.xfyun_adapter import XfyunCredentials, credentials_from_env
from app.services.xfyun_gateway import ASRResult, XfyunGatewayError

RTASR_DEFAULT_PATH = "/ast/communicate/v1"
FRAME_BYTES = 1280  # 16kHz * 16bit * 0.04s
FRAME_INTERVAL_S = 0.04


def _utc_now() -> str:
    tz = datetime.timezone(datetime.timedelta(hours=8))
    return datetime.datetime.now(tz).strftime("%Y-%m-%dT%H:%M:%S%z")


def build_rtasr_url(
    url: str,
    credentials: XfyunCredentials,
    *,
    lang: Optional[str] = None,
    audio_encode: str = "pcm_s16le",
    samplerate: str = "16000",
    request_uuid: Optional[str] = None,
    utc: Optional[str] = None,
) -> str:
    """构造带签名的 RTASR WebSocket URL。

    baseString：除 signature 外所有参数按参数名升序，键值各自 URL-encode 后用 & 连接；
    signature = base64(HMAC-SHA1(accessKeySecret, baseString))。
    """
    base_url = url.split("?", 1)[0]
    if not base_url.rstrip("/").endswith(RTASR_DEFAULT_PATH.strip("/")):
        base_url = base_url.rstrip("/") + RTASR_DEFAULT_PATH
    params = {
        "appId": credentials.app_id,
        "accessKeyId": credentials.api_key,
        "utc": utc or _utc_now(),
        "uuid": request_uuid or uuid_lib.uuid4().hex,
        "audio_encode": audio_encode,
        "lang": lang or os.getenv("XFYUN_ASR_LANG", "autodialect"),
        "samplerate": samplerate,
    }
    base_string = "&".join(f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in sorted(params.items()))
    signature = base64.b64encode(
        hmac.new(credentials.api_secret.encode("utf-8"), base_string.encode("utf-8"), hashlib.sha1).digest()
    ).decode("utf-8")
    query = urlencode({**params, "signature": signature})
    return f"{base_url}?{query}"


def extract_rtasr_text(message: Dict[str, Any]) -> str:
    """从一条 result 消息里拼出文本。"""
    data = message.get("data") or {}
    st = ((data.get("cn") or {}).get("st") or {})
    words: List[str] = []
    for rt in st.get("rt") or []:
        for ws_item in rt.get("ws") or []:
            for cw in ws_item.get("cw") or []:
                word = cw.get("w") or ""
                if word:
                    words.append(word)
    return "".join(words)


def rtasr_is_final(message: Dict[str, Any]) -> bool:
    """data.cn.st.type == "0" 表示该片段为最终结果。"""
    data = message.get("data") or {}
    st = ((data.get("cn") or {}).get("st") or {})
    return str(st.get("type")) == "0"


def rtasr_action(message: Dict[str, Any]) -> str:
    return str((message.get("data") or {}).get("action") or "")


def rtasr_error(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    data = message.get("data") or {}
    if rtasr_action(message) == "error" or message.get("msg_type") == "error":
        return {"code": data.get("code") or message.get("code"), "message": data.get("desc") or message.get("desc") or "RTASR error"}
    return None


class XfyunRTASRGateway:
    """实时语音转写网关：一次性识别一段 PCM（archive 补识别 / 自检）。"""

    def __init__(
        self,
        credentials: Optional[XfyunCredentials] = None,
        url: Optional[str] = None,
        connect: Optional[Any] = None,
    ) -> None:
        self.credentials = credentials or credentials_from_env()
        self.url = url
        self.connect = connect or websockets.connect

    async def open_stream(
        self,
        *,
        on_partial: Optional[Any] = None,
        on_final: Optional[Any] = None,
        on_error: Optional[Any] = None,
        **options: Any,
    ) -> "XfyunRTASRStreamSession":
        if not self.credentials:
            raise XfyunGatewayError("讯飞 ASR 缺少 XFYUN_APP_ID / XFYUN_API_KEY / XFYUN_API_SECRET。")
        url = self.url or options.pop("url", None)
        if not url:
            raise XfyunGatewayError("讯飞 ASR 缺少 XFYUN_ASR_URL。")
        session = XfyunRTASRStreamSession(
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

    async def recognize(self, audio: bytes, **options: Any) -> ASRResult:
        if not self.credentials:
            raise XfyunGatewayError("讯飞 ASR 缺少 XFYUN_APP_ID / XFYUN_API_KEY / XFYUN_API_SECRET。")
        url = self.url or options.pop("url", None)
        if not url:
            raise XfyunGatewayError("讯飞 ASR 缺少 XFYUN_ASR_URL。")
        signed_url = build_rtasr_url(url, self.credentials, **_url_options(options))
        started = time.perf_counter()
        finals: List[str] = []
        chunk_count = 0
        open_timeout = float(os.getenv("XFYUN_ASR_OPEN_TIMEOUT_S", "8"))
        close_timeout = float(os.getenv("XFYUN_ASR_CLOSE_TIMEOUT_S", "3"))
        async with self.connect(signed_url, open_timeout=open_timeout, close_timeout=close_timeout) as websocket:
            # 等待 action=started
            first = json.loads(await websocket.recv())
            error = rtasr_error(first)
            if error:
                raise XfyunGatewayError(str(error.get("message")), code=_as_int(error.get("code")))
            # 推送音频帧
            for index in range(0, max(len(audio), 1), FRAME_BYTES):
                frame = audio[index : index + FRAME_BYTES]
                if not frame:
                    break
                await websocket.send(frame)
                chunk_count += 1
                await asyncio.sleep(FRAME_INTERVAL_S)
            await websocket.send(json.dumps({"end": True}))
            try:
                while True:
                    raw = await websocket.recv()
                    if isinstance(raw, bytes):
                        continue
                    message = json.loads(raw)
                    error = rtasr_error(message)
                    if error:
                        raise XfyunGatewayError(str(error.get("message")), code=_as_int(error.get("code")))
                    if message.get("msg_type") == "result" and rtasr_is_final(message):
                        text = extract_rtasr_text(message)
                        if text:
                            finals.append(text)
            except websockets.ConnectionClosedOK:
                pass
            except websockets.ConnectionClosed:
                pass
        latency_ms = int((time.perf_counter() - started) * 1000)
        return ASRResult(text="".join(finals), latency_ms=latency_ms, chunk_count=chunk_count)


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


_URL_OPTION_KEYS = {"lang", "audio_encode", "samplerate", "request_uuid", "utc"}


def _url_options(options: Dict[str, Any]) -> Dict[str, Any]:
    """只保留 build_rtasr_url 认识的参数，忽略 IAT 风格的 audio_format/encoding 等。"""
    return {key: value for key, value in options.items() if key in _URL_OPTION_KEYS}


class XfyunRTASRStreamSession:
    """实时流式会话：辩手发言期间持续推送 PCM 分片，回调 partial/final，
    驱动大屏实时字幕。协议与 XfyunRTASRGateway.recognize 相同。"""

    def __init__(
        self,
        *,
        credentials: XfyunCredentials,
        url: str,
        connect: Any,
        on_partial: Optional[Any] = None,
        on_final: Optional[Any] = None,
        on_error: Optional[Any] = None,
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
        self.open_timeout = float(os.getenv("XFYUN_ASR_OPEN_TIMEOUT_S", "8"))
        self.close_timeout = float(os.getenv("XFYUN_ASR_CLOSE_TIMEOUT_S", "3"))
        self.final_timeout = float(os.getenv("XFYUN_ASR_FINAL_TIMEOUT_S", "12"))
        self._audio: "asyncio.Queue[Optional[bytes]]" = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None
        self._closed = False

    async def start(self) -> None:
        if self._task:
            return
        self._task = asyncio.create_task(self._run())

    async def send_audio(self, audio: bytes) -> None:
        if self._closed:
            raise XfyunGatewayError("ASR 流式会话已经结束。")
        if audio:
            await self._audio.put(audio)

    async def finish(self) -> ASRResult:
        if not self._closed:
            self._closed = True
            await self._audio.put(None)
        if not self._task:
            raise XfyunGatewayError("ASR 流式会话尚未启动。")
        try:
            return await asyncio.wait_for(self._task, timeout=self.final_timeout + 1)
        except asyncio.TimeoutError as exc:
            error = XfyunGatewayError("讯飞 ASR final 响应超时。")
            await _call(self.on_error, error)
            if self._task and not self._task.done():
                self._task.cancel()
            raise error from exc

    async def close(self) -> None:
        self._closed = True
        if self._task and not self._task.done():
            self._task.cancel()

    async def _run(self) -> ASRResult:
        signed_url = build_rtasr_url(self.url, self.credentials, **_url_options(self.options))
        # 回调约定是"累计全文"（与 IAT 网关一致）：转写层会用回调文本整段覆盖字幕。
        # RTASR 按句给最终结果（type=0），若每句只上报自身文本，字幕会只剩最后一句。
        # 因此把定稿分句并入 finals，始终上报"已定稿全文 + 当前在写分句"，
        # 真正的 final 在流结束时统一回调一次。
        finals: List[str] = []
        current_partial = ""
        async with self.connect(signed_url, open_timeout=self.open_timeout, close_timeout=self.close_timeout) as websocket:
            first = json.loads(await websocket.recv())
            error = rtasr_error(first)
            if error:
                err = XfyunGatewayError(str(error.get("message")), code=_as_int(error.get("code")))
                await _call(self.on_error, err)
                raise err

            async def pump() -> None:
                while True:
                    chunk = await self._audio.get()
                    if chunk is None:
                        await websocket.send(json.dumps({"end": True}))
                        return
                    for index in range(0, len(chunk), FRAME_BYTES):
                        await websocket.send(chunk[index : index + FRAME_BYTES])
                        self.chunk_count += 1

            pump_task = asyncio.create_task(pump())
            try:
                while True:
                    raw = await websocket.recv()
                    if isinstance(raw, bytes):
                        continue
                    message = json.loads(raw)
                    error = rtasr_error(message)
                    if error:
                        err = XfyunGatewayError(str(error.get("message")), code=_as_int(error.get("code")))
                        await _call(self.on_error, err)
                        raise err
                    if message.get("msg_type") != "result":
                        continue
                    text = extract_rtasr_text(message)
                    if not text:
                        continue
                    latency = int((time.perf_counter() - self.started) * 1000)
                    if rtasr_is_final(message):
                        finals.append(text)
                        current_partial = ""
                        await _call(self.on_partial, "".join(finals), latency, self.chunk_count)
                    else:
                        current_partial = text
                        await _call(self.on_partial, "".join(finals) + current_partial, latency, self.chunk_count)
            except (websockets.ConnectionClosedOK, websockets.ConnectionClosed):
                pass
            finally:
                if not pump_task.done():
                    pump_task.cancel()
        latency_ms = int((time.perf_counter() - self.started) * 1000)
        full_text = "".join(finals)
        await _call(self.on_final, full_text, latency_ms, self.chunk_count)
        return ASRResult(text=full_text, latency_ms=latency_ms, chunk_count=self.chunk_count)


async def _call(callback: Optional[Any], *args: Any) -> None:
    if callback is None:
        return
    result = callback(*args)
    if asyncio.iscoroutine(result):
        await result


def is_rtasr_url(url: str) -> bool:
    """根据 URL 判断是否走 RTASR（极速版）协议而非老版 IAT。"""
    lowered = (url or "").lower()
    if os.getenv("XFYUN_ASR_SCHEMA", "").strip().lower() == "rtasr":
        return True
    return "iflyaisol" in lowered or "/ast/communicate" in lowered


def select_asr_gateway(url: str, connect: Optional[Any] = None):
    """RTASR 端点用 XfyunRTASRGateway，否则用老版 XfyunASRGateway。"""
    from app.services.xfyun_gateway import XfyunASRGateway

    if is_rtasr_url(url):
        return XfyunRTASRGateway(url=url, connect=connect) if connect else XfyunRTASRGateway(url=url)
    return XfyunASRGateway(url=url, connect=connect) if connect else XfyunASRGateway(url=url)
