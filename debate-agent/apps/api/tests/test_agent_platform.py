from __future__ import annotations

import os
import asyncio
import json
from types import SimpleNamespace

os.environ.update(
    {
        "APP_ENV": "test",
        "APP_SECRET": "test-app-secret-with-sufficient-entropy",
        "ENCRYPTION_SECRET": "test-encryption-secret-with-sufficient-entropy",
        "DATABASE_URL": "sqlite:///./storage/test-debate-agent.db",
        "REDIS_URL": "redis://127.0.0.1:6399/15",
        "PUBLIC_ORIGIN": "http://testserver",
        "ADMIN_ACCOUNT": "agent_admin",
        "ADMIN_REAL_NAME": "Agent 管理员",
        "ADMIN_PASSWORD": "Agent-admin-test-1234",
        "BOOTSTRAP_GATEWAY_KEY": "gateway-test-secret",
    }
)

import pytest
import httpx
from app.agent_engine import agent_engine, request_hash
from app.config import settings
from app.database import Base, SessionLocal, engine
from app.main import app
from app.mem0_bridge import mem0_bridge
from app.models import AgentTask, GatewayKey, LLMProvider, MemoryItem, ModelPreset
from app.security import encrypt_secret
from fastapi.testclient import TestClient
from sqlalchemy import func, select


@pytest.fixture(autouse=True)
def clean_database():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    from app.seed import initialize_database

    initialize_database()
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
def client():
    with TestClient(app) as instance:
        yield instance


def login(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/debate/api/admin/login",
        json={"account": "agent_admin", "password": "Agent-admin-test-1234"},
    )
    assert response.status_code == 200, response.text
    return {"X-CSRF-Token": response.json()["csrf_token"]}


def payload(task_id: str = "task-1", topic: str = "人工智能是否提升学习效率？") -> dict:
    return {
        "model_name": "client-cannot-override",
        "agent_profile": "debater-1",
        "debater_name": "陈思远",
        "debate_position": "一辩",
        "debate_topic": topic,
        "current_stage": "正方一辩立论",
        "next_stage": "反方一辩立论",
        "holder": "正方",
        "debate_history": [],
        "task_type": "debate",
        "max_token": 300,
        "match_id": "match-a",
        "room_code": "123456",
        "task_id": task_id,
        "output": {"stream": False, "language": "zh-CN"},
    }


def sse_events(response) -> list[dict]:
    events = []
    for line in response.text.splitlines():
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        events.append(json.loads(line[6:]))
    return events


def sse_content(response) -> str:
    return "".join(str(event.get("delta", {}).get("content") or "") for event in sse_events(response))


def enable_provider() -> None:
    with SessionLocal() as db:
        provider = db.scalar(select(LLMProvider).order_by(LLMProvider.created_at).limit(1))
        provider.is_active = True
        provider.provider = "openai"
        provider.model_id = "server-authoritative-model"
        provider.api_key_ciphertext = encrypt_secret("llm-test-key")
        db.commit()


def test_admin_auth_secret_redaction_and_qwen3_8b_guard(client: TestClient) -> None:
    assert client.get("/debate/api/admin/dashboard").status_code == 401
    csrf = login(client)
    dashboard = client.get("/debate/api/admin/dashboard")
    assert dashboard.status_code == 200
    assert "password_hash" not in dashboard.text and "key_hash" not in dashboard.text
    rejected = client.post(
        "/debate/api/admin/llm-providers",
        headers=csrf,
        json={
            "name": "禁止模型",
            "provider": "openai",
            "base_url": "https://llm.example/v1",
            "api_key": "secret-never-returned",
            "model_id": "Qwen3-8B-Instruct",
            "is_active": True,
        },
    )
    assert rejected.status_code == 422 and "Qwen3 8B" in rejected.text
    created = client.post(
        "/debate/api/admin/llm-providers",
        headers=csrf,
        json={
            "name": "正式模型",
            "provider": "openai",
            "base_url": "https://llm.example/v1",
            "api_key": "secret-never-returned",
            "model_id": "qwen3.6-27b",
            "is_active": True,
        },
    )
    assert created.status_code == 200
    assert created.json()["provider"]["has_api_key"] is True
    assert "secret-never-returned" not in created.text
    preview = client.post(
        "/debate/api/admin/prompts/preview",
        headers=csrf,
        json={
            "system_template": "你是{{ debater_name }}，辩题是{{ debate_topic }}。",
            "user_template": "环节：{{ current_stage }}。",
            "variables": {"debater_name": "乾元"},
        },
    )
    assert preview.status_code == 200
    assert preview.json()["messages"][0]["content"].startswith("你是乾元")


def test_health_requires_database_redis_and_active_llm(client: TestClient, monkeypatch) -> None:
    class FakeRedis:
        async def ping(self):
            return True

        async def aclose(self):
            return None

    monkeypatch.setattr("app.main.redis.from_url", lambda *_args, **_kwargs: FakeRedis())
    degraded = client.get("/debate/api/health")
    assert degraded.status_code == 503
    assert degraded.json()["checks"]["llm_gateway"]["ok"] is False
    enable_provider()
    ready = client.get("/debate/api/health")
    assert ready.status_code == 200
    assert ready.json()["ok"] is True
    models = client.get("/debate/api/models")
    assert models.status_code == 200
    assert models.json()["models"][0]["id"] == "server-authoritative-model"


async def _never_interrupted(_task_id: str) -> bool:
    return False


def test_json_generation_is_idempotent_and_creates_reviewable_memory(client: TestClient, monkeypatch) -> None:
    enable_provider()
    calls: list[dict] = []

    class Stream:
        def __aiter__(self):
            async def values():
                for text in ("我方认为，", "人工智能能够提升学习反馈效率。"):
                    yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text))], usage=None)
            return values()

    async def completion(**kwargs):
        calls.append(kwargs)
        return Stream()

    monkeypatch.setattr("app.agent_engine.litellm.acompletion", completion)
    monkeypatch.setattr(agent_engine, "_interrupted", _never_interrupted)
    headers = {"X-Debate-Agent-Key": "gateway-test-secret"}
    missing = client.post("/debate/api/debate", json=payload())
    assert missing.status_code == 401
    first = client.post("/debate/api/debate", headers=headers, json=payload())
    replay = client.post("/debate/api/debate", headers=headers, json=payload())
    conflict = client.post("/debate/api/debate", headers=headers, json=payload(topic="不同辩题"))
    assert first.status_code == 200, first.text
    assert sse_content(first).endswith("学习反馈效率。")
    assert replay.status_code == 200 and sse_content(replay) == sse_content(first)
    assert all(event["model_name"] == "server-authoritative-model" for event in sse_events(replay))
    assert conflict.status_code == 409
    assert len(calls) == 1
    assert calls[0]["model"].endswith("server-authoritative-model")
    assert "client-cannot-override" not in calls[0]["model"]
    assert calls[0]["stream"] is True
    assert calls[0]["extra_body"] == {"enable_thinking": False}
    with SessionLocal() as db:
        task = db.scalar(select(AgentTask).where(AgentTask.task_id == "task-1"))
        assert task and task.status == "completed"
        assert db.scalar(select(func.count(MemoryItem.id)).where(MemoryItem.match_id == "match-a")) == 1
        candidate = db.scalar(select(MemoryItem).where(MemoryItem.scope == "long_term"))
        assert candidate and candidate.status == "pending_review"


def test_should_speak_is_strict_idempotent_and_never_creates_memory(client: TestClient, monkeypatch) -> None:
    enable_provider()
    calls = 0

    class Stream:
        def __aiter__(self):
            async def values():
                yield SimpleNamespace(
                    choices=[SimpleNamespace(delta=SimpleNamespace(content='{"should_speak":false,"reason":"论点已经充分"}'))],
                    usage=None,
                )

            return values()

    async def completion(**_kwargs):
        nonlocal calls
        calls += 1
        return Stream()

    monkeypatch.setattr("app.agent_engine.litellm.acompletion", completion)
    monkeypatch.setattr(agent_engine, "_interrupted", _never_interrupted)
    body = payload(task_id="decision-room-a-turn-3") | {
        "task_type": "should_speak",
        "max_token": 48,
    }
    headers = {"X-Debate-Agent-Key": "gateway-test-secret"}
    first = client.post("/debate/api/should-speak", headers=headers, json=body)
    replay = client.post("/debate/api/should-speak", headers=headers, json=body)
    conflict = client.post(
        "/debate/api/should-speak",
        headers=headers,
        json=body | {"room_code": "654321"},
    )
    assert first.status_code == 200, first.text
    assert first.json() == {
        "task_id": "decision-room-a-turn-3",
        "should_speak": False,
        "reason": "论点已经充分",
        "replayed": False,
    }
    assert replay.status_code == 200 and replay.json()["replayed"] is True
    assert conflict.status_code == 409
    assert calls == 1
    with SessionLocal() as db:
        task = db.scalar(select(AgentTask).where(AgentTask.task_id == "decision-room-a-turn-3"))
        assert task and task.room_code == "123456" and task.status == "completed"
        assert db.scalar(select(func.count(MemoryItem.id))) == 0


def test_debate_thinking_is_always_disabled_and_cannot_be_overridden(
    client: TestClient, monkeypatch
) -> None:
    enable_provider()
    with SessionLocal() as db:
        provider = db.scalar(select(LLMProvider).order_by(LLMProvider.created_at).limit(1))
        provider.model_id = "qwen3.6-27b"
        preset = db.scalar(select(ModelPreset).where(ModelPreset.name == "标准辩论"))
        preset.reasoning = {}
        db.commit()

    calls: list[dict] = []

    class Stream:
        def __aiter__(self):
            async def values():
                yield SimpleNamespace(
                    choices=[SimpleNamespace(delta=SimpleNamespace(content="快速可见正文"))],
                    usage=None,
                )

            return values()

    async def completion(**kwargs):
        calls.append(kwargs)
        return Stream()

    monkeypatch.setattr("app.agent_engine.litellm.acompletion", completion)
    monkeypatch.setattr(agent_engine, "_interrupted", _never_interrupted)
    headers = {"X-Debate-Agent-Key": "gateway-test-secret"}

    first_payload = payload(task_id="qwen-default", topic="服务端关闭思考")
    first_payload["enable_thinking"] = True
    first = client.post("/debate/api/debate", headers=headers, json=first_payload)
    assert first.status_code == 200, first.text
    assert calls[-1]["stream"] is True
    assert calls[-1]["extra_body"] == {"enable_thinking": False}

    with SessionLocal() as db:
        preset = db.scalar(select(ModelPreset).where(ModelPreset.name == "标准辩论"))
        preset.reasoning = {"enable_thinking": True}
        db.commit()
    second = client.post(
        "/debate/api/debate",
        headers=headers,
        json=payload(task_id="qwen-enabled", topic="预设开启思考"),
    )
    assert second.status_code == 200, second.text
    assert calls[-1]["stream"] is True
    assert calls[-1]["extra_body"] == {"enable_thinking": False}


def test_latency_qualified_openai_model_uses_raw_sse_without_litellm_buffering(
    client: TestClient,
    monkeypatch,
) -> None:
    enable_provider()
    with SessionLocal() as db:
        provider = db.scalar(select(LLMProvider).order_by(LLMProvider.created_at).limit(1))
        provider.provider = "openai"
        provider.base_url = "http://openai-fast.test/v1"
        provider.model_id = "qwen-plus"
        provider.api_key_ciphertext = encrypt_secret("direct-test-key")
        db.commit()

    calls: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "http://openai-fast.test/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer direct-test-key"
        body = json.loads(await request.aread())
        calls.append(body)
        assert body["model"] == "qwen-plus"
        assert body["stream"] is True
        assert body["enable_thinking"] is False
        assert body["stream_options"] == {"include_usage": True}
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(
                'data: {"choices":[{"delta":{"reasoning_content":"不得外泄"}}]}\n\n'
                'data: {"choices":[{"delta":{"content":"低延迟"}}]}\n\n'
                'data: {"choices":[{"delta":{"content":"可见正文。"}}]}\n\n'
                'data: {"choices":[],"usage":{"prompt_tokens":12,"completion_tokens":8}}\n\n'
                "data: [DONE]\n\n"
            ).encode(),
        )

    async def forbidden_litellm(**_kwargs):
        raise AssertionError("latency-qualified debate model must bypass LiteLLM streaming")

    async def within_limit(_provider) -> bool:
        return True

    monkeypatch.setattr(agent_engine, "_http_transport", httpx.MockTransport(handler))
    monkeypatch.setattr(agent_engine, "_interrupted", _never_interrupted)
    monkeypatch.setattr(agent_engine, "_within_provider_limit", within_limit)
    monkeypatch.setattr("app.agent_engine.litellm.acompletion", forbidden_litellm)
    response = client.post(
        "/debate/api/debate",
        headers={"X-Debate-Agent-Key": "gateway-test-secret"},
        json=payload(task_id="direct-openai-fastpath", topic="直接 SSE 快速路径"),
    )
    assert response.status_code == 200, response.text
    assert sse_content(response) == "低延迟可见正文。"
    assert "不得外泄" not in response.text
    assert len(calls) == 1
    with SessionLocal() as db:
        task = db.scalar(select(AgentTask).where(AgentTask.task_id == "direct-openai-fastpath"))
        assert task and task.status == "completed"
        assert task.usage["provider"] == "openai_direct"
        assert task.usage["completion_tokens"] == 8


def test_direct_openai_partial_stream_never_concatenates_a_fallback_provider(
    client: TestClient,
    monkeypatch,
) -> None:
    enable_provider()
    with SessionLocal() as db:
        provider = db.scalar(select(LLMProvider).order_by(LLMProvider.created_at).limit(1))
        provider.provider = "openai"
        provider.base_url = "http://openai-fast.test/v1"
        provider.model_id = "qwen-plus"
        provider.api_key_ciphertext = encrypt_secret("direct-test-key")
        provider.priority = 1
        db.add(
            LLMProvider(
                name="不得拼接的备用 Provider",
                provider="debate_api",
                base_url="http://fallback.test/api/debate",
                model_id="qwen3.7-plus",
                priority=2,
                is_active=True,
            )
        )
        db.commit()

    request_hosts: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        request_hosts.append(request.url.host)
        if request.url.host == "fallback.test":
            raise AssertionError("partial primary speech must not be concatenated with fallback output")
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(
                'data: {"choices":[{"delta":{"content":"已经播出的前缀。"}}]}\n\n'
                'data: {"error":{"message":"upstream reset"}}\n\n'
            ).encode(),
        )

    async def within_limit(_provider) -> bool:
        return True

    monkeypatch.setattr(agent_engine, "_http_transport", httpx.MockTransport(handler))
    monkeypatch.setattr(agent_engine, "_interrupted", _never_interrupted)
    monkeypatch.setattr(agent_engine, "_within_provider_limit", within_limit)
    response = client.post(
        "/debate/api/debate",
        headers={"X-Debate-Agent-Key": "gateway-test-secret"},
        json=payload(task_id="direct-partial-no-fallback", topic="部分流不得拼接"),
    )
    assert response.status_code == 200
    assert "已经播出的前缀。" in response.text
    assert '"code": "partial_stream_failed"' in response.text
    assert request_hosts == ["openai-fast.test"]
    with SessionLocal() as db:
        task = db.scalar(select(AgentTask).where(AgentTask.task_id == "direct-partial-no-fallback"))
        assert task and task.status == "failed" and task.error_code == "partial_stream_failed"


def test_direct_openai_length_stop_continues_same_speech_without_repeating_prefix(
    client: TestClient,
    monkeypatch,
) -> None:
    enable_provider()
    with SessionLocal() as db:
        provider = db.scalar(select(LLMProvider).order_by(LLMProvider.created_at).limit(1))
        provider.provider = "openai"
        provider.base_url = "http://openai-fast.test/v1"
        provider.model_id = "qwen-plus"
        provider.api_key_ciphertext = encrypt_secret("direct-test-key")
        db.commit()

    calls: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(await request.aread())
        calls.append(body)
        if len(calls) == 1:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=(
                    'data: {"choices":[{"delta":{"content":"因此真正需要守住的是——"}}]}\n\n'
                    'data: {"choices":[{"delta":{},"finish_reason":"length"}],'
                    '"usage":{"prompt_tokens":20,"completion_tokens":120,"total_tokens":140}}\n\n'
                    "data: [DONE]\n\n"
                ).encode(),
            )
        assert body["messages"][-2] == {"role": "assistant", "content": "因此真正需要守住的是——"}
        assert "不要重复" in body["messages"][-1]["content"]
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(
                'data: {"choices":[{"delta":{"content":"人类的判断权与责任。"}}]}\n\n'
                'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
                '"usage":{"prompt_tokens":30,"completion_tokens":12,"total_tokens":42}}\n\n'
                "data: [DONE]\n\n"
            ).encode(),
        )

    async def within_limit(_provider) -> bool:
        return True

    monkeypatch.setattr(agent_engine, "_http_transport", httpx.MockTransport(handler))
    monkeypatch.setattr(agent_engine, "_interrupted", _never_interrupted)
    monkeypatch.setattr(agent_engine, "_within_provider_limit", within_limit)
    response = client.post(
        "/debate/api/debate",
        headers={"X-Debate-Agent-Key": "gateway-test-secret"},
        json=payload(task_id="direct-length-continuation", topic="截断续写"),
    )
    assert response.status_code == 200, response.text
    assert sse_content(response) == "因此真正需要守住的是——人类的判断权与责任。"
    assert len(calls) == 2
    with SessionLocal() as db:
        task = db.scalar(select(AgentTask).where(AgentTask.task_id == "direct-length-continuation"))
        assert task and task.status == "completed"
        assert task.usage["finish_reason"] == "stop"
        assert task.usage["continuation_calls"] == 1
        assert task.usage["completion_tokens"] == 132


def test_startup_warmup_primes_three_direct_requests_without_business_rows(monkeypatch) -> None:
    enable_provider()
    with SessionLocal() as db:
        provider = db.scalar(select(LLMProvider).order_by(LLMProvider.created_at).limit(1))
        provider.provider = "openai"
        provider.base_url = "http://openai-fast.test/v1"
        provider.model_id = "qwen-plus"
        provider.api_key_ciphertext = encrypt_secret("direct-test-key")
        db.commit()
        before_tasks = db.scalar(select(func.count(AgentTask.id))) or 0
        before_memories = db.scalar(select(func.count(MemoryItem.id))) or 0

    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        body = json.loads(await request.aread())
        assert body["model"] == "qwen-plus"
        assert body["max_tokens"] == 16
        assert body["enable_thinking"] is False
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(
                'data: {"choices":[{"delta":{"content":"准备完成。"}}]}\n\n'
                "data: [DONE]\n\n"
            ).encode(),
        )

    monkeypatch.setattr(settings, "startup_model_warmup_enabled", True)
    monkeypatch.setattr(settings, "startup_model_warmup_requests", 3)
    monkeypatch.setattr(agent_engine, "_http_transport", httpx.MockTransport(handler))
    result = asyncio.run(agent_engine.warmup())

    assert result["status"] == "ok" and result["requests"] == 3
    assert result["model"] == "qwen-plus"
    assert calls == 3
    with SessionLocal() as db:
        assert (db.scalar(select(func.count(AgentTask.id))) or 0) == before_tasks
        assert (db.scalar(select(func.count(MemoryItem.id))) or 0) == before_memories


def test_debate_rest_provider_adapts_upstream_sse_and_controls_model(client: TestClient, monkeypatch) -> None:
    with SessionLocal() as db:
        provider = db.scalar(select(LLMProvider).order_by(LLMProvider.created_at).limit(1))
        provider.is_active = True
        provider.provider = "debate_api"
        provider.base_url = "http://upstream.test/api/debate"
        provider.model_id = "qwen3.7-plus"
        db.commit()

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(await request.aread())
        assert body["model_name"] == "qwen3.7-plus"
        assert body["agent_profile"] == "debater-1"
        assert body["output"]["stream"] is True
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(
                'data: {"delta":{"content":"上游"}}\n\n'
                'data: {"delta":{"content":"完整发言"}}\n\n'
                "data: [DONE]\n\n"
            ).encode(),
        )

    async def within_limit(_provider) -> bool:
        return True

    monkeypatch.setattr(agent_engine, "_http_transport", httpx.MockTransport(handler))
    monkeypatch.setattr(agent_engine, "_interrupted", _never_interrupted)
    monkeypatch.setattr(agent_engine, "_within_provider_limit", within_limit)
    response = client.post(
        "/debate/api/debate",
        headers={"X-Debate-Agent-Key": "gateway-test-secret"},
        json=payload("upstream-adapter") | {"model_name": "client-must-not-override"},
    )
    assert response.status_code == 200, response.text
    assert sse_content(response) == "上游完整发言"
    assert all(event["model_name"] == "qwen3.7-plus" for event in sse_events(response))


def test_admin_provider_test_probes_real_debate_generation_and_redacts_errors(client: TestClient, monkeypatch) -> None:
    """The admin check must validate generation, not just a models endpoint."""

    with SessionLocal() as db:
        provider = db.scalar(select(LLMProvider).order_by(LLMProvider.created_at).limit(1))
        provider.is_active = True
        provider.provider = "debate_api"
        provider.base_url = "http://upstream.test/api/debate"
        provider.model_id = "qwen3.7-plus"
        db.commit()

    class Response:
        def __init__(self, status_code: int, body: dict | None = None, lines: list[str] | None = None):
            self.status_code = status_code
            self._body = body or {}
            self._lines = lines or []
            self.text = json.dumps(self._body, ensure_ascii=False)

        def json(self):
            return self._body

        async def aiter_lines(self):
            for line in self._lines:
                yield line

    class Stream:
        def __init__(self, response):
            self.response = response

        async def __aenter__(self):
            return self.response

        async def __aexit__(self, *_args):
            return False

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, _url, **_kwargs):
            return Response(200, {"models": [{"id": "qwen3.7-plus"}]})

        def stream(self, _method, _url, **_kwargs):
            return Stream(Response(200, lines=['data: {"delta":{"content":"测试通过"}}', "data: [DONE]"]))

        async def post(self, *_args, **_kwargs):
            return Response(200, {"choices": [{"message": {"content": "测试通过"}}]})

    monkeypatch.setattr("app.main.httpx.AsyncClient", lambda **_kwargs: Client())
    csrf = login(client)
    provider_id = provider.id
    tested = client.post(f"/debate/api/admin/llm-providers/{provider_id}/test", headers=csrf, json={})
    assert tested.status_code == 200, tested.text
    assert tested.json()["generation_probe"] is True

    with SessionLocal() as db:
        current = db.get(LLMProvider, provider_id)
        current.provider = "openai"
        current.base_url = "http://openai.test/v1"
        db.commit()

    class AuthFailureClient(Client):
        async def get(self, _url, **_kwargs):
            return Response(401, {"error": {"code": "auth_unavailable", "message": "Invalid API key secret-never-returned"}})

    monkeypatch.setattr("app.main.httpx.AsyncClient", lambda **_kwargs: AuthFailureClient())
    failed = client.post(f"/debate/api/admin/llm-providers/{provider_id}/test", headers=csrf, json={})
    assert failed.status_code == 502
    assert "auth_unavailable" in failed.json()["detail"]
    assert "secret-never-returned" not in failed.json()["detail"]


def test_sse_contract_memory_review_and_interrupt(client: TestClient, monkeypatch) -> None:
    enable_provider()

    class Stream:
        def __aiter__(self):
            async def values():
                yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="流式发言"))], usage=None)
            return values()

    async def completion(**_kwargs):
        return Stream()

    monkeypatch.setattr("app.agent_engine.litellm.acompletion", completion)
    monkeypatch.setattr(agent_engine, "_interrupted", _never_interrupted)
    body = payload("stream-task")
    body["output"]["stream"] = True
    response = client.post("/debate/api/debate", headers={"X-Debate-Agent-Key": "gateway-test-secret"}, json=body)
    assert response.status_code == 200
    assert '"delta": {"content": "流式发言"}' in response.text
    assert '"model_name": "server-authoritative-model"' in response.text
    assert response.text.rstrip().endswith("data: [DONE]")

    csrf = login(client)
    dashboard = client.get("/debate/api/admin/dashboard").json()
    candidate = next(item for item in dashboard["memories"] if item["status"] == "pending_review")
    mem0_calls: list[tuple[str, str]] = []

    class FakeMem0:
        def add(self, content, *, user_id, metadata):
            mem0_calls.append((user_id, content))
            assert metadata["review_status"] == "approved"
            return {"results": [{"id": "mem0-approved-id"}]}

        def delete(self, _memory_id):
            return None

    monkeypatch.setattr(settings, "mem0_enabled", True)
    monkeypatch.setattr(mem0_bridge, "_client", FakeMem0())
    approved = client.post(f"/debate/api/admin/memories/{candidate['id']}/approve", headers=csrf, json={})
    assert approved.status_code == 200
    assert mem0_calls == [("debater-1", "流式发言")]
    stateless = client.get("/debate/api/admin/memories/preview?profile_key=debater-1")
    assert stateless.status_code == 200
    assert stateless.json()["stateless"] is True
    assert stateless.json()["approved_long_term_memory"] == []
    scoped = client.get("/debate/api/admin/memories/preview?profile_key=debater-1&match_id=match-a")
    assert scoped.status_code == 200
    assert len(scoped.json()["approved_long_term_memory"]) == 1
    with SessionLocal() as db:
        reviewed = db.get(MemoryItem, candidate["id"])
        assert reviewed.status == "approved" and reviewed.scope == "long_term"
        assert reviewed.metadata_json["mem0_id"] == "mem0-approved-id"
        running = AgentTask(
            task_id="running-task",
            request_hash="x" * 64,
            match_id="match-b",
            room_code="654321",
            profile_key="debater-1",
            status="running",
        )
        db.add(running)
        db.commit()
    interrupted = client.post(
        "/debate/api/tasks/running-task/interrupt",
        headers={"X-Debate-Agent-Key": "gateway-test-secret"},
        json={},
    )
    assert interrupted.status_code == 200 and interrupted.json()["status"] == "interrupted"


def test_gateway_key_is_only_returned_once(client: TestClient) -> None:
    csrf = login(client)
    created = client.post("/debate/api/admin/gateway-keys", headers=csrf, json={"name": "灰度平台"})
    assert created.status_code == 200
    secret = created.json()["secret"]
    assert secret.startswith("dba_")
    dashboard = client.get("/debate/api/admin/dashboard")
    assert secret not in dashboard.text
    key_id = created.json()["item"]["id"]
    revoked = client.post(f"/debate/api/admin/gateway-keys/{key_id}/revoke", headers=csrf, json={})
    assert revoked.status_code == 200
    with SessionLocal() as db:
        assert db.get(GatewayKey, key_id).is_active is False


def test_running_idempotent_task_returns_status_and_recovers_after_restart(client: TestClient, monkeypatch) -> None:
    body = payload("still-running")
    from app.schemas import DebateRequest

    parsed = DebateRequest.model_validate(body)
    with SessionLocal() as db:
        db.add(
            AgentTask(
                task_id="still-running",
                request_hash=request_hash(parsed),
                match_id="match-a",
                room_code="123456",
                profile_key="debater-1",
                status="running",
            )
        )
        db.commit()
    response = client.post(
        "/debate/api/debate",
        headers={"X-Debate-Agent-Key": "gateway-test-secret"},
        json=body,
    )
    assert response.status_code == 202
    assert response.json() == {"task_id": "still-running", "status": "running", "replayed": True}

    enable_provider()
    with SessionLocal() as db:
        task = db.scalar(select(AgentTask).where(AgentTask.task_id == "still-running"))
        task.status = "failed"
        task.error_code = "service_restarted"
        task.error_message = "restart"
        db.commit()

    class Stream:
        def __aiter__(self):
            async def values():
                yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="恢复后的发言"))], usage=None)

            return values()

    async def completion(**_kwargs):
        return Stream()

    monkeypatch.setattr("app.agent_engine.litellm.acompletion", completion)
    monkeypatch.setattr(agent_engine, "_interrupted", _never_interrupted)
    recovered = client.post(
        "/debate/api/debate",
        headers={"X-Debate-Agent-Key": "gateway-test-secret"},
        json=body,
    )
    assert recovered.status_code == 200
    assert sse_content(recovered) == "恢复后的发言"


def test_four_parallel_matches_keep_prompts_tasks_and_memory_isolated(monkeypatch) -> None:
    enable_provider()
    from app.schemas import DebateRequest

    requests = [
        DebateRequest.model_validate(
            payload(f"parallel-{index}", f"独立辩题-{index}")
            | {"match_id": f"match-{index}", "room_code": f"10000{index}"}
        )
        for index in range(1, 5)
    ]
    tasks: list[AgentTask] = []
    with SessionLocal() as db:
        for request in requests:
            task = AgentTask(
                task_id=request.task_id,
                request_hash=request_hash(request),
                match_id=request.match_id,
                room_code=request.room_code,
                profile_key=request.agent_profile,
                status="running",
            )
            db.add(task)
            tasks.append(task)
        db.commit()

    seen_messages: dict[str, str] = {}

    class Stream:
        def __init__(self, text: str):
            self.text = text

        def __aiter__(self):
            async def values():
                yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=self.text))], usage=None)

            return values()

    async def completion(**kwargs):
        rendered = "\n".join(item["content"] for item in kwargs["messages"])
        topic = next(f"独立辩题-{index}" for index in range(1, 5) if f"独立辩题-{index}" in rendered)
        seen_messages[topic] = rendered
        await asyncio.sleep(0.01)
        return Stream(f"只属于{topic}的发言")

    async def within_limit(_provider) -> bool:
        return True

    monkeypatch.setattr("app.agent_engine.litellm.acompletion", completion)
    monkeypatch.setattr(agent_engine, "_interrupted", _never_interrupted)
    monkeypatch.setattr(agent_engine, "_within_provider_limit", within_limit)

    async def run_one(task: AgentTask, request: DebateRequest) -> str:
        prepared = await agent_engine.prepare(request)
        final = ""
        async for event in agent_engine.stream(task, request, prepared):
            if event["type"] == "final":
                final = event["content"]
        return final

    async def run_all() -> list[str]:
        return await asyncio.gather(*(run_one(task, request) for task, request in zip(tasks, requests)))

    results = asyncio.run(run_all())
    assert len(set(results)) == 4
    for index in range(1, 5):
        topic = f"独立辩题-{index}"
        assert topic in seen_messages[topic]
        assert all(f"独立辩题-{other}" not in seen_messages[topic] for other in range(1, 5) if other != index)
    with SessionLocal() as db:
        for index in range(1, 5):
            task = db.scalar(select(AgentTask).where(AgentTask.task_id == f"parallel-{index}"))
            memories = db.scalars(select(MemoryItem).where(MemoryItem.match_id == f"match-{index}")).all()
            assert task.status == "completed"
            assert len(memories) == 1 and f"独立辩题-{index}" in memories[0].content


def test_judge_endpoint_uses_server_provider_and_returns_main_platform_contract(client: TestClient, monkeypatch) -> None:
    enable_provider()
    calls: list[dict] = []

    async def completion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(
                            {
                                "winner": "aff",
                                "affirmative_score": 86.5,
                                "negative_score": 81,
                                "individual_scores": {"aff_1": 88, "neg_1": 82},
                                "reasoning": "正方论证结构更完整，并有效回应了反方核心质疑。",
                            },
                            ensure_ascii=False,
                        )
                    )
                )
            ]
        )

    async def within_limit(_provider) -> bool:
        return True

    monkeypatch.setattr("app.agent_engine.litellm.acompletion", completion)
    monkeypatch.setattr(agent_engine, "_within_provider_limit", within_limit)
    response = client.post(
        "/debate/api/judge",
        json={
            "task_type": "judge",
            "debate_topic": "人工智能时代，还要不要学编程？",
            "debate_history": [
                {"seat_key": "aff_1", "content": "正方立论内容"},
                {"seat_key": "neg_1", "content": "反方立论内容"},
            ],
            "model_name": "client-cannot-override",
            "system_prompt": "请公平裁判并严格返回 JSON。",
            "output": {"stream": False, "language": "zh-CN"},
        },
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "winner": "aff",
        "affirmative_score": 86.5,
        "negative_score": 81.0,
        "individual_scores": {"aff_1": 88, "neg_1": 82},
        "reasoning": "正方论证结构更完整，并有效回应了反方核心质疑。",
    }
    assert calls[0]["model"].endswith("server-authoritative-model")
    assert "client-cannot-override" not in calls[0]["model"]
    assert calls[0]["stream"] is False
    assert calls[0]["extra_body"] == {"enable_thinking": False}
    assert "winner、affirmative_score、negative_score" in calls[0]["messages"][0]["content"]
    assert "请公平裁判并严格返回 JSON" in calls[0]["messages"][0]["content"]
