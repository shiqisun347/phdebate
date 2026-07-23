from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.entities import JudgeScorecard, Match, MatchEvent, Speech
from app.services.match_engine import match_engine
from app.services.providers import debate_agent, judge_provider, lighttts
from app.services.room_service import load_room, now
from conftest import csrf
from sqlalchemy import select
from test_platform import create_training_room


def _claim_ready(participant, code: str, seat_key: str | None = None) -> None:
    if seat_key:
        claimed = participant.post(
            f"/api/rooms/{code}/claim-seat",
            headers=csrf(participant),
            json={"seat_key": seat_key},
        )
        assert claimed.status_code == 200, claimed.text
    ready = participant.post(f"/api/rooms/{code}/ready", headers=csrf(participant), json={"ready": True})
    assert ready.status_code == 200, ready.text


def _lease(participant, code: str, lease: str) -> dict[str, str]:
    acquired = participant.post(
        f"/api/rooms/{code}/control-lease",
        headers=csrf(participant) | {"X-Control-Lease": lease},
        json={},
    )
    assert acquired.status_code == 200, acquired.text
    return csrf(participant) | {"X-Control-Lease": lease}


def _configure(code: str, stages: list[dict]) -> None:
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.template_snapshot = stages
        room.current_stage_index = 0
        room.status = "running"
        room.stage_started_at = now()
        room.stage_deadline_at = now() + timedelta(seconds=int(stages[0].get("duration", 30)))
        room.paused_remaining_seconds = None
        db.commit()


def _human_turn(participant, code: str, headers: dict[str, str], content: str, *, reject_empty: bool = False) -> str:
    started = participant.post(f"/api/rooms/{code}/speech/start", headers=headers, json={})
    assert started.status_code == 200, started.text
    speech_id = started.json()["speech_id"]
    if reject_empty:
        empty = participant.post(
            f"/api/rooms/{code}/speech/finish",
            headers=headers,
            json={"speech_id": speech_id, "content": ""},
        )
        assert empty.status_code == 422 and "不能为空" in empty.json()["detail"]
    finish_headers = headers | {"X-Idempotency-Key": f"finish-{speech_id}"}
    finished = participant.post(
        f"/api/rooms/{code}/speech/finish",
        headers=finish_headers,
        json={"speech_id": speech_id, "content": content},
    )
    replayed = participant.post(
        f"/api/rooms/{code}/speech/finish",
        headers=finish_headers,
        json={"speech_id": speech_id, "content": content},
    )
    assert finished.status_code == 200, finished.text
    assert replayed.status_code == 200 and replayed.json()["replayed"] is True
    return speech_id


async def test_round5_three_realistic_match_formats_complete_together_without_cross_room_state(
    client,
    register_user,
    monkeypatch,
) -> None:
    human_owner = register_user("round5_hh_aff")
    human_opponent = register_user("round5_hh_neg")
    human_room = create_training_room(human_owner, "Round5 人人完整比赛")
    human_code = human_room["code"]
    _claim_ready(human_owner, human_code)
    _claim_ready(human_opponent, human_code, "neg_1")
    assert human_owner.post(f"/api/rooms/{human_code}/start", headers=csrf(human_owner), json={}).status_code == 200

    hybrid_owner = register_user("round5_ha_aff")
    hybrid_room = create_training_room(hybrid_owner, "Round5 人机完整比赛")
    hybrid_code = hybrid_room["code"]
    _claim_ready(hybrid_owner, hybrid_code)
    assert hybrid_owner.post(f"/api/rooms/{hybrid_code}/start", headers=csrf(hybrid_owner), json={}).status_code == 200

    daily = client.get("/api/competitions/daily-4v4").json()["competition"]
    mixed_players = [register_user(f"round5_4v4_{index}") for index in range(4)]
    mixed_created = mixed_players[0].post(
        "/api/rooms",
        headers=csrf(mixed_players[0]),
        json={
            "competition_slug": "daily-4v4",
            "topic_id": daily["topics"][0]["id"],
            "seat_key": "aff_1",
            "visibility": "public",
        },
    )
    assert mixed_created.status_code == 200, mixed_created.text
    mixed_code = mixed_created.json()["room"]["code"]
    for participant, seat_key in zip(mixed_players, [None, "aff_2", "neg_1", "neg_2"]):
        _claim_ready(participant, mixed_code, seat_key)
    assert mixed_players[0].post(f"/api/rooms/{mixed_code}/start", headers=csrf(mixed_players[0]), json={}).status_code == 200

    _configure(
        human_code,
        [
            {"key": "aff_human", "name": "正方真人立论", "kind": "speech", "seat": "aff_1", "duration": 30},
            {"key": "neg_human", "name": "反方真人立论", "kind": "speech", "seat": "neg_1", "duration": 30},
            {"key": "judge", "name": "裁判", "kind": "judging", "duration": 10},
        ],
    )
    _configure(
        hybrid_code,
        [
            {"key": "aff_human", "name": "真人立论", "kind": "speech", "seat": "aff_1", "duration": 30},
            {"key": "neg_ai", "name": "AI 回应", "kind": "speech", "seat": "neg_1", "duration": 30},
            {"key": "judge", "name": "裁判", "kind": "judging", "duration": 10},
        ],
    )
    _configure(
        mixed_code,
        [
            {"key": "aff_1", "name": "正方真人一辩", "kind": "speech", "seat": "aff_1", "duration": 30},
            {"key": "neg_3", "name": "反方 AI 三辩", "kind": "speech", "seat": "neg_3", "duration": 30},
            {"key": "aff_2", "name": "正方真人二辩", "kind": "speech", "seat": "aff_2", "duration": 30},
            {"key": "neg_4", "name": "反方 AI 四辩", "kind": "speech", "seat": "neg_4", "duration": 30},
            {"key": "judge", "name": "裁判", "kind": "judging", "duration": 10},
        ],
    )

    async def generated(payload, **_kwargs):
        return f"围绕{payload['debate_topic']}，本席给出完整且只属于当前房间的论证。"

    async def synthesized(*_args, room_code: str, speech_id: str, **_kwargs):
        return f"/media/fake/{room_code}/{speech_id}.wav"

    async def judged(topic, _speeches, **_kwargs):
        return {
            "winner": "aff",
            "affirmative_score": 88,
            "negative_score": 84,
            "individual_scores": {},
            "reasoning": f"{topic} 的确定性裁判结果。",
        }

    monkeypatch.setattr(debate_agent, "generate", generated)
    monkeypatch.setattr(lighttts, "synthesize", synthesized)
    monkeypatch.setattr(judge_provider, "judge", judged)

    human_aff_headers = _lease(human_owner, human_code, "hh-aff")
    human_neg_headers = _lease(human_opponent, human_code, "hh-neg")
    hybrid_headers = _lease(hybrid_owner, hybrid_code, "ha-aff")
    mixed_aff1_headers = _lease(mixed_players[0], mixed_code, "mixed-aff1")
    mixed_aff2_headers = _lease(mixed_players[1], mixed_code, "mixed-aff2")

    human_aff_speech = _human_turn(
        human_owner,
        human_code,
        human_aff_headers,
        "人人赛正方提供完整立论。",
        reject_empty=True,
    )
    _human_turn(hybrid_owner, hybrid_code, hybrid_headers, "人机赛真人提供完整立论。")
    _human_turn(mixed_players[0], mixed_code, mixed_aff1_headers, "四对四正方一辩完整立论。")

    wrong_room = hybrid_owner.post(
        f"/api/rooms/{hybrid_code}/speech/finish",
        headers=hybrid_headers,
        json={"speech_id": human_aff_speech, "content": "不得跨房间完成发言。"},
    )
    assert wrong_room.status_code == 409

    await asyncio.gather(match_engine.process_room(hybrid_code), match_engine.process_room(mixed_code))
    _human_turn(human_opponent, human_code, human_neg_headers, "人人赛反方提供完整反驳。")
    _human_turn(mixed_players[1], mixed_code, mixed_aff2_headers, "四对四正方二辩继续推进论证。")
    await match_engine.process_room(mixed_code)
    await asyncio.gather(
        match_engine.process_room(human_code),
        match_engine.process_room(hybrid_code),
        match_engine.process_room(mixed_code),
    )

    expected_counts = {human_code: (2, 0), hybrid_code: (1, 1), mixed_code: (2, 2)}
    with SessionLocal() as db:
        for code, (human_count, ai_count) in expected_counts.items():
            room = load_room(db, code)
            match = db.scalar(select(Match).where(Match.room_id == room.id))
            speeches = list(db.scalars(select(Speech).where(Speech.room_id == room.id)).all())
            scorecard = db.scalar(select(JudgeScorecard).where(JudgeScorecard.match_id == match.id))
            assert room.status == "completed" and match.status == "completed"
            assert sum(item.speaker_type == "human" for item in speeches) == human_count
            assert sum(item.speaker_type == "ai" for item in speeches) == ai_count
            assert scorecard and scorecard.status == "approved" and room.topic in scorecard.reasoning
            for speech in speeches:
                if speech.speaker_type == "ai":
                    assert room.topic in speech.content


async def test_round5_invalid_agent_output_pauses_as_agent_failure_before_fake_tts_and_can_retry(
    register_user,
    monkeypatch,
) -> None:
    owner = register_user("round5_invalid_agent")
    room_data = create_training_room(owner, "Round5 Agent 坏输出恢复")
    code = room_data["code"]
    _claim_ready(owner, code)
    assert owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={}).status_code == 200
    _configure(
        code,
        [
            {"key": "neg_ai", "name": "AI 立论", "kind": "speech", "seat": "neg_1", "duration": 30},
            {"key": "judge", "name": "裁判", "kind": "judging", "duration": 10},
        ],
    )

    monkeypatch.setattr(debate_agent, "generate", AsyncMock(return_value="……!!!"))
    fake_tts = AsyncMock(return_value="/media/fake/never.wav")
    monkeypatch.setattr(lighttts, "synthesize", fake_tts)
    await match_engine.process_room(code)
    fake_tts.assert_not_awaited()

    with SessionLocal() as db:
        room = load_room(db, code)
        failed = db.scalar(
            select(MatchEvent)
            .where(MatchEvent.room_id == room.id, MatchEvent.event_type == "provider.failed")
            .order_by(MatchEvent.seq.desc())
        )
        assert room.status == "paused"
        assert failed and failed.payload["provider"] == "agent"
        assert failed.payload["code"] == "agent_invalid_output" and failed.payload["retryable"] is True

    retry_headers = csrf(owner) | {"X-Idempotency-Key": "retry-invalid-agent-once"}
    retried = owner.post(f"/api/rooms/{code}/control/retry", headers=retry_headers, json={"reason": "修复后重试"})
    replayed = owner.post(f"/api/rooms/{code}/control/retry", headers=retry_headers, json={"reason": "修复后重试"})
    assert retried.status_code == 200
    assert replayed.status_code == 200 and replayed.json()["replayed"] is True

    monkeypatch.setattr(debate_agent, "generate", AsyncMock(return_value="修复后的 Agent 给出完整且有效的立论内容。"))
    monkeypatch.setattr(
        judge_provider,
        "judge",
        AsyncMock(
            return_value={
                "winner": "neg",
                "affirmative_score": 82,
                "negative_score": 88,
                "individual_scores": {},
                "reasoning": "修复后的 Agent 输出可正常进入裁判。",
            }
        ),
    )
    await match_engine.process_room(code)
    await match_engine.process_room(code)
    assert fake_tts.await_count == 1
    with SessionLocal() as db:
        room = load_room(db, code)
        assert room.status == "completed"


async def test_round5_realtime_agent_body_gate_blocks_thinking_and_invalid_prefix_before_session(
    register_user,
    monkeypatch,
) -> None:
    owner = register_user("round5_realtime_body_gate")
    room_data = create_training_room(owner, "Round5 实时 Agent 正文门")
    code = room_data["code"]
    _claim_ready(owner, code)
    assert owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={}).status_code == 200
    _configure(
        code,
        [
            {"key": "neg_ai", "name": "AI 立论", "kind": "speech", "seat": "neg_1", "duration": 30},
            {"key": "judge", "name": "裁判", "kind": "judging", "duration": 10},
        ],
    )

    class FakeSession:
        def __init__(self) -> None:
            self.pushes: list[str] = []
            self.finish_calls = 0
            self.abort_calls = 0
            self.pushed = asyncio.Event()

        async def push_text(self, text: str) -> None:
            self.pushes.append(text)
            self.pushed.set()

        async def finish(self) -> str:
            self.finish_calls += 1
            return f"/media/fake/{code}/realtime.wav"

        async def abort(self, reason: str = "") -> None:
            del reason
            self.abort_calls += 1

    sessions: list[FakeSession] = []
    deadlines: list[float | None] = []

    def open_session(*, deadline_monotonic: float | None, **_kwargs):
        deadlines.append(deadline_monotonic)
        session = FakeSession()
        sessions.append(session)
        return session

    async def invalid_stream(*_args, **_kwargs):
        yield {"type": "thinking", "delta": "这一段内部分析绝不能进入语音。"}
        yield {"type": "delta", "channel": "reasoning", "delta": "这段推理也不能进入语音。"}
        yield {"type": "delta", "delta": "……"}
        yield {"type": "delta", "delta": "!!!"}
        yield {"type": "final", "content": "……!!!"}

    monkeypatch.setattr(settings, "realtime_voice_backend", "lighttts")
    monkeypatch.setattr(settings, "lighttts_streaming_enabled", True)
    monkeypatch.setattr(settings, "lighttts_bistream_enabled", True)
    monkeypatch.setattr(settings, "realtime_voice_pipeline_enabled", True)
    monkeypatch.setattr(lighttts, "open_incremental_session", open_session)
    monkeypatch.setattr(debate_agent, "generate_stream", invalid_stream)

    await match_engine.process_room(code)
    assert len(sessions) == 1
    assert deadlines[0] is not None and deadlines[0] > asyncio.get_running_loop().time()
    assert sessions[0].pushes == []
    assert sessions[0].finish_calls == 0
    assert sessions[0].abort_calls >= 1
    with SessionLocal() as db:
        room = load_room(db, code)
        failure = db.scalar(
            select(MatchEvent)
            .where(MatchEvent.room_id == room.id, MatchEvent.event_type == "provider.failed")
            .order_by(MatchEvent.seq.desc())
        )
        assert room.status == "paused"
        assert failure and failure.payload["provider"] == "agent"
        assert failure.payload["code"] == "agent_invalid_output"

    retried = owner.post(
        f"/api/rooms/{code}/control/retry",
        headers=csrf(owner) | {"X-Idempotency-Key": "retry-realtime-body-once"},
        json={"reason": "Agent 正文恢复"},
    )
    assert retried.status_code == 200, retried.text

    release_final = asyncio.Event()

    async def valid_stream(*_args, **_kwargs):
        yield {"type": "analysis", "delta": "隐藏的推理过程。"}
        yield {"type": "delta", "delta": "我方认为应当以长期影响作为核心判断标准，"}
        await release_final.wait()
        yield {"type": "delta", "delta": "并进一步比较双方方案的实际后果。"}
        yield {
            "type": "final",
            "content": "我方认为应当以长期影响作为核心判断标准，并进一步比较双方方案的实际后果。",
        }

    monkeypatch.setattr(debate_agent, "generate_stream", valid_stream)
    processing = asyncio.create_task(match_engine.process_room(code))
    for _ in range(100):
        if len(sessions) >= 2:
            break
        await asyncio.sleep(0.01)
    assert len(sessions) == 2
    assert deadlines[1] is not None and deadlines[1] > asyncio.get_running_loop().time()
    await asyncio.wait_for(sessions[1].pushed.wait(), timeout=1)
    assert release_final.is_set() is False
    prefinal_text = "".join(sessions[1].pushes)
    assert len(prefinal_text) >= 12
    assert "我方认为应当以长期影响作为核心判断标准，".startswith(prefinal_text)
    assert all("推理" not in chunk and "分析" not in chunk for chunk in sessions[1].pushes)
    release_final.set()
    await asyncio.wait_for(processing, timeout=2)
    assert sessions[1].finish_calls == 1
    assert "".join(sessions[1].pushes) == "我方认为应当以长期影响作为核心判断标准，并进一步比较双方方案的实际后果。"
