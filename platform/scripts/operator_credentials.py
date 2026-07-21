"""Resolve operator credentials during the single-version naming migration.

New automation should use ``PHDEBATE_ADMIN_*``. The previous names remain a
read-only fallback until the frozen runtime settings are migrated in a
separately approved release.
"""

from __future__ import annotations

import os


def admin_credentials() -> tuple[str, str]:
    account = os.environ.get("PHDEBATE_ADMIN_ACCOUNT") or os.environ.get("V2_ADMIN_ACCOUNT", "")
    password = os.environ.get("PHDEBATE_ADMIN_PASSWORD") or os.environ.get("V2_ADMIN_PASSWORD", "")
    if not account or not password:
        raise RuntimeError("production administrator credentials are not configured")
    return account, password
