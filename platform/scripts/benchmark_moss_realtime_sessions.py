#!/usr/bin/env python3
"""Benchmark native MOSS-TTS-Realtime incremental sessions.

This tool calls only dedicated candidate endpoints.  It does not create rooms,
touch Redis, or read production provider configuration.  WebSocket is the
formal transport; the legacy HTTP lifecycle remains an explicit diagnostic
fallback.  Each request records handshake latency, first PCM after the first
body delta, RTF, chunk gaps, cleanup acknowledgement and endpoint sharding.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import sys
import time
import uuid
import wave
from array import array
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import websockets

ROOT = Path(__file__).resolve().parents[1]
API_ROOT = ROOT / "apps" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from app.services.voice_runtime.text import SpeakableClauseAssembler  # noqa: E402


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


def simulate_continuous_playback(
    arrivals: list[float],
    durations: list[float],
    *,
    initial_buffer_ms: int,
) -> dict[str, float | int]:
    """Model a continuous browser audio clock fed by timestamped PCM chunks."""

    if not arrivals:
        return {
            "initial_buffer_ms": initial_buffer_ms,
            "startup_wait_ms": 0.0,
            "underrun_count": 0,
            "total_underrun_ms": 0.0,
            "max_underrun_ms": 0.0,
            "minimum_queue_lead_ms": 0.0,
        }
    if len(arrivals) != len(durations):
        raise ValueError("playback arrivals and durations must have equal length")

    target_seconds = initial_buffer_ms / 1000
    queued_duration = 0.0
    start_index = 0
    for index, duration in enumerate(durations):
        queued_duration += duration
        start_index = index
        if queued_duration >= target_seconds:
            break
    playback_started_at = arrivals[start_index]
    play_end = playback_started_at + queued_duration
    underrun_count = 0
    total_underrun = 0.0
    max_underrun = 0.0
    minimum_lead = queued_duration
    for arrival, duration in zip(arrivals[start_index + 1 :], durations[start_index + 1 :]):
        lead = play_end - arrival
        minimum_lead = min(minimum_lead, lead)
        if lead < 0:
            underrun = -lead
            underrun_count += 1
            total_underrun += underrun
            max_underrun = max(max_underrun, underrun)
            play_end = arrival + duration
        else:
            play_end += duration
    return {
        "initial_buffer_ms": initial_buffer_ms,
        "startup_wait_ms": round((playback_started_at - arrivals[0]) * 1000, 3),
        "underrun_count": underrun_count,
        "total_underrun_ms": round(total_underrun * 1000, 3),
        "max_underrun_ms": round(max_underrun * 1000, 3),
        "minimum_queue_lead_ms": round(minimum_lead * 1000, 3),
    }


def playback_profile(arrivals: list[float], durations: list[float]) -> dict[str, dict[str, float | int]]:
    return {
        str(buffer_ms): simulate_continuous_playback(
            arrivals,
            durations,
            initial_buffer_ms=buffer_ms,
        )
        for buffer_ms in (80, 100, 120, 800, 1_000, 1_200, 1_400, 1_600)
    }


def text_deltas(text: str, chunk_characters: int) -> list[str]:
    step = max(1, int(chunk_characters))
    return [text[index : index + step] for index in range(0, len(text), step)]


def production_text_deltas(text: str, source_delta_characters: int) -> list[str]:
    """Feed simulated Agent deltas through the production clause assembler."""

    assembler = SpeakableClauseAssembler()
    chunks: list[str] = []
    for delta in text_deltas(text, source_delta_characters):
        chunks.extend(assembler.feed(delta))
    chunks.extend(assembler.finish())
    return chunks


def pcm_chunk_is_non_silent(pcm: bytes, *, threshold: int = 128, minimum_samples: int = 480) -> bool:
    if len(pcm) < minimum_samples * 2 or len(pcm) % 2:
        return False
    samples = array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    return sum(abs(sample) >= threshold for sample in samples) >= minimum_samples // 4


def normalize_endpoint(endpoint: str) -> str:
    return endpoint.strip().rstrip("/")


def websocket_endpoint(endpoint: str) -> str:
    normalized = normalize_endpoint(endpoint)
    if normalized.startswith("https://"):
        normalized = f"wss://{normalized.removeprefix('https://')}"
    elif normalized.startswith("http://"):
        normalized = f"ws://{normalized.removeprefix('http://')}"
    elif not normalized.startswith(("ws://", "wss://")):
        raise ValueError("endpoint must use http, https, ws, or wss")
    if normalized.endswith("/tts/session/ws"):
        return normalized
    return f"{normalized}/tts/session/ws"


def health_endpoint(endpoint: str) -> str:
    normalized = normalize_endpoint(endpoint)
    if normalized.startswith("wss://"):
        normalized = f"https://{normalized.removeprefix('wss://')}"
    elif normalized.startswith("ws://"):
        normalized = f"http://{normalized.removeprefix('ws://')}"
    normalized = normalized.removesuffix("/tts/session/ws")
    return f"{normalized}/health/ready"


def parse_voice_prompts(values: list[str]) -> dict[str, str]:
    prompts: dict[str, str] = {}
    for value in values:
        voice, separator, prompt = value.partition("=")
        if not separator or not voice.strip() or not prompt.strip():
            raise SystemExit("--voice-prompt must use VOICE=remote-prompt.wav")
        prompts[voice.strip()] = prompt.strip()
    if not prompts:
        raise SystemExit("at least one --voice-prompt is required")
    return prompts


def write_wav(path: Path, pcm: bytes, sample_rate: int) -> None:
    if not pcm or len(pcm) % 2:
        raise RuntimeError("candidate returned empty or unaligned PCM16")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(pcm)


async def post_json(
    client: httpx.AsyncClient,
    url: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    response = await client.post(url, json=payload)
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict) or body.get("ok") is False:
        raise RuntimeError(f"invalid control response from {url}")
    return body


async def confirm_release(
    *,
    endpoint: str,
    api_key: str,
    timeout_seconds: float = 5,
) -> bool:
    headers = {"X-MOSS-Gateway-Key": api_key} if api_key else {}
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds, headers=headers) as client:
            response = await client.get(health_endpoint(endpoint))
        body = response.json() if response.is_success else {}
        return bool(
            response.is_success
            and isinstance(body, dict)
            and body.get("ok") is True
            and int(body.get("active") or 0) == 0
            and int(body.get("orphan_count") or 0) == 0
        )
    except (httpx.HTTPError, TypeError, ValueError):
        return False


async def run_http_request(
    *,
    endpoint: str,
    voice: str,
    prompt_audio: str,
    text: str,
    chunk_characters: int,
    delta_delay_seconds: float,
    finish_delay_seconds: float,
    output_path: Path,
    request_index: int,
    timeout_seconds: float,
    api_key: str,
) -> dict[str, Any]:
    session_id = f"benchmark-{request_index}-{uuid.uuid4().hex}"
    deltas = production_text_deltas(text, chunk_characters)
    started_at = time.perf_counter()
    first_pcm_at: float | None = None
    first_non_silent_pcm_at: float | None = None
    final_push_at: float | None = None
    previous_chunk_at: float | None = None
    chunk_gaps_ms: list[float] = []
    chunks: list[bytes] = []
    chunk_arrivals: list[float] = []
    chunk_durations: list[float] = []
    sample_rate = 24_000
    close_ack = False
    release_confirmed = False
    first_text_sent_at: float | None = None
    handshake_at: float | None = None
    source_wait_seconds = 0.0
    stream_task: asyncio.Task[None] | None = None
    stream_connected = asyncio.Event()
    timeout = httpx.Timeout(timeout_seconds, connect=10, write=30, pool=10)

    headers = {"X-MOSS-Gateway-Key": api_key} if api_key else {}
    async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:

        async def read_audio() -> None:
            nonlocal first_pcm_at, first_non_silent_pcm_at, previous_chunk_at, sample_rate
            async with client.stream("GET", f"{endpoint}/tts/session/{session_id}/audio", timeout=None) as response:
                response.raise_for_status()
                sample_rate = int(response.headers.get("X-Audio-Sample-Rate", "24000"))
                if response.headers.get("X-Audio-Codec", "pcm_s16le").lower() != "pcm_s16le":
                    raise RuntimeError("candidate did not return pcm_s16le")
                if int(response.headers.get("X-Audio-Channels", "1")) != 1:
                    raise RuntimeError("candidate did not return mono audio")
                stream_connected.set()
                async for chunk in response.aiter_bytes():
                    if not chunk:
                        continue
                    received_at = time.perf_counter()
                    if first_pcm_at is None:
                        first_pcm_at = received_at
                    if first_non_silent_pcm_at is None and pcm_chunk_is_non_silent(chunk):
                        first_non_silent_pcm_at = received_at
                    if previous_chunk_at is not None:
                        chunk_gaps_ms.append((received_at - previous_chunk_at) * 1000)
                    previous_chunk_at = received_at
                    chunks.append(bytes(chunk))
                    chunk_arrivals.append(received_at)
                    chunk_durations.append(len(chunk) / max(1, sample_rate * 2))

        try:
            if not deltas:
                raise RuntimeError("text is empty")
            await post_json(
                client,
                f"{endpoint}/tts/session/start",
                {
                    "session_id": session_id,
                    "assistant_text": "",
                    "user_text": None,
                    "prompt_audio": prompt_audio,
                    "user_audio": None,
                    "new_turn": True,
                },
            )
            stream_task = asyncio.create_task(read_audio())
            await asyncio.wait_for(stream_connected.wait(), timeout=10)
            handshake_at = time.perf_counter()
            first_text_sent_at = time.perf_counter()
            await post_json(
                client,
                f"{endpoint}/tts/session/push",
                {"session_id": session_id, "text": deltas[0], "is_final": False},
            )
            for delta in deltas[1:]:
                await post_json(
                    client,
                    f"{endpoint}/tts/session/push",
                    {"session_id": session_id, "text": delta, "is_final": False},
                )
                if delta_delay_seconds > 0:
                    wait_started = time.perf_counter()
                    await asyncio.sleep(delta_delay_seconds)
                    source_wait_seconds += time.perf_counter() - wait_started
            if finish_delay_seconds > 0:
                wait_started = time.perf_counter()
                await asyncio.sleep(finish_delay_seconds)
                source_wait_seconds += time.perf_counter() - wait_started
            final_push_at = time.perf_counter()
            await post_json(
                client,
                f"{endpoint}/tts/session/push",
                {"session_id": session_id, "text": "", "is_final": True},
            )
            await asyncio.wait_for(stream_task, timeout=timeout_seconds)
            completed_at = time.perf_counter()
            pcm = b"".join(chunks)
            if first_pcm_at is None:
                raise RuntimeError("candidate returned no PCM")
            if first_non_silent_pcm_at is None:
                raise RuntimeError("candidate returned no non-silent PCM")
            write_wav(output_path, pcm, sample_rate)
            duration_seconds = len(pcm) / max(1, sample_rate * 2)
            total_seconds = completed_at - started_at
            active_seconds = max(0.0, total_seconds - source_wait_seconds)
            result = {
                "request_index": request_index,
                "endpoint_index": None,
                "voice": voice,
                "transport": "http",
                "handshake_ms": round(((handshake_at or first_text_sent_at) - started_at) * 1000, 3),
                "first_pcm_ms": round((first_pcm_at - first_text_sent_at) * 1000, 3),
                "first_non_silent_pcm_ms": round((first_non_silent_pcm_at - first_text_sent_at) * 1000, 3),
                "first_pcm_from_request_start_ms": round((first_pcm_at - started_at) * 1000, 3),
                "first_pcm_before_final": bool(first_pcm_at < final_push_at),
                "total_ms": round(total_seconds * 1000, 3),
                "audio_duration_seconds": round(duration_seconds, 3),
                "rtf": round(total_seconds / max(0.001, duration_seconds), 4),
                "active_rtf": round(active_seconds / max(0.001, duration_seconds), 4),
                "source_wait_ms": round(source_wait_seconds * 1000, 3),
                "chunks": len(chunks),
                "pcm_bytes": len(pcm),
                "max_chunk_gap_ms": round(max(chunk_gaps_ms), 3) if chunk_gaps_ms else 0.0,
                "chunk_gap_p99_ms": round(percentile(chunk_gaps_ms, 0.99), 3) if chunk_gaps_ms else 0.0,
                "continuous_playback": playback_profile(chunk_arrivals, chunk_durations),
                "chunk_timeline": [
                    {
                        "arrival_ms": round((arrival - first_text_sent_at) * 1000, 3),
                        "duration_ms": round(duration * 1000, 3),
                    }
                    for arrival, duration in zip(chunk_arrivals, chunk_durations)
                ],
                "sha256": hashlib.sha256(pcm).hexdigest(),
                "wav": str(output_path),
            }
        except Exception as exc:
            result = {
                "request_index": request_index,
                "voice": voice,
                "transport": "http",
                "error": f"{type(exc).__name__}: {exc}",
                "elapsed_ms": round((time.perf_counter() - started_at) * 1000, 3),
            }
        finally:
            try:
                response = await asyncio.wait_for(
                    client.post(
                        f"{endpoint}/tts/session/close",
                        json={"session_id": session_id},
                    ),
                    timeout=5,
                )
                body = response.json() if response.is_success else {}
                close_ack = bool(response.is_success and isinstance(body, dict) and body.get("released") is True)
                if close_ack:
                    release_confirmed = await confirm_release(
                        endpoint=endpoint,
                        api_key=api_key,
                    )
            except Exception:
                close_ack = False
            if stream_task is not None and not stream_task.done():
                stream_task.cancel()
                await asyncio.gather(stream_task, return_exceptions=True)
        result["close_ack"] = close_ack
        result["release_confirmed"] = release_confirmed
        return result


async def run_websocket_request(
    *,
    endpoint: str,
    voice: str,
    prompt_audio: str,
    text: str,
    chunk_characters: int,
    delta_delay_seconds: float,
    finish_delay_seconds: float,
    output_path: Path,
    request_index: int,
    timeout_seconds: float,
    api_key: str,
    canary_stage_observability: bool = False,
) -> dict[str, Any]:
    del prompt_audio  # WS uses the fixed voice id, never a caller-controlled path.
    session_id = f"benchmark-{request_index}-{uuid.uuid4().hex}"
    deltas = production_text_deltas(text, chunk_characters)
    started_at = time.perf_counter()
    connected_at: float | None = None
    ready_at: float | None = None
    first_text_sent_at: float | None = None
    first_pcm_at: float | None = None
    first_non_silent_pcm_at: float | None = None
    final_push_at: float | None = None
    previous_chunk_at: float | None = None
    chunk_gaps_ms: list[float] = []
    chunks: list[bytes] = []
    chunk_arrivals: list[float] = []
    chunk_durations: list[float] = []
    chunk_observations: list[dict[str, Any] | None] = []
    stage_observations: list[dict[str, Any]] = []
    pending_chunk_observation: dict[str, Any] | None = None
    sample_rate = 24_000
    audio_ended = False
    close_ack = False
    release_confirmed = False
    source_wait_seconds = 0.0
    headers = {"X-MOSS-Gateway-Key": api_key} if api_key else {}

    def record_pcm(payload: bytes) -> None:
        nonlocal first_pcm_at, first_non_silent_pcm_at, previous_chunk_at, pending_chunk_observation
        if not payload:
            return
        if first_text_sent_at is None:
            raise RuntimeError("candidate returned PCM before the first body delta")
        if audio_ended:
            raise RuntimeError("candidate returned PCM after audio_end")
        received_at = time.perf_counter()
        if first_pcm_at is None:
            first_pcm_at = received_at
        if first_non_silent_pcm_at is None and pcm_chunk_is_non_silent(payload):
            first_non_silent_pcm_at = received_at
        if previous_chunk_at is not None:
            chunk_gaps_ms.append((received_at - previous_chunk_at) * 1000)
        previous_chunk_at = received_at
        chunks.append(bytes(payload))
        chunk_arrivals.append(received_at)
        chunk_durations.append(len(payload) / max(1, sample_rate * 2))
        if canary_stage_observability and pending_chunk_observation is None:
            raise RuntimeError("candidate omitted canary metadata before PCM")
        chunk_observations.append(pending_chunk_observation)
        pending_chunk_observation = None

    def record_canary(payload: dict[str, Any]) -> None:
        nonlocal pending_chunk_observation
        if not canary_stage_observability:
            raise RuntimeError("candidate emitted unsolicited canary metadata")
        event_type = payload.get("type")
        if event_type == "canary.stage":
            allowed = {"prefill", "talker_step", "decoder_yield", "flush"}
            if payload.get("stage") not in allowed:
                raise RuntimeError("candidate returned an invalid canary stage")
            stage_observations.append(
                {
                    "stage": payload["stage"],
                    "stage_index": int(payload["stage_index"]),
                    "duration_ms": float(payload["duration_ms"]),
                    "turn_elapsed_ms": float(payload["turn_elapsed_ms"]),
                    "source_stage": payload.get("source_stage"),
                    "source_stage_index": payload.get("source_stage_index"),
                }
            )
            return
        if event_type != "canary.pcm":
            raise RuntimeError("candidate returned an unknown canary event")
        if pending_chunk_observation is not None:
            raise RuntimeError("candidate returned duplicate canary PCM metadata")
        chunk_index = int(payload["chunk_index"])
        if chunk_index != len(chunks):
            raise RuntimeError("candidate returned an out-of-order canary chunk index")
        pending_chunk_observation = {
            "chunk_index": chunk_index,
            "source_stage": str(payload["source_stage"]),
            "source_stage_index": int(payload["source_stage_index"]),
            "source_stage_duration_ms": float(payload["source_stage_duration_ms"]),
            "decoder_stage_index": int(payload["decoder_stage_index"]),
            "decoder_yield_ms": float(payload["decoder_yield_ms"]),
            "turn_elapsed_ms": float(payload["turn_elapsed_ms"]),
        }

    async def receive_control(websocket: Any) -> dict[str, Any]:
        """The only recv site: demux binary PCM and return the next JSON control."""

        while True:
            frame = await websocket.recv()
            if isinstance(frame, bytes):
                record_pcm(frame)
                continue
            try:
                payload = json.loads(frame)
            except (json.JSONDecodeError, TypeError) as exc:
                raise RuntimeError("candidate returned invalid WS control JSON") from exc
            if not isinstance(payload, dict):
                raise RuntimeError("candidate returned a non-object WS control")
            if payload.get("session_id") not in {None, session_id}:
                raise RuntimeError("candidate returned a mismatched session_id")
            if payload.get("type") == "error":
                raise RuntimeError(f"candidate WS error: {payload.get('code') or 'unknown'}")
            if payload.get("type") in {"canary.stage", "canary.pcm"}:
                record_canary(payload)
                continue
            return payload

    async def receive_expected(websocket: Any, expected_type: str, *, seq: int | None = None) -> dict[str, Any]:
        payload = await receive_control(websocket)
        if payload.get("type") != expected_type:
            raise RuntimeError(f"expected WS {expected_type}, received {payload.get('type')!r}")
        if seq is not None and payload.get("seq") != seq:
            raise RuntimeError(f"expected WS ack seq {seq}, received {payload.get('seq')!r}")
        return payload

    if not deltas:
        return {
            "request_index": request_index,
            "voice": voice,
            "transport": "websocket",
            "error": "RuntimeError: text is empty",
            "elapsed_ms": round((time.perf_counter() - started_at) * 1000, 3),
            "close_ack": False,
            "release_confirmed": False,
        }

    try:
        connection = websockets.connect(
            websocket_endpoint(endpoint),
            additional_headers=headers,
            open_timeout=min(10, timeout_seconds),
            close_timeout=5,
            ping_interval=20,
            ping_timeout=20,
            compression=None,
            max_size=None,
        )
        async with connection as websocket:
            connected_at = time.perf_counter()
            try:
                await websocket.send(
                    json.dumps(
                        {
                            "type": "start",
                            "seq": 0,
                            "session_id": session_id,
                            "voice": voice,
                            "user_text": None,
                            "canary_observe": canary_stage_observability,
                        },
                        ensure_ascii=False,
                    )
                )
                ready = await asyncio.wait_for(receive_expected(websocket, "ready"), timeout=timeout_seconds)
                ready_at = time.perf_counter()
                audio = ready.get("audio")
                if ready.get("voice") != voice or ready.get("next_seq") != 1 or not isinstance(audio, dict):
                    raise RuntimeError("candidate returned invalid WS ready metadata")
                if canary_stage_observability and ready.get("canary_observe") is not True:
                    raise RuntimeError("candidate did not enable requested canary observability")
                if audio.get("codec") != "pcm_s16le" or int(audio.get("channels") or 0) != 1:
                    raise RuntimeError("candidate did not return mono pcm_s16le")
                sample_rate = int(audio.get("sample_rate") or 0)
                if not 8_000 <= sample_rate <= 48_000:
                    raise RuntimeError("candidate returned invalid sample rate")

                sequence = 1
                for index, delta in enumerate(deltas):
                    if index == 0:
                        first_text_sent_at = time.perf_counter()
                    await websocket.send(
                        json.dumps(
                            {"type": "text_delta", "seq": sequence, "text": delta},
                            ensure_ascii=False,
                        )
                    )
                    await asyncio.wait_for(
                        receive_expected(websocket, "ack", seq=sequence),
                        timeout=timeout_seconds,
                    )
                    sequence += 1
                    if index > 0 and delta_delay_seconds > 0:
                        wait_started = time.perf_counter()
                        await asyncio.sleep(delta_delay_seconds)
                        source_wait_seconds += time.perf_counter() - wait_started
                if finish_delay_seconds > 0:
                    wait_started = time.perf_counter()
                    await asyncio.sleep(finish_delay_seconds)
                    source_wait_seconds += time.perf_counter() - wait_started
                final_push_at = time.perf_counter()
                await websocket.send(json.dumps({"type": "final", "seq": sequence}))
                await asyncio.wait_for(
                    receive_expected(websocket, "ack", seq=sequence),
                    timeout=timeout_seconds,
                )
                await asyncio.wait_for(receive_expected(websocket, "audio_end"), timeout=timeout_seconds)
                audio_ended = True
                released = await asyncio.wait_for(
                    receive_expected(websocket, "released"),
                    timeout=timeout_seconds,
                )
                close_ack = bool(released.get("released") is True and released.get("status") == "closed")
                if not close_ack:
                    raise RuntimeError("candidate returned invalid WS release acknowledgement")
            except BaseException:
                if not close_ack:
                    try:
                        await websocket.send(json.dumps({"type": "abort", "reason": "benchmark_failure"}))
                        while True:
                            control = await asyncio.wait_for(receive_control(websocket), timeout=5)
                            if control.get("type") == "audio_reset":
                                chunks.clear()
                                pending_chunk_observation = None
                                continue
                            if control.get("type") == "released":
                                close_ack = bool(control.get("released") is True)
                                break
                    except Exception:
                        close_ack = False
                raise

        completed_at = time.perf_counter()
        if first_text_sent_at is None or final_push_at is None:
            raise RuntimeError("candidate did not accept the first text delta")
        if first_pcm_at is None:
            raise RuntimeError("candidate returned no PCM")
        if first_non_silent_pcm_at is None:
            raise RuntimeError("candidate returned no non-silent PCM")
        if pending_chunk_observation is not None:
            raise RuntimeError("candidate returned canary PCM metadata without a matching PCM frame")
        pcm = b"".join(chunks)
        write_wav(output_path, pcm, sample_rate)
        duration_seconds = len(pcm) / max(1, sample_rate * 2)
        total_seconds = completed_at - first_text_sent_at
        active_seconds = max(0.0, total_seconds - source_wait_seconds)
        release_confirmed = await confirm_release(endpoint=endpoint, api_key=api_key)
        result = {
            "request_index": request_index,
            "endpoint_index": None,
            "voice": voice,
            "transport": "websocket",
            "transport_connect_ms": round(((connected_at or started_at) - started_at) * 1000, 3),
            "handshake_ms": round(((ready_at or started_at) - started_at) * 1000, 3),
            "first_pcm_ms": round((first_pcm_at - first_text_sent_at) * 1000, 3),
            "first_non_silent_pcm_ms": round((first_non_silent_pcm_at - first_text_sent_at) * 1000, 3),
            "first_pcm_from_request_start_ms": round((first_pcm_at - started_at) * 1000, 3),
            "first_pcm_before_final": bool(first_pcm_at < final_push_at),
            "total_ms": round(total_seconds * 1000, 3),
            "audio_duration_seconds": round(duration_seconds, 3),
            "rtf": round(total_seconds / max(0.001, duration_seconds), 4),
            "active_rtf": round(active_seconds / max(0.001, duration_seconds), 4),
            "source_wait_ms": round(source_wait_seconds * 1000, 3),
            "chunks": len(chunks),
            "pcm_bytes": len(pcm),
            "max_chunk_gap_ms": round(max(chunk_gaps_ms), 3) if chunk_gaps_ms else 0.0,
            "chunk_gap_p99_ms": round(percentile(chunk_gaps_ms, 0.99), 3) if chunk_gaps_ms else 0.0,
            "continuous_playback": playback_profile(chunk_arrivals, chunk_durations),
            "chunk_timeline": [
                {
                    "arrival_ms": round((arrival - first_text_sent_at) * 1000, 3),
                    "duration_ms": round(duration * 1000, 3),
                    **({"native_stage": observation} if observation is not None else {}),
                }
                for arrival, duration, observation in zip(
                    chunk_arrivals,
                    chunk_durations,
                    chunk_observations,
                )
            ],
            "canary_stage_observability": canary_stage_observability,
            "native_stage_timings": stage_observations,
            "native_stage_summary_ms": {
                stage: distribution(
                    [
                        float(item["duration_ms"])
                        for item in stage_observations
                        if item["stage"] == stage
                    ]
                )
                for stage in ("prefill", "talker_step", "decoder_yield", "flush")
            },
            "sha256": hashlib.sha256(pcm).hexdigest(),
            "wav": str(output_path),
        }
    except Exception as exc:
        result = {
            "request_index": request_index,
            "voice": voice,
            "transport": "websocket",
            "error": f"{type(exc).__name__}: {exc}",
            "elapsed_ms": round((time.perf_counter() - started_at) * 1000, 3),
        }
    result["close_ack"] = close_ack
    result["release_confirmed"] = release_confirmed
    return result


async def run_request(*, transport: str = "websocket", **kwargs: Any) -> dict[str, Any]:
    if transport == "websocket":
        return await run_websocket_request(**kwargs)
    if transport == "http":
        kwargs.pop("canary_stage_observability", None)
        return await run_http_request(**kwargs)
    raise ValueError(f"unsupported transport: {transport}")


def summarize(records: list[dict[str, Any]], concurrency: int) -> dict[str, Any]:
    succeeded = [record for record in records if not record.get("error")]
    first_pcm = [float(record["first_pcm_ms"]) for record in succeeded]
    first_non_silent = [float(record["first_non_silent_pcm_ms"]) for record in succeeded]
    rtf = [float(record["rtf"]) for record in succeeded]
    active_rtf = [float(record.get("active_rtf", record["rtf"])) for record in succeeded]
    gaps = [float(record["chunk_gap_p99_ms"]) for record in succeeded]
    playback_1200 = [
        record.get("continuous_playback", {}).get("1200", {})
        for record in succeeded
    ]
    handshakes = [float(record.get("handshake_ms", 0)) for record in succeeded]
    summary = {
        "concurrency": concurrency,
        "requested": len(records),
        "succeeded": len(succeeded),
        "failed": len(records) - len(succeeded),
        "close_acknowledged": sum(bool(record.get("close_ack")) for record in records),
        "release_confirmed": sum(bool(record.get("release_confirmed")) for record in records),
        "first_pcm_before_final": sum(bool(record.get("first_pcm_before_final")) for record in records),
        "first_pcm_ms": distribution(first_pcm),
        "first_non_silent_pcm_ms": distribution(first_non_silent),
        "handshake_ms": distribution(handshakes),
        "rtf": distribution(rtf),
        "active_rtf": distribution(active_rtf),
        "per_request_chunk_gap_p99_ms": distribution(gaps),
        "playback_1200ms": {
            "requests_with_underrun": sum(int(item.get("underrun_count", 0)) > 0 for item in playback_1200),
            "underrun_count": sum(int(item.get("underrun_count", 0)) for item in playback_1200),
            "max_underrun_ms": round(max((float(item.get("max_underrun_ms", 0)) for item in playback_1200), default=0.0), 3),
            "minimum_queue_lead_ms": round(min((float(item.get("minimum_queue_lead_ms", 0)) for item in playback_1200), default=0.0), 3),
        },
    }
    summary["lifecycle_gate_passed"] = bool(
        summary["failed"] == 0
        and summary["close_acknowledged"] == summary["requested"]
        and summary["release_confirmed"] == summary["requested"]
        and summary["first_pcm_before_final"] == summary["requested"]
        and all(float(record.get("first_non_silent_pcm_ms", math.inf)) < 3_000 for record in succeeded)
        and summary["playback_1200ms"]["underrun_count"] == 0
    )
    summary["quality_thresholds_passed"] = bool(
        summary["lifecycle_gate_passed"]
        and summary["first_pcm_ms"]["p95"] is not None
        and summary["active_rtf"]["p95"] is not None
        and summary["active_rtf"]["p95"] < 1.0
        and summary["playback_1200ms"]["underrun_count"] == 0
    )
    summary["continuous_playback_gate_passed"] = bool(
        summary["failed"] == 0
        and summary["close_acknowledged"] == summary["requested"]
        and summary["release_confirmed"] == summary["requested"]
        and summary["first_non_silent_pcm_ms"]["p95"] is not None
        and summary["first_non_silent_pcm_ms"]["p95"] <= 3_000
        and summary["playback_1200ms"]["underrun_count"] == 0
    )
    summary["transport_gate_passed"] = bool(concurrency == 3 and summary["quality_thresholds_passed"])
    summary["release_gate_passed"] = bool(
        summary["quality_thresholds_passed"] and summary["requested"] >= concurrency * 20
    )
    return summary


def markdown(document: dict[str, Any]) -> str:
    transport = document["transport"]
    protocol = (
        "persistent WS start -> ready.audio -> sequenced text_delta/ack -> final/ack -> "
        "tail PCM -> audio_end -> released"
        if transport == "websocket"
        else "diagnostic HTTP start -> audio stream -> incremental push -> final -> close"
    )
    lines = [
        "# MOSS-TTS-Realtime native session benchmark",
        "",
        f"- Generated: {document['generated_at']}",
        f"- Candidate endpoints: {document['endpoint_count']} (addresses redacted)",
        f"- Transport: {transport}",
        f"- Protocol: {protocol}",
        "- First PCM origin: first body text_delta/push send time (handshake is reported separately).",
        "- Evidence boundary: transport/model only; CER, MOS and speaker drift remain separate gates.",
        "",
        "| Concurrency | Success | Handshake P50/P95/max ms | Non-silent PCM P50/P95/max ms | "
        "RTF/active P95 | Gap P99 max ms | 1200ms underruns/max ms | Lifecycle | Release gate |",
        "|---:|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    for scenario in document["scenarios"]:
        summary = scenario["summary"]
        first = summary["first_non_silent_pcm_ms"]
        handshake = summary["handshake_ms"]
        rtf = summary["rtf"]
        gap = summary["per_request_chunk_gap_p99_ms"]
        playback = summary["playback_1200ms"]
        lines.append(
            f"| {summary['concurrency']} | {summary['succeeded']}/{summary['requested']} | "
            f"{handshake['p50']}/{handshake['p95']}/{handshake['max']} | "
            f"{first['p50']}/{first['p95']}/{first['max']} | "
            f"{rtf['p95']}/{summary['active_rtf']['p95']} | {gap['max']} | "
            f"{playback['underrun_count']}/{playback['max_underrun_ms']} | "
            f"{'PASS' if summary['lifecycle_gate_passed'] else 'FAIL'} | "
            f"{'PASS' if summary['release_gate_passed'] else 'NO-GO'} |"
        )
    lines.extend(
        [
            "",
            "Release gate: c1/c2/c3 each contain 20 batches; every request produces non-silent PCM before "
            "final, the 1200ms continuous playout model has zero underruns, active RTF P95 is below 1.0, "
            "and every single-session request receives a release ACK. Browser first sound remains a separate "
            "end-to-end gate capped at 3000ms from the first readable Agent delta.",
        ]
    )
    return "\n".join(lines) + "\n"


async def run(args: argparse.Namespace) -> int:
    endpoints = [normalize_endpoint(endpoint) for endpoint in args.endpoint]
    prompts = parse_voice_prompts(args.voice_prompts)
    voices = list(prompts)
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    scenarios: list[dict[str, Any]] = []
    for concurrency in args.concurrency:
        records: list[dict[str, Any]] = []
        for round_index in range(args.rounds):
            tasks = []
            for offset in range(concurrency):
                request_index = round_index * concurrency + offset
                endpoint_index = request_index % len(endpoints)
                voice = voices[request_index % len(voices)]
                output = output_dir / "samples" / f"c{concurrency}-r{round_index + 1}-{voice}-{offset + 1}.wav"
                tasks.append(
                    run_request(
                        endpoint=endpoints[endpoint_index],
                        voice=voice,
                        prompt_audio=prompts[voice],
                        text=args.text,
                        chunk_characters=args.chunk_characters,
                        delta_delay_seconds=args.delta_delay_seconds,
                        finish_delay_seconds=args.finish_delay_seconds,
                        output_path=output,
                        request_index=request_index,
                        timeout_seconds=args.timeout_seconds,
                        api_key=args.api_key,
                        transport=args.transport,
                        canary_stage_observability=args.canary_stage_observability,
                    )
                )
            batch = await asyncio.gather(*tasks)
            for offset, record in enumerate(batch):
                record["endpoint_index"] = (round_index * concurrency + offset) % len(endpoints)
                print(json.dumps(record, ensure_ascii=False), flush=True)
            records.extend(batch)
        scenarios.append({"concurrency": concurrency, "summary": summarize(records, concurrency), "records": records})

    document = {
        "schema_version": 2,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "endpoint_count": len(endpoints),
        "endpoints_redacted": True,
        "transport": args.transport,
        "voices": voices,
        "text_characters": len(args.text),
        "text_sha256": hashlib.sha256(args.text.encode()).hexdigest(),
        "chunk_characters": args.chunk_characters,
        "delta_delay_seconds": args.delta_delay_seconds,
        "finish_delay_seconds": args.finish_delay_seconds,
        "rounds": args.rounds,
        "canary_stage_observability": args.canary_stage_observability,
        "scenarios": scenarios,
    }
    (output_dir / "moss-realtime-session-benchmark.json").write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output_dir / "moss-realtime-session-benchmark.md").write_text(markdown(document), encoding="utf-8")
    expected = {1, 2, 3}
    summaries = {scenario["concurrency"]: scenario["summary"] for scenario in scenarios}
    return 0 if expected.issubset(summaries) and all(summaries[item]["release_gate_passed"] for item in expected) else 1


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", action="append", required=True)
    parser.add_argument("--transport", choices=("websocket", "http"), default="websocket")
    parser.add_argument("--voice-prompt", dest="voice_prompts", action="append", default=[])
    parser.add_argument("--text", default="各位评委、同学，大家好。下面我将从事实、价值和长期影响三个方面展开论证。")
    parser.add_argument("--chunk-characters", type=int, default=12)
    parser.add_argument("--delta-delay-seconds", type=float, default=0.05)
    parser.add_argument("--finish-delay-seconds", type=float, default=2.0)
    parser.add_argument("--concurrency", type=int, action="append", default=[])
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--timeout-seconds", type=float, default=120)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--api-key-env", default="MOSS_TTS_REALTIME_API_KEY")
    parser.add_argument(
        "--canary-stage-observability",
        action="store_true",
        help="request numeric native-stage telemetry from an explicitly enabled canary gateway",
    )
    return parser


def main() -> None:
    args = argument_parser().parse_args()
    args.api_key = os.environ.get(args.api_key_env, "")
    args.concurrency = args.concurrency or [1, 2, 3]
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
