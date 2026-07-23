from __future__ import annotations

import asyncio
import re
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock, call

import pytest
from app.core.database import SessionLocal
from app.models.entities import JudgeScorecard, Match, MatchEvent, Speech, TranscriptSegment, User
from app.services import match_engine as match_engine_module
from app.services.match_engine import AgentTextPrefetch, match_engine
from app.services.providers import debate_agent, lighttts
from app.services.room_service import append_event, load_room, now
from conftest import csrf
from sqlalchemy import select
from test_platform import create_training_room


def _ready_and_start(owner, code: str, *participants) -> None:
    for browser in (owner, *participants):
        response = browser.post(f"/api/rooms/{code}/ready", headers=csrf(browser), json={"ready": True})
        assert response.status_code == 200, response.text
    response = owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    assert response.status_code == 200, response.text


def _create_two_human_room(owner, participant, topic: str) -> str:
    code = create_training_room(owner, topic)["code"]
    claimed = participant.post(
        f"/api/rooms/{code}/claim-seat",
        headers=csrf(participant),
        json={"seat_key": "neg_1"},
    )
    assert claimed.status_code == 200, claimed.text
    _ready_and_start(owner, code, participant)
    return code


def _event_exists(db, room_id: str, event_type: str) -> bool:
    return bool(
        db.scalar(
            select(MatchEvent.id)
            .where(MatchEvent.room_id == room_id, MatchEvent.event_type == event_type)
            .limit(1)
        )
    )


def test_runtime_source_has_no_path_that_creates_ai_substitute() -> None:
    """The retired seat type must not exist anywhere in runtime Python."""

    app_root = Path(__file__).resolve().parents[1] / "app"
    assignment_patterns = (
        re.compile(r"occupant_type\s*=\s*[\"']ai_substitute[\"']"),
        re.compile(r"occupant_type\s*:\s*[\"']ai_substitute[\"']"),
        re.compile(r"[\"']occupant_type[\"']\s*:\s*[\"']ai_substitute[\"']"),
        re.compile(r"[\"']occupant_type[\"']\s*=\s*[\"']ai_substitute[\"']"),
    )
    offenders: list[str] = []
    for source in app_root.rglob("*.py"):
        text = source.read_text(encoding="utf-8")
        if any(pattern.search(text) for pattern in assignment_patterns):
            offenders.append(str(source.relative_to(app_root)))
    assert offenders == [], f"runtime still creates ai_substitute: {offenders}"
    runtime = "\n".join(source.read_text(encoding="utf-8") for source in app_root.rglob("*.py"))
    assert '"ai_substitute"' not in runtime
    assert "services.seat_restore" not in runtime
    assert "seat-restore-requests" not in runtime


@pytest.mark.asyncio
async def test_fixed_human_disconnect_pauses_at_sixty_without_replacement_and_preserves_text(
    register_user,
    monkeypatch,
) -> None:
    owner = register_user("round45_fixed_disconnect")
    code = create_training_room(owner, "Round45 真人断线不得由 AI 接管")["code"]
    _ready_and_start(owner, code)
    owner_id = owner.get("/api/auth/session").json()["user"]["id"]

    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        seat = next(item for item in room.seats if item.user_id == owner_id)
        room.status = "running"
        room.current_stage_index = 0
        room.template_snapshot = [
            {"key": "aff_case", "name": "正方立论", "kind": "speech", "seat": seat.seat_key, "duration": 120}
        ]
        room.stage_started_at = now()
        room.stage_deadline_at = now() + timedelta(seconds=120)
        seat.connected = False
        seat.disconnected_at = now() - timedelta(seconds=59, milliseconds=500)
        speech = Speech(
            match_id=match.id,
            room_id=room.id,
            seat_key=seat.seat_key,
            stage_key="aff_case",
            speaker_type="human",
            status="speaking",
        )
        db.add(speech)
        db.flush()
        db.add(TranscriptSegment(speech_id=speech.id, start_ms=0, end_ms=900, text="已经确认的开头论点", is_final=True))
        db.commit()
        speech_id = speech.id

    agent = AsyncMock(return_value="绝不应生成的接管发言")
    monkeypatch.setattr(debate_agent, "generate", agent)
    monkeypatch.setattr(lighttts, "synthesize", AsyncMock(return_value="/media/mock/no-takeover.wav"))

    await match_engine.process_room(code)
    with SessionLocal() as db:
        room = load_room(db, code)
        seat = next(item for item in room.seats if item.user_id == owner_id)
        assert room.status == "running"
        assert seat.occupant_type == "human"
        assert not _event_exists(db, room.id, "participant.disconnect_timeout")

        seat.connected = False
        seat.disconnected_at = now() - timedelta(seconds=60, milliseconds=50)
        db.commit()

    await match_engine.process_room(code)
    with SessionLocal() as db:
        room = load_room(db, code)
        seat = next(item for item in room.seats if item.user_id == owner_id)
        speech = db.get(Speech, speech_id)
        assert room.status == "paused"
        assert room.current_stage_index == 0
        assert room.stage_deadline_at is None
        assert seat.occupant_type == "human"
        assert seat.user_id == owner_id
        assert speech.status == "interrupted"
        assert speech.content == "已经确认的开头论点"
        assert _event_exists(db, room.id, "participant.disconnect_timeout")
        assert not _event_exists(db, room.id, "seat.ai_substituted")
        disconnect_events = list(
            db.scalars(
                select(MatchEvent).where(
                    MatchEvent.room_id == room.id,
                    MatchEvent.event_type.in_(("participant.disconnect_timeout", "match.paused")),
                )
            ).all()
        )
        assert {item.event_type for item in disconnect_events} == {
            "participant.disconnect_timeout",
            "match.paused",
        }
        assert all(item.idempotency_key and len(item.idempotency_key) <= 120 for item in disconnect_events)
        assert not db.scalar(
            select(Speech.id).where(
                Speech.room_id == room.id,
                Speech.seat_key == seat.seat_key,
                Speech.speaker_type == "ai",
            )
        )
    agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_any_timed_out_human_pauses_before_a_permanent_ai_can_speak(
    register_user,
    client,
    monkeypatch,
) -> None:
    """A mixed 4v4 may use permanent AI, but cannot keep moving alone."""

    owner = register_user("round45_mixed_room")
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
    assert created.status_code == 200, created.text
    code = created.json()["room"]["code"]
    _ready_and_start(owner, code)
    owner_id = owner.get("/api/auth/session").json()["user"]["id"]
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        human = next(item for item in room.seats if item.user_id == owner_id)
        ai_seat = next(item for item in room.seats if item.occupant_type == "ai")
        room.status = "running"
        room.current_stage_index = 0
        room.template_snapshot = [
            {"key": "ai_case", "name": "反方立论", "kind": "speech", "seat": ai_seat.seat_key, "duration": 90}
        ]
        room.stage_started_at = now()
        room.stage_deadline_at = now() + timedelta(seconds=90)
        human.connected = False
        human.disconnected_at = now() - timedelta(seconds=61)
        db.commit()

    agent = AsyncMock(return_value="永久 AI 也不应在真人超时离线后继续比赛")
    monkeypatch.setattr(debate_agent, "generate", agent)
    monkeypatch.setattr(lighttts, "synthesize", AsyncMock(return_value="/media/mock/no-takeover.wav"))
    await match_engine.process_room(code)

    with SessionLocal() as db:
        room = load_room(db, code)
        human = next(item for item in room.seats if item.user_id == owner_id)
        assert room.status == "paused"
        assert human.occupant_type == "human"
        assert not db.scalar(select(Speech.id).where(Speech.room_id == room.id, Speech.stage_key == "ai_case"))
    agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_tick_level_disconnect_safety_pauses_and_flushes_while_room_provider_task_is_busy(
    register_user,
    client,
    monkeypatch,
) -> None:
    """The 60-second boundary must not wait for a long Agent/MOSS room task."""

    owner = register_user("round45_busy_provider_disconnect")
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
    assert created.status_code == 200, created.text
    code = created.json()["room"]["code"]
    _ready_and_start(owner, code)
    owner_id = owner.get("/api/auth/session").json()["user"]["id"]
    generation = "disconnect-generation"

    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        human = next(item for item in room.seats if item.user_id == owner_id)
        ai_seat = next(item for item in room.seats if item.occupant_type == "ai")
        room.status = "running"
        room.current_stage_index = 0
        room.template_snapshot = [
            {"key": "ai_case", "name": "AI 正常发言", "kind": "speech", "seat": ai_seat.seat_key, "duration": 90}
        ]
        room.stage_started_at = now()
        room.stage_deadline_at = now() + timedelta(seconds=90)
        human.connected = False
        human.disconnected_at = now() - timedelta(seconds=60, milliseconds=50)
        speech = Speech(
            match_id=match.id,
            room_id=room.id,
            seat_key=ai_seat.seat_key,
            stage_key="ai_case",
            speaker_type="ai",
            status="synthesizing",
            stream_generation=generation,
            stream_sample_rate=24_000,
        )
        db.add(speech)
        db.commit()
        speech_id = speech.id

    abort_generation = Mock(return_value=True)
    monkeypatch.setattr(match_engine_module.settings, "webrtc_audio_enabled", True)
    monkeypatch.setattr(match_engine_module.settings, "webrtc_audio_backend", "livekit")
    monkeypatch.setattr(
        match_engine_module.livekit_audio_registry,
        "abort_generation_nowait",
        abort_generation,
    )

    # This predicate is polled from inside the already-running provider task;
    # it must fail closed even before the independent reaper commits the pause.
    assert match_engine._tts_job_cancelled(code, speech_id) is True
    await match_engine._pause_overdue_disconnected_participants()

    with SessionLocal() as db:
        room = load_room(db, code)
        speech = db.get(Speech, speech_id)
        assert room.status == "paused"
        assert speech.status == "interrupted"
        assert speech.stream_generation == ""
        event_types = list(
            db.scalars(
                select(MatchEvent.event_type)
                .where(MatchEvent.room_id == room.id)
                .order_by(MatchEvent.seq)
            ).all()
        )
        assert "participant.disconnect_timeout" in event_types
        assert "match.paused" in event_types
        assert "audio.rtc.interrupt" in event_types
        assert "speech.interrupted" in event_types
    # The safety scan intentionally covers every overdue active room. A full
    # suite may therefore leave another isolated fixture eligible during this
    # assertion; require this room's generation to be revoked exactly once
    # without assuming it is the only room seen by the global scan.
    assert abort_generation.call_args_list.count(call(code, generation)) == 1


@pytest.mark.asyncio
async def test_cancelling_agent_prefetch_interrupts_the_remote_idempotent_task(monkeypatch) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked_generate(*_args, **_kwargs) -> str:
        started.set()
        await release.wait()
        return "不应在暂停后进入缓存的内容"

    interrupt = AsyncMock(return_value=True)
    monkeypatch.setattr(debate_agent, "generate", blocked_generate)
    monkeypatch.setattr(debate_agent, "interrupt", interrupt)
    descriptor = AgentTextPrefetch(
        room_code="654321",
        stage_index=1,
        stage_key="next_ai",
        seat_key="neg_1",
        fingerprint="fingerprint",
        task_id="prefetch-task-id",
        payload={"task_id": "prefetch-task-id"},
        provider_config={"endpoint": "https://agent.invalid/api/debate"},
    )

    task = asyncio.create_task(match_engine._run_agent_prefetch(descriptor))
    await asyncio.wait_for(started.wait(), timeout=0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    interrupt.assert_awaited_once_with(
        "prefetch-task-id",
        provider_config=descriptor.provider_config,
    )


@pytest.mark.asyncio
async def test_multiple_disconnected_humans_can_resume_only_after_everyone_returns(register_user) -> None:
    """A second timeout must not misclassify a pure disconnect as service failure."""

    owner = register_user("round45_multi_disconnect_owner")
    participant = register_user("round45_multi_disconnect_guest")
    code = _create_two_human_room(owner, participant, "Round45 多真人断线恢复")
    owner_id = owner.get("/api/auth/session").json()["user"]["id"]
    participant_id = participant.get("/api/auth/session").json()["user"]["id"]
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.status = "running"
        room.current_stage_index = 0
        room.template_snapshot = [{"key": "hold", "name": "等待真人", "kind": "announcement", "duration": 180}]
        room.stage_started_at = now()
        room.stage_deadline_at = now() + timedelta(seconds=180)
        owner_seat = next(item for item in room.seats if item.user_id == owner_id)
        participant_seat = next(item for item in room.seats if item.user_id == participant_id)
        owner_seat.connected = False
        owner_seat.disconnected_at = now() - timedelta(seconds=61)
        participant_seat.connected = True
        participant_seat.disconnected_at = None
        db.commit()

    await match_engine.process_room(code)
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        assert room.status == "paused"
        assert room.owner_id == owner_id
        assert all(seat.occupant_type == "human" for seat in room.seats)
        owner_seat = next(item for item in room.seats if item.user_id == owner_id)
        owner_seat.connected = True
        owner_seat.disconnected_at = None
        participant_seat = next(item for item in room.seats if item.user_id == participant_id)
        participant_seat.connected = False
        participant_seat.disconnected_at = now() - timedelta(seconds=10)
        db.commit()

    blocked = owner.post(
        f"/api/rooms/{code}/control/resume",
        headers=csrf(owner),
        json={"reason": "房主已返回"},
    )
    assert blocked.status_code == 409
    assert "尚未重新连接" in blocked.json()["detail"]

    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        participant_seat = next(item for item in room.seats if item.user_id == participant_id)
        participant_seat.connected = True
        participant_seat.disconnected_at = None
        db.commit()

    resumed = owner.post(
        f"/api/rooms/{code}/control/resume",
        headers=csrf(owner),
        json={"reason": "全部真人均已返回"},
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["room"]["status"] == "running"
    assert all(item["occupant_type"] == "human" for item in resumed.json()["room"]["seats"])


def test_timeout_context_survives_late_provider_failure_and_never_retries_offline(
    register_user, monkeypatch
) -> None:
    """A late provider.failed event must not erase the human disconnect gate.

    Engine/provider cancellation is asynchronous.  If the provider writes its
    failure event after the 60-second timeout, the control endpoint must still
    require every human to reconnect before retrying; only the retry-vs-resume
    action may change.  A reconnect alone must leave the room paused.
    """

    owner = register_user("round45_timeout_provider_race_owner")
    participant = register_user("round45_timeout_provider_race_guest")
    code = _create_two_human_room(owner, participant, "Round45 断线与服务失败事件乱序")
    participant_id = participant.get("/api/auth/session").json()["user"]["id"]
    async def voice_ready() -> None:
        return None

    monkeypatch.setattr("app.api.rooms._require_realtime_voice_ready_for_start", voice_ready)

    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        seat = next(item for item in room.seats if item.user_id == participant_id)
        for item in room.seats:
            if item.occupant_type == "human":
                item.connected = True
                item.disconnected_at = None
        seat.connected = False
        seat.disconnected_at = now() - timedelta(seconds=61)
        room.status = "paused"
        room.current_stage_index = 0
        room.template_snapshot = [{"key": "hold", "name": "等待真人", "kind": "announcement", "duration": 30}]
        room.stage_started_at = now() - timedelta(seconds=5)
        room.paused_remaining_seconds = 30
        room.failure_reason = "服务生成失败"
        append_event(
            db,
            room,
            "participant.disconnect_timeout",
            {
                "seat_key": seat.seat_key,
                "grace_seconds": 60,
                "prior_requires_retry": False,
            },
        )
        append_event(db, room, "provider.failed", {"provider": "moss", "code": "late_failure"})
        db.commit()

    blocked_offline = owner.post(
        f"/api/rooms/{code}/control/retry",
        headers=csrf(owner),
        json={"reason": "服务已恢复"},
    )
    assert blocked_offline.status_code == 409
    assert "尚未重新连接" in blocked_offline.json()["detail"]
    with SessionLocal() as db:
        room = load_room(db, code)
        seat = next(item for item in room.seats if item.user_id == participant_id)
        seat.connected = True
        seat.disconnected_at = None
        db.commit()

    # Returning to the room does not auto-resume and does not clear the
    # timeout boundary; the owner still has to issue the explicit retry.
    still_paused = owner.get(f"/api/rooms/{code}")
    assert still_paused.status_code == 200
    assert still_paused.json()["room"]["status"] == "paused"
    retried = owner.post(
        f"/api/rooms/{code}/control/retry",
        headers=csrf(owner),
        json={"reason": "全部真人已返回，重试服务步骤"},
    )
    assert retried.status_code == 200, retried.text
    assert retried.json()["room"]["status"] == "running"
    with SessionLocal() as db:
        room = load_room(db, code)
        seat = next(item for item in room.seats if item.user_id == participant_id)
        assert seat.occupant_type == "human" and seat.user_id == participant_id


@pytest.mark.asyncio
async def test_disabled_account_pauses_safely_without_owner_or_seat_replacement(register_user) -> None:
    """Administrative deactivation is an immediate safety exception to grace."""

    owner = register_user("round45_disabled_owner")
    participant = register_user("round45_disabled_guest")
    code = _create_two_human_room(owner, participant, "Round45 账号停用不得 AI 接管")
    participant_id = participant.get("/api/auth/session").json()["user"]["id"]
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.status = "running"
        room.current_stage_index = 0
        room.template_snapshot = [{"key": "hold", "name": "比赛进行中", "kind": "announcement", "duration": 180}]
        room.stage_started_at = now()
        room.stage_deadline_at = now() + timedelta(seconds=180)
        for seat in room.seats:
            if seat.occupant_type == "human":
                seat.connected = True
                seat.disconnected_at = None
        disabled = db.get(User, participant_id)
        disabled.is_active = False
        original_owner_id = room.owner_id
        db.commit()

    await match_engine.process_room(code)
    with SessionLocal() as db:
        room = load_room(db, code)
        seat = next(item for item in room.seats if item.user_id == participant_id)
        assert room.status == "paused"
        assert room.owner_id == original_owner_id
        assert seat.occupant_type == "human"
        assert seat.connected is False
        assert _event_exists(db, room.id, "participant.disconnect_timeout")
        assert not _event_exists(db, room.id, "seat.ai_substituted")

    blocked = owner.post(
        f"/api/rooms/{code}/control/resume",
        headers=csrf(owner),
        json={"reason": "停用账号不得让比赛恢复"},
    )
    assert blocked.status_code == 409
    assert "账号不可用" in blocked.json()["detail"]
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        participant_row = db.get(User, participant_id)
        participant_row.is_active = True
        seat = next(item for item in room.seats if item.user_id == participant_id)
        seat.connected = True
        seat.disconnected_at = None
        db.commit()
    resumed = owner.post(
        f"/api/rooms/{code}/control/resume",
        headers=csrf(owner),
        json={"reason": "账号恢复且真人已经重新连接"},
    )
    assert resumed.status_code == 200, resumed.text


@pytest.mark.asyncio
async def test_disconnect_during_preparing_resumes_preparation_instead_of_finishing(register_user) -> None:
    """A timeout before stage zero must return to cue preparation on resume."""

    owner = register_user("round45_preparing_disconnect")
    code = create_training_room(owner, "Round45 准备阶段断线恢复")['code']
    _ready_and_start(owner, code)
    owner_id = owner.get("/api/auth/session").json()["user"]["id"]
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.status = "preparing"
        room.current_stage_index = -1
        room.stage_started_at = None
        room.stage_deadline_at = None
        seat = next(item for item in room.seats if item.user_id == owner_id)
        seat.connected = False
        seat.disconnected_at = now() - timedelta(seconds=61)
        db.commit()

    await match_engine.process_room(code)
    with SessionLocal() as db:
        room = load_room(db, code)
        assert room.status == "paused"
        assert room.current_stage_index == -1
        seat = next(item for item in room.seats if item.user_id == owner_id)
        seat.connected = True
        seat.disconnected_at = None
        db.commit()

    resumed = owner.post(
        f"/api/rooms/{code}/control/resume",
        headers=csrf(owner),
        json={"reason": "真人已返回，继续准备比赛"},
    )
    assert resumed.status_code == 200, resumed.text
    resumed_room = resumed.json()["room"]
    assert resumed_room["status"] == "preparing"
    assert resumed_room["current_stage_index"] == -1


@pytest.mark.asyncio
async def test_disconnect_pause_cannot_be_bypassed_by_skip(register_user) -> None:
    """Skipping must not resume a room while a timed-out human is absent."""

    owner = register_user("round45_skip_disconnect_pause")
    code = create_training_room(owner, "Round45 断线暂停禁止跳过")['code']
    _ready_and_start(owner, code)
    owner_id = owner.get("/api/auth/session").json()["user"]["id"]
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.status = "running"
        room.current_stage_index = 0
        room.template_snapshot = [{"key": "hold", "name": "等待真人", "kind": "announcement", "duration": 180}]
        room.stage_started_at = now()
        room.stage_deadline_at = now() + timedelta(seconds=180)
        seat = next(item for item in room.seats if item.user_id == owner_id)
        seat.connected = False
        seat.disconnected_at = now() - timedelta(seconds=61)
        db.commit()

    await match_engine.process_room(code)
    skipped = owner.post(
        f"/api/rooms/{code}/control/skip",
        headers=csrf(owner),
        json={"reason": "不应绕过真人断线暂停"},
    )
    assert skipped.status_code == 409
    assert "真人断线期间不能跳过阶段" in skipped.json()["detail"]
    with SessionLocal() as db:
        room = load_room(db, code)
        assert room.status == "paused"
        assert room.current_stage_index == 0


def test_started_participant_cannot_request_ai_takeover(register_user) -> None:
    owner = register_user("round45_abandon_removed")
    code = create_training_room(owner, "Round45 主动退出也不得由 AI 接管")["code"]
    _ready_and_start(owner, code)
    owner_id = owner.get("/api/auth/session").json()["user"]["id"]

    response = owner.post(
        f"/api/rooms/{code}/abandon-seat",
        headers=csrf(owner) | {"X-Idempotency-Key": "round45-no-abandon"},
        json={},
    )
    assert response.status_code == 410
    assert "取消 AI 自动接管" in response.json()["detail"]
    with SessionLocal() as db:
        room = load_room(db, code)
        seat = next(item for item in room.seats if item.user_id == owner_id)
        assert seat.occupant_type == "human"
        assert not _event_exists(db, room.id, "seat.abandoned")
        assert not _event_exists(db, room.id, "seat.ai_substituted")


@pytest.mark.asyncio
async def test_four_v_four_free_debate_disconnect_freezes_turn_and_redacts_grace_for_public_viewers(
    client,
    register_user,
) -> None:
    owner = register_user("round45_four_v_four_free")
    competition = client.get("/api/competitions/daily-4v4").json()["competition"]
    room_response = owner.post(
        "/api/rooms",
        headers=csrf(owner),
        json={
            "competition_slug": "daily-4v4",
            "topic_id": competition["topics"][0]["id"],
            "seat_key": "aff_1",
            "visibility": "public",
        },
    )
    assert room_response.status_code == 200, room_response.text
    code = room_response.json()["room"]["code"]
    _ready_and_start(owner, code)
    owner_id = owner.get("/api/auth/session").json()["user"]["id"]
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        seat = next(item for item in room.seats if item.user_id == owner_id)
        room.status = "running"
        room.current_stage_index = 0
        room.template_snapshot = [
            {
                "key": "free",
                "name": "四对四自由辩论",
                "kind": "free",
                "side": "aff",
                "duration": 120,
                "turn_duration": 30,
                "turn_started_at": (now() - timedelta(seconds=10)).isoformat(),
            }
        ]
        room.stage_started_at = now()
        room.stage_deadline_at = now() + timedelta(seconds=120)
        seat.connected = False
        seat.disconnected_at = now() - timedelta(seconds=61)
        db.commit()

    owner_projection = owner.get(f"/api/rooms/{code}")
    assert owner_projection.status_code == 200
    grace = owner_projection.json()["room"]["disconnect_grace"]
    assert grace["will_pause"] is True
    assert grace["pending"][0]["remaining_seconds"] == 0
    public_projection = client.get(f"/api/rooms/{code}")
    assert public_projection.status_code == 200
    public_grace = public_projection.json()["room"]["disconnect_grace"]
    assert public_grace["pending_count"] == 1
    assert "display_name" not in public_grace

    await match_engine.process_room(code)
    with SessionLocal() as db:
        room = load_room(db, code)
        seat = next(item for item in room.seats if item.user_id == owner_id)
        current = room.template_snapshot[0]
        assert room.status == "paused"
        assert seat.occupant_type == "human"
        assert room.paused_remaining_seconds is not None
        assert current["paused_turn_remaining_seconds"] >= 1
        assert _event_exists(db, room.id, "participant.disconnect_timeout")
        assert not _event_exists(db, room.id, "seat.ai_substituted")


@pytest.mark.asyncio
async def test_judging_disconnect_pauses_and_interrupts_judge_without_ai_takeover(register_user) -> None:
    owner = register_user("round45_judging_disconnect")
    code = create_training_room(owner, "Round45 判分阶段真人断线安全暂停")["code"]
    _ready_and_start(owner, code)
    owner_id = owner.get("/api/auth/session").json()["user"]["id"]
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        room.status = "judging"
        room.current_stage_index = 0
        room.template_snapshot = [{"key": "judge", "name": "自动裁判", "kind": "judging", "duration": 60}]
        room.stage_started_at = now()
        room.stage_deadline_at = None
        match.status = "judging"
        db.add(JudgeScorecard(match_id=match.id, status="running", task_id="round45-judge-task"))
        seat = next(item for item in room.seats if item.user_id == owner_id)
        seat.connected = False
        seat.disconnected_at = now() - timedelta(seconds=61)
        db.commit()

    await match_engine.process_room(code)
    with SessionLocal() as db:
        room = load_room(db, code)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        scorecard = db.scalar(select(JudgeScorecard).where(JudgeScorecard.match_id == match.id))
        seat = next(item for item in room.seats if item.user_id == owner_id)
        assert room.status == "paused"
        assert match.status == "judging"
        assert scorecard.status == "interrupted"
        assert seat.occupant_type == "human"
        assert _event_exists(db, room.id, "participant.disconnect_timeout")
        assert not _event_exists(db, room.id, "seat.ai_substituted")
