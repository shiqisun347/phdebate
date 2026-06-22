import asyncio
import base64
import json

from app.services.speech_gateway import ALICLOUD_ASR_AUDIO_FRAME_BYTES, AlicloudASRGateway, AlicloudTTSGateway, normalize_tts_text


class FakeAlicloudASRWebSocket:
    """模拟 server-VAD 把一段发言切成多句：每句一个 .completed。"""

    def __init__(self, messages):
        self.sent = []
        self.messages = list(messages)

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


def test_alicloud_asr_stream_reports_cumulative_text_across_vad_segments() -> None:
    socket = FakeAlicloudASRWebSocket(
        [
            {"type": "conversation.item.input_audio_transcription.text", "text": "各位"},
            {"type": "conversation.item.input_audio_transcription.completed", "transcript": "各位评委大家好"},
            {"type": "conversation.item.input_audio_transcription.text", "text": "我方"},
            {"type": "conversation.item.input_audio_transcription.completed", "transcript": "我方认为编程思维更重要"},
            {"type": "session.finished"},
        ]
    )

    def fake_connect(_url, additional_headers=None, **_kwargs):
        return socket

    gateway = AlicloudASRGateway(
        section={
            "endpoint": "wss://dashscope.aliyuncs.com/api-ws/v1/realtime",
            "settings": {"model": "qwen3-asr-flash-realtime"},
            "secrets": {"alicloud": {"api_key": "test-key"}},
        },
        connect=fake_connect,
    )

    partials = []
    finals = []

    async def run_stream():
        session = await gateway.open_stream(
            on_partial=lambda text, latency, chunk: partials.append(text),
            on_final=lambda text, latency, chunk: finals.append(text),
        )
        await session.send_audio(b"\x00" * 1280)
        return await session.finish()

    result = asyncio.run(run_stream())

    full = "各位评委大家好我方认为编程思维更重要"
    # 历史 bug：每个 .completed 只上报当前分句，转写最终只剩最后一段。修复后：
    # 流结束统一回调一次 final，且为累计全文。
    assert result.text == full
    assert finals == [full]
    # partial 始终是"已定稿全文 + 当前在写分段"，绝不回退成单段。
    assert partials[-1] == full
    assert "各位评委大家好我方" in partials


class WaitingAlicloudASRWebSocket(FakeAlicloudASRWebSocket):
    def __init__(self):
        super().__init__([])
        self.finished_sent = False

    async def __anext__(self):
        if self.finished_sent:
            raise StopAsyncIteration
        # 发送侧按服务商速率上限(~1.5MB/s)节流，会做真实 sleep；用真实时间等待而非纯 sleep(0) 让步。
        for _ in range(2000):
            if any(message.get("type") == "session.finish" for message in self.sent):
                self.finished_sent = True
                return json.dumps({"type": "session.finished"})
            await asyncio.sleep(0.005)
        raise AssertionError("session.finish was not sent")


def test_alicloud_asr_stream_splits_large_audio_frames() -> None:
    socket = WaitingAlicloudASRWebSocket()

    def fake_connect(_url, additional_headers=None, **_kwargs):
        return socket

    gateway = AlicloudASRGateway(
        section={
            "endpoint": "wss://dashscope.aliyuncs.com/api-ws/v1/realtime",
            "settings": {"model": "qwen3-asr-flash-realtime"},
            "secrets": {"alicloud": {"api_key": "test-key"}},
        },
        connect=fake_connect,
    )
    audio = b"x" * (ALICLOUD_ASR_AUDIO_FRAME_BYTES * 2 + 123)

    result = asyncio.run(gateway.recognize(audio))

    audio_messages = [message for message in socket.sent if message.get("type") == "input_audio_buffer.append"]
    decoded_frames = [base64.b64decode(message["audio"]) for message in audio_messages]
    assert len(decoded_frames) == 3
    assert b"".join(decoded_frames) == audio
    assert all(len(frame) <= ALICLOUD_ASR_AUDIO_FRAME_BYTES for frame in decoded_frames)
    assert result.chunk_count == len(decoded_frames)


class FakeAlicloudWebSocket:
    def __init__(self):
        self.sent = []
        self.messages = [
            {"type": "response.audio.delta", "delta": "YQ=="},
            {"type": "session.finished"},
        ]

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


def test_normalize_tts_text_removes_markdown_and_rewrites_terms() -> None:
    text = "**\n\n第二，**AI/TTS 的 API Key 见 https://example.com —— `Qwen-ASR`。"

    result = normalize_tts_text(text)

    assert "**" not in result
    assert "https://" not in result
    assert "`" not in result
    assert result == "第二，人工智能、语音合成的接口密钥见链接，千问语音识别。"


def test_alicloud_tts_sends_clean_text_and_omits_instructions_for_flash_model() -> None:
    socket = FakeAlicloudWebSocket()

    def fake_connect(_url, additional_headers=None, **_kwargs):
        assert additional_headers["Authorization"] == "Bearer test-key"
        return socket

    gateway = AlicloudTTSGateway(
        section={
            "endpoint": "wss://dashscope.aliyuncs.com/api-ws/v1/realtime",
            "settings": {"model": "qwen3-tts-flash-realtime", "response_format": "mp3"},
            "secrets": {"alicloud": {"api_key": "test-key"}},
        },
        preset={
            "voice": "Neil",
            "instructions": "语气夸张、有明显情绪。",
            "speech_rate": 1.0,
            "volume": 72,
            "pitch_rate": 1.0,
        },
        connect=fake_connect,
    )

    result = asyncio.run(gateway.synthesize("**AI** —— `Qwen-TTS`"))

    assert result.audio == b"a"
    session_update = socket.sent[0]["session"]
    assert session_update["voice"] == "Neil"
    assert "instructions" not in session_update
    assert socket.sent[1]["text"] == "人工智能，千问语音合成"
