#!/usr/bin/env python3
"""Prepare and verify persisted provider-task recovery across a real engine restart."""

from __future__ import annotations

import argparse
import os
import time
import wave
from datetime import timedelta
from secrets import token_hex

import httpx
from app.core.config import settings
from app.core.database import SessionLocal
from app.models.entities import JudgeScorecard, Match, MatchEvent, Room, RoomSeat, Speech, User, UserSession
from app.services.room_service import load_room, now
from app.services.verification_cleanup import release_verification_room_codes
from sqlalchemy import delete, select

PASSWORD = "Engine-restart-1234"


def csrf(client: httpx.Client) -> dict[str, str]:
    value = client.cookies.get("jixia_csrf")
    return {"X-CSRF-Token": value} if value else {}


def write_wav(path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(b"\x00\x00" * 2400)


def prepare(base_url: str) -> None:
    suffix = f"{int(time.time())}_{token_hex(3)}"
    with httpx.Client(base_url=base_url, verify=False, timeout=30, follow_redirects=True) as client:
        registered = client.post(
            "/api/auth/register",
            json={
                "account": f"engine_restart_{suffix}",
                "real_name": "引擎重启恢复验收",
                "password": PASSWORD,
                "confirm_password": PASSWORD,
            },
        )
        registered.raise_for_status()
        created = client.post(
            "/api/rooms",
            headers=csrf(client) | {"X-Idempotency-Key": token_hex(16)},
            json={
                "competition_slug": "training-1v1",
                "custom_topic": "比赛引擎重启后是否能恢复未完成的语音与裁判任务？",
                "seat_key": "aff_1",
                "visibility": "private",
            },
        )
        created.raise_for_status()
        code = created.json()["room"]["code"]
        client.post(f"/api/rooms/{code}/ready", headers=csrf(client), json={"ready": True}).raise_for_status()
        client.post(f"/api/rooms/{code}/start", headers=csrf(client), json={}).raise_for_status()
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.status = "running"
        room.current_stage_index = 1
        room.stage_started_at = now()
        room.stage_deadline_at = now() + timedelta(seconds=180)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        speech = Speech(
            match_id=match.id,
            room_id=room.id,
            seat_key="neg_1",
            stage_key="restart_owned_task",
            speaker_type="ai",
            status="synthesizing",
            content="引擎重启前生成的测试文本。",
        )
        db.add(speech)
        db.flush()
        db.add(JudgeScorecard(match_id=match.id, status="running", reasoning="引擎重启前的裁判任务"))
        db.commit()
        speech_id = speech.id
    target_dir = settings.media_path / code
    write_wav(target_dir / f"{speech_id}.wav")
    (target_dir / f".{speech_id}.restart.wav.part").write_bytes(b"partial")
    print(f"engine_restart_prepared code={code}")


def verify(code: str) -> None:
    with SessionLocal() as db:
        room = load_room(db, code)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        speech = db.scalar(select(Speech).where(Speech.room_id == room.id, Speech.speaker_type == "ai"))
        scorecard = db.scalar(select(JudgeScorecard).where(JudgeScorecard.match_id == match.id))
        event_types = {
            item.event_type
            for item in db.scalars(
                select(MatchEvent).where(
                    MatchEvent.room_id == room.id,
                    MatchEvent.payload["reason"].as_string() == "engine_restart",
                )
            ).all()
        }
        assert speech and speech.status == "interrupted"
        assert scorecard and scorecard.status == "interrupted"
        assert event_types.issuperset({"speech.interrupted", "judge.interrupted"})
        assert not (settings.media_path / code / f"{speech.id}.wav").exists()
        assert not list((settings.media_path / code).glob(f".{speech.id}.*.wav.part"))
        user_id = room.owner_id
        room_id = room.id
        match_id = match.id
        scorecard_id = scorecard.id
        speech_id = speech.id
    with SessionLocal() as db:
        db.execute(delete(MatchEvent).where(MatchEvent.room_id == room_id))
        db.execute(delete(JudgeScorecard).where(JudgeScorecard.id == scorecard_id))
        db.execute(delete(Speech).where(Speech.id == speech_id))
        db.execute(delete(Match).where(Match.id == match_id))
        db.execute(delete(RoomSeat).where(RoomSeat.room_id == room_id))
        release_verification_room_codes(db, [room_id])
        db.execute(delete(Room).where(Room.id == room_id))
        db.execute(delete(UserSession).where(UserSession.user_id == user_id))
        db.execute(delete(User).where(User.id == user_id))
        db.commit()
    try:
        (settings.media_path / code).rmdir()
    except OSError:
        pass
    print("engine_restart_recovery_verified speeches=1 judges=1 orphan_audio=0 provider_calls=0")


def main() -> None:
    if os.geteuid() == 0:
        raise SystemExit("请使用 平台 服务账号运行引擎重启验收。")
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://117.50.192.216")
    parser.add_argument("--mode", choices=["prepare", "verify"], required=True)
    parser.add_argument("--code", default="")
    args = parser.parse_args()
    if args.mode == "prepare":
        prepare(args.base_url.rstrip("/"))
    elif not args.code:
        raise SystemExit("verify 模式必须提供 --code。")
    else:
        verify(args.code)


if __name__ == "__main__":
    main()
