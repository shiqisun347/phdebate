from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable, Generator, Iterator, Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any, ContextManager


@dataclass(frozen=True)
class CanaryStageTiming:
    stage: str
    stage_index: int
    duration_ms: float
    turn_elapsed_ms: float
    source_stage: str | None = None
    source_stage_index: int | None = None

    def __bool__(self) -> bool:
        # Stage markers must never make an audio-presence check pass.
        return False


@dataclass(frozen=True)
class CanaryAudioChunk:
    audio: Any
    source_stage: str
    source_stage_index: int
    source_stage_duration_ms: float
    decoder_stage_index: int
    decoder_yield_ms: float
    turn_elapsed_ms: float


@dataclass(frozen=True)
class _DecodeJob:
    tokens: Any
    timing: CanaryStageTiming | None
    ready_waiter: Callable[[], None] | None


@dataclass(frozen=True)
class _FlushJob:
    pass


@dataclass(frozen=True)
class _DecoderFailure:
    error: BaseException


@dataclass(frozen=True)
class _DecoderStopped:
    pass


class _BoundedDecoderWorker:
    """Single-owner decoder thread with bounded input and output queues."""

    def __init__(
        self,
        decoder: Any,
        *,
        observe_stages: bool,
        stage_timing: Callable[..., CanaryStageTiming],
        elapsed_ms: Callable[[], float],
        externally_cancelled: Callable[[], bool],
        decoder_execution_context: Callable[[], ContextManager[Any]],
        queue_size: int,
        poll_slice_seconds: float,
        join_timeout_seconds: float,
    ) -> None:
        if queue_size < 1:
            raise ValueError("decoder queue size must be at least one")
        if poll_slice_seconds <= 0 or join_timeout_seconds <= 0:
            raise ValueError("decoder worker timing limits must be positive")
        self.decoder = decoder
        self.observe_stages = observe_stages
        self.stage_timing = stage_timing
        self.elapsed_ms = elapsed_ms
        self.externally_cancelled = externally_cancelled
        self.decoder_execution_context = decoder_execution_context
        self.poll_slice_seconds = poll_slice_seconds
        self.join_timeout_seconds = join_timeout_seconds
        self.input_queue: queue.Queue[_DecodeJob | _FlushJob] = queue.Queue(maxsize=queue_size)
        self.output_queue: queue.Queue[Any] = queue.Queue(maxsize=max(4, queue_size * 4))
        self.cancel_requested = threading.Event()
        self.stopped = threading.Event()
        self.failure: BaseException | None = None
        self._finish_submitted = False
        self.thread = threading.Thread(
            target=self._run,
            name="openmoss-async-decoder",
            daemon=True,
        )
        self.thread.start()

    @property
    def alive(self) -> bool:
        return self.thread.is_alive()

    def submit(
        self,
        tokens: Any,
        timing: CanaryStageTiming | None,
        ready_waiter: Callable[[], None] | None,
    ) -> Iterator[Any]:
        if self._finish_submitted:
            raise RuntimeError("decoder worker is already finishing")
        yield from self._put_input(
            _DecodeJob(tokens=tokens, timing=timing, ready_waiter=ready_waiter)
        )
        yield from self.drain(wait_seconds=self.poll_slice_seconds)

    def finish(self) -> Iterator[Any]:
        if not self._finish_submitted:
            self._finish_submitted = True
            yield from self._put_input(_FlushJob())
        try:
            while not self.stopped.is_set() or not self.output_queue.empty():
                yield from self.drain(wait_seconds=self.poll_slice_seconds)
        finally:
            self._join()
        self._raise_failure()

    def abort(self) -> None:
        self.cancel_requested.set()
        self._clear_queue(self.input_queue)
        self._clear_queue(self.output_queue)
        self._join()

    def drain(self, *, wait_seconds: float = 0.0) -> Iterator[Any]:
        if self._cancelled():
            self._clear_queue(self.output_queue)
            return
        first = True
        while True:
            try:
                if first and wait_seconds > 0:
                    item = self.output_queue.get(timeout=wait_seconds)
                else:
                    item = self.output_queue.get_nowait()
            except queue.Empty:
                break
            first = False
            if isinstance(item, _DecoderFailure):
                self.failure = item.error
                continue
            if isinstance(item, _DecoderStopped):
                continue
            if not self._cancelled():
                yield item
        self._raise_failure()

    def _put_input(self, item: _DecodeJob | _FlushJob) -> Iterator[Any]:
        while True:
            self._raise_failure()
            if self._cancelled():
                return
            try:
                self.input_queue.put(item, timeout=self.poll_slice_seconds)
                return
            except queue.Full:
                yield from self.drain(wait_seconds=self.poll_slice_seconds)

    def _run(self) -> None:
        try:
            while not self._cancelled():
                try:
                    item = self.input_queue.get(timeout=self.poll_slice_seconds)
                except queue.Empty:
                    continue
                if isinstance(item, _FlushJob):
                    self._flush()
                    return
                self._decode(item)
        except BaseException as exc:
            self.failure = exc
            self._put_output(_DecoderFailure(exc))
        finally:
            self.stopped.set()
            self._put_output(_DecoderStopped())

    def _decode(self, job: _DecodeJob) -> None:
        with self.decoder_execution_context():
            if job.ready_waiter is not None:
                job.ready_waiter()
            if self._cancelled():
                return
            decoder_started_at = time.perf_counter() if self.observe_stages else 0.0
            self.decoder.push_tokens(job.tokens)
            decoder_chunks = iter(self.decoder.audio_chunks())
            while not self._cancelled():
                try:
                    wav = next(decoder_chunks)
                except StopIteration:
                    return
                if wav.numel() <= 0:
                    continue
                audio = wav.detach().cpu()
                if job.timing is None:
                    if not self._put_output(audio):
                        return
                else:
                    decoder_timing = self.stage_timing(
                        "decoder_yield",
                        decoder_started_at,
                        source_stage=job.timing.stage,
                        source_stage_index=job.timing.stage_index,
                    )
                    if not self._put_output(decoder_timing):
                        return
                    if not self._put_output(
                        CanaryAudioChunk(
                            audio=audio,
                            source_stage=job.timing.stage,
                            source_stage_index=job.timing.stage_index,
                            source_stage_duration_ms=job.timing.duration_ms,
                            decoder_stage_index=decoder_timing.stage_index,
                            decoder_yield_ms=decoder_timing.duration_ms,
                            turn_elapsed_ms=self.elapsed_ms(),
                        )
                    ):
                        return
                decoder_started_at = time.perf_counter() if self.observe_stages else 0.0

    def _flush(self) -> None:
        if self._cancelled():
            return
        started_at = time.perf_counter() if self.observe_stages else 0.0
        with self.decoder_execution_context():
            final = self.decoder.flush()
        flush_timing = self.stage_timing("flush", started_at) if self.observe_stages else None
        if flush_timing is not None and not self._put_output(flush_timing):
            return
        if final is None or final.numel() <= 0 or self._cancelled():
            return
        audio = final.detach().cpu()
        if flush_timing is None:
            self._put_output(audio)
        else:
            self._put_output(
                CanaryAudioChunk(
                    audio=audio,
                    source_stage=flush_timing.stage,
                    source_stage_index=flush_timing.stage_index,
                    source_stage_duration_ms=flush_timing.duration_ms,
                    decoder_stage_index=flush_timing.stage_index,
                    decoder_yield_ms=0.0,
                    turn_elapsed_ms=self.elapsed_ms(),
                )
            )

    def _put_output(self, item: Any) -> bool:
        while not self._cancelled():
            try:
                self.output_queue.put(item, timeout=self.poll_slice_seconds)
                return True
            except queue.Full:
                continue
        return False

    def _cancelled(self) -> bool:
        return self.cancel_requested.is_set() or self.externally_cancelled()

    def _join(self) -> None:
        self.thread.join(self.join_timeout_seconds)
        if self.thread.is_alive():
            raise RuntimeError("decoder worker did not exit within join timeout")

    def _raise_failure(self) -> None:
        if self.failure is not None:
            raise RuntimeError("decoder worker failed") from self.failure

    @staticmethod
    def _clear_queue(target: queue.Queue[Any]) -> None:
        while True:
            try:
                target.get_nowait()
            except queue.Empty:
                return


class OpenMossLowLatencyTextStreamBridge:
    """Incrementally decode the streaming API pinned by this gateway.

    It preserves the verified upstream session's segmentation, sampling, EOS and
    invalid-token rules while decoding each prefill/step frame immediately. The
    optional bounded decoder worker is opt-in; its CUDA execution context and
    producer-to-consumer readiness wait are injected without importing torch.
    """

    def __init__(
        self,
        session: Any,
        decoder: Any,
        *,
        sanitize_audio_tokens: Callable[..., tuple[Any, bool]],
        codebook_size: int | None = None,
        audio_eos_token: int | None = None,
        batch_size: int = 1,
        observe_stages: bool = False,
        async_decoder: bool = False,
        decoder_queue_size: int = 2,
        decoder_poll_slice_seconds: float = 0.001,
        decoder_join_timeout_seconds: float = 5.0,
        decoder_execution_context: Callable[[], ContextManager[Any]] | None = None,
        prepare_decode: Callable[[Any], Callable[[], None] | None] | None = None,
    ) -> None:
        self.session = session
        self.decoder = decoder
        self.batch_size = int(batch_size)
        self._sanitize_audio_tokens = sanitize_audio_tokens
        if codebook_size is None:
            codebook_size = int(getattr(getattr(session, "codec", None), "codebook_size", 1024))
        if audio_eos_token is None:
            audio_eos_token = int(getattr(session.inferencer, "audio_eos_token", 1026))
        self.codebook_size = int(codebook_size)
        self.audio_eos_token = int(audio_eos_token)
        self._decode_stopped = False
        self._observe_stages = bool(observe_stages)
        self._turn_started_at = time.perf_counter() if self._observe_stages else 0.0
        self._next_stage_index = 0
        self._stage_lock = threading.Lock()
        self._prepare_decode = prepare_decode or (lambda _tokens: None)

        required = (
            "_extract_text_segments",
            "_pending_tokens",
            "_prefill_if_needed",
            "_prefilled",
            "_text_cache",
            "_text_ended",
            "_tokenize",
            "inferencer",
        )
        missing = [name for name in required if not hasattr(session, name)]
        if missing:
            raise RuntimeError(
                "OpenMOSS fixed streaming session contract changed: " + ", ".join(missing)
            )

        self._decoder_worker = (
            _BoundedDecoderWorker(
                decoder,
                observe_stages=self._observe_stages,
                stage_timing=self._stage_timing,
                elapsed_ms=self._elapsed_ms,
                externally_cancelled=self._externally_cancelled,
                decoder_execution_context=decoder_execution_context or nullcontext,
                queue_size=decoder_queue_size,
                poll_slice_seconds=decoder_poll_slice_seconds,
                join_timeout_seconds=decoder_join_timeout_seconds,
            )
            if async_decoder
            else None
        )

    def push_text_delta(self, delta: str) -> Iterator[Any]:
        self.session._text_cache += delta
        for segment in self.session._extract_text_segments(force=False):
            self.session._pending_tokens.extend(self.session._tokenize(segment))
        yield from self._drain_pending_tokens()

    def push_text_tokens(self, token_ids: Sequence[int]) -> Iterator[Any]:
        if not token_ids:
            return
        self.session._pending_tokens.extend(int(token) for token in token_ids)
        yield from self._drain_pending_tokens()

    def finish(self, *, drain_step: int = 1) -> Iterator[Any]:
        if drain_step < 1:
            raise ValueError("drain_step must be at least one")
        if self._cancelled():
            if self._decoder_worker is not None:
                self._decoder_worker.abort()
            return
        self.session._text_ended = True
        if self.session._text_cache:
            self.session._pending_tokens.extend(self.session._tokenize(self.session._text_cache))
            self.session._text_cache = ""
        yield from self._drain_pending_tokens()

        inferencer = self.session.inferencer
        while (
            self.session._prefilled
            and not self._decode_stopped
            and not self._cancelled()
            and not inferencer.is_finished
        ):
            frame, timing = self._timed_step(None)
            if timing is not None:
                yield timing
            if self._cancelled():
                return
            stop = yield from self._decode_audio_frame(frame, timing=timing)
            if stop:
                self._decode_stopped = True
                break

        if self._cancelled():
            if self._decoder_worker is not None:
                self._decoder_worker.abort()
            return
        if self._decoder_worker is not None:
            yield from self._decoder_worker.finish()
            return
        started_at = time.perf_counter() if self._observe_stages else 0.0
        final = self.decoder.flush()
        flush_timing = self._stage_timing("flush", started_at) if self._observe_stages else None
        if flush_timing is not None:
            yield flush_timing
        if final is not None and final.numel() > 0:
            audio = final.detach().cpu()
            if flush_timing is None:
                yield audio
            else:
                yield CanaryAudioChunk(
                    audio=audio,
                    source_stage=flush_timing.stage,
                    source_stage_index=flush_timing.stage_index,
                    source_stage_duration_ms=flush_timing.duration_ms,
                    decoder_stage_index=flush_timing.stage_index,
                    decoder_yield_ms=0.0,
                    turn_elapsed_ms=self._elapsed_ms(),
                )

    def _drain_pending_tokens(self) -> Iterator[Any]:
        if self._decode_stopped or self._cancelled():
            return

        started_at = time.perf_counter() if self._observe_stages else 0.0
        prefill_frames = self.session._prefill_if_needed()
        prefill_timing = (
            self._stage_timing("prefill", started_at)
            if self._observe_stages and prefill_frames
            else None
        )
        if prefill_timing is not None:
            yield prefill_timing
        for frame in prefill_frames:
            if self._cancelled():
                return
            stop = yield from self._decode_audio_frame(frame, timing=prefill_timing)
            if stop:
                self._decode_stopped = True
                return

        # The pinned session deliberately buffers short text until its prefill
        # threshold is reached. Calling step() before that point is invalid.
        if not self.session._prefilled:
            return

        inferencer = self.session.inferencer
        while (
            self.session._pending_tokens
            and not self._decode_stopped
            and not self._cancelled()
            and not inferencer.is_finished
        ):
            token = self.session._pending_tokens.pop(0)
            frame, timing = self._timed_step(token)
            if timing is not None:
                yield timing
            if self._cancelled():
                return
            stop = yield from self._decode_audio_frame(frame, timing=timing)
            if stop:
                self._decode_stopped = True
                return

    def _step(self, text_token: int | None) -> Any:
        return self.session.inferencer.step(
            text_token,
            temperature=self.session.temperature,
            top_p=self.session.top_p,
            top_k=self.session.top_k,
            do_sample=self.session.do_sample,
            repetition_penalty=self.session.repetition_penalty,
            repetition_window=self.session.repetition_window,
        )

    def _timed_step(self, text_token: int | None) -> tuple[Any, CanaryStageTiming | None]:
        if not self._observe_stages:
            return self._step(text_token), None
        started_at = time.perf_counter()
        frame = self._step(text_token)
        return frame, self._stage_timing("talker_step", started_at)

    def _decode_audio_frame(
        self,
        frame: Any,
        *,
        timing: CanaryStageTiming | None,
    ) -> Generator[Any, None, bool]:
        tokens = frame
        if tokens.dim() == 3:
            tokens = tokens[0]
        if tokens.dim() != 2:
            raise ValueError(
                f"Expected [B, C] or [1, C] audio tokens, got {tuple(tokens.shape)}"
            )
        if tokens.shape[0] != 1:
            raise ValueError(
                "This bridge currently supports batch_size=1 for decoding, "
                f"got batch={tokens.shape[0]}."
            )

        tokens, stop = self._sanitize_audio_tokens(
            tokens,
            codebook_size=self.codebook_size,
            audio_eos_token=self.audio_eos_token,
        )
        if tokens.numel() > 0:
            if self._decoder_worker is not None:
                detached = tokens.detach()
                try:
                    ready_waiter = self._prepare_decode(detached)
                except BaseException:
                    self._decoder_worker.abort()
                    raise
                yield from self._decoder_worker.submit(detached, timing, ready_waiter)
                return stop
            decoder_started_at = time.perf_counter() if self._observe_stages else 0.0
            self.decoder.push_tokens(tokens.detach())
            decoder_chunks = iter(self.decoder.audio_chunks())
            while True:
                try:
                    wav = next(decoder_chunks)
                except StopIteration:
                    break
                if self._cancelled():
                    return True
                if wav.numel() > 0:
                    audio = wav.detach().cpu()
                    if timing is None:
                        yield audio
                    else:
                        decoder_timing = self._stage_timing(
                            "decoder_yield",
                            decoder_started_at,
                            source_stage=timing.stage,
                            source_stage_index=timing.stage_index,
                        )
                        yield decoder_timing
                        yield CanaryAudioChunk(
                            audio=audio,
                            source_stage=timing.stage,
                            source_stage_index=timing.stage_index,
                            source_stage_duration_ms=timing.duration_ms,
                            decoder_stage_index=decoder_timing.stage_index,
                            decoder_yield_ms=decoder_timing.duration_ms,
                            turn_elapsed_ms=self._elapsed_ms(),
                        )
                    decoder_started_at = time.perf_counter() if self._observe_stages else 0.0
        return stop

    def _stage_timing(
        self,
        stage: str,
        started_at: float,
        *,
        source_stage: str | None = None,
        source_stage_index: int | None = None,
    ) -> CanaryStageTiming:
        completed_at = time.perf_counter()
        with self._stage_lock:
            timing = CanaryStageTiming(
                stage=stage,
                stage_index=self._next_stage_index,
                duration_ms=round(max(0.0, completed_at - started_at) * 1000, 3),
                turn_elapsed_ms=round(max(0.0, completed_at - self._turn_started_at) * 1000, 3),
                source_stage=source_stage,
                source_stage_index=source_stage_index,
            )
            self._next_stage_index += 1
        return timing

    def _elapsed_ms(self) -> float:
        return round(max(0.0, time.perf_counter() - self._turn_started_at) * 1000, 3)

    def _cancelled(self) -> bool:
        cancelled = self._externally_cancelled()
        if cancelled and self._decoder_worker is not None:
            self._decoder_worker.cancel_requested.set()
        return cancelled

    def _externally_cancelled(self) -> bool:
        cancel_event = getattr(self.session.inferencer, "cancel_event", None)
        return bool(cancel_event is not None and cancel_event.is_set())

    @property
    def decoder_worker_alive(self) -> bool:
        return bool(self._decoder_worker is not None and self._decoder_worker.alive)

    def abort(self) -> None:
        cancel_event = getattr(self.session.inferencer, "cancel_event", None)
        if cancel_event is not None:
            cancel_event.set()
        if self._decoder_worker is not None:
            self._decoder_worker.abort()

    def close(self) -> None:
        if self._decoder_worker is not None and self._decoder_worker.alive:
            self.abort()
