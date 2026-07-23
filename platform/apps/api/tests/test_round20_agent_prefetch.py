from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from app.core.config import settings
from app.core.database import SessionLocal
from app.models.entities import MatchEvent, RoomSeat, Speech
from app.services.match_engine import MatchEngine, _adopt_prefetched_audio
from app.services.providers import ProviderError, debate_agent, moss_tts_realtime
from app.services.room_service import load_room, now
from conftest import csrf
from sqlalchemy import select
from test_platform import _wav_bytes, create_training_room


def _running_announcement_room(owner, topic: str) -> str:
    code = create_training_room(owner, topic)["code"]
    assert owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True}).status_code == 200
    assert owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={}).status_code == 200
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.status = "running"
        room.template_snapshot = [
            {"key": "host", "name": "主持人口播", "kind": "announcement", "duration": 20},
            {"key": "neg_case", "name": "反方一辩立论", "kind": "speech", "seat": "neg_1", "duration": 60},
        ]
        room.current_stage_index = 0
        room.stage_started_at = now()
        room.stage_deadline_at = now() + timedelta(seconds=20)
        seat = db.scalar(select(RoomSeat).where(RoomSeat.room_id == room.id, RoomSeat.seat_key == "neg_1"))
        assert seat is not None and seat.occupant_type == "ai"
        db.commit()
    return code


@pytest.mark.asyncio
async def test_fixed_ai_text_prefetch_is_idempotent_room_scoped_and_consumable(
    register_user, monkeypatch
) -> None:
    owner_a = register_user("prefetch_owner_a")
    owner_b = register_user("prefetch_owner_b")
    code_a = _running_announcement_room(owner_a, "预取房间甲")
    code_b = _running_announcement_room(owner_b, "预取房间乙")
    calls: list[dict] = []

    async def generate(payload, *, provider_config=None):
        calls.append(payload)
        return f"这是{payload['debate_topic']}的预生成完整立论内容。"

    monkeypatch.setattr(debate_agent, "generate", generate)
    engine = MatchEngine()
    engine._ensure_runtime()
    await engine.process_room(code_a)
    first_task = engine._agent_prefetch_tasks[code_a]
    engine._schedule_next_agent_prefetch(code_a, 0)
    assert engine._agent_prefetch_tasks[code_a] is first_task
    engine._schedule_next_agent_prefetch(code_b, 0)
    await first_task
    await engine._agent_prefetch_tasks[code_b]

    assert len(calls) == 2
    assert calls[0]["room_code"] != calls[1]["room_code"]
    assert calls[0]["task_id"] != calls[1]["task_id"]
    assert set(engine._agent_prefetch_cache) == {code_a, code_b}

    with SessionLocal() as db:
        room = load_room(db, code_a, lock=True)
        room.current_stage_index = 1
        seat = next(item for item in room.seats if item.seat_key == "neg_1")
        content = engine._consume_agent_prefetch(db, room, room.template_snapshot[1], seat)
        assert content == "这是预取房间甲的预生成完整立论内容。"
    assert code_a not in engine._agent_prefetch_cache
    assert code_b in engine._agent_prefetch_cache
    with SessionLocal() as db:
        room = load_room(db, code_b, lock=True)
        room.current_stage_index = 1
        room.topic = "房间乙已经换题"
        seat = next(item for item in room.seats if item.seat_key == "neg_1")
        assert engine._consume_agent_prefetch(db, room, room.template_snapshot[1], seat) == ""
    assert code_b not in engine._agent_prefetch_cache


@pytest.mark.asyncio
async def test_host_cue_prefetches_the_ai_stage_it_is_unlocking(register_user, monkeypatch) -> None:
    owner = register_user("hosted_prefetch_owner")
    code = _running_announcement_room(owner, "主持提示音期间预生成当前 AI 发言")
    calls: list[dict] = []

    async def generate(payload, *, provider_config=None):
        calls.append(payload)
        return "主持提示音播放期间已经完成的当前阶段立论。"

    monkeypatch.setattr(debate_agent, "generate", generate)
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        deadline = now() + timedelta(seconds=10)
        room.current_stage_index = 0
        room.stage_started_at = now()
        room.stage_deadline_at = deadline
        room.template_snapshot = [
            {
                "key": "neg_case",
                "name": "反方一辩立论",
                "kind": "announcement",
                "seat": "neg_1",
                "duration": 60,
                "cue": "下面进入反方一辩立论。",
                "host_announcement_pending": True,
                "host_target_kind": "speech",
                "host_target_duration_seconds": 60,
                "host_announcement_deadline_at": deadline.isoformat(),
            }
        ]
        db.commit()

    engine = MatchEngine()
    engine._ensure_runtime()
    await engine.process_room(code)
    assert code in engine._agent_prefetch_tasks
    await engine._agent_prefetch_tasks[code]
    assert len(calls) == 1
    assert calls[0]["current_stage"] == "反方一辩立论"

    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        hosted = dict(room.template_snapshot[0])
        hosted["host_announcement_deadline_at"] = (now() - timedelta(milliseconds=1)).isoformat()
        room.template_snapshot = [hosted]
        db.commit()
        assert await engine._open_hosted_stage(db, room, hosted) is True
        db.refresh(room)
        seat = next(item for item in room.seats if item.seat_key == "neg_1")
        cached = engine._take_agent_prefetch(db, room, room.template_snapshot[0], seat)
        assert cached and cached.content == "主持提示音播放期间已经完成的当前阶段立论。"


@pytest.mark.asyncio
async def test_prefetch_discards_changed_seat_and_pause_cancels_pending_work(
    register_user, monkeypatch
) -> None:
    owner = register_user("prefetch_invalidation_owner")
    code = _running_announcement_room(owner, "预取失效边界")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def generate(payload, *, provider_config=None):
        entered.set()
        await release.wait()
        return "这段文本不应进入已经变化的比赛。"

    monkeypatch.setattr(debate_agent, "generate", generate)
    engine = MatchEngine()
    engine._ensure_runtime()
    engine._schedule_next_agent_prefetch(code, 0)
    pending_task = engine._agent_prefetch_tasks[code]
    await entered.wait()
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.status = "paused"
        db.commit()
    await engine.process_room(code)
    assert code not in engine._agent_prefetch_tasks
    assert code not in engine._agent_prefetch_cache
    release.set()
    await asyncio.gather(pending_task, return_exceptions=True)

    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.status = "running"
        room.current_stage_index = 0
        seat = next(item for item in room.seats if item.seat_key == "neg_1")
        seat.occupant_type = "human"
        db.commit()
    engine._schedule_next_agent_prefetch(code, 0)
    assert code not in engine._agent_prefetch_tasks
    assert code not in engine._agent_prefetch_cache


@pytest.mark.asyncio
async def test_prefetch_provider_failure_is_attempted_once_per_fingerprint(
    register_user, monkeypatch
) -> None:
    owner = register_user("prefetch_failure_owner")
    code = _running_announcement_room(owner, "预取失败退避")
    calls = 0

    async def generate(payload, *, provider_config=None):
        nonlocal calls
        calls += 1
        raise ProviderError("上游暂时不可用", code="agent_unavailable", retryable=True)

    monkeypatch.setattr(debate_agent, "generate", generate)
    engine = MatchEngine()
    engine._ensure_runtime()
    engine._schedule_next_agent_prefetch(code, 0)
    await engine._agent_prefetch_tasks[code]
    engine._schedule_next_agent_prefetch(code, 0)

    assert calls == 1
    assert code not in engine._agent_prefetch_tasks
    assert code not in engine._agent_prefetch_cache


@pytest.mark.asyncio
async def test_prefetch_prepares_complete_stable_wav_and_atomically_adopts_it(
    register_user, monkeypatch
) -> None:
    owner = register_user("prefetch_audio_owner")
    code = _running_announcement_room(owner, "下一阶段完整语音预生成")

    async def generate(payload, *, provider_config=None):
        return "下一位辩手的正文和完整语音都应该在轮次到来前准备。"

    async def synthesize(
        _text: str,
        *,
        room_code: str,
        speech_id: str,
        publish_live: bool,
        **_kwargs,
    ) -> str:
        assert publish_live is False
        target = settings.media_path / room_code / f"{speech_id}.wav"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_wav_bytes(1.1))
        return f"/media/{room_code}/{speech_id}.wav"

    monkeypatch.setattr(settings, "realtime_voice_backend", "moss_realtime")
    monkeypatch.setattr(settings, "moss_tts_realtime_enabled", True)
    monkeypatch.setattr(settings, "moss_tts_stable_playback_enabled", True)
    monkeypatch.setattr(settings, "moss_tts_playback_speed", 1.1)
    monkeypatch.setattr(debate_agent, "generate", generate)
    monkeypatch.setattr(moss_tts_realtime, "synthesize", synthesize)
    engine = MatchEngine()
    engine._ensure_runtime()
    engine._schedule_next_agent_prefetch(code, 0)
    await engine._agent_prefetch_tasks[code]

    cached = engine._agent_prefetch_cache[code]
    assert cached.audio_asset_id
    assert (settings.media_path / code / f"{cached.audio_asset_id}.wav").is_file()
    assert (settings.media_path / code / f"{cached.audio_asset_id}.source.wav").is_file()
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.current_stage_index = 1
        seat = next(item for item in room.seats if item.seat_key == "neg_1")
        taken = engine._take_agent_prefetch(db, room, room.template_snapshot[1], seat)
        assert taken and taken.audio_asset_id == cached.audio_asset_id

    adopted = _adopt_prefetched_audio(code, cached.audio_asset_id, "formal-speech-id")
    assert adopted == f"/media/{code}/formal-speech-id.wav"
    assert (settings.media_path / code / "formal-speech-id.wav").is_file()
    assert (settings.media_path / code / "formal-speech-id.source.wav").is_file()


@pytest.mark.asyncio
async def test_formal_turn_waits_for_same_room_audio_prefetch_instead_of_restarting(
    register_user, monkeypatch
) -> None:
    owner = register_user("prefetch_formal_handoff_owner")
    code = _running_announcement_room(owner, "预生成任务必须无缝交给正式轮次")
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def generate(payload, *, provider_config=None):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return "这段完整发言只生成一次，并在正式阶段直接采用已经准备好的语音。"

    async def synthesize(
        _text: str,
        *,
        room_code: str,
        speech_id: str,
        publish_live: bool,
        **_kwargs,
    ) -> str:
        assert publish_live is False
        target = settings.media_path / room_code / f"{speech_id}.wav"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_wav_bytes(1.1))
        return f"/media/{room_code}/{speech_id}.wav"

    monkeypatch.setattr(settings, "realtime_voice_backend", "moss_realtime")
    monkeypatch.setattr(settings, "moss_tts_realtime_enabled", True)
    monkeypatch.setattr(settings, "moss_tts_stable_playback_enabled", True)
    monkeypatch.setattr(settings, "moss_tts_playback_speed", 1.1)
    monkeypatch.setattr(debate_agent, "generate", generate)
    monkeypatch.setattr(moss_tts_realtime, "synthesize", synthesize)
    monkeypatch.setattr("app.services.match_engine.livekit_audio_enabled", lambda: False)

    engine = MatchEngine()
    engine._ensure_runtime()
    engine._schedule_next_agent_prefetch(code, 0)
    await entered.wait()

    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.current_stage_index = 1
        room.stage_started_at = now()
        room.stage_deadline_at = now() + timedelta(seconds=60)
        db.commit()
        seat = next(item for item in room.seats if item.seat_key == "neg_1")
        formal = asyncio.create_task(
            engine._ai_speech(db, room, seat, room.template_snapshot[1])
        )
        await asyncio.sleep(0)
        assert not formal.done()
        release.set()
        await formal

    assert calls == 1
    with SessionLocal() as db:
        speech = db.scalar(
            select(Speech).where(Speech.room_id == load_room(db, code).id).order_by(Speech.created_at.desc())
        )
        assert speech is not None
        assert speech.status == "playing"
        assert speech.audio_url
        events = db.scalars(
            select(MatchEvent).where(MatchEvent.room_id == speech.room_id).order_by(MatchEvent.seq)
        ).all()
        assert any(event.event_type == "speech.content.prefetched" for event in events)
        # This focused handoff test does not run the engine clock until the
        # synthetic WAV ends. Do not leave an engine-owned in-flight row that
        # could contaminate later restart-recovery tests in the shared suite.
        speech.status = "completed"
        db.commit()
