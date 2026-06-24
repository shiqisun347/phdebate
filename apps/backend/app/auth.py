from __future__ import annotations

import hashlib
import json
import os
import time
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
    runtime = _runtime_auth_config()
    if isinstance(runtime.get("auth_required"), bool):
        return bool(runtime["auth_required"])
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
    if channel == "host" and principal.role not in {"admin", "host"}:
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
    runtime = _runtime_auth_config().get("token_hashes")
    merged: Dict[str, Any] = runtime if isinstance(runtime, dict) else {}
    path = _token_file_path()
    if not path or not path.exists():
        return merged
    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError):
        return merged
    if isinstance(data, dict):
        merged = {**data, **merged}
    return merged


def _token_file_path() -> Optional[Path]:
    raw = os.getenv("PHDEBATE_TOKEN_FILE", "").strip()
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_absolute() else _project_root() / path


def runtime_auth_status() -> Dict[str, Any]:
    runtime = _runtime_auth_config()
    runtime_hashes = runtime.get("token_hashes") if isinstance(runtime.get("token_hashes"), dict) else {}
    configured_path = _token_file_path()
    env_default = _env_default_auth_required()
    return {
        "auth_required": auth_required(),
        "runtime_configured": isinstance(runtime.get("auth_required"), bool),
        "env_default_auth_required": env_default,
        "runtime_path": str(_runtime_auth_path()),
        "token_file_path": str(configured_path) if configured_path else None,
        "roles": ["admin", "host", "screen", "speaker"],
        "token_sources": {
            "admin": _source_summary(runtime_hashes, "admin_hashes", "admin", "PHDEBATE_ADMIN_TOKEN", "PHDEBATE_ADMIN_PASSWORD"),
            "host": _source_summary(runtime_hashes, "host_hashes", "host", "PHDEBATE_HOST_TOKEN", "PHDEBATE_HOST_PASSWORD"),
            "screen": _source_summary(runtime_hashes, "screen_hashes", "screen", "PHDEBATE_SCREEN_TOKEN"),
            "speaker_shared": _source_summary(runtime_hashes, "speaker_shared_hashes", "speaker_shared", "PHDEBATE_SPEAKER_TOKEN"),
            "speaker_specific": {
                "runtime_count": len((runtime_hashes.get("speaker_hashes") or {}) if isinstance(runtime_hashes.get("speaker_hashes"), dict) else {}),
                "env_count": len(_speaker_tokens()),
                "file_count": len(_speaker_hashes_from_config_file_only()),
            },
        },
        "updated_at": runtime.get("updated_at"),
        "updated_by": runtime.get("updated_by"),
    }


def update_runtime_auth_config(auth_required_value: bool, token_hashes: Optional[Dict[str, Any]] = None, updated_by: str = "admin") -> Dict[str, Any]:
    current = _runtime_auth_config()
    next_hashes = current.get("token_hashes") if isinstance(current.get("token_hashes"), dict) else {}
    if token_hashes is not None:
        next_hashes = _normalize_token_hash_config(token_hashes)
    if auth_required_value and not _has_admin_token(next_hashes):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "missing_admin_token",
                "message": "开启登录前至少需要配置一个管理员 token。",
                "details": {},
            },
        )

    payload = {
        "auth_required": bool(auth_required_value),
        "token_hashes": next_hashes,
        "updated_at": int(time.time()),
        "updated_by": updated_by,
    }
    path = _runtime_auth_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    with tmp_path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2, sort_keys=True)
    tmp_path.replace(path)
    return runtime_auth_status()


def ensure_runtime_auth_seeded_from_env(updated_by: str = "startup_env_migration") -> Dict[str, Any]:
    """Persist env-provided access tokens as hashes so restarts do not depend on .env.

    This is a one-way migration: plaintext tokens remain in the process
    environment, but only sha256 hashes are written to storage/runtime_auth.json.
    """
    current = _runtime_auth_config()
    next_hashes = _normalize_token_hash_config(
        current.get("token_hashes") if isinstance(current.get("token_hashes"), dict) else {}
    )
    changed = False

    def add_hashes(key: str, tokens: Sequence[str]) -> None:
        nonlocal changed
        existing = set(_hash_values(next_hashes.get(key)))
        for token in tokens:
            digest = hash_token(token)
            if digest not in existing:
                existing.add(digest)
                changed = True
        if existing:
            next_hashes[key] = sorted(existing)

    add_hashes("admin_hashes", _tokens_from_env("PHDEBATE_ADMIN_TOKEN", "PHDEBATE_ADMIN_PASSWORD"))
    add_hashes("host_hashes", _tokens_from_env("PHDEBATE_HOST_TOKEN", "PHDEBATE_HOST_PASSWORD"))
    add_hashes("screen_hashes", _tokens_from_env("PHDEBATE_SCREEN_TOKEN"))
    add_hashes("speaker_shared_hashes", _tokens_from_env("PHDEBATE_SPEAKER_TOKEN"))

    speaker_hashes: Dict[str, Sequence[str]] = {}
    raw_speaker_hashes = next_hashes.get("speaker_hashes")
    if isinstance(raw_speaker_hashes, dict):
        speaker_hashes = {
            str(speaker_id): list(_hash_values(value))
            for speaker_id, value in raw_speaker_hashes.items()
            if _hash_values(value)
        }
    for speaker_id, token in _speaker_tokens().items():
        existing = set(_hash_values(speaker_hashes.get(speaker_id)))
        digest = hash_token(token)
        if digest not in existing:
            existing.add(digest)
            changed = True
        if existing:
            speaker_hashes[speaker_id] = sorted(existing)
    if speaker_hashes:
        next_hashes["speaker_hashes"] = speaker_hashes

    runtime_has_auth_required = isinstance(current.get("auth_required"), bool)
    if not changed and runtime_has_auth_required:
        return runtime_auth_status()

    auth_required_value = bool(current["auth_required"]) if runtime_has_auth_required else _env_default_auth_required()
    if auth_required_value and not _has_admin_token(next_hashes):
        return runtime_auth_status()
    if not changed and runtime_has_auth_required:
        return runtime_auth_status()
    if not changed and not any(next_hashes.values()):
        return runtime_auth_status()

    payload = {
        "auth_required": auth_required_value,
        "token_hashes": next_hashes,
        "updated_at": int(time.time()),
        "updated_by": updated_by,
    }
    path = _runtime_auth_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    with tmp_path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2, sort_keys=True)
    tmp_path.replace(path)
    return runtime_auth_status()


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


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


def _runtime_auth_path() -> Path:
    raw = os.getenv("PHDEBATE_RUNTIME_AUTH_FILE", "").strip()
    if raw:
        path = Path(raw)
        return path if path.is_absolute() else _project_root() / path
    return _project_root() / "apps" / "backend" / "storage" / "runtime_auth.json"


def _runtime_auth_config() -> Dict[str, Any]:
    path = _runtime_auth_path()
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _env_default_auth_required() -> bool:
    override = os.getenv("PHDEBATE_AUTH_REQUIRED", "").strip().lower()
    if override in {"1", "true", "yes", "on"}:
        return True
    if override in {"0", "false", "no", "off"}:
        return False
    return os.getenv("PHDEBATE_ENV", "development").strip().lower() in {"production", "prod"}


def _source_summary(runtime_hashes: Dict[str, Any], hash_key: str, legacy_hash_key: str, *env_names: str) -> Dict[str, Any]:
    return {
        "env": bool(_tokens_from_env(*env_names)),
        "runtime_count": len(_hash_values(runtime_hashes.get(hash_key)) or _hash_values(runtime_hashes.get(legacy_hash_key))),
        "file_count": len(_hash_values(_token_file_config_file_only().get(hash_key)) or _hash_values(_token_file_config_file_only().get(legacy_hash_key))),
    }


def _token_file_config_file_only() -> Dict[str, Any]:
    path = _token_file_path()
    if not path or not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _speaker_hashes_from_config_file_only() -> Dict[str, Sequence[str]]:
    config = _token_file_config_file_only()
    raw = config.get("speaker_hashes") or config.get("speakers") or {}
    if not isinstance(raw, dict):
        return {}
    return {str(speaker_id): _hash_values(value) for speaker_id, value in raw.items() if _hash_values(value)}


def _normalize_token_hash_config(config: Dict[str, Any]) -> Dict[str, Any]:
    normalized: Dict[str, Any] = {}
    for key in ("admin_hashes", "host_hashes", "screen_hashes", "speaker_shared_hashes"):
        hashes = _hash_values(config.get(key))
        if hashes:
            normalized[key] = list(hashes)
    raw_speakers = config.get("speaker_hashes") or config.get("speakers") or {}
    if isinstance(raw_speakers, dict):
        speaker_hashes = {
            str(speaker_id): list(_hash_values(value))
            for speaker_id, value in raw_speakers.items()
            if _hash_values(value)
        }
        if speaker_hashes:
            normalized["speaker_hashes"] = speaker_hashes
    return normalized


def _has_admin_token(runtime_hashes: Dict[str, Any]) -> bool:
    return bool(
        _tokens_from_env("PHDEBATE_ADMIN_TOKEN", "PHDEBATE_ADMIN_PASSWORD")
        or _hash_values(_token_file_config_file_only().get("admin_hashes"))
        or _hash_values(_token_file_config_file_only().get("admin"))
        or _hash_values(runtime_hashes.get("admin_hashes"))
        or _hash_values(runtime_hashes.get("admin"))
    )


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
