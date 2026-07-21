from __future__ import annotations

import hashlib
import os
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "prune-recovery-sets.sh"
QUARANTINE_SCRIPT = ROOT / "quarantine-recovery-manifests.py"
UPGRADE_SCRIPT = ROOT / "upgrade-schema1-recovery-manifests.py"


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


def make_legacy_set(directory: Path, name: str, age_hours: int) -> tuple[Path, dict[str, Path]]:
    directory.mkdir(parents=True, exist_ok=True)
    artifacts = {
        "platform_database": directory / f"auto-{name}.dump",
        "agent_database": directory / f"agent-{name}.dump",
        "data_volumes": directory / f"{name}-data-volumes.tar.gz",
        "private_config": directory / f"{name}-full-private-config.tar.gz",
        "reliable_voice": directory / f"{name}-reliable-voice-runtime.tar.gz",
        "moss_offline": directory / f"{name}-openmoss-offline.tar",
    }
    for role, artifact in artifacts.items():
        artifact.write_bytes(f"legacy:{role}:{name}".encode())
    legacy_paths = {
        "platform_database": f"/retired/phdebate-v2/runtime/backups/{artifacts['platform_database'].name}",
        "agent_database": f"/retired/debate-agent/backups/{artifacts['agent_database'].name}",
        **{
            role: f"/retired/phdebate-v2/runtime/deploy-backups/{artifact.name}"
            for role, artifact in artifacts.items()
            if role not in {"platform_database", "agent_database"}
        },
    }
    manifest = directory / f"recovery-set-{name}.manifest"
    lines = [
        "schema_version=1",
        "created_at=2026-07-19T10:15:00Z",
        f"code_branch=backup/{name}",
        f"code_commit={hashlib.sha1(name.encode()).hexdigest()}",
        f"api_release={name}",
        f"web_release={name}",
    ]
    for role in (
        "platform_database",
        "agent_database",
        "data_volumes",
        "private_config",
        "reliable_voice",
        "moss_offline",
    ):
        artifact = artifacts[role]
        lines.append(f"{hashlib.sha256(artifact.read_bytes()).hexdigest()}  {legacy_paths[role]}")
    manifest.write_text("\n".join(lines) + "\n")
    timestamp = time.time() - age_hours * 3600
    os.utime(manifest, (timestamp, timestamp))
    for artifact in artifacts.values():
        os.utime(artifact, (timestamp, timestamp))
    return manifest, artifacts


def upgrade_schema1(
    root: Path,
    mode: str = "write",
    *,
    report: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    directory = root / "runtime" / "deploy-backups"
    command = [
        "python3",
        str(UPGRADE_SCRIPT),
        mode,
        "--backup-dir",
        str(directory),
        "--artifact-root",
        str(directory),
    ]
    if report is not None:
        command.extend(("--report", str(report)))
    env = os.environ | {"PHDEBATE_ALLOW_SCHEMA1_MANIFEST_UPGRADE": "yes"}
    return subprocess.run(command, env=env, text=True, capture_output=True, check=True)


def run(root: Path, mode: str, *, keep: int = 2, minimum_age: int = 24) -> subprocess.CompletedProcess[str]:
    env = os.environ | {
        "PHDEBATE_ROOT": str(root),
        "PHDEBATE_AGENT_ROOT": str(root / "agent"),
        "PHDEBATE_DEPLOY_BACKUP_DIR": str(root / "runtime" / "deploy-backups"),
        "PHDEBATE_BACKUP_DIR": str(root / "runtime" / "deploy-backups"),
        "PHDEBATE_AGENT_BACKUP_DIR": str(root / "runtime" / "deploy-backups"),
        "PHDEBATE_RECOVERY_INDEX_VERIFY_SCRIPT": str(ROOT / "verify-recovery-index.py"),
        "PHDEBATE_RECOVERY_MANIFEST_UPGRADE_DIR": str(
            root / "runtime" / "deploy-backups" / "manifest-upgrades"
        ),
        "PHDEBATE_RECOVERY_KEEP": str(keep),
        "PHDEBATE_RECOVERY_MIN_AGE_HOURS": str(minimum_age),
    }
    if mode == "apply":
        env["PHDEBATE_ALLOW_RECOVERY_PRUNE"] = "yes"
    return subprocess.run(
        ["bash", str(SCRIPT), mode],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )


def test_recovery_prune_apply_requires_explicit_operator_acknowledgement(tmp_path: Path) -> None:
    directory = tmp_path / "runtime" / "deploy-backups"
    make_set(directory, "current", "current-data-volumes.tar.gz", 48)
    env = os.environ | {
        "PHDEBATE_ROOT": str(tmp_path),
        "PHDEBATE_DEPLOY_BACKUP_DIR": str(directory),
    }
    env.pop("PHDEBATE_ALLOW_RECOVERY_PRUNE", None)

    result = subprocess.run(
        ["bash", str(SCRIPT), "apply"],
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 2
    assert "requires PHDEBATE_ALLOW_RECOVERY_PRUNE=yes" in result.stderr


def quarantine(root: Path, mode: str = "apply") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", str(QUARANTINE_SCRIPT), mode, "--backup-dir", str(root / "runtime" / "deploy-backups")],
        env=os.environ | {"PHDEBATE_ALLOW_RECOVERY_MANIFEST_QUARANTINE": "yes"},
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


def test_schema1_upgrade_audit_is_read_only_and_reports_required_companions(tmp_path: Path) -> None:
    directory = tmp_path / "runtime" / "deploy-backups"
    manifest, artifacts = make_legacy_set(directory, "legacy-audit", 96)
    original_manifest = manifest.read_bytes()
    original_stats = {path: path.stat() for path in (manifest, *artifacts.values())}

    result = upgrade_schema1(tmp_path, mode="audit")

    assert "status=write-required" in result.stdout
    assert "manifests=1 written=0 existing=0" in result.stdout
    assert not (directory / "manifest-upgrades").exists()
    assert manifest.read_bytes() == original_manifest
    for path, before in original_stats.items():
        after = path.stat()
        assert (after.st_size, after.st_mtime_ns, after.st_ino) == (before.st_size, before.st_mtime_ns, before.st_ino)


def test_schema1_upgrade_writes_only_new_verified_companion_and_report(tmp_path: Path) -> None:
    directory = tmp_path / "runtime" / "deploy-backups"
    manifest, artifacts = make_legacy_set(directory, "legacy-write", 96)
    original_manifest = manifest.read_bytes()
    original_hashes = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (manifest, *artifacts.values())}
    report = tmp_path / "schema1-upgrade-report.json"

    result = upgrade_schema1(tmp_path, report=report)

    companion = directory / "manifest-upgrades" / f"{manifest.name}.schema3"
    assert "manifests=1 written=1 existing=0" in result.stdout
    assert companion.is_file() and companion.stat().st_mode & 0o777 == 0o600
    assert "schema_version=3\n" in companion.read_text()
    assert f"legacy_source.filename={manifest.name}\n" in companion.read_text()
    assert f"artifact.data_volumes.filename={artifacts['data_volumes'].name}\n" in companion.read_text()
    assert report.is_file() and report.stat().st_mode & 0o777 == 0o600
    assert manifest.read_bytes() == original_manifest
    assert {
        path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (manifest, *artifacts.values())
    } == original_hashes

    repeated = upgrade_schema1(tmp_path)
    assert "manifests=1 written=0 existing=1" in repeated.stdout


def test_schema1_upgrade_write_requires_explicit_operator_acknowledgement(tmp_path: Path) -> None:
    directory = tmp_path / "runtime" / "deploy-backups"
    make_legacy_set(directory, "legacy-ack", 96)
    env = {key: value for key, value in os.environ.items() if key != "PHDEBATE_ALLOW_SCHEMA1_MANIFEST_UPGRADE"}

    result = subprocess.run(
        [
            "python3",
            str(UPGRADE_SCRIPT),
            "write",
            "--backup-dir",
            str(directory),
            "--artifact-root",
            str(directory),
        ],
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert "requires PHDEBATE_ALLOW_SCHEMA1_MANIFEST_UPGRADE=yes" in result.stderr
    assert not (directory / "manifest-upgrades").exists()


def test_schema1_upgrade_fails_atomically_when_any_artifact_is_missing(tmp_path: Path) -> None:
    directory = tmp_path / "runtime" / "deploy-backups"
    first, _ = make_legacy_set(directory, "legacy-first", 96)
    _broken, broken_artifacts = make_legacy_set(directory, "legacy-broken", 96)
    broken_artifacts["agent_database"].unlink()

    result = subprocess.run(
        [
            "python3",
            str(UPGRADE_SCRIPT),
            "write",
            "--backup-dir",
            str(directory),
            "--artifact-root",
            str(directory),
        ],
        env=os.environ | {"PHDEBATE_ALLOW_SCHEMA1_MANIFEST_UPGRADE": "yes"},
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert "expected exactly one approved artifact" in result.stderr
    assert first.exists()
    assert not (directory / "manifest-upgrades").exists()


def test_schema1_upgrade_refuses_to_overwrite_an_existing_report_before_writing(tmp_path: Path) -> None:
    directory = tmp_path / "runtime" / "deploy-backups"
    make_legacy_set(directory, "legacy-report", 96)
    report = tmp_path / "existing-report.json"
    report.write_text("operator-owned\n")

    result = subprocess.run(
        [
            "python3",
            str(UPGRADE_SCRIPT),
            "write",
            "--backup-dir",
            str(directory),
            "--artifact-root",
            str(directory),
            "--report",
            str(report),
        ],
        env=os.environ | {"PHDEBATE_ALLOW_SCHEMA1_MANIFEST_UPGRADE": "yes"},
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert "report destination already exists" in result.stderr
    assert report.read_text() == "operator-owned\n"
    assert not (directory / "manifest-upgrades").exists()


def test_verified_schema1_companion_unblocks_prune_but_keeps_legacy_recovery_set(tmp_path: Path) -> None:
    directory = tmp_path / "runtime" / "deploy-backups"
    legacy, legacy_artifacts = make_legacy_set(directory, "legacy-protected", 144)
    obsolete, obsolete_data = make_set(directory, "obsolete", "obsolete-data-volumes.tar.gz", 120)
    current, current_data = make_set(directory, "current", "current-data-volumes.tar.gz", 48)
    upgrade_schema1(tmp_path)

    result = run(tmp_path, "apply", keep=1)

    assert "reason=verified-schema1-upgrade" in result.stdout
    assert "reason=unsafe-manifests-present" not in result.stdout
    assert legacy.exists() and all(path.exists() for path in legacy_artifacts.values())
    assert not obsolete.exists() and not obsolete_data.exists()
    assert current.exists() and current_data.exists()


def test_schema1_companion_fails_closed_after_source_manifest_changes(tmp_path: Path) -> None:
    directory = tmp_path / "runtime" / "deploy-backups"
    legacy, legacy_artifacts = make_legacy_set(directory, "legacy-tampered", 144)
    current, current_data = make_set(directory, "current", "current-data-volumes.tar.gz", 48)
    upgrade_schema1(tmp_path)
    legacy.write_text(legacy.read_text() + "# unexpected mutation\n")

    result = run(tmp_path, "apply", keep=1)

    assert "reason=missing-or-invalid-schema1-upgrade" in result.stdout
    assert "reason=unsafe-manifests-present" in result.stdout
    assert legacy.exists() and all(path.exists() for path in legacy_artifacts.values())
    assert current.exists() and current_data.exists()


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


def test_explicit_quarantine_preserves_old_data_and_unblocks_verified_pruning(tmp_path: Path) -> None:
    directory = tmp_path / "runtime" / "deploy-backups"
    invalid, invalid_data = make_set(directory, "early", "early-data-volumes.tar.gz", 144, schema="1")
    obsolete, obsolete_data = make_set(directory, "obsolete", "obsolete-data-volumes.tar.gz", 120)
    current, current_data = make_set(directory, "current", "current-data-volumes.tar.gz", 48)

    quarantined = quarantine(tmp_path)

    assert "candidates=1 refused=0" in quarantined.stdout
    assert not invalid.exists()
    isolated = directory / "quarantine" / invalid.name
    assert isolated.exists()
    assert (directory / "quarantine" / f"{invalid.name}.quarantine.json").exists()

    result = run(tmp_path, "apply", keep=1)

    assert "reason=unsafe-manifests-present" not in result.stdout
    assert f"protects={invalid_data.name}" in result.stdout
    assert invalid_data.exists()
    assert not obsolete.exists() and not obsolete_data.exists()
    assert current.exists() and current_data.exists()


def test_quarantine_refuses_an_incomplete_manifest_without_a_data_reference(tmp_path: Path) -> None:
    directory = tmp_path / "runtime" / "deploy-backups"
    directory.mkdir(parents=True)
    incomplete = directory / "recovery-set-no-data.manifest"
    incomplete.write_text("schema_version=2\nartifact.v2_database.filename=old.dump\n")

    result = quarantine(tmp_path)

    assert "candidates=0 refused=1" in result.stdout
    assert "quarantine_refused=missing-safe-data-reference" in result.stdout
    assert incomplete.exists()
    assert not (directory / "quarantine").exists()


def test_quarantine_apply_requires_explicit_operator_acknowledgement(tmp_path: Path) -> None:
    directory = tmp_path / "runtime" / "deploy-backups"
    make_set(directory, "early", "early-data-volumes.tar.gz", 144, schema="1")

    result = subprocess.run(
        ["python3", str(QUARANTINE_SCRIPT), "apply", "--backup-dir", str(directory)],
        env={key: value for key, value in os.environ.items() if key != "PHDEBATE_ALLOW_RECOVERY_MANIFEST_QUARANTINE"},
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert "requires PHDEBATE_ALLOW_RECOVERY_MANIFEST_QUARANTINE=yes" in result.stderr


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
