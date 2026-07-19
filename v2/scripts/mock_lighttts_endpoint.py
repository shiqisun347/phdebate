#!/usr/bin/env python3
"""Deterministic independent HTTP endpoint for LightTTS admission/load tests."""

from __future__ import annotations

import argparse
import io
import json
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def wav_bytes(seconds: float) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(b"\x01\x00" * int(24000 * seconds))
    return buffer.getvalue()


class Handler(BaseHTTPRequestHandler):
    delay_seconds = 0.2
    content = wav_bytes(1.0)
    lock = threading.Lock()
    active = 0
    calls = 0
    max_active = 0

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/metrics":
            with self.lock:
                payload = {
                    "active": type(self).active,
                    "calls": type(self).calls,
                    "max_active": type(self).max_active,
                }
            content = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return
        self.send_response(200)
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length", "0"))
        self.rfile.read(length)
        with self.lock:
            type(self).active += 1
            type(self).calls += 1
            type(self).max_active = max(type(self).max_active, type(self).active)
        try:
            time.sleep(self.delay_seconds)
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(self.content)))
            self.end_headers()
            self.wfile.write(self.content)
        finally:
            with self.lock:
                type(self).active -= 1

    def log_message(self, _format: str, *_args) -> None:
        return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18081)
    parser.add_argument("--delay-seconds", type=float, default=0.2)
    parser.add_argument("--wav-seconds", type=float, default=1.0)
    args = parser.parse_args()
    Handler.delay_seconds = max(0.01, args.delay_seconds)
    Handler.content = wav_bytes(max(0.2, args.wav_seconds))
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(json.dumps({"host": args.host, "port": args.port, "delay_seconds": Handler.delay_seconds}), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
