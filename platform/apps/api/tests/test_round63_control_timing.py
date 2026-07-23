from __future__ import annotations

from datetime import timedelta

from app.api import rooms as rooms_api
from app.core.database import SessionLocal
from app.models.entities import MatchEvent, Speech
from app.services.room_service import load_room, now
from conftest import csrf
from sqlalchemy import select
from test_platform import create_training_room, prepare_human_speech


def _two_human_started_room(owner, participant, topic: str) -> str:
    code = create_training_room(owner, topic)["code"]
    claimed = participant.post(
        f"/api/rooms/{code}/claim-seat",
        headers=csrf(participant),
        json={"seat_key": "neg_1"},
    )
    assert claimed.status_code == 200, claimed.text
    for browser in (owner, participant):
        ready = browser.post(
            f"/api/rooms/{code}/ready",
            headers=csrf(browser),
            json={"ready": True},
        )
        assert ready.status_code == 200, ready.text
    started = owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    assert started.status_code == 200, started.text
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.status = "running"
        room.current_stage_index = 0
        room.template_snapshot = [
            {"key": "hold", "name": "正常比赛阶段", "kind": "announcement", "duration": 180}
        ]
        room.stage_started_at = now()
        room.stage_deadline_at = now() + timedelta(seconds=180)
        for seat in room.seats:
            if seat.occupant_type == "human":
                seat.connected = True
                seat.disconnected_at = None
        db.commit()
    return code


def test_direct_skip_cannot_discard_an_active_human_speech(register_user) -> None:
    owner = register_user("round63_skip_human_guard")
    code, speech_id = prepare_human_speech(owner)

    skipped = owner.post(
        f"/api/rooms/{code}/control/skip",
        headers=csrf(owner),
        json={"reason": "绕过页面直接跳过真人发言"},
    )

    assert skipped.status_code == 409
    assert "紧急暂停" in skipped.json()["detail"]
    with SessionLocal() as db:
        room = load_room(db, code)
        assert db.get(Speech, speech_id).status == "speaking"
        assert room.current_stage_index == 1
        assert not db.scalar(
            select(MatchEvent.id).where(
                MatchEvent.room_id == room.id,
                MatchEvent.event_type == "control.skip",
            )
        )


def test_recovery_actions_reject_a_new_disconnect_before_timeout(
    register_user,
    monkeypatch,
) -> None:
    owner = register_user("round63_recovery_owner")
    participant = register_user("round63_recovery_participant")
    code = _two_human_started_room(owner, participant, "恢复操作不能绕过实时在线校验")
    monkeypatch.setattr(rooms_api, "RECOVERY_PRESENCE_ENFORCED", True)

    paused = owner.post(
        f"/api/rooms/{code}/control/pause",
        headers=csrf(owner),
        json={"reason": "现场设备检查"},
    )
    assert paused.status_code == 200, paused.text
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        offline = next(seat for seat in room.seats if seat.user_id != room.owner_id and seat.occupant_type == "human")
        offline.connected = False
        offline.disconnected_at = now()
        offline_name = offline.display_name
        db.commit()

    resumed = owner.post(
        f"/api/rooms/{code}/control/resume",
        headers=csrf(owner),
        json={"reason": "不应在辩手刚断线时恢复"},
    )
    assert resumed.status_code == 409
    assert offline_name in resumed.json()["detail"]

    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.failure_reason = "模拟可重试服务异常"
        db.commit()
    retried = owner.post(
        f"/api/rooms/{code}/control/retry",
        headers=csrf(owner),
        json={"reason": "不应在辩手刚断线时重试"},
    )
    assert retried.status_code == 409
    assert offline_name in retried.json()["detail"]

    skipped = owner.post(
        f"/api/rooms/{code}/control/skip",
        headers=csrf(owner),
        json={"reason": "不应通过跳过绕过断线"},
    )
    assert skipped.status_code == 409
    assert offline_name in skipped.json()["detail"]
    # This suite shares one session-scoped SQLite database. Do not leave a
    # synthetic online seat behind for startup presence-reset assertions that
    # intentionally count all connected rows created earlier in the run.
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        for seat in room.seats:
            if seat.occupant_type == "human":
                seat.connected = False
                seat.disconnected_at = now()
        db.commit()
