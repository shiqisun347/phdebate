#!/usr/bin/env python3
"""Verify production automation versioning and restore all formal configuration afterward."""

from __future__ import annotations

import argparse
import time
from secrets import token_hex

import httpx
from app.core.database import SessionLocal
from app.models.entities import AdminAuditLog, AutomationTemplate, Competition, MatchEvent, Room, RoomSeat, User, UserSession
from app.services.room_service import load_room
from app.services.seed import seed_database
from app.services.verification_cleanup import release_verification_room_codes
from operator_credentials import admin_credentials
from sqlalchemy import delete


def csrf(client: httpx.Client) -> dict[str, str]:
    value = client.cookies.get("jixia_csrf")
    return {"X-CSRF-Token": value} if value else {}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://117.50.192.216")
    args = parser.parse_args()
    account, password = admin_credentials()
    suffix = f"{int(time.time())}_{token_hex(3)}"
    admin = httpx.Client(base_url=args.base_url, verify=False, timeout=20, follow_redirects=True)
    participant = httpx.Client(base_url=args.base_url, verify=False, timeout=20, follow_redirects=True)
    source_template_id = created_template_id = competition_id = room_id = user_id = ""
    try:
        admin.post("/api/auth/login", json={"account": account, "password": password}).raise_for_status()
        competitions = admin.get("/api/admin/competitions").json()["items"]
        training = next(item for item in competitions if item["slug"] == "training-1v1")
        competition_id = training["id"]
        templates = admin.get("/api/admin/automation-templates").json()["items"]
        source = next(item for item in templates if any(bound["id"] == competition_id for bound in item["competitions"]))
        source_template_id = source["id"]
        stages = [dict(stage) for stage in source["stages"]]
        stages[0]["duration"] = int(stages[0]["duration"]) + 1
        created = admin.post(
            f"/api/admin/automation-templates/{source_template_id}/versions",
            headers=csrf(admin),
            json={
                "name": f"生产验收临时流程 {suffix}",
                "stages": stages,
                "competition_ids": [competition_id],
            },
        )
        created.raise_for_status()
        template = created.json()["template"]
        created_template_id = template["id"]
        assert template["version"] == source["version"] + 1
        replayed = admin.post(
            f"/api/admin/automation-templates/{source_template_id}/versions",
            headers=csrf(admin),
            json={
                "name": f"生产验收临时流程 {suffix}",
                "stages": stages,
                "competition_ids": [competition_id],
            },
        )
        replayed.raise_for_status()
        assert replayed.json()["replayed"] is True
        assert replayed.json()["template"]["id"] == created_template_id

        with SessionLocal() as db:
            competition = db.get(Competition, competition_id)
            assert competition.automation_template_id == created_template_id
            seed_database(db)
            db.refresh(competition)
            assert competition.automation_template_id == created_template_id

        registered = participant.post(
            "/api/auth/register",
            json={
                "account": f"template_verify_{suffix}",
                "real_name": "自动流程版本验收",
                "password": "Template-version-1234",
                "confirm_password": "Template-version-1234",
            },
        )
        registered.raise_for_status()
        user_id = registered.json()["user"]["id"]
        room_response = participant.post(
            "/api/rooms",
            headers=csrf(participant) | {"X-Idempotency-Key": token_hex(16)},
            json={
                "competition_slug": "training-1v1",
                "custom_topic": "新房间是否固定使用刚创建的自动流程版本？",
                "seat_key": "aff_1",
                "visibility": "private",
            },
        )
        room_response.raise_for_status()
        code = room_response.json()["room"]["code"]
        with SessionLocal() as db:
            room = load_room(db, code)
            room_id = room.id
            assert room.template_snapshot[0]["duration"] == stages[0]["duration"]
        print(
            f"automation_versioning_verified v{source['version']}->v{template['version']} replayed=1 seed_preserved=1 new_room_snapshot=1"
        )
    finally:
        participant.close()
        if competition_id and source_template_id:
            with SessionLocal() as db:
                if room_id:
                    db.execute(delete(MatchEvent).where(MatchEvent.room_id == room_id))
                    db.execute(delete(RoomSeat).where(RoomSeat.room_id == room_id))
                    release_verification_room_codes(db, [room_id])
                    db.execute(delete(Room).where(Room.id == room_id))
                if user_id:
                    db.execute(delete(UserSession).where(UserSession.user_id == user_id))
                    db.execute(delete(User).where(User.id == user_id))
                competition = db.get(Competition, competition_id)
                if competition:
                    competition.automation_template_id = source_template_id
                    db.flush()
                if created_template_id:
                    db.execute(delete(AdminAuditLog).where(AdminAuditLog.target_id == created_template_id))
                    db.execute(delete(AutomationTemplate).where(AutomationTemplate.id == created_template_id))
                db.commit()
        if admin.cookies.get("jixia_session"):
            admin.post("/api/auth/logout", headers=csrf(admin), json={})
        admin.close()


if __name__ == "__main__":
    main()
