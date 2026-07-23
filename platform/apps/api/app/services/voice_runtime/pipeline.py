from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Protocol

from app.services.voice_runtime.text import SpeakableClauseAssembler

logger = logging.getLogger(__name__)


def _final_prefix_end(final_text: str, submitted_text: str) -> int | None:
    """Map an already-spoken prefix into final text, ignoring layout whitespace only."""

    submitted = "".join(character for character in submitted_text if not character.isspace())
    if not submitted:
        return 0
    submitted_index = 0
    for final_index, character in enumerate(final_text):
        if character.isspace():
            continue
        if submitted_index >= len(submitted) or character != submitted[submitted_index]:
            return None
        submitted_index += 1
        if submitted_index == len(submitted):
            return final_index + 1
    return None


class RealtimeVoiceError(RuntimeError):
    pass


class RealtimeVoiceSynthesisError(RealtimeVoiceError):
    def __init__(self, message: str, *, final_text: str, cause: BaseException | None = None) -> None:
        super().__init__(message)
        self.final_text = final_text
        self.cause = cause


class RealtimeVoiceInterrupted(RealtimeVoiceError):
    pass


class SpeechSynthesisSession(Protocol):
    async def prepare(self) -> None: ...

    async def push_text(self, text: str) -> None: ...

    async def finish(self) -> str: ...

    async def abort(self, reason: str = "") -> None: ...


class IncrementalVoicePipeline:
    """Feed stable Agent body text into one continuous synthesis session."""

    def __init__(
        self,
        assembler: SpeakableClauseAssembler | None = None,
        *,
        maximum_wait_seconds: float = 0.2,
    ) -> None:
        if maximum_wait_seconds <= 0:
            raise ValueError("maximum_wait_seconds must be positive")
        self.assembler = assembler or SpeakableClauseAssembler()
        self.maximum_wait_seconds = maximum_wait_seconds

    async def run(
        self,
        events: AsyncIterator[dict[str, Any]],
        session: SpeechSynthesisSession,
        *,
        should_cancel: Callable[[], bool | Awaitable[bool]] | None = None,
        on_interrupt: Callable[[], None | Awaitable[None]] | None = None,
        on_first_readable_delta: Callable[[], None | Awaitable[None]] | None = None,
        on_tts_start: Callable[[], None | Awaitable[None]] | None = None,
        on_text_submitted: Callable[[str], None | Awaitable[None]] | None = None,
        cancellation_poll_seconds: float = 0.05,
    ) -> tuple[str, str]:
        if cancellation_poll_seconds <= 0:
            raise ValueError("cancellation_poll_seconds must be positive")
        streamed_text = ""
        submitted_text = ""
        # The clause assembler deliberately ignores layout whitespace when it
        # chooses safe synthesis boundaries.  Keep a second, compact cursor so
        # each emitted clause can be mapped back onto the exact Agent source
        # slice before it is handed to TTS.  Without this mapping, a boundary
        # that lands on a space turns e.g. ``large language model`` into
        # ``largelanguagemodel`` across incremental pushes.
        submitted_compact_text = ""
        submitted_source_end = 0
        final_text = ""
        synthesis_error: BaseException | None = None
        pending_event: asyncio.Task[dict[str, Any]] | None = None
        first_readable_delta_observed = False
        tts_started = False

        async def notify_tts_start() -> None:
            nonlocal tts_started
            if tts_started:
                return
            tts_started = True
            if on_tts_start is not None:
                result = on_tts_start()
                if inspect.isawaitable(result):
                    await result

        async def abort_session(reason: str) -> None:
            parameters = inspect.signature(session.abort).parameters
            if "reason" in parameters:
                await session.abort(reason=reason)
            else:
                await session.abort()  # type: ignore[call-arg]

        async def push_chunks(chunks: list[str], *, source_text: str | None = None) -> None:
            nonlocal synthesis_error, submitted_text, submitted_compact_text, submitted_source_end
            if synthesis_error is not None:
                return
            try:
                for chunk in chunks:
                    source = streamed_text if source_text is None else source_text
                    compact_candidate = submitted_compact_text + chunk
                    mapped_end = _final_prefix_end(source, compact_candidate)
                    synthesis_chunk = (
                        source[submitted_source_end:mapped_end]
                        if mapped_end is not None and mapped_end >= submitted_source_end
                        else chunk
                    )
                    if not synthesis_chunk.strip():
                        continue
                    await notify_tts_start()
                    await session.push_text(synthesis_chunk)
                    submitted_text += synthesis_chunk
                    submitted_compact_text = compact_candidate
                    if mapped_end is not None:
                        submitted_source_end = mapped_end
                    if on_text_submitted is not None:
                        result = on_text_submitted(synthesis_chunk)
                        if inspect.isawaitable(result):
                            await result
            except BaseException as exc:
                if isinstance(exc, asyncio.CancelledError) or getattr(exc, "code", "") == "provider_cancelled":
                    raise
                synthesis_error = exc
                await abort_session("synthesis_failed")

        async def cancellation_requested() -> bool:
            if should_cancel is None:
                return False
            requested = should_cancel()
            return bool(await requested) if inspect.isawaitable(requested) else bool(requested)

        async def interrupt() -> None:
            try:
                if on_interrupt is not None:
                    result = on_interrupt()
                    if inspect.isawaitable(result):
                        await result
            except Exception:
                logger.warning("Agent interrupt callback failed", exc_info=True)
            await abort_session("match_interrupted")
            raise RealtimeVoiceInterrupted("Realtime voice generation was interrupted")

        try:
            iterator = events.__aiter__()
            soft_deadline: float | None = None
            while True:
                if pending_event is None:
                    pending_event = asyncio.create_task(anext(iterator))
                timeout: float | None = None
                if synthesis_error is None and self.assembler.buffered_text:
                    if soft_deadline is None:
                        soft_deadline = asyncio.get_running_loop().time() + self.maximum_wait_seconds
                    timeout = max(0.0, soft_deadline - asyncio.get_running_loop().time())
                if should_cancel is not None:
                    timeout = cancellation_poll_seconds if timeout is None else min(timeout, cancellation_poll_seconds)
                done, _pending = await asyncio.wait({pending_event}, timeout=timeout)
                if not done:
                    if await cancellation_requested():
                        await interrupt()
                    # Cancellation polling may wake this loop before the text
                    # deadline.  It must not prematurely fragment speech.
                    now_monotonic = asyncio.get_running_loop().time()
                    if soft_deadline is not None and now_monotonic >= soft_deadline:
                        await push_chunks(self.assembler.flush_deadline())
                        soft_deadline = now_monotonic + self.maximum_wait_seconds if self.assembler.buffered_text else None
                    continue
                try:
                    event = pending_event.result()
                except StopAsyncIteration:
                    pending_event = None
                    break
                pending_event = None
                if await cancellation_requested():
                    await interrupt()
                event_type = str(event.get("type") or "").strip().lower()
                if event_type == "delta":
                    raw_delta = event.get("delta")
                    if not isinstance(raw_delta, str) or not raw_delta:
                        continue
                    delta = raw_delta
                    if not first_readable_delta_observed:
                        first_readable_delta_observed = True
                        if on_first_readable_delta is not None:
                            result = on_first_readable_delta()
                            if inspect.isawaitable(result):
                                await result
                    streamed_text += delta
                    chunks = self.assembler.feed(delta)
                    await push_chunks(chunks)
                    if chunks:
                        soft_deadline = (
                            asyncio.get_running_loop().time() + self.maximum_wait_seconds if self.assembler.buffered_text else None
                        )
                    elif self.assembler.buffered_text and soft_deadline is None:
                        # Start at the first readable body delta, not only
                        # after the preferred ten-character floor is reached.
                        soft_deadline = asyncio.get_running_loop().time() + self.maximum_wait_seconds
                    continue
                if event_type == "final":
                    final_text = str(event.get("content") or "").strip()
            if await cancellation_requested():
                await interrupt()
            if not final_text:
                final_text = streamed_text.strip()
            if not final_text:
                raise RealtimeVoiceError("Agent stream produced no speakable text")
            final_prefix_end = _final_prefix_end(final_text, submitted_text)
            if submitted_text and final_prefix_end is None:
                raise RealtimeVoiceSynthesisError(
                    "Agent final revised text that may already have been synthesized",
                    final_text=final_text,
                )
            # The Agent may safely revise text that was still buffered locally;
            # only the prefix already accepted by the TTS session is immutable.
            # Replacing the buffer also prevents stale, unplayed deltas from
            # leaking into the final audio or persisted transcript.
            if synthesis_error is None:
                self.assembler.replace_buffer(final_text[final_prefix_end or 0 :])
                submitted_source_end = final_prefix_end or 0
            remaining_chunks = self.assembler.finish()
            if synthesis_error is None:
                try:
                    await push_chunks(remaining_chunks, source_text=final_text)
                    if await cancellation_requested():
                        await interrupt()
                    audio_url = await session.finish()
                except BaseException as exc:
                    if (
                        isinstance(exc, (asyncio.CancelledError, RealtimeVoiceInterrupted))
                        or getattr(exc, "code", "") == "provider_cancelled"
                    ):
                        raise
                    raise RealtimeVoiceSynthesisError(
                        "Speech synthesis failed after Agent text was finalized",
                        final_text=final_text,
                        cause=exc,
                    ) from exc
            else:
                raise RealtimeVoiceSynthesisError(
                    "Speech synthesis failed before Agent text was finalized",
                    final_text=final_text,
                    cause=synthesis_error,
                ) from synthesis_error
            return final_text, audio_url
        except BaseException:
            if pending_event is not None and not pending_event.done():
                pending_event.cancel()
                await asyncio.gather(pending_event, return_exceptions=True)
            await abort_session("pipeline_aborted")
            raise
