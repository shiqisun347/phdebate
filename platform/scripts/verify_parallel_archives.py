#!/usr/bin/env python3
"""Verify concurrent multi-match archive generation, repair, and immutable downloads."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import time
from concurrent.futures import ThreadPoolExecutor
from secrets import token_hex

import httpx
from app.core.config import settings
from app.core.database import SessionLocal
from app.models.entities import Match, MatchEvent, Room, RoomSeat, Speech, User, UserSession
from app.services.archive_storage import inspect_archives
from app.services.match_archive import archive_lock_name, build_match_archive, read_match_archive
from app.services.room_service import append_event, load_room, now
from app.services.verification_cleanup import release_verification_room_codes
from sqlalchemy import delete, select

PASSWORD = "Parallel-archive-1234"


def csrf(client: httpx.AsyncClient) -> dict[str, str]:
    value = client.cookies.get("jixia_csrf")
    return {"X-CSRF-Token": value} if value else {}


async def register_and_create(client: httpx.AsyncClient, index: int, suffix: str) -> tuple[str, str]:
    registered = await client.post(
        "/api/auth/register",
        json={
            "account": f"archive_{index}_{suffix}",
            "real_name": f"并发归档验收选手{index + 1}",
            "password": PASSWORD,
            "confirm_password": PASSWORD,
        },
    )
    registered.raise_for_status()
    created = await client.post(
        "/api/rooms",
        headers=csrf(client) | {"X-Idempotency-Key": token_hex(16)},
        json={
            "competition_slug": "training-1v1",
            "custom_topic": f"第 {index + 1} 场比赛的档案能否与其他比赛并发且隔离生成？",
            "seat_key": "aff_1",
            "visibility": "private",
        },
    )
    created.raise_for_status()
    return registered.json()["user"]["id"], created.json()["room"]["code"]


async def main_async(base_url: str) -> None:
    suffix = f"{int(time.time())}_{token_hex(3)}"
    clients = [httpx.AsyncClient(base_url=base_url, verify=False, timeout=30, follow_redirects=True) for _ in range(4)]
    user_ids: list[str] = []
    room_ids: list[str] = []
    match_ids: list[str] = []
    codes: list[str] = []
    try:
        created = await asyncio.gather(*(register_and_create(client, index, suffix) for index, client in enumerate(clients)))
        user_ids = [item[0] for item in created]
        codes = [item[1] for item in created]
        with SessionLocal() as db:
            for index, code in enumerate(codes):
                room = load_room(db, code, lock=True)
                room_ids.append(room.id)
                room.status = "completed"
                room.started_at = now()
                room.completed_at = now()
                match = Match(
                    room_id=room.id,
                    competition_id=room.competition_id,
                    season_id=room.season_id,
                    status="completed",
                    winner="aff" if index % 2 == 0 else "neg",
                    result_reason=f"并发归档验收结果 {index + 1}",
                )
                db.add(match)
                db.flush()
                match_ids.append(match.id)
                db.add(
                    Speech(
                        match_id=match.id,
                        room_id=room.id,
                        seat_key="aff_1",
                        stage_key="archive_verification",
                        speaker_type="human",
                        status="completed",
                        content=f"第 {index + 1} 场归档初始内容。",
                    )
                )
                append_event(db, room, "match.completed", {"winner": match.winner, "verification": True})
            db.commit()

        with ThreadPoolExecutor(max_workers=16) as pool:
            first_results = list(pool.map(build_match_archive, match_ids * 4))
        for match_id in match_ids:
            per_match = [item for item in first_results if item.path.name == f"{match_id}.json"]
            assert len(per_match) == 4
            assert sum(not item.reused for item in per_match) == 1
            assert len({item.sha256 for item in per_match}) == 1

        checksum = settings.archive_path / f"{match_ids[0]}.json.sha256"
        checksum.unlink()
        with SessionLocal() as db:
            broken = inspect_archives(db)
        assert any(item["match_id"] == match_ids[0] for item in broken["invalid_archives"])
        repaired = build_match_archive(match_ids[0])
        assert repaired.reused and checksum.exists()

        with SessionLocal() as db:
            speeches = list(db.scalars(select(Speech).where(Speech.match_id.in_(match_ids))).all())
            for index, speech in enumerate(speeches):
                speech.content = f"第 {index + 1} 场并发修订后的最终内容。"
            db.commit()
        with ThreadPoolExecutor(max_workers=16) as pool:
            updated_results = list(pool.map(build_match_archive, match_ids * 4))
        for match_id in match_ids:
            per_match = [item for item in updated_results if item.path.name == f"{match_id}.json"]
            assert sum(not item.reused for item in per_match) == 1
            assert len({item.source_sha256 for item in per_match}) == 1

        with ThreadPoolExecutor(max_workers=16) as pool:
            payloads = list(pool.map(read_match_archive, match_ids * 4))
        assert all(hashlib.sha256(item.content).hexdigest() == item.result.sha256 for item in payloads)
        downloads = await asyncio.gather(*(client.get(f"/api/matches/{match_id}/archive") for client, match_id in zip(clients, match_ids)))
        for response in downloads:
            response.raise_for_status()
            assert response.headers["x-archive-sha256"] == hashlib.sha256(response.content).hexdigest()
            assert response.headers["content-disposition"].startswith("attachment;")
        with SessionLocal() as db:
            final_status = inspect_archives(db)
        invalid_ids = {item["match_id"] for item in final_status["invalid_archives"]}
        assert not invalid_ids.intersection(match_ids)
        print(
            "parallel_archives_verified rooms=4 builds=16 regenerated_per_room=1 "
            "checksum_repaired=1 immutable_downloads=16 http_downloads=4 cross_room_collisions=0"
        )
    finally:
        await asyncio.gather(*(client.aclose() for client in clients))
        if user_ids:
            with SessionLocal() as db:
                cleanup_room_ids = list(db.scalars(select(Room.id).where(Room.owner_id.in_(user_ids))).all())
                cleanup_match_ids = list(db.scalars(select(Match.id).where(Match.room_id.in_(cleanup_room_ids))).all())
                if cleanup_room_ids:
                    db.execute(delete(Speech).where(Speech.room_id.in_(cleanup_room_ids)))
                    db.execute(delete(MatchEvent).where(MatchEvent.room_id.in_(cleanup_room_ids)))
                    db.execute(delete(Match).where(Match.id.in_(cleanup_match_ids)))
                    db.execute(delete(RoomSeat).where(RoomSeat.room_id.in_(cleanup_room_ids)))
                    release_verification_room_codes(db, cleanup_room_ids)
                    db.execute(delete(Room).where(Room.id.in_(cleanup_room_ids)))
                db.execute(delete(UserSession).where(UserSession.user_id.in_(user_ids)))
                db.execute(delete(User).where(User.id.in_(user_ids)))
                db.commit()
                match_ids = list(set(match_ids).union(cleanup_match_ids))
        for match_id in match_ids:
            for suffix_name in (".json", ".meta.json", ".json.sha256"):
                (settings.archive_path / f"{match_id}{suffix_name}").unlink(missing_ok=True)
            (settings.archive_path / ".locks" / archive_lock_name(match_id)).unlink(missing_ok=True)


def main() -> None:
    if os.geteuid() == 0:
        raise SystemExit("请使用 平台 服务账号运行归档验收，避免生成 root 所有权的运行文件。")
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://117.50.192.216")
    args = parser.parse_args()
    asyncio.run(main_async(args.base_url))


if __name__ == "__main__":
    main()
