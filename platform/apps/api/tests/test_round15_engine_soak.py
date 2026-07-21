from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import timedelta

import pytest
from app.core.database import SessionLocal
from app.models.entities import JudgeScorecard, Match, MatchEvent, Speech
from app.services.match_engine import MatchEngine, match_engine, recover_inflight_engine_tasks
from app.services.providers import debate_agent, judge_provider, lighttts
from app.services.room_service import load_room, now, stage
from conftest import csrf
from sqlalchemy import func, select
from test_platform import create_training_room


def _ready(browser, code: str) -> None:
    response = browser.post(f"/api/rooms/{code}/ready", headers=csrf(browser), json={"ready": True})
    assert response.status_code == 200, response.text


def _create_daily_room(browser, topic_id: str, index: int) -> str:
    response = browser.post(
        "/api/rooms",
        headers=csrf(browser),
        json={
            "competition_slug": "daily-4v4",
            "topic_id": topic_id,
            "seat_key": "aff_1",
            "visibility": "private",
            "creation_key": f"round15-daily-{index}",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["room"]["code"]


def _finish_opening_human_turn(browser, code: str, index: int) -> None:
    headers = csrf(browser) | {"X-Control-Lease": f"round15-device-{index}"}
    lease = browser.post(f"/api/rooms/{code}/control-lease", headers=headers, json={})
    assert lease.status_code == 200, lease.text
    started = browser.post(f"/api/rooms/{code}/speech/start", headers=headers, json={})
    assert started.status_code == 200, started.text
    finished = browser.post(
        f"/api/rooms/{code}/speech/finish",
        headers=headers | {"X-Idempotency-Key": f"round15-human-finish-{index}"},
        json={
            "speech_id": started.json()["speech_id"],
            "content": f"Round15 真人开篇发言 {index}：本场论证只属于房间 {code}。",
        },
    )
    assert finished.status_code == 200, finished.text


@pytest.mark.asyncio
async def test_paused_room_remains_scheduled_for_disconnect_substitution(register_user) -> None:
    """A paused room must not strand an offline participant forever.

    Redis presence expiry records the disconnect, while the match engine owns
    the 60-second AI substitution and owner-transfer policy.  The scheduler
    therefore has to keep paused rooms in its lifecycle set even though it does
    not advance their debate stage.
    """

    assert "paused" in MatchEngine._ACTIVE_ROOM_STATUSES
    owner = register_user("round15_paused_owner")
    teammate = register_user("round15_paused_teammate")
    code = create_training_room(owner, "暂停期间离线也必须可恢复")["code"]
    claimed = teammate.post(
        f"/api/rooms/{code}/claim-seat",
        headers=csrf(teammate),
        json={"seat_key": "neg_1"},
    )
    assert claimed.status_code == 200, claimed.text
    _ready(owner, code)
    _ready(teammate, code)
    assert owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={}).status_code == 200

    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.status = "paused"
        room.current_stage_index = 0
        room.stage_deadline_at = None
        room.paused_remaining_seconds = 45
        owner_seat = next(item for item in room.seats if item.user_id == room.owner_id)
        owner_seat.connected = False
        owner_seat.disconnected_at = now() - timedelta(seconds=61)
        db.commit()

    await match_engine.process_room(code)

    with SessionLocal() as db:
        room = load_room(db, code)
        old_owner = next(item for item in room.seats if item.seat_key == "aff_1")
        assert room.status == "paused" and room.current_stage_index == 0
        assert room.paused_remaining_seconds == 45 and room.stage_deadline_at is None
        assert old_owner.occupant_type == "ai_substitute"
        assert room.owner_id == next(item.user_id for item in room.seats if item.seat_key == "neg_1")
        assert db.scalar(
            select(func.count(MatchEvent.id)).where(
                MatchEvent.room_id == room.id,
                MatchEvent.event_type == "seat.ai_substituted",
            )
        ) == 1


@pytest.mark.asyncio
async def test_five_room_mixed_format_long_soak_preserves_authority_and_recovers(
    client,
    register_user,
    monkeypatch,
) -> None:
    """Run the production maximum of five mixed 1v1/4v4 rooms through judging.

    This is a deterministic logical-time soak rather than a wall-clock sleep:
    it combines human turns, AI-filled seats, manual pause/resume, a paused
    disconnect substitution, engine-restart recovery and out-of-order judge
    retry while every allowed room advances concurrently.
    """

    competition = client.get("/api/competitions/daily-4v4").json()["competition"]
    topic_id = competition["topics"][0]["id"]
    scenarios: list[dict] = []
    retry_judge_codes: set[str] = set()
    restart_codes: set[str] = set()
    disconnected_codes: set[str] = set()

    for index in range(5):
        owner = register_user(f"round15_soak_owner_{index}")
        if index % 2:
            code = _create_daily_room(owner, topic_id, index)
            format_name = "4v4"
        else:
            code = create_training_room(owner, f"Round15 1v1 长时并发辩题 {index}")["code"]
            format_name = "1v1"
        _ready(owner, code)
        started = owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
        assert started.status_code == 200, started.text

        with SessionLocal() as db:
            room = load_room(db, code, lock=True)
            speech_stages = [
                {
                    "key": f"{seat.seat_key}_case",
                    "name": f"{seat.seat_key} 陈词",
                    "kind": "speech",
                    "seat": seat.seat_key,
                    "duration": 1_800,
                }
                for seat in sorted(room.seats, key=lambda item: (item.side, item.position))
            ]
            room.template_snapshot = speech_stages + [
                {"key": "judging", "name": "自动裁判", "kind": "judging", "duration": 300}
            ]
            room.current_stage_index = 0
            room.status = "running"
            room.stage_started_at = now()
            room.stage_deadline_at = now() + timedelta(seconds=1_800)
            db.commit()

        if index % 5 == 0:
            disconnected_codes.add(code)
            with SessionLocal() as db:
                room = load_room(db, code, lock=True)
                room.status = "paused"
                room.stage_deadline_at = None
                room.paused_remaining_seconds = 1_777
                seat = next(item for item in room.seats if item.user_id == room.owner_id)
                seat.connected = False
                seat.disconnected_at = now() - timedelta(seconds=61)
                db.commit()
            await match_engine.process_room(code)
            resumed = owner.post(
                f"/api/rooms/{code}/control/resume",
                headers=csrf(owner),
                json={"reason": "离线接替后继续自动流程"},
            )
            assert resumed.status_code == 200, resumed.text
        else:
            _finish_opening_human_turn(owner, code, index)

        if index % 5 == 1:
            paused = owner.post(
                f"/api/rooms/{code}/control/pause",
                headers=csrf(owner),
                json={"reason": "长时仿真人工暂停"},
            )
            assert paused.status_code == 200, paused.text
            resumed = owner.post(
                f"/api/rooms/{code}/control/resume",
                headers=csrf(owner),
                json={"reason": "长时仿真恢复"},
            )
            assert resumed.status_code == 200, resumed.text
        if index % 5 == 2:
            restart_codes.add(code)
        if index % 5 == 3:
            retry_judge_codes.add(code)
        scenarios.append({"index": index, "owner": owner, "code": code, "format": format_name})

    # Simulate a process crash after the next AI Agent result was persisted but
    # before synthesis/playback could publish it.  Recovery must invalidate the
    # old task and reuse only the exact current-stage content.
    with SessionLocal() as db:
        for code in restart_codes:
            room = load_room(db, code, lock=True)
            current = stage(room)
            match = db.scalar(select(Match).where(Match.room_id == room.id))
            current_with_freeze = dict(current)
            current_with_freeze["ai_preparing"] = True
            current_with_freeze["preparing_stage_remaining_seconds"] = 1_650
            snapshot = list(room.template_snapshot)
            snapshot[room.current_stage_index] = current_with_freeze
            room.template_snapshot = snapshot
            room.stage_deadline_at = None
            db.add(
                Speech(
                    match_id=match.id,
                    room_id=room.id,
                    seat_key=current["seat"],
                    stage_key=current["key"],
                    speaker_type="ai",
                    status="synthesizing",
                    content=f"{room.topic}｜重启后应只复用本房间当前席位的完整发言。",
                )
            )
        db.commit()
    recovered = recover_inflight_engine_tasks()
    assert recovered["speeches"] >= len(restart_codes)

    async def generated(payload, *args, **kwargs):
        await asyncio.sleep(0)
        return (
            f"{payload['debate_topic']}｜{payload['debater_name']}｜"
            f"{payload['current_stage']}｜房间 {payload['room_code']} 的独立论证。"
        )

    async def synthesized(*args, room_code: str, speech_id: str, **kwargs):
        # An absent test WAV intentionally exercises the no-audio completion
        # branch without modifying the protected production audio pipeline.
        await asyncio.sleep(0)
        return f"/media/round15/{room_code}/{speech_id}.wav"

    judge_calls: defaultdict[str, int] = defaultdict(int)
    judge_started: dict[tuple[str, int], asyncio.Event] = {}
    judge_release: dict[tuple[str, int], asyncio.Event] = {}
    for code in retry_judge_codes:
        for attempt in range(2):
            judge_started[(code, attempt)] = asyncio.Event()
            judge_release[(code, attempt)] = asyncio.Event()

    async def judged(topic, speeches, **kwargs):
        transcript = "\n".join(str(item.get("content") or "") for item in speeches)
        code = next(item["code"] for item in scenarios if item["code"] in transcript)
        attempt = judge_calls[code]
        judge_calls[code] += 1
        if code in retry_judge_codes:
            judge_started[(code, attempt)].set()
            await judge_release[(code, attempt)].wait()
        return {
            "winner": "neg" if attempt else "aff",
            "affirmative_score": 83.0 if attempt else 89.0,
            "negative_score": 91.0 if attempt else 81.0,
            "individual_scores": {},
            "reasoning": f"{topic}｜房间 {code}｜裁判任务 {attempt + 1}",
        }

    monkeypatch.setattr(debate_agent, "generate", generated)
    monkeypatch.setattr(lighttts, "synthesize", synthesized)
    monkeypatch.setattr(judge_provider, "judge", judged)

    codes = [item["code"] for item in scenarios]
    # Eight 4v4 speeches plus judging need at most nine state transitions.  The
    # extra cycles emulate delayed scheduler ticks and verify idempotent rest.
    for _logical_tick in range(14):
        processable: list[str] = []
        with SessionLocal() as db:
            for code in codes:
                room = load_room(db, code)
                if room.status in {"completed", "review_required"}:
                    continue
                if code in retry_judge_codes and room.status == "judging":
                    continue
                processable.append(code)
        if processable:
            await asyncio.gather(*(match_engine.process_room(code) for code in processable))

    # Four judge providers complete out of order: pause invalidates attempt A,
    # attempt B becomes authoritative, and A is released first.
    old_tasks = [asyncio.create_task(match_engine.process_room(code)) for code in sorted(retry_judge_codes)]
    await asyncio.gather(*(judge_started[(code, 0)].wait() for code in retry_judge_codes))
    for scenario in scenarios:
        if scenario["code"] not in retry_judge_codes:
            continue
        paused = scenario["owner"].post(
            f"/api/rooms/{scenario['code']}/control/pause",
            headers=csrf(scenario["owner"]),
            json={"reason": "裁判任务 A 延迟"},
        )
        assert paused.status_code == 200, paused.text
        resumed = scenario["owner"].post(
            f"/api/rooms/{scenario['code']}/control/resume",
            headers=csrf(scenario["owner"]),
            json={"reason": "启动裁判任务 B"},
        )
        assert resumed.status_code == 200, resumed.text
    new_tasks = [asyncio.create_task(match_engine.process_room(code)) for code in sorted(retry_judge_codes)]
    await asyncio.gather(*(judge_started[(code, 1)].wait() for code in retry_judge_codes))
    for code in retry_judge_codes:
        judge_release[(code, 0)].set()
    await asyncio.gather(*old_tasks)
    with SessionLocal() as db:
        for code in retry_judge_codes:
            room = load_room(db, code)
            match = db.scalar(select(Match).where(Match.room_id == room.id))
            scorecard = db.scalar(select(JudgeScorecard).where(JudgeScorecard.match_id == match.id))
            assert room.status == "running" and scorecard.status == "running" and scorecard.task_id
    for code in retry_judge_codes:
        judge_release[(code, 1)].set()
    await asyncio.gather(*new_tasks)

    with SessionLocal() as db:
        total_speeches = 0
        room_ids: set[str] = set()
        for scenario in scenarios:
            room = load_room(db, scenario["code"])
            room_ids.add(room.id)
            match = db.scalar(select(Match).where(Match.room_id == room.id))
            scorecard = db.scalar(select(JudgeScorecard).where(JudgeScorecard.match_id == match.id))
            speeches = list(db.scalars(select(Speech).where(Speech.room_id == room.id)).all())
            events = list(
                db.scalars(select(MatchEvent).where(MatchEvent.room_id == room.id).order_by(MatchEvent.seq)).all()
            )
            total_speeches += len(speeches)

            assert room.status == match.status == "completed"
            assert scorecard and scorecard.status == "approved"
            assert [item.seq for item in events] == list(range(1, room.seq + 1))
            assert all(item.room_id == room.id for item in events)
            assert not any(item.status in {"speaking", "synthesizing", "playing"} for item in speeches)
            assert all(
                room.topic in item.content if item.speaker_type == "ai" else room.code in item.content
                for item in speeches
                if item.status == "completed"
            )
            expected_stage_count = 8 if scenario["format"] == "4v4" else 2
            assert len({item.stage_key for item in speeches if item.status == "completed"}) == expected_stage_count
            if scenario["code"] in disconnected_codes:
                owner_seat = next(item for item in room.seats if item.seat_key == "aff_1")
                assert owner_seat.occupant_type == "ai_substitute"
            if scenario["code"] in restart_codes:
                assert any(item.event_type == "speech.interrupted" for item in events)
                assert any(item.event_type == "speech.content.reused" for item in events)
            if scenario["code"] in retry_judge_codes:
                assert scorecard.winner == "neg"
                assert len([item for item in events if item.event_type == "judge.started"]) == 2
                assert len([item for item in events if item.event_type == "judge.interrupted"]) == 1

        assert len(room_ids) == 5
        assert total_speeches >= 22
