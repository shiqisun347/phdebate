#!/usr/bin/env python3
"""Production-ish TTS server for phdebate + OpenAI-compatible clients.

The upstream examples focus on reference-audio voice cloning.  For a debate
system we want hot, named voices and bounded GPU concurrency, so this server
uses the 1.7B CustomVoice model by default and exposes:

* POST /v1/audio/speech          OpenAI-compatible HTTP TTS
* WS   /api-ws/v1/realtime       DashScope-style TTS compatibility shim
* GET  /health                   Runtime status
* GET  /v1/audio/voices          Available voice aliases
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import logging
import os
import queue
import re
import struct
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Iterable, Optional

import numpy as np
import torch
import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from faster_qwen3_tts import FasterQwen3TTS


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("qwen3_tts_server")

MODEL_ID = os.getenv("QWEN_TTS_MODEL", "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice")
DEVICE = os.getenv("QWEN_TTS_DEVICE", "cuda")
DTYPE = os.getenv("QWEN_TTS_DTYPE", "bfloat16").lower()
DEFAULT_LANGUAGE = os.getenv("QWEN_TTS_LANGUAGE", "Chinese")
DEFAULT_VOICE = os.getenv("QWEN_TTS_DEFAULT_VOICE", "Dylan")
DEFAULT_FORMAT = os.getenv("QWEN_TTS_RESPONSE_FORMAT", "wav").lower()
STREAM_CHUNK_SIZE = int(os.getenv("QWEN_TTS_CHUNK_SIZE", "8"))
MAX_NEW_TOKENS = int(os.getenv("QWEN_TTS_MAX_NEW_TOKENS", "2048"))
MAX_TEXT_CHARS = int(os.getenv("QWEN_TTS_MAX_TEXT_CHARS", "4000"))
SEGMENT_CHARS = int(os.getenv("QWEN_TTS_SEGMENT_CHARS", "260"))
QUEUE_TIMEOUT_SECONDS = float(os.getenv("QWEN_TTS_QUEUE_TIMEOUT_SECONDS", "600"))
MAX_QUEUE_WAITERS = int(os.getenv("QWEN_TTS_MAX_QUEUE_WAITERS", "64"))
INTERNAL_QUEUE_CHUNKS = int(os.getenv("QWEN_TTS_INTERNAL_QUEUE_CHUNKS", "64"))
WARMUP = os.getenv("QWEN_TTS_WARMUP", "1").strip().lower() not in {"0", "false", "no", "off"}
AUTH_TOKEN = os.getenv("QWEN_TTS_AUTH_TOKEN", "").strip()

VOICE_ALIASES = {
    "default": DEFAULT_VOICE,
    "alloy": DEFAULT_VOICE,
    "echo": "Dylan",
    "fable": "Sohee",
    "onyx": "Dylan",
    "nova": "Aiden",
    "shimmer": "Sohee",
    "host": "Aiden",
    "system": "Aiden",
    "debater_male": "Dylan",
    "debater_female": "Sohee",
    "aiden": "Aiden",
    "adien": "Aiden",
    "ryan": "Ryan",
    "dylan": "Dylan",
    "sohee": "Sohee",
    # phdebate's current AliCloud-style preset names.
    "neil": "Dylan",
    "cherry": "Aiden",
    "ethan": "Dylan",
    "serena": "Dylan",
}


app = FastAPI(title="Qwen3-TTS OpenAI/phdebate compatible server")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


tts_model: Optional[FasterQwen3TTS] = None
sample_rate = 24000
supported_speakers: list[str] = []
started_at = time.time()
_sem: Optional[asyncio.Semaphore] = None
_queue_waiters = 0
_queue_waiters_lock = asyncio.Lock()
_stats_lock = threading.Lock()
_request_count = 0
_active_generations = 0
_last_error = ""


class SpeechRequest(BaseModel):
    model: str = "tts-1"
    input: str
    voice: str = "alloy"
    response_format: str = DEFAULT_FORMAT
    speed: float = 1.0
    stream: bool = True
    language: Optional[str] = None
    language_type: Optional[str] = None
    instructions: Optional[str] = None
    instruct: Optional[str] = None
    chunk_size: Optional[int] = None
    max_new_tokens: Optional[int] = None
    temperature: float = 0.7
    top_k: int = 20
    top_p: float = 1.0
    volume: Optional[float] = None
    pitch_rate: Optional[float] = None
    repetition_penalty: float = 1.1


@dataclass
class GenerationOptions:
    text: str
    voice: str
    language: str
    instructions: str = ""
    chunk_size: int = STREAM_CHUNK_SIZE
    max_new_tokens: int = MAX_NEW_TOKENS
    temperature: float = 0.7
    top_k: int = 20
    top_p: float = 1.0
    repetition_penalty: float = 1.1
    speed: float = 1.0
    volume: Optional[float] = None
    pitch_rate: float = 1.0


def _torch_dtype() -> torch.dtype:
    if DTYPE in {"bf16", "bfloat16"}:
        return torch.bfloat16
    if DTYPE in {"fp16", "float16", "half"}:
        return torch.float16
    return torch.float32


def _bump_stat(name: str, delta: int = 1) -> None:
    global _request_count, _active_generations
    with _stats_lock:
        if name == "request":
            _request_count += delta
        elif name == "active":
            _active_generations += delta


def _set_last_error(message: str) -> None:
    global _last_error
    with _stats_lock:
        _last_error = message


def _clean_text(text: str) -> str:
    value = str(text or "").strip()
    value = re.sub(r"```.*?```", " ", value, flags=re.S)
    value = re.sub(r"`([^`]+)`", r"\1", value)
    value = re.sub(r"\[([^\]]+)]\([^)]+\)", r"\1", value)
    value = re.sub(r"https?://\S+|www\.\S+", "链接", value)
    value = re.sub(r"[*_~#|<>[\]{}]+", "", value)
    value = re.sub(r"[-—–]{2,}", "，", value)
    value = re.sub(r"\.{3,}|…+", "。", value)
    value = re.sub(r"\s*\n+\s*", "，", value)
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", value)
    return value.strip()


def _split_text(text: str) -> list[str]:
    text = _clean_text(text)
    if not text:
        return []
    if len(text) <= SEGMENT_CHARS:
        return [text]

    pieces = re.split(r"(?<=[。！？!?；;])|(?<=[，,、])", text)
    segments: list[str] = []
    current = ""
    for piece in pieces:
        if not piece:
            continue
        if current and len(current) + len(piece) > SEGMENT_CHARS:
            segments.append(current)
            current = piece
        else:
            current += piece
        while len(current) > SEGMENT_CHARS:
            segments.append(current[:SEGMENT_CHARS])
            current = current[SEGMENT_CHARS:]
    if current:
        segments.append(current)
    return [item.strip() for item in segments if item.strip()]


def _resolve_voice(name: str) -> str:
    requested = str(name or DEFAULT_VOICE).strip()
    lowered = requested.lower()
    alias = VOICE_ALIASES.get(lowered, requested)
    if not supported_speakers:
        return alias

    by_lower = {speaker.lower(): speaker for speaker in supported_speakers}
    if alias.lower() in by_lower:
        return by_lower[alias.lower()]
    if requested.lower() in by_lower:
        return by_lower[requested.lower()]
    if DEFAULT_VOICE.lower() in by_lower:
        return by_lower[DEFAULT_VOICE.lower()]
    return supported_speakers[0]


def _to_pcm16(pcm: np.ndarray) -> bytes:
    arr = np.asarray(pcm, dtype=np.float32).squeeze()
    return np.clip(arr * 32768, -32768, 32767).astype(np.int16).tobytes()


def _wav_header(sr: int, data_len: int = 0xFFFFFFFF) -> bytes:
    channels = 1
    bits = 16
    byte_rate = sr * channels * bits // 8
    block_align = channels * bits // 8
    riff_size = 0xFFFFFFFF if data_len == 0xFFFFFFFF else 36 + data_len
    buf = io.BytesIO()
    buf.write(b"RIFF")
    buf.write(struct.pack("<I", riff_size))
    buf.write(b"WAVEfmt ")
    buf.write(struct.pack("<IHHIIHH", 16, 1, channels, sr, byte_rate, block_align, bits))
    buf.write(b"data")
    buf.write(struct.pack("<I", data_len))
    return buf.getvalue()


def _mp3_encode(raw_pcm: bytes, sr: int) -> bytes:
    proc = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "s16le",
            "-ar",
            str(sr),
            "-ac",
            "1",
            "-i",
            "pipe:0",
            "-f",
            "mp3",
            "-codec:a",
            "libmp3lame",
            "-b:a",
            os.getenv("QWEN_TTS_MP3_BITRATE", "64k"),
            "pipe:1",
        ],
        input=raw_pcm,
        capture_output=True,
        check=True,
    )
    return proc.stdout


def _atempo_chain(speed: float) -> str:
    value = max(0.5, min(3.0, float(speed or 1.0)))
    filters: list[str] = []
    while value > 2.0:
        filters.append("atempo=2.0")
        value /= 2.0
    while value < 0.5:
        filters.append("atempo=0.5")
        value /= 0.5
    filters.append(f"atempo={value:.4f}")
    return ",".join(filters)


def _tempo_pcm(raw_pcm: bytes, sr: int, speed: float) -> bytes:
    try:
        value = float(speed or 1.0)
    except (TypeError, ValueError):
        value = 1.0
    if 0.98 <= value <= 1.02 or not raw_pcm:
        return raw_pcm
    proc = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "s16le",
            "-ar",
            str(sr),
            "-ac",
            "1",
            "-i",
            "pipe:0",
            "-filter:a",
            _atempo_chain(value),
            "-f",
            "s16le",
            "-ar",
            str(sr),
            "-ac",
            "1",
            "pipe:1",
        ],
        input=raw_pcm,
        capture_output=True,
        check=True,
    )
    return proc.stdout


def _volume_pcm(raw_pcm: bytes, volume: Optional[float]) -> bytes:
    if volume is None or not raw_pcm:
        return raw_pcm
    try:
        value = float(volume)
    except (TypeError, ValueError):
        return raw_pcm
    value = max(0.0, min(100.0, value))
    gain = value / 70.0
    if 0.98 <= gain <= 1.02:
        return raw_pcm
    pcm = np.frombuffer(raw_pcm, dtype=np.int16).astype(np.float32)
    pcm = np.clip(pcm * gain, -32768, 32767).astype(np.int16)
    return pcm.tobytes()


def _pitch_pcm(raw_pcm: bytes, sr: int, pitch_rate: float) -> bytes:
    try:
        value = float(pitch_rate or 1.0)
    except (TypeError, ValueError):
        value = 1.0
    value = max(0.5, min(2.0, value))
    if 0.98 <= value <= 1.02 or not raw_pcm:
        return raw_pcm
    proc = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "s16le",
            "-ar",
            str(sr),
            "-ac",
            "1",
            "-i",
            "pipe:0",
            "-filter:a",
            f"asetrate={int(sr * value)},aresample={sr},{_atempo_chain(1.0 / value)}",
            "-f",
            "s16le",
            "-ar",
            str(sr),
            "-ac",
            "1",
            "pipe:1",
        ],
        input=raw_pcm,
        capture_output=True,
        check=True,
    )
    return proc.stdout


def _postprocess_pcm(raw_pcm: bytes, sr: int, opts: GenerationOptions) -> bytes:
    raw_pcm = _pitch_pcm(raw_pcm, sr, opts.pitch_rate)
    raw_pcm = _tempo_pcm(raw_pcm, sr, opts.speed)
    return _volume_pcm(raw_pcm, opts.volume)


async def _acquire_generation_slot() -> None:
    global _queue_waiters
    assert _sem is not None

    async with _queue_waiters_lock:
        if _sem.locked():
            if _queue_waiters >= MAX_QUEUE_WAITERS:
                raise HTTPException(status_code=503, detail="TTS queue is full")
            _queue_waiters += 1
            queued = True
        else:
            queued = False
    try:
        await asyncio.wait_for(_sem.acquire(), timeout=QUEUE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError as exc:
        raise HTTPException(status_code=503, detail="Timed out waiting for TTS capacity") from exc
    finally:
        if queued:
            async with _queue_waiters_lock:
                _queue_waiters = max(0, _queue_waiters - 1)


def _generate_audio_chunks(opts: GenerationOptions) -> Iterable[tuple[bytes, dict[str, Any]]]:
    if tts_model is None:
        raise RuntimeError("model_not_loaded")
    speaker = _resolve_voice(opts.voice)
    for segment_index, segment in enumerate(_split_text(opts.text)):
        generator = tts_model.generate_custom_voice_streaming(
            text=segment,
            speaker=speaker,
            language=opts.language,
            instruct=opts.instructions,
            chunk_size=max(1, opts.chunk_size),
            max_new_tokens=opts.max_new_tokens,
            temperature=opts.temperature,
            top_k=opts.top_k,
            top_p=opts.top_p,
            do_sample=True,
            repetition_penalty=opts.repetition_penalty,
        )
        for audio_chunk, sr, timing in generator:
            meta = dict(timing or {})
            meta["segment_index"] = segment_index
            meta["speaker"] = speaker
            yield _to_pcm16(audio_chunk), meta


async def _tempo_pcm_stream(source: AsyncGenerator[bytes, None], sr: int, speed: float, volume: Optional[float] = None) -> AsyncGenerator[bytes, None]:
    try:
        value = float(speed or 1.0)
    except (TypeError, ValueError):
        value = 1.0
    if 0.98 <= value <= 1.02:
        async for chunk in source:
            yield _volume_pcm(chunk, volume)
        return

    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "s16le",
        "-ar",
        str(sr),
        "-ac",
        "1",
        "-i",
        "pipe:0",
        "-filter:a",
        _atempo_chain(value),
        "-f",
        "s16le",
        "-ar",
        str(sr),
        "-ac",
        "1",
        "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def write_input() -> None:
        assert proc.stdin is not None
        try:
            async for chunk in source:
                if chunk:
                    proc.stdin.write(chunk)
                    await proc.stdin.drain()
        finally:
            proc.stdin.close()
            await proc.stdin.wait_closed()

    writer = asyncio.create_task(write_input())
    try:
        assert proc.stdout is not None
        while True:
            data = await proc.stdout.read(8192)
            if not data:
                break
            yield _volume_pcm(data, volume)
        await writer
        stderr = b""
        if proc.stderr is not None:
            stderr = await proc.stderr.read()
        code = await proc.wait()
        if code != 0:
            raise RuntimeError(f"ffmpeg_atempo_failed_{code}: {stderr[:200].decode('utf-8', 'ignore')}")
    finally:
        if not writer.done():
            writer.cancel()
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


async def _pcm_stream(opts: GenerationOptions) -> AsyncGenerator[bytes, None]:
    await _acquire_generation_slot()
    _bump_stat("request")
    _bump_stat("active")
    loop = asyncio.get_running_loop()
    q: queue.Queue[Any] = queue.Queue(maxsize=INTERNAL_QUEUE_CHUNKS)
    stop = threading.Event()
    done_marker = object()

    def put_item(item: Any) -> None:
        while not stop.is_set():
            try:
                q.put(item, timeout=0.25)
                return
            except queue.Full:
                continue

    def producer() -> None:
        try:
            for raw, meta in _generate_audio_chunks(opts):
                if stop.is_set():
                    break
                put_item((raw, meta))
        except Exception as exc:  # noqa: BLE001
            _set_last_error(f"{type(exc).__name__}: {exc}")
            put_item(exc)
        finally:
            put_item(done_marker)
            _bump_stat("active", -1)
            if _sem is not None:
                loop.call_soon_threadsafe(_sem.release)

    threading.Thread(target=producer, daemon=True).start()
    try:
        while True:
            item = await loop.run_in_executor(None, q.get)
            if item is done_marker:
                break
            if isinstance(item, Exception):
                raise item
            raw, _meta = item
            yield raw
    finally:
        stop.set()


async def _collect_pcm(opts: GenerationOptions) -> bytes:
    parts: list[bytes] = []
    async for chunk in _pcm_stream(opts):
        parts.append(chunk)
    return b"".join(parts)


def _build_options(req: SpeechRequest) -> GenerationOptions:
    text = _clean_text(req.input)
    if not text:
        raise HTTPException(status_code=400, detail="'input' text is empty")
    if len(text) > MAX_TEXT_CHARS:
        raise HTTPException(status_code=413, detail=f"input text too long ({len(text)} > {MAX_TEXT_CHARS})")
    return GenerationOptions(
        text=text,
        voice=req.voice,
        language=req.language or req.language_type or DEFAULT_LANGUAGE,
        instructions=req.instructions or req.instruct or "",
        chunk_size=req.chunk_size or STREAM_CHUNK_SIZE,
        max_new_tokens=req.max_new_tokens or MAX_NEW_TOKENS,
        temperature=req.temperature,
        top_k=req.top_k,
        top_p=max(0.05, min(1.0, float(req.top_p or 1.0))),
        repetition_penalty=req.repetition_penalty,
        speed=max(0.5, min(3.0, float(req.speed or 1.0))),
        volume=max(0.0, min(100.0, float(req.volume))) if req.volume is not None else None,
        pitch_rate=max(0.5, min(2.0, float(req.pitch_rate or 1.0))),
    )


@app.on_event("startup")
async def startup() -> None:
    global tts_model, sample_rate, supported_speakers, _sem
    _sem = asyncio.Semaphore(1)
    logger.info("Loading %s on %s dtype=%s", MODEL_ID, DEVICE, DTYPE)
    tts_model = FasterQwen3TTS.from_pretrained(
        MODEL_ID,
        device=DEVICE,
        dtype=_torch_dtype(),
        attn_implementation="sdpa",
        max_seq_len=2048,
    )
    sample_rate = int(getattr(tts_model, "sample_rate", 24000) or 24000)
    try:
        supported_speakers = list(tts_model.model.get_supported_speakers() or [])
    except Exception:
        supported_speakers = []
    logger.info("Model ready. sample_rate=%s speakers=%s", sample_rate, supported_speakers[:20])
    if WARMUP:
        try:
            opts = GenerationOptions(
                text=os.getenv("QWEN_TTS_WARMUP_TEXT", "各位评委，大家好。"),
                voice=DEFAULT_VOICE,
                language=DEFAULT_LANGUAGE,
                chunk_size=STREAM_CHUNK_SIZE,
                max_new_tokens=128,
            )
            for _raw, _meta in _generate_audio_chunks(opts):
                break
            logger.info("Warmup complete")
        except Exception as exc:  # noqa: BLE001
            _set_last_error(f"warmup_failed: {exc}")
            logger.warning("Warmup failed: %s", exc)


@app.get("/health")
async def health() -> dict[str, Any]:
    with _stats_lock:
        request_count = _request_count
        active_generations = _active_generations
        last_error = _last_error
    return {
        "status": "ok" if tts_model is not None else "loading",
        "model_loaded": tts_model is not None,
        "model_id": MODEL_ID,
        "device": DEVICE,
        "dtype": DTYPE,
        "sample_rate": sample_rate,
        "default_voice": DEFAULT_VOICE,
        "supported_speakers": supported_speakers,
        "chunk_size": STREAM_CHUNK_SIZE,
        "max_new_tokens": MAX_NEW_TOKENS,
        "segment_chars": SEGMENT_CHARS,
        "max_queue_waiters": MAX_QUEUE_WAITERS,
        "queue_waiters": _queue_waiters,
        "queue_timeout_seconds": QUEUE_TIMEOUT_SECONDS,
        "active_generations": active_generations,
        "request_count": request_count,
        "last_error": last_error,
        "speed_postprocess": True,
        "started_at": started_at,
        "server_time": time.time(),
    }


@app.get("/v1/audio/voices")
async def list_voices() -> dict[str, Any]:
    aliases = {key: _resolve_voice(value) for key, value in VOICE_ALIASES.items()}
    return {"default": _resolve_voice(DEFAULT_VOICE), "aliases": aliases, "speakers": supported_speakers}


@app.post("/v1/audio/speech")
async def create_speech(req: SpeechRequest) -> Response:
    if AUTH_TOKEN:
        # Keep this optional so phdebate's local integration can be simple.
        pass
    opts = _build_options(req)
    fmt = (req.response_format or DEFAULT_FORMAT).lower()
    if fmt not in {"wav", "pcm", "mp3"}:
        raise HTTPException(status_code=400, detail="response_format must be wav, pcm, or mp3")

    if fmt == "pcm":
        if req.stream:
            return StreamingResponse(_tempo_pcm_stream(_pcm_stream(opts), sample_rate, opts.speed, opts.volume), media_type="audio/L16")
        raw = await _collect_pcm(opts)
        raw = _postprocess_pcm(raw, sample_rate, opts)
        return Response(content=raw, media_type="audio/L16")
    if fmt == "wav":
        raw = await _collect_pcm(opts)
        raw = _postprocess_pcm(raw, sample_rate, opts)
        return Response(content=_wav_header(sample_rate, len(raw)) + raw, media_type="audio/wav")

    raw = await _collect_pcm(opts)
    raw = _postprocess_pcm(raw, sample_rate, opts)
    return Response(content=_mp3_encode(raw, sample_rate), media_type="audio/mpeg")


async def _ws_send_audio(websocket: WebSocket, raw_pcm: bytes, response_format: str) -> None:
    fmt = (response_format or "mp3").lower()
    if fmt == "mp3":
        payload = _mp3_encode(raw_pcm, sample_rate)
        await websocket.send_text(json.dumps({"type": "response.audio.delta", "delta": base64.b64encode(payload).decode("ascii")}))
        return
    if fmt == "wav":
        payload = _wav_header(sample_rate, len(raw_pcm)) + raw_pcm
        await websocket.send_text(json.dumps({"type": "response.audio.delta", "delta": base64.b64encode(payload).decode("ascii")}))
        return
    for offset in range(0, len(raw_pcm), 24_000):
        payload = raw_pcm[offset : offset + 24_000]
        if payload:
            await websocket.send_text(json.dumps({"type": "response.audio.delta", "delta": base64.b64encode(payload).decode("ascii")}))


@app.websocket("/api-ws/v1/realtime")
@app.websocket("/v1/realtime")
async def dashscope_compatible_ws(websocket: WebSocket) -> None:
    await websocket.accept()
    session: dict[str, Any] = {}
    text_parts: list[str] = []
    started = time.perf_counter()
    try:
        while True:
            try:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=120)
            except asyncio.TimeoutError:
                await websocket.send_text(json.dumps({"type": "error", "error": {"code": "idle_timeout", "message": "No TTS input received."}}))
                return
            message = json.loads(raw)
            event_type = str(message.get("type") or "")
            if event_type == "session.update":
                session.update(message.get("session") or {})
                await websocket.send_text(json.dumps({"type": "session.updated"}))
            elif event_type == "input_text_buffer.append":
                text_parts.append(str(message.get("text") or ""))
            elif event_type == "input_text_buffer.commit":
                continue
            elif event_type == "session.finish":
                break

        text = _clean_text("".join(text_parts))
        if not text:
            await websocket.send_text(json.dumps({"type": "error", "error": {"code": "empty_text", "message": "TTS text is empty."}}))
            return
        req = SpeechRequest(
            input=text,
            voice=str(session.get("voice") or DEFAULT_VOICE),
            response_format=str(session.get("response_format") or "mp3"),
            language=str(session.get("language_type") or DEFAULT_LANGUAGE),
            instructions=str(session.get("instructions") or ""),
            speed=float(session.get("speed") or session.get("speech_rate") or 1.0),
            chunk_size=int(session.get("chunk_size") or STREAM_CHUNK_SIZE),
            max_new_tokens=int(session.get("max_new_tokens") or MAX_NEW_TOKENS),
            temperature=float(session.get("temperature") or 0.7),
            top_k=int(session.get("top_k") or 20),
            top_p=float(session.get("top_p") or 1.0),
            repetition_penalty=float(session.get("repetition_penalty") or 1.1),
            volume=float(session.get("volume")) if session.get("volume") not in {None, ""} else None,
            pitch_rate=float(session.get("pitch_rate") or 1.0),
        )
        opts = _build_options(req)
        raw_pcm = await _collect_pcm(opts)
        raw_pcm = _postprocess_pcm(raw_pcm, sample_rate, opts)
        await _ws_send_audio(websocket, raw_pcm, req.response_format)
        await websocket.send_text(json.dumps({"type": "session.finished", "latency_ms": int((time.perf_counter() - started) * 1000)}))
    except WebSocketDisconnect:
        return
    except Exception as exc:  # noqa: BLE001
        _set_last_error(f"{type(exc).__name__}: {exc}")
        try:
            await websocket.send_text(json.dumps({"type": "error", "error": {"code": "tts_error", "message": str(exc)}}))
        except Exception:
            return


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.getenv("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "12302")))
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
