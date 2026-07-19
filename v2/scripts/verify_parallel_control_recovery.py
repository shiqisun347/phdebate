#!/usr/bin/env python3
"""Verify concurrent pause/resume and failed-step retry across isolated rooms without provider calls."""

from __future__ import annotations

import argparse
import asyncio
import time
from datetime import timedelta
from secrets import token_hex

import httpx
from app.core.database import SessionLocal
from app.models.entities import JudgeScorecard, Match, MatchEvent, Room, RoomSeat, Speech, User, UserSession
from app.services.match_engine import match_engine
from app.services.room_service import load_room, now
from app.services.verification_cleanup import release_verification_room_codes
from sqlalchemy import delete, select

PASSWORD = "Parallel-control-1234"


def csrf(client: httpx.AsyncClient) -> dict[str, str]:
    value = client.cookies.get("jixia_v2_csrf")
    return {"X-CSRF-Token": value} if value else {}


async def create_room(client: httpx.AsyncClient, *, account: str, real_name: str, topic: str) -> tuple[str, str]:
    registered = await client.post(
        "/api/auth/register",
        json={
            "account": account,
            "real_name": real_name,
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
            "custom_topic": topic,
            "seat_key": "aff_1",
            "visibility": "private",
        },
    )
    created.raise_for_status()
    return registered.json()["user"]["id"], created.json()["room"]["code"]


async def main_async(base_url: str) -> None:
    suffix = f"{int(time.time())}_{token_hex(3)}"
    pause_client = httpx.AsyncClient(base_url=base_url, verify=False, timeout=20, follow_redirects=True)
    retry_client = httpx.AsyncClient(base_url=base_url, verify=False, timeout=20, follow_redirects=True)
    judge_client = httpx.AsyncClient(base_url=base_url, verify=False, timeout=20, follow_redirects=True)
    zero_client = httpx.AsyncClient(base_url=base_url, verify=False, timeout=20, follow_redirects=True)
    user_ids: list[str] = []
    room_ids: list[str] = []
    match_ids: list[str] = []
    try:
        created = await asyncio.gather(
            create_room(
                pause_client,
                account=f"pause_control_{suffix}",
                real_name="并行暂停恢复验收",
                topic="暂停恢复是否会复活旧的 AI 异步任务？",
            ),
            create_room(
                retry_client,
                account=f"retry_control_{suffix}",
                real_name="并行失败重试验收",
                topic="失败重试是否应保留原始失败记录？",
            ),
            create_room(
                judge_client,
                account=f"judge_control_{suffix}",
                real_name="并行裁判暂停验收",
                topic="暂停裁判后是否应拒绝迟到的旧判决？",
            ),
            create_room(
                zero_client,
                account=f"zero_control_{suffix}",
                real_name="零秒计时边界验收",
                topic="零秒暂停恢复是否应保持严格计时？",
            ),
        )
        user_ids = [item[0] for item in created]
        pause_code, retry_code, judge_code, zero_code = (item[1] for item in created)
        with SessionLocal() as db:
            for code, mode in (
                (pause_code, "pause"),
                (retry_code, "retry"),
                (judge_code, "judge"),
                (zero_code, "zero"),
            ):
                room = load_room(db, code, lock=True)
                room_ids.append(room.id)
                room.template_snapshot = (
                    [
                        {"key": "expired", "name": "已到时环节", "kind": "announcement", "duration": 10},
                        {"key": "next", "name": "下一环节", "kind": "announcement", "duration": 3600},
                    ]
                    if mode == "zero"
                    else [
                        {
                            "key": "provider_hold",
                            "name": "异步任务保护阶段",
                            "kind": "announcement",
                            "duration": 3600,
                        }
                    ]
                )
                room.current_stage_index = 0
                room.stage_started_at = now()
                room.stage_deadline_at = now() - timedelta(milliseconds=1) if mode == "zero" else now() + timedelta(seconds=3600)
                room.started_at = now()
                room.status = "paused" if mode == "retry" else "running"
                room.paused_remaining_seconds = 120 if mode == "retry" else None
                room.failure_reason = "模拟 LightTTS 故障" if mode == "retry" else ""
                match = Match(
                    room_id=room.id,
                    competition_id=room.competition_id,
                    season_id=room.season_id,
                    status="running",
                )
                db.add(match)
                db.flush()
                match_ids.append(match.id)
                if mode == "judge":
                    db.add(JudgeScorecard(match_id=match.id, status="running"))
                elif mode in {"pause", "retry"}:
                    db.add(
                        Speech(
                            match_id=match.id,
                            room_id=room.id,
                            seat_key="neg_1",
                            stage_key="provider_hold",
                            speaker_type="ai",
                            status="speaking" if mode == "pause" else "failed",
                            content="" if mode == "pause" else "失败前已生成的文本应保留。",
                        )
                    )
            db.commit()

        paused, retried, judge_paused, zero_paused = await asyncio.gather(
            pause_client.post(
                f"/api/rooms/{pause_code}/control/pause",
                headers=csrf(pause_client) | {"X-Idempotency-Key": "parallel-pause"},
                json={"reason": "并行暂停"},
            ),
            retry_client.post(
                f"/api/rooms/{retry_code}/control/retry",
                headers=csrf(retry_client) | {"X-Idempotency-Key": "parallel-retry"},
                json={"reason": "并行重试"},
            ),
            judge_client.post(
                f"/api/rooms/{judge_code}/control/pause",
                headers=csrf(judge_client) | {"X-Idempotency-Key": "parallel-judge-pause"},
                json={"reason": "并行暂停裁判"},
            ),
            zero_client.post(
                f"/api/rooms/{zero_code}/control/pause",
                headers=csrf(zero_client) | {"X-Idempotency-Key": "parallel-zero-pause"},
                json={"reason": "零秒暂停"},
            ),
        )
        paused.raise_for_status()
        retried.raise_for_status()
        judge_paused.raise_for_status()
        zero_paused.raise_for_status()
        assert zero_paused.json()["room"]["remaining_seconds"] == 0
        pause_replay = await pause_client.post(
            f"/api/rooms/{pause_code}/control/pause",
            headers=csrf(pause_client) | {"X-Idempotency-Key": "parallel-pause"},
            json={"reason": "并行暂停"},
        )
        pause_conflict = await pause_client.post(
            f"/api/rooms/{pause_code}/control/pause",
            headers=csrf(pause_client) | {"X-Idempotency-Key": "parallel-pause"},
            json={"reason": "不同的暂停参数"},
        )
        retry_conflict = await retry_client.post(
            f"/api/rooms/{retry_code}/control/retry",
            headers=csrf(retry_client) | {"X-Idempotency-Key": "parallel-retry"},
            json={"reason": "不同的重试参数"},
        )
        assert pause_replay.status_code == 200 and pause_replay.json()["replayed"] is True
        assert pause_conflict.status_code == retry_conflict.status_code == 409
        resumed, judge_resumed, zero_resumed = await asyncio.gather(
            pause_client.post(
                f"/api/rooms/{pause_code}/control/resume",
                headers=csrf(pause_client) | {"X-Idempotency-Key": "parallel-resume"},
                json={"reason": "并行恢复"},
            ),
            judge_client.post(
                f"/api/rooms/{judge_code}/control/resume",
                headers=csrf(judge_client) | {"X-Idempotency-Key": "parallel-judge-resume"},
                json={"reason": "恢复后重新评议"},
            ),
            zero_client.post(
                f"/api/rooms/{zero_code}/control/resume",
                headers=csrf(zero_client) | {"X-Idempotency-Key": "parallel-zero-resume"},
                json={"reason": "零秒恢复"},
            ),
        )
        resumed.raise_for_status()
        judge_resumed.raise_for_status()
        zero_resumed.raise_for_status()
        assert zero_resumed.json()["room"]["remaining_seconds"] == 0
        await match_engine.process_room(zero_code)

        with SessionLocal() as db:
            pause_room = load_room(db, pause_code)
            retry_room = load_room(db, retry_code)
            judge_room = load_room(db, judge_code)
            zero_room = load_room(db, zero_code)
            pause_speech = db.scalar(select(Speech).where(Speech.room_id == pause_room.id))
            retry_speech = db.scalar(select(Speech).where(Speech.room_id == retry_room.id))
            pause_events = list(db.scalars(select(MatchEvent).where(MatchEvent.room_id == pause_room.id)).all())
            retry_events = list(db.scalars(select(MatchEvent).where(MatchEvent.room_id == retry_room.id)).all())
            judge_match = db.scalar(select(Match).where(Match.room_id == judge_room.id))
            judge_scorecard = db.scalar(select(JudgeScorecard).where(JudgeScorecard.match_id == judge_match.id))
            judge_events = list(db.scalars(select(MatchEvent).where(MatchEvent.room_id == judge_room.id)).all())
            assert pause_room.status == retry_room.status == judge_room.status == "running"
            assert zero_room.status == "running" and zero_room.current_stage_index == 1
            assert pause_speech and pause_speech.status == "interrupted"
            assert retry_speech and retry_speech.status == "failed_retried"
            assert any(item.event_type == "speech.interrupted" and item.payload.get("reason") == "manual_pause" for item in pause_events)
            assert not any(item.event_type == "speech.interrupted" for item in retry_events)
            assert {item.event_type for item in pause_events}.issuperset({"control.pause", "control.resume"})
            assert "control.retry" in {item.event_type for item in retry_events}
            assert judge_scorecard.status == "interrupted"
            assert {item.event_type for item in judge_events}.issuperset({"judge.interrupted", "control.pause", "control.resume"})
            assert not any(item.event_type == "judge.interrupted" for item in pause_events + retry_events)
        print(
            "parallel_control_recovery_verified rooms=4 pause_invalidated_inflight=1 "
            "judge_attempt_invalidated=1 resume=3 failed_attempt_preserved=1 "
            "idempotency_conflicts=2 zero_second_preserved=1 cross_room_events=0"
        )
    finally:
        await asyncio.gather(pause_client.aclose(), retry_client.aclose(), judge_client.aclose(), zero_client.aclose())
        if room_ids:
            with SessionLocal() as db:
                db.execute(delete(Speech).where(Speech.room_id.in_(room_ids)))
                db.execute(delete(MatchEvent).where(MatchEvent.room_id.in_(room_ids)))
                db.execute(delete(JudgeScorecard).where(JudgeScorecard.match_id.in_(match_ids)))
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
    asyncio.run(main_async(args.base_url))


if __name__ == "__main__":
    main()
