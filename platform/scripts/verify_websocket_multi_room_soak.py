#!/usr/bin/env python3
"""Production-safe multi-room WebSocket soak and reconnect-storm verifier.

The verifier creates short-lived public hold rooms without starting Agent,
ASR, TTS, judging, or ranking work.  Every room stays within the product's
global 5-spectator limit and all synthetic database rows are removed in ``finally``.
Run it from the API release as the platform service account.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import ssl
import time
from dataclasses import dataclass, field
from datetime import timedelta
from secrets import token_hex
from urllib.parse import urlparse

import httpx
import redis.asyncio as redis
import websockets
from app.core.config import settings
from app.core.database import SessionLocal
from app.models.entities import MatchEvent, Room, RoomSeat, User, UserSession
from app.services.realtime import SPECTATOR_GLOBAL_KEY, SPECTATOR_LEASE_SECONDS, room_hub
from app.services.room_service import append_event, load_room, now
from app.services.verification_cleanup import release_verification_room_codes
from sqlalchemy import delete, select
from websockets.exceptions import ConnectionClosed

MAX_ACTIVE_ROOMS = 5
MAX_TOTAL_SPECTATORS = 5
PASSWORD = "Websocket-soak-1234"


@dataclass
class CycleMetrics:
    cycle: int
    connect_ms: list[float] = field(default_factory=list)
    event_ms: list[float] = field(default_factory=list)
    pong_ms: list[float] = field(default_factory=list)
    graceful_closed: int = 0
    aborted: int = 0
    errors: list[str] = field(default_factory=list)
    lease_residue: dict[str, int] = field(default_factory=dict)


def csrf(client: httpx.AsyncClient) -> dict[str, str]:
    value = client.cookies.get("jixia_csrf")
    return {"X-CSRF-Token": value} if value else {}


def websocket_base(http_base: str) -> str:
    parsed = urlparse(http_base.rstrip("/"))
    return f"{'wss' if parsed.scheme == 'https' else 'ws'}://{parsed.netloc}{parsed.path.rstrip('/')}"


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))], 2)


def validate_args(args: argparse.Namespace) -> None:
    if not 1 <= args.rooms <= MAX_ACTIVE_ROOMS:
        raise SystemExit(f"--rooms must be between 1 and {MAX_ACTIVE_ROOMS}")
    if not 1 <= args.clients_per_room <= MAX_TOTAL_SPECTATORS:
        raise SystemExit(f"--clients-per-room must be between 1 and {MAX_TOTAL_SPECTATORS}")
    if args.rooms * args.clients_per_room > MAX_TOTAL_SPECTATORS:
        raise SystemExit(
            f"all rooms combined cannot exceed {MAX_TOTAL_SPECTATORS} spectators"
        )
    if not 1 <= args.cycles <= 20:
        raise SystemExit("--cycles must be between 1 and 20")
    if not 1 <= args.handshake_concurrency <= 100:
        raise SystemExit("--handshake-concurrency must be between 1 and 100")
    if not 0 <= args.slow_clients_per_room <= args.clients_per_room:
        raise SystemExit("--slow-clients-per-room must be between 0 and clients-per-room")
    if not 0 <= args.hold_seconds <= 300:
        raise SystemExit("--hold-seconds must be between 0 and 300")
    if not 0 <= args.between_cycle_seconds <= 300:
        raise SystemExit("--between-cycle-seconds must be between 0 and 300")
    if not 0 <= args.abrupt_ratio <= 1:
        raise SystemExit("--abrupt-ratio must be between 0 and 1")


async def register_and_create(
    client: httpx.AsyncClient,
    *,
    index: int,
    suffix: str,
) -> tuple[str, str]:
    registered = await client.post(
        "/api/auth/register",
        json={
            "account": f"ws_soak_{index}_{suffix}",
            "real_name": f"长连接验收选手{index + 1}",
            "password": PASSWORD,
            "confirm_password": PASSWORD,
        },
    )
    registered.raise_for_status()
    created = await client.post(
        "/api/rooms",
        headers=csrf(client) | {"X-Idempotency-Key": token_hex(16)},
        json={
            "competition_slug": "training-1v1",
            "custom_topic": f"第 {index + 1} 个房间的实时状态是否与其他房间完全隔离？",
            "seat_key": "aff_1",
            "visibility": "public",
        },
    )
    created.raise_for_status()
    return registered.json()["user"]["id"], created.json()["room"]["code"]


async def wait_for_event(socket, *, code: str, expected_seq: int, slow: bool) -> float:
    if slow:
        await asyncio.sleep(2)
    started = time.perf_counter()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        payload = json.loads(await asyncio.wait_for(socket.recv(), timeout=max(0.1, deadline - time.monotonic())))
        room = payload.get("room") or {}
        if room.get("code") != code:
            raise AssertionError(f"cross-room snapshot: expected={code} actual={room.get('code')}")
        if int(room.get("seq", -1)) >= expected_seq:
            return (time.perf_counter() - started) * 1000
    raise TimeoutError(f"room {code} did not reach seq {expected_seq}")


async def wait_for_pong(socket, *, code: str) -> float:
    started = time.perf_counter()
    await socket.send(json.dumps({"type": "ping"}))
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        payload = json.loads(await asyncio.wait_for(socket.recv(), timeout=max(0.1, deadline - time.monotonic())))
        if payload.get("type") == "pong":
            return (time.perf_counter() - started) * 1000
        room = payload.get("room") or {}
        if room and room.get("code") != code:
            raise AssertionError(f"cross-room snapshot while awaiting pong: expected={code} actual={room.get('code')}")
    raise TimeoutError(f"room {code} did not answer ping")


async def expect_overflow_rejected(ws_base: str, code: str, ssl_context: ssl.SSLContext | None) -> None:
    try:
        async with websockets.connect(
            f"{ws_base}/ws/rooms/{code}",
            ssl=ssl_context,
            open_timeout=15,
            close_timeout=3,
            ping_interval=None,
        ) as socket:
            await asyncio.wait_for(socket.recv(), timeout=10)
    except ConnectionClosed as exc:
        if exc.code == 4429:
            return
        raise AssertionError(f"overflow spectator closed with {exc.code}, expected 4429") from exc
    raise AssertionError("21st spectator was admitted")


async def wait_for_lease_cleanup(
    codes: list[str], *, timeout_seconds: float = SPECTATOR_LEASE_SECONDS + 5
) -> dict[str, int]:
    """Wait through the authoritative Redis TTL for abruptly closed sockets."""
    del codes
    client = redis.from_url(settings.redis_url, decode_responses=True)
    try:
        deadline = time.monotonic() + timeout_seconds
        residue: dict[str, int] = {}
        while True:
            now_ms = int(time.time() * 1000)
            await client.zremrangebyscore(SPECTATOR_GLOBAL_KEY, "-inf", now_ms)
            residue = {"global": int(await client.zcard(SPECTATOR_GLOBAL_KEY))}
            if not any(residue.values()) or time.monotonic() >= deadline:
                return residue
            await asyncio.sleep(0.25)
    finally:
        await client.aclose()


async def run_cycle(
    args: argparse.Namespace,
    *,
    cycle: int,
    codes: list[str],
    final_sequences: dict[str, int],
    ssl_context: ssl.SSLContext | None,
) -> CycleMetrics:
    metrics = CycleMetrics(cycle=cycle)
    slots = asyncio.Semaphore(args.handshake_concurrency)
    sockets: list[tuple[str, int, object]] = []
    ws_base = websocket_base(args.base_url)

    async def connect_one(code: str, index: int) -> None:
        started = time.perf_counter()
        try:
            async with slots:
                socket = await websockets.connect(
                    f"{ws_base}/ws/rooms/{code}",
                    ssl=ssl_context,
                    open_timeout=30,
                    close_timeout=3,
                    ping_interval=None,
                    max_size=4 * 1024 * 1024,
                )
                payload = json.loads(await asyncio.wait_for(socket.recv(), timeout=30))
            if payload.get("type") != "snapshot" or payload.get("room", {}).get("code") != code:
                raise AssertionError(f"invalid initial snapshot for {code}")
            metrics.connect_ms.append((time.perf_counter() - started) * 1000)
            sockets.append((code, index, socket))
        except Exception as exc:
            metrics.errors.append(f"connect room={code} client={index} {type(exc).__name__}: {exc}")

    try:
        await asyncio.gather(
            *(connect_one(code, index) for code in codes for index in range(args.clients_per_room))
        )
        expected = len(codes) * args.clients_per_room
        if len(sockets) != expected or metrics.errors:
            raise RuntimeError(f"only {len(sockets)}/{expected} sockets connected: {metrics.errors[:5]}")
        if len(codes) * args.clients_per_room == MAX_TOTAL_SPECTATORS:
            await expect_overflow_rejected(ws_base, codes[0], ssl_context)

        # Publish only after every socket has consumed its initial snapshot.
        for code in codes:
            await room_hub.publish(
                code,
                {"type": "soak.probe", "room_code": code, "seq": final_sequences[code]},
            )
        metrics.event_ms.extend(
            await asyncio.gather(
                *(
                    wait_for_event(
                        socket,
                        code=code,
                        expected_seq=final_sequences[code],
                        slow=index < args.slow_clients_per_room,
                    )
                    for code, index, socket in sockets
                )
            )
        )
        metrics.pong_ms.extend(
            await asyncio.gather(*(wait_for_pong(socket, code=code) for code, _index, socket in sockets))
        )
        if args.hold_seconds:
            await asyncio.sleep(args.hold_seconds)
    except Exception as exc:
        metrics.errors.append(f"cycle={cycle} {type(exc).__name__}: {exc}")
    finally:
        abrupt_count = round(len(sockets) * args.abrupt_ratio)
        graceful: list[object] = []
        for position, (_code, _index, socket) in enumerate(sockets):
            if position < abrupt_count:
                transport = getattr(socket, "transport", None)
                if transport:
                    transport.abort()
                metrics.aborted += 1
            else:
                graceful.append(socket)
                metrics.graceful_closed += 1

        async def close_one(socket) -> None:
            try:
                await asyncio.wait_for(socket.close(), timeout=5)
            except Exception:
                transport = getattr(socket, "transport", None)
                if transport:
                    transport.abort()

        if graceful:
            await asyncio.gather(*(close_one(socket) for socket in graceful))
        metrics.lease_residue = await wait_for_lease_cleanup(codes)
        await room_hub.close()
    return metrics


async def main_async(args: argparse.Namespace) -> dict:
    validate_args(args)
    suffix = f"{int(time.time())}_{token_hex(3)}"
    clients = [
        httpx.AsyncClient(base_url=args.base_url, verify=False, timeout=30, follow_redirects=True)
        for _ in range(args.rooms)
    ]
    room_ids: list[str] = []
    codes: list[str] = []
    ssl_context = None
    if websocket_base(args.base_url).startswith("wss://"):
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
    started = time.perf_counter()
    cycle_results: list[CycleMetrics] = []
    try:
        # Argon2id registration intentionally consumes substantial CPU and
        # memory.  Setup is not part of the WebSocket load target, so keep it
        # bounded instead of accidentally turning a realtime soak into an
        # authentication hash flood.
        setup_slots = asyncio.Semaphore(2)

        async def setup_room(index: int, client: httpx.AsyncClient) -> tuple[str, str]:
            async with setup_slots:
                return await register_and_create(client, index=index, suffix=suffix)

        created = await asyncio.gather(*(setup_room(index, client) for index, client in enumerate(clients)))
        codes = [item[1] for item in created]
        capacity_client = httpx.AsyncClient(
            base_url=args.base_url, verify=False, timeout=30, follow_redirects=True
        )
        clients.append(capacity_client)
        capacity_registration = await capacity_client.post(
            "/api/auth/register",
            json={
                "account": f"ws_soak_capacity_{suffix}",
                "real_name": "房间容量边界验收",
                "password": PASSWORD,
                "confirm_password": PASSWORD,
            },
        )
        capacity_registration.raise_for_status()
        capacity_response = await capacity_client.post(
            "/api/rooms",
            headers=csrf(capacity_client) | {"X-Idempotency-Key": token_hex(16)},
            json={
                "competition_slug": "training-1v1",
                "custom_topic": "第六个同时开放房间必须被服务端拒绝",
                "seat_key": "aff_1",
                "visibility": "public",
            },
        )
        if capacity_response.status_code != 409 or "5 场上限" not in capacity_response.text:
            raise AssertionError(
                f"sixth active room was not rejected: status={capacity_response.status_code} body={capacity_response.text[:200]}"
            )
        final_sequences: dict[str, int] = {}
        with SessionLocal() as db:
            for index, code in enumerate(codes):
                room = load_room(db, code, lock=True)
                room_ids.append(room.id)
                room.status = "running"
                room.started_at = now()
                room.template_snapshot = [
                    {
                        "key": "websocket_multi_room_soak",
                        "name": "实时连接隔离验收",
                        "kind": "announcement",
                        "duration": 3600,
                    }
                ]
                room.current_stage_index = 0
                room.stage_started_at = now()
                room.stage_deadline_at = now() + timedelta(seconds=3600)
                append_event(db, room, "soak.probe", {"room_index": index, "cycle": 0})
                db.flush()
                final_sequences[code] = room.seq
            db.commit()
        print(
            json.dumps(
                {
                    "type": "soak.ready",
                    "rooms": codes,
                    "peak_connections": len(codes) * args.clients_per_room,
                    "cycles": args.cycles,
                    "sixth_room_status": capacity_response.status_code,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

        for cycle in range(1, args.cycles + 1):
            with SessionLocal() as db:
                for index, code in enumerate(codes):
                    room = load_room(db, code, lock=True)
                    append_event(db, room, "soak.probe", {"room_index": index, "cycle": cycle})
                    db.flush()
                    final_sequences[code] = room.seq
                db.commit()
            result = await run_cycle(
                args,
                cycle=cycle,
                codes=codes,
                final_sequences=final_sequences,
                ssl_context=ssl_context,
            )
            cycle_results.append(result)
            print(
                json.dumps(
                    {
                        "type": "soak.cycle_complete",
                        "cycle": cycle,
                        "handshakes": len(result.connect_ms),
                        "errors": len(result.errors),
                        "lease_residue": sum(result.lease_residue.values()),
                    }
                ),
                flush=True,
            )
            if result.errors or any(result.lease_residue.values()):
                break
            if cycle < args.cycles and args.between_cycle_seconds:
                await asyncio.sleep(args.between_cycle_seconds)

        all_connect = [value for cycle in cycle_results for value in cycle.connect_ms]
        all_event = [value for cycle in cycle_results for value in cycle.event_ms]
        all_pong = [value for cycle in cycle_results for value in cycle.pong_ms]
        errors = [error for cycle in cycle_results for error in cycle.errors]
        residue = {code: count for cycle in cycle_results for code, count in cycle.lease_residue.items() if count}
        result = {
            "ok": not errors and not residue and len(cycle_results) == args.cycles,
            "rooms": len(codes),
            "clients_per_room": args.clients_per_room,
            "peak_connections": len(codes) * args.clients_per_room,
            "cycles_completed": len(cycle_results),
            "total_successful_handshakes": len(all_connect),
            "abrupt_disconnects": sum(cycle.aborted for cycle in cycle_results),
            "graceful_disconnects": sum(cycle.graceful_closed for cycle in cycle_results),
            "connect_ms": {"median": percentile(all_connect, 0.5), "p95": percentile(all_connect, 0.95), "max": percentile(all_connect, 1)},
            "event_catchup_ms": {"median": percentile(all_event, 0.5), "p95": percentile(all_event, 0.95), "max": percentile(all_event, 1)},
            "pong_ms": {"median": percentile(all_pong, 0.5), "p95": percentile(all_pong, 0.95), "max": percentile(all_pong, 1)},
            "cross_room_mismatches": sum("cross-room" in error for error in errors),
            "sixth_room_rejected": capacity_response.status_code == 409,
            "lease_residue": residue,
            "errors": errors[:20],
            "wall_seconds": round(time.perf_counter() - started, 2),
            "room_codes": codes,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not result["ok"]:
            raise RuntimeError("WebSocket multi-room soak failed")
        return result
    finally:
        await asyncio.gather(*(client.aclose() for client in clients), return_exceptions=True)
        await room_hub.close()
        # Query by this run's unique account suffix instead of relying only on
        # completed HTTP responses.  A timeout can occur after the server has
        # committed a registration, and that partial user must still be
        # removed.
        with SessionLocal() as db:
            cleanup_user_ids = list(
                db.scalars(select(User.id).where(User.account.like(f"ws_soak_%_{suffix}"))).all()
            )
            if cleanup_user_ids:
                cleanup_room_ids = list(db.scalars(select(Room.id).where(Room.owner_id.in_(cleanup_user_ids))).all())
                if cleanup_room_ids:
                    db.execute(delete(MatchEvent).where(MatchEvent.room_id.in_(cleanup_room_ids)))
                    db.execute(delete(RoomSeat).where(RoomSeat.room_id.in_(cleanup_room_ids)))
                    release_verification_room_codes(db, cleanup_room_ids)
                    db.execute(delete(Room).where(Room.id.in_(cleanup_room_ids)))
                db.execute(delete(UserSession).where(UserSession.user_id.in_(cleanup_user_ids)))
                db.execute(delete(User).where(User.id.in_(cleanup_user_ids)))
                db.commit()


def main() -> None:
    if os.geteuid() == 0:
        raise SystemExit("请使用平台服务账号运行 WebSocket 长连接验收。")
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://117.50.192.216")
    parser.add_argument("--rooms", type=int, default=MAX_ACTIVE_ROOMS)
    parser.add_argument("--clients-per-room", type=int, default=1)
    parser.add_argument("--cycles", type=int, default=3)
    parser.add_argument("--handshake-concurrency", type=int, default=50)
    parser.add_argument("--slow-clients-per-room", type=int, default=1)
    parser.add_argument("--hold-seconds", type=float, default=5)
    parser.add_argument("--between-cycle-seconds", type=float, default=0)
    parser.add_argument("--abrupt-ratio", type=float, default=0.5)
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
