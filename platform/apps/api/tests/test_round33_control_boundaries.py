from __future__ import annotations

import asyncio
from datetime import timedelta

from app.core.database import SessionLocal
from app.models.entities import Match, Speech
from app.services import free_turn_queue
from app.services.match_engine import match_engine
from app.services.room_service import free_turn_remaining_seconds, load_room, now, remaining_seconds
from conftest import csrf
from sqlalchemy import func, select
from test_platform import create_training_room


def _started_room(owner, topic: str) -> str:
    code = create_training_room(owner, topic)["code"]
    assert owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True}).status_code == 200
    assert owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={}).status_code == 200
    return code


def _lease(owner, code: str, value: str) -> dict[str, str]:
    headers = csrf(owner) | {"X-Control-Lease": value}
    response = owner.post(f"/api/rooms/{code}/control-lease", headers=headers, json={})
    assert response.status_code == 200, response.text
    return headers


def test_live_countdown_never_reports_zero_before_deadline(register_user) -> None:
    """A positive fractional second must not be treated as an expired stage."""

    owner = register_user("round33_fractional_countdown")
    code = _started_room(owner, "Round33 倒计时最后一秒边界")
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        deadline = now() + timedelta(milliseconds=120)
        room.status = "running"
        room.current_stage_index = 0
        room.stage_started_at = now()
        room.stage_deadline_at = deadline
        room.template_snapshot = [{"key": "fixed", "name": "固定环节", "kind": "announcement", "duration": 1}]
        assert remaining_seconds(room) == 1
        db.rollback()


def test_live_free_turn_countdown_never_reports_zero_before_deadline(register_user) -> None:
    owner = register_user("round33_fractional_free_countdown")
    code = _started_room(owner, "Round33 自由辩论最后一秒边界")
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        deadline = now() + timedelta(milliseconds=120)
        started = deadline - timedelta(seconds=10)
        room.status = "running"
        room.current_stage_index = 0
        room.stage_started_at = started
        room.stage_deadline_at = deadline
        room.template_snapshot = [
            {
                "key": "free",
                "name": "自由辩论",
                "kind": "free",
                "side": "aff",
                "duration": 30,
                "turn_duration": 10,
                "turn_started_at": started.isoformat(),
            }
        ]
        assert free_turn_remaining_seconds(room) == 1
        db.rollback()


async def test_engine_advances_only_after_precise_stage_deadline(register_user) -> None:
    owner = register_user("round33_precise_engine_deadline")
    code = _started_room(owner, "Round33 引擎精确截止时间")
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.status = "running"
        room.current_stage_index = 0
        room.stage_started_at = now()
        room.stage_deadline_at = now() + timedelta(milliseconds=180)
        room.template_snapshot = [
            {"key": "short", "name": "短环节", "kind": "announcement", "duration": 1},
            {"key": "next", "name": "下一环节", "kind": "announcement", "duration": 30},
        ]
        db.commit()

    await match_engine.process_room(code)
    with SessionLocal() as db:
        assert load_room(db, code).current_stage_index == 0

    await asyncio.sleep(0.2)
    await match_engine.process_room(code)
    with SessionLocal() as db:
        assert load_room(db, code).current_stage_index == 1


async def test_expired_fixed_stage_rejects_late_speech_before_engine_tick(register_user) -> None:
    owner = register_user("round33_expired_fixed_stage")
    code = _started_room(owner, "Round33 固定环节迟到请求")
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.template_snapshot = [
            {"key": "expired", "name": "已经结束的立论", "kind": "speech", "seat": "aff_1", "duration": 30},
            {"key": "next", "name": "下一环节", "kind": "announcement", "duration": 30},
        ]
        room.current_stage_index = 0
        room.status = "running"
        room.stage_started_at = now() - timedelta(seconds=31)
        room.stage_deadline_at = now() - timedelta(milliseconds=1)
        db.commit()

    headers = _lease(owner, code, "round33-expired-fixed-device")
    rejected = owner.post(f"/api/rooms/{code}/speech/start", headers=headers, json={})
    assert rejected.status_code == 403
    assert "环节已经结束" in rejected.json()["detail"]
    with SessionLocal() as db:
        room = load_room(db, code)
        assert db.scalar(select(func.count(Speech.id)).where(Speech.room_id == room.id)) == 0

    await match_engine.process_room(code)
    with SessionLocal() as db:
        room = load_room(db, code)
        assert room.current_stage_index == 1


def test_expired_free_stage_rejects_late_speech_and_hand_raise(register_user) -> None:
    owner = register_user("round33_expired_free_stage")
    code = _started_room(owner, "Round33 自由辩论迟到请求")
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        room.template_snapshot = [
            {
                "key": "free",
                "name": "自由辩论",
                "kind": "free",
                "side": "aff",
                "duration": 120,
                "turn_duration": 45,
                "turn_started_at": (now() - timedelta(seconds=10)).isoformat(),
            }
        ]
        room.current_stage_index = 0
        room.status = "running"
        room.stage_started_at = now() - timedelta(seconds=121)
        room.stage_deadline_at = now() - timedelta(milliseconds=1)
        db.add(
            Speech(
                match_id=match.id,
                room_id=room.id,
                seat_key="neg_1",
                stage_key="free",
                speaker_type="ai",
                status="playing",
                playback_started_at=now() - timedelta(seconds=2),
                playback_ends_at=now() + timedelta(seconds=20),
            )
        )
        db.commit()

    headers = _lease(owner, code, "round33-expired-free-device")
    speech = owner.post(f"/api/rooms/{code}/speech/start", headers=headers, json={})
    request = owner.post(f"/api/rooms/{code}/free-turn-requests", headers=csrf(owner), json={})
    assert speech.status_code == 403 and "环节已经结束" in speech.json()["detail"]
    assert request.status_code == 409 and "环节已经结束" in request.json()["detail"]


def test_free_turn_request_at_exact_intermission_deadline_is_rejected(register_user, monkeypatch) -> None:
    owner = register_user("round33_exact_request_deadline")
    code = _started_room(owner, "Round33 举手窗口精确边界")
    boundary = now() + timedelta(seconds=10)
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.template_snapshot = [
            {
                "key": "free",
                "name": "自由辩论",
                "kind": "free",
                "side": "neg",
                "duration": 120,
                "turn_duration": 45,
                "turn_seq": 1,
                "turn_started_at": now().isoformat(),
                "intermission_side": "aff",
                "intermission_turn_seq": 2,
                "intermission_deadline_at": boundary.isoformat(),
            }
        ]
        room.current_stage_index = 0
        room.status = "running"
        room.stage_started_at = now()
        room.stage_deadline_at = now() + timedelta(seconds=120)
        owner_seat = next(item for item in room.seats if item.user_id == room.owner_id)
        owner_seat.connected = True
        owner_seat.disconnected_at = None
        db.commit()

    monkeypatch.setattr(free_turn_queue, "now", lambda: boundary)
    response = owner.post(f"/api/rooms/{code}/free-turn-requests", headers=csrf(owner), json={})
    assert response.status_code == 409
    assert "申请窗口已经结束" in response.json()["detail"]


def test_resume_and_retry_keep_judging_as_authoritative_room_status(register_user, monkeypatch) -> None:
    owner = register_user("round33_judging_status")
    code = _started_room(owner, "Round33 裁判暂停状态恢复")
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.template_snapshot = [{"key": "judge", "name": "自动裁判", "kind": "judging", "duration": 30}]
        room.current_stage_index = 0
        room.status = "judging"
        room.stage_started_at = now()
        room.stage_deadline_at = None
        db.commit()

    paused = owner.post(f"/api/rooms/{code}/control/pause", headers=csrf(owner), json={"reason": "暂停评议"})
    resumed = owner.post(f"/api/rooms/{code}/control/resume", headers=csrf(owner), json={"reason": "继续评议"})
    assert paused.status_code == 200
    assert resumed.status_code == 200
    assert resumed.json()["room"]["status"] == "judging"
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.status = "paused"
        room.failure_reason = "裁判执行器异常"
        room.paused_remaining_seconds = 15
        db.commit()

    async def ready() -> None:
        return None

    monkeypatch.setattr("app.api.rooms._require_realtime_voice_ready_for_start", ready)
    retried = owner.post(f"/api/rooms/{code}/control/retry", headers=csrf(owner), json={"reason": "重试评议"})
    assert retried.status_code == 200
    assert retried.json()["room"]["status"] == "judging"
    with SessionLocal() as db:
        room = load_room(db, code)
        assert room.stage_deadline_at is None


async def test_retry_preserves_expired_countdown_instead_of_restarting_stage(register_user, monkeypatch) -> None:
    """A provider retry must not turn 00:00 back into an unexplained 00:30."""

    owner = register_user("round33_retry_zero_countdown")
    code = _started_room(owner, "Round33 异常重试保持倒计时边界")
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.template_snapshot = [
            {"key": "expired", "name": "已到期环节", "kind": "announcement", "duration": 30},
            {"key": "next", "name": "下一环节", "kind": "announcement", "duration": 45},
        ]
        room.current_stage_index = 0
        room.status = "paused"
        room.stage_started_at = now() - timedelta(seconds=30)
        room.stage_deadline_at = None
        room.paused_remaining_seconds = 0
        room.failure_reason = "阶段结束边界发生可重试服务异常"
        db.commit()

    async def ready() -> None:
        return None

    monkeypatch.setattr("app.api.rooms._require_realtime_voice_ready_for_start", ready)
    retried = owner.post(
        f"/api/rooms/{code}/control/retry",
        headers=csrf(owner),
        json={"reason": "恢复服务并继续"},
    )
    assert retried.status_code == 200, retried.text
    assert retried.json()["room"]["remaining_seconds"] == 0

    await match_engine.process_room(code)
    with SessionLocal() as db:
        room = load_room(db, code)
        assert room.status == "running"
        assert room.current_stage_index == 1
        assert remaining_seconds(room) >= 44
