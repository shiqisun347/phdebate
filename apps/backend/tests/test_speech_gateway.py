import asyncio
import base64
import io
import json
import wave

from app.services.speech_gateway import (
    ALICLOUD_ASR_AUDIO_FRAME_BYTES,
    AlicloudASRGateway,
    AlicloudTTSGateway,
    FunASRASRGateway,
    LocalQwenTTSGateway,
    _local_qwen_tts_extra_payload,
    _local_qwen_tts_payload_variants,
    _prepare_local_qwen_asr_audio,
    normalize_tts_text,
)


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


def test_local_qwen_asr_wraps_l16_pcm_as_wav() -> None:
    pcm = (b"\x00\x00\x01\x00" * 160) + b"\xff"

    filename, payload, mime_type = _prepare_local_qwen_asr_audio(
        pcm,
        {"audio_format": "audio/L16;rate=16000", "encoding": "raw"},
    )

    assert filename == "audio.wav"
    assert mime_type == "audio/wav"
    assert payload.startswith(b"RIFF")
    with wave.open(io.BytesIO(payload), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == 16000
        assert wav.readframes(wav.getnframes()) == pcm[:-1]


class FakeFunASRWebSocket:
    def __init__(self):
        self.sent_text = []
        self.sent_binary = []
        self.messages = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        self.closed = True
        return None

    async def send(self, payload):
        if isinstance(payload, (bytes, bytearray)):
            self.sent_binary.append(bytes(payload))
            if len(self.sent_binary) == 1:
                self.messages.append({"sentences": [], "partial": "各位", "is_final": False})
            return
        self.sent_text.append(payload)
        if payload == "START":
            self.messages.append({"event": "started"})
        elif payload == "STOP":
            self.messages.extend(
                [
                    {"sentences": [{"text": "各位评委", "start": 0, "end": 1200}], "partial": "我方", "is_final": False},
                    {
                        "sentences": [
                            {"text": "各位评委", "start": 0, "end": 1200},
                            {"text": "我方认为实时转写可靠", "start": 1200, "end": 3200},
                        ],
                        "is_final": True,
                    },
                    {"event": "stopped"},
                ]
            )

    async def recv(self):
        for _ in range(2000):
            if self.messages:
                return json.dumps(self.messages.pop(0))
            await asyncio.sleep(0.001)
        raise AssertionError("FunASR fake websocket timed out")


def test_funasr_nano_stream_reports_realtime_partials_and_final_sentences() -> None:
    socket = FakeFunASRWebSocket()

    def fake_connect(_url, **_kwargs):
        return socket

    gateway = FunASRASRGateway(
        section={
            "endpoint": "ws://127.0.0.1:10095",
            "settings": {
                "language": "中文",
                "sample_rate": 16000,
                "frame_ms": 100,
                "final_timeout": 2,
            },
        },
        connect=fake_connect,
    )
    partials = []
    finals = []

    async def run_stream():
        session = await gateway.open_stream(
            on_partial=lambda text, latency, chunk: partials.append(text),
            on_final=lambda text, latency, chunk: finals.append(text),
            realtime_pacing=False,
        )
        await session.send_audio(b"\x00" * 4000)
        return await session.finish()

    result = asyncio.run(run_stream())

    assert socket.sent_text[:2] == ["START", "LANGUAGE:中文"]
    assert socket.sent_text[-1] == "STOP"
    assert len(socket.sent_binary) == 2
    assert partials[0] == "各位"
    assert partials[-1] == "各位评委我方认为实时转写可靠"
    assert finals == ["各位评委我方认为实时转写可靠"]
    assert result.text == "各位评委我方认为实时转写可靠"


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


def test_local_qwen_tts_consistency_seed_and_formal_params_are_sent_by_default() -> None:
    gateway = LocalQwenTTSGateway(
        section={
            "endpoint": "http://127.0.0.1:12302",
            "settings": {
                "model": "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
                "response_format": "mp3",
                "sample_rate": 24000,
                "speech_rate": 1.0,
                "volume": 70,
                "pitch_rate": 1.0,
            },
        },
        preset={"voice": "dylan", "volume": 72, "pitch_rate": 1.0},
    )

    session = gateway._session_options({"seed": 12345, "volume": 86, "pitch_rate": 1.05})

    extra = _local_qwen_tts_extra_payload(session)
    assert extra["seed"] == 12345
    assert extra["volume"] == 86
    assert extra["pitch_rate"] == 1.05
    variants = _local_qwen_tts_payload_variants({"input": "测试", "speed": 1.12}, session)
    assert variants[0]["volume"] == 86
    assert variants[0]["pitch_rate"] == 1.05
    assert variants[1] == {"input": "测试", "speed": 1.12, "seed": 12345}
    assert variants[-1] == {"input": "测试", "speed": 1.12}


def test_local_qwen_tts_payload_variants_keep_seed_without_losing_compatibility() -> None:
    base = {
        "model": "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
        "input": "测试",
        "voice": "dylan",
        "response_format": "mp3",
        "sample_rate": 24000,
        "speed": 1.0,
    }
    session = {"seed": 12345, "temperature": 0.25, "top_p": 0.8}

    variants = _local_qwen_tts_payload_variants(base, session)

    assert variants[0]["seed"] == 12345
    assert variants[0]["temperature"] == 0.25
    assert variants[0]["top_p"] == 0.8
    assert variants[1] == {**base, "seed": 12345}
    assert variants[2] == base
