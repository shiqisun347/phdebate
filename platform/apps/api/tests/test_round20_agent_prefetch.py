from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from app.core.database import SessionLocal
from app.models.entities import RoomSeat
from app.services.match_engine import MatchEngine
from app.services.providers import ProviderError, debate_agent
from app.services.room_service import load_room, now
from conftest import csrf
from sqlalchemy import select
from test_platform import create_training_room


def _running_announcement_room(owner, topic: str) -> str:
    code = create_training_room(owner, topic)["code"]
    assert owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True}).status_code == 200
    assert owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={}).status_code == 200
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.status = "running"
        room.template_snapshot = [
            {"key": "host", "name": "主持人口播", "kind": "announcement", "duration": 20},
            {"key": "neg_case", "name": "反方一辩立论", "kind": "speech", "seat": "neg_1", "duration": 60},
        ]
        room.current_stage_index = 0
        room.stage_started_at = now()
        room.stage_deadline_at = now() + timedelta(seconds=20)
        seat = db.scalar(select(RoomSeat).where(RoomSeat.room_id == room.id, RoomSeat.seat_key == "neg_1"))
        assert seat is not None and seat.occupant_type == "ai"
        db.commit()
    return code


@pytest.mark.asyncio
async def test_fixed_ai_text_prefetch_is_idempotent_room_scoped_and_consumable(
    register_user, monkeypatch
) -> None:
    owner_a = register_user("prefetch_owner_a")
    owner_b = register_user("prefetch_owner_b")
    code_a = _running_announcement_room(owner_a, "预取房间甲")
    code_b = _running_announcement_room(owner_b, "预取房间乙")
    calls: list[dict] = []

    async def generate(payload, *, provider_config=None):
        calls.append(payload)
        return f"这是{payload['debate_topic']}的预生成完整立论内容。"

    monkeypatch.setattr(debate_agent, "generate", generate)
    engine = MatchEngine()
    engine._ensure_runtime()
    await engine.process_room(code_a)
    first_task = engine._agent_prefetch_tasks[code_a]
    engine._schedule_next_agent_prefetch(code_a, 0)
    assert engine._agent_prefetch_tasks[code_a] is first_task
    engine._schedule_next_agent_prefetch(code_b, 0)
    await first_task
    await engine._agent_prefetch_tasks[code_b]

    assert len(calls) == 2
    assert calls[0]["room_code"] != calls[1]["room_code"]
    assert calls[0]["task_id"] != calls[1]["task_id"]
    assert set(engine._agent_prefetch_cache) == {code_a, code_b}

    with SessionLocal() as db:
        room = load_room(db, code_a, lock=True)
        room.current_stage_index = 1
        seat = next(item for item in room.seats if item.seat_key == "neg_1")
        content = engine._consume_agent_prefetch(db, room, room.template_snapshot[1], seat)
        assert content == "这是预取房间甲的预生成完整立论内容。"
    assert code_a not in engine._agent_prefetch_cache
    assert code_b in engine._agent_prefetch_cache
    with SessionLocal() as db:
        room = load_room(db, code_b, lock=True)
        room.current_stage_index = 1
        room.topic = "房间乙已经换题"
        seat = next(item for item in room.seats if item.seat_key == "neg_1")
        assert engine._consume_agent_prefetch(db, room, room.template_snapshot[1], seat) == ""
    assert code_b not in engine._agent_prefetch_cache


@pytest.mark.asyncio
async def test_prefetch_discards_changed_seat_and_pause_cancels_pending_work(
    register_user, monkeypatch
) -> None:
    owner = register_user("prefetch_invalidation_owner")
    code = _running_announcement_room(owner, "预取失效边界")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def generate(payload, *, provider_config=None):
        entered.set()
        await release.wait()
        return "这段文本不应进入已经变化的比赛。"

    monkeypatch.setattr(debate_agent, "generate", generate)
    engine = MatchEngine()
    engine._ensure_runtime()
    engine._schedule_next_agent_prefetch(code, 0)
    pending_task = engine._agent_prefetch_tasks[code]
    await entered.wait()
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.status = "paused"
        db.commit()
    await engine.process_room(code)
    assert code not in engine._agent_prefetch_tasks
    assert code not in engine._agent_prefetch_cache
    release.set()
    await asyncio.gather(pending_task, return_exceptions=True)

    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.status = "running"
        room.current_stage_index = 0
        seat = next(item for item in room.seats if item.seat_key == "neg_1")
        seat.occupant_type = "human"
        db.commit()
    engine._schedule_next_agent_prefetch(code, 0)
    assert code not in engine._agent_prefetch_tasks
    assert code not in engine._agent_prefetch_cache


@pytest.mark.asyncio
async def test_prefetch_provider_failure_is_attempted_once_per_fingerprint(
    register_user, monkeypatch
) -> None:
    owner = register_user("prefetch_failure_owner")
    code = _running_announcement_room(owner, "预取失败退避")
    calls = 0

    async def generate(payload, *, provider_config=None):
        nonlocal calls
        calls += 1
        raise ProviderError("上游暂时不可用", code="agent_unavailable", retryable=True)

    monkeypatch.setattr(debate_agent, "generate", generate)
    engine = MatchEngine()
    engine._ensure_runtime()
    engine._schedule_next_agent_prefetch(code, 0)
    await engine._agent_prefetch_tasks[code]
    engine._schedule_next_agent_prefetch(code, 0)

    assert calls == 1
    assert code not in engine._agent_prefetch_tasks
    assert code not in engine._agent_prefetch_cache
