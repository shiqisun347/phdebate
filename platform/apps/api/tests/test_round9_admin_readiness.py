from __future__ import annotations

from app.core.database import SessionLocal
from app.models.entities import AutomationTemplate, Competition, CompetitionTopic
from fastapi.testclient import TestClient
from sqlalchemy import select
from tests.conftest import csrf


def test_admin_cannot_publish_a_competition_that_new_rooms_cannot_run(client: TestClient) -> None:
    admin = TestClient(client.app)
    competition_id = ""
    topic_ids: list[str] = []
    template_id = ""
    try:
        with admin:
            login = admin.post("/api/auth/login", json={"account": "admin_test", "password": "Admin-test-1234"})
            assert login.status_code == 200
            competition = next(
                item for item in admin.get("/api/admin/competitions").json()["items"] if item["slug"] == "daily-4v4"
            )
            competition_id = competition["id"]
            topic_ids = [topic["id"] for topic in competition["topics"]]
            assert len(topic_ids) >= 2
            with SessionLocal() as db:
                stored = db.get(Competition, competition_id)
                template_id = stored.automation_template_id

            for topic_id in topic_ids[:-1]:
                response = admin.patch(
                    f"/api/admin/competitions/{competition_id}/topics/{topic_id}",
                    headers=csrf(admin),
                    json={"is_active": False},
                )
                assert response.status_code == 200

            blocked_last_topic = admin.patch(
                f"/api/admin/competitions/{competition_id}/topics/{topic_ids[-1]}",
                headers=csrf(admin),
                json={"is_active": False},
            )
            assert blocked_last_topic.status_code == 409
            assert "至少一个启用辩题" in blocked_last_topic.json()["detail"]

            assert admin.patch(
                f"/api/admin/competitions/{competition_id}",
                headers=csrf(admin),
                json={"is_active": False},
            ).status_code == 200
            assert admin.patch(
                f"/api/admin/competitions/{competition_id}/topics/{topic_ids[-1]}",
                headers=csrf(admin),
                json={"is_active": False},
            ).status_code == 200

            blocked_without_topics = admin.patch(
                f"/api/admin/competitions/{competition_id}",
                headers=csrf(admin),
                json={"is_active": True},
            )
            assert blocked_without_topics.status_code == 409
            assert "至少需要一个启用中的辩题" in blocked_without_topics.json()["detail"]

            assert admin.patch(
                f"/api/admin/competitions/{competition_id}/topics/{topic_ids[-1]}",
                headers=csrf(admin),
                json={"is_active": True},
            ).status_code == 200
            with SessionLocal() as db:
                template = db.get(AutomationTemplate, template_id)
                template.is_active = False
                db.commit()
            blocked_without_flow = admin.patch(
                f"/api/admin/competitions/{competition_id}",
                headers=csrf(admin),
                json={"is_active": True},
            )
            assert blocked_without_flow.status_code == 409
            assert "自动流程模板" in blocked_without_flow.json()["detail"]
    finally:
        if competition_id:
            with SessionLocal() as db:
                competition = db.get(Competition, competition_id)
                if competition:
                    competition.is_active = True
                if template_id:
                    template = db.get(AutomationTemplate, template_id)
                    if template:
                        template.is_active = True
                if topic_ids:
                    for topic in db.scalars(select(CompetitionTopic).where(CompetitionTopic.id.in_(topic_ids))).all():
                        topic.is_active = True
                db.commit()


def test_room_creation_reports_an_unavailable_flow_instead_of_crashing(client: TestClient, register_user) -> None:
    player = register_user("round9_flow_guard")
    template_id = ""
    try:
        with SessionLocal() as db:
            competition = db.scalar(select(Competition).where(Competition.slug == "training-1v1"))
            template_id = competition.automation_template_id
            template = db.get(AutomationTemplate, template_id)
            template.is_active = False
            db.commit()
        response = player.post(
            "/api/rooms",
            headers={**csrf(player), "X-Idempotency-Key": "round9-inactive-flow"},
            json={
                "competition_slug": "training-1v1",
                "custom_topic": "自动流程异常时应向参赛者明确报错",
                "seat_key": "aff_1",
                "visibility": "public",
            },
        )
        assert response.status_code == 409
        assert response.json()["detail"] == "该赛事的自动比赛流程暂不可用，请联系管理员处理。"
    finally:
        if template_id:
            with SessionLocal() as db:
                template = db.get(AutomationTemplate, template_id)
                if template:
                    template.is_active = True
                    db.commit()
