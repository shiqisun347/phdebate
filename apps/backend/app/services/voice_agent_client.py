from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Dict, Optional, Tuple

import httpx


class VoiceAgentClientError(Exception):
    pass


_health_lock = asyncio.Lock()
_health_cache: Optional[Tuple[float, Dict[str, Any]]] = None


def voice_agent_base_url() -> str:
    return os.getenv("PHDEBATE_VOICE_AGENT_BASE_URL", "http://127.0.0.1:6008").strip().rstrip("/")


def _health_cache_seconds() -> float:
    raw = os.getenv("PHDEBATE_VOICE_AGENT_HEALTH_CACHE_SECONDS", "2.0").strip()
    try:
        return max(0.0, min(30.0, float(raw)))
    except ValueError:
        return 2.0


async def voice_agent_health() -> Dict[str, Any]:
    global _health_cache
    ttl = _health_cache_seconds()
    now = time.monotonic()
    if ttl > 0 and _health_cache and now - _health_cache[0] < ttl:
        return dict(_health_cache[1])

    async with _health_lock:
        now = time.monotonic()
        if ttl > 0 and _health_cache and now - _health_cache[0] < ttl:
            return dict(_health_cache[1])
        data = await _fetch_voice_agent_health()
        if ttl > 0:
            _health_cache = (time.monotonic(), dict(data))
        return data


async def _fetch_voice_agent_health() -> Dict[str, Any]:
    base = voice_agent_base_url()
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(f"{base}/health")
            response.raise_for_status()
            return response.json()
    except Exception as exc:  # noqa: BLE001
        raise VoiceAgentClientError(f"voice-agent 不可用：{exc}") from exc


async def start_voice_agent(payload: Dict[str, Any]) -> Dict[str, Any]:
    return await _post("/tasks/start", payload)


async def stop_voice_agent(payload: Dict[str, Any]) -> Dict[str, Any]:
    return await _post("/tasks/stop", payload)


async def publish_tts_sentence(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if os.getenv("PHDEBATE_LIVEKIT_ENABLED", "").strip().lower() not in {"1", "true", "yes", "on"}:
        return None
    try:
        return await _post("/tts/sentence", payload, timeout=1.5)
    except VoiceAgentClientError:
        return None


async def _post(path: str, payload: Dict[str, Any], timeout: float = 5.0) -> Dict[str, Any]:
    base = voice_agent_base_url()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(f"{base}{path}", json=payload)
            response.raise_for_status()
            data = response.json()
            if isinstance(data, dict):
                return data
            return {"ok": True, "data": data}
    except Exception as exc:  # noqa: BLE001
        raise VoiceAgentClientError(f"voice-agent 请求失败：{exc}") from exc
