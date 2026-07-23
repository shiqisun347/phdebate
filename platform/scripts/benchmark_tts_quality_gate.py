#!/usr/bin/env python3
"""Independent streaming TTS quality gate with an offline self-test mode.

The default ``fake`` mode is deterministic and needs no service or credential.
Real modes only call explicitly supplied candidate endpoints; they do not import
the application, read its settings, create rooms, or mutate provider state.

Supported TTS protocols:

* ``fake``: local PCM generator used for CI and installation checks.
* ``openai-pcm``: OpenAI-compatible HTTP streaming response containing raw PCM.
* ``moss-session``: native start -> incremental push/audio -> close sessions.

Supported ASR protocols:

* ``fake``: returns the expected text (valid only with fake TTS).
* ``funasr-ws``: the project's START/LANGUAGE/audio/STOP WebSocket protocol.
* ``openai-http``: OpenAI-compatible multipart transcription endpoint.

Secrets are accepted only through named environment variables. Endpoint URLs,
query strings, headers, payload extensions and secret values are never written
to the JSON or Markdown report.
"""

from __future__ import annotations

import argparse
import asyncio
import audioop
import json
import math
import os
import re
import statistics
import time
import uuid
import wave
from array import array
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
import websockets

NORMALIZE_PATTERN = re.compile(r"[^\u4e00-\u9fffA-Za-z0-9]")
DEFAULT_TEXT = "各位评委、同学，大家好。下面我将从事实、价值和长期影响三个方面展开论证。"
SILENCE_DBFS = -45.0
SILENCE_WINDOW_MS = 10
TIMBRE_ANALYSIS_MAX_SAMPLES = 8_192
TIMBRE_FREQUENCIES_HZ = (100, 160, 220, 320, 440, 650, 900, 1_300, 1_800, 2_600, 3_600, 5_200, 7_000)
FIXED_VOICE_IDS = [f"debate_voice_{index}" for index in range(1, 9)]
FIXED_SEAT_MAPPING = {
    "aff_1": "debate_voice_1",
    "aff_2": "debate_voice_2",
    "aff_3": "debate_voice_3",
    "aff_4": "debate_voice_4",
    "neg_1": "debate_voice_5",
    "neg_2": "debate_voice_6",
    "neg_3": "debate_voice_7",
    "neg_4": "debate_voice_8",
}


def normalize_text(value: str) -> str:
    return NORMALIZE_PATTERN.sub("", value).lower()


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


def distribution(values: list[float], digits: int = 3) -> dict[str, float | None]:
    return {
        "p50": round(percentile(values, 0.50), digits) if values else None,
        "p95": round(percentile(values, 0.95), digits) if values else None,
        "p99": round(percentile(values, 0.99), digits) if values else None,
        "max": round(max(values), digits) if values else None,
    }


def edit_statistics(expected: str, actual: str) -> dict[str, Any]:
    """Return CER plus insertion/deletion/substitution counts."""

    source = normalize_text(expected)
    target = normalize_text(actual)
    rows = len(source) + 1
    columns = len(target) + 1
    distance = [[0] * columns for _ in range(rows)]
    operation = [[""] * columns for _ in range(rows)]
    for row in range(1, rows):
        distance[row][0] = row
        operation[row][0] = "delete"
    for column in range(1, columns):
        distance[0][column] = column
        operation[0][column] = "insert"
    for row in range(1, rows):
        for column in range(1, columns):
            if source[row - 1] == target[column - 1]:
                distance[row][column] = distance[row - 1][column - 1]
                operation[row][column] = "match"
                continue
            candidates = (
                (distance[row - 1][column] + 1, "delete"),
                (distance[row][column - 1] + 1, "insert"),
                (distance[row - 1][column - 1] + 1, "substitute"),
            )
            distance[row][column], operation[row][column] = min(candidates, key=lambda item: item[0])
    counts = {"insertions": 0, "deletions": 0, "substitutions": 0}
    reversed_operations: list[str] = []
    row, column = len(source), len(target)
    while row or column:
        current = operation[row][column]
        reversed_operations.append(current)
        if current == "match":
            row -= 1
            column -= 1
        elif current == "delete":
            counts["deletions"] += 1
            row -= 1
        elif current == "insert":
            counts["insertions"] += 1
            column -= 1
        else:
            counts["substitutions"] += 1
            row -= 1
            column -= 1
    forward_source_operations = [item for item in reversed(reversed_operations) if item != "insert"]
    leading_deletions = 0
    for item in forward_source_operations:
        if item != "delete":
            break
        leading_deletions += 1
    trailing_deletions = 0
    for item in reversed(forward_source_operations):
        if item != "delete":
            break
        trailing_deletions += 1
    edits = sum(counts.values())
    reference_characters = len(source)
    common_prefix_characters = 0
    for expected_character, actual_character in zip(source, target):
        if expected_character != actual_character:
            break
        common_prefix_characters += 1
    common_suffix_characters = 0
    for expected_character, actual_character in zip(reversed(source), reversed(target)):
        if expected_character != actual_character:
            break
        common_suffix_characters += 1
    return {
        "cer": round(edits / max(1, reference_characters), 6),
        "swallowed_character_rate": round(counts["deletions"] / max(1, reference_characters), 6),
        "repeated_or_extra_character_rate": round(counts["insertions"] / max(1, reference_characters), 6),
        "edits": edits,
        "reference_characters": reference_characters,
        "leading_deletions": leading_deletions,
        "trailing_deletions": trailing_deletions,
        "leading_text_swallowed": leading_deletions > 0,
        "trailing_text_swallowed": trailing_deletions > 0,
        "first_character_match": bool(source and target and source[0] == target[0]),
        "last_character_match": bool(source and target and source[-1] == target[-1]),
        "common_prefix_characters": common_prefix_characters,
        "common_suffix_characters": common_suffix_characters,
        **counts,
    }


def _adjacent_repetition_events(value: str, *, minimum_ngram_characters: int = 2) -> list[dict[str, Any]]:
    """Return non-overlapping adjacent repeated n-gram runs.

    Single-character repetitions are intentionally ignored because valid
    Mandarin commonly contains forms such as ``常常`` or ``人人``.  At each
    position the longest repeated unit wins so one loop is not counted again as
    several shorter overlapping n-grams.
    """

    normalized = normalize_text(value)
    events: list[dict[str, Any]] = []
    start = 0
    while start + minimum_ngram_characters * 2 <= len(normalized):
        remaining = len(normalized) - start
        maximum = remaining // 2
        chosen: tuple[str, int] | None = None
        for size in range(maximum, minimum_ngram_characters - 1, -1):
            unit = normalized[start : start + size]
            if unit == normalized[start + size : start + size * 2]:
                chosen = (unit, size)
                break
        if chosen is None:
            start += 1
            continue
        unit, size = chosen
        repeats = 2
        while normalized[start + repeats * size : start + (repeats + 1) * size] == unit:
            repeats += 1
        events.append(
            {
                "ngram": unit,
                "ngram_characters": size,
                "repeat_count": repeats,
                "extra_cycles": repeats - 1,
                "start": start,
            }
        )
        start += size * repeats
    return events


def adjacent_repetition_statistics(expected: str, actual: str) -> dict[str, Any]:
    """Find adjacent phrase loops in ASR text beyond repetitions in the source."""

    expected_cycles: Counter[str] = Counter()
    for event in _adjacent_repetition_events(expected):
        expected_cycles[str(event["ngram"])] += int(event["extra_cycles"])
    actual_events = _adjacent_repetition_events(actual)
    unexpected: list[dict[str, Any]] = []
    consumed_expected: Counter[str] = Counter()
    for event in actual_events:
        unit = str(event["ngram"])
        cycles = int(event["extra_cycles"])
        allowed = max(0, expected_cycles[unit] - consumed_expected[unit])
        consumed_expected[unit] += min(cycles, allowed)
        extra_cycles = max(0, cycles - allowed)
        if extra_cycles:
            unexpected.append({**event, "unexpected_extra_cycles": extra_cycles})
    return {
        "adjacent_repetition_count": sum(int(event["unexpected_extra_cycles"]) for event in unexpected),
        "adjacent_repetition_events": unexpected,
    }


def pcm_wav_bytes(pcm: bytes, *, sample_rate: int, channels: int, sample_width: int) -> bytes:
    output = BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(channels)
        audio.setsampwidth(sample_width)
        audio.setframerate(sample_rate)
        audio.writeframes(pcm)
    return output.getvalue()


def pcm16k_mono(pcm: bytes, *, sample_rate: int, channels: int, sample_width: int) -> bytes:
    if sample_width != 2:
        raise RuntimeError("ASR conversion currently requires PCM16")
    converted = pcm
    if channels > 1:
        converted = audioop.tomono(converted, sample_width, 0.5, 0.5)
        channels = 1
    if sample_rate != 16_000:
        converted, _state = audioop.ratecv(converted, sample_width, channels, sample_rate, 16_000, None)
    return converted


def dbfs(amplitude: float, full_scale: float = 32768.0) -> float:
    if amplitude <= 0:
        return -120.0
    return 20.0 * math.log10(amplitude / full_scale)


def _window_dbfs(pcm: bytes, *, sample_rate: int, channels: int, sample_width: int) -> list[float]:
    if sample_width != 2:
        raise RuntimeError("quality analysis currently requires PCM16")
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    window_samples = max(channels, round(sample_rate * SILENCE_WINDOW_MS / 1000) * channels)
    levels: list[float] = []
    for offset in range(0, len(samples), window_samples):
        window = samples[offset : offset + window_samples]
        if len(window) < window_samples:
            continue
        rms = math.sqrt(sum(sample * sample for sample in window) / len(window))
        levels.append(dbfs(rms))
    return levels


def _edge_silence_ms(levels: list[float]) -> tuple[int, int]:
    leading = 0
    for value in levels:
        if value > SILENCE_DBFS:
            break
        leading += SILENCE_WINDOW_MS
    trailing = 0
    for value in reversed(levels):
        if value > SILENCE_DBFS:
            break
        trailing += SILENCE_WINDOW_MS
    return leading, trailing


def _mono_samples(pcm: bytes, *, channels: int, sample_width: int) -> array[int]:
    if sample_width != 2:
        raise RuntimeError("quality analysis currently requires PCM16")
    interleaved = array("h")
    interleaved.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    if channels == 1:
        return interleaved
    mono = array("h")
    for offset in range(0, len(interleaved) - channels + 1, channels):
        mono.append(round(sum(interleaved[offset : offset + channels]) / channels))
    return mono


def _goertzel_power(samples: array[int], *, sample_rate: int, frequency_hz: float) -> float:
    if not samples or frequency_hz <= 0 or frequency_hz >= sample_rate / 2:
        return 0.0
    omega = 2 * math.pi * frequency_hz / sample_rate
    coefficient = 2 * math.cos(omega)
    previous = 0.0
    previous_previous = 0.0
    for sample in samples:
        current = float(sample) + coefficient * previous - previous_previous
        previous_previous = previous
        previous = current
    return max(0.0, previous_previous**2 + previous**2 - coefficient * previous * previous_previous)


def timbre_signature(
    pcm: bytes,
    *,
    sample_rate: int,
    channels: int,
    sample_width: int,
) -> dict[str, Any]:
    """Return a cheap amplitude-invariant spectral fingerprint.

    This is deliberately dependency-free so it can run in the release gate. It
    is a drift detector for repeated synthesis of the same text, not a speaker
    verification model and not a substitute for listening.
    """

    samples = _mono_samples(pcm, channels=channels, sample_width=sample_width)
    if len(samples) > TIMBRE_ANALYSIS_MAX_SAMPLES:
        step = TIMBRE_ANALYSIS_MAX_SAMPLES // 2
        candidates = list(range(0, len(samples) - TIMBRE_ANALYSIS_MAX_SAMPLES + 1, step))
        final_start = len(samples) - TIMBRE_ANALYSIS_MAX_SAMPLES
        if not candidates or candidates[-1] != final_start:
            candidates.append(final_start)
        start = max(
            candidates,
            key=lambda offset: sum(
                sample * sample for sample in samples[offset : offset + TIMBRE_ANALYSIS_MAX_SAMPLES]
            ),
        )
        samples = samples[start : start + TIMBRE_ANALYSIS_MAX_SAMPLES]
    if not samples:
        raise RuntimeError("TTS returned no samples for timbre analysis")
    mean = statistics.fmean(samples)
    centered = array("h", (max(-32768, min(32767, round(sample - mean))) for sample in samples))
    frequencies = [frequency for frequency in TIMBRE_FREQUENCIES_HZ if frequency < sample_rate / 2]
    powers = [_goertzel_power(centered, sample_rate=sample_rate, frequency_hz=frequency) for frequency in frequencies]
    total = sum(powers)
    if total <= 0:
        raise RuntimeError("TTS returned no spectral energy")
    normalized = [power / total for power in powers]
    centroid = sum(frequency * weight for frequency, weight in zip(frequencies, normalized))
    low_frequency_indices = [index for index, frequency in enumerate(frequencies) if frequency <= 500]
    dominant_low_index = max(low_frequency_indices, key=lambda index: normalized[index])
    return {
        "spectral_signature": [round(value, 8) for value in normalized],
        "sampled_frequencies_hz": frequencies,
        "sample_count": len(centered),
        "sampled_spectral_centroid_hz": round(centroid, 3),
        "dominant_low_frequency_hz": frequencies[dominant_low_index],
        "boundary": "dependency-free spectral drift proxy; not speaker verification or MOS",
    }


def cosine_distance(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        raise ValueError("spectral signatures must have the same non-zero length")
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return 1.0
    return max(0.0, min(1.0, 1.0 - dot / (left_norm * right_norm)))


def playback_stalls(
    chunks: list[bytes],
    arrivals: list[float],
    *,
    sample_rate: int,
    channels: int,
    sample_width: int,
    initial_buffer_ms: float,
) -> dict[str, Any]:
    if not chunks or len(chunks) != len(arrivals):
        return {"stutter_count": 0, "underrun_ms": 0.0, "max_network_gap_ms": 0.0}
    bytes_per_ms = sample_rate * channels * sample_width / 1000
    first_at = arrivals[0]
    previous_at = first_at
    queued_ms = len(chunks[0]) / max(bytes_per_ms, 0.001)
    stutters = 0
    underrun_ms = 0.0
    gaps: list[float] = []
    for chunk, arrived_at in zip(chunks[1:], arrivals[1:]):
        gap_ms = (arrived_at - previous_at) * 1000
        gaps.append(gap_ms)
        previous_since_first = (previous_at - first_at) * 1000
        arrived_since_first = (arrived_at - first_at) * 1000
        consume_start = max(previous_since_first, initial_buffer_ms)
        consumed_ms = max(0.0, arrived_since_first - consume_start)
        if consumed_ms > queued_ms + 0.001:
            stutters += 1
            underrun_ms += consumed_ms - queued_ms
            queued_ms = 0.0
        else:
            queued_ms -= consumed_ms
        queued_ms += len(chunk) / max(bytes_per_ms, 0.001)
        previous_at = arrived_at
    return {
        "stutter_count": stutters,
        "underrun_ms": round(underrun_ms, 3),
        "max_network_gap_ms": round(max(gaps), 3) if gaps else 0.0,
        "network_chunk_gap_ms": distribution(gaps),
        "chunk_gap_over_200ms_count": sum(gap > 200 for gap in gaps),
    }


def analyze_audio(
    chunks: list[bytes],
    arrivals: list[float],
    *,
    sample_rate: int,
    channels: int,
    sample_width: int,
    initial_buffer_ms: float,
) -> dict[str, Any]:
    pcm = b"".join(chunks)
    if not pcm:
        raise RuntimeError("TTS returned no PCM")
    frame_bytes = channels * sample_width
    if len(pcm) % frame_bytes:
        raise RuntimeError("TTS returned frame-unaligned PCM")
    rms = audioop.rms(pcm, sample_width)
    peak = audioop.max(pcm, sample_width)
    samples = array("h")
    samples.frombytes(pcm)
    zero_crossings = sum(
        (left < 0 <= right) or (right < 0 <= left)
        for left, right in zip(samples, samples[1:])
    )
    levels = _window_dbfs(pcm, sample_rate=sample_rate, channels=channels, sample_width=sample_width)
    active_levels = [value for value in levels if value > SILENCE_DBFS]
    leading_silence_ms, trailing_silence_ms = _edge_silence_ms(levels)
    boundary_silence_ms: list[float] = []
    chunk_levels = [
        _window_dbfs(chunk, sample_rate=sample_rate, channels=channels, sample_width=sample_width) for chunk in chunks
    ]
    edges = [_edge_silence_ms(item) for item in chunk_levels]
    for index in range(len(edges) - 1):
        boundary_silence_ms.append(float(edges[index][1] + edges[index + 1][0]))
    first_non_silent_chunk_index: int | None = None
    first_non_silent_offset_in_chunk_ms: int | None = None
    stream_offset_ms = 0.0
    first_non_silent_stream_offset_ms: float | None = None
    bytes_per_ms = sample_rate * frame_bytes / 1000
    for index, (chunk, chunk_level_values) in enumerate(zip(chunks, chunk_levels)):
        for window_index, value in enumerate(chunk_level_values):
            if value > SILENCE_DBFS:
                first_non_silent_chunk_index = index
                first_non_silent_offset_in_chunk_ms = window_index * SILENCE_WINDOW_MS
                first_non_silent_stream_offset_ms = stream_offset_ms + first_non_silent_offset_in_chunk_ms
                break
        if first_non_silent_chunk_index is not None:
            break
        stream_offset_ms += len(chunk) / max(bytes_per_ms, 0.001)
    spectral = timbre_signature(
        pcm,
        sample_rate=sample_rate,
        channels=channels,
        sample_width=sample_width,
    )
    stall = playback_stalls(
        chunks,
        arrivals,
        sample_rate=sample_rate,
        channels=channels,
        sample_width=sample_width,
        initial_buffer_ms=initial_buffer_ms,
    )
    return {
        "duration_seconds": round(len(pcm) / max(1, sample_rate * frame_bytes), 3),
        "pcm_bytes": len(pcm),
        "chunks": len(chunks),
        "rms_dbfs": round(dbfs(rms), 3),
        "peak_dbfs": round(dbfs(peak), 3),
        "crest_factor_db": round(dbfs(peak) - dbfs(rms), 3),
        "zero_crossing_rate": round(zero_crossings / max(1, len(samples) - 1), 6),
        "active_window_rms_dbfs_stddev": round(statistics.pstdev(active_levels), 3) if len(active_levels) > 1 else 0.0,
        "leading_silence_ms": leading_silence_ms,
        "trailing_silence_ms": trailing_silence_ms,
        "contains_non_silent_pcm": first_non_silent_chunk_index is not None,
        "first_non_silent_chunk_index": first_non_silent_chunk_index,
        "first_non_silent_offset_in_chunk_ms": first_non_silent_offset_in_chunk_ms,
        "first_non_silent_stream_offset_ms": round(first_non_silent_stream_offset_ms, 3)
        if first_non_silent_stream_offset_ms is not None
        else None,
        "chunk_boundary_silence_ms": distribution(boundary_silence_ms),
        "long_chunk_boundary_silence_count": sum(value > 200 for value in boundary_silence_ms),
        "timbre": spectral,
        **stall,
    }


def redact_url(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlsplit(value)
    return f"{parsed.scheme or 'configured'}://<redacted-host>/<redacted>"


def is_loopback_url(value: str) -> bool:
    host = (urlsplit(value).hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1"}


def sanitize_error(exc: BaseException, *, endpoints: list[str], secrets: list[str]) -> str:
    rendered = f"{type(exc).__name__}: {exc}"
    for value in [*endpoints, *secrets]:
        if value:
            rendered = rendered.replace(value, "<redacted>")
    rendered = re.sub(r"(?i)\b(?:https?|wss?)://[^\s'\"]+", "<redacted-url>", rendered)
    rendered = re.sub(r"(?i)(authorization|api[-_ ]?key|token)(\s*[:=]\s*)\S+", r"\1\2<redacted>", rendered)
    return rendered[:1000]


@dataclass
class Synthesis:
    chunks: list[bytes]
    arrivals: list[float]
    request_started_at: float
    first_text_submitted_at: float
    first_packet_at: float | None
    completed_at: float
    sample_rate: int
    channels: int
    sample_width: int
    canceled: bool = False
    cancel_latency_ms: float | None = None
    close_acknowledged: bool | None = None
    remote_released: bool | None = None
    release_active: int | None = None
    release_orphan_count: int | None = None


class TTSAdapter(Protocol):
    async def synthesize(self, text: str, *, room_id: str, voice: str, cancel_after_ms: float | None = None) -> Synthesis: ...

    async def aclose(self) -> None: ...


class ASRAdapter(Protocol):
    async def transcribe(self, synthesis: Synthesis, expected_text: str) -> tuple[str, dict[str, Any]]: ...

    async def aclose(self) -> None: ...


@dataclass
class FakeTTS:
    sample_rate: int = 16_000
    channels: int = 1
    sample_width: int = 2
    chunk_ms: int = 80

    async def synthesize(self, text: str, *, room_id: str, voice: str, cancel_after_ms: float | None = None) -> Synthesis:
        del room_id, voice
        request_started_at = time.perf_counter()
        first_text_submitted_at = request_started_at
        chunks: list[bytes] = []
        arrivals: list[float] = []
        first_packet_at: float | None = None
        duration_ms = max(320, min(1600, len(normalize_text(text)) * 35))
        samples_per_chunk = self.sample_rate * self.chunk_ms // 1000
        total_chunks = math.ceil(duration_ms / self.chunk_ms)
        cancel_latency_ms: float | None = None
        canceled = False
        for chunk_index in range(total_chunks):
            await asyncio.sleep(0.001)
            now = time.perf_counter()
            if first_packet_at is None:
                first_packet_at = now
            simulated_since_first_packet_ms = chunk_index * self.chunk_ms
            if cancel_after_ms is not None and simulated_since_first_packet_ms >= cancel_after_ms:
                cancel_started = time.perf_counter()
                await asyncio.sleep(0)
                cancel_latency_ms = (time.perf_counter() - cancel_started) * 1000
                canceled = True
                break
            start_sample = chunk_index * samples_per_chunk
            samples = array(
                "h",
                (
                    round(7000 * math.sin(2 * math.pi * 220 * (start_sample + offset) / self.sample_rate))
                    for offset in range(samples_per_chunk)
                ),
            )
            chunks.append(samples.tobytes())
            arrivals.append(now)
        return Synthesis(
            chunks=chunks,
            arrivals=arrivals,
            request_started_at=request_started_at,
            first_text_submitted_at=first_text_submitted_at,
            first_packet_at=first_packet_at,
            completed_at=time.perf_counter(),
            sample_rate=self.sample_rate,
            channels=self.channels,
            sample_width=self.sample_width,
            canceled=canceled,
            cancel_latency_ms=cancel_latency_ms,
            close_acknowledged=True,
            remote_released=True,
            release_active=0,
            release_orphan_count=0,
        )

    async def aclose(self) -> None:
        return None


@dataclass
class FakeASR:
    async def transcribe(self, synthesis: Synthesis, expected_text: str) -> tuple[str, dict[str, Any]]:
        del synthesis
        await asyncio.sleep(0)
        return expected_text, {"asr_latency_ms": 0.0, "asr_final": True}

    async def aclose(self) -> None:
        return None


class OpenAIPCMAdapter:
    def __init__(
        self,
        *,
        endpoint: str,
        model: str,
        api_key: str | None,
        extra_payload: dict[str, Any],
        sample_rate: int,
        channels: int,
        sample_width: int,
        timeout_seconds: float,
    ) -> None:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.client = httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(timeout_seconds, connect=10, write=30, pool=10))
        self.endpoint = endpoint
        self.model = model
        self.extra_payload = extra_payload
        self.sample_rate = sample_rate
        self.channels = channels
        self.sample_width = sample_width

    async def synthesize(self, text: str, *, room_id: str, voice: str, cancel_after_ms: float | None = None) -> Synthesis:
        request_started_at = time.perf_counter()
        first_text_submitted_at = request_started_at
        chunks: list[bytes] = []
        arrivals: list[float] = []
        first_packet_at: float | None = None
        first_packet_event = asyncio.Event()
        payload = {
            "model": self.model,
            "input": text,
            "voice": voice,
            "response_format": "pcm",
            "stream": True,
            **self.extra_payload,
        }
        del room_id

        async def receive() -> None:
            nonlocal first_packet_at
            async with self.client.stream("POST", self.endpoint, json=payload) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    if not chunk:
                        continue
                    arrived_at = time.perf_counter()
                    if first_packet_at is None:
                        first_packet_at = arrived_at
                        first_packet_event.set()
                    chunks.append(bytes(chunk))
                    arrivals.append(arrived_at)

        worker = asyncio.create_task(receive())
        canceled = False
        cancel_latency_ms: float | None = None
        try:
            if cancel_after_ms is not None:
                first_wait = asyncio.create_task(first_packet_event.wait())
                done, _pending = await asyncio.wait({worker, first_wait}, return_when=asyncio.FIRST_COMPLETED)
                if first_wait in done and first_packet_event.is_set() and not worker.done():
                    await asyncio.sleep(cancel_after_ms / 1000)
                    cancel_started = time.perf_counter()
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)
                    cancel_latency_ms = (time.perf_counter() - cancel_started) * 1000
                    canceled = True
                first_wait.cancel()
                await asyncio.gather(first_wait, return_exceptions=True)
            await worker
        except asyncio.CancelledError:
            if not canceled:
                raise
        return Synthesis(
            chunks=chunks,
            arrivals=arrivals,
            request_started_at=request_started_at,
            first_text_submitted_at=first_text_submitted_at,
            first_packet_at=first_packet_at,
            completed_at=time.perf_counter(),
            sample_rate=self.sample_rate,
            channels=self.channels,
            sample_width=self.sample_width,
            canceled=canceled,
            cancel_latency_ms=cancel_latency_ms,
        )

    async def aclose(self) -> None:
        await self.client.aclose()


class MossSessionAdapter:
    def __init__(
        self,
        *,
        endpoints: list[str],
        voice_prompts: dict[str, str],
        chunk_characters: int,
        delta_delay_ms: float,
        api_key: str | None,
        timeout_seconds: float,
        release_timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        normalized = tuple(dict.fromkeys(endpoint.rstrip("/") for endpoint in endpoints if endpoint.strip()))
        if not normalized:
            raise ValueError("at least one MOSS endpoint is required")
        headers = {"X-MOSS-Gateway-Key": api_key} if api_key else {}
        self.client = httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(timeout_seconds, connect=10, write=30, pool=10),
            transport=transport,
        )
        self.endpoints = normalized
        self._available_endpoints: asyncio.Queue[str] = asyncio.Queue()
        for endpoint in normalized:
            self._available_endpoints.put_nowait(endpoint)
        self.voice_prompts = voice_prompts
        self.chunk_characters = max(1, chunk_characters)
        self.delta_delay_ms = max(0.0, delta_delay_ms)
        self.timeout_seconds = timeout_seconds
        self.release_timeout_seconds = max(0.1, release_timeout_seconds)

    async def _post(self, endpoint: str, suffix: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self.client.post(f"{endpoint}{suffix}", json=payload)
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict) or body.get("ok") is False:
            raise RuntimeError("invalid MOSS session control response")
        return body

    async def _readiness(self, endpoint: str) -> dict[str, Any]:
        response = await self.client.get(f"{endpoint}/health/ready")
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict) or body.get("ok") is not True:
            raise RuntimeError("MOSS readiness check failed")
        active = body.get("active")
        orphan_count = body.get("orphan_count")
        if isinstance(active, bool) or not isinstance(active, int) or active < 0:
            raise RuntimeError("MOSS readiness response is missing a valid active count")
        if isinstance(orphan_count, bool) or not isinstance(orphan_count, int) or orphan_count < 0:
            raise RuntimeError("MOSS readiness response is missing a valid orphan_count")
        return body

    async def _require_idle_ready(self, endpoint: str) -> dict[str, Any]:
        snapshot = await self._readiness(endpoint)
        if snapshot["active"] != 0 or snapshot["orphan_count"] != 0:
            raise RuntimeError("MOSS endpoint is not idle and orphan-free before synthesis")
        return snapshot

    async def _release_and_verify(
        self,
        endpoint: str,
        session_id: str,
        *,
        abort: bool = False,
    ) -> dict[str, Any]:
        suffix = "/tts/session/abort" if abort else "/tts/session/close"
        payload = {"session_id": session_id, "reason": "quality_gate_cancel"} if abort else {"session_id": session_id}
        body = await self._post(endpoint, suffix, payload)
        if body.get("released") is not True:
            raise RuntimeError("MOSS terminal control did not confirm released:true")
        deadline = time.monotonic() + self.release_timeout_seconds
        last_snapshot: dict[str, Any] | None = None
        while True:
            last_snapshot = await self._readiness(endpoint)
            if last_snapshot["active"] == 0 and last_snapshot["orphan_count"] == 0:
                return last_snapshot
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "MOSS endpoint did not return to active=0 and orphan_count=0 after release"
                )
            await asyncio.sleep(0.05)

    async def synthesize(self, text: str, *, room_id: str, voice: str, cancel_after_ms: float | None = None) -> Synthesis:
        session_id = f"quality-{room_id}-{uuid.uuid4().hex}"
        deltas = [text[index : index + self.chunk_characters] for index in range(0, len(text), self.chunk_characters)]
        if not deltas:
            raise RuntimeError("text is empty")
        request_started_at = time.perf_counter()
        endpoint = await self._available_endpoints.get()
        first_text_submitted_at = request_started_at
        chunks: list[bytes] = []
        arrivals: list[float] = []
        first_packet_at: float | None = None
        first_packet_event = asyncio.Event()
        stream_connected = asyncio.Event()
        sample_rate = 24_000
        close_acknowledged = False
        remote_released = False
        release_snapshot: dict[str, Any] | None = None
        cancel_latency_ms: float | None = None
        canceled = False
        session_started = False
        stream: asyncio.Task[None] | None = None

        async def receive() -> None:
            nonlocal first_packet_at, sample_rate
            async with self.client.stream("GET", f"{endpoint}/tts/session/{session_id}/audio", timeout=None) as response:
                response.raise_for_status()
                sample_rate = int(response.headers.get("X-Audio-Sample-Rate", "24000"))
                if response.headers.get("X-Audio-Codec", "pcm_s16le").lower() != "pcm_s16le":
                    raise RuntimeError("MOSS endpoint did not return pcm_s16le")
                stream_connected.set()
                async for chunk in response.aiter_bytes():
                    if not chunk:
                        continue
                    arrived_at = time.perf_counter()
                    if first_packet_at is None:
                        first_packet_at = arrived_at
                        first_packet_event.set()
                    chunks.append(bytes(chunk))
                    arrivals.append(arrived_at)

        try:
            await self._require_idle_ready(endpoint)
            first_text_submitted_at = time.perf_counter()
            await self._post(
                endpoint,
                "/tts/session/start",
                {
                    "session_id": session_id,
                    "assistant_text": "",
                    "user_text": None,
                    "prompt_audio": self.voice_prompts[voice],
                    "user_audio": None,
                    "new_turn": True,
                },
            )
            session_started = True
            stream = asyncio.create_task(receive())
            await asyncio.wait_for(stream_connected.wait(), timeout=10)
            first_text_submitted_at = time.perf_counter()
            await self._post(
                endpoint,
                "/tts/session/push",
                {"session_id": session_id, "text": deltas[0], "is_final": False},
            )
            for delta in deltas[1:]:
                await self._post(
                    endpoint,
                    "/tts/session/push",
                    {"session_id": session_id, "text": delta, "is_final": False},
                )
                if self.delta_delay_ms:
                    await asyncio.sleep(self.delta_delay_ms / 1000)
            if cancel_after_ms is None:
                await self._post(
                    endpoint,
                    "/tts/session/push",
                    {"session_id": session_id, "text": "", "is_final": True},
                )
                await asyncio.wait_for(stream, timeout=self.timeout_seconds)
                release_snapshot = await self._release_and_verify(endpoint, session_id)
                close_acknowledged = True
                remote_released = True
            else:
                first_wait = asyncio.create_task(first_packet_event.wait())
                done, _pending = await asyncio.wait_for(
                    asyncio.wait({stream, first_wait}, return_when=asyncio.FIRST_COMPLETED),
                    timeout=self.timeout_seconds,
                )
                if first_wait in done and first_packet_event.is_set() and not stream.done():
                    await asyncio.sleep(cancel_after_ms / 1000)
                    cancel_started = time.perf_counter()
                    release_snapshot = await self._release_and_verify(endpoint, session_id, abort=True)
                    close_acknowledged = True
                    remote_released = True
                    stream.cancel()
                    await asyncio.gather(stream, return_exceptions=True)
                    cancel_latency_ms = (time.perf_counter() - cancel_started) * 1000
                    canceled = True
                elif stream in done:
                    stream.result()
                first_wait.cancel()
                await asyncio.gather(first_wait, return_exceptions=True)
        finally:
            try:
                if session_started and not remote_released:
                    release_snapshot = await self._release_and_verify(
                        endpoint,
                        session_id,
                        abort=cancel_after_ms is not None or not close_acknowledged,
                    )
                    close_acknowledged = True
                    remote_released = True
            finally:
                if stream is not None and not stream.done():
                    stream.cancel()
                    await asyncio.gather(stream, return_exceptions=True)
                self._available_endpoints.put_nowait(endpoint)
        return Synthesis(
            chunks=chunks,
            arrivals=arrivals,
            request_started_at=request_started_at,
            first_text_submitted_at=first_text_submitted_at,
            first_packet_at=first_packet_at,
            completed_at=time.perf_counter(),
            sample_rate=sample_rate,
            channels=1,
            sample_width=2,
            canceled=canceled,
            cancel_latency_ms=cancel_latency_ms,
            close_acknowledged=close_acknowledged,
            remote_released=remote_released,
            release_active=int(release_snapshot["active"]) if release_snapshot is not None else None,
            release_orphan_count=int(release_snapshot["orphan_count"]) if release_snapshot is not None else None,
        )

    async def aclose(self) -> None:
        await self.client.aclose()


class FunASRWebSocketAdapter:
    def __init__(self, *, endpoint: str, timeout_seconds: float) -> None:
        self.endpoint = endpoint
        self.timeout_seconds = timeout_seconds

    async def transcribe(self, synthesis: Synthesis, expected_text: str) -> tuple[str, dict[str, Any]]:
        del expected_text
        pcm = pcm16k_mono(
            b"".join(synthesis.chunks),
            sample_rate=synthesis.sample_rate,
            channels=synthesis.channels,
            sample_width=synthesis.sample_width,
        )
        started_at = time.perf_counter()
        async with websockets.connect(self.endpoint, max_size=8 * 1024 * 1024) as socket:
            await socket.send("START")
            await socket.send("LANGUAGE:zh-CN")
            for offset in range(0, len(pcm), 3200):
                await socket.send(pcm[offset : offset + 3200])
                await asyncio.sleep(0.005)
            stopped_at = time.perf_counter()
            await socket.send("STOP")
            partial = ""
            final = ""
            provider_latency_ms = 0
            deadline = time.perf_counter() + self.timeout_seconds
            while time.perf_counter() < deadline:
                raw = await asyncio.wait_for(socket.recv(), timeout=min(2, max(0.1, deadline - time.perf_counter())))
                payload = json.loads(raw)
                sentences = payload.get("sentences") if isinstance(payload.get("sentences"), list) else []
                sentence_text = "".join(str(item.get("text") or "") for item in sentences if isinstance(item, dict))
                text = str(payload.get("text") or payload.get("partial") or sentence_text).strip()
                is_final = bool(payload.get("is_final")) or payload.get("mode") in {"2pass-offline", "offline"}
                if text and is_final:
                    final = text
                elif text:
                    partial = text
                if is_final:
                    provider_latency_ms = int(payload.get("latency_ms") or 0)
                    break
        completed_at = time.perf_counter()
        return final or partial, {
            "asr_latency_ms": round((completed_at - started_at) * 1000, 3),
            "asr_after_stop_ms": round((completed_at - stopped_at) * 1000, 3),
            "asr_provider_latency_ms": provider_latency_ms,
            "asr_final": bool(final),
        }

    async def aclose(self) -> None:
        return None


class OpenAIHTTPASRAdapter:
    def __init__(self, *, endpoint: str, model: str, api_key: str | None, timeout_seconds: float) -> None:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.client = httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(timeout_seconds, connect=10, write=30, pool=10))
        self.endpoint = endpoint
        self.model = model

    async def transcribe(self, synthesis: Synthesis, expected_text: str) -> tuple[str, dict[str, Any]]:
        del expected_text
        wav = pcm_wav_bytes(
            b"".join(synthesis.chunks),
            sample_rate=synthesis.sample_rate,
            channels=synthesis.channels,
            sample_width=synthesis.sample_width,
        )
        started_at = time.perf_counter()
        response = await self.client.post(
            self.endpoint,
            data={"model": self.model, "response_format": "json"},
            files={"file": ("quality-gate.wav", wav, "audio/wav")},
        )
        response.raise_for_status()
        body = response.json()
        transcript = str(body.get("text") or "").strip()
        return transcript, {
            "asr_latency_ms": round((time.perf_counter() - started_at) * 1000, 3),
            "asr_final": bool(transcript),
        }

    async def aclose(self) -> None:
        await self.client.aclose()


def parse_voice_prompts(values: list[str]) -> dict[str, str]:
    prompts: dict[str, str] = {}
    for raw in values:
        voice, separator, prompt = raw.partition("=")
        if not separator or not voice.strip() or not prompt.strip():
            raise SystemExit("--voice-prompt must use VOICE=remote-prompt-reference")
        prompts[voice.strip()] = prompt.strip()
    return prompts


def load_extra_payload(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    body = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(body, dict):
        raise SystemExit("--tts-extra-json-file must contain a JSON object")
    forbidden = {key for key in body if any(token in key.lower() for token in ("secret", "token", "api_key", "authorization"))}
    if forbidden:
        raise SystemExit("payload extension contains credential-like fields; use an environment variable for credentials")
    return body


def load_voice_manifest(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    body = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(body, dict):
        raise SystemExit("--voice-manifest must contain a JSON object")
    voice_ids = [item.get("voice_id") for item in body.get("voices", []) if isinstance(item, dict)]
    if body.get("expected_voice_ids") != FIXED_VOICE_IDS or voice_ids != FIXED_VOICE_IDS:
        raise SystemExit("voice manifest must contain debate_voice_1..8 in fixed order")
    if body.get("seat_mapping") != FIXED_SEAT_MAPPING:
        raise SystemExit("voice manifest seat mapping does not match the fixed 4v4 mapping")
    version = body.get("voice_set_version_sha256")
    if not isinstance(version, str) or not re.fullmatch(r"[0-9a-f]{64}", version):
        raise SystemExit("voice manifest is missing a valid voice-set version SHA-256")
    safe_voices = []
    for item in body["voices"]:
        audio = item.get("audio") if isinstance(item.get("audio"), dict) else {}
        prompt = item.get("prompt") if isinstance(item.get("prompt"), dict) else {}
        safe_voices.append(
            {
                "voice_id": item["voice_id"],
                "compliant": bool(item.get("compliant")),
                "audio_sha256": audio.get("sha256"),
                "prompt_sha256": prompt.get("sha256"),
            }
        )
    return {
        "voice_set_version_sha256": version,
        "seat_mapping": FIXED_SEAT_MAPPING,
        "voices": safe_voices,
        "complete_and_compliant": all(
            item["compliant"] and isinstance(item["audio_sha256"], str) and isinstance(item["prompt_sha256"], str)
            for item in safe_voices
        ),
    }


def load_mos_scores(path: Path | None) -> dict[str, list[float]]:
    if path is None:
        return {}
    body = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(body, dict):
        raise SystemExit("--mos-scores-json must contain VOICE=[1..5 scores]")
    scores: dict[str, list[float]] = {}
    for voice_id, values in body.items():
        if voice_id not in FIXED_VOICE_IDS or not isinstance(values, list) or not values:
            raise SystemExit("MOS scores must use debate_voice_1..8 and non-empty score arrays")
        numeric = [float(value) for value in values]
        if any(value < 1 or value > 5 for value in numeric):
            raise SystemExit("MOS scores must be between 1 and 5")
        scores[voice_id] = numeric
    return scores


def configured_tts_endpoints(args: argparse.Namespace) -> list[str]:
    if args.tts_mode == "moss-session":
        values = [*args.moss_endpoints]
        if not values and args.tts_endpoint:
            values.append(args.tts_endpoint)
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))
    return [args.tts_endpoint] if args.tts_endpoint else []


def require_endpoint_safety(args: argparse.Namespace, manifest: dict[str, Any] | None = None) -> None:
    tts_endpoints = configured_tts_endpoints(args)
    if args.tts_mode != "fake" and not tts_endpoints:
        raise SystemExit("real TTS mode requires --tts-endpoint/--moss-endpoint or its environment setting")
    if args.asr_mode != "fake" and not args.asr_endpoint:
        raise SystemExit("real ASR mode requires --asr-endpoint or TTS_QUALITY_ASR_ENDPOINT")
    if args.tts_mode == "fake" and args.asr_mode != "fake":
        raise SystemExit("fake TTS must use fake ASR")
    if args.tts_mode != "fake" and args.asr_mode == "fake":
        raise SystemExit("real TTS must use a real ASR mode")
    endpoints = [*tts_endpoints, *([args.asr_endpoint] if args.asr_endpoint else [])]
    if endpoints and not args.allow_remote and any(not is_loopback_url(value) for value in endpoints):
        raise SystemExit("remote endpoints require explicit --allow-remote")
    if args.tts_mode == "moss-session" and not args.voice_prompts:
        raise SystemExit("moss-session requires at least one --voice-prompt")
    if args.tts_mode != "fake" and manifest is None and not args.allow_unversioned_voices:
        raise SystemExit("real TTS quality gates require --voice-manifest unless --allow-unversioned-voices is explicit")
    if manifest is not None and not manifest["complete_and_compliant"] and not args.allow_incomplete_voice_manifest:
        raise SystemExit("voice manifest is incomplete/noncompliant; use diagnostic-only --allow-incomplete-voice-manifest to continue")


def build_adapters(
    args: argparse.Namespace,
    manifest: dict[str, Any] | None = None,
) -> tuple[TTSAdapter, ASRAdapter, list[str], list[str]]:
    tts_secret = os.getenv(args.tts_api_key_env, "") if args.tts_api_key_env else ""
    asr_secret = os.getenv(args.asr_api_key_env, "") if args.asr_api_key_env else ""
    if args.require_tts_api_key and not tts_secret:
        raise SystemExit(f"required TTS credential environment variable is unset: {args.tts_api_key_env}")
    if args.require_asr_api_key and not asr_secret:
        raise SystemExit(f"required ASR credential environment variable is unset: {args.asr_api_key_env}")
    if args.tts_mode == "fake":
        tts: TTSAdapter = FakeTTS(sample_rate=args.sample_rate, channels=args.channels, sample_width=args.sample_width)
        voices = ["selftest"]
    elif args.tts_mode == "openai-pcm":
        tts = OpenAIPCMAdapter(
            endpoint=args.tts_endpoint,
            model=args.tts_model,
            api_key=tts_secret or None,
            extra_payload=load_extra_payload(args.tts_extra_json_file),
            sample_rate=args.sample_rate,
            channels=args.channels,
            sample_width=args.sample_width,
            timeout_seconds=args.timeout_seconds,
        )
        voices = FIXED_VOICE_IDS if manifest is not None else [item.strip() for item in args.voices.split(",") if item.strip()]
    else:
        prompts = parse_voice_prompts(args.voice_prompts)
        tts = MossSessionAdapter(
            endpoints=configured_tts_endpoints(args),
            voice_prompts=prompts,
            chunk_characters=args.chunk_characters,
            delta_delay_ms=args.delta_delay_ms,
            api_key=tts_secret or None,
            timeout_seconds=args.timeout_seconds,
            release_timeout_seconds=args.moss_release_timeout_seconds,
        )
        voices = list(prompts)
        if manifest is not None and voices != FIXED_VOICE_IDS:
            raise SystemExit("MOSS --voice-prompt values must cover debate_voice_1..8 in fixed order")
    if not voices:
        raise SystemExit("at least one voice is required")
    if args.asr_mode == "fake":
        asr: ASRAdapter = FakeASR()
    elif args.asr_mode == "funasr-ws":
        asr = FunASRWebSocketAdapter(endpoint=args.asr_endpoint, timeout_seconds=args.timeout_seconds)
    else:
        asr = OpenAIHTTPASRAdapter(
            endpoint=args.asr_endpoint,
            model=args.asr_model,
            api_key=asr_secret or None,
            timeout_seconds=args.timeout_seconds,
        )
    return tts, asr, voices, [tts_secret, asr_secret]


async def run_sample(
    *,
    tts: TTSAdapter,
    asr: ASRAdapter,
    text: str,
    room_id: str,
    voice: str,
    initial_buffer_ms: float,
    error_context: dict[str, list[str]],
) -> dict[str, Any]:
    record: dict[str, Any] = {"room_id": room_id, "voice": voice, "text": text}
    try:
        synthesis = await tts.synthesize(text, room_id=room_id, voice=voice)
        if synthesis.first_packet_at is None:
            raise RuntimeError("TTS returned no first PCM packet")
        audio = analyze_audio(
            synthesis.chunks,
            synthesis.arrivals,
            sample_rate=synthesis.sample_rate,
            channels=synthesis.channels,
            sample_width=synthesis.sample_width,
            initial_buffer_ms=initial_buffer_ms,
        )
        first_non_silent_chunk_index = audio["first_non_silent_chunk_index"]
        if first_non_silent_chunk_index is None:
            raise RuntimeError("TTS returned no non-silent PCM")
        first_non_silent_packet_at = synthesis.arrivals[int(first_non_silent_chunk_index)]
        transcript, asr_metrics = await asr.transcribe(synthesis, text)
        content_metrics = edit_statistics(text, transcript)
        repetition_metrics = adjacent_repetition_statistics(text, transcript)
        record.update(
            {
                "first_packet_ms": round((synthesis.first_packet_at - synthesis.request_started_at) * 1000, 3),
                "first_char_to_first_pcm_ms": round((synthesis.first_packet_at - synthesis.first_text_submitted_at) * 1000, 3),
                "first_non_silent_packet_ms": round(
                    (first_non_silent_packet_at - synthesis.request_started_at) * 1000,
                    3,
                ),
                "first_char_to_first_non_silent_pcm_ms": round(
                    (first_non_silent_packet_at - synthesis.first_text_submitted_at) * 1000,
                    3,
                ),
                "total_tts_ms": round((synthesis.completed_at - synthesis.request_started_at) * 1000, 3),
                "sample_rate_hz": synthesis.sample_rate,
                "channels": synthesis.channels,
                "sample_width_bytes": synthesis.sample_width,
                "audio": audio,
                "duration_per_visible_character_ms": round(
                    audio["duration_seconds"] * 1000 / max(1, len(normalize_text(text))),
                    3,
                ),
                "transcript": transcript,
                "remote_released": synthesis.remote_released,
                "release_active": synthesis.release_active,
                "release_orphan_count": synthesis.release_orphan_count,
                **content_metrics,
                **repetition_metrics,
                **asr_metrics,
            }
        )
    except Exception as exc:
        record["error"] = sanitize_error(exc, **error_context)
    return record


def summarize_scenario(
    records: list[dict[str, Any]],
    concurrency: int,
    baseline_first_pcm_ms: float | None,
    baseline_first_non_silent_pcm_ms: float | None = None,
) -> dict[str, Any]:
    succeeded = [record for record in records if not record.get("error")]
    if baseline_first_pcm_ms is not None:
        for record in succeeded:
            record["inferred_queue_delay_ms"] = round(max(0.0, float(record["first_packet_ms"]) - baseline_first_pcm_ms), 3)
    if baseline_first_non_silent_pcm_ms is not None:
        for record in succeeded:
            record["inferred_non_silent_queue_delay_ms"] = round(
                max(0.0, float(record["first_non_silent_packet_ms"]) - baseline_first_non_silent_pcm_ms),
                3,
            )
    first_packet = [float(record["first_packet_ms"]) for record in succeeded]
    first_char = [float(record["first_char_to_first_pcm_ms"]) for record in succeeded]
    first_non_silent_packet = [float(record["first_non_silent_packet_ms"]) for record in succeeded]
    first_char_to_non_silent = [float(record["first_char_to_first_non_silent_pcm_ms"]) for record in succeeded]
    cer = [float(record["cer"]) for record in succeeded]
    swallowed = [float(record["swallowed_character_rate"]) for record in succeeded]
    repeated_or_extra = [float(record["repeated_or_extra_character_rate"]) for record in succeeded]
    adjacent_repetitions = [float(record["adjacent_repetition_count"]) for record in succeeded]
    queue = [float(record.get("inferred_queue_delay_ms", 0)) for record in succeeded]
    non_silent_queue = [float(record.get("inferred_non_silent_queue_delay_ms", 0)) for record in succeeded]
    stalls = [float(record["audio"]["stutter_count"]) for record in succeeded]
    boundary_max = [float(record["audio"]["chunk_boundary_silence_ms"]["max"] or 0) for record in succeeded]
    chunk_gap_p99 = [float(record["audio"]["network_chunk_gap_ms"]["p99"] or 0) for record in succeeded]
    loudness = [float(record["audio"]["rms_dbfs"]) for record in succeeded]
    return {
        "concurrency": concurrency,
        "requested": len(records),
        "succeeded": len(succeeded),
        "failed": len(records) - len(succeeded),
        "first_packet_ms": distribution(first_packet),
        "first_char_to_first_pcm_ms": distribution(first_char),
        "first_non_silent_packet_ms": distribution(first_non_silent_packet),
        "first_char_to_first_non_silent_pcm_ms": distribution(first_char_to_non_silent),
        "cer": distribution(cer, digits=6),
        "swallowed_character_rate": distribution(swallowed, digits=6),
        "repeated_or_extra_character_rate": distribution(repeated_or_extra, digits=6),
        "adjacent_repetition_count": {"total": int(sum(adjacent_repetitions)), **distribution(adjacent_repetitions)},
        "stutter_count": {"total": int(sum(stalls)), **distribution(stalls)},
        "chunk_boundary_silence_max_ms": distribution(boundary_max),
        "per_request_chunk_gap_p99_ms": distribution(chunk_gap_p99),
        "rms_dbfs_range": round(max(loudness) - min(loudness), 3) if loudness else None,
        "inferred_queue_delay_ms": distribution(queue),
        "inferred_non_silent_queue_delay_ms": distribution(non_silent_queue),
        "queue_delay_definition": (
            "max(0, request first PCM - single-room first-PCM P50); "
            "client-observed excess, not server instrumentation"
        ),
    }


def coefficient_of_variation(values: list[float]) -> float | None:
    if not values:
        return None
    mean = statistics.mean(values)
    if mean == 0:
        return 0.0
    return statistics.pstdev(values) / abs(mean)


def summarize_voice_drift(
    records: list[dict[str, Any]],
    voice: str,
    expected_rounds: int,
    *,
    max_timbre_cosine_distance: float = 0.08,
) -> dict[str, Any]:
    succeeded = [record for record in records if not record.get("error")]
    rms = [float(record["audio"]["rms_dbfs"]) for record in succeeded]
    duration = [float(record["duration_per_visible_character_ms"]) for record in succeeded]
    crossing = [float(record["audio"]["zero_crossing_rate"]) for record in succeeded]
    crest = [float(record["audio"]["crest_factor_db"]) for record in succeeded]
    cer = [float(record["cer"]) for record in succeeded]
    signatures = [list(map(float, record["audio"]["timbre"]["spectral_signature"])) for record in succeeded]
    signature_centroid = [statistics.fmean(values) for values in zip(*signatures)] if signatures else []
    timbre_distances = [cosine_distance(signature, signature_centroid) for signature in signatures]
    sampled_spectral_centroids = [
        float(record["audio"]["timbre"]["sampled_spectral_centroid_hz"]) for record in succeeded
    ]
    dominant_low_frequencies = [
        float(record["audio"]["timbre"]["dominant_low_frequency_hz"]) for record in succeeded
    ]
    metrics = {
        "voice": voice,
        "rounds_requested": expected_rounds,
        "rounds_succeeded": len(succeeded),
        "rms_dbfs_range": round(max(rms) - min(rms), 3) if rms else None,
        "duration_per_character_cv": round(coefficient_of_variation(duration), 6) if duration else None,
        "zero_crossing_rate_cv": round(coefficient_of_variation(crossing), 6) if crossing else None,
        "crest_factor_db_range": round(max(crest) - min(crest), 3) if crest else None,
        "timbre_cosine_distance": distribution(timbre_distances, digits=6),
        "sampled_spectral_centroid_cv": round(coefficient_of_variation(sampled_spectral_centroids), 6)
        if sampled_spectral_centroids
        else None,
        "dominant_low_frequency_cv": round(coefficient_of_variation(dominant_low_frequencies), 6)
        if dominant_low_frequencies
        else None,
        "cer_p95": distribution(cer, digits=6)["p95"],
        "leading_or_trailing_swallow_rounds": sum(
            bool(record["leading_text_swallowed"] or record["trailing_text_swallowed"]) for record in succeeded
        ),
        "proxy_boundary": (
            "Acoustic consistency proxy only (spectral fingerprint, RMS, duration, zero-crossing rate, crest factor); "
            "it does not replace speaker-embedding comparison or 20-round human listening."
        ),
    }
    metrics["proxy_passed"] = bool(
        len(succeeded) == expected_rounds
        and metrics["rms_dbfs_range"] is not None
        and metrics["rms_dbfs_range"] <= 3
        and metrics["duration_per_character_cv"] is not None
        and metrics["duration_per_character_cv"] <= 0.10
        and metrics["zero_crossing_rate_cv"] is not None
        and metrics["zero_crossing_rate_cv"] <= 0.15
        and metrics["crest_factor_db_range"] is not None
        and metrics["crest_factor_db_range"] <= 3
        and metrics["timbre_cosine_distance"]["p95"] is not None
        and metrics["timbre_cosine_distance"]["p95"] <= max_timbre_cosine_distance
        and metrics["sampled_spectral_centroid_cv"] is not None
        and metrics["sampled_spectral_centroid_cv"] <= 0.15
        and metrics["dominant_low_frequency_cv"] is not None
        and metrics["dominant_low_frequency_cv"] <= 0.20
        and metrics["leading_or_trailing_swallow_rounds"] == 0
    )
    return metrics


def summarize_per_voice(
    records: list[dict[str, Any]],
    voices: list[str],
    mos_scores: dict[str, list[float]],
) -> dict[str, Any]:
    summaries: dict[str, Any] = {}
    for voice in voices:
        voice_records = [record for record in records if record.get("voice") == voice]
        succeeded = [record for record in voice_records if not record.get("error")]
        cer = [float(record["cer"]) for record in succeeded]
        swallowed = [float(record["swallowed_character_rate"]) for record in succeeded]
        repeated_or_extra = [float(record["repeated_or_extra_character_rate"]) for record in succeeded]
        adjacent_repetitions = sum(int(record["adjacent_repetition_count"]) for record in succeeded)
        rms = [float(record["audio"]["rms_dbfs"]) for record in succeeded]
        active_rms_stddev = [float(record["audio"]["active_window_rms_dbfs_stddev"]) for record in succeeded]
        stutters = sum(int(record["audio"]["stutter_count"]) for record in succeeded)
        boundary = max(
            (float(record["audio"]["chunk_boundary_silence_ms"]["max"] or 0) for record in succeeded),
            default=None,
        )
        cer_p95 = distribution(cer, digits=6)["p95"]
        rms_range = round(max(rms) - min(rms), 3) if rms else None
        automatic_penalty = (
            min(2.0, (cer_p95 if cer_p95 is not None else 1.0) * 10)
            + min(1.0, stutters * 0.25)
            + min(0.5, (boundary or 0) / 400)
            + min(0.5, (rms_range or 0) / 6)
        )
        human = mos_scores.get(voice, [])
        summaries[voice] = {
            "samples_requested": len(voice_records),
            "samples_succeeded": len(succeeded),
            "cer": distribution(cer, digits=6),
            "swallowed_character_rate": distribution(swallowed, digits=6),
            "repeated_or_extra_character_rate": distribution(repeated_or_extra, digits=6),
            "adjacent_repetition_count": adjacent_repetitions,
            "leading_swallow_samples": sum(bool(record["leading_text_swallowed"]) for record in succeeded),
            "trailing_swallow_samples": sum(bool(record["trailing_text_swallowed"]) for record in succeeded),
            "first_character_mismatch_samples": sum(not bool(record["first_character_match"]) for record in succeeded),
            "last_character_mismatch_samples": sum(not bool(record["last_character_match"]) for record in succeeded),
            "stutter_count": stutters,
            "chunk_boundary_silence_max_ms": boundary,
            "rms_dbfs_range": rms_range,
            "active_window_rms_dbfs_stddev_max": max(active_rms_stddev, default=None),
            "mos_auxiliary": {
                "automatic_proxy_score_1_to_5": round(max(1.0, 5.0 - automatic_penalty), 3),
                "automatic_proxy_is_not_mos": True,
                "human_mos_mean": round(statistics.mean(human), 3) if human else None,
                "human_mos_ratings": len(human),
                "human_mos_passed": bool(human and statistics.mean(human) >= 4),
            },
        }
    return summaries


def evaluate_gates(document: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    scenarios = {item["concurrency"]: item["summary"] for item in document["scenarios"]}
    all_records = [record for item in document["scenarios"] for record in item["records"]] + document["drift"]["records"]
    succeeded = [record for record in all_records if not record.get("error")]
    rms = [float(record["audio"]["rms_dbfs"]) for record in succeeded]
    checks: dict[str, dict[str, Any]] = {}

    def add(name: str, actual: Any, limit: Any, passed: bool, detail: str) -> None:
        checks[name] = {"passed": bool(passed), "actual": actual, "limit": limit, "detail": detail}

    all_failed = sum(bool(record.get("error")) for record in all_records)
    add("zero_request_failures", all_failed, 0, all_failed == 0, "All synthesis and ASR round trips must complete.")
    cer_p95 = distribution([float(record["cer"]) for record in succeeded], digits=6)["p95"]
    add("cer_p95", cer_p95, args.max_cer, cer_p95 is not None and cer_p95 <= args.max_cer, "TTS→ASR round-trip character error rate.")
    swallow_p95 = distribution([float(record["swallowed_character_rate"]) for record in succeeded], digits=6)["p95"]
    add(
        "swallowed_character_rate_p95",
        swallow_p95,
        args.max_swallowed_rate,
        swallow_p95 is not None and swallow_p95 <= args.max_swallowed_rate,
        "Deletion-only share from the CER alignment.",
    )
    repeated_or_extra_p95 = distribution(
        [float(record["repeated_or_extra_character_rate"]) for record in succeeded],
        digits=6,
    )["p95"]
    add(
        "repeated_or_extra_character_rate_p95",
        repeated_or_extra_p95,
        args.max_repeated_or_extra_rate,
        repeated_or_extra_p95 is not None and repeated_or_extra_p95 <= args.max_repeated_or_extra_rate,
        "Insertion-only share from the CER alignment; catches repeated or unrelated extra characters.",
    )
    adjacent_repetitions = sum(int(record["adjacent_repetition_count"]) for record in succeeded)
    add(
        "unexpected_adjacent_repetition_count",
        adjacent_repetitions,
        args.max_adjacent_repetitions,
        adjacent_repetitions <= args.max_adjacent_repetitions,
        "Unexpected adjacent repeated n-gram cycles; units shorter than two characters are ignored.",
    )
    boundary_swallow = sum(
        bool(record["leading_text_swallowed"] or record["trailing_text_swallowed"]) for record in succeeded
    )
    add(
        "leading_or_trailing_swallow_samples",
        boundary_swallow,
        0,
        boundary_swallow == 0,
        "Explicit alignment check for missing first or last characters.",
    )
    boundary_character_mismatch = sum(
        not bool(record["first_character_match"] and record["last_character_match"]) for record in succeeded
    )
    add(
        "first_or_last_character_mismatch_samples",
        boundary_character_mismatch,
        0,
        boundary_character_mismatch == 0,
        "Exact normalized first/last-character check catches boundary substitutions as well as deletions.",
    )
    first_char_p95 = distribution([float(record["first_char_to_first_pcm_ms"]) for record in succeeded])["p95"]
    add(
        "first_char_to_first_pcm_p95_ms",
        first_char_p95,
        args.max_first_pcm_ms,
        first_char_p95 is not None and first_char_p95 <= args.max_first_pcm_ms,
        "Clock starts immediately before the first text-bearing control/request is submitted.",
    )
    first_char_max = distribution([float(record["first_char_to_first_pcm_ms"]) for record in succeeded])["max"]
    add(
        "first_char_to_first_pcm_max_ms",
        first_char_max,
        f"<{args.max_first_pcm_hard_ms}",
        first_char_max is not None and first_char_max < args.max_first_pcm_hard_ms,
        "Every request must produce first PCM in under the hard limit; P95 cannot hide one slow request.",
    )
    first_non_silent_p95 = distribution(
        [float(record["first_char_to_first_non_silent_pcm_ms"]) for record in succeeded]
    )["p95"]
    add(
        "first_char_to_first_non_silent_pcm_p95_ms",
        first_non_silent_p95,
        args.max_first_non_silent_ms,
        first_non_silent_p95 is not None and first_non_silent_p95 <= args.max_first_non_silent_ms,
        "Arrival time of the first packet that actually contains non-silent PCM; silent first packets do not pass this gate.",
    )
    first_non_silent_max = distribution(
        [float(record["first_char_to_first_non_silent_pcm_ms"]) for record in succeeded]
    )["max"]
    add(
        "first_char_to_first_non_silent_pcm_max_ms",
        first_non_silent_max,
        f"<{args.max_first_pcm_hard_ms}",
        first_non_silent_max is not None and first_non_silent_max < args.max_first_pcm_hard_ms,
        "Every request must expose non-silent PCM before the same hard three-second budget.",
    )
    leading_silence_max = max(
        (float(record["audio"]["leading_silence_ms"]) for record in succeeded),
        default=None,
    )
    add(
        "leading_silence_max_ms",
        leading_silence_max,
        args.max_leading_silence_ms,
        leading_silence_max is not None and leading_silence_max <= args.max_leading_silence_ms,
        "Maximum silent PCM before the first active 10ms window.",
    )
    stutters = sum(int(record["audio"]["stutter_count"]) for record in succeeded)
    add(
        "stutter_count",
        stutters,
        args.max_stutters,
        stutters <= args.max_stutters,
        f"Playback simulation starts with {args.initial_buffer_ms}ms buffer.",
    )
    boundary_max = max(
        (float(record["audio"]["chunk_boundary_silence_ms"]["max"] or 0) for record in succeeded),
        default=None,
    )
    add(
        "chunk_boundary_silence_max_ms",
        boundary_max,
        args.max_boundary_silence_ms,
        boundary_max is not None and boundary_max <= args.max_boundary_silence_ms,
        "Silence windows touching adjacent PCM chunk boundaries.",
    )
    chunk_gap_p99_max = max(
        (float(record["audio"]["network_chunk_gap_ms"]["p99"] or 0) for record in succeeded),
        default=None,
    )
    add(
        "pcm_chunk_gap_p99_max_ms",
        chunk_gap_p99_max,
        args.max_chunk_gap_ms,
        chunk_gap_p99_max is not None and chunk_gap_p99_max <= args.max_chunk_gap_ms,
        "Maximum per-request P99 arrival gap between non-empty PCM chunks.",
    )
    rms_range = round(max(rms) - min(rms), 3) if rms else None
    add(
        "rms_dbfs_range",
        rms_range,
        args.max_rms_range_db,
        rms_range is not None and rms_range <= args.max_rms_range_db,
        "Cross-sample RMS consistency.",
    )
    per_voice_rms_ranges = [
        float(summary["rms_dbfs_range"])
        for summary in document["per_voice"].values()
        if summary.get("rms_dbfs_range") is not None
    ]
    per_voice_rms_range_max = max(per_voice_rms_ranges, default=None)
    add(
        "per_voice_rms_dbfs_range_max",
        per_voice_rms_range_max,
        args.max_rms_range_db,
        per_voice_rms_range_max is not None and per_voice_rms_range_max <= args.max_rms_range_db,
        "Worst repeated-sample RMS range within one fixed voice.",
    )
    active_rms_stddev_max = max(
        (float(record["audio"]["active_window_rms_dbfs_stddev"]) for record in succeeded),
        default=None,
    )
    add(
        "active_window_rms_dbfs_stddev_max",
        active_rms_stddev_max,
        args.max_active_rms_stddev_db,
        active_rms_stddev_max is not None and active_rms_stddev_max <= args.max_active_rms_stddev_db,
        "Worst within-utterance active-window RMS variation; silence windows are excluded.",
    )
    single_endpoint_quality_only = bool(getattr(args, "single_endpoint_quality_only", False))
    if not single_endpoint_quality_only:
        two = scenarios.get(2)
        two_queue_p95 = two["inferred_queue_delay_ms"]["p95"] if two else None
        add(
            "two_room_queue_delay_p95_ms",
            two_queue_p95,
            args.max_queue_delay_ms,
            two_queue_p95 is not None and two_queue_p95 <= args.max_queue_delay_ms,
            "Two-room excess first-PCM latency over the one-room P50 baseline.",
        )
        two_non_silent_queue_p95 = two["inferred_non_silent_queue_delay_ms"]["p95"] if two else None
        add(
            "two_room_non_silent_queue_delay_p95_ms",
            two_non_silent_queue_p95,
            args.max_queue_delay_ms,
            two_non_silent_queue_p95 is not None and two_non_silent_queue_p95 <= args.max_queue_delay_ms,
            "Two-room excess first-non-silent-PCM latency over the one-room P50 baseline.",
        )
        three = scenarios.get(3)
        queue_p95 = three["inferred_queue_delay_ms"]["p95"] if three else None
        add(
            "three_room_queue_delay_p95_ms",
            queue_p95,
            args.max_queue_delay_ms,
            queue_p95 is not None and queue_p95 <= args.max_queue_delay_ms,
            "Three-room excess first-PCM latency over the one-room P50 baseline.",
        )
        three_non_silent_queue_p95 = three["inferred_non_silent_queue_delay_ms"]["p95"] if three else None
        add(
            "three_room_non_silent_queue_delay_p95_ms",
            three_non_silent_queue_p95,
            args.max_queue_delay_ms,
            three_non_silent_queue_p95 is not None and three_non_silent_queue_p95 <= args.max_queue_delay_ms,
            "Three-room excess first-non-silent-PCM latency over the one-room P50 baseline.",
        )
    drift_summaries = document["drift"]["per_voice"]
    drift_passed = bool(drift_summaries) and all(item["proxy_passed"] for item in drift_summaries.values())
    add(
        "continuous_voice_drift_proxy",
        sum(item["proxy_passed"] for item in drift_summaries.values()),
        len(document["voices"]),
        drift_passed,
        f"Each voice must complete {document['drift']['rounds_per_voice']} sequential rounds within proxy thresholds.",
    )
    drift_timbre_p95_max = max(
        (
            float(item["timbre_cosine_distance"]["p95"])
            for item in drift_summaries.values()
            if item["timbre_cosine_distance"]["p95"] is not None
        ),
        default=None,
    )
    add(
        "twenty_round_timbre_cosine_distance_p95_max",
        drift_timbre_p95_max,
        args.max_timbre_cosine_distance,
        document["drift"]["rounds_per_voice"] >= 20
        and drift_timbre_p95_max is not None
        and drift_timbre_p95_max <= args.max_timbre_cosine_distance,
        "Each voice needs at least 20 sequential same-text rounds; measures distance to that voice's spectral centroid.",
    )
    manifest = document.get("voice_manifest")
    if document["tts_mode"] != "fake":
        add(
            "fixed_eight_voice_manifest",
            bool(manifest and manifest["complete_and_compliant"]),
            True,
            bool(manifest and manifest["complete_and_compliant"]),
            "Fixed 4v4 mapping requires eight compliant, hashed prompt assets and transcripts.",
        )
    cancellation = document.get("cancellation", {})
    cancel_latency = cancellation.get("cancel_latency_ms")
    add(
        "cancel_latency_ms",
        cancel_latency,
        args.max_cancel_ms,
        cancel_latency is not None and cancel_latency <= args.max_cancel_ms and not cancellation.get("error"),
        "Time from cancel/close request through confirmed remote release and local stream shutdown.",
    )
    if document["tts_mode"] == "moss-session":
        released_samples = sum(
            bool(
                record.get("remote_released") is True
                and record.get("release_active") == 0
                and record.get("release_orphan_count") == 0
            )
            for record in succeeded
        )
        add(
            "moss_all_sessions_remotely_released",
            released_samples,
            len(succeeded),
            released_samples == len(succeeded),
            "Every completed MOSS sample must receive released:true and return its endpoint to active=0/orphan_count=0.",
        )
        cancellation_released = bool(
            cancellation.get("remote_released") is True
            and cancellation.get("release_active") == 0
            and cancellation.get("release_orphan_count") == 0
            and cancellation.get("close_acknowledged") is True
        )
        add(
            "moss_cancel_remotely_released",
            cancellation_released,
            True,
            cancellation_released,
            "Cancellation passes only after the remote worker is released and readiness is idle/orphan-free.",
        )
    manual_mos = [item["mos_auxiliary"]["human_mos_passed"] for item in document["per_voice"].values()]
    manual = {
        "human_mos_all_voices": {
            "passed": bool(manual_mos) and all(manual_mos),
            "required_mean": 4.0,
            "detail": "Automatic acoustic proxies are not MOS; every real voice still requires human ratings.",
        },
        "twenty_round_human_voice_identity": {
            "passed": False,
            "detail": "Requires human listening or a separately validated speaker-embedding system; proxy metrics are auxiliary only.",
        },
    }
    automatic_passed = all(item["passed"] for item in checks.values())
    return {
        "scope": "single_endpoint_quality" if single_endpoint_quality_only else "release",
        "passed": automatic_passed,
        "checks": checks,
        "manual_checks": manual,
        "release_ready": (
            not single_endpoint_quality_only
            and automatic_passed
            and all(item["passed"] for item in manual.values())
        ),
    }


def markdown_report(document: dict[str, Any]) -> str:
    lines = [
        "# Independent streaming TTS quality gate",
        "",
        f"- Generated: {document['generated_at']}",
        f"- Mode: `{document['tts_mode']}` + `{document['asr_mode']}`",
        f"- Endpoints: {document['endpoints']}",
        f"- Fixed voice-set version: {(document.get('voice_manifest') or {}).get('voice_set_version_sha256') or 'self-test/unversioned'}",
        f"- Initial playback buffer simulation: {document['initial_buffer_ms']} ms",
        f"- Gate scope: `{document['gate']['scope']}`",
        "- Queue delay is client-observed excess over single-room first-PCM P50; it is not server-side queue instrumentation.",
        "- Reports never include endpoint paths/query strings, credential values, request headers, or payload extensions.",
        "",
        (
            "| Rooms | Success | First packet P50/P95/max ms | First char→PCM P50/P95/max ms | "
            "First char→non-silent P50/P95/max ms | "
            "CER P95 | Swallowed P95 | Extra P95 | Adjacent loops | Stutters | Gap P99 max ms | "
            "Boundary silence max ms | Queue/Non-silent queue P95 ms | RMS range dB |"
        ),
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for scenario in document["scenarios"]:
        summary = scenario["summary"]
        first = summary["first_packet_ms"]
        marked = summary["first_char_to_first_pcm_ms"]
        non_silent = summary["first_char_to_first_non_silent_pcm_ms"]
        lines.append(
            f"| {summary['concurrency']} | {summary['succeeded']}/{summary['requested']} | "
            f"{first['p50']}/{first['p95']}/{first['max']} | {marked['p50']}/{marked['p95']}/{marked['max']} | "
            f"{non_silent['p50']}/{non_silent['p95']}/{non_silent['max']} | "
            f"{summary['cer']['p95']} | {summary['swallowed_character_rate']['p95']} | "
            f"{summary['repeated_or_extra_character_rate']['p95']} | {summary['adjacent_repetition_count']['total']} | "
            f"{summary['stutter_count']['total']} | {summary['per_request_chunk_gap_p99_ms']['max']} | "
            f"{summary['chunk_boundary_silence_max_ms']['max']} | "
            f"{summary['inferred_queue_delay_ms']['p95']}/{summary['inferred_non_silent_queue_delay_ms']['p95']} | "
            f"{summary['rms_dbfs_range']} |"
        )
    lines.extend(
        [
            "",
            "## Per-voice content and MOS auxiliary metrics",
            "",
            "Automatic proxy scores are waveform/content diagnostics, not MOS and not a substitute for listening.",
            "",
            "| Voice | Success | CER P95 | Swallowed P95 | Extra P95 | Adjacent loops | "
            "Head/Tail swallow | First/Last mismatch | Stutters | RMS range dB | Active RMS stddev max dB | Auto proxy /5 | Human MOS |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for voice, summary in document["per_voice"].items():
        mos = summary["mos_auxiliary"]
        lines.append(
            f"| {voice} | {summary['samples_succeeded']}/{summary['samples_requested']} | {summary['cer']['p95']} | "
            f"{summary['swallowed_character_rate']['p95']} | "
            f"{summary['repeated_or_extra_character_rate']['p95']} | {summary['adjacent_repetition_count']} | "
            f"{summary['leading_swallow_samples']}/{summary['trailing_swallow_samples']} | "
            f"{summary['first_character_mismatch_samples']}/{summary['last_character_mismatch_samples']} | "
            f"{summary['stutter_count']} | {summary['rms_dbfs_range']} | {summary['active_window_rms_dbfs_stddev_max']} | "
            f"{mos['automatic_proxy_score_1_to_5']} | {mos['human_mos_mean']} ({mos['human_mos_ratings']}) |"
        )
    lines.extend(
        [
            "",
            f"## Continuous {document['drift']['rounds_per_voice']}-round voice drift proxy",
            "",
            "| Voice | Success | RMS range dB | Duration/char CV | ZCR CV | Crest range dB | Timbre cosine P95 | "
            "Spectral centroid CV | Low-frequency CV | CER P95 | Boundary swallow | Proxy |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    for voice, drift in document["drift"]["per_voice"].items():
        lines.append(
            f"| {voice} | {drift['rounds_succeeded']}/{drift['rounds_requested']} | {drift['rms_dbfs_range']} | "
            f"{drift['duration_per_character_cv']} | {drift['zero_crossing_rate_cv']} | "
            f"{drift['crest_factor_db_range']} | {drift['timbre_cosine_distance']['p95']} | "
            f"{drift['sampled_spectral_centroid_cv']} | {drift['dominant_low_frequency_cv']} | {drift['cer_p95']} | "
            f"{drift['leading_or_trailing_swallow_rounds']} | {'PASS' if drift['proxy_passed'] else 'NO-GO'} |"
        )
    cancellation = document["cancellation"]
    lines.extend(
        [
            "",
            "## Cancellation probe",
            "",
            f"- Result: {'ERROR: ' + cancellation['error'] if cancellation.get('error') else 'completed'}",
            f"- Cancel latency: {cancellation.get('cancel_latency_ms')} ms",
            f"- Close acknowledged: {cancellation.get('close_acknowledged')}",
            f"- Remote released: {cancellation.get('remote_released')}",
            f"- Release active/orphans: {cancellation.get('release_active')}/{cancellation.get('release_orphan_count')}",
            "",
            "## Automatic gates",
            "",
            "| Check | Actual | Limit | Result |",
            "|---|---:|---:|---|",
        ]
    )
    for name, check in document["gate"]["checks"].items():
        lines.append(f"| {name} | {check['actual']} | {check['limit']} | {'PASS' if check['passed'] else 'FAIL'} |")
    lines.extend(["", "## Manual release gates", ""])
    for name, check in document["gate"]["manual_checks"].items():
        lines.append(f"- {name}: {'PASS' if check['passed'] else 'BLOCKED'} — {check['detail']}")
    lines.extend(
        [
            "",
            f"Automatic gate: **{'PASS' if document['gate']['passed'] else 'NO-GO'}**",
            f"Release ready including manual gates: **{'YES' if document['gate']['release_ready'] else 'NO'}**",
            "",
        ]
    )
    if document["gate"]["scope"] == "single_endpoint_quality":
        lines.extend(
            [
                "Single-endpoint mode validates one-stream quality only; it is not a release gate and does not test multi-room queueing.",
                "",
            ]
        )
    return "\n".join(lines)


async def run(args: argparse.Namespace) -> int:
    manifest = load_voice_manifest(args.voice_manifest)
    mos_scores = load_mos_scores(args.mos_scores_json)
    require_endpoint_safety(args, manifest)
    output_dir = args.output_dir.expanduser().resolve()
    if output_dir.exists() and any(output_dir.iterdir()) and not args.overwrite:
        raise SystemExit(f"output directory is not empty; use --overwrite to replace reports: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    tts, asr, voices, secrets = build_adapters(args, manifest)
    tts_endpoints = configured_tts_endpoints(args)
    endpoints = [*tts_endpoints, *([args.asr_endpoint] if args.asr_endpoint else [])]
    error_context = {"endpoints": endpoints, "secrets": secrets}
    scenarios: list[dict[str, Any]] = []
    drift_records: list[dict[str, Any]] = []
    try:
        for warmup_index in range(args.warmup_requests):
            warmup = await tts.synthesize(args.text, room_id=f"warmup-{warmup_index}", voice=voices[warmup_index % len(voices)])
            if warmup.first_packet_at is None:
                raise RuntimeError("warmup returned no PCM")
        baseline_first_pcm_ms: float | None = None
        baseline_first_non_silent_pcm_ms: float | None = None
        for concurrency in args.concurrency:
            records: list[dict[str, Any]] = []
            for round_index in range(args.rounds):
                tasks = [
                    run_sample(
                        tts=tts,
                        asr=asr,
                        text=args.text,
                        room_id=f"c{concurrency}-r{round_index + 1}-room{offset + 1}",
                        voice=voices[(round_index * concurrency + offset) % len(voices)],
                        initial_buffer_ms=args.initial_buffer_ms,
                        error_context=error_context,
                    )
                    for offset in range(concurrency)
                ]
                batch = await asyncio.gather(*tasks)
                records.extend(batch)
                for record in batch:
                    print(json.dumps(record, ensure_ascii=False), flush=True)
            provisional = summarize_scenario(
                records,
                concurrency,
                baseline_first_pcm_ms,
                baseline_first_non_silent_pcm_ms,
            )
            if concurrency == 1 and provisional["first_packet_ms"]["p50"] is not None:
                baseline_first_pcm_ms = float(provisional["first_packet_ms"]["p50"])
                baseline_first_non_silent_pcm_ms = float(provisional["first_non_silent_packet_ms"]["p50"])
                provisional = summarize_scenario(
                    records,
                    concurrency,
                    baseline_first_pcm_ms,
                    baseline_first_non_silent_pcm_ms,
                )
            scenarios.append({"concurrency": concurrency, "summary": provisional, "records": records})
        for voice in voices:
            for drift_round in range(args.drift_rounds):
                record = await run_sample(
                    tts=tts,
                    asr=asr,
                    text=args.drift_text,
                    room_id=f"drift-{voice}-{drift_round + 1}",
                    voice=voice,
                    initial_buffer_ms=args.initial_buffer_ms,
                    error_context=error_context,
                )
                record["drift_round"] = drift_round + 1
                drift_records.append(record)
                print(json.dumps(record, ensure_ascii=False), flush=True)
        cancellation: dict[str, Any]
        try:
            probe = await tts.synthesize(
                args.text * 3,
                room_id="cancel-probe",
                voice=voices[0],
                cancel_after_ms=args.cancel_after_first_pcm_ms,
            )
            cancellation = {
                "canceled": probe.canceled,
                "cancel_latency_ms": round(probe.cancel_latency_ms, 3) if probe.cancel_latency_ms is not None else None,
                "pcm_chunks_before_cancel": len(probe.chunks),
                "close_acknowledged": probe.close_acknowledged,
                "remote_released": probe.remote_released,
                "release_active": probe.release_active,
                "release_orphan_count": probe.release_orphan_count,
            }
            if not probe.canceled:
                cancellation["error"] = "stream completed before cancellation could be exercised"
        except Exception as exc:
            cancellation = {"error": sanitize_error(exc, **error_context)}
    finally:
        await asyncio.gather(tts.aclose(), asr.aclose(), return_exceptions=True)
    document: dict[str, Any] = {
        "schema_version": 4,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tts_mode": args.tts_mode,
        "asr_mode": args.asr_mode,
        "endpoints": {
            "tts": [redact_url(value) for value in tts_endpoints],
            "asr": redact_url(args.asr_endpoint),
            "redacted": True,
        },
        "credential_sources": {
            "tts": args.tts_api_key_env if args.tts_api_key_env else None,
            "asr": args.asr_api_key_env if args.asr_api_key_env else None,
            "values_recorded": False,
        },
        "text": args.text,
        "voices": voices,
        "fixed_seat_mapping": FIXED_SEAT_MAPPING if manifest is not None else None,
        "voice_manifest": manifest,
        "rounds": args.rounds,
        "warmup_requests_excluded": args.warmup_requests,
        "initial_buffer_ms": args.initial_buffer_ms,
        "scenarios": scenarios,
        "drift": {
            "rounds_per_voice": args.drift_rounds,
            "text": args.drift_text,
            "per_voice": {
                voice: summarize_voice_drift(
                    [record for record in drift_records if record.get("voice") == voice],
                    voice,
                    args.drift_rounds,
                    max_timbre_cosine_distance=args.max_timbre_cosine_distance,
                )
                for voice in voices
            },
            "records": drift_records,
        },
        "cancellation": cancellation,
    }
    all_quality_records = [record for item in scenarios for record in item["records"]] + drift_records
    document["per_voice"] = summarize_per_voice(all_quality_records, voices, mos_scores)
    document["gate"] = evaluate_gates(document, args)
    json_path = output_dir / "tts-quality-gate.json"
    markdown_path = output_dir / "tts-quality-gate.md"
    json_path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(markdown_report(document), encoding="utf-8")
    print(json.dumps({"gate": document["gate"], "json": str(json_path), "markdown": str(markdown_path)}, ensure_ascii=False, indent=2))
    return 0 if document["gate"]["passed"] else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tts-mode", choices=("fake", "openai-pcm", "moss-session"), default="fake")
    parser.add_argument("--asr-mode", choices=("fake", "funasr-ws", "openai-http"), default="fake")
    parser.add_argument("--tts-endpoint", default=os.getenv("TTS_QUALITY_TTS_ENDPOINT", ""))
    parser.add_argument("--moss-endpoint", dest="moss_endpoints", action="append", default=[])
    parser.add_argument("--asr-endpoint", default=os.getenv("TTS_QUALITY_ASR_ENDPOINT", ""))
    parser.add_argument("--allow-remote", action="store_true", help="Explicitly permit non-loopback candidate endpoints.")
    parser.add_argument("--tts-api-key-env", default="TTS_QUALITY_TTS_API_KEY")
    parser.add_argument("--asr-api-key-env", default="TTS_QUALITY_ASR_API_KEY")
    parser.add_argument("--require-tts-api-key", action="store_true")
    parser.add_argument("--require-asr-api-key", action="store_true")
    parser.add_argument("--tts-model", default="tts-1")
    parser.add_argument("--asr-model", default="whisper-1")
    parser.add_argument("--tts-extra-json-file", type=Path)
    parser.add_argument("--voice-manifest", type=Path)
    parser.add_argument("--allow-unversioned-voices", action="store_true")
    parser.add_argument("--allow-incomplete-voice-manifest", action="store_true")
    parser.add_argument("--mos-scores-json", type=Path)
    parser.add_argument("--voice-prompt", dest="voice_prompts", action="append", default=[])
    parser.add_argument("--voices", default=",".join(FIXED_VOICE_IDS))
    parser.add_argument("--text", default=DEFAULT_TEXT)
    parser.add_argument("--drift-text", default="我们将继续围绕事实依据和价值判断，稳定地推进本轮论证。")
    parser.add_argument("--drift-rounds", type=int, default=20)
    parser.add_argument("--chunk-characters", type=int, default=12)
    parser.add_argument("--delta-delay-ms", type=float, default=50)
    parser.add_argument("--sample-rate", type=int, default=16_000)
    parser.add_argument("--channels", type=int, default=1)
    parser.add_argument("--sample-width", type=int, default=2)
    parser.add_argument("--concurrency", type=int, action="append", default=[])
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--warmup-requests", type=int, default=1)
    parser.add_argument("--initial-buffer-ms", type=float, default=100)
    parser.add_argument("--cancel-after-first-pcm-ms", type=float, default=20)
    parser.add_argument("--timeout-seconds", type=float, default=120)
    parser.add_argument("--moss-release-timeout-seconds", type=float, default=2)
    parser.add_argument("--max-cer", type=float, default=0.02)
    parser.add_argument("--max-swallowed-rate", type=float, default=0.0)
    parser.add_argument("--max-repeated-or-extra-rate", type=float, default=0.0)
    parser.add_argument("--max-adjacent-repetitions", type=int, default=0)
    parser.add_argument("--max-first-pcm-ms", type=float, default=800)
    parser.add_argument("--max-first-pcm-hard-ms", type=float, default=3000)
    parser.add_argument("--max-first-non-silent-ms", type=float, default=1000)
    parser.add_argument("--max-leading-silence-ms", type=float, default=250)
    parser.add_argument("--max-stutters", type=int, default=0)
    parser.add_argument("--max-boundary-silence-ms", type=float, default=200)
    parser.add_argument("--max-chunk-gap-ms", type=float, default=200)
    parser.add_argument("--max-rms-range-db", type=float, default=3)
    parser.add_argument("--max-active-rms-stddev-db", type=float, default=6)
    parser.add_argument("--max-timbre-cosine-distance", type=float, default=0.08)
    parser.add_argument("--max-queue-delay-ms", type=float, default=500)
    parser.add_argument("--max-cancel-ms", type=float, default=200)
    parser.add_argument("--output-dir", type=Path, default=Path("tts-quality-gate-output"))
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    if not args.moss_endpoints:
        args.moss_endpoints = [
            value.strip()
            for value in os.getenv("TTS_QUALITY_MOSS_ENDPOINTS", "").split(",")
            if value.strip()
        ]
    concurrency_was_explicit = bool(args.concurrency)
    args.concurrency = args.concurrency or [1, 2, 3]
    args.single_endpoint_quality_only = concurrency_was_explicit and args.concurrency == [1]
    if (
        any(value not in {1, 2, 3} for value in args.concurrency)
        or (
            not args.single_endpoint_quality_only
            and (1 not in args.concurrency or 3 not in args.concurrency)
        )
    ):
        raise SystemExit(
            "--concurrency values must be 1, 2, or 3 and include both 1 and 3; "
            "explicit --concurrency 1 is allowed for single-endpoint quality validation only"
        )
    if args.rounds < 1 or args.drift_rounds < 1 or args.warmup_requests < 0:
        raise SystemExit("--rounds/--drift-rounds must be positive and --warmup-requests must be non-negative")
    if args.sample_width != 2 or args.channels < 1 or args.sample_rate < 8000:
        raise SystemExit("audio format must be PCM16 with positive channels and sample rate >=8000")
    if (
        args.max_repeated_or_extra_rate < 0
        or args.max_adjacent_repetitions < 0
        or args.max_first_pcm_hard_ms <= 0
        or args.max_first_non_silent_ms <= 0
        or args.max_leading_silence_ms < 0
        or args.max_active_rms_stddev_db < 0
        or not 0 <= args.max_timbre_cosine_distance <= 1
        or args.moss_release_timeout_seconds <= 0
    ):
        raise SystemExit("repetition, first-PCM and MOSS release limits must be non-negative/positive")
    return args


def main() -> None:
    raise SystemExit(asyncio.run(run(parse_args())))


if __name__ == "__main__":
    main()
