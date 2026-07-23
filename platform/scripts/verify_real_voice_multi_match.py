#!/usr/bin/env python3
"""Run concurrent matches with mocked Agents and the real MOSS/LiveKit path.

The verifier deliberately does not retain or inspect per-speech audio files.
Success means every room publishes one continuous LiveKit track, records the
text and timing telemetry, survives one pause/resume, and reaches a result.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import time
from datetime import datetime
from secrets import token_hex

import httpx
from app.core.config import settings
from app.core.database import SessionLocal
from app.models.entities import (
    AudioAsset,
    CaptionSegment,
    FreeTurnRequest,
    JudgeScorecard,
    LeaderboardEntry,
    Match,
    MatchEvent,
    MatchParticipant,
    RatingChange,
    Room,
    RoomSeat,
    Speech,
    SpeechCorrectionRequest,
    SpeechDataIssueDisposition,
    TranscriptSegment,
    User,
    UserSession,
    VoiceTelemetry,
)
from app.services.match_archive import archive_lock_name
from app.services.match_engine import match_engine
from app.services.providers import debate_agent, judge_provider
from app.services.room_capacity import ACTIVE_PARTICIPANT_STATUSES, MAX_ACTIVE_ROOMS
from app.services.room_service import load_room
from app.services.verification_cleanup import release_verification_room_codes
from sqlalchemy import delete, select

PASSWORD = "Real-voice-multi-1234"


def csrf(client: httpx.AsyncClient) -> dict[str, str]:
    value = client.cookies.get("jixia_csrf")
    return {"X-CSRF-Token": value} if value else {}


async def create_room(
    client: httpx.AsyncClient,
    *,
    account: str,
    real_name: str,
    competition_slug: str,
    topic: str,
    topic_id: str | None = None,
) -> tuple[str, str]:
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
    payload = {
        "competition_slug": competition_slug,
        "seat_key": "aff_1",
        "visibility": "public",
    }
    if topic_id:
        payload["topic_id"] = topic_id
    else:
        payload["custom_topic"] = topic
    created = await client.post(
        "/api/rooms",
        headers=csrf(client) | {"X-Idempotency-Key": token_hex(16)},
        json=payload,
    )
    created.raise_for_status()
    code = created.json()["room"]["code"]
    (await client.post(f"/api/rooms/{code}/ready", headers=csrf(client), json={"ready": True})).raise_for_status()
    (await client.post(f"/api/rooms/{code}/start", headers=csrf(client), json={})).raise_for_status()
    return registered.json()["user"]["id"], code


async def main_async(base_url: str, room_count: int) -> None:
    suffix = f"{int(time.time())}_{token_hex(3)}"
    clients = [httpx.AsyncClient(base_url=base_url, verify=False, timeout=30, follow_redirects=True) for _ in range(room_count)]
    user_ids: list[str] = []
    room_ids: list[str] = []
    match_ids: list[str] = []
    codes: list[str] = []
    pause_code = ""
    paused_once = False
    generated_calls = 0
    provider_retries: dict[str, int] = {}
    started_at = time.monotonic()
    next_report_at = started_at
    last_summary: dict[str, object] = {}
    original_generate = debate_agent.generate
    original_judge = judge_provider.judge
    try:
        with SessionLocal() as db:
            active_count = len(list(db.scalars(select(Room.id).where(Room.status.in_(ACTIVE_PARTICIPANT_STATUSES))).all()))
        if active_count + room_count > MAX_ACTIVE_ROOMS:
            raise RuntimeError(f"active room capacity is {active_count}/{MAX_ACTIVE_ROOMS}; cannot add {room_count} verification rooms")
        daily = (await clients[0].get("/api/competitions/daily-4v4")).json()["competition"]
        topics = [
            "人工智能是否会增强人类创造力？",
            "短视频是否提升了公众获取知识的效率？",
            "大学教育应更重视通识还是职业技能？",
            "远程办公是否会成为知识工作的主要形态？",
        ]
        specs = [
            ("real_voice_daily_a", "多场语音验收甲", "daily-4v4", daily["topics"][0]["id"]),
            ("real_voice_training_a", "多场语音验收乙", "training-1v1", None),
            ("real_voice_daily_b", "多场语音验收丙", "daily-4v4", daily["topics"][1]["id"]),
            ("real_voice_training_b", "多场语音验收丁", "training-1v1", None),
        ][:room_count]
        created = await asyncio.gather(
            *(
                create_room(
                    clients[index],
                    account=f"{account}_{suffix}",
                    real_name=real_name,
                    competition_slug=competition_slug,
                    topic=topics[index],
                    topic_id=topic_id,
                )
                for index, (account, real_name, competition_slug, topic_id) in enumerate(specs)
            )
        )
        user_ids = [item[0] for item in created]
        codes = [item[1] for item in created]
        pause_code = codes[0]

        with SessionLocal() as db:
            for index, code in enumerate(codes):
                room = load_room(db, code, lock=True)
                room_ids.append(room.id)
                match = db.scalar(select(Match).where(Match.room_id == room.id))
                assert match is not None
                match_ids.append(match.id)
                ai_seats = [seat for seat in room.seats if seat.occupant_type == "ai" and seat.agent_profile_id]
                assert ai_seats
                first = ai_seats[index % len(ai_seats)]
                second = ai_seats[(index + 1) % len(ai_seats)] if len(ai_seats) > 1 else first
                room.template_snapshot = [
                    {
                        "key": "opening_case",
                        "name": "第一轮立论",
                        "kind": "speech",
                        "seat": first.seat_key,
                        "duration": 120,
                    },
                    {
                        "key": "response_case",
                        "name": "第二轮回应",
                        "kind": "speech",
                        "seat": second.seat_key,
                        "duration": 120,
                    },
                    {"key": "judging", "name": "AI 裁判", "kind": "judging", "duration": 30},
                ]
                room.current_stage_index = -1
                room.stage_started_at = None
                room.stage_deadline_at = None
                room.status = "running"
                for seat in room.seats:
                    seat.occupant_type = "ai"
                    seat.user_id = None
                    seat.is_ready = True
                match_engine._enter_stage(db, room, 0)
            db.commit()

        async def generated(payload, *_args, **_kwargs):
            nonlocal generated_calls
            generated_calls += 1
            return f"{payload['debater_name']}就“{payload['debate_topic']}”指出：判断争议要统一标准，并比较现实效果、长期风险和人的主体性。"

        async def judged(topic, speeches, **_kwargs):
            assert speeches and all(item.get("content") for item in speeches)
            winner = "aff" if len(topic) % 2 == 0 else "neg"
            return {
                "winner": winner,
                "affirmative_score": 86.0 if winner == "aff" else 82.0,
                "negative_score": 86.0 if winner == "neg" else 82.0,
                "individual_scores": {},
                "reasoning": "多场真实语音验收使用确定性模拟裁判完成流程验证。",
            }

        debate_agent.generate = generated
        judge_provider.judge = judged
        deadline = time.monotonic() + 240
        terminal_statuses = {"completed", "review_required", "terminated"}
        while time.monotonic() < deadline:
            await match_engine.tick()
            await asyncio.sleep(0.15)
            pause_now = False
            failed_rooms: list[tuple[int, str]] = []
            with SessionLocal() as db:
                rooms = [load_room(db, code) for code in codes]
                pause_room = next(item for item in rooms if item.code == pause_code)
                if not paused_once:
                    synthesizing = db.scalar(
                        select(Speech.id).where(
                            Speech.room_id == pause_room.id,
                            Speech.status == "synthesizing",
                        )
                    )
                    pause_now = synthesizing is not None
                statuses = [room.status for room in rooms]
                failed_rooms = [(index, room.code) for index, room in enumerate(rooms) if room.status == "paused" and room.failure_reason]
                speech_statuses = [
                    (item.room_id, item.stage_key, item.status)
                    for item in db.scalars(select(Speech).where(Speech.room_id.in_(room_ids)).order_by(Speech.created_at)).all()
                ]
                last_summary = {
                    "rooms": [(room.code, room.status, room.current_stage_index, room.failure_reason) for room in rooms],
                    "speeches": speech_statuses,
                    "tasks": sorted(match_engine._room_tasks),
                }
            if time.monotonic() >= next_report_at:
                print(f"real_voice_multi_match_progress {last_summary}", flush=True)
                next_report_at = time.monotonic() + 10
            if pause_now:
                paused = await clients[0].post(
                    f"/api/rooms/{pause_code}/control/pause",
                    headers=csrf(clients[0]) | {"X-Idempotency-Key": "real-voice-pause"},
                    json={"reason": "验证真实 TTS 取消与恢复"},
                )
                paused.raise_for_status()
                resumed = await clients[0].post(
                    f"/api/rooms/{pause_code}/control/resume",
                    headers=csrf(clients[0]) | {"X-Idempotency-Key": "real-voice-resume"},
                    json={"reason": "恢复多场并发验收"},
                )
                resumed.raise_for_status()
                paused_once = True
            for client_index, failed_code in failed_rooms:
                attempts = provider_retries.get(failed_code, 0)
                if attempts >= 2:
                    raise RuntimeError(f"room {failed_code} exceeded provider recovery limit: {last_summary}")
                retried = await clients[client_index].post(
                    f"/api/rooms/{failed_code}/control/retry",
                    headers=csrf(clients[client_index]) | {"X-Idempotency-Key": f"real-voice-provider-retry-{attempts + 1}"},
                    json={"reason": "真实 MOSS 实时语音瞬时故障恢复验收"},
                )
                retried.raise_for_status()
                provider_retries[failed_code] = attempts + 1
            if all(status in terminal_statuses for status in statuses):
                break
        else:
            raise TimeoutError(f"four real-voice matches did not finish within 240 seconds: {last_summary}")

        if match_engine._room_tasks:
            await asyncio.gather(*list(match_engine._room_tasks.values()), return_exceptions=True)
        with SessionLocal() as db:
            rooms = [load_room(db, code) for code in codes]
            assert all(room.status == "completed" for room in rooms)
            speeches = list(db.scalars(select(Speech).where(Speech.room_id.in_(room_ids))).all())
            completed = [item for item in speeches if item.status == "completed"]
            interrupted = [item for item in speeches if item.status == "interrupted"]
            assert len(completed) == room_count * 2
            assert paused_once and interrupted
            for room in rooms:
                room_speeches = [item for item in completed if item.room_id == room.id]
                assert len(room_speeches) == 2
                assert all(item.audio_url == "" for item in room_speeches)
                assert all(item.duration_seconds > 1 for item in room_speeches)
                assert all(item.content and room.topic in item.content for item in room_speeches)
                other_topics = {item.topic for item in rooms if item.id != room.id}
                assert all(not any(topic in item.content for topic in other_topics) for item in room_speeches)
                assert db.scalar(select(JudgeScorecard).join(Match).where(Match.room_id == room.id)) is not None
                rtc_events = list(
                    db.scalars(
                        select(MatchEvent).where(
                            MatchEvent.room_id == room.id,
                            MatchEvent.event_type == "audio.rtc.started",
                        )
                    ).all()
                )
                completed_ids = {item.id for item in room_speeches}
                completed_rtc = [event for event in rtc_events if event.payload.get("speech_id") in completed_ids]
                assert len(completed_rtc) == 2
                track_sids = {str(event.payload.get("track_sid") or "") for event in completed_rtc}
                assert len(track_sids) == 1 and "" not in track_sids
                assert all(event.payload.get("transport") == "livekit" for event in completed_rtc)
                telemetry = list(db.scalars(select(VoiceTelemetry).where(VoiceTelemetry.speech_id.in_(completed_ids))).all())
                assert len(telemetry) == 2
                for item in telemetry:
                    phases = item.phases or {}
                    assert {"agent_first_readable_delta", "tts_first_pcm", "livekit_first_capture", "playout_completed"} <= set(phases)
                    first_text = datetime.fromisoformat(phases["agent_first_readable_delta"]["at"])
                    first_pcm = datetime.fromisoformat(phases["tts_first_pcm"]["at"])
                    assert 0 <= (first_pcm - first_text).total_seconds() < 3
                assert not list((settings.media_path / room.code).glob("*.wav"))
            event_room_ids = set(
                db.scalars(
                    select(MatchEvent.room_id).where(
                        MatchEvent.room_id.in_(room_ids),
                        MatchEvent.event_type == "speech.audio.ready",
                    )
                ).all()
            )
            assert event_room_ids == set(room_ids)
        elapsed = time.monotonic() - started_at
        print(
            "real_voice_multi_match_verified "
            f"rooms={room_count} speeches={room_count * 2} agent_calls={generated_calls} "
            f"pause_resume=1 provider_retries={sum(provider_retries.values())} "
            f"real_moss_livekit=1 single_track_per_room=1 audio_files=0 "
            f"cross_room_audio_events=0 elapsed={elapsed:.2f}s"
        )
    finally:
        debate_agent.generate = original_generate
        judge_provider.judge = original_judge
        await asyncio.gather(*(client.aclose() for client in clients), return_exceptions=True)
        if room_ids:
            with SessionLocal() as db:
                speech_ids = list(db.scalars(select(Speech.id).where(Speech.room_id.in_(room_ids))).all())
                if speech_ids:
                    db.execute(delete(TranscriptSegment).where(TranscriptSegment.speech_id.in_(speech_ids)))
                    db.execute(delete(CaptionSegment).where(CaptionSegment.speech_id.in_(speech_ids)))
                    db.execute(delete(VoiceTelemetry).where(VoiceTelemetry.speech_id.in_(speech_ids)))
                    db.execute(delete(SpeechCorrectionRequest).where(SpeechCorrectionRequest.speech_id.in_(speech_ids)))
                    db.execute(delete(SpeechDataIssueDisposition).where(SpeechDataIssueDisposition.speech_id.in_(speech_ids)))
                db.execute(delete(FreeTurnRequest).where(FreeTurnRequest.room_id.in_(room_ids)))
                db.execute(delete(RatingChange).where(RatingChange.match_id.in_(match_ids)))
                db.execute(delete(JudgeScorecard).where(JudgeScorecard.match_id.in_(match_ids)))
                db.execute(delete(AudioAsset).where(AudioAsset.match_id.in_(match_ids)))
                db.execute(delete(MatchParticipant).where(MatchParticipant.match_id.in_(match_ids)))
                db.execute(delete(Speech).where(Speech.room_id.in_(room_ids)))
                db.execute(delete(MatchEvent).where(MatchEvent.room_id.in_(room_ids)))
                db.execute(delete(Match).where(Match.id.in_(match_ids)))
                db.execute(delete(RoomSeat).where(RoomSeat.room_id.in_(room_ids)))
                release_verification_room_codes(db, room_ids)
                db.execute(delete(Room).where(Room.id.in_(room_ids)))
                db.execute(delete(LeaderboardEntry).where(LeaderboardEntry.user_id.in_(user_ids)))
                db.execute(delete(UserSession).where(UserSession.user_id.in_(user_ids)))
                db.execute(delete(User).where(User.id.in_(user_ids)))
                db.commit()
        for code in codes:
            shutil.rmtree(settings.media_path / code, ignore_errors=True)
        for match_id in match_ids:
            for suffix in (".json", ".meta.json", ".json.sha256"):
                (settings.archive_path / f"{match_id}{suffix}").unlink(missing_ok=True)
            (settings.archive_path / ".locks" / archive_lock_name(match_id)).unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://117.50.192.216")
    parser.add_argument("--room-count", type=int, choices=range(1, 5), default=2)
    args = parser.parse_args()
    asyncio.run(main_async(args.base_url.rstrip("/"), args.room_count))


if __name__ == "__main__":
    main()
