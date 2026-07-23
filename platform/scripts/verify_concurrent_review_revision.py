#!/usr/bin/env python3
"""Verify optimistic review revisions under concurrent administrator decisions."""

from __future__ import annotations

import argparse
import os
import time
from concurrent.futures import ThreadPoolExecutor
from secrets import randbelow, token_hex

import httpx
from app.core.config import settings
from app.core.database import SessionLocal
from app.models.entities import (
    AdminAuditLog,
    Competition,
    JudgeScorecard,
    LeaderboardEntry,
    Match,
    MatchEvent,
    RatingChange,
    Room,
    RoomSeat,
    Speech,
    User,
    UserSession,
)
from app.services.match_archive import archive_lock_name, build_match_archive
from app.services.room_service import now
from app.services.verification_cleanup import release_verification_room_codes
from operator_credentials import admin_credentials
from sqlalchemy import delete, func, select

PASSWORD = "Concurrent-review-1234"


def csrf(client: httpx.Client) -> dict[str, str]:
    value = client.cookies.get("jixia_csrf")
    return {"X-CSRF-Token": value} if value else {}


def unique_room_code(db) -> str:
    while True:
        code = f"{randbelow(900_000) + 100_000:06d}"
        if not db.scalar(select(Room.id).where(Room.code == code)):
            return code


def post_review(client: httpx.Client, path: str, payload: dict) -> httpx.Response:
    return client.post(path, headers=csrf(client), json=payload)


def main() -> None:
    if os.geteuid() == 0:
        raise SystemExit("请使用 平台 服务账号运行裁判并发验收。")
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://117.50.192.216")
    args = parser.parse_args()
    clients = [httpx.Client(base_url=args.base_url, verify=False, timeout=30, follow_redirects=True) for _ in range(3)]
    participant, admin_a, admin_b = clients
    user_id = ""
    room_id = ""
    match_id = ""
    scorecard_id = ""
    try:
        admin_account, admin_password = admin_credentials()
        suffix = f"{int(time.time())}_{token_hex(3)}"
        registered = participant.post(
            "/api/auth/register",
            json={
                "account": f"reviewrace_{suffix}",
                "real_name": "并发复核验收选手",
                "password": PASSWORD,
                "confirm_password": PASSWORD,
            },
        )
        registered.raise_for_status()
        user_id = registered.json()["user"]["id"]
        for admin in (admin_a, admin_b):
            logged_in = admin.post(
                "/api/auth/login",
                json={"account": admin_account, "password": admin_password},
            )
            logged_in.raise_for_status()

        with SessionLocal() as db:
            competition = db.scalar(select(Competition).where(Competition.slug == "daily-4v4"))
            room = Room(
                code=unique_room_code(db),
                competition_id=competition.id,
                season_id=competition.season_id,
                owner_id=user_id,
                topic="两位管理员同时复核时是否只接受基于当前版本的决定？",
                status="review_required",
                visibility="private",
                template_snapshot=[],
                started_at=now(),
                completed_at=now(),
            )
            db.add(room)
            db.flush()
            room_id = room.id
            for side in ("aff", "neg"):
                for position in range(1, 5):
                    human = side == "aff" and position == 1
                    db.add(
                        RoomSeat(
                            room_id=room.id,
                            seat_key=f"{side}_{position}",
                            side=side,
                            position=position,
                            occupant_type="human" if human else "ai",
                            user_id=user_id if human else None,
                            display_name="并发复核验收选手" if human else f"AI {side}-{position}",
                            is_ready=True,
                        )
                    )
            match = Match(
                room_id=room.id,
                competition_id=competition.id,
                season_id=competition.season_id,
                status="review_required",
            )
            db.add(match)
            db.flush()
            match_id = match.id
            scorecard = JudgeScorecard(match_id=match.id, status="review_required", reasoning="等待管理员并发复核验收")
            db.add(scorecard)
            db.commit()
            scorecard_id = scorecard.id

        listing = admin_a.get("/api/admin/reviews")
        listing.raise_for_status()
        revision = next(item for item in listing.json()["items"] if item["scorecard_id"] == scorecard_id)["updated_at"]
        approve_path = f"/api/admin/reviews/{scorecard_id}/approve"
        approve_payloads = [
            {
                "winner": "aff",
                "affirmative_score": 90,
                "negative_score": 82,
                "reasoning": "管理员 A 的并发复核决定。",
                "expected_updated_at": revision,
            },
            {
                "winner": "neg",
                "affirmative_score": 81,
                "negative_score": 91,
                "reasoning": "管理员 B 的并发复核决定。",
                "expected_updated_at": revision,
            },
        ]
        with ThreadPoolExecutor(max_workers=2) as pool:
            approve_responses = list(
                pool.map(
                    lambda item: post_review(item[0], approve_path, item[1]),
                    zip((admin_a, admin_b), approve_payloads),
                )
            )
        assert sorted(item.status_code for item in approve_responses) == [200, 409]

        recent = admin_a.get("/api/admin/reviews")
        recent.raise_for_status()
        correction_revision = next(item for item in recent.json()["recent"] if item["scorecard_id"] == scorecard_id)["updated_at"]
        correction_path = f"/api/admin/reviews/{scorecard_id}/correct"
        correction_payloads = [
            {
                "winner": "draw",
                "affirmative_score": 84,
                "negative_score": 84,
                "reasoning": "管理员 A 修正为平局。",
                "expected_updated_at": correction_revision,
            },
            {
                "winner": "draw",
                "affirmative_score": 86,
                "negative_score": 86,
                "reasoning": "管理员 B 修正为平局。",
                "expected_updated_at": correction_revision,
            },
        ]
        with ThreadPoolExecutor(max_workers=2) as pool:
            correction_responses = list(
                pool.map(
                    lambda item: post_review(item[0], correction_path, item[1]),
                    zip((admin_a, admin_b), correction_payloads),
                )
            )
        assert sorted(item.status_code for item in correction_responses) == [200, 409]
        conflicts = [item for item in approve_responses + correction_responses if item.status_code == 409]
        assert all("已被其他管理员更新" in item.json()["detail"] for item in conflicts)
        time.sleep(0.5)
        build_match_archive(match_id)

        with SessionLocal() as db:
            scorecard = db.get(JudgeScorecard, scorecard_id)
            match = db.get(Match, match_id)
            entry = db.scalar(select(LeaderboardEntry).where(LeaderboardEntry.user_id == user_id))
            changes = list(db.scalars(select(RatingChange).where(RatingChange.match_id == match_id)).all())
            reviewed_events = db.scalar(
                select(func.count(MatchEvent.id)).where(
                    MatchEvent.room_id == room_id,
                    MatchEvent.event_type.in_(["judge.reviewed", "judge.corrected"]),
                )
            )
            audit_count = db.scalar(
                select(func.count(AdminAuditLog.id)).where(
                    AdminAuditLog.target_id == scorecard_id,
                    AdminAuditLog.action.in_(["judge.approve", "judge.correct"]),
                )
            )
            assert scorecard and match and scorecard.winner == match.winner == "draw"
            assert entry and entry.matches == 1 and entry.points == 1 and entry.draws == 1
            assert len(changes) == 2 and {item.source for item in changes} == {"initial", "correction"}
            assert reviewed_events == audit_count == 2
        print(
            "concurrent_review_revision_verified approve_success=1 approve_conflict=1 "
            "correction_success=1 correction_conflict=1 rating_changes=2 final_points=1"
        )
    finally:
        for admin in (admin_a, admin_b):
            token = admin.cookies.get("jixia_csrf")
            if token:
                admin.post("/api/auth/logout", headers={"X-CSRF-Token": token})
        for client in clients:
            client.close()
        if user_id:
            with SessionLocal() as db:
                cleanup_room_ids = list(db.scalars(select(Room.id).where(Room.owner_id == user_id)).all())
                cleanup_match_ids = list(db.scalars(select(Match.id).where(Match.room_id.in_(cleanup_room_ids))).all())
                cleanup_scorecard_ids = list(
                    db.scalars(select(JudgeScorecard.id).where(JudgeScorecard.match_id.in_(cleanup_match_ids))).all()
                )
                db.execute(delete(AdminAuditLog).where(AdminAuditLog.target_id.in_(cleanup_scorecard_ids)))
                db.execute(delete(RatingChange).where(RatingChange.match_id.in_(cleanup_match_ids)))
                db.execute(delete(LeaderboardEntry).where(LeaderboardEntry.user_id == user_id))
                db.execute(delete(JudgeScorecard).where(JudgeScorecard.match_id.in_(cleanup_match_ids)))
                db.execute(delete(Speech).where(Speech.room_id.in_(cleanup_room_ids)))
                db.execute(delete(MatchEvent).where(MatchEvent.room_id.in_(cleanup_room_ids)))
                db.execute(delete(Match).where(Match.id.in_(cleanup_match_ids)))
                db.execute(delete(RoomSeat).where(RoomSeat.room_id.in_(cleanup_room_ids)))
                release_verification_room_codes(db, cleanup_room_ids)
                db.execute(delete(Room).where(Room.id.in_(cleanup_room_ids)))
                db.execute(delete(UserSession).where(UserSession.user_id == user_id))
                db.execute(delete(User).where(User.id == user_id))
                db.commit()
                match_id = match_id or (cleanup_match_ids[0] if cleanup_match_ids else "")
        if match_id:
            for suffix in (".json", ".meta.json", ".json.sha256"):
                (settings.archive_path / f"{match_id}{suffix}").unlink(missing_ok=True)
            (settings.archive_path / ".locks" / archive_lock_name(match_id)).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
