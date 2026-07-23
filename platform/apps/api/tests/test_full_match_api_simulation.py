from __future__ import annotations

from datetime import timedelta

import pytest
from app.api import realtime as realtime_api
from app.core.database import SessionLocal
from app.models.entities import JudgeScorecard, Match, MatchEvent, Speech
from app.services.match_engine import match_engine
from app.services.providers import debate_agent, judge_provider, lighttts
from app.services.room_service import load_room, now, stage
from conftest import csrf
from sqlalchemy import select
from test_platform import create_training_room


def _ready(browser, code: str) -> None:
    response = browser.post(f"/api/rooms/{code}/ready", headers=csrf(browser), json={"ready": True})
    assert response.status_code == 200, response.text


def _start(browser, code: str) -> None:
    response = browser.post(f"/api/rooms/{code}/start", headers=csrf(browser), json={})
    assert response.status_code == 200, response.text


def _lease(browser, code: str, key: str) -> dict[str, str]:
    headers = csrf(browser) | {"X-Control-Lease": key}
    response = browser.post(f"/api/rooms/{code}/control-lease", headers=headers, json={})
    assert response.status_code == 200, response.text
    return headers


def _speak(browser, code: str, headers: dict[str, str], content: str) -> str:
    started = browser.post(f"/api/rooms/{code}/speech/start", headers=headers, json={})
    assert started.status_code == 200, started.text
    speech_id = started.json()["speech_id"]
    finished = browser.post(
        f"/api/rooms/{code}/speech/finish",
        headers=headers,
        json={"speech_id": speech_id, "content": content},
    )
    assert finished.status_code == 200, finished.text
    return speech_id


def _short_template(*, free: bool = True) -> list[dict]:
    stages = [
        {"key": "opening", "name": "开场", "kind": "announcement", "duration": 1},
        {"key": "aff_case", "name": "正方立论", "kind": "speech", "seat": "aff_1", "duration": 3},
        {"key": "neg_case", "name": "反方立论", "kind": "speech", "seat": "neg_1", "duration": 3},
    ]
    if free:
        stages.append(
            {
                "key": "free",
                "name": "自由辩论",
                "kind": "free",
                "side": "aff",
                "duration": 8,
                "turn_duration": 2,
            }
        )
    stages.extend(
        [
            {"key": "neg_summary", "name": "反方总结", "kind": "speech", "seat": "neg_1", "duration": 3},
            {"key": "aff_summary", "name": "正方总结", "kind": "speech", "seat": "aff_1", "duration": 3},
            {"key": "judge", "name": "裁判评议", "kind": "judging", "duration": 1},
        ]
    )
    return stages


def _set_stage_expired(code: str) -> None:
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.stage_started_at = now() - timedelta(seconds=10)
        room.stage_deadline_at = now() - timedelta(seconds=1)
        db.commit()


@pytest.mark.asyncio
async def test_complete_human_vs_human_flow_reaches_result_without_manual_stage_mutation(register_user, monkeypatch) -> None:
    """Exercise the authoritative API path for a complete two-human match."""

    owner = register_user("full_flow_hh_owner")
    opponent = register_user("full_flow_hh_opponent")
    room = create_training_room(owner, "完整双真人流程")
    code = room["code"]
    claimed = opponent.post(f"/api/rooms/{code}/claim-seat", headers=csrf(opponent), json={"seat_key": "neg_1"})
    assert claimed.status_code == 200, claimed.text
    _ready(owner, code)
    _ready(opponent, code)
    _start(owner, code)
    with SessionLocal() as db:
        current = load_room(db, code, lock=True)
        current.template_snapshot = _short_template()
        db.commit()

    # The opening announcement has no cue and is therefore purely a timer. It
    # must advance once expired, without waiting for an unexplained preparation
    # or host action.
    await match_engine.process_room(code)

    # A manual pause/resume must preserve the authoritative stage rather than
    # skip it or create a second timer. This is intentionally exercised before
    # any speech exists so that the remainder of the match proves recovery from
    # a real control boundary.
    paused = owner.post(
        f"/api/rooms/{code}/control/pause",
        headers=csrf(owner),
        json={"reason": "Round54 双真人全流程暂停检查"},
    )
    assert paused.status_code == 200, paused.text
    assert paused.json()["room"]["status"] == "paused"
    resumed = owner.post(
        f"/api/rooms/{code}/control/resume",
        headers=csrf(owner),
        json={"reason": "Round54 双真人全流程恢复检查"},
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["room"]["status"] == "running"

    # A human participant returning inside the 60-second grace period must not
    # pause the match, release the seat, or replace the human with an AI. The
    # WebSocket verifier separately covers the real presence event transport;
    # this API integration test covers the engine's persisted timeout boundary.
    with SessionLocal() as db:
        current = load_room(db, code, lock=True)
        disconnected = next(item for item in current.seats if item.user_id != current.owner_id)
        disconnected.connected = False
        disconnected.disconnected_at = now() - timedelta(seconds=30)
        db.commit()
    await match_engine.process_room(code)
    with SessionLocal() as db:
        current = load_room(db, code, lock=True)
        assert current.status == "running"
        disconnected = next(item for item in current.seats if item.user_id != current.owner_id)
        assert disconnected.occupant_type == "human"
        disconnected.connected = True
        disconnected.disconnected_at = None
        db.commit()

    _set_stage_expired(code)
    await match_engine.process_room(code)

    owner_headers = _lease(owner, code, "full-hh-owner")
    opponent_headers = _lease(opponent, code, "full-hh-opponent")
    first_speech_id = _speak(owner, code, owner_headers, "正方完成立论并给出清晰论据。")
    # Once the speech has been authoritatively finished, a racing ASR final is
    # stale and must not mutate either the speech or the match transcript.
    assert realtime_api.persist_asr_final(first_speech_id, "这是一条发言结束后才到达的迟到识别结果。") is False
    with SessionLocal() as db:
        assert db.get(Speech, first_speech_id).content == "正方完成立论并给出清晰论据。"
    _speak(opponent, code, opponent_headers, "反方回应立论并提出反驳。")

    # Free debate starts on the affirmative side. Both humans use the same
    # public request/selection protocol rather than bypassing the queue.
    _speak(owner, code, owner_headers, "正方自由辩论第一轮。")
    await match_engine.process_room(code)
    with SessionLocal() as db:
        current = load_room(db, code)
        assert current.status == "running"
        assert stage(current)["kind"] == "free"
        assert stage(current).get("intermission_deadline_at")
    # Expire the three-second request window. With no request on the opposing
    # side, the engine must keep the turn bounded and change sides rather than
    # call an Agent or stall forever.
    with SessionLocal() as db:
        current = load_room(db, code, lock=True)
        snapshot = list(current.template_snapshot)
        free = dict(snapshot[current.current_stage_index])
        free["intermission_deadline_at"] = (now() - timedelta(seconds=1)).isoformat()
        snapshot[current.current_stage_index] = free
        current.template_snapshot = snapshot
        db.commit()
    await match_engine.process_room(code)
    _speak(opponent, code, opponent_headers, "反方自由辩论回应。")

    # End the bounded free-debate stage; otherwise another intermission is
    # intentionally opened for the affirmative side.
    with SessionLocal() as db:
        current = load_room(db, code, lock=True)
        snapshot = list(current.template_snapshot)
        free = dict(snapshot[current.current_stage_index])
        # Free debate now counts actual speaking time only. Exhaust the
        # persisted speaking-time budget instead of relying on a wall-clock
        # deadline that is deliberately frozen during the request window.
        free["free_stage_remaining_seconds"] = 0
        free["intermission_deadline_at"] = (now() - timedelta(seconds=1)).isoformat()
        snapshot[current.current_stage_index] = free
        current.template_snapshot = snapshot
        db.commit()
    # First tick resolves the application window, second observes the expired
    # stage deadline and advances to the summary.
    await match_engine.process_room(code)
    await match_engine.process_room(code)

    _speak(opponent, code, opponent_headers, "反方完成最后总结。")
    _speak(owner, code, owner_headers, "正方完成最后总结。")
    with SessionLocal() as db:
        current = load_room(db, code)
        assert current.status == "judging"
    await match_engine.process_room(code)

    with SessionLocal() as db:
        current = load_room(db, code)
        match = db.scalar(select(Match).where(Match.room_id == current.id))
        speeches = list(db.scalars(select(Speech).where(Speech.room_id == current.id)).all())
        assert current.status == "completed"
        assert match and match.status == "completed"
        assert len([item for item in speeches if item.status == "completed"]) >= 4
        assert db.scalar(select(JudgeScorecard).where(JudgeScorecard.match_id == match.id))
        events = list(db.scalars(select(MatchEvent).where(MatchEvent.room_id == current.id).order_by(MatchEvent.seq)).all())
        assert [item.seq for item in events] == list(range(1, current.seq + 1))
        event_types = {item.event_type for item in events}
        assert {"control.pause", "control.resume"} <= event_types
        assert not event_types.intersection(
            {
                "provider.failed",
                "engine.quarantined",
                "participant.disconnect_timeout",
                "seat.ai_substituted",
            }
        )
        assert all(item.speaker_type == "human" for item in speeches)


@pytest.mark.asyncio
async def test_complete_human_vs_ai_and_four_v_four_room_isolation(register_user, client, monkeypatch) -> None:
    """Run an AI-filled 1v1 beside a 4v4 room with two human seats."""

    owner_1 = register_user("full_flow_ai_owner")
    owner_4 = register_user("full_flow_4v4_owner")
    human_4 = register_user("full_flow_4v4_human")
    topic = client.get("/api/competitions/daily-4v4").json()["competition"]["topics"][0]
    room_1 = create_training_room(owner_1, "完整人机流程")
    code_1 = room_1["code"]
    room_4_resp = owner_4.post(
        "/api/rooms",
        headers=csrf(owner_4),
        json={"competition_slug": "daily-4v4", "topic_id": topic["id"], "seat_key": "aff_1", "visibility": "public"},
    )
    assert room_4_resp.status_code == 200, room_4_resp.text
    code_4 = room_4_resp.json()["room"]["code"]
    assert human_4.post(f"/api/rooms/{code_4}/claim-seat", headers=csrf(human_4), json={"seat_key": "neg_1"}).status_code == 200
    _ready(owner_1, code_1)
    _start(owner_1, code_1)
    _ready(owner_4, code_4)
    _ready(human_4, code_4)
    _start(owner_4, code_4)

    calls: list[str] = []

    async def generated(payload, **_kwargs):
        calls.append(payload["room_code"])
        return f"{payload['room_code']} 完成 {payload['current_stage']}。"

    async def synthesized(*_args, **_kwargs):
        return ""

    async def judged(topic_value, speeches, **_kwargs):
        return {
            "winner": "aff",
            "affirmative_score": 87,
            "negative_score": 83,
            "individual_scores": {},
            "reasoning": f"{topic_value} 完成裁判。",
        }

    monkeypatch.setattr(debate_agent, "generate", generated)
    monkeypatch.setattr(lighttts, "synthesize", synthesized)
    monkeypatch.setattr(judge_provider, "judge", judged)
    # Keep the test deterministic and focus on stage ownership, not audio
    # delivery. Both rooms still use the real Agent/TTS/Judge orchestration.
    for code in (code_1, code_4):
        with SessionLocal() as db:
            current = load_room(db, code, lock=True)
            if code == code_1:
                current.template_snapshot = [
                    {"key": "human_aff", "name": "真人正方", "kind": "speech", "seat": "aff_1", "duration": 2},
                    {"key": "ai_neg", "name": "AI 反方", "kind": "speech", "seat": "neg_1", "duration": 2},
                    {"key": "judge", "name": "裁判", "kind": "judging", "duration": 1},
                ]
            else:
                # The owner is human on aff_1 and the second participant is
                # human on neg_1; this is the minimum real multi-human 4v4
                # topology, while the following aff_2/neg_2 turns prove that
                # the AI-filled seats are still part of the same flow.
                current.template_snapshot = [
                    {"key": "human_aff", "name": "真人正方", "kind": "speech", "seat": "aff_1", "duration": 2},
                    {"key": "human_neg", "name": "真人反方", "kind": "speech", "seat": "neg_1", "duration": 2},
                    {"key": "ai_aff", "name": "AI 正方二辩", "kind": "speech", "seat": "aff_2", "duration": 2},
                    {"key": "ai_neg", "name": "AI 反方二辩", "kind": "speech", "seat": "neg_2", "duration": 2},
                    {"key": "judge", "name": "裁判", "kind": "judging", "duration": 1},
                ]
            db.commit()

    # Enter each first stage, then complete their human turns through separate
    # device leases. Processing one room must not advance the other room.
    await match_engine.process_room(code_1)
    await match_engine.process_room(code_4)
    h1_owner = _lease(owner_1, code_1, "full-1v1-owner")
    h4_owner = _lease(owner_4, code_4, "full-4v4-owner")
    h4_human = _lease(human_4, code_4, "full-4v4-human")
    _speak(owner_1, code_1, h1_owner, "一对一真人正方完成发言。")
    _speak(owner_4, code_4, h4_owner, "四人制正方真人完成发言。")
    _speak(human_4, code_4, h4_human, "四人制反方真人完成发言。")
    for _ in range(8):
        await match_engine.process_room(code_1)
        await match_engine.process_room(code_4)
        with SessionLocal() as db:
            statuses = {code: load_room(db, code).status for code in (code_1, code_4)}
        if statuses == {code_1: "completed", code_4: "completed"}:
            break

    with SessionLocal() as db:
        one = load_room(db, code_1)
        four = load_room(db, code_4)
        assert one.status == "completed"
        assert four.status == "completed"
        assert calls and set(calls).issubset({code_1, code_4})
        # Every Agent-generated text carries its own room code; no room can
        # receive the other room's output or events.
        for code in (code_1, code_4):
            rows = db.scalars(select(Speech).where(Speech.room_id == load_room(db, code).id)).all()
            assert all(code in row.content for row in rows if row.speaker_type == "ai")
