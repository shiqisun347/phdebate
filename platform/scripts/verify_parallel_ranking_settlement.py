#!/usr/bin/env python3
"""Verify that two rooms can settle the same human participant without lost updates."""

from __future__ import annotations

import argparse
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from secrets import randbelow, token_hex

import httpx
from app.core.database import SessionLocal
from app.models.entities import (
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
from app.services.match_engine import match_engine
from app.services.room_service import load_room, now
from app.services.verification_cleanup import release_verification_room_codes
from sqlalchemy import delete, select

PASSWORD = "Parallel-ranking-1234"


def unique_room_code(db) -> str:
    while True:
        code = f"{randbelow(900_000) + 100_000:06d}"
        if not db.scalar(select(Room.id).where(Room.code == code)):
            return code


def settle(code: str, ready: threading.Barrier) -> None:
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        scorecard = db.scalar(select(JudgeScorecard).where(JudgeScorecard.match_id == match.id))
        ready.wait(timeout=15)
        match_engine._apply_ranking(db, room, match, scorecard)
        db.commit()


def main() -> None:
    if os.geteuid() == 0:
        raise SystemExit("请使用 平台 服务账号运行排行榜并发验收。")
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://117.50.192.216")
    args = parser.parse_args()
    suffix = f"{int(time.time())}_{token_hex(3)}"
    client = httpx.Client(base_url=args.base_url, verify=False, timeout=30, follow_redirects=True)
    user_id = ""
    room_ids: list[str] = []
    match_ids: list[str] = []
    codes: list[str] = []
    try:
        registered = client.post(
            "/api/auth/register",
            json={
                "account": f"ranking_{suffix}",
                "real_name": "并发排行榜验收选手",
                "password": PASSWORD,
                "confirm_password": PASSWORD,
            },
        )
        registered.raise_for_status()
        user_id = registered.json()["user"]["id"]
        with SessionLocal() as db:
            competition = db.scalar(select(Competition).where(Competition.slug == "daily-4v4"))
            if not competition or not competition.ranked:
                raise RuntimeError("缺少启用排名的 4v4 日常赛配置")
            for index, winner in enumerate(("aff", "neg")):
                room = Room(
                    code=unique_room_code(db),
                    competition_id=competition.id,
                    season_id=competition.season_id,
                    owner_id=user_id,
                    topic=f"同一选手第 {index + 1} 场比赛并发结算是否准确？",
                    status="completed",
                    visibility="private",
                    template_snapshot=[],
                    started_at=now(),
                    completed_at=now(),
                )
                db.add(room)
                db.flush()
                room_ids.append(room.id)
                codes.append(room.code)
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
                                display_name="并发排行榜验收选手" if human else f"AI {side}-{position}",
                                is_ready=True,
                            )
                        )
                match = Match(
                    room_id=room.id,
                    competition_id=competition.id,
                    season_id=competition.season_id,
                    status="completed",
                    winner=winner,
                    result_reason="并发排行榜结算验收",
                )
                db.add(match)
                db.flush()
                match_ids.append(match.id)
                db.add(
                    JudgeScorecard(
                        match_id=match.id,
                        status="approved",
                        winner=winner,
                        affirmative_score=90 if index == 0 else 70,
                        negative_score=80 if index == 0 else 92,
                        reasoning="并发排行榜结算验收",
                    )
                )
            db.commit()

        barrier = threading.Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda code: settle(code, barrier), codes))
        replay_barrier = threading.Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda code: settle(code, replay_barrier), codes))

        with SessionLocal() as db:
            competition = db.scalar(select(Competition).where(Competition.slug == "daily-4v4"))
            entry = db.scalar(
                select(LeaderboardEntry).where(
                    LeaderboardEntry.competition_id == competition.id,
                    LeaderboardEntry.season_id == competition.season_id,
                    LeaderboardEntry.user_id == user_id,
                )
            )
            changes = list(db.scalars(select(RatingChange).where(RatingChange.match_id.in_(match_ids))).all())
            assert entry and entry.matches == 2 and entry.points == 3
            assert entry.wins == 1 and entry.draws == 0 and entry.losses == 1
            assert abs(entry.average_score - 80.0) < 1e-9
            assert len(changes) == 2 and all(item.source == "initial" for item in changes)
        print(
            "parallel_ranking_settlement_verified rooms=2 shared_user=1 settlements=2 "
            "replays=2 lost_updates=0 duplicate_changes=0 final_matches=2 final_points=3"
        )
    finally:
        client.close()
        if user_id:
            with SessionLocal() as db:
                cleanup_room_ids = list(db.scalars(select(Room.id).where(Room.owner_id == user_id)).all())
                cleanup_match_ids = list(db.scalars(select(Match.id).where(Match.room_id.in_(cleanup_room_ids))).all())
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


if __name__ == "__main__":
    main()
