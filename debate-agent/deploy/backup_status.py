#!/usr/bin/env python3
"""Atomically publish a non-secret Agent database-backup status record."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def previous(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o640)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--status-file", required=True, type=Path)
    parser.add_argument("--state", required=True, choices=("running", "succeeded", "failed"))
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--retention-days", type=int, default=14)
    parser.add_argument("--error-code", default="")
    args = parser.parse_args()
    old = previous(args.status_file)
    now = datetime.now(timezone.utc).isoformat()
    payload = {key: old[key] for key in ("last_success_at", "file", "bytes", "sha256", "retention_days") if key in old}
    payload.update({"schema_version": 1, "state": args.state, "last_attempt_at": now})
    if args.state == "succeeded":
        if args.artifact is None or not args.artifact.is_file():
            raise SystemExit("successful status requires an existing artifact")
        payload.update(
            {
                "last_success_at": now,
                "file": args.artifact.name,
                "bytes": args.artifact.stat().st_size,
                "sha256": sha256(args.artifact),
                "retention_days": args.retention_days,
            }
        )
    elif args.state == "failed":
        payload["error_code"] = args.error_code or "backup_command_failed"
    write(args.status_file, payload)


if __name__ == "__main__":
    main()
