from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from app.core.config import settings
from app.core.database import SessionLocal
from app.models.entities import AudioAsset, AudioCue, Match, MatchEvent, RoomSeat, Speech
from app.services.match_engine import match_engine
from app.services.providers import ProviderError, lighttts
from app.services.room_service import free_turn_remaining_seconds, load_room, now, remaining_seconds, stage
from conftest import csrf
from sqlalchemy import select
from test_platform import _wav_bytes, create_training_room


def _ready(browser, code: str) -> None:
    response = browser.post(f"/api/rooms/{code}/ready", headers=csrf(browser), json={"ready": True})
    assert response.status_code == 200, response.text


def _lease(browser, code: str, lease: str) -> dict[str, str]:
    headers = csrf(browser) | {"X-Control-Lease": lease}
    response = browser.post(f"/api/rooms/{code}/control-lease", headers=headers, json={})
    assert response.status_code == 200, response.text
    return headers


def _daily_room(owner, client, topic_index: int = 0) -> str:
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


def _claim(browser, code: str, seat_key: str) -> None:
    response = browser.post(
        f"/api/rooms/{code}/claim-seat",
        headers=csrf(browser),
        json={"seat_key": seat_key},
    )
    assert response.status_code == 200, response.text


def _set_free_clock(code: str, *, stage_seconds: int, turn_elapsed_seconds: int) -> None:
    """Move only authoritative timestamps; never patch projected counters."""

    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        current = dict(stage(room) or {})
        assert current.get("kind") == "free", current
        instant = now()
        room.stage_started_at = instant
        room.stage_deadline_at = instant + timedelta(seconds=stage_seconds)
        current["turn_started_at"] = (instant - timedelta(seconds=turn_elapsed_seconds)).isoformat()
        snapshot = list(room.template_snapshot)
        snapshot[room.current_stage_index] = current
        room.template_snapshot = snapshot
        db.commit()


@pytest.mark.asyncio
async def test_required_host_cue_can_be_repaired_then_retry_starts_with_shared_preset(
    register_user,
    monkeypatch,
) -> None:
    """A missing mandatory cue pauses safely; adding its preset makes retry deterministic."""

    owner = register_user("round66_cue_repair_owner")
    code = create_training_room(owner, "Round66 提示音修复后继续")["code"]
    cue_key = "round66_required_opening"
    cue_text = "Round66 比赛现在开始，请双方选手准备。"
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.template_snapshot = [
            {
                "key": cue_key,
                "name": "统一女声开场",
                "kind": "announcement",
                "duration": 30,
                "cue": cue_text,
            },
            {"key": "round66_judge", "name": "裁判", "kind": "judging", "duration": 1},
        ]
        db.commit()
    _ready(owner, code)
    monkeypatch.setattr(settings, "host_cues_preset_only", True)
    started = owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    assert started.status_code == 409
    assert "提示音尚未完整配置" in started.json()["detail"]
    with SessionLocal() as db:
        room = load_room(db, code)
        assert room.status == "lobby"
        assert room.failure_reason == ""
        assert room.current_stage_index == -1

    cue_path = settings.media_path / "_cues" / f"{cue_key}.wav"
    cue_path.parent.mkdir(parents=True, exist_ok=True)
    cue_path.write_bytes(_wav_bytes(0.18))
    with SessionLocal() as db:
        db.add(
            AudioCue(
                key=cue_key,
                name="Round66 统一女声开场",
                text=cue_text,
                audio_url=f"/media/_cues/{cue_key}.wav",
                is_active=True,
            )
        )
        db.commit()

    retried = owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    assert retried.status_code == 200, retried.text
    assert retried.json()["room"]["status"] == "preparing"
    await match_engine.process_room(code)

    with SessionLocal() as db:
        room = load_room(db, code)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        asset = db.scalar(
            select(AudioAsset).where(AudioAsset.match_id == match.id, AudioAsset.kind == f"cue:{cue_key}")
        )
        events = list(db.scalars(select(MatchEvent).where(MatchEvent.room_id == room.id)).all())
        assert room.status == "running" and room.current_stage_index == 0
        assert stage(room)["kind"] == "announcement"
        assert remaining_seconds(room) in {1, 2}
        assert asset and asset.storage_key == f"/media/_cues/{cue_key}.wav"
        assert [event.event_type for event in events].count("audio.cue.ready") == 1
        assert [event.event_type for event in events].count("audio.cue.missing_preset") == 0
    cue_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_free_debate_uses_speaking_time_freezes_three_second_window_and_selects_human_or_permanent_ai(
    client,
    register_user,
    monkeypatch,
) -> None:
    """Exercise both free-debate clocks and both authoritative hand-off branches."""

    monkeypatch.setattr(match_engine, "schedule_free_agent_speculation", lambda *_args, **_kwargs: None)
    owner = register_user("round66_free_owner")
    opponent = register_user("round66_free_opponent")
    code = _daily_room(owner, client)
    _claim(opponent, code, "neg_1")
    for browser in (owner, opponent):
        _ready(browser, code)
    assert owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={}).status_code == 200

    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.status = "running"
        room.template_snapshot = [
            {
                "key": "round66_free",
                "name": "自由辩论",
                "kind": "free",
                "side": "aff",
                "duration": 12,
                "turn_duration": 4,
            },
            {"key": "round66_judge", "name": "裁判", "kind": "judging", "duration": 1},
        ]
        match_engine._enter_stage(db, room, 0)
        for seat in room.seats:
            if seat.occupant_type == "human":
                seat.connected = True
                seat.disconnected_at = None
        db.commit()
        assert remaining_seconds(room) == 12
        assert free_turn_remaining_seconds(room) == 4

    owner_headers = _lease(owner, code, "round66-free-owner-device")
    opponent_headers = _lease(opponent, code, "round66-free-opponent-device")
    first = owner.post(f"/api/rooms/{code}/speech/start", headers=owner_headers, json={})
    assert first.status_code == 200, first.text
    _set_free_clock(code, stage_seconds=10, turn_elapsed_seconds=2)

    requested = opponent.post(
        f"/api/rooms/{code}/free-turn-requests",
        headers=csrf(opponent) | {"X-Idempotency-Key": "round66-neg-human-request"},
        json={},
    )
    assert requested.status_code == 200, requested.text
    finished = owner.post(
        f"/api/rooms/{code}/speech/finish",
        headers=owner_headers,
        json={"speech_id": first.json()["speech_id"], "content": "正方第一轮完整发言，实际消耗两秒比赛时间。"},
    )
    assert finished.status_code == 200, finished.text

    with SessionLocal() as db:
        room = load_room(db, code)
        current = stage(room)
        frozen = remaining_seconds(room)
        assert frozen in {9, 10}
        assert current["intermission_side"] == "neg"
        assert room.stage_deadline_at is None
        assert free_turn_remaining_seconds(room) == 4

    paused = owner.post(
        f"/api/rooms/{code}/control/pause",
        headers=csrf(owner),
        json={"reason": "三秒换方期间检查设备"},
    )
    assert paused.status_code == 200, paused.text
    paused_remaining = paused.json()["room"]["remaining_seconds"]
    resumed = owner.post(
        f"/api/rooms/{code}/control/resume",
        headers=csrf(owner),
        json={"reason": "设备检查完成"},
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["room"]["remaining_seconds"] == paused_remaining
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        current = dict(stage(room) or {})
        current["intermission_deadline_at"] = (now() - timedelta(milliseconds=1)).isoformat()
        snapshot = list(room.template_snapshot)
        snapshot[room.current_stage_index] = current
        room.template_snapshot = snapshot
        db.commit()
    await match_engine.process_room(code)

    with SessionLocal() as db:
        room = load_room(db, code)
        current = stage(room)
        assert current["side"] == "neg"
        assert current["selected_human_seat"] == "neg_1"
        assert current["awaiting_human_start"] is True
        assert remaining_seconds(room) == paused_remaining
        assert free_turn_remaining_seconds(room) == 4

    second = opponent.post(f"/api/rooms/{code}/speech/start", headers=opponent_headers, json={})
    assert second.status_code == 200, second.text
    _set_free_clock(code, stage_seconds=6, turn_elapsed_seconds=4)
    finished = opponent.post(
        f"/api/rooms/{code}/speech/finish",
        headers=opponent_headers,
        json={"speech_id": second.json()["speech_id"], "content": "反方完成第二轮发言，下一轮无人类申请。"},
    )
    assert finished.status_code == 200, finished.text
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        current = dict(stage(room) or {})
        assert current["intermission_side"] == "aff"
        current["intermission_deadline_at"] = (now() - timedelta(milliseconds=1)).isoformat()
        snapshot = list(room.template_snapshot)
        snapshot[room.current_stage_index] = current
        room.template_snapshot = snapshot
        db.commit()
    await match_engine.process_room(code)

    with SessionLocal() as db:
        room = load_room(db, code)
        current = stage(room)
        human_ids = {seat.seat_key: seat.user_id for seat in room.seats if seat.occupant_type == "human"}
        assert human_ids == {"aff_1": owner.get("/api/me").json()["user"]["id"], "neg_1": opponent.get("/api/me").json()["user"]["id"]}
        assert current["side"] == "aff"
        assert current["force_ai_fallback"] is True
        assert current["ai_preparing"] is True
        assert remaining_seconds(room) in {5, 6}
        assert free_turn_remaining_seconds(room) == 4

    selected_ai: list[str] = []

    async def record_ai_fallback(_db, _room, seat: RoomSeat, current: dict) -> None:
        assert current["force_ai_fallback"] is True
        selected_ai.append(seat.seat_key)

    monkeypatch.setattr(match_engine, "_free_ai_speech_with_intent", record_ai_fallback)
    await match_engine.process_room(code)
    assert selected_ai and selected_ai[0].startswith("aff_") and selected_ai[0] != "aff_1"
    with SessionLocal() as db:
        room = load_room(db, code)
        assert next(seat for seat in room.seats if seat.seat_key == selected_ai[0]).occupant_type == "ai"
        event_types = list(db.scalars(select(MatchEvent.event_type).where(MatchEvent.room_id == room.id)).all())
        assert event_types.count("free.intermission_started") == 2
        assert event_types.count("free.side_changed") == 2
        assert "seat.ai_substituted" not in event_types


@pytest.mark.asyncio
async def test_disconnect_pause_restores_exact_free_clocks_and_final_judge_completes(
    client,
    register_user,
    monkeypatch,
) -> None:
    """A 60-second disconnect pauses, never substitutes, then resumes to an approved result."""

    owner = register_user("round66_disconnect_owner")
    opponent = register_user("round66_disconnect_opponent")
    code = _daily_room(owner, client, topic_index=1)
    _claim(opponent, code, "neg_1")
    for browser in (owner, opponent):
        _ready(browser, code)
    assert owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={}).status_code == 200
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.status = "running"
        room.template_snapshot = [
            {
                "key": "round66_disconnect_free",
                "name": "自由辩论",
                "kind": "free",
                "side": "aff",
                "duration": 9,
                "turn_duration": 4,
            },
            {"key": "round66_final_judge", "name": "最终裁判", "kind": "judging", "duration": 1},
        ]
        match_engine._enter_stage(db, room, 0)
        for seat in room.seats:
            if seat.occupant_type == "human":
                seat.connected = True
                seat.disconnected_at = None
        db.commit()

    headers = _lease(owner, code, "round66-disconnect-device")
    started = owner.post(f"/api/rooms/{code}/speech/start", headers=headers, json={})
    assert started.status_code == 200, started.text
    _set_free_clock(code, stage_seconds=7, turn_elapsed_seconds=2)
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        human = next(seat for seat in room.seats if seat.seat_key == "aff_1")
        human.connected = False
        human.disconnected_at = now() - timedelta(seconds=61)
        db.commit()
    await match_engine.process_room(code)

    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        current = stage(room)
        human = next(seat for seat in room.seats if seat.seat_key == "aff_1")
        assert room.status == "paused"
        assert room.paused_remaining_seconds in {6, 7}
        assert current["paused_turn_remaining_seconds"] in {1, 2}
        assert current["awaiting_human_start"] is True
        assert human.occupant_type == "human" and human.user_id
        assert db.get(Speech, started.json()["speech_id"]).status == "interrupted"
        human.connected = True
        human.disconnected_at = None
        db.commit()

    resumed = owner.post(
        f"/api/rooms/{code}/control/resume",
        headers=csrf(owner) | {"X-Idempotency-Key": "round66-disconnect-resume"},
        json={"reason": "真人辩手已经返回"},
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["room"]["status"] == "running"
    assert resumed.json()["room"]["current_stage"]["awaiting_human_start"] is True
    with SessionLocal() as db:
        room = load_room(db, code)
        assert room.stage_deadline_at is None
        assert remaining_seconds(room) in {6, 7}
        assert free_turn_remaining_seconds(room) in {1, 2}

    restarted = owner.post(f"/api/rooms/{code}/speech/start", headers=headers, json={})
    assert restarted.status_code == 200, restarted.text
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        current = dict(stage(room) or {})
        assert free_turn_remaining_seconds(room, current) in {1, 2}
        # End the remaining free-debate budget through the authoritative
        # deadline path; the final browser submission remains the auditable
        # text used by the judge.
        room.stage_deadline_at = now() - timedelta(milliseconds=1)
        current["turn_started_at"] = (now() - timedelta(seconds=4)).isoformat()
        snapshot = list(room.template_snapshot)
        snapshot[room.current_stage_index] = current
        room.template_snapshot = snapshot
        db.commit()
    finalized = owner.post(
        f"/api/rooms/{code}/speech/finish",
        headers=headers,
        json={
            "speech_id": restarted.json()["speech_id"],
            "content": "正方断线恢复后完成本轮论证，并保留可供最终裁判核验的文字记录。",
        },
    )
    assert finalized.status_code == 200, finalized.text
    with SessionLocal() as db:
        room = load_room(db, code)
        assert room.status == "judging"
        assert stage(room)["kind"] == "judging"

    async def judged(topic, speeches, **_kwargs):
        assert topic and speeches
        return {
            "winner": "neg",
            "affirmative_score": 86,
            "negative_score": 88,
            "individual_scores": {"aff_1": 85, "neg_1": 89},
            "reasoning": "反方回应更完整，且所有结论仅依据本场已经固定的文字记录。",
        }

    monkeypatch.setattr("app.services.match_engine.judge_provider.judge", judged)
    await match_engine.process_room(code)
    with SessionLocal() as db:
        room = load_room(db, code)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        event_types = list(db.scalars(select(MatchEvent.event_type).where(MatchEvent.room_id == room.id)).all())
        assert room.status == match.status == "completed"
        assert match.winner == "neg"
        assert event_types.count("participant.disconnect_timeout") == 1
        assert event_types.count("match.completed") == 1
        assert "seat.ai_substituted" not in event_types


@pytest.mark.asyncio
async def test_free_ai_tts_failure_retry_reuses_text_and_preserves_both_clocks(
    client,
    register_user,
    monkeypatch,
) -> None:
    """A free-turn TTS retry must not regenerate text or reset either countdown."""

    owner = register_user("round66_free_retry_owner")
    opponent = register_user("round66_free_retry_opponent")
    code = _daily_room(owner, client)
    _claim(opponent, code, "neg_1")
    for browser in (owner, opponent):
        _ready(browser, code)
    assert owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={}).status_code == 200
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.status = "running"
        room.template_snapshot = [
            {
                "key": "round66_free_retry",
                "name": "自由辩论",
                "kind": "free",
                "side": "neg",
                "duration": 18,
                "turn_duration": 4,
                "turn_seq": 2,
                "force_ai_fallback": True,
                "ai_preparing": True,
                "preparing_stage_remaining_seconds": 11,
                "preparing_turn_remaining_seconds": 3,
            },
            {"key": "round66_retry_judge", "name": "裁判", "kind": "judging", "duration": 1},
        ]
        room.current_stage_index = 0
        room.stage_started_at = None
        room.stage_deadline_at = None
        for seat in room.seats:
            if seat.occupant_type == "human":
                seat.connected = True
                seat.disconnected_at = None
        db.commit()

    frozen_content = "反方智能体已经完成本轮文本生成，重试只能重新合成这段完全相同的语音。"
    synth_attempts = 0

    async def synthesize(text: str, *, room_code: str, speech_id: str, **_kwargs) -> str:
        nonlocal synth_attempts
        synth_attempts += 1
        assert text == frozen_content
        if synth_attempts == 1:
            raise ProviderError("Round66 模拟自由辩论语音服务瞬时失败", code="lighttts_unavailable", retryable=True)
        path = settings.media_path / room_code / f"{speech_id}.wav"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_wav_bytes(0.2))
        return f"/media/{room_code}/{speech_id}.wav"

    async def start_with_frozen_text(db, room, seat, current) -> None:
        await match_engine._ai_speech(db, room, seat, current, free_turn=True, speculative_content=frozen_content)

    monkeypatch.setattr(lighttts, "synthesize", synthesize)
    monkeypatch.setattr(match_engine, "_free_ai_speech_with_intent", start_with_frozen_text)
    await match_engine.process_room(code)

    with SessionLocal() as db:
        room = load_room(db, code)
        failed = db.scalar(
            select(Speech).where(Speech.room_id == room.id, Speech.stage_key == "round66_free_retry")
        )
        current = stage(room)
        assert room.status == "paused"
        assert failed and failed.status == "failed" and failed.content == frozen_content
        assert room.paused_remaining_seconds == 11
        assert current["paused_turn_remaining_seconds"] == 3

    wrong_resume = owner.post(
        f"/api/rooms/{code}/control/resume",
        headers=csrf(owner),
        json={"reason": "错误地直接继续"},
    )
    assert wrong_resume.status_code == 409 and "重试当前步骤" in wrong_resume.json()["detail"]
    retried = owner.post(
        f"/api/rooms/{code}/control/retry",
        headers=csrf(owner) | {"X-Idempotency-Key": "round66-free-tts-retry"},
        json={"reason": "语音服务已经恢复"},
    )
    assert retried.status_code == 200, retried.text
    assert retried.json()["room"]["remaining_seconds"] == 11
    assert retried.json()["room"]["turn_remaining_seconds"] == 3

    await match_engine.process_room(code)
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        attempts = list(
            db.scalars(
                select(Speech)
                .where(Speech.room_id == room.id, Speech.stage_key == "round66_free_retry")
                .order_by(Speech.created_at, Speech.id)
            ).all()
        )
        assert [speech.status for speech in attempts] == ["failed_retried", "playing"]
        assert [speech.content for speech in attempts] == [frozen_content, frozen_content]
        assert remaining_seconds(room) in {10, 11}
        assert free_turn_remaining_seconds(room) in {2, 3}
        attempts[-1].playback_ends_at = now() - timedelta(milliseconds=1)
        db.commit()
    await match_engine.process_room(code)

    with SessionLocal() as db:
        room = load_room(db, code)
        current = stage(room)
        assert current["intermission_side"] == "aff"
        assert remaining_seconds(room) in {10, 11}
        assert synth_attempts == 2
        event_types = list(db.scalars(select(MatchEvent.event_type).where(MatchEvent.room_id == room.id)).all())
        assert event_types.count("provider.failed") == 1
        assert event_types.count("speech.content.reused") == 1


@pytest.mark.asyncio
async def test_fixed_human_start_timeout_pauses_and_resume_keeps_same_turn(
    register_user,
) -> None:
    """An online but inactive fixed-stage speaker cannot occupy a running room forever."""

    owner = register_user("round66_fixed_start_timeout")
    code = create_training_room(owner, "Round66 固定真人开口等待超时")["code"]
    _ready(owner, code)
    assert owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={}).status_code == 200
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.status = "running"
        room.template_snapshot = [
            {"key": "round66_fixed_human", "name": "正方真人立论", "kind": "speech", "seat": "aff_1", "duration": 90},
            {"key": "round66_fixed_judge", "name": "裁判", "kind": "judging", "duration": 1},
        ]
        match_engine._enter_stage(db, room, 0)
        human = next(seat for seat in room.seats if seat.seat_key == "aff_1")
        human.connected = True
        human.disconnected_at = None
        current = dict(stage(room) or {})
        assert current["awaiting_human_start"] is True
        # Simulate a room created by the previous release: the first engine
        # tick must grant a fresh full readiness window, not pause it
        # immediately merely because the new field did not exist yet.
        current.pop("human_start_deadline_at", None)
        snapshot = list(room.template_snapshot)
        snapshot[0] = current
        room.template_snapshot = snapshot
        db.commit()

    await match_engine.process_room(code)
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        current = dict(stage(room) or {})
        assert room.status == "running"
        assert datetime.fromisoformat(current["human_start_deadline_at"]) > now()
        current["human_start_deadline_at"] = (now() - timedelta(milliseconds=1)).isoformat()
        snapshot = list(room.template_snapshot)
        snapshot[0] = current
        room.template_snapshot = snapshot
        db.commit()
    await match_engine.process_room(code)
    projected = owner.get(f"/api/rooms/{code}").json()["room"]
    assert projected["status"] == "paused"
    assert projected["current_stage"]["human_start_timeout_paused"] is True
    assert projected["pause_health"]["reason_code"] == "participant_start_timeout"
    assert projected["pause_health"]["recommended_action"] == "resume"
    assert "未开始" in projected["failure_reason"]
    assert projected["current_stage_index"] == 0

    wrong_retry = owner.post(
        f"/api/rooms/{code}/control/retry",
        headers=csrf(owner),
        json={"reason": "不应当把真人等待超时当成服务故障"},
    )
    assert wrong_retry.status_code == 409 and "继续比赛" in wrong_retry.json()["detail"]

    resumed = owner.post(
        f"/api/rooms/{code}/control/resume",
        headers=csrf(owner) | {"X-Idempotency-Key": "round66-fixed-start-resume"},
        json={"reason": "选手已确认准备"},
    )
    assert resumed.status_code == 200, resumed.text
    restored = resumed.json()["room"]
    assert restored["status"] == "running" and restored["current_stage_index"] == 0
    assert restored["current_stage"]["seat"] == "aff_1"
    assert restored["current_stage"]["awaiting_human_start"] is True
    assert "human_start_timeout_paused" not in restored["current_stage"]
    assert restored["can_speak"] is True

    headers = _lease(owner, code, "round66-fixed-timeout-device")
    started = owner.post(f"/api/rooms/{code}/speech/start", headers=headers, json={})
    assert started.status_code == 200, started.text
    assert "awaiting_human_start" not in started.json()["room"]["current_stage"]
    with SessionLocal() as db:
        room = load_room(db, code)
        event_types = list(db.scalars(select(MatchEvent.event_type).where(MatchEvent.room_id == room.id)).all())
        assert event_types.count("participant.start_timeout") == 1
        assert "seat.ai_substituted" not in event_types


@pytest.mark.asyncio
async def test_selected_free_human_start_timeout_resumes_same_seat_and_frozen_clocks(
    client,
    register_user,
) -> None:
    """A selected free-debate requester remains authoritative after timeout recovery."""

    owner = register_user("round66_free_start_timeout_owner")
    opponent = register_user("round66_free_start_timeout_opponent")
    code = _daily_room(owner, client)
    _claim(opponent, code, "neg_1")
    for browser in (owner, opponent):
        _ready(browser, code)
    assert owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={}).status_code == 200
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.status = "running"
        room.template_snapshot = [
            {
                "key": "round66_selected_free",
                "name": "自由辩论",
                "kind": "free",
                "side": "neg",
                "duration": 80,
                "turn_duration": 20,
                "turn_seq": 3,
                "free_stage_remaining_seconds": 53,
                "selected_human_seat": "neg_1",
                "awaiting_human_start": True,
                "human_start_deadline_at": (now() - timedelta(milliseconds=1)).isoformat(),
            },
            {"key": "round66_selected_judge", "name": "裁判", "kind": "judging", "duration": 1},
        ]
        room.current_stage_index = 0
        room.stage_started_at = None
        room.stage_deadline_at = None
        for seat in room.seats:
            if seat.occupant_type == "human":
                seat.connected = True
                seat.disconnected_at = None
        db.commit()

    await match_engine.process_room(code)
    with SessionLocal() as db:
        room = load_room(db, code)
        current = stage(room)
        assert room.status == "paused"
        assert room.paused_remaining_seconds == 53
        assert current["selected_human_seat"] == "neg_1"
        assert current["side"] == "neg" and current["turn_seq"] == 3
        assert free_turn_remaining_seconds(room) == 20

    resumed = owner.post(
        f"/api/rooms/{code}/control/resume",
        headers=csrf(owner),
        json={"reason": "被选中的反方辩手已经准备"},
    )
    assert resumed.status_code == 200, resumed.text
    restored = resumed.json()["room"]
    assert restored["status"] == "running"
    assert restored["remaining_seconds"] == 53
    assert restored["turn_remaining_seconds"] == 20
    assert restored["current_stage"]["selected_human_seat"] == "neg_1"
    assert restored["current_stage"]["turn_seq"] == 3
    assert restored["can_speak"] is False
    opponent_view = opponent.get(f"/api/rooms/{code}").json()["room"]
    assert opponent_view["can_speak"] is True

    headers = _lease(opponent, code, "round66-selected-free-device")
    started = opponent.post(f"/api/rooms/{code}/speech/start", headers=headers, json={})
    assert started.status_code == 200, started.text
    assert started.json()["room"]["remaining_seconds"] in {52, 53}
    assert started.json()["room"]["turn_remaining_seconds"] in {19, 20}
    with SessionLocal() as db:
        room = load_room(db, code)
        assert all(seat.occupant_type == "human" for seat in room.seats if seat.seat_key in {"aff_1", "neg_1"})
