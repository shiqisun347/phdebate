#!/usr/bin/env python3
"""Benchmark Volcengine TTS V3 bidirectional text-in/audio-out streaming.

The API key is read only from VOLCENGINE_TTS_API_KEY. Reports never include
request headers, the key, or raw upstream error headers.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
import wave
from array import array
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import websockets

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "api"))

from app.services.voice_runtime.volcengine_protocol import (  # noqa: E402
    EventType,
    MessageType,
    cancel_session_frame,
    finish_connection_frame,
    finish_session_frame,
    parse_message,
    start_connection_frame,
    start_session_frame,
    task_request_frame,
)

DEFAULT_TEXTS = (
    "大家好，我是正方一辩。今天我方认为，人工智能时代仍然需要学习编程。",
    "对方辩友把工具效率等同于思维能力，却忽略了理解程序逻辑才能判断人工智能答案是否可靠。",
    "综上，学习编程的意义不是机械记忆语法，而是训练拆解问题、验证假设和构建系统的能力。",
)


@dataclass
class Record:
    ok: bool
    text: str
    connect_ms: float | None = None
    session_ms: float | None = None
    first_audio_ms: float | None = None
    first_non_silent_ms: float | None = None
    finish_input_ms: float | None = None
    audio_before_finish: bool | None = None
    total_ms: float | None = None
    audio_seconds: float | None = None
    rtf: float | None = None
    audio_chunks: int = 0
    audio_bytes: int = 0
    max_chunk_gap_ms: float | None = None
    playback_underruns_100ms: int = 0
    max_playback_underrun_ms: float = 0.0
    usage_characters: int | None = None
    canceled: bool = False
    cancel_ms: float | None = None
    error: str = ""


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * fraction)))
    return round(ordered[position], 3)


def simulate_playback(
    arrivals: list[float],
    durations: list[float],
    *,
    initial_buffer_ms: float = 100,
) -> tuple[int, float]:
    if not arrivals or len(arrivals) != len(durations):
        return 0, 0.0
    buffered = 0.0
    start_index = 0
    target = initial_buffer_ms / 1000
    for index, duration in enumerate(durations):
        buffered += duration
        start_index = index
        if buffered >= target:
            break
    play_end = arrivals[start_index] + buffered
    underruns: list[float] = []
    for arrival, duration in zip(arrivals[start_index + 1 :], durations[start_index + 1 :]):
        if arrival > play_end:
            underruns.append(arrival - play_end)
            play_end = arrival + duration
        else:
            play_end += duration
    return len(underruns), round(max(underruns, default=0.0) * 1000, 3)


async def wait_for_event(websocket: Any, expected: set[EventType]) -> Any:
    while True:
        message = parse_message(await websocket.recv())
        if message.message_type == MessageType.ERROR:
            raise RuntimeError(f"upstream_error:{message.error_code}")
        if message.event in {EventType.CONNECTION_FAILED, EventType.SESSION_FAILED}:
            payload = message.json_payload()
            code = payload.get("code") or payload.get("error_code") or "unknown"
            raise RuntimeError(f"upstream_failed:{code}")
        if message.event in expected:
            return message


async def run_once(args: argparse.Namespace, text: str, index: int) -> Record:
    api_key = os.environ.get("VOLCENGINE_TTS_API_KEY", "").strip()
    if not api_key:
        return Record(ok=False, text=text, error="VOLCENGINE_TTS_API_KEY is not set")
    record = Record(ok=False, text=text)
    started = time.perf_counter()
    first_text_at: float | None = None
    first_audio_at: float | None = None
    first_non_silent_at: float | None = None
    finish_input_at: float | None = None
    cancel_started_at: float | None = None
    previous_audio_at: float | None = None
    gaps: list[float] = []
    audio_arrivals: list[float] = []
    audio_durations: list[float] = []
    session_id = str(uuid.uuid4())
    connect_id = str(uuid.uuid4())
    audio = bytearray()
    request = {
        "req_params": {
            "model": args.model,
            "speaker": args.speaker,
            "audio_params": {
                "format": "pcm",
                "sample_rate": args.sample_rate,
                "speech_rate": args.speech_rate,
                "loudness_rate": args.loudness_rate,
                "enable_subtitle": False,
            },
            "additions": json.dumps(
                {
                    "disable_markdown_filter": True,
                    "disable_emoji_filter": True,
                    "explicit_language": "zh-cn",
                },
                ensure_ascii=False,
            ),
        }
    }
    headers = {
        "X-Api-Key": api_key,
        "X-Api-Resource-Id": args.resource_id,
        "X-Api-Connect-Id": connect_id,
        "X-Control-Require-Usage-Tokens-Return": "*",
    }
    try:
        async with websockets.connect(
            args.endpoint,
            additional_headers=headers,
            max_size=16 * 1024 * 1024,
            open_timeout=args.connect_timeout,
            close_timeout=2,
            ping_interval=20,
            ping_timeout=10,
        ) as websocket:
            connected = time.perf_counter()
            record.connect_ms = round((connected - started) * 1000, 3)
            await websocket.send(start_connection_frame())
            await asyncio.wait_for(
                wait_for_event(websocket, {EventType.CONNECTION_STARTED}),
                timeout=args.event_timeout,
            )
            await websocket.send(start_session_frame(session_id, request))
            await asyncio.wait_for(
                wait_for_event(websocket, {EventType.SESSION_STARTED}),
                timeout=args.event_timeout,
            )
            session_ready = time.perf_counter()
            record.session_ms = round((session_ready - started) * 1000, 3)

            async def sender() -> None:
                nonlocal cancel_started_at, finish_input_at, first_text_at
                for offset in range(0, len(text), args.chunk_characters):
                    chunk = text[offset : offset + args.chunk_characters]
                    if first_text_at is None:
                        first_text_at = time.perf_counter()
                    await websocket.send(task_request_frame(session_id, request, chunk))
                    if args.delta_delay_ms:
                        await asyncio.sleep(args.delta_delay_ms / 1000)
                if args.cancel_after_ms > 0:
                    await asyncio.sleep(args.cancel_after_ms / 1000)
                    cancel_started_at = time.perf_counter()
                    await websocket.send(cancel_session_frame(session_id))
                else:
                    finish_input_at = time.perf_counter()
                    await websocket.send(finish_session_frame(session_id))

            send_task = asyncio.create_task(sender())
            while True:
                message = parse_message(await asyncio.wait_for(websocket.recv(), timeout=args.job_timeout))
                if message.message_type == MessageType.ERROR:
                    raise RuntimeError(f"upstream_error:{message.error_code}")
                if message.event in {EventType.CONNECTION_FAILED, EventType.SESSION_FAILED}:
                    payload = message.json_payload()
                    code = payload.get("code") or payload.get("error_code") or "unknown"
                    raise RuntimeError(f"upstream_failed:{code}")
                if message.message_type == MessageType.AUDIO_ONLY_SERVER and message.payload:
                    now = time.perf_counter()
                    if first_audio_at is None:
                        first_audio_at = now
                    if first_non_silent_at is None:
                        samples = array("h")
                        samples.frombytes(message.payload[: len(message.payload) // 2 * 2])
                        if any(abs(sample) >= 64 for sample in samples):
                            first_non_silent_at = now
                    if previous_audio_at is not None:
                        gaps.append((now - previous_audio_at) * 1000)
                    previous_audio_at = now
                    audio_arrivals.append(now)
                    audio_durations.append(len(message.payload) / (args.sample_rate * 2))
                    audio.extend(message.payload)
                    record.audio_chunks += 1
                elif message.event in {EventType.USAGE_RESPONSE, EventType.SESSION_FINISHED}:
                    payload = message.json_payload()
                    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else payload
                    value = usage.get("text_words") if isinstance(usage, dict) else None
                    if isinstance(value, int):
                        record.usage_characters = value
                    if message.event == EventType.SESSION_FINISHED:
                        break
                elif message.event == EventType.SESSION_CANCELED:
                    record.canceled = True
                    if cancel_started_at is not None:
                        record.cancel_ms = round((time.perf_counter() - cancel_started_at) * 1000, 3)
                    break
            await send_task
            try:
                await websocket.send(finish_connection_frame())
            except Exception:
                pass

        completed = time.perf_counter()
        record.total_ms = round((completed - started) * 1000, 3)
        record.audio_bytes = len(audio)
        record.max_chunk_gap_ms = round(max(gaps), 3) if gaps else 0.0
        record.playback_underruns_100ms, record.max_playback_underrun_ms = simulate_playback(
            audio_arrivals, audio_durations
        )
        if first_audio_at is not None and first_text_at is not None:
            record.first_audio_ms = round((first_audio_at - first_text_at) * 1000, 3)
        if first_non_silent_at is not None and first_text_at is not None:
            record.first_non_silent_ms = round((first_non_silent_at - first_text_at) * 1000, 3)
        if finish_input_at is not None and first_text_at is not None:
            record.finish_input_ms = round((finish_input_at - first_text_at) * 1000, 3)
            record.audio_before_finish = first_audio_at is not None and first_audio_at < finish_input_at
        if audio:
            record.audio_seconds = round(len(audio) / (args.sample_rate * 2), 3)
            record.rtf = round((record.total_ms / 1000) / record.audio_seconds, 3)
            args.output_dir.mkdir(parents=True, exist_ok=True)
            target = args.output_dir / f"volcengine-{index:03d}.wav"
            with wave.open(str(target), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(args.sample_rate)
                wav.writeframes(audio)
        record.ok = record.canceled or bool(audio)
    except Exception as exc:
        record.error = type(exc).__name__
    return record


async def main_async(args: argparse.Namespace) -> int:
    tasks: list[asyncio.Task[Record]] = []
    semaphore = asyncio.Semaphore(args.concurrency)

    async def guarded(text: str, index: int) -> Record:
        async with semaphore:
            return await run_once(args, text, index)

    texts = (args.text,) if args.text else DEFAULT_TEXTS
    for index in range(args.rounds):
        tasks.append(asyncio.create_task(guarded(texts[index % len(texts)], index + 1)))
    records = await asyncio.gather(*tasks)
    successful = [record for record in records if record.ok]
    first_audio = [record.first_audio_ms for record in successful if record.first_audio_ms is not None]
    rtf = [record.rtf for record in successful if record.rtf is not None]
    gaps = [record.max_chunk_gap_ms for record in successful if record.max_chunk_gap_ms is not None]
    first_non_silent = [
        record.first_non_silent_ms for record in successful if record.first_non_silent_ms is not None
    ]
    bidirectional_passed = all(
        record.canceled or record.audio_before_finish is True for record in successful
    )
    summary = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "endpoint": args.endpoint,
        "resource_id": args.resource_id,
        "speaker": args.speaker,
        "model": args.model,
        "sample_rate": args.sample_rate,
        "rounds": args.rounds,
        "concurrency": args.concurrency,
        "success": len(successful),
        "audio_before_finish": sum(record.audio_before_finish is True for record in successful),
        "bidirectional_gate_passed": bidirectional_passed and len(successful) == len(records),
        "first_audio_ms": {
            "p50": percentile(first_audio, 0.50),
            "p95": percentile(first_audio, 0.95),
            "max": round(max(first_audio), 3) if first_audio else None,
        },
        "first_non_silent_ms": {
            "p50": percentile(first_non_silent, 0.50),
            "p95": percentile(first_non_silent, 0.95),
            "max": round(max(first_non_silent), 3) if first_non_silent else None,
        },
        "rtf": {
            "p50": percentile(rtf, 0.50),
            "p95": percentile(rtf, 0.95),
        },
        "chunk_gap_ms": {
            "p95": percentile(gaps, 0.95),
            "max": round(max(gaps), 3) if gaps else None,
        },
        "playback_100ms": {
            "underruns": sum(record.playback_underruns_100ms for record in successful),
            "max_underrun_ms": max(
                (record.max_playback_underrun_ms for record in successful), default=0.0
            ),
        },
        "records": [asdict(record) for record in records],
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "volcengine-tts-benchmark.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({key: value for key, value in summary.items() if key != "records"}, ensure_ascii=False))
    return 0 if len(successful) == len(records) else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", default="wss://openspeech.bytedance.com/api/v3/tts/bidirection")
    parser.add_argument("--resource-id", default="seed-tts-2.0")
    parser.add_argument("--speaker", default="zh_female_gaolengyujie_uranus_bigtts")
    parser.add_argument("--text", default="")
    parser.add_argument("--model", default="seed-tts-2.0-standard")
    parser.add_argument("--sample-rate", type=int, default=24000)
    parser.add_argument("--speech-rate", type=int, default=0)
    parser.add_argument("--loudness-rate", type=int, default=0)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--chunk-characters", type=int, default=12)
    parser.add_argument("--delta-delay-ms", type=float, default=50)
    parser.add_argument("--cancel-after-ms", type=float, default=0)
    parser.add_argument("--connect-timeout", type=float, default=10)
    parser.add_argument("--event-timeout", type=float, default=10)
    parser.add_argument("--job-timeout", type=float, default=60)
    parser.add_argument("--output-dir", type=Path, default=Path("/tmp/phdebate-volcengine-tts"))
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async(parse_args())))
