from __future__ import annotations

import wave
from pathlib import Path

import app.services.providers as providers_service
import httpx
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
    assert extract_agent_body_delta(
        {"type": "response.reasoning_summary_text.delta", "delta": "hidden"}
    ) == ""
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
