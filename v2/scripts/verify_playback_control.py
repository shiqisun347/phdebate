#!/usr/bin/env python3
"""Verify authoritative AI playback pause/resume/finish/terminate without calling an Agent."""

from __future__ import annotations

import argparse
import time
from datetime import timedelta
from secrets import token_hex

import httpx
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
    User,
    UserSession,
)
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
    account = f"playback_{int(time.time())}_{token_hex(3)}"
    password = "Playback-control-1234"
    user_id = room_id = match_id = first_speech_id = second_speech_id = ""
    client = httpx.Client(base_url=args.base_url, verify=False, timeout=20, follow_redirects=True)
    try:
        registered = client.post(
            "/api/auth/register",
            json={
                "account": account,
                "real_name": "AI 播放控制验收",
                "password": password,
                "confirm_password": password,
            },
        )
        registered.raise_for_status()
        user_id = registered.json()["user"]["id"]
        created = client.post(
            "/api/rooms",
            headers=csrf(client) | {"X-Idempotency-Key": token_hex(16)},
            json={
                "competition_slug": "training-1v1",
                "custom_topic": "自动语音暂停后是否应从原位置继续？",
                "seat_key": "aff_1",
                "visibility": "public",
            },
        )
        created.raise_for_status()
        code = created.json()["room"]["code"]
        with SessionLocal() as db:
            room = load_room(db, code, lock=True)
            room_id = room.id
            started_at = now() - timedelta(seconds=4)
            room.template_snapshot = [
                {"key": "ai_case", "name": "AI 发言", "kind": "speech", "seat": "neg_1", "duration": 10},
                {"key": "human_case", "name": "真人发言", "kind": "speech", "seat": "aff_1", "duration": 30},
            ]
            room.status = "running"
            room.current_stage_index = 0
            room.stage_started_at = started_at
            room.stage_deadline_at = now() + timedelta(seconds=6)
            match = Match(
                room_id=room.id,
                competition_id=room.competition_id,
                season_id=room.season_id,
                status="running",
            )
            db.add(match)
            db.flush()
            match_id = match.id
            speech = Speech(
                match_id=match.id,
                room_id=room.id,
                seat_key="neg_1",
                stage_key="ai_case",
                speaker_type="ai",
                status="playing",
                content="这是预置的播放控制验收音频文本。",
                audio_url="/media/test/nonexistent.wav",
                duration_seconds=10,
                playback_started_at=started_at,
                playback_ends_at=now() + timedelta(seconds=6),
            )
            db.add(speech)
            db.commit()
            first_speech_id = speech.id

        paused = client.post(
            f"/api/rooms/{code}/control/pause",
            headers=csrf(client) | {"X-Idempotency-Key": "playback-pause"},
            json={"reason": "线上续播验收"},
        )
        paused.raise_for_status()
        assert paused.json()["room"]["status"] == "paused"
        time.sleep(1.0)
        resumed = client.post(
            f"/api/rooms/{code}/control/resume",
            headers=csrf(client) | {"X-Idempotency-Key": "playback-resume"},
            json={"reason": "继续播放"},
        )
        resumed.raise_for_status()
        with SessionLocal() as db:
            room = load_room(db, code)
            speech = db.get(Speech, first_speech_id)
            playback_started_at = speech.playback_started_at
            playback_ends_at = speech.playback_ends_at
            if playback_started_at.tzinfo is None:
                playback_started_at = playback_started_at.replace(tzinfo=now().tzinfo)
            if playback_ends_at.tzinfo is None:
                playback_ends_at = playback_ends_at.replace(tzinfo=now().tzinfo)
            elapsed = (now() - playback_started_at).total_seconds()
            remaining = (playback_ends_at - now()).total_seconds()
            assert 3.5 <= elapsed <= 4.8
            assert 5.0 <= remaining <= 6.5
            assert "paused_playback_elapsed_seconds" not in room.template_snapshot[0]

        deadline = time.monotonic() + 10
        completed = None
        while time.monotonic() < deadline:
            completed = client.get(f"/api/rooms/{code}").json()["room"]
            if completed["current_stage_index"] == 1:
                break
            time.sleep(0.25)
        assert completed and completed["current_stage_index"] == 1
        with SessionLocal() as db:
            first = db.get(Speech, first_speech_id)
            assert first.status == "completed"
            room = load_room(db, code, lock=True)
            second = Speech(
                match_id=match_id,
                room_id=room.id,
                seat_key="neg_1",
                stage_key="human_case",
                speaker_type="ai",
                status="playing",
                content="终止比赛时此播放必须被中断。",
                duration_seconds=30,
                playback_started_at=now(),
                playback_ends_at=now() + timedelta(seconds=30),
            )
            db.add(second)
            db.commit()
            second_speech_id = second.id

        terminated = client.post(
            f"/api/rooms/{code}/control/terminate",
            headers=csrf(client) | {"X-Idempotency-Key": "playback-terminate"},
            json={"reason": "线上终止验收"},
        )
        terminated.raise_for_status()
        assert terminated.json()["room"]["status"] == "terminated"
        assert client.post(f"/api/rooms/{code}/control/resume", headers=csrf(client), json={}).status_code == 409
        with SessionLocal() as db:
            second = db.get(Speech, second_speech_id)
            match = db.get(Match, match_id)
            assert second.status == "interrupted"
            assert match.status == "terminated"
        print("playback_control_verified offset=4s completion=auto terminate=interrupted")
    finally:
        client.close()
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
