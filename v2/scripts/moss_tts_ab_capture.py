#!/usr/bin/env python3
"""Capture one fixed-voice MOSS realtime turn as a WAV for listening A/B tests."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
import wave
from pathlib import Path

import websockets


async def capture(args: argparse.Namespace) -> dict[str, float | int | str]:
    key = args.key_file.read_text(encoding="utf-8").strip()
    session_id = args.session_id or f"qa-{uuid.uuid4().hex}"
    started_at = time.monotonic()
    first_pcm_at: float | None = None
    pcm = bytearray()

    async with websockets.connect(
        args.url,
        additional_headers={"X-MOSS-Gateway-Key": key},
        max_size=None,
        open_timeout=args.timeout,
    ) as websocket:
        await websocket.send(
            json.dumps(
                {
                    "type": "start",
                    "seq": 0,
                    "session_id": session_id,
                    "voice": args.voice,
                    "user_text": args.instruction,
                },
                ensure_ascii=False,
            )
        )
        ready = json.loads(await asyncio.wait_for(websocket.recv(), timeout=args.timeout))
        if ready.get("type") != "ready":
            raise RuntimeError(f"unexpected start response: {ready}")

        await websocket.send(
            json.dumps({"type": "text_delta", "seq": 1, "text": args.text}, ensure_ascii=False)
        )
        next_seq = 2
        final_sent = False
        released = False
        while not released:
            frame = await asyncio.wait_for(websocket.recv(), timeout=args.timeout)
            if isinstance(frame, bytes):
                if first_pcm_at is None:
                    first_pcm_at = time.monotonic()
                pcm.extend(frame)
                continue
            payload = json.loads(frame)
            message_type = payload.get("type")
            if message_type == "ack" and not final_sent:
                await websocket.send(json.dumps({"type": "final", "seq": next_seq}))
                final_sent = True
            elif message_type == "error":
                raise RuntimeError(f"gateway error: {payload}")
            elif message_type == "released":
                released = True

    if not pcm:
        raise RuntimeError("gateway returned no PCM")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(args.output), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24_000)
        wav.writeframes(pcm)
    duration_seconds = len(pcm) / (24_000 * 2)
    return {
        "session_id": session_id,
        "voice": args.voice,
        "pcm_bytes": len(pcm),
        "duration_seconds": round(duration_seconds, 3),
        "first_pcm_ms": round(((first_pcm_at or time.monotonic()) - started_at) * 1_000, 1),
        "wall_seconds": round(time.monotonic() - started_at, 3),
        "output": str(args.output),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="ws://127.0.0.1:8890/tts/session/ws")
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--voice", default="debate_voice_1")
    parser.add_argument("--instruction", required=True)
    parser.add_argument("--text", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--session-id", default="")
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(capture(args)), ensure_ascii=False))


if __name__ == "__main__":
    main()
