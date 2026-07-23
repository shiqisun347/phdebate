#!/usr/bin/env python3
"""Run a repeatable Chinese FunASR benchmark without mutating application data.

The default corpus can be synthesized by the configured LightTTS service, or
replaced with independently recorded WAV files.  LightTTS round-trip results
are explicitly labelled ``tts_roundtrip`` because they are not evidence of
human microphone accuracy.
"""

from __future__ import annotations

import argparse
import asyncio
import audioop
import json
import math
import random
import re
import shutil
import statistics
import tempfile
import time
import unicodedata
import wave
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import websockets
from app.core.config import settings
from app.services.providers import lighttts

DEFAULT_MANIFEST = Path(__file__).with_name("asr_benchmark_corpus_v1.json")
PCM_SAMPLE_RATE = 16_000
PCM_SAMPLE_WIDTH = 2
CHUNK_MILLISECONDS = 100


def normalize_text(value: str) -> str:
    """Keep score-bearing Chinese/alphanumeric characters and fold case/width."""

    normalized = unicodedata.normalize("NFKC", value)
    return "".join(re.findall(r"[\u3400-\u9fffA-Za-z0-9]", normalized)).casefold()


@dataclass(frozen=True)
class EditCounts:
    substitutions: int
    deletions: int
    insertions: int

    @property
    def errors(self) -> int:
        return self.substitutions + self.deletions + self.insertions


def edit_counts(reference: str, hypothesis: str) -> EditCounts:
    """Return deterministic Levenshtein operation counts for two strings."""

    reference = normalize_text(reference)
    hypothesis = normalize_text(hypothesis)
    # Each cell stores (total, substitutions, deletions, insertions).  The
    # tuple ordering provides a stable tie-break that favours fewer edits of
    # each type after minimizing the total distance.
    previous = [(index, 0, 0, index) for index in range(len(hypothesis) + 1)]
    for ref_index, ref_character in enumerate(reference, start=1):
        current = [(ref_index, 0, ref_index, 0)]
        for hyp_index, hyp_character in enumerate(hypothesis, start=1):
            if ref_character == hyp_character:
                current.append(previous[hyp_index - 1])
                continue
            substitution = previous[hyp_index - 1]
            deletion = previous[hyp_index]
            insertion = current[hyp_index - 1]
            candidates = [
                (substitution[0] + 1, substitution[1] + 1, substitution[2], substitution[3]),
                (deletion[0] + 1, deletion[1], deletion[2] + 1, deletion[3]),
                (insertion[0] + 1, insertion[1], insertion[2], insertion[3] + 1),
            ]
            current.append(min(candidates))
        previous = current
    _total, substitutions, deletions, insertions = previous[-1]
    return EditCounts(substitutions, deletions, insertions)


def character_error_rate(reference: str, hypothesis: str) -> float:
    normalized_reference = normalize_text(reference)
    return edit_counts(reference, hypothesis).errors / max(1, len(normalized_reference))


def contains_anchor(hypothesis: str, anchor: str) -> bool:
    return normalize_text(anchor) in normalize_text(hypothesis)


def repeated_ngram_excess(reference: str, hypothesis: str, *, width: int = 2) -> int:
    """Count repeated hypothesis n-grams beyond occurrences in the reference."""

    def counts(text: str) -> dict[str, int]:
        normalized = normalize_text(text)
        result: dict[str, int] = {}
        for index in range(max(0, len(normalized) - width + 1)):
            ngram = normalized[index : index + width]
            result[ngram] = result.get(ngram, 0) + 1
        return result

    expected = counts(reference)
    actual = counts(hypothesis)
    # A single unseen n-gram is an ordinary substitution, not repetition.
    # Count only occurrences beyond one (or beyond the reference count when it
    # already repeats legitimately).
    return sum(max(0, count - max(1, expected.get(ngram, 0))) for ngram, count in actual.items())


def pcm16k(path: Path) -> tuple[bytes, float]:
    with wave.open(str(path), "rb") as audio:
        channels = audio.getnchannels()
        width = audio.getsampwidth()
        rate = audio.getframerate()
        frames = audio.readframes(audio.getnframes())
    if width != PCM_SAMPLE_WIDTH:
        frames = audioop.lin2lin(frames, width, PCM_SAMPLE_WIDTH)
        width = PCM_SAMPLE_WIDTH
    if channels != 1:
        if channels != 2:
            raise RuntimeError(f"unsupported WAV channel count: {channels}")
        frames = audioop.tomono(frames, width, 0.5, 0.5)
        channels = 1
    if rate != PCM_SAMPLE_RATE:
        frames, _state = audioop.ratecv(frames, width, channels, rate, PCM_SAMPLE_RATE, None)
    duration = len(frames) / (PCM_SAMPLE_RATE * PCM_SAMPLE_WIDTH)
    return frames, duration


def write_pcm_wav(path: Path, frames: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(PCM_SAMPLE_WIDTH)
        audio.setframerate(PCM_SAMPLE_RATE)
        audio.writeframes(frames)


def generated_negative_controls(directory: Path, duration_seconds: float = 3.0) -> list[dict[str, Any]]:
    sample_count = int(PCM_SAMPLE_RATE * duration_seconds)
    silence_path = directory / "negative_silence.wav"
    write_pcm_wav(silence_path, b"\x00\x00" * sample_count)

    # Seeded, low-amplitude broadband noise is deterministic and remains well
    # below the application's voice RMS/peak threshold.
    random_source = random.Random(20260717)
    noise_samples = [random_source.randint(-120, 120) for _ in range(sample_count)]
    noise_frames = b"".join(sample.to_bytes(2, "little", signed=True) for sample in noise_samples)
    noise_path = directory / "negative_low_noise.wav"
    write_pcm_wav(noise_path, noise_frames)
    return [
        {
            "id": "negative_silence",
            "category": "negative_control",
            "reference": "",
            "audio_path": silence_path,
            "evidence_type": "generated_negative_control",
        },
        {
            "id": "negative_low_noise",
            "category": "negative_control",
            "reference": "",
            "audio_path": noise_path,
            "evidence_type": "generated_negative_control",
        },
    ]


def extract_payload_text(payload: dict[str, Any]) -> str:
    sentences = payload.get("sentences") if isinstance(payload.get("sentences"), list) else []
    sentence_text = "".join(str(item.get("text") or "") for item in sentences if isinstance(item, dict))
    return str(payload.get("text") or payload.get("partial") or sentence_text).strip()


def payload_is_final(payload: dict[str, Any]) -> bool:
    return bool(payload.get("is_final")) or payload.get("mode") in {"2pass-offline", "offline"}


async def transcribe(
    case: dict[str, Any],
    *,
    endpoint: str,
    pace: float,
    final_timeout_seconds: float,
) -> dict[str, Any]:
    frames, duration_seconds = pcm16k(Path(case["audio_path"]))
    chunk_bytes = PCM_SAMPLE_RATE * PCM_SAMPLE_WIDTH * CHUNK_MILLISECONDS // 1_000
    started_at = time.perf_counter()
    first_audio_at: float | None = None
    first_partial_at: float | None = None
    stop_at: float | None = None
    final_at: float | None = None
    partials: list[str] = []
    final_text = ""

    async with websockets.connect(endpoint, max_size=8 * 1024 * 1024, open_timeout=15) as socket:
        await socket.send("START")
        await socket.send("LANGUAGE:zh-CN")

        async def sender() -> None:
            nonlocal first_audio_at, stop_at
            for offset in range(0, len(frames), chunk_bytes):
                first_audio_at = first_audio_at or time.perf_counter()
                await socket.send(frames[offset : offset + chunk_bytes])
                if pace > 0:
                    await asyncio.sleep(CHUNK_MILLISECONDS / 1_000 * pace)
            stop_at = time.perf_counter()
            await socket.send("STOP")

        async def receiver() -> None:
            nonlocal first_partial_at, final_at, final_text
            deadline = time.perf_counter() + duration_seconds * max(1.0, pace) + final_timeout_seconds
            while time.perf_counter() < deadline:
                try:
                    raw = await asyncio.wait_for(socket.recv(), timeout=min(2.0, max(0.05, deadline - time.perf_counter())))
                except asyncio.TimeoutError:
                    continue
                try:
                    payload = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    continue
                text = extract_payload_text(payload)
                is_final = payload_is_final(payload)
                if text and not is_final:
                    first_partial_at = first_partial_at or time.perf_counter()
                    partials.append(text)
                if is_final:
                    final_at = time.perf_counter()
                    final_text = text
                    return

        await asyncio.gather(sender(), receiver())

    reference = str(case.get("reference") or "")
    counts = edit_counts(reference, final_text)
    negative_control = not normalize_text(reference)
    result = {
        "id": case["id"],
        "category": case["category"],
        "evidence_type": case["evidence_type"],
        "reference": reference,
        "hypothesis": final_text,
        "reference_characters": len(normalize_text(reference)),
        "hypothesis_characters": len(normalize_text(final_text)),
        "audio_duration_seconds": round(duration_seconds, 3),
        "wall_seconds": round(time.perf_counter() - started_at, 3),
        "partial_count": len(partials),
        "first_partial_ms": round((first_partial_at - first_audio_at) * 1_000) if first_partial_at and first_audio_at else None,
        "final_after_stop_ms": round((final_at - stop_at) * 1_000) if final_at and stop_at else None,
        "substitutions": counts.substitutions,
        "deletions": counts.deletions,
        "insertions": counts.insertions,
        "cer": None if negative_control else round(character_error_rate(reference, final_text), 4),
        "head_anchor_ok": contains_anchor(final_text, str(case.get("head_anchor") or "")) if case.get("head_anchor") else None,
        "tail_anchor_ok": contains_anchor(final_text, str(case.get("tail_anchor") or "")) if case.get("tail_anchor") else None,
        "repeated_bigram_excess": repeated_ngram_excess(reference, final_text),
        "false_positive_characters": len(normalize_text(final_text)) if negative_control else 0,
    }
    return result


def aggregate(results: list[dict[str, Any]]) -> dict[str, Any]:
    positives = [item for item in results if item["reference_characters"]]
    negatives = [item for item in results if not item["reference_characters"]]
    reference_characters = sum(item["reference_characters"] for item in positives)
    total_errors = sum(item["substitutions"] + item["deletions"] + item["insertions"] for item in positives)
    partial_latencies = [item["first_partial_ms"] for item in positives if item["first_partial_ms"] is not None]
    final_latencies = [item["final_after_stop_ms"] for item in results if item["final_after_stop_ms"] is not None]
    return {
        "positive_cases": len(positives),
        "negative_controls": len(negatives),
        "micro_cer": round(total_errors / max(1, reference_characters), 4),
        "macro_cer": round(statistics.mean(item["cer"] for item in positives), 4) if positives else None,
        "head_anchor_pass_rate": round(statistics.mean(bool(item["head_anchor_ok"]) for item in positives), 4) if positives else None,
        "tail_anchor_pass_rate": round(statistics.mean(bool(item["tail_anchor_ok"]) for item in positives), 4) if positives else None,
        "deletion_rate": round(sum(item["deletions"] for item in positives) / max(1, reference_characters), 4),
        "insertion_rate": round(sum(item["insertions"] for item in positives) / max(1, reference_characters), 4),
        "repeated_bigram_excess": sum(item["repeated_bigram_excess"] for item in positives),
        "first_partial_p50_ms": round(statistics.median(partial_latencies)) if partial_latencies else None,
        "first_partial_max_ms": max(partial_latencies) if partial_latencies else None,
        "final_after_stop_p50_ms": round(statistics.median(final_latencies)) if final_latencies else None,
        "final_after_stop_max_ms": max(final_latencies) if final_latencies else None,
        "negative_false_positive_cases": sum(item["false_positive_characters"] > 0 for item in negatives),
        "negative_false_positive_characters": sum(item["false_positive_characters"] for item in negatives),
    }


def quality_score(summary: dict[str, Any], *, reference_characters: int) -> tuple[float, dict[str, Any]]:
    """Return a transparent 0–100 engineering score for this benchmark run.

    This score is intentionally conservative and only describes the supplied
    evidence set.  In particular, a TTS round trip cannot satisfy the human
    microphone release gate even when its numeric score is high.
    """

    accuracy = 60 * max(0.0, 1 - min(1.0, float(summary["micro_cer"])))
    boundaries = 5 * float(summary.get("head_anchor_pass_rate") or 0) + 5 * float(summary.get("tail_anchor_pass_rate") or 0)
    deletion = 5 * max(0.0, 1 - min(1.0, float(summary["deletion_rate"]) / 0.10))
    repetition_rate = float(summary["repeated_bigram_excess"]) / max(1, reference_characters)
    repetition = 5 * max(0.0, 1 - min(1.0, repetition_rate / 0.05))

    def latency_points(value: int | None, *, ideal: int, unacceptable: int) -> float:
        if value is None:
            return 0.0
        if value <= ideal:
            return 5.0
        if value >= unacceptable:
            return 0.0
        return 5 * (unacceptable - value) / (unacceptable - ideal)

    partial_latency = latency_points(summary.get("first_partial_p50_ms"), ideal=1_000, unacceptable=2_500)
    final_latency = latency_points(summary.get("final_after_stop_p50_ms"), ideal=1_000, unacceptable=3_000)
    negative_controls = 5 * max(
        0.0,
        1 - float(summary["negative_false_positive_cases"]) / max(1, int(summary["negative_controls"])),
    )
    isolation = 5.0 if not summary.get("cross_stream_contamination_cases") else 0.0
    components = {
        "accuracy_cer_60": round(accuracy, 2),
        "head_tail_10": round(boundaries, 2),
        "deletion_repetition_10": round(deletion + repetition, 2),
        "streaming_latency_10": round(partial_latency + final_latency, 2),
        "negative_controls_5": round(negative_controls, 2),
        "concurrent_isolation_5": round(isolation, 2),
    }
    return round(sum(components.values()), 2), components


def isolation_findings(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    positives = [item for item in results if item["reference_characters"]]
    findings: list[dict[str, Any]] = []
    for item in positives:
        own_cer = character_error_rate(item["reference"], item["hypothesis"])
        other_scores = [
            (other["id"], character_error_rate(other["reference"], item["hypothesis"]))
            for other in positives
            if other["id"] != item["id"]
        ]
        closest_other = min(other_scores, key=lambda pair: pair[1]) if other_scores else (None, math.inf)
        if closest_other[1] + 0.05 < own_cer:
            findings.append(
                {
                    "case_id": item["id"],
                    "closest_other_case_id": closest_other[0],
                    "own_cer": round(own_cer, 4),
                    "other_cer": round(closest_other[1], 4),
                }
            )
    return findings


def load_manifest(path: Path) -> dict[str, Any]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    cases = manifest.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("benchmark manifest must contain a non-empty cases list")
    seen: set[str] = set()
    for case in cases:
        if not isinstance(case, dict) or not str(case.get("id") or "").strip() or not str(case.get("reference") or "").strip():
            raise ValueError("every positive benchmark case needs id and reference")
        if case["id"] in seen:
            raise ValueError(f"duplicate benchmark case id: {case['id']}")
        seen.add(case["id"])
    return manifest


async def prepare_cases(
    manifest: dict[str, Any],
    *,
    audio_directory: Path | None,
    synthesize_lighttts: bool,
    working_directory: Path,
) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    tts_room_code = f"asr-benchmark-{int(time.time())}"
    for raw_case in manifest["cases"]:
        case = dict(raw_case)
        case_id = str(case["id"])
        if audio_directory:
            source = audio_directory / f"{case_id}.wav"
            if not source.is_file():
                raise FileNotFoundError(f"missing benchmark WAV: {source}")
            case["audio_path"] = source
            case["evidence_type"] = "preexisting_audio_artifact"
        elif synthesize_lighttts:
            voice = str(case.get("voice") or "debate_voice_1")
            await lighttts.synthesize(case["reference"], room_code=tts_room_code, speech_id=case_id, voice=voice)
            source = settings.media_path / tts_room_code / f"{case_id}.wav"
            target = working_directory / f"{case_id}.wav"
            shutil.copy2(source, target)
            case["audio_path"] = target
            case["evidence_type"] = "tts_roundtrip"
        else:
            raise ValueError("choose --audio-dir or --synthesize-lighttts")
        prepared.append(case)
    if synthesize_lighttts:
        shutil.rmtree(settings.media_path / tts_room_code, ignore_errors=True)
    return prepared


async def main_async(args: argparse.Namespace) -> dict[str, Any]:
    manifest = load_manifest(args.manifest)
    endpoint = args.endpoint or settings.funasr_ws_url
    with tempfile.TemporaryDirectory(prefix="jixia-asr-benchmark-") as temporary:
        working_directory = Path(temporary)
        prepared = await prepare_cases(
            manifest,
            audio_directory=args.audio_dir,
            synthesize_lighttts=args.synthesize_lighttts,
            working_directory=working_directory,
        )
        prepared.extend(generated_negative_controls(working_directory))
        if args.artifacts_dir:
            args.artifacts_dir.mkdir(parents=True, exist_ok=True)
            for case in prepared:
                shutil.copy2(case["audio_path"], args.artifacts_dir / f"{case['id']}.wav")
        semaphore = asyncio.Semaphore(args.concurrency)

        async def run_case(case: dict[str, Any]) -> dict[str, Any]:
            async with semaphore:
                return await transcribe(
                    case,
                    endpoint=endpoint,
                    pace=args.pace,
                    final_timeout_seconds=args.final_timeout,
                )

        results = await asyncio.gather(*(run_case(case) for case in prepared))

    contamination = isolation_findings(results)
    summary = aggregate(results) | {
        "cross_stream_contamination_cases": len(contamination),
    }
    reference_characters = sum(item["reference_characters"] for item in results)
    score, score_components = quality_score(summary, reference_characters=reference_characters)
    summary["quality_score_0_100"] = score
    report = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "corpus_schema_version": manifest.get("schema_version"),
        "evidence_note": "tts_roundtrip measures the deployed LightTTS→FunASR chain; it is not human-microphone CER evidence.",
        "release_gate_note": "真人麦克风固定语料 CER 仍需独立录音证据；TTS 回转分数不能单独通过 ASR 发布门槛。",
        "streaming": {
            "chunk_milliseconds": CHUNK_MILLISECONDS,
            "pace": args.pace,
            "pace_note": "pace only changes websocket transmission timing; it does not change the spoken audio speed or prosody.",
            "concurrency": args.concurrency,
            "latency_definition": {
                "first_partial_ms": "first non-final transcript minus first audio chunk send",
                "final_after_stop_ms": "first final transcript minus STOP send",
            },
        },
        "score_basis": {
            "components": score_components,
            "formula": (
                "CER accuracy 60 + head/tail 10 + deletion/repetition 10 + "
                "partial/final latency 10 + negative controls 5 + concurrent isolation 5"
            ),
            "thresholds": {
                "deletion_zero_points_at_rate": 0.10,
                "repetition_zero_points_at_excess_bigram_rate": 0.05,
                "partial_latency_full_points_ms": 1000,
                "partial_latency_zero_points_ms": 2500,
                "final_latency_full_points_ms": 1000,
                "final_latency_zero_points_ms": 3000
            },
            "interpretation": (
                "This is a reproducible engineering score for the current evidence set, "
                "not a validated human-speech accuracy score."
            ),
        },
        "aggregate": summary,
        "isolation_findings": contamination,
        "cases": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--audio-dir", type=Path, help="directory containing <case-id>.wav human/preexisting samples")
    source.add_argument("--synthesize-lighttts", action="store_true", help="generate samples through configured LightTTS")
    parser.add_argument("--endpoint", help="FunASR websocket URL; defaults to application settings and is never printed")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--pace", type=float, default=1.0, help="1.0 streams in real time; 0 sends as fast as possible")
    parser.add_argument("--final-timeout", type=float, default=30.0)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--artifacts-dir", type=Path, help="optional directory that retains the exact WAV inputs used")
    args = parser.parse_args()
    if args.concurrency < 1 or args.concurrency > 32:
        parser.error("--concurrency must be between 1 and 32")
    if args.pace < 0 or args.pace > 4:
        parser.error("--pace must be between 0 and 4")
    return args


def main() -> None:
    args = parse_args()
    report = asyncio.run(main_async(args))
    summary = report["aggregate"]
    print(
        "funasr_benchmark_complete "
        f"cases={summary['positive_cases']} micro_cer={summary['micro_cer']:.4f} "
        f"negative_false_positives={summary['negative_false_positive_cases']} "
        f"cross_stream_contamination={summary['cross_stream_contamination_cases']} "
        f"output={args.output.resolve()}"
    )


if __name__ == "__main__":
    main()
