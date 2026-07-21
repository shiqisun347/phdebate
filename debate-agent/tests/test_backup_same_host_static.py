from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_backup_status_remains_readable_by_the_platform_health_check() -> None:
    script = (ROOT / "deploy/backup.same-host.sh").read_text()

    assert 'STATUS_GROUP="${DEBATE_AGENT_STATUS_GROUP:-ubuntu}"' in script
    assert 'chgrp "$STATUS_GROUP" "$STATUS_FILE"' in script
    assert 'chmod 640 "$STATUS_FILE"' in script
    assert "write_status --state running" in script
    assert "write_status --state succeeded" in script
    assert "write_status --state failed" in script
