#!/usr/bin/env python3
"""Read-only HTTP/WebSocket load probe for public debate rooms."""

from __future__ import annotations

import argparse
import asyncio
import json
import ssl
import statistics
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx
import websockets


@dataclass
class Metrics:
    latencies_ms: list[float] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self, expected: int) -> dict:
        ordered = sorted(self.latencies_ms)

        def percentile(fraction: float) -> float | None:
            return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))] if ordered else None

        return {
            "expected": expected,
            "connected": len(ordered),
            "failed": len(self.errors),
            "success_rate": round(len(ordered) / expected * 100, 2) if expected else 0,
            "latency_ms": {
                "median": round(statistics.median(ordered), 2) if ordered else None,
                "p95": round(percentile(0.95), 2) if ordered else None,
                "max": round(max(ordered), 2) if ordered else None,
            },
            "sample_errors": self.errors[:10],
        }


def websocket_base(http_base: str) -> str:
    parsed = urlparse(http_base.rstrip("/"))
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return f"{scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"


async def watcher(index: int, ws_base: str, room_codes: list[str], duration: float, insecure: bool, metrics: Metrics) -> None:
    code = room_codes[index % len(room_codes)]
    ssl_context = None
    if ws_base.startswith("wss://") and insecure:
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
    started = time.perf_counter()
    try:
        connect_options = {
            "open_timeout": 15,
            "close_timeout": 3,
            "ping_interval": None,
            "max_size": 4 * 1024 * 1024,
        }
        if ssl_context is not None:
            connect_options["ssl"] = ssl_context
        async with websockets.connect(
            f"{ws_base}/ws/rooms/{code}",
            **connect_options,
        ) as socket:
            snapshot = json.loads(await asyncio.wait_for(socket.recv(), timeout=15))
            if snapshot.get("type") != "snapshot" or snapshot.get("room", {}).get("code") != code:
                raise RuntimeError("invalid initial room snapshot")
            metrics.latencies_ms.append((time.perf_counter() - started) * 1000)
            await socket.send(json.dumps({"type": "ping"}))
            deadline = time.monotonic() + duration
            while time.monotonic() < deadline:
                timeout = min(2.0, max(0.05, deadline - time.monotonic()))
                try:
                    await asyncio.wait_for(socket.recv(), timeout=timeout)
                except asyncio.TimeoutError:
                    continue
    except Exception as exc:
        metrics.errors.append(f"watcher {index} room {code}: {type(exc).__name__}: {exc}")


async def run(args: argparse.Namespace) -> int:
    room_codes = [item.strip() for item in args.rooms.split(",") if item.strip()]
    if not room_codes:
        raise SystemExit("--rooms must contain at least one room code")
    async with httpx.AsyncClient(base_url=args.base_url, verify=not args.insecure, timeout=15) as client:
        health, catalog = await asyncio.gather(client.get("/api/health"), client.get("/api/competitions"))
        health.raise_for_status()
        catalog.raise_for_status()

    metrics = Metrics()
    started = time.perf_counter()
    await asyncio.gather(
        *(watcher(index, websocket_base(args.base_url), room_codes, args.duration, args.insecure, metrics) for index in range(args.clients))
    )
    result = metrics.summary(args.clients)
    result["wall_seconds"] = round(time.perf_counter() - started, 2)
    result["rooms"] = room_codes
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not metrics.errors and len(metrics.latencies_ms) == args.clients else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:12340")
    parser.add_argument("--rooms", required=True, help="comma-separated public room codes")
    parser.add_argument("--clients", type=int, default=500)
    parser.add_argument("--duration", type=float, default=10.0)
    parser.add_argument("--insecure", action="store_true", help="disable TLS certificate verification")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
