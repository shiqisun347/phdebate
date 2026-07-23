#!/usr/bin/env python3
"""Read-only storage, retention, and single-platform audit for operators."""

from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path


def tree_usage(path: Path, seen: set[tuple[int, int]] | None = None) -> tuple[int, int]:
    seen = seen if seen is not None else set()
    if not path.exists():
        return 0, 0
    if path.is_file():
        stat = path.stat()
        key = (stat.st_dev, stat.st_ino)
        if key in seen:
            return 0, 0
        seen.add(key)
        return stat.st_size, stat.st_blocks * 512
    logical = 0
    allocated = 0
    for base, _directories, files in os.walk(path, followlinks=False):
        for filename in files:
            item = Path(base) / filename
            try:
                if not item.is_symlink():
                    stat = item.stat()
                    key = (stat.st_dev, stat.st_ino)
                    if key not in seen:
                        seen.add(key)
                        logical += stat.st_size
                        allocated += stat.st_blocks * 512
            except FileNotFoundError:
                continue
    return logical, allocated


def usage_payload(path: Path) -> dict[str, int]:
    logical, allocated = tree_usage(path)
    return {"bytes": logical, "allocated_bytes": allocated}


def files_matching(directory: Path, patterns: tuple[str, ...]) -> list[Path]:
    found: dict[Path, Path] = {}
    if directory.is_dir():
        for pattern in patterns:
            for item in directory.glob(pattern):
                if item.is_file() and not item.is_symlink():
                    found[item.resolve()] = item
    return sorted(found.values(), key=lambda item: item.name)


def summarize(items: list[Path]) -> dict[str, int]:
    seen: set[tuple[int, int]] = set()
    logical = 0
    allocated = 0
    for item in items:
        item_logical, item_allocated = tree_usage(item, seen)
        logical += item_logical
        allocated += item_allocated
    return {"count": len(items), "bytes": logical, "allocated_bytes": allocated}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("/home/ubuntu/sunsq/phdebate"))
    parser.add_argument("--agent-root", type=Path, default=Path("/home/ubuntu/sunsq/debate-agent"))
    parser.add_argument("--warning-free-percent", type=float, default=20.0)
    parser.add_argument("--critical-free-percent", type=float, default=10.0)
    parser.add_argument("--large-log-bytes", type=int, default=100 * 1024 * 1024)
    parser.add_argument("--fail-on-critical", action="store_true")
    args = parser.parse_args()

    root = args.root.resolve()
    usage = shutil.disk_usage(root)
    free_percent = round(usage.free * 100 / usage.total, 2) if usage.total else 0.0
    if free_percent < args.critical_free_percent:
        disk_state = "critical"
    elif free_percent < args.warning_free_percent:
        disk_state = "warning"
    else:
        disk_state = "ok"
    runtime = root / "runtime"
    deploy_backups = runtime / "deploy-backups"
    log_files = files_matching(runtime / "logs", ("*.log", "*.log.*"))
    large_logs = [item for item in log_files if item.stat().st_size >= args.large_log_bytes]
    source_archives = files_matching(deploy_backups, ("*-source.tar.gz", "*-source-*.tar.gz"))
    data_archives = files_matching(deploy_backups, ("*-data-volumes.tar.gz",))
    recovery_manifests = files_matching(deploy_backups, ("recovery-set-*.manifest",))

    payload = {
        "schema_version": 1,
        "root": str(root),
        "disk": {
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "free_percent": free_percent,
            "state": disk_state,
        },
        "paths": {
            "runtime": usage_payload(runtime),
            "storage": usage_payload(root / "storage"),
            "platform_database_backups": usage_payload(runtime / "backups"),
            "deploy_backups": usage_payload(deploy_backups),
            "agent_database_backups": usage_payload(args.agent_root / "backups"),
        },
        "retention": {
            "api_releases": summarize([item for item in (runtime / "api-releases").glob("*") if item.is_dir()])
            if (runtime / "api-releases").is_dir() else {"count": 0, "bytes": 0, "allocated_bytes": 0},
            "web_releases": summarize([item for item in (runtime / "web-releases").glob("*") if item.is_dir()])
            if (runtime / "web-releases").is_dir() else {"count": 0, "bytes": 0, "allocated_bytes": 0},
            "recovery_manifests": summarize(recovery_manifests),
            "data_volume_archives": summarize(data_archives),
            "source_archives": summarize(source_archives),
        },
        "logs": {
            "count": len(log_files),
            "bytes": sum(item.stat().st_size for item in log_files),
            "large": [
                {"file": str(item.relative_to(root)), "bytes": item.stat().st_size}
                for item in sorted(large_logs, key=lambda value: value.stat().st_size, reverse=True)
            ],
        },
        "single_platform": {
            "legacy_root_exists": (root.parent / "phdebate-v2").exists(),
            "canonical_root_exists": root.is_dir(),
        },
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    if args.fail_on_critical and (disk_state == "critical" or payload["single_platform"]["legacy_root_exists"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
