from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

DEPLOY = Path(__file__).resolve().parents[1]
SCRIPT = DEPLOY / "prune-source-archives.sh"


def make_archive(directory: Path, name: str, age_hours: int, size: int = 16) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(b"x" * size)
    timestamp = time.time() - age_hours * 3600
    os.utime(path, (timestamp, timestamp))
    return path


def run(root: Path, mode: str, *, allow: bool = False) -> subprocess.CompletedProcess[str]:
    env = os.environ | {
        "PHDEBATE_ROOT": str(root),
        "PHDEBATE_SOURCE_ARCHIVE_KEEP": "1",
        "PHDEBATE_SOURCE_ARCHIVE_MIN_AGE_HOURS": "24",
    }
    if allow:
        env["PHDEBATE_ALLOW_SOURCE_ARCHIVE_PRUNE"] = "yes"
    return subprocess.run(["bash", str(SCRIPT), mode], env=env, text=True, capture_output=True)


def test_dry_run_reports_reclaimable_bytes_without_deleting(tmp_path: Path) -> None:
    directory = tmp_path / "runtime" / "deploy-backups"
    old = make_archive(directory, "old-source.tar.gz", 96, 20)
    newest = make_archive(directory, "new-source.tar.gz", 72, 10)

    result = run(tmp_path, "dry-run")

    assert result.returncode == 0
    assert old.exists() and newest.exists()
    assert "dry-run kind=source_archive file=old-source.tar.gz" in result.stdout
    assert "bytes=20" in result.stdout


def test_apply_requires_explicit_operator_confirmation(tmp_path: Path) -> None:
    directory = tmp_path / "runtime" / "deploy-backups"
    old = make_archive(directory, "old-source.tar.gz", 96)
    make_archive(directory, "new-source.tar.gz", 72)

    result = run(tmp_path, "apply")

    assert result.returncode == 2
    assert old.exists()
    assert "requires PHDEBATE_ALLOW_SOURCE_ARCHIVE_PRUNE=yes" in result.stderr


def test_apply_keeps_newest_and_manifest_referenced_archives(tmp_path: Path) -> None:
    directory = tmp_path / "runtime" / "deploy-backups"
    obsolete = make_archive(directory, "obsolete-source.tar.gz", 120)
    referenced = make_archive(directory, "referenced-source.tar.gz", 110)
    newest = make_archive(directory, "newest-source.tar.gz", 100)
    manifest = directory / "recovery-set-current.manifest"
    manifest.write_text("artifact.private_config.filename=referenced-source.tar.gz\n")

    result = run(tmp_path, "apply", allow=True)

    assert result.returncode == 0
    assert not obsolete.exists()
    assert referenced.exists() and newest.exists()
    assert "reason=recovery-manifest-reference" in result.stdout


def test_unrelated_voice_and_data_archives_are_never_candidates(tmp_path: Path) -> None:
    directory = tmp_path / "runtime" / "deploy-backups"
    make_archive(directory, "old-source.tar.gz", 96)
    voice = make_archive(directory, "reliable-voice.tar.gz", 200)
    data = make_archive(directory, "old-data-volumes.tar.gz", 200)
    moss = make_archive(directory, "openmoss-offline.tar", 200)

    result = run(tmp_path, "apply", allow=True)

    assert result.returncode == 0
    assert voice.exists() and data.exists() and moss.exists()
