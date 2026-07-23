#!/usr/bin/env python3
"""Verify live admin password recovery with a disposable user."""

from __future__ import annotations

import argparse
import time
from secrets import token_hex

import httpx
from app.core.database import SessionLocal
from app.models.entities import AdminAuditLog, User, UserSession
from operator_credentials import admin_credentials
from sqlalchemy import delete, select


def csrf(client: httpx.Client) -> dict[str, str]:
    value = client.cookies.get("jixia_csrf")
    return {"X-CSRF-Token": value} if value else {}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://117.50.192.216")
    args = parser.parse_args()
    admin_account, admin_password = admin_credentials()

    account = f"recovery_{int(time.time())}_{token_hex(3)}"
    old_password = "Recovery-old-1234"
    new_password = "Recovery-new-5678"
    user_id = ""
    victim = httpx.Client(base_url=args.base_url, verify=False, timeout=20, follow_redirects=True)
    admin = httpx.Client(base_url=args.base_url, verify=False, timeout=20, follow_redirects=True)
    fresh = httpx.Client(base_url=args.base_url, verify=False, timeout=20, follow_redirects=True)
    try:
        registered = victim.post(
            "/api/auth/register",
            json={
                "account": account,
                "real_name": "管理员恢复验收用户",
                "password": old_password,
                "confirm_password": old_password,
            },
        )
        registered.raise_for_status()
        user_id = registered.json()["user"]["id"]
        admin.post(
            "/api/auth/login",
            json={"account": admin_account, "password": admin_password},
        ).raise_for_status()
        reset = admin.post(
            f"/api/admin/users/{user_id}/reset-password",
            headers=csrf(admin),
            json={"new_password": new_password, "confirm_password": new_password},
        )
        reset.raise_for_status()
        assert reset.json()["revoked_sessions"] == 1
        assert victim.get("/api/auth/session").status_code == 401
        assert fresh.post("/api/auth/login", json={"account": account, "password": old_password}).status_code == 401
        fresh.post("/api/auth/login", json={"account": account, "password": new_password}).raise_for_status()

        with SessionLocal() as db:
            log = db.scalar(
                select(AdminAuditLog)
                .where(AdminAuditLog.action == "user.password_reset", AdminAuditLog.target_id == user_id)
                .order_by(AdminAuditLog.created_at.desc())
            )
            assert log and log.payload == {"revoked_sessions": 1}
        print("admin_account_recovery_verified sessions_revoked=1 secret_audited=false")
    finally:
        if admin.cookies.get("jixia_session"):
            admin.post("/api/auth/logout", headers=csrf(admin), json={})
        victim.close()
        admin.close()
        fresh.close()
        if user_id:
            with SessionLocal() as db:
                db.execute(
                    delete(AdminAuditLog).where(
                        AdminAuditLog.action == "user.password_reset",
                        AdminAuditLog.target_id == user_id,
                    )
                )
                db.execute(delete(UserSession).where(UserSession.user_id == user_id))
                user = db.get(User, user_id)
                if user:
                    db.delete(user)
                db.commit()


if __name__ == "__main__":
    main()
