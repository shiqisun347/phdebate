from __future__ import annotations

import asyncio
import hashlib
from collections import defaultdict
from datetime import timedelta

import pytest
from app.core.database import SessionLocal
from app.models.entities import Competition, CompetitionTopic, JudgeScorecard, Match, MatchEvent, Speech
from app.services.match_engine import match_engine
from app.services.providers import ProviderError, debate_agent, judge_provider, lighttts
from app.services.room_service import load_room, now, remaining_seconds
from conftest import csrf
from sqlalchemy import select


def _create_room(owner, *, format_name: str, topic: str) -> str:
    payload: dict[str, object] = {
        "competition_slug": format_name,
        "seat_key": "aff_1",
        "visibility": "public",
    }
    if format_name == "training-1v1":
        payload["custom_topic"] = topic
    else:
        with SessionLocal() as db:
            competition = db.scalar(select(Competition).where(Competition.slug == format_name))
            assert competition is not None
            competition_topic = db.scalar(
                select(CompetitionTopic).where(
                    CompetitionTopic.competition_id == competition.id,
                    CompetitionTopic.is_active.is_(True),
                )
            )
            assert competition_topic is not None
            competition_topic.title = topic
            db.commit()
            payload["topic_id"] = competition_topic.id
    response = owner.post("/api/rooms", headers=csrf(owner), json=payload)
    assert response.status_code == 200, response.text
    return response.json()["room"]["code"]


def _ready(client, code: str) -> None:
    response = client.post(f"/api/rooms/{code}/ready", headers=csrf(client), json={"ready": True})
    assert response.status_code == 200, response.text


def _start(owner, code: str) -> None:
    response = owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    assert response.status_code == 200, response.text


def _lease(client, code: str, device: str) -> dict[str, str]:
    headers = csrf(client) | {"X-Control-Lease": device}
    response = client.post(f"/api/rooms/{code}/control-lease", headers=headers, json={})
    assert response.status_code == 200, response.text
    return headers


def _speak(client, code: str, headers: dict[str, str], content: str) -> None:
    identity = hashlib.sha256(content.encode()).hexdigest()[:16]
    started = client.post(
        f"/api/rooms/{code}/speech/start",
        headers=headers | {"X-Idempotency-Key": f"round55-start-{identity}"},
        json={},
    )
    assert started.status_code == 200, started.text
    finished = client.post(
        f"/api/rooms/{code}/speech/finish",
        headers=headers | {"X-Idempotency-Key": f"round55-finish-{identity}"},
        json={"speech_id": started.json()["speech_id"], "content": content},
    )
    assert finished.status_code == 200, finished.text


def _set_template(code: str, template: list[dict]) -> None:
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.template_snapshot = template
        db.commit()


@pytest.mark.asyncio
async def test_production_five_room_mixed_human_agent_flow_and_repair_controls(
    client,
    register_user,
    monkeypatch,
) -> None:
    """Exercise the whole production room allowance in one readable scenario.

    Two rooms contain two real human accounts, three rooms exercise automated
    Agent stages, and every owner repair control has an independent outcome.
    The simulation deliberately includes the 59/61-second disconnect boundary
    so a healthy room can never be replaced or advanced by another room.
    """

    owners = [register_user(f"round55_owner_{index}") for index in range(5)]
    guests = [register_user(f"round55_guest_{index}") for index in range(2)]
    topics = {
        "humans": "Round55 双真人完整赛",
        "four": "Round55 四对四真人与多智能体混合赛",
        "retry": "Round55 Agent 异常重试赛",
        "review": "Round55 裁判转人工复核赛",
        "terminate": "Round55 房主终止赛",
    }
    codes = {
        "humans": _create_room(owners[0], format_name="training-1v1", topic=topics["humans"]),
        "four": _create_room(owners[1], format_name="daily-4v4", topic=topics["four"]),
        "retry": _create_room(owners[2], format_name="training-1v1", topic=topics["retry"]),
        "review": _create_room(owners[3], format_name="training-1v1", topic=topics["review"]),
        "terminate": _create_room(owners[4], format_name="training-1v1", topic=topics["terminate"]),
    }

    for key, guest in zip(("humans", "four"), guests, strict=True):
        claimed = guest.post(
            f"/api/rooms/{codes[key]}/claim-seat",
            headers=csrf(guest),
            json={"seat_key": "neg_1"},
        )
        assert claimed.status_code == 200, claimed.text

    for key, owner in zip(codes, owners, strict=True):
        _ready(owner, codes[key])
    _ready(guests[0], codes["humans"])
    _ready(guests[1], codes["four"])
    for key, owner in zip(codes, owners, strict=True):
        _start(owner, codes[key])

    _set_template(
        codes["humans"],
        [
            {"key": "aff", "name": "正方真人立论", "kind": "speech", "seat": "aff_1", "duration": 90},
            {"key": "neg", "name": "反方真人立论", "kind": "speech", "seat": "neg_1", "duration": 90},
            {"key": "judge", "name": "裁判", "kind": "judging", "duration": 30},
        ],
    )
    _set_template(
        codes["four"],
        [
            {"key": "aff_h", "name": "正方真人立论", "kind": "speech", "seat": "aff_1", "duration": 90},
            {"key": "neg_h", "name": "反方真人立论", "kind": "speech", "seat": "neg_1", "duration": 90},
            {"key": "aff_ai", "name": "正方二辩驳论", "kind": "speech", "seat": "aff_2", "duration": 90},
            {"key": "neg_ai", "name": "反方二辩驳论", "kind": "speech", "seat": "neg_2", "duration": 90},
            {"key": "judge", "name": "裁判", "kind": "judging", "duration": 30},
        ],
    )
    _set_template(
        codes["retry"],
        [
            {"key": "ai", "name": "反方智能体立论", "kind": "speech", "seat": "neg_1", "duration": 90},
            {"key": "judge", "name": "裁判", "kind": "judging", "duration": 30},
        ],
    )
    _set_template(
        codes["review"],
        [
            {"key": "ai", "name": "反方智能体总结", "kind": "speech", "seat": "neg_1", "duration": 90},
            {"key": "judge", "name": "裁判", "kind": "judging", "duration": 30},
        ],
    )
    _set_template(
        codes["terminate"],
        [{"key": "human", "name": "终止前真人立论", "kind": "speech", "seat": "aff_1", "duration": 90}],
    )

    active_agents = 0
    peak_agents = 0
    agent_attempts: defaultdict[tuple[str, str], int] = defaultdict(int)
    agent_lock = asyncio.Lock()

    async def generated(payload, **_kwargs):
        nonlocal active_agents, peak_agents
        identity = (payload["room_code"], payload["current_stage"])
        agent_attempts[identity] += 1
        async with agent_lock:
            active_agents += 1
            peak_agents = max(peak_agents, active_agents)
        await asyncio.sleep(0.02)
        async with agent_lock:
            active_agents -= 1
        if payload["room_code"] == codes["retry"] and agent_attempts[identity] == 1:
            raise ProviderError("Round55 模拟 Agent 短暂异常", code="agent_timeout", retryable=True)
        return f"房间 {payload['room_code']} 围绕《{payload['debate_topic']}》完成 {payload['current_stage']}。"

    async def synthesized(*_args, **_kwargs):
        return ""

    async def judged(topic, speeches, **_kwargs):
        assert speeches and all(topic in item["content"] for item in speeches)
        return {
            "winner": "aff",
            "affirmative_score": 88,
            "negative_score": 84,
            "individual_scores": {},
            "reasoning": f"《{topic}》的独立裁判结论。",
        }

    monkeypatch.setattr(debate_agent, "generate", generated)
    monkeypatch.setattr(lighttts, "synthesize", synthesized)
    monkeypatch.setattr(judge_provider, "judge", judged)

    # Five rooms enter their first authoritative stage together. No room is
    # left in an unexplained preparing state and every running timer is valid.
    await asyncio.gather(*(match_engine.process_room(code) for code in codes.values()))
    with SessionLocal() as db:
        for code in codes.values():
            room = load_room(db, code)
            assert room.status == "running"
            assert room.current_stage_index == 0
            current = room.template_snapshot[0]
            if current.get("awaiting_human_start"):
                assert room.stage_started_at is None and room.stage_deadline_at is None
            else:
                assert room.stage_deadline_at is not None
            assert (remaining_seconds(room) or 0) > 0

    human_owner_headers = _lease(owners[0], codes["humans"], "round55-human-owner")
    human_guest_headers = _lease(guests[0], codes["humans"], "round55-human-guest")
    four_owner_headers = _lease(owners[1], codes["four"], "round55-four-owner")
    four_guest_headers = _lease(guests[1], codes["four"], "round55-four-guest")

    paused = owners[0].post(
        f"/api/rooms/{codes['humans']}/control/pause",
        headers=csrf(owners[0]) | {"X-Idempotency-Key": "round55-manual-pause"},
        json={"reason": "赛前设备复核"},
    )
    assert paused.status_code == 200 and paused.json()["room"]["status"] == "paused"
    resumed = owners[0].post(
        f"/api/rooms/{codes['humans']}/control/resume",
        headers=csrf(owners[0]) | {"X-Idempotency-Key": "round55-manual-resume"},
        json={"reason": "设备复核完成"},
    )
    assert resumed.status_code == 200 and resumed.json()["room"]["status"] == "running"
    _speak(owners[0], codes["humans"], human_owner_headers, f"《{topics['humans']}》正方真人完整立论。")
    _speak(owners[1], codes["four"], four_owner_headers, f"《{topics['four']}》正方真人完整立论。")
    _speak(guests[1], codes["four"], four_guest_headers, f"《{topics['four']}》反方真人完整立论。")

    # At 59 seconds the second human still owns the turn. At 61 seconds only
    # this room pauses; parallel Agent rooms continue or enter their own
    # explicit provider-failure state.
    with SessionLocal() as db:
        room = load_room(db, codes["humans"], lock=True)
        seat = next(item for item in room.seats if item.seat_key == "neg_1")
        seat.connected = False
        seat.disconnected_at = now() - timedelta(seconds=59)
        db.commit()
    await match_engine.process_room(codes["humans"])
    with SessionLocal() as db:
        assert load_room(db, codes["humans"]).status == "running"
        room = load_room(db, codes["humans"], lock=True)
        next(item for item in room.seats if item.seat_key == "neg_1").disconnected_at = now() - timedelta(seconds=61)
        db.commit()

    await asyncio.gather(
        match_engine.process_room(codes["humans"]),
        match_engine.process_room(codes["four"]),
        match_engine.process_room(codes["retry"]),
        match_engine.process_room(codes["review"]),
    )
    with SessionLocal() as db:
        human_room = load_room(db, codes["humans"])
        human_seat = next(item for item in human_room.seats if item.seat_key == "neg_1")
        assert human_room.status == "paused"
        assert human_seat.occupant_type == "human" and human_seat.user_id
        assert load_room(db, codes["retry"]).status == "paused"
        assert load_room(db, codes["review"]).status == "judging"
        assert load_room(db, codes["four"]).current_stage_index == 3
    assert peak_agents >= 2

    blocked = owners[0].post(
        f"/api/rooms/{codes['humans']}/control/resume",
        headers=csrf(owners[0]),
        json={"reason": "辩手仍离线"},
    )
    assert blocked.status_code == 409
    with SessionLocal() as db:
        room = load_room(db, codes["humans"], lock=True)
        for seat in room.seats:
            if seat.occupant_type == "human":
                seat.connected = True
                seat.disconnected_at = None
        db.commit()
    resumed_after_disconnect = owners[0].post(
        f"/api/rooms/{codes['humans']}/control/resume",
        headers=csrf(owners[0]) | {"X-Idempotency-Key": "round55-disconnect-resume"},
        json={"reason": "真人辩手已重新连接"},
    )
    assert resumed_after_disconnect.status_code == 200, resumed_after_disconnect.text
    _speak(guests[0], codes["humans"], human_guest_headers, f"《{topics['humans']}》反方真人重连后完整立论。")

    retried = owners[2].post(
        f"/api/rooms/{codes['retry']}/control/retry",
        headers=csrf(owners[2]) | {"X-Idempotency-Key": "round55-provider-retry"},
        json={"reason": "Agent 服务恢复"},
    )
    assert retried.status_code == 200, retried.text
    skipped = owners[3].post(
        f"/api/rooms/{codes['review']}/control/skip",
        headers=csrf(owners[3]) | {"X-Idempotency-Key": "round55-judge-skip"},
        json={"reason": "裁判转管理员复核"},
    )
    assert skipped.status_code == 200 and skipped.json()["room"]["status"] == "review_required"
    terminated = owners[4].post(
        f"/api/rooms/{codes['terminate']}/control/terminate",
        headers=csrf(owners[4]) | {"X-Idempotency-Key": "round55-terminate"},
        json={"reason": "参赛者主动结束测试比赛"},
    )
    assert terminated.status_code == 200 and terminated.json()["room"]["status"] == "terminated"

    for _ in range(6):
        processable: list[str] = []
        with SessionLocal() as db:
            for key in ("humans", "four", "retry"):
                if load_room(db, codes[key]).status in {"running", "judging"}:
                    processable.append(codes[key])
        if processable:
            await asyncio.gather(*(match_engine.process_room(code) for code in processable))
        with SessionLocal() as db:
            if all(load_room(db, codes[key]).status == "completed" for key in ("humans", "four", "retry")):
                break

    expected = {
        "humans": "completed",
        "four": "completed",
        "retry": "completed",
        "review": "review_required",
        "terminate": "terminated",
    }
    with SessionLocal() as db:
        for key, status in expected.items():
            room = load_room(db, codes[key])
            match = db.scalar(select(Match).where(Match.room_id == room.id))
            events = list(db.scalars(select(MatchEvent).where(MatchEvent.room_id == room.id).order_by(MatchEvent.seq)).all())
            assert room.status == status and match and match.status == status
            assert [event.seq for event in events] == list(range(1, room.seq + 1))
            assert not db.scalar(
                select(Speech.id).where(
                    Speech.room_id == room.id,
                    Speech.status.in_(("speaking", "synthesizing", "playing")),
                )
            )
            assert not any(event.event_type == "seat.ai_substituted" for event in events)
            assert all(seat.occupant_type != "ai_substitute" for seat in room.seats)
            if status == "completed":
                scorecard = db.scalar(select(JudgeScorecard).where(JudgeScorecard.match_id == match.id))
                assert scorecard and scorecard.status == "approved"

        human_room = load_room(db, codes["humans"])
        human_events = list(db.scalars(select(MatchEvent).where(MatchEvent.room_id == human_room.id)).all())
        assert sum(event.event_type == "participant.disconnect_timeout" for event in human_events) == 1
        assert {event.event_type for event in human_events} >= {"control.pause", "control.resume"}

    assert agent_attempts[(codes["retry"], "反方智能体立论")] == 2
