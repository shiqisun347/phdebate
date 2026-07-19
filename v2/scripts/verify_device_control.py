#!/usr/bin/env python3
"""Verify live two-device seat takeover without invoking any Agent provider."""

from __future__ import annotations

import argparse
import time
from datetime import timedelta
from secrets import token_hex

import httpx
from app.core.database import SessionLocal
from app.models.entities import AudioAsset, JudgeScorecard, Match, MatchEvent, RatingChange, Room, RoomSeat, Speech, User, UserSession
from app.services.room_service import load_room, now
from app.services.verification_cleanup import release_verification_room_codes
from sqlalchemy import delete


def csrf(client: httpx.Client) -> dict[str, str]:
    value = client.cookies.get("jixia_v2_csrf")
    return {"X-CSRF-Token": value} if value else {}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://117.50.192.216")
    args = parser.parse_args()
    account = f"device_{int(time.time())}_{token_hex(3)}"
    password = "Device-control-1234"
    user_id = ""
    room_id = ""
    match_id = ""
    primary = httpx.Client(base_url=args.base_url, verify=False, timeout=20, follow_redirects=True)
    secondary = httpx.Client(base_url=args.base_url, verify=False, timeout=20, follow_redirects=True)
    try:
        registered = primary.post(
            "/api/auth/register",
            json={
                "account": account,
                "real_name": "设备接管验收用户",
                "password": password,
                "confirm_password": password,
            },
        )
        registered.raise_for_status()
        user_id = registered.json()["user"]["id"]
        secondary.post("/api/auth/login", json={"account": account, "password": password}).raise_for_status()
        created = primary.post(
            "/api/rooms",
            headers=csrf(primary) | {"X-Idempotency-Key": token_hex(16)},
            json={
                "competition_slug": "training-1v1",
                "custom_topic": "同一席位是否应限制为单设备控制？",
                "seat_key": "aff_1",
                "visibility": "public",
            },
        )
        created.raise_for_status()
        code = created.json()["room"]["code"]
        with SessionLocal() as db:
            room = load_room(db, code, lock=True)
            room_id = room.id
            room.status = "running"
            room.current_stage_index = 1
            room.stage_started_at = now()
            room.stage_deadline_at = now() + timedelta(seconds=180)
            match = Match(
                room_id=room.id,
                competition_id=room.competition_id,
                season_id=room.season_id,
                status="running",
            )
            db.add(match)
            db.commit()
            match_id = match.id

        assert primary.post(f"/api/rooms/{code}/speech/start", headers=csrf(primary), json={}).status_code == 409
        acquired_a = primary.post(
            f"/api/rooms/{code}/control-lease",
            headers=csrf(primary) | {"X-Control-Lease": "live-device-a"},
            json={},
        )
        acquired_a.raise_for_status()
        started_a = primary.post(
            f"/api/rooms/{code}/speech/start",
            headers=csrf(primary) | {"X-Control-Lease": "live-device-a"},
            json={},
        )
        started_a.raise_for_status()
        recovered_a = primary.post(
            f"/api/rooms/{code}/speech/start",
            headers=csrf(primary) | {"X-Control-Lease": "live-device-a", "X-Idempotency-Key": "live-recover-existing"},
            json={},
        )
        recovered_a.raise_for_status()
        assert recovered_a.json()["resumed"] is True
        assert recovered_a.json()["speech_id"] == started_a.json()["speech_id"]
        assert (
            secondary.post(
                f"/api/rooms/{code}/control-lease",
                headers=csrf(secondary) | {"X-Control-Lease": "live-device-b"},
                json={},
            ).status_code
            == 409
        )
        primary.post(
            f"/api/rooms/{code}/speech/finish",
            headers=csrf(primary) | {"X-Control-Lease": "live-device-a"},
            json={"speech_id": started_a.json()["speech_id"], "content": "设备 A 完成发言。"},
        ).raise_for_status()
        with SessionLocal() as db:
            room = load_room(db, code, lock=True)
            room.current_stage_index = 1
            room.stage_started_at = now()
            room.stage_deadline_at = now() + timedelta(seconds=180)
            db.commit()
        assert (
            secondary.post(
                f"/api/rooms/{code}/control-lease",
                headers=csrf(secondary) | {"X-Control-Lease": "live-device-b"},
                json={},
            ).status_code
            == 409
        )
        acquired_b = secondary.post(
            f"/api/rooms/{code}/control-lease",
            headers=csrf(secondary) | {"X-Control-Lease": "live-device-b"},
            json={"force": True},
        )
        acquired_b.raise_for_status()
        assert (
            primary.post(
                f"/api/rooms/{code}/speech/start",
                headers=csrf(primary) | {"X-Control-Lease": "live-device-a"},
                json={},
            ).status_code
            == 409
        )
        secondary.post(
            f"/api/rooms/{code}/speech/start",
            headers=csrf(secondary) | {"X-Control-Lease": "live-device-b"},
            json={},
        ).raise_for_status()
        print(
            "device_control_verified missing=409 same_device_recovery=200 "
            "active_takeover=409 explicit_takeover=200 stale_device=409 current_device=200"
        )
    finally:
        primary.close()
        secondary.close()
        if room_id:
            with SessionLocal() as db:
                if match_id:
                    db.execute(delete(RatingChange).where(RatingChange.match_id == match_id))
                    db.execute(delete(JudgeScorecard).where(JudgeScorecard.match_id == match_id))
                    db.execute(delete(AudioAsset).where(AudioAsset.match_id == match_id))
                    db.execute(delete(Speech).where(Speech.match_id == match_id))
                db.execute(delete(MatchEvent).where(MatchEvent.room_id == room_id))
                if match_id:
                    db.execute(delete(Match).where(Match.id == match_id))
                db.execute(delete(RoomSeat).where(RoomSeat.room_id == room_id))
                release_verification_room_codes(db, [room_id])
                db.execute(delete(Room).where(Room.id == room_id))
                db.execute(delete(UserSession).where(UserSession.user_id == user_id))
                db.execute(delete(User).where(User.id == user_id))
                db.commit()


if __name__ == "__main__":
    main()
