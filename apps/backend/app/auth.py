from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from fastapi import HTTPException, Request, WebSocket


@dataclass(frozen=True)
class Principal:
    role: str
    actor_type: str
    actor_id: Optional[str] = None


def auth_required() -> bool:
    override = os.getenv("PHDEBATE_AUTH_REQUIRED", "").strip().lower()
    if override in {"1", "true", "yes", "on"}:
        return True
    if override in {"0", "false", "no", "off"}:
        return False
    return os.getenv("PHDEBATE_ENV", "development").strip().lower() in {"production", "prod"}


def require_read_access(request: Request) -> Principal:
    return _require_roles(request, {"admin", "host", "screen", "speaker"})


def require_admin(request: Request) -> Principal:
    return _require_roles(request, {"admin"})


def require_host(request: Request) -> Principal:
    return _require_roles(request, {"admin", "host"})


def require_speaker_or_host(request: Request, speaker_id: str) -> Principal:
    principal = _require_roles(request, {"admin", "host", "speaker"}, speaker_id=speaker_id)
    if principal.role == "speaker" and principal.actor_id != speaker_id:
        raise _forbidden("该辩手 token 不能操作其他辩手。")
    return principal


def authorize_speaker_or_host(request: Request, speaker_id: str) -> Principal:
    return require_speaker_or_host(request, speaker_id)


def authorize_websocket(websocket: WebSocket, channel: str, speaker_id: Optional[str]) -> Optional[Principal]:
    if not auth_required():
        return Principal(role=channel, actor_type=channel, actor_id=speaker_id)

    token = _token_from_mapping(websocket.headers, websocket.query_params)
    principal = _principal_for_token(token, speaker_id=speaker_id)
    if not principal:
        return None
    if channel == "admin" and principal.role not in {"admin", "host"}:
        return None
    if channel == "screen" and principal.role not in {"admin", "host", "screen"}:
        return None
    if channel == "speaker":
        if not speaker_id or principal.role not in {"admin", "host", "speaker"}:
            return None
        if principal.role == "speaker" and principal.actor_id != speaker_id:
            return None
    return principal


def _require_roles(request: Request, roles: set[str], speaker_id: Optional[str] = None) -> Principal:
    if not auth_required():
        return Principal(role="dev", actor_type="dev")
    principal = _principal_for_token(_token_from_request(request), speaker_id=speaker_id)
    if not principal:
        raise _unauthorized()
    if principal.role not in roles:
        raise _forbidden("当前 token 权限不足。")
    return principal


def _principal_for_token(token: Optional[str], speaker_id: Optional[str] = None) -> Optional[Principal]:
    if not token:
        return None

    if _matches_any(token, _tokens_from_env("PHDEBATE_ADMIN_TOKEN", "PHDEBATE_ADMIN_PASSWORD")):
        return Principal(role="admin", actor_type="admin")
    if _matches_any_hash(token, _hashes_from_token_file("admin_hashes", "admin")):
        return Principal(role="admin", actor_type="admin")
    if _matches_any(token, _tokens_from_env("PHDEBATE_HOST_TOKEN", "PHDEBATE_HOST_PASSWORD")):
        return Principal(role="host", actor_type="host")
    if _matches_any_hash(token, _hashes_from_token_file("host_hashes", "host")):
        return Principal(role="host", actor_type="host")
    if _matches_any(token, _tokens_from_env("PHDEBATE_SCREEN_TOKEN")):
        return Principal(role="screen", actor_type="screen")
    if _matches_any_hash(token, _hashes_from_token_file("screen_hashes", "screen")):
        return Principal(role="screen", actor_type="screen")

    speaker_tokens = _speaker_tokens()
    for configured_speaker_id, configured_token in speaker_tokens.items():
        if _constant_time_equal(token, configured_token):
            return Principal(role="speaker", actor_type="speaker", actor_id=configured_speaker_id)
    for configured_speaker_id, configured_hashes in _speaker_hashes_from_token_file().items():
        if _matches_any_hash(token, configured_hashes):
            return Principal(role="speaker", actor_type="speaker", actor_id=configured_speaker_id)

    shared = _tokens_from_env("PHDEBATE_SPEAKER_TOKEN")
    if speaker_id and _matches_any(token, shared):
        return Principal(role="speaker", actor_type="speaker", actor_id=speaker_id)
    if speaker_id and _matches_any_hash(token, _hashes_from_token_file("speaker_shared_hashes", "speaker_shared")):
        return Principal(role="speaker", actor_type="speaker", actor_id=speaker_id)
    return None


def _token_from_request(request: Request) -> Optional[str]:
    return _token_from_mapping(request.headers, request.query_params)


def _token_from_mapping(headers, query_params) -> Optional[str]:
    authorization = headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip() or None
    for key in ("token", "auth_token", "screen_token", "speaker_token"):
        value = query_params.get(key)
        if value:
            return value
    return None


def _tokens_from_env(*names: str) -> Sequence[str]:
    tokens = []
    for name in names:
        value = os.getenv(name, "")
        for item in value.split(","):
            token = item.strip()
            if token:
                tokens.append(token)
    return tokens


def _speaker_tokens() -> Dict[str, str]:
    raw = os.getenv("PHDEBATE_SPEAKER_TOKENS", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return {str(key): str(value) for key, value in parsed.items() if value}
    except json.JSONDecodeError:
        pass

    tokens: Dict[str, str] = {}
    for pair in raw.split(","):
        if ":" not in pair:
            continue
        speaker_id, token = pair.split(":", 1)
        speaker_id = speaker_id.strip()
        token = token.strip()
        if speaker_id and token:
            tokens[speaker_id] = token
    return tokens


def _hashes_from_token_file(*keys: str) -> Sequence[str]:
    config = _token_file_config()
    hashes = []
    for key in keys:
        hashes.extend(_hash_values(config.get(key)))
    return hashes


def _speaker_hashes_from_token_file() -> Dict[str, Sequence[str]]:
    config = _token_file_config()
    raw = config.get("speaker_hashes") or config.get("speakers") or {}
    if not isinstance(raw, dict):
        return {}
    result: Dict[str, Sequence[str]] = {}
    for speaker_id, value in raw.items():
        hashes = _hash_values(value)
        if hashes:
            result[str(speaker_id)] = hashes
    return result


def _token_file_config() -> Dict[str, Any]:
    path = _token_file_path()
    if not path or not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _token_file_path() -> Optional[Path]:
    raw = os.getenv("PHDEBATE_TOKEN_FILE", "").strip()
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_absolute() else _project_root() / path


def _hash_values(value: Any) -> Sequence[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [_normalize_hash(value)] if _normalize_hash(value) else []
    if isinstance(value, list):
        hashes = [_normalize_hash(item) for item in value if isinstance(item, str)]
        return [item for item in hashes if item]
    return []


def _normalize_hash(value: str) -> str:
    normalized = value.strip().lower()
    if normalized.startswith("sha256:"):
        normalized = normalized.removeprefix("sha256:")
    return normalized if len(normalized) == 64 and all(char in "0123456789abcdef" for char in normalized) else ""


def _matches_any(token: str, candidates: Sequence[str]) -> bool:
    return any(_constant_time_equal(token, candidate) for candidate in candidates)


def _matches_any_hash(token: str, candidates: Sequence[str]) -> bool:
    if not candidates:
        return False
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return any(_constant_time_equal(digest, candidate) for candidate in candidates)


def _constant_time_equal(left: str, right: str) -> bool:
    if len(left) != len(right):
        return False
    result = 0
    for a, b in zip(left.encode("utf-8"), right.encode("utf-8")):
        result |= a ^ b
    return result == 0


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail={
            "code": "unauthorized",
            "message": "需要有效的访问 token。",
            "details": {},
        },
    )


def _forbidden(message: str) -> HTTPException:
    return HTTPException(
        status_code=403,
        detail={
            "code": "forbidden",
            "message": message,
            "details": {},
        },
    )
