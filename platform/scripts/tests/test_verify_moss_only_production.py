from __future__ import annotations

from pathlib import Path

import pytest

from scripts.verify_moss_only_production import VerificationError, verify


def write_env(path: Path, **overrides: str) -> None:
    values = {
        "REALTIME_VOICE_PIPELINE_ENABLED": "true",
        "REALTIME_VOICE_BACKEND": "moss_realtime",
        "MOSS_TTS_REALTIME_ENABLED": "true",
        "MOSS_TTS_REALTIME_TRANSPORT": "websocket",
        "MOSS_TTS_STABLE_PLAYBACK_ENABLED": "false",
        "WEBRTC_AUDIO_ENABLED": "true",
        "WEBRTC_AUDIO_BACKEND": "livekit",
        "MATCH_AUDIO_ARCHIVE_ENABLED": "false",
        "HOST_CUES_PRESET_ONLY": "true",
        "LIGHTTTS_STREAMING_ENABLED": "false",
        "LIGHTTTS_BISTREAM_ENABLED": "false",
        "LIGHTTTS_URL": "http://127.0.0.1:8080/inference_zero_shot",
        "MOSS_TTS_REALTIME_API_KEY": "must-not-be-returned",
    }
    values.update(overrides)
    path.write_text("\n".join(f"{key}={value}" for key, value in values.items()) + "\n", encoding="utf-8")


def test_accepts_moss_only_runtime_and_reports_only_legacy_setting_names(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    supervisor_dir = tmp_path / "supervisor"
    supervisor_dir.mkdir()
    write_env(env_file)
    (supervisor_dir / "jixia.conf").write_text(
        "[program:jixia-moss-realtime]\ncommand=/opt/phdebate/run-moss\n",
        encoding="utf-8",
    )

    result = verify(env_file, supervisor_dir)

    assert result["ok"] is True
    assert result["active_voice_path"].startswith("agent_sse -> moss_realtime")
    assert result["dormant_legacy_setting_names"] == ["LIGHTTTS_URL"]
    assert "must-not-be-returned" not in repr(result)


def test_rejects_reenabled_lighttts_streaming(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    supervisor_dir = tmp_path / "supervisor"
    supervisor_dir.mkdir()
    write_env(env_file, LIGHTTTS_STREAMING_ENABLED="true")

    with pytest.raises(VerificationError, match="LIGHTTTS_STREAMING_ENABLED"):
        verify(env_file, supervisor_dir)


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("MOSS_TTS_STABLE_PLAYBACK_ENABLED", "true"),
        ("MATCH_AUDIO_ARCHIVE_ENABLED", "true"),
        ("HOST_CUES_PRESET_ONLY", "false"),
    ],
)
def test_rejects_complete_file_or_archived_audio_paths(
    tmp_path: Path,
    flag: str,
    value: str,
) -> None:
    env_file = tmp_path / ".env"
    supervisor_dir = tmp_path / "supervisor"
    supervisor_dir.mkdir()
    write_env(env_file, **{flag: value})

    with pytest.raises(VerificationError, match=flag):
        verify(env_file, supervisor_dir)


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("UNUSED_TTS_ENDPOINT", "http://127.0.0.1:6016/tts", "retired port 6016"),
        ("AGENT_MODEL_NAME", "Qwen3-8B-Instruct", "forbidden Qwen3 8B"),
    ],
)
def test_rejects_retired_port_or_model_without_echoing_the_value(
    tmp_path: Path,
    key: str,
    value: str,
    message: str,
) -> None:
    env_file = tmp_path / ".env"
    supervisor_dir = tmp_path / "supervisor"
    supervisor_dir.mkdir()
    write_env(env_file, **{key: value})

    with pytest.raises(VerificationError, match=message) as rejected:
        verify(env_file, supervisor_dir)
    assert value not in str(rejected.value)


def test_retired_port_check_does_not_inspect_unrelated_secret_digits(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    supervisor_dir = tmp_path / "supervisor"
    supervisor_dir.mkdir()
    write_env(env_file, MOSS_TTS_REALTIME_API_KEY="opaque-6016-secret")

    assert verify(env_file, supervisor_dir)["ok"] is True


@pytest.mark.parametrize(
    ("program", "command"),
    [
        ("legacy-lighttts", "/opt/legacy/start"),
        ("voice-service", "/opt/CosyVoice/server"),
        ("voice-service", "/opt/volcengine-tts/start"),
    ],
)
def test_rejects_legacy_supervisor_programs(tmp_path: Path, program: str, command: str) -> None:
    env_file = tmp_path / ".env"
    supervisor_dir = tmp_path / "supervisor"
    supervisor_dir.mkdir()
    write_env(env_file)
    (supervisor_dir / "legacy.conf").write_text(
        f"[program:{program}]\ncommand={command}\n",
        encoding="utf-8",
    )

    with pytest.raises(VerificationError, match="legacy TTS supervisor"):
        verify(env_file, supervisor_dir)
