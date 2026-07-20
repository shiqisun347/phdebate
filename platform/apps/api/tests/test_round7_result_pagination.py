from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.core.database import SessionLocal
from app.models.entities import Match, Speech
from app.services.room_service import load_room
from conftest import csrf
from fastapi.testclient import TestClient
from sqlalchemy import select


def create_training_room(client: TestClient, topic: str, *, visibility: str = "public") -> dict:
    response = client.post(
        "/api/rooms",
        headers=csrf(client),
        json={
            "competition_slug": "training-1v1",
            "custom_topic": topic,
            "seat_key": "aff_1",
            "visibility": visibility,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["room"]


def start_room(owner: TestClient, code: str) -> str:
    assert owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True}).status_code == 200
    response = owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={})
    assert response.status_code == 200, response.text
    with SessionLocal() as db:
        room = load_room(db, code)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        assert match
        return match.id


def add_speeches(code: str, match_id: str, *, count: int, prefix: str, legacy: bool = False) -> list[str]:
    created_at = datetime(2026, 7, 19, 8, 0, tzinfo=timezone.utc)
    ids: list[str] = []
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        match = db.get(Match, match_id)
        assert match
        if legacy:
            match.legacy = True
            match.legacy_source_id = f"round7-{prefix}"
        for index in range(count):
            # Two rows intentionally share a timestamp. The UUID/string ID is
            # the deterministic tie-breaker and prevents duplicate/omitted rows.
            speech_id = f"{prefix}-{index:03d}"
            ids.append(speech_id)
            db.add(
                Speech(
                    id=speech_id,
                    match_id=match_id,
                    room_id=room.id,
                    seat_key="aff_1" if index % 2 == 0 else "neg_1",
                    stage_key="legacy_stage" if legacy else f"stage_{index}",
                    speaker_type="human" if index % 2 == 0 else "ai",
                    status="completed",
                    content=f"{prefix} 发言 {index}",
                    audio_url="" if legacy else f"/media/{code}/{speech_id}.wav",
                    duration_seconds=float(index),
                    created_at=created_at + timedelta(seconds=index // 2),
                )
            )
        db.commit()
    return ids


def test_result_speeches_support_stable_offset_and_signed_cursor_pages(client: TestClient, register_user) -> None:
    owner = register_user("round7_cursor_owner")
    room = create_training_room(owner, "结果逐字稿稳定分页测试")
    match_id = start_room(owner, room["code"])
    expected_ids = add_speeches(room["code"], match_id, count=5, prefix="cursor")

    # Existing clients remain compatible: the default response still carries
    # the speeches array and, below the legacy 100-row default, returns all rows.
    compatible = owner.get(f"/api/rooms/{room['code']}/result")
    assert compatible.status_code == 200
    assert [item["id"] for item in compatible.json()["speeches"]] == expected_ids
    assert compatible.json()["speech_pagination"] == {
        "page": 1,
        "page_size": 100,
        "total": 5,
        "pages": 1,
        "next_cursor": None,
        "has_more": False,
    }

    first = owner.get(f"/api/rooms/{room['code']}/result?speech_page_size=2")
    assert first.status_code == 200
    first_payload = first.json()
    assert [item["id"] for item in first_payload["speeches"]] == expected_ids[3:]
    assert first_payload["speech_pagination"]["page"] == 1
    assert first_payload["speech_pagination"]["has_more"] is True
    cursor = first_payload["speech_pagination"]["next_cursor"]
    assert cursor

    # A running match may append a new speech between requests. A cursor for
    # earlier rows must not shift or repeat its already-observed newest page.
    appended_id = "cursor-005"
    with SessionLocal() as db:
        current_room = load_room(db, room["code"], lock=True)
        db.add(
            Speech(
                id=appended_id,
                match_id=match_id,
                room_id=current_room.id,
                seat_key="neg_1",
                stage_key="stage_5",
                speaker_type="ai",
                status="completed",
                content="cursor 发言 5",
                created_at=datetime(2026, 7, 19, 8, 0, 10, tzinfo=timezone.utc),
            )
        )
        db.commit()

    second = owner.get(
        f"/api/rooms/{room['code']}/result",
        params={"speech_page_size": 2, "speech_cursor": cursor},
    )
    assert second.status_code == 200
    second_payload = second.json()
    assert [item["id"] for item in second_payload["speeches"]] == expected_ids[1:3]
    assert second_payload["speech_pagination"]["page"] == 2

    third = owner.get(
        f"/api/rooms/{room['code']}/result",
        params={"speech_page_size": 2, "speech_cursor": second_payload["speech_pagination"]["next_cursor"]},
    )
    assert third.status_code == 200
    assert [item["id"] for item in third.json()["speeches"]] == expected_ids[:1]
    assert third.json()["speech_pagination"]["page"] == 3
    assert third.json()["speech_pagination"]["has_more"] is False
    assert third.json()["speech_pagination"]["total"] == 5

    offset_second = owner.get(f"/api/rooms/{room['code']}/result?speech_page=2&speech_page_size=2")
    assert [item["id"] for item in offset_second.json()["speeches"]] == expected_ids[2:4]

    # A cursor cannot silently change its page width or be edited by a client.
    wrong_size = owner.get(
        f"/api/rooms/{room['code']}/result",
        params={"speech_page_size": 3, "speech_cursor": cursor},
    )
    assert wrong_size.status_code == 422
    tampered = f"{cursor[:-1]}{'A' if cursor[-1] != 'A' else 'B'}"
    assert owner.get(
        f"/api/rooms/{room['code']}/result",
        params={"speech_page_size": 2, "speech_cursor": tampered},
    ).status_code == 422


def test_history_cursor_order_isolated_by_match_and_permissions(client: TestClient, register_user) -> None:
    owner = register_user("round7_history_owner")
    outsider = register_user("round7_history_outsider")
    first_room = create_training_room(owner, "私密比赛分页权限测试", visibility="private")
    first_match = start_room(owner, first_room["code"])
    first_ids = add_speeches(first_room["code"], first_match, count=4, prefix="private")

    first_page = owner.get(f"/api/matches/{first_match}/history?speech_page_size=2")
    assert first_page.status_code == 200
    assert [item["content"] for item in first_page.json()["speeches"]] == ["private 发言 2", "private 发言 3"]
    cursor = first_page.json()["speech_pagination"]["next_cursor"]
    next_page = owner.get(
        f"/api/matches/{first_match}/history",
        params={"speech_page_size": 2, "speech_cursor": cursor},
    )
    assert next_page.status_code == 200
    assert [item["content"] for item in next_page.json()["speeches"]] == ["private 发言 0", "private 发言 1"]
    assert first_ids == [f"private-{index:03d}" for index in range(4)]

    assert client.get(f"/api/rooms/{first_room['code']}/result").status_code == 401
    assert outsider.get(f"/api/rooms/{first_room['code']}/result").status_code == 403
    assert client.get(f"/api/matches/{first_match}/history").status_code == 401
    assert outsider.get(f"/api/matches/{first_match}/history").status_code == 403

    other_owner = register_user("round7_other_owner")
    second_room = create_training_room(other_owner, "游标跨比赛隔离测试")
    second_match = start_room(other_owner, second_room["code"])
    add_speeches(second_room["code"], second_match, count=3, prefix="other")
    cross_match = other_owner.get(
        f"/api/matches/{second_match}/history",
        params={"speech_page_size": 2, "speech_cursor": cursor},
    )
    assert cross_match.status_code == 422
    assert "不属于当前比赛" in cross_match.json()["detail"]


def test_legacy_match_speeches_keep_compatible_result_and_history_shape(client: TestClient, register_user) -> None:
    owner = register_user("round7_legacy_owner")
    room = create_training_room(owner, "旧比赛逐字稿兼容测试")
    match_id = start_room(owner, room["code"])
    add_speeches(room["code"], match_id, count=3, prefix="legacy", legacy=True)

    assert client.get(f"/api/rooms/{room['code']}/result?speech_page_size=2").status_code == 409
    result = owner.get(f"/api/rooms/{room['code']}/result?speech_page_size=2")
    assert result.status_code == 200
    payload = result.json()
    assert [item["content"] for item in payload["speeches"]] == ["legacy 发言 1", "legacy 发言 2"]
    assert payload["speeches"][0]["stage_name"] == "legacy_stage"
    assert payload["speeches"][0]["audio_url"] == ""
    assert payload["speech_pagination"]["total"] == 3

    history = owner.get(f"/api/matches/{match_id}/history?speech_page=2&speech_page_size=2")
    assert history.status_code == 200
    assert history.json()["speeches"] == [
        {
            "seat_key": "aff_1",
            "stage_key": "legacy_stage",
            "content": "legacy 发言 0",
            "audio_url": "",
        }
    ]


def test_0025_migration_is_idempotent_for_existing_databases(tmp_path: Path) -> None:
    migration_path = (
        Path(__file__).parents[1] / "alembic" / "versions" / "0025_speech_result_pagination.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0025_speech_result_pagination", migration_path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    metadata = sa.MetaData()
    sa.Table(
        "speeches",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("match_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        migration.op = Operations(context)
        migration.upgrade()
        migration.upgrade()
        indexes = {item["name"]: item for item in sa.inspect(connection).get_indexes("speeches")}
        assert indexes[migration.INDEX_NAME]["column_names"] == ["match_id", "created_at", "id"]
        migration.downgrade()
        migration.downgrade()
        assert migration.INDEX_NAME not in {
            item["name"] for item in sa.inspect(connection).get_indexes("speeches")
        }
