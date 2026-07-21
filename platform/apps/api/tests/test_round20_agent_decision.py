from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from app.core.secret_crypto import encrypt_secret
from app.services.agent_decision import decide_should_speak, wait_while_current


def _payload(task_id: str, room_code: str = "123456") -> dict:
    return {
        "task_id": task_id,
        "match_id": "match-a",
        "room_code": room_code,
        "debater_name": "乾元",
        "debate_position": "二辩",
        "debate_topic": "人工智能是否提升学习效率？",
        "current_stage": "自由辩论",
        "next_stage": "总结陈词",
        "holder": "正方",
        "debate_history": [],
        "task_type": "debate",
        "max_token": 700,
    }


@pytest.mark.asyncio
async def test_decision_client_uses_sibling_rest_endpoint_and_room_bound_payload() -> None:
    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={"task_id": "decision-a", "should_speak": False, "reason": "没有新增信息"},
        )

    result = await decide_should_speak(
        _payload("decision-a"),
        {
            "endpoint": "https://agent.example/debate/api/debate",
            "secret_ciphertext": encrypt_secret("room-gateway-secret"),
        },
        transport=httpx.MockTransport(handler),
    )
    assert result.should_speak is False and result.fallback is False
    assert captured[0].url == "https://agent.example/debate/api/should-speak"
    assert captured[0].headers["X-Debate-Agent-Key"] == "room-gateway-secret"
    body = json.loads(captured[0].content)
    assert body["room_code"] == "123456"
    assert body["task_id"] == "decision-a"
    assert body["task_type"] == "should_speak"


@pytest.mark.asyncio
async def test_decision_client_failure_is_bounded_fail_open() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "unavailable"})

    result = await decide_should_speak(
        _payload("decision-fail", "654321"),
        {"endpoint": "https://agent.example/debate/api/debate", "secret_ciphertext": ""},
        transport=httpx.MockTransport(handler),
    )
    assert result.should_speak is True
    assert result.fallback is True
    assert result.task_id == "decision-fail"


@pytest.mark.asyncio
async def test_speculative_wait_cancels_when_turn_becomes_invalid() -> None:
    invalidated = asyncio.Event()
    current = True

    async def pending() -> None:
        await asyncio.sleep(30)

    async def invalidate() -> None:
        invalidated.set()

    task = asyncio.create_task(pending())
    current = False
    valid = await wait_while_current({task}, lambda: current, invalidate)
    assert valid is False
    assert task.cancelled()
    assert invalidated.is_set()
