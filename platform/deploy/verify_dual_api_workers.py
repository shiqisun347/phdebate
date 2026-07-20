"""Verify two API processes without exercising the frozen voice paths."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import ssl
import uuid
from urllib.parse import urlsplit, urlunsplit

import httpx
import websockets
from app.services.realtime import RoomHub


def websocket_url(base_url: str, path: str) -> str:
    parsed = urlsplit(base_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunsplit((scheme, parsed.netloc, path, "", ""))


async def health_and_database(client: httpx.AsyncClient, expected_instance: str) -> list[str]:
    live = (await client.get("/api/health/live")).raise_for_status().json()
    assert live["ok"] is True and live["instance"] == expected_instance
    database = (await client.get("/api/health")).raise_for_status().json()
    assert database["ok"] is True and database["instance"] == expected_instance
    competitions = (await client.get("/api/competitions")).raise_for_status().json()
    return sorted(str(item["slug"]) for item in competitions["items"])


async def websocket_snapshot(base_url: str, room_code: str, origin: str) -> int:
    context = None
    if base_url.startswith("https://"):
        context = ssl.create_default_context()
        if os.getenv("VERIFY_TLS", "true").lower() == "false":
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
    async with websockets.connect(
        websocket_url(base_url, f"/ws/rooms/{room_code}"),
        ssl=context,
        origin=origin,
        open_timeout=5,
        close_timeout=3,
    ) as socket:
        payload = json.loads(await asyncio.wait_for(socket.recv(), timeout=5))
        assert payload["type"] == "snapshot" and payload["room"]["code"] == room_code
        return int(payload["room"]["seq"])


async def verify_redis_coordination() -> None:
    first = RoomHub()
    second = RoomHub()
    room_code = f"9{uuid.uuid4().int % 100000:05d}"
    user_id = f"verify-{uuid.uuid4().hex}"
    first_connection = f"primary-{uuid.uuid4().hex}"
    second_connection = f"secondary-{uuid.uuid4().hex}"
    try:
        assert await first.presence_join(room_code, "aff_1", user_id, connection_id=first_connection) is True
        assert await second.presence_join(room_code, "aff_1", user_id, connection_id=second_connection) is False
        assert await first.presence_leave(room_code, "aff_1", user_id, connection_id=first_connection) is False
        assert await second.presence_active(room_code, "aff_1", user_id) is True
        assert await second.presence_leave(room_code, "aff_1", user_id, connection_id=second_connection) is True
        assert await first.presence_active(room_code, "aff_1", user_id) is False
    finally:
        await first.close()
        await second.close()


async def run(args: argparse.Namespace) -> dict:
    assert args.primary.rstrip("/") != args.secondary.rstrip("/"), "worker ports must be isolated"
    limits = httpx.Limits(max_keepalive_connections=0)
    async with (
        httpx.AsyncClient(base_url=args.primary, timeout=5, limits=limits, verify=args.verify_tls) as primary,
        httpx.AsyncClient(base_url=args.secondary, timeout=5, limits=limits, verify=args.verify_tls) as secondary,
    ):
        primary_competitions, secondary_competitions = await asyncio.gather(
            health_and_database(primary, "api-primary"),
            health_and_database(secondary, "api-secondary"),
        )
    assert primary_competitions == secondary_competitions, "workers do not observe the same database state"
    await verify_redis_coordination()

    websocket_sequences = None
    if args.room_code:
        websocket_sequences = await asyncio.gather(
            websocket_snapshot(args.primary, args.room_code, args.origin),
            websocket_snapshot(args.secondary, args.room_code, args.origin),
        )
        assert websocket_sequences[0] == websocket_sequences[1]

    return {
        "ok": True,
        "ports_isolated": True,
        "rest": ["api-primary", "api-secondary"],
        "shared_database": True,
        "redis_presence_atomic": True,
        "websocket_sequences": websocket_sequences,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--primary", default="http://127.0.0.1:12340")
    parser.add_argument("--secondary", default="http://127.0.0.1:12342")
    parser.add_argument("--origin", default="https://117.50.192.216")
    parser.add_argument("--room-code")
    parser.add_argument("--no-verify-tls", dest="verify_tls", action="store_false")
    parser.set_defaults(verify_tls=True)
    print(json.dumps(asyncio.run(run(parser.parse_args())), ensure_ascii=False))


if __name__ == "__main__":
    main()
