"""Open many anonymous room watchers concurrently without mutating match state."""

from __future__ import annotations

import asyncio
import json
import os
import ssl
import time

import websockets

BASE_URL = os.getenv("VERIFY_BASE_URL", "https://117.50.192.216")
ROOM_CODE = os.getenv("VERIFY_ROOM_CODE", "278571")
CONNECTIONS = max(1, int(os.getenv("VERIFY_CONNECTIONS", "5")))
# Product policy permits at most 5 concurrent spectators in one room.
HANDSHAKE_CONCURRENCY = max(1, int(os.getenv("VERIFY_HANDSHAKE_CONCURRENCY", "5")))
HOLD_SECONDS = max(1.0, float(os.getenv("VERIFY_HOLD_SECONDS", "10")))


async def run() -> dict:
    if CONNECTIONS > 5:
        raise RuntimeError("VERIFY_CONNECTIONS cannot exceed the global 5-spectator product limit")
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    websocket_url = BASE_URL.replace("https://", "wss://").replace("http://", "ws://")
    origin = BASE_URL.rstrip("/")
    handshake_slots = asyncio.Semaphore(HANDSHAKE_CONCURRENCY)
    release = asyncio.Event()
    attempts_done = asyncio.Event()
    opened = 0
    failed = 0
    errors: list[str] = []

    async def watcher(index: int) -> None:
        nonlocal opened, failed
        socket = None
        try:
            async with handshake_slots:
                socket = await websockets.connect(
                    f"{websocket_url}/ws/rooms/{ROOM_CODE}",
                    ssl=context if websocket_url.startswith("wss://") else None,
                    origin=origin,
                    open_timeout=20,
                    close_timeout=5,
                    max_size=4 * 1024 * 1024,
                    ping_interval=20,
                    ping_timeout=20,
                )
                initial = json.loads(await asyncio.wait_for(socket.recv(), timeout=20))
                if initial.get("room", {}).get("code") != ROOM_CODE:
                    raise RuntimeError("initial room snapshot mismatch")
                opened += 1
        except Exception as exc:
            failed += 1
            if len(errors) < 10:
                errors.append(f"{index}:{type(exc).__name__}:{str(exc)[:120]}")
        finally:
            if opened + failed == CONNECTIONS:
                attempts_done.set()
        if socket is None:
            return
        try:
            await release.wait()
        finally:
            await socket.close()

    started = time.monotonic()
    tasks = [asyncio.create_task(watcher(index)) for index in range(CONNECTIONS)]
    try:
        await asyncio.wait_for(attempts_done.wait(), timeout=120)
        print(
            json.dumps(
                {"phase": "holding", "opened": opened, "failed": failed, "room_code": ROOM_CODE},
                ensure_ascii=False,
            ),
            flush=True,
        )
        if failed:
            raise RuntimeError(f"{failed} watcher connections failed: {errors}")
        await asyncio.sleep(HOLD_SECONDS)
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
    return {
        "ok": opened == CONNECTIONS and failed == 0,
        "connections": opened,
        "failed": failed,
        "room_code": ROOM_CODE,
        "hold_seconds": HOLD_SECONDS,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "anonymous_read_only": True,
    }


if __name__ == "__main__":
    print(asyncio.run(run()))
