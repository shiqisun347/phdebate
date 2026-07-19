#!/usr/bin/env python3
"""Verify live WebSockets bind presence when a user claims a seat later."""

from __future__ import annotations

import argparse
import asyncio
import json
import ssl
import time
from secrets import token_hex

import httpx
import websockets
from app.core.database import SessionLocal
from app.models.entities import MatchEvent, Room, RoomSeat, User, UserSession
from app.services.verification_cleanup import release_verification_room_codes
from sqlalchemy import delete, or_, select

PASSWORD = "Dynamic-presence-1234"


def csrf(client: httpx.AsyncClient) -> dict[str, str]:
    value = client.cookies.get("jixia_v2_csrf")
    return {"X-CSRF-Token": value} if value else {}


async def register(client: httpx.AsyncClient, account: str, name: str) -> str:
    response = await client.post(
        "/api/auth/register",
        json={"account": account, "real_name": name, "password": PASSWORD, "confirm_password": PASSWORD},
    )
    response.raise_for_status()
    return response.json()["user"]["id"]


async def receive_bound(socket, seat_key: str) -> dict:
    for _ in range(4):
        message = json.loads(await asyncio.wait_for(socket.recv(), timeout=10))
        room = message["room"]
        if room["my_seat"] == seat_key and next(item for item in room["seats"] if item["is_me"])["connected"]:
            return room
    raise RuntimeError("seat presence did not bind to the existing websocket")


async def main_async(base_url: str) -> None:
    suffix = f"{int(time.time())}_{token_hex(3)}"
    accounts = [f"presence_owner_{suffix}", f"presence_user_{suffix}"]
    owner = httpx.AsyncClient(base_url=base_url, verify=False, timeout=20, follow_redirects=True)
    participant = httpx.AsyncClient(base_url=base_url, verify=False, timeout=20, follow_redirects=True)
    user_ids: list[str] = []
    try:
        user_ids.append(await register(owner, accounts[0], "动态在线房主"))
        user_ids.append(await register(participant, accounts[1], "动态在线参与者"))
        created = await owner.post(
            "/api/rooms",
            headers=csrf(owner) | {"X-Idempotency-Key": token_hex(16)},
            json={
                "competition_slug": "training-1v1",
                "custom_topic": "认领席位后是否应立即同步在线状态？",
                "seat_key": "aff_1",
                "visibility": "public",
            },
        )
        created.raise_for_status()
        code = created.json()["room"]["code"]
        cookie_header = "; ".join(f"{key}={value}" for key, value in participant.cookies.items())
        ws_url = base_url.replace("https://", "wss://").replace("http://", "ws://") + f"/ws/rooms/{code}"
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        async with websockets.connect(ws_url, ssl=ssl_context, additional_headers={"Cookie": cookie_header}) as first:
            assert json.loads(await first.recv())["room"]["my_seat"] is None
            async with websockets.connect(ws_url, ssl=ssl_context, additional_headers={"Cookie": cookie_header}) as second:
                assert json.loads(await second.recv())["room"]["my_seat"] is None
                claimed = await participant.post(
                    f"/api/rooms/{code}/claim-seat",
                    headers=csrf(participant),
                    json={"seat_key": "neg_1"},
                )
                claimed.raise_for_status()
                await asyncio.gather(receive_bound(first, "neg_1"), receive_bound(second, "neg_1"))
            for _ in range(20):
                view = (await participant.get(f"/api/rooms/{code}")).json()["room"]
                if next(item for item in view["seats"] if item["is_me"])["connected"]:
                    break
                await asyncio.sleep(0.05)
            assert next(item for item in view["seats"] if item["is_me"])["connected"] is True
        for _ in range(20):
            view = (await participant.get(f"/api/rooms/{code}")).json()["room"]
            if not next(item for item in view["seats"] if item["is_me"])["connected"]:
                break
            await asyncio.sleep(0.05)
        assert next(item for item in view["seats"] if item["is_me"])["connected"] is False
        print("dynamic_presence_verified preclaim_sockets=2 after_one_close=online after_last_close=offline")
    finally:
        await owner.aclose()
        await participant.aclose()
        if user_ids:
            with SessionLocal() as db:
                room_ids = list(
                    db.scalars(
                        select(Room.id)
                        .outerjoin(RoomSeat, RoomSeat.room_id == Room.id)
                        .where(or_(Room.owner_id.in_(user_ids), RoomSeat.user_id.in_(user_ids)))
                        .distinct()
                    ).all()
                )
                if room_ids:
                    db.execute(delete(MatchEvent).where(MatchEvent.room_id.in_(room_ids)))
                    db.execute(delete(RoomSeat).where(RoomSeat.room_id.in_(room_ids)))
                    release_verification_room_codes(db, room_ids)
                    db.execute(delete(Room).where(Room.id.in_(room_ids)))
                db.execute(delete(UserSession).where(UserSession.user_id.in_(user_ids)))
                db.execute(delete(User).where(User.id.in_(user_ids)))
                db.commit()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://117.50.192.216")
    args = parser.parse_args()
    asyncio.run(main_async(args.base_url.rstrip("/")))


if __name__ == "__main__":
    main()
