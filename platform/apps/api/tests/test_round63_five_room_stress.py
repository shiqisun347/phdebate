from __future__ import annotations

import asyncio
from collections import defaultdict
from contextlib import ExitStack
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from app.api import rooms as rooms_api
from app.core.database import SessionLocal
from app.models.entities import JudgeScorecard, Match, MatchEvent, Room, Speech
from app.services.match_engine import match_engine
from app.services.providers import (
    MossTTSRealtimeProvider,
    ProviderError,
    debate_agent,
    judge_provider,
    lighttts,
)
from app.services.realtime import SPECTATOR_LIMIT
from app.services.room_service import load_room, now
from conftest import csrf
from fastapi import WebSocketDisconnect
from sqlalchemy import select
from test_platform import create_training_room
from test_providers import (
    FakePersistentMossConnection,
    FakePersistentMossWebSocket,
    configure_ws_moss,
    moss_ready_handler,
    normal_moss_ws_script,
)


def _ready_and_start(owner, code: str) -> None:
    ready = owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})
    assert ready.status_code == 200, ready.text
    started = owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    assert started.status_code == 200, started.text


def _active_codes(codes: list[str]) -> list[str]:
    with SessionLocal() as db:
        return [
            code
            for code in codes
            if load_room(db, code).status in {"preparing", "running", "judging"}
        ]


def test_five_rooms_share_one_room_and_audience_capacity(
    client,
    register_user,
    monkeypatch,
) -> None:
    """The documented five-room and five-viewer limits are truly global."""

    with SessionLocal() as db:
        for room in db.scalars(
            select(Room).where(Room.status.in_(rooms_api.ACTIVE_PARTICIPANT_STATUSES))
        ).all():
            room.status = "terminated"
            room.completed_at = now()
        db.commit()
    monkeypatch.setattr(rooms_api, "ROOM_CAPACITY_ENFORCED", True)

    owners = [register_user(f"round63_capacity_owner_{index}") for index in range(6)]
    rooms = [
        create_training_room(owner, f"Round63 全局容量房间 {index + 1}")
        for index, owner in enumerate(owners[:5])
    ]
    codes = [room["code"] for room in rooms]

    blocked = owners[5].post(
        "/api/rooms",
        headers=csrf(owners[5]),
        json={
            "competition_slug": "training-1v1",
            "custom_topic": "Round63 第六个房间应被拒绝",
            "seat_key": "aff_1",
            "visibility": "public",
        },
    )
    assert blocked.status_code == 409
    assert "达到 5 场上限" in blocked.json()["detail"]

    overflow_viewer = register_user("round63_spectator_overflow")
    first_context = client.websocket_connect(f"/ws/rooms/{codes[0]}")
    first_socket = first_context.__enter__()
    try:
        assert first_socket.receive_json()["room"]["code"] == codes[0]
        with ExitStack() as stack:
            other_sockets = [
                stack.enter_context(client.websocket_connect(f"/ws/rooms/{code}"))
                for code in codes[1:SPECTATOR_LIMIT]
            ]
            assert [socket.receive_json()["room"]["code"] for socket in other_sockets] == codes[1:]

            with pytest.raises(WebSocketDisconnect) as overflow:
                with overflow_viewer.websocket_connect(f"/ws/rooms/{codes[0]}") as socket:
                    socket.receive_json()
            assert overflow.value.code == 4429

            # Room owners and participants remain exempt from the spectator
            # quota so a full audience can never lock debaters out.
            with owners[0].websocket_connect(f"/ws/rooms/{codes[0]}") as owner_socket:
                assert owner_socket.receive_json()["room"]["my_seat"] == "aff_1"

            first_context.__exit__(None, None, None)
            first_socket = None
            with overflow_viewer.websocket_connect(f"/ws/rooms/{codes[0]}") as released:
                assert released.receive_json()["room"]["code"] == codes[0]
    finally:
        if first_socket is not None:
            first_context.__exit__(None, None, None)

    cancelled = owners[0].post(f"/api/rooms/{codes[0]}/cancel", headers=csrf(owners[0]), json={})
    assert cancelled.status_code == 200
    replacement = create_training_room(owners[5], "Round63 容量释放后的新房间")
    assert replacement["status"] == "lobby"

    for index, code in enumerate(codes[1:], start=1):
        owners[index].post(f"/api/rooms/{code}/cancel", headers=csrf(owners[index]), json={})
    owners[5].post(f"/api/rooms/{replacement['code']}/cancel", headers=csrf(owners[5]), json={})


@pytest.mark.asyncio
async def test_five_moss_rooms_wait_on_one_real_provider_slot_without_rejection(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Five rooms queue at the actual MOSS provider admission boundary."""

    configure_ws_moss(
        monkeypatch,
        tmp_path,
        endpoints=["https://round63-moss.internal:8443/api"],
    )
    connections: list[FakePersistentMossWebSocket] = []
    start_order: list[str] = []
    active = 0
    peak_active = 0

    async def script(websocket: FakePersistentMossWebSocket, payload: dict[str, object]) -> None:
        nonlocal active, peak_active
        event_type = payload["type"]
        if event_type == "start":
            active += 1
            peak_active = max(peak_active, active)
            start_order.append(str(payload["session_id"]).split("-speech-", 1)[0])
        await normal_moss_ws_script(websocket, payload)
        if event_type == "abort":
            active -= 1

    def connect(_url: str, **_kwargs):
        websocket = FakePersistentMossWebSocket(script)
        connections.append(websocket)
        return FakePersistentMossConnection(websocket)

    provider = MossTTSRealtimeProvider(
        httpx.MockTransport(moss_ready_handler),
        ws_connect=connect,
    )
    sessions = [
        provider.open_incremental_session(
            room_code=f"round63-moss-room-{index}",
            speech_id=f"speech-{index}",
            voice="debate_voice_1",
            on_stream_event=lambda _event: asyncio.sleep(0),
        )
        for index in range(5)
    ]

    await asyncio.wait_for(sessions[0].prepare(), timeout=1)
    waiters = [asyncio.create_task(session.prepare()) for session in sessions[1:]]
    await asyncio.sleep(0.05)
    assert all(not waiter.done() for waiter in waiters)
    assert peak_active == 1

    for index, session in enumerate(sessions):
        await asyncio.wait_for(session.abort(reason="round63_queue_release"), timeout=1)
        if index < len(waiters):
            await asyncio.wait_for(waiters[index], timeout=1)

    assert start_order == [f"round63-moss-room-{index}" for index in range(5)]
    assert peak_active == 1 and active == 0
    assert provider._endpoint_semaphores[0][1].locked() is False
    assert sum(
        payload["type"] == "start"
        for websocket in connections
        for payload in websocket.sent
    ) == 5


@pytest.mark.asyncio
async def test_five_agent_rooms_share_bounded_tts_and_recover_one_failure_without_leakage(
    register_user,
    monkeypatch,
) -> None:
    """Queue pressure and one retry must not contaminate another room."""

    owners = [register_user(f"round63_agent_owner_{index}") for index in range(5)]
    topics = [f"Round63 多智能体并行辩题 {index + 1}" for index in range(5)]
    codes: list[str] = []
    for owner, topic in zip(owners, topics, strict=True):
        code = create_training_room(owner, topic)["code"]
        codes.append(code)
        _ready_and_start(owner, code)
        with SessionLocal() as db:
            room = load_room(db, code, lock=True)
            match = db.scalar(select(Match).where(Match.room_id == room.id))
            assert match is not None
            room.template_snapshot = [
                {
                    "key": "agent_case",
                    "name": "智能体立论",
                    "kind": "speech",
                    "seat": "neg_1",
                    "duration": 60,
                },
                {"key": "judge", "name": "自动评审", "kind": "judging", "duration": 30},
            ]
            room.status = "running"
            room.current_stage_index = 0
            room.stage_started_at = now()
            room.stage_deadline_at = now() + timedelta(seconds=60)
            match.status = "running"
            for seat in room.seats:
                seat.occupant_type = "ai"
                seat.user_id = None
                seat.connected = True
                seat.display_name = f"Agent-{code}-{seat.seat_key}"
            db.commit()

    active_agents = 0
    peak_agents = 0
    active_tts = 0
    peak_tts = 0
    agent_lock = asyncio.Lock()
    tts_lock = asyncio.Lock()
    tts_gate = asyncio.Semaphore(1)
    attempts: defaultdict[str, int] = defaultdict(int)
    synthesized_rooms: list[str] = []
    failing_code = codes[2]

    async def generated(payload, **_kwargs):
        nonlocal active_agents, peak_agents
        code = payload["room_code"]
        attempts[code] += 1
        async with agent_lock:
            active_agents += 1
            peak_agents = max(peak_agents, active_agents)
        await asyncio.sleep(0.015)
        async with agent_lock:
            active_agents -= 1
        if code == failing_code and attempts[code] == 1:
            raise ProviderError("Round63 模拟 Agent 短暂失败", code="agent_timeout", retryable=True)
        return f"房间 {code} 仅讨论《{payload['debate_topic']}》，这是独立的智能体立论。"

    async def synthesized(*_args, room_code: str, **_kwargs):
        nonlocal active_tts, peak_tts
        async with tts_gate:
            async with tts_lock:
                active_tts += 1
                peak_tts = max(peak_tts, active_tts)
                synthesized_rooms.append(room_code)
            await asyncio.sleep(0.012)
            async with tts_lock:
                active_tts -= 1
        return ""

    async def judged(topic, speeches, **_kwargs):
        assert speeches
        assert all(topic in speech["content"] for speech in speeches)
        index = topics.index(topic)
        return {
            "winner": "aff" if index % 2 == 0 else "neg",
            "affirmative_score": 88 if index % 2 == 0 else 82,
            "negative_score": 82 if index % 2 == 0 else 88,
            "individual_scores": {},
            "reasoning": f"《{topic}》的隔离评审结果。",
        }

    monkeypatch.setattr(debate_agent, "generate", generated)
    monkeypatch.setattr(lighttts, "synthesize", synthesized)
    monkeypatch.setattr(judge_provider, "judge", judged)

    for _ in range(5):
        active = _active_codes(codes)
        if not active:
            break
        await asyncio.gather(*(match_engine._process_room_locked(code) for code in active))

    with SessionLocal() as db:
        assert load_room(db, failing_code).status == "paused"
        assert all(load_room(db, code).status == "completed" for code in codes if code != failing_code)

    retried = owners[2].post(
        f"/api/rooms/{failing_code}/control/retry",
        headers=csrf(owners[2]) | {"X-Idempotency-Key": "round63-agent-retry"},
        json={"reason": "Agent 服务恢复"},
    )
    assert retried.status_code == 200, retried.text
    for _ in range(4):
        if not _active_codes([failing_code]):
            break
        await match_engine._process_room_locked(failing_code)

    assert peak_agents >= 2
    assert peak_tts == 1
    assert set(synthesized_rooms) == set(codes)
    assert attempts[failing_code] == 2
    assert all(attempts[code] == 1 for code in codes if code != failing_code)

    with SessionLocal() as db:
        for index, (code, topic) in enumerate(zip(codes, topics, strict=True)):
            room = load_room(db, code)
            match = db.scalar(select(Match).where(Match.room_id == room.id))
            scorecard = db.scalar(select(JudgeScorecard).where(JudgeScorecard.match_id == match.id))
            speeches = list(db.scalars(select(Speech).where(Speech.room_id == room.id)).all())
            events = list(
                db.scalars(
                    select(MatchEvent)
                    .where(MatchEvent.room_id == room.id)
                    .order_by(MatchEvent.seq)
                ).all()
            )
            assert room.status == "completed" and match and match.status == "completed"
            assert match.winner == ("aff" if index % 2 == 0 else "neg")
            assert scorecard and scorecard.reasoning == f"《{topic}》的隔离评审结果。"
            completed_speeches = [speech for speech in speeches if speech.status == "completed"]
            assert len(completed_speeches) == 1
            assert len(speeches) == (2 if code == failing_code else 1)
            if code == failing_code:
                assert {speech.status for speech in speeches} == {"failed_retried", "completed"}
            assert code in completed_speeches[0].content and topic in completed_speeches[0].content
            assert [event.seq for event in events] == list(range(1, room.seq + 1))
            assert not any(event.event_type == "seat.ai_substituted" for event in events)


@pytest.mark.asyncio
async def test_disconnect_and_owner_repairs_affect_only_the_target_room_across_five_rooms(
    register_user,
) -> None:
    """A 60-second disconnect freezes one human seat and no other room."""

    owners = [register_user(f"round63_human_owner_{index}") for index in range(5)]
    connected_teammate = register_user("round63_connected_teammate")
    codes: list[str] = []
    for index, owner in enumerate(owners):
        code = create_training_room(owner, f"Round63 真人控制隔离 {index + 1}")["code"]
        codes.append(code)
        if index == 0:
            claimed = connected_teammate.post(
                f"/api/rooms/{code}/claim-seat",
                headers=csrf(connected_teammate),
                json={"seat_key": "neg_1"},
            )
            assert claimed.status_code == 200, claimed.text
            ready = connected_teammate.post(
                f"/api/rooms/{code}/ready",
                headers=csrf(connected_teammate),
                json={"ready": True},
            )
            assert ready.status_code == 200, ready.text
        _ready_and_start(owner, code)
        with SessionLocal() as db:
            room = load_room(db, code, lock=True)
            room.template_snapshot = [
                {
                    "key": "human_case",
                    "name": "真人立论",
                    "kind": "speech",
                    "seat": "aff_1",
                    "duration": 90,
                    "awaiting_human_start": True,
                },
                {
                    "key": "agent_case",
                    "name": "智能体回应",
                    "kind": "speech",
                    "seat": "neg_1",
                    "duration": 90,
                },
            ]
            room.status = "running"
            room.current_stage_index = 0
            room.stage_started_at = None
            room.stage_deadline_at = None
            db.commit()

    with SessionLocal() as db:
        room = load_room(db, codes[0], lock=True)
        owner_seat = next(seat for seat in room.seats if seat.seat_key == "aff_1")
        teammate_seat = next(seat for seat in room.seats if seat.seat_key == "neg_1")
        owner_seat.connected = False
        owner_seat.disconnected_at = now() - timedelta(seconds=61)
        teammate_seat.connected = True
        teammate_seat.disconnected_at = None
        before_sequences = {code: load_room(db, code).seq for code in codes[1:]}
        db.commit()

    await asyncio.gather(*(match_engine._process_room_locked(code) for code in codes))

    with SessionLocal() as db:
        disconnected_room = load_room(db, codes[0])
        owner_seat = next(seat for seat in disconnected_room.seats if seat.seat_key == "aff_1")
        teammate_seat = next(seat for seat in disconnected_room.seats if seat.seat_key == "neg_1")
        assert disconnected_room.status == "paused"
        assert disconnected_room.owner_id == owner_seat.user_id
        assert owner_seat.occupant_type == "human" and owner_seat.user_id
        assert teammate_seat.occupant_type == "human" and teammate_seat.connected
        disconnect_events = list(
            db.scalars(select(MatchEvent).where(MatchEvent.room_id == disconnected_room.id)).all()
        )
        assert sum(event.event_type == "participant.disconnect_timeout" for event in disconnect_events) == 1
        assert not any(event.event_type == "seat.ai_substituted" for event in disconnect_events)
        for code in codes[1:]:
            room = load_room(db, code)
            assert room.status == "running" and room.current_stage_index == 0
            assert room.seq == before_sequences[code]

    teammate_control = connected_teammate.post(
        f"/api/rooms/{codes[0]}/control/resume",
        headers=csrf(connected_teammate),
        json={"reason": "连接中的队友不能自动取得房主权"},
    )
    assert teammate_control.status_code == 403

    blocked_resume = owners[0].post(
        f"/api/rooms/{codes[0]}/control/resume",
        headers=csrf(owners[0]),
        json={"reason": "辩手仍未连接"},
    )
    assert blocked_resume.status_code == 409
    with SessionLocal() as db:
        room = load_room(db, codes[0], lock=True)
        owner_seat = next(seat for seat in room.seats if seat.seat_key == "aff_1")
        owner_seat.connected = True
        owner_seat.disconnected_at = None
        db.commit()
    resumed = owners[0].post(
        f"/api/rooms/{codes[0]}/control/resume",
        headers=csrf(owners[0]),
        json={"reason": "真人已经重新连接"},
    )
    assert resumed.status_code == 200 and resumed.json()["room"]["status"] == "running"

    paused = owners[1].post(
        f"/api/rooms/{codes[1]}/control/pause",
        headers=csrf(owners[1]),
        json={"reason": "现场短暂停顿"},
    )
    assert paused.status_code == 200 and paused.json()["room"]["status"] == "paused"
    continued = owners[1].post(
        f"/api/rooms/{codes[1]}/control/resume",
        headers=csrf(owners[1]),
        json={"reason": "现场恢复"},
    )
    assert continued.status_code == 200 and continued.json()["room"]["status"] == "running"

    skipped = owners[2].post(
        f"/api/rooms/{codes[2]}/control/skip",
        headers=csrf(owners[2]),
        json={"reason": "确认跳过当前阶段"},
    )
    assert skipped.status_code == 200
    assert skipped.json()["room"]["current_stage_index"] == 1

    terminated = owners[3].post(
        f"/api/rooms/{codes[3]}/control/terminate",
        headers=csrf(owners[3]),
        json={"reason": "确认终止本场"},
    )
    assert terminated.status_code == 200 and terminated.json()["room"]["status"] == "terminated"

    with SessionLocal() as db:
        expected = ["running", "running", "running", "terminated", "running"]
        for code, status in zip(codes, expected, strict=True):
            room = load_room(db, code)
            match = db.scalar(select(Match).where(Match.room_id == room.id))
            assert room.status == status and match and match.status == status
            events = list(db.scalars(select(MatchEvent).where(MatchEvent.room_id == room.id)).all())
            assert not any(event.event_type == "seat.ai_substituted" for event in events)
