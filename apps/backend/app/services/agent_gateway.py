from __future__ import annotations

import asyncio
import json
import os
from typing import Any, AsyncIterator, Dict, Iterable, Optional

import httpx


class AgentGatewayError(Exception):
    def __init__(self, code: str, message: str, details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


class AgentGateway:
    def __init__(self, transport: Optional[httpx.AsyncBaseTransport] = None) -> None:
        self.transport = transport
        self.token = os.getenv("PHDEBATE_AGENT_SHARED_TOKEN", "").strip()
        self.connect_timeout_ms = int(os.getenv("PHDEBATE_AGENT_CONNECT_TIMEOUT_MS", "3000"))
        self.read_timeout_ms = int(os.getenv("PHDEBATE_AGENT_READ_TIMEOUT_MS", "30000"))

    def endpoint_for(self, speaker: Dict[str, Any]) -> str:
        speaker_key = speaker["id"].upper().replace("-", "_")
        return (
            os.getenv(f"PHDEBATE_AGENT_ENDPOINT_{speaker_key}", "").strip()
            or speaker.get("agent_endpoint", "")
            or os.getenv("PHDEBATE_AGENT_BASE_URL", "").strip()
        )

    async def health(self, endpoint: str) -> Dict[str, Any]:
        if not endpoint:
            return {"ok": True, "status": "ready", "model": "embedded-mock", "latency_ms": 0}
        try:
            timeout = httpx.Timeout(self.connect_timeout_ms / 1000, read=self.read_timeout_ms / 1000)
            async with httpx.AsyncClient(timeout=timeout, transport=self.transport) as client:
                response = await client.get(self._url(endpoint, "/health"), headers=self._headers())
                response.raise_for_status()
                return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise AgentGatewayError("agent_unavailable", "Agent 健康检查失败。", {"endpoint": endpoint, "error": str(exc)})

    async def stream_speech(
        self,
        endpoint: str,
        payload: Dict[str, Any],
        fallback_chunks: Iterable[str],
    ) -> AsyncIterator[Dict[str, Any]]:
        if endpoint:
            async for event in self._stream_http_speech(endpoint, payload):
                yield event
            return

        content = ""
        for delta in fallback_chunks:
            await asyncio.sleep(0.35)
            content += delta
            yield {"type": "delta", "task_id": payload["task_id"], "delta": delta}
        yield {
            "type": "final",
            "task_id": payload["task_id"],
            "content": content,
            "usage": {"model": "embedded-mock", "latency_ms": 1000},
        }

    async def interrupt(self, endpoint: str, task_id: str, reason: str) -> Dict[str, Any]:
        if not endpoint:
            return {"ok": True, "task_id": task_id, "status": "interrupted"}
        try:
            timeout = httpx.Timeout(self.connect_timeout_ms / 1000, read=self.read_timeout_ms / 1000)
            async with httpx.AsyncClient(timeout=timeout, transport=self.transport) as client:
                response = await client.post(
                    self._url(endpoint, "/interrupt"),
                    json={"task_id": task_id, "reason": reason},
                    headers=self._headers(),
                )
                response.raise_for_status()
                return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise AgentGatewayError("agent_unavailable", "Agent 中断请求失败。", {"endpoint": endpoint, "error": str(exc)})

    async def _stream_http_speech(self, endpoint: str, payload: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
        timeout = httpx.Timeout(self.connect_timeout_ms / 1000, read=self.read_timeout_ms / 1000)
        try:
            async with httpx.AsyncClient(timeout=timeout, transport=self.transport) as client:
                async with client.stream(
                    "POST",
                    self._url(endpoint, "/speech"),
                    json=payload,
                    headers=self._headers(),
                ) as response:
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "")
                    if "text/event-stream" not in content_type:
                        data = response.json()
                        yield self._event_from_json_response(data, payload["task_id"])
                        return

                    async for line in response.aiter_lines():
                        line = line.strip()
                        if not line or line.startswith(":"):
                            continue
                        if not line.startswith("data:"):
                            continue
                        raw = line.removeprefix("data:").strip()
                        if raw == "[DONE]":
                            break
                        try:
                            event = json.loads(raw)
                        except json.JSONDecodeError as exc:
                            raise AgentGatewayError(
                                "agent_protocol_error",
                                "Agent SSE 返回了非法 JSON。",
                                {"raw": raw, "error": str(exc)},
                            )
                        if event.get("type") == "error":
                            error = event.get("error") or {}
                            raise AgentGatewayError(
                                error.get("code", "agent_error"),
                                error.get("message", "Agent 返回错误。"),
                                {"task_id": event.get("task_id"), "endpoint": endpoint},
                            )
                        yield event
        except AgentGatewayError:
            raise
        except httpx.HTTPError as exc:
            raise AgentGatewayError("agent_unavailable", "Agent 请求失败。", {"endpoint": endpoint, "error": str(exc)})

    def _event_from_json_response(self, data: Dict[str, Any], task_id: str) -> Dict[str, Any]:
        if data.get("status") == "completed":
            return {
                "type": "final",
                "task_id": data.get("task_id", task_id),
                "content": data.get("content", ""),
                "usage": data.get("usage", {}),
            }
        error = data.get("error") or {}
        raise AgentGatewayError(
            error.get("code", "agent_error"),
            error.get("message", "Agent 未完成发言任务。"),
            {"task_id": data.get("task_id", task_id), "status": data.get("status")},
        )

    def _headers(self) -> Dict[str, str]:
        headers = {"Accept": "text/event-stream, application/json"}
        if self.token:
            headers["X-Phdebate-Agent-Token"] = self.token
        return headers

    def _url(self, endpoint: str, path: str) -> str:
        return f"{endpoint.rstrip('/')}{path}"
