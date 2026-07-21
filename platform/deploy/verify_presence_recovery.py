"""Production-safe verifier for disconnect, AI substitution and human restore."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import ssl
import time
from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import websockets
from app.core.config import settings
from app.core.database import SessionLocal
from app.models.entities import (
    AdminAuditLog,
    AudioAsset,
    JudgeScorecard,
    LeaderboardEntry,
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
from app.services.match_archive import archive_lock_name
from app.services.match_engine import match_engine
from app.services.room_service import load_room, now
from app.services.verification_cleanup import release_verification_room_codes
from sqlalchemy import delete, select

BASE_URL = os.getenv("VERIFY_BASE_URL", "https://117.50.192.216")
PASSWORD = "Verify-prod-1234"


def run() -> dict:
    owner_client = httpx.Client(base_url=BASE_URL, verify=False, timeout=20, follow_redirects=True)
    admin_client = httpx.Client(base_url=BASE_URL, verify=False, timeout=20, follow_redirects=True)
    user_id = room_id = match_id = speech_id = code = None
    admin_account = os.getenv("PHDEBATE_ADMIN_ACCOUNT") or settings.v2_admin_account
    admin_password = os.getenv("PHDEBATE_ADMIN_PASSWORD") or settings.v2_admin_password
    if not admin_account or not admin_password:
        raise RuntimeError("production administrator credentials are not configured")

    def check(response: httpx.Response, expected: int = 200) -> dict:
        if response.status_code != expected:
            raise RuntimeError(f"{response.request.method} {response.request.url.path}: {response.status_code} {response.text[:300]}")
        return response.json()

    def owner_post(path: str, payload: dict) -> httpx.Response:
        return owner_client.post(path, headers={"X-CSRF-Token": owner_client.cookies["jixia_csrf"]}, json=payload)

    def admin_post(path: str) -> httpx.Response:
        return admin_client.post(path, headers={"X-CSRF-Token": admin_client.cookies["jixia_csrf"]}, json={})

    async def reconnect_and_restore() -> dict:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        cookie_header = "; ".join(f"{key}={value}" for key, value in owner_client.cookies.items())
        websocket_url = BASE_URL.replace("https://", "wss://").replace("http://", "ws://")
        async with websockets.connect(
            f"{websocket_url}/ws/rooms/{code}",
            ssl=context,
            origin=BASE_URL.rstrip("/"),
            additional_headers={"Cookie": cookie_header},
            open_timeout=10,
            close_timeout=5,
        ) as socket:
            snapshot = json.loads(await asyncio.wait_for(socket.recv(), timeout=10))["room"]
            returned = next(item for item in snapshot["seats"] if item["seat_key"] == "aff_1")
            assert returned["occupant_type"] == "ai_substitute" and returned["connected"] is True
            restored = await asyncio.to_thread(admin_post, f"/api/admin/rooms/{code}/seats/aff_1/restore")
            return check(restored)

    try:
        account = f"presence_{uuid4().hex[:12]}"
        registered = check(
            owner_client.post(
                "/api/auth/register",
                json={
                    "account": account,
                    "real_name": "断线恢复验证用户",
                    "password": PASSWORD,
                    "confirm_password": PASSWORD,
                },
            )
        )
        user_id = registered["user"]["id"]
        created = check(
            owner_post(
                "/api/rooms",
                {
                    "competition_slug": "training-1v1",
                    "custom_topic": "断线后的真人席位应如何安全恢复？",
                    "seat_key": "aff_1",
                    "visibility": "public",
                },
            )
        )["room"]
        room_id, code = created["id"], created["code"]
        check(owner_post(f"/api/rooms/{code}/ready", {"ready": True}))
        check(owner_post(f"/api/rooms/{code}/start", {}))

        with SessionLocal() as db:
            room = load_room(db, code, lock=True)
            room.template_snapshot = [
                {"key": "aff_turn", "name": "正方立论", "kind": "speech", "seat": "aff_1", "duration": 120},
                {"key": "neg_turn", "name": "反方立论", "kind": "speech", "seat": "neg_1", "duration": 120},
            ]
            room.current_stage_index = 0
            room.status = "running"
            room.stage_started_at = now()
            room.stage_deadline_at = now() + timedelta(seconds=59)
            seat = next(item for item in room.seats if item.seat_key == "aff_1")
            seat.connected = False
            seat.disconnected_at = now() - timedelta(seconds=61)
            match = db.scalar(select(Match).where(Match.room_id == room.id))
            match_id = match.id
            speech = Speech(
                match_id=match.id,
                room_id=room.id,
                seat_key=seat.seat_key,
                stage_key="aff_turn",
                speaker_type="human",
                status="speaking",
            )
            db.add(speech)
            db.commit()
            speech_id = speech.id

        ai_speech = AsyncMock()
        original_ai_speech = match_engine._ai_speech
        match_engine._ai_speech = ai_speech
        try:
            asyncio.run(match_engine.process_room(code))
        finally:
            match_engine._ai_speech = original_ai_speech
        ai_speech.assert_awaited_once()

        with SessionLocal() as db:
            room = load_room(db, code)
            seat = next(item for item in room.seats if item.seat_key == "aff_1")
            speech = db.get(Speech, speech_id)
            assert seat.occupant_type == "ai_substitute" and seat.connected is False
            assert speech.status == "interrupted"

        check(
            admin_client.post(
                "/api/auth/login",
                json={"account": admin_account, "password": admin_password},
            )
        )
        offline_restore = admin_post(f"/api/admin/rooms/{code}/seats/aff_1/restore")
        assert offline_restore.status_code == 409 and "尚未重新连接" in offline_restore.text

        restored = asyncio.run(reconnect_and_restore())["room"]
        restored_seat = next(item for item in restored["seats"] if item["seat_key"] == "aff_1")
        assert restored_seat["occupant_type"] == "human" and restored_seat["display_name"] == "断线恢复验证用户"

        check(owner_post(f"/api/rooms/{code}/control/terminate", {"reason": "production_presence_verification"}))
        return {
            "ok": True,
            "abandoned_human_speech_interrupted": True,
            "ai_takeover_scheduled": True,
            "offline_restore_rejected": True,
            "websocket_return_detected": True,
            "human_restore_succeeded": True,
        }
    finally:
        owner_client.close()
        admin_client.close()
        time.sleep(5)
        with SessionLocal() as db:
            if room_id:
                current_match_ids = list(db.scalars(select(Match.id).where(Match.room_id == room_id)).all())
                seat_ids = list(db.scalars(select(RoomSeat.id).where(RoomSeat.room_id == room_id)).all())
                if seat_ids:
                    db.execute(
                        delete(AdminAuditLog).where(
                            AdminAuditLog.action == "seat.restore_human",
                            AdminAuditLog.target_id.in_(seat_ids),
                        )
                    )
                if current_match_ids:
                    match_id = current_match_ids[0]
                speech_ids = list(db.scalars(select(Speech.id).where(Speech.room_id == room_id)).all())
                if speech_ids:
                    db.execute(delete(TranscriptSegment).where(TranscriptSegment.speech_id.in_(speech_ids)))
                if current_match_ids:
                    db.execute(delete(AudioAsset).where(AudioAsset.match_id.in_(current_match_ids)))
                    db.execute(delete(RatingChange).where(RatingChange.match_id.in_(current_match_ids)))
                    db.execute(delete(JudgeScorecard).where(JudgeScorecard.match_id.in_(current_match_ids)))
                db.execute(delete(Speech).where(Speech.room_id == room_id))
                db.execute(delete(MatchEvent).where(MatchEvent.room_id == room_id))
                if current_match_ids:
                    db.execute(delete(Match).where(Match.id.in_(current_match_ids)))
                release_verification_room_codes(db, [room_id])
                db.execute(delete(Room).where(Room.id == room_id))
            if user_id:
                db.execute(delete(LeaderboardEntry).where(LeaderboardEntry.user_id == user_id))
                db.execute(delete(UserSession).where(UserSession.user_id == user_id))
                db.execute(delete(User).where(User.id == user_id))
            db.commit()
        if code:
            shutil.rmtree(settings.media_path / code, ignore_errors=True)
        if match_id:
            for suffix in (".json", ".meta.json", ".json.sha256", ".part"):
                (settings.archive_path / f"{match_id}{suffix}").unlink(missing_ok=True)
            (settings.archive_path / ".locks" / archive_lock_name(match_id)).unlink(missing_ok=True)


if __name__ == "__main__":
    print(run())
