"""Resolve production operator credentials from the canonical environment names."""

from __future__ import annotations

import os


def admin_credentials() -> tuple[str, str]:
    account = os.environ.get("PHDEBATE_ADMIN_ACCOUNT", "")
    password = os.environ.get("PHDEBATE_ADMIN_PASSWORD", "")
    if not account or not password:
        raise RuntimeError("production administrator credentials are not configured")
    return account, password
