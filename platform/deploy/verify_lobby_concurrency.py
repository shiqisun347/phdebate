"""Production-safe lobby concurrency verifier.

Runs against the deployed HTTPS API, asserts PostgreSQL-backed room invariants,
then removes every temporary user, room, media file and archive it created.
It never advances far enough to invoke a debater Agent.
"""

from __future__ import annotations

import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
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
    RoomSeat,
    Speech,
    TranscriptSegment,
    User,
    UserSession,
)
from app.services.match_archive import archive_lock_name
from app.services.verification_cleanup import release_verification_room_codes
from sqlalchemy import delete, func, select

BASE_URL = os.getenv("VERIFY_BASE_URL", "https://117.50.192.216")
PASSWORD = "Verify-prod-1234"


def run() -> dict:
    clients: list[httpx.Client] = []
    users: list[dict] = []
    rooms: list[dict] = []
    match_ids: list[str] = []

    def check(response: httpx.Response, expected: int = 200) -> dict:
        if response.status_code != expected:
            raise RuntimeError(f"{response.request.method} {response.request.url.path}: {response.status_code} {response.text[:300]}")
        return response.json()

    def register(label: str) -> dict:
        client = httpx.Client(base_url=BASE_URL, verify=False, timeout=20, follow_redirects=True)
        account = f"race_{len(users)}_{uuid4().hex[:10]}"
        body = check(
            client.post(
                "/api/auth/register",
                json={
                    "account": account,
                    "real_name": f"并发验证{label}",
                    "password": PASSWORD,
                    "confirm_password": PASSWORD,
                },
            )
        )
        item = {"client": client, "account": account, "id": body["user"]["id"], "csrf": body["csrf_token"]}
        clients.append(client)
        users.append(item)
        return item

    def login(account: str) -> dict:
        client = httpx.Client(base_url=BASE_URL, verify=False, timeout=20, follow_redirects=True)
        body = check(client.post("/api/auth/login", json={"account": account, "password": PASSWORD}))
        item = {"client": client, "account": account, "id": body["user"]["id"], "csrf": body["csrf_token"]}
        clients.append(client)
        return item

    def post(user: dict, path: str, payload: dict) -> httpx.Response:
        return user["client"].post(path, headers={"X-CSRF-Token": user["csrf"]}, json=payload)

    def create_room(owner: dict, label: str) -> dict:
        body = check(
            post(
                owner,
                "/api/rooms",
                {
                    "competition_slug": "training-1v1",
                    "custom_topic": f"生产并发验证：{label}",
                    "seat_key": "aff_1",
                    "visibility": "public",
                },
            )
        )
        item = {"id": body["room"]["id"], "code": body["room"]["code"], "owner": owner}
        rooms.append(item)
        return item

    try:
        owners = [register(f"跨房房主{i}") for i in range(4)]
        cross_rooms = [create_room(owner, f"同账号跨房间抢座 {index}") for index, owner in enumerate(owners)]
        participant = register("同账号辩手")
        participant_devices = [participant, *(login(participant["account"]) for _ in range(3))]
        barrier = Barrier(4)

        def cross_claim(arguments: tuple[dict, dict]) -> httpx.Response:
            device, room = arguments
            barrier.wait()
            return post(device, f"/api/rooms/{room['code']}/claim-seat", {"seat_key": "neg_1"})

        with ThreadPoolExecutor(max_workers=4) as pool:
            cross_responses = list(pool.map(cross_claim, zip(participant_devices, cross_rooms)))
        assert sorted(item.status_code for item in cross_responses) == [200, 409, 409, 409]

        seat_owner = register("同席房主")
        seat_room = create_room(seat_owner, "六人同时抢同一席位")
        racers = [register(f"同席辩手{i}") for i in range(6)]
        barrier = Barrier(6)

        def same_seat_claim(racer: dict) -> httpx.Response:
            barrier.wait()
            return post(racer, f"/api/rooms/{seat_room['code']}/claim-seat", {"seat_key": "neg_1"})

        with ThreadPoolExecutor(max_workers=6) as pool:
            seat_responses = list(pool.map(same_seat_claim, racers))
        assert sorted(item.status_code for item in seat_responses) == [200, 409, 409, 409, 409, 409]

        race_owner = register("开始关闭房主")
        race_room = create_room(race_owner, "开始与关闭同时提交")
        check(post(race_owner, f"/api/rooms/{race_room['code']}/ready", {"ready": True}))
        second_owner_device = login(race_owner["account"])
        barrier = Barrier(2)

        def control(arguments: tuple[dict, str]) -> httpx.Response:
            device, action = arguments
            barrier.wait()
            return post(device, f"/api/rooms/{race_room['code']}/{action}", {})

        with ThreadPoolExecutor(max_workers=2) as pool:
            control_responses = list(pool.map(control, [(race_owner, "start"), (second_owner_device, "cancel")]))
        assert sorted(item.status_code for item in control_responses) == [200, 409]

        with SessionLocal() as db:
            participant_assignment_count = db.scalar(
                select(func.count())
                .select_from(RoomSeat)
                .join(Room, Room.id == RoomSeat.room_id)
                .where(
                    RoomSeat.user_id == participant["id"],
                    RoomSeat.occupant_type == "human",
                    Room.status.in_(["lobby", "preparing", "running", "paused", "judging"]),
                )
            )
            seat_claim_count = db.scalar(
                select(func.count())
                .select_from(MatchEvent)
                .where(MatchEvent.room_id == seat_room["id"], MatchEvent.event_type == "seat.claimed")
            )
            race = db.get(Room, race_room["id"])
            race_matches = list(db.scalars(select(Match).where(Match.room_id == race.id)).all())
            race_locked = db.scalar(
                select(func.count()).select_from(MatchEvent).where(MatchEvent.room_id == race.id, MatchEvent.event_type == "room.locked")
            )
            race_cancelled = db.scalar(
                select(func.count()).select_from(MatchEvent).where(MatchEvent.room_id == race.id, MatchEvent.event_type == "room.cancelled")
            )
            assert participant_assignment_count == 1
            assert seat_claim_count == 1
            assert (race.status == "cancelled" and not race_matches and race_locked == 0 and race_cancelled == 1) or (
                race.status == "preparing" and len(race_matches) == 1 and race_locked == 1 and race_cancelled == 0
            )
            match_ids.extend(item.id for item in race_matches)

        current = check(race_owner["client"].get(f"/api/rooms/{race_room['code']}"))["room"]
        if current["status"] in {"preparing", "running", "paused", "judging"}:
            terminated = post(
                race_owner,
                f"/api/rooms/{race_room['code']}/control/terminate",
                {"reason": "production_concurrency_verification"},
            )
            assert terminated.status_code == 200, terminated.text

        return {
            "ok": True,
            "same_user_cross_room": [item.status_code for item in cross_responses],
            "same_seat_six_users": [item.status_code for item in seat_responses],
            "start_cancel_race": [item.status_code for item in control_responses],
            "active_assignments_for_shared_user": 1,
            "seat_claim_events": 1,
        }
    finally:
        for client in clients:
            client.close()
        time.sleep(5)
        room_ids = [item["id"] for item in rooms]
        user_ids = [item["id"] for item in users]
        room_codes = [item["code"] for item in rooms]
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
