from __future__ import annotations

import hashlib
import json

import pytest
from app.core.config import settings
from app.core.database import SessionLocal
from app.main import app
from app.models.entities import Competition, Match, Room, Speech
from app.services.room_service import append_event, now
from conftest import csrf
from fastapi.testclient import TestClient
from sqlalchemy import select
from starlette.websockets import WebSocketDisconnect


def _create_public_test_room(owner: TestClient) -> tuple[str, str]:
    response = owner.post(
        "/api/rooms",
        headers=csrf(owner),
        json={
            "competition_slug": "training-1v1",
            "custom_topic": "QA 数据不应出现在公开赛事入口",
            "seat_key": "aff_1",
            "visibility": "public",
        },
    )
    assert response.status_code == 200, response.text
    room_id = response.json()["room"]["id"]
    with SessionLocal.begin() as db:
        room = db.get(Room, room_id)
        assert room is not None
        room.is_test_data = True
        room.status = "running"
        room.current_stage_index = 0
        room.started_at = now()
    return response.json()["room"]["code"], room_id


def test_public_test_room_is_absent_from_catalog_and_restricted_on_every_read_surface(
    client: TestClient,
    register_user,
) -> None:
    owner = register_user("round14_qa_owner")
    outsider = register_user("round14_qa_outsider")
    before = client.get("/api/competitions/training-1v1")
    assert before.status_code == 200
    baseline_live_count = before.json()["competition"]["live_count"]

    code, room_id = _create_public_test_room(owner)

    catalog = client.get("/api/competitions")
    assert catalog.status_code == 200
    competition = next(item for item in catalog.json()["items"] if item["slug"] == "training-1v1")
    assert competition["live_count"] == baseline_live_count

    live = client.get("/api/live-rooms")
    assert live.status_code == 200
    assert code not in {item["code"] for item in live.json()["items"]}
    detail = client.get("/api/competitions/training-1v1")
    assert detail.status_code == 200
    assert detail.json()["competition"]["live_count"] == baseline_live_count
    assert code not in {item["code"] for item in detail.json()["live_rooms"]}

    # A public visibility flag must not expose synthetic student identity,
    # transcript state or later recordings once the room is classified as QA.
    assert client.get(f"/api/rooms/{code}").status_code == 401
    assert client.get(f"/api/rooms/{code}/public").status_code == 401
    assert outsider.get(f"/api/rooms/{code}").status_code == 403
    assert outsider.get(f"/api/rooms/{code}/public").status_code == 403
    assert owner.get(f"/api/rooms/{code}").status_code == 200

    with pytest.raises(WebSocketDisconnect) as anonymous_socket:
        with client.websocket_connect(f"/ws/rooms/{code}") as socket:
            socket.receive_json()
    assert anonymous_socket.value.code == 4401
    with pytest.raises(WebSocketDisconnect) as outsider_socket:
        with outsider.websocket_connect(f"/ws/rooms/{code}") as socket:
            socket.receive_json()
    assert outsider_socket.value.code == 4403
    with owner.websocket_connect(f"/ws/rooms/{code}") as socket:
        assert socket.receive_json()["room"]["code"] == code

    with SessionLocal.begin() as db:
        room = db.get(Room, room_id)
        assert room is not None
        room.status = "completed"
        room.completed_at = now()
        match = Match(
            room_id=room.id,
            competition_id=room.competition_id,
            season_id=room.season_id,
            status="completed",
            winner="aff",
            judge_snapshot={"system_prompt": "ADMIN_ONLY_JUDGE_PROMPT", "model": "internal-judge"},
            service_snapshot={
                "agent": {
                    "endpoint": "http://internal-agent.example/api/debate",
                    "secret_ciphertext": "NEVER_EXPORT_THIS_CIPHERTEXT",
                }
            },
        )
        db.add(match)
        db.flush()
        match_id = match.id
        speech = Speech(
            match_id=match.id,
            room_id=room.id,
            seat_key="aff_1",
            stage_key="qa-privacy",
            speaker_type="human",
            content="仅用于验证 QA 媒体访问控制",
            audio_url=f"/media/{code}/qa-access.wav",
            status="completed",
        )
        db.add(speech)
        append_event(
            db,
            room,
            "stage.started",
            {
                "stage": {"key": "qa-privacy", "name": "隐私测试阶段", "kind": "fixed", "duration": 60},
                "stage_index": 0,
                "task_id": "INTERNAL_TASK_ID",
                "control_lease": "INTERNAL_LEASE",
            },
            actor_user_id=room.owner_id,
            idempotency_key="INTERNAL_IDEMPOTENCY_KEY",
        )
        append_event(
            db,
            room,
            "provider.failed",
            {"provider": "agent", "message": "INTERNAL_PROVIDER_ERROR"},
        )

    media_file = settings.media_path / code / "qa-access.wav"
    media_file.parent.mkdir(parents=True, exist_ok=True)
    media_file.write_bytes(b"privacy-fixture")

    for endpoint in (
        f"/api/rooms/{code}/result",
        f"/api/matches/{match_id}/result",
    ):
        assert client.get(endpoint).status_code == 401
        assert outsider.get(endpoint).status_code == 403
        assert owner.get(endpoint).status_code == 200

    assert client.get(f"/api/matches/{match_id}/history").status_code == 401
    assert outsider.get(f"/api/matches/{match_id}/history").status_code == 403
    assert owner.get(f"/api/matches/{match_id}/history").status_code == 200
    assert client.get(f"/api/matches/{match_id}/archive").status_code == 401
    assert outsider.get(f"/api/matches/{match_id}/archive").status_code == 403
    owner_archive = owner.get(f"/api/matches/{match_id}/archive")
    assert owner_archive.status_code == 200
    assert owner_archive.headers["cache-control"] == "private, no-store"
    assert owner_archive.headers["x-archive-projection"] == "participant"
    assert owner_archive.headers["x-archive-sha256"] == hashlib.sha256(owner_archive.content).hexdigest()
    participant_document = owner_archive.json()
    assert participant_document["projection"] == "participant"
    assert participant_document["data"]["speeches"][0]["audio_url"] == f"/media/{code}/qa-access.wav"
    participant_text = json.dumps(participant_document, ensure_ascii=False)
    for sensitive in (
        "user_id",
        "actor_user_id",
        "agent_profile_id",
        "judge_snapshot",
        "service_snapshot",
        "ADMIN_ONLY_JUDGE_PROMPT",
        "internal-agent.example",
        "INTERNAL_TASK_ID",
        "INTERNAL_LEASE",
        "INTERNAL_IDEMPOTENCY_KEY",
        "INTERNAL_PROVIDER_ERROR",
        "NEVER_EXPORT_THIS_CIPHERTEXT",
    ):
        assert sensitive not in participant_text
    assert [item["type"] for item in participant_document["data"]["timeline"]] == ["stage.started"]

    with TestClient(app) as admin:
        login = admin.post("/api/auth/login", json={"account": "admin_test", "password": "Admin-test-1234"})
        assert login.status_code == 200
        research_archive = admin.get(f"/api/matches/{match_id}/archive")
        assert research_archive.status_code == 200
        assert research_archive.headers["x-archive-projection"] == "research"
        assert research_archive.headers["x-archive-sha256"] == hashlib.sha256(research_archive.content).hexdigest()
        research_text = research_archive.text
        assert "ADMIN_ONLY_JUDGE_PROMPT" in research_text
        assert "internal-agent.example" in research_text
        assert "INTERNAL_PROVIDER_ERROR" in research_text
        assert "NEVER_EXPORT_THIS_CIPHERTEXT" not in research_text
    assert client.get(f"/media/{code}/qa-access.wav").status_code == 401
    assert outsider.get(f"/media/{code}/qa-access.wav").status_code == 403
    owner_media = owner.get(f"/media/{code}/qa-access.wav")
    assert owner_media.status_code == 200
    assert owner_media.headers["cache-control"] == "private, no-store"


def test_rankings_do_not_reveal_an_unpublished_competition(client: TestClient) -> None:
    with SessionLocal.begin() as db:
        competition = db.scalar(select(Competition).where(Competition.slug == "training-1v1"))
        assert competition is not None
        competition.is_public = False
    try:
        response = client.get("/api/rankings?competition_slug=training-1v1")
        assert response.status_code == 404
        assert response.json()["detail"] == "赛事不存在。"
    finally:
        with SessionLocal.begin() as db:
            competition = db.scalar(select(Competition).where(Competition.slug == "training-1v1"))
            assert competition is not None
            competition.is_public = True


def test_reclassifying_a_live_room_as_test_disconnects_existing_anonymous_spectators(
    client: TestClient,
    register_user,
) -> None:
    owner = register_user("round14_reclassify_owner")
    created = owner.post(
        "/api/rooms",
        headers=csrf(owner),
        json={
            "competition_slug": "training-1v1",
            "custom_topic": "运行中的误分类数据必须立即退出公开面",
            "seat_key": "aff_1",
            "visibility": "public",
        },
    )
    assert created.status_code == 200
    code = created.json()["room"]["code"]
    with SessionLocal.begin() as db:
        room = db.scalar(select(Room).where(Room.code == code))
        assert room is not None
        room.status = "running"
        room.current_stage_index = 0
        room.started_at = now()

    with TestClient(app) as admin:
        login = admin.post("/api/auth/login", json={"account": "admin_test", "password": "Admin-test-1234"})
        assert login.status_code == 200
        with client.websocket_connect(f"/ws/rooms/{code}") as spectator:
            assert spectator.receive_json()["room"]["code"] == code
            classified = admin.patch(
                f"/api/admin/rooms/{code}/data-scope",
                headers=csrf(admin),
                json={"is_test_data": True},
            )
            assert classified.status_code == 200
            with pytest.raises(WebSocketDisconnect) as disconnected:
                spectator.receive_json()
            assert disconnected.value.code == 4401

    assert client.get(f"/api/rooms/{code}/public").status_code == 401
    assert owner.get(f"/api/rooms/{code}").status_code == 200


def test_marking_an_account_as_qa_immediately_disconnects_its_public_room_spectators(
    client: TestClient,
    register_user,
) -> None:
    owner = register_user("round14_account_reclassify_owner")
    owner_id = owner.get("/api/auth/session").json()["user"]["id"]
    created = owner.post(
        "/api/rooms",
        headers=csrf(owner),
        json={
            "competition_slug": "training-1v1",
            "custom_topic": "账号改标 QA 后公开连接必须立即失效",
            "seat_key": "aff_1",
            "visibility": "public",
        },
    )
    assert created.status_code == 200
    code = created.json()["room"]["code"]
    with SessionLocal.begin() as db:
        room = db.scalar(select(Room).where(Room.code == code))
        assert room is not None
        room.status = "running"
        room.current_stage_index = 0
        room.started_at = now()

    with TestClient(app) as admin:
        login = admin.post("/api/auth/login", json={"account": "admin_test", "password": "Admin-test-1234"})
        assert login.status_code == 200
        with client.websocket_connect(f"/ws/rooms/{code}") as spectator:
            assert spectator.receive_json()["room"]["code"] == code
            classified = admin.patch(
                f"/api/admin/users/{owner_id}",
                headers=csrf(admin),
                json={"is_test_account": True},
            )
            assert classified.status_code == 200
            with pytest.raises(WebSocketDisconnect) as disconnected:
                spectator.receive_json()
            assert disconnected.value.code == 4401

    assert client.get(f"/api/rooms/{code}/public").status_code == 401
    assert owner.get(f"/api/rooms/{code}").status_code == 200
