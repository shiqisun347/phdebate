#!/usr/bin/env python3
"""Verify the authenticated browser ASR bridge against the deployed local FunASR protocol."""

from __future__ import annotations

import argparse
import asyncio
import audioop
import json
import shutil
import ssl
import time
import wave
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
from app.services.match_archive import archive_lock_name
from app.services.room_service import load_room, now
from app.services.verification_cleanup import release_verification_room_codes
from sqlalchemy import delete, select

PASSWORD = "ASR-bridge-1234"


def csrf(client: httpx.AsyncClient) -> dict[str, str]:
    value = client.cookies.get("jixia_csrf")
    return {"X-CSRF-Token": value} if value else {}


def pcm16k(path) -> bytes:
    with wave.open(str(path), "rb") as audio:
        frames = audio.readframes(audio.getnframes())
        if audio.getnchannels() != 1 or audio.getsampwidth() != 2:
            raise RuntimeError("generated MOSS WAV must be mono 16-bit PCM")
        if audio.getframerate() != 16000:
            frames, _state = audioop.ratecv(frames, 2, 1, audio.getframerate(), 16000, None)
        return frames


async def main_async(base_url: str) -> None:
    suffix = f"{int(time.time())}_{token_hex(3)}"
    client = httpx.AsyncClient(base_url=base_url, verify=False, timeout=20, follow_redirects=True)
    user_id = room_id = match_id = speech_id = code = ""
    voice_room = "asr-bridge-voice"
    try:
        registered = await client.post(
            "/api/auth/register",
            json={
                "account": f"asr_bridge_{suffix}",
                "real_name": "ASR 桥接验收",
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
                "custom_topic": "语音识别桥接协议是否正确？",
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
            room_id = room.id
            room.status = "running"
            room.current_stage_index = 1
            room.stage_started_at = now()
            room.stage_deadline_at = now() + timedelta(seconds=180)
            match = db.scalar(select(Match).where(Match.room_id == room.id))
            assert match is not None
            match_id = match.id
            assert match.service_snapshot["funasr"]["endpoint"]
            db.commit()

        lease = "asr-bridge-device"
        headers = csrf(client) | {"X-Control-Lease": lease}
        (await client.post(f"/api/rooms/{code}/control-lease", headers=headers, json={})).raise_for_status()
        started = await client.post(f"/api/rooms/{code}/speech/start", headers=headers, json={})
        started.raise_for_status()
        speech_id = started.json()["speech_id"]
        with SessionLocal() as db:
            speech = db.get(Speech, speech_id)
            assert speech is not None
            stage_key = speech.stage_key

        # Reuse a fixed, human-recorded MOSS prompt as the speech fixture.  ASR
        # verification must remain independent of TTS health; otherwise a TTS
        # warm-up or queue failure can hide whether the browser bridge itself
        # is genuinely duplex.
        fixture = settings.media_path.parents[1] / "assets" / "moss-prompts" / "audio" / "candidate_voice_1.wav"
        if not fixture.is_file():
            raise RuntimeError(f"ASR speech fixture is missing: {fixture}")
        frames = pcm16k(fixture)
        cookie_header = "; ".join(f"{key}={value}" for key, value in client.cookies.items())
        ws_url = base_url.replace("https://", "wss://").replace("http://", "ws://") + f"/ws/rooms/{code}/asr"
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        async with websockets.connect(ws_url, ssl=ssl_context, additional_headers={"Cookie": cookie_header}) as socket:
            await socket.send(
                json.dumps(
                    {
                        "type": "authenticate",
                        "lease": lease,
                        "protocol_version": 2,
                        "encoding": "pcm_s16le",
                        "channels": 1,
                        "sample_rate": 16_000,
                        "speech_id": speech_id,
                        "stage_key": stage_key,
                    }
                )
            )
            ready = json.loads(await asyncio.wait_for(socket.recv(), timeout=5))
            assert ready == {
                "type": "ready",
                "speech_id": speech_id,
                "stage_key": stage_key,
                "protocol_version": 2,
            }
            partial_received = asyncio.Event()
            final_received: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
            finish_sent_at: float | None = None
            first_partial_at: float | None = None
            input_stream_open = True
            partial_while_streaming = False

            async def receive_results() -> None:
                nonlocal first_partial_at, partial_while_streaming
                async for raw in socket:
                    message = json.loads(raw)
                    if message.get("type") != "asr":
                        continue
                    if message.get("is_final"):
                        if not final_received.done():
                            final_received.set_result(message)
                        return
                    if str(message.get("text") or "").strip():
                        first_partial_at = first_partial_at or time.monotonic()
                        partial_while_streaming = partial_while_streaming or input_stream_open
                        partial_received.set()

            receiver = asyncio.create_task(receive_results(), name=f"verify-asr-results-{code}")
            stream_started_at = time.monotonic()
            try:
                # Send 100 ms PCM blocks at their real playout cadence. The
                # upstream reader runs concurrently, so a partial result must
                # reach the browser before STOP proves genuine audio-in +
                # text-out duplex streaming rather than upload-then-transcribe.
                for offset in range(0, len(frames), 3200):
                    await socket.send(frames[offset : offset + 3200])
                    await asyncio.sleep(0.1)
                # Keep the input side open briefly after the spoken sample. A
                # realtime provider may need the final acoustic boundary before
                # emitting its first useful hypothesis, but that hypothesis must
                # still arrive before the browser sends STOP. This distinguishes
                # a duplex bridge from an upload-then-transcribe implementation.
                for _ in range(20):
                    if partial_received.is_set():
                        break
                    await socket.send(bytes(3200))
                    await asyncio.sleep(0.1)
                assert partial_received.is_set(), "ASR did not return a partial result before the input stream closed"
                assert partial_while_streaming, "ASR output was not delivered concurrently with the open PCM stream"
                input_stream_open = False
                finish_sent_at = time.monotonic()
                await socket.send(json.dumps({"type": "finish"}))
                final = await asyncio.wait_for(final_received, timeout=10)
            finally:
                receiver.cancel()
                await asyncio.gather(receiver, return_exceptions=True)
            assert final and len(str(final.get("text") or "")) >= 4
            assert first_partial_at is not None and finish_sent_at is not None
            partial_latency_ms = round((first_partial_at - stream_started_at) * 1000, 1)
            final_latency_ms = round((time.monotonic() - finish_sent_at) * 1000, 1)

        with SessionLocal() as db:
            speech = db.get(Speech, speech_id)
            assert speech and len(speech.content) >= 4
            assert db.scalar(select(TranscriptSegment).where(TranscriptSegment.speech_id == speech_id)) is not None
        terminated = await client.post(f"/api/rooms/{code}/control/terminate", headers=csrf(client), json={"reason": "ASR 验收完成"})
        terminated.raise_for_status()
        archive_path = settings.archive_path / f"{match_id}.json"
        archive_deadline = time.monotonic() + 5
        while not archive_path.exists() and time.monotonic() < archive_deadline:
            await asyncio.sleep(0.1)
        print(
            f"asr_bridge_verified room={code} duplex=true "
            f"partial_before_finish_ms={partial_latency_ms} final_after_finish_ms={final_latency_ms} "
            f"final={final['text']}"
        )
    finally:
        await client.aclose()
        if room_id:
            with SessionLocal() as db:
                db.execute(
                    delete(TranscriptSegment).where(TranscriptSegment.speech_id.in_(select(Speech.id).where(Speech.room_id == room_id)))
                )
                db.execute(delete(Speech).where(Speech.room_id == room_id))
                db.execute(delete(MatchEvent).where(MatchEvent.room_id == room_id))
                if match_id:
                    db.execute(delete(AudioAsset).where(AudioAsset.match_id == match_id))
                    db.execute(delete(JudgeScorecard).where(JudgeScorecard.match_id == match_id))
                    db.execute(delete(RatingChange).where(RatingChange.match_id == match_id))
                db.execute(delete(Match).where(Match.room_id == room_id))
                db.execute(delete(RoomSeat).where(RoomSeat.room_id == room_id))
                release_verification_room_codes(db, [room_id])
                db.execute(delete(Room).where(Room.id == room_id))
                db.execute(delete(UserSession).where(UserSession.user_id == user_id))
                db.execute(delete(User).where(User.id == user_id))
                db.commit()
        if code:
            shutil.rmtree(settings.media_path / code, ignore_errors=True)
        if match_id:
            for suffix in (".json", ".meta.json", ".json.sha256"):
                (settings.archive_path / f"{match_id}{suffix}").unlink(missing_ok=True)
            (settings.archive_path / ".locks" / archive_lock_name(match_id)).unlink(missing_ok=True)
        shutil.rmtree(settings.media_path / voice_room, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://117.50.192.216")
    args = parser.parse_args()
    asyncio.run(main_async(args.base_url.rstrip("/")))


if __name__ == "__main__":
    main()
