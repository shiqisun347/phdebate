#!/usr/bin/env python3
"""Verify that production can only reach the approved MOSS realtime voice path.

The checker deliberately reads configuration without importing the application,
opening network connections, or printing secret values. Dormant legacy settings
are reported as cleanup debt; enabled legacy synthesis paths fail closed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


class VerificationError(RuntimeError):
    pass


REQUIRED_FLAGS = {
    "REALTIME_VOICE_PIPELINE_ENABLED": "true",
    "REALTIME_VOICE_BACKEND": "moss_realtime",
    "MOSS_TTS_REALTIME_ENABLED": "true",
    "MOSS_TTS_REALTIME_TRANSPORT": "websocket",
    # A complete-WAV preparation pass is not bidirectional streaming even if
    # the finished file is later sent over LiveKit. Formal matches must keep
    # the native text-in/audio-out session active for the whole turn.
    "MOSS_TTS_STABLE_PLAYBACK_ENABLED": "false",
    "WEBRTC_AUDIO_ENABLED": "true",
    "WEBRTC_AUDIO_BACKEND": "livekit",
    # Match audio is transport data. Persist text only so a hidden file player
    # cannot become a second AI playback path after WebRTC is interrupted.
    "MATCH_AUDIO_ARCHIVE_ENABLED": "false",
    "LIGHTTTS_STREAMING_ENABLED": "false",
    "LIGHTTTS_BISTREAM_ENABLED": "false",
}

LEGACY_SETTING_PREFIXES = (
    "LIGHTTTS_",
    "VOLCENGINE_TTS_",
    "COSYVOICE_",
)

LEGACY_SERVICE_PATTERN = re.compile(r"light[\s_-]*tts|cosy[\s_-]*voice|volcengine.*tts", re.IGNORECASE)


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values


def normalized(value: str) -> str:
    return value.strip().lower().replace("-", "_")


def verify_flags(values: dict[str, str]) -> dict[str, str]:
    verified: dict[str, str] = {}
    errors: list[str] = []
    for key, expected in REQUIRED_FLAGS.items():
        actual = normalized(values.get(key, ""))
        if actual != expected:
            errors.append(f"{key} is {actual or '<missing>'!r}, expected {expected!r}")
        else:
            verified[key] = expected
    if errors:
        raise VerificationError("; ".join(errors))
    return verified


def active_supervisor_programs(supervisor_dir: Path) -> tuple[list[str], list[str]]:
    programs: list[str] = []
    legacy: list[str] = []
    for path in sorted(supervisor_dir.glob("*.conf")):
        text = path.read_text(encoding="utf-8", errors="replace")
        names = re.findall(r"^\[program:([^\]]+)\]", text, flags=re.MULTILINE)
        commands = re.findall(r"^command=(.+)$", text, flags=re.MULTILINE)
        for name in names:
            programs.append(name)
            if LEGACY_SERVICE_PATTERN.search(name):
                legacy.append(name)
        for command in commands:
            if LEGACY_SERVICE_PATTERN.search(command):
                legacy.append(path.name)
    return sorted(set(programs)), sorted(set(legacy))


def legacy_residue(values: dict[str, str]) -> list[str]:
    return sorted(
        key
        for key, value in values.items()
        if value.strip() and key.startswith(LEGACY_SETTING_PREFIXES) and key not in REQUIRED_FLAGS
    )


def verify(env_file: Path, supervisor_dir: Path) -> dict[str, object]:
    if not env_file.is_file():
        raise VerificationError(f"environment file does not exist: {env_file}")
    if not supervisor_dir.is_dir():
        raise VerificationError(f"supervisor directory does not exist: {supervisor_dir}")
    values = parse_env(env_file)
    flags = verify_flags(values)
    programs, legacy_programs = active_supervisor_programs(supervisor_dir)
    if legacy_programs:
        raise VerificationError(f"legacy TTS supervisor program is still configured: {', '.join(legacy_programs)}")
    return {
        "ok": True,
        "active_voice_path": "agent_sse -> moss_realtime_websocket -> livekit_opus -> browser",
        "verified_flags": flags,
        "supervisor_programs": programs,
        "legacy_supervisor_programs": [],
        "dormant_legacy_setting_names": legacy_residue(values),
        "secrets_exposed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--supervisor-dir", type=Path, default=Path("/etc/supervisor/conf.d"))
    args = parser.parse_args()
    try:
        result = verify(args.env_file, args.supervisor_dir)
    except (OSError, VerificationError) as exc:
        print(json.dumps({"ok": False, "error": str(exc), "secrets_exposed": False}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
