from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "prune-releases.sh"


def make_release(directory: Path, name: str, age_hours: int) -> Path:
    release = directory / name
    release.mkdir(parents=True)
    (release / ".release-complete").write_text(name)
    timestamp = time.time() - age_hours * 3600
    os.utime(release, (timestamp, timestamp))
    return release


def make_legacy_web_release(directory: Path, name: str, age_hours: int) -> Path:
    release = directory / name
    (release / ".next" / "static").mkdir(parents=True)
    (release / "server.js").write_text("// standalone release\n")
    timestamp = time.time() - age_hours * 3600
    os.utime(release, (timestamp, timestamp))
    return release


def run(root: Path, mode: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SCRIPT), mode],
        env=os.environ
        | {
            "PHDEBATE_ROOT": str(root),
            "PHDEBATE_RELEASE_KEEP": "2",
            "PHDEBATE_RELEASE_MIN_AGE_HOURS": "24",
        },
        text=True,
        capture_output=True,
        check=True,
    )


def test_dry_run_keeps_every_release_and_identifies_old_candidates(tmp_path: Path) -> None:
    releases = tmp_path / "runtime" / "web-releases"
    old = make_release(releases, "old", 72)
    make_release(releases, "middle", 48)
    current = make_release(releases, "current", 1)
    (tmp_path / ".web-current").symlink_to(current)

    result = run(tmp_path, "dry-run")

    assert old.exists()
    assert "dry-run kind=web release=old" in result.stdout
    assert "preserve kind=web release=current" in result.stdout


def test_apply_never_removes_current_or_recent_rollback_releases(tmp_path: Path) -> None:
    releases = tmp_path / "runtime" / "api-releases"
    oldest = make_release(releases, "oldest", 96)
    protected = make_release(releases, "protected", 72)
    recent_rollback = make_release(releases, "rollback", 48)
    newest = make_release(releases, "newest", 1)
    (tmp_path / ".api-primary").symlink_to(protected)
    (tmp_path / ".api-secondary").symlink_to(newest)

    result = run(tmp_path, "apply")

    assert not oldest.exists()
    assert protected.exists()
    assert recent_rollback.exists()
    assert newest.exists()
    assert "apply kind=api release=oldest" in result.stdout


def test_incomplete_directory_is_never_deleted(tmp_path: Path) -> None:
    releases = tmp_path / "runtime" / "web-releases"
    incomplete = releases / "manual-investigation"
    incomplete.mkdir(parents=True)
    timestamp = time.time() - 96 * 3600
    os.utime(incomplete, (timestamp, timestamp))
    make_release(releases, "newest", 1)
    make_release(releases, "second", 2)

    result = run(tmp_path, "apply")

    assert incomplete.exists()
    assert "reason=incomplete-marker-missing" in result.stdout


def test_incomplete_directory_does_not_consume_a_rollback_slot(tmp_path: Path) -> None:
    releases = tmp_path / "runtime" / "api-releases"
    incomplete = releases / "newest-incomplete"
    incomplete.mkdir(parents=True)
    make_release(releases, "newest-complete", 48)
    rollback = make_release(releases, "rollback", 72)
    obsolete = make_release(releases, "obsolete", 96)

    run(tmp_path, "apply")

    assert incomplete.exists()
    assert rollback.exists()
    assert not obsolete.exists()


def test_legacy_standalone_web_release_can_be_pruned_without_marker(tmp_path: Path) -> None:
    releases = tmp_path / "runtime" / "web-releases"
    legacy = make_legacy_web_release(releases, "legacy", 96)
    make_release(releases, "newest", 1)
    make_release(releases, "second", 2)

    run(tmp_path, "apply")

    assert not legacy.exists()
