from __future__ import annotations

import asyncio
import random
import time
import wave
from datetime import timedelta

import pytest
from app.core.config import settings
from app.core.database import SessionLocal
from app.models.entities import JudgeProfile, JudgeScorecard, Match, MatchEvent, ProviderConfig, Room, RoomSeat, Speech, User
from app.services.match_engine import MatchEngine, match_engine
from app.services.providers import ProviderCancelled, ProviderError, debate_agent, judge_provider, lighttts
from app.services.room_service import free_turn_remaining_seconds, load_room, now, remaining_seconds, speaking_permission
from conftest import csrf
from fastapi import HTTPException
from hypothesis import given
from hypothesis import settings as hypothesis_settings
from hypothesis import strategies as st
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from test_platform import create_training_room


def configure_running_ai_stage(code: str, *, duration: int = 90) -> None:
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        started_at = now()
        room.template_snapshot = [
            {"key": "ai_case", "name": "AI 发言", "kind": "speech", "seat": "neg_1", "duration": duration},
            {"key": "human_case", "name": "真人发言", "kind": "speech", "seat": "aff_1", "duration": 30},
        ]
        room.current_stage_index = 0
        room.status = "running"
        room.stage_started_at = started_at
        room.stage_deadline_at = started_at + timedelta(seconds=duration)
        seat = next(item for item in room.seats if item.seat_key == "neg_1")
        seat.occupant_type = "ai"
        seat.user_id = None
        db.commit()


def write_silence(room_code: str, speech_id: str, *, seconds: float = 3.0) -> str:
    target_dir = settings.media_path / room_code
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{speech_id}.wav"
    with wave.open(str(target), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(b"\x00\x00" * int(24000 * seconds))
    return f"/media/{room_code}/{speech_id}.wav"


async def test_five_rooms_complete_concurrently_without_state_leakage(client, register_user, monkeypatch) -> None:
    topics = [f"并发隔离辩题-{index}" for index in range(5)]
    codes: list[str] = []

    for index, topic in enumerate(topics):
        owner = register_user(f"multi_owner_{index}")
        room_data = create_training_room(owner, topic)
        code = room_data["code"]
        codes.append(code)
        assert owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True}).status_code == 200
        assert owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={}).status_code == 200
        with SessionLocal() as db:
            room = load_room(db, code, lock=True)
            room.template_snapshot = [
                {"key": "aff_case", "name": "正方立论", "kind": "speech", "seat": "aff_1", "duration": 30},
                {"key": "neg_case", "name": "反方立论", "kind": "speech", "seat": "neg_1", "duration": 30},
                {"key": "judging", "name": "AI 裁判", "kind": "judging", "duration": 10},
            ]
            for seat in room.seats:
                seat.occupant_type = "ai"
                seat.user_id = None
                seat.display_name = f"AI-{code}-{seat.seat_key}"
            db.commit()

    active_agents = 0
    peak_agents = 0
    concurrency_lock = asyncio.Lock()

    async def generated(payload, *args, **kwargs):
        nonlocal active_agents, peak_agents
        async with concurrency_lock:
            active_agents += 1
            peak_agents = max(peak_agents, active_agents)
        await asyncio.sleep(0.02)
        async with concurrency_lock:
            active_agents -= 1
        return f"{payload['debate_topic']}｜{payload['holder']}｜{payload['current_stage']}"

    async def synthesized(*args, room_code: str, speech_id: str, **kwargs):
        await asyncio.sleep(0.005)
        return f"/media/mock/{room_code}/{speech_id}.wav"

    async def judged(topic, speeches, **kwargs):
        await asyncio.sleep(0.01)
        index = topics.index(topic)
        return {
            "winner": "aff" if index % 2 == 0 else "neg",
            "affirmative_score": 88.0 if index % 2 == 0 else 82.0,
            "negative_score": 82.0 if index % 2 == 0 else 88.0,
            "individual_scores": {},
            "reasoning": f"{topic} 的隔离裁判结果",
        }

    monkeypatch.setattr(debate_agent, "generate", generated)
    monkeypatch.setattr(lighttts, "synthesize", synthesized)
    monkeypatch.setattr(judge_provider, "judge", judged)

    for _ in range(4):
        await asyncio.gather(*(match_engine._process_room_locked(code) for code in codes))

    assert 1 < peak_agents <= settings.engine_max_concurrent_rooms, "Agent 调用应并行但保持全局有界"

    with SessionLocal() as db:
        room_ids: set[str] = set()
        for index, (code, topic) in enumerate(zip(codes, topics)):
            room = load_room(db, code)
            room_ids.add(room.id)
            match = db.scalar(select(Match).where(Match.room_id == room.id))
            scorecard = db.scalar(select(JudgeScorecard).where(JudgeScorecard.match_id == match.id))
            speeches = list(db.scalars(select(Speech).where(Speech.room_id == room.id).order_by(Speech.created_at)).all())
            events = list(db.scalars(select(MatchEvent).where(MatchEvent.room_id == room.id).order_by(MatchEvent.seq)).all())

            assert room.status == "completed"
            assert match.status == "completed"
            assert match.winner == ("aff" if index % 2 == 0 else "neg")
            assert scorecard and scorecard.reasoning == f"{topic} 的隔离裁判结果"
            assert len(speeches) == 2
            assert all(item.status == "completed" and topic in item.content for item in speeches)
            assert [item.seq for item in events] == list(range(1, room.seq + 1))
            assert all(item.match_id in {None, match.id} for item in events)
            assert not db.scalar(
                select(Speech).where(
                    Speech.room_id == room.id,
                    Speech.status.in_(["speaking", "synthesizing", "playing"]),
                )
            )
        assert len(room_ids) == len(codes)


async def test_five_mixed_rooms_advance_only_their_authoritative_state(client, register_user, monkeypatch) -> None:
    groups: dict[str, list[str]] = {
        "paused": [],
        "human_waiting": [],
        "human_expired": [],
        "playing": [],
        "judging": [],
    }
    for group in groups:
        for index in range(1):
            owner = register_user(f"mixed_{group}_{index}")
            room_data = create_training_room(owner, f"混合并发-{group}-{index}")
            code = room_data["code"]
            groups[group].append(code)
            assert owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True}).status_code == 200
            assert owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={}).status_code == 200

            with SessionLocal() as db:
                room = load_room(db, code, lock=True)
                match = db.scalar(select(Match).where(Match.room_id == room.id))
                if group == "preparing":
                    room.template_snapshot = [
                        {
                            "key": "opening",
                            "name": "规则播报",
                            "kind": "announcement",
                            "duration": 20,
                            "cue": f"欢迎进入 {room.topic}",
                        },
                        {"key": "human", "name": "正方发言", "kind": "speech", "seat": "aff_1", "duration": 60},
                    ]
                elif group == "judging":
                    room.template_snapshot = [{"key": "judging", "name": "自动裁判", "kind": "judging", "duration": 30}]
                    room.status = "judging"
                    room.current_stage_index = 0
                    match.status = "running"
                else:
                    room.template_snapshot = [
                        {"key": "aff_case", "name": "正方发言", "kind": "speech", "seat": "aff_1", "duration": 90},
                        {"key": "neg_case", "name": "反方发言", "kind": "speech", "seat": "neg_1", "duration": 90},
                    ]
                    room.current_stage_index = 0
                    room.stage_started_at = now()
                    if group == "paused":
                        room.status = "paused"
                        room.stage_deadline_at = None
                        room.paused_remaining_seconds = 37
                    else:
                        room.status = "running"
                        room.stage_deadline_at = now() + timedelta(seconds=-1 if group == "human_expired" else 90)
                    if group == "playing":
                        db.add(
                            Speech(
                                match_id=match.id,
                                room_id=room.id,
                                seat_key="neg_1",
                                stage_key="aff_case",
                                speaker_type="ai",
                                content=f"{room.topic} 的播放中内容",
                                audio_url=f"/media/{code}/playing.wav",
                                duration_seconds=90,
                                playback_started_at=now(),
                                playback_ends_at=now() + timedelta(seconds=60),
                                status="playing",
                            )
                        )
                db.commit()

    async def forbidden_agent(*args, **kwargs):
        raise AssertionError("mixed-state simulation must not call the debater Agent")

    background_cues: set[str] = set()

    async def synthesized(*args, room_code: str, speech_id: str, background: bool = False, **kwargs):
        assert background is True
        background_cues.add(room_code)
        target_dir = settings.media_path / room_code
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{speech_id}.wav"
        with wave.open(str(target), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(24000)
            audio.writeframes(b"\x00\x00" * 24000)
        return f"/media/{room_code}/{speech_id}.wav"

    async def judged(topic, speeches, **kwargs):
        return {
            "winner": "aff",
            "affirmative_score": 86,
            "negative_score": 82,
            "individual_scores": {},
            "reasoning": f"{topic} 的混合并发裁判结果",
        }

    monkeypatch.setattr(debate_agent, "generate", forbidden_agent)
    monkeypatch.setattr(lighttts, "synthesize", synthesized)
    monkeypatch.setattr(judge_provider, "judge", judged)
    # Exercise the production maximum of five rooms concurrently without letting unrelated active
    # rooms created by earlier tests enter this test's mocked Provider scope.
    target_codes = [code for codes in groups.values() for code in codes]
    await asyncio.gather(*(match_engine._process_room_locked(code) for code in target_codes))

    with SessionLocal() as db:
        for code in groups["paused"]:
            room = load_room(db, code)
            assert room.status == "paused" and room.current_stage_index == 0
            assert room.paused_remaining_seconds == 37 and room.stage_deadline_at is None
        for code in groups["human_waiting"]:
            room = load_room(db, code)
            assert room.status == "running" and room.current_stage_index == 0
            assert remaining_seconds(room) and remaining_seconds(room) > 0
        for code in groups["human_expired"]:
            room = load_room(db, code)
            assert room.status == "running" and room.current_stage_index == 1
        for code in groups["playing"]:
            room = load_room(db, code)
            speech = db.scalar(select(Speech).where(Speech.room_id == room.id, Speech.status == "playing"))
            assert room.status == "running" and room.current_stage_index == 0 and speech
        for code in groups["judging"]:
            room = load_room(db, code)
            match = db.scalar(select(Match).where(Match.room_id == room.id))
            scorecard = db.scalar(select(JudgeScorecard).where(JudgeScorecard.match_id == match.id))
            assert room.status == "completed" and match.status == "completed" and match.winner == "aff"
            assert scorecard and scorecard.reasoning == f"{room.topic} 的混合并发裁判结果"
        assert background_cues == set()

        for codes in groups.values():
            for code in codes:
                room = load_room(db, code)
                events = list(db.scalars(select(MatchEvent).where(MatchEvent.room_id == room.id).order_by(MatchEvent.seq)).all())
                assert [event.seq for event in events] == list(range(1, room.seq + 1))
                assert all(event.room_id == room.id for event in events)


@pytest.mark.parametrize("seed", [20260716, 20260717, 20260718])
def test_seeded_random_multi_user_operations_preserve_room_invariants(client, register_user, seed: int) -> None:
    rng = random.Random(seed)
    owners = [register_user(f"random_{seed}_owner_{index}") for index in range(5)]
    participants = [register_user(f"random_{seed}_participant_{index}") for index in range(8)]
    all_clients = owners + participants
    client_by_user_id = {item.get("/api/auth/session").json()["user"]["id"]: item for item in all_clients}
    rooms = [create_training_room(owner, f"随机多人状态回归-{seed}-{index}") for index, owner in enumerate(owners)]
    codes = [item["code"] for item in rooms]
    owner_by_code = dict(zip(codes, owners))

    def assert_invariants() -> None:
        with SessionLocal() as db:
            stored_rooms = list(db.scalars(select(Room).where(Room.code.in_(codes))).all())
            assert len(stored_rooms) == len(codes)
            for room in stored_rooms:
                events = list(db.scalars(select(MatchEvent).where(MatchEvent.room_id == room.id).order_by(MatchEvent.seq)).all())
                assert [item.seq for item in events] == list(range(1, room.seq + 1))
                assert db.scalar(select(func.count(Match.id)).where(Match.room_id == room.id)) <= 1
                active_speeches = db.scalar(
                    select(func.count(Speech.id)).where(
                        Speech.room_id == room.id,
                        Speech.status.in_(["speaking", "synthesizing", "playing"]),
                    )
                )
                assert active_speeches <= 1
                match = db.scalar(select(Match).where(Match.room_id == room.id))
                if room.status == "lobby":
                    assert match is None
                if room.status == "paused":
                    assert room.stage_deadline_at is None
                if room.status in {"review_required", "completed", "terminated", "cancelled"}:
                    assert active_speeches == 0 and room.completed_at is not None
                if room.status == "terminated":
                    assert match and match.status == "terminated"
                if room.status == "review_required":
                    assert match and match.status == "review_required"

            duplicate_assignments = list(
                db.execute(
                    select(RoomSeat.user_id, func.count(RoomSeat.id))
                    .join(Room, Room.id == RoomSeat.room_id)
                    .where(
                        RoomSeat.user_id.in_(client_by_user_id),
                        RoomSeat.occupant_type == "human",
                        Room.status.in_(["lobby", "preparing", "running", "paused", "judging"]),
                    )
                    .group_by(RoomSeat.user_id)
                    .having(func.count(RoomSeat.id) > 1)
                ).all()
            )
            assert duplicate_assignments == []

    lobby_actions = ["claim", "claim", "release", "ready", "ready", "unauthorized_start", "owner_start", "cancel"]
    for step in range(140):
        code = rng.choice(codes)
        action = rng.choice(lobby_actions if step > 70 else lobby_actions[:-1])
        owner = owner_by_code[code]
        participant = rng.choice(participants)
        if action == "claim":
            response = participant.post(f"/api/rooms/{code}/claim-seat", headers=csrf(participant), json={"seat_key": "neg_1"})
            assert response.status_code in {200, 409}
        elif action == "release":
            response = participant.post(f"/api/rooms/{code}/release-seat", headers=csrf(participant), json={})
            assert response.status_code in {200, 404, 409}
        elif action == "ready":
            actor = rng.choice([owner, participant])
            response = actor.post(f"/api/rooms/{code}/ready", headers=csrf(actor), json={"ready": rng.choice([True, False])})
            assert response.status_code in {200, 403, 409}
        elif action == "unauthorized_start":
            response = participant.post(f"/api/rooms/{code}/start", headers=csrf(participant), json={})
            assert response.status_code == 403
        elif action == "owner_start":
            response = owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
            assert response.status_code in {200, 409}
        else:
            response = owner.post(f"/api/rooms/{code}/cancel", headers=csrf(owner), json={})
            assert response.status_code in {200, 409}
        assert response.status_code < 500
        if step % 10 == 0:
            assert_invariants()

    started_codes: list[str] = []
    for code in codes:
        with SessionLocal() as db:
            room = load_room(db, code)
            status = room.status
            human_user_ids = [seat.user_id for seat in room.seats if seat.occupant_type == "human" and seat.user_id]
        if status == "lobby":
            for user_id in human_user_ids:
                actor = client_by_user_id[user_id]
                assert actor.post(f"/api/rooms/{code}/ready", headers=csrf(actor), json={"ready": True}).status_code == 200
            assert owner_by_code[code].post(f"/api/rooms/{code}/start", headers=csrf(owner_by_code[code]), json={}).status_code == 200
            status = "preparing"
        if status == "preparing":
            with SessionLocal() as db:
                room = load_room(db, code, lock=True)
                room.template_snapshot = [
                    {"key": "aff_case", "name": "正方发言", "kind": "speech", "seat": "aff_1", "duration": 180},
                    {"key": "neg_case", "name": "反方发言", "kind": "speech", "seat": "neg_1", "duration": 180},
                ]
                room.status = "running"
                room.current_stage_index = 0
                room.stage_started_at = now()
                room.stage_deadline_at = now() + timedelta(seconds=180)
                db.commit()
            started_codes.append(code)

    active_actions = ["lease", "start_speech", "finish_speech", "pause", "resume", "skip", "terminate", "outsider_control"]
    for step in range(180):
        if not started_codes:
            break
        code = rng.choice(started_codes)
        owner = owner_by_code[code]
        outsider = rng.choice(participants)
        lease_headers = csrf(owner) | {"X-Control-Lease": f"device-{code}"}
        action = rng.choice(active_actions)
        if action == "lease":
            response = owner.post(f"/api/rooms/{code}/control-lease", headers=lease_headers, json={})
            assert response.status_code in {200, 409}
        elif action == "start_speech":
            response = owner.post(f"/api/rooms/{code}/speech/start", headers=lease_headers, json={})
            assert response.status_code in {200, 403, 409}
        elif action == "finish_speech":
            active_view = owner.get(f"/api/rooms/{code}").json()["room"].get("active_speech")
            response = owner.post(
                f"/api/rooms/{code}/speech/finish",
                headers=lease_headers,
                json={"speech_id": active_view["id"] if active_view else "missing", "content": f"随机状态回归发言 {step}"},
            )
            assert response.status_code in {200, 409}
        elif action in {"pause", "resume", "skip", "terminate"}:
            response = owner.post(
                f"/api/rooms/{code}/control/{action}",
                headers=csrf(owner),
                json={"reason": f"randomized-{action}-{step}"},
            )
            assert response.status_code in {200, 409}
        else:
            response = outsider.post(
                f"/api/rooms/{code}/control/terminate",
                headers=csrf(outsider),
                json={"reason": "unauthorized"},
            )
            assert response.status_code == 403
        assert response.status_code < 500
        if step % 10 == 0:
            assert_invariants()

    for code in started_codes:
        owner = owner_by_code[code]
        response = owner.post(
            f"/api/rooms/{code}/control/terminate",
            headers=csrf(owner),
            json={"reason": "randomized-test-cleanup"},
        )
        assert response.status_code in {200, 409}
    assert_invariants()


async def test_concurrent_rooms_keep_distinct_frozen_judge_profiles(client, register_user, monkeypatch) -> None:
    with SessionLocal() as db:
        for profile in db.scalars(select(JudgeProfile)).all():
            profile.is_active = False
        db.flush()
        first = JudgeProfile(
            name="隔离裁判甲",
            endpoint="http://judge-a.test/api/judge",
            model_name="judge-a",
            system_prompt="甲裁判标准",
            timeout_seconds=60,
            is_active=True,
        )
        db.add(first)
        db.commit()
        first_id = first.id

    first_owner = register_user("judge_isolation_a")
    first_room = create_training_room(first_owner, "裁判快照隔离辩题甲")
    first_owner.post(f"/api/rooms/{first_room['code']}/ready", headers=csrf(first_owner), json={"ready": True})
    assert first_owner.post(f"/api/rooms/{first_room['code']}/start", headers=csrf(first_owner), json={}).status_code == 200

    with SessionLocal() as db:
        first = db.get(JudgeProfile, first_id)
        first.is_active = False
        db.flush()
        second = JudgeProfile(
            name="隔离裁判乙",
            endpoint="http://judge-b.test/api/judge",
            model_name="judge-b",
            system_prompt="乙裁判标准",
            timeout_seconds=180,
            is_active=True,
        )
        db.add(second)
        db.commit()
        second_id = second.id

    second_owner = register_user("judge_isolation_b")
    second_room = create_training_room(second_owner, "裁判快照隔离辩题乙")
    second_owner.post(f"/api/rooms/{second_room['code']}/ready", headers=csrf(second_owner), json={"ready": True})
    assert second_owner.post(f"/api/rooms/{second_room['code']}/start", headers=csrf(second_owner), json={}).status_code == 200

    for code in [first_room["code"], second_room["code"]]:
        with SessionLocal() as db:
            room = load_room(db, code, lock=True)
            room.status = "judging"
            room.current_stage_index = len(room.template_snapshot) - 1
            match = db.scalar(select(Match).where(Match.room_id == room.id))
            match.status = "running"
            db.commit()

    received: dict[str, dict] = {}

    async def judged(topic, speeches, *, profile=None):
        received[topic] = dict(profile or {})
        await asyncio.sleep(0.01)
        return {
            "winner": "aff" if topic.endswith("甲") else "neg",
            "affirmative_score": 88 if topic.endswith("甲") else 82,
            "negative_score": 82 if topic.endswith("甲") else 88,
            "individual_scores": {},
            "reasoning": f"{profile['name']} 完成独立裁决",
        }

    monkeypatch.setattr(judge_provider, "judge", judged)

    async def run_judge(code: str) -> None:
        with SessionLocal() as db:
            room = load_room(db, code, lock=True)
            await match_engine._judge(db, room)

    await asyncio.gather(run_judge(first_room["code"]), run_judge(second_room["code"]))

    assert received["裁判快照隔离辩题甲"]["id"] == first_id
    assert received["裁判快照隔离辩题甲"]["endpoint"] == "http://judge-a.test/api/judge"
    assert received["裁判快照隔离辩题乙"]["id"] == second_id
    assert received["裁判快照隔离辩题乙"]["endpoint"] == "http://judge-b.test/api/judge"
    with SessionLocal() as db:
        for code, expected_winner, expected_name in [
            (first_room["code"], "aff", "隔离裁判甲"),
            (second_room["code"], "neg", "隔离裁判乙"),
        ]:
            room = load_room(db, code)
            match = db.scalar(select(Match).where(Match.room_id == room.id))
            scorecard = db.scalar(select(JudgeScorecard).where(JudgeScorecard.match_id == match.id))
            assert room.status == "completed" and match.winner == expected_winner
            assert scorecard and expected_name in scorecard.reasoning


async def test_concurrent_room_preparation_uses_distinct_frozen_speech_services(client, register_user, monkeypatch) -> None:
    with SessionLocal() as db:
        asr = db.scalar(select(ProviderConfig).where(ProviderConfig.kind == "funasr"))
        tts = db.scalar(select(ProviderConfig).where(ProviderConfig.kind == "lighttts"))
        asr.endpoint = "ws://asr-snapshot-a.test:10095"
        asr.settings = {"final_wait_seconds": 20}
        asr.is_active = True
        tts.endpoint = "http://tts-snapshot-a.test/inference"
        tts.settings = {"read_timeout_seconds": 60, "speed": 1.1}
        tts.is_active = True
        db.commit()

    owner_a = register_user("service_isolation_a")
    room_a = create_training_room(owner_a, "语音服务并发隔离甲")
    owner_a.post(f"/api/rooms/{room_a['code']}/ready", headers=csrf(owner_a), json={"ready": True})
    assert owner_a.post(f"/api/rooms/{room_a['code']}/start", headers=csrf(owner_a), json={}).status_code == 200

    with SessionLocal() as db:
        asr = db.scalar(select(ProviderConfig).where(ProviderConfig.kind == "funasr"))
        tts = db.scalar(select(ProviderConfig).where(ProviderConfig.kind == "lighttts"))
        asr.endpoint = "wss://asr-snapshot-b.test/ws"
        asr.settings = {"final_wait_seconds": 45}
        tts.endpoint = "https://tts-snapshot-b.test/inference"
        tts.settings = {"read_timeout_seconds": 120, "speed": 0.85}
        db.commit()

    owner_b = register_user("service_isolation_b")
    room_b = create_training_room(owner_b, "语音服务并发隔离乙")
    owner_b.post(f"/api/rooms/{room_b['code']}/ready", headers=csrf(owner_b), json={"ready": True})
    assert owner_b.post(f"/api/rooms/{room_b['code']}/start", headers=csrf(owner_b), json={}).status_code == 200

    received: dict[str, dict] = {}

    async def synthesized(*args, room_code: str, speech_id: str, provider_config=None, **kwargs):
        received[room_code] = dict(provider_config or {})
        await asyncio.sleep(0.01)
        return f"/media/{room_code}/{speech_id}.wav"

    monkeypatch.setattr(lighttts, "synthesize", synthesized)
    await asyncio.gather(
        match_engine._process_room_locked(room_a["code"]),
        match_engine._process_room_locked(room_b["code"]),
    )

    assert received[room_a["code"]]["endpoint"] == "http://tts-snapshot-a.test/inference"
    assert received[room_a["code"]]["settings"] == {"read_timeout_seconds": 60, "speed": 1.1}
    assert received[room_b["code"]]["endpoint"] == "https://tts-snapshot-b.test/inference"
    assert received[room_b["code"]]["settings"] == {"read_timeout_seconds": 120, "speed": 0.85}
    with SessionLocal() as db:
        stored_a = load_room(db, room_a["code"])
        match_a = db.scalar(select(Match).where(Match.room_id == stored_a.id))
        stored_b = load_room(db, room_b["code"])
        match_b = db.scalar(select(Match).where(Match.room_id == stored_b.id))
        assert match_a.service_snapshot["funasr"]["endpoint"] == "ws://asr-snapshot-a.test:10095"
        assert match_b.service_snapshot["funasr"]["endpoint"] == "wss://asr-snapshot-b.test/ws"
        asr = db.scalar(select(ProviderConfig).where(ProviderConfig.kind == "funasr"))
        tts = db.scalar(select(ProviderConfig).where(ProviderConfig.kind == "lighttts"))
        asr.endpoint = settings.funasr_ws_url
        asr.settings = {"final_wait_seconds": 30.0}
        tts.endpoint = settings.lighttts_url
        tts.settings = {"read_timeout_seconds": 180, "speed": 1.0}
        db.commit()
    assert (
        owner_a.post(
            f"/api/rooms/{room_a['code']}/control/terminate",
            headers=csrf(owner_a),
            json={"reason": "测试结束"},
        ).status_code
        == 200
    )
    assert (
        owner_b.post(
            f"/api/rooms/{room_b['code']}/control/terminate",
            headers=csrf(owner_b),
            json={"reason": "测试结束"},
        ).status_code
        == 200
    )


async def test_room_scheduler_runs_independent_rooms_without_a_global_room_slot_bottleneck(client, monkeypatch) -> None:
    active = 0
    peak = 0

    async def simulated_room(code: str) -> None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.025)
        active -= 1

    monkeypatch.setattr(match_engine, "process_room", simulated_room)
    codes = [f"sim-{index}" for index in range(settings.engine_max_concurrent_rooms * 2 + 1)]
    started = time.monotonic()
    await asyncio.gather(*(match_engine._process_room_locked(code) for code in codes))
    elapsed = time.monotonic() - started

    assert peak == len(codes)
    sequential_duration = len(codes) * 0.025
    assert elapsed < sequential_duration * 0.6
    assert not any(code in match_engine._room_locks for code in codes)


async def test_room_deleted_during_background_processing_is_a_clean_cancellation(client, monkeypatch) -> None:
    async def deleted_room(code: str) -> None:
        raise HTTPException(status_code=404, detail="房间不存在。")

    monkeypatch.setattr(match_engine, "process_room", deleted_room)
    await match_engine._process_room_locked("deleted-room")
    assert "deleted-room" not in match_engine._room_locks
    assert match_engine._cue_job_cancelled("deleted-room") is True


async def test_engine_refuses_to_orphan_registered_room_task_when_event_loop_changes() -> None:
    engine = MatchEngine()
    release = asyncio.Event()
    task = asyncio.create_task(release.wait(), name="loop-ownership-regression")
    engine._runtime_loop = asyncio.get_running_loop()
    engine._room_tasks["owned-room"] = task

    def enter_different_loop() -> str:
        async def exercise() -> str:
            try:
                engine._ensure_runtime()
            except RuntimeError as exc:
                return str(exc)
            return ""

        return asyncio.run(exercise())

    try:
        message = await asyncio.to_thread(enter_different_loop)
        assert "event loop changed while room tasks were active" in message
        assert engine._room_tasks["owned-room"] is task
        assert not task.done()
    finally:
        release.set()
        await task
        await engine.drain_room_tasks()
    assert engine._room_tasks == {}


async def test_engine_cancel_drain_waits_for_room_task_cleanup() -> None:
    engine = MatchEngine()
    cleanup_complete = asyncio.Event()

    async def processing() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleanup_complete.set()

    task = asyncio.create_task(processing(), name="cancel-drain-regression")
    engine._runtime_loop = asyncio.get_running_loop()
    engine._room_tasks["draining-room"] = task
    await asyncio.sleep(0)

    await engine.drain_room_tasks(cancel=True)

    assert task.cancelled()
    assert cleanup_complete.is_set()
    assert engine._room_tasks == {}


async def test_tick_does_not_wait_for_slow_provider_room_before_scanning_again(client, register_user, monkeypatch) -> None:
    slow_owner = register_user("scheduler_slow")
    fast_owner = register_user("scheduler_fast")
    slow_code = create_training_room(slow_owner, "慢外部服务房间")["code"]
    fast_code = create_training_room(fast_owner, "真人计时房间")["code"]
    slow_started = asyncio.Event()
    fast_processed = asyncio.Event()
    release_slow = asyncio.Event()

    async def simulated_room(code: str) -> None:
        if code == slow_code:
            slow_started.set()
            await release_slow.wait()
        elif code == fast_code:
            fast_processed.set()

    monkeypatch.setattr(match_engine, "process_room", simulated_room)
    started = time.monotonic()
    await match_engine.tick()
    assert time.monotonic() - started < 0.2
    await asyncio.wait_for(slow_started.wait(), timeout=1)
    await asyncio.wait_for(fast_processed.wait(), timeout=1)
    assert slow_code in match_engine._room_tasks

    second_tick_started = time.monotonic()
    await match_engine.tick()
    assert time.monotonic() - second_tick_started < 0.2
    release_slow.set()
    while match_engine._room_tasks:
        await asyncio.gather(*list(match_engine._room_tasks.values()), return_exceptions=True)
        await asyncio.sleep(0)


async def test_scheduler_quarantines_only_the_repeatedly_failing_room(client, register_user, monkeypatch) -> None:
    bad_owner = register_user("scheduler_quarantine_bad")
    healthy_owner = register_user("scheduler_quarantine_healthy")
    bad_code = create_training_room(bad_owner, "状态机故障隔离房间")["code"]
    healthy_code = create_training_room(healthy_owner, "健康并行房间")["code"]
    assert bad_owner.post(f"/api/rooms/{bad_code}/ready", headers=csrf(bad_owner), json={"ready": True}).status_code == 200
    assert bad_owner.post(f"/api/rooms/{bad_code}/start", headers=csrf(bad_owner), json={}).status_code == 200

    with SessionLocal() as db:
        room = load_room(db, bad_code, lock=True)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        room.status = "running"
        room.current_stage_index = 0
        room.stage_started_at = now()
        room.stage_deadline_at = now() + timedelta(seconds=90)
        db.add(
            Speech(
                match_id=match.id,
                room_id=room.id,
                seat_key="aff_1",
                stage_key=room.template_snapshot[0]["key"],
                speaker_type="human",
                status="speaking",
            )
        )
        db.commit()

    healthy_runs = 0

    async def simulated_room(code: str) -> None:
        nonlocal healthy_runs
        if code == bad_code:
            raise RuntimeError("simulated invariant violation")
        if code == healthy_code:
            healthy_runs += 1

    monkeypatch.setattr(match_engine, "process_room", simulated_room)

    for attempt in range(3):
        match_engine._room_retry_at[bad_code] = 0
        await match_engine.tick()
        if match_engine._room_tasks:
            await asyncio.gather(*list(match_engine._room_tasks.values()), return_exceptions=True)
        await asyncio.sleep(0)
        if attempt < 2:
            with SessionLocal() as db:
                assert load_room(db, bad_code).status == "running"

    with SessionLocal() as db:
        bad_room = load_room(db, bad_code)
        healthy_room = load_room(db, healthy_code)
        active_speech = db.scalar(select(Speech).where(Speech.room_id == bad_room.id))
        quarantine_events = list(
            db.scalars(
                select(MatchEvent).where(
                    MatchEvent.room_id == bad_room.id,
                    MatchEvent.event_type == "engine.quarantined",
                )
            ).all()
        )
        assert bad_room.status == "paused"
        assert bad_room.stage_deadline_at is None
        assert bad_room.paused_remaining_seconds and bad_room.paused_remaining_seconds > 0
        assert "RuntimeError" in bad_room.failure_reason
        assert active_speech and active_speech.status == "interrupted"
        assert len(quarantine_events) == 1
        assert quarantine_events[0].payload == {"attempts": 3, "error_type": "RuntimeError"}
        assert healthy_room.status == "lobby"
    assert healthy_runs == 3
    assert bad_code not in match_engine._room_locks
    assert bad_code not in match_engine._room_failures
    assert bad_code not in match_engine._room_retry_at

    retry = bad_owner.post(
        f"/api/rooms/{bad_code}/control/retry",
        headers=csrf(bad_owner),
        json={"reason": "修复后重试"},
    )
    assert retry.status_code == 200
    assert retry.json()["room"]["status"] == "running"


async def test_scheduler_backs_off_transient_database_errors_without_quarantine(client, register_user, monkeypatch) -> None:
    owner = register_user("scheduler_transient_db")
    code = create_training_room(owner, "数据库瞬时故障退避房间")["code"]

    async def database_busy(_code: str) -> None:
        raise OperationalError("SELECT room", {}, RuntimeError("database temporarily busy"))

    monkeypatch.setattr(match_engine, "process_room", database_busy)
    observed_delays: list[float] = []
    for _ in range(4):
        match_engine._room_retry_at[code] = 0
        await match_engine.tick()
        if match_engine._room_tasks:
            await asyncio.gather(*list(match_engine._room_tasks.values()), return_exceptions=True)
        await asyncio.sleep(0)
        observed_delays.append(match_engine._room_retry_at[code] - time.monotonic())

    with SessionLocal() as db:
        room = load_room(db, code)
        quarantine_event = db.scalar(
            select(MatchEvent).where(
                MatchEvent.room_id == room.id,
                MatchEvent.event_type == "engine.quarantined",
            )
        )
        assert room.status == "lobby"
        assert room.failure_reason == ""
        assert quarantine_event is None
    assert observed_delays[0] > 0.8
    assert observed_delays[1] > 1.8
    assert observed_delays[2] > 3.8
    assert observed_delays[3] > 7.8
    assert match_engine._room_transient_failures[code] == 4

    async def recovered(_code: str) -> None:
        return None

    monkeypatch.setattr(match_engine, "process_room", recovered)
    match_engine._room_retry_at[code] = 0
    await match_engine.tick()
    if match_engine._room_tasks:
        await asyncio.gather(*list(match_engine._room_tasks.values()), return_exceptions=True)
    await asyncio.sleep(0)
    assert code not in match_engine._room_transient_failures
    assert code not in match_engine._room_retry_at


async def test_ai_audio_playback_blocks_stage_advance(client, register_user, monkeypatch) -> None:
    owner = register_user("playback_owner")
    room_data = create_training_room(owner, "AI 音频播放状态测试")
    code = room_data["code"]
    owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})
    owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.template_snapshot = [
            {"key": "aff_case", "name": "正方立论", "kind": "speech", "seat": "aff_1", "duration": 30},
            {"key": "judging", "name": "AI 裁判", "kind": "judging", "duration": 10},
        ]
        for seat in room.seats:
            seat.occupant_type = "ai"
            seat.user_id = None
        db.commit()

    async def generated(*args, **kwargs):
        return "等待语音播放完成后才能进入下一阶段。"

    async def synthesized(*args, room_code: str, speech_id: str, **kwargs):
        target_dir = settings.media_path / room_code
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{speech_id}.wav"
        with wave.open(str(target), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(24000)
            audio.writeframes(b"\x00\x00" * 6000)
        return f"/media/{room_code}/{speech_id}.wav"

    async def judged(*args, **kwargs):
        return {
            "winner": "aff",
            "affirmative_score": 90,
            "negative_score": 80,
            "individual_scores": {},
            "reasoning": "播放状态测试完成。",
        }

    monkeypatch.setattr(debate_agent, "generate", generated)
    monkeypatch.setattr(lighttts, "synthesize", synthesized)
    monkeypatch.setattr(judge_provider, "judge", judged)

    await match_engine.process_room(code)
    await match_engine.process_room(code)
    with SessionLocal() as db:
        room = load_room(db, code)
        speech = db.scalar(select(Speech).where(Speech.room_id == room.id))
        assert room.current_stage_index == 0
        assert speech.status == "playing"
        assert speech.duration_seconds == 0.25
        assert speech.playback_ends_at is not None

    await match_engine.process_room(code)
    with SessionLocal() as db:
        room = load_room(db, code)
        speech = db.scalar(select(Speech).where(Speech.room_id == room.id))
        assert room.current_stage_index == 0 and speech.status == "playing"
        speech.playback_ends_at = now() - timedelta(seconds=1)
        db.commit()

    await match_engine.process_room(code)
    with SessionLocal() as db:
        room = load_room(db, code)
        speech = db.scalar(select(Speech).where(Speech.room_id == room.id))
        assert room.current_stage_index == 1 and speech.status == "completed"


async def test_free_ai_provider_preparation_does_not_consume_speaking_clock(client, register_user, monkeypatch) -> None:
    owner = register_user("free_ai_preparation_owner")
    room_data = create_training_room(owner, "AI 自由辩论准备时间不应吞掉发言时间")
    code = room_data["code"]
    owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})
    owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        started_at = now()
        room.template_snapshot = [
            {
                "key": "free",
                "name": "自由辩论",
                "kind": "free",
                "side": "aff",
                "duration": 120,
                "turn_duration": 30,
                "turn_started_at": started_at.isoformat(),
            }
        ]
        room.current_stage_index = 0
        room.status = "running"
        room.stage_started_at = started_at
        room.stage_deadline_at = started_at + timedelta(seconds=120)
        for seat in room.seats:
            seat.occupant_type = "ai"
            seat.user_id = None
        db.commit()

    async def generated(*args, **kwargs):
        with SessionLocal() as db:
            room = load_room(db, code, lock=True)
            current = dict(room.template_snapshot[0])
            current["turn_started_at"] = (now() - timedelta(seconds=60)).isoformat()
            room.template_snapshot = [current]
            room.stage_deadline_at = now() - timedelta(seconds=1)
            db.commit()
            assert remaining_seconds(room) >= 118
            assert free_turn_remaining_seconds(room) >= 28
            assert room.template_snapshot[0]["ai_preparing"] is True
        return "AI 准备完成后应获得完整的播放时间。"

    async def synthesized(*args, room_code: str, speech_id: str, should_cancel, **kwargs):
        assert should_cancel() is False
        target_dir = settings.media_path / room_code
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{speech_id}.wav"
        with wave.open(str(target), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(24000)
            audio.writeframes(b"\x00\x00" * 24000)
        return f"/media/{room_code}/{speech_id}.wav"

    monkeypatch.setattr(debate_agent, "generate", generated)
    monkeypatch.setattr(lighttts, "synthesize", synthesized)
    await match_engine.process_room(code)

    with SessionLocal() as db:
        room = load_room(db, code)
        speech = db.scalar(select(Speech).where(Speech.room_id == room.id))
        event = db.scalar(
            select(MatchEvent).where(
                MatchEvent.room_id == room.id,
                MatchEvent.event_type == "speech.audio.ready",
            )
        )
        assert speech and speech.status == "playing"
        assert remaining_seconds(room) >= 118
        assert free_turn_remaining_seconds(room) >= 28
        assert "ai_preparing" not in room.template_snapshot[0]
        assert event and event.payload["preparation_seconds"] >= 0


async def test_streamed_free_ai_final_wav_preserves_match_and_turn_deadlines(client, register_user, monkeypatch) -> None:
    owner = register_user("free_ai_stream_deadline_owner")
    room_data = create_training_room(owner, "流式自由辩论不得用音频尾点覆盖整段计时")
    code = room_data["code"]
    owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})
    owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        started_at = now()
        room.template_snapshot = [
            {
                "key": "free",
                "name": "自由辩论",
                "kind": "free",
                "side": "aff",
                "duration": 120,
                "turn_duration": 30,
                "turn_started_at": started_at.isoformat(),
            }
        ]
        room.current_stage_index = 0
        room.status = "running"
        room.stage_started_at = started_at
        room.stage_deadline_at = started_at + timedelta(seconds=120)
        for seat in room.seats:
            seat.occupant_type = "ai"
            seat.user_id = None
        db.commit()

    async def generated(*args, **kwargs):
        return "流式音频最终归档只能确定本轮播放尾点，不能缩短整段自由辩论。"

    async def streamed(*args, room_code: str, speech_id: str, on_stream_event, **kwargs):
        await on_stream_event(
            {
                "type": "audio.stream.started",
                "generation": "a" * 32,
                "stream_url": f"/ws/rooms/{room_code}/audio",
                "sample_rate": 24_000,
                "channels": 1,
                "sample_width": 2,
            }
        )
        return write_silence(room_code, speech_id, seconds=8)

    monkeypatch.setattr(settings, "lighttts_streaming_enabled", True)
    monkeypatch.setattr(debate_agent, "generate", generated)
    monkeypatch.setattr(lighttts, "synthesize", streamed)
    await match_engine.process_room(code)

    with SessionLocal() as db:
        room = load_room(db, code)
        speech = db.scalar(select(Speech).where(Speech.room_id == room.id))
        assert speech and speech.status == "playing"
        assert speech.playback_started_at and speech.playback_ends_at and room.stage_deadline_at
        assert 7.5 <= (speech.playback_ends_at - speech.playback_started_at).total_seconds() <= 8.5
        assert (room.stage_deadline_at - speech.playback_started_at).total_seconds() >= 115
        assert room.stage_deadline_at > speech.playback_ends_at + timedelta(seconds=100)
        assert room.current_stage_index == 0
        assert free_turn_remaining_seconds(room) >= 27
        assert remaining_seconds(room) >= 115


async def test_free_ai_audio_finishes_naturally_after_nominal_turn_boundary(client, register_user) -> None:
    owner = register_user("free_ai_natural_boundary_owner")
    room_data = create_training_room(owner, "AI 自由辩论不得在句中硬切")
    code = room_data["code"]
    owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})
    owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        started_at = now() - timedelta(seconds=41)
        room.template_snapshot = [
            {
                "key": "free",
                "name": "自由辩论",
                "kind": "free",
                "side": "aff",
                "duration": 240,
                "turn_duration": 40,
                "turn_started_at": started_at.isoformat(),
            }
        ]
        room.current_stage_index = 0
        room.status = "running"
        room.stage_started_at = started_at
        room.stage_deadline_at = now() + timedelta(seconds=199)
        speech = Speech(
            match_id=match.id,
            room_id=room.id,
            seat_key="aff_1",
            stage_key="free",
            speaker_type="ai",
            content="这段音频比名义轮次更长，但应完整播到自然句尾。",
            audio_url=f"/media/{code}/long-free-turn.wav",
            duration_seconds=56,
            playback_started_at=started_at,
            playback_ends_at=now() + timedelta(seconds=15),
            status="playing",
        )
        db.add(speech)
        db.commit()
        speech_id = speech.id

    await match_engine.process_room(code)
    with SessionLocal() as db:
        room = load_room(db, code)
        speech = db.get(Speech, speech_id)
        assert speech.status == "playing"
        assert room.template_snapshot[0]["side"] == "aff"
        assert not db.scalar(
            select(MatchEvent).where(
                MatchEvent.room_id == room.id,
                MatchEvent.event_type == "speech.timed_out",
            )
        )
        speech.playback_ends_at = now() - timedelta(seconds=1)
        db.commit()

    await match_engine.process_room(code)
    with SessionLocal() as db:
        room = load_room(db, code)
        speech = db.get(Speech, speech_id)
        assert speech.status == "completed"
        assert room.template_snapshot[0]["side"] == "aff"
        assert room.template_snapshot[0]["intermission_side"] == "neg"
        assert room.template_snapshot[0]["intermission_deadline_at"]

        current = dict(room.template_snapshot[0])
        current["intermission_deadline_at"] = (now() - timedelta(milliseconds=1)).isoformat()
        room.template_snapshot = [current]
        db.commit()

    await match_engine.process_room(code)
    with SessionLocal() as db:
        room = load_room(db, code)
        assert room.template_snapshot[0]["side"] == "neg"


async def test_fixed_ai_provider_preparation_freezes_clock_until_audio_is_ready(client, register_user, monkeypatch) -> None:
    owner = register_user("fixed_ai_preparation_owner")
    room_data = create_training_room(owner, "固定发言准备时间不应吞掉播放时间")
    code = room_data["code"]
    owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})
    owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    configure_running_ai_stage(code, duration=90)

    async def generated(*args, **kwargs):
        with SessionLocal() as db:
            room = load_room(db, code, lock=True)
            assert room.stage_deadline_at is None
            assert remaining_seconds(room) >= 88
            assert room.template_snapshot[0]["ai_preparing"] is True
            room.stage_started_at = now() - timedelta(minutes=5)
            db.commit()
        return "固定发言的 Agent 与语音准备期间，页面计时必须保持冻结。"

    async def synthesized(*args, room_code: str, speech_id: str, **kwargs):
        with SessionLocal() as db:
            room = load_room(db, code)
            assert room.stage_deadline_at is None
            assert remaining_seconds(room) >= 88
            assert room.template_snapshot[0]["ai_preparing"] is True
        return write_silence(room_code, speech_id, seconds=3)

    monkeypatch.setattr(debate_agent, "generate", generated)
    monkeypatch.setattr(lighttts, "synthesize", synthesized)
    await match_engine.process_room(code)

    with SessionLocal() as db:
        room = load_room(db, code)
        speech = db.scalar(select(Speech).where(Speech.room_id == room.id).order_by(Speech.created_at.desc()))
        assert speech and speech.status == "playing"
        assert speech.playback_started_at and speech.playback_ends_at
        assert room.stage_started_at == speech.playback_started_at
        assert room.stage_deadline_at == speech.playback_ends_at
        assert remaining_seconds(room) >= 2
        assert "ai_preparing" not in room.template_snapshot[0]


async def test_lighttts_admission_overload_retries_without_pausing_or_regenerating_agent(
    client,
    register_user,
    monkeypatch,
) -> None:
    owner = register_user("tts_overload_recovers")
    room_data = create_training_room(owner, "语音过载自动恢复不应暂停比赛")
    code = room_data["code"]
    owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})
    owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    configure_running_ai_stage(code)
    monkeypatch.setattr(match_engine, "_LIGHTTTS_ADMISSION_RETRY_DELAYS", (0.0, 0.0, 0.0))
    agent_calls = 0
    tts_calls = 0
    shared_deadlines: list[float] = []

    async def generated(*args, **kwargs):
        nonlocal agent_calls
        agent_calls += 1
        return "Agent 只生成一次，语音调度过载由引擎有限重试。"

    async def synthesized(*args, room_code: str, speech_id: str, deadline_monotonic: float, **kwargs):
        nonlocal tts_calls
        tts_calls += 1
        shared_deadlines.append(deadline_monotonic)
        if tts_calls <= 3:
            raise ProviderError(
                "LightTTS 全局队列已满，请稍后重试。",
                code="lighttts_queue_full",
                retryable=True,
                retry_after_seconds=0,
            )
        return write_silence(room_code, speech_id, seconds=2)

    monkeypatch.setattr(debate_agent, "generate", generated)
    monkeypatch.setattr(lighttts, "synthesize", synthesized)
    await match_engine.process_room(code)

    with SessionLocal() as db:
        room = load_room(db, code)
        speech = db.scalar(select(Speech).where(Speech.room_id == room.id).order_by(Speech.created_at.desc()))
        retry_events = list(
            db.scalars(
                select(MatchEvent).where(
                    MatchEvent.room_id == room.id,
                    MatchEvent.event_type == "provider.retrying",
                )
            ).all()
        )
        failed_event = db.scalar(
            select(MatchEvent.id).where(
                MatchEvent.room_id == room.id,
                MatchEvent.event_type == "provider.failed",
            )
        )
        assert room.status == "running" and room.failure_reason == ""
        assert speech and speech.status == "playing"
        assert [event.payload["attempt"] for event in retry_events] == [1, 2, 3]
        assert not failed_event
    assert agent_calls == 1
    assert tts_calls == 4
    assert len(set(shared_deadlines)) == 1


async def test_lighttts_admission_retry_exhaustion_pauses_only_after_bounded_attempts(
    client,
    register_user,
    monkeypatch,
) -> None:
    owner = register_user("tts_overload_exhausted")
    room_data = create_training_room(owner, "语音过载重试耗尽后安全暂停")
    code = room_data["code"]
    owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})
    owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    configure_running_ai_stage(code)
    monkeypatch.setattr(match_engine, "_LIGHTTTS_ADMISSION_RETRY_DELAYS", (0.0, 0.0, 0.0))
    agent_calls = 0
    tts_calls = 0

    async def generated(*args, **kwargs):
        nonlocal agent_calls
        agent_calls += 1
        return "重试耗尽前不重复生成 Agent 内容。"

    async def overloaded(*args, **kwargs):
        nonlocal tts_calls
        tts_calls += 1
        raise ProviderError(
            "LightTTS 全局排队超时，请稍后重试。",
            code="lighttts_queue_timeout",
            retryable=True,
            retry_after_seconds=0,
        )

    monkeypatch.setattr(debate_agent, "generate", generated)
    monkeypatch.setattr(lighttts, "synthesize", overloaded)
    await match_engine.process_room(code)

    with SessionLocal() as db:
        room = load_room(db, code)
        speech = db.scalar(select(Speech).where(Speech.room_id == room.id).order_by(Speech.created_at.desc()))
        retry_count = db.scalar(
            select(func.count(MatchEvent.id)).where(
                MatchEvent.room_id == room.id,
                MatchEvent.event_type == "provider.retrying",
            )
        )
        failed = db.scalar(
            select(MatchEvent).where(
                MatchEvent.room_id == room.id,
                MatchEvent.event_type == "provider.failed",
            )
        )
        assert room.status == "paused"
        assert room.paused_remaining_seconds >= 88
        assert speech and speech.status == "failed"
        assert retry_count == 3
        assert failed and failed.payload["code"] == "lighttts_queue_timeout"
        assert failed.payload["retryable"] is True
    assert agent_calls == 1
    assert tts_calls == 4


async def test_lighttts_overload_retry_wait_is_cancellation_aware(monkeypatch) -> None:
    engine = MatchEngine()
    monkeypatch.setattr(engine, "_LIGHTTTS_ADMISSION_RETRY_DELAYS", (30.0,))
    monkeypatch.setattr(engine, "_LIGHTTTS_RETRY_POLL_SECONDS", 0.001)
    cancelled = False
    attempts = 0

    async def overloaded() -> str:
        nonlocal attempts
        attempts += 1
        raise ProviderError(
            "LightTTS 全局队列已满，请稍后重试。",
            code="lighttts_queue_full",
            retryable=True,
            retry_after_seconds=0,
        )

    async def mark_cancelled(_exc: ProviderError, _attempt: int, _delay: float) -> None:
        nonlocal cancelled
        cancelled = True

    with pytest.raises(ProviderCancelled, match="重试已取消"):
        await engine._synthesize_with_admission_retry(
            overloaded,
            should_cancel=lambda: cancelled,
            on_retry=mark_cancelled,
            deadline_monotonic=asyncio.get_running_loop().time() + 60,
        )
    assert attempts == 1


async def test_lighttts_retry_sequence_uses_one_total_deadline_instead_of_resetting_each_attempt(monkeypatch) -> None:
    engine = MatchEngine()
    monkeypatch.setattr(engine, "_LIGHTTTS_ADMISSION_RETRY_DELAYS", (0.02, 0.02, 0.02))
    monkeypatch.setattr(engine, "_LIGHTTTS_RETRY_POLL_SECONDS", 0.001)
    attempts = 0
    observed_delays: list[float] = []

    async def overloaded() -> str:
        nonlocal attempts
        attempts += 1
        raise ProviderError(
            "LightTTS 全局队列已满，请稍后重试。",
            code="lighttts_queue_full",
            retryable=True,
            retry_after_seconds=0,
        )

    async def record_retry(_exc: ProviderError, _attempt: int, delay: float) -> None:
        observed_delays.append(delay)

    started = time.monotonic()
    with pytest.raises(ProviderError) as captured:
        await engine._synthesize_with_admission_retry(
            overloaded,
            should_cancel=lambda: False,
            on_retry=record_retry,
            deadline_monotonic=asyncio.get_running_loop().time() + 0.03,
        )
    elapsed = time.monotonic() - started
    assert captured.value.code == "lighttts_job_timeout"
    assert attempts == 2
    assert len(observed_delays) == 2
    assert observed_delays[1] < 0.02
    assert 0.02 <= elapsed < 0.1


async def test_free_debate_tts_retry_reuses_immediate_seat_once_then_returns_to_normal_rotation(
    client,
    register_user,
    monkeypatch,
) -> None:
    owner = register_user("free_tts_retry_same_seat")
    detail = client.get("/api/competitions/daily-4v4").json()["competition"]
    created = owner.post(
        "/api/rooms",
        headers=csrf(owner),
        json={
            "competition_slug": "daily-4v4",
            "topic_id": detail["topics"][0]["id"],
            "seat_key": "aff_1",
            "visibility": "public",
        },
    )
    code = created.json()["room"]["code"]
    owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})
    owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    started_at = now()
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        current = {
            "key": "free",
            "name": "自由辩论",
            "kind": "free",
            "side": "aff",
            "duration": 300,
            "turn_duration": 45,
            "turn_started_at": started_at.isoformat(),
        }
        room.template_snapshot = [current]
        room.current_stage_index = 0
        room.status = "running"
        room.stage_started_at = started_at
        room.stage_deadline_at = started_at + timedelta(seconds=300)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        for seat in room.seats:
            seat.occupant_type = "ai"
            seat.user_id = None
        failed = Speech(
            match_id=match.id,
            room_id=room.id,
            seat_key="aff_2",
            stage_key="free",
            speaker_type="ai",
            status="failed_retried",
            content="本轮 Agent 已生成，首次 TTS 失败后必须立即由原席位重试。",
        )
        db.add(failed)
        db.flush()
        source_speech_id = failed.id
        db.commit()

    async def forbidden_agent(*args, **kwargs):
        raise AssertionError("immediate free-debate TTS retry must not call Agent again")

    async def synthesized(*args, room_code: str, speech_id: str, **kwargs):
        return write_silence(room_code, speech_id, seconds=1)

    monkeypatch.setattr(debate_agent, "generate", forbidden_agent)
    monkeypatch.setattr(lighttts, "synthesize", synthesized)
    await match_engine.process_room(code)
    with SessionLocal() as db:
        room = load_room(db, code)
        retry = db.scalar(
            select(Speech)
            .where(Speech.room_id == room.id, Speech.status == "playing")
            .order_by(Speech.created_at.desc(), Speech.id.desc())
        )
        assert retry and retry.seat_key == "aff_2"
        assert retry.content == "本轮 Agent 已生成，首次 TTS 失败后必须立即由原席位重试。"
        retry.playback_ends_at = now() - timedelta(seconds=1)
        db.commit()

    await match_engine.process_room(code)
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        current = dict(room.template_snapshot[0])
        assert current["intermission_side"] == "neg"
        current["intermission_deadline_at"] = (now() - timedelta(milliseconds=1)).isoformat()
        room.template_snapshot = [current]
        db.commit()
    await match_engine.process_room(code)
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        current = dict(room.template_snapshot[0])
        assert current["side"] == "neg"
        current["side"] = "aff"
        current["turn_started_at"] = now().isoformat()
        room.template_snapshot = [current]
        db.commit()

    agent_calls = 0

    async def fresh_agent(*args, **kwargs):
        nonlocal agent_calls
        agent_calls += 1
        return "下一正常轮必须重新生成内容，不能再次复用旧失败文本。"

    monkeypatch.setattr(debate_agent, "generate", fresh_agent)
    await match_engine.process_room(code)

    with SessionLocal() as db:
        room = load_room(db, code)
        latest = db.scalar(
            select(Speech)
            .where(Speech.room_id == room.id)
            .order_by(Speech.created_at.desc(), Speech.id.desc())
        )
        reuse_events = list(
            db.scalars(
                select(MatchEvent).where(
                    MatchEvent.room_id == room.id,
                    MatchEvent.event_type == "speech.content.reused",
                )
            ).all()
        )
        assert latest and latest.status == "playing" and latest.seat_key == "aff_1"
        assert latest.content == "下一正常轮必须重新生成内容，不能再次复用旧失败文本。"
        assert len(reuse_events) == 1
        assert reuse_events[0].payload["source_speech_id"] == source_speech_id
    # The first call is the next-side candidate started during the three-second
    # window; this test then rewrites the side to exercise rotation, so that
    # candidate is correctly discarded and the authoritative side generates
    # once more. Neither call may reuse the stale failed TTS text.
    assert agent_calls == 2


def test_free_debate_rotates_across_ai_teammates(client, register_user) -> None:
    owner = register_user("rotation_owner")
    detail = client.get("/api/competitions/daily-4v4").json()["competition"]
    created = owner.post(
        "/api/rooms",
        headers=csrf(owner),
        json={
            "competition_slug": "daily-4v4",
            "topic_id": detail["topics"][0]["id"],
            "seat_key": "aff_1",
            "visibility": "public",
        },
    )
    code = created.json()["room"]["code"]
    owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})
    owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        current = {"key": "free", "name": "自由辩论", "kind": "free", "side": "aff", "duration": 300, "turn_duration": 45}
        room.template_snapshot = [current]
        room.current_stage_index = 0
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        for seat in room.seats:
            seat.occupant_type = "ai"
            seat.user_id = None
        db.add_all(
            [
                Speech(match_id=match.id, room_id=room.id, seat_key="aff_1", stage_key="free", speaker_type="ai", status="completed"),
                Speech(match_id=match.id, room_id=room.id, seat_key="aff_1", stage_key="free", speaker_type="ai", status="completed"),
                Speech(match_id=match.id, room_id=room.id, seat_key="aff_2", stage_key="free", speaker_type="ai", status="completed"),
            ]
        )
        db.commit()
        selected = match_engine._free_ai_seat(db, room, current, "aff")
        assert selected and selected.seat_key == "aff_3"


@hypothesis_settings(max_examples=120, deadline=None)
@given(
    room_status=st.sampled_from(["lobby", "preparing", "running", "paused", "judging", "completed", "terminated"]),
    stage_kind=st.sampled_from(["speech", "free", "cue", "judging"]),
    target_matches=st.booleans(),
    side_matches=st.booleans(),
    occupant_type=st.sampled_from(["human", "ai", "ai_substitute", "open"]),
    active_mode=st.sampled_from(["none", "self", "other"]),
)
def test_speaking_permission_invariants(
    room_status: str,
    stage_kind: str,
    target_matches: bool,
    side_matches: bool,
    occupant_type: str,
    active_mode: str,
) -> None:
    user = User(id="user-1", account="user_1", real_name="测试辩手", password_hash="hash")
    seat = RoomSeat(
        id="seat-1",
        room_id="room-1",
        seat_key="aff_1",
        side="aff",
        position=1,
        occupant_type=occupant_type,
        user_id=user.id,
        display_name=user.real_name,
        connected=True,
    )
    current = {
        "key": "stage-1",
        "name": "随机测试环节",
        "kind": stage_kind,
        "seat": "aff_1" if target_matches else "neg_1",
        "side": "aff" if side_matches else "neg",
        "duration": 60,
    }
    room = Room(
        id="room-1",
        code="123456",
        competition_id="competition-1",
        owner_id=user.id,
        topic="属性测试辩题",
        status=room_status,
        template_snapshot=[current],
        current_stage_index=0,
    )
    room.seats = [seat]
    if active_mode != "none":
        room._active_speech = Speech(
            id="speech-1",
            match_id="match-1",
            room_id=room.id,
            seat_key=seat.seat_key if active_mode == "self" else "neg_1",
            stage_key="stage-1",
            speaker_type="human",
            status="speaking",
        )

    allowed, reason = speaking_permission(room, user)

    expected = (
        room_status == "running"
        and occupant_type == "human"
        and stage_kind in {"speech", "free"}
        and (stage_kind != "speech" or target_matches)
        and (stage_kind != "free" or side_matches)
        and active_mode == "none"
    )
    assert allowed is expected, reason


def test_speaking_permission_reports_judging_stage_instead_of_not_started() -> None:
    user = User(id="user-judging", account="user_judging", real_name="裁判阶段辩手", password_hash="hash")
    seat = RoomSeat(
        id="seat-judging",
        room_id="room-judging",
        seat_key="aff_1",
        side="aff",
        position=1,
        occupant_type="human",
        user_id=user.id,
        display_name=user.real_name,
        connected=True,
    )
    room = Room(
        id="room-judging",
        code="654321",
        competition_id="competition-judging",
        owner_id=user.id,
        topic="裁判阶段提示语测试",
        status="judging",
        template_snapshot=[{"key": "judging", "name": "AI 裁判", "kind": "judging", "duration": 10}],
        current_stage_index=0,
    )
    room.seats = [seat]

    assert speaking_permission(room, user) == (False, "裁判正在评议")
