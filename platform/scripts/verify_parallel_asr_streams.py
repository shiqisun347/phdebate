#!/usr/bin/env python3
"""Verify four isolated ASR streams, duplicate rejection, and stale-final cancellation."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import ssl
import time
from datetime import timedelta
from secrets import token_hex

import httpx
import websockets
from app.core.config import settings
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
    TranscriptSegment,
    User,
    UserSession,
)
from app.services.room_service import load_room, now
from app.services.verification_cleanup import release_verification_room_codes
from sqlalchemy import delete, select

PASSWORD = "Parallel-asr-1234"


def csrf(client: httpx.AsyncClient) -> dict[str, str]:
    value = client.cookies.get("jixia_csrf")
    return {"X-CSRF-Token": value} if value else {}


async def mock_asr(socket) -> None:
    marker = 0
    async for payload in socket:
        if isinstance(payload, bytes) and len(payload) >= 2:
            amplitude = int.from_bytes(payload[:2], "little", signed=True)
            marker = max(0, amplitude // 2000 - 1)
        elif payload == "STOP":
            await socket.send(
                json.dumps(
                    {"is_final": True, "text": f"第 {marker + 1} 个房间的最终字幕。"},
                    ensure_ascii=False,
                )
            )
            return


async def prepare_room(client: httpx.AsyncClient, index: int, suffix: str, asr_endpoint: str) -> tuple[str, str, str, str, str]:
    registered = await client.post(
        "/api/auth/register",
        json={
            "account": f"parallel_asr_{index}_{suffix}",
            "real_name": f"并行 ASR 验收选手{index + 1}",
            "password": PASSWORD,
            "confirm_password": PASSWORD,
        },
    )
    registered.raise_for_status()
    user_id = registered.json()["user"]["id"]
    created = await client.post(
        "/api/rooms",
        headers=csrf(client) | {"X-Idempotency-Key": token_hex(16)},
        json={
            "competition_slug": "training-1v1",
            "custom_topic": f"第 {index + 1} 个房间的语音流是否与其他房间隔离？",
            "seat_key": "aff_1",
            "visibility": "private",
        },
    )
    created.raise_for_status()
    code = created.json()["room"]["code"]
    (await client.post(f"/api/rooms/{code}/ready", headers=csrf(client), json={"ready": True})).raise_for_status()
    (await client.post(f"/api/rooms/{code}/start", headers=csrf(client), json={})).raise_for_status()
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.status = "running"
        room.current_stage_index = 1
        room.stage_started_at = now()
        room.stage_deadline_at = now() + timedelta(seconds=180)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        snapshot = dict(match.service_snapshot)
        snapshot["funasr"] = {
            "kind": "funasr",
            "endpoint": asr_endpoint,
            "settings": {"final_wait_seconds": 5},
            "enabled": True,
            "source": "verification",
        }
        match.service_snapshot = snapshot
        db.commit()
        room_id = room.id
        match_id = match.id
    lease = f"parallel-asr-device-{index}"
    headers = csrf(client) | {"X-Control-Lease": lease}
    (await client.post(f"/api/rooms/{code}/control-lease", headers=headers, json={})).raise_for_status()
    started = await client.post(f"/api/rooms/{code}/speech/start", headers=headers, json={})
    started.raise_for_status()
    return user_id, room_id, match_id, started.json()["speech_id"], code


async def open_asr(base_url: str, client: httpx.AsyncClient, code: str, lease: str):
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    cookie_header = "; ".join(f"{key}={value}" for key, value in client.cookies.items())
    ws_url = base_url.replace("https://", "wss://").replace("http://", "ws://") + f"/ws/rooms/{code}/asr"
    return websockets.connect(ws_url, ssl=ssl_context, additional_headers={"Cookie": cookie_header}, open_timeout=15)


async def run_stream(
    index: int,
    *,
    base_url: str,
    client: httpx.AsyncClient,
    code: str,
    speech_id: str,
    stale_final: bool,
) -> str | None:
    lease = f"parallel-asr-device-{index}"
    with SessionLocal() as db:
        speech = db.get(Speech, speech_id)
        assert speech is not None
        stage_key = speech.stage_key
    authentication = {
        "type": "authenticate",
        "lease": lease,
        "protocol_version": 2,
        "encoding": "pcm_s16le",
        "channels": 1,
        "sample_rate": 16_000,
        "speech_id": speech_id,
        "stage_key": stage_key,
    }
    connection = await open_asr(base_url, client, code, lease)
    async with connection as socket:
        await socket.send(json.dumps(authentication))
        ready = json.loads(await asyncio.wait_for(socket.recv(), timeout=5))
        assert ready == {
            "type": "ready",
            "speech_id": speech_id,
            "stage_key": stage_key,
            "protocol_version": 2,
        }
        if index == 0:
            duplicate_connection = await open_asr(base_url, client, code, lease)
            try:
                async with duplicate_connection as duplicate:
                    await duplicate.send(json.dumps(authentication))
                    await duplicate.recv()
            except websockets.exceptions.ConnectionClosed as exc:
                assert exc.code == 4409
            else:
                raise AssertionError("duplicate ASR stream unexpectedly remained open")
        if stale_final:
            with SessionLocal() as db:
                room = load_room(db, code, lock=True)
                seat = next(item for item in room.seats if item.seat_key == "aff_1")
                seat.control_lease = "replacement-device"
                db.commit()
        amplitude = (index + 1) * 2000
        await socket.send(amplitude.to_bytes(2, "little", signed=True) * 4096)
        await socket.send(json.dumps({"type": "finish"}))
        try:
            while True:
                message = json.loads(await asyncio.wait_for(socket.recv(), timeout=10))
                if message.get("type") == "asr" and message.get("is_final"):
                    if stale_final:
                        raise AssertionError("stale ASR stream emitted a final result")
                    return str(message["text"])
        except websockets.exceptions.ConnectionClosed as exc:
            if stale_final and exc.code == 4409:
                return None
            raise


async def main_async(base_url: str) -> None:
    clients = [httpx.AsyncClient(base_url=base_url, verify=False, timeout=30, follow_redirects=True) for _ in range(4)]
    user_ids: list[str] = []
    room_ids: list[str] = []
    match_ids: list[str] = []
    speech_ids: list[str] = []
    codes: list[str] = []
    server = await websockets.serve(mock_asr, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        suffix = f"{int(time.time())}_{token_hex(3)}"
        prepared = await asyncio.gather(
            *(prepare_room(client, index, suffix, f"ws://127.0.0.1:{port}") for index, client in enumerate(clients))
        )
        user_ids = [item[0] for item in prepared]
        room_ids = [item[1] for item in prepared]
        match_ids = [item[2] for item in prepared]
        speech_ids = [item[3] for item in prepared]
        codes = [item[4] for item in prepared]
        results = await asyncio.gather(
            *(
                run_stream(
                    index,
                    base_url=base_url,
                    client=clients[index],
                    code=codes[index],
                    speech_id=speech_ids[index],
                    stale_final=index == 3,
                )
                for index in range(4)
            )
        )
        assert results[:3] == [f"第 {index + 1} 个房间的最终字幕。" for index in range(3)]
        assert results[3] is None
        with SessionLocal() as db:
            speeches = {item.id: item for item in db.scalars(select(Speech).where(Speech.id.in_(speech_ids))).all()}
            segments = list(db.scalars(select(TranscriptSegment).where(TranscriptSegment.speech_id.in_(speech_ids))).all())
            assert [speeches[speech_ids[index]].content for index in range(3)] == results[:3]
            assert speeches[speech_ids[3]].content == ""
            assert len(segments) == 3 and {item.speech_id for item in segments} == set(speech_ids[:3])
        print(
            "parallel_asr_streams_verified rooms=4 final_persisted=3 duplicate_rejected=1 stale_final_rejected=1 cross_room_transcripts=0"
        )
    finally:
        server.close()
        await server.wait_closed()
        await asyncio.gather(*(client.aclose() for client in clients), return_exceptions=True)
        if room_ids:
            with SessionLocal() as db:
                db.execute(delete(TranscriptSegment).where(TranscriptSegment.speech_id.in_(speech_ids)))
                db.execute(delete(Speech).where(Speech.room_id.in_(room_ids)))
                db.execute(delete(MatchEvent).where(MatchEvent.room_id.in_(room_ids)))
                db.execute(delete(AudioAsset).where(AudioAsset.match_id.in_(match_ids)))
                db.execute(delete(JudgeScorecard).where(JudgeScorecard.match_id.in_(match_ids)))
                db.execute(delete(RatingChange).where(RatingChange.match_id.in_(match_ids)))
                db.execute(delete(Match).where(Match.id.in_(match_ids)))
                db.execute(delete(RoomSeat).where(RoomSeat.room_id.in_(room_ids)))
                release_verification_room_codes(db, room_ids)
                db.execute(delete(Room).where(Room.id.in_(room_ids)))
                db.execute(delete(UserSession).where(UserSession.user_id.in_(user_ids)))
                db.execute(delete(User).where(User.id.in_(user_ids)))
                db.commit()
        for code in codes:
            shutil.rmtree(settings.media_path / code, ignore_errors=True)


def main() -> None:
    if os.geteuid() == 0:
        raise SystemExit("请使用 平台 服务账号运行并行 ASR 验收。")
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://117.50.192.216")
    args = parser.parse_args()
    asyncio.run(main_async(args.base_url.rstrip("/")))


if __name__ == "__main__":
    main()
