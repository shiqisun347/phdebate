#!/usr/bin/env python3
"""Run four different live room states concurrently and verify event isolation."""

from __future__ import annotations

import argparse
import asyncio
import json
import ssl
import time
from datetime import timedelta
from secrets import token_hex

import httpx
import websockets
from app.core.database import SessionLocal
from app.models.entities import (
    AudioAsset,
    JudgeScorecard,
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
from sqlalchemy import delete

PASSWORD = "Parallel-isolation-1234"


def csrf(client: httpx.AsyncClient) -> dict[str, str]:
    value = client.cookies.get("jixia_csrf")
    return {"X-CSRF-Token": value} if value else {}


async def register_and_create(client: httpx.AsyncClient, index: int, suffix: str) -> tuple[str, str]:
    registered = await client.post(
        "/api/auth/register",
        json={
            "account": f"parallel_{index}_{suffix}",
            "real_name": f"并行验收选手{index + 1}",
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
            "custom_topic": f"并行房间 {index + 1} 的状态是否与其他房间完全隔离？",
            "seat_key": "aff_1",
            "visibility": "public",
        },
    )
    created.raise_for_status()
    return registered.json()["user"]["id"], created.json()["room"]["code"]


async def watch_room(ws_base: str, code: str, ready: asyncio.Event, start: asyncio.Event) -> list[dict]:
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    events: list[dict] = []
    async with websockets.connect(
        f"{ws_base}/ws/rooms/{code}",
        ssl=ssl_context,
        open_timeout=15,
        close_timeout=3,
        ping_interval=None,
    ) as socket:
        initial = json.loads(await asyncio.wait_for(socket.recv(), timeout=15))
        assert initial["type"] == "snapshot" and initial["room"]["code"] == code
        ready.set()
        await start.wait()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                message = json.loads(await asyncio.wait_for(socket.recv(), timeout=min(1, deadline - time.monotonic())))
            except asyncio.TimeoutError:
                continue
            assert message.get("room", {}).get("code") == code, f"cross-room snapshot received in {code}: {message}"
            event = message.get("event")
            if not event:
                continue
            event_room = event.get("room_code")
            assert event_room in {None, code}, f"cross-room event received in {code}: {event}"
            events.append(event)
    return events


async def main_async(base_url: str) -> None:
    suffix = f"{int(time.time())}_{token_hex(3)}"
    clients = [httpx.AsyncClient(base_url=base_url, verify=False, timeout=20, follow_redirects=True) for _ in range(4)]
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
                match = Match(
                    room_id=room.id,
                    competition_id=room.competition_id,
                    season_id=room.season_id,
                    status="running",
                )
                db.add(match)
                db.flush()
                match_ids.append(match.id)
                if index == 0:
                    room.template_snapshot = [{"key": "human", "name": "真人立论", "kind": "speech", "seat": "aff_1", "duration": 120}]
                    room.status = "running"
                    room.current_stage_index = 0
                    room.stage_started_at = now()
                    room.stage_deadline_at = now() + timedelta(seconds=120)
                elif index == 1:
                    room.template_snapshot = [
                        {
                            "key": "free",
                            "name": "自由辩论",
                            "kind": "free",
                            "side": "aff",
                            "duration": 300,
                            "turn_duration": 2,
                            "turn_started_at": (now() + timedelta(seconds=60)).isoformat(),
                        }
                    ]
                    room.status = "running"
                    room.current_stage_index = 0
                    room.stage_started_at = now()
                    room.stage_deadline_at = now() + timedelta(seconds=300)
                elif index == 2:
                    room.template_snapshot = [
                        {"key": "ai", "name": "AI 播放", "kind": "speech", "seat": "neg_1", "duration": 30},
                        {"key": "human", "name": "真人回应", "kind": "speech", "seat": "aff_1", "duration": 120},
                    ]
                    room.status = "running"
                    room.current_stage_index = 0
                    room.stage_started_at = now()
                    room.stage_deadline_at = now() + timedelta(seconds=60)
                    db.add(
                        Speech(
                            match_id=match.id,
                            room_id=room.id,
                            seat_key="neg_1",
                            stage_key="ai",
                            speaker_type="ai",
                            status="playing",
                            content="并行验收中的预置 AI 播放。",
                            duration_seconds=60,
                            playback_started_at=now(),
                            playback_ends_at=now() + timedelta(seconds=60),
                        )
                    )
                else:
                    room.template_snapshot = [
                        {"key": "paused", "name": "暂停中的真人发言", "kind": "speech", "seat": "aff_1", "duration": 120}
                    ]
                    room.status = "paused"
                    room.current_stage_index = 0
                    room.stage_started_at = now() - timedelta(seconds=43)
                    room.stage_deadline_at = None
                    room.paused_remaining_seconds = 77
                db.flush()
            db.commit()

        ws_base = base_url.replace("https://", "wss://").replace("http://", "ws://")
        ready_events = [asyncio.Event() for _ in codes]
        start_event = asyncio.Event()
        watchers = [asyncio.create_task(watch_room(ws_base, code, ready, start_event)) for code, ready in zip(codes, ready_events)]
        await asyncio.wait_for(asyncio.gather(*(ready.wait() for ready in ready_events)), timeout=20)

        with SessionLocal() as db:
            free_room = load_room(db, codes[1], lock=True)
            current = dict(free_room.template_snapshot[0])
            current["turn_started_at"] = (now() - timedelta(seconds=5)).isoformat()
            free_room.template_snapshot = [current]
            playback_room = load_room(db, codes[2], lock=True)
            playing = db.query(Speech).filter(Speech.room_id == playback_room.id, Speech.status == "playing").one()
            playing.playback_ends_at = now() - timedelta(seconds=1)
            db.commit()

        lease_headers = csrf(clients[0]) | {"X-Control-Lease": "parallel-human-device"}
        (await clients[0].post(f"/api/rooms/{codes[0]}/control-lease", headers=lease_headers, json={})).raise_for_status()
        start_event.set()
        human_start, _, _ = await asyncio.gather(
            clients[0].post(f"/api/rooms/{codes[0]}/speech/start", headers=lease_headers, json={}),
            match_engine.process_room(codes[1]),
            match_engine.process_room(codes[2]),
        )
        human_start.raise_for_status()
        event_sets = await asyncio.gather(*watchers)

        views = [(await client.get(f"/api/rooms/{code}")).json()["room"] for client, code in zip(clients, codes)]
        assert views[0]["active_speech"] and views[0]["active_speech"]["speaker_type"] == "human"
        assert views[1]["current_stage"]["side"] == "neg"
        assert views[2]["current_stage_index"] == 1 and views[2]["active_speech"] is None
        assert views[3]["status"] == "paused" and views[3]["remaining_seconds"] == 77
        assert "speech.started" in {item["type"] for item in event_sets[0]}
        assert "free.turn_timed_out" in {item["type"] for item in event_sets[1]}
        assert "speech.completed" in {item["type"] for item in event_sets[2]}
        assert event_sets[3] == []

        terminated = await clients[0].post(
            f"/api/rooms/{codes[0]}/control/terminate",
            headers=csrf(clients[0]) | {"X-Idempotency-Key": "parallel-human-terminate"},
            json={"reason": "并行验收终止真人发言"},
        )
        terminated.raise_for_status()
        assert terminated.json()["room"]["status"] == "terminated"
        result = (await clients[0].get(f"/api/rooms/{codes[0]}/result")).json()
        interrupted = next(item for item in result["speeches"] if item["speaker_type"] == "human")
        assert interrupted["status"] == "interrupted" and interrupted["created_at"]
        print(
            "parallel_match_isolation_verified "
            "human=speaking->interrupted free=aff->neg playback=completed "
            "paused=77s websocket_cross_room=0 result_status=auditable"
        )
    finally:
        await asyncio.gather(*(client.aclose() for client in clients))
        if room_ids:
            with SessionLocal() as db:
                db.execute(delete(RatingChange).where(RatingChange.match_id.in_(match_ids)))
                db.execute(delete(JudgeScorecard).where(JudgeScorecard.match_id.in_(match_ids)))
                db.execute(delete(AudioAsset).where(AudioAsset.match_id.in_(match_ids)))
                db.execute(delete(Speech).where(Speech.match_id.in_(match_ids)))
                db.execute(delete(MatchEvent).where(MatchEvent.room_id.in_(room_ids)))
                db.execute(delete(Match).where(Match.id.in_(match_ids)))
                db.execute(delete(RoomSeat).where(RoomSeat.room_id.in_(room_ids)))
                release_verification_room_codes(db, room_ids)
                db.execute(delete(Room).where(Room.id.in_(room_ids)))
                db.execute(delete(UserSession).where(UserSession.user_id.in_(user_ids)))
                db.execute(delete(User).where(User.id.in_(user_ids)))
                db.commit()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://117.50.192.216")
    args = parser.parse_args()
    asyncio.run(main_async(args.base_url.rstrip("/")))


if __name__ == "__main__":
    main()
