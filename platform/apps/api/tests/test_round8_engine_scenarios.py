from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

from app.core.database import SessionLocal
from app.models.entities import JudgeScorecard, Match, MatchEvent, Speech
from app.services.match_engine import match_engine
from app.services.providers import ProviderError, debate_agent, judge_provider, lighttts
from app.services.room_service import load_room, now
from conftest import csrf
from sqlalchemy import func, select
from test_platform import create_training_room


def _start(owner, topic: str) -> str:
    code = create_training_room(owner, topic)["code"]
    assert owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True}).status_code == 200
    assert owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={}).status_code == 200
    return code


def _configure_ai_match(code: str) -> None:
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.template_snapshot = [
            {"key": "ai_case", "name": "AI 立论", "kind": "speech", "seat": "neg_1", "duration": 60},
            {"key": "judge", "name": "裁判", "kind": "judging", "duration": 10},
        ]
        room.current_stage_index = 0
        room.status = "running"
        room.stage_started_at = now()
        room.stage_deadline_at = now() + timedelta(seconds=60)
        seat = next(item for item in room.seats if item.seat_key == "neg_1")
        seat.occupant_type = "ai"
        seat.user_id = None
        seat.display_name = f"AI-{code}"
        db.commit()


async def test_parallel_agent_failure_recovery_is_room_scoped_and_settles_once(
    register_user,
    monkeypatch,
) -> None:
    """A broken room must remain repairable without delaying healthy matches.

    This is the production-shaped failure mode: three rooms ask the Agent at
    once, one times out, the other two finish, and the owner retries the exact
    failed stage.  The assertions include append-only failure history and
    exactly-once result settlement, not only the final room status.
    """

    topics = ["Round8 正常并发甲", "Round8 Agent 超时", "Round8 正常并发乙"]
    owners = [register_user(f"round8_parallel_{index}") for index in range(3)]
    codes = [_start(owner, topic) for owner, topic in zip(owners, topics, strict=True)]
    for code in codes:
        _configure_ai_match(code)

    attempts: dict[str, int] = {}

    async def generated(payload, **_kwargs):
        topic = payload["debate_topic"]
        attempts[topic] = attempts.get(topic, 0) + 1
        await asyncio.sleep(0.01)
        if topic == topics[1] and attempts[topic] == 1:
            raise ProviderError("Agent 上游超时，可由房主重试。", code="agent_timeout", retryable=True)
        return f"{topic} 的独立论证，房间号为 {payload['room_code']}。"

    async def judged(topic, _speeches, **_kwargs):
        return {
            "winner": "neg",
            "affirmative_score": 82,
            "negative_score": 88,
            "individual_scores": {},
            "reasoning": f"{topic} 的独立裁判结论。",
        }

    monkeypatch.setattr(debate_agent, "generate", generated)
    # A URL without a local WAV intentionally exercises the non-audio engine
    # path: the speech is committed and advanced without touching playback.
    monkeypatch.setattr(lighttts, "synthesize", AsyncMock(return_value="/media/mock/non-audio.wav"))
    monkeypatch.setattr(judge_provider, "judge", judged)

    await asyncio.gather(*(match_engine.process_room(code) for code in codes))
    with SessionLocal() as db:
        assert load_room(db, codes[0]).status == "judging"
        failed_room = load_room(db, codes[1])
        assert failed_room.status == "paused" and failed_room.paused_remaining_seconds > 0
        assert load_room(db, codes[2]).status == "judging"

    await asyncio.gather(match_engine.process_room(codes[0]), match_engine.process_room(codes[2]))
    retry_headers = csrf(owners[1]) | {"X-Idempotency-Key": "round8-agent-retry-once"}
    retried = owners[1].post(
        f"/api/rooms/{codes[1]}/control/retry",
        headers=retry_headers,
        json={"reason": "Agent 服务已恢复"},
    )
    replayed = owners[1].post(
        f"/api/rooms/{codes[1]}/control/retry",
        headers=retry_headers,
        json={"reason": "Agent 服务已恢复"},
    )
    assert retried.status_code == 200
    assert replayed.status_code == 200 and replayed.json()["replayed"] is True
    await match_engine.process_room(codes[1])
    await match_engine.process_room(codes[1])

    with SessionLocal() as db:
        for code, topic in zip(codes, topics, strict=True):
            room = load_room(db, code)
            match = db.scalar(select(Match).where(Match.room_id == room.id))
            scorecard = db.scalar(select(JudgeScorecard).where(JudgeScorecard.match_id == match.id))
            assert room.status == match.status == "completed"
            assert scorecard and scorecard.status == "approved" and topic in scorecard.reasoning
            assert (
                db.scalar(
                    select(func.count(MatchEvent.id)).where(
                        MatchEvent.room_id == room.id,
                        MatchEvent.event_type == "match.completed",
                    )
                )
                == 1
            )
        failed_room = load_room(db, codes[1])
        attempts_rows = list(
            db.scalars(
                select(Speech)
                .where(Speech.room_id == failed_room.id, Speech.stage_key == "ai_case")
                .order_by(Speech.created_at, Speech.id)
            ).all()
        )
        assert [item.status for item in attempts_rows] == ["failed_retried", "completed"]
        assert attempts_rows[0].content == ""
        assert topics[1] in attempts_rows[1].content
        failure = db.scalar(
            select(MatchEvent).where(
                MatchEvent.room_id == failed_room.id,
                MatchEvent.event_type == "provider.failed",
            )
        )
        assert failure and failure.payload["code"] == "agent_timeout" and failure.payload["retryable"] is True


async def test_disconnected_human_pauses_without_replacement_or_cross_room_effects(
    register_user,
    monkeypatch,
) -> None:
    owner = register_user("round8_restore_owner")
    participant = register_user("round8_restore_participant")
    other_owner = register_user("round8_restore_other_owner")
    code = _start(owner, "Round8 真人断线恢复")
    other_code = _start(other_owner, "Round8 隔离对照房间")

    # The participant joins before the first room starts in the real flow; this
    # test deliberately rewinds the fixture-created room to keep setup compact.
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        seat = next(item for item in room.seats if item.seat_key == "neg_1")
        participant_id = participant.get("/api/auth/session").json()["user"]["id"]
        seat.occupant_type = "human"
        seat.user_id = participant_id
        seat.display_name = participant.get("/api/auth/session").json()["user"]["real_name"]
        seat.connected = False
        seat.disconnected_at = now() - timedelta(seconds=61)
        seat.is_ready = True
        room.status = "running"
        room.template_snapshot = [
            {"key": "neg_case", "name": "反方发言", "kind": "speech", "seat": "neg_1", "duration": 60},
            {"key": "human_next", "name": "正方发言", "kind": "speech", "seat": "aff_1", "duration": 60},
        ]
        room.current_stage_index = 0
        room.stage_started_at = now()
        room.stage_deadline_at = now() + timedelta(seconds=60)
        db.commit()

    monkeypatch.setattr(debate_agent, "generate", AsyncMock(return_value="不应生成的自动接替发言。"))
    monkeypatch.setattr(lighttts, "synthesize", AsyncMock(return_value="/media/mock/non-audio.wav"))
    await asyncio.gather(match_engine.process_room(code), match_engine.process_room(other_code))

    with SessionLocal() as db:
        room = load_room(db, code)
        seat = next(item for item in room.seats if item.seat_key == "neg_1")
        assert seat.occupant_type == "human"
        assert room.status == "paused"
        assert room.current_stage_index == 0
        assert db.scalar(
            select(MatchEvent.id).where(
                MatchEvent.room_id == room.id,
                MatchEvent.event_type == "participant.disconnect_timeout",
            )
        )
        assert not db.scalar(
            select(MatchEvent.id).where(
                MatchEvent.room_id == room.id,
                MatchEvent.event_type == "seat.ai_substituted",
            )
        )
        other = load_room(db, other_code)
        assert not db.scalar(
            select(MatchEvent.id).where(
                MatchEvent.room_id == other.id,
                MatchEvent.event_type == "participant.disconnect_timeout",
            )
        )
        for human in room.seats:
            if human.occupant_type == "human":
                human.connected = True
                human.disconnected_at = None
        db.commit()

    requested = participant.post(
        f"/api/rooms/{code}/seat-restore-requests",
        headers=csrf(participant) | {"X-Idempotency-Key": "round8-no-restore-needed"},
        json={},
    )
    assert requested.status_code == 404
    resumed = owner.post(
        f"/api/rooms/{code}/control/resume",
        headers=csrf(owner),
        json={"reason": "所有真人已经重新连接"},
    )
    assert resumed.status_code == 200
    with SessionLocal() as db:
        room = load_room(db, code)
        restored_row = next(item for item in room.seats if item.seat_key == "neg_1")
        assert room.status == "running"
        assert restored_row.occupant_type == "human"


def test_stale_human_speech_cannot_bypass_pause_or_stage_authority(register_user) -> None:
    """The recovery shortcut must enforce the same turn rules as a new speech.

    A stale ``speaking`` row can survive an abrupt API/browser failure.  Before
    this regression guard, retrying ``speech/start`` returned ``resumed=true``
    before checking that the match was still running and on the same stage.
    That let a direct request bypass the disabled UI button in a paused room.
    """

    owner = register_user("round8_stale_human_speech")
    code = _start(owner, "Round8 过期真人发言不得越权恢复")
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.template_snapshot = [
            {"key": "human_case", "name": "正方发言", "kind": "speech", "seat": "aff_1", "duration": 60},
            {"key": "next_case", "name": "下一阶段", "kind": "speech", "seat": "aff_1", "duration": 60},
        ]
        room.current_stage_index = 0
        room.status = "running"
        room.stage_started_at = now()
        room.stage_deadline_at = now() + timedelta(seconds=60)
        db.commit()

    lease_headers = csrf(owner) | {"X-Control-Lease": "round8-stale-device"}
    assert owner.post(f"/api/rooms/{code}/control-lease", headers=lease_headers, json={}).status_code == 200
    started = owner.post(f"/api/rooms/{code}/speech/start", headers=lease_headers, json={})
    assert started.status_code == 200

    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.status = "paused"
        room.paused_remaining_seconds = 45
        room.stage_deadline_at = None
        db.commit()
    paused_resume = owner.post(f"/api/rooms/{code}/speech/start", headers=lease_headers, json={})
    assert paused_resume.status_code == 403 and paused_resume.json()["detail"] == "比赛已暂停"

    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.status = "running"
        room.current_stage_index = 1
        room.stage_started_at = now()
        room.stage_deadline_at = now() + timedelta(seconds=60)
        room.paused_remaining_seconds = None
        db.commit()
    stale_resume = owner.post(f"/api/rooms/{code}/speech/start", headers=lease_headers, json={})
    assert stale_resume.status_code == 409
    assert "不属于当前阶段" in stale_resume.json()["detail"]

    with SessionLocal() as db:
        room = load_room(db, code)
        speech = db.get(Speech, started.json()["speech_id"])
        assert room.current_stage_index == 1 and room.status == "running"
        assert speech and speech.status == "speaking" and speech.stage_key == "human_case"


def test_owner_can_safely_pause_and_restart_a_stuck_human_speech(register_user) -> None:
    owner = register_user("round8_emergency_human_pause")
    code = _start(owner, "Round8 真人发言卡死安全恢复")
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.template_snapshot = [
            {"key": "human_case", "name": "正方发言", "kind": "speech", "seat": "aff_1", "duration": 60}
        ]
        room.current_stage_index = 0
        room.status = "running"
        room.stage_started_at = now()
        room.stage_deadline_at = now() + timedelta(seconds=60)
        db.commit()

    lease_headers = csrf(owner) | {"X-Control-Lease": "round8-emergency-device"}
    assert owner.post(f"/api/rooms/{code}/control-lease", headers=lease_headers, json={}).status_code == 200
    started = owner.post(f"/api/rooms/{code}/speech/start", headers=lease_headers, json={})
    assert started.status_code == 200

    regular_pause = owner.post(
        f"/api/rooms/{code}/control/pause",
        headers=csrf(owner),
        json={"reason": "普通暂停不得截断真人发言"},
    )
    assert regular_pause.status_code == 409
    emergency_pause = owner.post(
        f"/api/rooms/{code}/control/safe-pause",
        headers=csrf(owner) | {"X-Idempotency-Key": "round8-safe-pause"},
        json={"reason": "浏览器异常导致发言无法结束"},
    )
    assert emergency_pause.status_code == 200

    with SessionLocal() as db:
        room = load_room(db, code)
        speech = db.get(Speech, started.json()["speech_id"])
        events = list(db.scalars(select(MatchEvent).where(MatchEvent.room_id == room.id)).all())
        assert room.status == "paused" and room.paused_remaining_seconds is not None
        assert speech and speech.status == "interrupted"
        interrupted = next(item for item in events if item.event_type == "speech.interrupted")
        assert interrupted.payload["reason"] == "emergency_human_pause"
        assert any(item.event_type == "control.safe-pause" for item in events)

    resumed = owner.post(
        f"/api/rooms/{code}/control/resume",
        headers=csrf(owner),
        json={"reason": "确认设备恢复"},
    )
    assert resumed.status_code == 200
    restarted = owner.post(f"/api/rooms/{code}/speech/start", headers=lease_headers, json={})
    assert restarted.status_code == 200
    assert restarted.json()["speech_id"] != started.json()["speech_id"]
