"""Production-safe verifier for per-room engine backoff and quarantine.

The standalone engine must be stopped while this script runs. It creates two
temporary API rooms, replaces only this process' ``process_room`` method with
deterministic fault injection, and never calls Agent, ASR, TTS or judge services.
All temporary database, archive and media records are removed afterward.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from datetime import timedelta
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
from app.services.match_engine import match_engine
from app.services.room_service import load_room, now
from app.services.verification_cleanup import release_verification_room_codes
from sqlalchemy import delete, select
from sqlalchemy.exc import OperationalError

BASE_URL = os.getenv("VERIFY_BASE_URL", "https://117.50.192.216")
PASSWORD = "Verify-prod-1234"


def run() -> dict:
    clients: list[httpx.Client] = []
    user_ids: list[str] = []
    room_ids: list[str] = []
    room_codes: list[str] = []
    match_ids: list[str] = []

    def check(response: httpx.Response, expected: int = 200) -> dict:
        if response.status_code != expected:
            raise RuntimeError(f"{response.request.method} {response.request.url.path}: {response.status_code} {response.text[:300]}")
        return response.json()

    def register(label: str) -> dict:
        client = httpx.Client(base_url=BASE_URL, verify=False, timeout=20, follow_redirects=True)
        account = f"engine_{uuid4().hex[:12]}"
        body = check(
            client.post(
                "/api/auth/register",
                json={
                    "account": account,
                    "real_name": f"状态机验证{label}",
                    "password": PASSWORD,
                    "confirm_password": PASSWORD,
                },
            )
        )
        clients.append(client)
        user_ids.append(body["user"]["id"])
        return {"client": client, "csrf": body["csrf_token"]}

    def post(user: dict, path: str, payload: dict) -> httpx.Response:
        return user["client"].post(path, headers={"X-CSRF-Token": user["csrf"]}, json=payload)

    def create_room(owner: dict, label: str) -> dict:
        body = check(
            post(
                owner,
                "/api/rooms",
                {
                    "competition_slug": "training-1v1",
                    "custom_topic": f"生产状态机隔离验证：{label}",
                    "seat_key": "aff_1",
                    "visibility": "public",
                },
            )
        )["room"]
        room_ids.append(body["id"])
        room_codes.append(body["code"])
        return body

    bad_owner = healthy_owner = None
    try:
        bad_owner = register("故障房间")
        healthy_owner = register("健康房间")
        bad_room = create_room(bad_owner, "连续异常仅暂停本房间")
        healthy_room = create_room(healthy_owner, "并行房间持续被调度")
        bad_code = bad_room["code"]
        healthy_code = healthy_room["code"]
        check(post(bad_owner, f"/api/rooms/{bad_code}/ready", {"ready": True}))
        check(post(bad_owner, f"/api/rooms/{bad_code}/start", {}))

        with SessionLocal() as db:
            room = load_room(db, bad_code, lock=True)
            match = db.scalar(select(Match).where(Match.room_id == room.id))
            match_ids.append(match.id)
            room.status = "running"
            room.current_stage_index = 0
            room.stage_started_at = now()
            room.stage_deadline_at = now() + timedelta(seconds=90)
            db.add(
                Speech(
                    match_id=match.id,
                    room_id=room.id,
                    seat_key="aff_1",
                    stage_key=room.template_snapshot[0]["key"],
                    speaker_type="human",
                    status="speaking",
                )
            )
            db.commit()

        async def verify_scheduler() -> dict:
            healthy_runs = 0

            async def drain_tick() -> None:
                await match_engine.tick()
                if match_engine._room_tasks:
                    await asyncio.gather(*list(match_engine._room_tasks.values()), return_exceptions=True)
                await asyncio.sleep(0)

            async def unexpected_failure(code: str) -> None:
                nonlocal healthy_runs
                if code == bad_code:
                    raise RuntimeError("production-safe simulated invariant violation")
                if code == healthy_code:
                    healthy_runs += 1

            original_process_room = match_engine.process_room
            match_engine.process_room = unexpected_failure
            try:
                for _ in range(3):
                    match_engine._room_retry_at[bad_code] = 0
                    await drain_tick()
            finally:
                match_engine.process_room = original_process_room

            with SessionLocal() as db:
                stored_bad = load_room(db, bad_code)
                stored_healthy = load_room(db, healthy_code)
                speech = db.scalar(select(Speech).where(Speech.room_id == stored_bad.id))
                quarantines = list(
                    db.scalars(
                        select(MatchEvent).where(
                            MatchEvent.room_id == stored_bad.id,
                            MatchEvent.event_type == "engine.quarantined",
                        )
                    ).all()
                )
                assert stored_bad.status == "paused"
                assert stored_bad.stage_deadline_at is None
                assert stored_bad.paused_remaining_seconds and stored_bad.paused_remaining_seconds > 0
                assert speech and speech.status == "interrupted"
                assert len(quarantines) == 1 and quarantines[0].payload["attempts"] == 3
                assert stored_healthy.status == "lobby"
            assert healthy_runs == 3

            transient_delays: list[float] = []

            async def transient_failure(code: str) -> None:
                if code == healthy_code:
                    raise OperationalError("SELECT room", {}, RuntimeError("production-safe simulated database busy"))

            match_engine.process_room = transient_failure
            try:
                for _ in range(4):
                    match_engine._room_retry_at[healthy_code] = 0
                    await drain_tick()
                    transient_delays.append(match_engine._room_retry_at[healthy_code] - time.monotonic())
            finally:
                match_engine.process_room = original_process_room

            with SessionLocal() as db:
                stored_healthy = load_room(db, healthy_code)
                quarantine = db.scalar(
                    select(MatchEvent).where(
                        MatchEvent.room_id == stored_healthy.id,
                        MatchEvent.event_type == "engine.quarantined",
                    )
                )
                assert stored_healthy.status == "lobby" and stored_healthy.failure_reason == ""
                assert quarantine is None
            assert all(actual > expected for actual, expected in zip(transient_delays, [0.8, 1.8, 3.8, 7.8]))
            await match_engine.stop()
            return {"healthy_runs": healthy_runs, "transient_backoff_seconds": [1, 2, 4, 8]}

        result = asyncio.run(verify_scheduler())
        retried = check(post(bad_owner, f"/api/rooms/{bad_code}/control/retry", {"reason": "生产验证修复后重试"}))["room"]
        assert retried["status"] == "running"
        check(post(bad_owner, f"/api/rooms/{bad_code}/control/terminate", {"reason": "production_engine_verification"}))
        return {
            "ok": True,
            "unexpected_failure_quarantined_after": 3,
            "healthy_room_runs": result["healthy_runs"],
            "transient_backoff_seconds": result["transient_backoff_seconds"],
            "manual_retry_succeeded": True,
            "external_providers_called": False,
        }
    finally:
        for client in clients:
            client.close()
        time.sleep(5)
        with SessionLocal() as db:
            all_match_ids = list(db.scalars(select(Match.id).where(Match.room_id.in_(room_ids))).all()) if room_ids else []
            match_ids[:] = sorted(set(match_ids + all_match_ids))
            speech_ids = list(db.scalars(select(Speech.id).where(Speech.room_id.in_(room_ids))).all()) if room_ids else []
            if speech_ids:
                db.execute(delete(TranscriptSegment).where(TranscriptSegment.speech_id.in_(speech_ids)))
            if match_ids:
                db.execute(delete(AudioAsset).where(AudioAsset.match_id.in_(match_ids)))
                db.execute(delete(RatingChange).where(RatingChange.match_id.in_(match_ids)))
                db.execute(delete(JudgeScorecard).where(JudgeScorecard.match_id.in_(match_ids)))
            if room_ids:
                db.execute(delete(Speech).where(Speech.room_id.in_(room_ids)))
                db.execute(delete(MatchEvent).where(MatchEvent.room_id.in_(room_ids)))
            if match_ids:
                db.execute(delete(Match).where(Match.id.in_(match_ids)))
            if room_ids:
                release_verification_room_codes(db, room_ids)
                db.execute(delete(Room).where(Room.id.in_(room_ids)))
            if user_ids:
                db.execute(delete(LeaderboardEntry).where(LeaderboardEntry.user_id.in_(user_ids)))
                db.execute(delete(UserSession).where(UserSession.user_id.in_(user_ids)))
                db.execute(delete(User).where(User.id.in_(user_ids)))
            db.commit()
        for code in room_codes:
            shutil.rmtree(settings.media_path / code, ignore_errors=True)
        for match_id in match_ids:
            for suffix in (".json", ".meta.json", ".json.sha256", ".part"):
                (settings.archive_path / f"{match_id}{suffix}").unlink(missing_ok=True)
            (settings.archive_path / ".locks" / archive_lock_name(match_id)).unlink(missing_ok=True)


if __name__ == "__main__":
    print(run())
