from __future__ import annotations

import asyncio
import wave
from pathlib import Path

import app.services.providers as providers_service
import httpx
import pytest
from app.services.providers import MossTTSRealtimeIncrementalSession, ProviderError
from app.services.realtime_voice import IncrementalVoicePipeline as LegacyPipeline
from app.services.voice_runtime.archive import inspect_wav, remove_partial_archives, wav_duration
from app.services.voice_runtime.lighttts import LightTTSRuntime, ProviderVoiceSession, probe_readiness, readiness_url
from app.services.voice_runtime.metrics import VoiceTurnMetrics
from app.services.voice_runtime.pipeline import IncrementalVoicePipeline
from app.services.voice_runtime.text import extract_agent_body_delta
from app.services.voice_runtime.voices import VOICE_IDS, voice_for_seat


def test_legacy_realtime_voice_module_reexports_new_runtime() -> None:
    assert LegacyPipeline is IncrementalVoicePipeline


def test_voice_catalog_quarantines_noisy_negative_first_speaker_prompt() -> None:
    assert VOICE_IDS == tuple(f"debate_voice_{index}" for index in range(1, 9))
    assert [voice_for_seat(f"aff_{index}") for index in range(1, 5)] == list(VOICE_IDS[:4])
    assert [voice_for_seat(f"neg_{index}") for index in range(1, 5)] == [
        "debate_voice_8",
        "debate_voice_6",
        "debate_voice_7",
        "debate_voice_8",
    ]


def test_text_runtime_filters_reasoning_without_provider_dependency() -> None:
    assert extract_agent_body_delta({"type": "thinking", "delta": "hidden"}) == ""
    assert extract_agent_body_delta({"type": "response.reasoning_summary_text.delta", "delta": "hidden"}) == ""
    assert extract_agent_body_delta({"choices": [{"delta": {"reasoning_content": "x", "content": "正文"}}]}) == "正文"


async def test_unified_runtime_opens_and_normalizes_provider_session(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Session:
        def __init__(self) -> None:
            self.chunks: list[str] = []
            self.aborted = False

        async def push_text(self, text: str) -> None:
            self.chunks.append(text)

        async def finish(self) -> str:
            return "/media/room/speech.wav"

        async def abort(self) -> None:
            self.aborted = True

    underlying = Session()

    def open_incremental_session(**kwargs):
        captured.update(kwargs)
        return underlying

    monkeypatch.setattr(providers_service.realtime_tts, "open_incremental_session", open_incremental_session)
    runtime = LightTTSRuntime()
    session = await runtime.open_session(
        room_code="123456",
        speech_id="speech-1",
        voice_id="debate_voice_5",
        on_stream_event=lambda _event: None,  # type: ignore[arg-type]
    )
    assert isinstance(session, ProviderVoiceSession)
    await session.push_text("正文。")
    assert await session.finish() == "/media/room/speech.wav"
    await session.abort(reason="match_paused")
    assert underlying.chunks == ["正文。"] and underlying.aborted is True
    assert session.abort_reason == "match_paused"
    assert captured["voice"] == "debate_voice_5"


async def test_unified_runtime_waits_for_provider_prepare_before_returning(monkeypatch) -> None:
    prepare_entered = asyncio.Event()
    provider_ready = asyncio.Event()

    class Session:
        async def prepare(self) -> None:
            prepare_entered.set()
            await provider_ready.wait()

        async def push_text(self, _text: str) -> None:
            return None

        async def finish(self) -> str:
            return "/media/room/prepared.wav"

        async def abort(self) -> None:
            return None

    monkeypatch.setattr(
        providers_service.realtime_tts,
        "open_incremental_session",
        lambda **_kwargs: Session(),
    )
    runtime = LightTTSRuntime()
    opening = asyncio.create_task(
        runtime.open_session(
            room_code="123456",
            speech_id="speech-prepare",
            voice_id="debate_voice_1",
            on_stream_event=lambda _event: None,  # type: ignore[arg-type]
        )
    )

    await asyncio.wait_for(prepare_entered.wait(), timeout=1)
    await asyncio.sleep(0)
    assert not opening.done(), "open_session must hold the turn before TTS capacity is ready"

    provider_ready.set()
    session = await asyncio.wait_for(opening, timeout=1)
    assert await session.finish() == "/media/room/prepared.wav"


async def test_unified_runtime_aborts_failed_prepare_without_masking_original_error(monkeypatch) -> None:
    expected = ProviderError("upstream ready failed", code="prepare_failed")
    abort_called = asyncio.Event()

    class Session:
        async def prepare(self) -> None:
            raise expected

        async def push_text(self, _text: str) -> None:
            return None

        async def finish(self) -> str:
            return ""

        async def abort(self) -> None:
            abort_called.set()
            raise RuntimeError("abort cleanup also failed")

    monkeypatch.setattr(
        providers_service.realtime_tts,
        "open_incremental_session",
        lambda **_kwargs: Session(),
    )

    with pytest.raises(ProviderError) as raised:
        await LightTTSRuntime().open_session(
            room_code="123456",
            speech_id="speech-prepare-failed-cleanup",
            voice_id="debate_voice_1",
            on_stream_event=lambda _event: None,  # type: ignore[arg-type]
        )
    assert raised.value is expected
    assert abort_called.is_set()


async def test_unified_runtime_aborts_cancelled_prepare_and_preserves_cancellation(monkeypatch) -> None:
    prepare_entered = asyncio.Event()
    abort_called = asyncio.Event()

    class Session:
        async def prepare(self) -> None:
            prepare_entered.set()
            await asyncio.Event().wait()

        async def push_text(self, _text: str) -> None:
            return None

        async def finish(self) -> str:
            return ""

        async def abort(self) -> None:
            abort_called.set()
            raise RuntimeError("abort cleanup also failed")

    monkeypatch.setattr(
        providers_service.realtime_tts,
        "open_incremental_session",
        lambda **_kwargs: Session(),
    )
    opening = asyncio.create_task(
        LightTTSRuntime().open_session(
            room_code="123456",
            speech_id="speech-prepare-cancelled-cleanup",
            voice_id="debate_voice_1",
            on_stream_event=lambda _event: None,  # type: ignore[arg-type]
        )
    )
    await asyncio.wait_for(prepare_entered.wait(), timeout=1)
    opening.cancel()

    with pytest.raises(asyncio.CancelledError):
        await opening
    assert abort_called.is_set()


async def test_agent_iterator_is_not_consumed_until_voice_session_is_prepared(monkeypatch) -> None:
    prepare_entered = asyncio.Event()
    provider_ready = asyncio.Event()
    agent_consumed = asyncio.Event()

    class Session:
        def __init__(self) -> None:
            self.chunks: list[str] = []

        async def prepare(self) -> None:
            prepare_entered.set()
            await provider_ready.wait()

        async def push_text(self, text: str) -> None:
            self.chunks.append(text)

        async def finish(self) -> str:
            return "/media/room/gated.wav"

        async def abort(self) -> None:
            return None

    underlying = Session()
    monkeypatch.setattr(
        providers_service.realtime_tts,
        "open_incremental_session",
        lambda **_kwargs: underlying,
    )

    async def agent_events():
        agent_consumed.set()
        yield {"type": "delta", "delta": "我方已经准备好开始发言。"}
        yield {"type": "final", "content": "我方已经准备好开始发言。"}

    async def run_turn() -> tuple[str, str]:
        session = await LightTTSRuntime().open_session(
            room_code="123456",
            speech_id="speech-gated-agent",
            voice_id="debate_voice_1",
            on_stream_event=lambda _event: None,  # type: ignore[arg-type]
        )
        return await IncrementalVoicePipeline().run(agent_events(), session)

    turn = asyncio.create_task(run_turn())
    await asyncio.wait_for(prepare_entered.wait(), timeout=1)
    await asyncio.sleep(0)
    assert not agent_consumed.is_set(), "Agent first delta must not be requested while TTS is queued"

    provider_ready.set()
    content, audio_url = await asyncio.wait_for(turn, timeout=1)
    assert agent_consumed.is_set()
    assert content == "我方已经准备好开始发言。"
    assert "".join(underlying.chunks) == content
    assert audio_url == "/media/room/gated.wav"


async def test_moss_prepare_abort_releases_inflight_provider_job() -> None:
    provider_entered = asyncio.Event()
    provider_released = asyncio.Event()

    class Provider:
        async def _synthesize_incremental_job(
            self,
            _chunks,
            _text_parts,
            *,
            should_cancel,
            on_session_ready,
            **_kwargs,
        ) -> str:
            provider_entered.set()
            try:
                while not await should_cancel():
                    await asyncio.sleep(0)
                raise ProviderError("cancelled while waiting for capacity", code="cancelled")
            finally:
                provider_released.set()

    session = MossTTSRealtimeIncrementalSession(
        Provider(),  # type: ignore[arg-type]
        room_code="123456",
        speech_id="speech-abort-prepare",
        voice="debate_voice_1",
        provider_config=None,
        should_cancel=None,
        deadline_monotonic=None,
        on_stream_event=lambda _event: None,  # type: ignore[arg-type]
        publish_live=True,
    )
    preparing = asyncio.create_task(session.prepare())
    await asyncio.wait_for(provider_entered.wait(), timeout=1)

    await asyncio.wait_for(session.abort(reason="match_paused"), timeout=1)
    await asyncio.wait_for(provider_released.wait(), timeout=1)
    with pytest.raises(ProviderError, match="cancelled while waiting for capacity"):
        await preparing
    assert session._task is not None and session._task.done()


async def test_moss_prepare_propagates_provider_failure_before_ready() -> None:
    expected = ProviderError(
        "MOSS endpoint unavailable before ready",
        code="moss_prepare_failed",
        retryable=True,
    )

    class Provider:
        async def _synthesize_incremental_job(self, *_args, **_kwargs) -> str:
            raise expected

    session = MossTTSRealtimeIncrementalSession(
        Provider(),  # type: ignore[arg-type]
        room_code="123456",
        speech_id="speech-prepare-failure",
        voice="debate_voice_1",
        provider_config=None,
        should_cancel=None,
        deadline_monotonic=None,
        on_stream_event=lambda _event: None,  # type: ignore[arg-type]
        publish_live=True,
    )

    with pytest.raises(ProviderError) as raised:
        await asyncio.wait_for(session.prepare(), timeout=1)
    assert raised.value is expected
    assert raised.value.code == "moss_prepare_failed"


async def test_moss_incremental_session_preserves_boundary_whitespace() -> None:
    observed: list[str] = []

    class Provider:
        async def _synthesize_incremental_job(
            self,
            chunks,
            text_parts,
            *,
            on_session_ready,
            **_kwargs,
        ) -> str:
            on_session_ready()
            async for chunk in chunks:
                observed.append(chunk)
            assert "".join(text_parts) == "large language model"
            return "/media/room/spaces.wav"

    session = MossTTSRealtimeIncrementalSession(
        Provider(),  # type: ignore[arg-type]
        room_code="123456",
        speech_id="speech-spaces",
        voice="debate_voice_1",
        provider_config=None,
        should_cancel=None,
        deadline_monotonic=None,
        on_stream_event=lambda _event: None,  # type: ignore[arg-type]
        publish_live=True,
    )

    await session.push_text("large ")
    await session.push_text("language ")
    await session.push_text("model")
    assert await session.finish() == "/media/room/spaces.wav"
    assert observed == ["large ", "language ", "model"]


def test_archive_helpers_inspect_and_remove_only_matching_parts(tmp_path: Path) -> None:
    target = tmp_path / "speech.wav"
    with wave.open(str(target), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24_000)
        audio.writeframes(b"\x00\x00" * 12_000)
    metadata = inspect_wav(target)
    assert metadata.sample_rate == 24_000 and metadata.duration_seconds == 0.5
    assert wav_duration(target) == 0.5
    matching = tmp_path / ".speech.generation.wav.part"
    unrelated = tmp_path / ".other.generation.wav.part"
    matching.write_bytes(b"part")
    unrelated.write_bytes(b"part")
    assert remove_partial_archives(tmp_path, "speech") == 1
    assert not matching.exists() and unrelated.exists()


def test_voice_turn_metrics_tracks_first_events_without_global_state() -> None:
    metrics = VoiceTurnMetrics(room_code="123456", speech_id="speech-1")
    metrics.mark_text()
    metrics.mark_pcm(1_920)
    metrics.finish()
    assert metrics.text_chunks == 1 and metrics.pcm_bytes == 1_920
    assert metrics.first_pcm_seconds is not None and metrics.completed_monotonic is not None


def test_lighttts_readiness_url_discards_inference_path_and_query() -> None:
    assert readiness_url("http://127.0.0.1:8000/inference_zero_shot?x=1") == "http://127.0.0.1:8000/health/ready"


async def test_lighttts_readiness_rejects_fake_green_and_preserves_lifecycle_fields() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/health/ready"
        return httpx.Response(
            503,
            json={
                "ok": False,
                "active": 1,
                "abort_pending": [246],
                "orphan_count": 1,
                "orphan_total": 3,
                "components": {"ok": False},
            },
        )

    snapshot = await probe_readiness(
        "http://lighttts.test/inference_zero_shot",
        transport=httpx.MockTransport(handler),
    )
    assert snapshot["ok"] is False
    assert snapshot["status_code"] == 503
    assert snapshot["abort_pending"] == [246]
    assert snapshot["orphan_count"] == 1 and snapshot["orphan_total"] == 3
