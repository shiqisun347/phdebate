#!/usr/bin/env python3
"""Verify abandoned lobby ownership is transferred or the empty lobby is cancelled."""

from __future__ import annotations

import argparse
import time
from datetime import timedelta
from secrets import token_hex

import httpx
from app.core.database import SessionLocal
from app.models.entities import MatchEvent, Room, RoomSeat, User, UserSession
from app.services.room_service import load_room, now
from app.services.verification_cleanup import release_verification_room_codes
from sqlalchemy import delete


def csrf(client: httpx.Client) -> dict[str, str]:
    value = client.cookies.get("jixia_csrf")
    return {"X-CSRF-Token": value} if value else {}


def create_room(client: httpx.Client, topic: str) -> dict:
    response = client.post(
        "/api/rooms",
        headers=csrf(client) | {"X-Idempotency-Key": token_hex(16)},
        json={
            "competition_slug": "training-1v1",
            "custom_topic": topic,
            "seat_key": "aff_1",
            "visibility": "public",
        },
    )
    response.raise_for_status()
    return response.json()["room"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://117.50.192.216")
    args = parser.parse_args()
    suffix = f"{int(time.time())}_{token_hex(3)}"
    password = "Abandoned-lobby-1234"
    user_ids: list[str] = []
    room_ids: list[str] = []
    client = httpx.Client(base_url=args.base_url, verify=False, timeout=20, follow_redirects=True)
    guest = httpx.Client(base_url=args.base_url, verify=False, timeout=20, follow_redirects=True)
    try:
        registered = client.post(
            "/api/auth/register",
            json={
                "account": f"abandoned_{suffix}",
                "real_name": "废弃房间回收验收",
                "password": password,
                "confirm_password": password,
            },
        )
        registered.raise_for_status()
        user_ids.append(registered.json()["user"]["id"])
        guest_registered = guest.post(
            "/api/auth/register",
            json={
                "account": f"abandoned_guest_{suffix}",
                "real_name": "废弃房间在线参与者",
                "password": password,
                "confirm_password": password,
            },
        )
        guest_registered.raise_for_status()
        user_ids.append(guest_registered.json()["user"]["id"])
        first = create_room(client, "房主离线后是否应释放废弃房间？")
        claimed = guest.post(
            f"/api/rooms/{first['code']}/claim-seat",
            headers=csrf(guest),
            json={"seat_key": "neg_1"},
        )
        claimed.raise_for_status()
        with SessionLocal() as db:
            room = load_room(db, first["code"], lock=True)
            room_ids.append(room.id)
            owner_seat = next(item for item in room.seats if item.user_id == user_ids[0])
            guest_seat = next(item for item in room.seats if item.user_id == user_ids[1])
            owner_seat.connected = False
            owner_seat.disconnected_at = now() - timedelta(seconds=121)
            guest_seat.connected = True
            guest_seat.disconnected_at = None
            db.commit()

        deadline = time.monotonic() + 10
        transferred = None
        while time.monotonic() < deadline:
            transferred = client.get(f"/api/rooms/{first['code']}").json()["room"]
            if transferred["owner"]["id"] == user_ids[1]:
                break
            time.sleep(0.25)
        assert transferred and transferred["status"] == "lobby"
        assert transferred["owner"]["id"] == user_ids[1]
        assert next(seat for seat in transferred["seats"] if seat["seat_key"] == "aff_1")["occupant_type"] == "open"
        assert next(seat for seat in transferred["seats"] if seat["seat_key"] == "neg_1")["is_owner"] is True
        assert guest.post(f"/api/rooms/{first['code']}/cancel", headers=csrf(guest), json={}).status_code == 200

        second = create_room(client, "没有在线接任者时是否自动取消废弃房间？")
        with SessionLocal() as db:
            second_room = load_room(db, second["code"])
            room_ids.append(second_room.id)
            owner_seat = next(item for item in second_room.seats if item.user_id == user_ids[0])
            owner_seat.connected = False
            owner_seat.disconnected_at = now() - timedelta(seconds=121)
            db.commit()

        deadline = time.monotonic() + 10
        cancelled = None
        while time.monotonic() < deadline:
            cancelled = client.get(f"/api/rooms/{second['code']}").json()["room"]
            if cancelled["status"] == "cancelled":
                break
            time.sleep(0.25)
        assert cancelled and cancelled["status"] == "cancelled"
        assert next(seat for seat in cancelled["seats"] if seat["seat_key"] == "aff_1")["occupant_type"] == "open"
        assert client.post(f"/api/rooms/{second['code']}/control/terminate", headers=csrf(client), json={}).status_code == 409

        third = create_room(guest, "废弃房间取消后是否能立即创建新比赛？")
        with SessionLocal() as db:
            room_ids.append(load_room(db, third["code"]).id)
        assert third["status"] == "lobby" and third["code"] not in {first["code"], second["code"]}
        print(
            "abandoned_lobby_verified owner_released=120s online_successor=transferred empty_room=cancelled new_room=created"
        )
    finally:
        client.close()
        guest.close()
        if room_ids:
            with SessionLocal() as db:
                db.execute(delete(MatchEvent).where(MatchEvent.room_id.in_(room_ids)))
                db.execute(delete(RoomSeat).where(RoomSeat.room_id.in_(room_ids)))
                release_verification_room_codes(db, room_ids)
                db.execute(delete(Room).where(Room.id.in_(room_ids)))
                db.execute(delete(UserSession).where(UserSession.user_id.in_(user_ids)))
                db.execute(delete(User).where(User.id.in_(user_ids)))
                db.commit()


if __name__ == "__main__":
    main()
