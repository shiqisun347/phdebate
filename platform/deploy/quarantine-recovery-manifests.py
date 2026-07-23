#!/usr/bin/env python3
"""Safely isolate structurally incomplete recovery manifests.

The command never deletes a manifest or an artifact. It only moves a manifest
after proving that its data-volume archive can still be named and protected by
the pruning job. Dry-run is the default; apply requires an explicit operator
acknowledgement.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

SAFE_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
ROLES = {
    "2": ("v2_database", "agent_database", "data_volumes", "private_config", "reliable_voice", "moss_offline"),
    "3": ("platform_database", "agent_database", "data_volumes", "private_config", "reliable_voice", "moss_offline"),
}


def read_manifest(path: Path) -> tuple[dict[str, str], str | None]:
    values: dict[str, str] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator:
            return values, f"invalid-line-{line_number}"
        if key in values:
            return values, f"duplicate-field-{key}"
        values[key] = value
    return values, None


def structural_problem(values: dict[str, str], parse_problem: str | None) -> str | None:
    if parse_problem:
        return parse_problem
    schema = values.get("schema_version", "")
    roles = ROLES.get(schema)
    if not roles:
        return f"unsupported-schema-{schema or 'missing'}"
    for role in roles:
        filename = values.get(f"artifact.{role}.filename", "")
        size = values.get(f"artifact.{role}.bytes", "")
        checksum = values.get(f"artifact.{role}.sha256", "")
        if not SAFE_FILENAME.fullmatch(filename):
            return f"missing-or-unsafe-{role}-filename"
        if not size.isdigit():
            return f"missing-or-invalid-{role}-bytes"
        if not re.fullmatch(r"[A-Fa-f0-9]{64}", checksum):
            return f"missing-or-invalid-{role}-sha256"
    return None


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", nargs="?", choices=("dry-run", "apply"), default="dry-run")
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=Path(os.environ.get("PHDEBATE_DEPLOY_BACKUP_DIR", "/home/ubuntu/sunsq/phdebate/runtime/deploy-backups")),
    )
    args = parser.parse_args()
    backup_dir = args.backup_dir.resolve()
    quarantine_dir = backup_dir / "quarantine"
    if args.mode == "apply" and os.environ.get("PHDEBATE_ALLOW_RECOVERY_MANIFEST_QUARANTINE") != "yes":
        raise SystemExit("apply requires PHDEBATE_ALLOW_RECOVERY_MANIFEST_QUARANTINE=yes after reviewing dry-run output")
    if not backup_dir.is_dir():
        print(f"recovery_manifest_quarantine_complete mode={args.mode} candidates=0 refused=0")
        return

    candidates = 0
    refused = 0
    for manifest in sorted(backup_dir.glob("recovery-set-*.manifest")):
        values, parse_problem = read_manifest(manifest)
        problem = structural_problem(values, parse_problem)
        if not problem:
            continue
        data_filename = values.get("artifact.data_volumes.filename", "")
        if not SAFE_FILENAME.fullmatch(data_filename):
            print(f"preserve file={manifest.name} reason={problem} quarantine_refused=missing-safe-data-reference")
            refused += 1
            continue
        data_archive = backup_dir / data_filename
        if not data_archive.is_file():
            print(f"preserve file={manifest.name} reason={problem} quarantine_refused=data-artifact-missing")
            refused += 1
            continue
        destination = quarantine_dir / manifest.name
        sidecar = quarantine_dir / f"{manifest.name}.quarantine.json"
        if destination.exists() or sidecar.exists():
            print(f"preserve file={manifest.name} reason={problem} quarantine_refused=destination-exists")
            refused += 1
            continue
        print(f"{args.mode} file={manifest.name} reason={problem} protects={data_filename}")
        candidates += 1
        if args.mode != "apply":
            continue
        quarantine_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = {
            "schema_version": 1,
            "quarantined_at": datetime.now(timezone.utc).isoformat(),
            "original_filename": manifest.name,
            "manifest_sha256": sha256(manifest),
            "reason": problem,
            "protected_data_volume": data_filename,
        }
        manifest.replace(destination)
        sidecar.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        sidecar.chmod(0o600)

    print(f"recovery_manifest_quarantine_complete mode={args.mode} candidates={candidates} refused={refused}")


if __name__ == "__main__":
    main()
