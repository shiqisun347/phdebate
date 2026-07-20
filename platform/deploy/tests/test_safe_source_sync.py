from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_source_sync_preserves_all_server_owned_paths() -> None:
    script = (ROOT / "sync-github-source.sh").read_text()
    required_excludes = (
        "/.env",
        "/runtime/",
        "/**/runtime/",
        "/storage/",
        "/backups/",
        "/.venv/",
        "/.python-venvs/",
        "/.quality-venv/",
        "/**/.venv*/",
        "/**/__pycache__/",
        "/**/*.pyc",
        "/**/*.egg-info/",
        "/.api-primary",
        "/.api-secondary",
        "/.web-current",
        "/apps/web/node_modules/",
        "/apps/web/.next/",
        "/assets/moss-prompts/audio/",
    )
    for path in required_excludes:
        assert f"--exclude='{path}'" in script


def test_source_sync_requires_a_clean_git_checkout_and_supports_dry_run() -> None:
    script = (ROOT / "sync-github-source.sh").read_text()
    assert '[[ ! -d "$SOURCE/.git" ]]' in script
    assert 'git -C "$SOURCE" rev-parse --verify HEAD' in script
    assert "git -C \"$SOURCE\" diff --quiet" in script
    assert "git -C \"$SOURCE\" diff --cached --quiet" in script
    assert "--dry-run" in script
    assert "-rlp --checksum --delete" in script
    assert "-a --delete" not in script
    assert '"$SOURCE_PLATFORM/" "$DESTINATION/"' in script


def test_source_sync_replaces_code_but_preserves_server_runtime(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source_platform = source / "platform"
    destination = tmp_path / "live"
    (source_platform / "apps").mkdir(parents=True)
    (source_platform / "README.md").write_text("platform\n")
    (source_platform / "apps" / "new.py").write_text("new\n")
    destination.mkdir()
    (destination / "runtime").mkdir()
    (destination / "runtime" / "state.json").write_text("keep\n")
    (destination / "services" / "voice" / ".venv-cu128").mkdir(parents=True)
    (destination / "services" / "voice" / ".venv-cu128" / "python").write_text("keep\n")
    (destination / ".env").write_text("secret\n")
    (destination / ".web-current").symlink_to(destination / "runtime" / "web-release")
    (destination / "obsolete.py").write_text("delete\n")

    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(source), "config", "user.name", "test"], check=True)
    subprocess.run(["git", "-C", str(source), "add", "platform"], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-qm", "fixture"], check=True)

    env = os.environ | {"PHDEBATE_ROOT": str(destination)}
    subprocess.run([str(ROOT / "sync-github-source.sh"), str(source), "apply"], env=env, check=True)

    assert (destination / "apps" / "new.py").read_text() == "new\n"
    assert not (destination / "obsolete.py").exists()
    assert (destination / ".env").read_text() == "secret\n"
    assert (destination / ".web-current").is_symlink()
    assert (destination / "runtime" / "state.json").read_text() == "keep\n"
    assert (destination / "services" / "voice" / ".venv-cu128" / "python").read_text() == "keep\n"
    assert (destination / "runtime" / "source-commit").read_text().strip()
    assert (destination / "runtime" / "source-tree").read_text().strip() == subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD^{tree}"], check=True, capture_output=True, text=True
    ).stdout.strip()
