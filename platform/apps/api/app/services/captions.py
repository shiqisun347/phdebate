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

logger = logging.getLogger(__name__)


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


@dataclass
class StreamingCaptionWriter:
    """Persist and publish stable AI caption clauses without touching TTS.

    The realtime voice pipeline remains authoritative for synthesis. This
    writer observes the same Agent deltas with its own sentence assembler so
    spectators receive readable clauses instead of a final full-text flash.
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
    _tail: asyncio.Task[None] | None = field(default=None, init=False, repr=False)

    async def feed(self, delta: str) -> None:
        await self._write(self.assembler.feed(delta))

    async def finish(self, final_text: str = "") -> None:
        if final_text:
            prefix_end = _prefix_end(final_text, self.emitted_text)
            if prefix_end is not None:
                self.assembler.replace_buffer(final_text[prefix_end:])
            else:
                self.assembler.replace_buffer("")
        await self._write(self.assembler.finish(), drain=True)

    async def _write(self, chunks: list[str], *, drain: bool = False) -> None:
        for chunk in chunks:
            normalized = chunk.strip()
            if not normalized:
                continue
            self.emitted_text += normalized
            start_ms = max(0, round((monotonic() - self.started_at) * 1000))
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
                        "timing_basis": "agent_text",
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
                    timing_basis="agent_text",
                    presentation_offset_ms=offset_ms,
                )
            )
        db.commit()
        return True
