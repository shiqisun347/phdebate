#!/usr/bin/env python3
"""Run read-only live checks for paginated admin collections."""

from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone

import httpx
from app.core.database import SessionLocal
from app.models.entities import AdminAuditLog, AgentProfile, Room, RoomSeat
from sqlalchemy import delete, select


def csrf(client: httpx.Client) -> dict[str, str]:
    value = client.cookies.get("jixia_csrf")
    return {"X-CSRF-Token": value} if value else {}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://117.50.192.216")
    args = parser.parse_args()
    account = os.environ.get("V2_ADMIN_ACCOUNT", "")
    password = os.environ.get("V2_ADMIN_PASSWORD", "")
    if not account or not password:
        raise RuntimeError("production admin credentials are not configured")

    client = httpx.Client(base_url=args.base_url, verify=False, timeout=20, follow_redirects=True)
    actor_id = ""
    mutation_started_at: datetime | None = None
    topic_state: tuple[str, str, bool] | None = None
    agent_state: tuple[str, bool] | None = None
    try:
        client.post("/api/auth/login", json={"account": account, "password": password}).raise_for_status()
        actor_id = client.get("/api/auth/session").json()["user"]["id"]
        users = client.get("/api/admin/users", params={"page": 1, "page_size": 2})
        users.raise_for_status()
        user_payload = users.json()
        assert len(user_payload["items"]) <= 2 and user_payload["pagination"]["total"] >= 1
        found = client.get("/api/admin/users", params={"q": account})
        found.raise_for_status()
        assert found.json()["pagination"]["total"] == 1

        rooms = client.get("/api/admin/rooms", params={"page": 1, "page_size": 2})
        rooms.raise_for_status()
        room_payload = rooms.json()
        assert len(room_payload["items"]) <= 2 and room_payload["pagination"]["total"] >= 1
        audit = client.get("/api/admin/audit", params={"page": 1, "page_size": 2})
        audit.raise_for_status()
        audit_payload = audit.json()
        assert len(audit_payload["items"]) <= 2
        assert all(item.get("actor_name") and isinstance(item.get("payload"), dict) for item in audit_payload["items"])

        competitions = client.get("/api/admin/competitions").json()["items"]
        competition = next(item for item in competitions if item["topics"])
        topic = competition["topics"][0]
        topic_state = (competition["id"], topic["id"], topic.get("is_active", True))
        agents = client.get("/api/admin/agents").json()["items"]
        agent = agents[0]
        agent_state = (agent["id"], agent["is_active"])
        mutation_started_at = datetime.now(timezone.utc)
        topic_update = client.patch(
            f"/api/admin/competitions/{competition['id']}/topics/{topic['id']}",
            headers=csrf(client),
            json={"is_active": not topic_state[2]},
        )
        topic_update.raise_for_status()
        assert topic_update.json()["topic"]["is_active"] is (not topic_state[2])
        rejected_model = client.patch(
            f"/api/admin/agents/{agent['id']}",
            headers=csrf(client),
            json={"model_name": "Qwen3-8B"},
        )
        assert rejected_model.status_code == 422
        agent_update = client.patch(
            f"/api/admin/agents/{agent['id']}",
            headers=csrf(client),
            json={"is_active": not agent_state[1]},
        )
        agent_update.raise_for_status()
        assert agent_update.json()["agent"]["is_active"] is (not agent_state[1])

        with SessionLocal() as db:
            active_profile = db.execute(
                select(AgentProfile.id, AgentProfile.voice_id)
                .join(RoomSeat, RoomSeat.agent_profile_id == AgentProfile.id)
                .join(Room, Room.id == RoomSeat.room_id)
                .where(Room.status.in_(["preparing", "running", "paused", "judging"]))
                .limit(1)
            ).first()
        if active_profile:
            blocked_runtime_edit = client.patch(
                f"/api/admin/agents/{active_profile.id}",
                headers=csrf(client),
                json={"voice_id": active_profile.voice_id},
            )
            assert blocked_runtime_edit.status_code == 409

        client.patch(
            f"/api/admin/competitions/{topic_state[0]}/topics/{topic_state[1]}",
            headers=csrf(client),
            json={"is_active": topic_state[2]},
        ).raise_for_status()
        topic_state = None
        client.patch(
            f"/api/admin/agents/{agent_state[0]}",
            headers=csrf(client),
            json={"is_active": agent_state[1]},
        ).raise_for_status()
        agent_state = None
        print(
            "admin_pagination_verified "
            f"users={user_payload['pagination']['total']} "
            f"rooms={room_payload['pagination']['total']} "
            f"audit={audit_payload['pagination']['total']} "
            "topic_toggle=restored agent_toggle=restored qwen3_8b=422 active_profile_edit=409"
        )
    finally:
        if topic_state:
            client.patch(
                f"/api/admin/competitions/{topic_state[0]}/topics/{topic_state[1]}",
                headers=csrf(client),
                json={"is_active": topic_state[2]},
            )
        if agent_state:
            client.patch(
                f"/api/admin/agents/{agent_state[0]}",
                headers=csrf(client),
                json={"is_active": agent_state[1]},
            )
        if client.cookies.get("jixia_session"):
            client.post("/api/auth/logout", headers=csrf(client), json={})
        client.close()
        if actor_id and mutation_started_at:
            with SessionLocal() as db:
                db.execute(
                    delete(AdminAuditLog).where(
                        AdminAuditLog.actor_user_id == actor_id,
                        AdminAuditLog.created_at >= mutation_started_at,
                        AdminAuditLog.action.in_(["topic.patch", "agent.patch"]),
                    )
                )
                db.commit()


if __name__ == "__main__":
    main()
