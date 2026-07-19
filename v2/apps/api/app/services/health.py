from __future__ import annotations

import asyncio
import time
from typing import Any
from urllib.parse import urlparse

import httpx
from app.core.config import settings
from app.core.secret_crypto import decrypt_secret
from app.services.provider_config import default_agent_health_endpoint


def _base(name: str, endpoint: str, *, enabled: bool | None = None, **extra: Any) -> dict[str, Any]:
    active = bool(endpoint) if enabled is None else enabled
    status = "unconfigured" if not endpoint else "disabled" if not active else "configured"
    return {"name": name, "endpoint": endpoint, "enabled": active, "healthy": None, "status": status, **extra}


async def _tcp_probe(name: str, endpoint: str, *, enabled: bool | None = None, **extra: Any) -> dict[str, Any]:
    result = _base(name, endpoint, enabled=enabled, **extra)
    if not endpoint or result["enabled"] is False:
        return result
    parsed = urlparse(endpoint)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme in {"https", "wss"} else 80)
    if not host:
        return result | {"healthy": False, "status": "invalid", "message": "地址格式无效"}
    started = time.perf_counter()
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=1.5)
        writer.close()
        await writer.wait_closed()
        return result | {"healthy": True, "status": "reachable", "latency_ms": round((time.perf_counter() - started) * 1000, 1)}
    except Exception as exc:
        return result | {
            "healthy": False,
            "status": "unreachable",
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "message": type(exc).__name__,
        }


async def _agent_probe(config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or {}
    endpoint = str(config.get("endpoint") or settings.agent_api_url)
    enabled = bool(config.get("enabled", bool(endpoint)))
    result = _base("agent", endpoint)
    result["enabled"] = enabled
    if not endpoint or not enabled:
        result["status"] = "disabled" if endpoint else "unconfigured"
        return result
    provider_settings = config.get("settings") if isinstance(config.get("settings"), dict) else {}
    health_url = str(provider_settings.get("health_endpoint") or default_agent_health_endpoint(endpoint))
    headers = {}
    secret = decrypt_secret(str(config.get("secret_ciphertext") or ""))
    if secret:
        headers["X-Debate-Agent-Key"] = secret
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(health_url, headers=headers)
        return result | {
            "healthy": response.is_success,
            "status": "reachable" if response.is_success else "degraded",
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "message": f"HTTP {response.status_code}",
        }
    except Exception as exc:
        return result | {
            "healthy": False,
            "status": "unreachable",
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "message": type(exc).__name__,
        }


async def provider_health(
    *,
    judge_profile: dict[str, Any] | None = None,
    service_configs: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    judge_profile = judge_profile or {}
    service_configs = service_configs or {}
    agent_config = service_configs.get("agent") or {
        "endpoint": settings.agent_api_url,
        "enabled": bool(settings.agent_api_url),
        "settings": {},
    }
    judge_endpoint = str(judge_profile.get("endpoint") or settings.judge_api_url)
    judge_extra = {
        "profile_name": judge_profile.get("name") or "",
        "model_name": judge_profile.get("model_name") or "",
    }
    funasr_config = service_configs.get("funasr") or {
        "endpoint": settings.funasr_ws_url,
        "enabled": bool(settings.funasr_ws_url),
        "settings": {},
    }
    lighttts_config = service_configs.get("lighttts") or {
        "endpoint": settings.lighttts_url,
        "enabled": bool(settings.lighttts_url),
        "settings": {},
    }
    funasr_endpoint = str(funasr_config.get("endpoint") or "")
    lighttts_endpoint = str(lighttts_config.get("endpoint") or "")
    if settings.app_env == "test":
        return {
            "agent": _base(
                "agent",
                str(agent_config.get("endpoint") or ""),
                enabled=bool(agent_config.get("enabled")),
            ),
            "funasr": _base(
                "funasr",
                funasr_endpoint,
                enabled=bool(funasr_config.get("enabled")),
                settings=funasr_config.get("settings") or {},
            ),
            "lighttts": _base(
                "lighttts",
                lighttts_endpoint,
                enabled=bool(lighttts_config.get("enabled")),
                max_active=settings.lighttts_max_active,
                settings=lighttts_config.get("settings") or {},
            ),
            "judge": _base("judge", judge_endpoint, **judge_extra),
        }
    agent, funasr, lighttts, judge = await asyncio.gather(
        _agent_probe(agent_config),
        _tcp_probe(
            "funasr",
            funasr_endpoint,
            enabled=bool(funasr_config.get("enabled")),
            settings=funasr_config.get("settings") or {},
        ),
        _tcp_probe(
            "lighttts",
            lighttts_endpoint,
            enabled=bool(lighttts_config.get("enabled")),
            max_active=settings.lighttts_max_active,
            settings=lighttts_config.get("settings") or {},
        ),
        _tcp_probe("judge", judge_endpoint, **judge_extra),
    )
    return {"agent": agent, "funasr": funasr, "lighttts": lighttts, "judge": judge}
