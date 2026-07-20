from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from app.core.database import SessionLocal
from app.main import app
from app.models.entities import Match, MatchEvent, Room, Speech, TranscriptSegment
from app.services.match_archive import build_match_archive
from app.services.match_engine import match_engine
from app.services.room_service import load_room, now
from conftest import csrf
from fastapi.testclient import TestClient
from sqlalchemy import func, select
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


def test_restored_human_cannot_duplicate_turn_already_completed_by_ai(register_user) -> None:
    """A stale browser must not overwrite an AI takeover's authoritative turn.

    Weak-network finalization remains supported when no replacement exists.
    Once an AI substitute has completed the exact seat/stage, accepting the old
    retained transcript would count the same turn twice in judging and export.
    """

    owner = register_user("round13_takeover_owner")
    returning = register_user("round13_takeover_returning")
    code = _two_human_room(owner, returning, "Round13 AI 接替后迟到提交不得重复")

    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        seat = next(item for item in room.seats if item.seat_key == "neg_1")
        room.status = "running"
        room.template_snapshot = [
            {"key": "neg_case", "name": "反方立论", "kind": "speech", "seat": "neg_1", "duration": 60},
            {"key": "aff_summary", "name": "正方总结", "kind": "speech", "seat": "aff_1", "duration": 60},
        ]
        room.current_stage_index = 1
        room.stage_started_at = now()
        room.stage_deadline_at = now() + timedelta(seconds=60)
        seat.occupant_type = "ai_substitute"
        seat.display_name = f"AI 接替·{seat.display_name}"
        seat.connected = True
        seat.disconnected_at = None
        seat.control_lease = ""
        seat.control_session_id = None
        stale = Speech(
            match_id=match.id,
            room_id=room.id,
            seat_key="neg_1",
            stage_key="neg_case",
            speaker_type="human",
            status="timed_out",
        )
        replacement = Speech(
            match_id=match.id,
            room_id=room.id,
            seat_key="neg_1",
            stage_key="neg_case",
            speaker_type="ai",
            status="completed",
            content="AI 接替辩手已经完成本轮反方立论，形成唯一权威记录。",
        )
        db.add_all([stale, replacement])
        db.commit()
        stale_id = stale.id
        match_id = match.id
        seq_before_restore = room.seq

    requested = returning.post(
        f"/api/rooms/{code}/seat-restore-requests",
        headers=csrf(returning) | {"X-Idempotency-Key": "round13-restore-after-ai"},
        json={},
    )
    assert requested.status_code == 200, requested.text
    request_id = requested.json()["request"]["id"]
    approved = owner.post(
        f"/api/rooms/{code}/seat-restore-requests/{request_id}/approve",
        headers=csrf(owner),
        json={"reason": "辩手已返回，但接替轮次已经完成"},
    )
    assert approved.status_code == 200, approved.text
    headers = _lease(returning, code, "round13-returned-device")

    with SessionLocal() as db:
        room = load_room(db, code)
        seq_before_late_finish = room.seq
    rejected = returning.post(
        f"/api/rooms/{code}/speech/finish",
        headers=headers | {"X-Idempotency-Key": "round13-stale-finish"},
        json={
            "speech_id": stale_id,
            "content": "这是旧设备恢复后迟到的本地文字，不能覆盖已经完成的 AI 接替发言。",
        },
    )
    assert rejected.status_code == 409
    assert "接替辩手完成" in rejected.json()["detail"]

    with SessionLocal() as db:
        room = load_room(db, code)
        stale = db.get(Speech, stale_id)
        assert room.seq == seq_before_late_finish
        assert room.seq > seq_before_restore  # restore and lease remain audited
        assert stale and stale.status == "timed_out" and stale.content == ""
        assert db.scalar(select(func.count(TranscriptSegment.id)).where(TranscriptSegment.speech_id == stale_id)) == 0
        completed = list(
            db.scalars(
                select(Speech).where(
                    Speech.match_id == match_id,
                    Speech.seat_key == "neg_1",
                    Speech.stage_key == "neg_case",
                    Speech.status == "completed",
                )
            ).all()
        )
        assert len(completed) == 1 and completed[0].speaker_type == "ai"

    archive = build_match_archive(match_id)
    assert archive.sha256 and archive.source_sha256


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
async def test_disconnected_speaking_owner_is_substituted_and_stale_device_loses_authority(register_user) -> None:
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
    # under test is the transactional handoff before any replacement Agent is
    # scheduled.
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        assert await match_engine._expire_presence(db, room) is True
        db.commit()

    stale_finish = owner.post(
        f"/api/rooms/{code}/speech/finish",
        headers=headers,
        json={"speech_id": speech_id, "content": "旧设备迟到提交不应在 AI 接替之后重新获得写权限。"},
    )
    assert stale_finish.status_code == 409
    old_control = owner.post(
        f"/api/rooms/{code}/control/pause",
        headers=csrf(owner),
        json={"reason": "旧房主页面迟到操作"},
    )
    assert old_control.status_code == 403
    new_control = successor.post(
        f"/api/rooms/{code}/control/pause",
        headers=csrf(successor),
        json={"reason": "新房主检查断线接替状态"},
    )
    assert new_control.status_code == 200, new_control.text

    with SessionLocal() as db:
        room = load_room(db, code)
        speech = db.get(Speech, speech_id)
        owner_seat = next(item for item in room.seats if item.user_id == owner_id)
        assert room.owner_id == successor_id and room.status == "paused"
        assert owner_seat.occupant_type == "ai_substitute" and owner_seat.control_lease == ""
        assert speech and speech.status == "interrupted" and speech.content == ""
        transfer_events = list(
            db.scalars(
                select(MatchEvent).where(
                    MatchEvent.room_id == room.id,
                    MatchEvent.event_type == "room.owner_transferred",
                )
            ).all()
        )
        assert len(transfer_events) == 1 and transfer_events[0].payload["seat_key"] == "neg_1"


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
