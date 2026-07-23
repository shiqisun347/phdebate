from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from app.core.database import SessionLocal
from app.models.entities import Match, Speech
from app.services.match_engine import match_engine
from app.services.providers import debate_agent
from app.services.room_service import free_turn_remaining_seconds, load_room, now, remaining_seconds, stage
from conftest import csrf
from sqlalchemy import select
from test_platform import create_training_room


def _started_room(owner, topic: str) -> str:
    code = create_training_room(owner, topic)["code"]
    assert owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True}).status_code == 200
    assert owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={}).status_code == 200
    # API integration tests do not open the debate WebSocket. Model the same
    # authoritative online state that the production room connection writes.
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        for seat in room.seats:
            if seat.occupant_type == "human":
                seat.connected = True
                seat.disconnected_at = None
        db.commit()
    return code


def _lease(owner, code: str, key: str) -> dict[str, str]:
    headers = csrf(owner) | {"X-Control-Lease": key}
    response = owner.post(f"/api/rooms/{code}/control-lease", headers=headers, json={})
    assert response.status_code == 200, response.text
    return headers


@pytest.mark.asyncio
async def test_disconnect_resume_waits_for_restarted_fixed_human_speech(register_user) -> None:
    """A recovered participant must not lose speech time before pressing start."""

    owner = register_user("round57_fixed_disconnect_restart")
    code = _started_room(owner, "断线恢复后重新点击发言才继续计时")
    headers = _lease(owner, code, "round57-fixed-device")
    owner_id = owner.get("/api/auth/session").json()["user"]["id"]

    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.template_snapshot = [
            {"key": "aff_case", "name": "正方立论", "kind": "speech", "seat": "aff_1", "duration": 120}
        ]
        match_engine._enter_stage(db, room, 0)
        db.commit()

    started = owner.post(f"/api/rooms/{code}/speech/start", headers=headers, json={})
    assert started.status_code == 200, started.text
    first_speech_id = started.json()["speech_id"]
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        seat = next(item for item in room.seats if item.user_id == owner_id)
        room.stage_started_at = now() - timedelta(seconds=45)
        room.stage_deadline_at = now() + timedelta(seconds=75)
        seat.connected = False
        seat.disconnected_at = now() - timedelta(seconds=61)
        db.commit()

    await match_engine.process_room(code)
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        seat = next(item for item in room.seats if item.user_id == owner_id)
        assert room.status == "paused"
        assert db.get(Speech, first_speech_id).status == "interrupted"
        assert stage(room)["awaiting_human_start"] is True
        assert 74 <= int(stage(room)["human_speech_duration_seconds"]) <= 75
        seat.connected = True
        seat.disconnected_at = None
        db.commit()

    resumed = owner.post(
        f"/api/rooms/{code}/control/resume",
        headers=csrf(owner),
        json={"reason": "真人已经返回，等待本人重新开麦"},
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["room"]["remaining_seconds"] in {74, 75}
    with SessionLocal() as db:
        room = load_room(db, code)
        assert room.stage_deadline_at is None
        assert stage(room)["awaiting_human_start"] is True

    # Arbitrarily many engine ticks while the participant checks their device
    # must neither consume time nor advance the stage.
    for _ in range(3):
        await match_engine.process_room(code)
    with SessionLocal() as db:
        room = load_room(db, code)
        assert room.current_stage_index == 0
        assert room.stage_deadline_at is None
        assert remaining_seconds(room) in {74, 75}

    restarted = owner.post(f"/api/rooms/{code}/speech/start", headers=headers, json={})
    assert restarted.status_code == 200, restarted.text
    assert restarted.json()["speech_id"] != first_speech_id
    with SessionLocal() as db:
        room = load_room(db, code)
        assert room.stage_deadline_at is not None
        assert "awaiting_human_start" not in stage(room)
        assert 73 <= remaining_seconds(room) <= 75


@pytest.mark.asyncio
async def test_safe_pause_free_human_turn_freezes_total_and_turn_until_restart(register_user) -> None:
    """Emergency pause must not restart either free-debate clock on resume."""

    owner = register_user("round57_free_safe_pause")
    code = _started_room(owner, "自由辩论紧急暂停恢复计时")
    headers = _lease(owner, code, "round57-free-device")
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.template_snapshot = [
            {
                "key": "free",
                "name": "自由辩论",
                "kind": "free",
                "side": "aff",
                "duration": 180,
                "turn_duration": 30,
            }
        ]
        match_engine._enter_stage(db, room, 0)
        db.commit()

    started = owner.post(f"/api/rooms/{code}/speech/start", headers=headers, json={})
    assert started.status_code == 200, started.text
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        current = dict(stage(room))
        current["turn_started_at"] = (now() - timedelta(seconds=10)).isoformat()
        snapshot = list(room.template_snapshot)
        snapshot[0] = current
        room.template_snapshot = snapshot
        room.stage_started_at = now() - timedelta(seconds=80)
        room.stage_deadline_at = now() + timedelta(seconds=100)
        db.commit()

    paused = owner.post(
        f"/api/rooms/{code}/control/safe-pause",
        headers=csrf(owner),
        json={"reason": "麦克风异常，紧急暂停"},
    )
    assert paused.status_code == 200, paused.text
    assert paused.json()["room"]["remaining_seconds"] in {99, 100}
    assert paused.json()["room"]["turn_remaining_seconds"] in {19, 20}

    resumed = owner.post(
        f"/api/rooms/{code}/control/resume",
        headers=csrf(owner),
        json={"reason": "设备恢复，等待辩手重新发言"},
    )
    assert resumed.status_code == 200, resumed.text
    with SessionLocal() as db:
        room = load_room(db, code)
        current = stage(room)
        assert room.stage_deadline_at is None
        assert current["awaiting_human_start"] is True
        assert remaining_seconds(room) in {99, 100}
        assert free_turn_remaining_seconds(room, current) in {19, 20}

    for _ in range(3):
        await match_engine.process_room(code)
    with SessionLocal() as db:
        room = load_room(db, code)
        assert room.stage_deadline_at is None
        assert remaining_seconds(room) in {99, 100}
        assert free_turn_remaining_seconds(room) in {19, 20}

    restarted = owner.post(f"/api/rooms/{code}/speech/start", headers=headers, json={})
    assert restarted.status_code == 200, restarted.text
    with SessionLocal() as db:
        room = load_room(db, code)
        assert room.stage_deadline_at is not None
        assert remaining_seconds(room) in {99, 100}
        assert free_turn_remaining_seconds(room) in {19, 20}


@pytest.mark.asyncio
async def test_disconnected_free_side_human_is_not_silently_replaced_by_permanent_ai(
    client,
    register_user,
    monkeypatch,
) -> None:
    """A permanent AI teammate must not consume a disconnected human window."""

    owner = register_user("round57_free_no_silent_ai")
    competition = client.get("/api/competitions/daily-4v4").json()["competition"]
    response = owner.post(
        "/api/rooms",
        headers=csrf(owner),
        json={
            "competition_slug": "daily-4v4",
            "topic_id": competition["topics"][0]["id"],
            "seat_key": "aff_1",
            "visibility": "public",
        },
    )
    assert response.status_code == 200, response.text
    code = response.json()["room"]["code"]
    assert owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True}).status_code == 200
    assert owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={}).status_code == 200
    owner_id = owner.get("/api/auth/session").json()["user"]["id"]
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        human = next(item for item in room.seats if item.user_id == owner_id)
        assert human.side == "aff"
        assert any(item.side == "aff" and item.occupant_type == "ai" for item in room.seats)
        room.template_snapshot = [
            {
                "key": "free",
                "name": "自由辩论",
                "kind": "free",
                "side": "aff",
                "duration": 180,
                "turn_duration": 45,
            }
        ]
        match_engine._enter_stage(db, room, 0)
        human.connected = False
        human.disconnected_at = now() - timedelta(seconds=30)
        db.commit()

    generated = AsyncMock(return_value="这段 AI 发言不应在真人断线宽限期内生成。")
    monkeypatch.setattr(debate_agent, "generate", generated)
    for _ in range(3):
        await match_engine.process_room(code)
    with SessionLocal() as db:
        room = load_room(db, code)
        assert room.status == "running"
        assert room.current_stage_index == 0
        assert stage(room)["awaiting_human_start"] is True
        assert not db.query(Speech).filter(Speech.room_id == room.id).count()
    generated.assert_not_awaited()

    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        human = next(item for item in room.seats if item.user_id == owner_id)
        human.disconnected_at = now() - timedelta(seconds=61)
        db.commit()
    await match_engine.process_room(code)
    with SessionLocal() as db:
        room = load_room(db, code)
        assert room.status == "paused"
        assert all(item.occupant_type in {"human", "ai"} for item in room.seats)
        assert not db.query(Speech).filter(Speech.room_id == room.id).count()
    generated.assert_not_awaited()


def test_skipped_provider_failure_does_not_poison_later_pause_resume(register_user) -> None:
    """Historical failed speech rows must not block an unrelated later stage."""

    owner = register_user("round57_skip_failure_recovery")
    code = _started_room(owner, "跳过服务失败后后续阶段仍可暂停恢复")
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        room.template_snapshot = [
            {"key": "ai_failed", "name": "AI 立论", "kind": "speech", "seat": "neg_1", "duration": 90},
            {"key": "human_next", "name": "真人回应", "kind": "speech", "seat": "aff_1", "duration": 120},
        ]
        room.status = "paused"
        room.current_stage_index = 0
        room.stage_started_at = now() - timedelta(seconds=5)
        room.stage_deadline_at = None
        room.paused_remaining_seconds = 85
        room.failure_reason = "模拟当前 AI 服务失败"
        failed = Speech(
            match_id=match.id,
            room_id=room.id,
            seat_key="neg_1",
            stage_key="ai_failed",
            speaker_type="ai",
            status="failed",
            content="已生成但未能播放的文本。",
        )
        db.add(failed)
        db.commit()
        failed_id = failed.id

    skipped = owner.post(
        f"/api/rooms/{code}/control/skip",
        headers=csrf(owner),
        json={"reason": "当前 AI 服务异常，跳过该阶段"},
    )
    assert skipped.status_code == 200, skipped.text
    assert skipped.json()["room"]["status"] == "running"
    assert skipped.json()["room"]["current_stage_index"] == 1
    with SessionLocal() as db:
        room = load_room(db, code)
        assert db.get(Speech, failed_id).status == "failed_skipped"
        assert stage(room)["awaiting_human_start"] is True

    paused = owner.post(
        f"/api/rooms/{code}/control/pause",
        headers=csrf(owner),
        json={"reason": "下一阶段正常设备检查"},
    )
    assert paused.status_code == 200, paused.text
    resumed = owner.post(
        f"/api/rooms/{code}/control/resume",
        headers=csrf(owner),
        json={"reason": "设备检查结束"},
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["room"]["status"] == "running"
    assert resumed.json()["room"]["current_stage_index"] == 1


@pytest.mark.asyncio
async def test_zero_second_recovered_human_stage_cannot_resurrect_time(register_user) -> None:
    owner = register_user("round57_zero_human_recovery")
    code = _started_room(owner, "真人恢复时零秒边界不得复活")
    headers = _lease(owner, code, "round57-zero-device")
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.template_snapshot = [
            {
                "key": "expired_human",
                "name": "已到时真人阶段",
                "kind": "speech",
                "seat": "aff_1",
                "duration": 120,
                "awaiting_human_start": True,
                "human_speech_duration_seconds": 0,
            },
            {"key": "next", "name": "下一阶段", "kind": "announcement", "duration": 30},
        ]
        room.status = "running"
        room.current_stage_index = 0
        room.stage_started_at = None
        room.stage_deadline_at = None
        db.commit()

    blocked = owner.post(f"/api/rooms/{code}/speech/start", headers=headers, json={})
    assert blocked.status_code == 403
    assert "已经结束" in blocked.json()["detail"]
    await match_engine.process_room(code)
    with SessionLocal() as db:
        room = load_room(db, code)
        assert room.current_stage_index == 1
        assert not db.query(Speech).filter(Speech.room_id == room.id).count()
