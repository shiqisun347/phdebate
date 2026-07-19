#!/usr/bin/env python3
"""Verify AI provider preparation cannot consume a free-debate speaking turn."""

from __future__ import annotations

import argparse
import asyncio
import shutil
import time
import wave
from datetime import timedelta
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
from app.services.match_engine import match_engine
from app.services.providers import debate_agent, lighttts
from app.services.room_service import free_turn_remaining_seconds, load_room, now, remaining_seconds
from app.services.verification_cleanup import release_verification_room_codes
from sqlalchemy import delete, select


def csrf(client: httpx.AsyncClient) -> dict[str, str]:
    value = client.cookies.get("jixia_v2_csrf")
    return {"X-CSRF-Token": value} if value else {}


async def main_async(base_url: str) -> None:
    suffix = f"{int(time.time())}_{token_hex(3)}"
    password = "Free-ai-clock-1234"
    client = httpx.AsyncClient(base_url=base_url, verify=False, timeout=30, follow_redirects=True)
    user_id = room_id = match_id = code = ""
    try:
        registered = await client.post(
            "/api/auth/register",
            json={
                "account": f"free_ai_clock_{suffix}",
                "real_name": "AI 准备计时验收",
                "password": password,
                "confirm_password": password,
            },
        )
        registered.raise_for_status()
        user_id = registered.json()["user"]["id"]
        created = await client.post(
            "/api/rooms",
            headers=csrf(client) | {"X-Idempotency-Key": token_hex(16)},
            json={
                "competition_slug": "training-1v1",
                "custom_topic": "AI 准备时间是否应占用自由辩论发言时间？",
                "seat_key": "aff_1",
                "visibility": "private",
            },
        )
        created.raise_for_status()
        code = created.json()["room"]["code"]
        (await client.post(f"/api/rooms/{code}/ready", headers=csrf(client), json={"ready": True})).raise_for_status()
        (await client.post(f"/api/rooms/{code}/start", headers=csrf(client), json={})).raise_for_status()
        with SessionLocal() as db:
            room = load_room(db, code, lock=True)
            room_id = room.id
            match = db.scalar(select(Match).where(Match.room_id == room.id))
            assert match is not None
            match_id = match.id
            started_at = now()
            room.template_snapshot = [
                {
                    "key": "free",
                    "name": "自由辩论",
                    "kind": "free",
                    "side": "aff",
                    "duration": 120,
                    "turn_duration": 45,
                    "turn_started_at": started_at.isoformat(),
                }
            ]
            room.current_stage_index = 0
            room.status = "running"
            room.stage_started_at = started_at
            room.stage_deadline_at = started_at + timedelta(seconds=120)
            for seat in room.seats:
                seat.occupant_type = "ai"
                seat.user_id = None
            db.commit()

        async def generated(*_args, **_kwargs):
            with SessionLocal() as db:
                room = load_room(db, code, lock=True)
                current = dict(room.template_snapshot[0])
                current["turn_started_at"] = (now() - timedelta(seconds=60)).isoformat()
                room.template_snapshot = [current]
                room.stage_deadline_at = now() - timedelta(seconds=1)
                db.commit()
                assert remaining_seconds(room) >= 118
                assert free_turn_remaining_seconds(room) >= 43
                assert room.template_snapshot[0]["ai_preparing"] is True
            return "AI 准备完成后应获得完整的播放时间。"

        async def synthesized(*_args, room_code: str, speech_id: str, should_cancel, **_kwargs):
            assert should_cancel() is False
            target_dir = settings.media_path / room_code
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / f"{speech_id}.wav"
            with wave.open(str(target), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(24000)
                audio.writeframes(b"\x00\x00" * 24000)
            return f"/media/{room_code}/{speech_id}.wav"

        original_generate = debate_agent.generate
        original_synthesize = lighttts.synthesize
        debate_agent.generate = generated
        lighttts.synthesize = synthesized
        try:
            await match_engine.process_room(code)
        finally:
            debate_agent.generate = original_generate
            lighttts.synthesize = original_synthesize

        with SessionLocal() as db:
            room = load_room(db, code)
            speech = db.scalar(select(Speech).where(Speech.room_id == room.id))
            event = db.scalar(
                select(MatchEvent).where(
                    MatchEvent.room_id == room.id,
                    MatchEvent.event_type == "speech.audio.ready",
                )
            )
            assert speech and speech.status == "playing"
            assert remaining_seconds(room) >= 118
            assert free_turn_remaining_seconds(room) >= 43
            assert "ai_preparing" not in room.template_snapshot[0]
            assert event and event.payload["preparation_seconds"] >= 0
        print("free_ai_preparation_clock_verified stage>=118s turn>=43s expired_provider_clock_ignored=1")
    finally:
        await client.aclose()
        if room_id:
            with SessionLocal() as db:
                if match_id:
                    speech_ids = select(Speech.id).where(Speech.match_id == match_id)
                    db.execute(delete(TranscriptSegment).where(TranscriptSegment.speech_id.in_(speech_ids)))
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
        if code:
            shutil.rmtree(settings.media_path / code, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://117.50.192.216")
    args = parser.parse_args()
    asyncio.run(main_async(args.base_url.rstrip("/")))


if __name__ == "__main__":
    main()
