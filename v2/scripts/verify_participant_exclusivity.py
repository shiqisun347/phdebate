#!/usr/bin/env python3
"""Verify PostgreSQL participant locking with disposable live API users."""

from __future__ import annotations

import argparse
import asyncio
import time
from secrets import token_hex

import httpx
from app.core.database import SessionLocal
from app.models.entities import MatchEvent, Room, RoomSeat, User, UserSession
from app.services.verification_cleanup import release_verification_room_codes
from sqlalchemy import delete, or_, select

PASSWORD = "Participant-lock-1234"


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


async def create_room(client: httpx.AsyncClient, topic: str) -> httpx.Response:
    return await client.post(
        "/api/rooms",
        headers=csrf(client) | {"X-Idempotency-Key": token_hex(16)},
        json={
            "competition_slug": "training-1v1",
            "custom_topic": topic,
            "seat_key": "aff_1",
            "visibility": "public",
        },
    )


async def main_async(base_url: str) -> None:
    suffix = f"{int(time.time())}_{token_hex(3)}"
    accounts = [f"lock_participant_{suffix}", f"lock_owner_a_{suffix}", f"lock_owner_b_{suffix}"]
    clients = [httpx.AsyncClient(base_url=base_url, verify=False, timeout=30, follow_redirects=True) for _ in range(4)]
    user_ids: list[str] = []
    try:
        participant, participant_second, owner_a, owner_b = clients
        user_ids.append(await register(participant, accounts[0], "并发参与者"))
        user_ids.append(await register(owner_a, accounts[1], "并发房主甲"))
        user_ids.append(await register(owner_b, accounts[2], "并发房主乙"))
        (await participant_second.post("/api/auth/login", json={"account": accounts[0], "password": PASSWORD})).raise_for_status()

        room_a_response, room_b_response = await asyncio.gather(
            create_room(owner_a, "并发抢座房间甲"),
            create_room(owner_b, "并发抢座房间乙"),
        )
        room_a_response.raise_for_status()
        room_b_response.raise_for_status()
        room_a = room_a_response.json()["room"]["code"]
        room_b = room_b_response.json()["room"]["code"]

        claims = await asyncio.gather(
            participant.post(
                f"/api/rooms/{room_a}/claim-seat",
                headers=csrf(participant),
                json={"seat_key": "neg_1"},
            ),
            participant_second.post(
                f"/api/rooms/{room_b}/claim-seat",
                headers=csrf(participant_second),
                json={"seat_key": "neg_1"},
            ),
        )
        assert sorted(item.status_code for item in claims) == [200, 409]
        winning_room = room_a if claims[0].status_code == 200 else room_b
        winning_client = participant if claims[0].status_code == 200 else participant_second
        me = await participant.get("/api/me")
        me.raise_for_status()
        assert me.json()["summary"]["active_total"] == 1
        (await winning_client.post(f"/api/rooms/{winning_room}/release-seat", headers=csrf(winning_client), json={})).raise_for_status()

        creations = await asyncio.gather(
            create_room(participant, "并发建房尝试甲"),
            create_room(participant_second, "并发建房尝试乙"),
        )
        assert sorted(item.status_code for item in creations) == [200, 409]
        created = next(item for item in creations if item.status_code == 200)
        created_code = created.json()["room"]["code"]
        (await participant.post(f"/api/rooms/{created_code}/cancel", headers=csrf(participant), json={})).raise_for_status()
        me_after = await participant.get("/api/me")
        me_after.raise_for_status()
        assert me_after.json()["summary"]["active_total"] == 0
        print("participant_exclusivity_verified claims=1/2 creates=1/2 active_after=0")
    finally:
        await asyncio.gather(*(client.aclose() for client in clients))
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
    asyncio.run(main_async(args.base_url))


if __name__ == "__main__":
    main()
