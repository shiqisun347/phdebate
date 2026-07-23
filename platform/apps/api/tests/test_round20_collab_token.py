from __future__ import annotations

from datetime import datetime, timezone

import pytest
from app.core.database import SessionLocal
from app.models.entities import Match, Room, Speech
from app.services.room_service import append_event, load_room
from app.services.transcript_collab import verify_transcript_collab_token
from conftest import csrf
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_platform import create_training_room


def _admin(client: TestClient) -> TestClient:
    admin = TestClient(client.app)
    admin.__enter__()
    response = admin.post("/api/auth/login", json={"account": "admin_test", "password": "Admin-test-1234"})
    assert response.status_code == 200
    return admin


def _completed_speech(db, room: Room, match: Match, seat_key: str, user_id: str, content: str) -> str:
    speech = Speech(
        match_id=match.id,
        room_id=room.id,
        seat_key=seat_key,
        stage_key=f"stage_{seat_key}",
        speaker_type="human",
        status="completed",
        content=content,
    )
    db.add(speech)
    db.flush()
    append_event(
        db,
        room,
        "speech.completed",
        {"speech_id": speech.id, "seat_key": seat_key, "content": content},
        actor_user_id=user_id,
    )
    return speech.id


def test_collab_token_scopes_editable_speeches_and_survives_seat_transfer(client, register_user, monkeypatch) -> None:
    monkeypatch.setenv("TRANSCRIPT_COLLAB_HMAC_SECRET", "round20-collab-secret-that-is-at-least-32-bytes")
    owner = register_user("collab_owner")
    participant = register_user("collab_participant")
    replacement = register_user("collab_replacement")
    owner_id = owner.get("/api/auth/session").json()["user"]["id"]
    participant_id = participant.get("/api/auth/session").json()["user"]["id"]
    replacement_id = replacement.get("/api/auth/session").json()["user"]["id"]
    code = create_training_room(owner, "Round20 协同文字稿权限")["code"]
    assert participant.post(
        f"/api/rooms/{code}/claim-seat",
        headers=csrf(participant),
        json={"seat_key": "neg_1"},
    ).status_code == 200
    for browser in (owner, participant):
        assert browser.post(f"/api/rooms/{code}/ready", headers=csrf(browser), json={"ready": True}).status_code == 200
    assert owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={}).status_code == 200

    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        owner_speech = _completed_speech(db, room, match, "aff_1", owner_id, "房主完成的真人发言。")
        participant_speech = _completed_speech(db, room, match, "neg_1", participant_id, "原参赛者完成的真人发言。")
        ai_speech = Speech(
            match_id=match.id,
            room_id=room.id,
            seat_key="neg_1",
            stage_key="ai_stage",
            speaker_type="ai",
            status="completed",
            content="AI 发言不能由普通协同文字稿接口编辑。",
        )
        db.add(ai_speech)
        seat = next(item for item in room.seats if item.seat_key == "neg_1")
        seat.user_id = replacement_id
        seat.display_name = "后续席位接替者"
        db.commit()
        room_id = room.id
        ai_speech_id = ai_speech.id

    owner_token = owner.post(f"/api/rooms/{code}/transcript-collab-token", headers=csrf(owner))
    participant_token = participant.post(f"/api/rooms/{code}/transcript-collab-token", headers=csrf(participant))
    replacement_token = replacement.post(f"/api/rooms/{code}/transcript-collab-token", headers=csrf(replacement))
    assert owner_token.status_code == participant_token.status_code == replacement_token.status_code == 200
    assert owner_token.headers["cache-control"] == "private, no-store"
    assert owner_token.json()["document_name"] == f"room:{room_id}"
    assert owner_token.json()["role"] == "participant"
    assert owner_token.json()["editable_speech_ids"] == [owner_speech]
    assert participant_token.json()["editable_speech_ids"] == [participant_speech]
    assert replacement_token.json()["role"] == "viewer"
    assert replacement_token.json()["editable_speech_ids"] == []
    owner_claims = verify_transcript_collab_token(owner_token.json()["token"], f"room:{room_id}")
    participant_claims = verify_transcript_collab_token(participant_token.json()["token"], f"room:{room_id}")
    replacement_claims = verify_transcript_collab_token(replacement_token.json()["token"], f"room:{room_id}")
    assert owner_claims["editable_speech_ids"] == [owner_speech]
    assert participant_claims["editable_speech_ids"] == [participant_speech]
    assert replacement_claims["editable_speech_ids"] == [] and replacement_claims["role"] == "viewer"
    assert ai_speech_id not in owner_claims["editable_speech_ids"] + participant_claims["editable_speech_ids"]

    admin = _admin(client)
    try:
        response = admin.post(f"/api/rooms/{code}/transcript-collab-token", headers=csrf(admin))
        assert response.status_code == 200
        admin_claims = verify_transcript_collab_token(response.json()["token"], f"room:{room_id}")
        assert set(admin_claims["editable_speech_ids"]) == {owner_speech, participant_speech}
        assert admin_claims["role"] == "admin"
        assert response.json()["role"] == "admin"
        assert set(response.json()["editable_speech_ids"]) == {owner_speech, participant_speech}
    finally:
        admin.__exit__(None, None, None)


def test_collab_token_requires_csrf_room_access_and_short_expiry(register_user, monkeypatch) -> None:
    monkeypatch.setenv("TRANSCRIPT_COLLAB_HMAC_SECRET", "round20-collab-secret-that-is-at-least-32-bytes")
    monkeypatch.setenv("TRANSCRIPT_COLLAB_TOKEN_TTL_SECONDS", "300")
    owner = register_user("collab_private_owner")
    outsider = register_user("collab_private_outsider")
    created = owner.post(
        "/api/rooms",
        headers=csrf(owner),
        json={
            "competition_slug": "training-1v1",
            "custom_topic": "私密协同房间",
            "seat_key": "aff_1",
            "visibility": "private",
        },
    )
    assert created.status_code == 200
    code = created.json()["room"]["code"]
    assert owner.post(f"/api/rooms/{code}/transcript-collab-token").status_code == 403
    assert outsider.post(f"/api/rooms/{code}/transcript-collab-token", headers=csrf(outsider)).status_code == 403
    response = owner.post(f"/api/rooms/{code}/transcript-collab-token", headers=csrf(owner))
    assert response.status_code == 200
    payload = response.json()
    claims = verify_transcript_collab_token(payload["token"], payload["document_name"])
    now_seconds = int(datetime.now(timezone.utc).timestamp())
    assert 0 < claims["exp"] - now_seconds <= 300
    with pytest.raises(ValueError):
        verify_transcript_collab_token(payload["token"], "room:another-room")
    with pytest.raises(ValueError):
        verify_transcript_collab_token(payload["token"], payload["document_name"], now_seconds=claims["exp"])


def test_logged_in_public_spectator_cannot_obtain_transcript_collaboration_token(
    register_user,
    monkeypatch,
) -> None:
    owner = register_user("round20_collab_public_owner")
    spectator = register_user("round20_collab_public_spectator")
    created = owner.post(
        "/api/rooms",
        headers=csrf(owner),
        json={
            "competition_slug": "training-1v1",
            "custom_topic": "观众不可查看协同文字稿",
            "seat_key": "aff_1",
            "visibility": "public",
        },
    )
    assert created.status_code == 200
    code = created.json()["room"]["code"]
    assert owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True}).status_code == 200
    assert owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={}).status_code == 200
    monkeypatch.setenv("TRANSCRIPT_COLLAB_HMAC_SECRET", "round20-collab-public-secret-at-least-32")

    public_room = spectator.get(f"/api/rooms/{code}")
    assert public_room.status_code == 200
    assert public_room.json()["room"]["my_seat"] is None
    assert public_room.json()["room"]["can_control"] is False

    denied = spectator.post(f"/api/rooms/{code}/transcript-collab-token", headers=csrf(spectator))
    assert denied.status_code == 403
    assert denied.json()["detail"] == "观众不可查看文字稿。"
