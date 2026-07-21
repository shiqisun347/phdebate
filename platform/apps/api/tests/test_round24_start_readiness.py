from __future__ import annotations

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.entities import Match, MatchEvent, RoomSeat, Speech
from app.services.providers import moss_tts_realtime
from app.services.room_service import load_room
from conftest import csrf
from sqlalchemy import func, select
from test_platform import create_training_room


def test_production_start_preserves_lobby_when_moss_is_not_warmed(register_user, monkeypatch) -> None:
    owner = register_user("round24_cold_moss")
    room_view = create_training_room(owner, "实时语音服务未暖机时不应锁定比赛")
    code = room_view["code"]
    ready = owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})
    assert ready.status_code == 200, ready.text

    async def cold_snapshot() -> dict:
        return {
            "ok": False,
            "enabled": True,
            "ready_endpoints": 0,
            "required_endpoints": 1,
            "endpoints": [{"index": 0, "ok": False, "code": "moss_tts_not_ready"}],
        }

    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "realtime_voice_backend", "moss_realtime")
    monkeypatch.setattr(settings, "moss_tts_realtime_enabled", True)
    monkeypatch.setattr(moss_tts_realtime, "readiness_snapshot", cold_snapshot)

    rejected = owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    assert rejected.status_code == 503
    assert rejected.headers["retry-after"] == "5"
    assert "尚未锁定" in rejected.json()["detail"]

    with SessionLocal() as db:
        room = load_room(db, code)
        assert room.status == "lobby"
        assert room.started_at is None
        assert db.scalar(select(func.count(Match.id)).where(Match.room_id == room.id)) == 0
        assert db.scalar(select(func.count(MatchEvent.id)).where(MatchEvent.room_id == room.id)) == 2
        open_seats = db.scalars(
            select(RoomSeat).where(RoomSeat.room_id == room.id, RoomSeat.occupant_type == "open")
        ).all()
        assert open_seats


def test_production_start_accepts_one_warmed_moss_endpoint(register_user, monkeypatch) -> None:
    owner = register_user("round24_ready_moss")
    code = create_training_room(owner, "至少一个实时语音端点暖机后可以开始比赛")["code"]
    ready = owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})
    assert ready.status_code == 200, ready.text

    async def warm_snapshot() -> dict:
        return {
            "ok": False,
            "enabled": True,
            "ready_endpoints": 1,
            "required_endpoints": 3,
            "endpoints": [
                {"index": 0, "ok": True, "model_warmed": True, "active": 1, "pending": 0, "orphan_count": 0},
                {"index": 1, "ok": False, "code": "moss_tts_not_ready"},
                {"index": 2, "ok": False, "code": "moss_tts_not_ready"},
            ],
        }

    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "realtime_voice_backend", "moss_realtime")
    monkeypatch.setattr(settings, "moss_tts_realtime_enabled", True)
    monkeypatch.setattr(moss_tts_realtime, "readiness_snapshot", warm_snapshot)

    started = owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    assert started.status_code == 200, started.text
    assert started.json()["room"]["status"] == "preparing"

    async def cold_after_commit() -> dict:
        return {"ok": False, "enabled": True, "ready_endpoints": 0, "required_endpoints": 3, "endpoints": []}

    monkeypatch.setattr(moss_tts_realtime, "readiness_snapshot", cold_after_commit)
    replayed = owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    assert replayed.status_code == 200, replayed.text
    assert replayed.json()["replayed"] is True


def test_production_retry_keeps_failure_intact_until_moss_is_warmed(register_user, monkeypatch) -> None:
    owner = register_user("round24_retry_cold_moss")
    code = create_training_room(owner, "异常重试必须等待实时语音服务暖机")["code"]
    assert owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True}).status_code == 200
    assert owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={}).status_code == 200

    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        room.status = "paused"
        room.failure_reason = "实时语音服务尚未暖机"
        failed = Speech(
            match_id=match.id,
            room_id=room.id,
            seat_key="neg_1",
            stage_key="opening",
            speaker_type="ai",
            status="failed",
            content="已保存的失败尝试",
        )
        db.add(failed)
        db.commit()
        failed_id = failed.id

    async def cold_snapshot() -> dict:
        return {"ok": False, "enabled": True, "ready_endpoints": 0, "required_endpoints": 1, "endpoints": []}

    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "realtime_voice_backend", "moss_realtime")
    monkeypatch.setattr(settings, "moss_tts_realtime_enabled", True)
    monkeypatch.setattr(moss_tts_realtime, "readiness_snapshot", cold_snapshot)
    headers = csrf(owner) | {"X-Idempotency-Key": "round24-retry-after-warmup"}

    rejected = owner.post(
        f"/api/rooms/{code}/control/retry",
        headers=headers,
        json={"reason": "服务恢复后重试"},
    )
    assert rejected.status_code == 503
    with SessionLocal() as db:
        room = load_room(db, code)
        assert room.status == "paused"
        assert room.failure_reason == "实时语音服务尚未暖机"
        assert db.get(Speech, failed_id).status == "failed"
        assert db.scalar(
            select(func.count(MatchEvent.id)).where(
                MatchEvent.room_id == room.id,
                MatchEvent.event_type == "control.retry",
            )
        ) == 0

    async def warm_snapshot() -> dict:
        return {
            "ok": True,
            "enabled": True,
            "ready_endpoints": 1,
            "required_endpoints": 1,
            "endpoints": [{"index": 0, "ok": True, "model_warmed": True, "active": 0, "pending": 0}],
        }

    monkeypatch.setattr(moss_tts_realtime, "readiness_snapshot", warm_snapshot)
    retried = owner.post(
        f"/api/rooms/{code}/control/retry",
        headers=headers,
        json={"reason": "服务恢复后重试"},
    )
    assert retried.status_code == 200, retried.text
    assert retried.json()["room"]["status"] == "preparing"
    with SessionLocal() as db:
        room = load_room(db, code)
        assert room.failure_reason == ""
        assert db.get(Speech, failed_id).status == "failed_retried"
