from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from app.core.database import SessionLocal
from app.main import app
from app.models.entities import Match, MatchEvent, Room, Speech
from app.services.match_engine import match_engine
from app.services.room_service import load_room, now
from conftest import csrf
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_platform import create_training_room


def _two_human_room(owner, participant, topic: str) -> str:
    code = create_training_room(owner, topic)["code"]
    claimed = participant.post(
        f"/api/rooms/{code}/claim-seat",
        headers=csrf(participant),
        json={"seat_key": "neg_1"},
    )
    assert claimed.status_code == 200, claimed.text
    for browser in (owner, participant):
        response = browser.post(f"/api/rooms/{code}/ready", headers=csrf(browser), json={"ready": True})
        assert response.status_code == 200, response.text
    started = owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    assert started.status_code == 200, started.text
    return code


def _lease(browser, code: str, lease: str) -> dict[str, str]:
    headers = csrf(browser) | {"X-Control-Lease": lease}
    response = browser.post(f"/api/rooms/{code}/control-lease", headers=headers, json={})
    assert response.status_code == 200, response.text
    return headers


def test_reconnected_human_can_finalize_a_timed_out_turn_without_replacement(register_user) -> None:
    owner = register_user("round13_late_human_owner")
    returning = register_user("round13_late_human_returning")
    code = _two_human_room(owner, returning, "Round13 真人断线后迟到文字保留")

    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        room.status = "running"
        room.template_snapshot = [
            {"key": "neg_case", "name": "反方立论", "kind": "speech", "seat": "neg_1", "duration": 60},
            {"key": "aff_summary", "name": "正方总结", "kind": "speech", "seat": "aff_1", "duration": 60},
        ]
        room.current_stage_index = 1
        room.stage_started_at = now()
        room.stage_deadline_at = now() + timedelta(seconds=60)
        stale = Speech(
            match_id=match.id,
            room_id=room.id,
            seat_key="neg_1",
            stage_key="neg_case",
            speaker_type="human",
            status="timed_out",
        )
        db.add(stale)
        db.commit()
        stale_id = stale.id

    headers = _lease(returning, code, "round13-returned-device")
    finalized = returning.post(
        f"/api/rooms/{code}/speech/finish",
        headers=headers | {"X-Idempotency-Key": "round13-late-human-finish"},
        json={"speech_id": stale_id, "content": "这是断线设备恢复后提交的已确认真人发言。"},
    )
    assert finalized.status_code == 200, finalized.text

    with SessionLocal() as db:
        speech = db.get(Speech, stale_id)
        assert speech and speech.status == "completed"
        assert speech.speaker_type == "human"
        assert speech.content == "这是断线设备恢复后提交的已确认真人发言。"
        assert not db.scalar(
            select(Speech.id).where(
                Speech.match_id == speech.match_id,
                Speech.seat_key == speech.seat_key,
                Speech.stage_key == speech.stage_key,
                Speech.speaker_type == "ai",
            )
        )


def test_late_free_debate_turn_is_preserved_even_after_later_ai_turn(register_user) -> None:
    """Free debate intentionally permits repeated seat/stage speeches.

    The fixed-stage takeover guard must not erase a recoverable earlier human
    turn merely because the same seat produced a later AI free-debate turn.
    """

    owner = register_user("round13_free_late_owner")
    code = create_training_room(owner, "Round13 自由辩论迟到文字保留")["code"]
    assert owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True}).status_code == 200
    assert owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={}).status_code == 200
    lease = "round13-free-returned-device"
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        seat = next(item for item in room.seats if item.seat_key == "aff_1")
        room.status = "running"
        room.template_snapshot = [
            {"key": "free", "name": "自由辩论", "kind": "free", "side": "neg", "duration": 180, "turn_duration": 30}
        ]
        room.current_stage_index = 0
        room.stage_started_at = now()
        room.stage_deadline_at = now() + timedelta(seconds=180)
        seat.control_lease = lease
        stale = Speech(
            match_id=match.id,
            room_id=room.id,
            seat_key="aff_1",
            stage_key="free",
            speaker_type="human",
            status="timed_out",
        )
        later = Speech(
            match_id=match.id,
            room_id=room.id,
            seat_key="aff_1",
            stage_key="free",
            speaker_type="ai",
            status="completed",
            content="稍后轮次中 AI 接替形成的另一段自由辩论发言。",
        )
        db.add_all([stale, later])
        db.commit()
        stale_id = stale.id

    finalized = owner.post(
        f"/api/rooms/{code}/speech/finish",
        headers=csrf(owner) | {"X-Control-Lease": lease},
        json={"speech_id": stale_id, "content": "较早轮次的真人发言在弱网恢复后仍应写入自由辩论历史。"},
    )
    assert finalized.status_code == 200, finalized.text
    assert finalized.json()["timed_out"] is True
    with SessionLocal() as db:
        stored = db.get(Speech, stale_id)
        assert stored and stored.status == "completed" and "较早轮次" in stored.content


@pytest.mark.asyncio
async def test_disconnected_speaking_owner_is_paused_without_identity_or_control_transfer(register_user) -> None:
    owner = register_user("round13_speaking_owner")
    successor = register_user("round13_owner_successor")
    code = _two_human_room(owner, successor, "Round13 房主发言中断线后的控制权恢复")
    owner_id = owner.get("/api/auth/session").json()["user"]["id"]
    successor_id = successor.get("/api/auth/session").json()["user"]["id"]

    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.status = "running"
        room.template_snapshot = [
            {"key": "aff_case", "name": "正方立论", "kind": "speech", "seat": "aff_1", "duration": 90}
        ]
        room.current_stage_index = 0
        room.stage_started_at = now()
        room.stage_deadline_at = now() + timedelta(seconds=90)
        successor_seat = next(item for item in room.seats if item.user_id == successor_id)
        successor_seat.connected = True
        successor_seat.disconnected_at = None
        db.commit()

    headers = _lease(owner, code, "round13-owner-old-device")
    started = owner.post(f"/api/rooms/{code}/speech/start", headers=headers, json={})
    assert started.status_code == 200, started.text
    speech_id = started.json()["speech_id"]
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        owner_seat = next(item for item in room.seats if item.user_id == owner_id)
        owner_seat.connected = False
        owner_seat.disconnected_at = now() - timedelta(seconds=61)
        db.commit()

    # Isolate presence recovery from provider/audio execution: the invariant
    # under test is a transactional safe pause without any replacement Agent.
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        assert await match_engine._expire_presence(db, room) is True
        db.commit()

    stale_finish = owner.post(
        f"/api/rooms/{code}/speech/finish",
        headers=headers,
        json={"speech_id": speech_id, "content": "旧设备迟到提交不应绕过断线安全暂停。"},
    )
    assert stale_finish.status_code == 409
    old_control = owner.post(
        f"/api/rooms/{code}/control/pause",
        headers=csrf(owner),
        json={"reason": "旧房主页面迟到操作"},
    )
    assert old_control.status_code == 409
    new_control = successor.post(
        f"/api/rooms/{code}/control/pause",
        headers=csrf(successor),
        json={"reason": "非房主不能接管断线比赛"},
    )
    assert new_control.status_code == 403

    with SessionLocal() as db:
        room = load_room(db, code)
        speech = db.get(Speech, speech_id)
        owner_seat = next(item for item in room.seats if item.user_id == owner_id)
        assert room.owner_id == owner_id and room.status == "paused"
        assert owner_seat.occupant_type == "human"
        assert speech and speech.status == "interrupted" and speech.content == ""
        transfer_events = list(
            db.scalars(
                select(MatchEvent).where(
                    MatchEvent.room_id == room.id,
                    MatchEvent.event_type == "room.owner_transferred",
                )
            ).all()
        )
        assert transfer_events == []
        assert db.scalar(
            select(MatchEvent.id).where(
                MatchEvent.room_id == room.id,
                MatchEvent.event_type == "participant.disconnect_timeout",
            )
        )


def test_paused_room_rejects_new_human_turn_without_mutating_timeline(register_user) -> None:
    owner = register_user("round13_paused_submit")
    code = create_training_room(owner, "Round13 暂停时真人不能创建新发言")["code"]
    assert owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True}).status_code == 200
    assert owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={}).status_code == 200
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.status = "paused"
        room.template_snapshot = [
            {"key": "aff_case", "name": "正方立论", "kind": "speech", "seat": "aff_1", "duration": 60}
        ]
        room.current_stage_index = 0
        room.stage_started_at = now()
        room.stage_deadline_at = None
        room.paused_remaining_seconds = 45
        db.commit()
        before_seq = room.seq

    headers = _lease(owner, code, "round13-paused-device")
    with SessionLocal() as db:
        before_seq = load_room(db, code).seq
    response = owner.post(
        f"/api/rooms/{code}/speech/start",
        headers=headers | {"X-Idempotency-Key": "round13-paused-start"},
        json={},
    )
    assert response.status_code == 403
    assert "暂停" in response.json()["detail"]
    with SessionLocal() as db:
        room = load_room(db, code)
        assert room.seq == before_seq and room.status == "paused"
        assert not db.scalar(select(Speech.id).where(Speech.room_id == room.id))


def test_concurrent_rematch_replays_one_room_instead_of_forking_duplicate_lobbies(register_user) -> None:
    owner = register_user("round13_concurrent_rematch")
    source_code = create_training_room(owner, "Round13 同拍再次比赛幂等性")["code"]
    assert owner.post(f"/api/rooms/{source_code}/ready", headers=csrf(owner), json={"ready": True}).status_code == 200
    assert owner.post(f"/api/rooms/{source_code}/start", headers=csrf(owner), json={}).status_code == 200
    with SessionLocal() as db:
        source = load_room(db, source_code, lock=True)
        match = db.scalar(select(Match).where(Match.room_id == source.id))
        source.status = "completed"
        source.completed_at = now()
        match.status = "completed"
        db.commit()
        source_id = source.id

    second_tab = TestClient(app)
    with second_tab:
        second_tab.cookies.update(owner.cookies)
        barrier = Barrier(2)

        def rematch(browser: TestClient):
            barrier.wait()
            return browser.post(
                f"/api/rooms/{source_code}/rematch",
                headers=csrf(browser) | {"X-Idempotency-Key": "round13-rematch-double-click"},
                json={},
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(rematch, (owner, second_tab)))

    assert [item.status_code for item in responses] == [200, 200]
    codes = {item.json()["room"]["code"] for item in responses}
    assert len(codes) == 1
    assert sorted(bool(item.json().get("replayed")) for item in responses) == [False, True]
    with SessionLocal() as db:
        children = list(
            db.scalars(
                select(Room).where(
                    Room.id != source_id,
                    Room.topic == "Round13 同拍再次比赛幂等性",
                    Room.owner_id == load_room(db, source_code).owner_id,
                )
            ).all()
        )
        assert len(children) == 1 and children[0].code in codes and children[0].status == "lobby"
        created_events = list(
            db.scalars(
                select(MatchEvent).where(
                    MatchEvent.room_id == children[0].id,
                    MatchEvent.event_type == "room.created",
                )
            ).all()
        )
        assert len(created_events) == 1 and created_events[0].payload["rematch_of"] == source_code
