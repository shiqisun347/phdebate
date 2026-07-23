from __future__ import annotations

import csv
import io

from app.core.database import SessionLocal
from app.main import app
from app.models.entities import Match, Room
from app.services.room_service import now
from conftest import csrf
from fastapi.testclient import TestClient


def _create_final_match(owner: TestClient, topic: str, *, is_test_data: bool) -> str:
    response = owner.post(
        "/api/rooms",
        headers=csrf(owner),
        json={
            "competition_slug": "training-1v1",
            "custom_topic": topic,
            "seat_key": "aff_1",
            "visibility": "public",
        },
    )
    assert response.status_code == 200, response.text
    room_id = response.json()["room"]["id"]
    with SessionLocal.begin() as db:
        room = db.get(Room, room_id)
        assert room is not None
        room.status = "completed"
        room.is_test_data = is_test_data
        room.started_at = now()
        room.completed_at = now()
        match = Match(
            room_id=room.id,
            competition_id=room.competition_id,
            season_id=room.season_id,
            status="completed",
            winner="affirmative",
        )
        db.add(match)
        db.flush()
        return match.id


def _csv_rows(response) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))


def test_admin_archive_index_excludes_test_data_and_is_admin_only(register_user) -> None:
    owner = register_user("archive_index_owner")
    production_id = _create_final_match(owner, '=HYPERLINK("https://invalid.example","正式比赛")', is_test_data=False)
    test_id = _create_final_match(owner, "测试比赛索引隔离测试", is_test_data=True)

    forbidden = owner.get("/api/admin/archive-index.csv")
    assert forbidden.status_code == 403

    with TestClient(app) as admin:
        login = admin.post("/api/auth/login", json={"account": "admin_test", "password": "Admin-test-1234"})
        assert login.status_code == 200

        exported = admin.get("/api/admin/archive-index.csv")
        assert exported.status_code == 200, exported.text
        assert exported.headers["content-type"].startswith("text/csv")
        assert exported.headers["cache-control"] == "private, no-store"
        assert "attachment" in exported.headers["content-disposition"]
        rows = _csv_rows(exported)
        ids = {row["match_id"] for row in rows}
        assert production_id in ids
        assert test_id not in ids
        production = next(row for row in rows if row["match_id"] == production_id)
        assert production["data_scope"] == "production"
        assert production["topic"].startswith("'=HYPERLINK")
        assert production["archive_url"] == f"/api/matches/{production_id}/archive"
        assert "测试选手archive_index_owner" in production["participants"]

        including_test = admin.get("/api/admin/archive-index.csv?include_test_data=true")
        assert including_test.status_code == 200
        included_ids = {row["match_id"] for row in _csv_rows(including_test)}
        assert {production_id, test_id}.issubset(included_ids)

        bounded = admin.get("/api/admin/archive-index.csv?include_test_data=true&limit=1")
        assert bounded.status_code == 409
        assert "分批导出" in bounded.json()["detail"]
