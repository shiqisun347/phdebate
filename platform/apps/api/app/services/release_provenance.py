from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

OBJECT_ID = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


def provenance_path() -> Path:
    configured = os.getenv("RELEASE_PROVENANCE_FILE", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parents[4] / ".release-provenance.json"


def release_provenance() -> dict[str, Any]:
    try:
        payload = json.loads(provenance_path().read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise ValueError("unsupported schema")
        commit = str(payload["source_commit"])
        tree = str(payload["source_tree"])
        release = str(payload["release"])
        if payload.get("service") != "phdebate-api":
            raise ValueError("service mismatch")
        if not OBJECT_ID.fullmatch(commit) or not OBJECT_ID.fullmatch(tree):
            raise ValueError("invalid Git object id")
        if not release or len(release) > 160:
            raise ValueError("invalid release")
        return {
            "ok": True,
            "release": release,
            "source_commit": commit,
            "source_tree": tree,
            "built_at": str(payload.get("built_at") or ""),
        }
    except Exception as exc:
        return {"ok": False, "message": type(exc).__name__}
