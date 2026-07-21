from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from app.core.database import SessionLocal
from app.main import app
from app.models.entities import LeaderboardEntry, Match, MatchEvent, RatingChange, Speech, UserSession
from app.services.match_archive import build_match_archive
from app.services.match_engine import match_engine
from app.services.room_service import append_event, leaderboard, load_room, now
from conftest import csrf
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from starlette.websockets import WebSocketDisconnect
from test_platform import create_training_room


def _admin(client: TestClient) -> TestClient:
    admin = TestClient(client.app)
    admin.__enter__()
    login = admin.post("/api/auth/login", json={"account": "admin_test", "password": "Admin-test-1234"})
    assert login.status_code == 200, login.text
    return admin


def _daily_room(owner: TestClient, client: TestClient, topic_suffix: str) -> str:
    competition = client.get("/api/competitions/daily-4v4").json()["competition"]
    topic_id = competition["topics"][0]["id"]
    created = owner.post(
        "/api/rooms",
        headers=csrf(owner),
        json={
            "competition_slug": "daily-4v4",
            "topic_id": topic_id,
            "seat_key": "aff_1",
            "visibility": "public",
        },
    )
    assert created.status_code == 200, f"{topic_suffix}: {created.text}"
    return created.json()["room"]["code"]


def test_four_humans_can_claim_and_ready_a_4v4_room_concurrently_once(client, register_user) -> None:
    owner = register_user("round10_4v4_owner")
    participants = [register_user(f"round10_4v4_player_{index}") for index in range(3)]
    code = _daily_room(owner, client, "四真人并发入场")
    claims = [(participants[0], "aff_2"), (participants[1], "neg_1"), (participants[2], "neg_2")]
    barrier = Barrier(len(claims))

    def claim(item: tuple[TestClient, str]):
        target, seat_key = item
        barrier.wait()
        return target.post(
            f"/api/rooms/{code}/claim-seat",
            headers=csrf(target),
            json={"seat_key": seat_key},
        )

    with ThreadPoolExecutor(max_workers=len(claims)) as pool:
        responses = list(pool.map(claim, claims))
    assert [response.status_code for response in responses] == [200, 200, 200]

    humans = [owner, *participants]
    ready_barrier = Barrier(len(humans))

    def mark_ready(target: TestClient):
        ready_barrier.wait()
        return target.post(f"/api/rooms/{code}/ready", headers=csrf(target), json={"ready": True})

    with ThreadPoolExecutor(max_workers=len(humans)) as pool:
        ready_responses = list(pool.map(mark_ready, humans))
    assert [response.status_code for response in ready_responses] == [200, 200, 200, 200]
    # A repeated click is a semantic replay and must not append another event.
    replay = participants[0].post(f"/api/rooms/{code}/ready", headers=csrf(participants[0]), json={"ready": True})
    assert replay.status_code == 200 and replay.json()["replayed"] is True

    second_device = TestClient(app)
    with second_device:
        second_device.cookies.update(owner.cookies)
        start_barrier = Barrier(2)

        def start(target: TestClient):
            start_barrier.wait()
            return target.post(f"/api/rooms/{code}/start", headers=csrf(target), json={})

        with ThreadPoolExecutor(max_workers=2) as pool:
            starts = list(pool.map(start, [owner, second_device]))
    assert [response.status_code for response in starts] == [200, 200]
    assert sum(bool(response.json().get("replayed")) for response in starts) == 1

    with SessionLocal() as db:
        room = load_room(db, code)
        assert room.status == "preparing"
        assert sum(seat.occupant_type == "human" for seat in room.seats) == 4
        assert sum(seat.occupant_type == "ai" for seat in room.seats) == 4
        assert db.scalar(select(func.count(Match.id)).where(Match.room_id == room.id)) == 1
        assert (
            db.scalar(
                select(func.count(MatchEvent.id)).where(
                    MatchEvent.room_id == room.id,
                    MatchEvent.event_type == "room.locked",
                )
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count(MatchEvent.id)).where(
                    MatchEvent.room_id == room.id,
                    MatchEvent.event_type == "seat.ready_changed",
                )
            )
            == 4
        )


def test_anonymous_users_cannot_enumerate_waiting_room_identities_but_logged_in_join_still_works(
    client,
    register_user,
) -> None:
    owner = register_user("round10_lobby_privacy_owner")
    invited = register_user("round10_lobby_privacy_invited")
    private_owner = register_user("round10_lobby_privacy_private_owner")
    owner_name = owner.get("/api/auth/session").json()["user"]["real_name"]
    invited_name = invited.get("/api/auth/session").json()["user"]["real_name"]
    code = create_training_room(owner, "等待大厅真人身份仅登录后可见")["code"]
    private_created = private_owner.post(
        "/api/rooms",
        headers=csrf(private_owner),
        json={
            "competition_slug": "training-1v1",
            "custom_topic": "私密等待大厅身份不可被旁观者读取",
            "seat_key": "aff_1",
            "visibility": "private",
        },
    )
    assert private_created.status_code == 200
    private_code = private_created.json()["room"]["code"]

    assert client.get(f"/api/rooms/{code}").status_code == 401
    assert client.get(f"/api/rooms/{code}/public").status_code == 401
    assert client.post(f"/api/rooms/{code}/rtc-token").status_code == 401
    assert client.get(f"/api/rooms/{private_code}").status_code == 401
    assert invited.get(f"/api/rooms/{private_code}").status_code == 403
    with pytest.raises(WebSocketDisconnect) as hidden_socket:
        with client.websocket_connect(f"/ws/rooms/{code}") as socket:
            socket.receive_json()
    assert hidden_socket.value.code == 4401

    # Possession of the room code plus a valid login preserves the intended
    # public-lobby join flow; no high-entropy invitation token is required for
    # this compatibility boundary.
    searched = invited.get("/api/rooms/search", params={"code": code})
    assert searched.status_code == 200
    assert owner_name in {seat["display_name"] for seat in searched.json()["room"]["seats"]}
    claimed = invited.post(
        f"/api/rooms/{code}/claim-seat",
        headers=csrf(invited),
        json={"seat_key": "neg_1"},
    )
    assert claimed.status_code == 200
    assert invited_name in {seat["display_name"] for seat in claimed.json()["room"]["seats"]}

    for target in (owner, invited):
        assert target.post(f"/api/rooms/{code}/ready", headers=csrf(target), json={"ready": True}).status_code == 200
    assert owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={}).status_code == 200
    spectator = client.get(f"/api/rooms/{code}/public")
    assert spectator.status_code == 200
    published_names = {seat["display_name"] for seat in spectator.json()["room"]["seats"]}
    assert {owner_name, invited_name}.issubset(published_names)


@pytest.mark.asyncio
async def test_disabled_lobby_owner_loses_sessions_and_room_transfers_without_timeout(client, register_user) -> None:
    owner = register_user("round10_disabled_lobby_owner")
    successor = register_user("round10_disabled_lobby_successor")
    owner_identity = owner.get("/api/auth/session").json()["user"]
    successor_identity = successor.get("/api/auth/session").json()["user"]
    code = create_training_room(owner, "停用房主后立即修复大厅")["code"]
    assert successor.post(
        f"/api/rooms/{code}/claim-seat",
        headers=csrf(successor),
        json={"seat_key": "neg_1"},
    ).status_code == 200
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        for seat in room.seats:
            if seat.user_id in {owner_identity["id"], successor_identity["id"]}:
                seat.connected = True
                seat.disconnected_at = None
        db.commit()

    admin = _admin(client)
    try:
        disabled = admin.patch(
            f"/api/admin/users/{owner_identity['id']}",
            headers=csrf(admin),
            json={"is_active": False},
        )
        assert disabled.status_code == 200, disabled.text
    finally:
        admin.__exit__(None, None, None)
    assert owner.get("/api/auth/session").status_code == 401
    with SessionLocal() as db:
        assert db.scalar(select(func.count(UserSession.id)).where(UserSession.user_id == owner_identity["id"])) == 0

    await match_engine.process_room(code)

    with SessionLocal() as db:
        room = load_room(db, code)
        old_owner = next(seat for seat in room.seats if seat.seat_key == "aff_1")
        assert room.status == "lobby"
        assert room.owner_id == successor_identity["id"]
        assert old_owner.occupant_type == "open" and old_owner.user_id is None
        transfer = db.scalar(
            select(MatchEvent).where(
                MatchEvent.room_id == room.id,
                MatchEvent.event_type == "room.owner_transferred",
            )
        )
        expired = db.scalar(
            select(MatchEvent).where(
                MatchEvent.room_id == room.id,
                MatchEvent.event_type == "seat.expired",
            )
        )
        assert transfer and transfer.payload["reason"] == "owner_account_disabled"
        assert expired and expired.payload["reason"] == "account_disabled"
    assert successor.post(f"/api/rooms/{code}/cancel", headers=csrf(successor), json={}).status_code == 200


@pytest.mark.asyncio
async def test_disabled_owner_cancels_lobby_when_only_successor_is_offline(client, register_user) -> None:
    owner = register_user("round10_disabled_offline_owner")
    returning = register_user("round10_disabled_offline_successor")
    owner_identity = owner.get("/api/auth/session").json()["user"]
    returning_identity = returning.get("/api/auth/session").json()["user"]
    code = create_training_room(owner, "房主停用时离线真人仍可成为恢复控制者")["code"]
    assert returning.post(
        f"/api/rooms/{code}/claim-seat",
        headers=csrf(returning),
        json={"seat_key": "neg_1"},
    ).status_code == 200
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        owner_seat = next(seat for seat in room.seats if seat.user_id == owner_identity["id"])
        returning_seat = next(seat for seat in room.seats if seat.user_id == returning_identity["id"])
        owner_seat.connected = True
        owner_seat.disconnected_at = None
        returning_seat.connected = False
        returning_seat.disconnected_at = now()
        db.commit()

    admin = _admin(client)
    try:
        assert admin.patch(
            f"/api/admin/users/{owner_identity['id']}",
            headers=csrf(admin),
            json={"is_active": False},
        ).status_code == 200
    finally:
        admin.__exit__(None, None, None)
    await match_engine.process_room(code)

    with SessionLocal() as db:
        room = load_room(db, code)
        assert room.status == "cancelled"
        owner_seat = next(seat for seat in room.seats if seat.seat_key == "aff_1")
        assert owner_seat.occupant_type == "open" and owner_seat.user_id is None


@pytest.mark.asyncio
async def test_manual_then_disabled_owner_handoff_forms_one_authoritative_transfer_chain(client, register_user) -> None:
    first_owner = register_user("round10_chain_owner_a")
    second_owner = register_user("round10_chain_owner_b")
    final_owner = register_user("round10_chain_owner_c")
    second_identity = second_owner.get("/api/auth/session").json()["user"]
    final_identity = final_owner.get("/api/auth/session").json()["user"]
    code = _daily_room(first_owner, client, "连续房主移交")
    assert second_owner.post(
        f"/api/rooms/{code}/claim-seat",
        headers=csrf(second_owner),
        json={"seat_key": "aff_2"},
    ).status_code == 200
    assert final_owner.post(
        f"/api/rooms/{code}/claim-seat",
        headers=csrf(final_owner),
        json={"seat_key": "neg_1"},
    ).status_code == 200
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        for seat in room.seats:
            seat.connected = seat.user_id in {second_identity["id"], final_identity["id"]}
            seat.disconnected_at = None if seat.connected else seat.disconnected_at
        db.commit()

    handoff = first_owner.post(
        f"/api/rooms/{code}/transfer-owner",
        headers=csrf(first_owner) | {"X-Idempotency-Key": "round10-chain-a-to-b"},
        json={"seat_key": "aff_2"},
    )
    assert handoff.status_code == 200 and handoff.json()["replayed"] is False

    admin = _admin(client)
    try:
        assert admin.patch(
            f"/api/admin/users/{second_identity['id']}",
            headers=csrf(admin),
            json={"is_active": False},
        ).status_code == 200
    finally:
        admin.__exit__(None, None, None)
    await match_engine.process_room(code)

    with SessionLocal() as db:
        room = load_room(db, code)
        transfers = list(
            db.scalars(
                select(MatchEvent)
                .where(
                    MatchEvent.room_id == room.id,
                    MatchEvent.event_type == "room.owner_transferred",
                )
                .order_by(MatchEvent.seq)
            ).all()
        )
        assert room.owner_id == final_identity["id"]
        assert [event.payload["reason"] for event in transfers] == ["manual_handoff", "owner_account_disabled"]
        assert transfers[0].payload["new_owner_id"] == second_identity["id"]
        assert transfers[1].payload["new_owner_id"] == final_identity["id"]
        disabled_seat = next(seat for seat in room.seats if seat.seat_key == "aff_2")
        assert disabled_seat.occupant_type == "open" and disabled_seat.user_id is None


@pytest.mark.asyncio
async def test_disabled_active_speaker_is_interrupted_substituted_and_cannot_restore(client, register_user) -> None:
    owner = register_user("round10_disabled_active_owner")
    participant = register_user("round10_disabled_active_player")
    participant_identity = participant.get("/api/auth/session").json()["user"]
    code = create_training_room(owner, "停用进行中真人后由 AI 安全接替")["code"]
    assert participant.post(
        f"/api/rooms/{code}/claim-seat",
        headers=csrf(participant),
        json={"seat_key": "neg_1"},
    ).status_code == 200
    for target in (owner, participant):
        assert target.post(f"/api/rooms/{code}/ready", headers=csrf(target), json={"ready": True}).status_code == 200
    assert owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={}).status_code == 200

    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.status = "paused"
        room.template_snapshot = [{"key": "neg_human", "name": "反方真人发言", "kind": "speech", "seat": "neg_1", "duration": 60}]
        room.current_stage_index = 0
        seat = next(seat for seat in room.seats if seat.user_id == participant_identity["id"])
        seat.connected = True
        seat.disconnected_at = None
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        speech = Speech(
            match_id=match.id,
            room_id=room.id,
            seat_key=seat.seat_key,
            stage_key="neg_human",
            speaker_type="human",
            status="speaking",
        )
        db.add(speech)
        append_event(db, room, "speech.started", {"speech_id": speech.id, "seat_key": seat.seat_key})
        db.commit()
        speech_id = speech.id

    admin = _admin(client)
    try:
        disabled = admin.patch(
            f"/api/admin/users/{participant_identity['id']}",
            headers=csrf(admin),
            json={"is_active": False},
        )
        assert disabled.status_code == 200
    finally:
        admin.__exit__(None, None, None)
    await match_engine.process_room(code)

    with SessionLocal() as db:
        room = load_room(db, code)
        seat = next(seat for seat in room.seats if seat.seat_key == "neg_1")
        speech = db.get(Speech, speech_id)
        assert room.status == "paused"
        assert seat.occupant_type == "ai_substitute" and seat.connected is False
        assert speech.status == "interrupted"
        event = db.scalar(
            select(MatchEvent)
            .where(
                MatchEvent.room_id == room.id,
                MatchEvent.event_type == "speech.interrupted",
                MatchEvent.payload["speech_id"].as_string() == speech_id,
            )
            .order_by(MatchEvent.created_at.desc())
        )
        assert event and event.payload["reason"] == "account_disabled"
    assert participant.post(
        f"/api/rooms/{code}/seat-restore-requests",
        headers=csrf(participant),
        json={},
    ).status_code == 401


def test_disabled_accounts_are_hidden_from_public_rankings_without_erasing_history(client, register_user) -> None:
    participant = register_user("round10_disabled_ranking")
    identity = participant.get("/api/auth/session").json()["user"]
    with SessionLocal() as db:
        room = load_room(db, create_training_room(participant, "停用用户排名可逆隐藏")["code"])
        db.add(
            LeaderboardEntry(
                competition_id=room.competition_id,
                season_id=room.season_id,
                user_id=identity["id"],
                points=3,
                wins=1,
                matches=1,
                average_score=88,
                last_match_at=now(),
            )
        )
        db.commit()
        assert any(item["user_id"] == identity["id"] for item in leaderboard(db, competition_id=room.competition_id))

    admin = _admin(client)
    try:
        assert admin.patch(
            f"/api/admin/users/{identity['id']}",
            headers=csrf(admin),
            json={"is_active": False},
        ).status_code == 200
        with SessionLocal() as db:
            stored = db.scalar(select(LeaderboardEntry).where(LeaderboardEntry.user_id == identity["id"]))
            assert stored and stored.points == 3 and stored.matches == 1
            assert all(item["user_id"] != identity["id"] for item in leaderboard(db, competition_id=stored.competition_id))
            assert any(
                item["user_id"] == identity["id"]
                for item in leaderboard(db, competition_id=stored.competition_id, include_test_accounts=True)
            )
        assert admin.patch(
            f"/api/admin/users/{identity['id']}",
            headers=csrf(admin),
            json={"is_active": True},
        ).status_code == 200
    finally:
        admin.__exit__(None, None, None)
    with SessionLocal() as db:
        stored = db.scalar(select(LeaderboardEntry).where(LeaderboardEntry.user_id == identity["id"]))
        assert any(item["user_id"] == identity["id"] for item in leaderboard(db, competition_id=stored.competition_id))


def test_terminated_match_rejects_mutations_and_archive_remains_stable(client, register_user) -> None:
    owner = register_user("round10_terminal_owner")
    participant = register_user("round10_terminal_player")
    code = create_training_room(owner, "终局写保护与归档一致性")["code"]
    assert participant.post(
        f"/api/rooms/{code}/claim-seat",
        headers=csrf(participant),
        json={"seat_key": "neg_1"},
    ).status_code == 200
    for target in (owner, participant):
        assert target.post(f"/api/rooms/{code}/ready", headers=csrf(target), json={"ready": True}).status_code == 200
    assert owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={}).status_code == 200
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        owner_seat = next(seat for seat in room.seats if seat.seat_key == "aff_1")
        owner_seat.control_lease = "round10-terminal-lease"
        timed_out = Speech(
            match_id=match.id,
            room_id=room.id,
            seat_key=owner_seat.seat_key,
            stage_key="expired-human-turn",
            speaker_type="human",
            status="timed_out",
        )
        db.add(timed_out)
        db.commit()
        timed_out_id = timed_out.id
    terminated = owner.post(
        f"/api/rooms/{code}/control/terminate",
        headers=csrf(owner) | {"X-Idempotency-Key": "round10-terminate-once"},
        json={"reason": "验证终局写保护"},
    )
    assert terminated.status_code == 200

    with SessionLocal() as db:
        room = load_room(db, code)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        assert room.status == "terminated" and match.status == "terminated"
        before_seq = room.seq
        match_id = match.id
    first_archive = build_match_archive(match_id)

    rejected = [
        participant.post(f"/api/rooms/{code}/abandon-seat", headers=csrf(participant), json={}),
        participant.post(f"/api/rooms/{code}/ready", headers=csrf(participant), json={"ready": False}),
        participant.post(
            f"/api/rooms/{code}/claim-seat",
            headers=csrf(participant),
            json={"seat_key": "neg_1"},
        ),
        owner.post(
            f"/api/rooms/{code}/transfer-owner",
            headers=csrf(owner),
            json={"seat_key": "neg_1"},
        ),
        owner.post(f"/api/rooms/{code}/control/pause", headers=csrf(owner), json={"reason": "过期页面误操作"}),
        owner.post(f"/api/rooms/{code}/speech/start", headers=csrf(owner), json={}),
        owner.post(
            f"/api/rooms/{code}/speech/finish",
            headers=csrf(owner) | {"X-Control-Lease": "round10-terminal-lease"},
            json={"speech_id": timed_out_id, "content": "终止后迟到的发言文字不应写入历史。"},
        ),
    ]
    assert all(response.status_code in {403, 409} for response in rejected)
    second_archive = build_match_archive(match_id)
    assert second_archive.reused is True
    assert second_archive.sha256 == first_archive.sha256
    assert second_archive.source_sha256 == first_archive.source_sha256

    with SessionLocal() as db:
        room = load_room(db, code)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        timed_out = db.get(Speech, timed_out_id)
        assert room.status == "terminated" and match.status == "terminated"
        assert room.seq == before_seq
        assert timed_out.status == "timed_out" and timed_out.content == ""
        assert not db.scalar(select(RatingChange.id).where(RatingChange.match_id == match.id))


def test_cancelled_lobby_is_an_immutable_boundary_for_stale_clients(client, register_user) -> None:
    owner = register_user("round10_cancelled_owner")
    outsider = register_user("round10_cancelled_outsider")
    code = create_training_room(owner, "关闭大厅后的陈旧页面不能复活房间")["code"]
    cancelled = owner.post(f"/api/rooms/{code}/cancel", headers=csrf(owner), json={})
    assert cancelled.status_code == 200
    with SessionLocal() as db:
        room = load_room(db, code)
        assert room.status == "cancelled"
        before_seq = room.seq

    stale_writes = [
        owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True}),
        owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={}),
        owner.post(
            f"/api/rooms/{code}/transfer-owner",
            headers=csrf(owner),
            json={"seat_key": "aff_1"},
        ),
        outsider.post(
            f"/api/rooms/{code}/claim-seat",
            headers=csrf(outsider),
            json={"seat_key": "neg_1"},
        ),
    ]
    assert all(response.status_code == 409 for response in stale_writes)
    replay = owner.post(f"/api/rooms/{code}/cancel", headers=csrf(owner), json={})
    assert replay.status_code == 200 and replay.json()["replayed"] is True
    with SessionLocal() as db:
        room = load_room(db, code)
        assert room.status == "cancelled" and room.seq == before_seq
        assert db.scalar(select(func.count(Match.id)).where(Match.room_id == room.id)) == 0
