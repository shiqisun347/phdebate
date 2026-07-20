#!/usr/bin/env python3
"""Verify frozen room seasons and season-isolated rankings in one rolled-back transaction."""

from __future__ import annotations

from datetime import timedelta
from secrets import token_hex

from app.core.database import SessionLocal
from app.models.entities import Competition, LeaderboardEntry, Match, Room, Season, User
from app.services.room_service import leaderboard, now, room_code
from app.services.seasons import season_is_open
from sqlalchemy import select


def main() -> None:
    with SessionLocal() as db:
        try:
            clock = now()
            suffix = token_hex(5)
            first = Season(
                name=f"事务验收赛季 A {suffix}",
                slug=f"verify-a-{suffix}",
                starts_at=clock - timedelta(days=1),
                ends_at=clock + timedelta(days=1),
                is_active=True,
            )
            second = Season(
                name=f"事务验收赛季 B {suffix}",
                slug=f"verify-b-{suffix}",
                starts_at=clock - timedelta(hours=1),
                ends_at=clock + timedelta(days=2),
                is_active=True,
            )
            db.add_all([first, second])
            db.flush()
            assert season_is_open(first) and season_is_open(second)

            daily = db.scalar(select(Competition).where(Competition.slug == "daily-4v4"))
            training = db.scalar(select(Competition).where(Competition.slug == "training-1v1"))
            user = db.scalar(select(User).order_by(User.created_at, User.id).limit(1))
            if not daily or not training or not user:
                raise RuntimeError("seeded competitions and at least one user are required")

            room = Room(
                code=room_code(db),
                competition_id=daily.id,
                season_id=first.id,
                owner_id=user.id,
                topic="房间赛季快照是否能抵抗赛事热切换？",
                visibility="private",
                template_snapshot=[],
            )
            db.add(room)
            db.flush()
            daily.season_id = second.id
            db.flush()
            assert room.season_id == first.id

            match = Match(
                room_id=room.id,
                competition_id=daily.id,
                season_id=room.season_id,
                status="running",
            )
            db.add(match)
            db.add_all(
                [
                    LeaderboardEntry(
                        competition_id=daily.id,
                        season_id=first.id,
                        user_id=user.id,
                        points=3,
                        wins=1,
                        matches=1,
                        average_score=90,
                        last_match_at=clock,
                    ),
                    LeaderboardEntry(
                        competition_id=training.id,
                        season_id=first.id,
                        user_id=user.id,
                        points=1,
                        draws=1,
                        matches=1,
                        average_score=80,
                        last_match_at=clock - timedelta(hours=1),
                    ),
                ]
            )
            db.flush()

            global_rows = leaderboard(db, season_id=first.id)
            competition_rows = leaderboard(db, competition_id=daily.id, season_id=first.id)
            assert len(global_rows) == 1
            assert global_rows[0]["points"] == 4
            assert global_rows[0]["matches"] == 2
            assert global_rows[0]["average_score"] == 85
            assert len(competition_rows) == 1 and competition_rows[0]["points"] == 3
            assert leaderboard(db, competition_id=daily.id, season_id=second.id) == []
            print("season_lifecycle_verified frozen_room=1 isolated_rankings=1 global_user_dedup=1 transaction=rolled_back")
        finally:
            db.rollback()


if __name__ == "__main__":
    main()
