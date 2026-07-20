from __future__ import annotations

import json
from collections import namedtuple

from app.services import release_provenance as release_service
from app.services import system_health


def test_storage_has_early_warning_and_lower_readiness_failure_threshold(monkeypatch) -> None:
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setenv("HEALTH_DISK_WARNING_FREE_PERCENT", "20")
    monkeypatch.setenv("HEALTH_DISK_FAILURE_FREE_PERCENT", "10")

    monkeypatch.setattr(system_health.shutil, "disk_usage", lambda _path: Usage(1000, 816, 184))
    warning = system_health._storage_check(production=True)
    assert warning == {
        "ok": True,
        "warning": True,
        "status": "warning",
        "skipped": False,
        "free_percent": 18.4,
        "free_bytes": 184,
        "warning_percent": 20.0,
        "failure_percent": 10.0,
    }

    monkeypatch.setattr(system_health.shutil, "disk_usage", lambda _path: Usage(1000, 905, 95))
    critical = system_health._storage_check(production=True)
    assert critical["ok"] is False and critical["status"] == "critical"


def test_api_release_provenance_exposes_only_allowlisted_non_secret_fields(monkeypatch, tmp_path) -> None:
    record = tmp_path / ".release-provenance.json"
    record.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "service": "phdebate-api",
                "release": "round15",
                "source_commit": "a" * 40,
                "source_tree": "b" * 40,
                "built_at": "2026-07-20T00:00:00Z",
                "accidental_secret": "must-not-leak",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("RELEASE_PROVENANCE_FILE", str(record))

    result = release_service.release_provenance()

    assert result["ok"] is True and result["release"] == "round15"
    assert result["source_commit"] == "a" * 40 and result["source_tree"] == "b" * 40
    assert "accidental_secret" not in result


def test_api_release_provenance_fails_closed_on_tampering(monkeypatch, tmp_path) -> None:
    record = tmp_path / ".release-provenance.json"
    record.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "service": "phdebate-api",
                "release": "round15",
                "source_commit": "not-a-git-object",
                "source_tree": "b" * 40,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("RELEASE_PROVENANCE_FILE", str(record))
    assert release_service.release_provenance()["ok"] is False
