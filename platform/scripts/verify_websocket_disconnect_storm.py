#!/usr/bin/env python3
"""Open WebSockets and abort them before reading the initial snapshot."""

from __future__ import annotations

import argparse
import asyncio
import json
import ssl
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx
import websockets

MAX_TOTAL_SPECTATORS = 5


@dataclass
class Metrics:
    connected: int = 0
    errors: list[str] = field(default_factory=list)


def websocket_base(http_base: str) -> str:
    parsed = urlparse(http_base.rstrip("/"))
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return f"{scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"


async def abort_connection(index: int, url: str, ssl_context: ssl.SSLContext | None, metrics: Metrics) -> None:
    options: dict = {"open_timeout": 15, "close_timeout": 0, "ping_interval": None}
    if ssl_context is not None:
        options["ssl"] = ssl_context
    try:
        socket = await websockets.connect(url, **options)
        metrics.connected += 1
        socket.transport.abort()
    except Exception as exc:
        metrics.errors.append(f"connection {index}: {type(exc).__name__}: {exc}")


async def run(args: argparse.Namespace) -> int:
    if not 1 <= args.clients <= MAX_TOTAL_SPECTATORS:
        raise SystemExit(
            f"--clients must be between 1 and {MAX_TOTAL_SPECTATORS}; "
            "the product admits at most 5 spectators across all rooms",
        )
    async with httpx.AsyncClient(base_url=args.base_url, verify=not args.insecure, timeout=15) as client:
        response = await client.get("/api/health")
        response.raise_for_status()

    ssl_context = None
    ws_base = websocket_base(args.base_url)
    if ws_base.startswith("wss://") and args.insecure:
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

    metrics = Metrics()
    url = f"{ws_base}/ws/rooms/{args.room}"
    await asyncio.gather(*(abort_connection(index, url, ssl_context, metrics) for index in range(args.clients)))
    await asyncio.sleep(args.settle_seconds)
    result = {
        "expected": args.clients,
        "connected": metrics.connected,
        "failed": len(metrics.errors),
        "sample_errors": metrics.errors[:10],
        "room": args.room,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if metrics.connected == args.clients and not metrics.errors else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:12340")
    parser.add_argument("--room", required=True)
    parser.add_argument(
        "--clients",
        type=int,
        default=MAX_TOTAL_SPECTATORS,
        help=f"simultaneous aborted spectators across the platform (1-{MAX_TOTAL_SPECTATORS})",
    )
    parser.add_argument("--settle-seconds", type=float, default=2.0)
    parser.add_argument("--insecure", action="store_true")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
