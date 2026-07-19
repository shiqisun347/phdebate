#!/usr/bin/env python3
"""Verify many rooms can upload finalized human recordings without SQL-pool starvation."""

from __future__ import annotations

import argparse
import asyncio
import io
import shutil
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

PASSWORD = "Parallel-audio-1234"


def csrf(client: httpx.AsyncClient) -> dict[str, str]:
    value = client.cookies.get("jixia_v2_csrf")
    return {"X-CSRF-Token": value} if value else {}


def wav_bytes() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x00\x00" * 800)
    return buffer.getvalue()


async def main_async(base_url: str, count: int) -> None:
    suffix = f"{int(time.time())}_{token_hex(3)}"
    clients = [httpx.AsyncClient(base_url=base_url, verify=False, timeout=30, follow_redirects=True) for _ in range(count)]
    user_ids: list[str] = []
    room_ids: list[str] = []
    match_ids: list[str] = []
    speech_ids: list[str] = []
    room_codes: list[str] = []
    started = time.monotonic()
    try:

        async def register_and_create(index: int) -> tuple[str, str]:
            client = clients[index]
            registered = await client.post(
                "/api/auth/register",
                json={
                    "account": f"parallel_audio_{index}_{suffix}",
                    "real_name": f"并行录音选手{index + 1}",
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
                    "custom_topic": f"第 {index + 1} 个房间并行上传录音是否互不阻塞？",
                    "seat_key": "aff_1",
                    "visibility": "private",
                },
            )
            created.raise_for_status()
            return registered.json()["user"]["id"], created.json()["room"]["code"]

        identities = await asyncio.gather(*(register_and_create(index) for index in range(count)))
        user_ids[:] = [item[0] for item in identities]
        room_codes[:] = [item[1] for item in identities]

        with SessionLocal() as db:
            for index, code in enumerate(room_codes):
                room = load_room(db, code, lock=True)
                room_ids.append(room.id)
                match = Match(
                    room_id=room.id,
                    competition_id=room.competition_id,
                    season_id=room.season_id,
                    status="running",
                )
                db.add(match)
                db.flush()
                speech = Speech(
                    match_id=match.id,
                    room_id=room.id,
                    seat_key="aff_1",
                    stage_key="parallel-upload",
                    speaker_type="human",
                    status="completed",
                    content=f"并行录音 {index + 1}",
                )
                db.add(speech)
                db.flush()
                match_ids.append(match.id)
                speech_ids.append(speech.id)
            db.commit()

        payload = wav_bytes()

        async def upload(index: int) -> httpx.Response:
            return await clients[index].post(
                f"/api/rooms/{room_codes[index]}/speech/{speech_ids[index]}/audio",
                headers=csrf(clients[index]),
                files={"audio": (f"speech-{index}.wav", payload, "audio/wav")},
            )

        responses = await asyncio.wait_for(asyncio.gather(*(upload(index) for index in range(count))), timeout=25)
        assert all(response.status_code == 200 for response in responses), [
            (response.status_code, response.text[:200]) for response in responses if response.status_code != 200
        ]
        urls = [response.json()["audio_url"] for response in responses]
        assert len(set(urls)) == count and all(url.endswith(".wav") for url in urls)
        elapsed = time.monotonic() - started
        print(f"parallel_audio_uploads_verified rooms={count} completed={len(urls)} elapsed={elapsed:.2f}s")
    finally:
        await asyncio.gather(*(client.aclose() for client in clients), return_exceptions=True)
        if room_ids:
            with SessionLocal() as db:
                db.execute(delete(Speech).where(Speech.room_id.in_(room_ids)))
                db.execute(delete(MatchEvent).where(MatchEvent.room_id.in_(room_ids)))
                db.execute(delete(Match).where(Match.id.in_(match_ids)))
                db.execute(delete(RoomSeat).where(RoomSeat.room_id.in_(room_ids)))
                release_verification_room_codes(db, room_ids)
                db.execute(delete(Room).where(Room.id.in_(room_ids)))
                db.execute(delete(UserSession).where(UserSession.user_id.in_(user_ids)))
                db.execute(delete(User).where(User.id.in_(user_ids)))
                db.commit()
        for code in room_codes:
            shutil.rmtree(settings.media_path / code, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://117.50.192.216")
    parser.add_argument("--rooms", type=int, default=20)
    args = parser.parse_args()
    if not 1 <= args.rooms <= 40:
        raise SystemExit("--rooms must be between 1 and 40")
    asyncio.run(main_async(args.base_url.rstrip("/"), args.rooms))


if __name__ == "__main__":
    main()
