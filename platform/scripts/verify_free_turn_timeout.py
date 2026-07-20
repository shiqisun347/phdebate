#!/usr/bin/env python3
"""Verify live human free-debate turn timeout without calling an Agent."""

from __future__ import annotations

import argparse
import time
from datetime import timedelta
from secrets import token_hex

import httpx
from app.core.database import SessionLocal
from app.models.entities import AudioAsset, JudgeScorecard, Match, MatchEvent, RatingChange, Room, RoomSeat, Speech, User, UserSession
from app.services.room_service import load_room, now
from app.services.verification_cleanup import release_verification_room_codes
from sqlalchemy import delete, select


def csrf(client: httpx.Client) -> dict[str, str]:
    value = client.cookies.get("jixia_csrf")
    return {"X-CSRF-Token": value} if value else {}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://117.50.192.216")
    args = parser.parse_args()
    account = f"free_timeout_{int(time.time())}_{token_hex(3)}"
    password = "Free-timeout-1234"
    user_id = room_id = match_id = ""
    client = httpx.Client(base_url=args.base_url, verify=False, timeout=20, follow_redirects=True)
    try:
        registered = client.post(
            "/api/auth/register",
            json={
                "account": account,
                "real_name": "自由辩论超时验收",
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
                "custom_topic": "自由辩论是否需要限制单轮发言时间？",
                "seat_key": "aff_1",
                "visibility": "public",
            },
        )
        created.raise_for_status()
        code = created.json()["room"]["code"]
        with SessionLocal() as db:
            room = load_room(db, code, lock=True)
            room_id = room.id
            room.template_snapshot = [
                {"key": "free", "name": "自由辩论", "kind": "free", "side": "aff", "duration": 300, "turn_duration": 5}
            ]
            room.status = "running"
            room.current_stage_index = 0
            room.stage_started_at = now()
            room.stage_deadline_at = now() + timedelta(seconds=300)
            match = Match(
                room_id=room.id,
                competition_id=room.competition_id,
                season_id=room.season_id,
                status="running",
            )
            db.add(match)
            db.commit()
            match_id = match.id

        initial = client.get(f"/api/rooms/{code}").json()["room"]
        initial_turn = initial["turn_remaining_seconds"]
        assert initial_turn in {4, 5}
        time.sleep(1.1)
        paused = client.post(
            f"/api/rooms/{code}/control/pause",
            headers=csrf(client) | {"X-Idempotency-Key": "live-free-pause"},
            json={},
        )
        paused.raise_for_status()
        paused_turn = paused.json()["room"]["turn_remaining_seconds"]
        assert paused_turn in {initial_turn - 2, initial_turn - 1, initial_turn}
        time.sleep(1.1)
        still_paused = client.get(f"/api/rooms/{code}").json()["room"]
        assert still_paused["turn_remaining_seconds"] == paused_turn
        resumed = client.post(
            f"/api/rooms/{code}/control/resume",
            headers=csrf(client) | {"X-Idempotency-Key": "live-free-resume"},
            json={},
        )
        resumed.raise_for_status()
        resumed_turn = resumed.json()["room"]["turn_remaining_seconds"]
        assert resumed_turn in {max(0, paused_turn - 1), paused_turn}

        lease_headers = csrf(client) | {"X-Control-Lease": "live-free-device"}
        client.post(f"/api/rooms/{code}/control-lease", headers=lease_headers, json={}).raise_for_status()
        started = client.post(f"/api/rooms/{code}/speech/start", headers=lease_headers, json={})
        started.raise_for_status()
        speech_id = started.json()["speech_id"]
        view = client.get(f"/api/rooms/{code}").json()["room"]
        assert view["turn_remaining_seconds"] <= resumed_turn
        with SessionLocal() as db:
            room = load_room(db, code, lock=True)
            current = dict(room.template_snapshot[room.current_stage_index])
            current["turn_started_at"] = (now() - timedelta(seconds=6)).isoformat()
            snapshot = list(room.template_snapshot)
            snapshot[room.current_stage_index] = current
            room.template_snapshot = snapshot
            db.commit()

        deadline = time.monotonic() + 10
        timed_out_view = None
        while time.monotonic() < deadline:
            timed_out_view = client.get(f"/api/rooms/{code}").json()["room"]
            if timed_out_view["current_stage"]["side"] == "neg" and timed_out_view["active_speech"] is None:
                break
            time.sleep(0.25)
        assert timed_out_view and timed_out_view["current_stage"]["side"] == "neg"
        finish_headers = lease_headers | {"X-Idempotency-Key": "live-late-finish"}
        finalized = client.post(
            f"/api/rooms/{code}/speech/finish",
            headers=finish_headers,
            json={"speech_id": speech_id, "content": "线上超时后自动补交内容。"},
        )
        finalized.raise_for_status()
        assert finalized.json()["timed_out"] is True
        with SessionLocal() as db:
            speech = db.get(Speech, speech_id)
            assert speech.status == "completed" and speech.content == "线上超时后自动补交内容。"

            room = load_room(db, code, lock=True)
            current = dict(room.template_snapshot[room.current_stage_index])
            assert current["side"] == "neg"
            current["turn_started_at"] = (now() - timedelta(seconds=6)).isoformat()
            snapshot = list(room.template_snapshot)
            snapshot[room.current_stage_index] = current
            room.template_snapshot = snapshot
            db.commit()

        deadline = time.monotonic() + 10
        idle_timed_out_view = None
        while time.monotonic() < deadline:
            idle_timed_out_view = client.get(f"/api/rooms/{code}").json()["room"]
            if idle_timed_out_view["current_stage"]["side"] == "aff":
                break
            time.sleep(0.25)
        assert idle_timed_out_view and idle_timed_out_view["current_stage"]["side"] == "aff"
        with SessionLocal() as db:
            event = db.scalar(
                select(MatchEvent).where(
                    MatchEvent.room_id == room_id,
                    MatchEvent.event_type == "free.turn_timed_out",
                )
            )
            assert event is not None
        print(f"free_turn_timeout_verified pause={paused_turn}s resume={resumed_turn}s active=aff->neg idle=neg->aff late_finalize=200")
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
