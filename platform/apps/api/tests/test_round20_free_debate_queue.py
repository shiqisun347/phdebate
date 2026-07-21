from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from app.core.database import SessionLocal
from app.models.entities import FreeTurnRequest, Match, MatchEvent, Speech, User
from app.services.agent_decision import AgentSpeechDecision
from app.services.free_turn_queue import begin_intermission, request_free_turn
from app.services.match_engine import match_engine
from app.services.room_service import load_room, now, stage
from conftest import csrf
from sqlalchemy import select
from test_platform import create_training_room


def _daily_room(owner, client, topic: str) -> str:
    competition = client.get("/api/competitions/daily-4v4").json()["competition"]
    created = owner.post(
        "/api/rooms",
        headers=csrf(owner),
        json={
            "competition_slug": "daily-4v4",
            "topic_id": competition["topics"][0]["id"],
            "seat_key": "aff_1",
            "visibility": "public",
        },
    )
    assert created.status_code == 200, f"{topic}: {created.text}"
    return created.json()["room"]["code"]


def _claim(browser, code: str, seat_key: str) -> None:
    response = browser.post(f"/api/rooms/{code}/claim-seat", headers=csrf(browser), json={"seat_key": seat_key})
    assert response.status_code == 200, response.text


def _start_free_room(owner, code: str, participants: list, *, active_side: str = "aff") -> str:
    for browser in [owner, *participants]:
        assert browser.post(f"/api/rooms/{code}/ready", headers=csrf(browser), json={"ready": True}).status_code == 200
    assert owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={}).status_code == 200
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        room.template_snapshot = [
            {
                "key": "free",
                "name": "自由辩论",
                "kind": "free",
                "side": active_side,
                "duration": 300,
                "turn_duration": 30,
                "turn_seq": 0,
                "turn_started_at": now().isoformat(),
            }
        ]
        room.current_stage_index = 0
        room.status = "running"
        room.stage_started_at = now()
        room.stage_deadline_at = now() + timedelta(seconds=300)
        for seat in room.seats:
            if seat.occupant_type == "human":
                seat.connected = True
                seat.disconnected_at = None
        active_seat = next(item for item in room.seats if item.side == active_side and item.occupant_type == "human")
        speech = Speech(
            match_id=match.id,
            room_id=room.id,
            seat_key=active_seat.seat_key,
            stage_key="free",
            speaker_type="human",
            status="speaking",
        )
        db.add(speech)
        db.commit()
        return speech.id


def test_concurrent_requests_are_ordered_once_and_only_selected_human_can_speak(client, register_user) -> None:
    owner = register_user("round20_queue_owner")
    first = register_user("round20_queue_first")
    second = register_user("round20_queue_second")
    code = _daily_room(owner, client, "Round20 多人举手排序")
    _claim(first, code, "neg_1")
    _claim(second, code, "neg_2")
    speech_id = _start_free_room(owner, code, [first, second])

    barrier = Barrier(2)

    def request(browser, key: str):
        barrier.wait()
        return browser.post(
            f"/api/rooms/{code}/free-turn-requests",
            headers=csrf(browser) | {"X-Idempotency-Key": key},
            json={},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda item: request(*item), ((first, "first"), (second, "second"))))
    assert [item.status_code for item in responses] == [200, 200]
    queue_items = first.get(f"/api/rooms/{code}").json()["room"]["free_turn_queue"]["items"]
    assert [item["order"] for item in queue_items] == [1, 2]
    assert all(item["requested_at"].endswith(("+00:00", "Z")) for item in queue_items)
    assert next(item for item in queue_items if item["is_me"])["request_id"]
    assert all("request_id" not in item for item in queue_items if not item["is_me"])
    anonymous_items = client.get(f"/api/rooms/{code}").json()["room"]["free_turn_queue"]["items"]
    assert all("request_id" not in item for item in anonymous_items)
    replay = first.post(
        f"/api/rooms/{code}/free-turn-requests",
        headers=csrf(first) | {"X-Idempotency-Key": "first"},
        json={},
    )
    assert replay.status_code == 200 and replay.json()["replayed"] is True

    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        requests = list(
            db.scalars(
                select(FreeTurnRequest)
                .where(FreeTurnRequest.room_id == room.id)
                .order_by(FreeTurnRequest.requested_at, FreeTurnRequest.id)
            ).all()
        )
        expected_winner = requests[0]
        db.get(Speech, speech_id).status = "completed"
        match_engine._advance(db, room, reason="free_turn_completed")
        current = dict(stage(room))
        current["intermission_deadline_at"] = (now() - timedelta(milliseconds=1)).isoformat()
        snapshot = list(room.template_snapshot)
        snapshot[0] = current
        room.template_snapshot = snapshot
        db.commit()

    asyncio.run(match_engine.process_room(code))
    with SessionLocal() as db:
        room = load_room(db, code)
        requests = list(db.scalars(select(FreeTurnRequest).where(FreeTurnRequest.room_id == room.id)).all())
        selected = next(item for item in requests if item.status == "selected")
        expired = next(item for item in requests if item.status == "expired")
        assert selected.id == expected_winner.id
        assert selected.requested_at <= expired.requested_at or selected.id < expired.id
        assert stage(room)["selected_human_seat"] == selected.seat_key
        assert stage(room)["side"] == "neg"

    selected_browser = first if selected.user_id == first.get("/api/auth/session").json()["user"]["id"] else second
    other_browser = second if selected_browser is first else first
    assert selected_browser.get(f"/api/rooms/{code}").json()["room"]["can_speak"] is True
    assert other_browser.get(f"/api/rooms/{code}").json()["room"]["can_speak"] is False


def test_cancel_cross_room_pause_terminate_and_window_boundary(client, register_user) -> None:
    owner = register_user("round20_control_owner")
    requester = register_user("round20_control_requester")
    other_owner = register_user("round20_other_owner")
    code = _daily_room(owner, client, "Round20 取消暂停终止")
    other_code = create_training_room(other_owner, "Round20 跨房隔离")["code"]
    _claim(requester, code, "neg_1")
    speech_id = _start_free_room(owner, code, [requester])
    requested = requester.post(f"/api/rooms/{code}/free-turn-requests", headers=csrf(requester), json={})
    assert requested.status_code == 200
    request_id = requested.json()["request_id"]
    assert requester.post(
        f"/api/rooms/{other_code}/free-turn-requests/{request_id}/cancel",
        headers=csrf(requester),
        json={},
    ).status_code == 403
    cancelled = requester.post(
        f"/api/rooms/{code}/free-turn-requests/{request_id}/cancel",
        headers=csrf(requester),
        json={},
    )
    assert cancelled.status_code == 200
    replacement = requester.post(f"/api/rooms/{code}/free-turn-requests", headers=csrf(requester), json={})
    assert replacement.status_code == 200 and replacement.json()["request_id"] != request_id

    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        db.get(Speech, speech_id).status = "completed"
        match_engine._advance(db, room, reason="free_turn_completed")
        db.commit()
    paused = owner.post(f"/api/rooms/{code}/control/pause", headers=csrf(owner), json={"reason": "暂停申请窗口"})
    assert paused.status_code == 200
    paused_room = paused.json()["room"]
    assert paused_room["free_turn_queue"]["window_deadline_at"] is None
    assert requester.post(f"/api/rooms/{code}/free-turn-requests", headers=csrf(requester), json={}).status_code == 409
    resumed = owner.post(f"/api/rooms/{code}/control/resume", headers=csrf(owner), json={"reason": "恢复申请窗口"})
    assert resumed.status_code == 200
    assert resumed.json()["room"]["free_turn_queue"]["window_deadline_at"] is not None

    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        current = dict(stage(room))
        current["intermission_deadline_at"] = (now() - timedelta(milliseconds=1)).isoformat()
        snapshot = list(room.template_snapshot)
        snapshot[0] = current
        room.template_snapshot = snapshot
        db.commit()
    assert requester.post(f"/api/rooms/{code}/free-turn-requests", headers=csrf(requester), json={}).status_code == 409
    terminated = owner.post(f"/api/rooms/{code}/control/terminate", headers=csrf(owner), json={"reason": "终止测试"})
    assert terminated.status_code == 200
    with SessionLocal() as db:
        item = db.get(FreeTurnRequest, replacement.json()["request_id"])
        assert item.status == "expired" and item.resolution_reason == "match_terminated"


@pytest.mark.asyncio
async def test_no_request_resolves_to_bounded_ai_fallback_after_restart(register_user, monkeypatch) -> None:
    owner = register_user("round20_fallback_owner")
    code = create_training_room(owner, "Round20 无人举手 AI 接替")["code"]
    speech_id = _start_free_room(owner, code, [])
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        db.get(Speech, speech_id).status = "completed"
        current = begin_intermission(db, room, stage(room))
        current = dict(current)
        current["intermission_deadline_at"] = (now() - timedelta(seconds=1)).isoformat()
        snapshot = list(room.template_snapshot)
        snapshot[0] = current
        room.template_snapshot = snapshot
        db.commit()

    # No process-local queue state is required: a fresh engine pass resolves
    # the persisted deadline and database queue after restart.
    await match_engine.process_room(code)
    with SessionLocal() as db:
        room = load_room(db, code)
        assert stage(room)["side"] == "neg"
        assert stage(room)["force_ai_fallback"] is True

    called: list[str] = []

    async def fake_ai_intent(_db, _room, seat, _current):
        called.append(seat.seat_key)

    monkeypatch.setattr(match_engine, "_free_ai_speech_with_intent", fake_ai_intent)
    await match_engine.process_room(code)
    assert called == ["neg_1"]


@pytest.mark.asyncio
async def test_ai_intent_and_candidate_run_concurrently_and_negative_discards(
    register_user,
    monkeypatch,
) -> None:
    owner = register_user("round20_intent_decline_owner")
    code = create_training_room(owner, "Round20 AI 判断无需重复发言")["code"]
    speech_id = _start_free_room(owner, code, [])
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        db.get(Speech, speech_id).status = "completed"
        current = begin_intermission(db, room, stage(room))
        current = dict(current)
        current["intermission_deadline_at"] = (now() - timedelta(seconds=1)).isoformat()
        snapshot = list(room.template_snapshot)
        snapshot[0] = current
        room.template_snapshot = snapshot
        db.commit()
    await match_engine.process_room(code)

    candidate_started = asyncio.Event()
    candidate_cancelled = asyncio.Event()

    async def decline(payload, _config):
        await candidate_started.wait()
        return AgentSpeechDecision(False, "当前没有新增论点", payload["task_id"])

    async def slow_candidate(*_args, **_kwargs):
        candidate_started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            candidate_cancelled.set()
            raise

    async def no_interrupt(*_args, **_kwargs):
        return True

    async def must_not_speak(*_args, **_kwargs):
        raise AssertionError("negative intent must not publish a speech")

    monkeypatch.setattr("app.services.match_engine.decide_should_speak", decline)
    monkeypatch.setattr("app.services.match_engine.debate_agent.generate", slow_candidate)
    monkeypatch.setattr("app.services.match_engine.interrupt_agent_task", no_interrupt)
    monkeypatch.setattr(match_engine, "_ai_speech", must_not_speak)
    await match_engine.process_room(code)
    assert candidate_cancelled.is_set()
    with SessionLocal() as db:
        room = load_room(db, code)
        current = stage(room)
        assert current["intermission_side"] == "aff"
        events = list(db.scalars(select(MatchEvent.event_type).where(MatchEvent.room_id == room.id)).all())
        assert "free.agent_intent_resolved" in events
        assert "free.agent_candidate_discarded" in events


@pytest.mark.asyncio
async def test_ai_intent_failure_fails_open_and_reuses_concurrent_candidate(register_user, monkeypatch) -> None:
    owner = register_user("round20_intent_failopen_owner")
    code = create_training_room(owner, "Round20 AI 判断故障保持流程")["code"]
    speech_id = _start_free_room(owner, code, [])
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        db.get(Speech, speech_id).status = "completed"
        current = begin_intermission(db, room, stage(room))
        current = dict(current)
        current["intermission_deadline_at"] = (now() - timedelta(seconds=1)).isoformat()
        snapshot = list(room.template_snapshot)
        snapshot[0] = current
        room.template_snapshot = snapshot
        db.commit()
    await match_engine.process_room(code)

    async def broken_decision(*_args, **_kwargs):
        raise RuntimeError("decision service unavailable")

    async def candidate(*_args, **_kwargs):
        return "我方补充一个新的因果反驳，并回应对方刚才论证中的关键前提。"

    published: list[str] = []

    async def fake_ai(_db, _room, _seat, _current, *, free_turn=False, speculative_content=""):
        assert free_turn is True
        published.append(speculative_content)

    monkeypatch.setattr("app.services.match_engine.decide_should_speak", broken_decision)
    monkeypatch.setattr("app.services.match_engine.debate_agent.generate", candidate)
    monkeypatch.setattr(match_engine, "_ai_speech", fake_ai)
    await match_engine.process_room(code)
    assert published == ["我方补充一个新的因果反驳，并回应对方刚才论证中的关键前提。"]


@pytest.mark.asyncio
async def test_ai_speculation_starts_during_three_second_window_and_is_reused_once(
    register_user,
    monkeypatch,
) -> None:
    owner = register_user("round20_early_speculation_owner")
    code = create_training_room(owner, "Round20 三秒窗口提前生成")["code"]
    speech_id = _start_free_room(owner, code, [])
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        db.get(Speech, speech_id).status = "completed"
        begin_intermission(db, room, stage(room))
        db.commit()

    candidate_started = asyncio.Event()
    release_candidate = asyncio.Event()
    calls = {"decision": 0, "candidate": 0}

    async def decide(payload, _config):
        calls["decision"] += 1
        return AgentSpeechDecision(True, "存在新的反驳角度", payload["task_id"])

    async def candidate(*_args, **_kwargs):
        calls["candidate"] += 1
        candidate_started.set()
        await release_candidate.wait()
        return "我方提出一个尚未出现的新反驳，并直接回应对方刚才的因果前提。"

    published: list[str] = []

    async def fake_ai(_db, _room, _seat, _current, *, free_turn=False, speculative_content=""):
        assert free_turn is True
        published.append(speculative_content)

    monkeypatch.setattr("app.services.match_engine.decide_should_speak", decide)
    monkeypatch.setattr("app.services.match_engine.debate_agent.generate", candidate)
    monkeypatch.setattr(match_engine, "_ai_speech", fake_ai)

    match_engine.schedule_free_agent_speculation(code)
    await asyncio.wait_for(candidate_started.wait(), timeout=1)
    assert calls == {"decision": 1, "candidate": 1}
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        current = dict(stage(room))
        assert current["intermission_deadline_at"] > now().isoformat()
        current["intermission_deadline_at"] = (now() - timedelta(milliseconds=1)).isoformat()
        snapshot = list(room.template_snapshot)
        snapshot[0] = current
        room.template_snapshot = snapshot
        db.commit()

    await match_engine.process_room(code)
    release_candidate.set()
    await match_engine.process_room(code)
    assert calls == {"decision": 1, "candidate": 1}
    assert published == ["我方提出一个尚未出现的新反驳，并直接回应对方刚才的因果前提。"]


@pytest.mark.asyncio
async def test_human_request_during_window_cancels_cross_process_speculation(
    client,
    register_user,
    monkeypatch,
) -> None:
    owner = register_user("round20_speculation_cancel_owner")
    requester = register_user("round20_speculation_cancel_requester")
    code = _daily_room(owner, client, "Round20 真人申请中断推测")
    _claim(requester, code, "neg_1")
    speech_id = _start_free_room(owner, code, [requester])
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        db.get(Speech, speech_id).status = "completed"
        begin_intermission(db, room, stage(room))
        db.commit()

    candidate_started = asyncio.Event()
    candidate_cancelled = asyncio.Event()

    async def slow_decision(*_args, **_kwargs):
        await asyncio.sleep(30)

    async def slow_candidate(*_args, **_kwargs):
        candidate_started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            candidate_cancelled.set()
            raise

    async def no_interrupt(*_args, **_kwargs):
        return True

    monkeypatch.setattr("app.services.match_engine.decide_should_speak", slow_decision)
    monkeypatch.setattr("app.services.match_engine.debate_agent.generate", slow_candidate)
    monkeypatch.setattr("app.services.match_engine.interrupt_agent_task", no_interrupt)
    match_engine.schedule_free_agent_speculation(code)
    await asyncio.wait_for(candidate_started.wait(), timeout=1)

    requester_id = requester.get("/api/auth/session").json()["user"]["id"]
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        user = db.get(User, requester_id)
        request_free_turn(db, room, user, provided_idempotency_key="cross-process-cancel")
        db.commit()

    await asyncio.wait_for(candidate_cancelled.wait(), timeout=1)
    descriptor = match_engine._free_agent_speculations.pop(code, None)
    if descriptor and descriptor.task:
        await asyncio.gather(descriptor.task, return_exceptions=True)


def test_ai_request_window_opens_only_after_authoritative_playback_start(client, register_user) -> None:
    owner = register_user("round20_ai_window_owner")
    requester = register_user("round20_ai_window_requester")
    code = _daily_room(owner, client, "Round20 AI 实际开口后才可举手")
    _claim(requester, code, "neg_1")
    human_speech_id = _start_free_room(owner, code, [requester])
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        db.get(Speech, human_speech_id).status = "interrupted"
        ai_speech = Speech(
            match_id=match.id,
            room_id=room.id,
            seat_key="aff_2",
            stage_key="free",
            speaker_type="ai",
            status="synthesizing",
            content="AI 已生成文本但尚未实际播放。",
        )
        db.add(ai_speech)
        db.commit()
        ai_speech_id = ai_speech.id

    preparing = requester.post(f"/api/rooms/{code}/free-turn-requests", headers=csrf(requester), json={})
    assert preparing.status_code == 409 and "实际开始播放" in preparing.json()["detail"]
    with SessionLocal() as db:
        db.get(Speech, ai_speech_id).playback_started_at = now()
        db.commit()
    audible = requester.post(f"/api/rooms/{code}/free-turn-requests", headers=csrf(requester), json={})
    assert audible.status_code == 200
