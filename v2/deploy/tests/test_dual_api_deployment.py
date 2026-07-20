from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(name: str) -> str:
    return (ROOT / name).read_text()


def test_supervisor_workers_have_unique_ports_and_safe_process_roles() -> None:
    config = read("phdebate-v2.supervisor.conf")
    primary = config.split("[program:jixia-v2-api]", 1)[1].split("[program:jixia-v2-api-secondary]", 1)[0]
    secondary = config.split("[program:jixia-v2-api-secondary]", 1)[1].split("[program:jixia-v2-engine]", 1)[0]
    engine = config.split("[program:jixia-v2-engine]", 1)[1].split("[program:jixia-v2-worker]", 1)[0]

    assert "--port 12340" in primary and "API_INSTANCE_ID=api-primary" in primary
    assert "--port 12342" in secondary and "API_INSTANCE_ID=api-secondary" in secondary
    for worker in (primary, secondary):
        assert "ENGINE_ENABLED=false" in worker
        assert "PRESENCE_RESET_ON_STARTUP=false" in worker
        assert "MEDIA_ROOT=/home/ubuntu/sunsq/phdebate-v2/storage/audio" in worker
        assert "ARCHIVE_ROOT=/home/ubuntu/sunsq/phdebate-v2/storage/archives" in worker
    assert "ENGINE_ENABLED=true" in engine
    assert "PRESENCE_RESET_ON_STARTUP=false" in engine


def test_nginx_balances_rest_and_state_ws_but_not_audio_or_asr() -> None:
    config = read("jixia-nginx-v2-root.conf")
    assert "worker_rlimit_nofile 65535;" in config
    assert "worker_connections 8192;" in config
    assert config.count("backlog=4096") == 2
    assert config.count("reuseport") >= 2
    assert "multi_accept on" not in config
    assert "upstream jixia_v2_api" in config
    assert "server 127.0.0.1:12340" in config
    assert "server 127.0.0.1:12342" in config
    assert config.count("max_fails=0") == 2
    assert "proxy_pass http://jixia_v2_api/api/" in config
    assert 'location ~ "^/ws/rooms/(?<room_code>[0-9]{6})/?$"' in config
    assert 'location ~ "^/v2/ws/rooms/(?<v2_room_code>[0-9]{6})/?$"' in config
    # The nested /audio and /asr endpoints fall through to this primary-only
    # prefix, preserving the reliable voice transport.
    assert "location /ws/" in config
    assert "location /v2/ws/" in config
    assert config.count("proxy_pass http://127.0.0.1:12340/ws/;") == 2


def test_nginx_supervisor_uses_the_tracked_production_config() -> None:
    config = read("jixia-nginx.supervisor.conf")
    assert "deploy/jixia-nginx-v2-root.conf" in config
    assert "nginx-new-server.conf" not in config
    assert "autostart=true" in config
    assert "autorestart=true" in config


def test_production_moss_gateway_recovers_after_a_host_reboot() -> None:
    config = read("moss-production.supervisor.conf")
    assert "autostart=true" in config
    assert "autorestart=unexpected" in config
    assert ".venv-cu128/bin/python" in config
    assert "MOSS_GATEWAY_ASYNC_DECODER_ENABLED=\"false\"" in config
    assert "run-moss-production.sh" in config
    assert "GPU-042703fe" not in config
    launcher = read("run-moss-production.sh")
    assert 'EXPECTED_NAME="NVIDIA GeForce RTX 3090"' in launcher
    assert 'EXPECTED_MEMORY_MIB="24576"' in launcher
    assert 'EXPECTED_VBIOS="94.02.26.88.08"' in launcher
    assert "gpu_fingerprint.py" in launcher


def test_rollout_updates_secondary_first_and_has_automatic_rollback() -> None:
    script = read("roll-api-workers.sh")
    assert 'local link="$1"\n  local target="$2"\n  local temp="${link}.new.$$"' in script
    rollout = script.split("trap rollback ERR", 1)[1]
    secondary_restart = rollout.index('supervisorctl restart "$SECONDARY_SERVICE"')
    primary_restart = rollout.index('supervisorctl restart "$PRIMARY_SERVICE"')
    assert secondary_restart < primary_restart
    assert "trap rollback ERR" in script
    assert 'wait_healthy "$SECONDARY_URL" api-secondary' in script
    assert 'wait_healthy "$PRIMARY_URL" api-primary' in script
    assert "wait_public_healthy" in script


def test_api_release_excludes_runtime_caches_and_validates_import() -> None:
    script = read("build-api-release.sh")
    assert "--exclude '__pycache__/'" in script
    assert "--exclude '.pytest_cache/'" in script
    assert "from app.main import app" in script
    assert ".release-complete" in script
    assert 'chown -R "$SERVICE_USER:$SERVICE_GROUP" "$RELEASE_DIR"' in script


def test_web_release_is_marked_complete_only_after_runtime_assets_exist() -> None:
    script = read("build-web-release.sh")
    runtime_check = script.index('test -f "$RELEASE_DIR/public/worklets/livekit-interrupt-gate.js"')
    marker = script.index('>"$RELEASE_DIR/.release-complete"')
    assert runtime_check < marker


def test_backup_scripts_keep_runtime_data_and_voice_recovery_separate() -> None:
    data = read("backup-data-volumes.sh")
    voice = read("capture-reliable-voice-runtime.sh")
    assert "storage assets/moss-prompts backups/reliable-audio-20260719-declick" in data
    assert "files.sha256" in data and "chmod 600" in data
    assert "openmoss-files.sha256" in voice
    assert "pip freeze --all" in voice
    assert '"model_files_packaged": False' in voice
    assert "reliable-audio-baseline.json" in voice
