import asyncio
import json

import pytest

from app.services.xfyun_adapter import XfyunCredentials
from app.services.xfyun_gateway import XfyunASRGateway, XfyunGatewayError, XfyunTTSGateway


class FakeWebSocket:
    def __init__(self, messages):
        self.messages = messages
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def send(self, payload):
        self.sent.append(json.loads(payload))

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.messages:
            raise StopAsyncIteration
        return json.dumps(self.messages.pop(0))


class FakeStreamingWebSocket:
    def __init__(self, messages):
        self.messages = messages
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def send(self, payload):
        self.sent.append(json.loads(payload))

    def __aiter__(self):
        return self

    async def __anext__(self):
        for _ in range(200):
            if any(item.get("data", {}).get("status") == 2 for item in self.sent):
                if not self.messages:
                    raise StopAsyncIteration
                return json.dumps(self.messages.pop(0))
            await asyncio.sleep(0.001)
        raise AssertionError("streaming ASR end frame was not sent")


class FakeNeverFinalWebSocket:
    def __init__(self):
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def send(self, payload):
        self.sent.append(json.loads(payload))

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(5)
        raise StopAsyncIteration


def test_tts_gateway_synthesizes_audio_with_fake_websocket() -> None:
    captured = {}

    def fake_connect(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return FakeWebSocket(
            [
                {"code": 0, "data": {"status": 1, "audio": "YQ=="}},
                {"code": 0, "data": {"status": 2, "audio": "Yg=="}},
            ]
        )

    gateway = XfyunTTSGateway(
        credentials=XfyunCredentials(app_id="appid", api_key="apikey", api_secret="secret"),
        url="wss://tts-api.xfyun.cn/v2/tts",
        connect=fake_connect,
    )

    result = asyncio.run(gateway.synthesize("测试语音"))

    assert result.audio == b"ab"
    assert result.mime_type == "audio/mpeg"
    assert result.chunk_count == 2
    assert "authorization=" in captured["url"]
    assert captured["kwargs"]["open_timeout"] == 8


def test_super_tts_gateway_reports_licc_limit_without_switching_voice(monkeypatch) -> None:
    socket = FakeWebSocket([{"header": {"code": 11200, "message": "LiccCheck failed, unauthenticated, err: licc limit"}}])
    sent_frames = []

    def fake_connect(_url, **_kwargs):
        original_send = socket.send

        async def capture_send(payload):
            await original_send(payload)
            sent_frames.append(json.loads(payload))

        socket.send = capture_send
        return socket

    monkeypatch.setenv("XFYUN_TTS_VOICE", "x7_xinchang_pro")
    gateway = XfyunTTSGateway(
        credentials=XfyunCredentials(app_id="appid", api_key="apikey", api_secret="secret"),
        url="wss://cbm01.cn-huabei-1.xf-yun.com/v1/private/mcd9m97e6",
        connect=fake_connect,
    )

    with pytest.raises(XfyunGatewayError) as exc:
        asyncio.run(gateway.synthesize("测试语音"))

    assert exc.value.code == 11200
    assert "License 校验失败" in exc.value.message
    assert "未切换发音人" in exc.value.message
    assert [frame["parameter"]["tts"]["vcn"] for frame in sent_frames] == ["x7_xinchang_pro"]


def test_asr_gateway_recognizes_audio_with_fake_websocket() -> None:
    captured = {}

    def fake_connect(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return FakeWebSocket(
            [
                {"code": 0, "data": {"status": 1, "result": {"ws": [{"cw": [{"w": "你好"}]}]}}},
                {"code": 0, "data": {"status": 2, "result": {"ws": [{"cw": [{"w": "现场"}]}]}}},
            ]
        )

    gateway = XfyunASRGateway(
        credentials=XfyunCredentials(app_id="appid", api_key="apikey", api_secret="secret"),
        url="wss://iat-api.xfyun.cn/v2/iat",
        connect=fake_connect,
    )

    result = asyncio.run(gateway.recognize(b"pcm-audio", chunk_size=4))

    assert result.text == "你好现场"
    assert result.chunk_count == 3
    assert "authorization=" in captured["url"]
    assert captured["kwargs"]["open_timeout"] == 8


def test_asr_stream_session_sends_incremental_frames_and_callbacks() -> None:
    captured = {}
    callbacks = []

    def fake_connect(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        captured["websocket"] = FakeStreamingWebSocket(
            [
                {"code": 0, "data": {"status": 1, "result": {"ws": [{"cw": [{"w": "你好"}]}]}}},
                {"code": 0, "data": {"status": 2, "result": {"ws": [{"cw": [{"w": "现场"}]}]}}},
            ]
        )
        return captured["websocket"]

    async def run_stream():
        gateway = XfyunASRGateway(
            credentials=XfyunCredentials(app_id="appid", api_key="apikey", api_secret="secret"),
            url="wss://iat-api.xfyun.cn/v2/iat",
            connect=fake_connect,
        )
        session = await gateway.open_stream(
            on_partial=lambda text, latency_ms, chunk_count: callbacks.append(("partial", text, chunk_count)),
            on_final=lambda text, latency_ms, chunk_count: callbacks.append(("final", text, chunk_count)),
        )
        await session.send_audio(b"pcm-0")
        await session.send_audio(b"pcm-1")
        return await session.finish()

    result = asyncio.run(run_stream())

    assert result.text == "你好现场"
    assert result.chunk_count == 2
    assert callbacks[-1] == ("final", "你好现场", 2)
    statuses = [item["data"]["status"] for item in captured["websocket"].sent]
    assert statuses == [0, 1, 2]
    assert "authorization=" in captured["url"]
    assert captured["kwargs"]["open_timeout"] == 8


def test_asr_stream_session_times_out_after_end_frame_without_final() -> None:
    captured = {}
    errors = []

    def fake_connect(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        captured["websocket"] = FakeNeverFinalWebSocket()
        return captured["websocket"]

    async def run_stream():
        gateway = XfyunASRGateway(
            credentials=XfyunCredentials(app_id="appid", api_key="apikey", api_secret="secret"),
            url="wss://iat-api.xfyun.cn/v2/iat",
            connect=fake_connect,
        )
        session = await gateway.open_stream(
            final_timeout=0.01,
            open_timeout=2,
            close_timeout=1,
            on_error=lambda exc: errors.append(exc.message),
        )
        await session.send_audio(b"pcm-0")
        await session.finish()

    with pytest.raises(XfyunGatewayError) as exc:
        asyncio.run(run_stream())

    assert "final 响应超时" in exc.value.message
    assert "final 响应超时" in errors[-1]
    assert [item["data"]["status"] for item in captured["websocket"].sent] == [0, 2]
    assert captured["kwargs"]["open_timeout"] == 2
    assert captured["kwargs"]["close_timeout"] == 1


def test_tts_gateway_raises_on_xfyun_error() -> None:
    def fake_connect(_url, **_kwargs):
        return FakeWebSocket([{"code": 11200, "message": "auth failed", "data": {"status": 2}}])

    gateway = XfyunTTSGateway(
        credentials=XfyunCredentials(app_id="appid", api_key="apikey", api_secret="secret"),
        url="wss://tts-api.xfyun.cn/v2/tts",
        connect=fake_connect,
    )

    with pytest.raises(XfyunGatewayError) as exc:
        asyncio.run(gateway.synthesize("测试语音"))

    assert exc.value.code == 11200
    assert "auth failed" in exc.value.message
