#!/usr/bin/env python3
"""Run a disposable two-device account-security check against a live API."""

from __future__ import annotations

import argparse
import time
from secrets import token_hex

import httpx
from app.core.database import SessionLocal
from app.models.entities import User, UserSession
from sqlalchemy import delete, select


def csrf(client: httpx.Client) -> dict[str, str]:
    value = client.cookies.get("jixia_v2_csrf")
    return {"X-CSRF-Token": value} if value else {}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://117.50.192.216")
    args = parser.parse_args()
    account = f"security_{int(time.time())}_{token_hex(3)}"
    old_password = "Security-old-1234"
    new_password = "Security-new-5678"

    primary = httpx.Client(base_url=args.base_url, verify=False, timeout=20, follow_redirects=True)
    secondary = httpx.Client(base_url=args.base_url, verify=False, timeout=20, follow_redirects=True)
    fresh = httpx.Client(base_url=args.base_url, verify=False, timeout=20, follow_redirects=True)
    try:
        registered = primary.post(
            "/api/auth/register",
            json={
                "account": account,
                "real_name": "安全验收用户",
                "password": old_password,
                "confirm_password": old_password,
            },
        )
        registered.raise_for_status()
        secondary.post("/api/auth/login", json={"account": account, "password": old_password}).raise_for_status()

        revoked = primary.post("/api/auth/sessions/revoke-others", headers=csrf(primary), json={})
        revoked.raise_for_status()
        assert revoked.json()["revoked"] == 1
        assert secondary.get("/api/auth/session").status_code == 401
        secondary.post("/api/auth/login", json={"account": account, "password": old_password}).raise_for_status()

        changed = primary.post(
            "/api/auth/password",
            headers=csrf(primary),
            json={
                "current_password": old_password,
                "new_password": new_password,
                "confirm_password": new_password,
            },
        )
        changed.raise_for_status()
        assert primary.get("/api/auth/session").status_code == 200
        assert secondary.get("/api/auth/session").status_code == 401
        assert fresh.post("/api/auth/login", json={"account": account, "password": old_password}).status_code == 401
        fresh.post("/api/auth/login", json={"account": account, "password": new_password}).raise_for_status()
        print("account_security_verified devices=2 rotated=true old_password_rejected=true")
    finally:
        primary.close()
        secondary.close()
        fresh.close()
        with SessionLocal() as db:
            user = db.scalar(select(User).where(User.account == account))
            if user:
                db.execute(delete(UserSession).where(UserSession.user_id == user.id))
                db.delete(user)
                db.commit()


if __name__ == "__main__":
    main()
