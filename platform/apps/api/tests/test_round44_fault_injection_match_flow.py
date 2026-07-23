from __future__ import annotations

import time
from datetime import timedelta

import pytest
from app.core.config import settings
from app.core.database import SessionLocal
from app.models.entities import AudioAsset, AudioCue, Match, MatchEvent
from app.services.match_engine import match_engine
from app.services.room_service import free_turn_remaining_seconds, load_room, now, stage
from conftest import csrf
from sqlalchemy import select
from test_platform import _wav_bytes, create_training_room


def _ready(browser, code: str) -> None:
    response = browser.post(f"/api/rooms/{code}/ready", headers=csrf(browser), json={"ready": True})
    assert response.status_code == 200, response.text


def _start(browser, code: str) -> None:
    response = browser.post(f"/api/rooms/{code}/start", headers=csrf(browser), json={})
    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_start_with_prebuilt_host_cue_enters_stage_without_mysterious_wait(
    register_user,
    monkeypatch,
) -> None:
    """A ready preset must make startup a single preparation tick.

    The public UI should not leave students staring at an unexplained
    ``preparing`` state while the first announcement is regenerated.  This
    test uses the same immutable cue path as production and fails if the
    provider is called despite the preset being available.
    """

    owner = register_user("round44_preset_start")
    code = create_training_room(owner, "Round44 预生成开场不应等待")["code"]
    _ready(owner, code)
    _start(owner, code)

    cue_text = "Round44 比赛即将开始，请双方准备。"
    cue = AudioCue(
        key="round44_opening",
        name="Round44 开场",
        text=cue_text,
        audio_url="/media/_cues/round44-opening.wav",
        is_active=True,
    )
    cue_path = settings.media_path / "_cues" / "round44-opening.wav"
    cue_path.parent.mkdir(parents=True, exist_ok=True)
    cue_path.write_bytes(_wav_bytes(0.25))
    with SessionLocal() as db:
        db.add(cue)
        room = load_room(db, code, lock=True)
        room.template_snapshot = [
            {
                "key": "round44_opening",
                "name": "开场",
                "kind": "announcement",
                "duration": 8,
                "cue": cue_text,
            }
        ]
        db.commit()

    async def must_not_call_provider(*_args, **_kwargs):
        raise AssertionError("预生成主持提示音不应重新调用 TTS")

    monkeypatch.setattr("app.services.match_engine.lighttts.synthesize", must_not_call_provider)
    monkeypatch.setattr("app.services.match_engine.moss_tts_realtime.synthesize", must_not_call_provider)

    await match_engine.process_room(code)
    first = owner.get(f"/api/rooms/{code}")
    assert first.status_code == 200
    first_room = first.json()["room"]
    assert first_room["status"] == "running"
    assert first_room["current_stage"]["key"] == "round44_opening"
    assert first_room["failure_reason"] == ""
    # Real cue duration (0.25 s) plus the delivery guard is used; countdown
    # must be positive but must not expose the template's unrelated 8 seconds.
    assert 0 < first_room["remaining_seconds"] <= 2

    # A short delay must never increase the authoritative countdown.
    before = first_room["remaining_seconds"]
    time.sleep(0.06)
    after = owner.get(f"/api/rooms/{code}").json()["room"]["remaining_seconds"]
    assert after <= before

    with SessionLocal() as db:
        room = load_room(db, code)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        asset = db.scalar(select(AudioAsset).where(AudioAsset.match_id == match.id, AudioAsset.kind == "cue:round44_opening"))
        events = list(db.scalars(select(MatchEvent).where(MatchEvent.room_id == room.id).order_by(MatchEvent.seq)).all())
        assert asset and asset.storage_key == cue.audio_url
        assert [item.seq for item in events] == list(range(1, room.seq + 1))
        assert any(item.event_type == "audio.cue.ready" and item.payload.get("source") == "preset" for item in events)
        persisted_cue = db.scalar(select(AudioCue).where(AudioCue.key == "round44_opening"))
        assert persisted_cue is not None
        db.delete(persisted_cue)
        db.commit()
    cue_path.unlink(missing_ok=True)


def test_fixed_stage_exposes_only_the_current_human_speaker_and_countdown_is_monotonic(register_user, client) -> None:
    """A four-person room must make one, and only one, seat actionable."""

    owner = register_user("round44_permission_owner")
    aff_two = register_user("round44_permission_aff_two")
    neg_one = register_user("round44_permission_neg_one")
    neg_two = register_user("round44_permission_neg_two")
    competition = client.get("/api/competitions/daily-4v4").json()["competition"]
    created = owner.post(
        "/api/rooms",
        headers=csrf(owner),
        json={
            "competition_slug": "daily-4v4",
            "topic_id": competition["topics"][0]["id"],
            "seat_key": "aff_1",
            "visibility": "public",
        },
    )
    assert created.status_code == 200, created.text
    code = created.json()["room"]["code"]
    for browser, seat_key in ((aff_two, "aff_2"), (neg_one, "neg_1"), (neg_two, "neg_2")):
        claimed = browser.post(f"/api/rooms/{code}/claim-seat", headers=csrf(browser), json={"seat_key": seat_key})
        assert claimed.status_code == 200, claimed.text
    for browser in (owner, aff_two, neg_one, neg_two):
        _ready(browser, code)
    _start(owner, code)
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.status = "running"
        room.current_stage_index = 0
        room.stage_started_at = now()
        room.stage_deadline_at = now() + timedelta(seconds=30)
        room.template_snapshot = [{"key": "round44_neg_two", "name": "反方二辩", "kind": "speech", "seat": "neg_2", "duration": 30}]
        db.commit()

    projections = {browser: browser.get(f"/api/rooms/{code}").json()["room"] for browser in (owner, aff_two, neg_one, neg_two)}
    assert projections[neg_two]["can_speak"] is True
    assert projections[neg_two]["speak_reason"] == "轮到你发言"
    for browser in (owner, aff_two, neg_one):
        assert projections[browser]["can_speak"] is False
        assert "当前轮到" in projections[browser]["speak_reason"]

    # A wrong-seat request must be rejected by the API, not merely disabled in
    # the browser.  The target seat can start after taking its device lease.
    forbidden = owner.post(
        f"/api/rooms/{code}/speech/start",
        headers=csrf(owner) | {"X-Control-Lease": "wrong-seat"},
        json={},
    )
    assert forbidden.status_code == 403
    target_headers = csrf(neg_two) | {"X-Control-Lease": "round44-neg-two-device"}
    lease = neg_two.post(f"/api/rooms/{code}/control-lease", headers=target_headers, json={})
    assert lease.status_code == 200, lease.text
    started = neg_two.post(f"/api/rooms/{code}/speech/start", headers=target_headers, json={})
    assert started.status_code == 200, started.text


@pytest.mark.asyncio
async def test_all_human_free_turn_without_requests_keeps_rotating_after_bounded_turn(
    register_user,
) -> None:
    """No-AI free debate must exchange sides instead of becoming stuck."""

    owner = register_user("round44_free_owner")
    opponent = register_user("round44_free_opponent")
    code = create_training_room(owner, "Round44 双真人无人举手仍可轮转")["code"]
    claimed = opponent.post(f"/api/rooms/{code}/claim-seat", headers=csrf(opponent), json={"seat_key": "neg_1"})
    assert claimed.status_code == 200
    _ready(owner, code)
    _ready(opponent, code)
    _start(owner, code)
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.status = "running"
        room.current_stage_index = 0
        room.stage_started_at = now() - timedelta(seconds=10)
        room.stage_deadline_at = now() + timedelta(seconds=90)
        room.template_snapshot = [
            {
                "key": "round44_free",
                "name": "自由辩论",
                "kind": "free",
                "side": "aff",
                "turn_seq": 0,
                "turn_duration": 8,
                "duration": 90,
                "turn_started_at": (now() - timedelta(seconds=8)).isoformat(),
                "intermission_deadline_at": (now() - timedelta(milliseconds=1)).isoformat(),
                "intermission_side": "neg",
                "intermission_turn_seq": 1,
            }
        ]
        db.commit()

    # The expired application window resolves to the negative side. There is
    # no AI seat, so the engine must keep the human speaking button available
    # rather than entering an impossible AI-fallback state.
    await match_engine.process_room(code)
    with SessionLocal() as db:
        room = load_room(db, code)
        current = dict(stage(room))
        assert current["side"] == "neg"
        assert "force_ai_fallback" not in current
        opponent_projection = opponent.get(f"/api/rooms/{code}").json()["room"]
        assert opponent_projection["can_speak"] is True
        assert opponent_projection["speak_reason"] == "轮到你发言"
        current.pop("awaiting_human_start", None)
        current["turn_started_at"] = (now() - timedelta(seconds=9)).isoformat()
        room.stage_started_at = now() - timedelta(seconds=9)
        room.stage_deadline_at = now() + timedelta(seconds=90)
        snapshot = list(room.template_snapshot)
        snapshot[0] = current
        room.template_snapshot = snapshot
        db.commit()

    # The exhausted turn starts the next three-second intermission. This is
    # the observable proof that an all-human room does not hang forever.
    await match_engine.process_room(code)
    with SessionLocal() as db:
        room = load_room(db, code)
        current = stage(room)
        assert current.get("intermission_side") == "aff", (
            room.status,
            room.current_stage_index,
            current,
            free_turn_remaining_seconds(room, current),
        )
        assert "force_ai_fallback" not in current
        assert not db.scalar(select(MatchEvent.id).where(MatchEvent.room_id == room.id, MatchEvent.event_type == "speech.started"))


@pytest.mark.asyncio
async def test_disconnect_at_exact_sixty_seconds_pauses_and_preserves_every_human_seat(register_user) -> None:
    """At the exact boundary the whole match pauses; no identity is replaced."""

    owner = register_user("round44_disconnect_owner")
    opponent = register_user("round44_disconnect_opponent")
    code = create_training_room(owner, "Round44 断线六十秒边界")["code"]
    assert opponent.post(f"/api/rooms/{code}/claim-seat", headers=csrf(opponent), json={"seat_key": "neg_1"}).status_code == 200
    _ready(owner, code)
    _ready(opponent, code)
    _start(owner, code)
    owner_id = owner.get("/api/auth/session").json()["user"]["id"]
    opponent_id = opponent.get("/api/auth/session").json()["user"]["id"]
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.status = "running"
        room.stage_started_at = now()
        room.stage_deadline_at = now() + timedelta(minutes=5)
        room.template_snapshot = [{"key": "hold", "name": "等待恢复", "kind": "announcement", "duration": 300}]
        owner_seat = next(item for item in room.seats if item.user_id == owner_id)
        opponent_seat = next(item for item in room.seats if item.user_id == opponent_id)
        owner_seat.connected = False
        owner_seat.disconnected_at = now() - timedelta(seconds=60)
        opponent_seat.connected = True
        opponent_seat.disconnected_at = None
        db.commit()

    await match_engine.process_room(code)
    with SessionLocal() as db:
        room = load_room(db, code)
        owner_seat = next(item for item in room.seats if item.user_id == owner_id)
        opponent_seat = next(item for item in room.seats if item.user_id == opponent_id)
        assert room.status == "paused"
        assert room.stage_deadline_at is None
        assert owner_seat.occupant_type == "human"
        assert owner_seat.user_id == owner_id
        assert opponent_seat.occupant_type == "human"
        assert opponent_seat.connected is True
        assert room.owner_id == owner_id
        assert db.scalar(
            select(MatchEvent.id).where(
                MatchEvent.room_id == room.id,
                MatchEvent.event_type == "participant.disconnect_timeout",
            )
        )
        assert not db.scalar(
            select(MatchEvent.id).where(
                MatchEvent.room_id == room.id,
                MatchEvent.event_type == "seat.ai_substituted",
            )
        )
