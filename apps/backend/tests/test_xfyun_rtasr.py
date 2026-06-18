import asyncio
import json

from websockets.exceptions import ConnectionClosedOK

from app.services.xfyun_adapter import (
    XfyunCredentials,
    build_super_tts_frame,
    extract_super_tts_audio,
    super_tts_finished,
)
from app.services.xfyun_gateway import XfyunTTSGateway
from app.services.xfyun_rtasr import (
    XfyunRTASRGateway,
    build_rtasr_url,
    extract_rtasr_text,
    is_rtasr_url,
    rtasr_is_final,
)

CREDS = XfyunCredentials(app_id="app123", api_key="akid456", api_secret="secret789")


def _result(word: str, final: bool) -> dict:
    return {
        "msg_type": "result",
        "data": {"cn": {"st": {"type": "0" if final else "1", "rt": [{"ws": [{"cw": [{"w": word}]}]}]}}, "ls": final},
    }


class FakeRTASRWebSocket:
    def __init__(self, results):
        self.results = results
        self.sent_binary = 0
        self.sent_json = []
        self._started = False
        self._ended = False
        self._queue = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def send(self, payload):
        if isinstance(payload, (bytes, bytearray)):
            self.sent_binary += 1
        else:
            message = json.loads(payload)
            self.sent_json.append(message)
            if message.get("end"):
                self._ended = True
                self._queue = list(self.results)

    async def recv(self):
        if not self._started:
            self._started = True
            return json.dumps({"data": {"action": "started", "sessionId": "s"}, "msg_type": "action"})
        for _ in range(2000):
            if self._ended:
                break
            await asyncio.sleep(0.001)
        if self._queue:
            return json.dumps(self._queue.pop(0))
        raise ConnectionClosedOK(None, None)


def _connect_factory(ws):
    def connect(url, **_kw):
        ws.url = url
        return ws

    return connect


def test_build_rtasr_url_has_required_params_and_signature():
    url = build_rtasr_url("wss://office-api-ast-dx.iflyaisol.com/", CREDS)
    assert "/ast/communicate/v1?" in url
    for key in ("appId=", "accessKeyId=", "utc=", "uuid=", "audio_encode=pcm_s16le", "lang=", "samplerate=16000", "signature="):
        assert key in url


def test_is_rtasr_url_detection():
    assert is_rtasr_url("wss://office-api-ast-dx.iflyaisol.com/") is True
    assert is_rtasr_url("wss://iat-api.xfyun.cn/v2/iat") is False


def test_extract_rtasr_text_and_final_flag():
    final = _result("你好", True)
    inter = _result("正在", False)
    assert extract_rtasr_text(final) == "你好"
    assert rtasr_is_final(final) is True
    assert rtasr_is_final(inter) is False


def test_rtasr_gateway_recognize_collects_final_text():
    ws = FakeRTASRWebSocket([_result("正在识别", False), _result("人机辩论赛", True)])
    gateway = XfyunRTASRGateway(credentials=CREDS, url="wss://office-api-ast-dx.iflyaisol.com/", connect=_connect_factory(ws))
    # 2 frames of silence -> 2 binary sends, then the end JSON
    result = asyncio.run(gateway.recognize(b"\x00" * 2560, audio_format="audio/L16;rate=16000", encoding="raw"))
    assert result.text == "人机辩论赛"
    assert ws.sent_binary == 2
    assert ws.sent_json[-1] == {"end": True}


def test_rtasr_stream_session_reports_cumulative_text_across_segments():
    ws = FakeRTASRWebSocket(
        [
            _result("各位", False),
            _result("各位评委大家好", True),
            _result("我方", False),
            _result("我方认为编程思维更重要", True),
        ]
    )
    gateway = XfyunRTASRGateway(credentials=CREDS, url="wss://office-api-ast-dx.iflyaisol.com/", connect=_connect_factory(ws))
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
    # 历史 bug：每句 final 只上报自身文本，整段发言最终只剩最后一句（如只剩一个"嗯"）。
    # 修复后：流结束统一回调一次 final，且为累计全文。
    assert result.text == full
    assert finals == [full]
    assert partials[-1] == full
    assert "各位评委大家好我方" in partials


# --- super smart-tts schema ---

def test_build_super_tts_frame_uses_header_parameter_payload():
    frame = build_super_tts_frame("测试", CREDS, voice="x7_xinchang_pro")
    assert frame["header"]["app_id"] == "app123"
    assert frame["parameter"]["tts"]["vcn"] == "x7_xinchang_pro"
    assert frame["parameter"]["tts"]["audio"]["encoding"] == "lame"
    assert "text" in frame["payload"]["text"]


class FakeSuperTTSWebSocket:
    def __init__(self, messages):
        self.messages = messages
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def send(self, payload):
        self.sent.append(json.loads(payload))

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.messages:
            raise StopAsyncIteration
        return json.dumps(self.messages.pop(0))


def test_super_tts_gateway_decodes_payload_audio():
    import base64

    chunk = base64.b64encode(b"mp3-bytes").decode()
    messages = [
        {"header": {"code": 0, "status": 1}, "payload": {"audio": {"audio": chunk, "status": 1}}},
        {"header": {"code": 0, "status": 2}, "payload": {"audio": {"audio": "", "status": 2}}},
    ]
    ws = FakeSuperTTSWebSocket(messages)
    gateway = XfyunTTSGateway(credentials=CREDS, url="wss://cbm01.cn-huabei-1.xf-yun.com/v1/private/mcd9m97e6", connect=_connect_factory(ws))
    result = asyncio.run(gateway.synthesize("各位好"))
    assert result.audio == b"mp3-bytes"
    assert result.mime_type == "audio/mpeg"
    # super-tts schema sent header/parameter/payload, not common/business/data
    assert "header" in ws.sent[0] and "parameter" in ws.sent[0]


def test_super_tts_finished_detects_status_two():
    assert super_tts_finished({"header": {"status": 2}}) is True
    assert super_tts_finished({"header": {"status": 1}, "payload": {"audio": {"status": 1}}}) is False
    assert extract_super_tts_audio({"payload": {"audio": {"audio": ""}}}) == b""
