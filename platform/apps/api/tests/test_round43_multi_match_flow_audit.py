from __future__ import annotations

import asyncio
import hashlib

import pytest
from app.core.database import SessionLocal
from app.models.entities import Competition, CompetitionTopic, JudgeScorecard, Match, MatchEvent, Speech
from app.services.match_engine import match_engine
from app.services.providers import ProviderError, debate_agent, judge_provider, lighttts
from app.services.room_service import load_room
from conftest import csrf
from sqlalchemy import select


def _create_room(owner, *, competition_slug: str, topic: str, seat_key: str = "aff_1") -> dict:
    payload: dict[str, object] = {
        "competition_slug": competition_slug,
        "seat_key": seat_key,
        "visibility": "public",
    }
    if competition_slug == "training-1v1":
        payload["custom_topic"] = topic
    else:
        with SessionLocal() as db:
            competition = db.scalar(select(Competition).where(Competition.slug == competition_slug))
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
    room = response.json()["room"]
    ready = owner.post(f"/api/rooms/{room['code']}/ready", headers=csrf(owner), json={"ready": True})
    assert ready.status_code == 200, ready.text
    started = owner.post(f"/api/rooms/{room['code']}/start", headers=csrf(owner), json={})
    assert started.status_code == 200, started.text
    return room


def _install_template(code: str, template: list[dict], *, all_ai: bool = False) -> None:
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.template_snapshot = template
        if all_ai:
            for seat in room.seats:
                seat.occupant_type = "ai"
                seat.user_id = None
                seat.display_name = f"AI-{code}-{seat.seat_key}"
        db.commit()


def _lease(owner, code: str) -> dict[str, str]:
    headers = csrf(owner) | {"X-Control-Lease": f"round43-device-{code}"}
    acquired = owner.post(f"/api/rooms/{code}/control-lease", headers=headers, json={})
    assert acquired.status_code == 200, acquired.text
    return headers


def _speak(owner, code: str, headers: dict[str, str], content: str) -> None:
    key = hashlib.sha256(content.encode()).hexdigest()[:16]
    started = owner.post(
        f"/api/rooms/{code}/speech/start",
        headers=headers | {"X-Idempotency-Key": f"round43-start-{key}"},
        json={},
    )
    assert started.status_code == 200, started.text
    finished = owner.post(
        f"/api/rooms/{code}/speech/finish",
        headers=headers | {"X-Idempotency-Key": f"round43-finish-{key}"},
        json={"speech_id": started.json()["speech_id"], "content": content},
    )
    assert finished.status_code == 200, finished.text


@pytest.mark.asyncio
async def test_four_concurrent_1v1_and_4v4_matches_cover_repair_controls_and_never_cross_rooms(
    client,
    register_user,
    monkeypatch,
) -> None:
    """One authoritative mixed-competition simulation for the production limit.

    The four rooms deliberately end differently: a normal 1v1 result, a 4v4
    result after an Agent retry, a judge-stage skip to manual review, and an
    emergency termination.  This complements the randomized invariant tests
    with one readable end-to-end control and isolation scenario.
    """

    owners = [register_user(f"round43_owner_{index}") for index in range(4)]
    topics = {
        "one": "Round43 一对一真人与智能体完整赛",
        "four": "Round43 四对四多人多智能体重试赛",
        "review": "Round43 四对四裁判人工接管赛",
        "terminate": "Round43 一对一紧急终止赛",
    }
    rooms = {
        "one": _create_room(owners[0], competition_slug="training-1v1", topic=topics["one"]),
        "four": _create_room(owners[1], competition_slug="daily-4v4", topic=topics["four"]),
        "review": _create_room(owners[2], competition_slug="daily-4v4", topic=topics["review"]),
        "terminate": _create_room(owners[3], competition_slug="training-1v1", topic=topics["terminate"]),
    }
    codes = {key: room["code"] for key, room in rooms.items()}

    _install_template(
        codes["one"],
        [
            {"key": "human_case", "name": "正方真人立论", "kind": "speech", "seat": "aff_1", "duration": 60},
            {"key": "ai_reply", "name": "反方智能体回应", "kind": "speech", "seat": "neg_1", "duration": 60},
            {"key": "judge", "name": "AI 裁判", "kind": "judging", "duration": 20},
        ],
    )
    _install_template(
        codes["four"],
        [
            {"key": "aff_second", "name": "正方二辩立论", "kind": "speech", "seat": "aff_2", "duration": 60},
            {"key": "neg_second", "name": "反方二辩驳论", "kind": "speech", "seat": "neg_2", "duration": 60},
            {"key": "aff_owner", "name": "正方一辩总结", "kind": "speech", "seat": "aff_1", "duration": 60},
            {"key": "neg_fourth", "name": "反方四辩总结", "kind": "speech", "seat": "neg_4", "duration": 60},
            {"key": "judge", "name": "AI 裁判", "kind": "judging", "duration": 20},
        ],
    )
    _install_template(
        codes["review"],
        [
            {"key": "aff_fourth", "name": "正方四辩总结", "kind": "speech", "seat": "aff_4", "duration": 60},
            {"key": "neg_fourth", "name": "反方四辩总结", "kind": "speech", "seat": "neg_4", "duration": 60},
            {"key": "judge", "name": "AI 裁判", "kind": "judging", "duration": 20},
        ],
        all_ai=True,
    )
    _install_template(
        codes["terminate"],
        [{"key": "human_case", "name": "终止前真人立论", "kind": "speech", "seat": "aff_1", "duration": 60}],
    )

    agent_attempts: dict[tuple[str, str], int] = {}
    judge_inputs: dict[str, list[dict]] = {}

    async def generated(payload, **_kwargs):
        identity = (payload["room_code"], payload["current_stage"])
        agent_attempts[identity] = agent_attempts.get(identity, 0) + 1
        if identity == (codes["four"], "正方二辩立论") and agent_attempts[identity] == 1:
            raise ProviderError("Round43 模拟 Agent 暂时不可用", code="agent_timeout", retryable=True)
        assert payload["match_id"] and payload["task_id"]
        return f"房间{payload['room_code']}围绕《{payload['debate_topic']}》完成{payload['current_stage']}。"

    async def synthesized(*_args, **_kwargs):
        return ""

    async def judged(topic, speeches, **_kwargs):
        judge_inputs[topic] = list(speeches)
        assert speeches
        assert all(topic in item["content"] for item in speeches)
        return {
            "winner": "aff",
            "affirmative_score": 90,
            "negative_score": 84,
            "individual_scores": {},
            "reasoning": f"《{topic}》的独立裁判结论。",
        }

    monkeypatch.setattr(debate_agent, "generate", generated)
    monkeypatch.setattr(lighttts, "synthesize", synthesized)
    monkeypatch.setattr(judge_provider, "judge", judged)

    # All four leave preparation together. The owner can pause one room while
    # the other three remain independently actionable.
    await asyncio.gather(*(match_engine.process_room(code) for code in codes.values()))
    paused = owners[0].post(
        f"/api/rooms/{codes['one']}/control/pause",
        headers=csrf(owners[0]) | {"X-Idempotency-Key": "round43-pause-one"},
        json={"reason": "真人开麦前检查设备"},
    )
    assert paused.status_code == 200, paused.text
    terminated = owners[3].post(
        f"/api/rooms/{codes['terminate']}/control/terminate",
        headers=csrf(owners[3]) | {"X-Idempotency-Key": "round43-terminate"},
        json={"reason": "参赛设备无法恢复"},
    )
    assert terminated.status_code == 200, terminated.text

    # While the 1v1 room is paused, the 4v4 Agent failure pauses only its own
    # room and the review room advances one stage.
    await asyncio.gather(*(match_engine.process_room(code) for code in codes.values()))
    with SessionLocal() as db:
        assert load_room(db, codes["one"]).status == "paused"
        failed_four = load_room(db, codes["four"])
        assert failed_four.status == "paused" and "暂时不可用" in failed_four.failure_reason
        assert load_room(db, codes["review"]).current_stage_index == 1
        assert load_room(db, codes["terminate"]).status == "terminated"

    resumed = owners[0].post(
        f"/api/rooms/{codes['one']}/control/resume",
        headers=csrf(owners[0]) | {"X-Idempotency-Key": "round43-resume-one"},
        json={"reason": "设备检查通过"},
    )
    retried = owners[1].post(
        f"/api/rooms/{codes['four']}/control/retry",
        headers=csrf(owners[1]) | {"X-Idempotency-Key": "round43-retry-four"},
        json={"reason": "Agent 服务已恢复"},
    )
    assert resumed.status_code == 200, resumed.text
    assert retried.status_code == 200, retried.text

    one_headers = _lease(owners[0], codes["one"])
    _speak(owners[0], codes["one"], one_headers, f"围绕《{topics['one']}》，真人完成可追溯立论。")

    four_headers = _lease(owners[1], codes["four"])
    review_skipped = False
    for _ in range(10):
        active_codes: list[str] = []
        with SessionLocal() as db:
            for key in ("one", "four", "review"):
                room = load_room(db, codes[key])
                if room.status in {"preparing", "running", "judging"}:
                    active_codes.append(room.code)
        if active_codes:
            await asyncio.gather(*(match_engine.process_room(code) for code in active_codes))

        with SessionLocal() as db:
            four_room = load_room(db, codes["four"])
            four_stage = (
                four_room.template_snapshot[four_room.current_stage_index]
                if four_room.status == "running" and four_room.current_stage_index < len(four_room.template_snapshot)
                else {}
            )
            review_room = load_room(db, codes["review"])
            should_speak_four = four_stage.get("seat") == "aff_1" and four_stage.get("kind") == "speech"
            should_skip_review = review_room.status == "judging" and not review_skipped

        if should_speak_four:
            _speak(
                owners[1],
                codes["four"],
                four_headers,
                f"围绕《{topics['four']}》，真人一辩完成总结。",
            )
        if should_skip_review:
            skipped = owners[2].post(
                f"/api/rooms/{codes['review']}/control/skip",
                headers=csrf(owners[2]) | {"X-Idempotency-Key": "round43-skip-review"},
                json={"reason": "裁判超时，转管理员复核"},
            )
            assert skipped.status_code == 200, skipped.text
            assert skipped.json()["room"]["status"] == "review_required"
            review_skipped = True

        with SessionLocal() as db:
            statuses = {key: load_room(db, code).status for key, code in codes.items()}
        if statuses == {
            "one": "completed",
            "four": "completed",
            "review": "review_required",
            "terminate": "terminated",
        }:
            break

    assert statuses == {
        "one": "completed",
        "four": "completed",
        "review": "review_required",
        "terminate": "terminated",
    }

    # Results and append-only events remain attributable to exactly one room.
    with SessionLocal() as db:
        room_ids: set[str] = set()
        for key, code in codes.items():
            room = load_room(db, code)
            room_ids.add(room.id)
            match = db.scalar(select(Match).where(Match.room_id == room.id))
            assert match is not None and match.status == room.status
            events = list(db.scalars(select(MatchEvent).where(MatchEvent.room_id == room.id).order_by(MatchEvent.seq)).all())
            assert [event.seq for event in events] == list(range(1, room.seq + 1))
            assert all(event.room_id == room.id and event.match_id in {None, match.id} for event in events)
            assert not db.scalar(
                select(Speech).where(
                    Speech.room_id == room.id,
                    Speech.status.in_(["speaking", "synthesizing", "playing"]),
                )
            )
            for speech in db.scalars(select(Speech).where(Speech.room_id == room.id, Speech.status == "completed")):
                assert topics[key] in speech.content
        assert len(room_ids) == 4

        for key in ("one", "four"):
            room = load_room(db, codes[key])
            match = db.scalar(select(Match).where(Match.room_id == room.id))
            scorecard = db.scalar(select(JudgeScorecard).where(JudgeScorecard.match_id == match.id))
            assert scorecard and scorecard.status == "approved" and topics[key] in scorecard.reasoning

    assert set(judge_inputs) == {topics["one"], topics["four"]}
    for topic, speeches in judge_inputs.items():
        assert speeches and all(topic in item["content"] for item in speeches)
    assert agent_attempts[(codes["four"], "正方二辩立论")] == 2

    for key, owner in zip(("one", "four", "review", "terminate"), owners):
        result = owner.get(f"/api/rooms/{codes[key]}/result")
        assert result.status_code == 200, result.text
        assert result.json()["match"]["status"] == statuses[key]
