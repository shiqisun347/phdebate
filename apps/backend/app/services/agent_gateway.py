from __future__ import annotations

import asyncio
import json
import os
import time
from urllib.parse import urlparse
from typing import Any, AsyncIterator, Dict, Iterable, List, Optional

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
        started = time.perf_counter()
        try:
            timeout = httpx.Timeout(self.connect_timeout_ms / 1000, read=self.read_timeout_ms / 1000)
            async with httpx.AsyncClient(timeout=timeout, transport=self.transport) as client:
                response = await client.get(self._url(endpoint, "/health"), headers=self._headers())
                if self._is_speech_only_health_response(endpoint, response):
                    return self._speech_only_health(endpoint, started, f"HTTP {response.status_code}")
                response.raise_for_status()
                try:
                    return response.json()
                except ValueError:
                    if self._has_explicit_path(endpoint):
                        return self._speech_only_health(endpoint, started, "non-json health response")
                    raise
        except (httpx.HTTPError, ValueError) as exc:
            raise AgentGatewayError("agent_unavailable", "Agent 健康检查失败。", {"endpoint": endpoint, "error": str(exc)})

    async def stream_speech(
        self,
        endpoint: str,
        payload: Dict[str, Any],
        fallback_chunks: Iterable[str],
        *,
        config: Optional[Dict[str, Any]] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        provider = (config or {}).get("provider_type", "rest_api")

        if provider == "openai_sdk":
            async for event in self._stream_openai_sdk_speech(config or {}, payload):
                yield event
            return

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

    async def _stream_openai_sdk_speech(
        self,
        config: Dict[str, Any],
        payload: Dict[str, Any],
    ) -> AsyncIterator[Dict[str, Any]]:
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:
            raise AgentGatewayError("openai_sdk_missing", "openai 包未安装，无法使用 OpenAI SDK 模式。", {"error": str(exc)})

        base_url = config.get("base_url", "").strip()
        api_key_env = config.get("api_key_env", "DASHSCOPE_API_KEY").strip()
        api_key = os.getenv(api_key_env, "").strip()
        # `model_name` is the display label (e.g. "Qwen-Max"); `model_id` is the actual
        # id sent to the OpenAI-compatible API. Fall back to the 需求 2.md test model.
        model = (
            str(config.get("model_id") or "").strip()
            or os.getenv("DASHSCOPE_MODEL", "").strip()
            or "qwen3.6-plus"
        )

        if not api_key:
            raise AgentGatewayError(
                "openai_sdk_no_key",
                f"环境变量 {api_key_env} 未设置，无法调用 OpenAI SDK。",
                {"api_key_env": api_key_env},
            )

        messages = self._build_openai_messages(payload)
        task_id = payload.get("task_id", "")
        # Deterministic per-speech ceiling derived from the time limit + TTS rate so the
        # spoken reply stays within the phase clock; falls back to a safe default.
        try:
            max_tokens = int(payload.get("max_token") or 0)
        except (TypeError, ValueError):
            max_tokens = 0
        if max_tokens <= 0:
            max_tokens = 800

        client_kwargs: Dict[str, Any] = {"api_key": api_key}
        if base_url:
            client_kwargs["base_url"] = base_url

        try:
            client = AsyncOpenAI(**client_kwargs)
            content = ""
            stream = await client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                stream=True,
            )
            async for chunk in stream:
                delta = chunk.choices[0].delta.content if chunk.choices else None
                if delta:
                    content += delta
                    yield {"type": "delta", "task_id": task_id, "delta": delta}
            yield {
                "type": "final",
                "task_id": task_id,
                "content": content,
                "usage": {"model": model, "latency_ms": 0},
            }
        except AgentGatewayError:
            raise
        except Exception as exc:
            raise AgentGatewayError("openai_sdk_error", f"OpenAI SDK 调用失败：{exc}", {"model": model, "error": str(exc)})

    def _build_openai_messages(self, payload: Dict[str, Any]) -> List[Dict[str, str]]:
        topic = payload.get("debate_topic", "")
        current_stage = payload.get("current_stage", "")
        next_stage = payload.get("next_stage", "")
        debater_name = payload.get("debater_name", "")
        debate_position = payload.get("debate_position", "")
        holder = payload.get("holder", "")
        time_limit = payload.get("time_limit_seconds", 180)
        target_chars = payload.get("target_chars", 400)
        debate_history: list = payload.get("debate_history", [])

        if payload.get("task_type") == "self_intro":
            system_prompt = (
                f"你是即将参加辩论赛的 AI 辩手【{debater_name}】，担任{holder}{debate_position}。"
                f"本场辩题：{topic}。"
                f"请做一段简短的赛前自我介绍，介绍你的身份与风格，可表达对本场比赛的期待，"
                f"目标 {target_chars} 字以内，语气自信友好，用中文直接开始，不要重复辩题全文，不要展开论证。"
            )
            user_content = "现在请你做赛前自我介绍。"
            return [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ]

        history_text = ""
        for stage in debate_history:
            stage_name = stage.get("stage", "")
            history_text += f"\n【{stage_name}】\n"
            for msg in stage.get("content", stage.get("message", [])):
                history_text += f"  {msg.get('speaker', '')}: {msg.get('content', '')}\n"

        system_prompt = (
            f"你是辩论赛辩手【{debater_name}】，{holder}{debate_position}。"
            f"辩题：{topic}。"
            f"当前环节：{current_stage}，下一环节：{next_stage}。"
            f"请用中文进行辩论发言，发言时间 {time_limit} 秒，目标 {target_chars} 字以内，"
            "语言简洁有力，直接开始发言，不要有任何开场白或自我介绍。"
        )

        user_content = "以下是本场辩论的发言记录：\n" + (history_text.strip() or "（本场辩论刚刚开始，暂无发言记录）")
        user_content += f"\n\n现在请你作为{holder}{debate_position}进行【{current_stage}】环节的发言。"

        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

    async def _stream_http_speech(self, endpoint: str, payload: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
        task_id = payload.get("task_id", "")
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

                    if "text/event-stream" in content_type:
                        # SSE stream — supports both phdebate format (type:delta/final)
                        # and OpenAI-compatible format (choices[].delta.content).
                        openai_content = ""
                        got_phdebate_final = False
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
                            # phdebate native error event
                            if event.get("type") == "error":
                                error = event.get("error") or {}
                                raise AgentGatewayError(
                                    error.get("code", "agent_error"),
                                    error.get("message", "Agent 返回错误。"),
                                    {"task_id": event.get("task_id"), "endpoint": endpoint},
                                )
                            # OpenAI-compatible SSE chunk
                            if "choices" in event:
                                choices = event.get("choices") or []
                                if choices:
                                    delta_text = (choices[0].get("delta") or {}).get("content") or ""
                                    if delta_text:
                                        openai_content += delta_text
                                        yield {"type": "delta", "task_id": task_id, "delta": delta_text}
                                continue
                            # Custom debate API format: {"delta": {"content": "..."}, ...}
                            if isinstance(event.get("delta"), dict):
                                delta_text = str(event["delta"].get("content") or "")
                                if delta_text:
                                    openai_content += delta_text
                                    yield {"type": "delta", "task_id": task_id, "delta": delta_text}
                                continue
                            # phdebate native event — pass through
                            if event.get("type") == "final":
                                got_phdebate_final = True
                            yield event
                        # Emit final event for formats that end with [DONE] (no explicit final)
                        if openai_content and not got_phdebate_final:
                            yield {"type": "final", "task_id": task_id, "content": openai_content, "usage": {}}
                        return

                    if "application/json" in content_type or not content_type:
                        data = await response.aread()
                        yield self._event_from_json_response(json.loads(data), task_id)
                        return

                    # Plain text / unknown streaming — accumulate and emit as single speech
                    raw_content = await response.aread()
                    text = raw_content.decode("utf-8", errors="replace").strip()
                    if text:
                        yield {"type": "delta", "task_id": task_id, "delta": text}
                        yield {"type": "final", "task_id": task_id, "content": text, "usage": {}}
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
        endpoint = endpoint.rstrip("/")
        if path == "/speech" and self._has_explicit_path(endpoint):
            return endpoint
        return f"{endpoint}{path}"

    def _has_explicit_path(self, endpoint: str) -> bool:
        parsed = urlparse(endpoint.rstrip("/"))
        return bool(parsed.path and parsed.path != "/")

    def _is_speech_only_health_response(self, endpoint: str, response: httpx.Response) -> bool:
        return self._has_explicit_path(endpoint) and response.status_code in {404, 405, 501}

    def _speech_only_health(self, endpoint: str, started: float, detail: str) -> Dict[str, Any]:
        return {
            "ok": True,
            "status": "speech_only",
            "model": "rest-api",
            "latency_ms": max(0, int((time.perf_counter() - started) * 1000)),
            "endpoint": endpoint,
            "detail": f"Agent endpoint exposes speech generation only ({detail}); live speech calls still use POST {endpoint}.",
        }
