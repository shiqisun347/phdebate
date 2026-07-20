"""Read-only multi-room WebSocket isolation verifier."""

from __future__ import annotations

import argparse
import asyncio
import json
import ssl
import time

import httpx
import websockets


async def run(base_url: str, room_codes: list[str], connections_per_room: int, concurrency: int) -> dict:
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    websocket_base = base_url.replace("https://", "wss://").replace("http://", "ws://")
    origin = base_url.rstrip("/")
    async with httpx.AsyncClient(base_url=base_url, verify=False, timeout=10) as client:
        expected = {}
        for code in room_codes:
            response = await client.get(f"/api/rooms/{code}/public")
            response.raise_for_status()
            expected[code] = int(response.json()["room"]["seq"])

    slots = asyncio.Semaphore(concurrency)
    opened = mismatches = failures = 0
    observed: dict[str, set[int]] = {code: set() for code in room_codes}
    errors: list[str] = []

    async def watcher(code: str, index: int) -> None:
        nonlocal opened, mismatches, failures
        try:
            async with slots:
                async with websockets.connect(
                    f"{websocket_base}/ws/rooms/{code}",
                    ssl=ssl_context if websocket_base.startswith("wss://") else None,
                    origin=origin,
                    open_timeout=15,
                    close_timeout=3,
                ) as socket:
                    payload = json.loads(await asyncio.wait_for(socket.recv(), timeout=15))
                    actual_code = payload.get("room", {}).get("code")
                    actual_seq = int(payload.get("room", {}).get("seq", -1))
                    if actual_code != code:
                        mismatches += 1
                        raise AssertionError(f"expected {code}, received {actual_code}")
                    observed[code].add(actual_seq)
                    opened += 1
        except Exception as exc:
            failures += 1
            if len(errors) < 10:
                errors.append(f"{code}:{index}:{type(exc).__name__}:{str(exc)[:100]}")

    started = time.monotonic()
    await asyncio.gather(
        *(watcher(code, index) for code in room_codes for index in range(connections_per_room))
    )
    assert failures == 0, errors
    assert mismatches == 0
    for code, sequences in observed.items():
        assert sequences == {expected[code]}, (code, sequences, expected[code])
    return {
        "ok": True,
        "rooms": room_codes,
        "expected_sequences": expected,
        "connections": opened,
        "failures": failures,
        "cross_room_mismatches": mismatches,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://117.50.192.216")
    parser.add_argument("--room-codes", default="551958,214317,596306,287138,192885")
    parser.add_argument("--connections-per-room", type=int, default=40)
    parser.add_argument("--concurrency", type=int, default=20)
    args = parser.parse_args()
    print(
        json.dumps(
            asyncio.run(
                run(
                    args.base_url.rstrip("/"),
                    [code.strip() for code in args.room_codes.split(",") if code.strip()],
                    args.connections_per_room,
                    args.concurrency,
                )
            ),
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
