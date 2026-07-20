from __future__ import annotations

import threading
import time
from collections import deque

from moss_realtime_gateway.low_latency_bridge import (
    CanaryAudioChunk,
    CanaryStageTiming,
    OpenMossLowLatencyTextStreamBridge,
)


class FakeTensor:
    def __init__(self, name: str, shape: tuple[int, ...] = (1, 16)) -> None:
        self.name = name
        self.shape = shape

    def dim(self) -> int:
        return len(self.shape)

    def numel(self) -> int:
        result = 1
        for size in self.shape:
            result *= size
        return result

    def detach(self) -> FakeTensor:
        return self

    def cpu(self) -> FakeTensor:
        return self


class FakeInferencer:
    audio_eos_token = 1026

    def __init__(self, events: list[str], outputs: list[str] | None = None) -> None:
        self.events = events
        self.outputs = deque(outputs or [])
        self.cancel_event = threading.Event()
        self.finished = False

    @property
    def is_finished(self) -> bool:
        return self.cancel_event.is_set() or self.finished

    def prefill(self, tokens: list[int]) -> FakeTensor:
        self.events.append(f"prefill:{tokens}")
        return FakeTensor("prefill")

    def step(self, token, **_kwargs) -> FakeTensor:
        self.events.append(f"step:{token}")
        name = self.outputs.popleft() if self.outputs else f"token-{token}"
        if name == "eos":
            self.finished = True
        return FakeTensor(name)


class FakeSession:
    temperature = 0.8
    top_p = 0.6
    top_k = 30
    do_sample = True
    repetition_penalty = 1.1
    repetition_window = 50
    codec = None

    def __init__(
        self,
        inferencer: FakeInferencer,
        events: list[str],
        *,
        prefill_text_len: int = 1,
    ) -> None:
        self.inferencer = inferencer
        self.events = events
        self.prefill_text_len = prefill_text_len
        self._pending_tokens: list[int] = []
        self._prefilled = False
        self._text_cache = ""
        self._text_ended = False

    def _extract_text_segments(self, *, force: bool) -> list[str]:
        del force
        if not self._text_cache:
            return []
        segment = self._text_cache
        self._text_cache = ""
        return [segment]

    def _tokenize(self, text: str) -> list[int]:
        self.events.append(f"tokenize:{text}")
        return list(range(1, len(text) + 1))

    def _prefill_if_needed(self) -> list[FakeTensor]:
        if self._prefilled:
            return []
        if len(self._pending_tokens) < self.prefill_text_len and not self._text_ended:
            return []
        if not self._pending_tokens:
            return []
        count = len(self._pending_tokens) if self._text_ended else self.prefill_text_len
        tokens = [self._pending_tokens.pop(0) for _ in range(count)]
        self._prefilled = True
        return [self.inferencer.prefill(tokens)]


class FakeDecoder:
    def __init__(
        self,
        events: list[str],
        *,
        emit_immediately: bool = True,
        flush_value: FakeTensor | None = None,
    ) -> None:
        self.events = events
        self.emit_immediately = emit_immediately
        self.flush_value = flush_value
        self.pending: list[FakeTensor] = []

    def push_tokens(self, tokens: FakeTensor) -> None:
        self.events.append(f"decoder_push:{tokens.name}")
        self.pending.append(tokens)

    def audio_chunks(self):
        if not self.emit_immediately:
            return
        while self.pending:
            tokens = self.pending.pop(0)
            self.events.append(f"decoder_audio:{tokens.name}")
            yield FakeTensor(f"pcm:{tokens.name}", shape=(240,))

    def flush(self) -> FakeTensor | None:
        self.events.append("decoder_flush")
        return self.flush_value


def sanitize(tokens: FakeTensor, *, codebook_size: int, audio_eos_token: int):
    assert codebook_size == 1024
    assert audio_eos_token == 1026
    if tokens.name in {"eos", "invalid"}:
        return FakeTensor(tokens.name, shape=(0, 16)), True
    return tokens, False


def test_first_pcm_is_consumable_before_later_text_token_steps() -> None:
    events: list[str] = []
    inferencer = FakeInferencer(events)
    session = FakeSession(inferencer, events, prefill_text_len=1)
    decoder = FakeDecoder(events)
    bridge = OpenMossLowLatencyTextStreamBridge(
        session,
        decoder,
        sanitize_audio_tokens=sanitize,
    )

    chunks = bridge.push_text_delta("甲乙丙")
    first = next(chunks)

    assert first.name == "pcm:prefill"
    assert events == [
        "tokenize:甲乙丙",
        "prefill:[1]",
        "decoder_push:prefill",
        "decoder_audio:prefill",
    ]
    assert [chunk.name for chunk in chunks] == ["pcm:token-2", "pcm:token-3"]
    assert events.index("decoder_audio:prefill") < events.index("step:2")


def test_short_text_is_buffered_without_step_until_prefill_threshold() -> None:
    events: list[str] = []
    inferencer = FakeInferencer(events)
    session = FakeSession(inferencer, events, prefill_text_len=4)
    decoder = FakeDecoder(events)
    bridge = OpenMossLowLatencyTextStreamBridge(
        session,
        decoder,
        sanitize_audio_tokens=sanitize,
    )

    assert list(bridge.push_text_delta("甲乙")) == []
    assert session._pending_tokens == [1, 2]
    assert not any(event.startswith(("prefill:", "step:", "decoder_")) for event in events)


def test_finish_drains_one_step_at_a_time_stops_at_eos_and_flushes_buffer() -> None:
    events: list[str] = []
    inferencer = FakeInferencer(events, outputs=["eos", "must-not-run"])
    session = FakeSession(inferencer, events, prefill_text_len=1)
    session._text_cache = "尾"
    decoder = FakeDecoder(
        events,
        emit_immediately=False,
        flush_value=FakeTensor("flushed", shape=(120,)),
    )
    bridge = OpenMossLowLatencyTextStreamBridge(
        session,
        decoder,
        sanitize_audio_tokens=sanitize,
    )

    chunks = list(bridge.finish(drain_step=8))

    assert [chunk.name for chunk in chunks] == ["flushed"]
    assert [event for event in events if event.startswith("step:")] == ["step:None"]
    assert [event for event in events if event.startswith("decoder_push:")] == [
        "decoder_push:prefill"
    ]
    assert events[-1] == "decoder_flush"


def test_invalid_audio_token_stops_before_later_steps_and_is_never_decoded() -> None:
    events: list[str] = []
    inferencer = FakeInferencer(events, outputs=["invalid", "must-not-run"])
    session = FakeSession(inferencer, events)
    session._prefilled = True
    decoder = FakeDecoder(events)
    bridge = OpenMossLowLatencyTextStreamBridge(
        session,
        decoder,
        sanitize_audio_tokens=sanitize,
    )

    assert list(bridge.push_text_tokens([1, 2])) == []
    assert [event for event in events if event.startswith("step:")] == ["step:1"]
    assert not any(event.startswith("decoder_push:") for event in events)


def test_cancel_during_step_drops_that_frame_stops_more_work_and_skips_flush() -> None:
    events: list[str] = []

    class CancellingInferencer(FakeInferencer):
        def step(self, token, **kwargs) -> FakeTensor:
            frame = super().step(token, **kwargs)
            self.cancel_event.set()
            return frame

    inferencer = CancellingInferencer(events)
    session = FakeSession(inferencer, events)
    session._prefilled = True
    decoder = FakeDecoder(events, flush_value=FakeTensor("must-not-flush", shape=(120,)))
    bridge = OpenMossLowLatencyTextStreamBridge(
        session,
        decoder,
        sanitize_audio_tokens=sanitize,
    )

    assert list(bridge.push_text_tokens([1, 2])) == []
    assert list(bridge.finish()) == []
    assert [event for event in events if event.startswith("step:")] == ["step:1"]
    assert not any(event.startswith("decoder_push:") for event in events)
    assert "decoder_flush" not in events


def test_canary_observation_maps_every_pcm_to_native_stage_without_text() -> None:
    events: list[str] = []
    inferencer = FakeInferencer(events, outputs=["token-2", "eos"])
    session = FakeSession(inferencer, events, prefill_text_len=1)
    decoder = FakeDecoder(events, flush_value=FakeTensor("flushed", shape=(120,)))
    bridge = OpenMossLowLatencyTextStreamBridge(
        session,
        decoder,
        sanitize_audio_tokens=sanitize,
        observe_stages=True,
    )

    emitted = [*bridge.push_text_delta("甲乙"), *bridge.finish()]
    timings = [item for item in emitted if isinstance(item, CanaryStageTiming)]
    chunks = [item for item in emitted if isinstance(item, CanaryAudioChunk)]

    assert {item.stage for item in timings} == {
        "prefill",
        "talker_step",
        "decoder_yield",
        "flush",
    }
    assert [item.source_stage for item in chunks] == ["prefill", "talker_step", "flush"]
    assert all(item.source_stage_index >= 0 and item.decoder_stage_index >= 0 for item in chunks)
    assert all(item.source_stage_duration_ms >= 0 and item.decoder_yield_ms >= 0 for item in chunks)
    assert "甲" not in repr(timings) and "乙" not in repr(chunks)


def test_default_bridge_emits_no_canary_objects() -> None:
    events: list[str] = []
    inferencer = FakeInferencer(events)
    bridge = OpenMossLowLatencyTextStreamBridge(
        FakeSession(inferencer, events),
        FakeDecoder(events),
        sanitize_audio_tokens=sanitize,
    )

    emitted = list(bridge.push_text_delta("甲"))
    assert emitted
    assert not any(isinstance(item, (CanaryStageTiming, CanaryAudioChunk)) for item in emitted)


def test_async_decoder_keeps_blocking_push_off_talker_thread_and_finish_joins() -> None:
    events: list[str] = []
    decoder_started = threading.Event()
    decoder_release = threading.Event()
    decoder_threads: list[int] = []
    handoff_events: list[tuple[str, int]] = []

    class DecoderContext:
        def __enter__(self):
            handoff_events.append(("context_enter", threading.get_ident()))

        def __exit__(self, *_args):
            handoff_events.append(("context_exit", threading.get_ident()))

    def prepare_decode(_tokens: FakeTensor):
        handoff_events.append(("prepare", threading.get_ident()))

        def wait_until_ready() -> None:
            handoff_events.append(("wait", threading.get_ident()))

        return wait_until_ready

    class BlockingDecoder(FakeDecoder):
        def push_tokens(self, tokens: FakeTensor) -> None:
            decoder_threads.append(threading.get_ident())
            decoder_started.set()
            decoder_release.wait()
            super().push_tokens(tokens)

    inferencer = FakeInferencer(events)
    session = FakeSession(inferencer, events)
    session._prefilled = True
    bridge = OpenMossLowLatencyTextStreamBridge(
        session,
        BlockingDecoder(events),
        sanitize_audio_tokens=sanitize,
        async_decoder=True,
        decoder_poll_slice_seconds=0.001,
        decoder_execution_context=DecoderContext,
        prepare_decode=prepare_decode,
    )

    started_at = time.perf_counter()
    assert list(bridge.push_text_tokens([1])) == []
    elapsed = time.perf_counter() - started_at
    assert decoder_started.wait(0.2)
    assert elapsed < 0.05
    assert decoder_threads == [bridge._decoder_worker.thread.ident]
    assert decoder_threads[0] != threading.get_ident()
    assert handoff_events[:3] == [
        ("prepare", threading.get_ident()),
        ("context_enter", decoder_threads[0]),
        ("wait", decoder_threads[0]),
    ]

    decoder_release.set()
    inferencer.finished = True
    assert [chunk.name for chunk in bridge.finish()] == ["pcm:token-1"]
    assert bridge.decoder_worker_alive is False
    assert sum(name == "context_enter" for name, _thread in handoff_events) == 2
    assert all(
        thread_id == decoder_threads[0]
        for name, thread_id in handoff_events
        if name != "prepare"
    )


def test_async_decoder_preserves_submission_and_pcm_order_with_bounded_backpressure() -> None:
    events: list[str] = []

    class DelayedDecoder(FakeDecoder):
        def audio_chunks(self):
            while self.pending:
                tokens = self.pending.pop(0)
                time.sleep(0.005 if tokens.name == "token-1" else 0.001)
                self.events.append(f"decoder_audio:{tokens.name}")
                yield FakeTensor(f"pcm:{tokens.name}", shape=(240,))

    inferencer = FakeInferencer(events)
    session = FakeSession(inferencer, events)
    session._prefilled = True
    bridge = OpenMossLowLatencyTextStreamBridge(
        session,
        DelayedDecoder(events),
        sanitize_audio_tokens=sanitize,
        async_decoder=True,
        decoder_queue_size=1,
        decoder_poll_slice_seconds=0.001,
    )

    chunks = list(bridge.push_text_tokens([1, 2, 3]))
    inferencer.finished = True
    chunks.extend(bridge.finish())

    assert [chunk.name for chunk in chunks] == ["pcm:token-1", "pcm:token-2", "pcm:token-3"]
    assert [event for event in events if event.startswith("decoder_push:")] == [
        "decoder_push:token-1",
        "decoder_push:token-2",
        "decoder_push:token-3",
    ]
    assert bridge.decoder_worker_alive is False


def test_async_decoder_abort_discards_late_pcm_and_reliably_joins() -> None:
    events: list[str] = []
    decoder_started = threading.Event()
    decoder_release = threading.Event()

    class LateDecoder(FakeDecoder):
        def audio_chunks(self):
            while self.pending:
                tokens = self.pending.pop(0)
                decoder_started.set()
                decoder_release.wait()
                yield FakeTensor(f"late:{tokens.name}", shape=(240,))

    inferencer = FakeInferencer(events)
    session = FakeSession(inferencer, events)
    session._prefilled = True
    bridge = OpenMossLowLatencyTextStreamBridge(
        session,
        LateDecoder(events),
        sanitize_audio_tokens=sanitize,
        async_decoder=True,
        decoder_poll_slice_seconds=0.001,
        decoder_join_timeout_seconds=0.5,
    )

    assert list(bridge.push_text_tokens([1])) == []
    assert decoder_started.wait(0.2)
    threading.Timer(0.02, decoder_release.set).start()
    bridge.abort()

    assert bridge.decoder_worker_alive is False
    assert list(bridge.finish()) == []
    assert "decoder_flush" not in events


def test_async_decoder_preserves_canary_stage_and_pcm_mapping() -> None:
    events: list[str] = []
    inferencer = FakeInferencer(events, outputs=["eos"])
    session = FakeSession(inferencer, events, prefill_text_len=1)
    decoder = FakeDecoder(events, flush_value=FakeTensor("flushed", shape=(120,)))
    bridge = OpenMossLowLatencyTextStreamBridge(
        session,
        decoder,
        sanitize_audio_tokens=sanitize,
        observe_stages=True,
        async_decoder=True,
        decoder_poll_slice_seconds=0.001,
    )

    emitted = [*bridge.push_text_delta("甲"), *bridge.finish()]
    timings = [item for item in emitted if isinstance(item, CanaryStageTiming)]
    chunks = [item for item in emitted if isinstance(item, CanaryAudioChunk)]

    assert {item.stage for item in timings} == {
        "prefill",
        "talker_step",
        "decoder_yield",
        "flush",
    }
    assert [item.source_stage for item in chunks] == ["prefill", "flush"]
    assert bridge.decoder_worker_alive is False
