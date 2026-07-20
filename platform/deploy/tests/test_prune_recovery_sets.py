from __future__ import annotations

import hashlib
import os
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "prune-recovery-sets.sh"


def make_set(directory: Path, name: str, data_name: str, age_hours: int, schema: str = "3") -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    data = directory / data_name
    data.write_bytes(data_name.encode())
    (directory / f"{data_name}.sha256").write_text("checksum\n")
    manifest = directory / f"recovery-set-{name}.manifest"
    roles = {
        "platform_database" if schema == "3" else "v2_database": f"{name}-platform.dump",
        "agent_database": f"{name}-agent.dump",
        "data_volumes": data_name,
        "private_config": f"{name}-private.tar.gz",
        "reliable_voice": f"{name}-voice.tar.gz",
        "moss_offline": f"{name}-moss.tar",
    }
    manifest_lines = [f"schema_version={schema}"]
    for role, filename in roles.items():
        artifact = directory / filename
        if role != "data_volumes":
            artifact.write_bytes(f"{role}:{name}".encode())
        payload = artifact.read_bytes()
        manifest_lines.extend(
            (
                f"artifact.{role}.filename={filename}",
                f"artifact.{role}.bytes={len(payload)}",
                f"artifact.{role}.sha256={hashlib.sha256(payload).hexdigest()}",
            )
        )
    manifest.write_text("\n".join(manifest_lines) + "\n")
    timestamp = time.time() - age_hours * 3600
    os.utime(data, (timestamp, timestamp))
    os.utime(directory / f"{data_name}.sha256", (timestamp, timestamp))
    os.utime(manifest, (timestamp, timestamp))
    return manifest, data


def run(root: Path, mode: str, *, keep: int = 2, minimum_age: int = 24) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), mode],
        env=os.environ
        | {
            "PHDEBATE_ROOT": str(root),
            "PHDEBATE_AGENT_ROOT": str(root / "agent"),
            "PHDEBATE_DEPLOY_BACKUP_DIR": str(root / "runtime" / "deploy-backups"),
            "PHDEBATE_BACKUP_DIR": str(root / "runtime" / "deploy-backups"),
            "PHDEBATE_AGENT_BACKUP_DIR": str(root / "runtime" / "deploy-backups"),
            "PHDEBATE_RECOVERY_INDEX_VERIFY_SCRIPT": str(ROOT / "verify-recovery-index.py"),
            "PHDEBATE_RECOVERY_KEEP": str(keep),
            "PHDEBATE_RECOVERY_MIN_AGE_HOURS": str(minimum_age),
        },
        text=True,
        capture_output=True,
        check=True,
    )


def test_dry_run_identifies_only_unprotected_old_sets(tmp_path: Path) -> None:
    directory = tmp_path / "runtime" / "deploy-backups"
    old_manifest, old_data = make_set(directory, "old", "old-data-volumes.tar.gz", 96)
    current_manifest, current_data = make_set(directory, "current", "current-data-volumes.tar.gz", 1)

    result = run(tmp_path, "dry-run", keep=1)

    assert old_manifest.exists() and old_data.exists()
    assert current_manifest.exists() and current_data.exists()
    assert "dry-run kind=manifest file=recovery-set-old.manifest" in result.stdout
    assert "dry-run kind=data_volumes file=old-data-volumes.tar.gz" in result.stdout
    assert "preserve kind=manifest file=recovery-set-current.manifest" in result.stdout


def test_apply_keeps_the_configured_rollback_floor_and_shared_data(tmp_path: Path) -> None:
    directory = tmp_path / "runtime" / "deploy-backups"
    obsolete, obsolete_data = make_set(directory, "obsolete", "obsolete-data-volumes.tar.gz", 120)
    rollback, shared_data = make_set(directory, "rollback", "shared-data-volumes.tar.gz", 72)
    current, _ = make_set(directory, "current", "shared-data-volumes.tar.gz", 48)
    private = directory / "private.tar.gz"
    private.write_bytes(b"private")

    run(tmp_path, "apply", keep=2)

    assert not obsolete.exists()
    assert not obsolete_data.exists()
    assert not (directory / "obsolete-data-volumes.tar.gz.sha256").exists()
    assert rollback.exists() and current.exists()
    assert shared_data.exists()
    assert private.exists()


def test_recent_set_is_preserved_even_beyond_keep_count(tmp_path: Path) -> None:
    directory = tmp_path / "runtime" / "deploy-backups"
    older, older_data = make_set(directory, "older", "older-data-volumes.tar.gz", 48)
    recent, recent_data = make_set(directory, "recent", "recent-data-volumes.tar.gz", 2)
    current, current_data = make_set(directory, "current", "current-data-volumes.tar.gz", 1)

    run(tmp_path, "apply", keep=1, minimum_age=24)

    assert not older.exists() and not older_data.exists()
    assert recent.exists() and recent_data.exists()
    assert current.exists() and current_data.exists()


def test_invalid_manifest_and_all_data_are_preserved_without_a_valid_floor(tmp_path: Path) -> None:
    directory = tmp_path / "runtime" / "deploy-backups"
    invalid, data = make_set(directory, "legacy", "legacy-data-volumes.tar.gz", 96, schema="1")

    result = run(tmp_path, "apply", keep=1)

    assert invalid.exists() and data.exists()
    assert "reason=unsafe-manifests-present" in result.stdout


def test_schema_two_recovery_sets_remain_valid_during_single_version_migration(tmp_path: Path) -> None:
    directory = tmp_path / "runtime" / "deploy-backups"
    legacy, legacy_data = make_set(directory, "schema2", "schema2-data-volumes.tar.gz", 48, schema="2")
    current, current_data = make_set(directory, "schema3", "schema3-data-volumes.tar.gz", 1, schema="3")

    result = run(tmp_path, "dry-run", keep=2)

    assert legacy.exists() and legacy_data.exists()
    assert current.exists() and current_data.exists()
    assert "unsafe-manifests-present" not in result.stdout


def test_one_invalid_manifest_stops_pruning_even_with_a_valid_floor(tmp_path: Path) -> None:
    directory = tmp_path / "runtime" / "deploy-backups"
    oldest, oldest_data = make_set(directory, "oldest", "oldest-data-volumes.tar.gz", 120)
    rollback, rollback_data = make_set(directory, "rollback", "rollback-data-volumes.tar.gz", 72)
    current, current_data = make_set(directory, "current", "current-data-volumes.tar.gz", 48)
    invalid, invalid_data = make_set(directory, "unknown", "unknown-data-volumes.tar.gz", 144, schema="1")

    result = run(tmp_path, "apply", keep=2)

    assert "reason=unsafe-manifests-present" in result.stdout
    for artifact in (
        oldest,
        oldest_data,
        rollback,
        rollback_data,
        current,
        current_data,
        invalid,
        invalid_data,
    ):
        assert artifact.exists()


def test_truncated_schema_two_manifest_fails_closed(tmp_path: Path) -> None:
    directory = tmp_path / "runtime" / "deploy-backups"
    current, current_data = make_set(directory, "current", "current-data-volumes.tar.gz", 48)
    truncated = directory / "recovery-set-truncated.manifest"
    truncated.write_text(
        "schema_version=2\n"
        "artifact.data_volumes.filename=truncated-data-volumes.tar.gz\n"
    )
    truncated_data = directory / "truncated-data-volumes.tar.gz"
    truncated_data.write_bytes(b"important")

    result = run(tmp_path, "apply", keep=1)

    assert "reason=unsafe-manifests-present" in result.stdout
    assert current.exists() and current_data.exists()
    assert truncated.exists() and truncated_data.exists()


def test_complete_but_corrupt_recovery_set_stops_all_pruning(tmp_path: Path) -> None:
    directory = tmp_path / "runtime" / "deploy-backups"
    oldest, oldest_data = make_set(directory, "oldest", "oldest-data-volumes.tar.gz", 120)
    rollback, rollback_data = make_set(directory, "rollback", "rollback-data-volumes.tar.gz", 72)
    current, current_data = make_set(directory, "current", "current-data-volumes.tar.gz", 48)
    corrupt, corrupt_data = make_set(directory, "corrupt", "corrupt-data-volumes.tar.gz", 144)
    corrupt_data.write_bytes(b"corrupted after manifest creation")

    result = run(tmp_path, "apply", keep=2)

    assert "reason=artifact-verification-failed" in result.stdout
    assert "reason=unsafe-manifests-present" in result.stdout
    for artifact in (oldest, oldest_data, rollback, rollback_data, current, current_data, corrupt, corrupt_data):
        assert artifact.exists()


def test_recovery_index_hashes_shared_artifacts_only_once(tmp_path: Path) -> None:
    directory = tmp_path / "runtime" / "deploy-backups"
    first, _data = make_set(directory, "shared", "shared-data-volumes.tar.gz", 48)
    second = directory / "recovery-set-shared-copy.manifest"
    second.write_text(first.read_text())

    result = subprocess.run(
        [
            "python3",
            str(ROOT / "verify-recovery-index.py"),
            "--artifact-root", str(directory),
            str(first),
            str(second),
        ],
        text=True,
        capture_output=True,
        check=True,
    )

    assert "manifests=2" in result.stdout
    assert "unique_artifacts=6" in result.stdout
    assert "hashed_files=6" in result.stdout
