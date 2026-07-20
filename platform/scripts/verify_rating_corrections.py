#!/usr/bin/env python3
"""Verify append-only rating corrections against the configured database.

The whole verification runs inside one transaction and is always rolled back, so
it is safe to use against a paused production room with no existing settlement.
"""

from __future__ import annotations

import argparse
from uuid import uuid4

from app.core.database import SessionLocal
from app.models.entities import JudgeScorecard, LeaderboardEntry, Match, RatingChange
from app.services.match_engine import match_engine
from app.services.room_service import load_room
from sqlalchemy import select


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--room-code", required=True)
    args = parser.parse_args()

    with SessionLocal() as db:
        try:
            room = load_room(db, args.room_code, lock=True)
            if not room.competition.ranked:
                raise RuntimeError("verification room must belong to a ranked competition")
            match = db.scalar(select(Match).where(Match.room_id == room.id))
            if not match:
                raise RuntimeError("verification room has no match")
            seat = next(
                (item for item in room.seats if item.user_id and item.occupant_type in {"human", "ai_substitute"}),
                None,
            )
            if not seat:
                raise RuntimeError("verification room has no human participant")
            if db.scalar(
                select(RatingChange.id).where(
                    RatingChange.match_id == match.id,
                    RatingChange.user_id == seat.user_id,
                )
            ):
                raise RuntimeError("verification room already has a rating settlement")

            baseline = db.scalar(
                select(LeaderboardEntry).where(
                    LeaderboardEntry.competition_id == room.competition_id,
                    LeaderboardEntry.season_id == match.season_id,
                    LeaderboardEntry.user_id == seat.user_id,
                )
            )
            baseline_values = {
                "points": baseline.points if baseline else 0,
                "matches": baseline.matches if baseline else 0,
                "wins": baseline.wins if baseline else 0,
                "draws": baseline.draws if baseline else 0,
                "losses": baseline.losses if baseline else 0,
                "average_score": baseline.average_score if baseline else 0.0,
            }

            match.winner = seat.side
            initial = JudgeScorecard(
                match_id=match.id,
                status="approved",
                winner=seat.side,
                affirmative_score=90,
                negative_score=80,
            )
            match_engine._apply_ranking(db, room, match, initial)
            db.flush()

            opposite = "neg" if seat.side == "aff" else "aff"
            match_engine._correct_ranking(
                db,
                room,
                match,
                correction_id=str(uuid4()),
                old_winner=seat.side,
                new_winner=opposite,
                old_affirmative_score=90,
                old_negative_score=80,
                new_affirmative_score=75,
                new_negative_score=93,
            )
            db.flush()
            match_engine._correct_ranking(
                db,
                room,
                match,
                correction_id=str(uuid4()),
                old_winner=opposite,
                new_winner="draw",
                old_affirmative_score=75,
                old_negative_score=93,
                new_affirmative_score=85,
                new_negative_score=85,
            )
            db.flush()

            changes = list(
                db.scalars(
                    select(RatingChange)
                    .where(RatingChange.match_id == match.id, RatingChange.user_id == seat.user_id)
                    .order_by(RatingChange.created_at, RatingChange.id)
                ).all()
            )
            entry = db.scalar(
                select(LeaderboardEntry).where(
                    LeaderboardEntry.competition_id == room.competition_id,
                    LeaderboardEntry.season_id == match.season_id,
                    LeaderboardEntry.user_id == seat.user_id,
                )
            )
            expected_score = (baseline_values["average_score"] * baseline_values["matches"] + 85) / (baseline_values["matches"] + 1)
            assert [(item.source, item.points_delta) for item in changes] == [
                ("initial", 3),
                ("correction", -3),
                ("correction", 1),
            ]
            assert entry
            assert entry.points == baseline_values["points"] + 1
            assert entry.matches == baseline_values["matches"] + 1
            assert entry.wins == baseline_values["wins"]
            assert entry.draws == baseline_values["draws"] + 1
            assert entry.losses == baseline_values["losses"]
            assert abs(entry.average_score - expected_score) < 1e-8
            print("rating_correction_verified changes=3 final_delta=1 transaction=rolled_back")
        finally:
            db.rollback()


if __name__ == "__main__":
    main()
