#!/usr/bin/env python3
"""Verify multiple recovery manifests while hashing each shared artifact once."""

from __future__ import annotations

import argparse
import hashlib
import re
from pathlib import Path

SAFE_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
ROLES = {
    "2": ("v2_database", "agent_database", "data_volumes", "private_config", "reliable_voice", "moss_offline"),
    "3": ("platform_database", "agent_database", "data_volumes", "private_config", "reliable_voice", "moss_offline"),
}


def read_manifest(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or key in values:
            raise ValueError(f"invalid or duplicate field in {path.name}: {key}")
        values[key] = value
    return values


def digest(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(4 * 1024 * 1024):
            checksum.update(chunk)
    return checksum.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-root", action="append", type=Path, required=True)
    parser.add_argument("manifest", nargs="+", type=Path)
    args = parser.parse_args()
    roots = [root.resolve() for root in args.artifact_root if root.is_dir()]
    checksum_cache: dict[tuple[str, int, int, int, int], str] = {}
    verified_artifacts: set[Path] = set()

    try:
        for manifest in args.manifest:
            values = read_manifest(manifest)
            schema = values.get("schema_version", "")
            if schema not in ROLES:
                raise ValueError(f"unsupported schema in {manifest.name}")
            for role in ROLES[schema]:
                filename = values.get(f"artifact.{role}.filename", "")
                expected_bytes = values.get(f"artifact.{role}.bytes", "")
                expected_sha = values.get(f"artifact.{role}.sha256", "").lower()
                if not SAFE_FILENAME.fullmatch(filename):
                    raise ValueError(f"unsafe filename for {role} in {manifest.name}")
                if not expected_bytes.isdigit() or not re.fullmatch(r"[a-f0-9]{64}", expected_sha):
                    raise ValueError(f"invalid metadata for {role} in {manifest.name}")
                matches = {candidate.resolve() for root in roots if (candidate := root / filename).is_file()}
                if len(matches) != 1:
                    raise ValueError(f"expected one {role} artifact for {manifest.name}; found {len(matches)}")
                artifact = matches.pop()
                stat = artifact.stat()
                if stat.st_size != int(expected_bytes):
                    raise ValueError(f"size mismatch for {role} in {manifest.name}")
                cache_key = (str(artifact), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
                actual_sha = checksum_cache.get(cache_key)
                if actual_sha is None:
                    actual_sha = digest(artifact)
                    checksum_cache[cache_key] = actual_sha
                if actual_sha != expected_sha:
                    raise ValueError(f"checksum mismatch for {role} in {manifest.name}")
                verified_artifacts.add(artifact)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"recovery_index_invalid error={exc}") from exc

    print(
        f"recovery_index_verified manifests={len(args.manifest)} "
        f"unique_artifacts={len(verified_artifacts)} hashed_files={len(checksum_cache)}"
    )


if __name__ == "__main__":
    main()
