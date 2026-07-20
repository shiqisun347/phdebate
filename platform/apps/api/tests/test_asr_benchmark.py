from __future__ import annotations

import json
import wave
from pathlib import Path

import pytest
import websockets

from scripts.benchmark_funasr import (
    aggregate,
    character_error_rate,
    contains_anchor,
    edit_counts,
    isolation_findings,
    load_manifest,
    normalize_text,
    quality_score,
    repeated_ngram_excess,
    transcribe,
)


def test_asr_benchmark_character_metrics_distinguish_edit_types() -> None:
    assert normalize_text("Ａ，人工 智能！") == "a人工智能"
    assert edit_counts("人工智能", "人智熊") == edit_counts("人工智能", "人智熊")
    counts = edit_counts("人工智能", "人智熊")
    assert (counts.substitutions, counts.deletions, counts.insertions) == (1, 1, 0)
    assert character_error_rate("人工智能", "人智熊") == 0.5
    assert contains_anchor("开场先说明：观点如下", "开场先说明")
    assert repeated_ngram_excess("人工智能", "人智熊") == 0
    assert repeated_ngram_excess("效率与公平", "效率效率与公平") > 0


def test_asr_benchmark_aggregation_and_isolation_detection() -> None:
    results = [
        {
            "id": "a",
            "reference": "青松支持教育公平",
            "hypothesis": "青松支持教育公平",
            "reference_characters": 8,
            "substitutions": 0,
            "deletions": 0,
            "insertions": 0,
            "cer": 0.0,
            "head_anchor_ok": True,
            "tail_anchor_ok": True,
            "repeated_bigram_excess": 0,
            "first_partial_ms": 300,
            "final_after_stop_ms": 500,
            "false_positive_characters": 0,
        },
        {
            "id": "b",
            "reference": "白鹭反对算法替代",
            "hypothesis": "青松支持教育公平",
            "reference_characters": 8,
            "substitutions": 7,
            "deletions": 0,
            "insertions": 0,
            "cer": 0.875,
            "head_anchor_ok": False,
            "tail_anchor_ok": False,
            "repeated_bigram_excess": 0,
            "first_partial_ms": None,
            "final_after_stop_ms": 700,
            "false_positive_characters": 0,
        },
        {
            "id": "silence",
            "reference": "",
            "hypothesis": "谢谢观看",
            "reference_characters": 0,
            "substitutions": 0,
            "deletions": 0,
            "insertions": 4,
            "cer": None,
            "head_anchor_ok": None,
            "tail_anchor_ok": None,
            "repeated_bigram_excess": 3,
            "first_partial_ms": None,
            "final_after_stop_ms": 600,
            "false_positive_characters": 4,
        },
    ]
    summary = aggregate(results)
    assert summary["micro_cer"] == 0.4375
    assert summary["negative_false_positive_cases"] == 1
    assert isolation_findings(results) == [
        {"case_id": "b", "closest_other_case_id": "a", "own_cer": 1.0, "other_cer": 0.0}
    ]
    summary["cross_stream_contamination_cases"] = 1
    score, components = quality_score(summary, reference_characters=16)
    assert 0 <= score <= 100
    assert sum(components.values()) == score


def test_asr_benchmark_manifest_is_fixed_and_complete() -> None:
    path = Path(__file__).parents[3] / "scripts" / "asr_benchmark_corpus_v1.json"
    manifest = load_manifest(path)
    assert manifest["schema_version"] == 1
    assert len(manifest["cases"]) == 8
    assert {item["category"] for item in manifest["cases"]} == {
        "standard_chinese",
        "numbers",
        "names_and_terms",
        "mixed_language",
        "polyphonic_characters",
        "boundary_integrity",
        "repetition",
        "long_sentence",
    }
    # Round-trip evidence must remain visibly distinct from human recordings.
    assert "不替代真人麦克风语料" in manifest["description"]
    assert json.loads(path.read_text(encoding="utf-8"))["language"] == "zh-CN"


@pytest.mark.asyncio
async def test_asr_benchmark_transcribe_records_partial_and_final_latency(tmp_path: Path) -> None:
    audio_path = tmp_path / "sample.wav"
    with wave.open(str(audio_path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16_000)
        audio.writeframes((500).to_bytes(2, "little", signed=True) * 3_200)

    async def mock_asr(socket) -> None:
        sent_partial = False
        async for message in socket:
            if isinstance(message, bytes) and not sent_partial:
                await socket.send(json.dumps({"partial": "开场", "is_final": False}, ensure_ascii=False))
                sent_partial = True
            elif message == "STOP":
                await socket.send(json.dumps({"text": "开场说明", "is_final": True}, ensure_ascii=False))
                return

    server = await websockets.serve(mock_asr, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        result = await transcribe(
            {
                "id": "streaming",
                "category": "standard_chinese",
                "reference": "开场说明",
                "head_anchor": "开场",
                "tail_anchor": "说明",
                "audio_path": audio_path,
                "evidence_type": "test_fixture",
            },
            endpoint=f"ws://127.0.0.1:{port}",
            pace=0,
            final_timeout_seconds=1,
        )
    finally:
        server.close()
        await server.wait_closed()

    assert result["hypothesis"] == "开场说明"
    assert result["partial_count"] == 1
    assert result["first_partial_ms"] is not None
    assert result["final_after_stop_ms"] is not None
    assert result["cer"] == 0
