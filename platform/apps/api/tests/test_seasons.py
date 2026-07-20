from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.core.database import SessionLocal
from app.models.entities import Competition, LeaderboardEntry, Match, Room
from conftest import csrf
from fastapi.testclient import TestClient
from sqlalchemy import select


def admin_client(client: TestClient) -> TestClient:
    admin = TestClient(client.app)
    admin.__enter__()
    response = admin.post("/api/auth/login", json={"account": "admin_test", "password": "Admin-test-1234"})
    assert response.status_code == 200, response.text
    return admin


def create_season(admin: TestClient, *, prefix: str, starts_at: datetime, ends_at: datetime | None) -> dict:
    slug = f"{prefix}-{uuid4().hex[:10]}"
    response = admin.post(
        "/api/admin/seasons",
        headers=csrf(admin),
        json={
            "name": f"赛季 {slug}",
            "slug": slug,
            "starts_at": starts_at.isoformat(),
            "ends_at": ends_at.isoformat() if ends_at else None,
            "is_active": True,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["season"]


def test_ranked_rooms_freeze_their_season_and_closed_seasons_cannot_start(client: TestClient, register_user) -> None:
    admin = admin_client(client)
    clock = datetime.now(timezone.utc)
    first = create_season(admin, prefix="freeze-a", starts_at=clock - timedelta(days=1), ends_at=clock + timedelta(days=7))
    second = create_season(admin, prefix="freeze-b", starts_at=clock - timedelta(hours=1), ends_at=clock + timedelta(days=14))
    competition = client.get("/api/competitions/daily-4v4").json()["competition"]
    original_season_id = competition["season"]["id"]
    owner_a = register_user("season_freeze_a")
    owner_b = register_user("season_freeze_b")
    late_joiner = register_user("season_late_joiner")

    try:
        invalid = admin.post(
            "/api/admin/seasons",
            headers=csrf(admin),
            json={
                "name": "倒置赛季",
                "slug": f"invalid-{uuid4().hex[:10]}",
                "starts_at": clock.isoformat(),
                "ends_at": (clock - timedelta(seconds=1)).isoformat(),
            },
        )
        assert invalid.status_code == 422

        bound_first = admin.patch(f"/api/admin/competitions/{competition['id']}", headers=csrf(admin), json={"season_id": first["id"]})
        assert bound_first.status_code == 200, bound_first.text
        created_a = owner_a.post(
            "/api/rooms",
            headers=csrf(owner_a),
            json={
                "competition_slug": "daily-4v4",
                "topic_id": competition["topics"][0]["id"],
                "seat_key": "aff_1",
                "visibility": "public",
            },
        )
        assert created_a.status_code == 200, created_a.text
        room_a = created_a.json()["room"]
        assert room_a["season"]["id"] == first["id"]

        bound_second = admin.patch(f"/api/admin/competitions/{competition['id']}", headers=csrf(admin), json={"season_id": second["id"]})
        assert bound_second.status_code == 200, bound_second.text
        assert owner_a.post(f"/api/rooms/{room_a['code']}/ready", headers=csrf(owner_a), json={"ready": True}).status_code == 200
        started_a = owner_a.post(f"/api/rooms/{room_a['code']}/start", headers=csrf(owner_a), json={})
        assert started_a.status_code == 200, started_a.text

        with SessionLocal() as db:
            stored_a = db.scalar(select(Room).where(Room.code == room_a["code"]))
            match_a = db.scalar(select(Match).where(Match.room_id == stored_a.id))
            assert stored_a.season_id == first["id"]
            assert match_a and match_a.season_id == first["id"]

        immutable_dates = admin.patch(
            f"/api/admin/seasons/{first['id']}",
            headers=csrf(admin),
            json={"starts_at": (clock - timedelta(days=2)).isoformat()},
        )
        assert immutable_dates.status_code == 409

        created_b = owner_b.post(
            "/api/rooms",
            headers=csrf(owner_b),
            json={
                "competition_slug": "daily-4v4",
                "topic_id": competition["topics"][0]["id"],
                "seat_key": "neg_1",
                "visibility": "public",
            },
        )
        assert created_b.status_code == 200, created_b.text
        room_b = created_b.json()["room"]
        assert room_b["season"]["id"] == second["id"]
        assert owner_b.post(f"/api/rooms/{room_b['code']}/ready", headers=csrf(owner_b), json={"ready": True}).status_code == 200

        still_bound = admin.patch(f"/api/admin/seasons/{second['id']}", headers=csrf(admin), json={"is_active": False})
        assert still_bound.status_code == 409

        restored = admin.patch(f"/api/admin/competitions/{competition['id']}", headers=csrf(admin), json={"season_id": original_season_id})
        assert restored.status_code == 200, restored.text
        deactivated = admin.patch(f"/api/admin/seasons/{second['id']}", headers=csrf(admin), json={"is_active": False})
        assert deactivated.status_code == 200, deactivated.text
        rejected_claim = late_joiner.post(
            f"/api/rooms/{room_b['code']}/claim-seat",
            headers=csrf(late_joiner),
            json={"seat_key": "aff_2"},
        )
        assert rejected_claim.status_code == 409 and "赛季已关闭" in rejected_claim.json()["detail"]
        assert owner_b.post(f"/api/rooms/{room_b['code']}/ready", headers=csrf(owner_b), json={"ready": False}).status_code == 200
        rejected_ready = owner_b.post(f"/api/rooms/{room_b['code']}/ready", headers=csrf(owner_b), json={"ready": True})
        assert rejected_ready.status_code == 409 and "赛季已关闭" in rejected_ready.json()["detail"]
        rejected_start = owner_b.post(f"/api/rooms/{room_b['code']}/start", headers=csrf(owner_b), json={})
        assert rejected_start.status_code == 409
        assert "赛季已关闭" in rejected_start.json()["detail"]

        cleared_end = admin.patch(f"/api/admin/seasons/{second['id']}", headers=csrf(admin), json={"ends_at": None})
        assert cleared_end.status_code == 200, cleared_end.text
        assert cleared_end.json()["season"]["ends_at"] is None
    finally:
        admin.patch(f"/api/admin/competitions/{competition['id']}", headers=csrf(admin), json={"season_id": original_season_id})
        for season in (first, second):
            admin.patch(f"/api/admin/seasons/{season['id']}", headers=csrf(admin), json={"is_active": False})
        admin.__exit__(None, None, None)


def test_rankings_filter_by_season_and_aggregate_users_across_competitions(client: TestClient, register_user) -> None:
    admin = admin_client(client)
    clock = datetime.now(timezone.utc)
    season = create_season(admin, prefix="ranking", starts_at=clock - timedelta(days=2), ends_at=clock + timedelta(days=2))
    other_season = create_season(admin, prefix="ranking-other", starts_at=clock - timedelta(days=4), ends_at=clock - timedelta(days=3))
    first_user = register_user("season_rank_first")
    second_user = register_user("season_rank_second")
    first_user_id = first_user.get("/api/auth/session").json()["user"]["id"]
    second_user_id = second_user.get("/api/auth/session").json()["user"]["id"]

    try:
        with SessionLocal() as db:
            daily = db.scalar(select(Competition).where(Competition.slug == "daily-4v4"))
            training = db.scalar(select(Competition).where(Competition.slug == "training-1v1"))
            db.add_all(
                [
                    LeaderboardEntry(
                        competition_id=daily.id,
                        season_id=season["id"],
                        user_id=first_user_id,
                        points=3,
                        wins=1,
                        matches=1,
                        average_score=90,
                        last_match_at=clock,
                    ),
                    LeaderboardEntry(
                        competition_id=training.id,
                        season_id=season["id"],
                        user_id=first_user_id,
                        points=1,
                        draws=1,
                        matches=1,
                        average_score=80,
                        last_match_at=clock - timedelta(hours=1),
                    ),
                    LeaderboardEntry(
                        competition_id=daily.id,
                        season_id=season["id"],
                        user_id=second_user_id,
                        points=3,
                        wins=1,
                        matches=1,
                        average_score=90,
                        last_match_at=clock - timedelta(days=1),
                    ),
                    LeaderboardEntry(
                        competition_id=daily.id,
                        season_id=other_season["id"],
                        user_id=first_user_id,
                        points=99,
                        wins=33,
                        matches=33,
                        average_score=99,
                        last_match_at=clock - timedelta(days=3),
                    ),
                ]
            )
            db.commit()

        competition_ranking = client.get(f"/api/rankings?competition_slug=daily-4v4&season_slug={season['slug']}")
        assert competition_ranking.status_code == 200, competition_ranking.text
        competition_items = competition_ranking.json()["items"]
        assert [item["user_id"] for item in competition_items[:2]] == [first_user_id, second_user_id]
        assert all(item["season_id"] == season["id"] for item in competition_items)

        global_ranking = client.get(f"/api/rankings?season_slug={season['slug']}")
        assert global_ranking.status_code == 200, global_ranking.text
        items = global_ranking.json()["items"]
        first_rows = [item for item in items if item["user_id"] == first_user_id]
        assert len(first_rows) == 1
        assert first_rows[0]["points"] == 4
        assert first_rows[0]["matches"] == 2
        assert first_rows[0]["average_score"] == 85
        assert all(item["points"] < 99 for item in items)
        assert client.get("/api/rankings?season_slug=missing-season").status_code == 404
    finally:
        for item in (season, other_season):
            admin.patch(f"/api/admin/seasons/{item['id']}", headers=csrf(admin), json={"is_active": False})
        admin.__exit__(None, None, None)
