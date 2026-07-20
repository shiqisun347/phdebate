from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import time
import uuid
import wave
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Barrier, Event
from unittest.mock import AsyncMock, Mock

import pytest
from app.api import auth as auth_api
from app.api import realtime as realtime_api
from app.api import rooms as rooms_api
from app.api.realtime import persist_asr_final
from app.api.rooms import upload_speech_audio
from app.core import database as database_module
from app.core.config import settings
from app.core.database import SessionLocal, engine
from app.core.security import expires_in_days, token_hash, verify_password
from app.models.entities import (
    AdminAuditLog,
    AgentProfile,
    AudioAsset,
    AudioCue,
    AutomationTemplate,
    Competition,
    CompetitionTopic,
    JudgeProfile,
    JudgeScorecard,
    LeaderboardEntry,
    Match,
    MatchEvent,
    ProviderConfig,
    RatingChange,
    Room,
    RoomCodeReservation,
    RoomSeat,
    SeatRestoreRequest,
    Speech,
    TranscriptSegment,
    User,
    UserSession,
)
from app.schemas.requests import RegisterRequest
from app.services import media_storage
from app.services import realtime as realtime_service
from app.services import room_service as room_service_service
from app.services import system_health as system_health_service
from app.services.match_engine import match_engine, recover_inflight_engine_tasks
from app.services.providers import ProviderError, debate_agent, judge_provider, lighttts
from app.services.public_snapshot import public_snapshot_cache
from app.services.realtime import RoomHub
from app.services.room_service import append_event, leaderboard, load_room, now, reset_connected_presence
from app.services.seed import seed_database
from conftest import csrf
from fastapi import HTTPException, Request, Response
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import IntegrityError
from starlette.formparsers import MultiPartParser
from starlette.websockets import WebSocketDisconnect


def create_training_room(client: TestClient, topic: str = "技术进步是否让人更自由？") -> dict:
    response = client.post(
        "/api/rooms",
        headers=csrf(client),
        json={
            "competition_slug": "training-1v1",
            "custom_topic": topic,
            "seat_key": "aff_1",
            "visibility": "public",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["room"]


def test_public_catalog_and_registration(client: TestClient, register_user) -> None:
    select_statements: list[str] = []

    def track_selects(_connection, _cursor, statement, _parameters, _context, _executemany) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            select_statements.append(statement)

    event.listen(engine, "before_cursor_execute", track_selects)
    try:
        catalog = client.get("/api/competitions")
    finally:
        event.remove(engine, "before_cursor_execute", track_selects)
    assert catalog.status_code == 200
    assert len(select_statements) <= 2
    assert {item["slug"] for item in catalog.json()["items"]} == {"daily-4v4", "training-1v1"}
    anonymous_session = client.get("/api/auth/session-state")
    assert anonymous_session.status_code == 200
    assert anonymous_session.json() == {"user": None}

    user = register_user("catalog")
    session = user.get("/api/auth/session")
    assert session.status_code == 200
    assert session.json()["user"]["real_name"].startswith("测试选手")
    session_state = user.get("/api/auth/session-state")
    assert session_state.status_code == 200
    assert session_state.json()["user"]["id"] == session.json()["user"]["id"]


def test_public_catalog_aggregates_live_room_counts_and_room_metadata(client: TestClient, register_user) -> None:
    first = register_user("catalog_live_first")
    second = register_user("catalog_live_second")
    third = register_user("catalog_live_third")
    fourth = register_user("catalog_live_fourth")
    first_room = create_training_room(first, "公开目录统计测试甲")
    second_room = create_training_room(second, "公开目录统计测试乙")
    third_room = create_training_room(third, "公开目录过期暂停测试")
    fourth_room = create_training_room(fourth, "公开目录私密房间测试")
    baseline_catalog = client.get("/api/competitions")
    assert baseline_catalog.status_code == 200
    baseline_by_slug = {item["slug"]: item for item in baseline_catalog.json()["items"]}
    baseline_live_count = baseline_by_slug["training-1v1"]["live_count"]

    with SessionLocal() as db:
        rooms = list(
            db.scalars(
                select(Room).where(
                    Room.code.in_([first_room["code"], second_room["code"], third_room["code"], fourth_room["code"]])
                )
            ).all()
        )
        assert len(rooms) == 4
        by_code = {room.code: room for room in rooms}
        for room in rooms:
            room.current_stage_index = 0
        by_code[first_room["code"]].status = "running"
        by_code[second_room["code"]].status = "paused"
        append_event(db, by_code[second_room["code"]], "control.pause", {"reason": "recent pause"})
        by_code[third_room["code"]].status = "paused"
        stale_pause = append_event(db, by_code[third_room["code"]], "control.pause", {"reason": "stale pause"})
        stale_pause.created_at = now() - timedelta(hours=2)
        by_code[fourth_room["code"]].status = "running"
        by_code[fourth_room["code"]].visibility = "private"
        db.commit()

    catalog = client.get("/api/competitions")
    assert catalog.status_code == 200
    by_slug = {item["slug"]: item for item in catalog.json()["items"]}
    assert by_slug["training-1v1"]["live_count"] == baseline_live_count + 2

    live = client.get("/api/live-rooms")
    assert live.status_code == 200
    indexed = {item["code"]: item for item in live.json()["items"]}
    assert indexed[first_room["code"]]["competition_name"] == "1v1 辩论训练赛"
    assert indexed[first_room["code"]]["stage"]
    assert indexed[second_room["code"]]["paused_at"]
    assert third_room["code"] not in indexed
    assert fourth_room["code"] not in indexed

    detail = client.get("/api/competitions/training-1v1")
    assert detail.status_code == 200
    detail_codes = {item["code"] for item in detail.json()["live_rooms"]}
    assert first_room["code"] in detail_codes
    assert second_room["code"] in detail_codes
    assert third_room["code"] not in detail_codes
    assert fourth_room["code"] not in detail_codes
    assert detail.json()["competition"]["live_count"] == baseline_live_count + 2


def test_room_projection_labels_completed_caption_as_previous_speech(client: TestClient, register_user) -> None:
    owner = register_user("previous_caption_owner")
    room_data = create_training_room(owner, "历史字幕与当前阶段区分测试")
    code = room_data["code"]
    owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})
    started = owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    assert started.status_code == 200, started.text

    with SessionLocal() as db:
        room = load_room(db, code)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        assert match is not None
        db.add(
            Speech(
                match_id=match.id,
                room_id=room.id,
                seat_key="aff_1",
                stage_key="aff_case",
                speaker_type="ai",
                status="completed",
                content="这是已经完成的上一段发言。",
            )
        )
        append_event(db, room, "presence.connected", {"seat_key": "aff_1"})
        append_event(db, room, "seat.control_acquired", {"seat_key": "aff_1", "lease_fingerprint": "private"})
        append_event(db, room, "stage.started", {"stage": {"key": "neg_case", "name": "反方立论"}})
        db.commit()

    projection = client.get(f"/api/rooms/{code}/public")
    assert projection.status_code == 200
    speech = projection.json()["room"]["speeches"][-1]
    assert speech["content"] == "这是已经完成的上一段发言。"
    assert speech["speaker"] == "上一段发言 · 正方1辩"
    projected_event_types = {item["type"] for item in projection.json()["room"]["recent_events"]}
    assert "stage.started" in projected_event_types
    assert "presence.connected" not in projected_event_types
    assert "seat.control_acquired" not in projected_event_types

    anonymous_result = client.get(f"/api/rooms/{code}/result")
    owner_result = owner.get(f"/api/rooms/{code}/result")
    assert anonymous_result.status_code == 409
    assert anonymous_result.json()["detail"] == "比赛尚未结束，请前往观战页面查看实时内容。"
    assert owner_result.status_code == 200
    owner_types = {item["type"] for item in owner_result.json()["events"]}
    assert {"presence.connected", "seat.control_acquired"}.issubset(owner_types)


def test_runtime_exposes_only_competition_scoped_product_domains(client: TestClient, register_user) -> None:
    user = register_user("competition_only")
    session_user = user.get("/api/auth/session").json()["user"]
    assert "teacher_access" not in session_user

    room = create_training_room(user)
    assert "recording_consent" not in room

    retired_routes = (
        "/api/teacher/classrooms",
        "/api/consents/organizations",
        "/api/consents/me/classrooms",
        "/api/research/exports",
        "/api/admin/classrooms",
        "/api/admin/qa/provision",
    )
    for route in retired_routes:
        assert user.get(route).status_code == 404

    retired_tables = {
        "organizations",
        "organization_memberships",
        "classrooms",
        "classroom_memberships",
        "activities",
        "activity_participants",
        "activity_operations",
        "consent_policies",
        "consent_records",
        "research_subject_identities",
        "research_export_jobs",
    }
    assert not (retired_tables & set(database_module.Base.metadata.tables))

    user.post(f"/api/rooms/{room['code']}/cancel", headers=csrf(user), json={})


def test_room_topic_and_admin_text_inputs_are_normalized(client: TestClient, register_user) -> None:
    owner = register_user("normalized_topic")
    normalized = owner.post(
        "/api/rooms",
        headers=csrf(owner),
        json={
            "competition_slug": "training-1v1",
            "custom_topic": "  人工智能   是否提升创造力？  ",
            "seat_key": "aff_1",
            "visibility": "public",
        },
    )
    assert normalized.status_code == 200
    assert normalized.json()["room"]["topic"] == "人工智能 是否提升创造力？"
    code = normalized.json()["room"]["code"]
    owner.post(f"/api/rooms/{code}/cancel", headers=csrf(owner), json={})

    fallback = owner.post(
        "/api/rooms",
        headers=csrf(owner),
        json={
            "competition_slug": "training-1v1",
            "custom_topic": "   \n  ",
            "seat_key": "aff_1",
            "visibility": "public",
        },
    )
    assert fallback.status_code == 200 and len(fallback.json()["room"]["topic"]) >= 4
    owner.post(f"/api/rooms/{fallback.json()['room']['code']}/cancel", headers=csrf(owner), json={})
    assert (
        owner.post(
            "/api/rooms",
            headers=csrf(owner),
            json={
                "competition_slug": "training-1v1",
                "custom_topic": "太短",
                "seat_key": "aff_1",
                "visibility": "public",
            },
        ).status_code
        == 422
    )
    assert (
        owner.post(
            "/api/rooms",
            headers=csrf(owner),
            json={
                "competition_slug": "x" * 81,
                "custom_topic": "足够长的测试辩题",
                "seat_key": "aff_1",
                "visibility": "public",
            },
        ).status_code
        == 422
    )

    admin = TestClient(client.app)
    with admin:
        admin.post("/api/auth/login", json={"account": "admin_test", "password": "Admin-test-1234"})
        competition_id = client.get("/api/competitions/training-1v1").json()["competition"]["id"]
        assert (
            admin.post(
                f"/api/admin/competitions/{competition_id}/topics",
                headers=csrf(admin),
                json={"title": "    "},
            ).status_code
            == 422
        )
        assert (
            admin.post(
                "/api/admin/reviews/not-found/approve",
                headers=csrf(admin),
                json={"winner": "draw", "affirmative_score": 80, "negative_score": 80, "reasoning": "   "},
            ).status_code
            == 422
        )


def test_seeded_competition_topics_are_idempotent(client: TestClient) -> None:
    expected = {
        "daily-4v4": {
            "人工智能时代，还要不要学编程？",
            "AI 的迅猛发展提升了还是降低了人类创作者存在的意义？",
            "信息爆炸时代，深度思考是否正在变得更稀缺？",
        },
        "training-1v1": {
            "技术进步是否让人更自由？",
            "短视频正在提升还是削弱公众表达能力？",
            "年轻人应优先选择热爱还是稳定？",
        },
    }
    with SessionLocal() as db:
        seed_database(db)
        seed_database(db)
        for slug, titles in expected.items():
            competition = db.scalar(select(Competition).where(Competition.slug == slug))
            rows = list(db.scalars(select(CompetitionTopic.title).where(CompetitionTopic.competition_id == competition.id)).all())
            assert all(rows.count(title) == 1 for title in titles)
            if slug == "daily-4v4":
                assert competition.name == "4v4 人机辩论正式赛"
        assert db.scalar(select(func.count(ProviderConfig.id)).where(ProviderConfig.kind == "funasr")) == 1
        assert db.scalar(select(func.count(ProviderConfig.id)).where(ProviderConfig.kind == "lighttts")) == 1


def test_legacy_competition_is_immutable_through_admin_api(client: TestClient) -> None:
    with SessionLocal() as db:
        source = db.scalar(select(Competition).where(Competition.slug == "daily-4v4"))
        legacy_template = AutomationTemplate(
            slug=f"legacy-import-{uuid.uuid4().hex[:8]}",
            name="旧系统只读流程",
            version=1,
            stages=[],
            is_active=False,
        )
        db.add(legacy_template)
        db.flush()
        legacy = Competition(
            slug=f"legacy-read-only-{uuid.uuid4().hex[:8]}",
            name="旧系统历史比赛",
            tagline="只读迁移记录",
            description="历史归档",
            rules="旧规则",
            format="legacy",
            seat_count=8,
            ranked=False,
            allow_custom_topic=False,
            is_public=False,
            is_active=False,
            automation_template_id=legacy_template.id,
        )
        db.add(legacy)
        db.flush()
        topic = CompetitionTopic(competition_id=legacy.id, title="历史辩题")
        db.add(topic)
        db.commit()
        legacy_id = legacy.id
        topic_id = topic.id
        legacy_template_id = legacy_template.id

    admin = TestClient(client.app)
    try:
        with admin:
            assert admin.post("/api/auth/login", json={"account": "admin_test", "password": "Admin-test-1234"}).status_code == 200
            requests = (
                admin.patch(
                    f"/api/admin/competitions/{legacy_id}",
                    headers=csrf(admin),
                    json={"is_active": True},
                ),
                admin.post(
                    f"/api/admin/competitions/{legacy_id}/topics",
                    headers=csrf(admin),
                    json={"title": "不能添加的新辩题"},
                ),
                admin.patch(
                    f"/api/admin/competitions/{legacy_id}/topics/{topic_id}",
                    headers=csrf(admin),
                    json={"is_active": False},
                ),
                admin.post(
                    f"/api/admin/automation-templates/{legacy_template_id}/versions",
                    headers=csrf(admin),
                    json={
                        "name": "不应创建的新版本",
                        "stages": [
                            {"key": "aff", "name": "正方发言", "kind": "speech", "duration": 60, "seat": "aff_1"},
                            {"key": "judge", "name": "裁判", "kind": "judging", "duration": 30},
                        ],
                        "competition_ids": [source.id],
                    },
                ),
            )
            for response in requests:
                assert response.status_code == 409
                assert "只读归档" in response.json()["detail"]
    finally:
        with SessionLocal() as db:
            db.query(CompetitionTopic).filter(CompetitionTopic.competition_id == legacy_id).delete()
            db.query(Competition).filter(Competition.id == legacy_id).delete()
            db.query(AutomationTemplate).filter(AutomationTemplate.id == legacy_template_id).delete()
            db.commit()


def test_issued_room_codes_remain_reserved_after_the_room_record_is_absent(client: TestClient, monkeypatch) -> None:
    with SessionLocal() as db:
        existing = set(db.scalars(select(RoomCodeReservation.code)).all()) | set(db.scalars(select(Room.code)).all())
        available = [code for number in range(100000, 100100) if (code := f"{number:06d}") not in existing]
        assert len(available) >= 2
        first_code, second_code = available[:2]
        values = iter([int(first_code) - 100000, int(first_code) - 100000, int(second_code) - 100000])
        monkeypatch.setattr(room_service_service.secrets, "randbelow", lambda _limit: next(values))
        assert room_service_service.room_code(db) == first_code
        db.commit()

    with SessionLocal() as db:
        assert db.get(RoomCodeReservation, first_code)
        assert not db.scalar(select(Room.id).where(Room.code == first_code))
        assert room_service_service.room_code(db) == second_code
        db.commit()
        assert db.get(RoomCodeReservation, second_code)


def test_security_headers_and_cors_boundaries(client: TestClient, monkeypatch) -> None:
    response = client.get("/api/health")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert "microphone=(self)" in response.headers["permissions-policy"]
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert "strict-transport-security" not in response.headers

    monkeypatch.setattr(settings, "app_env", "production")
    secure = client.get("/api/health", headers={"X-Forwarded-Proto": "https"})
    assert secure.headers["strict-transport-security"].startswith("max-age=31536000")

    allowed = client.options(
        "/api/health",
        headers={"Origin": "http://localhost:3200", "Access-Control-Request-Method": "GET"},
    )
    blocked = client.options(
        "/api/health",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"},
    )
    assert allowed.headers.get("access-control-allow-origin") == "http://localhost:3200"
    assert blocked.headers.get("access-control-allow-origin") is None


def test_readiness_checks_schema_redis_engine_backup_and_storage(client: TestClient, monkeypatch, tmp_path) -> None:
    class FakeRedis:
        dead_letters = 0

        async def ping(self) -> bool:
            return True

        async def get(self, key: str) -> str:
            assert key == "jixia:heartbeat:engine"
            return str(time.time())

        async def zrange(self, key: str, start: int, end: int, *, withscores: bool = False):
            assert key == "dramatiq:__heartbeats__" and start == 0 and end == -1 and withscores is True
            return [("worker-test", time.time() * 1000)]

        async def zcard(self, key: str) -> int:
            assert key == "dramatiq:default.XQ"
            return self.dead_letters

        async def hlen(self, key: str) -> int:
            assert key == "dramatiq:default.XQ.msgs"
            return self.dead_letters

        async def llen(self, key: str) -> int:
            assert key == "dramatiq:default"
            return 0

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "backup_status_file", str(tmp_path / "backup-status.json"))
    monkeypatch.setattr(settings, "health_min_disk_free_percent", 0.0)
    monkeypatch.setattr(system_health_service, "expected_schema_revision", lambda: "test-head")
    monkeypatch.setattr(system_health_service.redis, "from_url", lambda *args, **kwargs: FakeRedis())
    monkeypatch.setattr(
        system_health_service,
        "_provider_checks",
        lambda **_kwargs: asyncio.sleep(
            0,
            result={"lighttts": {"ok": True}, "funasr": {"ok": True}},
        ),
    )
    settings.backup_status_path.write_text(
        json.dumps({"last_success_at": now().isoformat(), "bytes": 12345}),
        encoding="utf-8",
    )
    with SessionLocal() as db:
        db.execute(text("create table if not exists alembic_version (version_num varchar(64) not null)"))
        db.execute(text("delete from alembic_version"))
        db.execute(text("insert into alembic_version(version_num) values ('test-head')"))
        db.commit()

    ready = client.get("/api/health/ready")
    assert ready.status_code == 200
    payload = ready.json()
    assert payload["ok"] is True
    assert all(item["ok"] for item in payload["checks"].values())
    assert payload["checks"]["engine"]["age_seconds"] <= 1
    assert payload["checks"]["worker"]["workers"] == 1
    assert payload["checks"]["worker"]["dead_letters"] == 0

    settings.backup_status_path.write_text(
        json.dumps({"last_success_at": (now() - timedelta(hours=48)).isoformat(), "bytes": 12345}),
        encoding="utf-8",
    )
    stale = client.get("/api/health/ready")
    assert stale.status_code == 503
    assert stale.json()["checks"]["backup"]["ok"] is False

    settings.backup_status_path.write_text(
        json.dumps({"last_success_at": now().isoformat(), "bytes": 12345}),
        encoding="utf-8",
    )
    FakeRedis.dead_letters = 2
    dead_letters = client.get("/api/health/ready")
    assert dead_letters.status_code == 503
    assert dead_letters.json()["checks"]["worker"]["dead_letters"] == 2


def test_production_startup_never_bypasses_alembic_with_create_all(monkeypatch) -> None:
    create_all = Mock()
    monkeypatch.setattr(database_module.Base.metadata, "create_all", create_all)
    monkeypatch.setattr(settings, "app_env", "production")
    database_module.create_schema()
    create_all.assert_not_called()
    monkeypatch.setattr(settings, "app_env", "development")
    database_module.create_schema()
    create_all.assert_called_once_with(bind=database_module.engine)


def test_unknown_account_login_still_runs_argon2_verification(client: TestClient, monkeypatch) -> None:
    checked_hashes: list[str] = []

    def reject(password_hash: str, password: str) -> bool:
        checked_hashes.append(password_hash)
        return False

    monkeypatch.setattr(auth_api, "verify_password", reject)
    response = client.post("/api/auth/login", json={"account": "definitely_missing_account", "password": "Password-1234"})
    assert response.status_code == 401
    assert checked_hashes == [auth_api.DUMMY_PASSWORD_HASH]
    assert verify_password("not-an-argon2-hash", "Password-1234") is False


def test_login_applies_both_source_and_account_rate_limits(client: TestClient, monkeypatch) -> None:
    guarded: list[tuple[str, int]] = []

    def record_guard(key: str, *, limit: int = 12) -> None:
        guarded.append((key, limit))

    monkeypatch.setattr(auth_api, "_guard", record_guard)
    response = client.post("/api/auth/login", json={"account": "  Missing_Account  ", "password": "Password-1234"})
    assert response.status_code == 401
    assert guarded == [
        ("login-ip:testclient", auth_api.LOGIN_IP_LIMIT),
        ("login:testclient:missing_account", 12),
    ]


def test_login_rate_limit_normalizes_whitespace_and_rejects_oversized_input(client: TestClient) -> None:
    normalized = "missing_rate_limit_account"
    key = f"login:testclient:{normalized}"
    source_key = "login-ip:testclient"
    auth_api._clear_attempts(key)
    auth_api._clear_attempts(source_key)
    for index in range(12):
        account = f"{' ' * (index % 3)}{normalized}{' ' * ((index + 1) % 3)}"
        assert client.post("/api/auth/login", json={"account": account, "password": "Wrong-password-1234"}).status_code == 401
    assert client.post("/api/auth/login", json={"account": f"  {normalized}  ", "password": "Wrong-password-1234"}).status_code == 429
    auth_api._clear_attempts(key)
    auth_api._clear_attempts(source_key)
    assert client.post("/api/auth/login", json={"account": "a" * 129, "password": "Password-1234"}).status_code == 422
    assert client.post("/api/auth/login", json={"account": "valid_account", "password": "p" * 129}).status_code == 422


def test_rate_limit_registry_has_a_hard_key_bound(monkeypatch) -> None:
    auth_api.attempts.clear()
    monkeypatch.setattr(auth_api, "MAX_ATTEMPT_KEYS", 3)
    for index in range(3):
        auth_api._guard(f"bounded:{index}")
    with pytest.raises(HTTPException) as limited:
        auth_api._guard("bounded:overflow")
    assert limited.value.status_code == 429
    assert len(auth_api.attempts) == 3
    auth_api.attempts.clear()


def test_registration_rate_limit_allows_event_sized_burst() -> None:
    key = "register:shared-event-ip"
    auth_api._clear_attempts(key)
    for _ in range(60):
        auth_api._guard(key, limit=60)
    with pytest.raises(HTTPException) as limited:
        auth_api._guard(key, limit=60)
    assert limited.value.status_code == 429
    auth_api._clear_attempts(key)


def test_registration_unique_constraint_race_returns_conflict() -> None:
    class RacingSession:
        rolled_back = False

        def scalar(self, statement):
            return None

        def add(self, item) -> None:
            return None

        def flush(self) -> None:
            raise IntegrityError("insert user", {}, RuntimeError("unique violation"))

        def rollback(self) -> None:
            self.rolled_back = True

    db = RacingSession()
    request = Request({"type": "http", "method": "POST", "path": "/api/auth/register", "headers": [], "client": ("race", 1)})
    with pytest.raises(HTTPException) as raised:
        auth_api.register(
            RegisterRequest(
                account="race_account",
                real_name="竞态测试",
                password="Password-1234",
                confirm_password="Password-1234",
            ),
            request,
            Response(),
            db,
        )
    assert raised.value.status_code == 409
    assert db.rolled_back is True


def test_room_creation_ai_fill_and_public_projection(client: TestClient, register_user) -> None:
    owner = register_user("owner")
    room = create_training_room(owner)
    code = room["code"]
    assert len(code) == 6
    assert room["my_seat"] == "aff_1"
    assert next(seat for seat in room["seats"] if seat["is_me"])["connected"] is False

    ready = owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})
    assert ready.status_code == 200
    started = owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    assert started.status_code == 200, started.text
    seats = started.json()["room"]["seats"]
    assert any(seat["occupant_type"] == "ai" for seat in seats)

    public = client.get(f"/api/rooms/{code}/public")
    assert public.status_code == 200
    assert public.json()["room"]["can_control"] is False


def test_room_creation_is_idempotent_and_binds_request_payload(client: TestClient, register_user) -> None:
    owner = register_user("create_idempotency")
    headers = csrf(owner) | {"X-Idempotency-Key": "same-create-attempt"}
    payload = {
        "competition_slug": "training-1v1",
        "custom_topic": "重复提交只能创建一个房间",
        "seat_key": "aff_1",
        "visibility": "public",
    }
    first = owner.post("/api/rooms", headers=headers, json=payload)
    replayed = owner.post("/api/rooms", headers=headers, json=payload)
    conflict = owner.post("/api/rooms", headers=headers, json={**payload, "seat_key": "neg_1"})

    assert first.status_code == 200
    assert replayed.status_code == 200 and replayed.json()["replayed"] is True
    assert replayed.json()["room"]["code"] == first.json()["room"]["code"]
    assert conflict.status_code == 409
    with SessionLocal() as db:
        created = db.scalar(select(Room).where(Room.code == first.json()["room"]["code"]))
        assert created
        rooms = list(db.scalars(select(Room).where(Room.owner_id == created.owner_id)).all())
        assert len([room for room in rooms if room.topic == payload["custom_topic"]]) == 1


def test_concurrent_seat_claim_is_authoritative(client: TestClient, register_user) -> None:
    owner = register_user("race_owner")
    room = create_training_room(owner, "规则是否限制创新？")
    code = room["code"]
    first = register_user("race_first")
    second = register_user("race_second")
    barrier = Barrier(2)

    def claim(target: TestClient):
        barrier.wait()
        return target.post(f"/api/rooms/{code}/claim-seat", headers=csrf(target), json={"seat_key": "neg_1"})

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(claim, [first, second]))
    assert sorted(item.status_code for item in responses) == [200, 409]
    with SessionLocal() as db:
        stored = load_room(db, code)
        seat = next(item for item in stored.seats if item.seat_key == "neg_1")
        claims = list(db.scalars(select(MatchEvent).where(MatchEvent.room_id == stored.id, MatchEvent.event_type == "seat.claimed")).all())
        assert seat.occupant_type == "human" and seat.user_id
        assert len(claims) == 1 and claims[0].payload["seat_key"] == "neg_1"


def test_room_owner_can_remove_a_blocking_lobby_participant_idempotently(client: TestClient, register_user) -> None:
    owner = register_user("remove_lobby_owner")
    participant = register_user("remove_lobby_participant")
    outsider = register_user("remove_lobby_outsider")
    room = create_training_room(owner, "开赛前应否允许房主调整误入席位？")
    code = room["code"]
    assert participant.post(
        f"/api/rooms/{code}/claim-seat",
        headers=csrf(participant),
        json={"seat_key": "neg_1"},
    ).status_code == 200
    loaded = owner.get(f"/api/rooms/{code}").json()["room"]
    assert next(seat for seat in loaded["seats"] if seat["seat_key"] == "aff_1")["is_owner"] is True
    assert next(seat for seat in loaded["seats"] if seat["seat_key"] == "neg_1")["is_owner"] is False

    denied = outsider.post(
        f"/api/rooms/{code}/seats/neg_1/remove",
        headers=csrf(outsider),
        json={"reason": "无权操作"},
    )
    assert denied.status_code == 403
    self_remove = owner.post(
        f"/api/rooms/{code}/seats/aff_1/remove",
        headers=csrf(owner),
        json={"reason": "错误操作"},
    )
    assert self_remove.status_code == 409

    headers = csrf(owner) | {"X-Idempotency-Key": "remove-blocking-seat-once"}
    removed = owner.post(
        f"/api/rooms/{code}/seats/neg_1/remove",
        headers=headers,
        json={"reason": "参与者长期未准备"},
    )
    replayed = owner.post(
        f"/api/rooms/{code}/seats/neg_1/remove",
        headers=headers,
        json={"reason": "参与者长期未准备"},
    )
    assert removed.status_code == 200 and replayed.status_code == 200
    assert replayed.json()["replayed"] is True
    seat = next(item for item in removed.json()["room"]["seats"] if item["seat_key"] == "neg_1")
    assert seat["occupant_type"] == "open" and seat["display_name"] == "待加入"
    with SessionLocal() as db:
        stored = load_room(db, code)
        events = list(
            db.scalars(
                select(MatchEvent).where(
                    MatchEvent.room_id == stored.id,
                    MatchEvent.event_type == "seat.removed_by_owner",
                )
            ).all()
        )
        assert len(events) == 1
        assert events[0].payload["seat_key"] == "neg_1"
        assert events[0].payload["reason"] == "参与者长期未准备"
    replacement = create_training_room(participant, "被移出后可以立即加入其他比赛")
    assert replacement["status"] == "lobby"


def test_same_user_concurrent_cross_room_claim_has_exactly_one_winner(client: TestClient, register_user) -> None:
    owner_a = register_user("cross_claim_owner_a")
    owner_b = register_user("cross_claim_owner_b")
    participant = register_user("cross_claim_participant")
    room_a = create_training_room(owner_a, "同一辩手并发抢座 A")
    room_b = create_training_room(owner_b, "同一辩手并发抢座 B")
    account = participant.get("/api/auth/session").json()["user"]["account"]
    user_id = participant.get("/api/auth/session").json()["user"]["id"]
    second_device = TestClient(client.app)
    with second_device:
        assert second_device.post("/api/auth/login", json={"account": account, "password": "Password-1234"}).status_code == 200
        barrier = Barrier(2)

        def claim(target: TestClient, code: str):
            barrier.wait()
            return target.post(f"/api/rooms/{code}/claim-seat", headers=csrf(target), json={"seat_key": "neg_1"})

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda args: claim(*args), [(participant, room_a["code"]), (second_device, room_b["code"])]))
    assert sorted(item.status_code for item in responses) == [200, 409]
    with SessionLocal() as db:
        assignments = db.scalar(
            select(func.count())
            .select_from(RoomSeat)
            .join(Room, Room.id == RoomSeat.room_id)
            .where(
                RoomSeat.user_id == user_id,
                RoomSeat.occupant_type == "human",
                Room.status.in_(["lobby", "preparing", "running", "paused", "judging"]),
            )
        )
        assert assignments == 1


@pytest.mark.asyncio
async def test_same_event_loop_conflicting_claims_release_failed_transactions(client: TestClient, register_user) -> None:
    owners = [register_user(f"event_loop_owner_{index}") for index in range(4)]
    rooms = [create_training_room(owner, f"同事件循环冲突房间 {index}") for index, owner in enumerate(owners)]
    participant = register_user("event_loop_participant")
    user = participant.get("/api/auth/session").json()["user"]
    cookies = {key: value for key, value in participant.cookies.items()}
    headers = {"X-CSRF-Token": cookies["jixia_v2_csrf"]}
    transport = ASGITransport(app=client.app)
    async with AsyncClient(transport=transport, base_url="http://testserver", cookies=cookies, timeout=10) as async_client:
        responses = await asyncio.gather(
            *(async_client.post(f"/api/rooms/{room['code']}/claim-seat", headers=headers, json={"seat_key": "neg_1"}) for room in rooms)
        )
    assert sorted(item.status_code for item in responses) == [200, 409, 409, 409]
    with SessionLocal() as db:
        assignments = db.scalar(
            select(func.count())
            .select_from(RoomSeat)
            .join(Room, Room.id == RoomSeat.room_id)
            .where(
                RoomSeat.user_id == user["id"],
                RoomSeat.occupant_type == "human",
                Room.status.in_(["lobby", "preparing", "running", "paused", "judging"]),
            )
        )
        assert assignments == 1


def test_start_and_cancel_race_reaches_one_authoritative_outcome(client: TestClient, register_user) -> None:
    owner = register_user("start_cancel_race")
    room_data = create_training_room(owner, "开始与关闭竞态测试")
    code = room_data["code"]
    assert owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True}).status_code == 200
    account = owner.get("/api/auth/session").json()["user"]["account"]
    second_device = TestClient(client.app)
    with second_device:
        assert second_device.post("/api/auth/login", json={"account": account, "password": "Password-1234"}).status_code == 200
        barrier = Barrier(2)

        def submit(target: TestClient, path: str):
            barrier.wait()
            return target.post(f"/api/rooms/{code}/{path}", headers=csrf(target), json={})

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda args: submit(*args), [(owner, "start"), (second_device, "cancel")]))
    assert sorted(item.status_code for item in responses) == [200, 409]
    with SessionLocal() as db:
        room = load_room(db, code)
        matches = list(db.scalars(select(Match).where(Match.room_id == room.id)).all())
        locked = list(db.scalars(select(MatchEvent).where(MatchEvent.room_id == room.id, MatchEvent.event_type == "room.locked")).all())
        cancelled = list(
            db.scalars(select(MatchEvent).where(MatchEvent.room_id == room.id, MatchEvent.event_type == "room.cancelled")).all()
        )
        assert (room.status == "cancelled" and not matches and not locked and len(cancelled) == 1) or (
            room.status == "preparing" and len(matches) == 1 and len(locked) == 1 and not cancelled
        )


def test_ready_and_start_race_is_retryable_without_duplicate_match(client: TestClient, register_user) -> None:
    owner = register_user("ready_start_race")
    room_data = create_training_room(owner, "准备与开始竞态测试")
    code = room_data["code"]
    second_device = TestClient(client.app)
    with second_device:
        # Exercise two simultaneous requests from the same authenticated
        # browser session. A separately logged-in device must explicitly take
        # over the seat and is covered by the device-control tests below.
        second_device.cookies.update(owner.cookies)
        barrier = Barrier(2)

        def mark_ready():
            barrier.wait()
            return owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})

        def start():
            barrier.wait()
            return second_device.post(f"/api/rooms/{code}/start", headers=csrf(second_device), json={})

        with ThreadPoolExecutor(max_workers=2) as pool:
            ready_response = pool.submit(mark_ready)
            start_response = pool.submit(start)
            responses = [ready_response.result(), start_response.result()]
        assert responses[0].status_code == 200
        if responses[1].status_code == 409:
            responses[1] = second_device.post(f"/api/rooms/{code}/start", headers=csrf(second_device), json={})
        assert responses[1].status_code == 200
    with SessionLocal() as db:
        room = load_room(db, code)
        assert len(db.scalars(select(Match).where(Match.room_id == room.id)).all()) == 1
        assert (
            len(db.scalars(select(MatchEvent).where(MatchEvent.room_id == room.id, MatchEvent.event_type == "seat.ready_changed")).all())
            == 1
        )
        assert len(db.scalars(select(MatchEvent).where(MatchEvent.room_id == room.id, MatchEvent.event_type == "room.locked")).all()) == 1


def test_user_can_hold_only_one_active_human_seat(client: TestClient, register_user) -> None:
    participant = register_user("single_active_participant")
    other_owner = register_user("single_active_other_owner")
    first_room = create_training_room(participant, "唯一活跃席位 A")
    second_create = participant.post(
        "/api/rooms",
        headers=csrf(participant),
        json={
            "competition_slug": "training-1v1",
            "custom_topic": "唯一活跃席位 B",
            "seat_key": "aff_1",
            "visibility": "public",
        },
    )
    assert second_create.status_code == 409
    assert f"#{first_room['code']}" in second_create.json()["detail"]

    other_room = create_training_room(other_owner, "跨房间认领冲突")
    blocked_claim = participant.post(
        f"/api/rooms/{other_room['code']}/claim-seat",
        headers=csrf(participant),
        json={"seat_key": "neg_1"},
    )
    assert blocked_claim.status_code == 409
    participant.post(f"/api/rooms/{first_room['code']}/cancel", headers=csrf(participant), json={})
    claimed = participant.post(
        f"/api/rooms/{other_room['code']}/claim-seat",
        headers=csrf(participant),
        json={"seat_key": "neg_1"},
    )
    assert claimed.status_code == 200
    assert participant.post(f"/api/rooms/{other_room['code']}/release-seat", headers=csrf(participant), json={}).status_code == 200
    created_after_release = create_training_room(participant, "释放后可创建新房间")
    assert created_after_release["status"] == "lobby"


def test_participant_can_abandon_started_match_for_ai_and_restore_rechecks_cross_room_conflict(client: TestClient, register_user) -> None:
    owner = register_user("abandon_started_owner")
    participant = register_user("abandon_started_participant")
    room_data = create_training_room(owner, "开赛后主动退出并由 AI 接替")
    code = room_data["code"]
    assert (
        participant.post(
            f"/api/rooms/{code}/claim-seat",
            headers=csrf(participant),
            json={"seat_key": "neg_1"},
        ).status_code
        == 200
    )
    assert owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True}).status_code == 200
    assert participant.post(f"/api/rooms/{code}/ready", headers=csrf(participant), json={"ready": True}).status_code == 200
    assert owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={}).status_code == 200
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.status = "paused"
        room.paused_remaining_seconds = 90
        db.commit()

    headers = csrf(participant) | {"X-Idempotency-Key": "leave-started-once"}
    abandoned = participant.post(f"/api/rooms/{code}/abandon-seat", headers=headers, json={})
    replayed = participant.post(f"/api/rooms/{code}/abandon-seat", headers=headers, json={})
    assert abandoned.status_code == 200, abandoned.text
    assert replayed.status_code == 200 and replayed.json()["replayed"] is True
    seat = next(item for item in abandoned.json()["room"]["seats"] if item["seat_key"] == "neg_1")
    assert seat["occupant_type"] == "ai_substitute"
    assert seat["display_name"].startswith("AI 接替·")
    assert abandoned.json()["room"]["can_speak"] is False

    replacement = create_training_room(participant, "退出旧比赛后立即创建新房")
    assert replacement["status"] == "lobby"

    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        seat = next(item for item in room.seats if item.seat_key == "neg_1")
        seat.connected = True
        seat.disconnected_at = None
        db.commit()
    admin = TestClient(client.app)
    with admin:
        assert admin.post("/api/auth/login", json={"account": "admin_test", "password": "Admin-test-1234"}).status_code == 200
        blocked = admin.post(f"/api/admin/rooms/{code}/seats/neg_1/restore", headers=csrf(admin), json={})
        assert blocked.status_code == 409
        assert f"#{replacement['code']}" in blocked.json()["detail"]


def test_participant_cannot_abandon_while_their_speech_is_active(client: TestClient, register_user) -> None:
    owner = register_user("abandon_active_owner")
    participant = register_user("abandon_active_participant")
    room_data = create_training_room(owner, "当前发言未完成时不能退出")
    code = room_data["code"]
    participant.post(f"/api/rooms/{code}/claim-seat", headers=csrf(participant), json={"seat_key": "neg_1"})
    owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})
    participant.post(f"/api/rooms/{code}/ready", headers=csrf(participant), json={"ready": True})
    owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        db.add(
            Speech(
                match_id=match.id,
                room_id=room.id,
                seat_key="neg_1",
                stage_key="neg-active",
                speaker_type="human",
                status="speaking",
            )
        )
        db.commit()
    blocked = participant.post(f"/api/rooms/{code}/abandon-seat", headers=csrf(participant), json={})
    assert blocked.status_code == 409
    assert "发言尚未完成" in blocked.json()["detail"]


def test_production_retry_rejects_a_legacy_match_without_frozen_provider_snapshot(client: TestClient, register_user, monkeypatch) -> None:
    owner = register_user("legacy_snapshot_retry_owner")
    room_data = create_training_room(owner, "旧比赛缺少 Provider 快照时禁止盲目重试")
    code = room_data["code"]
    owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})
    owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        match.service_snapshot = {}
        room.status = "paused"
        room.failure_reason = "legacy provider failed"
        room.paused_remaining_seconds = 60
        before_seq = room.seq
        db.commit()
    monkeypatch.setattr(settings, "app_env", "production")
    rejected = owner.post(
        f"/api/rooms/{code}/control/retry",
        headers=csrf(owner) | {"X-Idempotency-Key": "legacy-retry"},
        json={"reason": "旧比赛重试"},
    )
    assert rejected.status_code == 409
    assert "服务配置快照" in rejected.json()["detail"]
    with SessionLocal() as db:
        room = load_room(db, code)
        assert room.status == "paused" and room.seq == before_seq


def test_start_rejects_legacy_cross_room_participant_conflict(client: TestClient, register_user) -> None:
    conflicted = register_user("legacy_conflicted")
    owner = register_user("legacy_conflict_owner")
    first_room = create_training_room(conflicted, "遗留冲突来源房间")
    target_room = create_training_room(owner, "遗留冲突待开赛房间")
    with SessionLocal() as db:
        target = load_room(db, target_room["code"], lock=True)
        user_id = conflicted.get("/api/auth/session").json()["user"]["id"]
        user = db.get(User, user_id)
        seat = next(item for item in target.seats if item.seat_key == "neg_1")
        seat.occupant_type = "human"
        seat.user_id = user_id
        seat.display_name = user.real_name
        db.commit()
    assert owner.post(f"/api/rooms/{target_room['code']}/ready", headers=csrf(owner), json={"ready": True}).status_code == 200
    assert conflicted.post(f"/api/rooms/{target_room['code']}/ready", headers=csrf(conflicted), json={"ready": True}).status_code == 200
    blocked = owner.post(f"/api/rooms/{target_room['code']}/start", headers=csrf(owner), json={})
    assert blocked.status_code == 409 and f"#{first_room['code']}" in blocked.json()["detail"]
    conflicted.post(f"/api/rooms/{first_room['code']}/cancel", headers=csrf(conflicted), json={})
    assert owner.post(f"/api/rooms/{target_room['code']}/start", headers=csrf(owner), json={}).status_code == 200


def test_lobby_actions_are_idempotent_and_owner_can_cancel(client: TestClient, register_user) -> None:
    owner = register_user("lobby_idempotent_owner")
    teammate = register_user("lobby_idempotent_teammate")
    room = create_training_room(owner, "大厅弱网幂等测试")
    code = room["code"]

    claim_payload = {"seat_key": "neg_1"}
    first_claim = teammate.post(f"/api/rooms/{code}/claim-seat", headers=csrf(teammate), json=claim_payload)
    replayed_claim = teammate.post(f"/api/rooms/{code}/claim-seat", headers=csrf(teammate), json=claim_payload)
    assert first_claim.status_code == 200
    assert replayed_claim.status_code == 200 and replayed_claim.json()["replayed"] is True

    release_headers = csrf(teammate) | {"X-Idempotency-Key": "release-once"}
    first_release = teammate.post(f"/api/rooms/{code}/release-seat", headers=release_headers, json={})
    replayed_release = teammate.post(f"/api/rooms/{code}/release-seat", headers=release_headers, json={})
    assert first_release.status_code == 200
    assert replayed_release.status_code == 200 and replayed_release.json()["replayed"] is True

    teammate.post(f"/api/rooms/{code}/claim-seat", headers=csrf(teammate), json=claim_payload)
    first_owner_ready = owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})
    replayed_owner_ready = owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})
    teammate.post(f"/api/rooms/{code}/ready", headers=csrf(teammate), json={"ready": True})
    assert first_owner_ready.status_code == 200
    assert replayed_owner_ready.status_code == 200 and replayed_owner_ready.json()["replayed"] is True

    started = owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    replayed_start = owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    assert started.status_code == 200
    assert replayed_start.status_code == 200 and replayed_start.json()["replayed"] is True
    with SessionLocal() as db:
        stored = load_room(db, code)
        assert len(db.scalars(select(Match).where(Match.room_id == stored.id)).all()) == 1
        assert len(db.scalars(select(MatchEvent).where(MatchEvent.room_id == stored.id, MatchEvent.event_type == "room.locked")).all()) == 1

    assert owner.post(f"/api/rooms/{code}/control/terminate", headers=csrf(owner), json={"reason": "继续测试大厅关闭"}).status_code == 200
    assert owner.post(f"/api/rooms/{code}/control/terminate", headers=csrf(owner), json={"reason": "重复终止"}).status_code == 409
    cancellable = create_training_room(owner, "房主关闭未开始房间测试")
    cancel_code = cancellable["code"]
    assert teammate.post(f"/api/rooms/{cancel_code}/cancel", headers=csrf(teammate), json={}).status_code == 403
    cancelled = owner.post(f"/api/rooms/{cancel_code}/cancel", headers=csrf(owner), json={})
    replayed_cancel = owner.post(f"/api/rooms/{cancel_code}/cancel", headers=csrf(owner), json={})
    assert cancelled.status_code == 200 and cancelled.json()["room"]["status"] == "cancelled"
    assert replayed_cancel.status_code == 200 and replayed_cancel.json()["replayed"] is True
    assert owner.post(f"/api/rooms/{cancel_code}/control/terminate", headers=csrf(owner), json={}).status_code == 409
    assert cancel_code not in {item["code"] for item in owner.get("/api/me").json()["active_rooms"]}
    with SessionLocal() as db:
        cancelled_room = load_room(db, cancel_code)
        assert db.scalar(select(Match).where(Match.room_id == cancelled_room.id)) is None


def test_room_isolation_and_rbac(client: TestClient, register_user) -> None:
    owner_a = register_user("isolation_a")
    owner_b = register_user("isolation_b")
    room_a = create_training_room(owner_a, "A 房间辩题")
    room_b = create_training_room(owner_b, "B 房间辩题")
    owner_a.post(f"/api/rooms/{room_a['code']}/ready", headers=csrf(owner_a), json={"ready": True})
    owner_a.post(f"/api/rooms/{room_a['code']}/start", headers=csrf(owner_a), json={})

    refreshed_b = owner_b.get(f"/api/rooms/{room_b['code']}").json()["room"]
    assert refreshed_b["status"] == "lobby"
    forbidden = owner_b.post(
        f"/api/rooms/{room_a['code']}/control/pause",
        headers=csrf(owner_b),
        json={"reason": "cross-room"},
    )
    assert forbidden.status_code == 403


def test_private_room_rejects_outsider_rest_and_websocket(client: TestClient, register_user) -> None:
    owner = register_user("private_owner")
    outsider = register_user("private_outsider")
    created = owner.post(
        "/api/rooms",
        headers=csrf(owner),
        json={
            "competition_slug": "training-1v1",
            "custom_topic": "私密房间权限测试",
            "seat_key": "aff_1",
            "visibility": "private",
        },
    )
    assert created.status_code == 200
    code = created.json()["room"]["code"]

    assert outsider.get(f"/api/rooms/{code}").status_code == 403
    assert outsider.get(f"/api/rooms/{code}/public").status_code == 403
    claim = outsider.post(f"/api/rooms/{code}/claim-seat", headers=csrf(outsider), json={"seat_key": "neg_1"})
    assert claim.status_code == 403
    with pytest.raises(WebSocketDisconnect) as disconnected:
        with outsider.websocket_connect(f"/ws/rooms/{code}") as socket:
            socket.receive_json()
    assert disconnected.value.code == 4403


def test_websocket_rejects_cross_site_browser_origins(client: TestClient, register_user) -> None:
    owner = register_user("origin_owner")
    room = create_training_room(owner, "WebSocket Origin 白名单测试")
    code = room["code"]
    with pytest.raises(WebSocketDisconnect) as rejected:
        with client.websocket_connect(f"/ws/rooms/{code}", headers={"Origin": "https://evil.example"}) as socket:
            socket.receive_json()
    assert rejected.value.code == 4403

    with owner.websocket_connect(f"/ws/rooms/{code}", headers={"Origin": "http://localhost:3200"}) as socket:
        assert socket.receive_json()["room"]["code"] == code

    with pytest.raises(WebSocketDisconnect) as asr_rejected:
        with client.websocket_connect(f"/ws/rooms/{code}/asr", headers={"Origin": "https://evil.example"}) as socket:
            socket.receive_json()
    assert asr_rejected.value.code == 4403

    with pytest.raises(WebSocketDisconnect) as audio_rejected:
        with client.websocket_connect(f"/ws/rooms/{code}/audio", headers={"Origin": "https://evil.example"}) as socket:
            socket.receive_json()
    assert audio_rejected.value.code == 4403


def test_audio_websocket_streams_fixed_pcm_frames_with_generation_seq_and_pts(client: TestClient, register_user) -> None:
    owner = register_user("audio_stream_owner")
    room_data = create_training_room(owner, "独立音频 WebSocket 固定帧测试")
    code = room_data["code"]
    generation = uuid.uuid4().hex
    speech_id = str(uuid.uuid4())
    with SessionLocal() as db:
        room = load_room(db, code)
        room.status = "running"
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        if match is None:
            match = Match(
                room_id=room.id,
                competition_id=room.competition_id,
                season_id=room.season_id,
                status="running",
            )
            db.add(match)
            db.flush()
        db.add(
            Speech(
                id=speech_id,
                match_id=match.id,
                room_id=room.id,
                seat_key="neg_1",
                stage_key="stream-test",
                speaker_type="ai",
                content="固定帧测试",
                status="synthesizing",
                playback_started_at=now(),
                stream_generation=generation,
                stream_sample_rate=24_000,
            )
        )
        db.commit()
    target = settings.media_path / code / f"{speech_id}.wav"
    target.parent.mkdir(parents=True, exist_ok=True)
    wav_buffer = io.BytesIO()
    with wave.open(wav_buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24_000)
        audio.writeframes((1200).to_bytes(2, "little", signed=True) * 2_400)
    target.write_bytes(wav_buffer.getvalue())

    with owner.websocket_connect(f"/ws/rooms/{code}/audio", headers={"Origin": "http://localhost:3200"}) as socket:
        socket.send_json(
            {
                "type": "subscribe",
                "protocol_version": 1,
                "speech_id": speech_id,
                "generation": generation,
                "after_seq": -1,
            }
        )
        start = socket.receive_json()
        assert start == {
            "type": "audio.start",
            "protocol_version": 1,
            "speech_id": speech_id,
            "generation": generation,
            "encoding": "pcm_s16le",
            "sample_rate": 24_000,
            "channels": 1,
            "frame_samples": 960,
            "start_seq": 0,
            "start_pts_samples": 0,
            "requested_start_seq": 0,
            "available_samples": 2_400,
            "clamped": False,
            "playback_started_at": start["playback_started_at"],
            "replay": False,
        }
        frames: list[tuple[int, int, int, bytes]] = []
        while True:
            packet = socket.receive()
            if packet.get("bytes") is not None:
                raw = packet["bytes"]
                magic, version, flags, generation_id, seq, pts, samples = realtime_api.AUDIO_STREAM_BINARY_HEADER.unpack(
                    raw[: realtime_api.AUDIO_STREAM_BINARY_HEADER.size]
                )
                assert magic == b"JX" and version == 1 and flags == 0
                assert generation_id == int(generation[:8], 16)
                frames.append((seq, pts, samples, raw[realtime_api.AUDIO_STREAM_BINARY_HEADER.size :]))
                continue
            message = json.loads(packet["text"])
            assert message["type"] == "audio.final"
            assert message["generation"] == generation
            break
    assert [(seq, pts, samples) for seq, pts, samples, _pcm in frames] == [
        (0, 0, 960),
        (1, 960, 960),
        (2, 1920, 480),
    ]
    assert all(len(pcm) == samples * 2 for _seq, _pts, samples, pcm in frames)

    with client.websocket_connect(f"/ws/rooms/{code}/audio", headers={"Origin": "http://localhost:3200"}) as public_socket:
        public_socket.send_json(
            {
                "type": "subscribe",
                "protocol_version": 1,
                "speech_id": speech_id,
                "generation": generation,
                "after_seq": 1,
            }
        )
        assert public_socket.receive_json()["start_seq"] == 2

    with client.websocket_connect(f"/ws/rooms/{code}/audio", headers={"Origin": "http://localhost:3200"}) as future_socket:
        future_socket.send_json(
            {
                "type": "subscribe",
                "protocol_version": 1,
                "speech_id": speech_id,
                "generation": generation,
                "after_seq": 999,
            }
        )
        future_start = future_socket.receive_json()
        assert future_start["start_seq"] == 0
        first_packet = future_socket.receive()
        assert first_packet.get("bytes") is not None
        _magic, _version, _flags, _generation_id, seq, pts, samples = realtime_api.AUDIO_STREAM_BINARY_HEADER.unpack(
            first_packet["bytes"][: realtime_api.AUDIO_STREAM_BINARY_HEADER.size]
        )
        assert (seq, pts, samples) == (0, 0, 960)
    with SessionLocal() as db:
        stored_room = load_room(db, code, lock=True)
        stored_speech = db.get(Speech, speech_id)
        stored_room.status = "terminated"
        stored_speech.status = "completed"
        db.commit()


def test_audio_websocket_flow_control_bounds_initial_burst(client: TestClient, register_user) -> None:
    owner = register_user("audio_stream_flow_control")
    room_data = create_training_room(owner, "音频 WebSocket 客户端流控测试")
    code = room_data["code"]
    generation = uuid.uuid4().hex
    speech_id = str(uuid.uuid4())
    with SessionLocal() as db:
        room = load_room(db, code)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        if match is None:
            match = Match(room_id=room.id, competition_id=room.competition_id, season_id=room.season_id, status="running")
            db.add(match)
            db.flush()
        db.add(
            Speech(
                id=speech_id,
                match_id=match.id,
                room_id=room.id,
                seat_key="neg_1",
                stage_key="flow-control-test",
                speaker_type="ai",
                content="客户端流控测试",
                status="synthesizing",
                playback_started_at=now(),
                stream_generation=generation,
                stream_sample_rate=24_000,
            )
        )
        db.commit()
    target = settings.media_path / code / f"{speech_id}.wav"
    target.parent.mkdir(parents=True, exist_ok=True)
    wav_buffer = io.BytesIO()
    with wave.open(wav_buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24_000)
        audio.writeframes((900).to_bytes(2, "little", signed=True) * (25 * 960))
    target.write_bytes(wav_buffer.getvalue())

    with owner.websocket_connect(f"/ws/rooms/{code}/audio", headers={"Origin": "http://localhost:3200"}) as socket:
        socket.send_json(
            {
                "type": "subscribe",
                "protocol_version": 1,
                "speech_id": speech_id,
                "generation": generation,
                "after_seq": -1,
                "flow_control": True,
            }
        )
        assert socket.receive_json()["type"] == "audio.start"
        for expected_seq in range(realtime_api.AUDIO_STREAM_INITIAL_BURST_FRAMES):
            raw = socket.receive_bytes()
            _magic, _version, _flags, _generation_id, seq, _pts, _samples = realtime_api.AUDIO_STREAM_BINARY_HEADER.unpack(
                raw[: realtime_api.AUDIO_STREAM_BINARY_HEADER.size]
            )
            assert seq == expected_seq
        socket.send_json(
            {
                "type": "flow",
                "protocol_version": 1,
                "generation": generation,
                "buffered_ms": 800,
                "played_ms": 0,
            }
        )
        for expected_seq in range(realtime_api.AUDIO_STREAM_INITIAL_BURST_FRAMES, 25):
            raw = socket.receive_bytes()
            _magic, _version, _flags, _generation_id, seq, _pts, _samples = realtime_api.AUDIO_STREAM_BINARY_HEADER.unpack(
                raw[: realtime_api.AUDIO_STREAM_BINARY_HEADER.size]
            )
            assert seq == expected_seq
        assert socket.receive_json()["type"] == "audio.final"


def test_audio_websocket_rejects_stale_generation(client: TestClient, register_user) -> None:
    owner = register_user("audio_stream_stale")
    room_data = create_training_room(owner, "旧 generation 不得读取")
    code = room_data["code"]
    with owner.websocket_connect(f"/ws/rooms/{code}/audio") as socket:
        socket.send_json(
            {
                "type": "subscribe",
                "protocol_version": 1,
                "speech_id": str(uuid.uuid4()),
                "generation": uuid.uuid4().hex,
                "after_seq": -1,
            }
        )
        with pytest.raises(WebSocketDisconnect) as rejected:
            socket.receive_json()
    assert rejected.value.code == 4409


def test_audio_websocket_emits_abort_immediately_when_server_queue_is_cleared(
    client: TestClient,
    register_user,
) -> None:
    owner = register_user("audio_stream_abort")
    room_data = create_training_room(owner, "音频服务端队列即时清空测试")
    code = room_data["code"]
    generation = uuid.uuid4().hex
    speech_id = str(uuid.uuid4())
    with SessionLocal() as db:
        room = load_room(db, code)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        if match is None:
            match = Match(room_id=room.id, competition_id=room.competition_id, season_id=room.season_id, status="running")
            db.add(match)
            db.flush()
        db.add(
            Speech(
                id=speech_id,
                match_id=match.id,
                room_id=room.id,
                seat_key="neg_1",
                stage_key="stream-abort",
                speaker_type="ai",
                status="synthesizing",
                playback_started_at=now(),
                stream_generation=generation,
                stream_sample_rate=24_000,
            )
        )
        db.commit()
    part = settings.media_path / code / f".{speech_id}.{generation}.wav.part"
    part.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(part), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24_000)
        audio.writeframes(b"")

    with owner.websocket_connect(f"/ws/rooms/{code}/audio", headers={"Origin": "http://localhost:3200"}) as socket:
        socket.send_json(
            {
                "type": "subscribe",
                "protocol_version": 1,
                "speech_id": speech_id,
                "generation": generation,
                "after_seq": -1,
            }
        )
        assert socket.receive_json()["type"] == "audio.start"
        assert realtime_api.audio_stream_aborts.abort(code, speech_id, generation) == 1
        aborted = socket.receive_json()
        assert aborted == {
            "type": "audio.abort",
            "speech_id": speech_id,
            "generation": generation,
            "final_seq": 0,
            "total_samples": 0,
        }


def test_anonymous_audio_stream_is_revoked_when_a_public_room_becomes_private(client: TestClient, register_user) -> None:
    owner = register_user("audio_stream_visibility")
    room_data = create_training_room(owner, "音频长连接权限复验")
    code = room_data["code"]
    generation = uuid.uuid4().hex
    speech_id = str(uuid.uuid4())
    with SessionLocal() as db:
        room = load_room(db, code)
        room.status = "running"
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        if match is None:
            match = Match(room_id=room.id, competition_id=room.competition_id, season_id=room.season_id, status="running")
            db.add(match)
            db.flush()
        db.add(
            Speech(
                id=speech_id,
                match_id=match.id,
                room_id=room.id,
                seat_key="neg_1",
                stage_key="stream-visibility",
                speaker_type="ai",
                content="权限复验",
                status="synthesizing",
                playback_started_at=now(),
                stream_generation=generation,
                stream_sample_rate=24_000,
            )
        )
        db.commit()
    part = settings.media_path / code / f".{speech_id}.{generation}.wav.part"
    part.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(part), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24_000)
        audio.writeframes(b"")

    with pytest.raises(WebSocketDisconnect) as revoked:
        with client.websocket_connect(f"/ws/rooms/{code}/audio", headers={"Origin": "http://localhost:3200"}) as socket:
            socket.send_json(
                {
                    "type": "subscribe",
                    "protocol_version": 1,
                    "speech_id": speech_id,
                    "generation": generation,
                    "after_seq": -1,
                }
            )
            assert socket.receive_json()["type"] == "audio.start"
            with SessionLocal() as db:
                room = load_room(db, code, lock=True)
                room.visibility = "private"
                db.commit()
            socket.receive_json()
    assert revoked.value.code == 4403
    with SessionLocal() as db:
        stored_room = load_room(db, code, lock=True)
        stored_speech = db.get(Speech, speech_id)
        stored_room.status = "terminated"
        stored_speech.status = "interrupted"
        db.commit()


@pytest.mark.asyncio
async def test_disconnect_during_initial_snapshot_releases_presence_without_server_error(register_user) -> None:
    owner = register_user("initial_snapshot_disconnect")
    room_data = create_training_room(owner, "初始快照断开是否会留下虚假在线状态？")
    code = room_data["code"]
    user_id = owner.get("/api/auth/session").json()["user"]["id"]

    class DisconnectingWebSocket:
        headers = {"origin": "http://localhost:3200"}
        cookies = {realtime_api.SESSION_COOKIE: owner.cookies.get(realtime_api.SESSION_COOKIE)}

        async def accept(self) -> None:
            return None

        async def send_json(self, _payload: dict) -> None:
            raise RuntimeError("the handler is closed")

        async def send_text(self, _payload: str) -> None:
            raise AssertionError("authenticated snapshots use JSON")

        async def close(self, *, code: int = 1000) -> None:
            return None

    await realtime_api.room_websocket(DisconnectingWebSocket(), code)

    with SessionLocal() as db:
        room = load_room(db, code)
        seat = next(item for item in room.seats if item.user_id == user_id)
        event_types = list(db.scalars(select(MatchEvent.event_type).where(MatchEvent.room_id == room.id).order_by(MatchEvent.seq)).all())
        assert seat.connected is False
        assert seat.disconnected_at is not None
        assert event_types[-2:] == ["presence.connected", "presence.disconnected"]
    assert await realtime_api.room_hub.presence_join(code, "aff_1", user_id) is True
    assert await realtime_api.room_hub.presence_leave(code, "aff_1", user_id) is True


@pytest.mark.asyncio
async def test_room_websocket_lock_timeout_closes_for_retry_without_leaking_presence(
    register_user,
    monkeypatch,
) -> None:
    owner = register_user("websocket_lock_timeout")
    room_data = create_training_room(owner, "房间更新时重连是否能够自动恢复？")
    code = room_data["code"]
    user_id = owner.get("/api/auth/session").json()["user"]["id"]
    original_load_room = realtime_api.load_room

    def lock_timeout(db, target_code: str, *, lock: bool = False):
        if lock:
            raise realtime_api.TransactionLockTimeout("simulated room row lock")
        return original_load_room(db, target_code, lock=lock)

    monkeypatch.setattr(realtime_api, "load_room", lock_timeout)

    class LockContendedWebSocket:
        headers = {"origin": "http://localhost:3200"}
        cookies = {realtime_api.SESSION_COOKIE: owner.cookies.get(realtime_api.SESSION_COOKIE)}

        def __init__(self) -> None:
            self.close_codes: list[int] = []

        async def accept(self) -> None:
            return None

        async def send_json(self, _payload: dict) -> None:
            raise AssertionError("a contended presence join must close before the initial snapshot")

        async def send_text(self, _payload: str) -> None:
            raise AssertionError("authenticated snapshots use JSON")

        async def close(self, *, code: int = 1000) -> None:
            self.close_codes.append(code)

    websocket = LockContendedWebSocket()
    await realtime_api.room_websocket(websocket, code)

    assert websocket.close_codes == [1013]
    assert await realtime_api.room_hub.presence_join(code, "aff_1", user_id) is True
    assert await realtime_api.room_hub.presence_leave(code, "aff_1", user_id) is True
    with SessionLocal() as db:
        room = original_load_room(db, code)
        seat = next(item for item in room.seats if item.user_id == user_id)
        assert seat.connected is False
        event_types = list(db.scalars(select(MatchEvent.event_type).where(MatchEvent.room_id == room.id).order_by(MatchEvent.seq)).all())
        assert "presence.connected" not in event_types


@pytest.mark.asyncio
async def test_room_websocket_awaits_cancelled_sender_cleanup(register_user, monkeypatch) -> None:
    owner = register_user("websocket_task_cleanup")
    room_data = create_training_room(owner, "断线时后台订阅任务是否会完整释放？")
    code = room_data["code"]
    stream_started = asyncio.Event()
    stream_closed = asyncio.Event()

    async def blocked_stream(_code: str, **_kwargs):
        stream_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stream_closed.set()
        yield {"type": "unreachable"}

    monkeypatch.setattr(realtime_api.room_hub, "stream", blocked_stream)

    class DisconnectAfterSenderStarts:
        headers = {"origin": "http://localhost:3200"}
        cookies = {realtime_api.SESSION_COOKIE: owner.cookies.get(realtime_api.SESSION_COOKIE)}

        async def accept(self) -> None:
            return None

        async def send_json(self, _payload: dict) -> None:
            return None

        async def send_text(self, _payload: str) -> None:
            raise AssertionError("authenticated snapshots use JSON")

        async def receive_json(self) -> dict:
            await stream_started.wait()
            raise WebSocketDisconnect(code=1006)

        async def close(self, *, code: int = 1000) -> None:
            return None

    await realtime_api.room_websocket(DisconnectAfterSenderStarts(), code)

    assert stream_closed.is_set()


@pytest.mark.asyncio
async def test_anonymous_room_websocket_catches_event_committed_during_initial_subscription_handoff(register_user) -> None:
    public_snapshot_cache.clear()
    owner = register_user("websocket_initial_gap")
    room_data = create_training_room(owner, "初始订阅交接前的辩题")
    code = room_data["code"]
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.status = "running"
        db.commit()
    second_snapshot_sent = asyncio.Event()

    class CommitAfterInitialSnapshot:
        headers = {"origin": "http://localhost:3200"}
        cookies: dict[str, str] = {}
        messages: list[dict] = []

        async def accept(self) -> None:
            return None

        async def send_text(self, payload: str) -> None:
            self.messages.append(json.loads(payload))
            with SessionLocal() as db:
                room = load_room(db, code, lock=True)
                room.topic = "订阅建立前刚刚更新的辩题"
                append_event(db, room, "topic.updated.during_handoff", {})
                db.commit()

        async def send_json(self, payload: dict) -> None:
            self.messages.append(payload)
            if len(self.messages) >= 2:
                second_snapshot_sent.set()

        async def receive_json(self) -> dict:
            await second_snapshot_sent.wait()
            raise WebSocketDisconnect(code=1006)

        async def close(self, *, code: int = 1000) -> None:
            return None

    websocket = CommitAfterInitialSnapshot()
    await realtime_api.room_websocket(websocket, code)

    assert len(websocket.messages) == 2
    assert websocket.messages[0]["room"]["topic"] == "初始订阅交接前的辩题"
    assert websocket.messages[1]["room"]["topic"] == "订阅建立前刚刚更新的辩题"
    assert websocket.messages[1]["room"]["seq"] == websocket.messages[0]["room"]["seq"] + 1


@pytest.mark.asyncio
async def test_disconnect_during_asr_ready_releases_stream_without_server_error(register_user) -> None:
    owner = register_user("asr_ready_disconnect")
    code, speech_id = prepare_human_speech(owner)

    class DisconnectingAsrWebSocket:
        headers = {"origin": "http://localhost:3200"}
        cookies = {realtime_api.SESSION_COOKIE: owner.cookies.get(realtime_api.SESSION_COOKIE)}

        async def accept(self) -> None:
            return None

        async def receive_json(self) -> dict:
            return {"type": "authenticate", "lease": "test-lease"}

        async def send_json(self, _payload: dict) -> None:
            raise RuntimeError("the handler is closed")

        async def close(self, *, code: int = 1000) -> None:
            return None

    await realtime_api.asr_websocket(DisconnectingAsrWebSocket(), code)

    assert realtime_api._claim_asr_stream(speech_id) is True
    realtime_api._release_asr_stream(speech_id)


def test_asr_websocket_rejects_stale_device_lease(client: TestClient, register_user) -> None:
    owner = register_user("asr_device_lease")
    code, _speech_id = prepare_human_speech(owner)
    with pytest.raises(WebSocketDisconnect) as rejected:
        with owner.websocket_connect(f"/ws/rooms/{code}/asr") as socket:
            socket.send_json({"type": "authenticate", "lease": "stale-device"})
            socket.receive_json()
    assert rejected.value.code == 4409


@pytest.mark.parametrize(
    "authentication",
    [
        {"type": "authenticate", "lease": "test-lease"},
        {
            "type": "authenticate",
            "lease": "test-lease",
            "protocol_version": 1,
            "encoding": "pcm_s16le",
            "channels": 1,
            "sample_rate": 16_000,
        },
    ],
    ids=["legacy", "explicit-v1"],
)
def test_asr_websocket_accepts_current_device_lease(client: TestClient, register_user, monkeypatch, authentication) -> None:
    class FakeUpstream:
        def __init__(self) -> None:
            self.sent: list[bytes | str] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def send(self, payload: bytes | str) -> None:
            self.sent.append(payload)

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.Event().wait()
            raise StopAsyncIteration

    upstream = FakeUpstream()
    connected_urls: list[str] = []

    def connect(url: str, *args, **kwargs):
        connected_urls.append(url)
        return upstream

    monkeypatch.setattr(realtime_api.websockets, "connect", connect)
    owner = register_user(f"asr_current_lease_{'legacy' if len(authentication) == 2 else 'v1'}")
    code, speech_id = prepare_human_speech(owner)
    with SessionLocal() as db:
        room = load_room(db, code)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        expected_asr_url = match.service_snapshot["funasr"]["endpoint"]
    with owner.websocket_connect(f"/ws/rooms/{code}/asr") as socket:
        socket.send_json(authentication)
        ready = socket.receive_json()
        assert ready == {"type": "ready", "speech_id": speech_id}
        socket.send_json({"type": "finish"})
    assert "START" in upstream.sent and "STOP" in upstream.sent
    assert connected_urls == [expected_asr_url]


@pytest.mark.parametrize(
    ("case", "metadata"),
    [
        ("version", {"protocol_version": 2, "encoding": "pcm_s16le", "channels": 1, "sample_rate": 16_000}),
        ("encoding", {"protocol_version": 1, "encoding": "f32le", "channels": 1, "sample_rate": 16_000}),
        ("channels", {"protocol_version": 1, "encoding": "pcm_s16le", "channels": 2, "sample_rate": 16_000}),
        ("sample_rate", {"protocol_version": 1, "encoding": "pcm_s16le", "channels": 1, "sample_rate": 48_000}),
        ("partial", {"protocol_version": 1}),
        ("bool_version", {"protocol_version": True, "encoding": "pcm_s16le", "channels": 1, "sample_rate": 16_000}),
        ("unknown_field", {"sampleRate": 48_000}),
    ],
)
def test_asr_websocket_rejects_unsupported_wire_protocol_before_claiming_stream(
    client: TestClient,
    register_user,
    monkeypatch,
    case,
    metadata,
) -> None:
    def unexpected_connect(*_args, **_kwargs):
        raise AssertionError("unsupported ASR protocol must be rejected before upstream connect")

    monkeypatch.setattr(realtime_api.websockets, "connect", unexpected_connect)
    owner = register_user(f"asr_bad_protocol_{case}")
    code, speech_id = prepare_human_speech(owner)
    with pytest.raises(WebSocketDisconnect) as rejected:
        with owner.websocket_connect(f"/ws/rooms/{code}/asr") as socket:
            socket.send_json({"type": "authenticate", "lease": "test-lease", **metadata})
            assert socket.receive_json() == {
                "type": "error",
                "code": "unsupported_asr_protocol",
                "message": "语音识别协议不兼容，录音仍会正常保存。",
            }
            socket.receive_json()
    assert rejected.value.code == 4400
    assert realtime_api._claim_asr_stream(speech_id) is True
    realtime_api._release_asr_stream(speech_id)


def test_asr_allows_only_one_live_stream_per_speech(client: TestClient, register_user, monkeypatch) -> None:
    class BlockingUpstream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def send(self, _payload: bytes | str) -> None:
            return None

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.Event().wait()
            raise StopAsyncIteration

    monkeypatch.setattr(realtime_api.websockets, "connect", lambda *args, **kwargs: BlockingUpstream())
    owner = register_user("asr_single_stream")
    code, speech_id = prepare_human_speech(owner)
    with owner.websocket_connect(f"/ws/rooms/{code}/asr") as first:
        first.send_json({"type": "authenticate", "lease": "test-lease"})
        assert first.receive_json() == {"type": "ready", "speech_id": speech_id}
        with pytest.raises(WebSocketDisconnect) as rejected:
            with owner.websocket_connect(f"/ws/rooms/{code}/asr") as second:
                second.send_json({"type": "authenticate", "lease": "test-lease"})
                second.receive_json()
        assert rejected.value.code == 4409


def test_asr_rejects_late_final_after_device_takeover(client: TestClient, register_user, monkeypatch) -> None:
    class FinalUpstream:
        def __init__(self) -> None:
            self.messages: asyncio.Queue[str] | None = None

        async def __aenter__(self):
            self.messages = asyncio.Queue()
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def send(self, payload: bytes | str) -> None:
            if payload == "STOP":
                assert self.messages is not None
                await self.messages.put(json.dumps({"is_final": True, "text": "不应写入的迟到字幕。"}, ensure_ascii=False))

        def __aiter__(self):
            return self

        async def __anext__(self):
            assert self.messages is not None
            return await self.messages.get()

    monkeypatch.setattr(realtime_api.websockets, "connect", lambda *args, **kwargs: FinalUpstream())
    owner = register_user("asr_takeover_final")
    code, speech_id = prepare_human_speech(owner)
    with pytest.raises(WebSocketDisconnect) as rejected:
        with owner.websocket_connect(f"/ws/rooms/{code}/asr") as socket:
            socket.send_json({"type": "authenticate", "lease": "test-lease"})
            assert socket.receive_json() == {"type": "ready", "speech_id": speech_id}
            with SessionLocal() as db:
                room = load_room(db, code, lock=True)
                seat = next(item for item in room.seats if item.seat_key == "aff_1")
                seat.control_lease = "new-device"
                db.commit()
            socket.send_json({"type": "finish"})
            socket.receive_json()
    assert rejected.value.code == 4409
    with SessionLocal() as db:
        speech = db.get(Speech, speech_id)
        segments = list(db.scalars(select(TranscriptSegment).where(TranscriptSegment.speech_id == speech_id)).all())
        assert speech and speech.content == "" and segments == []


def test_asr_websocket_reports_service_disabled_from_match_snapshot(client: TestClient, register_user) -> None:
    owner = register_user("asr_disabled_snapshot")
    code, _speech_id = prepare_human_speech(owner)
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        snapshot = dict(match.service_snapshot)
        snapshot["funasr"] = {
            "kind": "funasr",
            "endpoint": "ws://asr-disabled.test/ws",
            "settings": {"final_wait_seconds": 30},
            "enabled": False,
            "source": "database",
        }
        match.service_snapshot = snapshot
        db.commit()
    with pytest.raises(WebSocketDisconnect) as disconnected:
        with owner.websocket_connect(f"/ws/rooms/{code}/asr") as socket:
            socket.send_json({"type": "authenticate", "lease": "test-lease"})
            error = socket.receive_json()
            assert error == {"type": "error", "message": "语音识别服务当前未启用，请使用文字补录。"}
            socket.receive_json()
    assert disconnected.value.code == 1013


def test_asr_finish_waits_for_and_persists_offline_final_result(client: TestClient, register_user, monkeypatch) -> None:
    class FakeUpstream:
        def __init__(self) -> None:
            self.messages: asyncio.Queue[str] | None = None

        async def __aenter__(self):
            self.messages = asyncio.Queue()
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def send(self, payload: bytes | str) -> None:
            if payload == "STOP":
                assert self.messages is not None
                await self.messages.put(
                    json.dumps(
                        {"is_final": True, "partial": "", "sentences": [{"text": "最终离线识别句。"}]},
                        ensure_ascii=False,
                    )
                )

        def __aiter__(self):
            return self

        async def __anext__(self):
            assert self.messages is not None
            return await self.messages.get()

    upstream = FakeUpstream()
    monkeypatch.setattr(realtime_api.websockets, "connect", lambda *args, **kwargs: upstream)
    owner = register_user("asr_offline_final")
    code, speech_id = prepare_human_speech(owner)
    with owner.websocket_connect(f"/ws/rooms/{code}/asr") as socket:
        socket.send_json({"type": "authenticate", "lease": "test-lease"})
        assert socket.receive_json() == {"type": "ready", "speech_id": speech_id}
        voiced_pcm = ((4000).to_bytes(2, "little", signed=True) + (-4000).to_bytes(2, "little", signed=True)) * 4096
        socket.send_bytes(voiced_pcm)
        socket.send_json({"type": "finish"})
        final = socket.receive_json()
        assert final["type"] == "asr" and final["is_final"] is True
        assert final["text"] == "最终离线识别句。"

    with SessionLocal() as db:
        speech = db.get(Speech, speech_id)
        assert speech and speech.content == "最终离线识别句。"
        segments = list(db.scalars(select(TranscriptSegment).where(TranscriptSegment.speech_id == speech_id)).all())
        assert [item.text for item in segments] == ["最终离线识别句。"]


@pytest.mark.parametrize(
    ("pcm", "final_payload", "expected_reason"),
    [
        (b"\x00\x00" * 4096, {"is_final": True, "text": "静音环境误识别出的长句。"}, "silence"),
        (
            ((4000).to_bytes(2, "little", signed=True) + (-4000).to_bytes(2, "little", signed=True)) * 4096,
            {"is_final": True, "text": "低置信度识别结果。", "confidence": 0.05},
            "low_confidence",
        ),
    ],
)
def test_asr_rejects_silence_and_low_confidence_finals(
    client: TestClient,
    register_user,
    monkeypatch,
    pcm: bytes,
    final_payload: dict,
    expected_reason: str,
) -> None:
    class FinalUpstream:
        def __init__(self) -> None:
            self.messages: asyncio.Queue[str] | None = None

        async def __aenter__(self):
            self.messages = asyncio.Queue()
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def send(self, payload: bytes | str) -> None:
            if payload == "STOP":
                assert self.messages is not None
                await self.messages.put(json.dumps(final_payload, ensure_ascii=False))

        def __aiter__(self):
            return self

        async def __anext__(self):
            assert self.messages is not None
            return await self.messages.get()

    monkeypatch.setattr(realtime_api.websockets, "connect", lambda *args, **kwargs: FinalUpstream())
    owner = register_user(f"asr_rejected_{expected_reason}")
    code, speech_id = prepare_human_speech(owner)
    with owner.websocket_connect(f"/ws/rooms/{code}/asr") as socket:
        socket.send_json({"type": "authenticate", "lease": "test-lease"})
        assert socket.receive_json() == {"type": "ready", "speech_id": speech_id}
        socket.send_bytes(pcm)
        socket.send_json({"type": "finish"})
        rejected = socket.receive_json()
        assert rejected["type"] == "asr_rejected"
        assert rejected["reason"] == expected_reason

    with SessionLocal() as db:
        speech = db.get(Speech, speech_id)
        segments = list(db.scalars(select(TranscriptSegment).where(TranscriptSegment.speech_id == speech_id)).all())
        assert speech and speech.content == "" and segments == []


@pytest.mark.parametrize("hallucination", ["七", "去"])
def test_asr_rejects_single_character_noise_hallucinations(
    client: TestClient,
    register_user,
    monkeypatch,
    hallucination: str,
) -> None:
    class FinalUpstream:
        def __init__(self) -> None:
            self.messages: asyncio.Queue[str] | None = None

        async def __aenter__(self):
            self.messages = asyncio.Queue()
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def send(self, payload: bytes | str) -> None:
            if payload == "STOP":
                assert self.messages is not None
                await self.messages.put(json.dumps({"is_final": True, "text": hallucination}, ensure_ascii=False))

        def __aiter__(self):
            return self

        async def __anext__(self):
            assert self.messages is not None
            return await self.messages.get()

    monkeypatch.setattr(realtime_api.websockets, "connect", lambda *args, **kwargs: FinalUpstream())
    owner = register_user(f"asr_single_character_{ord(hallucination)}")
    code, speech_id = prepare_human_speech(owner)
    voiced_pcm = ((4000).to_bytes(2, "little", signed=True) + (-4000).to_bytes(2, "little", signed=True)) * 4096
    with owner.websocket_connect(f"/ws/rooms/{code}/asr") as socket:
        socket.send_json({"type": "authenticate", "lease": "test-lease"})
        assert socket.receive_json() == {"type": "ready", "speech_id": speech_id}
        socket.send_bytes(voiced_pcm)
        socket.send_json({"type": "finish"})
        rejected = socket.receive_json()
        assert rejected["type"] == "asr_rejected"
        assert rejected["reason"] == "too_short"

    with SessionLocal() as db:
        speech = db.get(Speech, speech_id)
        segments = list(db.scalars(select(TranscriptSegment).where(TranscriptSegment.speech_id == speech_id)).all())
        assert speech and speech.content == "" and segments == []


def test_asr_rejects_single_transient_noise_chunk(client: TestClient, register_user, monkeypatch) -> None:
    class FinalUpstream:
        def __init__(self) -> None:
            self.messages: asyncio.Queue[str] | None = None

        async def __aenter__(self):
            self.messages = asyncio.Queue()
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def send(self, payload: bytes | str) -> None:
            if payload == "STOP":
                assert self.messages is not None
                await self.messages.put(json.dumps({"is_final": True, "text": "环境噪声误识别成长句。"}, ensure_ascii=False))

        def __aiter__(self):
            return self

        async def __anext__(self):
            assert self.messages is not None
            return await self.messages.get()

    monkeypatch.setattr(realtime_api.websockets, "connect", lambda *args, **kwargs: FinalUpstream())
    owner = register_user("asr_transient_noise")
    code, speech_id = prepare_human_speech(owner)
    transient_pcm = ((4000).to_bytes(2, "little", signed=True) + (-4000).to_bytes(2, "little", signed=True)) * 2048
    with owner.websocket_connect(f"/ws/rooms/{code}/asr") as socket:
        socket.send_json({"type": "authenticate", "lease": "test-lease"})
        assert socket.receive_json() == {"type": "ready", "speech_id": speech_id}
        socket.send_bytes(transient_pcm)
        socket.send_json({"type": "finish"})
        rejected = socket.receive_json()
        assert rejected["type"] == "asr_rejected"
        assert rejected["reason"] == "silence"

    with SessionLocal() as db:
        speech = db.get(Speech, speech_id)
        segments = list(db.scalars(select(TranscriptSegment).where(TranscriptSegment.speech_id == speech_id)).all())
        assert speech and speech.content == "" and segments == []


def test_asr_failure_does_not_expose_internal_service_address(client: TestClient, register_user, monkeypatch) -> None:
    def failed_connect(*args, **kwargs):
        raise RuntimeError("failed to connect ws://127.0.0.1:10095/internal")

    monkeypatch.setattr(realtime_api.websockets, "connect", failed_connect)
    owner = register_user("asr_error_redaction")
    code, speech_id = prepare_human_speech(owner)
    with owner.websocket_connect(f"/ws/rooms/{code}/asr") as socket:
        socket.send_json({"type": "authenticate", "lease": "test-lease"})
        assert socket.receive_json() == {"type": "ready", "speech_id": speech_id}
        error = socket.receive_json()
        assert error == {"type": "error", "message": "语音识别服务暂时不可用，录音仍可继续。"}
        assert "127.0.0.1" not in error["message"]


def test_closing_one_of_two_seat_websockets_keeps_presence_online(register_user) -> None:
    owner = register_user("presence_owner")
    room_data = create_training_room(owner, "多设备在线状态测试")
    code = room_data["code"]

    with owner.websocket_connect(f"/ws/rooms/{code}") as first:
        first_snapshot = first.receive_json()["room"]
        assert first_snapshot["my_seat"] == "aff_1"
        assert first_snapshot["can_control"] is True
        with owner.websocket_connect(f"/ws/rooms/{code}") as second:
            second.receive_json()
        after_second_closed = owner.get(f"/api/rooms/{code}").json()["room"]
        my_seat = next(item for item in after_second_closed["seats"] if item["is_me"])
        assert my_seat["connected"] is True

    after_last_closed = owner.get(f"/api/rooms/{code}").json()["room"]
    my_seat = next(item for item in after_last_closed["seats"] if item["is_me"])
    assert my_seat["connected"] is False


def test_websockets_opened_before_claim_dynamically_bind_seat_presence(client: TestClient, register_user) -> None:
    owner = register_user("dynamic_presence_owner")
    participant = register_user("dynamic_presence_participant")
    room = create_training_room(owner, "先连接后认领席位在线状态测试")
    code = room["code"]
    with participant.websocket_connect(f"/ws/rooms/{code}") as first:
        assert first.receive_json()["room"]["my_seat"] is None
        with participant.websocket_connect(f"/ws/rooms/{code}") as second:
            assert second.receive_json()["room"]["my_seat"] is None
            claimed = participant.post(
                f"/api/rooms/{code}/claim-seat",
                headers=csrf(participant),
                json={"seat_key": "neg_1"},
            )
            assert claimed.status_code == 200
            first_snapshot = first.receive_json()["room"]
            second_snapshot = second.receive_json()["room"]
            assert first_snapshot["my_seat"] == second_snapshot["my_seat"] == "neg_1"
            assert next(item for item in first_snapshot["seats"] if item["is_me"])["connected"] is True
            with SessionLocal() as db:
                stored = load_room(db, code)
                seat = next(item for item in stored.seats if item.seat_key == "neg_1")
                assert seat.connected is True and seat.disconnected_at is None
        with SessionLocal() as db:
            stored = load_room(db, code)
            seat = next(item for item in stored.seats if item.seat_key == "neg_1")
            assert seat.connected is True
    with SessionLocal() as db:
        stored = load_room(db, code)
        seat = next(item for item in stored.seats if item.seat_key == "neg_1")
        assert seat.connected is False and seat.disconnected_at is not None


async def test_anonymous_public_snapshot_cache_coalesces_and_invalidates(register_user, client: TestClient) -> None:
    public_snapshot_cache.clear()
    owner_a = register_user("snapshot_a")
    owner_b = register_user("snapshot_b")
    room_a = create_training_room(owner_a, "公开快照缓存 A")
    room_b = create_training_room(owner_b, "公开快照缓存 B")
    with SessionLocal() as db:
        for code in (room_a["code"], room_b["code"]):
            stored = load_room(db, code, lock=True)
            stored.status = "running"
        db.commit()

    snapshots = await asyncio.gather(*(public_snapshot_cache.get(room_a["code"]) for _ in range(40)))
    assert all(item and item["code"] == room_a["code"] for item in snapshots)
    assert public_snapshot_cache.database_loads(room_a["code"]) == 1
    assert snapshots[0]["can_control"] is False
    assert "id" not in snapshots[0]["owner"]

    other = await public_snapshot_cache.get(room_b["code"])
    assert other and other["code"] == room_b["code"] and other["topic"] != snapshots[0]["topic"]
    assert public_snapshot_cache.database_loads(room_b["code"]) == 1

    with SessionLocal() as db:
        stored = load_room(db, room_a["code"], lock=True)
        stored.topic = "缓存失效后的新辩题"
        append_event(db, stored, "test.snapshot_invalidated", {})
        expected_seq = stored.seq
        db.commit()
    refreshed = await public_snapshot_cache.get(room_a["code"], expected_seq=expected_seq)
    assert refreshed and refreshed["topic"] == "缓存失效后的新辩题"
    assert public_snapshot_cache.database_loads(room_a["code"]) == 2
    newer_snapshot = await public_snapshot_cache.get(room_a["code"], expected_seq=expected_seq - 1)
    assert newer_snapshot and newer_snapshot["seq"] == expected_seq
    assert public_snapshot_cache.database_loads(room_a["code"]) == 2

    private_owner = register_user("snapshot_private")
    private = private_owner.post(
        "/api/rooms",
        headers=csrf(private_owner),
        json={
            "competition_slug": "training-1v1",
            "custom_topic": "私密房间不能进入公开快照缓存",
            "seat_key": "aff_1",
            "visibility": "private",
        },
    ).json()["room"]
    assert await public_snapshot_cache.get(private["code"]) is None
    assert await public_snapshot_cache.get(private["code"]) is None
    assert public_snapshot_cache.database_loads(private["code"]) == 1

    with client.websocket_connect(f"/ws/rooms/{room_a['code']}") as socket:
        anonymous = socket.receive_json()["room"]
        assert anonymous["code"] == room_a["code"]
        assert anonymous["can_control"] is False and anonymous["my_seat"] is None


@pytest.mark.asyncio
async def test_public_snapshot_authority_check_does_not_block_websocket_event_loop(monkeypatch) -> None:
    def slow_authority_check(_code: str):
        time.sleep(0.15)
        return None

    monkeypatch.setattr(public_snapshot_cache, "_load_room_authority", slow_authority_check)
    started = time.perf_counter()
    task = asyncio.create_task(public_snapshot_cache.get_if_newer("123456", known_seq=1))
    await asyncio.sleep(0.01)

    assert time.perf_counter() - started < 0.08
    assert not task.done()
    assert await task is None

def test_presence_event_invalidates_anonymous_snapshot_cache(register_user, client: TestClient) -> None:
    public_snapshot_cache.clear()
    owner = register_user("presence_broadcast")
    room = create_training_room(owner, "在线状态广播缓存失效测试")
    code = room["code"]
    with SessionLocal() as db:
        stored = load_room(db, code, lock=True)
        stored.status = "running"
        db.commit()

    with client.websocket_connect(f"/ws/rooms/{code}") as watcher:
        initial = watcher.receive_json()["room"]
        assert next(item for item in initial["seats"] if item["seat_key"] == "aff_1")["connected"] is False
        with owner.websocket_connect(f"/ws/rooms/{code}") as participant:
            participant.receive_json()
            update = watcher.receive_json()
            assert update["event"]["type"] == "presence.connected"
            assert next(item for item in update["room"]["seats"] if item["seat_key"] == "aff_1")["connected"] is True


def test_anonymous_room_websocket_strips_hub_diagnostics(register_user, client: TestClient, monkeypatch) -> None:
    public_snapshot_cache.clear()
    owner = register_user("anonymous_ws_redaction")
    room = create_training_room(owner, "匿名 WebSocket 只发送观战必需字段")
    with SessionLocal() as db:
        stored = load_room(db, room["code"], lock=True)
        stored.status = "running"
        db.commit()

    async def diagnostic_stream(_code: str, **_kwargs):
        yield {
            "type": "asr",
            "text": "观众可见字幕",
            "is_final": False,
            "room_code": room["code"],
            "speech_id": "private-speech-id",
            "transport": "livekit",
            "track_sid": "TR_private",
            "tts_session_id": "tts-private",
            "voice_id": "private-voice",
            "synthesis_mode": "bistream",
            "agent_first_readable_delta_at": "2026-07-19T01:02:03Z",
            "server_first_capture_at": "2026-07-19T01:02:04Z",
            "agent_diagnostics": {"endpoint": "https://llm.internal.example/v1"},
        }
        await asyncio.Event().wait()

    monkeypatch.setattr(realtime_api.room_hub, "stream", diagnostic_stream)
    with client.websocket_connect(f"/ws/rooms/{room['code']}") as socket:
        initial = socket.receive_json()
        assert initial["room"]["code"] == room["code"]
        update = socket.receive_json()
        assert update["event"] == {"type": "asr", "text": "观众可见字幕", "is_final": False}


async def test_redis_client_initialization_is_single_flight(monkeypatch) -> None:
    created = 0

    class FakeRedis:
        async def ping(self) -> bool:
            await asyncio.sleep(0.01)
            return True

        async def aclose(self) -> None:
            return None

    def from_url(*args, **kwargs):
        nonlocal created
        created += 1
        return FakeRedis()

    monkeypatch.setattr(realtime_service.redis, "from_url", from_url)
    hub = RoomHub()
    clients = await asyncio.gather(*(hub._client() for _ in range(50)))
    assert created == 1
    assert all(client is clients[0] for client in clients)


async def test_slow_room_subscriber_coalesces_to_latest_snapshot_and_releases_resources() -> None:
    hub = RoomHub(redis_enabled=False)
    stream = hub.stream("slow-room")
    first = asyncio.create_task(stream.__anext__())
    for _ in range(20):
        if await hub.local_subscriber_count("slow-room") == 1:
            break
        await asyncio.sleep(0)
    await hub.publish("slow-room", {"type": "event", "seq": 0})
    assert (await asyncio.wait_for(first, timeout=1))["seq"] == 0

    for seq in range(1, 301):
        await hub.publish("slow-room", {"type": "event", "seq": seq})
    received: list[int] = []
    for _ in range(16):
        message = await asyncio.wait_for(stream.__anext__(), timeout=1)
        received.append(message["seq"])
        if message["seq"] == 300:
            break
    assert received[-1] == 300
    assert len(received) <= 16
    await stream.aclose()
    assert await hub.local_subscriber_count("slow-room") == 0


async def test_room_hub_initial_sync_closes_snapshot_subscription_gap_and_keeps_rooms_isolated() -> None:
    hub = RoomHub(redis_enabled=False)
    room_a = hub.stream("room-a", initial_sync=True)
    room_b = hub.stream("room-b", initial_sync=True)

    assert await asyncio.wait_for(room_a.__anext__(), timeout=1) == {"type": "_sync"}
    assert await asyncio.wait_for(room_b.__anext__(), timeout=1) == {"type": "_sync"}
    await hub.publish("room-a", {"type": "stage.advanced", "room_code": "room-a", "seq": 7})

    update_a = await asyncio.wait_for(room_a.__anext__(), timeout=1)
    assert update_a == {"type": "stage.advanced", "room_code": "room-a", "seq": 7}
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(room_b.__anext__(), timeout=0.02)

    await room_a.aclose()
    await room_b.aclose()
    assert await hub.local_subscriber_count("room-a") == 0
    assert await hub.local_subscriber_count("room-b") == 0


def test_startup_presence_reset_marks_stale_humans_offline(register_user) -> None:
    owner = register_user("presence_reset")
    room_data = create_training_room(owner, "服务重启在线状态重置测试")
    with SessionLocal() as db:
        room = load_room(db, room_data["code"], lock=True)
        seat = next(item for item in room.seats if item.user_id)
        seat.connected = True
        seat.disconnected_at = None
        db.commit()
        assert reset_connected_presence(db) == 1
        db.commit()
    with SessionLocal() as db:
        room = load_room(db, room_data["code"])
        seat = next(item for item in room.seats if item.user_id)
        assert seat.connected is False and seat.disconnected_at is not None


async def test_distributed_presence_expiry_persists_disconnect_exactly_once(register_user, monkeypatch) -> None:
    owner = register_user("presence_lease_expiry")
    room_data = create_training_room(owner, "分布式在线租约过期测试")
    code = room_data["code"]
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        seat = next(item for item in room.seats if item.user_id)
        user_id = seat.user_id
        seat_key = seat.seat_key
        seat.connected = True
        seat.disconnected_at = None
        db.commit()

    claims = [[(code, seat_key, user_id)], []]
    monkeypatch.setattr(realtime_service.room_hub, "reap_expired_presence", AsyncMock(side_effect=claims))
    monkeypatch.setattr(realtime_service.room_hub, "presence_active", AsyncMock(return_value=False))
    published = AsyncMock()
    monkeypatch.setattr(realtime_service.room_hub, "publish", published)

    await match_engine._reap_expired_presence()
    await match_engine._reap_expired_presence()

    with SessionLocal() as db:
        room = load_room(db, code)
        seat = next(item for item in room.seats if item.seat_key == seat_key)
        assert seat.connected is False
        assert seat.disconnected_at is not None
        assert (
            db.scalar(
                select(func.count(MatchEvent.id)).where(
                    MatchEvent.room_id == room.id,
                    MatchEvent.event_type == "presence.disconnected",
                )
            )
            == 1
        )
    published.assert_awaited_once()


async def test_presence_expiry_releases_lobby_seat_and_substitutes_running_human(register_user) -> None:
    owner = register_user("expiry_owner")
    teammate = register_user("expiry_teammate")
    lobby = create_training_room(owner, "大厅离线席位释放测试")
    code = lobby["code"]
    teammate.post(f"/api/rooms/{code}/claim-seat", headers=csrf(teammate), json={"seat_key": "neg_1"})
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        seat = next(item for item in room.seats if item.seat_key == "neg_1")
        seat.connected = False
        seat.disconnected_at = now() - timedelta(seconds=119)
        assert await match_engine._expire_presence(db, room) is False
        seat.disconnected_at = now() - timedelta(seconds=121)
        assert await match_engine._expire_presence(db, room) is True
        db.commit()
        assert seat.occupant_type == "open" and seat.user_id is None

    abandoned_owner = register_user("expiry_abandoned_owner")
    abandoned_guest = register_user("expiry_abandoned_guest")
    abandoned = create_training_room(abandoned_owner, "房主离线后仍可返回大厅测试")
    abandoned_guest.post(
        f"/api/rooms/{abandoned['code']}/claim-seat",
        headers=csrf(abandoned_guest),
        json={"seat_key": "neg_1"},
    )
    with SessionLocal() as db:
        room = load_room(db, abandoned["code"], lock=True)
        owner_seat = next(item for item in room.seats if item.user_id == room.owner_id)
        guest_seat = next(item for item in room.seats if item.user_id and item.user_id != room.owner_id)
        owner_seat.connected = False
        owner_seat.disconnected_at = now() - timedelta(seconds=121)
        guest_seat.connected = True
        guest_seat.disconnected_at = None
        assert await match_engine._expire_presence(db, room) is False
        db.commit()
        assert owner_seat.occupant_type == "human" and owner_seat.user_id == room.owner_id
        assert owner_seat.connected is False
        assert guest_seat.occupant_type == "human" and guest_seat.connected is True
        assert room.status == "lobby" and room.completed_at is None
        transferred = db.scalar(
            select(MatchEvent).where(
                MatchEvent.room_id == room.id,
                MatchEvent.event_type == "room.owner_transferred",
            )
        )
        assert transferred is None
        assert not db.scalar(
            select(MatchEvent.id).where(
                MatchEvent.room_id == room.id,
                MatchEvent.event_type == "room.cancelled",
            )
        )

    owner2 = register_user("substitute_owner")
    teammate2 = register_user("substitute_teammate")
    running = create_training_room(owner2, "比赛断线 AI 接替测试")
    running_code = running["code"]
    teammate2.post(f"/api/rooms/{running_code}/claim-seat", headers=csrf(teammate2), json={"seat_key": "neg_1"})
    owner2.post(f"/api/rooms/{running_code}/ready", headers=csrf(owner2), json={"ready": True})
    teammate2.post(f"/api/rooms/{running_code}/ready", headers=csrf(teammate2), json={"ready": True})
    owner2.post(f"/api/rooms/{running_code}/start", headers=csrf(owner2), json={})
    with SessionLocal() as db:
        room = load_room(db, running_code, lock=True)
        seat = next(item for item in room.seats if item.seat_key == "neg_1")
        original_user_id = seat.user_id
        seat.connected = False
        seat.disconnected_at = now() - timedelta(seconds=59)
        assert await match_engine._expire_presence(db, room) is False
        seat.disconnected_at = now() - timedelta(seconds=61)
        assert await match_engine._expire_presence(db, room) is True
        db.commit()
        assert seat.occupant_type == "ai_substitute"
        assert seat.user_id == original_user_id
        assert seat.display_name.startswith("AI 接替·")


@pytest.mark.asyncio
async def test_expired_stage_advances_before_newly_substituted_ai_can_start(register_user, monkeypatch) -> None:
    owner = register_user("expired_substitute")
    room_data = create_training_room(owner, "断线接替时过期阶段不得启动 AI")
    code = room_data["code"]
    owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})
    owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.template_snapshot = [
            {"key": "expired_aff", "name": "已过期正方立论", "kind": "speech", "seat": "aff_1", "duration": 30},
            {"key": "next_neg", "name": "反方立论", "kind": "speech", "seat": "neg_1", "duration": 30},
        ]
        room.current_stage_index = 0
        room.status = "running"
        room.stage_started_at = now() - timedelta(seconds=90)
        room.stage_deadline_at = now() - timedelta(seconds=60)
        seat = next(item for item in room.seats if item.seat_key == "aff_1")
        seat.occupant_type = "human"
        seat.connected = False
        seat.disconnected_at = now() - timedelta(seconds=61)
        db.commit()

    ai_speech = AsyncMock()
    monkeypatch.setattr(match_engine, "_ai_speech", ai_speech)
    await match_engine.process_room(code)
    ai_speech.assert_not_awaited()
    with SessionLocal() as db:
        room = load_room(db, code)
        seat = next(item for item in room.seats if item.seat_key == "aff_1")
        assert seat.occupant_type == "ai_substitute"
        assert room.current_stage_index == 1 and room.current_stage_index < len(room.template_snapshot)
        event_types = list(db.scalars(select(MatchEvent.event_type).where(MatchEvent.room_id == room.id).order_by(MatchEvent.seq)).all())
        assert "seat.ai_substituted" in event_types
        assert "stage.completed" in event_types


@pytest.mark.asyncio
async def test_presence_expiry_interrupts_abandoned_human_speech_and_allows_ai_takeover(register_user, monkeypatch) -> None:
    owner = register_user("abandoned_speech_owner")
    teammate = register_user("abandoned_speech_teammate")
    room_data = create_training_room(owner, "真人断线后 AI 应立即接替当前轮次")
    code = room_data["code"]
    teammate.post(f"/api/rooms/{code}/claim-seat", headers=csrf(teammate), json={"seat_key": "neg_1"})
    owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})
    teammate.post(f"/api/rooms/{code}/ready", headers=csrf(teammate), json={"ready": True})
    owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.template_snapshot = [
            {"key": "neg_turn", "name": "反方立论", "kind": "speech", "seat": "neg_1", "duration": 120},
            {"key": "next_turn", "name": "下一环节", "kind": "speech", "seat": "aff_1", "duration": 120},
        ]
        room.current_stage_index = 0
        room.status = "running"
        room.stage_started_at = now() - timedelta(seconds=61)
        room.stage_deadline_at = now() + timedelta(seconds=59)
        seat = next(item for item in room.seats if item.seat_key == "neg_1")
        seat.connected = False
        seat.disconnected_at = now() - timedelta(seconds=61)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        abandoned = Speech(
            match_id=match.id,
            room_id=room.id,
            seat_key=seat.seat_key,
            stage_key="neg_turn",
            speaker_type="human",
            status="speaking",
        )
        db.add(abandoned)
        db.commit()
        abandoned_id = abandoned.id

    ai_speech = AsyncMock()
    monkeypatch.setattr(match_engine, "_ai_speech", ai_speech)
    await match_engine.process_room(code)
    ai_speech.assert_awaited_once()
    assert ai_speech.await_args.args[2].seat_key == "neg_1"
    with SessionLocal() as db:
        room = load_room(db, code)
        seat = next(item for item in room.seats if item.seat_key == "neg_1")
        speech = db.get(Speech, abandoned_id)
        assert seat.occupant_type == "ai_substitute"
        assert speech.status == "interrupted"
        interrupted = next(
            (
                event
                for event in db.scalars(
                    select(MatchEvent).where(
                        MatchEvent.room_id == room.id,
                        MatchEvent.event_type == "speech.interrupted",
                    )
                ).all()
                if event.payload.get("speech_id") == abandoned_id
            ),
            None,
        )
        assert interrupted and interrupted.payload["reason"] == "presence_expired"


def test_admin_is_system_only(client: TestClient, register_user) -> None:
    normal = register_user("normal")
    assert normal.get("/api/admin/dashboard").status_code == 403

    admin = TestClient(client.app)
    with admin:
        login = admin.post("/api/auth/login", json={"account": "admin_test", "password": "Admin-test-1234"})
        assert login.status_code == 200
        dashboard = admin.get("/api/admin/dashboard")
        assert dashboard.status_code == 200
        assert "providers" in dashboard.json()


def test_admin_lists_clamp_stale_pages_and_reject_unknown_room_status(client: TestClient) -> None:
    admin = TestClient(client.app)
    with admin:
        assert admin.post("/api/auth/login", json={"account": "admin_test", "password": "Admin-test-1234"}).status_code == 200

        for path in ("/api/admin/users", "/api/admin/rooms", "/api/admin/audit"):
            response = admin.get(f"{path}?page=100000&page_size=1")
            assert response.status_code == 200
            payload = response.json()
            assert payload["pagination"]["page"] == payload["pagination"]["pages"]
            if payload["pagination"]["total"]:
                assert len(payload["items"]) == 1

        invalid_status = admin.get("/api/admin/rooms?status=teacher_activity")
        assert invalid_status.status_code == 422


def test_admin_room_inventory_uses_compact_bounded_queries(client: TestClient, register_user) -> None:
    owner = register_user("admin_room_summary")
    created = create_training_room(owner, "长期暂停比赛需要运营关注")
    decoy_owner = register_user("admin_room_summary_decoy")
    decoy = create_training_room(decoy_owner, "高基数历史事件不应进入当前页聚合")
    with SessionLocal.begin() as db:
        room = load_room(db, created["code"], lock=True)
        room.status = "paused"
        for seat in room.seats:
            seat.connected = False
        paused = append_event(db, room, "control.pause", {"reason": "test"}, actor_user_id=room.owner_id)
        paused.created_at = now() - timedelta(hours=2)
        decoy_room = load_room(db, decoy["code"], lock=True)
        for index in range(250):
            append_event(db, decoy_room, "speech.completed", {"index": index})

    admin = TestClient(client.app)
    with admin:
        assert admin.post("/api/auth/login", json={"account": "admin_test", "password": "Admin-test-1234"}).status_code == 200
        statements: list[str] = []

        def track_selects(_connection, _cursor, statement, _parameters, _context, _executemany) -> None:
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement.lower())

        event.listen(engine, "before_cursor_execute", track_selects)
        try:
            response = admin.get(f"/api/admin/rooms?page_size=200&q={created['code']}")
        finally:
            event.remove(engine, "before_cursor_execute", track_selects)
        assert response.status_code == 200
        inventory_statements = [
            statement
            for statement in statements
            if "user_sessions" not in statement and "from users" not in statement
        ]
        assert len(inventory_statements) <= 4
        assert sum("match_events" in statement for statement in inventory_statements) <= 1
        assert not any("speeches" in statement for statement in inventory_statements)
        event_query = next(statement for statement in inventory_statements if "match_events" in statement)
        seat_query = next(statement for statement in inventory_statements if "room_seats" in statement)
        assert "match_events.room_id in" in event_query
        assert "room_seats.room_id in" in seat_query
        items = response.json()["items"]
        if items:
            item = items[0]
            assert {
                "id",
                "code",
                "topic",
                "status",
                "is_test_data",
                "competition",
                "current_stage",
                "connected_humans",
                "failure_reason",
                "paused_at",
                "attention_reason",
                "updated_at",
            } == set(item)
            assert set(item["competition"]) == {"id", "slug", "name"}
            assert item["connected_humans"] == 0
            assert item["attention_reason"] == "stale_paused"
            assert item["paused_at"] is not None

        with SessionLocal.begin() as db:
            room = load_room(db, created["code"], lock=True)
            room.status = "review_required"
            room.failure_reason = "裁判服务超时"
        review = admin.get(f"/api/admin/rooms?q={created['code']}")
        assert review.status_code == 200
        assert review.json()["items"][0]["attention_reason"] == "review_required"


def test_admin_can_manage_topics_and_agent_pool_without_exposing_controls(client: TestClient, register_user) -> None:
    normal = register_user("settings_normal")
    admin = TestClient(client.app)
    with admin:
        assert admin.post("/api/auth/login", json={"account": "admin_test", "password": "Admin-test-1234"}).status_code == 200
        competitions = admin.get("/api/admin/competitions").json()["items"]
        competition = next(item for item in competitions if item["slug"] == "daily-4v4")
        topic = competition["topics"][0]
        forbidden = normal.patch(
            f"/api/admin/competitions/{competition['id']}/topics/{topic['id']}",
            headers=csrf(normal),
            json={"is_active": False},
        )
        assert forbidden.status_code == 403
        disabled = admin.patch(
            f"/api/admin/competitions/{competition['id']}/topics/{topic['id']}",
            headers=csrf(admin),
            json={"is_active": False},
        )
        assert disabled.status_code == 200 and disabled.json()["topic"]["is_active"] is False
        public_topics = client.get("/api/competitions/daily-4v4").json()["competition"]["topics"]
        assert topic["id"] not in {item["id"] for item in public_topics}
        restored = admin.patch(
            f"/api/admin/competitions/{competition['id']}/topics/{topic['id']}",
            headers=csrf(admin),
            json={"is_active": True},
        )
        assert restored.status_code == 200

        agent = admin.get("/api/admin/agents").json()["items"][0]
        rejected_model = admin.patch(
            f"/api/admin/agents/{agent['id']}",
            headers=csrf(admin),
            json={"model_name": "Qwen3-8B-Instruct"},
        )
        assert rejected_model.status_code == 422
        disabled_agent = admin.patch(
            f"/api/admin/agents/{agent['id']}",
            headers=csrf(admin),
            json={"is_active": False},
        )
        assert disabled_agent.status_code == 200 and disabled_agent.json()["agent"]["is_active"] is False
        restored_agent = admin.patch(
            f"/api/admin/agents/{agent['id']}",
            headers=csrf(admin),
            json={"is_active": True},
        )
        assert restored_agent.status_code == 200

        active_room = create_training_room(normal, "活跃比赛中的 AI 配置不可热修改")
        normal.post(f"/api/rooms/{active_room['code']}/ready", headers=csrf(normal), json={"ready": True})
        normal.post(f"/api/rooms/{active_room['code']}/start", headers=csrf(normal), json={})
        with SessionLocal() as db:
            room = load_room(db, active_room["code"])
            assigned_profile_id = next(seat.agent_profile_id for seat in room.seats if seat.agent_profile_id)
        blocked_edit = admin.patch(
            f"/api/admin/agents/{assigned_profile_id}",
            headers=csrf(admin),
            json={"voice_id": "debate_voice_4"},
        )
        assert blocked_edit.status_code == 409 and "活跃房间" in blocked_edit.json()["detail"]

        with SessionLocal() as db:
            profiles = list(db.scalars(select(AgentProfile)).all())
            for profile in profiles:
                profile.is_active = profile.id == assigned_profile_id
            db.commit()
        last_active = admin.patch(
            f"/api/admin/agents/{assigned_profile_id}",
            headers=csrf(admin),
            json={"is_active": False},
        )
        assert last_active.status_code == 409 and "至少保留一个" in last_active.json()["detail"]
        with SessionLocal() as db:
            for profile in db.scalars(select(AgentProfile)).all():
                profile.is_active = True
            db.commit()
    with SessionLocal() as db:
        actions = set(db.scalars(select(AdminAuditLog.action).where(AdminAuditLog.action.in_(["topic.patch", "agent.patch"]))).all())
        assert actions == {"topic.patch", "agent.patch"}


async def test_admin_audio_cue_is_reused_by_new_rooms_and_can_fall_back_to_lighttts(client: TestClient, register_user, monkeypatch) -> None:
    normal = register_user("audio_cue_normal")
    admin = TestClient(client.app)
    cue_id = ""
    with admin:
        assert admin.post("/api/auth/login", json={"account": "admin_test", "password": "Admin-test-1234"}).status_code == 200
        forbidden = normal.post(
            "/api/admin/audio-cues",
            headers=csrf(normal),
            data={"key": "opening_preset_test", "name": "测试开场", "text": "欢迎进入比赛"},
            files={"audio": ("opening.wav", _wav_bytes(0.4), "audio/wav")},
        )
        assert forbidden.status_code == 403
        invalid = admin.post(
            "/api/admin/audio-cues",
            headers=csrf(admin),
            data={"key": "Bad-Key", "name": "错误 key", "text": "测试"},
            files={"audio": ("bad.wav", b"not-wave", "audio/wav")},
        )
        assert invalid.status_code in {415, 422}
        created = admin.post(
            "/api/admin/audio-cues",
            headers=csrf(admin),
            data={"key": "opening_preset_test", "name": "测试开场", "text": "欢迎进入比赛"},
            files={"audio": ("opening.wav", _wav_bytes(0.4), "audio/wav")},
        )
        assert created.status_code == 200, created.text
        cue = created.json()["audio_cue"]
        cue_id = cue["id"]
        assert cue["audio_url"].endswith(f"/{cue_id}.wav") and cue["is_active"] is True
        listed = admin.get("/api/admin/audio-cues").json()["items"]
        assert any(item["id"] == cue_id for item in listed)

        owner = register_user("audio_cue_owner")
        room_data = create_training_room(owner, "预设语音应跳过 LightTTS")
        code = room_data["code"]
        owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})
        owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
        with SessionLocal() as db:
            room = load_room(db, code, lock=True)
            room.template_snapshot = [
                {
                    "key": "opening_preset_test",
                    "name": "测试开场",
                    "kind": "announcement",
                    "duration": 30,
                    "cue": "欢迎进入比赛",
                }
            ]
            db.commit()

        async def must_not_synthesize(*args, **kwargs):
            raise AssertionError("active preset cue should skip LightTTS")

        monkeypatch.setattr(lighttts, "synthesize", must_not_synthesize)
        await match_engine.process_room(code)
        with SessionLocal() as db:
            room = load_room(db, code)
            room.status = "running"
            match = db.scalar(select(Match).where(Match.room_id == room.id))
            asset = db.scalar(select(AudioAsset).where(AudioAsset.match_id == match.id, AudioAsset.kind == "cue:opening_preset_test"))
            event = db.scalar(
                select(MatchEvent).where(
                    MatchEvent.room_id == room.id,
                    MatchEvent.event_type == "audio.cue.ready",
                )
            )
            assert asset and (settings.media_path / code / f"{asset.id}.wav").is_file()
            assert event and event.payload["source"] == "preset"

        disabled = admin.patch(
            f"/api/admin/audio-cues/{cue_id}",
            headers=csrf(admin),
            json={"is_active": False},
        )
        assert disabled.status_code == 200 and disabled.json()["audio_cue"]["is_active"] is False

        fallback_owner = register_user("audio_cue_fallback")
        fallback_room = create_training_room(fallback_owner, "停用预设后应回退 LightTTS")
        fallback_code = fallback_room["code"]
        fallback_owner.post(f"/api/rooms/{fallback_code}/ready", headers=csrf(fallback_owner), json={"ready": True})
        fallback_owner.post(f"/api/rooms/{fallback_code}/start", headers=csrf(fallback_owner), json={})
        with SessionLocal() as db:
            room = load_room(db, fallback_code, lock=True)
            room.template_snapshot = [
                {
                    "key": "opening_preset_test",
                    "name": "测试开场",
                    "kind": "announcement",
                    "duration": 30,
                    "cue": "欢迎进入比赛",
                }
            ]
            db.commit()
        synthesized = 0

        async def fallback_synthesize(*args, room_code: str, speech_id: str, **kwargs):
            nonlocal synthesized
            assert kwargs["background"] is True
            synthesized += 1
            return f"/media/{room_code}/{speech_id}.wav"

        monkeypatch.setattr(lighttts, "synthesize", fallback_synthesize)
        await match_engine.process_room(fallback_code)
        assert synthesized == 1

    with SessionLocal() as db:
        actions = set(
            db.scalars(
                select(AdminAuditLog.action).where(
                    AdminAuditLog.target_id == cue_id,
                    AdminAuditLog.action.in_(["audio_cue.create", "audio_cue.patch"]),
                )
            ).all()
        )
        assert actions == {"audio_cue.create", "audio_cue.patch"}
        cue = db.get(AudioCue, cue_id)
        assert cue and cue.audio_url.removeprefix("/media/") in media_storage.referenced_media(db)
        cue_path = settings.media_path / "_cues" / f"{cue.id}.wav"
        db.delete(cue)
        db.commit()
        cue_path.unlink(missing_ok=True)


def test_admin_judge_profiles_are_single_active_versioned_and_frozen_per_match(client: TestClient, register_user) -> None:
    normal = register_user("judge_profile_normal")
    with SessionLocal() as db:
        for profile in db.scalars(select(JudgeProfile)).all():
            profile.is_active = False
        db.commit()
    admin = TestClient(client.app)
    with admin:
        assert admin.post("/api/auth/login", json={"account": "admin_test", "password": "Admin-test-1234"}).status_code == 200
        forbidden = normal.post(
            "/api/admin/judges",
            headers=csrf(normal),
            json={"name": "越权裁判", "endpoint": "http://judge.test/forbidden", "model_name": "judge-model"},
        )
        assert forbidden.status_code == 403
        invalid_endpoint = admin.post(
            "/api/admin/judges",
            headers=csrf(admin),
            json={"name": "错误地址", "endpoint": "ftp://judge.test", "model_name": "judge-model"},
        )
        assert invalid_endpoint.status_code == 422
        forbidden_model = admin.post(
            "/api/admin/judges",
            headers=csrf(admin),
            json={"name": "禁用模型", "endpoint": "http://judge.test/api", "model_name": "Qwen3-8B"},
        )
        assert forbidden_model.status_code == 422

        first_response = admin.post(
            "/api/admin/judges",
            headers=csrf(admin),
            json={
                "name": "训练裁判 A",
                "endpoint": "http://judge-a.test/api/judge",
                "model_name": "judge-a",
                "system_prompt": "依据论证结构评分",
                "timeout_seconds": 90,
                "is_active": False,
            },
        )
        assert first_response.status_code == 200, first_response.text
        first = first_response.json()["judge"]
        assert first["is_active"] is True

        second_response = admin.post(
            "/api/admin/judges",
            headers=csrf(admin),
            json={
                "name": "正式裁判 B",
                "endpoint": "http://judge-b.test/api/judge",
                "model_name": "judge-b",
                "system_prompt": "依据回应能力和事实准确性评分",
                "timeout_seconds": 150,
                "is_active": True,
            },
        )
        assert second_response.status_code == 200
        second = second_response.json()["judge"]
        listed = admin.get("/api/admin/judges").json()["items"]
        assert sum(item["is_active"] for item in listed) == 1
        assert next(item for item in listed if item["id"] == second["id"])["is_active"] is True
        duplicate = admin.post(
            "/api/admin/judges",
            headers=csrf(admin),
            json={"name": "正式裁判 B", "endpoint": "http://judge-c.test/api", "model_name": "judge-c"},
        )
        assert duplicate.status_code == 409

        owner = register_user("judge_snapshot_owner")
        room_data = create_training_room(owner, "裁判配置快照不得热切换")
        code = room_data["code"]
        owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})
        assert owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={}).status_code == 200
        with SessionLocal() as db:
            room = load_room(db, code)
            room.status = "running"
            match = db.scalar(select(Match).where(Match.room_id == room.id))
            assert match.judge_profile_id == second["id"]
            assert match.judge_snapshot == {
                "id": second["id"],
                "name": "正式裁判 B",
                "endpoint": "http://judge-b.test/api/judge",
                "model_name": "judge-b",
                "system_prompt": "依据回应能力和事实准确性评分",
                "timeout_seconds": 150,
            }

        blocked_edit = admin.patch(
            f"/api/admin/judges/{second['id']}",
            headers=csrf(admin),
            json={"endpoint": "http://judge-b.test/api/judge-v2"},
        )
        assert blocked_edit.status_code == 409 and code in blocked_edit.json()["detail"]
        activated = admin.patch(
            f"/api/admin/judges/{first['id']}",
            headers=csrf(admin),
            json={"is_active": True},
        )
        assert activated.status_code == 200 and activated.json()["judge"]["is_active"] is True

        future_owner = register_user("judge_snapshot_future")
        future_room = create_training_room(future_owner, "新比赛使用新启用裁判")
        future_code = future_room["code"]
        future_owner.post(f"/api/rooms/{future_code}/ready", headers=csrf(future_owner), json={"ready": True})
        future_owner.post(f"/api/rooms/{future_code}/start", headers=csrf(future_owner), json={})
        with SessionLocal() as db:
            room = load_room(db, future_code)
            match = db.scalar(select(Match).where(Match.room_id == room.id))
            assert match.judge_profile_id == first["id"] and match.judge_snapshot["model_name"] == "judge-a"

        assert owner.post(f"/api/rooms/{code}/control/terminate", headers=csrf(owner), json={"reason": "测试结束"}).status_code == 200
        allowed_edit = admin.patch(
            f"/api/admin/judges/{second['id']}",
            headers=csrf(admin),
            json={"endpoint": "http://judge-b.test/api/judge-v2"},
        )
        assert allowed_edit.status_code == 200
        with SessionLocal() as db:
            room = load_room(db, code)
            match = db.scalar(select(Match).where(Match.room_id == room.id))
            assert match.judge_snapshot["endpoint"] == "http://judge-b.test/api/judge"
        assert (
            future_owner.post(
                f"/api/rooms/{future_code}/control/terminate",
                headers=csrf(future_owner),
                json={"reason": "测试结束"},
            ).status_code
            == 200
        )

    with SessionLocal() as db:
        actions = set(
            db.scalars(
                select(AdminAuditLog.action).where(
                    AdminAuditLog.target_type == "judge_profile",
                    AdminAuditLog.action.in_(["judge_profile.create", "judge_profile.patch"]),
                )
            ).all()
        )
        assert actions == {"judge_profile.create", "judge_profile.patch"}
        assert db.scalar(select(JudgeProfile).where(JudgeProfile.is_active.is_(True))).id == first["id"]


def test_admin_speech_provider_configs_are_validated_and_frozen_per_match(client: TestClient, register_user) -> None:
    normal = register_user("provider_config_normal")
    admin = TestClient(client.app)
    with admin:
        assert admin.post("/api/auth/login", json={"account": "admin_test", "password": "Admin-test-1234"}).status_code == 200
        forbidden = normal.put(
            "/api/admin/providers/funasr",
            headers=csrf(normal),
            json={"endpoint": "ws://asr.test/ws", "settings": {}, "is_active": True},
        )
        assert forbidden.status_code == 403
        invalid_kind = admin.put(
            "/api/admin/providers/unknown",
            headers=csrf(admin),
            json={"endpoint": "http://service.test", "settings": {}, "is_active": True},
        )
        assert invalid_kind.status_code == 422
        invalid_scheme = admin.put(
            "/api/admin/providers/funasr",
            headers=csrf(admin),
            json={"endpoint": "http://asr.test", "settings": {}, "is_active": True},
        )
        assert invalid_scheme.status_code == 422
        invalid_setting = admin.put(
            "/api/admin/providers/lighttts",
            headers=csrf(admin),
            json={"endpoint": "http://tts.test", "settings": {"max_active": 99}, "is_active": True},
        )
        assert invalid_setting.status_code == 422

        asr_a = admin.put(
            "/api/admin/providers/funasr",
            headers=csrf(admin),
            json={
                "endpoint": "ws://asr-a.test:10095",
                "settings": {"final_wait_seconds": 20.5},
                "is_active": True,
            },
        )
        tts_a = admin.put(
            "/api/admin/providers/lighttts",
            headers=csrf(admin),
            json={
                "endpoint": "http://tts-a.test/inference",
                "settings": {"read_timeout_seconds": 90, "speed": 1.15},
                "is_active": True,
            },
        )
        assert asr_a.status_code == 200 and tts_a.status_code == 200

        owner = register_user("provider_snapshot_owner")
        first_room = create_training_room(owner, "语音服务配置快照甲")
        first_code = first_room["code"]
        owner.post(f"/api/rooms/{first_code}/ready", headers=csrf(owner), json={"ready": True})
        owner.post(f"/api/rooms/{first_code}/start", headers=csrf(owner), json={})

        asr_b = admin.put(
            "/api/admin/providers/funasr",
            headers=csrf(admin),
            json={
                "endpoint": "wss://asr-b.test/ws",
                "settings": {"final_wait_seconds": 40},
                "is_active": True,
            },
        )
        tts_b = admin.put(
            "/api/admin/providers/lighttts",
            headers=csrf(admin),
            json={
                "endpoint": "https://tts-b.test/inference",
                "settings": {"read_timeout_seconds": 150, "speed": 0.9},
                "is_active": False,
            },
        )
        assert asr_b.status_code == 200 and tts_b.status_code == 200

        future_owner = register_user("provider_snapshot_future")
        second_room = create_training_room(future_owner, "语音服务配置快照乙")
        second_code = second_room["code"]
        future_owner.post(f"/api/rooms/{second_code}/ready", headers=csrf(future_owner), json={"ready": True})
        future_owner.post(f"/api/rooms/{second_code}/start", headers=csrf(future_owner), json={})

        with SessionLocal() as db:
            first = load_room(db, first_code)
            first_match = db.scalar(select(Match).where(Match.room_id == first.id))
            second = load_room(db, second_code)
            second_match = db.scalar(select(Match).where(Match.room_id == second.id))
            assert first_match.service_snapshot["funasr"]["endpoint"] == "ws://asr-a.test:10095"
            assert first_match.service_snapshot["lighttts"]["settings"]["speed"] == 1.15
            assert second_match.service_snapshot["funasr"]["endpoint"] == "wss://asr-b.test/ws"
            assert second_match.service_snapshot["lighttts"]["endpoint"] == "https://tts-b.test/inference"
            assert second_match.service_snapshot["lighttts"]["enabled"] is False

        dashboard = admin.get("/api/admin/dashboard")
        assert dashboard.status_code == 200
        assert dashboard.json()["providers"]["funasr"]["endpoint"] == "wss://asr-b.test/ws"
        assert dashboard.json()["providers"]["lighttts"]["enabled"] is False

        assert owner.post(f"/api/rooms/{first_code}/control/terminate", headers=csrf(owner), json={"reason": "测试结束"}).status_code == 200
        assert (
            future_owner.post(
                f"/api/rooms/{second_code}/control/terminate",
                headers=csrf(future_owner),
                json={"reason": "测试结束"},
            ).status_code
            == 200
        )
        admin.put(
            "/api/admin/providers/funasr",
            headers=csrf(admin),
            json={
                "endpoint": settings.funasr_ws_url,
                "settings": {"final_wait_seconds": 30},
                "is_active": True,
            },
        )
        admin.put(
            "/api/admin/providers/lighttts",
            headers=csrf(admin),
            json={
                "endpoint": settings.lighttts_url,
                "settings": {"read_timeout_seconds": 180, "speed": 1},
                "is_active": True,
            },
        )

    with SessionLocal() as db:
        assert {item.kind for item in db.scalars(select(ProviderConfig)).all()} == {"agent", "funasr", "lighttts"}
        actions = set(
            db.scalars(
                select(AdminAuditLog.action).where(
                    AdminAuditLog.target_type == "provider_config",
                    AdminAuditLog.action.in_(["provider_config.create", "provider_config.patch"]),
                )
            ).all()
        )
        assert "provider_config.patch" in actions


def test_admin_restful_agent_gateway_encrypts_redacts_and_freezes_secret(client: TestClient, register_user) -> None:
    admin = TestClient(client.app)
    normal = register_user("agent_gateway_normal")
    secret = "gateway-secret-that-must-never-leak"
    endpoint = "https://117.50.218.251/debate/api/debate"
    health_endpoint = "https://117.50.218.251/debate/api/health"
    with admin:
        assert admin.post("/api/auth/login", json={"account": "admin_test", "password": "Admin-test-1234"}).status_code == 200
        forbidden = normal.put(
            "/api/admin/providers/agent",
            headers=csrf(normal),
            json={"endpoint": endpoint, "settings": {}, "is_active": True},
        )
        assert forbidden.status_code == 403
        invalid = admin.put(
            "/api/admin/providers/agent",
            headers=csrf(admin),
            json={"endpoint": endpoint, "settings": {"method": "GET"}, "is_active": True},
        )
        assert invalid.status_code == 422
        saved = admin.put(
            "/api/admin/providers/agent",
            headers=csrf(admin),
            json={
                "endpoint": endpoint,
                "settings": {
                    "method": "POST",
                    "protocol": "restful",
                    "health_endpoint": health_endpoint,
                    "timeout_seconds": 90,
                    "stream": True,
                },
                "secret": secret,
                "is_active": True,
            },
        )
        assert saved.status_code == 200
        assert saved.json()["provider"]["has_secret"] is True
        assert secret not in saved.text
        listed = admin.get("/api/admin/providers")
        assert listed.status_code == 200 and secret not in listed.text

        owner = register_user("agent_gateway_snapshot")
        room = create_training_room(owner, "RESTful Agent 服务快照测试")
        owner.post(f"/api/rooms/{room['code']}/ready", headers=csrf(owner), json={"ready": True})
        assert owner.post(f"/api/rooms/{room['code']}/start", headers=csrf(owner), json={}).status_code == 200

        with SessionLocal() as db:
            config = db.scalar(select(ProviderConfig).where(ProviderConfig.kind == "agent"))
            match = db.scalar(select(Match).join(Room, Match.room_id == Room.id).where(Room.code == room["code"]))
            assert config.secret_ciphertext and config.secret_ciphertext != secret
            assert match.service_snapshot["agent"]["endpoint"] == endpoint
            assert match.service_snapshot["agent"]["secret_ciphertext"] == config.secret_ciphertext
            audit_payloads = db.scalars(
                select(AdminAuditLog.payload).where(
                    AdminAuditLog.target_type == "provider_config",
                    AdminAuditLog.action.in_(["provider_config.create", "provider_config.patch"]),
                )
            ).all()
            assert secret not in json.dumps(audit_payloads, ensure_ascii=False)
            from app.services.match_archive import _archive_source

            archive = _archive_source(db, match.id)
            assert "secret_ciphertext" not in archive["match"]["service_snapshot"]["agent"]

        terminated = owner.post(
            f"/api/rooms/{room['code']}/control/terminate",
            headers=csrf(owner),
            json={"reason": "测试结束"},
        )
        assert terminated.status_code == 200
        restored = admin.put(
            "/api/admin/providers/agent",
            headers=csrf(admin),
            json={
                "endpoint": settings.agent_api_url,
                "settings": {
                    "method": "POST",
                    "protocol": "restful",
                    "timeout_seconds": settings.agent_timeout_seconds,
                    "stream": True,
                },
                "clear_secret": True,
                "is_active": bool(settings.agent_api_url),
            },
        )
        assert restored.status_code == 200


def test_automation_template_versioning_preserves_existing_rooms_and_survives_seed(client: TestClient, register_user) -> None:
    existing_owner = register_user("template_existing")
    future_owner = register_user("template_future")
    existing_room = create_training_room(existing_owner, "旧房间应固定旧流程快照")

    admin = TestClient(client.app)
    with admin:
        assert admin.post("/api/auth/login", json={"account": "admin_test", "password": "Admin-test-1234"}).status_code == 200
        competitions = admin.get("/api/admin/competitions").json()["items"]
        training = next(item for item in competitions if item["slug"] == "training-1v1")
        templates = admin.get("/api/admin/automation-templates").json()["items"]
        source = next(item for item in templates if any(bound["id"] == training["id"] for bound in item["competitions"]))
        stages = [dict(stage) for stage in source["stages"]]
        stages[1]["duration"] += 15
        invalid = admin.post(
            f"/api/admin/automation-templates/{source['id']}/versions",
            headers=csrf(admin),
            json={"stages": stages[:-1], "competition_ids": [training["id"]]},
        )
        assert invalid.status_code == 422
        typo_stages = [dict(stage) for stage in stages]
        typo_stages[1]["duraton"] = typo_stages[1]["duration"]
        typo = admin.post(
            f"/api/admin/automation-templates/{source['id']}/versions",
            headers=csrf(admin),
            json={"stages": typo_stages, "competition_ids": [training["id"]]},
        )
        assert typo.status_code == 422
        double_judging = [dict(stage) for stage in stages]
        double_judging.insert(-1, {"key": "early_judge", "name": "错误的提前裁判", "kind": "judging", "duration": 10})
        premature = admin.post(
            f"/api/admin/automation-templates/{source['id']}/versions",
            headers=csrf(admin),
            json={"stages": double_judging, "competition_ids": [training["id"]]},
        )
        assert premature.status_code == 422
        created = admin.post(
            f"/api/admin/automation-templates/{source['id']}/versions",
            headers=csrf(admin),
            json={"name": "1v1 自动训练流程验收版", "stages": stages, "competition_ids": [training["id"]]},
        )
        assert created.status_code == 200, created.text
        created_template = created.json()["template"]
        assert created_template["version"] == source["version"] + 1
        assert created_template["stages"][1]["duration"] == stages[1]["duration"]
        replayed = admin.post(
            f"/api/admin/automation-templates/{source['id']}/versions",
            headers=csrf(admin),
            json={"name": "1v1 自动训练流程验收版", "stages": stages, "competition_ids": [training["id"]]},
        )
        assert replayed.status_code == 200 and replayed.json()["replayed"] is True
        assert replayed.json()["template"]["id"] == created_template["id"]

    with SessionLocal() as db:
        stored_existing = load_room(db, existing_room["code"])
        assert stored_existing.template_snapshot[1]["duration"] != stages[1]["duration"]
        competition = db.get(Competition, training["id"])
        assert competition.automation_template_id == created_template["id"]
        seed_database(db)
        db.refresh(competition)
        assert competition.automation_template_id == created_template["id"]

    future_room = create_training_room(future_owner, "新房间应使用新版流程快照")
    with SessionLocal() as db:
        stored_future = load_room(db, future_room["code"])
        assert stored_future.template_snapshot[1]["duration"] == stages[1]["duration"]
        log = db.scalar(
            select(AdminAuditLog).where(
                AdminAuditLog.action == "automation_template.version_create",
                AdminAuditLog.target_id == created_template["id"],
            )
        )
        assert log and log.payload["source_template_id"] == source["id"]


def test_admin_lists_are_searchable_paginated_and_auditable(client: TestClient, register_user) -> None:
    first = register_user("admin_page_first")
    register_user("admin_page_second")
    register_user("admin_page_third")
    first_user = first.get("/api/auth/session").json()["user"]
    room = create_training_room(first, "后台房间分页检索测试")
    with SessionLocal() as db:
        admin_user = db.scalar(select(User).where(User.account == "admin_test"))
        retired_audit = AdminAuditLog(
            actor_user_id=admin_user.id,
            action="classroom.member.upsert",
            target_type="classroom_membership",
            target_id=str(uuid.uuid4()),
            payload={"retired": True},
        )
        db.add(retired_audit)
        db.commit()
        retired_audit_id = retired_audit.id
    admin = TestClient(client.app)
    try:
        with admin:
            admin.post("/api/auth/login", json={"account": "admin_test", "password": "Admin-test-1234"})
            first_page = admin.get("/api/admin/users", params={"page": 1, "page_size": 2})
            assert first_page.status_code == 200
            assert len(first_page.json()["items"]) == 2
            assert first_page.json()["pagination"]["total"] >= 4
            found_user = admin.get("/api/admin/users", params={"q": first_user["account"]}).json()
            assert found_user["pagination"]["total"] == 1 and found_user["items"][0]["id"] == first_user["id"]

            found_room = admin.get("/api/admin/rooms", params={"q": room["code"], "status": "lobby"}).json()
            assert found_room["pagination"]["total"] == 1 and found_room["items"][0]["code"] == room["code"]
            assert admin.get("/api/admin/rooms", params={"page": 0}).status_code == 422

            admin.patch(
                f"/api/admin/users/{first_user['id']}",
                headers=csrf(admin),
                json={"is_active": False},
            ).raise_for_status()
            audit_page = admin.get("/api/admin/audit", params={"q": "user.patch", "page_size": 1}).json()
            assert audit_page["pagination"]["total"] >= 1
            assert len(audit_page["items"]) == 1
            assert audit_page["items"][0]["actor_name"] == "测试管理员"
            assert audit_page["items"][0]["payload"]["is_active"] is False
            retired_page = admin.get("/api/admin/audit", params={"q": "classroom"}).json()
            assert retired_page["pagination"]["total"] == 0
            assert not retired_page["items"]
            admin.patch(
                f"/api/admin/users/{first_user['id']}",
                headers=csrf(admin),
                json={"is_active": True},
            ).raise_for_status()
    finally:
        with SessionLocal() as db:
            db.query(AdminAuditLog).filter(AdminAuditLog.id == retired_audit_id).delete()
            db.commit()


def test_current_admin_cannot_disable_or_demote_self(client: TestClient, register_user) -> None:
    candidate = register_user("admin_candidate")
    candidate_user = candidate.get("/api/auth/session").json()["user"]
    admin = TestClient(client.app)
    with admin:
        assert admin.post("/api/auth/login", json={"account": "admin_test", "password": "Admin-test-1234"}).status_code == 200
        current = admin.get("/api/auth/session").json()["user"]
        assert admin.patch(f"/api/admin/users/{current['id']}", headers=csrf(admin), json={"is_active": False}).status_code == 409
        assert admin.patch(f"/api/admin/users/{current['id']}", headers=csrf(admin), json={"role": "user"}).status_code == 409
        promoted = admin.patch(f"/api/admin/users/{candidate_user['id']}", headers=csrf(admin), json={"role": "system_admin"})
        assert promoted.status_code == 200 and promoted.json()["user"]["role"] == "system_admin"
        assert candidate.get("/api/auth/session").status_code == 401
        assert (
            candidate.post(
                "/api/auth/login",
                json={"account": candidate_user["account"], "password": "Password-1234"},
            ).status_code
            == 200
        )
        assert candidate.get("/api/auth/session").json()["user"]["role"] == "system_admin"
        demoted = admin.patch(f"/api/admin/users/{candidate_user['id']}", headers=csrf(admin), json={"role": "user"})
        assert demoted.status_code == 200 and demoted.json()["user"]["role"] == "user"
        assert candidate.get("/api/auth/session").status_code == 401
        assert (
            candidate.post(
                "/api/auth/login",
                json={"account": candidate_user["account"], "password": "Password-1234"},
            ).status_code
            == 200
        )
        assert candidate.get("/api/auth/session").json()["user"]["role"] == "user"
        assert admin.get("/api/admin/dashboard").status_code == 200


def test_admin_password_reset_revokes_sessions_without_auditing_secret(client: TestClient, register_user) -> None:
    victim = register_user("reset_victim")
    victim_user = victim.get("/api/auth/session").json()["user"]
    forbidden = victim.post(
        f"/api/admin/users/{victim_user['id']}/reset-password",
        headers=csrf(victim),
        json={"new_password": "Reset-password-8765", "confirm_password": "Reset-password-8765"},
    )
    assert forbidden.status_code == 403

    admin = TestClient(client.app)
    with admin:
        assert admin.post("/api/auth/login", json={"account": "admin_test", "password": "Admin-test-1234"}).status_code == 200
        admin_user = admin.get("/api/auth/session").json()["user"]
        assert (
            admin.post(
                f"/api/admin/users/{admin_user['id']}/reset-password",
                headers=csrf(admin),
                json={"new_password": "Self-reset-1234", "confirm_password": "Self-reset-1234"},
            ).status_code
            == 409
        )
        mismatched = admin.post(
            f"/api/admin/users/{victim_user['id']}/reset-password",
            headers=csrf(admin),
            json={"new_password": "Reset-password-8765", "confirm_password": "Different-password-1"},
        )
        assert mismatched.status_code == 422
        reset = admin.post(
            f"/api/admin/users/{victim_user['id']}/reset-password",
            headers=csrf(admin),
            json={"new_password": "Reset-password-8765", "confirm_password": "Reset-password-8765"},
        )
        assert reset.status_code == 200 and reset.json()["revoked_sessions"] == 1

    assert victim.get("/api/auth/session").status_code == 401
    assert (
        victim.post(
            "/api/auth/login",
            json={"account": victim_user["account"], "password": "Password-1234"},
        ).status_code
        == 401
    )
    assert (
        victim.post(
            "/api/auth/login",
            json={"account": victim_user["account"], "password": "Reset-password-8765"},
        ).status_code
        == 200
    )
    with SessionLocal() as db:
        log = db.scalar(
            select(AdminAuditLog)
            .where(AdminAuditLog.action == "user.password_reset", AdminAuditLog.target_id == victim_user["id"])
            .order_by(AdminAuditLog.created_at.desc())
        )
        assert log and log.payload == {"revoked_sessions": 1}
        assert "password" not in json.dumps(log.payload).lower()


def test_review_requires_review_state_and_is_applied_once(client: TestClient, register_user) -> None:
    owner = register_user("ranked_review_owner")
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
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        room.status = "judging"
        room.current_stage_index = len(room.template_snapshot) - 1
        match.status = "running"
        scorecard = JudgeScorecard(match_id=match.id, status="running", reasoning="自动裁判仍在执行")
        db.add(scorecard)
        if not db.scalar(select(JudgeProfile).where(JudgeProfile.is_active.is_(True))):
            db.add(
                JudgeProfile(
                    name="恢复裁判",
                    endpoint="http://judge.test/api/judge",
                    model_name="judge-test",
                    timeout_seconds=120,
                    is_active=True,
                )
            )
        db.commit()
        scorecard_id = scorecard.id
        review_revision = scorecard.updated_at.isoformat()

    review_payload = {
        "winner": "aff",
        "affirmative_score": 90,
        "negative_score": 82,
        "reasoning": "管理员确认正方获胜。",
        "expected_updated_at": review_revision,
    }
    admin = TestClient(client.app)
    with admin:
        assert admin.post("/api/auth/login", json={"account": "admin_test", "password": "Admin-test-1234"}).status_code == 200
        premature = admin.post(f"/api/admin/reviews/{scorecard_id}/approve", headers=csrf(admin), json=review_payload)
        assert premature.status_code == 409
        with SessionLocal() as db:
            room = load_room(db, code, lock=True)
            match = db.scalar(select(Match).where(Match.room_id == room.id))
            scorecard = db.get(JudgeScorecard, scorecard_id)
            room.status = "review_required"
            match.status = "review_required"
            scorecard.status = "review_required"
            db.commit()
            review_payload["expected_updated_at"] = scorecard.updated_at.isoformat()
        retried = admin.post(
            f"/api/admin/reviews/{scorecard_id}/retry",
            headers=csrf(admin),
            json={"expected_updated_at": review_payload["expected_updated_at"]},
        )
        stale_retry = admin.post(
            f"/api/admin/reviews/{scorecard_id}/retry",
            headers=csrf(admin),
            json={"expected_updated_at": review_payload["expected_updated_at"]},
        )
        assert retried.status_code == 202, retried.text
        assert stale_retry.status_code == 409
        with SessionLocal() as db:
            room = load_room(db, code, lock=True)
            match = db.scalar(select(Match).where(Match.room_id == room.id))
            scorecard = db.get(JudgeScorecard, scorecard_id)
            active_judge = db.scalar(select(JudgeProfile).where(JudgeProfile.is_active.is_(True)))
            retry_event = db.scalar(
                select(MatchEvent).where(
                    MatchEvent.room_id == room.id,
                    MatchEvent.event_type == "judge.retry_requested",
                )
            )
            retry_audit = db.scalar(
                select(AdminAuditLog).where(
                    AdminAuditLog.action == "judge.retry",
                    AdminAuditLog.target_id == scorecard_id,
                )
            )
            assert active_judge
            assert room.status == match.status == "judging"
            assert scorecard.status == "running"
            assert match.judge_profile_id == active_judge.id
            assert match.judge_snapshot["endpoint"] == active_judge.endpoint
            assert retry_event and retry_event.payload["judge_profile_id"] == active_judge.id
            assert retry_audit and retry_audit.payload["judge_profile_id"] == active_judge.id
            room.status = "review_required"
            match.status = "review_required"
            scorecard.status = "review_required"
            scorecard.reasoning = "重试后仍需人工复核。"
            db.commit()
            db.refresh(scorecard)
            review_payload["expected_updated_at"] = scorecard.updated_at.isoformat()
        approved = admin.post(f"/api/admin/reviews/{scorecard_id}/approve", headers=csrf(admin), json=review_payload)
        repeated = admin.post(f"/api/admin/reviews/{scorecard_id}/approve", headers=csrf(admin), json=review_payload)
        assert approved.status_code == 200
        assert repeated.status_code == 409

        corrected_to_negative = {
            "winner": "neg",
            "affirmative_score": 80,
            "negative_score": 92,
            "reasoning": "复核录像后修正为反方获胜。",
            "expected_updated_at": next(
                item for item in admin.get("/api/admin/reviews").json()["recent"] if item["scorecard_id"] == scorecard_id
            )["updated_at"],
        }
        corrected = admin.post(
            f"/api/admin/reviews/{scorecard_id}/correct",
            headers=csrf(admin),
            json=corrected_to_negative,
        )
        duplicate_correction = admin.post(
            f"/api/admin/reviews/{scorecard_id}/correct",
            headers=csrf(admin),
            json=corrected_to_negative,
        )
        assert corrected.status_code == 200, corrected.text
        assert duplicate_correction.status_code == 409

        corrected_to_draw = {
            "winner": "draw",
            "affirmative_score": 85,
            "negative_score": 85,
            "reasoning": "进一步复核后确认双方表现相当。",
            "expected_updated_at": next(
                item for item in admin.get("/api/admin/reviews").json()["recent"] if item["scorecard_id"] == scorecard_id
            )["updated_at"],
        }
        corrected_again = admin.post(
            f"/api/admin/reviews/{scorecard_id}/correct",
            headers=csrf(admin),
            json=corrected_to_draw,
        )
        assert corrected_again.status_code == 200, corrected_again.text
        review_listing = admin.get("/api/admin/reviews").json()
        assert any(item["scorecard_id"] == scorecard_id and item["winner"] == "draw" for item in review_listing["recent"])

    owner_user = owner.get("/api/auth/session").json()["user"]
    owner_id = owner_user["id"]
    with SessionLocal() as db:
        room = load_room(db, code)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        changes = list(db.scalars(select(RatingChange).where(RatingChange.match_id == match.id, RatingChange.user_id == owner_id)).all())
        events = list(db.scalars(select(MatchEvent).where(MatchEvent.room_id == room.id, MatchEvent.event_type == "judge.reviewed")).all())
        correction_events = list(
            db.scalars(select(MatchEvent).where(MatchEvent.room_id == room.id, MatchEvent.event_type == "judge.corrected")).all()
        )
        audits = list(
            db.scalars(select(AdminAuditLog).where(AdminAuditLog.action == "judge.approve", AdminAuditLog.target_id == scorecard_id)).all()
        )
        correction_audits = list(
            db.scalars(select(AdminAuditLog).where(AdminAuditLog.action == "judge.correct", AdminAuditLog.target_id == scorecard_id)).all()
        )
        entry = db.scalar(
            select(LeaderboardEntry).where(
                LeaderboardEntry.competition_id == room.competition_id,
                LeaderboardEntry.season_id == match.season_id,
                LeaderboardEntry.user_id == owner_id,
            )
        )
        scorecard = db.get(JudgeScorecard, scorecard_id)
        assert room.status == match.status == "completed" and match.winner == "draw"
        assert scorecard and scorecard.winner == "draw" and scorecard.affirmative_score == scorecard.negative_score == 85
        assert len(events) == len(audits) == 1
        assert len(changes) == 3
        assert [(item.source, item.points_delta) for item in changes] == [
            ("initial", 3),
            ("correction", -3),
            ("correction", 1),
        ]
        assert len(correction_events) == len(correction_audits) == 2
        assert entry and entry.matches == 1 and entry.points == 1
        assert entry.wins == 0 and entry.losses == 0 and entry.draws == 1 and entry.average_score == 85

    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        owner_seat = next(item for item in room.seats if item.user_id == owner_id)
        owner_seat.occupant_type = "ai_substitute"
        owner_seat.display_name = f"AI 接替·{owner_seat.display_name}"
        db.commit()
    result = owner.get(f"/api/rooms/{code}/result").json()
    assert {item["display_name"] for item in result["rating_changes"]} == {owner_user["real_name"]}
    assert all(item["user_id"] == owner_id for item in result["rating_changes"])
    public_result = client.get(f"/api/rooms/{code}/result").json()
    assert all("user_id" not in item for item in public_result["rating_changes"])
    public_match_result = client.get(f"/api/matches/{public_result['match']['id']}/result").json()
    assert all("user_id" not in item for item in public_match_result["rating_changes"])
    owner_match_result = owner.get(f"/api/matches/{public_result['match']['id']}/result").json()
    assert all(item["user_id"] == owner_id for item in owner_match_result["rating_changes"])
    public_correction = next(item for item in public_result["events"] if item["type"] == "judge.corrected")
    assert public_correction["payload"]["old"]["winner"] in {"aff", "neg"}
    assert public_correction["payload"]["new"]["winner"] in {"neg", "draw"}
    assert "reasoning" not in public_correction["payload"]["old"]


def test_admin_data_quality_separates_production_coverage_from_qa_data(client: TestClient, register_user) -> None:
    baseline_admin = TestClient(client.app)
    with baseline_admin:
        assert baseline_admin.post(
            "/api/auth/login",
            json={"account": "admin_test", "password": "Admin-test-1234"},
        ).status_code == 200
        baseline = baseline_admin.get("/api/admin/data-quality").json()
    healthy_owner = register_user("data_quality_healthy")
    broken_owner = register_user("data_quality_broken")
    qa_owner = register_user("data_quality_qa")
    healthy = create_training_room(healthy_owner, "数据质量完整比赛")
    broken = create_training_room(broken_owner, "数据质量缺失比赛")
    qa = create_training_room(qa_owner, "数据质量 QA 比赛")
    for owner, room in ((healthy_owner, healthy), (broken_owner, broken), (qa_owner, qa)):
        assert owner.post(f"/api/rooms/{room['code']}/ready", headers=csrf(owner), json={"ready": True}).status_code == 200
        assert owner.post(f"/api/rooms/{room['code']}/start", headers=csrf(owner), json={}).status_code == 200

    with SessionLocal.begin() as db:
        rooms = {
            item.code: item
            for item in db.scalars(select(Room).where(Room.code.in_([healthy["code"], broken["code"], qa["code"]]))).all()
        }
        matches = {
            item.room_id: item
            for item in db.scalars(select(Match).where(Match.room_id.in_([item.id for item in rooms.values()]))).all()
        }
        for room in rooms.values():
            room.status = "completed"
            room.completed_at = now()
            matches[room.id].status = "completed"
            matches[room.id].winner = "aff"
        rooms[qa["code"]].is_test_data = True

        healthy_match = matches[rooms[healthy["code"]].id]
        healthy_speech = Speech(
            match_id=healthy_match.id,
            room_id=rooms[healthy["code"]].id,
            seat_key="aff_1",
            stage_key="aff_case",
            speaker_type="human",
            status="completed",
            content="这是一段完整保存的真人发言。",
            audio_url=f"/media/{healthy['code']}/healthy.wav",
            duration_seconds=8,
        )
        db.add(healthy_speech)
        db.flush()
        db.add(TranscriptSegment(speech_id=healthy_speech.id, start_ms=0, end_ms=8000, text=healthy_speech.content))
        db.add(
            JudgeScorecard(
                match_id=healthy_match.id,
                status="approved",
                winner="aff",
                affirmative_score=88,
                negative_score=82,
                individual_scores={"aff_1": 88, "neg_1": 82},
                reasoning="测试结论",
            )
        )

        broken_match = matches[rooms[broken["code"]].id]
        broken_speech = Speech(
            match_id=broken_match.id,
            room_id=rooms[broken["code"]].id,
            seat_key="aff_1",
            stage_key="aff_case",
            speaker_type="human",
            status="completed",
            content="",
            audio_url="",
        )
        db.add(broken_speech)
        db.flush()
        broken_match_id = broken_match.id
        broken_speech_id = broken_speech.id

        qa_match = matches[rooms[qa["code"]].id]
        db.add(
            Speech(
                match_id=qa_match.id,
                room_id=rooms[qa["code"]].id,
                seat_key="aff_1",
                stage_key="aff_case",
                speaker_type="human",
                status="completed",
                content="",
                audio_url="",
            )
        )

    admin = TestClient(client.app)
    with admin:
        assert admin.post("/api/auth/login", json={"account": "admin_test", "password": "Admin-test-1234"}).status_code == 200
        response = admin.get("/api/admin/data-quality")
        assert response.status_code == 200
        payload = response.json()
        assert payload["scope"] == "production"
        assert payload["matches"] == baseline["matches"] | {
            "total": baseline["matches"]["total"] + 2,
            "completed": baseline["matches"]["completed"] + 2,
        }
        assert payload["speeches"]["human_completed"] == baseline["speeches"]["human_completed"] + 2
        assert payload["speeches"]["human_with_transcript"] == baseline["speeches"]["human_with_transcript"] + 1
        assert payload["speeches"]["human_with_audio"] == baseline["speeches"]["human_with_audio"] + 1
        assert payload["speeches"]["ai_completed"] == baseline["speeches"]["ai_completed"]
        assert payload["speeches"]["transcript_coverage_percent"] == round(
            100
            * (baseline["speeches"]["human_with_transcript"] + 1)
            / (baseline["speeches"]["human_completed"] + 2),
            1,
        )
        assert payload["speeches"]["audio_coverage_percent"] == round(
            100
            * (baseline["speeches"]["human_with_audio"] + 1)
            / (baseline["speeches"]["human_completed"] + 2),
            1,
        )
        assert (
            payload["attention"]["published_without_scorecard"]
            == baseline["attention"]["published_without_scorecard"] + 1
        )
        assert (
            payload["attention"]["published_without_speeches"]
            == baseline["attention"]["published_without_speeches"]
        )
        assert payload["attention"]["human_missing_transcript"] == baseline["attention"]["human_missing_transcript"] + 1
        assert payload["attention"]["human_missing_audio"] == baseline["attention"]["human_missing_audio"] + 1
        assert payload["attention"]["human_missing_segments"] == baseline["attention"]["human_missing_segments"] + 1
        broken_sample = next(item for item in payload["attention"]["samples"] if item["speech_id"] == broken_speech_id)
        assert broken_sample == {
            "room_code": broken["code"],
            "match_id": broken_match_id,
            "speech_id": broken_speech_id,
            "seat_key": "aff_1",
            "stage_key": "aff_case",
            "issues": ["missing_transcript", "missing_audio", "missing_segments"],
        }
        assert all(item["room_code"] != qa["code"] for item in payload["attention"]["samples"])


def test_admin_media_inventory_and_cleanup_never_delete_referenced_audio(client: TestClient, register_user, monkeypatch) -> None:
    owner = register_user("media_owner")
    room_data = create_training_room(owner, "媒体生命周期测试")
    code = room_data["code"]
    owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})
    owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})

    target_dir = settings.media_path / code
    target_dir.mkdir(parents=True, exist_ok=True)
    referenced = target_dir / "referenced.wav"
    orphan = target_dir / "orphan.wav"
    stale_part = target_dir / "stale.wav.part"
    referenced.write_bytes(_wav_bytes())
    orphan.write_bytes(_wav_bytes())
    stale_part.write_bytes(b"partial")
    old_timestamp = time.time() - 48 * 3600
    for path in (referenced, orphan, stale_part):
        os.utime(path, (old_timestamp, old_timestamp))

    with SessionLocal() as db:
        room = load_room(db, code)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        assert match
        db.add(
            Speech(
                match_id=match.id,
                room_id=room.id,
                seat_key="aff_1",
                stage_key="media-test",
                speaker_type="human",
                status="completed",
                audio_url=f"/media/{code}/{referenced.name}",
            )
        )
        db.add(
            Speech(
                match_id=match.id,
                room_id=room.id,
                seat_key="neg_1",
                stage_key="missing-media-test",
                speaker_type="ai",
                status="completed",
                audio_url=f"/media/{code}/missing.wav",
            )
        )
        db.commit()

    public_audio = client.get(f"/media/{code}/{referenced.name}")
    assert public_audio.status_code == 200 and public_audio.content == referenced.read_bytes()
    assert public_audio.headers["cache-control"] == "public, max-age=3600"
    partial_audio = client.get(f"/media/{code}/{referenced.name}", headers={"Range": "bytes=0-3"})
    assert partial_audio.status_code == 206 and partial_audio.content == referenced.read_bytes()[:4]
    assert partial_audio.headers["content-range"].startswith("bytes 0-3/")
    assert client.get(f"/media/{code}/{orphan.name}").status_code == 404

    admin = TestClient(client.app)
    with admin:
        assert admin.post("/api/auth/login", json={"account": "admin_test", "password": "Admin-test-1234"}).status_code == 200
        inventory = admin.get("/api/admin/media")
        assert inventory.status_code == 200
        media = inventory.json()["media"]
        candidates = {item["path"] for item in media["orphan_candidates"]}
        assert f"{code}/{orphan.name}" in candidates
        assert f"{code}/{stale_part.name}" in candidates
        assert f"{code}/{referenced.name}" not in candidates
        assert f"{code}/missing.wav" in media["missing_references"]

        payload = {"dry_run": False, "min_age_hours": 24, "include_stale_parts": True}
        assert admin.post("/api/admin/media/cleanup", json=payload).status_code == 403
        cleaned = admin.post("/api/admin/media/cleanup", headers=csrf(admin), json=payload)
        assert cleaned.status_code == 200
        deleted = set(cleaned.json()["media"]["deleted_paths"])
        assert {f"{code}/{orphan.name}", f"{code}/{stale_part.name}"}.issubset(deleted)

        blocked = target_dir / "blocked-by-scan-limit.wav"
        blocked.write_bytes(_wav_bytes())
        os.utime(blocked, (old_timestamp, old_timestamp))
        monkeypatch.setattr(media_storage, "MAX_SCAN_FILES", 0)
        refused = admin.post("/api/admin/media/cleanup", headers=csrf(admin), json=payload)
        assert refused.status_code == 409
        assert blocked.exists()

    assert referenced.exists()
    assert not orphan.exists() and not stale_part.exists()
    with SessionLocal() as db:
        log = db.scalar(select(AdminAuditLog).where(AdminAuditLog.action == "media.cleanup").order_by(AdminAuditLog.created_at.desc()))
        assert log and log.payload["deleted_files"] >= 2
    referenced.unlink(missing_ok=True)
    blocked.unlink(missing_ok=True)


def test_private_room_media_requires_membership_and_database_reference(client: TestClient, register_user) -> None:
    owner = register_user("private_media_owner")
    outsider = register_user("private_media_outsider")
    created = owner.post(
        "/api/rooms",
        headers=csrf(owner),
        json={
            "competition_slug": "training-1v1",
            "custom_topic": "私密比赛录音是否只能由房间成员访问？",
            "seat_key": "aff_1",
            "visibility": "private",
        },
    )
    assert created.status_code == 200
    code = created.json()["room"]["code"]
    owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})
    owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    target_dir = settings.media_path / code
    target_dir.mkdir(parents=True, exist_ok=True)
    referenced = target_dir / "private.wav"
    orphan = target_dir / "private-orphan.wav"
    linked = target_dir / "private-linked.wav"
    referenced.write_bytes(_wav_bytes())
    orphan.write_bytes(_wav_bytes())
    linked.symlink_to(referenced)
    try:
        with SessionLocal() as db:
            room = load_room(db, code)
            match = db.scalar(select(Match).where(Match.room_id == room.id))
            db.add(
                Speech(
                    match_id=match.id,
                    room_id=room.id,
                    seat_key="aff_1",
                    stage_key="private-media",
                    speaker_type="human",
                    status="completed",
                    audio_url=f"/media/{code}/{referenced.name}",
                )
            )
            db.add(
                Speech(
                    match_id=match.id,
                    room_id=room.id,
                    seat_key="neg_1",
                    stage_key="private-linked-media",
                    speaker_type="ai",
                    status="completed",
                    audio_url=f"/media/{code}/{linked.name}",
                )
            )
            db.commit()
        assert client.get(f"/media/{code}/{referenced.name}").status_code == 401
        assert outsider.get(f"/media/{code}/{referenced.name}").status_code == 403
        owner_audio = owner.get(f"/media/{code}/{referenced.name}")
        assert owner_audio.status_code == 200 and owner_audio.content == referenced.read_bytes()
        assert owner_audio.headers["cache-control"] == "private, no-store"
        assert owner.get(f"/media/{code}/{orphan.name}").status_code == 404
        assert owner.get(f"/media/{code}/{linked.name}").status_code == 404
    finally:
        referenced.unlink(missing_ok=True)
        orphan.unlink(missing_ok=True)
        linked.unlink(missing_ok=True)


def test_expired_session_and_banned_user_lose_access_immediately(client: TestClient, register_user) -> None:
    expired = register_user("expired_session")
    expired_user = expired.get("/api/auth/session").json()["user"]
    with SessionLocal() as db:
        session = db.scalar(select(UserSession).where(UserSession.user_id == expired_user["id"]))
        session.expires_at = now() - timedelta(seconds=1)
        db.commit()
    assert expired.get("/api/auth/session").status_code == 401

    participant = register_user("banned_socket")
    participant_user = participant.get("/api/auth/session").json()["user"]
    room = create_training_room(participant, "封禁用户实时连接测试")
    admin = TestClient(client.app)
    with admin:
        assert admin.post("/api/auth/login", json={"account": "admin_test", "password": "Admin-test-1234"}).status_code == 200
        with participant.websocket_connect(f"/ws/rooms/{room['code']}") as socket:
            socket.receive_json()
            banned = admin.patch(
                f"/api/admin/users/{participant_user['id']}",
                headers=csrf(admin),
                json={"is_active": False},
            )
            assert banned.status_code == 200
            socket.send_json({"type": "ping"})
            with pytest.raises(WebSocketDisconnect) as disconnected:
                socket.receive_json()
            assert disconnected.value.code == 4401
        assert participant.get("/api/auth/session").status_code == 401
        restored = admin.patch(
            f"/api/admin/users/{participant_user['id']}",
            headers=csrf(admin),
            json={"is_active": True},
        )
        assert restored.status_code == 200
        assert participant.get("/api/auth/session").status_code == 401
        assert (
            participant.post(
                "/api/auth/login",
                json={"account": participant_user["account"], "password": "Password-1234"},
            ).status_code
            == 200
        )


def test_login_caps_sessions_and_logout_requires_csrf(client: TestClient, register_user) -> None:
    user_client = register_user("session_cap")
    user = user_client.get("/api/auth/session").json()["user"]
    with SessionLocal() as db:
        for index in range(14):
            db.add(
                UserSession(
                    user_id=user["id"],
                    token_hash=token_hash(f"session-{index}"),
                    csrf_hash=token_hash(f"csrf-{index}"),
                    expires_at=expires_in_days(30),
                )
            )
        db.commit()

    another = TestClient(client.app)
    with another:
        login = another.post("/api/auth/login", json={"account": user["account"], "password": "Password-1234"})
        assert login.status_code == 200
        with SessionLocal() as db:
            assert len(db.scalars(select(UserSession).where(UserSession.user_id == user["id"])).all()) == 10
        assert another.post("/api/auth/logout").status_code == 403
        assert another.post("/api/auth/logout", headers=csrf(another)).status_code == 200
        assert another.get("/api/auth/session").status_code == 401


def test_password_change_rotates_session_and_revokes_other_devices(client: TestClient, register_user) -> None:
    primary = register_user("password_owner")
    user = primary.get("/api/auth/session").json()["user"]
    secondary = TestClient(client.app)
    fresh_login = TestClient(client.app)
    with secondary, fresh_login:
        assert (
            secondary.post(
                "/api/auth/login",
                json={"account": user["account"], "password": "Password-1234"},
            ).status_code
            == 200
        )
        wrong = primary.post(
            "/api/auth/password",
            headers=csrf(primary),
            json={
                "current_password": "Wrong-password",
                "new_password": "New-password-5678",
                "confirm_password": "New-password-5678",
            },
        )
        assert wrong.status_code == 400
        assert secondary.get("/api/auth/session").status_code == 200

        revoked = primary.post("/api/auth/sessions/revoke-others", headers=csrf(primary), json={})
        assert revoked.status_code == 200 and revoked.json()["revoked"] == 1
        assert secondary.get("/api/auth/session").status_code == 401
        assert (
            secondary.post(
                "/api/auth/login",
                json={"account": user["account"], "password": "Password-1234"},
            ).status_code
            == 200
        )

        unchanged = primary.post(
            "/api/auth/password",
            headers=csrf(primary),
            json={
                "current_password": "Password-1234",
                "new_password": "Password-1234",
                "confirm_password": "Password-1234",
            },
        )
        assert unchanged.status_code == 409
        changed = primary.post(
            "/api/auth/password",
            headers=csrf(primary),
            json={
                "current_password": "Password-1234",
                "new_password": "New-password-5678",
                "confirm_password": "New-password-5678",
            },
        )
        assert changed.status_code == 200 and changed.json()["other_sessions_revoked"] is True
        assert primary.get("/api/auth/session").status_code == 200
        assert secondary.get("/api/auth/session").status_code == 401
        assert (
            fresh_login.post(
                "/api/auth/login",
                json={"account": user["account"], "password": "Password-1234"},
            ).status_code
            == 401
        )
        assert (
            fresh_login.post(
                "/api/auth/login",
                json={"account": user["account"], "password": "New-password-5678"},
            ).status_code
            == 200
        )


def test_admin_can_restore_ai_substituted_human_seat(client: TestClient, register_user) -> None:
    owner = register_user("restore_owner")
    room_data = create_training_room(owner, "AI 接替席位恢复测试")
    code = room_data["code"]
    owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})
    owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        seat = next(item for item in room.seats if item.seat_key == "aff_1")
        seat.occupant_type = "ai_substitute"
        seat.display_name = f"AI 接替·{seat.display_name}"
        seat.connected = False
        seat.disconnected_at = now() - timedelta(seconds=61)
        db.commit()

    substituted_profile = owner.get("/api/me").json()
    substituted_room = next(item for item in substituted_profile["active_rooms"] if item["code"] == code)
    assert substituted_room["occupant_type"] == "ai_substitute"
    assert substituted_room["can_resume"] is False
    substituted_room_view = owner.get(f"/api/rooms/{code}").json()["room"]
    assert substituted_room_view["can_speak"] is False
    assert substituted_room_view["speak_reason"] == "你的席位已由 AI 接替，等待管理员恢复真人控制"

    forbidden = owner.post(f"/api/admin/rooms/{code}/seats/aff_1/restore", headers=csrf(owner), json={})
    assert forbidden.status_code == 403

    admin = TestClient(client.app)
    with admin:
        assert admin.post("/api/auth/login", json={"account": "admin_test", "password": "Admin-test-1234"}).status_code == 200
        offline = admin.post(f"/api/admin/rooms/{code}/seats/aff_1/restore", headers=csrf(admin), json={})
        assert offline.status_code == 409 and "尚未重新连接" in offline.json()["detail"]
        with SessionLocal() as db:
            room = load_room(db, code, lock=True)
            seat = next(item for item in room.seats if item.seat_key == "aff_1")
            match = db.scalar(select(Match).where(Match.room_id == room.id))
            seat.connected = True
            seat.disconnected_at = None
            active_speech = Speech(
                match_id=match.id,
                room_id=room.id,
                seat_key=seat.seat_key,
                stage_key="restore-race",
                speaker_type="ai",
                status="speaking",
            )
            db.add(active_speech)
            db.commit()
            active_speech_id = active_speech.id
        speaking = admin.post(f"/api/admin/rooms/{code}/seats/aff_1/restore", headers=csrf(admin), json={})
        assert speaking.status_code == 409 and "正在发言" in speaking.json()["detail"]
        with SessionLocal() as db:
            speech = db.get(Speech, active_speech_id)
            speech.status = "interrupted"
            db.commit()
        restored = admin.post(f"/api/admin/rooms/{code}/seats/aff_1/restore", headers=csrf(admin), json={})
        assert restored.status_code == 200, restored.text
        seat = next(item for item in restored.json()["room"]["seats"] if item["seat_key"] == "aff_1")
        assert seat["occupant_type"] == "human"
        assert seat["display_name"].startswith("测试选手restore_owner")
        assert seat["connected"] is True
    restored_profile = owner.get("/api/me").json()
    restored_room = next(item for item in restored_profile["active_rooms"] if item["code"] == code)
    assert restored_room["occupant_type"] == "human" and restored_room["can_resume"] is True


def _prepare_restore_request_room(register_user, suffix: str):
    owner = register_user(f"restore_request_owner_{suffix}")
    participant = register_user(f"restore_request_guest_{suffix}")
    room_data = create_training_room(owner, f"真人席位恢复申请测试 {suffix}")
    code = room_data["code"]
    assert participant.post(
        f"/api/rooms/{code}/claim-seat",
        headers=csrf(participant),
        json={"seat_key": "neg_1"},
    ).status_code == 200
    owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})
    participant.post(f"/api/rooms/{code}/ready", headers=csrf(participant), json={"ready": True})
    assert owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={}).status_code == 200
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        seat = next(item for item in room.seats if item.seat_key == "neg_1")
        seat.occupant_type = "ai_substitute"
        seat.display_name = f"AI 接替·{seat.display_name}"
        seat.connected = False
        seat.disconnected_at = now() - timedelta(seconds=61)
        db.commit()
    return owner, participant, code


def test_participant_restore_request_owner_approval_and_realtime_sync(client: TestClient, register_user) -> None:
    owner, participant, code = _prepare_restore_request_room(register_user, "approval")
    request_key = str(uuid.uuid4())

    with owner.websocket_connect(f"/ws/rooms/{code}") as socket:
        socket.receive_json()
        created = participant.post(
            f"/api/rooms/{code}/seat-restore-requests",
            headers={**csrf(participant), "X-Idempotency-Key": request_key},
            json={},
        )
        assert created.status_code == 200, created.text
        request = created.json()["request"]
        assert request["status"] == "pending" and request["can_cancel"] is True
        assert created.json()["room"]["seat_restore_requests"][0]["id"] == request["id"]

        replay = participant.post(
            f"/api/rooms/{code}/seat-restore-requests",
            headers={**csrf(participant), "X-Idempotency-Key": request_key},
            json={},
        )
        assert replay.status_code == 200 and replay.json()["replayed"] is True
        owner_view = owner.get(f"/api/rooms/{code}").json()["room"]
        assert owner_view["seat_restore_requests"][0]["can_review"] is True

        realtime = None
        for _ in range(6):
            message = socket.receive_json()
            if message.get("event", {}).get("type") == "seat.restore_requested":
                realtime = message
                break
        assert realtime is not None
        assert realtime["room"]["seat_restore_requests"][0]["id"] == request["id"]

        with SessionLocal() as db:
            room = load_room(db, code, lock=True)
            seat = next(item for item in room.seats if item.user_id == request["requester"]["id"])
            seat.connected = True
            seat.disconnected_at = None
            db.commit()

        approved = owner.post(
            f"/api/rooms/{code}/seat-restore-requests/{request['id']}/approve",
            headers=csrf(owner),
            json={"reason": "本人已确认返回"},
        )
        assert approved.status_code == 200, approved.text
        restored = next(item for item in approved.json()["room"]["seats"] if item["seat_key"] == "neg_1")
        assert restored["occupant_type"] == "human"
        participant_room = participant.get(f"/api/rooms/{code}").json()["room"]
        assert participant_room["my_seat"] == "neg_1"
        assert participant_room["seat_restore_requests"][0]["status"] == "approved"


def test_restore_request_is_cancellable_rejectable_and_strictly_authorized(client: TestClient, register_user) -> None:
    owner, participant, code = _prepare_restore_request_room(register_user, "authorization")
    outsider = register_user("restore_request_outsider")
    created = participant.post(
        f"/api/rooms/{code}/seat-restore-requests",
        headers=csrf(participant),
        json={},
    ).json()["request"]

    forbidden_review = outsider.post(
        f"/api/rooms/{code}/seat-restore-requests/{created['id']}/approve",
        headers=csrf(outsider),
        json={"reason": "越权"},
    )
    assert forbidden_review.status_code == 403
    forbidden_cancel = outsider.post(
        f"/api/rooms/{code}/seat-restore-requests/{created['id']}/cancel",
        headers=csrf(outsider),
        json={},
    )
    assert forbidden_cancel.status_code == 403

    rejected = owner.post(
        f"/api/rooms/{code}/seat-restore-requests/{created['id']}/reject",
        headers=csrf(owner),
        json={"reason": "等待网络稳定"},
    )
    assert rejected.status_code == 200
    participant_view = participant.get(f"/api/rooms/{code}").json()["room"]
    assert participant_view["seat_restore_requests"][0]["status"] == "rejected"
    assert participant_view["seat_restore_requests"][0]["resolution_reason"] == "等待网络稳定"

    replacement = participant.post(
        f"/api/rooms/{code}/seat-restore-requests",
        headers=csrf(participant),
        json={},
    ).json()["request"]
    cancelled = participant.post(
        f"/api/rooms/{code}/seat-restore-requests/{replacement['id']}/cancel",
        headers=csrf(participant),
        json={},
    )
    assert cancelled.status_code == 200
    assert participant.get(f"/api/rooms/{code}").json()["room"]["seat_restore_requests"][0]["status"] == "cancelled"
    replay = participant.post(
        f"/api/rooms/{code}/seat-restore-requests/{replacement['id']}/cancel",
        headers=csrf(participant),
        json={},
    )
    assert replay.status_code == 200 and replay.json()["replayed"] is True


def test_restore_request_rejects_active_speech_conflicting_seat_and_terminal_room(client: TestClient, register_user) -> None:
    owner, participant, code = _prepare_restore_request_room(register_user, "guards")
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        speech = Speech(
            match_id=match.id,
            room_id=room.id,
            seat_key="aff_1",
            stage_key="guard",
            speaker_type="ai",
            status="playing",
        )
        db.add(speech)
        db.commit()
        speech_id = speech.id
    blocked = participant.post(f"/api/rooms/{code}/seat-restore-requests", headers=csrf(participant), json={})
    assert blocked.status_code == 409 and "发言正在进行" in blocked.json()["detail"]
    with SessionLocal() as db:
        db.get(Speech, speech_id).status = "completed"
        db.commit()

    other_room = create_training_room(participant, "恢复席位并发冲突房间")
    conflict = participant.post(f"/api/rooms/{code}/seat-restore-requests", headers=csrf(participant), json={})
    assert conflict.status_code == 409 and other_room["code"] in conflict.json()["detail"]
    participant.post(f"/api/rooms/{other_room['code']}/cancel", headers=csrf(participant), json={})
    request = participant.post(f"/api/rooms/{code}/seat-restore-requests", headers=csrf(participant), json={}).json()["request"]
    owner.post(f"/api/rooms/{code}/control/terminate", headers=csrf(owner), json={"reason": "测试终局失效"})
    terminal_view = participant.get(f"/api/rooms/{code}").json()["room"]
    request_view = next(item for item in terminal_view["seat_restore_requests"] if item["id"] == request["id"])
    assert request_view["status"] == "expired"
    with SessionLocal() as db:
        stored = db.get(SeatRestoreRequest, request["id"])
        assert stored.status == "expired" and stored.resolution_reason == "比赛已终止"
    approval = owner.post(
        f"/api/rooms/{code}/seat-restore-requests/{request['id']}/approve",
        headers=csrf(owner),
        json={"reason": "终局后误操作"},
    )
    assert approval.status_code == 409


def test_admin_cannot_restore_substituted_seat_after_match_ends(client: TestClient, register_user) -> None:
    owner = register_user("terminal_restore")
    room_data = create_training_room(owner, "终局不得恢复席位测试")
    code = room_data["code"]
    owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})
    owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    owner.post(f"/api/rooms/{code}/control/terminate", headers=csrf(owner), json={"reason": "test"})
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        seat = next(item for item in room.seats if item.seat_key == "aff_1")
        seat.occupant_type = "ai_substitute"
        seat.connected = True
        db.commit()
    admin = TestClient(client.app)
    with admin:
        admin.post("/api/auth/login", json={"account": "admin_test", "password": "Admin-test-1234"})
        response = admin.post(f"/api/admin/rooms/{code}/seats/aff_1/restore", headers=csrf(admin), json={})
        assert response.status_code == 409 and "比赛已经结束" in response.json()["detail"]


def test_ranking_application_is_idempotent(client: TestClient, register_user) -> None:
    owner = register_user("ranking_owner")
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
    assert created.status_code == 200
    code = created.json()["room"]["code"]
    owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})
    owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})

    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        match.winner = "aff"
        scorecard = JudgeScorecard(
            match_id=match.id,
            status="approved",
            winner="aff",
            affirmative_score=91,
            negative_score=84,
        )
        db.add(scorecard)
        match_engine._apply_ranking(db, room, match, scorecard)
        match_engine._apply_ranking(db, room, match, scorecard)
        db.commit()

        user_id = owner.get("/api/auth/session").json()["user"]["id"]
        changes = list(db.scalars(select(RatingChange).where(RatingChange.match_id == match.id, RatingChange.user_id == user_id)).all())
        entry = db.scalar(
            select(LeaderboardEntry).where(
                LeaderboardEntry.competition_id == room.competition_id,
                LeaderboardEntry.season_id == match.season_id,
                LeaderboardEntry.user_id == user_id,
            )
        )
        assert len(changes) == 1
        assert entry and entry.matches == 1 and entry.points == 3 and entry.wins == 1


def test_qa_account_rooms_are_excluded_from_public_rankings(client: TestClient, register_user) -> None:
    qa_user = register_user("qa_scope")
    qa_identity = qa_user.get("/api/auth/session").json()["user"]
    historical_room = create_training_room(qa_user, "账号标记前创建的验收房也应归入 QA 数据")
    admin = TestClient(client.app)
    with admin:
        assert admin.post("/api/auth/login", json={"account": "admin_test", "password": "Admin-test-1234"}).status_code == 200
        marked = admin.patch(
            f"/api/admin/users/{qa_identity['id']}",
            headers=csrf(admin),
            json={"is_test_account": True},
        )
        assert marked.status_code == 200, marked.text
        assert marked.json()["user"]["is_test_account"] is True
        historical = admin.get(f"/api/admin/rooms?data_scope=qa&q={historical_room['code']}")
        assert historical.status_code == 200
        assert [item["code"] for item in historical.json()["items"]] == [historical_room["code"]]

    qa_user.post(f"/api/rooms/{historical_room['code']}/cancel", headers=csrf(qa_user), json={})

    room = create_training_room(qa_user, "QA 数据域不会污染正式统计")
    assert room["is_test_data"] is True
    with admin:
        filtered = admin.get(f"/api/admin/rooms?data_scope=qa&q={room['code']}")
        assert filtered.status_code == 200, filtered.text
        assert [item["code"] for item in filtered.json()["items"]] == [room["code"]]
        hidden_from_production = admin.get(f"/api/admin/rooms?data_scope=production&q={room['code']}")
        assert hidden_from_production.status_code == 200
        assert hidden_from_production.json()["items"] == []
    qa_user.post(f"/api/rooms/{room['code']}/cancel", headers=csrf(qa_user), json={})

    with SessionLocal() as db:
        competition = db.scalar(select(Competition).where(Competition.slug == "daily-4v4"))
        db.add(
            LeaderboardEntry(
                competition_id=competition.id,
                season_id=competition.season_id,
                user_id=qa_identity["id"],
                points=3,
                wins=1,
                matches=1,
                average_score=88,
                last_match_at=now(),
            )
        )
        db.commit()
        assert all(item["user_id"] != qa_identity["id"] for item in leaderboard(db, season_id=competition.season_id))
        assert any(
            item["user_id"] == qa_identity["id"] for item in leaderboard(db, season_id=competition.season_id, include_test_accounts=True)
        )


def test_qa_rooms_do_not_apply_ranking_and_test_joiners_taint_lobby(client: TestClient, register_user) -> None:
    qa_user = register_user("qa_rank_skip")
    regular_owner = register_user("qa_regular_owner")
    qa_identity = qa_user.get("/api/auth/session").json()["user"]
    admin = TestClient(client.app)
    with admin:
        assert admin.post("/api/auth/login", json={"account": "admin_test", "password": "Admin-test-1234"}).status_code == 200
        assert (
            admin.patch(
                f"/api/admin/users/{qa_identity['id']}",
                headers=csrf(admin),
                json={"is_test_account": True},
            ).status_code
            == 200
        )

    regular_room = create_training_room(regular_owner, "QA 参与者加入后整场转为测试数据")
    claimed = qa_user.post(
        f"/api/rooms/{regular_room['code']}/claim-seat",
        headers=csrf(qa_user),
        json={"seat_key": "neg_1"},
    )
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["room"]["is_test_data"] is True
    regular_owner.post(f"/api/rooms/{regular_room['code']}/cancel", headers=csrf(regular_owner), json={})

    detail = client.get("/api/competitions/daily-4v4").json()["competition"]
    created = qa_user.post(
        "/api/rooms",
        headers=csrf(qa_user),
        json={
            "competition_slug": "daily-4v4",
            "topic_id": detail["topics"][0]["id"],
            "seat_key": "aff_1",
            "visibility": "public",
        },
    )
    assert created.status_code == 200, created.text
    code = created.json()["room"]["code"]
    assert qa_user.post(f"/api/rooms/{code}/ready", headers=csrf(qa_user), json={"ready": True}).status_code == 200
    assert qa_user.post(f"/api/rooms/{code}/start", headers=csrf(qa_user), json={}).status_code == 200

    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        match.winner = "aff"
        scorecard = JudgeScorecard(
            match_id=match.id,
            status="approved",
            winner="aff",
            affirmative_score=90,
            negative_score=82,
        )
        db.add(scorecard)
        match_engine._apply_ranking(db, room, match, scorecard)
        db.commit()
        assert not db.scalar(select(RatingChange.id).where(RatingChange.match_id == match.id))
        assert not db.scalar(
            select(LeaderboardEntry.id).where(
                LeaderboardEntry.competition_id == room.competition_id,
                LeaderboardEntry.season_id == match.season_id,
                LeaderboardEntry.user_id == qa_identity["id"],
            )
        )


def test_admin_reclassifying_completed_room_rebuilds_leaderboard(client: TestClient, register_user) -> None:
    owner = register_user("room_scope_rebuild")
    owner_id = owner.get("/api/auth/session").json()["user"]["id"]
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
    assert created.status_code == 200, created.text
    code = created.json()["room"]["code"]
    assert owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True}).status_code == 200
    assert owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={}).status_code == 200

    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        room.status = "completed"
        room.completed_at = now()
        match.status = "completed"
        match.winner = "aff"
        scorecard = JudgeScorecard(
            match_id=match.id,
            status="approved",
            winner="aff",
            affirmative_score=93,
            negative_score=81,
        )
        db.add(scorecard)
        match_engine._apply_ranking(db, room, match, scorecard)
        db.commit()
        assert db.scalar(
            select(LeaderboardEntry.id).where(
                LeaderboardEntry.competition_id == room.competition_id,
                LeaderboardEntry.season_id == match.season_id,
                LeaderboardEntry.user_id == owner_id,
            )
        )
        match_id = match.id

    archive_before = owner.get(f"/api/matches/{match_id}/archive")
    assert archive_before.status_code == 200
    assert archive_before.json()["data"]["room"]["is_test_data"] is False

    admin = TestClient(client.app)
    with admin:
        assert admin.post("/api/auth/login", json={"account": "admin_test", "password": "Admin-test-1234"}).status_code == 200
        classified = admin.patch(
            f"/api/admin/rooms/{code}/data-scope",
            headers=csrf(admin),
            json={"is_test_data": True},
        )
        assert classified.status_code == 200, classified.text
        assert classified.json()["room"]["is_test_data"] is True
        rejected_restore = admin.patch(
            f"/api/admin/rooms/{code}/data-scope",
            headers=csrf(admin),
            json={"is_test_data": False},
        )
        assert rejected_restore.status_code == 409

    archive_after = owner.get(f"/api/matches/{match_id}/archive")
    assert archive_after.status_code == 200
    assert archive_after.headers["x-archive-source-sha256"] != archive_before.headers["x-archive-source-sha256"]
    assert archive_after.json()["data"]["room"]["is_test_data"] is True

    with SessionLocal() as db:
        room = load_room(db, code)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        assert room.is_test_data is True
        assert not db.scalar(
            select(LeaderboardEntry.id).where(
                LeaderboardEntry.competition_id == room.competition_id,
                LeaderboardEntry.season_id == match.season_id,
                LeaderboardEntry.user_id == owner_id,
            )
        )


def prepare_human_speech(client: TestClient) -> tuple[str, str]:
    room = create_training_room(client, "实时发言边界测试")
    code = room["code"]
    assert client.post(f"/api/rooms/{code}/ready", headers=csrf(client), json={"ready": True}).status_code == 200
    assert client.post(f"/api/rooms/{code}/start", headers=csrf(client), json={}).status_code == 200
    with SessionLocal() as db:
        stored = load_room(db, code, lock=True)
        stored.status = "running"
        stored.current_stage_index = 1
        stored.stage_started_at = now()
        stored.stage_deadline_at = now() + timedelta(seconds=180)
        db.commit()
    acquired = client.post(
        f"/api/rooms/{code}/control-lease",
        headers=csrf(client) | {"X-Control-Lease": "test-lease"},
        json={},
    )
    assert acquired.status_code == 200, acquired.text
    started = client.post(
        f"/api/rooms/{code}/speech/start",
        headers=csrf(client) | {"X-Control-Lease": "test-lease"},
        json={},
    )
    assert started.status_code == 200, started.text
    return code, started.json()["speech_id"]


def test_seat_control_lease_is_explicit_and_takeover_is_safe(client: TestClient, register_user) -> None:
    owner = register_user("device_lease_owner")
    owner_identity = owner.get("/api/auth/session").json()["user"]
    second_device = TestClient(client.app)
    second_device.__enter__()
    assert (
        second_device.post(
            "/api/auth/login",
            json={"account": owner_identity["account"], "password": "Password-1234"},
        ).status_code
        == 200
    )
    room = create_training_room(owner, "多设备席位接管测试")
    code = room["code"]
    owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})
    owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    with SessionLocal() as db:
        stored = load_room(db, code, lock=True)
        stored.status = "running"
        stored.current_stage_index = 1
        stored.stage_started_at = now()
        stored.stage_deadline_at = now() + timedelta(seconds=180)
        db.commit()

    missing_lease = owner.post(f"/api/rooms/{code}/speech/start", headers=csrf(owner), json={})
    assert missing_lease.status_code == 409
    acquired_a = owner.post(
        f"/api/rooms/{code}/control-lease",
        headers=csrf(owner) | {"X-Control-Lease": "device-a"},
    )
    assert acquired_a.status_code == 200 and acquired_a.json()["replayed"] is False
    replayed_a = owner.post(
        f"/api/rooms/{code}/control-lease",
        headers=csrf(owner) | {"X-Control-Lease": "device-a"},
        json={},
    )
    assert replayed_a.status_code == 200 and replayed_a.json()["replayed"] is True
    started_a = owner.post(
        f"/api/rooms/{code}/speech/start",
        headers=csrf(owner) | {"X-Control-Lease": "device-a"},
        json={},
    )
    assert started_a.status_code == 200
    resumed_a = owner.post(
        f"/api/rooms/{code}/speech/start",
        headers=csrf(owner) | {"X-Control-Lease": "device-a", "X-Idempotency-Key": "recover-existing-speech"},
        json={},
    )
    assert resumed_a.status_code == 200
    assert resumed_a.json()["resumed"] is True and resumed_a.json()["speech_id"] == started_a.json()["speech_id"]
    blocked_takeover = second_device.post(
        f"/api/rooms/{code}/control-lease",
        headers=csrf(second_device) | {"X-Control-Lease": "device-b"},
        json={},
    )
    assert blocked_takeover.status_code == 409
    finished = owner.post(
        f"/api/rooms/{code}/speech/finish",
        headers=csrf(owner) | {"X-Control-Lease": "device-a"},
        json={"speech_id": started_a.json()["speech_id"], "content": "设备 A 完成当前发言。"},
    )
    assert finished.status_code == 200
    requires_confirmation = second_device.post(
        f"/api/rooms/{code}/control-lease",
        headers=csrf(second_device) | {"X-Control-Lease": "device-b"},
        json={},
    )
    assert requires_confirmation.status_code == 409
    assert "确认接管" in requires_confirmation.json()["detail"]
    acquired_b = second_device.post(
        f"/api/rooms/{code}/control-lease",
        headers=csrf(second_device) | {"X-Control-Lease": "device-b"},
        json={"force": True},
    )
    assert acquired_b.status_code == 200 and acquired_b.json()["replayed"] is False
    with SessionLocal() as db:
        stored = load_room(db, code, lock=True)
        stored.current_stage_index = 1
        stored.stage_started_at = now()
        stored.stage_deadline_at = now() + timedelta(seconds=180)
        db.commit()
    stale_device = owner.post(
        f"/api/rooms/{code}/speech/start",
        headers=csrf(owner) | {"X-Control-Lease": "device-a"},
        json={},
    )
    current_device = second_device.post(
        f"/api/rooms/{code}/speech/start",
        headers=csrf(second_device) | {"X-Control-Lease": "device-b"},
        json={},
    )
    assert stale_device.status_code == 409 and current_device.status_code == 200
    with SessionLocal() as db:
        stored = load_room(db, code)
        events = list(
            db.scalars(
                select(MatchEvent)
                .where(
                    MatchEvent.room_id == stored.id,
                    MatchEvent.event_type.in_(["seat.control_acquired", "seat.control_taken_over"]),
                )
                .order_by(MatchEvent.seq)
            ).all()
        )
        assert [item.event_type for item in events] == ["seat.control_acquired", "seat.control_taken_over"]
        assert all(len(item.payload["lease_fingerprint"]) == 16 for item in events)
        assert events[0].payload["forced"] is False and events[1].payload["forced"] is True
    public_room = client.get(f"/api/rooms/{code}").json()["room"]
    public_control_events = [
        item for item in public_room["recent_events"] if item["type"] in {"seat.control_acquired", "seat.control_taken_over"}
    ]
    assert public_control_events == []
    second_device.__exit__(None, None, None)


def test_lobby_mutations_follow_the_authoritative_device_lease(client: TestClient, register_user) -> None:
    owner = register_user("lobby_device_owner")
    identity = owner.get("/api/auth/session").json()["user"]
    second_device = TestClient(client.app)
    second_device.__enter__()
    assert second_device.post(
        "/api/auth/login",
        json={"account": identity["account"], "password": "Password-1234"},
    ).status_code == 200
    room = create_training_room(owner, "大厅设备控制权测试")
    code = room["code"]

    acquired = owner.post(
        f"/api/rooms/{code}/control-lease",
        headers=csrf(owner) | {"X-Control-Lease": "lobby-device-a"},
        json={},
    )
    assert acquired.status_code == 200
    assert owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True}).status_code == 200

    stale_ready = second_device.post(
        f"/api/rooms/{code}/ready",
        headers=csrf(second_device),
        json={"ready": False},
    )
    assert stale_ready.status_code == 409
    assert "确认接管" in stale_ready.json()["detail"]
    assert second_device.post(
        f"/api/rooms/{code}/control-lease",
        headers=csrf(second_device) | {"X-Control-Lease": "lobby-device-b"},
        json={},
    ).status_code == 409
    assert second_device.post(
        f"/api/rooms/{code}/control-lease",
        headers=csrf(second_device) | {"X-Control-Lease": "lobby-device-b"},
        json={"force": True},
    ).status_code == 200
    assert second_device.post(
        f"/api/rooms/{code}/ready",
        headers=csrf(second_device),
        json={"ready": False},
    ).status_code == 200

    assert owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True}).status_code == 409
    assert owner.post(f"/api/rooms/{code}/cancel", headers=csrf(owner), json={}).status_code == 409
    assert second_device.post(f"/api/rooms/{code}/cancel", headers=csrf(second_device), json={}).status_code == 200
    second_device.__exit__(None, None, None)


def test_same_login_session_recovers_control_lease_without_confirmation(client: TestClient, register_user) -> None:
    owner = register_user("same_session_lease")
    room = create_training_room(owner, "同一浏览器刷新自动恢复席位控制")
    code = room["code"]
    owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})
    owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    with SessionLocal() as db:
        stored = load_room(db, code, lock=True)
        stored.status = "running"
        stored.current_stage_index = 1
        stored.stage_started_at = now()
        stored.stage_deadline_at = now() + timedelta(seconds=180)
        db.commit()

    first = owner.post(
        f"/api/rooms/{code}/control-lease",
        headers=csrf(owner) | {"X-Control-Lease": "before-refresh"},
        json={},
    )
    assert first.status_code == 200 and first.json()["same_session_recovery"] is False
    recovered = owner.post(
        f"/api/rooms/{code}/control-lease",
        headers=csrf(owner) | {"X-Control-Lease": "after-refresh"},
        json={},
    )
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["same_session_recovery"] is True
    assert (
        owner.post(
            f"/api/rooms/{code}/speech/start",
            headers=csrf(owner) | {"X-Control-Lease": "before-refresh"},
            json={},
        ).status_code
        == 409
    )
    assert (
        owner.post(
            f"/api/rooms/{code}/speech/start",
            headers=csrf(owner) | {"X-Control-Lease": "after-refresh"},
            json={},
        ).status_code
        == 200
    )


def test_review_required_room_rejects_new_device_control_without_appending_events(client: TestClient, register_user) -> None:
    owner = register_user("review_terminal_lease")
    room = create_training_room(owner, "待复核比赛必须保持只读")
    code = room["code"]
    owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})
    owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    with SessionLocal() as db:
        stored = load_room(db, code, lock=True)
        match = db.scalar(select(Match).where(Match.room_id == stored.id))
        stored.status = "review_required"
        stored.completed_at = now()
        match.status = "review_required"
        db.commit()
        seq_before = stored.seq

    rejected = owner.post(
        f"/api/rooms/{code}/control-lease",
        headers=csrf(owner) | {"X-Control-Lease": "late-device"},
        json={},
    )
    assert rejected.status_code == 409 and "比赛已经结束" in rejected.json()["detail"]
    with SessionLocal() as db:
        stored = load_room(db, code)
        assert stored.seq == seq_before
        assert not db.scalar(
            select(MatchEvent.id).where(
                MatchEvent.room_id == stored.id,
                MatchEvent.event_type.in_(["seat.control_acquired", "seat.control_taken_over"]),
            )
        )


@pytest.mark.asyncio
async def test_free_debate_human_turn_timeout_advances_and_accepts_late_recording(client: TestClient, register_user) -> None:
    owner = register_user("free_timeout_owner")
    room = create_training_room(owner, "自由辩论单轮超时测试")
    code = room["code"]
    owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})
    owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    with SessionLocal() as db:
        stored = load_room(db, code, lock=True)
        stored.template_snapshot = [{"key": "free", "name": "自由辩论", "kind": "free", "side": "aff", "duration": 300, "turn_duration": 5}]
        stored.status = "running"
        stored.current_stage_index = 0
        stored.stage_started_at = now()
        stored.stage_deadline_at = now() + timedelta(seconds=300)
        db.commit()
    lease_headers = csrf(owner) | {"X-Control-Lease": "free-timeout-device"}
    owner.post(f"/api/rooms/{code}/control-lease", headers=lease_headers, json={}).raise_for_status()
    started = owner.post(f"/api/rooms/{code}/speech/start", headers=lease_headers, json={})
    assert started.status_code == 200
    active_view = owner.get(f"/api/rooms/{code}").json()["room"]
    assert active_view["turn_remaining_seconds"] in {4, 5}
    speech_id = started.json()["speech_id"]
    with SessionLocal() as db:
        stored = load_room(db, code, lock=True)
        current = dict(stored.template_snapshot[0])
        current["turn_started_at"] = (now() - timedelta(seconds=6)).isoformat()
        stored.template_snapshot = [current]
        db.commit()
    await match_engine.process_room(code)
    after_timeout = owner.get(f"/api/rooms/{code}").json()["room"]
    assert after_timeout["current_stage"]["side"] == "neg"
    assert after_timeout["active_speech"] is None and after_timeout["turn_remaining_seconds"] in {4, 5}
    with SessionLocal() as db:
        stored = load_room(db, code)
        match = db.scalar(select(Match).where(Match.room_id == stored.id))
        decoy = Speech(
            match_id=match.id,
            room_id=stored.id,
            seat_key="aff_1",
            stage_key="free",
            speaker_type="human",
            status="timed_out",
            content="不应被按席位猜中的旧式候选。",
        )
        db.add(decoy)
        db.commit()
        decoy_id = decoy.id
    finish_headers = lease_headers | {"X-Idempotency-Key": "late-free-finish"}
    finalized = owner.post(
        f"/api/rooms/{code}/speech/finish",
        headers=finish_headers,
        json={"speech_id": speech_id, "content": "超时后浏览器自动补交的最终录音文本。"},
    )
    replayed = owner.post(
        f"/api/rooms/{code}/speech/finish",
        headers=finish_headers,
        json={"speech_id": speech_id, "content": "超时后浏览器自动补交的最终录音文本。"},
    )
    conflicting = owner.post(
        f"/api/rooms/{code}/speech/finish",
        headers=finish_headers,
        json={"speech_id": speech_id, "content": "不应覆盖原文本"},
    )
    assert finalized.status_code == 200 and finalized.json()["timed_out"] is True
    assert replayed.status_code == 200 and replayed.json()["replayed"] is True
    assert conflicting.status_code == 409 and "幂等键" in conflicting.json()["detail"]
    with SessionLocal() as db:
        speech = db.get(Speech, speech_id)
        decoy = db.get(Speech, decoy_id)
        events = list(
            db.scalars(
                select(MatchEvent).where(
                    MatchEvent.room_id == speech.room_id,
                    MatchEvent.event_type.in_(["speech.timed_out", "speech.late_finalized"]),
                )
            ).all()
        )
        assert speech.status == "completed" and speech.content == "超时后浏览器自动补交的最终录音文本。"
        assert decoy.status == "timed_out" and decoy.content == "不应被按席位猜中的旧式候选。"
        assert len(events) == 2


@pytest.mark.asyncio
async def test_free_debate_idle_side_yields_after_turn_window(client: TestClient, register_user) -> None:
    owner = register_user("free_idle_owner")
    room = create_training_room(owner, "自由辩论无人发言自动换边测试")
    code = room["code"]
    owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})
    owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    with SessionLocal() as db:
        stored = load_room(db, code, lock=True)
        stored.template_snapshot = [
            {
                "key": "free",
                "name": "自由辩论",
                "kind": "free",
                "side": "aff",
                "duration": 300,
                "turn_duration": 5,
                "turn_started_at": (now() - timedelta(seconds=6)).isoformat(),
            }
        ]
        stored.status = "running"
        stored.current_stage_index = 0
        stored.stage_started_at = now() - timedelta(seconds=6)
        stored.stage_deadline_at = now() + timedelta(seconds=294)
        db.commit()
    await match_engine.process_room(code)
    with SessionLocal() as db:
        stored = load_room(db, code)
        assert stored.template_snapshot[0]["side"] == "neg"
        assert db.scalar(select(MatchEvent).where(MatchEvent.room_id == stored.id, MatchEvent.event_type == "free.turn_timed_out"))
        assert not db.scalar(select(Speech.id).where(Speech.room_id == stored.id))


def test_free_turn_pause_resume_preserves_clock_and_rejects_pause_during_human_speech(client: TestClient, register_user) -> None:
    owner = register_user("free_pause_owner")
    room = create_training_room(owner, "自由辩论暂停恢复计时测试")
    code = room["code"]
    owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})
    owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    with SessionLocal() as db:
        stored = load_room(db, code, lock=True)
        stored.template_snapshot = [
            {
                "key": "free",
                "name": "自由辩论",
                "kind": "free",
                "side": "aff",
                "duration": 300,
                "turn_duration": 40,
                "turn_started_at": (now() - timedelta(seconds=10)).isoformat(),
            }
        ]
        stored.status = "running"
        stored.current_stage_index = 0
        stored.stage_started_at = now() - timedelta(seconds=10)
        stored.stage_deadline_at = now() + timedelta(seconds=290)
        db.commit()
    paused = owner.post(f"/api/rooms/{code}/control/pause", headers=csrf(owner), json={"reason": "计时测试"})
    assert paused.status_code == 200
    paused_turn = paused.json()["room"]["turn_remaining_seconds"]
    assert paused_turn in {29, 30}
    assert owner.get(f"/api/rooms/{code}").json()["room"]["turn_remaining_seconds"] == paused_turn
    assert owner.post(f"/api/rooms/{code}/control/retry", headers=csrf(owner), json={"reason": "误点重试"}).status_code == 409
    resumed = owner.post(f"/api/rooms/{code}/control/resume", headers=csrf(owner), json={"reason": "继续"})
    assert resumed.status_code == 200
    assert resumed.json()["room"]["turn_remaining_seconds"] in {paused_turn - 1, paused_turn}

    lease_headers = csrf(owner) | {"X-Control-Lease": "free-pause-device"}
    owner.post(f"/api/rooms/{code}/control-lease", headers=lease_headers, json={}).raise_for_status()
    owner.post(f"/api/rooms/{code}/speech/start", headers=lease_headers, json={}).raise_for_status()
    blocked = owner.post(f"/api/rooms/{code}/control/pause", headers=csrf(owner), json={"reason": "不应暂停"})
    assert blocked.status_code == 409 and "真人正在发言" in blocked.json()["detail"]


def test_ai_playback_pause_resume_keeps_audio_offset(client: TestClient, register_user) -> None:
    owner = register_user("playback_pause_owner")
    room = create_training_room(owner, "AI 播放暂停续播测试")
    code = room["code"]
    owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})
    owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    with SessionLocal() as db:
        stored = load_room(db, code, lock=True)
        match = db.scalar(select(Match).where(Match.room_id == stored.id))
        started_at = now() - timedelta(seconds=4)
        stored.template_snapshot = [{"key": "ai_case", "name": "AI 发言", "kind": "speech", "seat": "neg_1", "duration": 30}]
        stored.status = "running"
        stored.current_stage_index = 0
        stored.stage_started_at = started_at
        stored.stage_deadline_at = now() + timedelta(seconds=6)
        speech = Speech(
            match_id=match.id,
            room_id=stored.id,
            seat_key="neg_1",
            stage_key="ai_case",
            speaker_type="ai",
            status="playing",
            duration_seconds=10,
            playback_started_at=started_at,
            playback_ends_at=now() + timedelta(seconds=6),
        )
        db.add(speech)
        db.commit()
        speech_id = speech.id
    assert owner.post(f"/api/rooms/{code}/control/pause", headers=csrf(owner), json={"reason": "检查续播"}).status_code == 200
    assert owner.post(f"/api/rooms/{code}/control/resume", headers=csrf(owner), json={"reason": "继续播放"}).status_code == 200
    with SessionLocal() as db:
        stored = load_room(db, code)
        speech = db.get(Speech, speech_id)
        playback_started_at = speech.playback_started_at
        playback_ends_at = speech.playback_ends_at
        if playback_started_at.tzinfo is None:
            playback_started_at = playback_started_at.replace(tzinfo=now().tzinfo)
        if playback_ends_at.tzinfo is None:
            playback_ends_at = playback_ends_at.replace(tzinfo=now().tzinfo)
        elapsed = (now() - playback_started_at).total_seconds()
        remaining = (playback_ends_at - now()).total_seconds()
        assert 3.5 <= elapsed <= 4.5
        assert 5.4 <= remaining <= 6.5
        assert "paused_playback_elapsed_seconds" not in stored.template_snapshot[0]


async def test_pause_resume_preserves_zero_second_boundary(client: TestClient, register_user) -> None:
    owner = register_user("zero_pause_boundary")
    room_data = create_training_room(owner, "零秒暂停恢复不得增加比赛时间")
    code = room_data["code"]
    owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})
    owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.template_snapshot = [
            {"key": "expired", "name": "已到时环节", "kind": "announcement", "duration": 10},
            {"key": "next", "name": "下一环节", "kind": "announcement", "duration": 30},
        ]
        room.status = "running"
        room.current_stage_index = 0
        room.stage_started_at = now() - timedelta(seconds=10)
        room.stage_deadline_at = now() - timedelta(milliseconds=1)
        db.commit()

    paused = owner.post(f"/api/rooms/{code}/control/pause", headers=csrf(owner), json={"reason": "边界暂停"})
    assert paused.status_code == 200 and paused.json()["room"]["remaining_seconds"] == 0
    resumed = owner.post(f"/api/rooms/{code}/control/resume", headers=csrf(owner), json={"reason": "边界恢复"})
    assert resumed.status_code == 200 and resumed.json()["room"]["remaining_seconds"] == 0
    await match_engine.process_room(code)
    with SessionLocal() as db:
        room = load_room(db, code)
        assert room.current_stage_index == 1


def test_pause_resume_preserves_frozen_free_ai_preparation_clock(client: TestClient, register_user) -> None:
    owner = register_user("pause_free_ai_preparation")
    created = owner.post(
        "/api/rooms",
        headers=csrf(owner),
        json={
            "competition_slug": "training-1v1",
            "custom_topic": "暂停 AI 准备是否保留自由辩论计时？",
            "seat_key": "aff_1",
            "visibility": "private",
        },
    )
    code = created.json()["room"]["code"]
    assert owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True}).status_code == 200
    assert owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={}).status_code == 200
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        assert match is not None
        room.template_snapshot = [
            {
                "key": "free",
                "name": "自由辩论",
                "kind": "free",
                "side": "aff",
                "duration": 300,
                "turn_duration": 45,
                "turn_started_at": (now() - timedelta(seconds=90)).isoformat(),
                "ai_preparing": True,
                "preparing_stage_remaining_seconds": 100,
                "preparing_turn_remaining_seconds": 40,
            }
        ]
        room.current_stage_index = 0
        room.status = "running"
        room.stage_started_at = now() - timedelta(seconds=90)
        room.stage_deadline_at = now() - timedelta(seconds=1)
        db.add(
            Speech(
                match_id=match.id,
                room_id=room.id,
                seat_key="aff_1",
                stage_key="free",
                speaker_type="ai",
                status="synthesizing",
                content="已生成的测试内容",
            )
        )
        db.commit()

    paused = owner.post(f"/api/rooms/{code}/control/pause", headers=csrf(owner), json={"reason": "准备中暂停"})
    assert paused.status_code == 200
    paused_room = paused.json()["room"]
    assert paused_room["remaining_seconds"] == 100
    assert paused_room["turn_remaining_seconds"] == 40
    assert "ai_preparing" not in paused_room["current_stage"]
    with SessionLocal() as db:
        room = load_room(db, code)
        speech = db.scalar(select(Speech).where(Speech.room_id == room.id))
        assert speech and speech.status == "interrupted"

    resumed = owner.post(f"/api/rooms/{code}/control/resume", headers=csrf(owner), json={"reason": "准备中暂停后恢复"})
    assert resumed.status_code == 200
    resumed_room = resumed.json()["room"]
    assert resumed_room["remaining_seconds"] in {99, 100}
    assert resumed_room["turn_remaining_seconds"] in {39, 40}
    assert "ai_preparing" not in resumed_room["current_stage"]


def test_four_human_free_debate_rotates_sides_and_allows_only_one_speaker(client: TestClient, register_user) -> None:
    owner = register_user("mixed_humans_aff1")
    aff_two = register_user("mixed_humans_aff2")
    neg_one = register_user("mixed_humans_neg1")
    neg_two = register_user("mixed_humans_neg2")
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
    for participant, seat_key in [(aff_two, "aff_2"), (neg_one, "neg_1"), (neg_two, "neg_2")]:
        assert (
            participant.post(
                f"/api/rooms/{code}/claim-seat",
                headers=csrf(participant),
                json={"seat_key": seat_key},
            ).status_code
            == 200
        )
    for participant in [owner, aff_two, neg_one, neg_two]:
        assert participant.post(f"/api/rooms/{code}/ready", headers=csrf(participant), json={"ready": True}).status_code == 200
    assert owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={}).status_code == 200
    with SessionLocal() as db:
        stored = load_room(db, code, lock=True)
        stored.template_snapshot = [
            {"key": "free", "name": "自由辩论", "kind": "free", "side": "aff", "duration": 300, "turn_duration": 40}
        ]
        stored.status = "running"
        stored.current_stage_index = 0
        stored.stage_started_at = now()
        stored.stage_deadline_at = now() + timedelta(seconds=300)
        db.commit()
    participants = [(owner, "lease-aff1"), (aff_two, "lease-aff2"), (neg_one, "lease-neg1"), (neg_two, "lease-neg2")]
    for participant, lease in participants:
        assert (
            participant.post(
                f"/api/rooms/{code}/control-lease",
                headers=csrf(participant) | {"X-Control-Lease": lease},
                json={},
            ).status_code
            == 200
        )
    aff_started = owner.post(
        f"/api/rooms/{code}/speech/start",
        headers=csrf(owner) | {"X-Control-Lease": "lease-aff1"},
        json={},
    )
    competing_aff = aff_two.post(
        f"/api/rooms/{code}/speech/start",
        headers=csrf(aff_two) | {"X-Control-Lease": "lease-aff2"},
        json={},
    )
    assert aff_started.status_code == 200 and competing_aff.status_code == 409
    assert (
        owner.post(
            f"/api/rooms/{code}/speech/finish",
            headers=csrf(owner) | {"X-Control-Lease": "lease-aff1"},
            json={"speech_id": aff_started.json()["speech_id"], "content": "正方第一轮自由辩论。"},
        ).status_code
        == 200
    )
    neg_started = neg_two.post(
        f"/api/rooms/{code}/speech/start",
        headers=csrf(neg_two) | {"X-Control-Lease": "lease-neg2"},
        json={},
    )
    assert neg_started.status_code == 200
    assert (
        neg_two.post(
            f"/api/rooms/{code}/speech/finish",
            headers=csrf(neg_two) | {"X-Control-Lease": "lease-neg2"},
            json={"speech_id": neg_started.json()["speech_id"], "content": "反方第一轮自由辩论。"},
        ).status_code
        == 200
    )
    aff_two_view = aff_two.get(f"/api/rooms/{code}").json()["room"]
    assert aff_two_view["current_stage"]["side"] == "aff" and aff_two_view["can_speak"] is True
    with SessionLocal() as db:
        stored = load_room(db, code)
        speeches = list(db.scalars(select(Speech).where(Speech.room_id == stored.id).order_by(Speech.created_at)).all())
        assert [(item.seat_key, item.content) for item in speeches] == [
            ("aff_1", "正方第一轮自由辩论。"),
            ("neg_2", "反方第一轮自由辩论。"),
        ]


def test_skip_closes_active_speech(client: TestClient, register_user) -> None:
    owner = register_user("skip_owner")
    code, speech_id = prepare_human_speech(owner)
    skipped = owner.post(
        f"/api/rooms/{code}/control/skip",
        headers=csrf(owner),
        json={"reason": "test"},
    )
    assert skipped.status_code == 200, skipped.text
    with SessionLocal() as db:
        assert db.get(Speech, speech_id).status == "interrupted"


def test_control_operation_is_idempotent(client: TestClient, register_user) -> None:
    owner = register_user("control_idempotency")
    code, speech_id = prepare_human_speech(owner)
    headers = csrf(owner) | {"X-Idempotency-Key": "skip-current-stage-once"}
    first = owner.post(f"/api/rooms/{code}/control/skip", headers=headers, json={"reason": "network retry"})
    replayed = owner.post(f"/api/rooms/{code}/control/skip", headers=headers, json={"reason": "network retry"})
    conflicting = owner.post(f"/api/rooms/{code}/control/skip", headers=headers, json={"reason": "different request"})

    assert first.status_code == 200
    assert replayed.status_code == 200 and replayed.json()["replayed"] is True
    assert conflicting.status_code == 409 and "幂等键" in conflicting.json()["detail"]
    with SessionLocal() as db:
        room = load_room(db, code)
        events = list(db.scalars(select(MatchEvent).where(MatchEvent.room_id == room.id, MatchEvent.event_type == "control.skip")).all())
        assert len(events) == 1
        assert db.get(Speech, speech_id).status == "interrupted"
    public_room = client.get(f"/api/rooms/{code}/public").json()["room"]
    public_skip = next(item for item in public_room["recent_events"] if item["type"] == "control.skip")
    assert public_skip["payload"] == {"reason": "network retry"}


def test_timeout_closes_active_speech(client: TestClient, register_user) -> None:
    owner = register_user("timeout_owner")
    code, speech_id = prepare_human_speech(owner)
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        closed = match_engine.close_active_speeches(db, room, status="timed_out", reason="timer_elapsed")
        db.commit()
        assert speech_id in closed
        assert db.get(Speech, speech_id).status == "timed_out"


def test_asr_final_is_persisted(client: TestClient, register_user) -> None:
    owner = register_user("asr_owner")
    _, speech_id = prepare_human_speech(owner)
    assert persist_asr_final(speech_id, "这是第一句最终字幕。") is True
    with SessionLocal() as db:
        speech = db.get(Speech, speech_id)
        segment = db.scalar(select(TranscriptSegment).where(TranscriptSegment.speech_id == speech_id))
        assert speech.content == "这是第一句最终字幕。"
        assert segment and segment.text == "这是第一句最终字幕。"


@pytest.mark.parametrize("hallucination", ["七", "去"])
def test_persist_asr_final_rejects_single_character_hallucination(
    client: TestClient,
    register_user,
    hallucination: str,
) -> None:
    owner = register_user(f"asr_persist_short_{ord(hallucination)}")
    _, speech_id = prepare_human_speech(owner)
    assert persist_asr_final(speech_id, hallucination) is False
    with SessionLocal() as db:
        speech = db.get(Speech, speech_id)
        segments = list(db.scalars(select(TranscriptSegment).where(TranscriptSegment.speech_id == speech_id)).all())
        assert speech and speech.content == "" and segments == []


def test_public_projection_redacts_internal_failures(client: TestClient, register_user) -> None:
    owner = register_user("privacy_owner")
    outsider = register_user("privacy_outsider")
    room_data = create_training_room(owner, "公开观战脱敏测试")
    owner.post(f"/api/rooms/{room_data['code']}/ready", headers=csrf(owner), json={"ready": True})
    owner.post(f"/api/rooms/{room_data['code']}/start", headers=csrf(owner), json={})
    with SessionLocal() as db:
        room = load_room(db, room_data["code"], lock=True)
        room.failure_reason = "prompt file missing: /secret/provider/path.wav"
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        for existing in db.scalars(
            select(Speech).where(Speech.room_id == room.id, Speech.status.in_(["speaking", "synthesizing", "playing"]))
        ).all():
            existing.status = "interrupted"
        playback_started_at = now()
        streamed = Speech(
            match_id=match.id,
            room_id=room.id,
            seat_key="neg_1",
            stage_key="neg_1_case",
            speaker_type="ai",
            content="匿名观战仍能看到实时字幕。",
            audio_url=f"/media/{room.code}/public-fallback.wav",
            duration_seconds=18.5,
            playback_started_at=playback_started_at,
            playback_ends_at=playback_started_at + timedelta(seconds=18.5),
            stream_generation="a" * 32,
            stream_sample_rate=24_000,
            status="playing",
        )
        db.add(streamed)
        db.flush()
        append_event(
            db,
            room,
            "audio.rtc.started",
            {
                "speech_id": streamed.id,
                "generation": streamed.stream_generation,
                "sample_rate": streamed.stream_sample_rate,
                "transport": "livekit",
                "track_sid": "TR_secret_track",
                "tts_session_id": "tts-secret-session",
                "voice_id": "private-voice-id",
                "synthesis_mode": "bistream",
                "agent_first_readable_delta_at": "2026-07-19T01:02:03Z",
                "server_first_capture_at": "2026-07-19T01:02:04Z",
                "flush_guard_ms": 220,
                "llm_endpoint": "https://llm.internal.example/v1",
            },
        )
        append_event(
            db,
            room,
            "provider.failed",
            {
                "provider": "lighttts",
                "message": "missing /secret/provider/path.wav",
                "user_id": owner.get("/api/auth/session").json()["user"]["id"],
            },
        )
        db.commit()
    public = client.get(f"/api/rooms/{room_data['code']}/public")
    assert public.status_code == 200
    room = public.json()["room"]
    assert room["failure_reason"] == "服务暂时异常，比赛已安全暂停。"
    assert room["active_speech"] == {
        "id": streamed.id,
        "seat_key": "neg_1",
        "speaker_type": "ai",
        "status": "playing",
        "content": "匿名观战仍能看到实时字幕。",
        "playback_started_at": playback_started_at.replace(tzinfo=None).isoformat(),
        "stream_generation": "a" * 32,
        "stream_sample_rate": 24_000,
    }
    fallback = next(item for item in room["speeches"] if item["id"] == streamed.id)
    assert fallback["audio_url"].endswith("/public-fallback.wav")
    assert fallback["duration_seconds"] == 18.5
    assert fallback["playback_started_at"] == playback_started_at.replace(tzinfo=None).isoformat()
    rtc_event = next(item for item in room["recent_events"] if item["type"] == "audio.rtc.started")
    assert rtc_event["payload"] == {"speech_id": streamed.id, "generation": "a" * 32}
    failed = next(item for item in room["recent_events"] if item["type"] == "provider.failed")
    assert failed["payload"] == {"message": "服务暂时异常"}
    owner_view = owner.get(f"/api/rooms/{room_data['code']}").json()["room"]
    assert owner_view["failure_reason"] == "服务暂时异常，比赛已安全暂停。"
    owner_failed = next(item for item in owner_view["recent_events"] if item["type"] == "provider.failed")
    assert owner_failed["payload"] == {"provider": "lighttts", "message": "服务暂时异常"}
    owner_rtc = next(item for item in owner_view["recent_events"] if item["type"] == "audio.rtc.started")
    assert owner_rtc["payload"]["transport"] == "livekit"
    assert owner_rtc["payload"]["voice_id"] == "private-voice-id"
    with TestClient(client.app) as admin:
        assert admin.post("/api/auth/login", json={"account": "admin_test", "password": "Admin-test-1234"}).status_code == 200
        admin_view = admin.get(f"/api/rooms/{room_data['code']}").json()["room"]
        assert admin_view["failure_reason"] == "prompt file missing: /secret/provider/path.wav"
        admin_failed = next(item for item in admin_view["recent_events"] if item["type"] == "provider.failed")
        assert admin_failed["payload"]["message"] == "missing /secret/provider/path.wav"
        admin_rtc = next(item for item in admin_view["recent_events"] if item["type"] == "audio.rtc.started")
        assert admin_rtc["payload"]["llm_endpoint"] == "https://llm.internal.example/v1"
    outsider_view = outsider.get(f"/api/rooms/{room_data['code']}").json()["room"]
    assert outsider_view["failure_reason"] == "服务暂时异常，比赛已安全暂停。"
    outsider_failed = next(item for item in outsider_view["recent_events"] if item["type"] == "provider.failed")
    assert outsider_failed["payload"] == {"message": "服务暂时异常"}
    public_result = client.get(f"/api/rooms/{room_data['code']}/result")
    assert public_result.status_code == 409
    assert public_result.json()["detail"] == "比赛尚未结束，请前往观战页面查看实时内容。"


def test_anonymous_realtime_event_allows_captions_and_flush_identity_only() -> None:
    caption = room_service_service.anonymous_realtime_event(
        {
            "type": "asr",
            "seq": 42,
            "text": "观众需要看到的实时字幕",
            "is_final": False,
            "room_code": "123456",
            "speech_id": "internal-speech-id",
            "transport": "websocket_pcm",
            "agent_latency_ms": 812,
        }
    )
    assert caption == {"type": "asr", "seq": 42, "text": "观众需要看到的实时字幕", "is_final": False}

    interrupted = room_service_service.anonymous_realtime_event(
        {
            "type": "audio.rtc.interrupt",
            "seq": 43,
            "generation": "b" * 32,
            "track_sid": "TR_private",
            "tts_session_id": "tts-private",
            "interrupt_id": "interrupt-private",
            "flush_guard_ms": 220,
        }
    )
    assert interrupted == {"type": "audio.rtc.interrupt", "seq": 43, "generation": "b" * 32}


async def test_agent_failure_cannot_resurrect_terminated_room(client: TestClient, register_user, monkeypatch) -> None:
    owner = register_user("terminate_race")
    room_data = create_training_room(owner, "终止竞态测试")
    code = room_data["code"]
    owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})
    owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})

    async def delayed_failure(*args, **kwargs):
        await asyncio.sleep(0.08)
        raise ProviderError("upstream timeout")

    monkeypatch.setattr(debate_agent, "generate", delayed_failure)
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.status = "running"
        room.current_stage_index = 2
        room.stage_started_at = now()
        room.stage_deadline_at = now() + timedelta(seconds=150)
        db.commit()
        current = room.template_snapshot[2]
        seat = next(item for item in room.seats if item.seat_key == "neg_1")

        async def terminate() -> None:
            await asyncio.sleep(0.02)
            with SessionLocal() as other:
                stored = load_room(other, code, lock=True)
                stored.status = "terminated"
                other.commit()

        await asyncio.gather(match_engine._ai_speech(db, room, seat, current), terminate())

    with SessionLocal() as db:
        room = load_room(db, code)
        assert room.status == "terminated"
        speech = db.scalar(select(Speech).where(Speech.room_id == room.id).order_by(Speech.created_at.desc()))
        assert speech and speech.status == "interrupted"


async def test_late_agent_result_cannot_resurrect_skipped_stage(client: TestClient, register_user, monkeypatch) -> None:
    owner = register_user("skip_agent_race")
    room_data = create_training_room(owner, "跳过阶段后迟到 Agent 结果测试")
    code = room_data["code"]
    owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})
    owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})

    async def delayed_success(*args, **kwargs):
        await asyncio.sleep(0.08)
        return "这段迟到内容不得复活已跳过的发言。"

    async def forbidden_tts(*args, **kwargs):
        raise AssertionError("stale Agent result must not reach TTS")

    monkeypatch.setattr(debate_agent, "generate", delayed_success)
    monkeypatch.setattr(lighttts, "synthesize", forbidden_tts)
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.status = "running"
        room.current_stage_index = 2
        room.stage_started_at = now()
        room.stage_deadline_at = now() + timedelta(seconds=150)
        db.commit()
        current = dict(room.template_snapshot[2])
        seat = next(item for item in room.seats if item.seat_key == "neg_1")

        async def skip_stage() -> None:
            await asyncio.sleep(0.02)
            with SessionLocal() as other:
                stored = load_room(other, code, lock=True)
                match_engine.close_active_speeches(other, stored, status="interrupted", reason="manual_skip")
                match_engine._advance(other, stored, reason="manual_skip")
                other.commit()

        await asyncio.gather(match_engine._ai_speech(db, room, seat, current), skip_stage())

    with SessionLocal() as db:
        room = load_room(db, code)
        speech = db.scalar(select(Speech).where(Speech.room_id == room.id, Speech.stage_key == "neg_1_case"))
        assert room.current_stage_index == 3
        assert speech and speech.status == "interrupted" and speech.audio_url == ""
        assert not db.scalar(
            select(MatchEvent.id).where(
                MatchEvent.room_id == room.id,
                MatchEvent.event_type == "speech.audio.ready",
                MatchEvent.payload["speech_id"].as_string() == speech.id,
            )
        )


async def test_interrupted_ai_stage_is_retryable_after_resume(client: TestClient, register_user, monkeypatch) -> None:
    owner = register_user("resume_ai_retry")
    room_data = create_training_room(owner, "暂停恢复后的 AI 发言重试测试")
    code = room_data["code"]
    owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})
    owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        room.status = "running"
        room.current_stage_index = 2
        room.stage_started_at = now()
        room.stage_deadline_at = now() + timedelta(seconds=150)
        db.add(
            Speech(
                match_id=match.id,
                room_id=room.id,
                seat_key="neg_1",
                stage_key="neg_1_case",
                speaker_type="ai",
                status="interrupted",
            )
        )
        db.commit()

    called: list[str] = []

    async def record_retry(db, room, seat, current, **kwargs):
        called.append(seat.seat_key)

    monkeypatch.setattr(match_engine, "_ai_speech", record_retry)
    await match_engine.process_room(code)
    assert called == ["neg_1"]


async def test_ai_bistream_persists_authoritative_generation_before_final_wav(client: TestClient, register_user, monkeypatch) -> None:
    owner = register_user("ai_bistream_state")
    room_data = create_training_room(owner, "流式语音先播放后归档状态测试")
    code = room_data["code"]
    owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})
    owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    with SessionLocal() as db:
        target_room = load_room(db, code, lock=True)
        target_room.status = "running"
        target_room.current_stage_index = 2
        target_room.stage_started_at = now()
        target_room.stage_deadline_at = now() + timedelta(seconds=150)
        db.commit()

    generation = uuid.uuid4().hex
    started = asyncio.Event()
    release = asyncio.Event()

    async def generated_content(*_args, **_kwargs) -> str:
        return "流式输出应在最终 WAV 完成前建立权威播放 generation。"

    async def streaming_tts(*_args, room_code: str, speech_id: str, on_stream_event, **_kwargs) -> str:
        await on_stream_event(
            {
                "type": "audio.stream.started",
                "room_code": room_code,
                "speech_id": speech_id,
                "generation": generation,
                "stream_url": f"/ws/rooms/{room_code}/audio",
                "sample_rate": 24_000,
                "channels": 1,
                "sample_width": 2,
            }
        )
        started.set()
        await release.wait()
        target = settings.media_path / room_code / f"{speech_id}.wav"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_wav_bytes(2.0))
        return f"/media/{room_code}/{speech_id}.wav"

    monkeypatch.setattr(settings, "lighttts_streaming_enabled", True)
    monkeypatch.setattr(settings, "lighttts_bistream_enabled", True)
    monkeypatch.setattr(debate_agent, "generate", generated_content)
    monkeypatch.setattr(lighttts, "synthesize", streaming_tts)
    processing = asyncio.create_task(match_engine.process_room(code))
    await asyncio.wait_for(started.wait(), timeout=2)
    with SessionLocal() as db:
        target_room = load_room(db, code)
        speech = db.scalar(select(Speech).where(Speech.room_id == target_room.id, Speech.stage_key == "neg_1_case"))
        stream_event = db.scalar(
            select(MatchEvent).where(
                MatchEvent.room_id == target_room.id,
                MatchEvent.event_type == "audio.stream.started",
            )
        )
        assert speech and speech.status == "synthesizing"
        assert speech.stream_generation == generation and speech.stream_sample_rate == 24_000
        assert speech.playback_started_at is not None
        assert target_room.stage_deadline_at is not None
        assert stream_event and stream_event.payload["generation"] == generation
        started_at = speech.playback_started_at

    release.set()
    await asyncio.wait_for(processing, timeout=2)
    with SessionLocal() as db:
        target_room = load_room(db, code)
        speech = db.scalar(select(Speech).where(Speech.room_id == target_room.id, Speech.stage_key == "neg_1_case"))
        ready = db.scalar(
            select(MatchEvent).where(
                MatchEvent.room_id == target_room.id,
                MatchEvent.event_type == "speech.audio.ready",
            )
        )
        assert speech and speech.status == "playing" and speech.audio_url.endswith(f"/{speech.id}.wav")
        assert speech.playback_started_at == started_at
        assert ready and ready.payload["streamed"] is True and ready.payload["stream_generation"] == generation


async def test_ai_one_shot_tts_wav_uses_livekit_fallback_without_enabling_bistream(
    client: TestClient,
    register_user,
    monkeypatch,
) -> None:
    owner = register_user("ai_livekit_wav_fallback")
    room_data = create_training_room(owner, "一次性 TTS WAV 经 LiveKit 发布测试")
    code = room_data["code"]
    owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})
    owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    with SessionLocal() as db:
        target_room = load_room(db, code, lock=True)
        target_room.status = "running"
        target_room.current_stage_index = 2
        target_room.stage_started_at = now()
        target_room.stage_deadline_at = now() + timedelta(seconds=150)
        db.commit()

    generation = uuid.uuid4().hex
    synthesized = False
    published = False

    async def generated_content(*_args, **_kwargs) -> str:
        return "关闭 bi-stream 时先完成一次性 TTS，再通过 LiveKit 连续播放。"

    async def one_shot_tts(*_args, room_code: str, speech_id: str, on_stream_event, **_kwargs) -> str:
        nonlocal synthesized
        synthesized = True
        assert on_stream_event is None, "streaming=false must never enter the bi-stream callback path"
        target = settings.media_path / room_code / f"{speech_id}.wav"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_wav_bytes(1.0))
        return f"/media/{room_code}/{speech_id}.wav"

    async def publish_wav(*, room_code: str, speech_id: str, should_cancel, on_stream_event) -> str:
        nonlocal published
        published = True
        assert should_cancel() is False
        await on_stream_event(
            {
                "type": "audio.stream.started",
                "room_code": room_code,
                "speech_id": speech_id,
                "generation": generation,
                "stream_url": None,
                "sample_rate": 24_000,
                "channels": 1,
                "sample_width": 2,
                "transport": "livekit",
                "track_sid": "TR_fallback",
                "server_first_capture_at": now().isoformat(),
            }
        )
        return generation

    monkeypatch.setattr(settings, "webrtc_audio_enabled", True)
    monkeypatch.setattr(settings, "webrtc_audio_backend", "livekit")
    monkeypatch.setattr(settings, "lighttts_streaming_enabled", False)
    monkeypatch.setattr(settings, "lighttts_bistream_enabled", False)
    monkeypatch.setattr(settings, "realtime_voice_pipeline_enabled", False)
    monkeypatch.setattr(debate_agent, "generate", generated_content)
    monkeypatch.setattr(lighttts, "synthesize", one_shot_tts)
    monkeypatch.setattr(lighttts, "publish_wav_to_livekit", publish_wav)

    await match_engine.process_room(code)

    assert synthesized is True and published is True
    with SessionLocal() as db:
        room = load_room(db, code)
        speech = db.scalar(select(Speech).where(Speech.room_id == room.id, Speech.stage_key == "neg_1_case"))
        rtc_started = db.scalar(
            select(MatchEvent).where(
                MatchEvent.room_id == room.id,
                MatchEvent.event_type == "audio.rtc.started",
            )
        )
        assert speech and speech.status == "playing"
        assert speech.stream_generation == generation
        assert speech.audio_url.endswith(f"/{speech.id}.wav")
        assert rtc_started and rtc_started.payload["transport"] == "livekit"


async def test_realtime_voice_pipeline_starts_tts_from_agent_delta_before_final(
    client: TestClient,
    register_user,
    monkeypatch,
) -> None:
    owner = register_user("agent_delta_to_tts")
    room_data = create_training_room(owner, "Agent delta 直达连续 TTS 测试")
    code = room_data["code"]
    owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})
    owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    with SessionLocal() as db:
        target_room = load_room(db, code, lock=True)
        target_room.status = "running"
        target_room.current_stage_index = 2
        target_room.stage_started_at = now()
        target_room.stage_deadline_at = now() + timedelta(seconds=150)
        db.commit()

    generation = uuid.uuid4().hex
    first_pcm = asyncio.Event()
    release_final = asyncio.Event()
    chunks: list[str] = []

    async def generated_stream(*_args, **_kwargs):
        yield {"type": "delta", "delta": "我方先提出第一点。"}
        await release_final.wait()
        yield {"type": "delta", "delta": "再补充第二点。"}
        yield {"type": "final", "content": "我方先提出第一点。再补充第二点。"}

    class FakeIncrementalSession:
        def __init__(self, *, room_code: str, speech_id: str, on_stream_event) -> None:
            self.room_code = room_code
            self.speech_id = speech_id
            self.on_stream_event = on_stream_event
            self.started = False

        async def push_text(self, text: str) -> None:
            chunks.append(text)
            if self.started:
                return
            self.started = True
            await self.on_stream_event(
                {
                    "type": "audio.stream.started",
                    "room_code": self.room_code,
                    "speech_id": self.speech_id,
                    "generation": generation,
                    "stream_url": f"/ws/rooms/{self.room_code}/audio",
                    "sample_rate": 24_000,
                    "channels": 1,
                    "sample_width": 2,
                }
            )
            first_pcm.set()

        async def finish(self) -> str:
            target = settings.media_path / self.room_code / f"{self.speech_id}.wav"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(_wav_bytes(2.0))
            return f"/media/{self.room_code}/{self.speech_id}.wav"

        async def abort(self) -> None:
            return None

    def open_session(*, room_code: str, speech_id: str, on_stream_event, **_kwargs):
        return FakeIncrementalSession(room_code=room_code, speech_id=speech_id, on_stream_event=on_stream_event)

    async def forbidden_full_text_tts(*_args, **_kwargs):
        raise AssertionError("realtime pipeline must not invoke full-text synthesize")

    monkeypatch.setattr(settings, "lighttts_streaming_enabled", True)
    monkeypatch.setattr(settings, "lighttts_bistream_enabled", True)
    monkeypatch.setattr(settings, "realtime_voice_pipeline_enabled", True)
    monkeypatch.setattr(debate_agent, "generate_stream", generated_stream)
    monkeypatch.setattr(lighttts, "open_incremental_session", open_session)
    monkeypatch.setattr(lighttts, "synthesize", forbidden_full_text_tts)

    processing = asyncio.create_task(match_engine.process_room(code))
    await asyncio.wait_for(first_pcm.wait(), timeout=2)
    with SessionLocal() as db:
        target_room = load_room(db, code)
        speech = db.scalar(select(Speech).where(Speech.room_id == target_room.id, Speech.stage_key == "neg_1_case"))
        assert speech and speech.status == "synthesizing"
        assert speech.stream_generation == generation
        assert chunks == ["我方先提出第一点。"]
        assert speech.content == ""
        rtc_started = db.scalar(
            select(MatchEvent).where(
                MatchEvent.room_id == target_room.id,
                MatchEvent.event_type == "audio.stream.started",
            )
        )
        assert rtc_started and rtc_started.payload["agent_first_readable_delta_at"]

    release_final.set()
    await asyncio.wait_for(processing, timeout=2)
    with SessionLocal() as db:
        target_room = load_room(db, code)
        speech = db.scalar(select(Speech).where(Speech.room_id == target_room.id, Speech.stage_key == "neg_1_case"))
        assert speech and speech.status == "playing"
        assert speech.content == "我方先提出第一点。再补充第二点。"
        assert speech.audio_url.endswith(f"/{speech.id}.wav")
        assert chunks == ["我方先提出第一点。", "再补充第二点。"]


async def test_realtime_voice_pipeline_preserves_final_text_when_tts_fails(
    client: TestClient,
    register_user,
    monkeypatch,
) -> None:
    owner = register_user("agent_delta_tts_failure")
    room_data = create_training_room(owner, "连续 TTS 失败保留权威逐字稿")
    code = room_data["code"]
    owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})
    owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    with SessionLocal() as db:
        target_room = load_room(db, code, lock=True)
        target_room.status = "running"
        target_room.current_stage_index = 2
        target_room.stage_started_at = now()
        target_room.stage_deadline_at = now() + timedelta(seconds=150)
        db.commit()

    final_text = "即使语音合成失败，Agent 最终正文也必须完整保存。"

    async def generated_stream(*_args, **_kwargs):
        yield {"type": "delta", "delta": final_text}
        yield {"type": "final", "content": final_text}

    class FailingSession:
        async def push_text(self, _text: str) -> None:
            return None

        async def finish(self) -> str:
            raise ProviderError("候选 TTS 解码失败。")

        async def abort(self) -> None:
            return None

    monkeypatch.setattr(settings, "lighttts_streaming_enabled", True)
    monkeypatch.setattr(settings, "lighttts_bistream_enabled", True)
    monkeypatch.setattr(settings, "realtime_voice_pipeline_enabled", True)
    monkeypatch.setattr(debate_agent, "generate_stream", generated_stream)
    monkeypatch.setattr(lighttts, "open_incremental_session", lambda **_kwargs: FailingSession())

    await match_engine.process_room(code)

    with SessionLocal() as db:
        target_room = load_room(db, code)
        speech = db.scalar(select(Speech).where(Speech.room_id == target_room.id, Speech.stage_key == "neg_1_case"))
        failure = db.scalar(
            select(MatchEvent)
            .where(MatchEvent.room_id == target_room.id, MatchEvent.event_type == "provider.failed")
            .order_by(MatchEvent.seq.desc())
        )
        assert target_room.status == "paused"
        assert speech and speech.status == "failed" and speech.content == final_text
        assert failure and failure.payload["provider"] == "lighttts"


async def test_realtime_voice_pause_interrupts_agent_clears_tts_and_keeps_unplayed_text_out_of_history(
    client: TestClient,
    register_user,
    monkeypatch,
) -> None:
    owner = register_user("realtime_full_interrupt")
    room_data = create_training_room(owner, "实时语音全链路打断测试")
    code = room_data["code"]
    owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})
    owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    with SessionLocal() as db:
        target_room = load_room(db, code, lock=True)
        target_room.status = "running"
        target_room.current_stage_index = 2
        target_room.stage_started_at = now()
        target_room.stage_deadline_at = now() + timedelta(seconds=150)
        db.commit()

    first_text_submitted = asyncio.Event()
    tts_aborted = asyncio.Event()
    agent_interrupted = asyncio.Event()
    generator_closed = asyncio.Event()

    async def generated_stream(*_args, **_kwargs):
        try:
            yield {"type": "delta", "delta": "这段文字已经进入语音队列，"}
            await asyncio.Event().wait()
        finally:
            generator_closed.set()

    async def interrupt_agent(task_id: str, **_kwargs) -> bool:
        assert task_id
        agent_interrupted.set()
        return True

    class InterruptibleSession:
        async def push_text(self, text: str) -> None:
            assert text == "这段文字已经进入语音队列，"
            first_text_submitted.set()

        async def finish(self) -> str:
            raise AssertionError("interrupted TTS must not finish")

        async def abort(self) -> None:
            tts_aborted.set()

    monkeypatch.setattr(settings, "lighttts_streaming_enabled", True)
    monkeypatch.setattr(settings, "lighttts_bistream_enabled", True)
    monkeypatch.setattr(settings, "realtime_voice_pipeline_enabled", True)
    monkeypatch.setattr(debate_agent, "generate_stream", generated_stream)
    monkeypatch.setattr(debate_agent, "interrupt", interrupt_agent)
    monkeypatch.setattr(lighttts, "open_incremental_session", lambda **_kwargs: InterruptibleSession())

    processing = asyncio.create_task(match_engine.process_room(code))
    await asyncio.wait_for(first_text_submitted.wait(), timeout=2)
    paused = owner.post(f"/api/rooms/{code}/control/pause", headers=csrf(owner), json={"reason": "立即打断"})
    assert paused.status_code == 200, paused.text
    await asyncio.wait_for(processing, timeout=2)

    assert agent_interrupted.is_set()
    assert tts_aborted.is_set()
    assert generator_closed.is_set()
    with SessionLocal() as db:
        room = load_room(db, code)
        speech = db.scalar(select(Speech).where(Speech.room_id == room.id, Speech.stage_key == "neg_1_case"))
        assert speech and speech.status == "interrupted"
        assert speech.content == "" and speech.audio_url == ""
        assert match_engine._history(db, room.id) == []


async def test_pause_resume_invalidates_inflight_ai_generation(client: TestClient, register_user, monkeypatch) -> None:
    owner = register_user("pause_agent_race")
    room_data = create_training_room(owner, "暂停恢复不得复活旧 AI 生成任务")
    code = room_data["code"]
    owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})
    owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    started = asyncio.Event()
    release = asyncio.Event()

    async def delayed_success(*args, **kwargs):
        started.set()
        await release.wait()
        return "暂停前的迟到内容不得继续进入语音合成。"

    async def forbidden_tts(*args, **kwargs):
        raise AssertionError("interrupted pre-pause Agent result must not reach TTS")

    monkeypatch.setattr(debate_agent, "generate", delayed_success)
    monkeypatch.setattr(lighttts, "synthesize", forbidden_tts)
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.status = "running"
        room.current_stage_index = 2
        room.stage_started_at = now()
        room.stage_deadline_at = now() + timedelta(seconds=150)
        db.commit()

    processing = asyncio.create_task(match_engine.process_room(code))
    await asyncio.wait_for(started.wait(), timeout=2)
    paused = owner.post(f"/api/rooms/{code}/control/pause", headers=csrf(owner), json={"reason": "临时暂停"})
    assert paused.status_code == 200, paused.text
    resumed = owner.post(f"/api/rooms/{code}/control/resume", headers=csrf(owner), json={"reason": "立即恢复"})
    assert resumed.status_code == 200, resumed.text
    release.set()
    await asyncio.wait_for(processing, timeout=2)

    with SessionLocal() as db:
        room = load_room(db, code)
        stale = db.scalar(select(Speech).where(Speech.room_id == room.id, Speech.stage_key == "neg_1_case").order_by(Speech.created_at))
        assert room.status == "running" and room.current_stage_index == 2
        assert stale and stale.status == "interrupted" and stale.audio_url == ""
        interruption = db.scalar(
            select(MatchEvent).where(
                MatchEvent.room_id == room.id,
                MatchEvent.event_type == "speech.interrupted",
                MatchEvent.payload["reason"].as_string() == "manual_pause",
            )
        )
        assert interruption

    retried: list[str] = []

    async def record_retry(db, room, seat, current, **kwargs):
        retried.append(seat.seat_key)

    monkeypatch.setattr(match_engine, "_ai_speech", record_retry)
    await match_engine.process_room(code)
    assert retried == ["neg_1"]


async def test_cue_generation_releases_room_lock_and_honors_termination(client: TestClient, register_user, monkeypatch) -> None:
    owner = register_user("cue_cancel")
    room_data = create_training_room(owner, "提示音生成期间终止测试")
    code = room_data["code"]
    owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})
    owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.template_snapshot = [
            {"key": "opening", "name": "开场", "kind": "announcement", "duration": 10, "cue": "正在生成提示音。"},
            {"key": "judging", "name": "裁判", "kind": "judging", "duration": 10},
        ]
        db.commit()

    started = asyncio.Event()
    release = asyncio.Event()
    generated_path: Path | None = None

    async def delayed_tts(*args, speech_id: str, **kwargs):
        nonlocal generated_path
        started.set()
        await release.wait()
        generated_path = settings.media_path / code / f"{speech_id}.wav"
        generated_path.parent.mkdir(parents=True, exist_ok=True)
        generated_path.write_bytes(_wav_bytes())
        return f"/media/{code}/{speech_id}.wav"

    monkeypatch.setattr(lighttts, "synthesize", delayed_tts)
    processing = asyncio.create_task(match_engine.process_room(code))
    await asyncio.wait_for(started.wait(), timeout=2)
    lock_started = time.perf_counter()
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        room.status = "terminated"
        match.status = "terminated"
        db.commit()
    assert time.perf_counter() - lock_started < 0.5
    release.set()
    await asyncio.wait_for(processing, timeout=2)
    with SessionLocal() as db:
        room = load_room(db, code)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        assert room.status == "terminated" and room.current_stage_index == -1
        assert not db.scalar(select(AudioAsset.id).where(AudioAsset.match_id == match.id))
    assert generated_path is not None and not generated_path.exists()


async def test_engine_restart_interrupts_only_engine_owned_inflight_tasks(client: TestClient, register_user, monkeypatch) -> None:
    owner = register_user("engine_restart_recovery")
    room_data = create_training_room(owner, "引擎重启后异步任务恢复测试")
    code = room_data["code"]
    owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})
    owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.status = "running"
        room.current_stage_index = 0
        room.stage_started_at = now()
        room.stage_deadline_at = now() + timedelta(seconds=120)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        speech = Speech(
            match_id=match.id,
            room_id=room.id,
            seat_key="neg_1",
            stage_key=room.template_snapshot[0]["key"],
            speaker_type="ai",
            status="synthesizing",
            content="重启前已经生成但尚未完成语音的文本。",
        )
        db.add(speech)
        db.flush()
        scorecard = JudgeScorecard(match_id=match.id, status="running", reasoning="重启前正在裁判")
        db.add(scorecard)
        db.commit()
        speech_id = speech.id
        scorecard_id = scorecard.id
    target_dir = settings.media_path / code
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{speech_id}.wav"
    partial = target_dir / f".{speech_id}.restart.wav.part"
    target.write_bytes(_wav_bytes())
    partial.write_bytes(b"partial")

    assert recover_inflight_engine_tasks() == {"speeches": 1, "judges": 1}
    assert recover_inflight_engine_tasks() == {"speeches": 0, "judges": 0}
    with SessionLocal() as db:
        room = load_room(db, code)
        speech = db.get(Speech, speech_id)
        scorecard = db.get(JudgeScorecard, scorecard_id)
        reasons = {
            item.payload.get("reason")
            for item in db.scalars(
                select(MatchEvent).where(
                    MatchEvent.room_id == room.id,
                    MatchEvent.event_type.in_(["speech.interrupted", "judge.interrupted"]),
                )
            ).all()
        }
        assert speech and speech.status == "interrupted"
        assert scorecard and scorecard.status == "interrupted"
        assert reasons == {"engine_restart"}
    assert not target.exists() and not partial.exists()

    async def forbidden_agent(*args, **kwargs):
        raise AssertionError("engine restart must reuse already generated TTS-only content")

    async def recovered_tts(*args, **kwargs):
        return "/media/mock/restart-recovered.wav"

    monkeypatch.setattr(debate_agent, "generate", forbidden_agent)
    monkeypatch.setattr(lighttts, "synthesize", recovered_tts)
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        seat = next(item for item in room.seats if item.seat_key == "neg_1")
        await match_engine._ai_speech(db, room, seat, room.template_snapshot[0])
    with SessionLocal() as db:
        room = load_room(db, code)
        attempts = list(
            db.scalars(
                select(Speech)
                .where(Speech.room_id == room.id, Speech.stage_key == room.template_snapshot[0]["key"])
                .order_by(Speech.created_at, Speech.id)
            ).all()
        )
        assert [item.status for item in attempts] == ["interrupted", "completed"]
        assert attempts[1].content == attempts[0].content
        assert db.scalar(
            select(MatchEvent.id).where(
                MatchEvent.room_id == room.id,
                MatchEvent.event_type == "speech.content.reused",
                MatchEvent.payload["source_speech_id"].as_string() == attempts[0].id,
            )
        )


async def test_missing_cue_asset_is_invalidated_and_regenerated(client: TestClient, register_user, monkeypatch) -> None:
    owner = register_user("cue_asset_repair")
    room_data = create_training_room(owner, "提示音文件缺失后的自动修复测试")
    code = room_data["code"]
    owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})
    owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.template_snapshot = [
            {"key": "opening", "name": "开场", "kind": "announcement", "duration": 10, "cue": "缺失文件需要重建。"},
            {"key": "human", "name": "真人发言", "kind": "speech", "seat": "aff_1", "duration": 30},
        ]
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        db.add(
            AudioAsset(
                id=str(uuid.uuid4()),
                match_id=match.id,
                kind="cue:opening",
                storage_key=f"/media/{code}/missing.wav",
                mime_type="audio/wav",
                size_bytes=123,
            )
        )
        db.commit()

    async def regenerated(*args, speech_id: str, **kwargs):
        target = settings.media_path / code / f"{speech_id}.wav"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_wav_bytes())
        return f"/media/{code}/{speech_id}.wav"

    monkeypatch.setattr(lighttts, "synthesize", regenerated)
    await match_engine.process_room(code)
    with SessionLocal() as db:
        room = load_room(db, code)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        assets = list(db.scalars(select(AudioAsset).where(AudioAsset.match_id == match.id, AudioAsset.kind == "cue:opening")).all())
        event_types = {
            item.event_type
            for item in db.scalars(
                select(MatchEvent).where(
                    MatchEvent.room_id == room.id,
                    MatchEvent.event_type.in_(["audio.cue.invalidated", "audio.cue.ready"]),
                )
            ).all()
        }
        assert room.status == "running" and room.current_stage_index == 0
        assert len(assets) == 1 and assets[0].size_bytes > 0
        assert (settings.media_path / code / f"{assets[0].id}.wav").is_file()
        assert event_types == {"audio.cue.invalidated", "audio.cue.ready"}


async def test_cue_generation_failure_pauses_and_retry_returns_to_preparing(client: TestClient, register_user, monkeypatch) -> None:
    owner = register_user("cue_failure_retry")
    room_data = create_training_room(owner, "提示音失败后重试测试")
    code = room_data["code"]
    owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})
    owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.template_snapshot = [
            {"key": "opening", "name": "开场", "kind": "announcement", "duration": 10, "cue": "请双方辩手准备。"},
            {"key": "human_case", "name": "真人发言", "kind": "speech", "seat": "aff_1", "duration": 30},
        ]
        db.commit()

    async def failed_tts(*args, **kwargs):
        raise ProviderError("LightTTS 暂时不可用")

    monkeypatch.setattr(lighttts, "synthesize", failed_tts)
    await match_engine.process_room(code)
    with SessionLocal() as db:
        room = load_room(db, code)
        assert room.status == "paused"
        assert room.current_stage_index == -1
        assert room.failure_reason == "LightTTS 暂时不可用"
        assert db.scalar(
            select(MatchEvent.id).where(
                MatchEvent.room_id == room.id,
                MatchEvent.event_type == "provider.failed",
            )
        )

    rejected_resume = owner.post(f"/api/rooms/{code}/control/resume", headers=csrf(owner), json={"reason": "错误恢复"})
    assert rejected_resume.status_code == 409 and "重试当前步骤" in rejected_resume.json()["detail"]

    retried = owner.post(f"/api/rooms/{code}/control/retry", headers=csrf(owner), json={"reason": "服务已恢复"})
    assert retried.status_code == 200
    assert retried.json()["room"]["status"] == "preparing"
    assert retried.json()["room"]["failure_reason"] == ""

    async def recovered_tts(*args, **kwargs):
        return f"/media/{code}/opening.wav"

    monkeypatch.setattr(lighttts, "synthesize", recovered_tts)
    await match_engine.process_room(code)
    with SessionLocal() as db:
        room = load_room(db, code)
        assert room.status == "running"
        assert room.current_stage_index == 0


async def test_ai_tts_failure_pauses_current_stage_and_retry_restarts_it(client: TestClient, register_user, monkeypatch) -> None:
    owner = register_user("speech_tts_failure_retry")
    room_data = create_training_room(owner, "AI 语音失败后重试测试")
    code = room_data["code"]
    owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})
    owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.template_snapshot = [
            {"key": "ai_case", "name": "AI 发言", "kind": "speech", "seat": "neg_1", "duration": 30},
            {"key": "human_case", "name": "真人发言", "kind": "speech", "seat": "aff_1", "duration": 30},
        ]
        room.status = "running"
        room.current_stage_index = 0
        room.stage_started_at = now()
        room.stage_deadline_at = now() + timedelta(seconds=30)
        seat = next(item for item in room.seats if item.seat_key == "neg_1")
        seat.occupant_type = "ai"
        seat.user_id = None
        db.commit()

    agent_calls = 0

    async def generated(*args, **kwargs):
        nonlocal agent_calls
        agent_calls += 1
        return "这段文字生成成功，但首次语音合成失败。"

    async def failed_tts(*args, **kwargs):
        raise ProviderError("LightTTS 合成失败")

    monkeypatch.setattr(debate_agent, "generate", generated)
    monkeypatch.setattr(lighttts, "synthesize", failed_tts)
    await match_engine.process_room(code)
    with SessionLocal() as db:
        room = load_room(db, code)
        failed = db.scalar(select(Speech).where(Speech.room_id == room.id, Speech.stage_key == "ai_case"))
        assert room.status == "paused" and room.current_stage_index == 0
        assert room.failure_reason == "LightTTS 合成失败"
        assert failed and failed.status == "failed"

    rejected_resume = owner.post(f"/api/rooms/{code}/control/resume", headers=csrf(owner), json={"reason": "错误恢复"})
    assert rejected_resume.status_code == 409 and "重试当前步骤" in rejected_resume.json()["detail"]

    retried = owner.post(f"/api/rooms/{code}/control/retry", headers=csrf(owner), json={"reason": "重新合成"})
    assert retried.status_code == 200
    assert retried.json()["room"]["status"] == "running"
    with SessionLocal() as db:
        room = load_room(db, code)
        failed_attempt = db.scalar(select(Speech).where(Speech.room_id == room.id, Speech.stage_key == "ai_case"))
        assert failed_attempt and failed_attempt.status == "failed_retried"

    reused_chunks: list[str] = []

    class ReusedRealtimeSession:
        async def push_text(self, text: str) -> None:
            reused_chunks.append(text)

        async def finish(self) -> str:
            return "/media/mock/recovered.wav"

        async def abort(self, reason: str = "") -> None:
            return None

    monkeypatch.setattr(settings, "realtime_voice_pipeline_enabled", True)
    monkeypatch.setattr(settings, "realtime_voice_backend", "lighttts")
    monkeypatch.setattr(settings, "lighttts_streaming_enabled", True)
    monkeypatch.setattr(settings, "lighttts_bistream_enabled", True)
    monkeypatch.setattr(lighttts, "open_incremental_session", lambda **_kwargs: ReusedRealtimeSession())
    await match_engine.process_room(code)
    with SessionLocal() as db:
        room = load_room(db, code)
        attempts = list(
            db.scalars(
                select(Speech).where(Speech.room_id == room.id, Speech.stage_key == "ai_case").order_by(Speech.created_at, Speech.id)
            ).all()
        )
        assert room.current_stage_index == 1
        assert [item.status for item in attempts] == ["failed_retried", "completed"]
        assert attempts[1].content == attempts[0].content
        reused = db.scalar(
            select(MatchEvent).where(
                MatchEvent.room_id == room.id,
                MatchEvent.event_type == "speech.content.reused",
                MatchEvent.payload["source_speech_id"].as_string() == attempts[0].id,
            )
        )
        assert reused
    assert "".join(reused_chunks) == "这段文字生成成功，但首次语音合成失败。"
    assert agent_calls == 1, "manual TTS retry must reuse generated content instead of calling Agent again"
    assert owner.post(f"/api/rooms/{code}/control/pause", headers=csrf(owner), json={"reason": "人工暂停"}).status_code == 200
    assert owner.post(f"/api/rooms/{code}/control/resume", headers=csrf(owner), json={"reason": "继续比赛"}).status_code == 200


async def test_pause_resume_invalidates_inflight_judge_result(client: TestClient, register_user, monkeypatch) -> None:
    owner = register_user("pause_judge_race")
    room_data = create_training_room(owner, "暂停恢复不得接受旧裁判结果")
    code = room_data["code"]
    owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})
    owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        room.status = "judging"
        room.current_stage_index = len(room.template_snapshot) - 1
        room.stage_started_at = now()
        room.stage_deadline_at = None
        match.status = "running"
        db.add(JudgeScorecard(match_id=match.id, status="running"))
        db.commit()

    started = asyncio.Event()
    release = asyncio.Event()

    async def delayed_judge(*args, **kwargs):
        started.set()
        await release.wait()
        return {
            "winner": "aff",
            "affirmative_score": 99,
            "negative_score": 1,
            "individual_scores": {},
            "reasoning": "暂停前的迟到裁判结果",
        }

    monkeypatch.setattr(judge_provider, "judge", delayed_judge)
    judging = asyncio.create_task(match_engine.process_room(code))
    await asyncio.wait_for(started.wait(), timeout=2)
    paused = owner.post(f"/api/rooms/{code}/control/pause", headers=csrf(owner), json={"reason": "暂停裁判"})
    assert paused.status_code == 200, paused.text
    resumed = owner.post(f"/api/rooms/{code}/control/resume", headers=csrf(owner), json={"reason": "重新评议"})
    assert resumed.status_code == 200, resumed.text
    release.set()
    await asyncio.wait_for(judging, timeout=2)

    with SessionLocal() as db:
        room = load_room(db, code)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        scorecard = db.scalar(select(JudgeScorecard).where(JudgeScorecard.match_id == match.id))
        assert room.status == "running" and match.status == "running" and match.winner is None
        assert scorecard.status == "interrupted" and scorecard.winner is None
        assert not db.scalar(select(MatchEvent.id).where(MatchEvent.room_id == room.id, MatchEvent.event_type == "match.completed"))

    async def recovered_judge(*args, **kwargs):
        return {
            "winner": "draw",
            "affirmative_score": 86,
            "negative_score": 86,
            "individual_scores": {},
            "reasoning": "恢复后重新生成的有效裁判结果",
        }

    monkeypatch.setattr(judge_provider, "judge", recovered_judge)
    await match_engine.process_room(code)
    with SessionLocal() as db:
        room = load_room(db, code)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        scorecard = db.scalar(select(JudgeScorecard).where(JudgeScorecard.match_id == match.id))
        assert room.status == match.status == "completed" and match.winner == "draw"
        assert scorecard.status == "approved" and scorecard.winner == "draw"
        assert scorecard.reasoning == "恢复后重新生成的有效裁判结果"


async def test_skipping_judge_enters_review_and_late_result_cannot_overwrite(client: TestClient, register_user, monkeypatch) -> None:
    owner = register_user("judge_skip")
    room_data = create_training_room(owner, "跳过裁判进入人工复核测试")
    code = room_data["code"]
    owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})
    owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        room.status = "judging"
        room.current_stage_index = len(room.template_snapshot) - 1
        match.status = "running"
        scorecard = JudgeScorecard(match_id=match.id, status="running")
        db.add(scorecard)
        db.commit()

    judge_started = asyncio.Event()
    judge_release = asyncio.Event()

    async def delayed_judge(*args, **kwargs):
        judge_started.set()
        await judge_release.wait()
        return {
            "winner": "aff",
            "affirmative_score": 90,
            "negative_score": 80,
            "individual_scores": {},
            "reasoning": "迟到的自动裁判结果",
        }

    monkeypatch.setattr(judge_provider, "judge", delayed_judge)

    async def run_judge() -> None:
        with SessionLocal() as db:
            room = load_room(db, code, lock=True)
            await match_engine._judge(db, room)

    judging = asyncio.create_task(run_judge())
    await asyncio.wait_for(judge_started.wait(), timeout=2)
    skipped = owner.post(f"/api/rooms/{code}/control/skip", headers=csrf(owner), json={"reason": "转人工复核"})
    assert skipped.status_code == 200, skipped.text
    judge_release.set()
    await asyncio.wait_for(judging, timeout=2)

    with SessionLocal() as db:
        room = load_room(db, code)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        scorecard = db.scalar(select(JudgeScorecard).where(JudgeScorecard.match_id == match.id))
        assert room.status == match.status == scorecard.status == "review_required"
        assert scorecard.reasoning == "转人工复核"


async def test_exhausted_template_creates_reviewable_result(client: TestClient, register_user) -> None:
    owner = register_user("template_exhausted")
    room_data = create_training_room(owner, "无裁判模板结束测试")
    code = room_data["code"]
    owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})
    owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        room.template_snapshot = [{"key": "only", "name": "唯一阶段", "kind": "announcement", "duration": 1}]
        room.current_stage_index = 1
        room.status = "judging"
        room.stage_deadline_at = None
        match.status = "running"
        db.commit()
    await match_engine.process_room(code)
    with SessionLocal() as db:
        room = load_room(db, code)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        scorecard = db.scalar(select(JudgeScorecard).where(JudgeScorecard.match_id == match.id))
        assert room.status == match.status == scorecard.status == "review_required"


def _wav_bytes(seconds: float = 0.3, *, amplitude: int = 1200) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        bounded = max(-32768, min(32767, amplitude))
        sample_pair = bounded.to_bytes(2, "little", signed=True) + (-bounded).to_bytes(2, "little", signed=True)
        frame_count = int(16000 * seconds)
        audio.writeframes((sample_pair * ((frame_count + 1) // 2))[: frame_count * 2])
    return buffer.getvalue()


def test_human_finish_and_audio_upload_are_idempotent(client: TestClient, register_user) -> None:
    owner = register_user("idempotent_speech")
    code, speech_id = prepare_human_speech(owner)
    headers = csrf(owner) | {"X-Control-Lease": "test-lease", "X-Idempotency-Key": "finish-once"}
    missing = owner.post(f"/api/rooms/{code}/speech/finish", headers=headers, json={"content": "缺少发言标识"})
    mismatched = owner.post(
        f"/api/rooms/{code}/speech/finish",
        headers=headers | {"X-Idempotency-Key": "finish-wrong-id"},
        json={"speech_id": str(uuid.uuid4()), "content": "错误发言标识"},
    )
    assert missing.status_code == 422
    assert mismatched.status_code == 409 and "发言标识" in mismatched.json()["detail"]
    first = owner.post(f"/api/rooms/{code}/speech/finish", headers=headers, json={"speech_id": speech_id, "content": "完整真人发言"})
    second = owner.post(f"/api/rooms/{code}/speech/finish", headers=headers, json={"speech_id": speech_id, "content": "完整真人发言"})
    conflicting = owner.post(f"/api/rooms/{code}/speech/finish", headers=headers, json={"speech_id": speech_id, "content": "不会重复覆盖"})
    assert first.status_code == 200
    assert second.status_code == 200 and second.json()["replayed"] is True
    assert conflicting.status_code == 409 and "幂等键" in conflicting.json()["detail"]

    invalid = owner.post(
        f"/api/rooms/{code}/speech/{speech_id}/audio",
        headers=csrf(owner),
        files={"audio": ("fake.wav", b"not-an-audio-file", "audio/wav")},
    )
    assert invalid.status_code == 415
    uploaded = owner.post(
        f"/api/rooms/{code}/speech/{speech_id}/audio",
        headers=csrf(owner),
        files={"audio": ("recording.bin", _wav_bytes(), "application/octet-stream")},
    )
    replayed = owner.post(
        f"/api/rooms/{code}/speech/{speech_id}/audio",
        headers=csrf(owner),
        files={"audio": ("different.mp3", b"ID3replacement", "audio/mpeg")},
    )
    assert uploaded.status_code == 200 and uploaded.json()["audio_url"].endswith(".wav")
    assert replayed.status_code == 200 and replayed.json()["replayed"] is True
    assert replayed.json()["audio_url"] == uploaded.json()["audio_url"]
    result_speech = next(item for item in owner.get(f"/api/rooms/{code}/result").json()["speeches"] if item["id"] == speech_id)
    assert result_speech["stage_name"] and result_speech["stage_name"] != result_speech["stage_key"]

    with SessionLocal() as db:
        room = load_room(db, code)
        speech = db.get(Speech, speech_id)
        completed_events = [
            event
            for event in db.scalars(
                select(MatchEvent).where(MatchEvent.room_id == room.id, MatchEvent.event_type == "speech.completed")
            ).all()
            if event.payload.get("speech_id") == speech_id
        ]
        audio_events = list(
            db.scalars(select(MatchEvent).where(MatchEvent.room_id == room.id, MatchEvent.event_type == "speech.audio.ready")).all()
        )
        assert speech.content == "完整真人发言"
        assert len(completed_events) == 1
        assert len(audio_events) == 1


@pytest.mark.parametrize("hallucination", ["七", "去。"])
def test_human_finish_rejects_single_character_asr_fallback(
    client: TestClient,
    register_user,
    hallucination: str,
) -> None:
    owner = register_user(f"finish_short_asr_{ord(hallucination[0])}")
    code, speech_id = prepare_human_speech(owner)

    rejected = owner.post(
        f"/api/rooms/{code}/speech/finish",
        headers=csrf(owner) | {"X-Control-Lease": "test-lease"},
        json={"speech_id": speech_id, "content": hallucination},
    )

    assert rejected.status_code == 422
    assert "过短" in rejected.json()["detail"]
    with SessionLocal() as db:
        speech = db.get(Speech, speech_id)
        assert speech and speech.status == "speaking" and speech.content == ""


def test_manual_transcript_replaces_asr_segments_and_keeps_audio_on_same_speech(client: TestClient, register_user) -> None:
    owner = register_user("manual_transcript_sync")
    code, speech_id = prepare_human_speech(owner)
    assert persist_asr_final(speech_id, "需要人工纠正的识别文字。") is True

    finalized = owner.post(
        f"/api/rooms/{code}/speech/finish",
        headers=csrf(owner) | {"X-Control-Lease": "test-lease"},
        json={"speech_id": speech_id, "content": "人工核对后的最终发言。"},
    )
    assert finalized.status_code == 200 and finalized.json()["speech_id"] == speech_id
    uploaded = owner.post(
        f"/api/rooms/{code}/speech/{finalized.json()['speech_id']}/audio",
        headers=csrf(owner),
        files={"audio": ("speech.wav", _wav_bytes(), "audio/wav")},
    )
    assert uploaded.status_code == 200

    with SessionLocal() as db:
        speech = db.get(Speech, speech_id)
        segments = list(db.scalars(select(TranscriptSegment).where(TranscriptSegment.speech_id == speech_id)).all())
        assert speech and speech.content == "人工核对后的最终发言。"
        assert speech.audio_url == uploaded.json()["audio_url"]
        assert speech.duration_seconds == pytest.approx(0.3, abs=0.01)
        assert [item.text for item in segments] == ["人工核对后的最终发言。"]


def test_empty_and_silent_audio_are_not_archived(client: TestClient, register_user) -> None:
    owner = register_user("empty_audio_rejected")
    code, speech_id = prepare_human_speech(owner)
    finished = owner.post(
        f"/api/rooms/{code}/speech/finish",
        headers=csrf(owner) | {"X-Control-Lease": "test-lease"},
        json={"speech_id": speech_id, "content": "仅保存有效语音文件。"},
    )
    assert finished.status_code == 200

    empty = owner.post(
        f"/api/rooms/{code}/speech/{speech_id}/audio",
        headers=csrf(owner),
        files={"audio": ("empty.wav", b"", "audio/wav")},
    )
    silent = owner.post(
        f"/api/rooms/{code}/speech/{speech_id}/audio",
        headers=csrf(owner),
        files={"audio": ("silent.wav", _wav_bytes(amplitude=0), "audio/wav")},
    )
    assert empty.status_code == 422 and "音频为空" in empty.json()["detail"]
    assert silent.status_code == 422 and "未检测到清晰语音" in silent.json()["detail"]

    with SessionLocal() as db:
        speech = db.get(Speech, speech_id)
        assert speech and speech.audio_url == "" and speech.duration_seconds == 0
    target_dir = settings.media_path / code
    assert not list(target_dir.glob(f"{speech_id}.*"))


async def test_audio_upload_releases_database_transaction_while_streaming(client: TestClient, register_user) -> None:
    owner = register_user("streaming_audio_pool")
    code, speech_id = prepare_human_speech(owner)
    finished = owner.post(
        f"/api/rooms/{code}/speech/finish",
        headers=csrf(owner) | {"X-Control-Lease": "test-lease"},
        json={"speech_id": speech_id, "content": "慢速录音上传连接池测试"},
    )
    assert finished.status_code == 200
    payload = _wav_bytes()

    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.account.like("streaming_audio_pool%")))
        assert user is not None

        class SlowAudio:
            offset = 0
            checks = 0

            async def read(self, size: int) -> bytes:
                self.checks += 1
                assert not db.in_transaction(), "录音网络读取期间不得占用数据库事务"
                await asyncio.sleep(0)
                chunk = payload[self.offset : self.offset + size]
                self.offset += len(chunk)
                return chunk

        audio = SlowAudio()
        result = await upload_speech_audio(code, speech_id, audio, user, db)  # type: ignore[arg-type]
        assert result["audio_url"].endswith(".wav")
        assert audio.checks >= 2


async def test_audio_validation_runs_off_the_event_loop(monkeypatch, tmp_path: Path) -> None:
    def slow_validation(_path: Path, _suffix: str, _size: int) -> float:
        time.sleep(0.08)
        return 1.25

    monkeypatch.setattr(rooms_api, "_validate_uploaded_audio", slow_validation)
    validation = asyncio.create_task(rooms_api._validate_uploaded_audio_off_loop(tmp_path / "audio.wav", ".wav", 1024))
    await asyncio.sleep(0.01)
    assert not validation.done(), "audio validation must not block the API event loop"
    assert await validation == 1.25


def test_large_wav_vad_uses_a_bounded_sample_budget(monkeypatch, tmp_path: Path) -> None:
    observed = 0

    class CountingActivity:
        @property
        def has_voice(self) -> bool:
            return False

        def observe(self, _frames: bytes) -> None:
            nonlocal observed
            observed += 1

    monkeypatch.setattr(rooms_api, "PcmVoiceActivity", CountingActivity)
    path = tmp_path / "long-silence.wav"
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x00\x00" * (16000 * 40))

    with pytest.raises(HTTPException) as rejected:
        rooms_api._validate_uploaded_audio(path, ".wav", path.stat().st_size)
    assert rejected.value.status_code == 422
    assert observed == rooms_api.MAX_AUDIO_VAD_WINDOWS


def test_webm_validation_uses_decoded_pcm_for_duration_and_vad(monkeypatch, tmp_path: Path) -> None:
    frame_count = int(rooms_api.DECODED_AUDIO_SAMPLE_RATE * 34.61)
    sample_pair = (1200).to_bytes(2, "little", signed=True) + (-1200).to_bytes(2, "little", signed=True)
    pcm = (sample_pair * ((frame_count + 1) // 2))[: frame_count * 2]
    invocations: list[list[str]] = []

    def decoded_audio(command: list[str], **_kwargs):
        invocations.append(command)
        return Mock(returncode=0, stdout=pcm, stderr=b"")

    monkeypatch.setattr(rooms_api.subprocess, "run", decoded_audio)
    path = tmp_path / "missing-container-duration.webm"
    path.write_bytes(b"\x1aE\xdf\xa3" + b"0" * 256)

    duration = rooms_api._validate_uploaded_audio(path, ".webm", path.stat().st_size)

    assert duration == pytest.approx(34.61, abs=0.001)
    assert invocations and "ffmpeg" == invocations[0][0]
    assert "ffprobe" not in invocations[0]
    assert invocations[0][invocations[0].index("-f") + 1] == "s16le"


def test_webm_validation_rejects_decoded_silence(monkeypatch, tmp_path: Path) -> None:
    pcm = b"\x00\x00" * (rooms_api.DECODED_AUDIO_SAMPLE_RATE * 2)
    monkeypatch.setattr(
        rooms_api.subprocess,
        "run",
        lambda *_args, **_kwargs: Mock(returncode=0, stdout=pcm, stderr=b""),
    )
    path = tmp_path / "silent.webm"
    path.write_bytes(b"\x1aE\xdf\xa3" + b"0" * 256)

    with pytest.raises(HTTPException) as rejected:
        rooms_api._validate_uploaded_audio(path, ".webm", path.stat().st_size)

    assert rejected.value.status_code == 422
    assert "未检测到清晰语音" in rejected.value.detail


def test_concurrent_audio_upload_lease_rejects_duplicate_before_multipart_parsing(client: TestClient, register_user, monkeypatch) -> None:
    owner = register_user("audio_upload_lease")
    code, speech_id = prepare_human_speech(owner)
    finished = owner.post(
        f"/api/rooms/{code}/speech/finish",
        headers=csrf(owner) | {"X-Control-Lease": "test-lease"},
        json={"speech_id": speech_id, "content": "同一发言只能保留一个上传流。"},
    )
    assert finished.status_code == 200
    payload = _wav_bytes()
    validation_started = Event()
    release_validation = Event()
    parsed_requests: list[int] = []
    original_parse = MultiPartParser.parse
    original_validation = rooms_api._validate_uploaded_audio_off_loop

    async def track_parse(parser: MultiPartParser):
        parsed_requests.append(id(parser))
        return await original_parse(parser)

    async def block_first_validation(path: Path, suffix: str, size: int) -> float:
        validation_started.set()
        released = await asyncio.to_thread(release_validation.wait, 2)
        assert released, "first upload validation did not receive its release signal"
        return await original_validation(path, suffix, size)

    monkeypatch.setattr(MultiPartParser, "parse", track_parse)
    monkeypatch.setattr(rooms_api, "_validate_uploaded_audio_off_loop", block_first_validation)

    def upload():
        return owner.post(
            f"/api/rooms/{code}/speech/{speech_id}/audio",
            headers=csrf(owner),
            files={"audio": ("speech.wav", payload, "audio/wav")},
        )

    with ThreadPoolExecutor(max_workers=1) as pool:
        first_upload = pool.submit(upload)
        assert validation_started.wait(2), "first upload never reached validation"
        duplicate = upload()
        assert duplicate.status_code == 409
        assert "正在上传" in duplicate.json()["detail"]
        assert len(parsed_requests) == 1, "duplicate multipart body must be rejected before parsing"
        release_validation.set()
        result = first_upload.result(timeout=2)

    assert result.status_code == 200 and result.json()["audio_url"].endswith(".wav")
    assert not list((settings.media_path / code).glob(f".{speech_id}.*.part"))


def test_audio_upload_middleware_lease_is_released_after_validation_failure(client: TestClient, register_user, monkeypatch) -> None:
    owner = register_user("audio_upload_lease_failure")
    code, speech_id = prepare_human_speech(owner)
    assert (
        owner.post(
            f"/api/rooms/{code}/speech/finish",
            headers=csrf(owner) | {"X-Control-Lease": "test-lease"},
            json={"speech_id": speech_id, "content": "失败后必须允许安全重试。"},
        ).status_code
        == 200
    )
    original_validation = rooms_api._validate_uploaded_audio_off_loop
    attempts = 0

    async def fail_once(path: Path, suffix: str, size: int) -> float:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise HTTPException(status_code=422, detail="simulated validation failure")
        return await original_validation(path, suffix, size)

    monkeypatch.setattr(rooms_api, "_validate_uploaded_audio_off_loop", fail_once)
    request = {
        "headers": csrf(owner),
        "files": {"audio": ("speech.wav", _wav_bytes(), "audio/wav")},
    }
    failed = owner.post(f"/api/rooms/{code}/speech/{speech_id}/audio", **request)
    retried = owner.post(f"/api/rooms/{code}/speech/{speech_id}/audio", **request)
    assert failed.status_code == 422
    assert retried.status_code == 200 and retried.json()["audio_url"].endswith(".wav")


async def test_parallel_readiness_probes_do_not_hold_database_connections(client: TestClient, monkeypatch) -> None:
    active = 0
    peak = 0

    async def delayed_redis_checks(*, production: bool):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return {
            "redis": {"ok": True, "skipped": not production},
            "engine": {"ok": True, "skipped": not production},
        }

    monkeypatch.setattr(system_health_service, "_redis_checks", delayed_redis_checks)

    async def probe() -> dict:
        with SessionLocal() as db:
            return await system_health_service.system_readiness(db)

    results = await asyncio.wait_for(asyncio.gather(*(probe() for _ in range(20))), timeout=3)
    assert all(item["ok"] for item in results)
    assert peak == 20


def test_interrupted_speech_rejects_audio_upload(client: TestClient, register_user) -> None:
    owner = register_user("interrupted_audio")
    code, speech_id = prepare_human_speech(owner)
    owner.post(f"/api/rooms/{code}/control/skip", headers=csrf(owner), json={"reason": "test"})
    response = owner.post(
        f"/api/rooms/{code}/speech/{speech_id}/audio",
        headers=csrf(owner),
        files={"audio": ("speech.wav", _wav_bytes(), "audio/wav")},
    )
    assert response.status_code == 409


def test_terminated_match_moves_from_active_to_history(client: TestClient, register_user) -> None:
    owner = register_user("terminated_history")
    room = create_training_room(owner, "终止比赛历史记录测试")
    code = room["code"]
    owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})
    owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    terminated = owner.post(f"/api/rooms/{code}/control/terminate", headers=csrf(owner), json={"reason": "test"})
    assert terminated.status_code == 200
    me = owner.get("/api/me").json()
    assert code not in {item["code"] for item in me["active_rooms"]}
    assert any(item["room_code"] == code and item["status"] == "terminated" for item in me["history"])


def test_terminal_participant_can_create_idempotent_same_topic_rematch(client: TestClient, register_user) -> None:
    owner = register_user("rematch_owner")
    outsider = register_user("rematch_outsider")
    topic = "同一辩题是否值得立即复盘再赛？"
    source = create_training_room(owner, topic)
    source_code = source["code"]

    assert owner.post(f"/api/rooms/{source_code}/rematch", headers=csrf(owner), json={}).status_code == 409
    owner.post(f"/api/rooms/{source_code}/ready", headers=csrf(owner), json={"ready": True})
    owner.post(f"/api/rooms/{source_code}/start", headers=csrf(owner), json={})
    assert owner.post(
        f"/api/rooms/{source_code}/control/terminate",
        headers=csrf(owner),
        json={"reason": "rematch test"},
    ).status_code == 200

    assert outsider.post(f"/api/rooms/{source_code}/rematch", headers=csrf(outsider), json={}).status_code == 403
    headers = {**csrf(owner), "X-Idempotency-Key": "same-topic-rematch"}
    created = owner.post(f"/api/rooms/{source_code}/rematch", headers=headers, json={})
    replayed = owner.post(f"/api/rooms/{source_code}/rematch", headers=headers, json={})
    assert created.status_code == 200, created.text
    assert replayed.status_code == 200 and replayed.json()["replayed"] is True
    room = created.json()["room"]
    assert replayed.json()["room"]["code"] == room["code"]
    assert room["code"] != source_code
    assert room["status"] == "lobby" and room["topic"] == topic
    assert room["competition"]["slug"] == source["competition"]["slug"]
    assert room["visibility"] == source["visibility"]
    assert room["owner"]["real_name"] == owner.get("/api/auth/session").json()["user"]["real_name"]
    assert room["my_seat"] == source["my_seat"]
    claimed = next(item for item in room["seats"] if item["seat_key"] == room["my_seat"])
    assert claimed["occupant_type"] == "human" and claimed["is_ready"] is False
    assert all(item["occupant_type"] == "open" for item in room["seats"] if item["seat_key"] != room["my_seat"])

    with SessionLocal() as db:
        rematch = load_room(db, room["code"])
        created_event = db.scalar(
            select(MatchEvent).where(MatchEvent.room_id == rematch.id, MatchEvent.event_type == "room.created")
        )
        assert created_event and created_event.payload["rematch_of"] == source_code

    duplicate = owner.post(
        f"/api/rooms/{source_code}/rematch",
        headers={**csrf(owner), "X-Idempotency-Key": "another-rematch"},
        json={},
    )
    assert duplicate.status_code == 409
    assert f"#{room['code']}" in duplicate.json()["detail"]


def test_terminal_room_websocket_does_not_mutate_final_timeline(client: TestClient, register_user) -> None:
    owner = register_user("terminal_websocket")
    room_data = create_training_room(owner, "终局页面连接不应污染时间线")
    code = room_data["code"]
    owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})
    owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    assert owner.post(f"/api/rooms/{code}/control/terminate", headers=csrf(owner), json={"reason": "test"}).status_code == 200
    with SessionLocal() as db:
        room = load_room(db, code)
        before_seq = room.seq
        before_events = db.scalar(select(func.count(MatchEvent.id)).where(MatchEvent.room_id == room.id))
    with owner.websocket_connect(f"/ws/rooms/{code}") as socket:
        snapshot = socket.receive_json()["room"]
        assert snapshot["status"] == "terminated"
        socket.send_json({"type": "ping"})
        assert socket.receive_json()["type"] == "pong"
    with SessionLocal() as db:
        room = load_room(db, code)
        after_events = db.scalar(select(func.count(MatchEvent.id)).where(MatchEvent.room_id == room.id))
        assert room.seq == before_seq
        assert after_events == before_events


def test_pending_scorecard_details_are_admin_only(client: TestClient, register_user) -> None:
    owner = register_user("pending_scorecard")
    room_data = create_training_room(owner, "待复核裁判信息脱敏测试")
    code = room_data["code"]
    owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})
    owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    raw_reason = "judge provider http://internal.example failed with secret config"
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        room.status = "review_required"
        match.status = "review_required"
        db.add(
            JudgeScorecard(
                match_id=match.id,
                status="review_required",
                winner="aff",
                affirmative_score=91,
                negative_score=80,
                individual_scores={"aff_1": 91, "neg_1": 80},
                reasoning=raw_reason,
            )
        )
        db.commit()
        match_id = match.id

    for response in (
        owner.get(f"/api/rooms/{code}/result"),
        owner.get(f"/api/matches/{match_id}/history"),
    ):
        assert response.status_code == 200, response.text
        scorecard = response.json()["scorecard"]
        assert scorecard["winner"] is None
        assert scorecard["affirmative_score"] is None and scorecard["negative_score"] is None
        assert scorecard["individual_scores"] == {}
        assert scorecard["reasoning"] == "裁判结果等待管理员复核。"
        assert raw_reason not in response.text

    for response in (
        client.get(f"/api/rooms/{code}/result"),
        client.get(f"/api/matches/{match_id}/result"),
    ):
        assert response.status_code == 409
        assert response.json()["detail"] == "比赛尚未结束，请前往观战页面查看实时内容。"

    admin = TestClient(client.app)
    with admin:
        assert admin.post("/api/auth/login", json={"account": "admin_test", "password": "Admin-test-1234"}).status_code == 200
        admin_result = admin.get(f"/api/rooms/{code}/result")
        assert admin_result.status_code == 200
        assert admin_result.json()["scorecard"]["reasoning"] == raw_reason
        assert admin_result.json()["scorecard"]["affirmative_score"] == 91
        assert admin_result.json()["scorecard"]["individual_scores"] == {"aff_1": 91.0, "neg_1": 80.0}


def test_approved_scorecard_exposes_only_valid_scores_for_room_seats(client: TestClient, register_user) -> None:
    owner = register_user("approved_individual_scores")
    room_data = create_training_room(owner, "个人评分白名单与数值范围测试")
    code = room_data["code"]
    owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})
    owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        room.status = "completed"
        match.status = "completed"
        match.winner = "aff"
        db.add(
            JudgeScorecard(
                match_id=match.id,
                status="approved",
                winner="aff",
                affirmative_score=88,
                negative_score=77.5,
                individual_scores={
                    "aff_1": 88,
                    "neg_1": 77.5,
                    "aff_2": 99,
                    "__proto__": 50,
                    "invalid_high": 101,
                    "invalid_text": "90",
                    "invalid_bool": True,
                },
                reasoning="正方论证更完整。",
            )
        )
        db.commit()
        match_id = match.id

    expected = {"aff_1": 88.0, "neg_1": 77.5}
    assert client.get(f"/api/rooms/{code}/result").json()["scorecard"]["individual_scores"] == expected
    assert owner.get(f"/api/matches/{match_id}/history").json()["scorecard"]["individual_scores"] == expected
    assert client.get(f"/api/matches/{match_id}/result").json()["scorecard"]["individual_scores"] == expected


def test_personal_history_and_result_events_are_paginated(client: TestClient, register_user) -> None:
    owner = register_user("pagination_owner")
    match_ids: list[str] = []
    codes: list[str] = []
    for index in range(3):
        room_data = create_training_room(owner, f"分页历史测试 {index}")
        code = room_data["code"]
        codes.append(code)
        owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})
        owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
        owner.post(f"/api/rooms/{code}/control/terminate", headers=csrf(owner), json={"reason": "pagination"})
        with SessionLocal() as db:
            room = load_room(db, code)
            match = db.scalar(select(Match).where(Match.room_id == room.id))
            assert match
            match_ids.append(match.id)

    user_id = owner.get("/api/auth/session").json()["user"]["id"]
    with SessionLocal() as db:
        for index, match_id in enumerate(match_ids):
            db.add(RatingChange(match_id=match_id, user_id=user_id, points_delta=index + 1, score=80 + index, reason="test"))
        room = load_room(db, codes[-1], lock=True)
        db.add(
            Speech(
                match_id=match_ids[-1],
                room_id=room.id,
                seat_key="neg_1",
                stage_key="failed_attempt",
                speaker_type="ai",
                status="failed",
                content="这段内容没有成功合成和播放。",
            )
        )
        for index in range(5):
            append_event(db, room, "test.pagination", {"index": index})
        db.commit()

    first = owner.get("/api/me?page=1&page_size=2")
    second = owner.get("/api/me?page=2&page_size=2")
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["summary"] == {"history_total": 3, "active_total": 0, "total_points": 6}
    assert first.json()["pagination"] == {"page": 1, "page_size": 2, "total": 3, "pages": 2}
    assert len(first.json()["history"]) == 2 and len(second.json()["history"]) == 1
    assert {item["match_id"] for item in first.json()["history"]}.isdisjoint({item["match_id"] for item in second.json()["history"]})
    assert owner.get("/api/me?page=0&page_size=2").status_code == 422

    result_first = owner.get(f"/api/rooms/{codes[-1]}/result?event_page=1&event_page_size=2")
    result_second = owner.get(f"/api/rooms/{codes[-1]}/result?event_page=2&event_page_size=2")
    assert result_first.status_code == 200 and result_second.status_code == 200
    assert result_first.json()["event_pagination"]["page_size"] == 2
    assert result_first.json()["event_pagination"]["total"] >= 8
    assert len(result_first.json()["events"]) == 2 and len(result_second.json()["events"]) == 2
    failed_attempt = result_first.json()["speeches"][0]
    assert failed_attempt["status"] == "failed" and failed_attempt["speaker_type"] == "ai"
    assert failed_attempt["created_at"]
    assert max(item["seq"] for item in result_second.json()["events"]) < min(item["seq"] for item in result_first.json()["events"])
    first_payload = result_first.json()
    cursor = first_payload["event_pagination"]["next_before_seq"]
    assert first_payload["event_pagination"]["has_more"] is True and cursor == min(item["seq"] for item in first_payload["events"])
    with SessionLocal() as db:
        room = load_room(db, codes[-1], lock=True)
        appended = append_event(db, room, "test.concurrent_result_update", {})
        db.commit()
        appended_seq = appended.seq
    cursor_page = owner.get(f"/api/rooms/{codes[-1]}/result?event_page=2&event_page_size=2&event_before_seq={cursor}")
    assert cursor_page.status_code == 200
    cursor_events = cursor_page.json()["events"]
    assert {item["seq"] for item in cursor_events}.isdisjoint({item["seq"] for item in first_payload["events"]})
    assert appended_seq not in {item["seq"] for item in cursor_events}
    assert max(item["seq"] for item in cursor_events) < cursor
    assert owner.get(f"/api/rooms/{codes[-1]}/result?event_page_size=201").status_code == 422
    assert owner.get(f"/api/rooms/{codes[-1]}/result?event_before_seq=0").status_code == 422


async def test_automatic_ai_match_reaches_judged_result(client: TestClient, register_user, monkeypatch) -> None:
    owner = register_user("auto_match")
    room_data = create_training_room(owner, "自动状态机闭环测试")
    code = room_data["code"]
    owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})
    owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})

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
            seat.display_name = f"AI-{seat.seat_key}"
        db.commit()

    async def generated(*args, **kwargs):
        return "这是一段完整、可验证的自动辩论发言。"

    async def synthesized(*args, **kwargs):
        return "/media/mock/generated.wav"

    received_judge_profile: dict = {}

    async def judged(*args, **kwargs):
        received_judge_profile.update(kwargs.get("profile") or {})
        return {
            "winner": "aff",
            "affirmative_score": 88.0,
            "negative_score": 84.0,
            "individual_scores": {},
            "reasoning": "正方论证结构更完整。",
        }

    monkeypatch.setattr(debate_agent, "generate", generated)
    monkeypatch.setattr(lighttts, "synthesize", synthesized)
    monkeypatch.setattr(judge_provider, "judge", judged)

    await match_engine.process_room(code)
    await match_engine.process_room(code)
    await match_engine.process_room(code)
    await match_engine.process_room(code)

    with SessionLocal() as db:
        room = load_room(db, code)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        speeches = list(db.scalars(select(Speech).where(Speech.room_id == room.id)).all())
        scorecard = db.scalar(select(JudgeScorecard).where(JudgeScorecard.match_id == match.id))
        assert room.status == "completed"
        assert match.status == "completed" and match.winner == "aff"
        assert len(speeches) == 2 and all(item.status == "completed" for item in speeches)
        assert scorecard and scorecard.status == "approved" and scorecard.affirmative_score == 88.0
        assert received_judge_profile == match.judge_snapshot


async def test_unavailable_judge_enters_review_once_without_publishing_ranking(
    client: TestClient,
    register_user,
    monkeypatch,
) -> None:
    owner = register_user("judge_unavailable_ranked")
    owner_user = owner.get("/api/auth/session").json()["user"]
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
    assert created.status_code == 200, created.text
    code = created.json()["room"]["code"]
    assert owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True}).status_code == 200
    assert owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={}).status_code == 200

    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        room.template_snapshot = [{"key": "judging", "name": "AI 裁判", "kind": "judging", "duration": 60}]
        room.current_stage_index = 0
        room.status = "judging"
        room.stage_started_at = now()
        room.stage_deadline_at = now() + timedelta(seconds=60)
        match.status = "running"
        db.commit()

    async def unavailable(*_args, **_kwargs):
        raise ProviderError("未配置 AI 裁判服务。")

    monkeypatch.setattr(judge_provider, "judge", unavailable)
    await match_engine.process_room(code)
    await match_engine.process_room(code)

    with SessionLocal() as db:
        room = load_room(db, code)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        scorecards = list(db.scalars(select(JudgeScorecard).where(JudgeScorecard.match_id == match.id)).all())
        review_events = list(
            db.scalars(
                select(MatchEvent).where(
                    MatchEvent.room_id == room.id,
                    MatchEvent.event_type == "judge.review_required",
                )
            ).all()
        )
        assert room.status == match.status == "review_required"
        assert room.completed_at is not None
        assert room.stage_deadline_at is None and room.paused_remaining_seconds is None
        assert len(scorecards) == 1 and scorecards[0].status == "review_required"
        assert len(review_events) == 1
        assert not db.scalar(select(RatingChange.id).where(RatingChange.match_id == match.id))
        assert not db.scalar(
            select(LeaderboardEntry.id).where(
                LeaderboardEntry.competition_id == room.competition_id,
                LeaderboardEntry.season_id == match.season_id,
                LeaderboardEntry.user_id == owner_user["id"],
            )
        )

    public_result = client.get(f"/api/rooms/{code}/result")
    assert public_result.status_code == 409
    payload = owner.get(f"/api/rooms/{code}/result").json()
    assert payload["room"]["remaining_seconds"] == 0
    assert payload["scorecard"]["reasoning"] == "裁判结果等待管理员复核。"
    assert payload["rating_changes"] == []
    history = owner.get("/api/me").json()
    assert history["summary"]["total_points"] == 0
    assert any(item["room_code"] == code and item["status"] == "review_required" for item in history["history"])


def test_match_archive_is_complete_idempotent_tamper_resistant_and_access_controlled(client: TestClient, register_user) -> None:
    owner = register_user("archive_owner")
    outsider = register_user("archive_outsider")
    room_data = create_training_room(owner, "比赛归档完整性测试")
    code = room_data["code"]
    owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True})
    started = owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    assert started.status_code == 200

    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        assert match
        speech = Speech(
            match_id=match.id,
            room_id=room.id,
            seat_key="aff_1",
            stage_key="aff_case",
            speaker_type="human",
            content="技术应当服务于人的自由与尊严。",
            status="completed",
            duration_seconds=3.2,
        )
        db.add(speech)
        db.flush()
        db.add(TranscriptSegment(speech_id=speech.id, start_ms=0, end_ms=3200, text=speech.content, is_final=True))
        match.status = "completed"
        match.winner = "aff"
        match.result_reason = "正方论证更完整。"
        room.status = "completed"
        room.completed_at = now()
        append_event(db, room, "match.completed", {"winner": "aff"})
        db.commit()
        match_id = match.id
        speech_id = speech.id

    assert client.get(f"/api/matches/{match_id}/archive").status_code == 401
    assert outsider.get(f"/api/matches/{match_id}/archive").status_code == 403
    first = owner.get(f"/api/matches/{match_id}/archive")
    assert first.status_code == 200
    assert "x-research-export-consent" not in first.headers
    assert first.headers["content-type"].startswith("application/json")
    assert "attachment" in first.headers["content-disposition"]
    assert first.headers["x-archive-sha256"] == hashlib.sha256(first.content).hexdigest()
    document = first.json()
    assert document["schema"] == "jixia-debate-match-archive" and document["version"] == 2
    assert document["data"]["room"]["code"] == code
    assert document["data"]["speeches"][0]["transcript_segments"][0]["text"] == "技术应当服务于人的自由与尊严。"
    serialized = json.dumps(document, ensure_ascii=False)
    assert "control_lease" not in serialized and "password_hash" not in serialized and "token_hash" not in serialized

    second = owner.get(f"/api/matches/{match_id}/archive")
    assert second.status_code == 200 and second.content == first.content
    archive_path = settings.archive_path / f"{match_id}.json"
    archive_path.write_text("tampered", encoding="utf-8")
    repaired = owner.get(f"/api/matches/{match_id}/archive")
    assert repaired.status_code == 200 and repaired.content != b"tampered"
    assert repaired.headers["x-archive-source-sha256"] == first.headers["x-archive-source-sha256"]
    assert repaired.json()["data"] == document["data"]

    with SessionLocal() as db:
        speech = db.get(Speech, speech_id)
        speech.content = "修订后的最终发言文本。"
        db.commit()
    updated = owner.get(f"/api/matches/{match_id}/archive")
    assert updated.status_code == 200
    assert updated.content != first.content
    assert updated.headers["x-archive-source-sha256"] != first.headers["x-archive-source-sha256"]
    assert updated.json()["data"]["speeches"][0]["content"] == "修订后的最终发言文本。"

    from app.services.match_archive import archive_lock_name, build_match_archive, read_match_archive

    task_result = build_match_archive(match_id)
    assert task_result.reused is True and task_result.sha256 == updated.headers["x-archive-sha256"]

    checksum_path = settings.archive_path / f"{match_id}.json.sha256"
    checksum_path.unlink()
    repaired_checksum = build_match_archive(match_id)
    assert repaired_checksum.reused is True
    assert checksum_path.read_text("ascii") == f"{repaired_checksum.sha256}  {match_id}.json\n"

    with SessionLocal() as db:
        speech = db.get(Speech, speech_id)
        speech.content = "并发归档时只能生成一份一致的新版本。"
        db.commit()
    with ThreadPoolExecutor(max_workers=8) as pool:
        concurrent_results = list(pool.map(build_match_archive, [match_id] * 8))
    assert sum(not item.reused for item in concurrent_results) == 1
    assert len({item.sha256 for item in concurrent_results}) == 1
    assert len({item.source_sha256 for item in concurrent_results}) == 1
    concurrent_document = json.loads(archive_path.read_text("utf-8"))
    concurrent_metadata = json.loads((settings.archive_path / f"{match_id}.meta.json").read_text("utf-8"))
    assert concurrent_document["data"]["speeches"][0]["content"] == "并发归档时只能生成一份一致的新版本。"
    assert concurrent_metadata["sha256"] == hashlib.sha256(archive_path.read_bytes()).hexdigest()
    assert checksum_path.read_text("ascii") == f"{concurrent_metadata['sha256']}  {match_id}.json\n"
    with ThreadPoolExecutor(max_workers=8) as pool:
        download_payloads = list(pool.map(read_match_archive, [match_id] * 8))
    assert all(hashlib.sha256(item.content).hexdigest() == item.result.sha256 for item in download_payloads)
    assert len({item.content for item in download_payloads}) == 1

    admin = TestClient(client.app)
    with admin:
        assert admin.post("/api/auth/login", json={"account": "admin_test", "password": "Admin-test-1234"}).status_code == 200
        assert outsider.get("/api/admin/archives").status_code == 403
        checksum_path.write_text("invalid checksum\n", encoding="ascii")
        orphan_paths = [
            settings.archive_path / "removed-match.json",
            settings.archive_path / "removed-match.meta.json",
            settings.archive_path / "removed-match.json.sha256",
            settings.archive_path / ".stale-build.123.part",
        ]
        old_timestamp = (now() - timedelta(hours=48)).timestamp()
        for path in orphan_paths:
            path.write_text("orphan", encoding="utf-8")
            os.utime(path, (old_timestamp, old_timestamp))
        inspected = admin.get("/api/admin/archives")
        assert inspected.status_code == 200
        archive_status = inspected.json()["archives"]
        assert any(item["match_id"] == match_id for item in archive_status["invalid_archives"])
        assert archive_status["orphan_candidate_count"] >= len(orphan_paths)

        repaired_archives = admin.post("/api/admin/archives/repair", headers=csrf(admin), json={})
        assert repaired_archives.status_code == 200
        assert match_id in repaired_archives.json()["repaired"]
        assert not any(item["match_id"] == match_id for item in repaired_archives.json()["archives"]["invalid_archives"])

        cleaned_archives = admin.post(
            "/api/admin/archives/cleanup",
            headers=csrf(admin),
            json={"dry_run": False, "min_age_hours": 24, "include_stale_parts": True},
        )
        assert cleaned_archives.status_code == 200
        assert set(path.name for path in orphan_paths).issubset(set(cleaned_archives.json()["archives"]["deleted_paths"]))
        assert all(not path.exists() for path in orphan_paths)
        with SessionLocal() as db:
            actions = set(
                db.scalars(select(AdminAuditLog.action).where(AdminAuditLog.action.in_(["archives.repair", "archives.cleanup"]))).all()
            )
            assert actions == {"archives.repair", "archives.cleanup"}

    from app.worker_tasks import archive_match

    assert archive_match.fn("removed-match-id") is None
    assert not (settings.archive_path / ".locks" / archive_lock_name("removed-match-id")).exists()
