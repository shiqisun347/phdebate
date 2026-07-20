from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from app.core.database import SessionLocal
from app.main import app
from app.models.entities import JudgeScorecard, Match, MatchEvent, Room, Speech, TranscriptSegment
from app.services.match_engine import match_engine, recover_inflight_engine_tasks
from app.services.providers import judge_provider
from app.services.room_service import load_room, now
from conftest import csrf
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from test_platform import create_training_room


def _ready(browser: TestClient, code: str) -> None:
    response = browser.post(f"/api/rooms/{code}/ready", headers=csrf(browser), json={"ready": True})
    assert response.status_code == 200, response.text


def _lease(browser: TestClient, code: str, value: str) -> dict[str, str]:
    headers = csrf(browser) | {"X-Control-Lease": value}
    response = browser.post(f"/api/rooms/{code}/control-lease", headers=headers, json={})
    assert response.status_code == 200, response.text
    return headers


def test_four_rooms_race_for_seats_and_start_idempotently_without_cross_room_state(register_user) -> None:
    """Exercise the lobby hot path across four authoritative room locks.

    Each room has two students racing for the same negative seat. Exactly one
    wins, both human seats must be ready, and a two-device start retry must
    still create one match and one room.locked event.
    """

    scenarios: list[tuple[TestClient, TestClient, TestClient, str]] = []
    for index in range(4):
        owner = register_user(f"round14_room_{index}_owner")
        first = register_user(f"round14_room_{index}_first")
        second = register_user(f"round14_room_{index}_second")
        code = create_training_room(owner, f"Round14 四房并发隔离辩题 {index}")["code"]
        scenarios.append((owner, first, second, code))

    def claim(contender: TestClient, code: str):
        return contender.post(
            f"/api/rooms/{code}/claim-seat",
            headers=csrf(contender),
            json={"seat_key": "neg_1"},
        )

    winners: list[TestClient] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for owner, first, second, code in scenarios:
            first_future = pool.submit(claim, first, code)
            second_future = pool.submit(claim, second, code)
            first_response, second_response = first_future.result(), second_future.result()
            assert sorted([first_response.status_code, second_response.status_code]) == [200, 409]
            winners.append(first if first_response.status_code == 200 else second)

    for (owner, _first, _second, code), winner in zip(scenarios, winners, strict=True):
        _ready(owner, code)
        _ready(winner, code)

    def start_twice(owner: TestClient, code: str) -> tuple:
        second_device = TestClient(app)
        with second_device:
            second_device.cookies.update(owner.cookies)
            barrier = Barrier(2)

            def start(browser: TestClient):
                barrier.wait()
                return browser.post(f"/api/rooms/{code}/start", headers=csrf(browser), json={})

            with ThreadPoolExecutor(max_workers=2) as pool:
                return tuple(pool.map(start, (owner, second_device)))

    with ThreadPoolExecutor(max_workers=4) as pool:
        starts = list(
            pool.map(
                lambda item: start_twice(item[0], item[3]),
                scenarios,
            )
        )

    assert all([response.status_code for response in pair] == [200, 200] for pair in starts)
    assert all(sorted(bool(response.json().get("replayed")) for response in pair) == [False, True] for pair in starts)

    with SessionLocal() as db:
        room_ids: set[str] = set()
        for index, (_owner, _first, _second, code) in enumerate(scenarios):
            room = load_room(db, code)
            room_ids.add(room.id)
            matches = list(db.scalars(select(Match).where(Match.room_id == room.id)).all())
            events = list(
                db.scalars(select(MatchEvent).where(MatchEvent.room_id == room.id).order_by(MatchEvent.seq)).all()
            )
            assert room.status == "preparing"
            assert len(matches) == 1
            assert sum(item.event_type == "seat.claimed" for item in events) == 1
            assert sum(item.event_type == "room.locked" for item in events) == 1
            assert [item.seq for item in events] == list(range(1, room.seq + 1))
            assert all(f"辩题 {index}" not in other.topic for other in db.scalars(select(Room).where(Room.id != room.id)))
        assert len(room_ids) == 4


@pytest.mark.asyncio
async def test_paused_judge_retry_rejects_out_of_order_old_task_result(register_user, monkeypatch) -> None:
    """An interrupted judge response must never revive after a newer task starts."""

    owner = register_user("round14_judge_owner")
    code = create_training_room(owner, "Round14 裁判乱序任务不得覆盖新结果")["code"]
    _ready(owner, code)
    assert owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={}).status_code == 200
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.template_snapshot = [{"key": "judging", "name": "自动裁判", "kind": "judging", "duration": 30}]
        room.current_stage_index = 0
        room.status = "judging"
        room.stage_started_at = now()
        room.stage_deadline_at = None
        db.commit()

    started = [asyncio.Event(), asyncio.Event()]
    releases = [asyncio.Event(), asyncio.Event()]
    calls = 0

    async def judged(*args, **kwargs):
        nonlocal calls
        index = calls
        calls += 1
        started[index].set()
        await releases[index].wait()
        return {
            "winner": "aff" if index == 0 else "neg",
            "affirmative_score": 91.0 if index == 0 else 81.0,
            "negative_score": 79.0 if index == 0 else 93.0,
            "individual_scores": {},
            "reasoning": f"authoritative judge attempt {index + 1}",
        }

    monkeypatch.setattr(judge_provider, "judge", judged)
    old_task = asyncio.create_task(match_engine.process_room(code))
    await asyncio.wait_for(started[0].wait(), timeout=2)

    paused = owner.post(f"/api/rooms/{code}/control/pause", headers=csrf(owner), json={"reason": "网络异常暂停"})
    assert paused.status_code == 200, paused.text
    resumed = owner.post(f"/api/rooms/{code}/control/resume", headers=csrf(owner), json={"reason": "服务恢复"})
    assert resumed.status_code == 200, resumed.text

    new_task = asyncio.create_task(match_engine.process_room(code))
    await asyncio.wait_for(started[1].wait(), timeout=2)
    with SessionLocal() as db:
        room = load_room(db, code)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        scorecard = db.scalar(select(JudgeScorecard).where(JudgeScorecard.match_id == match.id))
        new_task_id = scorecard.task_id
        assert scorecard.status == "running" and new_task_id

    releases[0].set()
    await asyncio.wait_for(old_task, timeout=2)
    with SessionLocal() as db:
        room = load_room(db, code)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        scorecard = db.scalar(select(JudgeScorecard).where(JudgeScorecard.match_id == match.id))
        assert room.status == "running"
        assert match.status == "running"
        assert scorecard.status == "running" and scorecard.task_id == new_task_id
        assert scorecard.reasoning != "authoritative judge attempt 1"

    releases[1].set()
    await asyncio.wait_for(new_task, timeout=2)
    with SessionLocal() as db:
        room = load_room(db, code)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        scorecard = db.scalar(select(JudgeScorecard).where(JudgeScorecard.match_id == match.id))
        events = list(db.scalars(select(MatchEvent).where(MatchEvent.room_id == room.id)).all())
        assert room.status == match.status == "completed"
        assert match.winner == scorecard.winner == "neg"
        assert scorecard.reasoning == "authoritative judge attempt 2"
        assert len([item for item in events if item.event_type == "judge.started"]) == 2
        assert len([item for item in events if item.event_type == "judge.interrupted"]) == 1
        assert len([item for item in events if item.event_type == "match.completed"]) == 1


def test_cross_room_speech_ids_and_paused_late_finish_preserve_authoritative_data(register_user) -> None:
    first = register_user("round14_cross_room_first")
    second = register_user("round14_cross_room_second")
    first_code = create_training_room(first, "Round14 跨房隔离 A")["code"]
    second_code = create_training_room(second, "Round14 跨房隔离 B")["code"]
    for browser, code in ((first, first_code), (second, second_code)):
        _ready(browser, code)
        assert browser.post(f"/api/rooms/{code}/start", headers=csrf(browser), json={}).status_code == 200
        with SessionLocal() as db:
            room = load_room(db, code, lock=True)
            room.template_snapshot = [
                {"key": "human", "name": "真人立论", "kind": "speech", "seat": "aff_1", "duration": 60}
            ]
            room.current_stage_index = 0
            room.status = "running"
            room.stage_started_at = now()
            db.commit()

    first_headers = _lease(first, first_code, "round14-shared-looking-lease")
    second_headers = _lease(second, second_code, "round14-shared-looking-lease")
    first_speech = first.post(f"/api/rooms/{first_code}/speech/start", headers=first_headers, json={}).json()["speech_id"]
    second_speech = second.post(f"/api/rooms/{second_code}/speech/start", headers=second_headers, json={}).json()["speech_id"]

    wrong_room = second.post(
        f"/api/rooms/{second_code}/speech/finish",
        headers=second_headers,
        json={"speech_id": first_speech, "content": "错误房间的任务标识不得写入本房间比赛记录。"},
    )
    assert wrong_room.status_code == 409

    with SessionLocal() as db:
        first_room = load_room(db, first_code, lock=True)
        speech = db.get(Speech, first_speech)
        speech.status = "timed_out"
        first_room.status = "paused"
        first_room.paused_remaining_seconds = 30
        first_room.stage_deadline_at = None
        db.commit()
        first_seq = first_room.seq

    late = first.post(
        f"/api/rooms/{first_code}/speech/finish",
        headers=first_headers | {"X-Idempotency-Key": "round14-paused-late-finish"},
        json={"speech_id": first_speech, "content": "暂停期间到达的弱网最终文本应保留，但不能推进比赛阶段。"},
    )
    assert late.status_code == 200 and late.json()["timed_out"] is True
    with SessionLocal() as db:
        first_room = load_room(db, first_code)
        second_room = load_room(db, second_code)
        stored = db.get(Speech, first_speech)
        untouched = db.get(Speech, second_speech)
        assert first_room.status == "paused" and first_room.current_stage_index == 0
        assert first_room.seq == first_seq + 1
        assert stored.status == "completed" and "弱网最终文本" in stored.content
        assert untouched.status == "speaking" and untouched.content == ""
        assert db.scalar(select(func.count(TranscriptSegment.id)).where(TranscriptSegment.speech_id == first_speech)) == 1
        assert not db.scalar(
            select(MatchEvent.id).where(
                MatchEvent.room_id == second_room.id,
                MatchEvent.event_type == "speech.late_finalized",
            )
        )


def test_engine_restart_invalidates_judge_task_and_preserves_event_sequence(register_user) -> None:
    owner = register_user("round14_restart_owner")
    code = create_training_room(owner, "Round14 引擎重启恢复")["code"]
    _ready(owner, code)
    assert owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={}).status_code == 200
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        room.template_snapshot = [{"key": "judging", "name": "自动裁判", "kind": "judging", "duration": 30}]
        room.current_stage_index = 0
        room.status = "judging"
        db.add(JudgeScorecard(match_id=match.id, status="running", task_id="old-process-task"))
        db.commit()

    recovered = recover_inflight_engine_tasks()
    assert recovered["judges"] >= 1
    with SessionLocal() as db:
        room = load_room(db, code)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        scorecard = db.scalar(select(JudgeScorecard).where(JudgeScorecard.match_id == match.id))
        events = list(db.scalars(select(MatchEvent).where(MatchEvent.room_id == room.id).order_by(MatchEvent.seq)).all())
        interrupted = next(item for item in events if item.event_type == "judge.interrupted")
        assert scorecard.status == "interrupted" and scorecard.task_id == ""
        assert interrupted.payload["task_id"] == "old-process-task"
        assert [item.seq for item in events] == list(range(1, room.seq + 1))
