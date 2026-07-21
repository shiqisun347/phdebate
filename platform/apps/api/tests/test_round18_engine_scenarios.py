from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import timedelta
from threading import Barrier

import pytest
from app.core.database import SessionLocal
from app.models.entities import Competition, CompetitionTopic, Match, MatchEvent
from app.services.match_engine import match_engine
from app.services.providers import lighttts
from app.services.realtime import SPECTATOR_LIMIT
from app.services.room_service import load_room, now
from conftest import csrf
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from starlette.websockets import WebSocketDisconnect
from test_platform import create_training_room


def _daily_room(owner: TestClient, topic: str) -> str:
    with SessionLocal() as db:
        competition = db.scalar(select(Competition).where(Competition.slug == "daily-4v4"))
        topic_row = CompetitionTopic(
            competition_id=competition.id,
            title=topic,
            is_active=True,
        )
        db.add(topic_row)
        db.flush()
        topic_id = topic_row.id
        db.commit()
    response = owner.post(
        "/api/rooms",
        headers=csrf(owner),
        json={
            "competition_slug": "daily-4v4",
            "topic_id": topic_id,
            "seat_key": "aff_1",
            "visibility": "public",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["room"]["code"]


def _claim(browser: TestClient, code: str, seat_key: str):
    return browser.post(
        f"/api/rooms/{code}/claim-seat",
        headers=csrf(browser),
        json={"seat_key": seat_key},
    )


def _ready(browser: TestClient, code: str) -> None:
    response = browser.post(f"/api/rooms/{code}/ready", headers=csrf(browser), json={"ready": True})
    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_4v4_human_ai_1v1_human_human_and_1v1_human_ai_run_in_parallel(
    client: TestClient,
    register_user,
    monkeypatch,
) -> None:
    """Exercise the three launch shapes students use without sharing state.

    The daily room deliberately races two students for one seat.  The winner
    joins three other humans and the remaining seats are filled by AI.  The two
    training rooms prove that the same start path supports both person-person
    and person-Agent matches.
    """

    daily_owner = register_user("round18_daily_owner")
    daily_players = [register_user(f"round18_daily_player_{index}") for index in range(4)]
    human_owner = register_user("round18_hh_owner")
    human_opponent = register_user("round18_hh_opponent")
    hybrid_owner = register_user("round18_ha_owner")

    daily_code = _daily_room(daily_owner, "Round18 4v4 多真人与多 Agent 并行验证")
    human_code = create_training_room(human_owner, "Round18 1v1 人人训练")['code']
    hybrid_code = create_training_room(hybrid_owner, "Round18 1v1 人机训练")['code']

    race = Barrier(2)

    def race_for_negative_one(browser: TestClient):
        race.wait()
        return _claim(browser, daily_code, "neg_1")

    with ThreadPoolExecutor(max_workers=4) as pool:
        first = pool.submit(race_for_negative_one, daily_players[0])
        second = pool.submit(race_for_negative_one, daily_players[1])
        third = pool.submit(_claim, daily_players[2], daily_code, "aff_2")
        fourth = pool.submit(_claim, daily_players[3], daily_code, "neg_2")
        race_results = [first.result(), second.result()]
        assert third.result().status_code == 200
        assert fourth.result().status_code == 200
    assert sorted(response.status_code for response in race_results) == [200, 409]
    race_winner = daily_players[0] if race_results[0].status_code == 200 else daily_players[1]

    assert _claim(human_opponent, human_code, "neg_1").status_code == 200
    for browser, code in (
        (daily_owner, daily_code),
        (daily_players[2], daily_code),
        (daily_players[3], daily_code),
        (race_winner, daily_code),
        (human_owner, human_code),
        (human_opponent, human_code),
        (hybrid_owner, hybrid_code),
    ):
        _ready(browser, code)

    start_barrier = Barrier(3)

    def start(browser: TestClient, code: str):
        start_barrier.wait()
        return browser.post(f"/api/rooms/{code}/start", headers=csrf(browser), json={})

    with ThreadPoolExecutor(max_workers=3) as pool:
        responses = list(
            pool.map(
                lambda item: start(*item),
                ((daily_owner, daily_code), (human_owner, human_code), (hybrid_owner, hybrid_code)),
            )
        )
    assert all(response.status_code == 200 for response in responses)
    replay = hybrid_owner.post(f"/api/rooms/{hybrid_code}/start", headers=csrf(hybrid_owner), json={})
    assert replay.status_code == 200 and replay.json()["replayed"] is True

    async def no_audio(*_args, **_kwargs):
        return ""

    monkeypatch.setattr(lighttts, "synthesize", no_audio)
    await asyncio.gather(
        match_engine.process_room(daily_code),
        match_engine.process_room(human_code),
        match_engine.process_room(hybrid_code),
    )

    with SessionLocal() as db:
        daily = load_room(db, daily_code)
        human = load_room(db, human_code)
        hybrid = load_room(db, hybrid_code)
        assert daily.topic == "Round18 4v4 多真人与多 Agent 并行验证"
        assert human.topic == "Round18 1v1 人人训练"
        assert hybrid.topic == "Round18 1v1 人机训练"
        assert (sum(seat.occupant_type == "human" for seat in daily.seats), len(daily.seats)) == (4, 8)
        assert sum(seat.occupant_type == "ai" for seat in daily.seats) == 4
        assert sum(seat.occupant_type == "human" for seat in human.seats) == 2
        assert sum(seat.occupant_type == "ai" for seat in human.seats) == 0
        assert sum(seat.occupant_type == "human" for seat in hybrid.seats) == 1
        assert sum(seat.occupant_type == "ai" for seat in hybrid.seats) == 1
        for room in (daily, human, hybrid):
            assert room.status == "running"
            assert db.scalar(select(func.count(Match.id)).where(Match.room_id == room.id)) == 1
            assert db.scalar(
                select(func.count(MatchEvent.id)).where(
                    MatchEvent.room_id == room.id,
                    MatchEvent.event_type == "room.locked",
                )
            ) == 1

    paused = human_owner.post(
        f"/api/rooms/{human_code}/control/pause",
        headers=csrf(human_owner) | {"X-Idempotency-Key": "round18-pause"},
        json={"reason": "模拟教室网络抖动"},
    )
    resumed = human_owner.post(
        f"/api/rooms/{human_code}/control/resume",
        headers=csrf(human_owner) | {"X-Idempotency-Key": "round18-resume"},
        json={"reason": "网络恢复"},
    )
    forbidden = hybrid_owner.post(
        f"/api/rooms/{daily_code}/control/pause",
        headers=csrf(hybrid_owner),
        json={"reason": "跨房操作"},
    )
    assert paused.status_code == resumed.status_code == 200
    assert forbidden.status_code == 403
    with SessionLocal() as db:
        assert load_room(db, human_code).status == "running"
        assert load_room(db, daily_code).status == "running"
        assert load_room(db, hybrid_code).status == "running"


@pytest.mark.asyncio
async def test_disconnect_grace_ai_substitution_and_real_websocket_restore(register_user) -> None:
    """A student keeps the seat for 60 seconds, then has a usable repair path."""

    owner = register_user("round18_restore_owner")
    returning = register_user("round18_restore_participant")
    code = create_training_room(owner, "Round18 断线、AI 接替与真人恢复")['code']
    assert _claim(returning, code, "neg_1").status_code == 200
    _ready(owner, code)
    _ready(returning, code)
    assert owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={}).status_code == 200

    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.template_snapshot = [
            {"key": "hold", "name": "等待下一阶段", "kind": "announcement", "duration": 3600}
        ]
        room.current_stage_index = 0
        room.status = "running"
        room.stage_started_at = now()
        room.stage_deadline_at = now() + timedelta(seconds=3600)
        seat = next(item for item in room.seats if item.seat_key == "neg_1")
        seat.connected = False
        seat.disconnected_at = now() - timedelta(seconds=59)
        db.commit()

    await match_engine.process_room(code)
    with SessionLocal() as db:
        assert next(item for item in load_room(db, code).seats if item.seat_key == "neg_1").occupant_type == "human"
        room = load_room(db, code, lock=True)
        seat = next(item for item in room.seats if item.seat_key == "neg_1")
        seat.disconnected_at = now() - timedelta(seconds=61)
        db.commit()

    await match_engine.process_room(code)
    requested = returning.post(
        f"/api/rooms/{code}/seat-restore-requests",
        headers=csrf(returning) | {"X-Idempotency-Key": "round18-restore-request"},
        json={},
    )
    assert requested.status_code == 200, requested.text
    request_id = requested.json()["request"]["id"]
    premature = owner.post(
        f"/api/rooms/{code}/seat-restore-requests/{request_id}/approve",
        headers=csrf(owner),
        json={"reason": "尚未重新连接"},
    )
    assert premature.status_code == 409 and "尚未重新连接" in premature.json()["detail"]

    with returning.websocket_connect(f"/ws/rooms/{code}") as socket:
        snapshot = socket.receive_json()["room"]
        restored_seat = next(item for item in snapshot["seats"] if item["seat_key"] == "neg_1")
        assert snapshot["my_seat"] == "neg_1"
        assert restored_seat["occupant_type"] == "ai_substitute"
        assert restored_seat["connected"] is True
        approved = owner.post(
            f"/api/rooms/{code}/seat-restore-requests/{request_id}/approve",
            headers=csrf(owner),
            json={"reason": "已重新连接并完成设备检查"},
        )
        assert approved.status_code == 200, approved.text

    with SessionLocal() as db:
        room = load_room(db, code)
        seat = next(item for item in room.seats if item.seat_key == "neg_1")
        assert seat.occupant_type == "human"
        assert seat.connected is False
        assert db.scalar(
            select(func.count(MatchEvent.id)).where(
                MatchEvent.room_id == room.id,
                MatchEvent.event_type == "seat.ai_substituted",
            )
        ) == 1
        assert db.scalar(
            select(func.count(MatchEvent.id)).where(
                MatchEvent.room_id == room.id,
                MatchEvent.event_type == "seat.human_restored",
            )
        ) == 1


def test_five_spectators_are_globally_shared_and_all_repair_roles_remain_exempt(
    client: TestClient,
    register_user,
) -> None:
    """The hard cap applies to observers, never to participants or repair staff."""

    owner = register_user("round18_spectator_owner")
    participant = register_user("round18_spectator_participant")
    outsider = register_user("round18_spectator_outsider")
    other_owner = register_user("round18_other_room_owner")
    code = create_training_room(owner, "Round18 5 人观战上限与角色豁免")['code']
    other_code = create_training_room(other_owner, "Round18 另一房间观战容量独立")['code']
    assert _claim(participant, code, "neg_1").status_code == 200
    with SessionLocal() as db:
        for room_code in (code, other_code):
            room = load_room(db, room_code, lock=True)
            room.status = "running"
        db.commit()

    admin = TestClient(client.app)
    admin.__enter__()
    try:
        login = admin.post("/api/auth/login", json={"account": "admin_test", "password": "Admin-test-1234"})
        assert login.status_code == 200, login.text
        with ExitStack() as stack:
            spectators = [stack.enter_context(client.websocket_connect(f"/ws/rooms/{code}")) for _ in range(SPECTATOR_LIMIT)]
            assert all(socket.receive_json()["room"]["code"] == code for socket in spectators)

            with owner.websocket_connect(f"/ws/rooms/{code}") as owner_socket:
                assert owner_socket.receive_json()["room"]["my_seat"] == "aff_1"
            with participant.websocket_connect(f"/ws/rooms/{code}") as participant_socket:
                assert participant_socket.receive_json()["room"]["my_seat"] == "neg_1"
            with admin.websocket_connect(f"/ws/rooms/{code}") as admin_socket:
                assert admin_socket.receive_json()["room"]["can_control"] is True
            with pytest.raises(WebSocketDisconnect) as other_room_overflow:
                with client.websocket_connect(f"/ws/rooms/{other_code}") as other_room_socket:
                    other_room_socket.receive_json()
            assert other_room_overflow.value.code == 4429

            with pytest.raises(WebSocketDisconnect) as overflow:
                with outsider.websocket_connect(f"/ws/rooms/{code}") as overflow_socket:
                    overflow_socket.receive_json()
            assert overflow.value.code == 4429

        with outsider.websocket_connect(f"/ws/rooms/{other_code}") as released_socket:
            assert released_socket.receive_json()["room"]["code"] == other_code
    finally:
        admin.__exit__(None, None, None)
