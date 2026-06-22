from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import asyncio
import httpx
import websockets
from fastapi import FastAPI


app = FastAPI(title="phdebate voice-agent", version="0.1.0")


@dataclass
class VoiceAgentTask:
    match_id: str
    speech_id: str
    speaker_id: str = ""
    livekit: Dict[str, Any] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    tts_sentences: List[Dict[str, Any]] = field(default_factory=list)


TASKS: Dict[str, VoiceAgentTask] = {}
SERVICE_PROBE_TTL_SECONDS = 5.0
_SERVICE_PROBE_CACHE: Dict[str, Dict[str, Any]] = {}
_SERVICE_PROBE_LOCKS: Dict[str, asyncio.Lock] = {}


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {
        "ok": True,
        "service": "phdebate-voice-agent",
        "version": "0.1.0",
        "livekit": _livekit_status(),
        "local_services": {
            "asr": await _cached_probe_service("asr", _asr_base_url()),
            "tts": await _cached_probe_service("tts", _tts_base_url()),
        },
        "active_tasks": len(TASKS),
    }


@app.post("/tasks/start")
async def start_task(body: Dict[str, Any]) -> Dict[str, Any]:
    task = VoiceAgentTask(
        match_id=str(body.get("match_id") or ""),
        speech_id=str(body.get("speech_id") or ""),
        speaker_id=str(body.get("speaker_id") or ""),
        livekit=_redact_livekit(body.get("livekit") or {}),
    )
    TASKS[_task_key(task.match_id, task.speech_id)] = task
    return {
        "ok": True,
        "status": "started",
        "task": _task_view(task),
        "livekit_runtime": _livekit_runtime_status(),
    }


@app.post("/tasks/stop")
async def stop_task(body: Dict[str, Any]) -> Dict[str, Any]:
    match_id = str(body.get("match_id") or "")
    speech_id = str(body.get("speech_id") or "")
    task = TASKS.pop(_task_key(match_id, speech_id), None)
    return {"ok": True, "status": "stopped", "task": _task_view(task) if task else None}


@app.post("/tts/sentence")
async def enqueue_tts_sentence(body: Dict[str, Any]) -> Dict[str, Any]:
    match_id = str(body.get("match_id") or "")
    speech_id = str(body.get("speech_id") or "")
    task = TASKS.get(_task_key(match_id, speech_id))
    if not task:
        task = VoiceAgentTask(match_id=match_id, speech_id=speech_id, speaker_id=str(body.get("speaker_id") or ""))
        TASKS[_task_key(match_id, speech_id)] = task
    sentence = {
        "task_id": body.get("task_id"),
        "speech_id": speech_id,
        "speaker_id": body.get("speaker_id"),
        "sentence_idx": int(body.get("sentence_idx") or 0),
        "text": str(body.get("text") or ""),
        "voice": str(body.get("voice") or ""),
        "provider": str(body.get("provider") or ""),
        "received_at": time.time(),
    }
    task.tts_sentences.append(sentence)
    task.tts_sentences = task.tts_sentences[-50:]
    return {
        "ok": True,
        "status": "queued",
        "sentence_idx": sentence["sentence_idx"],
        "queued_sentences": len(task.tts_sentences),
        "livekit_runtime": _livekit_runtime_status(),
    }


@app.get("/tasks")
async def list_tasks() -> Dict[str, Any]:
    return {"ok": True, "items": [_task_view(task) for task in TASKS.values()]}


def _task_key(match_id: str, speech_id: str) -> str:
    return f"{match_id}:{speech_id}"


def _task_view(task: Optional[VoiceAgentTask]) -> Dict[str, Any]:
    if not task:
        return {}
    return {
        "match_id": task.match_id,
        "speech_id": task.speech_id,
        "speaker_id": task.speaker_id,
        "started_at": task.started_at,
        "livekit": task.livekit,
        "queued_sentences": len(task.tts_sentences),
    }


def _redact_livekit(data: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in data.items() if key != "token"}


def _livekit_status() -> Dict[str, Any]:
    return {
        "enabled": _env_bool("PHDEBATE_LIVEKIT_ENABLED", False),
        "configured": bool(os.getenv("LIVEKIT_URL") and os.getenv("LIVEKIT_API_KEY") and os.getenv("LIVEKIT_API_SECRET")),
        "url": os.getenv("LIVEKIT_URL", ""),
    }


def _livekit_runtime_status() -> Dict[str, Any]:
    try:
        import livekit  # noqa: F401

        sdk_available = True
    except Exception:
        sdk_available = False
    return {
        "sdk_available": sdk_available,
        "media_bridge": "pending_credentials" if not _livekit_status()["configured"] else "ready_for_runtime_wiring",
    }


async def _probe_service(base_url: str) -> Dict[str, Any]:
    if base_url.startswith(("ws://", "wss://")):
        try:
            async with websockets.connect(base_url, open_timeout=1.5, close_timeout=1.0) as websocket:
                await websocket.send("START")
                raw = await asyncio.wait_for(websocket.recv(), timeout=1.5)
            return {"ok": True, "url": base_url, "path": "websocket:start", "status_code": 101, "message": raw}
        except Exception:
            return {"ok": False, "url": base_url, "path": "websocket:start"}
    for path in ("/health", "/api/health", "/v1/models"):
        try:
            async with httpx.AsyncClient(timeout=1.5) as client:
                response = await client.get(f"{base_url.rstrip('/')}{path}")
            if response.status_code < 500:
                return {"ok": True, "url": base_url, "path": path, "status_code": response.status_code}
        except Exception:
            continue
    return {"ok": False, "url": base_url}


async def _cached_probe_service(key: str, base_url: str) -> Dict[str, Any]:
    now = time.monotonic()
    cache_key = f"{key}:{base_url}"
    cached = _SERVICE_PROBE_CACHE.get(cache_key)
    if cached and now - float(cached.get("checked_at", 0.0)) < SERVICE_PROBE_TTL_SECONDS:
        result = dict(cached["result"])
        result["cached"] = True
        return result
    lock = _SERVICE_PROBE_LOCKS.setdefault(cache_key, asyncio.Lock())
    async with lock:
        now = time.monotonic()
        cached = _SERVICE_PROBE_CACHE.get(cache_key)
        if cached and now - float(cached.get("checked_at", 0.0)) < SERVICE_PROBE_TTL_SECONDS:
            result = dict(cached["result"])
            result["cached"] = True
            return result
        result = await _probe_service(base_url)
        _SERVICE_PROBE_CACHE[cache_key] = {"checked_at": now, "result": dict(result)}
    result["cached"] = False
    return result


def _asr_base_url() -> str:
    return os.getenv("PHDEBATE_FUNASR_ASR_URL") or os.getenv("PHDEBATE_LOCAL_ASR_BASE_URL", "ws://127.0.0.1:10095")


def _tts_base_url() -> str:
    return os.getenv("PHDEBATE_LOCAL_TTS_BASE_URL", "http://127.0.0.1:12302")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default
