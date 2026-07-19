#!/usr/bin/env python3
"""Create or verify the frozen realtime-audio baseline manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

PROTECTED_PATHS = (
    "apps/api/app/core/config.py",
    "apps/api/app/services/livekit_audio.py",
    "apps/api/app/services/providers.py",
    "apps/api/app/services/voice_runtime",
    "apps/web/components/debate-stage.tsx",
    "apps/web/lib/audio",
    "apps/web/public/worklets/asr-pcm-capture.js",
    "apps/web/public/worklets/livekit-interrupt-gate.js",
    "apps/web/public/worklets/pcm-ring-player.js",
    "assets/moss-prompts",
    "deploy/openmoss",
    "services/moss-realtime-gateway",
)

IGNORED_PARTS = {
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "node_modules",
}
IGNORED_SUFFIXES = {".pyc", ".pyo"}


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def protected_files(root: Path) -> list[Path]:
    files: set[Path] = set()
    for relative in PROTECTED_PATHS:
        target = root / relative
        if target.is_file():
            files.add(target)
            continue
        if not target.is_dir():
            raise SystemExit(f"protected path is missing: {relative}")
        for item in target.rglob("*"):
            if not item.is_file():
                continue
            if any(
                part in IGNORED_PARTS or part.startswith(".venv")
                for part in item.relative_to(root).parts
            ):
                continue
            if item.suffix in IGNORED_SUFFIXES:
                continue
            files.add(item)
    return sorted(files)


def build(root: Path) -> dict[str, object]:
    records = [
        {
            "path": str(path.relative_to(root)),
            "bytes": path.stat().st_size,
            "sha256": digest(path),
        }
        for path in protected_files(root)
    ]
    fingerprint = hashlib.sha256(
        json.dumps(records, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "policy": "frozen_reliable_audio_baseline",
        "change_rule": "Do not modify without explicit user approval and a new audio release gate.",
        "protected_paths": list(PROTECTED_PATHS),
        "file_count": len(records),
        "fingerprint_sha256": fingerprint,
        "files": records,
    }


def verify(root: Path, manifest_path: Path) -> None:
    expected = json.loads(manifest_path.read_text(encoding="utf-8"))
    actual = build(root)
    if expected.get("files") != actual["files"]:
        expected_by_path = {item["path"]: item for item in expected.get("files", [])}
        actual_by_path = {item["path"]: item for item in actual["files"]}
        changed = sorted(
            path
            for path in set(expected_by_path) | set(actual_by_path)
            if expected_by_path.get(path) != actual_by_path.get(path)
        )
        raise SystemExit("audio baseline mismatch: " + ", ".join(changed[:20]))
    print(
        f"audio_baseline_verified files={actual['file_count']} "
        f"fingerprint={actual['fingerprint_sha256']}"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--verify", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    if bool(args.output) == bool(args.verify):
        raise SystemExit("choose exactly one of --output or --verify")
    if args.verify:
        verify(root, args.verify.resolve())
        return
    document = build(root)
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"audio_baseline_created file={output} files={document['file_count']} "
        f"fingerprint={document['fingerprint_sha256']}"
    )


if __name__ == "__main__":
    main()
