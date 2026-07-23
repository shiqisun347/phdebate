#!/usr/bin/env python3
"""Verify deployed provider-failure retry state transitions without invoking providers."""

from __future__ import annotations

import argparse
import time
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
from app.services.room_service import load_room
from app.services.verification_cleanup import release_verification_room_codes
from sqlalchemy import delete


def csrf(client: httpx.Client) -> dict[str, str]:
    value = client.cookies.get("jixia_csrf")
    return {"X-CSRF-Token": value} if value else {}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://117.50.192.216")
    args = parser.parse_args()
    account = f"provider_retry_{int(time.time())}_{token_hex(3)}"
    password = "Provider-retry-1234"
    user_id = room_id = match_id = ""
    client = httpx.Client(base_url=args.base_url, verify=False, timeout=20, follow_redirects=True)
    try:
        registered = client.post(
            "/api/auth/register",
            json={
                "account": account,
                "real_name": "服务恢复验收",
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
                "custom_topic": "外部服务恢复后比赛能否从正确步骤继续？",
                "seat_key": "aff_1",
                "visibility": "public",
            },
        )
        created.raise_for_status()
        code = created.json()["room"]["code"]
        with SessionLocal() as db:
            room = load_room(db, code, lock=True)
            room_id = room.id
            room.template_snapshot = [{"key": "human_case", "name": "真人立论", "kind": "speech", "seat": "aff_1", "duration": 120}]
            room.status = "paused"
            room.current_stage_index = -1
            room.failure_reason = "模拟赛前 LightTTS 失败"
            match = Match(
                room_id=room.id,
                competition_id=room.competition_id,
                season_id=room.season_id,
                status="running",
            )
            db.add(match)
            db.commit()
            match_id = match.id

        rejected_prepare_resume = client.post(
            f"/api/rooms/{code}/control/resume",
            headers=csrf(client),
            json={"reason": "不应绕过赛前异常"},
        )
        assert rejected_prepare_resume.status_code == 409
        preparing = client.post(
            f"/api/rooms/{code}/control/retry",
            headers=csrf(client) | {"X-Idempotency-Key": "provider-preparing-retry"},
            json={"reason": "赛前服务已恢复"},
        )
        preparing.raise_for_status()
        assert preparing.json()["room"]["status"] == "preparing"
        deadline = time.monotonic() + 10
        started = None
        while time.monotonic() < deadline:
            started = client.get(f"/api/rooms/{code}").json()["room"]
            if started["status"] == "running" and started["current_stage_index"] == 0:
                break
            time.sleep(0.25)
        assert started and started["status"] == "running" and started["current_stage_index"] == 0

        with SessionLocal() as db:
            room = load_room(db, code, lock=True)
            failed = Speech(
                match_id=match_id,
                room_id=room.id,
                seat_key="neg_1",
                stage_key="human_case",
                speaker_type="ai",
                status="failed",
                content="模拟已生成但未合成语音的内容。",
            )
            db.add(failed)
            room.status = "paused"
            room.failure_reason = "模拟比赛中 LightTTS 失败"
            room.paused_remaining_seconds = 90
            room.stage_deadline_at = None
            db.commit()
            failed_id = failed.id

        rejected_speech_resume = client.post(
            f"/api/rooms/{code}/control/resume",
            headers=csrf(client),
            json={"reason": "不应绕过发言异常"},
        )
        assert rejected_speech_resume.status_code == 409
        running = client.post(
            f"/api/rooms/{code}/control/retry",
            headers=csrf(client) | {"X-Idempotency-Key": "provider-speech-retry"},
            json={"reason": "发言合成服务已恢复"},
        )
        running.raise_for_status()
        room_view = running.json()["room"]
        assert room_view["status"] == "running" and room_view["current_stage_index"] == 0
        assert room_view["remaining_seconds"] in {89, 90}
        with SessionLocal() as db:
            failed_attempt = db.get(Speech, failed_id)
            assert failed_attempt is not None
            assert failed_attempt.status == "failed_retried"
            assert failed_attempt.content == "模拟已生成但未合成语音的内容。"
            room = load_room(db, code)
            assert room.failure_reason == ""
        print(
            "provider_retry_state_verified failure_resume=409 preparation=preparing->running "
            "speech=paused->running failed_attempt=preserved"
        )
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
