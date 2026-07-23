from __future__ import annotations

from datetime import timedelta

import pytest
from app.core.database import SessionLocal
from app.models.entities import AdminAuditLog, Match, MatchEvent, Room
from app.services.match_engine import match_engine
from app.services.room_capacity import ACTIVE_PARTICIPANT_STATUSES
from app.services.room_service import append_event, load_room, now, speaking_permission
from conftest import csrf
from fastapi.testclient import TestClient
from sqlalchemy import func, select


def _create_room(client: TestClient, topic: str) -> dict:
    response = client.post(
        "/api/rooms",
        headers=csrf(client),
        json={
            "competition_slug": "training-1v1",
            "custom_topic": topic,
            "seat_key": "aff_1",
            "visibility": "public",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["room"]


def _admin(client: TestClient) -> TestClient:
    admin = TestClient(client.app)
    admin.__enter__()
    response = admin.post("/api/auth/login", json={"account": "admin_test", "password": "Admin-test-1234"})
    assert response.status_code == 200, response.text
    return admin


def test_stale_pause_is_projected_without_automatic_deletion_and_owner_can_release_capacity(
    client: TestClient, register_user
) -> None:
    owner = register_user("round44_stale_owner")
    created = _create_room(owner, "长期异常暂停是否应该由房主明确处置？")
    code = created["code"]
    with SessionLocal.begin() as db:
        room = load_room(db, code, lock=True)
        room.status = "paused"
        room.failure_reason = "实时语音服务历史异常"
        room.updated_at = now() - timedelta(hours=3)
        for seat in room.seats:
            seat.connected = False
        pause = append_event(db, room, "provider.failed", {"provider": "moss", "message": "test"})
        pause.created_at = now() - timedelta(hours=3)

    owner_projection = owner.get(f"/api/rooms/{code}")
    assert owner_projection.status_code == 200
    pause_health = owner_projection.json()["room"]["pause_health"]
    assert pause_health["is_stale"] is True
    assert pause_health["capacity_consuming"] is True
    assert pause_health["recommended_action"] == "retry"
    assert pause_health["can_terminate_to_release_capacity"] is True

    anonymous = TestClient(client.app)
    with anonymous:
        public_projection = anonymous.get(f"/api/rooms/{code}")
        assert public_projection.status_code == 200
        public_pause_health = public_projection.json()["room"]["pause_health"]
        assert public_pause_health["reason_code"] == "service_or_manual_pause"
        assert public_pause_health["recommended_action"] == "retry"

    admin = _admin(client)
    try:
        inventory = admin.get(f"/api/admin/rooms?q={code}")
        assert inventory.status_code == 200
        item = inventory.json()["items"][0]
        assert item["is_stale_paused"] is True
        assert item["capacity_consuming"] is True
        assert item["attention_reason"] == "stale_paused"
        assert item["available_actions"] == ["retry", "terminate"]
        capacity_before = inventory.json()["capacity"]
        assert capacity_before["limit"] == 5
        assert capacity_before["paused_room_count"] >= 1
        assert capacity_before["stale_paused_room_count"] >= 1
    finally:
        admin.__exit__(None, None, None)

    terminated = owner.post(
        f"/api/rooms/{code}/control/terminate",
        headers=csrf(owner) | {"X-Idempotency-Key": "round44-release-stale-capacity"},
        json={"reason": "旧异常场次不再继续，明确结束并释放容量"},
    )
    assert terminated.status_code == 200, terminated.text
    assert terminated.json()["room"]["status"] == "terminated"

    with SessionLocal() as db:
        open_count = int(
            db.scalar(select(func.count(Room.id)).where(Room.status.in_(ACTIVE_PARTICIPANT_STATUSES))) or 0
        )
        room = load_room(db, code)
        assert room.status == "terminated"
        assert room.completed_at is not None
    admin = _admin(client)
    try:
        capacity_after = admin.get("/api/admin/rooms?page_size=1").json()["capacity"]
        assert capacity_after["open_room_count"] == open_count
        assert capacity_after["open_room_count"] == capacity_before["open_room_count"] - 1
    finally:
        admin.__exit__(None, None, None)


def test_admin_consistency_scan_is_read_only_and_compensation_is_explicit_audited_and_idempotent(
    client: TestClient, register_user
) -> None:
    owner = register_user("round44_consistency_owner")
    created = _create_room(owner, "终止房间与比赛状态应该如何保持一致？")
    code = created["code"]
    with SessionLocal.begin() as db:
        room = load_room(db, code, lock=True)
        room.status = "terminated"
        room.completed_at = now()
        match = Match(
            room_id=room.id,
            competition_id=room.competition_id,
            season_id=room.season_id,
            status="running",
            service_snapshot={"source": "round44-test"},
        )
        db.add(match)
        db.flush()
        match_id = match.id

    forbidden = owner.get(f"/api/admin/consistency/room-matches?room_code={code}")
    assert forbidden.status_code == 403

    admin = _admin(client)
    try:
        scan = admin.get(f"/api/admin/consistency/room-matches?room_code={code}")
        assert scan.status_code == 200
        assert scan.json()["summary"] == {
            "issue_count": 1,
            "repairable_count": 1,
            "non_repairable_count": 0,
        }
        issue = scan.json()["items"][0]
        assert issue["issue_type"] == "terminal_room_match_mismatch"
        assert issue["room_status"] == "terminated"
        assert issue["match_status"] == "running"
        assert issue["expected_match_status"] == "terminated"
        assert issue["repairable"] is True
        with SessionLocal() as db:
            assert db.get(Match, match_id).status == "running"

        body = {
            "expected_room_status": "terminated",
            "expected_match_status": "terminated",
            "reason": "补偿历史异常竞争导致的终态缺失",
        }
        repaired = admin.post(
            f"/api/admin/consistency/room-matches/{code}/repair",
            headers=csrf(admin) | {"X-Idempotency-Key": "round44-repair-once"},
            json=body,
        )
        assert repaired.status_code == 200, repaired.text
        assert repaired.json()["replayed"] is False
        assert repaired.json()["room"]["status"] == "terminated"

        replay = admin.post(
            f"/api/admin/consistency/room-matches/{code}/repair",
            headers=csrf(admin) | {"X-Idempotency-Key": "round44-repair-once"},
            json=body,
        )
        assert replay.status_code == 200
        assert replay.json()["replayed"] is True

        reused_with_different_reason = admin.post(
            f"/api/admin/consistency/room-matches/{code}/repair",
            headers=csrf(admin) | {"X-Idempotency-Key": "round44-repair-once"},
            json=body | {"reason": "另一个补偿原因"},
        )
        assert reused_with_different_reason.status_code == 409

        rescanned = admin.get(f"/api/admin/consistency/room-matches?room_code={code}")
        assert rescanned.status_code == 200
        assert rescanned.json()["summary"]["issue_count"] == 0
    finally:
        admin.__exit__(None, None, None)

    with SessionLocal() as db:
        match = db.get(Match, match_id)
        assert match.status == "terminated"
        events = list(
            db.scalars(
                select(MatchEvent).where(
                    MatchEvent.room_id == match.room_id,
                    MatchEvent.event_type == "consistency.match_status_compensated",
                )
            ).all()
        )
        audits = list(
            db.scalars(
                select(AdminAuditLog).where(
                    AdminAuditLog.target_id == match.room_id,
                    AdminAuditLog.action == "consistency.match_status_compensated",
                )
            ).all()
        )
        assert len(events) == 1
        assert events[0].payload["old_match_status"] == "running"
        assert events[0].payload["new_match_status"] == "terminated"
        assert len(audits) == 1


def test_consistency_scan_reports_active_room_with_terminal_match_but_refuses_automatic_room_mutation(
    client: TestClient, register_user
) -> None:
    owner = register_user("round44_inverse_consistency")
    created = _create_room(owner, "反向状态异常不能被静默改写房间")
    code = created["code"]
    with SessionLocal.begin() as db:
        room = load_room(db, code, lock=True)
        room.status = "running"
        db.add(
            Match(
                room_id=room.id,
                competition_id=room.competition_id,
                season_id=room.season_id,
                status="terminated",
                service_snapshot={"source": "round44-test"},
            )
        )

    admin = _admin(client)
    try:
        scan = admin.get(f"/api/admin/consistency/room-matches?room_code={code}")
        assert scan.status_code == 200
        issue = scan.json()["items"][0]
        assert issue["issue_type"] == "terminal_match_active_room"
        assert issue["repairable"] is False
        rejected = admin.post(
            f"/api/admin/consistency/room-matches/{code}/repair",
            headers=csrf(admin) | {"X-Idempotency-Key": "round44-no-unsafe-repair"},
            json={
                "expected_room_status": "terminated",
                "expected_match_status": "terminated",
                "reason": "不允许自动把仍运行的房间改成终态",
            },
        )
        assert rejected.status_code == 409
    finally:
        admin.__exit__(None, None, None)

    with SessionLocal() as db:
        assert load_room(db, code).status == "running"


@pytest.mark.asyncio
async def test_match_start_resets_lobby_disconnect_clock_and_preserves_full_sixty_second_grace(
    client: TestClient, register_user
) -> None:
    owner = register_user("round44_start_grace")
    created = _create_room(owner, "未建立房间 WebSocket 时开赛是否保留断线宽限？")
    code = created["code"]
    ready = owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})
    assert ready.status_code == 200
    with SessionLocal.begin() as db:
        room = load_room(db, code, lock=True)
        owner_seat = next(item for item in room.seats if item.seat_key == "aff_1")
        owner_seat.connected = False
        owner_seat.disconnected_at = now() - timedelta(minutes=10)

    started = owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    assert started.status_code == 200, started.text
    with SessionLocal.begin() as db:
        room = load_room(db, code, lock=True)
        owner_seat = next(item for item in room.seats if item.seat_key == "aff_1")
        assert room.status == "preparing"
        assert owner_seat.occupant_type == "human"
        assert owner_seat.connected is False
        assert owner_seat.disconnected_at is not None
        assert abs((owner_seat.disconnected_at - room.started_at).total_seconds()) < 0.01
        changed = await match_engine._expire_presence(db, room)
        assert changed is False
        assert owner_seat.occupant_type == "human"

    with SessionLocal.begin() as db:
        room = load_room(db, code, lock=True)
        owner_seat = next(item for item in room.seats if item.seat_key == "aff_1")
        owner_seat.disconnected_at = now() - timedelta(seconds=61)
        changed = await match_engine._expire_presence(db, room)
        assert changed is True
        assert owner_seat.occupant_type == "human"
        assert owner_seat.user_id is not None
        assert room.status == "paused"
        assert "全部真人重新连接" in room.failure_reason


def test_fixed_turn_speak_reason_uses_compact_chinese_seat_label(client: TestClient, register_user) -> None:
    owner = register_user("round44_speak_reason")
    created = _create_room(owner, "未轮到席位时应明确告诉选手等待谁？")
    code = created["code"]
    with SessionLocal.begin() as db:
        room = load_room(db, code, lock=True)
        room.status = "running"
        room.current_stage_index = 0
        room.template_snapshot = [
            {"key": "neg_case", "name": "反方二辩立论", "kind": "speech", "seat": "neg_2", "duration": 60}
        ]
        allowed, reason = speaking_permission(room, room.owner)
        assert allowed is False
        assert reason == "当前轮到反方二辩"
