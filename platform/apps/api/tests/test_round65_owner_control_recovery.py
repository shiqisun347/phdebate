from __future__ import annotations

from app.core.database import SessionLocal
from app.models.entities import Match, MatchEvent, Speech
from app.services.match_engine import match_engine
from app.services.room_service import load_room, remaining_seconds, stage
from conftest import csrf
from sqlalchemy import select
from test_platform import create_training_room


def _ready(browser, code: str) -> None:
    response = browser.post(f"/api/rooms/{code}/ready", headers=csrf(browser), json={"ready": True})
    assert response.status_code == 200, response.text


def _lease(browser, code: str, device: str) -> dict[str, str]:
    headers = csrf(browser) | {"X-Control-Lease": device}
    response = browser.post(f"/api/rooms/{code}/control-lease", headers=headers, json={})
    assert response.status_code == 200, response.text
    return headers


async def test_owner_can_pause_and_resume_initial_match_preparation_without_finishing_match(register_user) -> None:
    """Opening preparation is a controllable wait, not a phantom match end."""

    owner = register_user("round65_preparing_owner")
    code = create_training_room(owner, "开赛准备暂停恢复不能提前结束比赛")["code"]
    _ready(owner, code)
    started = owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    assert started.status_code == 200, started.text
    assert started.json()["room"]["status"] == "preparing"
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.template_snapshot = [
            {"key": "opening", "name": "开场等待", "kind": "announcement", "duration": 30},
            {"key": "judge", "name": "裁判", "kind": "judging", "duration": 1},
        ]
        assert room.current_stage_index == -1
        assert room.stage_deadline_at is None
        db.commit()

    pause_headers = csrf(owner) | {"X-Idempotency-Key": "round65-pause-preparing"}
    paused = owner.post(
        f"/api/rooms/{code}/control/pause",
        headers=pause_headers,
        json={"reason": "开场设备检查"},
    )
    replayed_pause = owner.post(
        f"/api/rooms/{code}/control/pause",
        headers=pause_headers,
        json={"reason": "开场设备检查"},
    )
    assert paused.status_code == 200, paused.text
    assert paused.json()["room"]["status"] == "paused"
    assert paused.json()["room"]["remaining_seconds"] is None
    assert replayed_pause.status_code == 200 and replayed_pause.json()["replayed"] is True

    # Engine ticks during a deliberate pause must not enter a stage, create a
    # speech, or turn a no-stage room into review_required.
    for _ in range(3):
        await match_engine.process_room(code)
    with SessionLocal() as db:
        room = load_room(db, code)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        assert room.status == "paused" and room.current_stage_index == -1
        assert match and match.status == "running"
        assert not db.scalar(select(Speech.id).where(Speech.room_id == room.id))

    resume_headers = csrf(owner) | {"X-Idempotency-Key": "round65-resume-preparing"}
    resumed = owner.post(
        f"/api/rooms/{code}/control/resume",
        headers=resume_headers,
        json={"reason": "开场设备检查完成"},
    )
    replayed_resume = owner.post(
        f"/api/rooms/{code}/control/resume",
        headers=resume_headers,
        json={"reason": "开场设备检查完成"},
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["room"]["status"] == "preparing"
    assert resumed.json()["room"]["current_stage_index"] == -1
    assert replayed_resume.status_code == 200 and replayed_resume.json()["replayed"] is True

    await match_engine.process_room(code)
    with SessionLocal() as db:
        room = load_room(db, code)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        events = list(db.scalars(select(MatchEvent).where(MatchEvent.room_id == room.id).order_by(MatchEvent.seq)).all())
        assert room.status == "running" and room.current_stage_index == 0
        assert stage(room)["key"] == "opening"
        assert remaining_seconds(room) in {29, 30}
        assert match and match.status == "running"
        assert [event.event_type for event in events].count("control.pause") == 1
        assert [event.event_type for event in events].count("control.resume") == 1


async def test_free_debate_with_unavailable_host_cue_still_starts_both_clocks(register_user) -> None:
    """A cue label without playable audio must not collapse free debate to one turn."""

    owner = register_user("round65_free_missing_cue_owner")
    code = create_training_room(owner, "自由辩论提示音缺失时仍应完整换方")["code"]
    _ready(owner, code)
    assert owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={}).status_code == 200
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.status = "running"
        room.template_snapshot = [
            {
                "key": "free",
                "name": "自由辩论",
                "kind": "free",
                "side": "aff",
                "duration": 120,
                "turn_duration": 30,
                "cue": "进入自由辩论。",
            },
            {"key": "judge", "name": "裁判", "kind": "judging", "duration": 1},
        ]
        match_engine._enter_stage(db, room, 0)
        for seat in room.seats:
            if seat.occupant_type == "human":
                seat.connected = True
                seat.disconnected_at = None
        db.commit()
        current = stage(room)
        assert current.get("kind") == "free"
        assert current.get("awaiting_human_start") is True
        assert current.get("free_stage_remaining_seconds") == 120
        assert room.stage_deadline_at is None

    headers = _lease(owner, code, "round65-free-cue-device")
    started = owner.post(f"/api/rooms/{code}/speech/start", headers=headers, json={})
    assert started.status_code == 200, started.text
    with SessionLocal() as db:
        room = load_room(db, code)
        current = stage(room)
        assert room.stage_deadline_at is not None
        assert remaining_seconds(room) in {119, 120}
        assert current.get("turn_started_at")

    finished = owner.post(
        f"/api/rooms/{code}/speech/finish",
        headers=headers,
        json={"speech_id": started.json()["speech_id"], "content": "正方完成自由辩论首轮并等待反方换方回应。"},
    )
    assert finished.status_code == 200, finished.text
    with SessionLocal() as db:
        room = load_room(db, code)
        current = stage(room)
        assert room.current_stage_index == 0
        assert current.get("kind") == "free"
        assert current.get("intermission_side") == "neg"
        assert current.get("free_stage_remaining_seconds", 0) > 0
        assert db.scalar(
            select(MatchEvent.id).where(
                MatchEvent.room_id == room.id,
                MatchEvent.event_type == "free.intermission_started",
            )
        )


async def test_only_owner_controls_live_human_recovery_and_termination(register_user) -> None:
    """A participant cannot bypass owner recovery controls or preserve stale speech writes."""

    owner = register_user("round65_control_owner")
    participant = register_user("round65_control_participant")
    code = create_training_room(owner, "房主控制、紧急暂停与终止边界")["code"]
    claimed = participant.post(
        f"/api/rooms/{code}/claim-seat",
        headers=csrf(participant),
        json={"seat_key": "neg_1"},
    )
    assert claimed.status_code == 200, claimed.text
    _ready(owner, code)
    _ready(participant, code)
    assert owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={}).status_code == 200
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.status = "running"
        room.current_stage_index = 0
        room.template_snapshot = [
            {"key": "human", "name": "正方真人立论", "kind": "speech", "seat": "aff_1", "duration": 120},
            {"key": "next", "name": "反方真人立论", "kind": "speech", "seat": "neg_1", "duration": 120},
        ]
        match_engine._enter_stage(db, room, 0)
        for seat in room.seats:
            if seat.occupant_type == "human":
                seat.connected = True
                seat.disconnected_at = None
        db.commit()

    for action in ("pause", "terminate"):
        rejected = participant.post(
            f"/api/rooms/{code}/control/{action}",
            headers=csrf(participant),
            json={"reason": "普通辩手不应获得房主控制"},
        )
        assert rejected.status_code == 403

    owner_headers = _lease(owner, code, "round65-owner-device")
    first_started = owner.post(f"/api/rooms/{code}/speech/start", headers=owner_headers, json={})
    assert first_started.status_code == 200, first_started.text
    first_speech_id = first_started.json()["speech_id"]

    normal_pause = owner.post(
        f"/api/rooms/{code}/control/pause",
        headers=csrf(owner),
        json={"reason": "不能静默丢弃真人发言"},
    )
    assert normal_pause.status_code == 409
    assert "结束发言" in normal_pause.json()["detail"]

    safe_paused = owner.post(
        f"/api/rooms/{code}/control/safe-pause",
        headers=csrf(owner),
        json={"reason": "真人设备异常"},
    )
    assert safe_paused.status_code == 200, safe_paused.text
    assert safe_paused.json()["room"]["status"] == "paused"
    assert safe_paused.json()["room"]["current_stage"]["awaiting_human_start"] is True
    with SessionLocal() as db:
        assert db.get(Speech, first_speech_id).status == "interrupted"

    resumed = owner.post(
        f"/api/rooms/{code}/control/resume",
        headers=csrf(owner),
        json={"reason": "真人设备恢复"},
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["room"]["status"] == "running"
    assert resumed.json()["room"]["current_stage"]["awaiting_human_start"] is True

    second_started = owner.post(f"/api/rooms/{code}/speech/start", headers=owner_headers, json={})
    assert second_started.status_code == 200, second_started.text
    second_speech_id = second_started.json()["speech_id"]
    terminate_headers = csrf(owner) | {"X-Idempotency-Key": "round65-terminate-live-speech"}
    terminated = owner.post(
        f"/api/rooms/{code}/control/terminate",
        headers=terminate_headers,
        json={"reason": "现场确认终止比赛"},
    )
    replayed_terminate = owner.post(
        f"/api/rooms/{code}/control/terminate",
        headers=terminate_headers,
        json={"reason": "现场确认终止比赛"},
    )
    assert terminated.status_code == 200, terminated.text
    assert terminated.json()["room"]["status"] == "terminated"
    assert replayed_terminate.status_code == 200 and replayed_terminate.json()["replayed"] is True

    stale_finish = owner.post(
        f"/api/rooms/{code}/speech/finish",
        headers=owner_headers,
        json={"speech_id": second_speech_id, "content": "终止后到达的迟到真人文字不能复活比赛。"},
    )
    assert stale_finish.status_code == 409
    with SessionLocal() as db:
        room = load_room(db, code)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        active = db.scalar(
            select(Speech.id).where(
                Speech.room_id == room.id,
                Speech.status.in_(["speaking", "synthesizing", "playing"]),
            )
        )
        events = list(db.scalars(select(MatchEvent).where(MatchEvent.room_id == room.id).order_by(MatchEvent.seq)).all())
        assert room.status == match.status == "terminated"
        assert db.get(Speech, second_speech_id).status == "interrupted"
        assert active is None
        assert "seat.ai_substituted" not in {event.event_type for event in events}
        assert all(seat.occupant_type == "human" for seat in room.seats)

    await match_engine.process_room(code)
    with SessionLocal() as db:
        room = load_room(db, code)
        assert room.status == "terminated" and room.current_stage_index == 0
