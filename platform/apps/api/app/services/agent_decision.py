from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
from app.core.config import settings
from app.core.secret_crypto import decrypt_secret


@dataclass(frozen=True)
class AgentSpeechDecision:
    should_speak: bool
    reason: str
    task_id: str
    fallback: bool = False


def _service_url(endpoint: str, suffix: str) -> str:
    parts = urlsplit(endpoint)
    path = parts.path.rstrip("/")
    if path.endswith("/debate"):
        path = path[: -len("/debate")]
    return urlunsplit((parts.scheme, parts.netloc, f"{path}/{suffix.lstrip('/')}", "", ""))


def _headers(provider_config: dict[str, Any]) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    secret = decrypt_secret(str(provider_config.get("secret_ciphertext") or ""))
    if secret:
        headers["X-Debate-Agent-Key"] = secret
    return headers


async def decide_should_speak(
    payload: dict[str, Any],
    provider_config: dict[str, Any],
    *,
    timeout_seconds: float = 5.5,
    transport: httpx.AsyncBaseTransport | None = None,
) -> AgentSpeechDecision:
    """Request a bounded intent decision, failing open on service trouble.

    A decision outage must not deadlock an otherwise healthy automated match.
    The returned ``fallback`` bit is persisted in the match event so operators
    can distinguish an affirmative model choice from resilience behavior.
    """

    task_id = str(payload.get("task_id") or "")
    if settings.agent_mock and transport is None:
        await asyncio.sleep(0)
        return AgentSpeechDecision(True, "测试环境保持既有自动发言流程", task_id)
    endpoint = str(provider_config.get("endpoint") or settings.agent_api_url).strip()
    if not endpoint:
        return AgentSpeechDecision(True, "判断服务未配置，按既有流程发言", task_id, True)
    body = dict(payload)
    body["task_type"] = "should_speak"
    body["max_token"] = min(96, max(16, int(body.get("max_token") or 48)))
    body["output"] = {"stream": False, "language": "zh-CN"}
    try:
        timeout = httpx.Timeout(timeout_seconds, connect=min(2.0, timeout_seconds), read=timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
            response = await client.post(
                _service_url(endpoint, "should-speak"),
                json=body,
                headers=_headers(provider_config),
            )
            response.raise_for_status()
            result = response.json()
        if not isinstance(result, dict) or type(result.get("should_speak")) is not bool:
            raise ValueError("invalid should_speak response")
        return AgentSpeechDecision(
            result["should_speak"],
            str(result.get("reason") or "").strip()[:300],
            str(result.get("task_id") or task_id),
        )
    except (httpx.HTTPError, ValueError):
        return AgentSpeechDecision(True, "判断服务异常，按既有流程发言", task_id, True)


async def interrupt_agent_task(
    endpoint: str,
    task_id: str,
    provider_config: dict[str, Any],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> bool:
    if settings.agent_mock or not endpoint or not task_id:
        return False
    try:
        async with httpx.AsyncClient(timeout=2, transport=transport) as client:
            response = await client.post(
                _service_url(endpoint, f"tasks/{quote(task_id, safe='')}/interrupt"),
                headers=_headers(provider_config),
            )
            return response.is_success and bool(response.json().get("ok"))
    except (httpx.HTTPError, ValueError):
        return False


async def wait_while_current(
    tasks: set[asyncio.Task[Any]],
    is_current: Callable[[], bool],
    on_invalid: Callable[[], Awaitable[None]],
    *,
    poll_seconds: float = 0.1,
) -> bool:
    """Watch speculative tasks and invalidate them promptly on pause/stop."""

    while any(not task.done() for task in tasks):
        if not is_current():
            await on_invalid()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            return False
        await asyncio.wait(tasks, timeout=poll_seconds, return_when=asyncio.FIRST_COMPLETED)
    return is_current()
