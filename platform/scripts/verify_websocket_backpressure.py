#!/usr/bin/env python3
"""Exercise anonymous WebSocket fanout, slow consumers, and burst catch-up."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import ssl
import statistics
import time
from dataclasses import dataclass, field
from datetime import timedelta
from secrets import token_hex
from urllib.parse import urlparse

import httpx
import websockets
from app.core.database import SessionLocal
from app.models.entities import MatchEvent, Room, RoomSeat, User, UserSession
from app.services.realtime import room_hub
from app.services.room_service import append_event, load_room, now
from app.services.verification_cleanup import release_verification_room_codes
from sqlalchemy import delete, select
from websockets.exceptions import ConnectionClosed

PASSWORD = "Websocket-load-1234"


@dataclass
class Metrics:
    connect_ms: list[float] = field(default_factory=list)
    catchup_ms: list[float] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def csrf(client: httpx.AsyncClient) -> dict[str, str]:
    value = client.cookies.get("jixia_csrf")
    return {"X-CSRF-Token": value} if value else {}


def websocket_base(http_base: str) -> str:
    parsed = urlparse(http_base.rstrip("/"))
    return f"{'wss' if parsed.scheme == 'https' else 'ws'}://{parsed.netloc}{parsed.path.rstrip('/')}"


async def watcher(
    index: int,
    *,
    ws_base: str,
    code: str,
    final_seq: int,
    slow_clients: int,
    ready: asyncio.Queue,
    start: asyncio.Event,
    metrics: Metrics,
) -> None:
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    connected_at = time.perf_counter()
    try:
        async with websockets.connect(
            f"{ws_base}/ws/rooms/{code}",
            ssl=ssl_context,
            open_timeout=30,
            close_timeout=3,
            ping_interval=None,
            max_queue=1,
            max_size=4 * 1024 * 1024,
        ) as socket:
            initial = json.loads(await asyncio.wait_for(socket.recv(), timeout=30))
            if initial.get("type") != "snapshot" or initial.get("room", {}).get("code") != code:
                raise RuntimeError("invalid initial snapshot")
            metrics.connect_ms.append((time.perf_counter() - connected_at) * 1000)
            await ready.put(None)
            await start.wait()
            await socket.send(json.dumps({"type": "ping"}))
            if index < slow_clients:
                await asyncio.sleep(2)
            catchup_started = time.perf_counter()
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                payload = json.loads(await asyncio.wait_for(socket.recv(), timeout=max(0.1, deadline - time.monotonic())))
                event = payload.get("event") or {}
                if event.get("type") == "load.probe" and event.get("seq") == final_seq:
                    metrics.catchup_ms.append((time.perf_counter() - catchup_started) * 1000)
                    return
            raise TimeoutError("final burst snapshot not received")
    except Exception as exc:
        metrics.errors.append(f"client={index} {type(exc).__name__}: {exc}")
        await ready.put(metrics.errors[-1])


async def expect_spectator_limit(ws_base: str, code: str) -> None:
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    try:
        async with websockets.connect(
            f"{ws_base}/ws/rooms/{code}",
            ssl=ssl_context,
            open_timeout=30,
            close_timeout=3,
            ping_interval=None,
            max_size=4 * 1024 * 1024,
        ) as socket:
            await asyncio.wait_for(socket.recv(), timeout=10)
    except ConnectionClosed as exc:
        if exc.code == 4429:
            return
        raise RuntimeError(f"overflow spectator closed with unexpected code {exc.code}") from exc
    raise RuntimeError("overflow spectator was admitted beyond the 20-person room limit")


async def verify_released_slot(ws_base: str, code: str) -> None:
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    async with websockets.connect(
        f"{ws_base}/ws/rooms/{code}",
        ssl=ssl_context,
        open_timeout=30,
        close_timeout=3,
        ping_interval=None,
        max_size=4 * 1024 * 1024,
    ) as socket:
        initial = json.loads(await asyncio.wait_for(socket.recv(), timeout=10))
        if initial.get("type") != "snapshot" or initial.get("room", {}).get("code") != code:
            raise RuntimeError("released spectator slot did not admit a valid replacement")


async def main_async(args: argparse.Namespace) -> None:
    client = httpx.AsyncClient(base_url=args.base_url, verify=False, timeout=30, follow_redirects=True)
    user_id = ""
    try:
        suffix = f"{int(time.time())}_{token_hex(3)}"
        registered = await client.post(
            "/api/auth/register",
            json={
                "account": f"wsload_{suffix}",
                "real_name": "WebSocket 压力验收选手",
                "password": PASSWORD,
                "confirm_password": PASSWORD,
            },
        )
        registered.raise_for_status()
        user_id = registered.json()["user"]["id"]
        created = await client.post(
            "/api/rooms",
            headers=csrf(client) | {"X-Idempotency-Key": token_hex(16)},
            json={
                "competition_slug": "training-1v1",
                "custom_topic": "大量观众与慢客户端能否及时追上最新比赛状态？",
                "seat_key": "aff_1",
                "visibility": "public",
            },
        )
        created.raise_for_status()
        code = created.json()["room"]["code"]
        with SessionLocal() as db:
            room = load_room(db, code, lock=True)
            # Public lobbies intentionally require authentication because they
            # expose student names and readiness.  Anonymous fanout begins only
            # after a match is underway.  Hold a non-provider announcement
            # stage so this load gate exercises spectator backpressure without
            # entering Agent/TTS execution.
            room.status = "running"
            room.started_at = now()
            room.template_snapshot = [
                {
                    "key": "websocket_load_hold",
                    "name": "WebSocket 负载验收",
                    "kind": "announcement",
                    "duration": 3600,
                }
            ]
            room.current_stage_index = 0
            room.stage_started_at = now()
            room.stage_deadline_at = now() + timedelta(seconds=3600)
            for index in range(args.events):
                append_event(db, room, "load.probe", {"index": index + 1})
            db.commit()
            final_seq = room.seq

        ready: asyncio.Queue = asyncio.Queue()
        start = asyncio.Event()
        metrics = Metrics()
        tasks = [
            asyncio.create_task(
                watcher(
                    index,
                    ws_base=websocket_base(args.base_url),
                    code=code,
                    final_seq=final_seq,
                    slow_clients=args.slow_clients,
                    ready=ready,
                    start=start,
                    metrics=metrics,
                )
            )
            for index in range(args.clients)
        ]
        readiness = [await asyncio.wait_for(ready.get(), timeout=60) for _ in range(args.clients)]
        early_errors = [item for item in readiness if item]
        if early_errors:
            raise RuntimeError(f"WebSocket initial connection failures: {early_errors[:5]}")
        overflow_verified = args.clients == 20
        if overflow_verified:
            await expect_spectator_limit(websocket_base(args.base_url), code)
        start.set()
        await asyncio.sleep(0.1)
        for seq in range(final_seq - args.events + 1, final_seq + 1):
            await room_hub.publish(code, {"type": "load.probe", "room_code": code, "seq": seq})
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=45)
        if metrics.errors:
            raise RuntimeError(f"WebSocket watcher failures: {metrics.errors[:5]}")
        await verify_released_slot(websocket_base(args.base_url), code)
        ordered_connect = sorted(metrics.connect_ms)
        ordered_catchup = sorted(metrics.catchup_ms)
        p95_index = max(0, int(len(ordered_connect) * 0.95) - 1)
        print(
            "websocket_backpressure_verified "
            f"clients={args.clients} slow_clients={args.slow_clients} events={args.events} "
            f"connected={len(ordered_connect)} caught_up={len(ordered_catchup)} "
            f"connect_median_ms={statistics.median(ordered_connect):.1f} "
            f"connect_p95_ms={ordered_connect[p95_index]:.1f} "
            f"catchup_max_ms={max(ordered_catchup):.1f} "
            f"overflow_close={'4429' if overflow_verified else 'not_checked'} released_slot=admitted errors=0"
        )
    finally:
        await client.aclose()
        await room_hub.close()
        if user_id:
            with SessionLocal() as db:
                cleanup_room_ids = list(db.scalars(select(Room.id).where(Room.owner_id == user_id)).all())
                db.execute(delete(MatchEvent).where(MatchEvent.room_id.in_(cleanup_room_ids)))
                db.execute(delete(RoomSeat).where(RoomSeat.room_id.in_(cleanup_room_ids)))
                release_verification_room_codes(db, cleanup_room_ids)
                db.execute(delete(Room).where(Room.id.in_(cleanup_room_ids)))
                db.execute(delete(UserSession).where(UserSession.user_id == user_id))
                db.execute(delete(User).where(User.id == user_id))
                db.commit()


def main() -> None:
    if os.geteuid() == 0:
        raise SystemExit("请使用 平台 服务账号运行 WebSocket 压力验收。")
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://117.50.192.216")
    parser.add_argument("--clients", type=int, default=20)
    parser.add_argument("--slow-clients", type=int, default=5)
    parser.add_argument("--events", type=int, default=64)
    args = parser.parse_args()
    if not 1 <= args.clients <= 20 or not 0 <= args.slow_clients <= args.clients or args.events < 1:
        raise SystemExit("clients 必须位于 1..20，events 必须为正数，slow-clients 必须位于 0..clients。")
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
