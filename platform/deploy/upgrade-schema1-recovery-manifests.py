#!/usr/bin/env python3
"""Create verified schema 3 companions for legacy schema 1 recovery manifests.

Schema 1 manifests contain six ``SHA256  /absolute/path`` records instead of
named artifact fields.  This command never trusts those absolute paths and
never edits, moves, or deletes an existing manifest or recovery artifact.  It
locates each artifact by a safe basename inside explicitly approved roots,
checks its size and SHA-256, then creates a deterministic schema 3 companion
in a separate directory.

The default mode is an audit.  ``write`` requires an explicit environment
acknowledgement and uses exclusive, atomic file creation so an existing file
can never be overwritten.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

SAFE_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SHA256_LINE = re.compile(r"^([A-Fa-f0-9]{64})\s{2,}(.+)$")
ROLES = (
    "platform_database",
    "agent_database",
    "data_volumes",
    "private_config",
    "reliable_voice",
    "moss_offline",
)
METADATA_FIELDS = (
    "created_at",
    "code_branch",
    "code_commit",
    "deployed_application_commit",
    "database_schema",
    "api_release",
    "web_release",
    "reliable_audio_fingerprint",
)


@dataclass(frozen=True)
class LegacyArtifact:
    role: str
    filename: str
    bytes: int
    sha256: str


@dataclass(frozen=True)
class Upgrade:
    source: Path
    destination: Path
    content: str
    source_bytes: int
    source_sha256: str
    data_filename: str
    status: str


def digest(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(4 * 1024 * 1024):
            checksum.update(chunk)
    return checksum.hexdigest()


def read_legacy_manifest(path: Path, source_content: bytes) -> tuple[dict[str, str], list[tuple[str, str]]]:
    values: dict[str, str] = {}
    records: list[tuple[str, str]] = []
    for line_number, raw_line in enumerate(source_content.decode("utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, value = line.split("=", 1)
            if not key or key in values:
                raise ValueError(f"{path.name}: invalid or duplicate field on line {line_number}")
            values[key] = value
            continue
        match = SHA256_LINE.fullmatch(raw_line)
        if not match:
            raise ValueError(f"{path.name}: invalid legacy checksum record on line {line_number}")
        records.append((match.group(1).lower(), match.group(2)))
    if values.get("schema_version") != "1":
        raise ValueError(f"{path.name}: expected schema_version=1")
    if len(records) != len(ROLES):
        raise ValueError(f"{path.name}: expected six legacy artifacts; found {len(records)}")
    return values, records


def classify_legacy_path(raw_path: str) -> tuple[str, str]:
    legacy_path = PurePosixPath(raw_path)
    filename = legacy_path.name
    if not SAFE_FILENAME.fullmatch(filename):
        raise ValueError(f"unsafe legacy artifact basename: {filename or raw_path}")
    normalized = "/".join(legacy_path.parts).lower()
    candidates: list[str] = []
    if filename.endswith(".dump") and "debate-agent" in normalized:
        candidates.append("agent_database")
    elif filename.endswith(".dump") and "/runtime/backups/" in f"/{normalized}/":
        candidates.append("platform_database")
    if filename.endswith("-data-volumes.tar.gz"):
        candidates.append("data_volumes")
    if filename.endswith("-private-config.tar.gz") or filename.endswith("-full-private-config.tar.gz"):
        candidates.append("private_config")
    if filename.endswith("-reliable-voice-runtime.tar.gz"):
        candidates.append("reliable_voice")
    if filename.endswith("-openmoss-offline.tar"):
        candidates.append("moss_offline")
    if len(candidates) != 1:
        raise ValueError(f"cannot classify legacy artifact basename: {filename}")
    return candidates[0], filename


def resolve_artifact(filename: str, roots: list[Path]) -> Path:
    candidates = [root / filename for root in roots if (root / filename).is_file()]
    if any(candidate.is_symlink() for candidate in candidates):
        raise ValueError(f"artifact must not be a symlink: {filename}")
    matches = {candidate.resolve() for candidate in candidates}
    if len(matches) != 1:
        raise ValueError(f"expected exactly one approved artifact named {filename}; found {len(matches)}")
    artifact = matches.pop()
    return artifact


def inspect_legacy_artifacts(
    records: list[tuple[str, str]],
    roots: list[Path],
    digest_cache: dict[tuple[Path, int, int], str] | None = None,
) -> dict[str, LegacyArtifact]:
    digest_cache = digest_cache if digest_cache is not None else {}
    artifacts: dict[str, LegacyArtifact] = {}
    for expected_sha, raw_path in records:
        role, filename = classify_legacy_path(raw_path)
        if role in artifacts:
            raise ValueError(f"duplicate legacy artifact role: {role}")
        artifact = resolve_artifact(filename, roots)
        stat_before = artifact.stat()
        cache_key = (artifact, stat_before.st_size, stat_before.st_mtime_ns)
        actual_sha = digest_cache.get(cache_key)
        if actual_sha is None:
            actual_sha = digest(artifact)
            stat_after = artifact.stat()
            if (stat_after.st_size, stat_after.st_mtime_ns) != (
                stat_before.st_size,
                stat_before.st_mtime_ns,
            ):
                raise ValueError(f"artifact changed while hashing: {filename}")
            digest_cache[cache_key] = actual_sha
        if actual_sha != expected_sha:
            raise ValueError(f"SHA-256 mismatch for {role}: {filename}")
        artifacts[role] = LegacyArtifact(role, filename, stat_before.st_size, actual_sha)
    missing = [role for role in ROLES if role not in artifacts]
    if missing:
        raise ValueError(f"missing legacy artifact roles: {','.join(missing)}")
    return artifacts


def render_companion(
    source: Path,
    source_content: bytes,
    values: dict[str, str],
    artifacts: dict[str, LegacyArtifact],
) -> str:
    lines = [
        "schema_version=3",
        "upgrade_kind=schema1-verified-companion",
        "legacy_source.schema_version=1",
        f"legacy_source.filename={source.name}",
        f"legacy_source.bytes={len(source_content)}",
        f"legacy_source.sha256={hashlib.sha256(source_content).hexdigest()}",
    ]
    for key in METADATA_FIELDS:
        if key in values:
            lines.append(f"{key}={values[key]}")
    for role in ROLES:
        artifact = artifacts[role]
        lines.extend(
            (
                f"artifact.{role}.filename={artifact.filename}",
                f"artifact.{role}.bytes={artifact.bytes}",
                f"artifact.{role}.sha256={artifact.sha256}",
            )
        )
    return "\n".join(lines) + "\n"


def default_roots(backup_dir: Path) -> list[Path]:
    platform_root = Path(os.environ.get("PHDEBATE_ROOT", "/home/ubuntu/sunsq/phdebate"))
    agent_root = Path(os.environ.get("PHDEBATE_AGENT_ROOT", "/home/ubuntu/sunsq/debate-agent"))
    return [backup_dir, platform_root / "runtime" / "backups", agent_root / "backups"]


def plan_upgrades(backup_dir: Path, output_dir: Path, roots: list[Path]) -> list[Upgrade]:
    upgrades: list[Upgrade] = []
    # Historical recovery sets intentionally reuse large immutable artifacts.
    # Hash each unchanged path once per audit instead of repeatedly reading the
    # same multi-gigabyte archive for every manifest that references it.
    digest_cache: dict[tuple[Path, int, int], str] = {}
    for source in sorted(backup_dir.glob("recovery-set-*.manifest")):
        if source.is_symlink() or not source.is_file():
            raise ValueError(f"legacy manifest must be a regular file: {source.name}")
        source_content = source.read_bytes()
        source_text = source_content.decode("utf-8")
        first_values: dict[str, str] = {}
        for line in source_text.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                first_values.setdefault(key, value)
        if first_values.get("schema_version") != "1":
            continue
        values, records = read_legacy_manifest(source, source_content)
        artifacts = inspect_legacy_artifacts(records, roots, digest_cache)
        content = render_companion(source, source_content, values, artifacts)
        destination = output_dir / f"{source.name}.schema3"
        if destination.exists():
            if destination.is_symlink() or not destination.is_file():
                raise ValueError(f"upgrade destination is not a regular file: {destination.name}")
            if destination.read_text(encoding="utf-8") != content:
                raise ValueError(f"existing upgrade companion does not match verified source: {destination.name}")
            status = "verified-existing"
        else:
            status = "write-required"
        upgrades.append(
            Upgrade(
                source,
                destination,
                content,
                len(source_content),
                hashlib.sha256(source_content).hexdigest(),
                artifacts["data_volumes"].filename,
                status,
            )
        )
    return upgrades


def write_exclusive(path: Path, content: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", nargs="?", choices=("audit", "write"), default="audit")
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=Path(os.environ.get("PHDEBATE_DEPLOY_BACKUP_DIR", "/home/ubuntu/sunsq/phdebate/runtime/deploy-backups")),
    )
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--artifact-root", action="append", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    if args.mode == "write" and os.environ.get("PHDEBATE_ALLOW_SCHEMA1_MANIFEST_UPGRADE") != "yes":
        raise SystemExit("write requires PHDEBATE_ALLOW_SCHEMA1_MANIFEST_UPGRADE=yes after reviewing audit output")

    backup_dir = args.backup_dir.resolve()
    output_dir = (args.output_dir or backup_dir / "manifest-upgrades").resolve()
    report_path = args.report.resolve() if args.report else None
    if report_path is not None and report_path.exists():
        raise SystemExit(f"schema1_upgrade_invalid error=report destination already exists: {report_path.name}")
    roots = [root.resolve() for root in (args.artifact_root or default_roots(backup_dir)) if root.is_dir()]
    if not backup_dir.is_dir():
        print(f"schema1_upgrade_complete mode={args.mode} manifests=0 written=0 existing=0")
        return
    if not roots:
        raise SystemExit("schema1_upgrade_invalid error=no approved artifact roots exist")

    try:
        upgrades = plan_upgrades(backup_dir, output_dir, roots)
    except (OSError, UnicodeError, ValueError) as exc:
        raise SystemExit(f"schema1_upgrade_invalid error={exc}") from exc

    written = 0
    existing = 0
    if args.mode == "write":
        for upgrade in upgrades:
            current_content = upgrade.source.read_bytes()
            current_sha256 = hashlib.sha256(current_content).hexdigest()
            if len(current_content) != upgrade.source_bytes or current_sha256 != upgrade.source_sha256:
                raise SystemExit(f"schema1_upgrade_invalid error=source changed during audit: {upgrade.source.name}")
        for upgrade in upgrades:
            if upgrade.status == "verified-existing":
                existing += 1
                continue
            write_exclusive(upgrade.destination, upgrade.content, 0o600)
            written += 1
    else:
        existing = sum(upgrade.status == "verified-existing" for upgrade in upgrades)

    report = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": args.mode,
        "backup_dir": str(backup_dir),
        "output_dir": str(output_dir),
        "manifests": [
            {
                "source": upgrade.source.name,
                "source_sha256": upgrade.source_sha256,
                "destination": upgrade.destination.name,
                "data_volumes": upgrade.data_filename,
                "status": "written" if args.mode == "write" and upgrade.status == "write-required" else upgrade.status,
            }
            for upgrade in upgrades
        ],
    }
    for upgrade, manifest_report in zip(upgrades, report["manifests"]):
        print(
            f"schema1_upgrade mode={args.mode} file={upgrade.source.name} "
            f"status={manifest_report['status']} protects={upgrade.data_filename}"
        )
    if report_path is not None:
        write_exclusive(report_path, json.dumps(report, ensure_ascii=False, indent=2) + "\n", 0o600)
    print(
        f"schema1_upgrade_complete mode={args.mode} manifests={len(upgrades)} "
        f"written={written} existing={existing}"
    )


if __name__ == "__main__":
    main()
