from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional


class LiveKitConfigError(Exception):
    pass


@dataclass(frozen=True)
class LiveKitTokenRequest:
    match_id: str
    role: str
    identity: str
    name: str = ""
    speaker_id: str = ""
    ttl_seconds: int = 3600


def livekit_room_name(match_id: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in str(match_id or "current"))
    return f"debate-{safe or 'current'}"


def livekit_status() -> Dict[str, Any]:
    enabled = _env_bool("PHDEBATE_LIVEKIT_ENABLED", False)
    url = os.getenv("LIVEKIT_URL", "").strip()
    api_key = os.getenv("LIVEKIT_API_KEY", "").strip()
    api_secret = os.getenv("LIVEKIT_API_SECRET", "").strip()
    return {
        "enabled": enabled,
        "configured": bool(url and api_key and api_secret),
        "url": url,
        "has_api_key": bool(api_key),
        "has_api_secret": bool(api_secret),
        "mode": os.getenv("PHDEBATE_LIVEKIT_MODE", "cloud").strip() or "cloud",
    }


def issue_livekit_token(request: LiveKitTokenRequest) -> Dict[str, Any]:
    status = livekit_status()
    if not status["enabled"]:
        raise LiveKitConfigError("LiveKit 未启用，请设置 PHDEBATE_LIVEKIT_ENABLED=1。")
    if not status["configured"]:
        raise LiveKitConfigError("LiveKit 缺少 LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET。")

    room = livekit_room_name(request.match_id)
    now = int(time.time())
    ttl = max(60, min(int(request.ttl_seconds or 3600), 24 * 3600))
    can_publish = request.role in {"speaker", "voice-agent", "agent"}
    can_subscribe = request.role in {"screen", "host", "admin", "voice-agent", "agent"}
    payload: Dict[str, Any] = {
        "iss": os.getenv("LIVEKIT_API_KEY", "").strip(),
        "sub": request.identity,
        "nbf": now - 5,
        "exp": now + ttl,
        "name": request.name or request.identity,
        "video": {
            "roomJoin": True,
            "room": room,
            "canPublish": can_publish,
            "canSubscribe": can_subscribe,
            "canPublishData": True,
        },
        "metadata": json.dumps(
            {
                "match_id": request.match_id,
                "role": request.role,
                "speaker_id": request.speaker_id,
            },
            ensure_ascii=False,
        ),
    }
    return {
        "enabled": True,
        "configured": True,
        "url": status["url"],
        "room": room,
        "identity": request.identity,
        "role": request.role,
        "token": _jwt_hs256(payload, os.getenv("LIVEKIT_API_SECRET", "").strip()),
        "expires_at": payload["exp"],
    }


def voice_agent_identity(match_id: str) -> str:
    digest = hashlib.sha1(str(match_id).encode("utf-8")).hexdigest()[:10]
    return f"voice-agent-{digest}"


def _jwt_hs256(payload: Dict[str, Any], secret: str) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    signing_input = ".".join([_b64_json(header), _b64_json(payload)])
    sig = hmac.new(secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).digest()
    return f"{signing_input}.{_b64(sig)}"


def _b64_json(data: Dict[str, Any]) -> str:
    return _b64(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default
