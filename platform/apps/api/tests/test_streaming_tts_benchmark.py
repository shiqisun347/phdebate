from __future__ import annotations

import importlib.util
import wave
from pathlib import Path

MODULE_PATH = Path(__file__).parents[3] / "scripts" / "benchmark_streaming_tts_candidate.py"
SPEC = importlib.util.spec_from_file_location("benchmark_streaming_tts_candidate", MODULE_PATH)
assert SPEC and SPEC.loader
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)


def test_percentile_and_distribution_are_interpolated() -> None:
    assert benchmark.percentile([1, 2, 3, 4], 0.5) == 2.5
    assert benchmark.distribution([100, 200, 300])["p95"] == 290.0


def test_write_pcm_wav_preserves_declared_format(tmp_path: Path) -> None:
    target = tmp_path / "candidate.wav"
    benchmark.write_pcm_wav(
        target,
        b"\x00\x00" * 2 * 480,
        sample_rate=48_000,
        channels=2,
        sample_width=2,
    )
    with wave.open(str(target), "rb") as audio:
        assert audio.getframerate() == 48_000
        assert audio.getnchannels() == 2
        assert audio.getsampwidth() == 2
        assert audio.getnframes() == 480


def test_three_way_transport_gate_requires_every_threshold() -> None:
    records = [
        {"first_pcm_ms": 500, "rtf": 0.4, "chunk_gap_p99_ms": 100},
        {"first_pcm_ms": 600, "rtf": 0.5, "chunk_gap_p99_ms": 120},
        {"first_pcm_ms": 700, "rtf": 0.6, "chunk_gap_p99_ms": 150},
    ]
    assert benchmark.summarize(records, 3)["transport_gate_passed"] is True
    records[-1]["chunk_gap_p99_ms"] = 250
    assert benchmark.summarize(records, 3)["transport_gate_passed"] is False
