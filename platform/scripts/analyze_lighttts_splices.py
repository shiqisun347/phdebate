#!/usr/bin/env python3
"""Measure LightTTS WAV splice boundaries marked by inserted digital zero.

The production provider currently inserts a contiguous zero platform between
segments.  This tool locates that platform, then reports the surrounding
low-energy gap, hard-step level, and adjacent active RMS difference.  It is
intentionally read-only and uses only the Python standard library.
"""

from __future__ import annotations

import argparse
import json
import math
import struct
import wave
from pathlib import Path
from typing import Any

WINDOW_MS = 10
MIN_ZERO_PLATFORM_MS = 80
SILENCE_RMS_DBFS = -60.0
SILENCE_PEAK_DBFS = -50.0
ACTIVE_CONTEXT_MS = 250
ACTIVE_WINDOW_DBFS = -45.0
ACTIVE_SEARCH_MS = 2000


def dbfs(amplitude: float) -> float:
    if amplitude <= 0:
        return -120.0
    return 20.0 * math.log10(amplitude / 32768.0)


def rms(samples: tuple[int, ...] | list[int]) -> float:
    if not samples:
        return 0.0
    return math.sqrt(sum(sample * sample for sample in samples) / len(samples))


def zero_runs(samples: tuple[int, ...], minimum_samples: int) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for index, sample in enumerate(samples + (1,)):
        if sample == 0 and start is None:
            start = index
        elif sample != 0 and start is not None:
            if index - start >= minimum_samples:
                runs.append((start, index))
            start = None
    return runs


def analyze(path: Path) -> dict[str, Any]:
    with wave.open(str(path), "rb") as audio:
        channels = audio.getnchannels()
        width = audio.getsampwidth()
        rate = audio.getframerate()
        compression = audio.getcomptype()
        frame_count = audio.getnframes()
        frames = audio.readframes(frame_count)
    if (channels, width, compression) != (1, 2, "NONE"):
        raise RuntimeError(
            f"{path.name}: expected uncompressed PCM16 mono, got "
            f"channels={channels}, width={width}, compression={compression}"
        )
    samples = struct.unpack(f"<{len(frames) // 2}h", frames)
    minimum_zero_samples = round(rate * MIN_ZERO_PLATFORM_MS / 1000)
    candidates = [
        run
        for run in zero_runs(samples, minimum_zero_samples)
        if run[0] > 0 and run[1] < len(samples)
    ]
    if len(candidates) != 1:
        raise RuntimeError(f"{path.name}: expected one internal zero splice, found {len(candidates)}")
    zero_start, zero_end = candidates[0]
    window_samples = max(1, round(rate * WINDOW_MS / 1000))

    def silent_window(window_index: int) -> bool:
        start = window_index * window_samples
        chunk = samples[start : min(len(samples), start + window_samples)]
        return dbfs(rms(chunk)) <= SILENCE_RMS_DBFS and dbfs(max((abs(value) for value in chunk), default=0)) <= SILENCE_PEAK_DBFS

    first_window = zero_start // window_samples
    last_window = max(first_window, (zero_end - 1) // window_samples)
    low_start = first_window
    while low_start > 0 and silent_window(low_start - 1):
        low_start -= 1
    low_end = last_window + 1
    total_windows = math.ceil(len(samples) / window_samples)
    while low_end < total_windows and silent_window(low_end):
        low_end += 1

    active_windows_needed = max(1, round(ACTIVE_CONTEXT_MS / WINDOW_MS))
    active_search_windows = max(active_windows_needed, round(ACTIVE_SEARCH_MS / WINDOW_MS))

    def active_context(anchor_window: int, direction: int) -> list[int]:
        collected: list[tuple[int, ...]] = []
        for step in range(active_search_windows):
            window_index = anchor_window + direction * step
            if direction < 0:
                window_index -= 1
            if window_index < 0 or window_index >= total_windows:
                break
            start = window_index * window_samples
            chunk = samples[start : min(len(samples), start + window_samples)]
            if dbfs(rms(chunk)) > ACTIVE_WINDOW_DBFS:
                if direction < 0:
                    collected.insert(0, chunk)
                else:
                    collected.append(chunk)
                if len(collected) >= active_windows_needed:
                    break
        return [sample for chunk in collected for sample in chunk]

    before = active_context(first_window, -1)
    after = active_context(last_window + 1, 1)
    edge_samples = max(1, round(rate * WINDOW_MS / 1000))
    before_edge = samples[max(0, zero_start - edge_samples) : zero_start]
    after_edge = samples[zero_end : min(len(samples), zero_end + edge_samples)]
    before_rms_dbfs = dbfs(rms(before))
    after_rms_dbfs = dbfs(rms(after))
    before_segment_rms_dbfs = dbfs(rms(samples[:zero_start]))
    after_segment_rms_dbfs = dbfs(rms(samples[zero_end:]))
    return {
        "file": path.name,
        "sample_rate_hz": rate,
        "duration_seconds": round(frame_count / rate, 3),
        "splice_seconds": round(zero_start / rate, 4),
        "zero_platform_ms": round((zero_end - zero_start) * 1000 / rate, 1),
        "join_low_energy_ms": round((low_end - low_start) * WINDOW_MS, 1),
        "sample_before_zero": samples[zero_start - 1],
        "sample_before_zero_dbfs": round(dbfs(abs(samples[zero_start - 1])), 2),
        "sample_after_zero": samples[zero_end],
        "sample_after_zero_dbfs": round(dbfs(abs(samples[zero_end])), 2),
        "edge_10ms_before_rms_dbfs": round(dbfs(rms(before_edge)), 2),
        "edge_10ms_after_rms_dbfs": round(dbfs(rms(after_edge)), 2),
        "active_250ms_before_rms_dbfs": round(before_rms_dbfs, 2),
        "active_250ms_after_rms_dbfs": round(after_rms_dbfs, 2),
        "adjacent_active_rms_delta_db": round(abs(before_rms_dbfs - after_rms_dbfs), 2),
        "segment_before_rms_dbfs": round(before_segment_rms_dbfs, 2),
        "segment_after_rms_dbfs": round(after_segment_rms_dbfs, 2),
        "adjacent_segment_rms_delta_db": round(abs(before_segment_rms_dbfs - after_segment_rms_dbfs), 2),
    }


def markdown(document: dict[str, Any]) -> str:
    lines = [
        "# LightTTS 拼接边界审计",
        "",
        f"- 样本数：{document['summary']['samples']}",
        f"- 数字零平台范围：{document['summary']['zero_platform_ms_min']}–{document['summary']['zero_platform_ms_max']} ms",
        "- 拼接低能量间隔中位数/最大值："
        f"{document['summary']['join_low_energy_ms_median']} / "
        f"{document['summary']['join_low_energy_ms_max']} ms",
        f"- 相邻分段整体 RMS 差最大值：{document['summary']['adjacent_segment_rms_delta_db_max']} dB",
        f"- 插零前最后样本高于 -40dBFS 的硬阶跃样本：{document['summary']['hard_cut_samples']}",
        "",
        "| 文件 | 零平台 ms | 低能量间隔 ms | 前 10ms RMS | 后 10ms RMS | 相邻分段 RMS 差 dB |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for item in document["results"]:
        lines.append(
            f"| {item['file']} | {item['zero_platform_ms']} | {item['join_low_energy_ms']} | "
            f"{item['edge_10ms_before_rms_dbfs']} | {item['edge_10ms_after_rms_dbfs']} | "
            f"{item['adjacent_segment_rms_delta_db']} |"
        )
    lines.extend(
        [
            "",
            "说明：零平台是合并器插入的数字零标记；低能量间隔按 10ms 窗口、"
            "RMS≤-60dBFS 且 peak≤-50dBFS 计算。该指标只描述拼接边界，"
            "不把句内自然停顿混入统计。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-md", type=Path, required=True)
    args = parser.parse_args()
    results = [analyze(path) for path in sorted(args.input_dir.glob("*.wav"))]
    if not results:
        raise SystemExit("no WAV files found")
    zero_platforms = sorted(float(item["zero_platform_ms"]) for item in results)
    gaps = sorted(float(item["join_low_energy_ms"]) for item in results)
    midpoint = len(gaps) // 2
    median_gap = gaps[midpoint] if len(gaps) % 2 else (gaps[midpoint - 1] + gaps[midpoint]) / 2
    document = {
        "schema_version": 1,
        "thresholds": {
            "window_ms": WINDOW_MS,
            "minimum_zero_platform_ms": MIN_ZERO_PLATFORM_MS,
            "silence_rms_dbfs": SILENCE_RMS_DBFS,
            "silence_peak_dbfs": SILENCE_PEAK_DBFS,
            "active_context_ms": ACTIVE_CONTEXT_MS,
            "active_window_dbfs": ACTIVE_WINDOW_DBFS,
            "active_search_ms": ACTIVE_SEARCH_MS,
        },
        "summary": {
            "samples": len(results),
            "zero_platform_ms_min": min(zero_platforms),
            "zero_platform_ms_max": max(zero_platforms),
            "join_low_energy_ms_median": round(median_gap, 1),
            "join_low_energy_ms_max": max(gaps),
            "adjacent_active_rms_delta_db_max": max(float(item["adjacent_active_rms_delta_db"]) for item in results),
            "adjacent_segment_rms_delta_db_max": max(float(item["adjacent_segment_rms_delta_db"]) for item in results),
            "hard_cut_samples": sum(float(item["sample_before_zero_dbfs"]) > -40 for item in results),
        },
        "results": results,
    }
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.output_md.write_text(markdown(document), encoding="utf-8")


if __name__ == "__main__":
    main()
