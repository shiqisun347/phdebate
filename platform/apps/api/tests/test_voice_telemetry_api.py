from __future__ import annotations

from datetime import timedelta

from app.api import rooms as rooms_api
from app.core.database import SessionLocal
from app.main import app
from app.models.entities import Match, Speech, VoiceTelemetry
from app.services import realtime as realtime_service
from app.services.room_service import load_room, now
from app.services.voice_telemetry import mark_voice_phase
from conftest import csrf
from fastapi.testclient import TestClient
from sqlalchemy import select


def _voice_fixture(owner: TestClient) -> tuple[str, str, str]:
    created = owner.post(
        "/api/rooms",
        headers=csrf(owner),
        json={
            "competition_slug": "training-1v1",
            "custom_topic": "实时语音链路应如何定位性能瓶颈？",
            "seat_key": "aff_1",
            "visibility": "public",
        },
    ).json()["room"]
    generation = "a" * 32
    with SessionLocal() as db:
        room = load_room(db, created["code"], lock=True)
        room.status = "running"
        match = Match(
            room_id=room.id,
            competition_id=room.competition_id,
            season_id=room.season_id,
            status="running",
        )
        db.add(match)
        db.flush()
        speech = Speech(
            match_id=match.id,
            room_id=room.id,
            seat_key="neg_1",
            stage_key="neg_case",
            speaker_type="ai",
            status="playing",
            stream_generation=generation,
        )
        db.add(speech)
        db.flush()
        start = now() - timedelta(seconds=2)
        mark_voice_phase(db, room, speech, "speech_created", occurred_at=start)
        mark_voice_phase(db, room, speech, "request_start", occurred_at=start)
        mark_voice_phase(
            db,
            room,
            speech,
            "livekit_first_capture",
            occurred_at=start + timedelta(seconds=1),
            generation=generation,
        )
        db.commit()
        return room.code, speech.id, generation


def test_browser_voice_telemetry_requires_csrf_and_is_generation_bound(register_user) -> None:
    owner = register_user("voice_telemetry_owner")
    code, speech_id, generation = _voice_fixture(owner)
    payload = {
        "speech_id": speech_id,
        "generation": generation,
        "event": "browser_first_audible",
        "metrics": {"rms": 0.1, "peak": 0.3},
    }
    assert owner.post(f"/api/rooms/{code}/voice-telemetry", json=payload).status_code == 403
    accepted = owner.post(f"/api/rooms/{code}/voice-telemetry", headers=csrf(owner), json=payload)
    assert accepted.status_code == 202
    stale = owner.post(
        f"/api/rooms/{code}/voice-telemetry",
        headers=csrf(owner),
        json={**payload, "generation": "b" * 32},
    )
    assert stale.status_code == 409
    with SessionLocal() as db:
        item = db.scalar(select(VoiceTelemetry).where(VoiceTelemetry.speech_id == speech_id))
        assert item is not None
        assert "browser_first_audible" in item.phases
        # Browser-only values outside the network allowlist are never retained.
        assert "rms" not in item.network_summary


def test_admitted_anonymous_spectator_can_report_without_reading_transcript(
    client: TestClient,
    register_user,
    monkeypatch,
) -> None:
    owner = register_user("voice_telemetry_spectator")
    code, speech_id, generation = _voice_fixture(owner)
    ticket = realtime_service.new_spectator_ticket()
    client.cookies.set(realtime_service.SPECTATOR_TICKET_COOKIE, ticket)

    async def authorized(_code: str, *, connection_id: str) -> bool:
        assert connection_id == realtime_service.spectator_connection_id(code, ticket)
        return True

    monkeypatch.setattr(rooms_api.room_hub, "spectator_authorized", authorized)
    response = client.post(
        f"/api/rooms/{code}/voice-telemetry",
        json={
            "speech_id": speech_id,
            "generation": generation,
            "event": "browser_network_sample",
            "metrics": {
                "jitter_ms": 18,
                "packets_lost": 1,
                # Legacy clients reported the cumulative WebRTC delay under
                # this established field. The server must normalize it.
                "jitter_buffer_delay_ms": 2_000,
                "jitter_buffer_emitted_count": 100,
                "transcript": 999,
            },
        },
    )
    assert response.status_code == 202
    with SessionLocal() as db:
        item = db.scalar(select(VoiceTelemetry).where(VoiceTelemetry.speech_id == speech_id))
        assert item.network_summary["last"] == {
            "packets_lost": 1.0,
            "jitter_ms": 18.0,
            "jitter_buffer_delay_ms": 20.0,
            "jitter_buffer_delay_avg_ms": 20.0,
            "jitter_buffer_delay_current_ms": 20.0,
            "jitter_buffer_delay_total_ms": 2_000.0,
            "jitter_buffer_emitted_count": 100.0,
        }


def test_only_admin_can_read_safe_voice_summary(register_user) -> None:
    owner = register_user("voice_telemetry_admin_view")
    code, speech_id, generation = _voice_fixture(owner)
    assert owner.get(f"/api/admin/rooms/{code}/voice-telemetry").status_code == 403
    with TestClient(app) as admin:
        assert admin.post(
            "/api/auth/login",
            json={"account": "admin_test", "password": "Admin-test-1234"},
        ).status_code == 200
        response = admin.get(f"/api/admin/rooms/{code}/voice-telemetry")
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["speech_id"] == speech_id and item["generation"] == generation
    assert item["latency_ms"]["first_pcm_to_livekit_capture"] is None
    assert "content" not in item and "audio_url" not in item
