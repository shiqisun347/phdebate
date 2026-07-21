from __future__ import annotations

import pytest
from app.api.realtime import persist_asr_final
from app.core.database import SessionLocal
from app.models.entities import CaptionSegment, Match, Speech, TranscriptSegment
from app.services.captions import StreamingCaptionWriter, _persist_agent_caption
from app.services.room_service import anonymous_event_payload, anonymous_realtime_event, load_room, serialize_room
from sqlalchemy import select
from test_platform import create_training_room


@pytest.mark.asyncio
async def test_ai_caption_writer_publishes_clauses_without_mutating_transcript(register_user, monkeypatch) -> None:
    owner = register_user("round20_caption_owner")
    code = create_training_room(owner, "Round20 AI 逐句字幕")['code']
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        match = Match(room_id=room.id, competition_id=room.competition_id, status="running")
        db.add(match)
        db.flush()
        speech = Speech(
            match_id=match.id,
            room_id=room.id,
            seat_key="neg_1",
            stage_key="neg_case",
            speaker_type="ai",
            status="synthesizing",
        )
        db.add(speech)
        db.commit()
        speech_id = speech.id

    published: list[dict] = []

    async def capture(_code: str, event: dict) -> None:
        published.append(event)

    monkeypatch.setattr("app.services.captions.room_hub.publish", capture)
    writer = StreamingCaptionWriter(code, speech_id, "neg_1")
    await writer.feed("第一句已经完整。第二句仍在")
    await writer.finish("第一句已经完整。第二句仍在继续。")

    with SessionLocal() as db:
        room = load_room(db, code)
        stored_speech = db.get(Speech, speech_id)
        assert stored_speech is not None
        stored_speech.content = "观众不可回溯的完整文字稿"
        stored_speech.audio_url = "/api/media/speech.wav"
        db.commit()
        # AI display timing is persisted only in the presentation table. It
        # must never masquerade as audio-aligned research transcript data.
        assert list(db.scalars(select(TranscriptSegment).where(TranscriptSegment.speech_id == speech_id))) == []
        persisted = list(
            db.scalars(
                select(CaptionSegment)
                .where(CaptionSegment.speech_id == speech_id)
                .order_by(CaptionSegment.ordinal)
            )
        )
        assert [item.text for item in persisted] == ["第一句已经完整。", "第二句仍在继续。"]
        snapshot = serialize_room(db, room, None, public=True)
        assert snapshot["active_speech"]["content"] == ""
        assert snapshot["speeches"][0]["content"] == ""
        assert snapshot["speeches"][0]["audio_url"] == "/api/media/speech.wav"
        assert [item["text"] for item in snapshot["caption_segments"]] == ["第一句已经完整。", "第二句仍在继续。"]
        assert {item["timing_basis"] for item in snapshot["caption_segments"]} == {"agent_text"}
    assert [item["text"] for item in published] == ["第一句已经完整。", "第二句仍在继续。"]
    assert all(item["timing_basis"] == "agent_text" for item in published)


def test_caption_persistence_rejects_cross_room_projection_and_declares_cascade(register_user) -> None:
    owner = register_user("round20_caption_scope_owner")
    other_owner = register_user("round20_caption_scope_other")
    first_code = create_training_room(owner, "字幕房间隔离一")["code"]
    second_code = create_training_room(other_owner, "字幕房间隔离二")["code"]
    with SessionLocal() as db:
        room = load_room(db, first_code, lock=True)
        match = Match(room_id=room.id, competition_id=room.competition_id, status="running")
        db.add(match)
        db.flush()
        speech = Speech(
            match_id=match.id,
            room_id=room.id,
            seat_key="aff_1",
            stage_key="aff_case",
            speaker_type="ai",
            status="synthesizing",
        )
        db.add(speech)
        db.commit()
        speech_id = speech.id

    segment_id = "0d809f06-7639-4fb1-b5fb-78a291940f31"
    assert _persist_agent_caption(segment_id, second_code, speech_id, "aff_1", 1, "不应串房。", 20) is False
    assert _persist_agent_caption(segment_id, first_code, speech_id, "aff_1", 1, "本房字幕。", 20) is True
    with SessionLocal() as db:
        assert db.get(CaptionSegment, segment_id) is not None
    speech_fk = next(key for key in CaptionSegment.__table__.foreign_keys if key.target_fullname == "speeches.id")
    room_fk = next(key for key in CaptionSegment.__table__.foreign_keys if key.target_fullname == "rooms.id")
    assert speech_fk.ondelete == "CASCADE"
    assert room_fk.ondelete == "CASCADE"


def test_public_caption_event_keeps_only_presentation_fields() -> None:
    projected = anonymous_realtime_event(
        {
            "type": "caption.segment",
            "room_code": "123456",
            "speech_id": "speech-1",
            "seat_key": "aff_1",
            "segment_id": "segment-1",
            "text": "逐句字幕。",
            "is_final": True,
            "start_ms": 120,
            "end_ms": 840,
            "timing_basis": "agent_text",
            "provider_debug": "secret",
        }
    )
    assert projected == {
        "type": "caption.segment",
        "speech_id": "speech-1",
        "seat_key": "aff_1",
        "segment_id": "segment-1",
        "text": "逐句字幕。",
        "is_final": True,
        "start_ms": 120,
        "end_ms": 840,
        "timing_basis": "agent_text",
    }


def test_public_match_event_never_exposes_authoritative_transcript_content() -> None:
    projected = anonymous_event_payload(
        "speech.completed",
        {
            "speech_id": "speech-1",
            "seat_key": "aff_1",
            "speaker_type": "human",
            "content": "不可向观众回放的完整文字稿。",
            "audio_url": "/media/room/speech-1.wav",
        },
    )
    assert projected == {
        "speech_id": "speech-1",
        "seat_key": "aff_1",
        "speaker_type": "human",
        "audio_url": "/media/room/speech-1.wav",
    }


def test_asr_caption_projection_is_recoverable_and_duplicate_safe(register_user) -> None:
    owner = register_user("round20_caption_asr_owner")
    code = create_training_room(owner, "ASR 字幕恢复")["code"]
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.status = "running"
        match = Match(room_id=room.id, competition_id=room.competition_id, status="running")
        db.add(match)
        db.flush()
        speech = Speech(
            match_id=match.id,
            room_id=room.id,
            seat_key="aff_1",
            stage_key="aff_case",
            speaker_type="human",
            status="speaking",
        )
        db.add(speech)
        db.commit()
        speech_id = speech.id
    assert persist_asr_final(speech_id, "人类发言第一句。") is True
    assert persist_asr_final(speech_id, "人类发言第一句。") is True
    with SessionLocal() as db:
        room = load_room(db, code)
        transcripts = list(db.scalars(select(TranscriptSegment).where(TranscriptSegment.speech_id == speech_id)))
        captions = list(db.scalars(select(CaptionSegment).where(CaptionSegment.speech_id == speech_id)))
        snapshot = serialize_room(db, room, None, public=True)
    assert [item.text for item in transcripts] == ["人类发言第一句。"]
    assert [item.text for item in captions] == ["人类发言第一句。"]
    assert snapshot["caption_segments"][0]["timing_basis"] == "asr"
