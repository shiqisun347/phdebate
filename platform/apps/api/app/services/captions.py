from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from time import monotonic

from app.core.database import SessionLocal
from app.models.entities import CaptionSegment, Room, Speech
from app.services.realtime import room_hub
from app.services.voice_runtime.text import SpeakableClauseAssembler
from sqlalchemy import select
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_ESTIMATED_CAPTION_CHARACTERS_PER_SECOND = 5.2


def _segment_id(speech_id: str, index: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"phdebate:caption:{speech_id}:{index}"))


def _prefix_end(final_text: str, emitted_text: str) -> int | None:
    emitted = "".join(character for character in emitted_text if not character.isspace())
    if not emitted:
        return 0
    emitted_index = 0
    for final_index, character in enumerate(final_text):
        if character.isspace():
            continue
        if emitted_index >= len(emitted) or character != emitted[emitted_index]:
            return None
        emitted_index += 1
        if emitted_index == len(emitted):
            return final_index + 1
    return None


def audio_caption_clauses(text: str) -> list[str]:
    """Split an immutable AI answer into readable, single-line clauses.

    Stable WAV playback receives the whole Agent answer before synthesis, so
    Agent-delta timestamps are unrelated to what the listener is currently
    hearing.  Reuse the voice pipeline's punctuation-aware clause assembler,
    but calculate presentation offsets only after the final WAV duration is
    known.
    """

    assembler = SpeakableClauseAssembler()
    return [chunk.strip() for chunk in [*assembler.feed(text), *assembler.finish()] if chunk.strip()]


def sync_audio_timed_agent_captions(
    db: Session,
    room: Room,
    speech: Speech,
    text: str,
    duration_seconds: float,
) -> list[CaptionSegment]:
    """Create an idempotent, playback-timed caption projection for one WAV.

    This is presentation metadata only.  It deliberately does not create ASR
    transcript timing or publish the full answer.  Offsets are proportional
    to readable character weight and are consumed against the authoritative
    ``Speech.playback_started_at`` timestamp in the browser.
    """

    if speech.room_id != room.id or duration_seconds <= 0:
        return []
    clauses = audio_caption_clauses(text)
    if not clauses:
        return []

    duration_ms = max(1, round(duration_seconds * 1000))

    def weight(clause: str) -> int:
        # Whitespace is not spoken. Strong punctuation carries a small pause,
        # which keeps the following subtitle from appearing too early.
        readable = sum(1 for character in clause if not character.isspace())
        pauses = sum(2 for character in clause if character in "。！？!?；;")
        return max(1, readable + pauses)

    weights = [weight(clause) for clause in clauses]
    total_weight = sum(weights)
    offsets: list[int] = []
    elapsed_weight = 0
    for item_weight in weights:
        offsets.append(min(duration_ms - 1, round(duration_ms * elapsed_weight / total_weight)))
        elapsed_weight += item_weight

    existing = {
        item.ordinal: item
        for item in db.scalars(
            select(CaptionSegment).where(
                CaptionSegment.speech_id == speech.id,
                CaptionSegment.source == "agent",
            )
        )
    }
    result: list[CaptionSegment] = []
    for ordinal, (clause, offset_ms) in enumerate(zip(clauses, offsets, strict=True), start=1):
        item = existing.pop(ordinal, None)
        if item is None:
            item = CaptionSegment(
                id=_segment_id(speech.id, ordinal),
                room_id=room.id,
                speech_id=speech.id,
                seat_key=speech.seat_key,
                source="agent",
                ordinal=ordinal,
                text=clause,
                is_final=True,
                timing_basis="audio_duration",
                presentation_offset_ms=offset_ms,
            )
            db.add(item)
        else:
            item.room_id = room.id
            item.seat_key = speech.seat_key
            item.text = clause
            item.is_final = True
            item.timing_basis = "audio_duration"
            item.presentation_offset_ms = offset_ms
        result.append(item)
    for stale in existing.values():
        db.delete(stale)
    return result


@dataclass
class StreamingCaptionWriter:
    """Persist and publish stable AI caption clauses without touching TTS.

    The realtime voice pipeline remains authoritative for synthesis. This
    writer observes the same Agent deltas with its own sentence assembler so
    authorized participants receive readable clauses instead of a final
    full-text flash. Spectator projections deliberately discard these rows.
    Writes are chained in background tasks: the Agent/TTS iterator only
    enqueues them, while ``finish`` drains the chain after TTS has already
    received the final event.
    """

    room_code: str
    speech_id: str
    seat_key: str
    assembler: SpeakableClauseAssembler = field(default_factory=SpeakableClauseAssembler)
    started_at: float = field(default_factory=monotonic)
    segment_index: int = 0
    emitted_text: str = ""
    submitted_playback_ms: int = 0
    _tail: asyncio.Task[None] | None = field(default=None, init=False, repr=False)

    async def feed(self, delta: str) -> None:
        await self._write(self.assembler.feed(delta))

    async def submit(self, chunk: str) -> None:
        """Publish one stable phrase after the TTS session accepts it.

        Prefetched Agent answers bypass the live-delta iterator.  Observing the
        exact phrases submitted to the one continuous TTS context keeps those
        turns captioned too, without exposing text that synthesis rejected or
        adding persistence work to the audio transport itself.
        """

        normalized = chunk.strip()
        if not normalized:
            return
        start_ms = self.submitted_playback_ms
        readable = sum(1 for character in normalized if not character.isspace())
        pauses = sum(2 for character in normalized if character in "。！？!?；;")
        estimated_duration_ms = max(
            250,
            round((readable + pauses) / _ESTIMATED_CAPTION_CHARACTERS_PER_SECOND * 1000),
        )
        self.submitted_playback_ms += estimated_duration_ms
        await self._write(
            [normalized],
            timing_basis="estimated_playback",
            offsets=[start_ms],
        )

    async def finish(self, final_text: str = "") -> None:
        if final_text:
            prefix_end = _prefix_end(final_text, self.emitted_text)
            if prefix_end is not None:
                self.assembler.replace_buffer(final_text[prefix_end:])
            else:
                self.assembler.replace_buffer("")
        await self._write(self.assembler.finish(), drain=True)

    async def _write(
        self,
        chunks: list[str],
        *,
        drain: bool = False,
        timing_basis: str = "agent_text",
        offsets: list[int] | None = None,
    ) -> None:
        for chunk_index, chunk in enumerate(chunks):
            normalized = chunk.strip()
            if not normalized:
                continue
            self.emitted_text += normalized
            start_ms = (
                max(0, offsets[chunk_index])
                if offsets is not None and chunk_index < len(offsets)
                else max(0, round((monotonic() - self.started_at) * 1000))
            )
            self.segment_index += 1
            segment_id = _segment_id(self.speech_id, self.segment_index)
            previous = self._tail

            async def persist_then_publish(
                *,
                prior: asyncio.Task[None] | None = previous,
                ordinal: int = self.segment_index,
                item_id: str = segment_id,
                text: str = normalized,
                offset_ms: int = start_ms,
                basis: str = timing_basis,
            ) -> None:
                if prior:
                    try:
                        await prior
                    except Exception:
                        # One failed caption row must not suppress later text.
                        pass
                try:
                    persisted = await asyncio.to_thread(
                        _persist_agent_caption,
                        item_id,
                        self.room_code,
                        self.speech_id,
                        self.seat_key,
                        ordinal,
                        text,
                        offset_ms,
                        basis,
                    )
                except Exception:
                    logger.exception("caption persistence failed", extra={"speech_id": self.speech_id})
                    return
                if not persisted:
                    return
                await room_hub.publish(
                    self.room_code,
                    {
                        "type": "caption.segment",
                        "room_code": self.room_code,
                        "speech_id": self.speech_id,
                        "seat_key": self.seat_key,
                        "segment_id": item_id,
                        "text": text,
                        "is_final": True,
                        "start_ms": offset_ms,
                        "end_ms": offset_ms,
                        "timing_basis": basis,
                    },
                )

            self._tail = asyncio.create_task(persist_then_publish())

        if self._tail and drain:
            # ``finish`` is the only call that empties the assembler after the
            # final Agent event. Waiting here cannot delay first audio.
            await self._tail


def _persist_agent_caption(
    segment_id: str,
    room_code: str,
    speech_id: str,
    seat_key: str,
    ordinal: int,
    text: str,
    offset_ms: int,
    timing_basis: str = "agent_text",
) -> bool:
    """Idempotently persist a display projection outside the event loop."""

    with SessionLocal() as db:
        speech = db.get(Speech, speech_id)
        if not speech or speech.seat_key != seat_key:
            return False
        room = db.get(Room, speech.room_id)
        if not room or room.code != room_code:
            return False
        existing = db.get(CaptionSegment, segment_id)
        if existing:
            if existing.speech_id != speech_id:
                return False
            existing.text = text
            existing.presentation_offset_ms = offset_ms
            existing.timing_basis = timing_basis
        else:
            db.add(
                CaptionSegment(
                    id=segment_id,
                    room_id=speech.room_id,
                    speech_id=speech_id,
                    seat_key=seat_key,
                    source="agent",
                    ordinal=ordinal,
                    text=text,
                    is_final=True,
                    timing_basis=timing_basis,
                    presentation_offset_ms=offset_ms,
                )
            )
        db.commit()
        return True
