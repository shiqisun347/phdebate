from __future__ import annotations

import os
import subprocess
from pathlib import Path


def test_create_recovery_manifest_binds_code_and_private_artifacts(tmp_path: Path) -> None:
    root = tmp_path / "v2"
    agent_root = tmp_path / "agent"
    deploy_backups = root / "runtime" / "deploy-backups"
    database_backups = root / "runtime" / "backups"
    agent_backups = agent_root / "backups"
    api_release = root / "runtime" / "api-releases" / "round8"
    web_release = root / "runtime" / "web-releases" / "round8"
    for directory in (
        deploy_backups,
        database_backups,
        agent_backups,
        api_release,
        web_release,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    (root / ".api-primary").symlink_to(api_release)
    (root / ".web-current").symlink_to(web_release)

    files = {
        database_backups / "auto-20260720.dump": b"v2-db",
        agent_backups / "agent-20260720.dump": b"agent-db",
        deploy_backups / "20260720-data-volumes.tar.gz": b"data",
        deploy_backups / "private.tar.gz": b"private",
        deploy_backups / "voice.tar.gz": b"voice",
        deploy_backups / "moss.tar": b"moss",
    }
    for path, content in files.items():
        path.write_bytes(content)

    output = deploy_backups / "recovery-set-test.manifest"
    env = {
        **os.environ,
        "PHDEBATE_V2_ROOT": str(root),
        "PHDEBATE_AGENT_ROOT": str(agent_root),
        "PHDEBATE_CODE_BRANCH": "backup/production-round8",
        "PHDEBATE_CODE_COMMIT": "a" * 40,
        "PHDEBATE_DEPLOYED_APPLICATION_COMMIT": "b" * 40,
        "PHDEBATE_DATABASE_SCHEMA": "0025_speech_result_pagination",
        "PHDEBATE_RELIABLE_AUDIO_FINGERPRINT": "c" * 64,
        "PHDEBATE_V2_DATABASE_BACKUP": str(database_backups / "auto-20260720.dump"),
        "PHDEBATE_AGENT_DATABASE_BACKUP": str(agent_backups / "agent-20260720.dump"),
        "PHDEBATE_DATA_VOLUME_BACKUP": str(deploy_backups / "20260720-data-volumes.tar.gz"),
        "PHDEBATE_PRIVATE_CONFIG_BACKUP": str(deploy_backups / "private.tar.gz"),
        "PHDEBATE_RELIABLE_VOICE_BACKUP": str(deploy_backups / "voice.tar.gz"),
        "PHDEBATE_MOSS_OFFLINE_BACKUP": str(deploy_backups / "moss.tar"),
    }
    script = Path(__file__).parents[1] / "create-recovery-manifest.sh"
    result = subprocess.run(
        ["bash", str(script), str(output)],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

    manifest = output.read_text()
    assert "code_branch=backup/production-round8" in manifest
    assert f"code_commit={'a' * 40}" in manifest
    assert f"deployed_application_commit={'b' * 40}" in manifest
    assert "api_release=round8" in manifest
    assert "web_release=round8" in manifest
    assert "schema_version=2" in manifest
    assert "artifact.v2_database.filename=auto-20260720.dump" in manifest
    assert "artifact.agent_database.filename=agent-20260720.dump" in manifest
    assert len([line for line in manifest.splitlines() if line.endswith(".sha256=")]) == 0
    assert len([line for line in manifest.splitlines() if ".sha256=" in line]) == 6
    assert not any(str(tmp_path) in line for line in manifest.splitlines())
    assert "recovery_manifest_ready" in result.stdout


def test_verify_recovery_manifest_is_portable(tmp_path: Path) -> None:
    # Reuse the generator test setup through an independent compact fixture so
    # the verifier proves artifacts can move away from source-server paths.
    source = tmp_path / "source"
    destination = tmp_path / "new-server"
    root = source / "v2"
    agent_root = source / "agent"
    deploy_backups = root / "runtime" / "deploy-backups"
    database_backups = root / "runtime" / "backups"
    agent_backups = agent_root / "backups"
    for directory in (deploy_backups, database_backups, agent_backups, destination):
        directory.mkdir(parents=True, exist_ok=True)
    (root / "runtime/api-releases/r1").mkdir(parents=True)
    (root / "runtime/web-releases/r1").mkdir(parents=True)
    (root / ".api-primary").symlink_to(root / "runtime/api-releases/r1")
    (root / ".web-current").symlink_to(root / "runtime/web-releases/r1")
    artifacts = {
        database_backups / "v2.dump": b"v2",
        agent_backups / "agent.dump": b"agent",
        deploy_backups / "data.tar.gz": b"data",
        deploy_backups / "private.tar.gz": b"private",
        deploy_backups / "voice.tar.gz": b"voice",
        deploy_backups / "moss.tar": b"moss",
    }
    for path, content in artifacts.items():
        path.write_bytes(content)
    output = source / "recovery.manifest"
    env = {
        **os.environ,
        "PHDEBATE_V2_ROOT": str(root),
        "PHDEBATE_AGENT_ROOT": str(agent_root),
        "PHDEBATE_CODE_BRANCH": "backup/test",
        "PHDEBATE_CODE_COMMIT": "a" * 40,
        "PHDEBATE_DEPLOYED_APPLICATION_COMMIT": "b" * 40,
        "PHDEBATE_DATABASE_SCHEMA": "head",
        "PHDEBATE_RELIABLE_AUDIO_FINGERPRINT": "c" * 64,
        "PHDEBATE_V2_DATABASE_BACKUP": str(database_backups / "v2.dump"),
        "PHDEBATE_AGENT_DATABASE_BACKUP": str(agent_backups / "agent.dump"),
        "PHDEBATE_DATA_VOLUME_BACKUP": str(deploy_backups / "data.tar.gz"),
        "PHDEBATE_PRIVATE_CONFIG_BACKUP": str(deploy_backups / "private.tar.gz"),
        "PHDEBATE_RELIABLE_VOICE_BACKUP": str(deploy_backups / "voice.tar.gz"),
        "PHDEBATE_MOSS_OFFLINE_BACKUP": str(deploy_backups / "moss.tar"),
    }
    deploy = Path(__file__).parents[1]
    subprocess.run(["bash", str(deploy / "create-recovery-manifest.sh"), str(output)], check=True, env=env)
    for artifact in artifacts:
        (destination / artifact.name).write_bytes(artifact.read_bytes())

    result = subprocess.run(
        [
            "bash",
            str(deploy / "verify-recovery-manifest.sh"),
            str(output),
            str(destination),
            str(destination),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "recovery_manifest_verified artifacts=6" in result.stdout
