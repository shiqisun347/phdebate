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

MAX_SPECTATORS_PER_ROOM = 20


@dataclass
class Metrics:
    latencies_ms: list[float] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    attempted: int = 0
    active: int = 0
    peak_active: int = 0
    expected: int = 0

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
            "peak_connected": self.peak_active,
        }


def websocket_base(http_base: str) -> str:
    parsed = urlparse(http_base.rstrip("/"))
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return f"{scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"


def validate_args(args: argparse.Namespace) -> None:
    parsed = urlparse(args.base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SystemExit("--base-url must be an absolute http(s) URL")
    if not 1 <= args.clients <= 2_000:
        raise SystemExit("--clients must be between 1 and 2000")
    if not 1 <= args.handshake_concurrency <= 100:
        raise SystemExit("--handshake-concurrency must be between 1 and 100")
    if not 1 <= args.duration <= 300:
        raise SystemExit("--duration must be between 1 and 300 seconds")
    if not 1 <= args.startup_timeout <= 600:
        raise SystemExit("--startup-timeout must be between 1 and 600 seconds")
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"} and not args.allow_public_load:
        raise SystemExit("remote load tests require --allow-public-load after operator approval")


async def watcher(
    index: int,
    ws_base: str,
    room_codes: list[str],
    insecure: bool,
    metrics: Metrics,
    handshake_slots: asyncio.Semaphore,
    all_attempted: asyncio.Event,
    release: asyncio.Event,
) -> None:
    code = room_codes[index % len(room_codes)]
    ssl_context = None
    if ws_base.startswith("wss://") and insecure:
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
    started = time.perf_counter()
    socket = None
    try:
        connect_options = {
            "open_timeout": 15,
            "close_timeout": 3,
            "ping_interval": None,
            "max_size": 4 * 1024 * 1024,
        }
        if ssl_context is not None:
            connect_options["ssl"] = ssl_context
        async with handshake_slots:
            socket = await websockets.connect(
                f"{ws_base}/ws/rooms/{code}",
                **connect_options,
            )
            snapshot = json.loads(await asyncio.wait_for(socket.recv(), timeout=15))
            if snapshot.get("type") != "snapshot" or snapshot.get("room", {}).get("code") != code:
                raise RuntimeError("invalid initial room snapshot")
            metrics.latencies_ms.append((time.perf_counter() - started) * 1000)
            metrics.active += 1
            metrics.peak_active = max(metrics.peak_active, metrics.active)
    except Exception as exc:
        metrics.errors.append(f"watcher {index} room {code}: {type(exc).__name__}: {exc}")
        if socket is not None:
            await socket.close()
            socket = None
    finally:
        metrics.attempted += 1
        if metrics.attempted == metrics.expected:
            all_attempted.set()

    if socket is None:
        return
    try:
        await socket.send(json.dumps({"type": "ping"}))
        while not release.is_set():
            try:
                await asyncio.wait_for(socket.recv(), timeout=2.0)
            except asyncio.TimeoutError:
                continue
    except Exception as exc:
        if not release.is_set():
            metrics.errors.append(f"watcher {index} room {code} disconnected while held: {type(exc).__name__}: {exc}")
    finally:
        metrics.active -= 1
        await socket.close()


async def run(args: argparse.Namespace) -> int:
    validate_args(args)
    room_codes = [item.strip() for item in args.rooms.split(",") if item.strip()]
    room_codes = list(dict.fromkeys(room_codes))
    if not room_codes:
        raise SystemExit("--rooms must contain at least one room code")
    if args.clients > len(room_codes) * MAX_SPECTATORS_PER_ROOM:
        raise SystemExit(
            f"--clients exceeds the product limit of {MAX_SPECTATORS_PER_ROOM} spectators per room; "
            f"provide at least {(args.clients + MAX_SPECTATORS_PER_ROOM - 1) // MAX_SPECTATORS_PER_ROOM} rooms"
        )
    async with httpx.AsyncClient(base_url=args.base_url, verify=not args.insecure, timeout=15) as client:
        health, catalog = await asyncio.gather(client.get("/api/health"), client.get("/api/competitions"))
        health.raise_for_status()
        catalog.raise_for_status()
        room_checks = await asyncio.gather(*(client.get(f"/api/rooms/{code}/public") for code in room_codes))
        for code, response in zip(room_codes, room_checks):
            if response.status_code != 200:
                raise RuntimeError(f"public room preflight failed: room={code} status={response.status_code}")

    metrics = Metrics()
    metrics.expected = args.clients
    handshake_slots = asyncio.Semaphore(args.handshake_concurrency)
    all_attempted = asyncio.Event()
    release = asyncio.Event()
    started = time.perf_counter()
    tasks = [
        asyncio.create_task(
            watcher(
                index,
                websocket_base(args.base_url),
                room_codes,
                args.insecure,
                metrics,
                handshake_slots,
                all_attempted,
                release,
            )
        )
        for index in range(args.clients)
    ]
    try:
        await asyncio.wait_for(all_attempted.wait(), timeout=args.startup_timeout)
        if metrics.errors:
            raise RuntimeError(f"{len(metrics.errors)} watcher connection(s) failed")
        await asyncio.sleep(args.duration)
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
    result = metrics.summary(args.clients)
    result["wall_seconds"] = round(time.perf_counter() - started, 2)
    result["rooms"] = room_codes
    result["handshake_concurrency"] = args.handshake_concurrency
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not metrics.errors and len(metrics.latencies_ms) == args.clients else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:12340")
    parser.add_argument("--rooms", required=True, help="comma-separated public room codes")
    parser.add_argument("--clients", type=int, default=20)
    parser.add_argument("--duration", type=float, default=10.0)
    parser.add_argument("--handshake-concurrency", type=int, default=20)
    parser.add_argument("--startup-timeout", type=float, default=180.0)
    parser.add_argument("--insecure", action="store_true", help="disable TLS certificate verification")
    parser.add_argument(
        "--allow-public-load",
        action="store_true",
        help="confirm that the operator approved a load test against a non-loopback target",
    )
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
