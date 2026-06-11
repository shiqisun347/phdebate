import base64
from urllib.parse import parse_qs, urlparse

from app.services.xfyun_adapter import (
    XfyunCredentials,
    build_iat_end_frame,
    build_iat_frame,
    build_tts_frame,
    extract_iat_text,
    extract_tts_audio,
    redact_xfyun_url,
    tts_finished,
    xfyun_auth_preview,
    xfyun_signed_url,
)


def test_xfyun_signed_url_contains_required_query_without_secret() -> None:
    credentials = XfyunCredentials(app_id="appid", api_key="apikey", api_secret="secret")

    signed = xfyun_signed_url("wss://iat-api.xfyun.cn/v2/iat", credentials, date="Wed, 10 Jun 2026 20:00:00 GMT")

    parsed = urlparse(signed)
    query = parse_qs(parsed.query)
    assert parsed.scheme == "wss"
    assert parsed.netloc == "iat-api.xfyun.cn"
    assert query["date"] == ["Wed, 10 Jun 2026 20:00:00 GMT"]
    assert query["host"] == ["iat-api.xfyun.cn"]
    assert "authorization" in query
    decoded = base64.b64decode(query["authorization"][0]).decode("utf-8")
    assert 'api_key="apikey"' in decoded
    assert 'algorithm="hmac-sha256"' in decoded
    assert 'headers="host date request-line"' in decoded
    assert "secret" not in signed


def test_xfyun_payload_builders_and_extractors(monkeypatch) -> None:
    credentials = XfyunCredentials(app_id="appid", api_key="apikey", api_secret="secret")
    monkeypatch.setenv("XFYUN_TTS_VOICE", "aisjiuxu")

    iat = build_iat_frame(b"audio-bytes", credentials)
    assert iat["common"]["app_id"] == "appid"
    assert iat["business"]["language"] == "zh_cn"
    assert iat["data"]["status"] == 0
    assert base64.b64decode(iat["data"]["audio"]) == b"audio-bytes"
    assert build_iat_end_frame() == {"data": {"status": 2}}

    tts = build_tts_frame("你好，辩论现场。", credentials)
    assert tts["common"]["app_id"] == "appid"
    assert tts["business"]["vcn"] == "aisjiuxu"
    assert base64.b64decode(tts["data"]["text"]).decode("utf-8") == "你好，辩论现场。"

    asr_message = {"data": {"result": {"ws": [{"cw": [{"w": "你好"}]}, {"cw": [{"w": "现场"}]}]}}}
    assert extract_iat_text(asr_message) == "你好现场"

    tts_message = {"data": {"status": 2, "audio": base64.b64encode(b"mp3").decode("utf-8")}}
    assert extract_tts_audio(tts_message) == b"mp3"
    assert tts_finished(tts_message) is True


def test_xfyun_url_redaction_and_preview() -> None:
    url = "wss://tts-api.xfyun.cn/v2/tts?authorization=abc&date=today&host=tts-api.xfyun.cn"

    redacted = redact_xfyun_url(url)
    assert "authorization=..." in redacted
    assert "abc" not in redacted

    assert xfyun_auth_preview("wss://tts-api.xfyun.cn/v2/tts") == {
        "host": "tts-api.xfyun.cn",
        "request_line": "GET /v2/tts HTTP/1.1",
        "auth_algorithm": "hmac-sha256",
    }
