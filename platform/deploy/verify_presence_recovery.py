"""Production-safe verifier for the 60-second human disconnect policy.

The verifier creates an isolated 1v1 room, moves one real human presence just
past the grace boundary, and proves the following authoritative behaviour:

* the room pauses;
* the seat remains human and no Agent speech starts;
* reconnecting does not resume the room;
* the owner must explicitly resume after the human is back.

All temporary database rows and media paths are removed in ``finally``.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import ssl
import time
from datetime import timedelta
from uuid import uuid4

import httpx
import websockets
from app.core.config import settings
from app.core.database import SessionLocal
from app.models.entities import (
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
from app.services.room_service import load_room, now
from app.services.verification_cleanup import release_verification_room_codes
from sqlalchemy import delete, select

BASE_URL = os.getenv("VERIFY_BASE_URL", "https://117.50.192.216")
PASSWORD = "Verify-prod-1234"


def run() -> dict:
    owner = httpx.Client(base_url=BASE_URL, verify=False, timeout=20, follow_redirects=True)
    user_id = room_id = match_id = speech_id = code = None

    def check(response: httpx.Response, expected: int = 200) -> dict:
        if response.status_code != expected:
            raise RuntimeError(
                f"{response.request.method} {response.request.url.path}: "
                f"{response.status_code} {response.text[:300]}"
            )
        return response.json()

    def owner_post(path: str, payload: dict) -> httpx.Response:
        return owner.post(path, headers={"X-CSRF-Token": owner.cookies["jixia_csrf"]}, json=payload)

    async def reconnect_and_resume() -> dict:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        cookie_header = "; ".join(f"{key}={value}" for key, value in owner.cookies.items())
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
            assert returned["occupant_type"] == "human" and returned["connected"] is True
            assert snapshot["status"] == "paused", "reconnect must never auto-resume the match"
            resumed = await asyncio.to_thread(owner_post, f"/api/rooms/{code}/control/resume", {})
            return check(resumed)["room"]

    try:
        account = f"presence_{uuid4().hex[:12]}"
        registered = check(
            owner.post(
                "/api/auth/register",
                json={
                    "account": account,
                    "real_name": "断线暂停验证用户",
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
                    "custom_topic": "真人断线后是否应暂停比赛并保留席位？",
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

        # Do not invoke MatchEngine in this verifier process. Waiting for the
        # production engine heartbeat proves that the independently running
        # tick-level safety scan observes the overdue presence even if the
        # room's normal provider task is busy.
        pause_deadline = time.monotonic() + 10
        while time.monotonic() < pause_deadline:
            with SessionLocal() as db:
                if load_room(db, code).status == "paused":
                    break
            time.sleep(0.1)
        else:
            raise AssertionError("production engine did not pause the overdue human room within 10 seconds")

        with SessionLocal() as db:
            room = load_room(db, code)
            seat = next(item for item in room.seats if item.seat_key == "aff_1")
            speech = db.get(Speech, speech_id)
            event_types = set(
                db.scalars(select(MatchEvent.event_type).where(MatchEvent.room_id == room.id)).all()
            )
            assert room.status == "paused"
            assert seat.occupant_type == "human" and seat.user_id == user_id
            assert speech.status == "interrupted"
            assert "participant.disconnect_timeout" in event_types
            assert "match.paused" in event_types
            assert "seat.ai_substituted" not in event_types

        resumed = asyncio.run(reconnect_and_resume())
        assert resumed["status"] == "running"
        resumed_seat = next(item for item in resumed["seats"] if item["seat_key"] == "aff_1")
        assert resumed_seat["occupant_type"] == "human"

        check(owner_post(f"/api/rooms/{code}/control/terminate", {"reason": "presence verification complete"}))
        return {
            "ok": True,
            "disconnect_timeout_seconds": 60,
            "match_paused": True,
            "human_seat_preserved": True,
            "ai_takeover_absent": True,
            "reconnect_did_not_auto_resume": True,
            "explicit_resume_succeeded": True,
        }
    finally:
        owner.close()
        time.sleep(2)
        with SessionLocal() as db:
            if room_id:
                current_match_ids = list(db.scalars(select(Match.id).where(Match.room_id == room_id)).all())
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
                db.execute(delete(RoomSeat).where(RoomSeat.room_id == room_id))
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
    print(json.dumps(run(), ensure_ascii=False))
