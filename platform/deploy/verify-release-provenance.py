#!/usr/bin/env python3
"""Fail closed unless a release record matches its Git source checkout."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

OBJECT_ID = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


def validated_object_id(value: str, label: str) -> str:
    normalized = value.strip().lower()
    if not OBJECT_ID.fullmatch(normalized):
        raise ValueError(f"{label} must be a 40- or 64-character lowercase Git object id")
    return normalized


def git(source: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(source), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip().lower()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-dir", required=True, type=Path)
    parser.add_argument("--source-checkout", required=True, type=Path)
    parser.add_argument("--kind", required=True, choices=("api", "web"))
    args = parser.parse_args()
    record_path = args.release_dir / ".release-provenance.json"
    try:
        payload = json.loads(record_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise ValueError("unsupported provenance schema")
        commit = validated_object_id(str(payload["source_commit"]), "commit")
        tree = validated_object_id(str(payload["source_tree"]), "tree")
        if payload.get("service") != f"phdebate-{args.kind}":
            raise ValueError("release kind mismatch")
        if payload.get("release") != args.release_dir.name:
            raise ValueError("release name mismatch")
        if git(args.source_checkout, "rev-parse", f"{commit}^{{commit}}") != commit:
            raise ValueError("commit is unavailable in source checkout")
        if git(args.source_checkout, "rev-parse", f"{commit}^{{tree}}") != tree:
            raise ValueError("tree does not belong to commit")
    except (KeyError, OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        raise SystemExit(f"release provenance verification failed: {type(exc).__name__}: {exc}") from exc
    print(f"release_provenance_ok kind={args.kind} release={args.release_dir.name} commit={commit} tree={tree}")


if __name__ == "__main__":
    main()
