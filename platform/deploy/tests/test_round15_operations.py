from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

DEPLOY = Path(__file__).resolve().parents[1]


def run(*args: str, env: dict[str, str] | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, env=env, text=True, capture_output=True, check=check)


def test_backup_status_preserves_last_success_while_exposing_a_failed_attempt(tmp_path: Path) -> None:
    status = tmp_path / "backup-status.json"
    artifact = tmp_path / "database.dump"
    artifact.write_bytes(b"verified database fixture")
    script = DEPLOY / "backup_status.py"

    run(sys.executable, str(script), "--status-file", str(status), "--state", "succeeded", "--artifact", str(artifact))
    succeeded = json.loads(status.read_text())
    run(
        sys.executable,
        str(script),
        "--status-file",
        str(status),
        "--state",
        "failed",
        "--error-code",
        "backup_command_failed",
    )
    failed = json.loads(status.read_text())

    assert succeeded["sha256"] == hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert failed["state"] == "failed" and failed["error_code"] == "backup_command_failed"
    assert failed["last_success_at"] == succeeded["last_success_at"]
    assert failed["sha256"] == succeeded["sha256"]
    assert "password" not in status.read_text().lower()


def initialise_git_source(path: Path) -> tuple[str, str]:
    path.mkdir()
    (path / "tracked.txt").write_text("release source\n")
    run("git", "init", "-q", str(path))
    run("git", "-C", str(path), "config", "user.email", "test@example.invalid")
    run("git", "-C", str(path), "config", "user.name", "test")
    run("git", "-C", str(path), "add", "tracked.txt")
    run("git", "-C", str(path), "commit", "-qm", "fixture")
    commit = run("git", "-C", str(path), "rev-parse", "HEAD").stdout.strip()
    tree = run("git", "-C", str(path), "rev-parse", "HEAD^{tree}").stdout.strip()
    return commit, tree


def test_release_provenance_is_bound_to_the_exact_commit_tree(tmp_path: Path) -> None:
    source = tmp_path / "source"
    release = tmp_path / "release-one"
    release.mkdir()
    commit, tree = initialise_git_source(source)
    record = release / ".release-provenance.json"
    run(
        sys.executable,
        str(DEPLOY / "write-release-provenance.py"),
        "--kind", "api", "--release", release.name,
        "--commit", commit, "--tree", tree, "--output", str(record),
    )
    verified = run(
        sys.executable,
        str(DEPLOY / "verify-release-provenance.py"),
        "--kind", "api", "--release-dir", str(release), "--source-checkout", str(source),
    )
    assert "release_provenance_ok" in verified.stdout

    payload = json.loads(record.read_text())
    payload["source_tree"] = "f" * 40
    record.write_text(json.dumps(payload))
    rejected = run(
        sys.executable,
        str(DEPLOY / "verify-release-provenance.py"),
        "--kind", "api", "--release-dir", str(release), "--source-checkout", str(source),
        check=False,
    )
    assert rejected.returncode != 0 and "tree does not belong to commit" in rejected.stderr


def prepare_web_root(tmp_path: Path) -> tuple[Path, Path, Path, str, str]:
    root = tmp_path / "live"
    runtime = root / "runtime"
    releases = runtime / "web-releases"
    bin_dir = tmp_path / "bin"
    releases.mkdir(parents=True)
    bin_dir.mkdir()
    (root / ".venv" / "bin").mkdir(parents=True)
    (root / ".venv" / "bin" / "python").symlink_to(sys.executable)
    commit, tree = "a" * 40, "b" * 40
    (runtime / "source-commit").write_text(commit)
    (runtime / "source-tree").write_text(tree)
    for name in ("old", "new"):
        release = releases / name
        (release / "public").mkdir(parents=True)
        (release / "server.js").write_text("// server")
        (release / ".release-complete").write_text(name)
        record = {
            "schema_version": 1,
            "service": "phdebate-web",
            "release": name,
            "source_commit": commit,
            "source_tree": tree,
            "base_path": "",
        }
        serialized = json.dumps(record)
        (release / ".release-provenance.json").write_text(serialized)
        (release / "public" / "release.json").write_text(serialized)
    (root / ".web-current").symlink_to(releases / "old")
    (bin_dir / "supervisorctl").write_text("#!/bin/sh\necho \"$@\" >>\"$SUPERVISOR_LOG\"\n")
    (bin_dir / "flock").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "curl").write_text(
        "#!/bin/sh\n"
        "url=''\nfor value in \"$@\"; do url=$value; done\n"
        "case \"$url\" in\n"
        "  *public.invalid*deployment=new*) [ \"${FAIL_PUBLIC_NEW:-0}\" = 1 ] && exit 22 ;;\n"
        "esac\n"
        "case \"$url\" in\n"
        "  *release.json*deployment=new*) cat \"$WEB_ROOT/runtime/web-releases/new/public/release.json\" ;;\n"
        "  *release.json*deployment=old*) cat \"$WEB_ROOT/runtime/web-releases/old/public/release.json\" ;;\n"
        "  *) printf '<html>healthy</html>' ;;\n"
        "esac\n"
    )
    os.chmod(bin_dir / "supervisorctl", 0o755)
    os.chmod(bin_dir / "curl", 0o755)
    os.chmod(bin_dir / "flock", 0o755)
    return root, releases / "old", releases / "new", str(bin_dir), commit


def web_env(root: Path, bin_dir: str, log: Path, **extra: str) -> dict[str, str]:
    return os.environ | {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "PHDEBATE_ROOT": str(root),
        "PHDEBATE_WEB_DIRECT_ORIGIN": "http://direct.invalid",
        "PHDEBATE_PUBLIC_ORIGIN": "https://public.invalid",
        "PHDEBATE_WEB_HEALTH_RETRIES": "1",
        "SUPERVISOR_LOG": str(log),
        "WEB_ROOT": str(root),
    } | extra


def test_web_activation_switches_atomically_after_direct_and_public_health_gates(tmp_path: Path) -> None:
    root, _old, new, bin_dir, _commit = prepare_web_root(tmp_path)
    log = tmp_path / "supervisor.log"
    result = run("bash", str(DEPLOY / "activate-web-release.sh"), "new", env=web_env(root, bin_dir, log))
    assert result.returncode == 0
    assert (root / ".web-current").resolve() == new.resolve()
    assert "web_rollout_complete release=new previous=old" in result.stdout


def test_web_activation_automatically_rolls_back_when_public_gate_fails(tmp_path: Path) -> None:
    root, old, _new, bin_dir, _commit = prepare_web_root(tmp_path)
    log = tmp_path / "supervisor.log"
    result = run(
        "bash", str(DEPLOY / "activate-web-release.sh"), "new",
        env=web_env(root, bin_dir, log, FAIL_PUBLIC_NEW="1"), check=False,
    )
    assert result.returncode != 0
    assert (root / ".web-current").resolve() == old.resolve()
    assert "restoring release=old" in result.stderr
    assert log.read_text().count("restart") == 2


def test_release_builds_fail_closed_without_source_provenance() -> None:
    for script_name in ("build-api-release.sh", "build-web-release.sh"):
        script = (DEPLOY / script_name).read_text()
        assert 'test -s "$SOURCE_COMMIT_FILE"' in script
        assert 'test -s "$SOURCE_TREE_FILE"' in script
        assert ".release-provenance.json" in script
        assert 'chown "$SERVICE_USER:$SERVICE_GROUP" "$RELEASE_DIR/.release-provenance.json"' in script


def test_backup_scripts_publish_failure_state_and_agent_loop_retries() -> None:
    database = (DEPLOY / "backup-database.sh").read_text()
    agent_root = DEPLOY.parents[1] / "debate-agent" / "deploy"
    agent = (agent_root / "backup.same-host.sh").read_text()
    loop = (agent_root / "backup-loop.same-host.sh").read_text()
    for script in (database, agent):
        assert "--state running" in script
        assert "--state failed" in script
        assert "--state succeeded" in script
    assert "DEBATE_AGENT_BACKUP_RETRY_SECONDS" in loop
