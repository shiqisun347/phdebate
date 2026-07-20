#!/usr/bin/env python3
"""Measure real MOSS WebSocket abort -> audio_reset -> released latency."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import websockets


def websocket_url(endpoint: str) -> str:
    value = endpoint.strip().rstrip("/")
    if value.startswith("https://"):
        value = "wss://" + value.removeprefix("https://")
    elif value.startswith("http://"):
        value = "ws://" + value.removeprefix("http://")
    if not value.startswith(("ws://", "wss://")):
        raise ValueError("endpoint must use http, https, ws, or wss")
    return value if value.endswith("/tts/session/ws") else value + "/tts/session/ws"


def health_url(endpoint: str) -> str:
    value = endpoint.strip().rstrip("/")
    value = value.replace("wss://", "https://", 1).replace("ws://", "http://", 1)
    value = value.removesuffix("/tts/session/ws")
    return value + "/health/ready"


async def run(args: argparse.Namespace) -> dict[str, Any]:
    api_key = os.environ.get(args.api_key_env, "")
    if not api_key:
        raise RuntimeError(f"missing API key environment variable: {args.api_key_env}")
    session_id = f"interrupt-{uuid.uuid4().hex}"
    headers = {"X-MOSS-Gateway-Key": api_key}
    first_pcm_at: float | None = None
    abort_sent_at: float | None = None
    audio_reset_at: float | None = None
    released_at: float | None = None
    post_abort_pcm_frames = 0
    released_payload: dict[str, Any] | None = None
    controls: list[str] = []

    async with websockets.connect(
        websocket_url(args.endpoint),
        additional_headers=headers,
        open_timeout=10,
        close_timeout=5,
        ping_interval=20,
        compression=None,
        max_size=None,
    ) as socket:
        await socket.send(
            json.dumps(
                {
                    "type": "start",
                    "seq": 0,
                    "session_id": session_id,
                    "voice": args.voice,
                    "user_text": None,
                },
                ensure_ascii=False,
            )
        )
        ready = json.loads(await asyncio.wait_for(socket.recv(), timeout=args.timeout_seconds))
        if ready.get("type") != "ready":
            raise RuntimeError(f"expected ready, received {ready.get('type')!r}")
        await socket.send(
            json.dumps({"type": "text_delta", "seq": 1, "text": args.text}, ensure_ascii=False)
        )

        while released_at is None:
            frame = await asyncio.wait_for(socket.recv(), timeout=args.timeout_seconds)
            now = time.perf_counter()
            if isinstance(frame, bytes):
                if first_pcm_at is None:
                    first_pcm_at = now
                    abort_sent_at = time.perf_counter()
                    await socket.send(json.dumps({"type": "abort", "reason": "qa_interrupt_probe"}))
                elif abort_sent_at is not None:
                    post_abort_pcm_frames += 1
                continue
            payload = json.loads(frame)
            control_type = str(payload.get("type") or "")
            controls.append(control_type)
            if control_type == "audio_reset":
                audio_reset_at = now
            elif control_type == "released":
                released_at = now
                released_payload = payload
            elif control_type == "error":
                raise RuntimeError(f"gateway returned error: {payload.get('code')}")

    if first_pcm_at is None or abort_sent_at is None or audio_reset_at is None or released_at is None:
        raise RuntimeError("interrupt lifecycle did not produce PCM, audio_reset, and released")
    async with httpx.AsyncClient(headers=headers, timeout=5) as client:
        health_response = await client.get(health_url(args.endpoint))
    health = health_response.json() if health_response.is_success else {}
    audio_reset_ms = (audio_reset_at - abort_sent_at) * 1000
    release_ms = (released_at - abort_sent_at) * 1000
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "endpoint_redacted": True,
        "session_id_recorded": False,
        "voice": args.voice,
        "first_pcm_observed": True,
        "audio_reset_ms": round(audio_reset_ms, 3),
        "released_ms": round(release_ms, 3),
        "post_abort_pcm_frames_before_reset": post_abort_pcm_frames,
        "controls": controls,
        "released_status": released_payload.get("status") if released_payload else None,
        "released": bool(released_payload and released_payload.get("released") is True),
        "health_active": health.get("active"),
        "health_orphan_count": health.get("orphan_count"),
        "gate_passed": bool(
            audio_reset_ms <= args.max_interrupt_ms
            and release_ms <= args.max_interrupt_ms
            and released_payload
            and released_payload.get("released") is True
            and released_payload.get("status") == "aborted"
            and health.get("active") == 0
            and health.get("orphan_count") == 0
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--voice", default="debate_voice_1")
    parser.add_argument(
        "--text",
        default="我们需要从事实、因果、成本和长期影响四个层面继续展开这一段完整的正式辩论论证。" * 4,
    )
    parser.add_argument("--api-key-env", default="MOSS_TTS_REALTIME_API_KEY")
    parser.add_argument("--timeout-seconds", type=float, default=30)
    parser.add_argument("--max-interrupt-ms", type=float, default=250)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(run(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result["gate_passed"] else 1)


if __name__ == "__main__":
    main()
