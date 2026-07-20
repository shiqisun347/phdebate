from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

DEPLOY = Path(__file__).resolve().parents[1]


def test_storage_audit_reports_releases_archives_logs_and_legacy_root(tmp_path: Path) -> None:
    root = tmp_path / "phdebate"
    agent = tmp_path / "debate-agent"
    (root / "runtime" / "api-releases" / "api-one").mkdir(parents=True)
    (root / "runtime" / "web-releases" / "web-one").mkdir(parents=True)
    deploy_backups = root / "runtime" / "deploy-backups"
    logs = root / "runtime" / "logs"
    agent_backups = agent / "backups"
    for directory in (deploy_backups, logs, agent_backups, root / "storage"):
        directory.mkdir(parents=True, exist_ok=True)
    (deploy_backups / "old-source.tar.gz").write_bytes(b"source")
    (deploy_backups / "current-data-volumes.tar.gz").write_bytes(b"data")
    (deploy_backups / "recovery-set-current.manifest").write_text("schema_version=3\n")
    (logs / "api.stderr.log").write_bytes(b"large-log")
    (agent_backups / "agent.dump").write_bytes(b"agent")
    (tmp_path / "phdebate-v2").mkdir()

    result = subprocess.run(
        [
            sys.executable,
            str(DEPLOY / "audit-storage.py"),
            "--root", str(root),
            "--agent-root", str(agent),
            "--large-log-bytes", "1",
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    payload = json.loads(result.stdout)

    assert payload["retention"]["source_archives"]["count"] == 1
    assert payload["retention"]["source_archives"]["bytes"] == 6
    assert payload["retention"]["data_volume_archives"]["count"] == 1
    assert payload["retention"]["data_volume_archives"]["bytes"] == 4
    assert payload["retention"]["api_releases"]["count"] == 1
    assert payload["logs"]["large"][0]["file"] == "runtime/logs/api.stderr.log"
    assert payload["single_platform"]["legacy_root_exists"] is True


def test_storage_audit_fail_closed_flag_rejects_legacy_parallel_root(tmp_path: Path) -> None:
    root = tmp_path / "phdebate"
    root.mkdir()
    (tmp_path / "phdebate-v2").mkdir()

    result = subprocess.run(
        [sys.executable, str(DEPLOY / "audit-storage.py"), "--root", str(root), "--fail-on-critical"],
        text=True,
        capture_output=True,
    )

    assert result.returncode == 1
