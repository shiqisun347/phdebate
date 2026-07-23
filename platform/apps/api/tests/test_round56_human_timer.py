from __future__ import annotations

from app.core.database import SessionLocal
from app.services.match_engine import match_engine
from app.services.room_service import load_room, remaining_seconds
from conftest import csrf
from test_platform import create_training_room


def _started_room(owner, topic: str) -> str:
    code = create_training_room(owner, topic)["code"]
    assert owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True}).status_code == 200
    assert owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={}).status_code == 200
    return code


def _lease(owner, code: str) -> dict[str, str]:
    headers = csrf(owner) | {"X-Control-Lease": "round56-human-timer-device"}
    response = owner.post(f"/api/rooms/{code}/control-lease", headers=headers, json={})
    assert response.status_code == 200, response.text
    return headers


def test_fixed_human_countdown_starts_only_after_explicit_speech_start(register_user) -> None:
    owner = register_user("round56_human_timer")
    code = _started_room(owner, "真人点击发言后才开始计时")
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.template_snapshot = [{"key": "aff_human", "name": "正方立论", "kind": "speech", "seat": "aff_1", "duration": 120}]
        match_engine._enter_stage(db, room, 0)
        db.commit()

    with SessionLocal() as db:
        room = load_room(db, code)
        assert room.status == "running"
        assert room.stage_started_at is None
        assert room.stage_deadline_at is None
        assert room.template_snapshot[0]["awaiting_human_start"] is True
        assert remaining_seconds(room) == 120

    paused = owner.post(
        f"/api/rooms/{code}/control/pause",
        headers=csrf(owner),
        json={"reason": "开麦前检查设备"},
    )
    assert paused.status_code == 200, paused.text
    resumed = owner.post(
        f"/api/rooms/{code}/control/resume",
        headers=csrf(owner),
        json={"reason": "设备检查完成"},
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["room"]["remaining_seconds"] == 120

    with SessionLocal() as db:
        room = load_room(db, code)
        assert room.stage_started_at is None
        assert room.stage_deadline_at is None
        assert room.template_snapshot[0]["awaiting_human_start"] is True

    started = owner.post(
        f"/api/rooms/{code}/speech/start",
        headers=_lease(owner, code),
        json={},
    )
    assert started.status_code == 200, started.text
    assert started.json()["room"]["remaining_seconds"] in {119, 120}

    with SessionLocal() as db:
        room = load_room(db, code)
        assert room.stage_started_at is not None
        assert room.stage_deadline_at is not None
        assert "awaiting_human_start" not in room.template_snapshot[0]
        assert 119 <= remaining_seconds(room) <= 120


def test_permanent_ai_fixed_stage_keeps_automatic_clock(register_user) -> None:
    owner = register_user("round56_ai_timer")
    code = _started_room(owner, "AI 固定阶段仍由系统自动推进")
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.template_snapshot = [{"key": "neg_ai", "name": "反方立论", "kind": "speech", "seat": "neg_1", "duration": 90}]
        match_engine._enter_stage(db, room, 0)
        assert room.stage_started_at is not None
        assert room.stage_deadline_at is not None
        assert "awaiting_human_start" not in room.template_snapshot[0]
        assert remaining_seconds(room) in {89, 90}
        db.rollback()
