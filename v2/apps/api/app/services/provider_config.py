from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from app.core.config import settings
from app.models.entities import ProviderConfig
from sqlalchemy import select
from sqlalchemy.orm import Session

SUPPORTED_PROVIDER_KINDS = {"agent", "funasr", "lighttts"}


def default_agent_health_endpoint(endpoint: str) -> str:
    normalized = endpoint.rstrip("/")
    if normalized.endswith("/api/debate"):
        return f"{normalized[:-len('/debate')]}/health"
    return f"{normalized}/health"


def normalize_provider_config(kind: str, endpoint: str, values: dict[str, Any] | None) -> tuple[str, dict[str, Any]]:
    normalized_kind = kind.strip().lower()
    if normalized_kind not in SUPPORTED_PROVIDER_KINDS:
        raise ValueError("只支持配置 RESTful Agent、FunASR 和 LightTTS。")
    normalized_endpoint = endpoint.strip()
    parsed = urlparse(normalized_endpoint)
    allowed_schemes = {"ws", "wss"} if normalized_kind == "funasr" else {"http", "https"}
    if parsed.scheme.lower() not in allowed_schemes or not parsed.hostname:
        expected = "ws:// 或 wss://" if normalized_kind == "funasr" else "http:// 或 https://"
        raise ValueError(f"{normalized_kind} 地址必须使用 {expected}。")
    raw = values or {}
    if not isinstance(raw, dict):
        raise ValueError("服务参数必须是 JSON 对象。")
    if normalized_kind == "agent":
        allowed = {"method", "protocol", "health_endpoint", "timeout_seconds", "stream"}
        unknown = set(raw) - allowed
        if unknown:
            raise ValueError(f"RESTful Agent 不支持参数：{', '.join(sorted(unknown))}。")
        if str(raw.get("method", "POST")).upper() != "POST" or str(raw.get("protocol", "restful")).lower() != "restful":
            raise ValueError("辩手 Agent 只支持 RESTful POST 接入。")
        health_endpoint = str(raw.get("health_endpoint") or default_agent_health_endpoint(normalized_endpoint)).strip()
        health_parsed = urlparse(health_endpoint)
        if health_parsed.scheme.lower() not in {"http", "https"} or not health_parsed.hostname:
            raise ValueError("Agent 健康检查地址必须使用 http:// 或 https://。")
        try:
            timeout_seconds = int(raw.get("timeout_seconds", settings.agent_timeout_seconds))
        except (TypeError, ValueError) as exc:
            raise ValueError("Agent 超时必须是整数秒。") from exc
        if not 10 <= timeout_seconds <= 300:
            raise ValueError("Agent 超时必须在 10–300 秒之间。")
        stream = raw.get("stream", True)
        if not isinstance(stream, bool):
            raise ValueError("Agent 流式输出开关必须是布尔值。")
        normalized_settings = {
            "method": "POST",
            "protocol": "restful",
            "health_endpoint": health_endpoint,
            "timeout_seconds": timeout_seconds,
            "stream": stream,
        }
    elif normalized_kind == "funasr":
        allowed = {"final_wait_seconds"}
        unknown = set(raw) - allowed
        if unknown:
            raise ValueError(f"FunASR 不支持参数：{', '.join(sorted(unknown))}。")
        try:
            final_wait = float(raw.get("final_wait_seconds", 30))
        except (TypeError, ValueError) as exc:
            raise ValueError("FunASR 最终结果等待时间必须是数字。") from exc
        if not 5 <= final_wait <= 60:
            raise ValueError("FunASR 最终结果等待时间必须在 5–60 秒之间。")
        normalized_settings = {"final_wait_seconds": round(final_wait, 1)}
    else:
        allowed = {"read_timeout_seconds", "speed"}
        unknown = set(raw) - allowed
        if unknown:
            raise ValueError(f"LightTTS 不支持参数：{', '.join(sorted(unknown))}。")
        try:
            read_timeout = int(raw.get("read_timeout_seconds", 180))
            speed = float(raw.get("speed", 1.0))
        except (TypeError, ValueError) as exc:
            raise ValueError("LightTTS 超时和语速参数格式无效。") from exc
        if not 30 <= read_timeout <= 300:
            raise ValueError("LightTTS 读取超时必须在 30–300 秒之间。")
        if not 0.5 <= speed <= 2.0:
            raise ValueError("LightTTS 语速必须在 0.5–2.0 之间。")
        normalized_settings = {"read_timeout_seconds": read_timeout, "speed": round(speed, 2)}
    return normalized_endpoint, normalized_settings


def fallback_provider_config(kind: str) -> dict[str, Any]:
    if kind == "agent":
        return {
            "id": None,
            "kind": kind,
            "endpoint": settings.agent_api_url,
            "settings": {
                "method": "POST",
                "protocol": "restful",
                "health_endpoint": default_agent_health_endpoint(settings.agent_api_url) if settings.agent_api_url else "",
                "timeout_seconds": settings.agent_timeout_seconds,
                "stream": True,
            },
            "secret_ciphertext": "",
            "enabled": bool(settings.agent_api_url),
            "source": "environment",
        }
    if kind == "funasr":
        return {
            "id": None,
            "kind": kind,
            "endpoint": settings.funasr_ws_url,
            "settings": {"final_wait_seconds": 30.0},
            "enabled": bool(settings.funasr_ws_url),
            "source": "environment",
        }
    if kind == "lighttts":
        return {
            "id": None,
            "kind": kind,
            "endpoint": settings.lighttts_url,
            "settings": {"read_timeout_seconds": 180, "speed": 1.0},
            "enabled": bool(settings.lighttts_url),
            "source": "environment",
        }
    raise ValueError("未知服务类型。")


def build_service_snapshot(db: Session) -> dict[str, dict[str, Any]]:
    rows = {item.kind: item for item in db.scalars(select(ProviderConfig).where(ProviderConfig.kind.in_(SUPPORTED_PROVIDER_KINDS))).all()}
    result: dict[str, dict[str, Any]] = {}
    for kind in sorted(SUPPORTED_PROVIDER_KINDS):
        item = rows.get(kind)
        if not item:
            result[kind] = fallback_provider_config(kind)
            continue
        try:
            endpoint, normalized_settings = normalize_provider_config(kind, item.endpoint, item.settings)
        except ValueError:
            endpoint, normalized_settings = item.endpoint, dict(item.settings or {})
        result[kind] = {
            "id": item.id,
            "kind": kind,
            "endpoint": endpoint,
            "settings": normalized_settings,
            "secret_ciphertext": item.secret_ciphertext if kind == "agent" else "",
            "enabled": item.is_active,
            "source": "database",
        }
    return result


def runtime_provider_config(snapshot: dict[str, Any] | None, kind: str) -> dict[str, Any]:
    if isinstance(snapshot, dict) and kind in snapshot and isinstance(snapshot[kind], dict):
        item = dict(snapshot[kind])
        item.setdefault("kind", kind)
        item.setdefault("settings", {})
        item.setdefault("enabled", bool(item.get("endpoint")))
        return item
    return fallback_provider_config(kind)
