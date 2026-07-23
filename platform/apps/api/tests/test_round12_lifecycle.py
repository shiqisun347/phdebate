from __future__ import annotations

import asyncio
import hashlib
from datetime import timedelta

import pytest
from app.api import rooms as rooms_api
from app.core.database import SessionLocal
from app.models.entities import (
    Competition,
    CompetitionTopic,
    JudgeScorecard,
    LeaderboardEntry,
    Match,
    MatchEvent,
    Speech,
)
from app.services.match_archive import build_match_archive
from app.services.match_engine import match_engine, recover_inflight_engine_tasks
from app.services.providers import ProviderError, debate_agent, judge_provider, lighttts
from app.services.room_service import load_room, now
from conftest import csrf
from fastapi.testclient import TestClient
from sqlalchemy import func, select


def _create_daily_room(owner: TestClient, topic_title: str) -> dict:
    with SessionLocal() as db:
        competition = db.scalar(select(Competition).where(Competition.slug == "daily-4v4"))
        assert competition is not None
        topic = db.scalar(
            select(CompetitionTopic).where(
                CompetitionTopic.competition_id == competition.id,
                CompetitionTopic.is_active.is_(True),
            )
        )
        assert topic is not None
        topic.title = topic_title
        db.commit()
        topic_id = topic.id
    response = owner.post(
        "/api/rooms",
        headers=csrf(owner),
        json={
            "competition_slug": "daily-4v4",
            "topic_id": topic_id,
            "seat_key": "aff_1",
            "visibility": "public",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["room"]


def _create_training_room(owner: TestClient, topic: str) -> dict:
    response = owner.post(
        "/api/rooms",
        headers=csrf(owner),
        json={
            "competition_slug": "training-1v1",
            "custom_topic": topic,
            "seat_key": "aff_1",
            "visibility": "public",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["room"]


def _ready_and_start(owner: TestClient, code: str, *participants: TestClient) -> None:
    for participant in (owner, *participants):
        ready = participant.post(f"/api/rooms/{code}/ready", headers=csrf(participant), json={"ready": True})
        assert ready.status_code == 200, ready.text
    started = owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    assert started.status_code == 200, started.text


def _set_template(code: str, template: list[dict]) -> None:
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.template_snapshot = template
        db.commit()


def _lease(client: TestClient, code: str, value: str) -> dict[str, str]:
    headers = csrf(client) | {"X-Control-Lease": value}
    acquired = client.post(f"/api/rooms/{code}/control-lease", headers=headers, json={})
    assert acquired.status_code == 200, acquired.text
    return headers


def _speak(client: TestClient, code: str, headers: dict[str, str], content: str) -> str:
    identity = hashlib.sha256(content.encode()).hexdigest()[:16]
    started = client.post(
        f"/api/rooms/{code}/speech/start",
        headers=headers | {"X-Idempotency-Key": f"start-{identity}"},
        json={},
    )
    assert started.status_code == 200, started.text
    speech_id = started.json()["speech_id"]
    finished = client.post(
        f"/api/rooms/{code}/speech/finish",
        headers=headers | {"X-Idempotency-Key": f"finish-{identity}"},
        json={"speech_id": speech_id, "content": content},
    )
    assert finished.status_code == 200, finished.text
    return speech_id


@pytest.mark.asyncio
async def test_round12_complete_mixed_human_agent_lifecycle_is_recoverable_and_room_isolated(
    client: TestClient,
    register_user,
    monkeypatch,
) -> None:
    """Exercise one realistic ranked match plus two concurrent repair paths.

    All providers are deterministic in-process fakes.  No production Agent,
    judge, speech synthesis, streaming, browser audio, or media file is used.
    """

    owner = register_user("round12_ranked_owner")
    returning_debater = register_user("round12_returning_debater")
    skipped_owner = register_user("round12_skipped_owner")
    terminated_owner = register_user("round12_terminated_owner")

    ranked_topic = "Round12 混合真人与多智能体完整生命周期"
    skipped_topic = "Round12 并发人工跳过隔离场"
    terminated_topic = "Round12 房主紧急终止隔离场"

    ranked_room = _create_daily_room(owner, ranked_topic)
    ranked_code = ranked_room["code"]
    claimed = returning_debater.post(
        f"/api/rooms/{ranked_code}/claim-seat",
        headers=csrf(returning_debater),
        json={"seat_key": "neg_1"},
    )
    assert claimed.status_code == 200, claimed.text
    _ready_and_start(owner, ranked_code, returning_debater)
    _set_template(
        ranked_code,
        [
            {"key": "aff_case", "name": "正方真人立论", "kind": "speech", "seat": "aff_1", "duration": 60},
            {"key": "neg_case", "name": "反方接替立论", "kind": "speech", "seat": "neg_1", "duration": 60},
            {"key": "aff_ai", "name": "正方 AI 驳论", "kind": "speech", "seat": "aff_2", "duration": 60},
            {
                "key": "free",
                "name": "真人自由辩论",
                "kind": "free",
                "side": "aff",
                "duration": 120,
                "turn_duration": 30,
            },
            {"key": "neg_summary", "name": "反方真人总结", "kind": "speech", "seat": "neg_1", "duration": 60},
            {"key": "judge", "name": "AI 裁判", "kind": "judging", "duration": 20},
        ],
    )

    skipped_room = _create_training_room(skipped_owner, skipped_topic)
    skipped_code = skipped_room["code"]
    _ready_and_start(skipped_owner, skipped_code)
    _set_template(
        skipped_code,
        [
            {"key": "ai_case", "name": "可人工跳过的 AI 发言", "kind": "speech", "seat": "neg_1", "duration": 60},
            {"key": "judge", "name": "人工接管裁判", "kind": "judging", "duration": 20},
        ],
    )

    terminated_room = _create_training_room(terminated_owner, terminated_topic)
    terminated_code = terminated_room["code"]
    _ready_and_start(terminated_owner, terminated_code)
    _set_template(
        terminated_code,
        [{"key": "human_case", "name": "终止前真人发言", "kind": "speech", "seat": "aff_1", "duration": 60}],
    )

    agent_attempts: dict[tuple[str, str], int] = {}

    async def generated(payload, **_kwargs):
        identity = (payload["room_code"], payload["current_stage"])
        agent_attempts[identity] = agent_attempts.get(identity, 0) + 1
        if payload["room_code"] == ranked_code and payload["current_stage"] == "反方接替立论" and agent_attempts[identity] == 1:
            raise ProviderError("Round12 模拟 Agent 暂时不可用", code="agent_timeout", retryable=True)
        return f"房间{payload['room_code']}围绕《{payload['debate_topic']}》在{payload['current_stage']}给出独立、完整且可审计的论证。"

    async def synthesized(*_args, **_kwargs):
        # An empty URL makes the engine complete the text-only fake immediately,
        # without creating, decoding, streaming, or playing any audio artifact.
        return ""

    async def judged(topic, speeches, **_kwargs):
        assert speeches
        assert all(topic in item["content"] or item["seat_key"] in {"aff_1", "neg_1"} for item in speeches)
        return {
            "winner": "aff",
            "affirmative_score": 91,
            "negative_score": 86,
            "individual_scores": {},
            "reasoning": f"只属于《{topic}》的 Round12 独立裁判结论。",
        }

    queued_archives: list[str] = []
    monkeypatch.setattr(debate_agent, "generate", generated)
    monkeypatch.setattr(lighttts, "synthesize", synthesized)
    monkeypatch.setattr(judge_provider, "judge", judged)
    monkeypatch.setattr("app.services.match_engine.enqueue_match_archive", lambda match_id: queued_archives.append(match_id) or True)
    monkeypatch.setattr(rooms_api, "enqueue_match_archive", lambda match_id: queued_archives.append(match_id) or True)

    # The three rooms leave `preparing` concurrently. No room may consume
    # another room's template or provider result.
    await asyncio.gather(
        match_engine.process_room(ranked_code),
        match_engine.process_room(skipped_code),
        match_engine.process_room(terminated_code),
    )
    with SessionLocal() as db:
        assert load_room(db, ranked_code).current_stage_index == 0
        assert load_room(db, skipped_code).current_stage_index == 0
        assert load_room(db, terminated_code).current_stage_index == 0

    # A human can safely pause and resume before speaking.
    paused = owner.post(
        f"/api/rooms/{ranked_code}/control/pause",
        headers=csrf(owner) | {"X-Idempotency-Key": "round12-pause-once"},
        json={"reason": "核对设备与网络"},
    )
    assert paused.status_code == 200, paused.text
    resumed = owner.post(
        f"/api/rooms/{ranked_code}/control/resume",
        headers=csrf(owner) | {"X-Idempotency-Key": "round12-resume-once"},
        json={"reason": "网络稳定，继续比赛"},
    )
    assert resumed.status_code == 200, resumed.text
    owner_headers = _lease(owner, ranked_code, "round12-owner-device")
    _speak(owner, ranked_code, owner_headers, f"围绕《{ranked_topic}》，正方真人完成首轮完整立论并提交可追溯文字。")

    # The second human disconnects for more than the grace period. The engine
    # pauses only this room, preserves the human seat, and never starts an AI
    # replacement while a different room advances normally.
    with SessionLocal() as db:
        room = load_room(db, ranked_code, lock=True)
        returning_seat = next(item for item in room.seats if item.seat_key == "neg_1")
        returning_seat.connected = False
        returning_seat.disconnected_at = now() - timedelta(seconds=61)
        db.commit()
    await asyncio.gather(match_engine.process_room(ranked_code), match_engine.process_room(skipped_code))
    with SessionLocal() as db:
        ranked = load_room(db, ranked_code)
        skipped = load_room(db, skipped_code)
        human_seat = next(item for item in ranked.seats if item.seat_key == "neg_1")
        assert ranked.status == "paused" and "真人辩手断线" in ranked.failure_reason
        assert human_seat.occupant_type == "human" and human_seat.user_id is not None
        assert not db.scalar(
            select(MatchEvent.id).where(
                MatchEvent.room_id == ranked.id,
                MatchEvent.event_type == "seat.ai_substituted",
            )
        )
        assert skipped.status == "judging" and skipped.current_stage_index == 1

    blocked_resume = owner.post(
        f"/api/rooms/{ranked_code}/control/resume",
        headers=csrf(owner),
        json={"reason": "辩手仍离线时不得继续"},
    )
    assert blocked_resume.status_code == 409
    with SessionLocal() as db:
        room = load_room(db, ranked_code, lock=True)
        returning_seat = next(item for item in room.seats if item.seat_key == "neg_1")
        returning_seat.connected = True
        returning_seat.disconnected_at = None
        owner_seat = next(item for item in room.seats if item.user_id == room.owner_id)
        owner_seat.connected = True
        owner_seat.disconnected_at = None
        db.commit()
    resumed_after_return = owner.post(
        f"/api/rooms/{ranked_code}/control/resume",
        headers=csrf(owner) | {"X-Idempotency-Key": "round12-resume-after-return"},
        json={"reason": "真人辩手已经重新连接"},
    )
    assert resumed_after_return.status_code == 200, resumed_after_return.text
    returning_headers = _lease(returning_debater, ranked_code, "round12-returning-device")
    _speak(
        returning_debater,
        ranked_code,
        returning_headers,
        f"围绕《{ranked_topic}》，反方真人重连后完成本轮立论。",
    )
    with SessionLocal() as db:
        room = load_room(db, ranked_code)
        assert room.status == "running" and room.current_stage_index == 2

    # Simulate a process crash with an in-flight AI speech. Startup recovery
    # invalidates the orphan, and a fresh engine pass completes the same stage.
    with SessionLocal() as db:
        room = load_room(db, ranked_code, lock=True)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        orphan = Speech(
            match_id=match.id,
            room_id=room.id,
            seat_key="aff_2",
            stage_key="aff_ai",
            speaker_type="ai",
            status="speaking",
        )
        db.add(orphan)
        db.commit()
        orphan_id = orphan.id
    recovered = recover_inflight_engine_tasks()
    assert recovered["speeches"] >= 1
    with SessionLocal() as db:
        assert db.get(Speech, orphan_id).status == "interrupted"
    await match_engine.process_room(ranked_code)
    with SessionLocal() as db:
        room = load_room(db, ranked_code)
        assert room.current_stage_index == 3 and room.template_snapshot[3]["side"] == "aff"

    free_content = f"《{ranked_topic}》自由辩论正方发言，观点与本房间历史严格关联。"
    free_identity = hashlib.sha256(free_content.encode()).hexdigest()[:16]
    free_started = owner.post(
        f"/api/rooms/{ranked_code}/speech/start",
        headers=owner_headers | {"X-Idempotency-Key": f"start-{free_identity}"},
        json={},
    )
    assert free_started.status_code == 200, free_started.text
    requested_turn = returning_debater.post(
        f"/api/rooms/{ranked_code}/free-turn-requests",
        headers=csrf(returning_debater) | {"X-Idempotency-Key": "round12-neg-free-turn"},
        json={},
    )
    assert requested_turn.status_code == 200, requested_turn.text
    free_finished = owner.post(
        f"/api/rooms/{ranked_code}/speech/finish",
        headers=owner_headers | {"X-Idempotency-Key": f"finish-{free_identity}"},
        json={"speech_id": free_started.json()["speech_id"], "content": free_content},
    )
    assert free_finished.status_code == 200, free_finished.text
    with SessionLocal() as db:
        room = load_room(db, ranked_code, lock=True)
        current = dict(room.template_snapshot[room.current_stage_index])
        current["intermission_deadline_at"] = (now() - timedelta(milliseconds=1)).isoformat()
        snapshot = list(room.template_snapshot)
        snapshot[room.current_stage_index] = current
        room.template_snapshot = snapshot
        db.commit()
    await match_engine.process_room(ranked_code)
    _speak(
        returning_debater,
        ranked_code,
        returning_headers,
        f"《{ranked_topic}》自由辩论反方恢复后发言，证明掉线恢复措施真实可用。",
    )
    with SessionLocal() as db:
        room = load_room(db, ranked_code, lock=True)
        current = dict(room.template_snapshot[room.current_stage_index])
        current["free_stage_remaining_seconds"] = 0
        current["intermission_deadline_at"] = (now() - timedelta(milliseconds=1)).isoformat()
        snapshot = list(room.template_snapshot)
        snapshot[room.current_stage_index] = current
        room.template_snapshot = snapshot
        db.commit()
    await match_engine.process_room(ranked_code)
    await match_engine.process_room(ranked_code)
    with SessionLocal() as db:
        assert load_room(db, ranked_code).current_stage_index == 4
    _speak(
        returning_debater,
        ranked_code,
        returning_headers,
        f"围绕《{ranked_topic}》，反方真人完成恢复后的总结陈词并保留完整历史。",
    )
    await match_engine.process_room(ranked_code)

    # The other controllers retain explicit repair exits: skip sends a stuck
    # judge to review; terminate creates an immutable terminal boundary.
    skipped = skipped_owner.post(
        f"/api/rooms/{skipped_code}/control/skip",
        headers=csrf(skipped_owner) | {"X-Idempotency-Key": "round12-skip-judge"},
        json={"reason": "裁判等待过久，转人工复核"},
    )
    assert skipped.status_code == 200 and skipped.json()["room"]["status"] == "review_required"
    terminated = terminated_owner.post(
        f"/api/rooms/{terminated_code}/control/terminate",
        headers=csrf(terminated_owner) | {"X-Idempotency-Key": "round12-terminate-once"},
        json={"reason": "参赛者设备故障，房主终止"},
    )
    assert terminated.status_code == 200 and terminated.json()["room"]["status"] == "terminated"

    with SessionLocal() as db:
        ranked = load_room(db, ranked_code)
        ranked_match = db.scalar(select(Match).where(Match.room_id == ranked.id))
        ranked_scorecard = db.scalar(select(JudgeScorecard).where(JudgeScorecard.match_id == ranked_match.id))
        skipped_room_db = load_room(db, skipped_code)
        skipped_match = db.scalar(select(Match).where(Match.room_id == skipped_room_db.id))
        terminated_room_db = load_room(db, terminated_code)
        terminated_match = db.scalar(select(Match).where(Match.room_id == terminated_room_db.id))
        assert ranked.status == ranked_match.status == "completed"
        assert ranked_scorecard.status == "approved"
        assert ranked_match.winner == "aff" and ranked_topic in ranked_scorecard.reasoning
        assert skipped_room_db.status == skipped_match.status == "review_required"
        assert terminated_room_db.status == terminated_match.status == "terminated"
        assert (
            db.scalar(
                select(func.count(Speech.id)).where(
                    Speech.room_id.in_([ranked.id, skipped_room_db.id, terminated_room_db.id]),
                    Speech.status.in_(["speaking", "synthesizing", "playing"]),
                )
            )
            == 0
        )
        ranked_match_id = ranked_match.id
        skipped_match_id = skipped_match.id
        terminated_match_id = terminated_match.id
        ranked_competition_id = ranked.competition_id
        owner_id = ranked.owner_id
        returning_id = next(item.user_id for item in ranked.seats if item.seat_key == "neg_1")

        # No generated text, scorecard, or event can cross a room boundary.
        for room, expected_topic in (
            (ranked, ranked_topic),
            (skipped_room_db, skipped_topic),
            (terminated_room_db, terminated_topic),
        ):
            speeches = list(db.scalars(select(Speech).where(Speech.room_id == room.id)).all())
            assert all(
                expected_topic in speech.content for speech in speeches if speech.speaker_type == "ai" and speech.status == "completed"
            )
            events = list(db.scalars(select(MatchEvent).where(MatchEvent.room_id == room.id)).all())
            assert events and all(event.room_id == room.id for event in events)

        owner_entry = db.scalar(
            select(LeaderboardEntry).where(
                LeaderboardEntry.competition_id == ranked_competition_id,
                LeaderboardEntry.user_id == owner_id,
            )
        )
        returning_entry = db.scalar(
            select(LeaderboardEntry).where(
                LeaderboardEntry.competition_id == ranked_competition_id,
                LeaderboardEntry.user_id == returning_id,
            )
        )
        assert owner_entry and owner_entry.matches == 1 and owner_entry.points == 3 and owner_entry.wins == 1
        assert returning_entry and returning_entry.matches == 1 and returning_entry.points == 0 and returning_entry.losses == 1

    # Result, paginated history, personal return path, public leaderboard,
    # immutable archives, and an idempotent rematch all agree on the terminal
    # state produced above.
    room_result = owner.get(f"/api/rooms/{ranked_code}/result?speech_page_size=3&event_page_size=5")
    assert room_result.status_code == 200, room_result.text
    assert room_result.json()["match"]["id"] == ranked_match_id
    assert room_result.json()["speech_pagination"]["has_more"] is True
    history = owner.get(f"/api/matches/{ranked_match_id}/history?speech_page_size=2")
    assert history.status_code == 200 and history.json()["speech_pagination"]["has_more"] is True
    result = client.get(f"/api/matches/{ranked_match_id}/result")
    assert result.status_code == 200 and result.json()["match"]["winner"] == "aff"
    personal = owner.get("/api/me")
    assert personal.status_code == 200
    assert any(item["match_id"] == ranked_match_id and item["status"] == "completed" for item in personal.json()["history"])
    rankings = client.get("/api/rankings?competition_slug=daily-4v4")
    assert rankings.status_code == 200
    by_id = {item["user_id"]: item for item in rankings.json()["items"]}
    assert by_id[owner_id]["points"] == 3 and by_id[returning_id]["matches"] == 1
    assert {ranked_match_id, skipped_match_id, terminated_match_id}.issubset(set(queued_archives))

    for match_id, participant in (
        (ranked_match_id, owner),
        (skipped_match_id, skipped_owner),
        (terminated_match_id, terminated_owner),
    ):
        archive = build_match_archive(match_id)
        assert archive.reused is False and archive.sha256 and archive.source_sha256
        downloaded = participant.get(f"/api/matches/{match_id}/archive")
        assert downloaded.status_code == 200, downloaded.text
        assert downloaded.headers["x-archive-projection"] == "participant"
        assert downloaded.headers["x-archive-sha256"] == hashlib.sha256(downloaded.content).hexdigest()
        assert downloaded.headers["x-archive-sha256"] != archive.sha256
        assert downloaded.headers["x-archive-source-sha256"] == archive.source_sha256

    rematch_headers = csrf(owner) | {"X-Idempotency-Key": "round12-rematch-once"}
    rematch = owner.post(f"/api/rooms/{ranked_code}/rematch", headers=rematch_headers, json={})
    rematch_replay = owner.post(f"/api/rooms/{ranked_code}/rematch", headers=rematch_headers, json={})
    assert rematch.status_code == 200, rematch.text
    assert rematch_replay.status_code == 200 and rematch_replay.json()["replayed"] is True
    assert rematch.json()["room"]["code"] != ranked_code
    assert rematch.json()["room"]["topic"] == ranked_topic
