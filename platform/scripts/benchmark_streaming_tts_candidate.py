#!/usr/bin/env python3
"""Benchmark an OpenAI-compatible raw-PCM streaming TTS candidate.

This tool intentionally bypasses rooms, matches, Redis and production provider
configuration. It is for an independently launched loopback canary such as
vLLM-Omni MOSS-TTS-Nano/Realtime. It measures transport/model evidence only;
browser audible latency, CER and speaker drift require the later pipeline gate.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import math
import time
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx


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


def distribution(values: list[float]) -> dict[str, float | None]:
    return {
        "p50": round(percentile(values, 0.50), 3) if values else None,
        "p95": round(percentile(values, 0.95), 3) if values else None,
        "p99": round(percentile(values, 0.99), 3) if values else None,
        "max": round(max(values), 3) if values else None,
    }


def data_url(path: Path) -> str:
    mime = "audio/wav" if path.suffix.lower() == ".wav" else "application/octet-stream"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def parse_voice_refs(values: list[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        name, separator, raw_path = value.partition("=")
        if not separator or not name.strip() or not raw_path.strip():
            raise SystemExit("--voice-ref must use NAME=/absolute/path.wav")
        path = Path(raw_path).expanduser().resolve()
        if not path.is_file():
            raise SystemExit(f"voice reference does not exist: {path}")
        result[name.strip()] = path
    if not result:
        raise SystemExit("at least one --voice-ref is required")
    return result


def write_pcm_wav(path: Path, pcm: bytes, *, sample_rate: int, channels: int, sample_width: int) -> None:
    if len(pcm) % (channels * sample_width):
        raise RuntimeError("raw PCM length is not frame-aligned")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(sample_width)
        output.setframerate(sample_rate)
        output.writeframes(pcm)


async def run_request(
    client: httpx.AsyncClient,
    *,
    endpoint: str,
    model: str,
    text: str,
    voice_name: str,
    reference: str,
    output_path: Path,
    sample_rate: int,
    channels: int,
    sample_width: int,
    request_index: int,
) -> dict[str, Any]:
    payload = {
        "model": model,
        "input": text,
        "voice": "default",
        "ref_audio": reference,
        "response_format": "pcm",
        "stream_format": "audio",
        "stream": True,
    }
    started = time.perf_counter()
    first_pcm_at: float | None = None
    previous_chunk_at: float | None = None
    chunk_gaps_ms: list[float] = []
    chunks: list[bytes] = []
    status_code: int | None = None
    try:
        async with client.stream("POST", endpoint, json=payload) as response:
            status_code = response.status_code
            response.raise_for_status()
            async for raw in response.aiter_bytes():
                if not raw:
                    continue
                received = time.perf_counter()
                if first_pcm_at is None:
                    first_pcm_at = received
                if previous_chunk_at is not None:
                    chunk_gaps_ms.append((received - previous_chunk_at) * 1000)
                previous_chunk_at = received
                chunks.append(bytes(raw))
        completed = time.perf_counter()
        pcm = b"".join(chunks)
        if not pcm or first_pcm_at is None:
            raise RuntimeError("candidate returned no PCM")
        write_pcm_wav(
            output_path,
            pcm,
            sample_rate=sample_rate,
            channels=channels,
            sample_width=sample_width,
        )
        frame_bytes = channels * sample_width
        audio_duration = len(pcm) / max(1, sample_rate * frame_bytes)
        total_seconds = completed - started
        return {
            "request_index": request_index,
            "voice": voice_name,
            "status_code": status_code,
            "first_pcm_ms": round((first_pcm_at - started) * 1000, 3),
            "total_ms": round(total_seconds * 1000, 3),
            "audio_duration_seconds": round(audio_duration, 3),
            "rtf": round(total_seconds / max(audio_duration, 0.001), 4),
            "chunks": len(chunks),
            "pcm_bytes": len(pcm),
            "max_chunk_gap_ms": round(max(chunk_gaps_ms), 3) if chunk_gaps_ms else 0.0,
            "chunk_gap_p99_ms": round(percentile(chunk_gaps_ms, 0.99), 3) if chunk_gaps_ms else 0.0,
            "sha256": hashlib.sha256(pcm).hexdigest(),
            "wav": str(output_path),
        }
    except Exception as exc:
        return {
            "request_index": request_index,
            "voice": voice_name,
            "status_code": status_code,
            "error": f"{type(exc).__name__}: {exc}",
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        }


def summarize(records: list[dict[str, Any]], concurrency: int) -> dict[str, Any]:
    succeeded = [item for item in records if not item.get("error")]
    first_pcm = [float(item["first_pcm_ms"]) for item in succeeded]
    rtf = [float(item["rtf"]) for item in succeeded]
    gaps = [float(item["chunk_gap_p99_ms"]) for item in succeeded]
    summary = {
        "concurrency": concurrency,
        "requested": len(records),
        "succeeded": len(succeeded),
        "failed": len(records) - len(succeeded),
        "first_pcm_ms": distribution(first_pcm),
        "rtf": distribution(rtf),
        "per_request_chunk_gap_p99_ms": distribution(gaps),
    }
    summary["transport_gate_passed"] = bool(
        concurrency == 3
        and summary["failed"] == 0
        and summary["first_pcm_ms"]["p95"] is not None
        and summary["first_pcm_ms"]["p95"] <= 800
        and summary["rtf"]["p95"] is not None
        and summary["rtf"]["p95"] <= 0.65
        and summary["per_request_chunk_gap_p99_ms"]["max"] is not None
        and summary["per_request_chunk_gap_p99_ms"]["max"] <= 200
    )
    return summary


def markdown(document: dict[str, Any]) -> str:
    lines = [
        "# Streaming TTS candidate benchmark",
        "",
        f"- Generated: {document['generated_at']}",
        f"- Model: `{document['model']}`",
        f"- Audio: {document['sample_rate']} Hz / {document['channels']} channel(s) / PCM{document['sample_width'] * 8}",
        "- Evidence boundary: raw endpoint transport/model only; this does not prove browser audible latency, CER or speaker stability.",
        "",
        "| Concurrency | Success | First PCM P50/P95/max ms | RTF P50/P95/max | Per-request gap P99 max ms | Gate |",
        "|---:|---:|---:|---:|---:|---|",
    ]
    for scenario in document["scenarios"]:
        summary = scenario["summary"]
        first = summary["first_pcm_ms"]
        rtf = summary["rtf"]
        gap = summary["per_request_chunk_gap_p99_ms"]
        lines.append(
            f"| {summary['concurrency']} | {summary['succeeded']}/{summary['requested']} | "
            f"{first['p50']}/{first['p95']}/{first['max']} | "
            f"{rtf['p50']}/{rtf['p95']}/{rtf['max']} | {gap['max']} | "
            f"{'PASS' if summary['transport_gate_passed'] else 'NO-GO'} |"
        )
    lines.extend(
        [
            "",
            "Transport gate for three concurrent requests: first PCM P95 ≤800ms, "
            "RTF P95 ≤0.65, each request chunk-gap P99 ≤200ms, zero failures.",
        ]
    )
    return "\n".join(lines) + "\n"


async def run(args: argparse.Namespace) -> int:
    voice_paths = parse_voice_refs(args.voice_refs)
    encoded_refs = {name: data_url(path) for name, path in voice_paths.items()}
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    scenarios: list[dict[str, Any]] = []
    timeout = httpx.Timeout(args.timeout_seconds, connect=10, write=30, pool=10)
    async with httpx.AsyncClient(timeout=timeout) as client:
        for concurrency in args.concurrency:
            records: list[dict[str, Any]] = []
            voices = list(encoded_refs)
            for round_index in range(args.rounds):
                tasks = []
                for request_offset in range(concurrency):
                    request_index = round_index * concurrency + request_offset
                    voice_name = voices[request_index % len(voices)]
                    wav = output_dir / "samples" / f"c{concurrency}-r{round_index + 1}-{voice_name}-{request_offset + 1}.wav"
                    tasks.append(
                        run_request(
                            client,
                            endpoint=args.endpoint,
                            model=args.model,
                            text=args.text,
                            voice_name=voice_name,
                            reference=encoded_refs[voice_name],
                            output_path=wav,
                            sample_rate=args.sample_rate,
                            channels=args.channels,
                            sample_width=args.sample_width,
                            request_index=request_index,
                        )
                    )
                batch = await asyncio.gather(*tasks)
                records.extend(batch)
                for record in batch:
                    print(json.dumps(record, ensure_ascii=False), flush=True)
            scenarios.append(
                {
                    "concurrency": concurrency,
                    "summary": summarize(records, concurrency),
                    "records": records,
                }
            )
    document = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "endpoint_redacted": True,
        "model": args.model,
        "text": args.text,
        "voices": list(voice_paths),
        "sample_rate": args.sample_rate,
        "channels": args.channels,
        "sample_width": args.sample_width,
        "rounds": args.rounds,
        "scenarios": scenarios,
    }
    (output_dir / "streaming-tts-candidate.json").write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output_dir / "streaming-tts-candidate.md").write_text(markdown(document), encoding="utf-8")
    three_way = next((item["summary"] for item in scenarios if item["concurrency"] == 3), None)
    return 0 if three_way and three_way["transport_gate_passed"] else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--voice-ref", dest="voice_refs", action="append", default=[])
    parser.add_argument("--text", default="各位评委、同学，大家好。下面我将从事实、价值和长期影响三个方面展开论证。")
    parser.add_argument("--concurrency", type=int, action="append", default=[])
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--sample-rate", type=int, default=48_000)
    parser.add_argument("--channels", type=int, default=2)
    parser.add_argument("--sample-width", type=int, default=2)
    parser.add_argument("--timeout-seconds", type=float, default=120)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.concurrency = args.concurrency or [1, 3]
    if any(value < 1 or value > 32 for value in args.concurrency):
        raise SystemExit("--concurrency must be between 1 and 32")
    if args.rounds < 1:
        raise SystemExit("--rounds must be positive")
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
