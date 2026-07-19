#!/usr/bin/env python3
"""Repeatable LightTTS quality benchmark using the production application chain.

The script deliberately calls ``LightTTSProvider.synthesize`` instead of the
underlying endpoint directly.  Measurements therefore include the deployed
text splitter, retries, prompt/voice selection, WAV validation, and segment
merging.  It never changes provider configuration or database state.
"""

from __future__ import annotations

import argparse
import asyncio
import audioop
import json
import math
import re
import shutil
import statistics
import time
import wave
from array import array
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import websockets
from app.core.config import settings
from app.services.providers import LightTTSProvider

NORMALIZE_PATTERN = re.compile(r"[^\u4e00-\u9fffA-Za-z0-9]")
SILENCE_THRESHOLD_DBFS = -45.0
SILENCE_WINDOW_MS = 10
MIN_SILENCE_RUN_MS = 50


def normalize_text(value: str) -> str:
    return NORMALIZE_PATTERN.sub("", value).lower()


def levenshtein_distance(expected: str, actual: str) -> int:
    if len(expected) < len(actual):
        expected, actual = actual, expected
    previous = list(range(len(actual) + 1))
    for row, expected_character in enumerate(expected, start=1):
        current = [row]
        for column, actual_character in enumerate(actual, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[column] + 1,
                    previous[column - 1] + (expected_character != actual_character),
                )
            )
        previous = current
    return previous[-1]


def character_error_rate(expected: str, actual: str) -> tuple[float, int, int]:
    normalized_expected = normalize_text(expected)
    normalized_actual = normalize_text(actual)
    distance = levenshtein_distance(normalized_expected, normalized_actual)
    return distance / max(1, len(normalized_expected)), distance, len(normalized_expected)


def percentile(values: list[float], proportion: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * proportion
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def dbfs(amplitude: float, full_scale: float = 32768.0) -> float:
    if amplitude <= 0:
        return -120.0
    return 20.0 * math.log10(amplitude / full_scale)


def _silence_runs(window_dbfs: list[float]) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for index, value in enumerate(window_dbfs + [0.0]):
        silent = value <= SILENCE_THRESHOLD_DBFS and index < len(window_dbfs)
        if silent and start is None:
            start = index
        elif not silent and start is not None:
            runs.append((start, index))
            start = None
    return runs


def analyze_wav(path: Path) -> dict[str, Any]:
    with wave.open(str(path), "rb") as audio:
        channels = audio.getnchannels()
        sample_width = audio.getsampwidth()
        sample_rate = audio.getframerate()
        frame_count = audio.getnframes()
        compression = audio.getcomptype()
        frames = audio.readframes(frame_count)
    if sample_width != 2:
        raise RuntimeError(f"benchmark currently requires PCM16 WAV, got sample_width={sample_width}")
    samples = array("h")
    samples.frombytes(frames)
    if not samples:
        raise RuntimeError("LightTTS produced an empty WAV")
    peak = max(abs(item) for item in samples)
    rms = audioop.rms(frames, sample_width)
    clipping_samples = sum(1 for item in samples if abs(item) >= 32760)
    clipping_ratio = clipping_samples / len(samples)
    duration_seconds = frame_count / max(1, sample_rate)
    window_frames = max(1, round(sample_rate * SILENCE_WINDOW_MS / 1000))
    window_samples = window_frames * channels
    window_levels: list[float] = []
    for offset in range(0, len(samples), window_samples):
        chunk = samples[offset : offset + window_samples]
        if not chunk:
            continue
        chunk_rms = math.sqrt(sum(item * item for item in chunk) / len(chunk))
        window_levels.append(dbfs(chunk_rms))
    runs = _silence_runs(window_levels)
    significant = [(start, end) for start, end in runs if (end - start) * SILENCE_WINDOW_MS >= MIN_SILENCE_RUN_MS]
    internal = [(end - start) * SILENCE_WINDOW_MS for start, end in significant if start > 0 and end < len(window_levels)]
    leading_ms = significant[0][1] * SILENCE_WINDOW_MS if significant and significant[0][0] == 0 else 0
    trailing_ms = (
        (significant[-1][1] - significant[-1][0]) * SILENCE_WINDOW_MS if significant and significant[-1][1] == len(window_levels) else 0
    )
    file_bytes = path.stat().st_size
    theoretical_pcm_bytes = frame_count * channels * sample_width
    return {
        "format": f"WAV/{compression}",
        "sample_rate_hz": sample_rate,
        "channels": channels,
        "bit_depth": sample_width * 8,
        "duration_seconds": round(duration_seconds, 3),
        "file_bytes": file_bytes,
        "effective_kbps": round(file_bytes * 8 / max(duration_seconds, 0.001) / 1000, 1),
        "pcm_kbps": round(sample_rate * channels * sample_width * 8 / 1000, 1),
        "compression_ratio": round(theoretical_pcm_bytes / max(1, file_bytes), 3),
        "rms_dbfs": round(dbfs(rms), 2),
        "peak_dbfs": round(dbfs(peak), 2),
        "clipping_sample_ratio": round(clipping_ratio, 8),
        "leading_silence_ms": leading_ms,
        "trailing_silence_ms": trailing_ms,
        "internal_silence_count": len(internal),
        "internal_silence_median_ms": round(statistics.median(internal), 1) if internal else 0.0,
        "internal_silence_max_ms": max(internal, default=0),
        "internal_silence_over_500ms": sum(value > 500 for value in internal),
    }


def pcm16k(path: Path) -> bytes:
    with wave.open(str(path), "rb") as audio:
        channels = audio.getnchannels()
        width = audio.getsampwidth()
        rate = audio.getframerate()
        frames = audio.readframes(audio.getnframes())
    if width != 2:
        raise RuntimeError(f"FunASR conversion requires PCM16 WAV, got width={width}")
    if channels > 1:
        frames = audioop.tomono(frames, width, 0.5, 0.5)
        channels = 1
    if rate != 16000:
        frames, _state = audioop.ratecv(frames, width, channels, rate, 16000, None)
    return frames


async def transcribe(path: Path) -> dict[str, Any]:
    frames = pcm16k(path)
    started = time.perf_counter()
    async with websockets.connect(settings.funasr_ws_url, max_size=8 * 1024 * 1024) as socket:
        await socket.send("START")
        await socket.send("LANGUAGE:zh-CN")
        await asyncio.sleep(0.1)
        for offset in range(0, len(frames), 3200):
            await socket.send(frames[offset : offset + 3200])
            await asyncio.sleep(0.005)
        stopped = time.perf_counter()
        await socket.send("STOP")
        partial_text = ""
        final_text = ""
        provider_latency_ms = 0
        deadline = time.perf_counter() + 30
        while time.perf_counter() < deadline:
            try:
                raw = await asyncio.wait_for(socket.recv(), timeout=2)
            except asyncio.TimeoutError:
                continue
            payload = json.loads(raw)
            sentences = payload.get("sentences") if isinstance(payload.get("sentences"), list) else []
            sentence_text = "".join(str(item.get("text") or "") for item in sentences if isinstance(item, dict))
            text = str(payload.get("text") or payload.get("partial") or sentence_text).strip()
            is_final = bool(payload.get("is_final")) or payload.get("mode") in {"2pass-offline", "offline"}
            if text and is_final:
                final_text = text
            elif text:
                partial_text = text
            if is_final:
                provider_latency_ms = int(payload.get("latency_ms") or 0)
                break
        completed = time.perf_counter()
    return {
        "transcript": final_text or partial_text,
        "asr_feed_seconds": round(stopped - started, 3),
        "asr_final_after_stop_ms": round((completed - stopped) * 1000),
        "asr_provider_latency_ms": provider_latency_ms,
        "asr_is_final": bool(final_text),
    }


def load_cases(path: Path, profile: str, case_ids: set[str]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    corpus = json.loads(path.read_text(encoding="utf-8"))
    cases = []
    for item in corpus.get("cases", []):
        if case_ids and item.get("id") not in case_ids:
            continue
        if not case_ids and profile not in item.get("profiles", []):
            continue
        materialized = dict(item)
        target_characters = materialized.get("target_visible_characters")
        if target_characters is not None:
            target = int(target_characters)
            pattern = str(materialized.get("text_pattern") or "")
            if target < 1 or not pattern:
                raise SystemExit(f"invalid generated corpus case: {materialized.get('id')}")
            repetitions = math.ceil(target / max(1, LightTTSProvider._visible_characters(pattern)))
            materialized["text"] = (pattern * repetitions)[:target]
            if LightTTSProvider._visible_characters(materialized["text"]) != target:
                raise SystemExit(f"failed to materialize exact corpus length: {materialized.get('id')}")
        cases.append(materialized)
    if not cases:
        raise SystemExit("no corpus cases matched the requested selection")
    return corpus, cases


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    succeeded = [item for item in results if not item.get("error")]
    eligible = [item for item in succeeded if item.get("cer_eligible") and item.get("cer") is not None]
    cer_values = [float(item["cer"]) for item in eligible]
    synth_values = [float(item["synthesis_seconds"]) for item in succeeded]
    rtf_values = [float(item["rtf"]) for item in succeeded]
    loudness = [float(item["audio"]["rms_dbfs"]) for item in succeeded]
    peaks = [float(item["audio"]["peak_dbfs"]) for item in succeeded]
    per_voice: dict[str, dict[str, Any]] = {}
    for voice in sorted({str(item.get("voice")) for item in results}):
        voice_items = [item for item in succeeded if item.get("voice") == voice]
        voice_eligible = [float(item["cer"]) for item in voice_items if item.get("cer_eligible") and item.get("cer") is not None]
        per_voice[voice] = {
            "samples": len(voice_items),
            "cer_median": round(statistics.median(voice_eligible), 4) if voice_eligible else None,
            "cer_p95": round(percentile(voice_eligible, 0.95), 4) if voice_eligible else None,
            "rtf_median": round(statistics.median(float(item["rtf"]) for item in voice_items), 3) if voice_items else None,
            "rms_dbfs_median": round(statistics.median(float(item["audio"]["rms_dbfs"]) for item in voice_items), 2)
            if voice_items
            else None,
        }
    summary = {
        "samples_requested": len(results),
        "samples_succeeded": len(succeeded),
        "samples_failed": len(results) - len(succeeded),
        "cer_eligible_samples": len(eligible),
        "cer_median": round(statistics.median(cer_values), 4) if cer_values else None,
        "cer_p95": round(percentile(cer_values, 0.95), 4) if cer_values else None,
        "synthesis_seconds_median": round(statistics.median(synth_values), 3) if synth_values else None,
        "synthesis_seconds_p95": round(percentile(synth_values, 0.95), 3) if synth_values else None,
        "rtf_median": round(statistics.median(rtf_values), 3) if rtf_values else None,
        "rtf_p95": round(percentile(rtf_values, 0.95), 3) if rtf_values else None,
        "rms_dbfs_range": round(max(loudness) - min(loudness), 2) if loudness else None,
        "peak_dbfs_max": max(peaks) if peaks else None,
        "clipped_samples": sum(item["audio"]["clipping_sample_ratio"] > 0 for item in succeeded),
        "internal_silence_over_500ms_samples": sum(item["audio"]["internal_silence_over_500ms"] > 0 for item in succeeded),
        "per_voice": per_voice,
    }
    success_ratio = len(succeeded) / max(1, len(results))
    cer_median = summary["cer_median"]
    cer_p95 = summary["cer_p95"]
    content_score = round(
        20 * success_ratio
        + (
            50
            if cer_median is not None and cer_median <= 0.05
            else 35
            if cer_median is not None and cer_median <= 0.10
            else 20
            if cer_median is not None and cer_median <= 0.15
            else 0
        )
        + (
            30
            if cer_p95 is not None and cer_p95 <= 0.10
            else 20
            if cer_p95 is not None and cer_p95 <= 0.15
            else 10
            if cer_p95 is not None and cer_p95 <= 0.25
            else 0
        )
    )
    rtf_median = summary["rtf_median"]
    rtf_p95 = summary["rtf_p95"]
    synth_p50 = summary["synthesis_seconds_median"]
    realtime_score = round(
        (
            50
            if rtf_median is not None and rtf_median < 0.8
            else 40
            if rtf_median is not None and rtf_median < 1.0
            else 20
            if rtf_median is not None and rtf_median < 1.5
            else 0
        )
        + (
            30
            if rtf_p95 is not None and rtf_p95 < 0.8
            else 20
            if rtf_p95 is not None and rtf_p95 < 1.0
            else 10
            if rtf_p95 is not None and rtf_p95 < 1.5
            else 0
        )
        + (20 if synth_p50 is not None and synth_p50 <= 3 else 10 if synth_p50 is not None and synth_p50 <= 6 else 0)
    )
    summary["quality_gate_scores"] = {
        "asr_quality": {
            "score": None,
            "status": "BLOCKED",
            "reason": "TTS→FunASR 回转不能把 FunASR 自身错误与 TTS 错误分离，需固定真人参考音频独立测量。",
        },
        "tts_content_completeness": {
            "score": content_score,
            "status": "PASS" if content_score >= 80 else "FAIL",
            "reason": "按合成成功率、标准中文 CER 中位数和 P95 自动评分。",
        },
        "tts_naturalness": {
            "score": None,
            "status": "BLOCKED",
            "reason": "需要至少两个场景的人工试听与 MOS；自动波形指标不能替代自然度评分。",
        },
        "tts_realtime": {
            "score": realtime_score,
            "status": "PASS" if realtime_score >= 80 else "FAIL",
            "reason": "仅评价服务端完整 WAV 落盘耗时与 RTF，不代表浏览器首播延迟。",
        },
        "browser_playback_stability": {
            "score": None,
            "status": "BLOCKED",
            "reason": "需要 Computer Use、Network 和实际播放观测卡顿、缓冲、取消与旧音频复活。",
        },
    }
    return summary


def markdown_report(run: dict[str, Any]) -> str:
    summary = run["summary"]
    lines = [
        "# LightTTS 固定语料质量基准",
        "",
        f"- 执行时间（UTC）：{run['generated_at']}",
        f"- 语料版本：{run['corpus_version']}",
        f"- Profile：{run['profile']}",
        f"- 音色：{', '.join(run['voices'])}",
        f"- 成功/请求：{summary['samples_succeeded']}/{summary['samples_requested']}",
        f"- 标准中文回转 CER 中位数 / P95：{summary['cer_median']} / {summary['cer_p95']}",
        f"- 服务端完整 WAV 合成耗时 P50 / P95：{summary['synthesis_seconds_median']} s / {summary['synthesis_seconds_p95']} s",
        f"- RTF P50 / P95：{summary['rtf_median']} / {summary['rtf_p95']}",
        f"- 样本 RMS 响度极差：{summary['rms_dbfs_range']} dB",
        "",
        (
            "说明：合成耗时从应用 provider 调用开始到完整 WAV 原子落盘，包含排队、切分、重试和合并；"
            "它不是浏览器首帧或实际发声延迟。CER 是 LightTTS→FunASR 回转指标，必须结合人工试听，不能替代 MOS。"
        ),
        "",
        "| ID | 音色 | 字符 | 分段 | 合成 s | 音频 s | RTF | 格式 | kbps | RMS dBFS | 峰值 dBFS | 最大内静音 ms | CER | 回转文本 | 结论 |",
        "|---|---|---:|---:|---:|---:|---:|---|---:|---:|---:|---:|---:|---|---|",
    ]
    for item in run["results"]:
        if item.get("error"):
            lines.append(f"| {item['case_id']} | {item['voice']} | - | - | - | - | - | - | - | - | - | - | - | - | FAIL: {item['error']} |")
            continue
        audio = item["audio"]
        cer = f"{item['cer']:.1%}" if item.get("cer") is not None else "NOT-RUN"
        transcript = str(item.get("transcript") or "").replace("|", "\\|")
        conclusion = "PASS"
        if item.get("cer_eligible") and item.get("cer", 0) > 0.10:
            conclusion = "FAIL: CER>10%"
        elif item["rtf"] >= 0.8:
            conclusion = "FAIL: RTF>=0.8"
        elif audio["internal_silence_over_500ms"]:
            conclusion = "WARN: 内部静音>500ms"
        lines.append(
            f"| {item['case_id']} | {item['voice']} | {item['visible_characters']} | {item['chunk_count']} | "
            f"{item['synthesis_seconds']:.3f} | {audio['duration_seconds']:.3f} | {item['rtf']:.3f} | "
            f"{audio['format']}/{audio['sample_rate_hz']}Hz/{audio['bit_depth']}bit/{audio['channels']}ch | "
            f"{audio['effective_kbps']} | {audio['rms_dbfs']} | {audio['peak_dbfs']} | "
            f"{audio['internal_silence_max_ms']} | {cer} | {transcript} | {conclusion} |"
        )
    lines.extend(
        [
            "",
            "## 五项发布门槛评分框架",
            "",
            "| 项目 | 分数 | 状态 | 证据边界 |",
            "|---|---:|---|---|",
        ]
    )
    for key, label in (
        ("asr_quality", "ASR 质量"),
        ("tts_content_completeness", "TTS 内容完整性"),
        ("tts_naturalness", "TTS 自然度 / MOS"),
        ("tts_realtime", "TTS 实时性"),
        ("browser_playback_stability", "浏览器播放稳定性"),
    ):
        score = summary["quality_gate_scores"][key]
        rendered_score = str(score["score"]) if score["score"] is not None else "BLOCKED"
        lines.append(f"| {label} | {rendered_score} | {score['status']} | {score['reason']} |")
    lines.extend(
        [
            "",
            "## 自动门槛",
            "",
            "- 标准中文 CER：中位数 ≤5%，P95 ≤10%。",
            "- 长文本 RTF：建议 <0.8。这里统一报告所有样本，短句固定开销会使 RTF 更苛刻。",
            "- 内部非语义静音：单次不超过 500ms；自动静音检测也会包含正常标点停顿，需人工复核波形和试听。",
            "- 响度：跨样本 RMS 极差建议 ≤3dB，削波样本应为 0。",
            "- MOS 与浏览器首播/卡顿必须另行人工和 Computer Use 验收，本脚本不虚构这两项分数。",
            "",
        ]
    )
    return "\n".join(lines)


async def run(args: argparse.Namespace) -> int:
    corpus, cases = load_cases(args.corpus, args.profile, set(args.case_ids))
    output_dir = args.output_dir.resolve()
    sample_dir = output_dir / "samples"
    work_dir = output_dir / ".provider-work"
    sample_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    settings.media_root = str(work_dir)
    provider = LightTTSProvider()
    voices = [item.strip() for item in args.voices.split(",") if item.strip()]
    if not voices:
        raise SystemExit("--voices must contain at least one voice")
    results: list[dict[str, Any]] = []
    run_id = datetime.now(timezone.utc).strftime("ttsbench-%Y%m%dT%H%M%SZ")
    for voice in voices:
        for case in cases:
            case_id = str(case["id"])
            speech_id = f"{case_id.lower()}-{voice}"
            result: dict[str, Any] = {
                "case_id": case_id,
                "category": case.get("category"),
                "voice": voice,
                "text": case["text"],
                "cer_eligible": bool(case.get("cer_eligible")),
            }
            try:
                chunks = provider._split_text(str(case["text"]))
                result["visible_characters"] = provider._visible_characters(str(case["text"]))
                result["chunk_count"] = len(chunks)
                started = time.perf_counter()
                await provider.synthesize(str(case["text"]), room_code=run_id, speech_id=speech_id, voice=voice)
                result["synthesis_seconds"] = round(time.perf_counter() - started, 3)
                generated = settings.media_path / run_id / f"{speech_id}.wav"
                saved = sample_dir / f"AUDIO-{case_id}-{voice}.wav"
                shutil.copy2(generated, saved)
                audio = analyze_wav(saved)
                result["audio"] = audio
                result["rtf"] = round(result["synthesis_seconds"] / max(audio["duration_seconds"], 0.001), 3)
                if args.skip_asr:
                    result.update({"transcript": "", "cer": None})
                else:
                    asr = await transcribe(saved)
                    result.update(asr)
                    cer, edits, reference_characters = character_error_rate(str(case["text"]), asr["transcript"])
                    result.update(
                        {
                            "cer": round(cer, 4),
                            "cer_edits": edits,
                            "cer_reference_characters": reference_characters,
                        }
                    )
            except Exception as exc:  # benchmark must preserve partial evidence
                result["error"] = f"{type(exc).__name__}: {exc}"
            results.append(result)
            print(json.dumps(result, ensure_ascii=False), flush=True)
    run_document = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "corpus_version": corpus.get("version"),
        "profile": args.profile,
        "voices": voices,
        "funasr_roundtrip": not args.skip_asr,
        "thresholds": {
            "cer_median_max": 0.05,
            "cer_p95_max": 0.10,
            "rtf_max": 0.8,
            "rms_range_db_max": 3.0,
            "internal_silence_ms_max": 500,
        },
        "summary": summarize(results),
        "results": results,
    }
    (output_dir / "lighttts-benchmark.json").write_text(json.dumps(run_document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output_dir / "lighttts-benchmark.md").write_text(markdown_report(run_document), encoding="utf-8")
    if not args.keep_work:
        shutil.rmtree(work_dir, ignore_errors=True)
    print(json.dumps(run_document["summary"], ensure_ascii=False, indent=2))
    return 1 if run_document["summary"]["samples_failed"] else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--corpus",
        type=Path,
        default=Path(__file__).with_name("lighttts_benchmark_corpus.json"),
    )
    parser.add_argument("--profile", choices=("smoke", "core", "full", "extreme"), default="smoke")
    parser.add_argument("--case-id", dest="case_ids", action="append", default=[])
    parser.add_argument("--voices", default="debate_voice_1,debate_voice_2,debate_voice_3,debate_voice_4")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--skip-asr", action="store_true")
    parser.add_argument("--keep-work", action="store_true")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
