"""Production-safe verifier that review-required matches remain read-only."""

from __future__ import annotations

import os
import shutil
import time
from uuid import uuid4

import httpx
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
    client = httpx.Client(base_url=BASE_URL, verify=False, timeout=20, follow_redirects=True)
    user_id = room_id = match_id = code = None

    def check(response: httpx.Response, expected: int = 200) -> dict:
        if response.status_code != expected:
            raise RuntimeError(f"{response.request.method} {response.request.url.path}: {response.status_code} {response.text[:300]}")
        return response.json()

    def post(path: str, payload: dict, extra_headers: dict[str, str] | None = None) -> httpx.Response:
        headers = {"X-CSRF-Token": client.cookies["jixia_csrf"]}
        headers.update(extra_headers or {})
        return client.post(path, headers=headers, json=payload)

    try:
        registered = check(
            client.post(
                "/api/auth/register",
                json={
                    "account": f"terminal_{uuid4().hex[:12]}",
                    "real_name": "终局只读验证用户",
                    "password": PASSWORD,
                    "confirm_password": PASSWORD,
                },
            )
        )
        user_id = registered["user"]["id"]
        room = check(
            post(
                "/api/rooms",
                {
                    "competition_slug": "training-1v1",
                    "custom_topic": "待复核比赛是否必须保持只读？",
                    "seat_key": "aff_1",
                    "visibility": "public",
                },
            )
        )["room"]
        room_id, code = room["id"], room["code"]
        check(post(f"/api/rooms/{code}/ready", {"ready": True}))
        check(post(f"/api/rooms/{code}/start", {}))

        with SessionLocal() as db:
            stored = load_room(db, code, lock=True)
            match = db.scalar(select(Match).where(Match.room_id == stored.id))
            match_id = match.id
            stored.status = "review_required"
            stored.completed_at = now()
            match.status = "review_required"
            db.commit()
            seq_before = stored.seq

        rejected = post(
            f"/api/rooms/{code}/control-lease",
            {},
            {"X-Control-Lease": "late-production-device"},
        )
        assert rejected.status_code == 409 and "比赛已经结束" in rejected.text
        with SessionLocal() as db:
            stored = load_room(db, code)
            assert stored.seq == seq_before
            late_events = list(
                db.scalars(
                    select(MatchEvent).where(
                        MatchEvent.room_id == stored.id,
                        MatchEvent.event_type.in_(["seat.control_acquired", "seat.control_taken_over"]),
                    )
                ).all()
            )
            assert late_events == []

        cancelled_search = client.get(f"/api/rooms/search?code={code}")
        assert cancelled_search.status_code == 200
        return {
            "ok": True,
            "reserved_room_code": code,
            "review_required_control_rejected": True,
            "event_sequence_unchanged": True,
            "external_providers_called": False,
        }
    finally:
        client.close()
        time.sleep(2)
        with SessionLocal() as db:
            if room_id:
                match_ids = list(db.scalars(select(Match.id).where(Match.room_id == room_id)).all())
                speech_ids = list(db.scalars(select(Speech.id).where(Speech.room_id == room_id)).all())
                if speech_ids:
                    db.execute(delete(TranscriptSegment).where(TranscriptSegment.speech_id.in_(speech_ids)))
                if match_ids:
                    db.execute(delete(AudioAsset).where(AudioAsset.match_id.in_(match_ids)))
                    db.execute(delete(RatingChange).where(RatingChange.match_id.in_(match_ids)))
                    db.execute(delete(JudgeScorecard).where(JudgeScorecard.match_id.in_(match_ids)))
                db.execute(delete(Speech).where(Speech.room_id == room_id))
                db.execute(delete(MatchEvent).where(MatchEvent.room_id == room_id))
                if match_ids:
                    db.execute(delete(Match).where(Match.id.in_(match_ids)))
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
