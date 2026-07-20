#!/usr/bin/env python3
"""Write a non-secret, machine-verifiable release provenance record."""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

OBJECT_ID = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
RELEASE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


def validated_object_id(value: str, label: str) -> str:
    normalized = value.strip().lower()
    if not OBJECT_ID.fullmatch(normalized):
        raise ValueError(f"{label} must be a 40- or 64-character lowercase Git object id")
    return normalized


def atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", required=True, choices=("api", "web"))
    parser.add_argument("--release", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--tree", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--base-path", default="")
    args = parser.parse_args()
    if not RELEASE_NAME.fullmatch(args.release):
        raise SystemExit("release name contains unsupported characters")
    if args.base_path != "":
        raise SystemExit("base path must be empty for the canonical deployment")
    try:
        commit = validated_object_id(args.commit, "commit")
        tree = validated_object_id(args.tree, "tree")
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    atomic_json(
        args.output,
        {
            "schema_version": 1,
            "service": f"phdebate-{args.kind}",
            "release": args.release,
            "source_commit": commit,
            "source_tree": tree,
            "base_path": args.base_path,
            "built_at": datetime.now(timezone.utc).isoformat(),
        },
    )


if __name__ == "__main__":
    main()
