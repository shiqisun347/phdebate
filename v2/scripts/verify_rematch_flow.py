#!/usr/bin/env python3
"""Verify the live terminal-match rematch flow with disposable QA data."""

from __future__ import annotations

import argparse
import shutil
import time
from secrets import token_hex

import httpx
from app.core.config import settings
from app.core.database import SessionLocal
from app.models.entities import (
    AudioAsset,
    JudgeScorecard,
    Match,
    MatchEvent,
    RatingChange,
    Room,
    RoomSeat,
    Speech,
    TranscriptSegment,
    User,
    UserSession,
)
from app.services.verification_cleanup import release_verification_room_codes
from sqlalchemy import delete, select

PASSWORD = "Rematch-verify-1234"


def csrf(client: httpx.Client) -> dict[str, str]:
    value = client.cookies.get("jixia_v2_csrf")
    return {"X-CSRF-Token": value} if value else {}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://117.50.192.216")
    args = parser.parse_args()
    account = f"qa_rematch_{int(time.time())}_{token_hex(3)}"
    client = httpx.Client(base_url=args.base_url, verify=False, timeout=30, follow_redirects=True)
    user_id = ""
    room_codes: list[str] = []
    try:
        registered = client.post(
            "/api/auth/register",
            json={
                "account": account,
                "real_name": "再次比赛验收用户",
                "password": PASSWORD,
                "confirm_password": PASSWORD,
            },
        )
        registered.raise_for_status()
        user_id = registered.json()["user"]["id"]
        with SessionLocal() as db:
            user = db.get(User, user_id)
            assert user
            user.is_test_account = True
            db.commit()

        created = client.post(
            "/api/rooms",
            headers=csrf(client) | {"X-Idempotency-Key": token_hex(16)},
            json={
                "competition_slug": "training-1v1",
                "custom_topic": "完成比赛后能否快速复盘再赛？",
                "seat_key": "aff_1",
                "visibility": "private",
            },
        )
        created.raise_for_status()
        source = created.json()["room"]
        source_code = source["code"]
        room_codes.append(source_code)
        client.post(f"/api/rooms/{source_code}/ready", headers=csrf(client), json={"ready": True}).raise_for_status()
        client.post(f"/api/rooms/{source_code}/start", headers=csrf(client), json={}).raise_for_status()
        client.post(
            f"/api/rooms/{source_code}/control/terminate",
            headers=csrf(client),
            json={"reason": "live rematch verification"},
        ).raise_for_status()

        rematch_headers = csrf(client) | {"X-Idempotency-Key": "live-rematch-verification"}
        rematch = client.post(f"/api/rooms/{source_code}/rematch", headers=rematch_headers, json={})
        replay = client.post(f"/api/rooms/{source_code}/rematch", headers=rematch_headers, json={})
        rematch.raise_for_status()
        replay.raise_for_status()
        new_room = rematch.json()["room"]
        room_codes.append(new_room["code"])
        assert replay.json()["replayed"] is True
        assert replay.json()["room"]["code"] == new_room["code"]
        assert new_room["topic"] == source["topic"]
        assert new_room["competition"]["slug"] == source["competition"]["slug"]
        assert new_room["my_seat"] == source["my_seat"]
        assert new_room["status"] == "lobby" and new_room["is_test_data"] is True
        client.post(f"/api/rooms/{new_room['code']}/cancel", headers=csrf(client), json={}).raise_for_status()
        print(
            "rematch_flow_verified terminal=1 rematch=1 replay=1 "
            f"source={source_code} rematch={new_room['code']} qa_isolated=true"
        )
    finally:
        client.close()
        if user_id:
            with SessionLocal() as db:
                room_ids = list(db.scalars(select(Room.id).where(Room.owner_id == user_id)).all())
                match_ids = list(db.scalars(select(Match.id).where(Match.room_id.in_(room_ids))).all())
                speech_ids = list(db.scalars(select(Speech.id).where(Speech.room_id.in_(room_ids))).all())
                if speech_ids:
                    db.execute(delete(TranscriptSegment).where(TranscriptSegment.speech_id.in_(speech_ids)))
                if match_ids:
                    db.execute(delete(RatingChange).where(RatingChange.match_id.in_(match_ids)))
                    db.execute(delete(JudgeScorecard).where(JudgeScorecard.match_id.in_(match_ids)))
                    db.execute(delete(AudioAsset).where(AudioAsset.match_id.in_(match_ids)))
                if room_ids:
                    db.execute(delete(Speech).where(Speech.room_id.in_(room_ids)))
                    db.execute(delete(MatchEvent).where(MatchEvent.room_id.in_(room_ids)))
                    db.execute(delete(Match).where(Match.id.in_(match_ids)))
                    db.execute(delete(RoomSeat).where(RoomSeat.room_id.in_(room_ids)))
                    release_verification_room_codes(db, room_ids)
                    db.execute(delete(Room).where(Room.id.in_(room_ids)))
                db.execute(delete(UserSession).where(UserSession.user_id == user_id))
                db.execute(delete(User).where(User.id == user_id))
                db.commit()
        for code in room_codes:
            shutil.rmtree(settings.media_path / code, ignore_errors=True)


if __name__ == "__main__":
    main()
