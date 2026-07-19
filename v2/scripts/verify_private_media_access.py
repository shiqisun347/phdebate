#!/usr/bin/env python3
"""Verify private room audio is membership-gated and reference-gated in production."""

from __future__ import annotations

import argparse
import time
import wave
from secrets import token_hex

import httpx
from app.core.config import settings
from app.core.database import SessionLocal
from app.models.entities import Match, MatchEvent, Room, RoomSeat, Speech, User, UserSession
from app.services.room_service import load_room
from app.services.verification_cleanup import release_verification_room_codes
from sqlalchemy import delete


def csrf(client: httpx.Client) -> dict[str, str]:
    value = client.cookies.get("jixia_v2_csrf")
    return {"X-CSRF-Token": value} if value else {}


def register(client: httpx.Client, account: str, name: str, password: str) -> str:
    response = client.post(
        "/api/auth/register",
        json={"account": account, "real_name": name, "password": password, "confirm_password": password},
    )
    response.raise_for_status()
    return response.json()["user"]["id"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://117.50.192.216")
    args = parser.parse_args()
    suffix = f"{int(time.time())}_{token_hex(3)}"
    password = "Private-media-1234"
    owner = httpx.Client(base_url=args.base_url, verify=False, timeout=20, follow_redirects=True)
    outsider = httpx.Client(base_url=args.base_url, verify=False, timeout=20, follow_redirects=True)
    anonymous = httpx.Client(base_url=args.base_url, verify=False, timeout=20, follow_redirects=True)
    user_ids: list[str] = []
    room_id = match_id = ""
    referenced = orphan = None
    try:
        user_ids.append(register(owner, f"private_media_owner_{suffix}", "私密媒体房主", password))
        user_ids.append(register(outsider, f"private_media_outsider_{suffix}", "私密媒体外部用户", password))
        created = owner.post(
            "/api/rooms",
            headers=csrf(owner) | {"X-Idempotency-Key": token_hex(16)},
            json={
                "competition_slug": "training-1v1",
                "custom_topic": "私密比赛录音是否必须经过成员权限校验？",
                "seat_key": "aff_1",
                "visibility": "private",
            },
        )
        created.raise_for_status()
        code = created.json()["room"]["code"]
        target_dir = settings.media_path / code
        target_dir.mkdir(parents=True, exist_ok=True)
        referenced = target_dir / "protected.wav"
        orphan = target_dir / "orphan.wav"
        for target in (referenced, orphan):
            with wave.open(str(target), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(16000)
                audio.writeframes(b"\x00\x00" * 160)

        with SessionLocal() as db:
            room = load_room(db, code, lock=True)
            room_id = room.id
            match = Match(
                room_id=room.id,
                competition_id=room.competition_id,
                season_id=room.season_id,
                status="running",
            )
            db.add(match)
            db.flush()
            match_id = match.id
            db.add(
                Speech(
                    match_id=match.id,
                    room_id=room.id,
                    seat_key="aff_1",
                    stage_key="protected-media",
                    speaker_type="human",
                    status="completed",
                    audio_url=f"/media/{code}/{referenced.name}",
                )
            )
            db.commit()

        media_url = f"/media/{code}/{referenced.name}"
        assert anonymous.get(media_url).status_code == 401
        assert outsider.get(media_url).status_code == 403
        owner_audio = owner.get(media_url)
        assert owner_audio.status_code == 200 and owner_audio.content == referenced.read_bytes()
        assert owner_audio.headers["cache-control"] == "private, no-store"
        partial = owner.get(media_url, headers={"Range": "bytes=0-3"})
        assert partial.status_code == 206 and partial.content == referenced.read_bytes()[:4]
        assert owner.get(f"/media/{code}/{orphan.name}").status_code == 404
        print("private_media_access_verified anonymous=401 outsider=403 member=200 range=206 orphan=404 cache=private")
    finally:
        owner.close()
        outsider.close()
        anonymous.close()
        if referenced:
            referenced.unlink(missing_ok=True)
        if orphan:
            orphan.unlink(missing_ok=True)
        if room_id:
            with SessionLocal() as db:
                db.execute(delete(Speech).where(Speech.match_id == match_id))
                db.execute(delete(MatchEvent).where(MatchEvent.room_id == room_id))
                db.execute(delete(Match).where(Match.id == match_id))
                db.execute(delete(RoomSeat).where(RoomSeat.room_id == room_id))
                release_verification_room_codes(db, [room_id])
                db.execute(delete(Room).where(Room.id == room_id))
                db.execute(delete(UserSession).where(UserSession.user_id.in_(user_ids)))
                db.execute(delete(User).where(User.id.in_(user_ids)))
                db.commit()


if __name__ == "__main__":
    main()
