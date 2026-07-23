from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from app.core.config import settings
from app.core.database import SessionLocal
from app.models.entities import JudgeScorecard, Match, MatchEvent, Speech, User
from app.services.match_engine import match_engine
from app.services.provider_config import build_service_snapshot
from app.services.providers import judge_provider
from app.services.room_service import load_room, now
from conftest import csrf
from sqlalchemy import func, select
from test_platform import create_training_room


def _ready(browser, code: str) -> None:
    response = browser.post(f"/api/rooms/{code}/ready", headers=csrf(browser), json={"ready": True})
    assert response.status_code == 200, response.text


def _start_one_human(browser, topic: str) -> str:
    code = create_training_room(browser, topic)["code"]
    _ready(browser, code)
    response = browser.post(f"/api/rooms/{code}/start", headers=csrf(browser), json={})
    assert response.status_code == 200, response.text
    return code


def _start_two_humans(owner, participant, topic: str) -> str:
    code = create_training_room(owner, topic)["code"]
    claimed = participant.post(
        f"/api/rooms/{code}/claim-seat",
        headers=csrf(participant),
        json={"seat_key": "neg_1"},
    )
    assert claimed.status_code == 200, claimed.text
    _ready(owner, code)
    _ready(participant, code)
    response = owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    assert response.status_code == 200, response.text
    return code


def test_public_qa_lobby_allows_only_test_accounts_to_join(register_user) -> None:
    """Synthetic multi-user verification must not be blocked by QA privacy.

    The previous policy hid test rooms from everyone except existing
    participants.  That made it impossible for a second synthetic account to
    become a participant in the first place.  The exception remains restricted
    to authenticated test accounts in a public lobby.
    """

    owner = register_user("round16_qa_owner")
    participant = register_user("round16_qa_participant")
    ordinary = register_user("round16_ordinary_outsider")
    user_ids = [
        owner.get("/api/auth/session").json()["user"]["id"],
        participant.get("/api/auth/session").json()["user"]["id"],
    ]
    with SessionLocal() as db:
        for user_id in user_ids:
            db.get(User, user_id).is_test_account = True
        db.commit()

    code = create_training_room(owner, "Round16 QA 多真人加入")["code"]
    joined = participant.post(
        f"/api/rooms/{code}/claim-seat",
        headers=csrf(participant),
        json={"seat_key": "neg_1"},
    )
    assert joined.status_code == 200, joined.text
    blocked = ordinary.get(f"/api/rooms/search?code={code}")
    assert blocked.status_code == 403

    with SessionLocal() as db:
        room = load_room(db, code)
        assert room.is_test_data is True
        assert next(item for item in room.seats if item.seat_key == "neg_1").user_id == user_ids[1]


def test_production_failed_step_retry_requires_and_accepts_authoritative_service_snapshot(
    register_user,
    monkeypatch,
) -> None:
    """The production retry verifier must model a normally started match."""

    owner = register_user("round16_retry_owner")
    code = _start_one_human(owner, "Round16 生产失败步骤重试")
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        room.status = "paused"
        room.failure_reason = "模拟 Agent 故障"
        room.paused_remaining_seconds = 90
        for seat in room.seats:
            if seat.occupant_type == "human":
                seat.connected = True
                seat.disconnected_at = None
        match.service_snapshot = {}
        db.add(
            Speech(
                match_id=match.id,
                room_id=room.id,
                seat_key="neg_1",
                stage_key="retry-stage",
                speaker_type="ai",
                status="failed",
                content="失败尝试保留的完整文本。",
            )
        )
        db.commit()

    monkeypatch.setattr(settings, "app_env", "production")
    headers = csrf(owner) | {"X-Idempotency-Key": "round16-production-retry"}
    rejected = owner.post(
        f"/api/rooms/{code}/control/retry",
        headers=headers,
        json={"reason": "服务恢复后重试"},
    )
    assert rejected.status_code == 409 and "服务配置快照" in rejected.json()["detail"]

    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        match.service_snapshot = build_service_snapshot(db)
        db.commit()

    retried = owner.post(
        f"/api/rooms/{code}/control/retry",
        headers=headers,
        json={"reason": "服务恢复后重试"},
    )
    replayed = owner.post(
        f"/api/rooms/{code}/control/retry",
        headers=headers,
        json={"reason": "服务恢复后重试"},
    )
    conflict = owner.post(
        f"/api/rooms/{code}/control/retry",
        headers=headers,
        json={"reason": "另一个重试参数"},
    )
    assert retried.status_code == 200, retried.text
    assert replayed.status_code == 200 and replayed.json()["replayed"] is True
    assert conflict.status_code == 409
    with SessionLocal() as db:
        room = load_room(db, code)
        speech = db.scalar(select(Speech).where(Speech.room_id == room.id))
        assert speech.status == "failed_retried"
        assert db.scalar(
            select(func.count(MatchEvent.id)).where(
                MatchEvent.room_id == room.id,
                MatchEvent.event_type == "control.retry",
            )
        ) == 1


def test_two_user_websockets_keep_identity_projection_isolated(register_user) -> None:
    """Every snapshot and incremental update is projected for its connection.

    RoomHub broadcasts only an event/sequence hint. The WebSocket handler must
    rebuild owner and participant views independently; a serialized owner view
    must never be reused for another student.
    """

    owner = register_user("round16_ws_owner")
    participant = register_user("round16_ws_participant")
    code = create_training_room(owner, "Round16 双用户 WS 投影隔离")["code"]
    claimed = participant.post(
        f"/api/rooms/{code}/claim-seat",
        headers=csrf(participant),
        json={"seat_key": "neg_1"},
    )
    assert claimed.status_code == 200, claimed.text

    def projection_for(socket, event_type: str) -> dict:
        for _ in range(10):
            message = socket.receive_json()
            if (message.get("event") or {}).get("type") == event_type:
                return message["room"]
        raise AssertionError(f"did not receive {event_type}")

    with owner.websocket_connect(f"/ws/rooms/{code}") as owner_socket:
        owner_initial = owner_socket.receive_json()["room"]
        with participant.websocket_connect(f"/ws/rooms/{code}") as participant_socket:
            participant_initial = participant_socket.receive_json()["room"]
            changed = participant.post(
                f"/api/rooms/{code}/ready",
                headers=csrf(participant),
                json={"ready": True},
            )
            assert changed.status_code == 200, changed.text
            owner_update = projection_for(owner_socket, "seat.ready_changed")
            participant_update = projection_for(participant_socket, "seat.ready_changed")

            for projection, seat_key, can_control in (
                (owner_initial, "aff_1", True),
                (owner_update, "aff_1", True),
                (participant_initial, "neg_1", False),
                (participant_update, "neg_1", False),
            ):
                assert projection["my_seat"] == seat_key
                assert projection["can_control"] is can_control
                marked = [seat["seat_key"] for seat in projection["seats"] if seat["is_me"]]
                assert marked == [seat_key]


@pytest.mark.asyncio
async def test_four_concurrent_rooms_isolate_malformed_judge_disconnect_and_manual_recovery(
    register_user,
    monkeypatch,
) -> None:
    """Run four non-audio rooms through distinct failure and repair paths."""

    malformed_owner = register_user("round16_malformed_judge")
    healthy_owner = register_user("round16_healthy_judge")
    disconnected_owner = register_user("round16_disconnected_owner")
    successor = register_user("round16_disconnect_successor")
    paused_owner = register_user("round16_paused_owner")

    malformed_code = _start_one_human(malformed_owner, "Round16 裁判异常房间")
    healthy_code = _start_one_human(healthy_owner, "Round16 裁判正常房间")
    disconnected_code = _start_two_humans(
        disconnected_owner,
        successor,
        "Round16 多真人断线接替房间",
    )
    paused_code = _start_one_human(paused_owner, "Round16 人工暂停房间")

    disconnected_owner_id = disconnected_owner.get("/api/auth/session").json()["user"]["id"]
    successor_id = successor.get("/api/auth/session").json()["user"]["id"]
    with SessionLocal() as db:
        for code in (malformed_code, healthy_code):
            room = load_room(db, code, lock=True)
            room.template_snapshot = [
                {"key": "judge", "name": "自动裁判", "kind": "judging", "duration": 300}
            ]
            room.current_stage_index = 0
            room.status = "judging"
            room.stage_started_at = now()
            room.stage_deadline_at = None

        disconnected_room = load_room(db, disconnected_code, lock=True)
        disconnected_room.template_snapshot = [
            {"key": "hold", "name": "等待恢复", "kind": "announcement", "duration": 3600}
        ]
        disconnected_room.current_stage_index = 0
        disconnected_room.status = "running"
        disconnected_room.stage_started_at = now()
        disconnected_room.stage_deadline_at = now() + timedelta(seconds=3600)
        old_owner_seat = next(item for item in disconnected_room.seats if item.user_id == disconnected_owner_id)
        old_owner_seat.connected = False
        old_owner_seat.disconnected_at = now() - timedelta(seconds=61)
        successor_seat = next(item for item in disconnected_room.seats if item.user_id == successor_id)
        successor_seat.connected = True
        successor_seat.disconnected_at = None

        paused_room = load_room(db, paused_code, lock=True)
        paused_room.template_snapshot = [
            {"key": "hold", "name": "人工暂停", "kind": "announcement", "duration": 3600}
        ]
        paused_room.current_stage_index = 0
        paused_room.status = "paused"
        paused_room.stage_started_at = now()
        paused_room.stage_deadline_at = None
        paused_room.paused_remaining_seconds = 900
        db.commit()

    async def judged(topic, speeches, **kwargs):
        await asyncio.sleep(0)
        if "异常" in topic:
            # Simulate an alternate adapter bypassing the normal provider
            # normalizer. This used to raise KeyError and repeatedly quarantine
            # the room instead of producing a human-reviewable outcome.
            return {"winner": "unknown", "affirmative_score": "NaN"}
        return {
            "winner": "aff",
            "affirmative_score": 88,
            "negative_score": 82,
            "individual_scores": {"aff_1": 90, "foreign_room_seat": 99},
            "reasoning": "双方论证完整，正方回应更充分。",
        }

    monkeypatch.setattr(judge_provider, "judge", judged)
    await asyncio.gather(
        match_engine.process_room(malformed_code),
        match_engine.process_room(healthy_code),
        match_engine.process_room(disconnected_code),
        match_engine.process_room(paused_code),
    )

    stale_control = disconnected_owner.post(
        f"/api/rooms/{disconnected_code}/control/pause",
        headers=csrf(disconnected_owner),
        json={"reason": "旧房主迟到控制"},
    )
    new_control = successor.post(
        f"/api/rooms/{disconnected_code}/control/pause",
        headers=csrf(successor) | {"X-Idempotency-Key": "round16-successor-pause"},
        json={"reason": "新房主检查接替状态"},
    )
    cross_room_control = paused_owner.post(
        f"/api/rooms/{healthy_code}/control/terminate",
        headers=csrf(paused_owner),
        json={"reason": "跨房越权操作"},
    )
    assert stale_control.status_code == 409
    assert new_control.status_code == 403
    assert cross_room_control.status_code == 403

    with SessionLocal() as db:
        malformed_room = load_room(db, malformed_code)
        healthy_room = load_room(db, healthy_code)
        disconnected_room = load_room(db, disconnected_code)
        paused_room = load_room(db, paused_code)
        malformed_match = db.scalar(select(Match).where(Match.room_id == malformed_room.id))
        healthy_match = db.scalar(select(Match).where(Match.room_id == healthy_room.id))
        malformed_scorecard = db.scalar(
            select(JudgeScorecard).where(JudgeScorecard.match_id == malformed_match.id)
        )
        healthy_scorecard = db.scalar(
            select(JudgeScorecard).where(JudgeScorecard.match_id == healthy_match.id)
        )
        old_owner_seat = next(item for item in disconnected_room.seats if item.user_id == disconnected_owner_id)

        assert malformed_room.status == malformed_match.status == "review_required"
        assert malformed_scorecard.status == "review_required"
        assert "无法识别的胜方" in malformed_scorecard.reasoning
        assert healthy_room.status == healthy_match.status == "completed"
        assert healthy_scorecard.status == "approved" and healthy_scorecard.winner == "aff"
        assert healthy_scorecard.individual_scores == {"aff_1": 90.0}
        assert disconnected_room.status == "paused" and disconnected_room.owner_id == disconnected_owner_id
        assert old_owner_seat.occupant_type == "human"
        assert db.scalar(
            select(MatchEvent.id).where(
                MatchEvent.room_id == disconnected_room.id,
                MatchEvent.event_type == "participant.disconnect_timeout",
            )
        )
        assert paused_room.status == "paused" and paused_room.paused_remaining_seconds == 900
        assert not db.scalar(
            select(MatchEvent.id).where(
                MatchEvent.room_id == healthy_room.id,
                MatchEvent.event_type.in_(["seat.ai_substituted", "control.pause"]),
            )
        )
