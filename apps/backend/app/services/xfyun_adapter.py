from __future__ import annotations

import base64
import hashlib
import hmac
import os
from dataclasses import dataclass
from email.utils import formatdate
from time import time
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse


@dataclass(frozen=True)
class XfyunCredentials:
    app_id: str
    api_key: str
    api_secret: str


def credentials_from_env() -> Optional[XfyunCredentials]:
    app_id = os.getenv("XFYUN_APP_ID", "").strip()
    api_key = os.getenv("XFYUN_API_KEY", "").strip()
    api_secret = os.getenv("XFYUN_API_SECRET", "").strip()
    if not (app_id and api_key and api_secret):
        return None
    return XfyunCredentials(app_id=app_id, api_key=api_key, api_secret=api_secret)


def xfyun_signed_url(url: str, credentials: XfyunCredentials, date: Optional[str] = None) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"ws", "wss"} or not parsed.netloc:
        raise ValueError("XFYUN WebAPI URL must be ws:// or wss:// with a host")
    request_path = parsed.path or "/"
    host = parsed.netloc
    request_date = date or formatdate(timeval=time(), usegmt=True)
    request_line = f"GET {request_path} HTTP/1.1"
    signature_origin = f"host: {host}\ndate: {request_date}\n{request_line}"
    signature = base64.b64encode(
        hmac.new(
            credentials.api_secret.encode("utf-8"),
            signature_origin.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
    ).decode("utf-8")
    authorization_origin = (
        f'api_key="{credentials.api_key}", algorithm="hmac-sha256", '
        f'headers="host date request-line", signature="{signature}"'
    )
    authorization = base64.b64encode(authorization_origin.encode("utf-8")).decode("utf-8")
    query = urlencode({"authorization": authorization, "date": request_date, "host": host})
    return urlunparse((parsed.scheme, parsed.netloc, request_path, "", query, ""))


def redact_xfyun_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    if not parsed.query:
        return url
    query = parse_qs(parsed.query)
    redacted = {}
    for key, values in query.items():
        redacted[key] = ["..." if key in {"authorization", "api_key", "api_secret"} else values[0]]
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", urlencode(redacted, doseq=True), ""))


def xfyun_auth_preview(url: str) -> Dict[str, str]:
    parsed = urlparse(url)
    return {
        "host": parsed.netloc,
        "request_line": f"GET {parsed.path or '/'} HTTP/1.1",
        "auth_algorithm": "hmac-sha256",
    }


def build_iat_frame(
    audio: bytes,
    credentials: XfyunCredentials,
    *,
    status: int = 0,
    audio_format: str = "audio/L16;rate=16000",
    encoding: str = "raw",
    language: Optional[str] = None,
    domain: Optional[str] = None,
    accent: Optional[str] = None,
) -> Dict[str, Any]:
    frame: Dict[str, Any] = {
        "data": {
            "status": status,
            "format": audio_format,
            "encoding": encoding,
            "audio": base64.b64encode(audio).decode("utf-8"),
        }
    }
    if status == 0:
        frame["common"] = {"app_id": credentials.app_id}
        frame["business"] = {
            "language": language or os.getenv("XFYUN_ASR_LANGUAGE", "zh_cn"),
            "domain": domain or os.getenv("XFYUN_ASR_DOMAIN", "iat"),
            "accent": accent or os.getenv("XFYUN_ASR_ACCENT", "mandarin"),
        }
        optional_int_env(frame["business"], "eos", "XFYUN_ASR_EOS")
        optional_int_env(frame["business"], "ptt", "XFYUN_ASR_PTT")
        optional_str_env(frame["business"], "dwa", "XFYUN_ASR_DWA")
    return frame


def build_iat_end_frame() -> Dict[str, Any]:
    return {"data": {"status": 2}}


def build_tts_frame(
    text: str,
    credentials: XfyunCredentials,
    *,
    voice: Optional[str] = None,
    audio_encoding: Optional[str] = None,
    sample_rate: Optional[str] = None,
    speed: Optional[int] = None,
    volume: Optional[int] = None,
    pitch: Optional[int] = None,
) -> Dict[str, Any]:
    business: Dict[str, Any] = {
        "aue": audio_encoding or os.getenv("XFYUN_TTS_AUE", "lame"),
        "auf": sample_rate or os.getenv("XFYUN_TTS_AUF", "audio/L16;rate=16000"),
        "vcn": voice or os.getenv("XFYUN_TTS_VOICE", "xiaoyan"),
        "speed": speed if speed is not None else int(os.getenv("XFYUN_TTS_SPEED", "50")),
        "volume": volume if volume is not None else int(os.getenv("XFYUN_TTS_VOLUME", "70")),
        "pitch": pitch if pitch is not None else int(os.getenv("XFYUN_TTS_PITCH", "50")),
        "tte": os.getenv("XFYUN_TTS_TTE", "UTF8"),
    }
    if business["aue"] == "lame":
        business["sfl"] = 1
    return {
        "common": {"app_id": credentials.app_id},
        "business": business,
        "data": {
            "status": 2,
            "text": base64.b64encode(text.encode("utf-8")).decode("utf-8"),
        },
    }


def extract_iat_text(message: Dict[str, Any]) -> str:
    result = ((message.get("data") or {}).get("result") or {})
    words: List[str] = []
    for item in result.get("ws") or []:
        candidates = item.get("cw") or []
        if candidates:
            words.append(str(candidates[0].get("w") or ""))
    return "".join(words)


def iat_finished(message: Dict[str, Any]) -> bool:
    return ((message.get("data") or {}).get("status")) == 2


def extract_tts_audio(message: Dict[str, Any]) -> bytes:
    audio = ((message.get("data") or {}).get("audio") or "")
    return base64.b64decode(audio) if audio else b""


def tts_finished(message: Dict[str, Any]) -> bool:
    return ((message.get("data") or {}).get("status")) == 2


# --- 超拟人 / super smart-tts（wss://.../v1/private/...）schema ---
# 该接口与老版 TTS 不同：使用 header / parameter / payload 三段式，
# 响应音频在 payload.audio.audio（base64）。参见 需求 2.md 与讯飞 super smart-tts 文档。

def build_super_tts_frame(
    text: str,
    credentials: XfyunCredentials,
    *,
    voice: Optional[str] = None,
    audio_encoding: Optional[str] = None,
    sample_rate: Optional[int] = None,
    speed: Optional[int] = None,
    volume: Optional[int] = None,
    pitch: Optional[int] = None,
) -> Dict[str, Any]:
    encoding = audio_encoding or os.getenv("XFYUN_TTS_AUE", "lame")
    rate = sample_rate if sample_rate is not None else int(os.getenv("XFYUN_TTS_SAMPLE_RATE", "24000"))
    return {
        "header": {"app_id": credentials.app_id, "status": 2},
        "parameter": {
            "tts": {
                # 该试用 app 的免费发音人：x6_lingfeiyi_pro / x6_lingxiaoxuan_pro /
                # x6_lingfeibo_pro / x6_lingxiaoyue_pro（详见 docs/14）。其它需授权，否则 licc limit。
                "vcn": voice or os.getenv("XFYUN_TTS_VOICE", "x6_lingfeiyi_pro"),
                "speed": speed if speed is not None else int(os.getenv("XFYUN_TTS_SPEED", "50")),
                "volume": volume if volume is not None else int(os.getenv("XFYUN_TTS_VOLUME", "50")),
                "pitch": pitch if pitch is not None else int(os.getenv("XFYUN_TTS_PITCH", "50")),
                "audio": {
                    "encoding": encoding,
                    "sample_rate": rate,
                    "channels": 1,
                    "bit_depth": 16,
                    "frame_size": 0,
                },
            }
        },
        "payload": {
            "text": {
                "encoding": "utf8",
                "compress": "raw",
                "format": "plain",
                "status": 2,
                "seq": 0,
                "text": base64.b64encode(text.encode("utf-8")).decode("utf-8"),
            }
        },
    }


def extract_super_tts_audio(message: Dict[str, Any]) -> bytes:
    audio = (((message.get("payload") or {}).get("audio") or {}).get("audio") or "")
    return base64.b64decode(audio) if audio else b""


def super_tts_error(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    header = message.get("header") or {}
    code = header.get("code")
    if code not in (None, 0):
        return {"code": code, "message": header.get("message") or "super-tts error"}
    return None


def super_tts_finished(message: Dict[str, Any]) -> bool:
    header = message.get("header") or {}
    if header.get("status") == 2:
        return True
    audio = ((message.get("payload") or {}).get("audio") or {})
    return audio.get("status") == 2


def optional_int_env(target: Dict[str, Any], key: str, env_name: str) -> None:
    value = os.getenv(env_name, "").strip()
    if value:
        target[key] = int(value)


def optional_str_env(target: Dict[str, Any], key: str, env_name: str) -> None:
    value = os.getenv(env_name, "").strip()
    if value:
        target[key] = value
