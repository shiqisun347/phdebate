from __future__ import annotations

from datetime import timedelta

from app.core.database import SessionLocal
from app.models.entities import MatchEvent
from app.services.match_engine import match_engine
from app.services.room_service import load_room, now
from conftest import csrf
from sqlalchemy import func, select
from test_platform import create_training_room


def _room_with_teammate(owner, teammate, topic: str) -> str:
    code = create_training_room(owner, topic)["code"]
    claimed = teammate.post(
        f"/api/rooms/{code}/claim-seat",
        headers=csrf(teammate),
        json={"seat_key": "neg_1"},
    )
    assert claimed.status_code == 200, claimed.text
    return code


async def test_lobby_owner_presence_expiry_transfers_control_to_online_successor(
    register_user,
) -> None:
    owner = register_user("round9_lobby_owner")
    teammate = register_user("round9_lobby_successor")
    other_owner = register_user("round9_lobby_other")
    code = _room_with_teammate(owner, teammate, "Round9 大厅房主断线接管")
    other_code = create_training_room(other_owner, "Round9 大厅隔离对照")["code"]
    teammate_id = teammate.get("/api/auth/session").json()["user"]["id"]

    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        owner_seat = next(seat for seat in room.seats if seat.user_id == room.owner_id)
        successor = next(seat for seat in room.seats if seat.user_id == teammate_id)
        owner_seat.connected = False
        owner_seat.disconnected_at = now() - timedelta(seconds=121)
        successor.connected = True
        successor.disconnected_at = None
        db.commit()

    await match_engine.process_room(code)

    with SessionLocal() as db:
        room = load_room(db, code)
        assert room.status == "lobby"
        assert room.owner_id == teammate_id
        owner_seat = next(seat for seat in room.seats if seat.seat_key == "aff_1")
        assert owner_seat.occupant_type == "open"
        assert owner_seat.user_id is None
        event = db.scalar(
            select(MatchEvent).where(
                MatchEvent.room_id == room.id,
                MatchEvent.event_type == "room.owner_transferred",
            )
        )
        assert event is not None
        other = load_room(db, other_code)
        assert other.status == "lobby" and other.owner_id == other_owner.get("/api/auth/session").json()["user"]["id"]

    # The online successor now owns repair controls and can close the lobby.
    assert teammate.post(f"/api/rooms/{code}/cancel", headers=csrf(teammate), json={}).status_code == 200


async def test_lobby_owner_presence_expiry_cancels_when_no_online_successor(register_user) -> None:
    owner = register_user("round9_abandoned_lobby_owner")
    code = create_training_room(owner, "Round9 无人大厅自动回收")["code"]
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        owner_seat = next(seat for seat in room.seats if seat.user_id == room.owner_id)
        owner_seat.connected = False
        owner_seat.disconnected_at = now() - timedelta(seconds=121)
        db.commit()

    await match_engine.process_room(code)

    with SessionLocal() as db:
        room = load_room(db, code)
        owner_seat = next(seat for seat in room.seats if seat.seat_key == "aff_1")
        assert room.status == "cancelled" and room.completed_at is not None
        assert owner_seat.occupant_type == "open" and owner_seat.user_id is None
        assert db.scalar(
            select(MatchEvent.id).where(
                MatchEvent.room_id == room.id,
                MatchEvent.event_type == "room.cancelled",
            )
        )


async def test_running_owner_disconnect_pauses_without_transferring_control_or_seat(
    register_user,
) -> None:
    owner = register_user("round9_running_owner")
    teammate = register_user("round9_running_successor")
    other_owner = register_user("round9_running_other")
    code = _room_with_teammate(owner, teammate, "Round9 运行中房主断线接管")
    other_code = create_training_room(other_owner, "Round9 运行中隔离对照")["code"]
    assert owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True}).status_code == 200
    assert teammate.post(f"/api/rooms/{code}/ready", headers=csrf(teammate), json={"ready": True}).status_code == 200
    assert owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={}).status_code == 200
    teammate_id = teammate.get("/api/auth/session").json()["user"]["id"]
    owner_id = owner.get("/api/auth/session").json()["user"]["id"]

    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.status = "running"
        room.template_snapshot = [
            {"key": "teammate_case", "name": "反方真人发言", "kind": "speech", "seat": "neg_1", "duration": 60}
        ]
        room.current_stage_index = 0
        room.stage_started_at = now()
        room.stage_deadline_at = now() + timedelta(seconds=60)
        owner_seat = next(seat for seat in room.seats if seat.user_id == room.owner_id)
        successor = next(seat for seat in room.seats if seat.user_id == teammate_id)
        owner_seat.connected = False
        owner_seat.disconnected_at = now() - timedelta(seconds=61)
        successor.connected = True
        successor.disconnected_at = None
        db.commit()

    await match_engine.process_room(code)

    with SessionLocal() as db:
        room = load_room(db, code)
        old_owner_seat = next(seat for seat in room.seats if seat.seat_key == "aff_1")
        assert room.status == "paused" and room.owner_id == owner_id
        assert old_owner_seat.occupant_type == "human"
        assert (
            db.scalar(
                select(func.count(MatchEvent.id)).where(
                    MatchEvent.room_id == room.id,
                    MatchEvent.event_type == "room.owner_transferred",
                )
            )
            == 0
        )
        assert db.scalar(
            select(MatchEvent.id).where(
                MatchEvent.room_id == room.id,
                MatchEvent.event_type == "participant.disconnect_timeout",
            )
        )
        other = load_room(db, other_code)
        assert other.status == "lobby"
        assert not db.scalar(
            select(MatchEvent.id).where(
                MatchEvent.room_id == other.id,
                MatchEvent.event_type == "room.owner_transferred",
            )
        )

    assert teammate.post(f"/api/rooms/{code}/control/resume", headers=csrf(teammate), json={}).status_code == 403
    with SessionLocal.begin() as db:
        room = load_room(db, code, lock=True)
        owner_seat = next(seat for seat in room.seats if seat.user_id == owner_id)
        owner_seat.connected = True
        owner_seat.disconnected_at = None
    resumed = owner.post(f"/api/rooms/{code}/control/resume", headers=csrf(owner), json={"reason": "房主已重连"})
    assert resumed.status_code == 200


async def test_paused_owner_timeout_preserves_prior_failure_and_requires_owner_reconnect(register_user) -> None:
    owner = register_user("round9_paused_owner")
    teammate = register_user("round9_paused_successor")
    other_owner = register_user("round9_paused_other")
    code = _room_with_teammate(owner, teammate, "Round9 异常暂停房主断线接管")
    other_code = create_training_room(other_owner, "Round9 暂停房间隔离对照")["code"]
    assert owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True}).status_code == 200
    assert teammate.post(f"/api/rooms/{code}/ready", headers=csrf(teammate), json={"ready": True}).status_code == 200
    assert owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={}).status_code == 200
    teammate_id = teammate.get("/api/auth/session").json()["user"]["id"]
    owner_id = owner.get("/api/auth/session").json()["user"]["id"]

    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.status = "paused"
        room.failure_reason = "Agent 服务暂时不可用"
        room.paused_remaining_seconds = 47
        owner_seat = next(seat for seat in room.seats if seat.user_id == room.owner_id)
        successor = next(seat for seat in room.seats if seat.user_id == teammate_id)
        owner_seat.connected = False
        owner_seat.disconnected_at = now() - timedelta(seconds=61)
        successor.connected = True
        successor.disconnected_at = None
        db.commit()

    await match_engine.process_room(code)

    with SessionLocal() as db:
        room = load_room(db, code)
        old_owner_seat = next(seat for seat in room.seats if seat.seat_key == "aff_1")
        assert room.status == "paused"
        assert room.failure_reason == "Agent 服务暂时不可用"
        assert room.paused_remaining_seconds == 47
        assert room.owner_id == owner_id
        assert old_owner_seat.occupant_type == "human"
        assert not db.scalar(
            select(MatchEvent.id).where(
                MatchEvent.room_id == room.id,
                MatchEvent.event_type == "room.owner_transferred",
            )
        )
        assert db.scalar(
            select(MatchEvent.id).where(
                MatchEvent.room_id == room.id,
                MatchEvent.event_type == "participant.disconnect_timeout",
            )
        )
        other = load_room(db, other_code)
        assert other.status == "lobby"

    assert teammate.post(
        f"/api/rooms/{code}/control/retry",
        headers=csrf(teammate),
        json={"reason": "非房主不得重试"},
    ).status_code == 403
    with SessionLocal.begin() as db:
        room = load_room(db, code, lock=True)
        owner_seat = next(seat for seat in room.seats if seat.user_id == owner_id)
        owner_seat.connected = True
        owner_seat.disconnected_at = None
    retried = owner.post(
        f"/api/rooms/{code}/control/retry",
        headers=csrf(owner),
        json={"reason": "房主重连后重试原服务异常"},
    )
    assert retried.status_code == 200


def test_manual_owner_handoff_is_authorized_idempotent_and_target_validated(register_user) -> None:
    owner = register_user("round9_manual_owner")
    teammate = register_user("round9_manual_successor")
    outsider = register_user("round9_manual_outsider")
    code = _room_with_teammate(owner, teammate, "Round9 主动移交控制权")
    headers = csrf(owner) | {"X-Idempotency-Key": "round9-owner-handoff-once"}

    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        successor = next(seat for seat in room.seats if seat.seat_key == "neg_1")
        successor.connected = False
        successor.disconnected_at = now()
        db.commit()

    offline = owner.post(
        f"/api/rooms/{code}/transfer-owner",
        headers=csrf(owner),
        json={"seat_key": "neg_1"},
    )
    assert offline.status_code == 409
    assert "当前不在线" in offline.json()["detail"]

    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        successor = next(seat for seat in room.seats if seat.seat_key == "neg_1")
        successor.connected = True
        successor.disconnected_at = None
        db.commit()

    rejected = outsider.post(
        f"/api/rooms/{code}/transfer-owner",
        headers=csrf(outsider),
        json={"seat_key": "neg_1"},
    )
    invalid = owner.post(
        f"/api/rooms/{code}/transfer-owner",
        headers=csrf(owner),
        json={"seat_key": "aff_2"},
    )
    transferred = owner.post(
        f"/api/rooms/{code}/transfer-owner",
        headers=headers,
        json={"seat_key": "neg_1"},
    )
    replayed = owner.post(
        f"/api/rooms/{code}/transfer-owner",
        headers=headers,
        json={"seat_key": "neg_1"},
    )

    assert rejected.status_code == 403
    assert invalid.status_code == 404
    assert transferred.status_code == 200 and transferred.json()["replayed"] is False
    assert replayed.status_code == 200 and replayed.json()["replayed"] is True
    room = teammate.get(f"/api/rooms/{code}").json()["room"]
    assert room["can_control"] is True and room["owner"]["id"] == teammate.get("/api/auth/session").json()["user"]["id"]


async def test_disconnect_timeout_keeps_human_seat_and_requires_reconnection_before_resume(register_user) -> None:
    owner = register_user("round9_disconnect_owner")
    teammate = register_user("round9_disconnect_participant")
    code = _room_with_teammate(owner, teammate, "Round9 断线暂停后必须真实重连")
    assert owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True}).status_code == 200
    assert teammate.post(f"/api/rooms/{code}/ready", headers=csrf(teammate), json={"ready": True}).status_code == 200
    assert owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={}).status_code == 200
    teammate_id = teammate.get("/api/auth/session").json()["user"]["id"]

    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        seat = next(seat for seat in room.seats if seat.user_id == teammate_id)
        room.status = "running"
        room.current_stage_index = 0
        room.template_snapshot = [{"key": "hold", "name": "等待真人", "kind": "announcement", "duration": 120}]
        room.stage_started_at = now()
        room.stage_deadline_at = now() + timedelta(seconds=120)
        seat.connected = False
        seat.disconnected_at = now() - timedelta(seconds=70)
        db.commit()

    await match_engine.process_room(code)
    removed_route = teammate.post(
        f"/api/rooms/{code}/seat-restore-requests",
        headers=csrf(teammate),
        json={},
    )
    assert removed_route.status_code == 404
    premature = owner.post(
        f"/api/rooms/{code}/control/resume",
        headers=csrf(owner),
        json={"reason": "尚未真正重连时不得继续"},
    )
    assert premature.status_code == 409
    assert "尚未重新连接" in premature.json()["detail"]

    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        for seat in room.seats:
            if seat.occupant_type == "human":
                seat.connected = True
                seat.disconnected_at = None
        db.commit()

    resumed = owner.post(
        f"/api/rooms/{code}/control/resume",
        headers=csrf(owner),
        json={"reason": "全部真人已重新连接"},
    )
    assert resumed.status_code == 200
    restored = next(seat for seat in resumed.json()["room"]["seats"] if seat["seat_key"] == "neg_1")
    assert restored["occupant_type"] == "human" and restored["connected"] is True


def test_owner_cannot_replace_started_human_seat_with_ai(register_user) -> None:
    owner = register_user("round9_abandon_owner")
    teammate = register_user("round9_abandon_successor")
    code = _room_with_teammate(owner, teammate, "Round9 房主主动退出后接管")
    assert owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True}).status_code == 200
    assert teammate.post(f"/api/rooms/{code}/ready", headers=csrf(teammate), json={"ready": True}).status_code == 200
    assert owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={}).status_code == 200
    teammate_id = teammate.get("/api/auth/session").json()["user"]["id"]

    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        successor = next(seat for seat in room.seats if seat.user_id == teammate_id)
        successor.connected = True
        successor.disconnected_at = None
        db.commit()

    abandoned = owner.post(
        f"/api/rooms/{code}/abandon-seat",
        headers=csrf(owner) | {"X-Idempotency-Key": "round9-owner-abandon"},
        json={},
    )
    assert abandoned.status_code == 410
    assert "取消 AI 自动接管" in abandoned.json()["detail"]
    with SessionLocal() as db:
        room = load_room(db, code)
        owner_seat = next(seat for seat in room.seats if seat.seat_key == "aff_1")
        assert owner_seat.occupant_type == "human"
        assert room.owner_id != teammate_id


def test_solo_owner_cannot_choose_ai_substitute(register_user) -> None:
    owner = register_user("round9_solo_owner_abandon")
    code = create_training_room(owner, "Round9 单人房主主动 AI 接替")["code"]
    owner_id = owner.get("/api/auth/session").json()["user"]["id"]
    assert owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True}).status_code == 200
    assert owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={}).status_code == 200

    abandoned = owner.post(
        f"/api/rooms/{code}/abandon-seat",
        headers=csrf(owner),
        json={},
    )
    assert abandoned.status_code == 410
    with SessionLocal() as db:
        room = load_room(db, code)
        seat = next(item for item in room.seats if item.user_id == owner_id)
        assert seat.occupant_type == "human"
        assert room.owner_id == owner_id
    assert owner.post(
        f"/api/rooms/{code}/control/terminate",
        headers=csrf(owner),
        json={"reason": "单人房主仍可纠错"},
    ).status_code == 200
