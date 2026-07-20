from __future__ import annotations

import asyncio
from datetime import timedelta

from app.core.database import SessionLocal
from app.models.entities import JudgeProfile, JudgeScorecard, Match, MatchEvent, Speech
from app.services.match_engine import match_engine
from app.services.providers import ProviderError, debate_agent, judge_provider, lighttts
from app.services.room_service import load_room, now
from conftest import csrf
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from test_platform import create_training_room


def _start_ai_room(owner, topic: str) -> str:
    room_data = create_training_room(owner, topic)
    code = room_data["code"]
    assert owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True}).status_code == 200
    assert owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={}).status_code == 200
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.template_snapshot = [
            {"key": "ai_case", "name": "AI 立论", "kind": "speech", "seat": "aff_1", "duration": 30},
            {"key": "judge", "name": "AI 裁判", "kind": "judging", "duration": 10},
        ]
        room.current_stage_index = 0
        room.status = "running"
        room.stage_started_at = now()
        room.stage_deadline_at = now() + timedelta(seconds=30)
        for seat in room.seats:
            seat.occupant_type = "ai"
            seat.user_id = None
            seat.display_name = f"AI-{code}-{seat.seat_key}"
        db.commit()
    return code


async def test_terminate_interrupts_running_judge_immediately_and_late_result_cannot_resurrect(
    register_user,
    monkeypatch,
) -> None:
    owner = register_user("round6_terminate_judge")
    code = _start_ai_room(owner, "Round6 终止裁判任务")
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.current_stage_index = 1
        room.status = "judging"
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        match.status = "judging"
        db.commit()

    entered = asyncio.Event()
    release = asyncio.Event()

    async def delayed_judge(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return {
            "winner": "aff",
            "affirmative_score": 90,
            "negative_score": 80,
            "individual_scores": {},
            "reasoning": "这个迟到裁判结果不得复活比赛。",
        }

    monkeypatch.setattr(judge_provider, "judge", delayed_judge)
    processing = asyncio.create_task(match_engine.process_room(code))
    await asyncio.wait_for(entered.wait(), timeout=1)
    headers = csrf(owner) | {"X-Idempotency-Key": "terminate-running-judge-once"}
    terminated = owner.post(
        f"/api/rooms/{code}/control/terminate",
        headers=headers,
        json={"reason": "终止卡住的裁判"},
    )
    replayed = owner.post(
        f"/api/rooms/{code}/control/terminate",
        headers=headers,
        json={"reason": "终止卡住的裁判"},
    )
    assert terminated.status_code == 200
    assert replayed.status_code == 200 and replayed.json()["replayed"] is True
    with SessionLocal() as db:
        room = load_room(db, code)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        scorecard = db.scalar(select(JudgeScorecard).where(JudgeScorecard.match_id == match.id))
        assert room.status == match.status == "terminated"
        assert scorecard and scorecard.status == "interrupted" and scorecard.reasoning == "match_terminated"
        assert (
            db.scalar(
                select(func.count(MatchEvent.id)).where(
                    MatchEvent.room_id == room.id,
                    MatchEvent.event_type == "judge.interrupted",
                )
            )
            == 1
        )

    release.set()
    await asyncio.wait_for(processing, timeout=1)
    with SessionLocal() as db:
        room = load_room(db, code)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        scorecard = db.scalar(select(JudgeScorecard).where(JudgeScorecard.match_id == match.id))
        assert room.status == match.status == "terminated"
        assert scorecard.status == "interrupted"
        assert not db.scalar(
            select(MatchEvent.id).where(
                MatchEvent.room_id == room.id,
                MatchEvent.event_type == "match.completed",
            )
        )


async def test_four_rooms_recover_agent_and_judge_failures_without_cross_room_leakage(
    client,
    register_user,
    monkeypatch,
) -> None:
    topics = {
        "healthy": "Round6 健康房间",
        "agent": "Round6 Agent 超时房间",
        "judge": "Round6 裁判失败房间",
        "terminate": "Round6 中途终止房间",
    }
    owners = {name: register_user(f"round6_{name}") for name in topics}
    codes = {name: _start_ai_room(owners[name], topic) for name, topic in topics.items()}
    agent_attempts: dict[str, int] = {}
    judge_attempts: dict[str, int] = {}
    terminate_entered = asyncio.Event()
    release_terminated_agent = asyncio.Event()

    async def generated(payload, **_kwargs):
        topic = payload["debate_topic"]
        agent_attempts[topic] = agent_attempts.get(topic, 0) + 1
        if topic == topics["agent"] and agent_attempts[topic] == 1:
            raise ProviderError("Agent 请求超时。", code="agent_timeout", retryable=True)
        if topic == topics["terminate"]:
            terminate_entered.set()
            await release_terminated_agent.wait()
        return f"只属于{topic}的确定性 Agent 正文。"

    async def synthesized(*_args, room_code: str, speech_id: str, **_kwargs):
        return f"/media/fake/{room_code}/{speech_id}.wav"

    async def judged(topic, _speeches, **_kwargs):
        judge_attempts[topic] = judge_attempts.get(topic, 0) + 1
        if topic == topics["judge"] and judge_attempts[topic] == 1:
            raise ProviderError("裁判服务暂时不可用。", code="judge_timeout", retryable=True)
        return {
            "winner": "aff",
            "affirmative_score": 88,
            "negative_score": 84,
            "individual_scores": {},
            "reasoning": f"只属于{topic}的裁判结果。",
        }

    monkeypatch.setattr(debate_agent, "generate", generated)
    monkeypatch.setattr(lighttts, "synthesize", synthesized)
    monkeypatch.setattr(judge_provider, "judge", judged)

    first_pass = {name: asyncio.create_task(match_engine.process_room(code)) for name, code in codes.items()}
    await asyncio.wait_for(terminate_entered.wait(), timeout=1)
    terminate_headers = csrf(owners["terminate"]) | {"X-Idempotency-Key": "terminate-agent-room-once"}
    terminated = owners["terminate"].post(
        f"/api/rooms/{codes['terminate']}/control/terminate",
        headers=terminate_headers,
        json={"reason": "测试中途终止"},
    )
    replayed = owners["terminate"].post(
        f"/api/rooms/{codes['terminate']}/control/terminate",
        headers=terminate_headers,
        json={"reason": "测试中途终止"},
    )
    assert terminated.status_code == 200
    assert replayed.status_code == 200 and replayed.json()["replayed"] is True
    release_terminated_agent.set()
    await asyncio.gather(*first_pass.values())

    with SessionLocal() as db:
        assert load_room(db, codes["healthy"]).status == "judging"
        agent_room = load_room(db, codes["agent"])
        assert agent_room.status == "paused" and "超时" in agent_room.failure_reason
        assert load_room(db, codes["judge"]).status == "judging"
        assert load_room(db, codes["terminate"]).status == "terminated"

    await asyncio.gather(
        match_engine.process_room(codes["healthy"]),
        match_engine.process_room(codes["judge"]),
    )

    retry_headers = csrf(owners["agent"]) | {"X-Idempotency-Key": "retry-agent-timeout-once"}
    retried = owners["agent"].post(
        f"/api/rooms/{codes['agent']}/control/retry",
        headers=retry_headers,
        json={"reason": "Agent 已恢复"},
    )
    retry_replay = owners["agent"].post(
        f"/api/rooms/{codes['agent']}/control/retry",
        headers=retry_headers,
        json={"reason": "Agent 已恢复"},
    )
    assert retried.status_code == 200
    assert retry_replay.status_code == 200 and retry_replay.json()["replayed"] is True
    await match_engine.process_room(codes["agent"])
    await match_engine.process_room(codes["agent"])

    with SessionLocal() as db:
        judge_room = load_room(db, codes["judge"])
        judge_match = db.scalar(select(Match).where(Match.room_id == judge_room.id))
        scorecard = db.scalar(select(JudgeScorecard).where(JudgeScorecard.match_id == judge_match.id))
        assert judge_room.status == judge_match.status == scorecard.status == "review_required"
        scorecard_id = scorecard.id
        expected_updated_at = scorecard.updated_at.isoformat()
        if not db.scalar(select(JudgeProfile.id).where(JudgeProfile.is_active.is_(True))):
            db.add(
                JudgeProfile(
                    name="Round6 恢复裁判",
                    endpoint="http://judge.test/api/judge",
                    model_name="judge-recovery-test",
                    timeout_seconds=30,
                    is_active=True,
                )
            )
            db.commit()

    admin = TestClient(client.app)
    with admin:
        assert admin.post(
            "/api/auth/login",
            json={"account": "admin_test", "password": "Admin-test-1234"},
        ).status_code == 200
        judge_retry = admin.post(
            f"/api/admin/reviews/{scorecard_id}/retry",
            headers=csrf(admin),
            json={"expected_updated_at": expected_updated_at},
        )
        stale_retry = admin.post(
            f"/api/admin/reviews/{scorecard_id}/retry",
            headers=csrf(admin),
            json={"expected_updated_at": expected_updated_at},
        )
        assert judge_retry.status_code == 202, judge_retry.text
        assert stale_retry.status_code == 409

    await match_engine.process_room(codes["judge"])

    with SessionLocal() as db:
        for name in ("healthy", "agent", "judge"):
            room = load_room(db, codes[name])
            match = db.scalar(select(Match).where(Match.room_id == room.id))
            scorecard = db.scalar(select(JudgeScorecard).where(JudgeScorecard.match_id == match.id))
            speeches = list(db.scalars(select(Speech).where(Speech.room_id == room.id)).all())
            assert room.status == match.status == "completed"
            assert scorecard.status == "approved" and room.topic in scorecard.reasoning
            assert all(
                room.topic in speech.content
                for speech in speeches
                if speech.speaker_type == "ai" and speech.status == "completed"
            )
            assert all(
                not speech.content
                for speech in speeches
                if speech.speaker_type == "ai" and speech.status in {"failed", "failed_retried"}
            )
        terminated_room = load_room(db, codes["terminate"])
        terminated_match = db.scalar(select(Match).where(Match.room_id == terminated_room.id))
        assert terminated_room.status == terminated_match.status == "terminated"
        assert not db.scalar(
            select(MatchEvent.id).where(
                MatchEvent.room_id == terminated_room.id,
                MatchEvent.event_type == "match.completed",
            )
        )
