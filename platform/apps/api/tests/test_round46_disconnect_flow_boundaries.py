from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import pytest
from app.api import realtime as realtime_api
from app.core.database import SessionLocal
from app.models.entities import CaptionSegment, Match, MatchEvent, Speech, TranscriptSegment
from app.services.match_engine import match_engine
from app.services.room_service import load_room, now
from conftest import csrf
from sqlalchemy import func, select
from test_platform import asr_authentication, asr_ready, create_training_room, prepare_human_speech


def _ready_and_start(owner, code: str, *participants) -> None:
    for browser in (owner, *participants):
        response = browser.post(f"/api/rooms/{code}/ready", headers=csrf(browser), json={"ready": True})
        assert response.status_code == 200, response.text
    response = owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    assert response.status_code == 200, response.text


def _two_human_room(owner, participant, topic: str) -> str:
    code = create_training_room(owner, topic)["code"]
    claimed = participant.post(
        f"/api/rooms/{code}/claim-seat",
        headers=csrf(participant),
        json={"seat_key": "neg_1"},
    )
    assert claimed.status_code == 200, claimed.text
    _ready_and_start(owner, code, participant)
    return code


@pytest.mark.asyncio
async def test_two_simultaneous_disconnect_timeouts_are_idempotent_and_never_auto_resume(register_user) -> None:
    owner = register_user("round46_two_timeout_owner")
    participant = register_user("round46_two_timeout_guest")
    code = _two_human_room(owner, participant, "Round46 多真人同时断线幂等")

    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.status = "running"
        room.current_stage_index = 0
        room.template_snapshot = [{"key": "hold", "name": "真人在线检查", "kind": "announcement", "duration": 180}]
        room.stage_started_at = now()
        room.stage_deadline_at = now() + timedelta(seconds=180)
        disconnected_at = now() - timedelta(seconds=61)
        for seat in room.seats:
            if seat.occupant_type == "human":
                seat.connected = False
                seat.disconnected_at = disconnected_at
        db.commit()

    await match_engine.process_room(code)
    with SessionLocal() as db:
        room = load_room(db, code)
        first_seq = room.seq
        timeout_events = list(
            db.scalars(
                select(MatchEvent).where(
                    MatchEvent.room_id == room.id,
                    MatchEvent.event_type == "participant.disconnect_timeout",
                )
            ).all()
        )
        assert room.status == "paused"
        assert len(timeout_events) == 2
        assert len({item.idempotency_key for item in timeout_events}) == 2
        assert all(item.idempotency_key and len(item.idempotency_key) <= 120 for item in timeout_events)
        assert (
            db.scalar(
                select(func.count(MatchEvent.id)).where(
                    MatchEvent.room_id == room.id,
                    MatchEvent.event_type == "match.paused",
                )
            )
            == 1
        )
        assert all(seat.occupant_type == "human" for seat in room.seats)
        assert not db.scalar(
            select(MatchEvent.id).where(
                MatchEvent.room_id == room.id,
                MatchEvent.event_type == "seat.ai_substituted",
            )
        )

    # Reprocessing the same expired leases must not append duplicate timeout
    # or pause events.
    await match_engine.process_room(code)
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        assert room.seq == first_seq
        owner_seat = next(item for item in room.seats if item.user_id == room.owner_id)
        owner_seat.connected = True
        owner_seat.disconnected_at = None
        offline = next(item for item in room.seats if item.user_id != room.owner_id)
        offline_name = offline.display_name
        # Simulate a process restart resetting the timestamp for the same
        # still-offline episode. It must not create a second timeout event.
        offline.disconnected_at = now() - timedelta(seconds=61)
        db.commit()

    await match_engine.process_room(code)
    with SessionLocal() as db:
        room = load_room(db, code)
        assert room.seq == first_seq
        assert (
            db.scalar(
                select(func.count(MatchEvent.id)).where(
                    MatchEvent.room_id == room.id,
                    MatchEvent.event_type == "participant.disconnect_timeout",
                )
            )
            == 2
        )

    for action in ("resume", "retry"):
        blocked = owner.post(
            f"/api/rooms/{code}/control/{action}",
            headers=csrf(owner),
            json={"reason": "仍有真人离线时不得恢复"},
        )
        assert blocked.status_code == 409
        assert offline_name in blocked.json()["detail"]

    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        offline = next(item for item in room.seats if item.user_id != room.owner_id)
        offline.connected = True
        offline.disconnected_at = None
        db.commit()

    # Reconnection alone is not a state transition.
    still_paused = owner.get(f"/api/rooms/{code}")
    assert still_paused.status_code == 200
    assert still_paused.json()["room"]["status"] == "paused"
    resumed = owner.post(
        f"/api/rooms/{code}/control/resume",
        headers=csrf(owner),
        json={"reason": "全部真人已返回，房主确认继续"},
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["room"]["status"] == "running"


@pytest.mark.asyncio
async def test_disconnect_interrupts_human_speech_and_preserves_final_caption_fallback(register_user) -> None:
    owner = register_user("round46_caption_pause_owner")
    code = create_training_room(owner, "Round46 断线保留最终字幕")["code"]
    _ready_and_start(owner, code)
    owner_id = owner.get("/api/auth/session").json()["user"]["id"]

    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        seat = next(item for item in room.seats if item.user_id == owner_id)
        room.status = "running"
        room.current_stage_index = 0
        room.template_snapshot = [{"key": "human", "name": "真人发言", "kind": "speech", "seat": seat.seat_key, "duration": 90}]
        room.stage_started_at = now()
        room.stage_deadline_at = now() + timedelta(seconds=90)
        speech = Speech(
            match_id=match.id,
            room_id=room.id,
            seat_key=seat.seat_key,
            stage_key="human",
            speaker_type="human",
            status="speaking",
        )
        db.add(speech)
        db.flush()
        db.add(
            CaptionSegment(
                room_id=room.id,
                speech_id=speech.id,
                seat_key=seat.seat_key,
                source="asr",
                ordinal=1,
                text="这是断线前已经确认的最终字幕。",
                is_final=True,
                timing_basis="asr",
            )
        )
        seat.connected = False
        seat.disconnected_at = now() - timedelta(seconds=61)
        db.commit()
        speech_id = speech.id

    await match_engine.process_room(code)
    with SessionLocal() as db:
        room = load_room(db, code)
        speech = db.get(Speech, speech_id)
        assert room.status == "paused"
        assert speech.status == "interrupted"
        assert speech.content == "这是断线前已经确认的最终字幕。"
        assert not db.scalar(select(TranscriptSegment.id).where(TranscriptSegment.speech_id == speech_id))

    # Once interrupted, a delayed ASR final cannot extend the authoritative
    # transcript after the pause boundary.
    assert realtime_api.persist_asr_final(speech_id, "这是一条暂停后才到达的迟到识别结果。") is False
    with SessionLocal() as db:
        assert db.get(Speech, speech_id).content == "这是断线前已经确认的最终字幕。"


def test_asr_bridge_never_reports_rejected_racing_final_as_success(register_user, monkeypatch) -> None:
    class FinalUpstream:
        def __init__(self) -> None:
            self.messages: asyncio.Queue[str] | None = None

        async def __aenter__(self):
            self.messages = asyncio.Queue()
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def send(self, payload: bytes | str) -> None:
            if payload == "STOP":
                assert self.messages is not None
                await self.messages.put(json.dumps({"is_final": True, "text": "暂停竞态中的迟到最终识别。"}, ensure_ascii=False))

        def __aiter__(self):
            return self

        async def __anext__(self):
            assert self.messages is not None
            return await self.messages.get()

    monkeypatch.setattr(realtime_api.websockets, "connect", lambda *_args, **_kwargs: FinalUpstream())
    # Force the pre-write validation to represent the last successful check;
    # the persisted speech state below is the authoritative post-check pause.
    monkeypatch.setattr(realtime_api, "_asr_stream_active", lambda *_args, **_kwargs: True)
    owner = register_user("round46_asr_final_race")
    code, speech_id = prepare_human_speech(owner)

    with owner.websocket_connect(f"/ws/rooms/{code}/asr") as socket:
        socket.send_json(asr_authentication(speech_id))
        assert socket.receive_json() == asr_ready(speech_id)
        voiced_pcm = ((4000).to_bytes(2, "little", signed=True) + (-4000).to_bytes(2, "little", signed=True)) * 4096
        socket.send_bytes(voiced_pcm)
        with SessionLocal() as db:
            speech = db.get(Speech, speech_id)
            speech.status = "interrupted"
            db.commit()
        socket.send_json({"type": "finish"})
        rejected = socket.receive_json()
        assert rejected["type"] == "asr_rejected"
        assert rejected["reason"] == "speech_inactive"

    with SessionLocal() as db:
        speech = db.get(Speech, speech_id)
        assert speech.content == ""
        assert not db.scalar(select(TranscriptSegment.id).where(TranscriptSegment.speech_id == speech_id))


@pytest.mark.asyncio
async def test_disconnect_pause_isolated_from_another_room_stage_advance(register_user) -> None:
    paused_owner = register_user("round46_isolated_pause_owner")
    healthy_owner = register_user("round46_isolated_healthy_owner")
    paused_code = create_training_room(paused_owner, "Round46 断线暂停隔离场")["code"]
    healthy_code = create_training_room(healthy_owner, "Round46 正常推进隔离场")["code"]
    _ready_and_start(paused_owner, paused_code)
    _ready_and_start(healthy_owner, healthy_code)
    paused_owner_id = paused_owner.get("/api/auth/session").json()["user"]["id"]

    with SessionLocal() as db:
        paused_room = load_room(db, paused_code, lock=True)
        paused_room.status = "running"
        paused_room.current_stage_index = 0
        paused_room.template_snapshot = [{"key": "pause_here", "name": "等待断线暂停", "kind": "announcement", "duration": 90}]
        paused_room.stage_started_at = now()
        paused_room.stage_deadline_at = now() + timedelta(seconds=90)
        seat = next(item for item in paused_room.seats if item.user_id == paused_owner_id)
        seat.connected = False
        seat.disconnected_at = now() - timedelta(seconds=61)

        healthy_room = load_room(db, healthy_code, lock=True)
        healthy_room.status = "running"
        healthy_room.current_stage_index = 0
        healthy_room.template_snapshot = [
            {"key": "finished", "name": "已结束提示", "kind": "announcement", "duration": 1},
            {"key": "next", "name": "下一正常环节", "kind": "announcement", "duration": 60},
        ]
        healthy_room.stage_started_at = now() - timedelta(seconds=2)
        healthy_room.stage_deadline_at = now() - timedelta(milliseconds=1)
        db.commit()

    await asyncio.gather(
        match_engine.process_room(paused_code),
        match_engine.process_room(healthy_code),
    )

    with SessionLocal() as db:
        paused_room = load_room(db, paused_code)
        healthy_room = load_room(db, healthy_code)
        assert paused_room.status == "paused"
        assert paused_room.current_stage_index == 0
        assert healthy_room.status == "running"
        assert healthy_room.current_stage_index == 1
        assert not db.scalar(
            select(MatchEvent.id).where(
                MatchEvent.room_id == healthy_room.id,
                MatchEvent.event_type == "participant.disconnect_timeout",
            )
        )
        assert not db.scalar(
            select(MatchEvent.id).where(
                MatchEvent.room_id == paused_room.id,
                MatchEvent.event_type == "stage.completed",
            )
        )


@pytest.mark.asyncio
async def test_unrelated_human_timeout_interrupts_active_permanent_ai_playback(client, register_user) -> None:
    owner = register_user("round46_active_ai_pause_owner")
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
    _ready_and_start(owner, code)
    owner_id = owner.get("/api/auth/session").json()["user"]["id"]

    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        human = next(item for item in room.seats if item.user_id == owner_id)
        ai_seat = next(item for item in room.seats if item.occupant_type == "ai")
        room.status = "running"
        room.current_stage_index = 0
        room.template_snapshot = [{"key": "active_ai", "name": "永久 AI 发言", "kind": "speech", "seat": ai_seat.seat_key, "duration": 90}]
        room.stage_started_at = now()
        room.stage_deadline_at = now() + timedelta(seconds=90)
        human.connected = False
        human.disconnected_at = now() - timedelta(seconds=61)
        speech = Speech(
            match_id=match.id,
            room_id=room.id,
            seat_key=ai_seat.seat_key,
            stage_key="active_ai",
            speaker_type="ai",
            content="这段永久 AI 发言正在播放。",
            status="playing",
            playback_started_at=now() - timedelta(seconds=2),
            playback_ends_at=now() + timedelta(seconds=30),
        )
        db.add(speech)
        db.commit()
        speech_id = speech.id

    await match_engine.process_room(code)
    with SessionLocal() as db:
        room = load_room(db, code)
        speech = db.get(Speech, speech_id)
        assert room.status == "paused"
        assert speech.status == "interrupted"
        assert room.stage_deadline_at is None
        assert all(seat.occupant_type != "ai_substitute" for seat in room.seats)
        assert not db.scalar(
            select(Speech.id).where(
                Speech.room_id == room.id,
                Speech.stage_key == "active_ai",
                Speech.id != speech_id,
            )
        )
