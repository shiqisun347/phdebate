"""Production-safe dual-API WebSocket presence verifier.

Creates one disposable lobby and user, connects the same seat directly to both
API workers, and proves Redis emits one connected and one disconnected
database transition. No match is started and no audio endpoint is touched.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import httpx
import redis.asyncio as redis
import websockets
from app.core.config import settings
from app.core.database import SessionLocal
from app.models.entities import MatchEvent, Room, RoomSeat, User, UserSession
from app.services.realtime import PRESENCE_KEY_PREFIX, RoomHub
from app.services.room_service import load_room
from app.services.verification_cleanup import release_verification_room_codes
from sqlalchemy import delete, func, select

PASSWORD = "Verify-prod-1234"


async def wait_for_state(code: str, *, connected: bool, timeout: float = 5) -> tuple[int, int]:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        with SessionLocal() as db:
            room = load_room(db, code)
            seat = next(item for item in room.seats if item.seat_key == "aff_1")
            connected_events = int(
                db.scalar(
                    select(func.count(MatchEvent.id)).where(
                        MatchEvent.room_id == room.id,
                        MatchEvent.event_type == "presence.connected",
                    )
                )
                or 0
            )
            disconnected_events = int(
                db.scalar(
                    select(func.count(MatchEvent.id)).where(
                        MatchEvent.room_id == room.id,
                        MatchEvent.event_type == "presence.disconnected",
                    )
                )
                or 0
            )
            if seat.connected is connected:
                return connected_events, disconnected_events
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError(f"seat did not reach connected={connected}")
        await asyncio.sleep(0.05)


async def run() -> dict:
    client = httpx.Client(base_url="http://127.0.0.1:12340", timeout=10)
    account = f"dual_presence_{uuid.uuid4().hex[:12]}"
    user_id = room_id = code = None
    first = second = None
    redis_client: redis.Redis | None = None
    try:
        registered = client.post(
            "/api/auth/register",
            json={
                "account": account,
                "real_name": "双 API 在线状态验证用户",
                "password": PASSWORD,
                "confirm_password": PASSWORD,
            },
        )
        registered.raise_for_status()
        user_id = registered.json()["user"]["id"]
        csrf = client.cookies["jixia_v2_csrf"]
        cookie = "; ".join(f"{key}={value}" for key, value in client.cookies.items())
        created = client.post(
            "/api/rooms",
            headers={"X-CSRF-Token": csrf, "Cookie": cookie},
            json={
                "competition_slug": "training-1v1",
                "custom_topic": "多实例在线状态是否应该保持一致？",
                "seat_key": "aff_1",
                "visibility": "public",
            },
        )
        created.raise_for_status()
        room = created.json()["room"]
        room_id, code = room["id"], room["code"]
        headers = {"Cookie": cookie}
        first = await websockets.connect(
            f"ws://127.0.0.1:12340/ws/rooms/{code}",
            origin="https://117.50.192.216",
            additional_headers=headers,
            open_timeout=5,
            close_timeout=3,
        )
        first_snapshot = json.loads(await asyncio.wait_for(first.recv(), timeout=5))
        assert first_snapshot["room"]["code"] == code
        assert await wait_for_state(code, connected=True) == (1, 0)

        second = await websockets.connect(
            f"ws://127.0.0.1:12342/ws/rooms/{code}",
            origin="https://117.50.192.216",
            additional_headers=headers,
            open_timeout=5,
            close_timeout=3,
        )
        second_snapshot = json.loads(await asyncio.wait_for(second.recv(), timeout=5))
        assert second_snapshot["room"]["code"] == code
        assert await wait_for_state(code, connected=True) == (1, 0)

        with SessionLocal() as db:
            loaded = load_room(db, code)
            seat = next(item for item in loaded.seats if item.seat_key == "aff_1")
            member = RoomHub._presence_member(code, seat.seat_key, str(seat.user_id))
        redis_client = redis.from_url(settings.redis_url, decode_responses=True)
        lease_count_with_two_workers = int(await redis_client.zcard(f"{PRESENCE_KEY_PREFIX}{member}"))
        assert lease_count_with_two_workers == 2

        await first.close()
        first = None
        await asyncio.sleep(0.15)
        assert await wait_for_state(code, connected=True) == (1, 0)
        assert int(await redis_client.zcard(f"{PRESENCE_KEY_PREFIX}{member}")) == 1

        await second.close()
        second = None
        assert await wait_for_state(code, connected=False) == (1, 1)
        assert int(await redis_client.zcard(f"{PRESENCE_KEY_PREFIX}{member}")) == 0
        return {
            "ok": True,
            "room_code": code,
            "direct_workers": ["api-primary", "api-secondary"],
            "lease_count_with_two_workers": lease_count_with_two_workers,
            "connected_events": 1,
            "intermediate_disconnect_events": 0,
            "disconnected_events": 1,
            "audio_paths_touched": False,
        }
    finally:
        if first:
            await first.close()
        if second:
            await second.close()
        if redis_client:
            await redis_client.aclose()
        client.close()
        with SessionLocal() as db:
            if room_id:
                db.execute(delete(MatchEvent).where(MatchEvent.room_id == room_id))
                db.execute(delete(RoomSeat).where(RoomSeat.room_id == room_id))
                release_verification_room_codes(db, [room_id])
                db.execute(delete(Room).where(Room.id == room_id))
            if user_id:
                db.execute(delete(UserSession).where(UserSession.user_id == user_id))
                db.execute(delete(User).where(User.id == user_id))
            db.commit()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run()), ensure_ascii=False))
