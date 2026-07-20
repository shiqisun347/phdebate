#!/usr/bin/env python3
"""Verify AI substitution, read-only return routing and administrator restoration without Agent calls."""

from __future__ import annotations

import argparse
import asyncio
import os
import time
from datetime import timedelta
from secrets import token_hex

import httpx
from app.core.database import SessionLocal
from app.models.entities import AdminAuditLog, Match, MatchEvent, Room, RoomSeat, User, UserSession
from app.services.match_engine import match_engine
from app.services.room_service import load_room, now
from app.services.verification_cleanup import release_verification_room_codes
from sqlalchemy import delete

PASSWORD = "Return-substitution-1234"


def csrf(client: httpx.Client) -> dict[str, str]:
    value = client.cookies.get("jixia_csrf")
    return {"X-CSRF-Token": value} if value else {}


def register(client: httpx.Client, account: str, real_name: str) -> str:
    response = client.post(
        "/api/auth/register",
        json={
            "account": account,
            "real_name": real_name,
            "password": PASSWORD,
            "confirm_password": PASSWORD,
        },
    )
    response.raise_for_status()
    return response.json()["user"]["id"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://117.50.192.216")
    args = parser.parse_args()
    admin_account = os.environ.get("V2_ADMIN_ACCOUNT", "")
    admin_password = os.environ.get("V2_ADMIN_PASSWORD", "")
    if not admin_account or not admin_password:
        raise RuntimeError("production administrator credentials are not configured")

    suffix = f"{int(time.time())}_{token_hex(3)}"
    owner = httpx.Client(base_url=args.base_url, verify=False, timeout=20, follow_redirects=True)
    participant = httpx.Client(base_url=args.base_url, verify=False, timeout=20, follow_redirects=True)
    admin = httpx.Client(base_url=args.base_url, verify=False, timeout=20, follow_redirects=True)
    user_ids: list[str] = []
    room_id = match_id = seat_id = ""
    try:
        user_ids.append(register(owner, f"return_owner_{suffix}", "返回链路验收房主"))
        user_ids.append(register(participant, f"return_guest_{suffix}", "返回链路验收辩手"))
        admin.post("/api/auth/login", json={"account": admin_account, "password": admin_password}).raise_for_status()

        created = owner.post(
            "/api/rooms",
            headers=csrf(owner) | {"X-Idempotency-Key": token_hex(16)},
            json={
                "competition_slug": "training-1v1",
                "custom_topic": "断线超时后，用户返回比赛是否应安全进入只读观战？",
                "seat_key": "aff_1",
                "visibility": "public",
            },
        )
        created.raise_for_status()
        code = created.json()["room"]["code"]
        participant.post(
            f"/api/rooms/{code}/claim-seat",
            headers=csrf(participant),
            json={"seat_key": "neg_1"},
        ).raise_for_status()

        with SessionLocal() as db:
            room = load_room(db, code, lock=True)
            room_id = room.id
            room.status = "running"
            room.template_snapshot = [{"key": "human_hold", "name": "真人发言等待", "kind": "speech", "seat": "aff_1", "duration": 3600}]
            room.current_stage_index = 0
            room.stage_started_at = now()
            room.stage_deadline_at = now() + timedelta(seconds=3600)
            match = Match(
                room_id=room.id,
                competition_id=room.competition_id,
                season_id=room.season_id,
                status="running",
            )
            db.add(match)
            db.flush()
            match_id = match.id
            seat = next(item for item in room.seats if item.user_id == user_ids[1])
            seat_id = seat.id
            seat.connected = False
            seat.disconnected_at = now() - timedelta(seconds=61)
            assert asyncio.run(match_engine._expire_presence(db, room)) is True
            db.commit()
            assert seat.occupant_type == "ai_substitute"

        substituted = participant.get("/api/me")
        substituted.raise_for_status()
        active = next(item for item in substituted.json()["active_rooms"] if item["code"] == code)
        assert active["occupant_type"] == "ai_substitute" and active["can_resume"] is False

        restored = admin.post(f"/api/admin/rooms/{code}/seats/neg_1/restore", headers=csrf(admin), json={})
        restored.raise_for_status()
        restored_active = next(item for item in participant.get("/api/me").json()["active_rooms"] if item["code"] == code)
        assert restored_active["occupant_type"] == "human" and restored_active["can_resume"] is True
        print("return_after_substitution_verified timeout=61s watch_only=1 admin_restore=1 resume_enabled=1")
    finally:
        owner.close()
        participant.close()
        if room_id:
            with SessionLocal() as db:
                if seat_id:
                    db.execute(delete(AdminAuditLog).where(AdminAuditLog.target_id == seat_id))
                db.execute(delete(MatchEvent).where(MatchEvent.room_id == room_id))
                if match_id:
                    db.execute(delete(Match).where(Match.id == match_id))
                db.execute(delete(RoomSeat).where(RoomSeat.room_id == room_id))
                release_verification_room_codes(db, [room_id])
                db.execute(delete(Room).where(Room.id == room_id))
                db.execute(delete(UserSession).where(UserSession.user_id.in_(user_ids)))
                db.execute(delete(User).where(User.id.in_(user_ids)))
                db.commit()
        if admin.cookies.get("jixia_session"):
            admin.post("/api/auth/logout", headers=csrf(admin), json={})
        admin.close()


if __name__ == "__main__":
    main()
