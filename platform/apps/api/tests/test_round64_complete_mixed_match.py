from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import timedelta

import pytest
from app.core.config import settings
from app.core.database import SessionLocal
from app.models.entities import AudioCue, JudgeScorecard, Match, MatchEvent, Speech
from app.services.match_engine import match_engine
from app.services.providers import ProviderError, debate_agent, judge_provider, lighttts
from app.services.room_service import load_room, now, stage
from app.services.seed import DAILY_STAGES
from conftest import csrf
from sqlalchemy import select
from test_platform import _wav_bytes


def _create_daily_room(owner, client, topic_index: int) -> str:
    competition = client.get("/api/competitions/daily-4v4").json()["competition"]
    response = owner.post(
        "/api/rooms",
        headers=csrf(owner),
        json={
            "competition_slug": "daily-4v4",
            "topic_id": competition["topics"][topic_index]["id"],
            "seat_key": "aff_1",
            "visibility": "public",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["room"]["code"]


def _ready(browser, code: str) -> None:
    response = browser.post(f"/api/rooms/{code}/ready", headers=csrf(browser), json={"ready": True})
    assert response.status_code == 200, response.text


def _lease(browser, code: str, device: str) -> dict[str, str]:
    headers = csrf(browser) | {"X-Control-Lease": device}
    response = browser.post(f"/api/rooms/{code}/control-lease", headers=headers, json={})
    assert response.status_code == 200, response.text
    return headers


def _finish_human_speech(browser, code: str, headers: dict[str, str], content: str) -> str:
    suffix = hashlib.sha256(content.encode()).hexdigest()[:16]
    started = browser.post(
        f"/api/rooms/{code}/speech/start",
        headers=headers | {"X-Idempotency-Key": f"round64-start-{suffix}"},
        json={},
    )
    assert started.status_code == 200, started.text
    speech_id = started.json()["speech_id"]
    finished = browser.post(
        f"/api/rooms/{code}/speech/finish",
        headers=headers | {"X-Idempotency-Key": f"round64-finish-{suffix}"},
        json={"speech_id": speech_id, "content": content},
    )
    assert finished.status_code == 200, finished.text
    return speech_id


def _finish_free_turn(
    speaker,
    code: str,
    headers: dict[str, str],
    content: str,
    *,
    requester=None,
    request_index: int = 0,
) -> str:
    suffix = hashlib.sha256(content.encode()).hexdigest()[:16]
    started = speaker.post(
        f"/api/rooms/{code}/speech/start",
        headers=headers | {"X-Idempotency-Key": f"round64-free-start-{suffix}"},
        json={},
    )
    assert started.status_code == 200, started.text
    speech_id = started.json()["speech_id"]
    if requester is not None:
        requested = requester.post(
            f"/api/rooms/{code}/free-turn-requests",
            headers=csrf(requester) | {"X-Idempotency-Key": f"round64-free-request-{request_index}"},
            json={},
        )
        assert requested.status_code == 200, requested.text
    finished = speaker.post(
        f"/api/rooms/{code}/speech/finish",
        headers=headers | {"X-Idempotency-Key": f"round64-free-finish-{suffix}"},
        json={"speech_id": speech_id, "content": content},
    )
    assert finished.status_code == 200, finished.text
    return speech_id


def _short_daily_template(prefix: str) -> list[dict]:
    result = deepcopy(DAILY_STAGES)
    for index, item in enumerate(result):
        item["key"] = f"{prefix}_{index}_{item['key']}"
        if item["kind"] == "announcement":
            item["duration"] = 1
        elif item["kind"] == "free":
            item["duration"] = 16
            item["turn_duration"] = 3
        elif item["kind"] == "judging":
            item["duration"] = 1
        else:
            item["duration"] = 8
    return result


def _install_preset_cues(code: str, template: list[dict]) -> list:
    paths = []
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.template_snapshot = template
        for item in template:
            cue_text = str(item.get("cue") or "").strip()
            if not cue_text:
                continue
            filename = f"{item['key']}.wav"
            path = settings.media_path / "_cues" / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(_wav_bytes(0.08))
            paths.append(path)
            db.add(
                AudioCue(
                    key=item["key"],
                    name=f"Round64 {item['name']}",
                    text=cue_text,
                    audio_url=f"/media/_cues/{filename}",
                    is_active=True,
                )
            )
        db.commit()
    return paths


def _expire_host_prompt(code: str) -> None:
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        current = dict(stage(room) or {})
        assert current.get("host_announcement_pending") is True, current
        current["host_announcement_deadline_at"] = (now() - timedelta(milliseconds=1)).isoformat()
        snapshot = list(room.template_snapshot)
        snapshot[room.current_stage_index] = current
        room.template_snapshot = snapshot
        room.stage_deadline_at = now() - timedelta(milliseconds=1)
        db.commit()


async def _select_next_free_human(code: str, turn_index: int) -> None:
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        current = dict(stage(room) or {})
        assert current.get("kind") == "free", {
            "status": room.status,
            "stage_index": room.current_stage_index,
            "stage": current,
            "deadline": room.stage_deadline_at,
            "turn_index": turn_index,
        }
        current["intermission_deadline_at"] = (now() - timedelta(milliseconds=1)).isoformat()
        snapshot = list(room.template_snapshot)
        snapshot[room.current_stage_index] = current
        room.template_snapshot = snapshot
        db.commit()
    await match_engine.process_room(code)


@pytest.mark.asyncio
async def test_two_humans_six_agents_complete_4v4_with_faults_and_room_isolation(
    client,
    register_user,
    monkeypatch,
) -> None:
    """Complete the production 4v4 topology under recoverable real-world faults.

    The main room contains exactly two humans and six permanent Agent seats.
    It must survive one Agent timeout and one 61-second human disconnect,
    complete four alternating free-debate turns, reach judging, and preserve a
    full text-only audit trail.  A second live room proves that room state,
    generated content, events, retries, and results never leak across rooms.
    """

    owner = register_user("round64_aff_one")
    opponent = register_user("round64_neg_two")
    isolation_owner = register_user("round64_isolation_owner")
    main_code = _create_daily_room(owner, client, 0)
    isolation_code = _create_daily_room(isolation_owner, client, 1)

    claimed = opponent.post(
        f"/api/rooms/{main_code}/claim-seat",
        headers=csrf(opponent),
        json={"seat_key": "neg_2"},
    )
    assert claimed.status_code == 200, claimed.text
    for browser, code in ((owner, main_code), (opponent, main_code), (isolation_owner, isolation_code)):
        _ready(browser, code)

    main_template = _short_daily_template("round64_main")
    isolation_template = [
        {
            "key": "round64_isolation_neg",
            "name": "隔离房间反方智能体立论",
            "kind": "speech",
            "seat": "neg_1",
            "duration": 8,
        },
        {"key": "round64_isolation_judge", "name": "隔离房间裁判", "kind": "judging", "duration": 1},
    ]
    cue_paths = _install_preset_cues(main_code, main_template)
    with SessionLocal() as db:
        isolation_room = load_room(db, isolation_code, lock=True)
        isolation_room.template_snapshot = isolation_template
        for room in (load_room(db, main_code, lock=True), isolation_room):
            for seat in room.seats:
                if seat.occupant_type == "human":
                    seat.connected = True
                    seat.disconnected_at = None
        db.commit()

    monkeypatch.setattr(settings, "host_cues_preset_only", True)
    for browser, code, operation in (
        (owner, main_code, "round64-main-start"),
        (isolation_owner, isolation_code, "round64-isolation-start"),
    ):
        started = browser.post(
            f"/api/rooms/{code}/start",
            headers=csrf(browser) | {"X-Idempotency-Key": operation},
            json={},
        )
        assert started.status_code == 200, started.text

    # Materialize every reusable host cue before accelerated test-stage
    # transitions. Production does the same work in the background while the
    # opening plays; the test intentionally advances faster than wall time.
    with SessionLocal() as db:
        assert await match_engine._prepare_cues(
            db,
            main_code,
            stage_keys={item["key"] for item in main_template},
            required=True,
        ) is True

    with SessionLocal() as db:
        main_room = load_room(db, main_code)
        assert {seat.seat_key for seat in main_room.seats if seat.occupant_type == "human"} == {"aff_1", "neg_2"}
        assert {seat.seat_key for seat in main_room.seats if seat.occupant_type == "ai"} == {
            "aff_2",
            "aff_3",
            "aff_4",
            "neg_1",
            "neg_3",
            "neg_4",
        }

    attempts: defaultdict[tuple[str, str], int] = defaultdict(int)
    generated_payloads: list[dict] = []
    judge_calls: dict[str, list[dict]] = {}

    async def generated(payload, **_kwargs):
        generated_payloads.append(dict(payload))
        identity = (payload["room_code"], payload["current_stage"])
        attempts[identity] += 1
        if payload["room_code"] == main_code and payload["current_stage"] == "反方一辩立论" and attempts[identity] == 1:
            raise ProviderError("Round64 模拟单次 Agent 超时", code="agent_timeout", retryable=True)
        return (
            f"房间{payload['room_code']}围绕《{payload['debate_topic']}》完成"
            f"{payload['current_stage']}，给出独立、完整且可追溯的论证。"
        )

    async def synthesized(text, *, room_code: str, **_kwargs):
        assert room_code in {main_code, isolation_code}
        assert text.strip()
        return ""

    async def judged(topic, speeches, **_kwargs):
        assert speeches
        contents = [str(item.get("content") or "") for item in speeches]
        if any(main_code in content for content in contents):
            assert not any(isolation_code in content for content in contents)
        if any(isolation_code in content for content in contents):
            assert not any(main_code in content for content in contents)
        judge_calls[topic] = [dict(item) for item in speeches]
        return {
            "winner": "aff",
            "affirmative_score": 90,
            "negative_score": 87,
            "individual_scores": {},
            "reasoning": f"《{topic}》已依据本房间完整文字记录独立裁决。",
        }

    monkeypatch.setattr(debate_agent, "generate", generated)
    monkeypatch.setattr(lighttts, "synthesize", synthesized)
    monkeypatch.setattr(judge_provider, "judge", judged)
    monkeypatch.setattr(match_engine, "_schedule_next_agent_prefetch", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(match_engine, "_schedule_hosted_stage_agent_prefetch", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(match_engine, "_schedule_remaining_cue_prefetch", lambda *_args, **_kwargs: None)

    # The independent room completes while the main room is still preparing.
    # Its immutable terminal snapshot becomes the cross-room contamination canary.
    for _ in range(4):
        await match_engine.process_room(isolation_code)
        with SessionLocal() as db:
            if load_room(db, isolation_code).status == "completed":
                break
    with SessionLocal() as db:
        isolated = load_room(db, isolation_code)
        assert isolated.status == "completed"
        isolation_seq = isolated.seq
        isolation_match = db.scalar(select(Match).where(Match.room_id == isolated.id))
        isolation_speeches = list(db.scalars(select(Speech).where(Speech.room_id == isolated.id)).all())
        assert isolation_match and len(isolation_speeches) == 1
        assert isolation_code in isolation_speeches[0].content
        isolation_result = db.scalar(select(JudgeScorecard).where(JudgeScorecard.match_id == isolation_match.id))
        assert isolation_result and isolation_result.status == "approved"

    owner_headers = _lease(owner, main_code, "round64-aff-one-device")
    opponent_headers = _lease(opponent, main_code, "round64-neg-two-device")
    await match_engine.process_room(main_code)
    with SessionLocal() as db:
        room = load_room(db, main_code, lock=True)
        assert stage(room)["kind"] == "announcement"
        room.stage_deadline_at = now() - timedelta(milliseconds=1)
        db.commit()
    await match_engine.process_room(main_code)

    timeout_recovered = False
    disconnect_recovered = False
    free_turns: list[str] = []
    loop_guard = 0
    while loop_guard < 90:
        loop_guard += 1
        with SessionLocal() as db:
            room = load_room(db, main_code)
            current = dict(stage(room) or {})
            status = room.status
        if status == "completed":
            break

        if status == "paused" and "Agent 超时" in room.failure_reason:
            retry = owner.post(
                f"/api/rooms/{main_code}/control/retry",
                headers=csrf(owner) | {"X-Idempotency-Key": "round64-agent-retry"},
                json={"reason": "Agent 服务已恢复，重试当前阶段"},
            )
            assert retry.status_code == 200, retry.text
            timeout_recovered = True
            continue

        assert status in {"running", "judging"}, (status, current, room.failure_reason, loop_guard)
        if current.get("host_announcement_pending"):
            _expire_host_prompt(main_code)
            await match_engine.process_room(main_code)
            continue
        if current.get("kind") == "judging":
            await match_engine.process_room(main_code)
            continue
        if current.get("kind") == "speech":
            seat_key = str(current["seat"])
            if seat_key == "aff_1":
                _finish_human_speech(
                    owner,
                    main_code,
                    owner_headers,
                    f"《{room.topic}》正方一辩明确判准并完成完整立论。",
                )
            elif seat_key == "neg_2":
                if not disconnect_recovered:
                    with SessionLocal() as db:
                        locked = load_room(db, main_code, lock=True)
                        human = next(item for item in locked.seats if item.seat_key == "neg_2")
                        human.connected = False
                        human.disconnected_at = now() - timedelta(seconds=61)
                        db.commit()
                    await match_engine.process_room(main_code)
                    with SessionLocal() as db:
                        locked = load_room(db, main_code, lock=True)
                        human = next(item for item in locked.seats if item.seat_key == "neg_2")
                        assert locked.status == "paused"
                        assert human.occupant_type == "human" and human.user_id
                        assert locked.owner_id == owner.get("/api/me").json()["user"]["id"]
                        human.connected = True
                        human.disconnected_at = None
                        db.commit()
                    resumed = owner.post(
                        f"/api/rooms/{main_code}/control/resume",
                        headers=csrf(owner) | {"X-Idempotency-Key": "round64-disconnect-resume"},
                        json={"reason": "真人辩手已重新连接"},
                    )
                    assert resumed.status_code == 200, resumed.text
                    disconnect_recovered = True
                _finish_human_speech(
                    opponent,
                    main_code,
                    opponent_headers,
                    f"《{room.topic}》反方二辩逐项回应正方并完成驳论。",
                )
            else:
                await match_engine.process_room(main_code)
            continue
        if current.get("kind") == "free":
            assert current.get("free_stage_remaining_seconds") == 16, current
            assert current.get("awaiting_human_start") is True, current
            turns = [
                (owner, owner_headers, opponent, "正方自由辩论第一轮，比较双方方案的现实效果。"),
                (opponent, opponent_headers, owner, "反方自由辩论第一轮，回应正方因果链条。"),
                (owner, owner_headers, opponent, "正方自由辩论第二轮，补充执行成本与长期影响。"),
                (opponent, opponent_headers, None, "反方自由辩论第二轮，完成最后反驳并收束分歧。"),
            ]
            for index, (speaker, headers, requester, content) in enumerate(turns):
                speech_id = _finish_free_turn(
                    speaker,
                    main_code,
                    headers,
                    content,
                    requester=requester,
                    request_index=index,
                )
                free_turns.append(speech_id)
                if requester is not None:
                    await _select_next_free_human(main_code, index)
            with SessionLocal() as db:
                locked = load_room(db, main_code, lock=True)
                free_stage = dict(stage(locked) or {})
                free_stage["free_stage_remaining_seconds"] = 0
                free_stage["intermission_deadline_at"] = (now() - timedelta(milliseconds=1)).isoformat()
                snapshot = list(locked.template_snapshot)
                snapshot[locked.current_stage_index] = free_stage
                locked.template_snapshot = snapshot
                db.commit()
            await match_engine.process_room(main_code)
            await match_engine.process_room(main_code)
            continue
        raise AssertionError((status, current, loop_guard))

    assert loop_guard < 90
    assert timeout_recovered and disconnect_recovered
    assert len(free_turns) == 4

    with SessionLocal() as db:
        main_room = load_room(db, main_code)
        main_match = db.scalar(select(Match).where(Match.room_id == main_room.id))
        main_speeches = list(
            db.scalars(select(Speech).where(Speech.room_id == main_room.id).order_by(Speech.created_at, Speech.id)).all()
        )
        main_events = list(
            db.scalars(select(MatchEvent).where(MatchEvent.room_id == main_room.id).order_by(MatchEvent.seq)).all()
        )
        scorecard = db.scalar(select(JudgeScorecard).where(JudgeScorecard.match_id == main_match.id))
        assert main_room.status == main_match.status == "completed"
        assert scorecard and scorecard.status == "approved" and scorecard.winner == "aff"
        assert len([item for item in main_speeches if item.status == "completed"]) == 12, [
            Counter((item.stage_key, item.status) for item in main_speeches),
            Counter((item.speaker_type, item.status) for item in main_speeches),
        ]
        assert {item.seat_key for item in main_speeches if item.speaker_type == "ai"} == {
            "aff_2",
            "aff_3",
            "aff_4",
            "neg_1",
            "neg_3",
            "neg_4",
        }
        assert len([item for item in main_speeches if item.speaker_type == "human"]) == 6
        assert all(item.content.strip() for item in main_speeches if item.status == "completed")
        assert [item.seq for item in main_events] == list(range(1, main_room.seq + 1))
        event_types = [item.event_type for item in main_events]
        assert event_types.count("provider.failed") == 1
        assert event_types.count("participant.disconnect_timeout") == 1
        assert "seat.ai_substituted" not in event_types
        assert not db.scalar(
            select(Speech.id).where(
                Speech.room_id == main_room.id,
                Speech.status.in_(["speaking", "synthesizing", "playing"]),
            )
        )

        isolated = load_room(db, isolation_code)
        isolation_events = list(
            db.scalars(select(MatchEvent).where(MatchEvent.room_id == isolated.id).order_by(MatchEvent.seq)).all()
        )
        assert isolated.status == "completed" and isolated.seq == isolation_seq
        assert len(isolation_events) == isolation_seq
        assert not any(event.event_type in {"provider.failed", "participant.disconnect_timeout"} for event in isolation_events)

    main_topic = owner.get(f"/api/rooms/{main_code}").json()["room"]["topic"]
    isolation_topic = isolation_owner.get(f"/api/rooms/{isolation_code}").json()["room"]["topic"]
    assert main_topic != isolation_topic
    assert len(judge_calls[main_topic]) == 12
    assert len(judge_calls[isolation_topic]) == 1
    assert all(payload["room_code"] in {main_code, isolation_code} for payload in generated_payloads)
    assert attempts[(main_code, "反方一辩立论")] == 2
    assert attempts[(isolation_code, "隔离房间反方智能体立论")] == 1

    for path in cue_paths:
        path.unlink(missing_ok=True)
