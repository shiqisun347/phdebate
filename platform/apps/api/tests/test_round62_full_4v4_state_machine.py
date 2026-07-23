from __future__ import annotations

import hashlib
from copy import deepcopy
from datetime import timedelta

import pytest
from app.api import realtime as realtime_api
from app.core.config import settings
from app.core.database import SessionLocal
from app.models.entities import AudioCue, JudgeScorecard, Match, MatchEvent, Speech
from app.services.match_engine import match_engine
from app.services.providers import debate_agent, judge_provider, lighttts
from app.services.room_service import load_room, now, stage
from app.services.seed import DAILY_STAGES
from conftest import csrf
from sqlalchemy import func, select
from test_platform import _wav_bytes, create_training_room


def _claim(browser, code: str, seat_key: str) -> None:
    response = browser.post(
        f"/api/rooms/{code}/claim-seat",
        headers=csrf(browser),
        json={"seat_key": seat_key},
    )
    assert response.status_code == 200, response.text


def _ready(browser, code: str) -> None:
    response = browser.post(
        f"/api/rooms/{code}/ready",
        headers=csrf(browser),
        json={"ready": True},
    )
    assert response.status_code == 200, response.text


def _lease(browser, code: str, lease: str) -> dict[str, str]:
    headers = csrf(browser) | {"X-Control-Lease": lease}
    response = browser.post(f"/api/rooms/{code}/control-lease", headers=headers, json={})
    assert response.status_code == 200, response.text
    return headers


def _finish_human_turn(
    browser,
    code: str,
    headers: dict[str, str],
    content: str,
    *,
    use_mock_asr: bool = False,
) -> str:
    operation_suffix = hashlib.sha256(content.encode()).hexdigest()[:16]
    started = browser.post(
        f"/api/rooms/{code}/speech/start",
        headers=headers | {"X-Idempotency-Key": f"round62-start-{operation_suffix}"},
        json={},
    )
    assert started.status_code == 200, started.text
    speech_id = started.json()["speech_id"]
    if use_mock_asr:
        assert realtime_api.persist_asr_final(
            speech_id,
            content,
            confidence=0.99,
            voice_detected=True,
        ) is True
    finished_headers = headers | {"X-Idempotency-Key": f"round62-finish-{operation_suffix}"}
    finished = browser.post(
        f"/api/rooms/{code}/speech/finish",
        headers=finished_headers,
        json={"speech_id": speech_id, "content": content},
    )
    assert finished.status_code == 200, finished.text
    replay = browser.post(
        f"/api/rooms/{code}/speech/finish",
        headers=finished_headers,
        json={"speech_id": speech_id, "content": content},
    )
    assert replay.status_code == 200 and replay.json()["replayed"] is True
    return speech_id


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


def _short_official_template() -> list[dict]:
    """Keep the production stage topology while making the test deterministic."""

    result = deepcopy(DAILY_STAGES)
    for index, item in enumerate(result):
        item["key"] = f"round62_{index}_{item['key']}"
        if item["kind"] == "announcement":
            item["duration"] = 1
        elif item["kind"] == "free":
            item["duration"] = 12
            item["turn_duration"] = 3
        elif item["kind"] == "judging":
            item["duration"] = 1
        else:
            item["duration"] = 8
    return result


def test_start_rejects_ready_but_disconnected_human(register_user, monkeypatch) -> None:
    owner = register_user("round62_disconnected_start")
    code = create_training_room(owner, "离线真人不能开赛")["code"]
    _ready(owner, code)
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        seat = next(item for item in room.seats if item.user_id == room.owner_id)
        assert seat.is_ready is True
        seat.connected = False
        db.commit()

    monkeypatch.setattr(settings, "app_env", "development")

    blocked = owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    assert blocked.status_code == 409
    assert "未连接比赛房间" in blocked.json()["detail"]
    with SessionLocal() as db:
        room = load_room(db, code)
        assert room.status == "lobby"
        assert db.scalar(select(Match.id).where(Match.room_id == room.id)) is None

    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        seat = next(item for item in room.seats if item.user_id == room.owner_id)
        seat.connected = True
        db.commit()
    started = owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    assert started.status_code == 200, started.text


def test_preset_only_match_cannot_start_with_missing_or_invalid_host_audio(
    register_user,
    monkeypatch,
) -> None:
    owner = register_user("round62_missing_host_cue")
    code = create_training_room(owner, "提示音缺失不能静默开赛")["code"]
    cue_key = "round62_required_opening"
    cue_text = "欢迎进入 Round62 完整流程验证。"
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.template_snapshot = [
            {
                "key": cue_key,
                "name": "Round62 必需开场",
                "kind": "announcement",
                "duration": 1,
                "cue": cue_text,
            }
        ]
        seat = next(item for item in room.seats if item.user_id == room.owner_id)
        seat.connected = True
        db.commit()
    _ready(owner, code)
    monkeypatch.setattr(settings, "host_cues_preset_only", True)

    missing = owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    assert missing.status_code == 409
    assert "系统阶段提示音尚未完整配置" in missing.json()["detail"]

    invalid_path = settings.media_path / "_cues" / f"{cue_key}.wav"
    invalid_path.parent.mkdir(parents=True, exist_ok=True)
    invalid_path.write_bytes(b"not-a-wave")
    with SessionLocal() as db:
        db.add(
            AudioCue(
                key=cue_key,
                name="Round62 必需开场",
                text=cue_text,
                audio_url=f"/media/_cues/{invalid_path.name}",
                is_active=True,
            )
        )
        db.commit()
    invalid = owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    assert invalid.status_code == 409
    assert "系统阶段提示音尚未完整配置" in invalid.json()["detail"]

    invalid_path.write_bytes(_wav_bytes(0.08))
    started = owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    assert started.status_code == 200, started.text
    invalid_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_official_four_v_four_human_agent_match_runs_every_stage_to_result(
    client,
    register_user,
    monkeypatch,
) -> None:
    """Run the real 4v4 topology through every authoritative lifecycle edge.

    Four people occupy fixed seats and the remaining four seats are filled by
    permanent AI debaters.  The scenario covers preset host cues, human ASR,
    fixed Agent turns, question stages, two queued free-debate turns, summary,
    judging, replay-safe writes, manual pause/resume and disconnect recovery.
    """

    owner = register_user("round62_aff_one")
    neg_two = register_user("round62_neg_two")
    aff_three = register_user("round62_aff_three")
    neg_four = register_user("round62_neg_four")
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
    _claim(neg_two, code, "neg_2")
    _claim(aff_three, code, "aff_3")
    _claim(neg_four, code, "neg_4")
    for browser in (owner, neg_two, aff_three, neg_four):
        _ready(browser, code)
    # TestClient does not keep four real lobby WebSockets open.  Model the
    # connected presence required by the production start boundary.
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        for seat in room.seats:
            if seat.occupant_type == "human":
                seat.connected = True
                seat.disconnected_at = None
        db.commit()
    started = owner.post(
        f"/api/rooms/{code}/start",
        headers=csrf(owner) | {"X-Idempotency-Key": "round62-start-match"},
        json={},
    )
    assert started.status_code == 200, started.text

    template = _short_official_template()
    cue_paths = []
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        assert room.status == "preparing"
        assert len(room.seats) == 8
        assert {seat.seat_key for seat in room.seats if seat.occupant_type == "human"} == {
            "aff_1",
            "aff_3",
            "neg_2",
            "neg_4",
        }
        assert {seat.seat_key for seat in room.seats if seat.occupant_type == "ai"} == {
            "aff_2",
            "aff_4",
            "neg_1",
            "neg_3",
        }
        room.template_snapshot = template
        for item in template:
            cue_text = str(item.get("cue") or "").strip()
            if not cue_text:
                continue
            filename = f"{item['key']}.wav"
            path = settings.media_path / "_cues" / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(_wav_bytes(0.08))
            cue_paths.append(path)
            db.add(
                AudioCue(
                    key=item["key"],
                    name=f"Round62 {item['name']}",
                    text=cue_text,
                    audio_url=f"/media/_cues/{filename}",
                    is_active=True,
                )
            )
        db.commit()

    # All host prompts are immutable, pre-generated female cue assets.  The
    # match must never ask the TTS provider to regenerate them in its hot path.
    with SessionLocal() as db:
        assert await match_engine._prepare_cues(
            db,
            code,
            stage_keys={item["key"] for item in template},
            required=True,
        ) is True

    agent_calls: list[tuple[str, str]] = []
    tts_calls: list[tuple[str, str]] = []
    judge_inputs: list[dict] = []

    async def generated(payload, **_kwargs):
        agent_calls.append((payload["current_stage"], payload["debate_position"]))
        return f"{payload['debater_name']}在{payload['current_stage']}给出完整、清晰且可追溯的论证。"

    async def synthesized(text, *, room_code: str, speech_id: str, **_kwargs):
        tts_calls.append((room_code, speech_id))
        assert text.strip()
        # Empty URL models a fully drained mocked audio transport.  The engine
        # must still complete the speech and advance exactly once.
        return ""

    async def judged(topic, speeches, **_kwargs):
        judge_inputs.extend(speeches)
        return {
            "winner": "aff",
            "affirmative_score": 89,
            "negative_score": 86,
            "individual_scores": {
                "aff_1": 90,
                "aff_2": 88,
                "aff_3": 89,
                "aff_4": 87,
                "neg_1": 86,
                "neg_2": 87,
                "neg_3": 85,
                "neg_4": 86,
            },
            "reasoning": f"围绕《{topic}》完成全阶段裁决。",
        }

    monkeypatch.setattr(debate_agent, "generate", generated)
    monkeypatch.setattr(lighttts, "synthesize", synthesized)
    monkeypatch.setattr(judge_provider, "judge", judged)
    # Speculative prefetch has its own concurrency suite.  Disable it here so
    # this test counts only authoritative provider calls and cannot leave a
    # TestClient-owned background task behind when an HTTP request returns.
    monkeypatch.setattr(match_engine, "_schedule_next_agent_prefetch", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(match_engine, "_schedule_hosted_stage_agent_prefetch", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(match_engine, "_schedule_remaining_cue_prefetch", lambda *_args, **_kwargs: None)

    leases = {
        "aff_1": (owner, _lease(owner, code, "round62-aff-one-device")),
        "neg_2": (neg_two, _lease(neg_two, code, "round62-neg-two-device")),
        "aff_3": (aff_three, _lease(aff_three, code, "round62-aff-three-device")),
        "neg_4": (neg_four, _lease(neg_four, code, "round62-neg-four-device")),
    }
    human_text = {
        "aff_1": "正方一辩完成正式立论，明确标准并提供完整论据。",
        "neg_2": "反方二辩完成驳论，逐项回应正方的核心论证。",
        "aff_3": "正方三辩完成质询，指出对方论证中的关键矛盾。",
        "neg_4": "反方四辩完成总结，归纳本方比较优势与价值判断。",
    }

    await match_engine.process_room(code)
    with SessionLocal() as db:
        room = load_room(db, code)
        assert room.status == "running"
        assert stage(room)["kind"] == "announcement"
        room.stage_deadline_at = now() - timedelta(milliseconds=1)
        db.commit()
    await match_engine.process_room(code)

    paused_once = False
    disconnected_once = False
    free_completed = False
    loop_guard = 0
    while loop_guard < 80:
        loop_guard += 1
        with SessionLocal() as db:
            room = load_room(db, code)
            current = dict(stage(room) or {})
            status = room.status
        if status == "completed":
            break
        assert status in {"running", "judging"}, (status, current, loop_guard)

        if current.get("host_announcement_pending"):
            _expire_host_prompt(code)
            await match_engine.process_room(code)
            continue

        if current.get("kind") == "judging":
            await match_engine.process_room(code)
            continue

        if current.get("kind") == "speech":
            seat_key = str(current["seat"])
            if seat_key in leases:
                if seat_key == "aff_1" and not paused_once:
                    pause_headers = csrf(owner) | {"X-Idempotency-Key": "round62-pause-before-first-speech"}
                    paused = owner.post(
                        f"/api/rooms/{code}/control/pause",
                        headers=pause_headers,
                        json={"reason": "开麦前确认设备"},
                    )
                    replayed_pause = owner.post(
                        f"/api/rooms/{code}/control/pause",
                        headers=pause_headers,
                        json={"reason": "开麦前确认设备"},
                    )
                    assert paused.status_code == 200
                    assert replayed_pause.status_code == 200 and replayed_pause.json()["replayed"] is True
                    resume_headers = csrf(owner) | {"X-Idempotency-Key": "round62-resume-before-first-speech"}
                    resumed = owner.post(
                        f"/api/rooms/{code}/control/resume",
                        headers=resume_headers,
                        json={"reason": "设备确认完毕"},
                    )
                    replayed_resume = owner.post(
                        f"/api/rooms/{code}/control/resume",
                        headers=resume_headers,
                        json={"reason": "设备确认完毕"},
                    )
                    assert resumed.status_code == 200
                    assert replayed_resume.status_code == 200 and replayed_resume.json()["replayed"] is True
                    paused_once = True
                if seat_key == "aff_3" and not disconnected_once:
                    with SessionLocal() as db:
                        room = load_room(db, code, lock=True)
                        missing = next(item for item in room.seats if item.seat_key == "neg_2")
                        missing.connected = False
                        missing.disconnected_at = now() - timedelta(seconds=61)
                        db.commit()
                    await match_engine.process_room(code)
                    with SessionLocal() as db:
                        room = load_room(db, code, lock=True)
                        missing = next(item for item in room.seats if item.seat_key == "neg_2")
                        assert room.status == "paused"
                        assert "断线超过 60 秒" in room.failure_reason
                        assert missing.occupant_type == "human" and missing.user_id
                        missing.connected = True
                        missing.disconnected_at = None
                        db.commit()
                    resumed = owner.post(
                        f"/api/rooms/{code}/control/resume",
                        headers=csrf(owner) | {"X-Idempotency-Key": "round62-resume-after-disconnect"},
                        json={"reason": "真人辩手已重新连接"},
                    )
                    assert resumed.status_code == 200, resumed.text
                    disconnected_once = True
                browser, headers = leases[seat_key]
                _finish_human_turn(
                    browser,
                    code,
                    headers,
                    human_text[seat_key],
                    use_mock_asr=seat_key == "aff_1",
                )
            else:
                await match_engine.process_room(code)
            continue

        if current.get("kind") == "free":
            if free_completed:
                await match_engine.process_room(code)
                continue
            aff_browser, aff_headers = leases["aff_3"]
            neg_browser, neg_headers = leases["neg_4"]
            aff_content = "正方自由辩论首轮发言，集中比较双方方案的现实效果。"
            aff_started = aff_browser.post(
                f"/api/rooms/{code}/speech/start",
                headers=aff_headers | {"X-Idempotency-Key": "round62-aff-free-start"},
                json={},
            )
            assert aff_started.status_code == 200, aff_started.text
            first_id = aff_started.json()["speech_id"]
            requested = neg_browser.post(
                f"/api/rooms/{code}/free-turn-requests",
                headers=csrf(neg_browser) | {"X-Idempotency-Key": "round62-neg-free-request"},
                json={},
            )
            assert requested.status_code == 200, requested.text
            aff_finish_headers = aff_headers | {"X-Idempotency-Key": "round62-aff-free-finish"}
            aff_finished = aff_browser.post(
                f"/api/rooms/{code}/speech/finish",
                headers=aff_finish_headers,
                json={"speech_id": first_id, "content": aff_content},
            )
            aff_replayed = aff_browser.post(
                f"/api/rooms/{code}/speech/finish",
                headers=aff_finish_headers,
                json={"speech_id": first_id, "content": aff_content},
            )
            assert aff_finished.status_code == 200, aff_finished.text
            assert aff_replayed.status_code == 200 and aff_replayed.json()["replayed"] is True
            with SessionLocal() as db:
                room = load_room(db, code, lock=True)
                current = dict(stage(room) or {})
                current["intermission_deadline_at"] = (now() - timedelta(milliseconds=1)).isoformat()
                snapshot = list(room.template_snapshot)
                snapshot[room.current_stage_index] = current
                room.template_snapshot = snapshot
                db.commit()
            await match_engine.process_room(code)
            selected = neg_browser.get(f"/api/rooms/{code}").json()["room"]
            assert selected["current_stage"]["selected_human_seat"] == "neg_4"
            neg_content = "反方自由辩论回应，说明本方标准更能解释真实影响。"
            neg_started = neg_browser.post(
                f"/api/rooms/{code}/speech/start",
                headers=neg_headers | {"X-Idempotency-Key": "round62-neg-free-start"},
                json={},
            )
            assert neg_started.status_code == 200, neg_started.text
            second_id = neg_started.json()["speech_id"]
            aff_requested = owner.post(
                f"/api/rooms/{code}/free-turn-requests",
                headers=csrf(owner) | {"X-Idempotency-Key": "round62-aff-free-request"},
                json={},
            )
            assert aff_requested.status_code == 200, aff_requested.text
            neg_finish_headers = neg_headers | {"X-Idempotency-Key": "round62-neg-free-finish"}
            neg_finished = neg_browser.post(
                f"/api/rooms/{code}/speech/finish",
                headers=neg_finish_headers,
                json={"speech_id": second_id, "content": neg_content},
            )
            neg_replayed = neg_browser.post(
                f"/api/rooms/{code}/speech/finish",
                headers=neg_finish_headers,
                json={"speech_id": second_id, "content": neg_content},
            )
            assert neg_finished.status_code == 200, neg_finished.text
            assert neg_replayed.status_code == 200 and neg_replayed.json()["replayed"] is True
            with SessionLocal() as db:
                room = load_room(db, code, lock=True)
                current = dict(stage(room) or {})
                current["free_stage_remaining_seconds"] = 0
                current["intermission_deadline_at"] = (now() - timedelta(milliseconds=1)).isoformat()
                snapshot = list(room.template_snapshot)
                snapshot[room.current_stage_index] = current
                room.template_snapshot = snapshot
                db.commit()
            await match_engine.process_room(code)
            await match_engine.process_room(code)
            with SessionLocal() as db:
                assert db.get(Speech, first_id).status == "completed"
                assert db.get(Speech, second_id).status == "completed"
            free_completed = True
            continue

        raise AssertionError((status, current, loop_guard))

    assert loop_guard < 80
    assert paused_once and disconnected_once and free_completed
    fixed_agent_calls = [item for item in agent_calls if item[0] != "自由辩论"]
    assert len(fixed_agent_calls) == 4
    # Free-debate candidate text may be generated speculatively, but a queued
    # human always wins and therefore no speculative candidate reaches TTS.
    assert all(item[0] == "自由辩论" for item in agent_calls if item not in fixed_agent_calls)
    assert len(tts_calls) == 4
    assert len(judge_inputs) == 10
    assert {item["seat_key"] for item in judge_inputs} == {
        "aff_1",
        "aff_2",
        "aff_3",
        "aff_4",
        "neg_1",
        "neg_2",
        "neg_3",
        "neg_4",
    }

    with SessionLocal() as db:
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
        assert room.status == match.status == "completed"
        assert scorecard and scorecard.status == "approved" and scorecard.winner == "aff"
        assert len([item for item in speeches if item.status == "completed"]) == 10
        assert [item.seq for item in events] == list(range(1, room.seq + 1))
        assert (
            db.scalar(
                select(func.count(MatchEvent.id)).where(
                    MatchEvent.room_id == room.id,
                    MatchEvent.event_type == "stage.started",
                )
            )
            == len(template)
        )
        assert (
            db.scalar(
                select(func.count(MatchEvent.id)).where(
                    MatchEvent.room_id == room.id,
                    MatchEvent.event_type == "stage.host_announcement_completed",
                )
            )
            == len(template) - 1
        )
        event_types = {item.event_type for item in events}
        assert {
            "control.pause",
            "control.resume",
            "participant.disconnect_timeout",
            "match.completed",
        } <= event_types
        assert "seat.ai_substituted" not in event_types
        assert not db.scalar(
            select(Speech.id).where(
                Speech.room_id == room.id,
                Speech.status.in_(["speaking", "synthesizing", "playing"]),
            )
        )

    for path in cue_paths:
        path.unlink(missing_ok=True)
