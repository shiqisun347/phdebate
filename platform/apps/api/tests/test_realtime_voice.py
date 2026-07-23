import asyncio

import pytest
from app.services.realtime_voice import (
    IncrementalVoicePipeline,
    RealtimeVoiceError,
    RealtimeVoiceInterrupted,
    RealtimeVoiceSynthesisError,
    SpeakableClauseAssembler,
)
from app.services.voice_runtime.pipeline import _final_prefix_end


def test_final_prefix_mapping_allows_layout_whitespace_but_not_rewrites() -> None:
    final = "第一点成立。\n\n第二点继续。"
    submitted = "第一点成立。第二点"
    end = _final_prefix_end(final, submitted)
    assert end is not None
    assert final[end:] == "继续。"
    assert _final_prefix_end("第一点改变。", "第一点成立。") is None


def test_clause_assembler_emits_strong_punctuation_immediately() -> None:
    assembler = SpeakableClauseAssembler(minimum_characters=10, maximum_characters=24)
    assert assembler.feed("我方认为人工智能能够") == []
    assert assembler.feed("提升学习效率。下一点") == ["我方认为人工智能能够提升学习效率。"]
    assert assembler.finish() == ["下一点"]


def test_clause_assembler_emits_short_complete_sentence_below_normal_floor() -> None:
    assembler = SpeakableClauseAssembler()
    assert assembler.feed("我同意。") == ["我同意。"]
    assert assembler.finish() == []


def test_clause_assembler_uses_soft_boundary_without_splitting_ascii_token() -> None:
    assembler = SpeakableClauseAssembler(minimum_characters=8, maximum_characters=16)
    chunks = assembler.feed("我们使用ChatGPT-4o分析数据并复核结论")
    assert chunks
    assert not chunks[0].endswith(("Chat", "ChatGPT-", "ChatGPT-4"))
    assert "".join(chunks + assembler.finish()) == "我们使用ChatGPT-4o分析数据并复核结论"


def test_clause_assembler_prefers_weak_punctuation_near_maximum() -> None:
    assembler = SpeakableClauseAssembler(minimum_characters=8, maximum_characters=16)
    chunks = assembler.feed("第一项是证据核验，第二项是因果分析，第三项是价值判断")
    assert chunks[0] == "第一项是证据核验，"
    assert "".join(chunks + assembler.finish()) == "第一项是证据核验，第二项是因果分析，第三项是价值判断"


def test_clause_assembler_commits_first_stable_comma_then_grows_following_chunks() -> None:
    assembler = SpeakableClauseAssembler()
    assert assembler.feed("我方首先指出这个前提，") == ["我方首先指出这个前提，"]
    assert assembler.current_minimum_characters == 16
    assert assembler.feed("效率并不等于真正的教育质量提升，") == ["效率并不等于真正的教育质量提升，"]
    assert assembler.current_minimum_characters == 22
    assert assembler.feed("后续内容需要继续积累到更完整而且稳定的长短语，") == ["后续内容需要继续积累到更完整而且稳定的长短语，"]


def test_clause_assembler_caps_late_sentence_boundary_at_current_chunk_window() -> None:
    assembler = SpeakableClauseAssembler()
    text = "我方认为人工智能带来的效率提升不能自动等同于教育质量提升。"
    chunks = assembler.feed(text)
    assert chunks
    assert 10 <= len(chunks[0]) <= 16
    assert all(len(chunk) <= 28 for chunk in chunks)
    assert "".join(chunks + assembler.finish()) == text


def test_clause_assembler_grows_plain_mandarin_chunks_from_16_to_28_characters() -> None:
    assembler = SpeakableClauseAssembler()
    chunks = assembler.feed("甲" * 80)
    assert [len(chunk) for chunk in chunks[:3]] == [16, 22, 28]
    assert "".join(chunks + assembler.finish()) == "甲" * 80


def test_first_flush_guard_keeps_tiny_prefix_until_moss_prefill_is_safe() -> None:
    assembler = SpeakableClauseAssembler(first_flush_minimum_characters=12)
    assembler.feed("我")
    assert assembler.flush_deadline() == []
    assembler.feed("方认为")
    assert assembler.flush_deadline() == []
    assert assembler.feed("，辩论应当基于事实。") == ["我方认为，辩论应当基于事实。"]


def test_first_flush_guard_does_not_emit_short_sentence_boundary() -> None:
    assembler = SpeakableClauseAssembler(first_flush_minimum_characters=12)
    assert assembler.feed("我同意。") == []
    assert assembler.finish() == ["我同意。"]


class RecordingSession:
    def __init__(self) -> None:
        self.chunks: list[str] = []
        self.finished = False
        self.aborted = False

    async def push_text(self, text: str) -> None:
        self.chunks.append(text)

    async def finish(self) -> str:
        self.finished = True
        return "/media/room/speech.wav"

    async def abort(self) -> None:
        self.aborted = True


async def agent_events(*events: dict[str, str]):
    for event in events:
        yield event


async def test_incremental_pipeline_starts_before_final_and_preserves_transcript() -> None:
    session = RecordingSession()
    pipeline = IncrementalVoicePipeline(SpeakableClauseAssembler(minimum_characters=8, maximum_characters=16))
    first_readable_delta_calls = 0
    tts_start_calls = 0
    submitted_chunks: list[str] = []

    async def mark_first_readable_delta() -> None:
        nonlocal first_readable_delta_calls
        first_readable_delta_calls += 1

    async def mark_tts_start() -> None:
        nonlocal tts_start_calls
        tts_start_calls += 1

    async def record_submitted_text(chunk: str) -> None:
        submitted_chunks.append(chunk)

    content, audio_url = await pipeline.run(
        agent_events(
            {"type": "thinking_delta", "delta": "这段推理不能成为首正文时间"},
            {"type": "delta", "delta": "我方首先指出，"},
            {"type": "delta", "delta": "效率不等于教育质量。"},
            {"type": "final", "content": "我方首先指出，效率不等于教育质量。"},
        ),
        session,
        on_first_readable_delta=mark_first_readable_delta,
        on_tts_start=mark_tts_start,
        on_text_submitted=record_submitted_text,
    )
    assert content == "我方首先指出，效率不等于教育质量。"
    assert "".join(session.chunks) == content
    assert session.finished is True and session.aborted is False
    assert audio_url.endswith("speech.wav")
    assert first_readable_delta_calls == 1
    assert tts_start_calls == 1
    assert submitted_chunks == session.chunks
    assert "".join(submitted_chunks) == content


async def test_incremental_pipeline_preserves_word_spaces_across_tts_chunks() -> None:
    session = RecordingSession()
    text = "我方主张 large language model 能提升学习效率，但必须保留 human oversight。"
    pipeline = IncrementalVoicePipeline(
        SpeakableClauseAssembler(minimum_characters=8, maximum_characters=16),
        maximum_wait_seconds=0.01,
    )

    content, _audio_url = await pipeline.run(
        agent_events(
            {"type": "delta", "delta": "我方主张 large language model 能提升"},
            {"type": "delta", "delta": "学习效率，但必须保留 human oversight。"},
            {"type": "final", "content": text},
        ),
        session,
    )

    assert content == text
    assert len(session.chunks) > 1
    assert "".join(session.chunks) == text
    assert "largelanguage" not in "".join(session.chunks)
    assert "humanoversight" not in "".join(session.chunks)


async def test_incremental_pipeline_flushes_minimum_safe_text_after_latency_deadline() -> None:
    session = RecordingSession()
    first_chunk = asyncio.Event()
    release_final = asyncio.Event()

    async def delayed_events():
        yield {"type": "delta", "delta": "这是没有标点但已可朗读的内容"}
        await release_final.wait()
        yield {"type": "final", "content": "这是没有标点但已可朗读的内容"}

    original_push = session.push_text

    async def recording_push(text: str) -> None:
        await original_push(text)
        first_chunk.set()

    session.push_text = recording_push  # type: ignore[method-assign]
    pipeline = IncrementalVoicePipeline(
        SpeakableClauseAssembler(minimum_characters=8, maximum_characters=16),
        maximum_wait_seconds=0.01,
    )
    task = asyncio.create_task(pipeline.run(delayed_events(), session))
    await asyncio.wait_for(first_chunk.wait(), timeout=0.2)
    assert len(session.chunks) == 1
    assert 8 <= len(session.chunks[0]) <= 16
    assert "这是没有标点但已可朗读的内容".startswith(session.chunks[0])
    release_final.set()
    content, _audio_url = await task
    assert content == "这是没有标点但已可朗读的内容"
    assert "".join(session.chunks) == content


async def test_incremental_pipeline_hard_deadline_starts_at_short_first_body_delta() -> None:
    session = RecordingSession()
    first_chunk = asyncio.Event()
    release_final = asyncio.Event()

    async def delayed_events():
        yield {"type": "delta", "delta": "短正文"}
        await release_final.wait()
        yield {"type": "final", "content": "短正文"}

    original_push = session.push_text

    async def recording_push(text: str) -> None:
        await original_push(text)
        first_chunk.set()

    session.push_text = recording_push  # type: ignore[method-assign]
    pipeline = IncrementalVoicePipeline(maximum_wait_seconds=0.02)
    task = asyncio.create_task(pipeline.run(delayed_events(), session))
    await asyncio.wait_for(first_chunk.wait(), timeout=0.15)
    assert session.chunks == ["短正文"]
    release_final.set()
    content, _audio_url = await task
    assert content == "短正文"


async def test_incremental_pipeline_cancellation_poll_does_not_flush_before_text_deadline() -> None:
    session = RecordingSession()
    release_final = asyncio.Event()

    async def delayed_events():
        yield {"type": "delta", "delta": "已有十二个字但仍等待短语边界"}
        await release_final.wait()
        yield {"type": "final", "content": "已有十二个字但仍等待短语边界"}

    pipeline = IncrementalVoicePipeline(maximum_wait_seconds=0.06)
    task = asyncio.create_task(
        pipeline.run(
            delayed_events(),
            session,
            should_cancel=lambda: False,
            cancellation_poll_seconds=0.005,
        )
    )
    await asyncio.sleep(0.025)
    assert session.chunks == []
    while not session.chunks:
        await asyncio.sleep(0)
    release_final.set()
    await task


async def test_incremental_pipeline_accepts_authoritative_suffix_after_deltas() -> None:
    session = RecordingSession()
    pipeline = IncrementalVoicePipeline(SpeakableClauseAssembler(minimum_characters=8, maximum_characters=16))
    content, _audio_url = await pipeline.run(
        agent_events(
            {"type": "delta", "delta": "我方观点已经明确。"},
            {"type": "final", "content": "我方观点已经明确。谢谢。"},
        ),
        session,
    )
    assert content == "我方观点已经明确。谢谢。"
    assert "".join(session.chunks) == content


async def test_incremental_pipeline_allows_final_to_revise_only_unsubmitted_tail() -> None:
    session = RecordingSession()
    pipeline = IncrementalVoicePipeline()
    content, _audio_url = await pipeline.run(
        agent_events(
            {"type": "delta", "delta": "我方已经提交这个稳定前提，旧的尾部仍在缓冲"},
            {"type": "final", "content": "我方已经提交这个稳定前提，新的尾部结论。"},
        ),
        session,
    )
    assert content == "我方已经提交这个稳定前提，新的尾部结论。"
    assert "".join(session.chunks) == content


async def test_incremental_pipeline_aborts_if_final_revises_spoken_prefix() -> None:
    session = RecordingSession()
    pipeline = IncrementalVoicePipeline(SpeakableClauseAssembler(minimum_characters=4, maximum_characters=8))
    with pytest.raises(RealtimeVoiceError, match="revised text"):
        await pipeline.run(
            agent_events(
                {"type": "delta", "delta": "旧观点已经播出。"},
                {"type": "final", "content": "新观点替换旧内容。"},
            ),
            session,
        )
    assert session.finished is False and session.aborted is True


async def test_incremental_pipeline_drains_agent_final_after_non_cancel_tts_failure() -> None:
    consumed_final = False

    class FailingSession(RecordingSession):
        async def push_text(self, text: str) -> None:
            self.chunks.append(text)
            raise RuntimeError("decoder failed")

    async def events():
        nonlocal consumed_final
        yield {"type": "delta", "delta": "第一点已经可以朗读。"}
        yield {"type": "delta", "delta": "第二点仍要保存。"}
        consumed_final = True
        yield {"type": "final", "content": "第一点已经可以朗读。第二点仍要保存。"}

    session = FailingSession()
    pipeline = IncrementalVoicePipeline(SpeakableClauseAssembler(minimum_characters=8, maximum_characters=16))
    with pytest.raises(RealtimeVoiceSynthesisError) as raised:
        await pipeline.run(events(), session)
    assert consumed_final is True
    assert raised.value.final_text == "第一点已经可以朗读。第二点仍要保存。"
    assert isinstance(raised.value.cause, RuntimeError)
    assert session.aborted is True


async def test_incremental_pipeline_interrupts_agent_aborts_tts_and_discards_pending_text() -> None:
    cancelled = False
    agent_interrupted = False
    generator_closed = False

    async def events():
        nonlocal generator_closed
        try:
            yield {"type": "delta", "delta": "第一段内容已经完整提交，"}
            await asyncio.Event().wait()
        finally:
            generator_closed = True

    async def should_cancel() -> bool:
        return cancelled

    async def interrupt_agent() -> None:
        nonlocal agent_interrupted
        agent_interrupted = True

    session = RecordingSession()
    pipeline = IncrementalVoicePipeline(maximum_wait_seconds=0.01)
    task = asyncio.create_task(
        pipeline.run(
            events(),
            session,
            should_cancel=should_cancel,
            on_interrupt=interrupt_agent,
            cancellation_poll_seconds=0.001,
        )
    )
    while not session.chunks:
        await asyncio.sleep(0)
    cancelled = True
    with pytest.raises(RealtimeVoiceInterrupted):
        await asyncio.wait_for(task, timeout=0.2)
    assert agent_interrupted is True
    assert session.aborted is True and session.finished is False
    assert generator_closed is True


async def test_incremental_pipeline_interrupt_discards_unsubmitted_buffered_suffix() -> None:
    cancelled = False
    tail_received = asyncio.Event()

    async def events():
        yield {"type": "delta", "delta": "第一段已经稳定提交。"}
        yield {"type": "delta", "delta": "未播放尾部"}
        tail_received.set()
        await asyncio.Event().wait()

    session = RecordingSession()
    pipeline = IncrementalVoicePipeline(maximum_wait_seconds=1)
    task = asyncio.create_task(
        pipeline.run(
            events(),
            session,
            should_cancel=lambda: cancelled,
            cancellation_poll_seconds=0.001,
        )
    )
    await asyncio.wait_for(tail_received.wait(), timeout=0.1)
    assert session.chunks == ["第一段已经稳定提交。"]
    cancelled = True
    with pytest.raises(RealtimeVoiceInterrupted):
        await asyncio.wait_for(task, timeout=0.2)
    assert "未播放尾部" not in "".join(session.chunks)
    assert session.aborted is True and session.finished is False
