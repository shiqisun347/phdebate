from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.models.entities import Match
from app.services.match_archive import ARCHIVE_SCHEMA, ARCHIVE_VERSION, _canonical_json
from sqlalchemy import select
from sqlalchemy.orm import Session

MAX_SCAN_FILES = 100_000
MAX_DETAIL_ITEMS = 200
FINAL_MATCH_STATUSES = {"completed", "terminated", "review_required"}


def _safe_regular_file(root: Path, path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _match_id_for_archive_file(name: str) -> str | None:
    for suffix in (".meta.json", ".json.sha256", ".json"):
        if name.endswith(suffix):
            return name.removesuffix(suffix)
    return None


def _validate_archive(root: Path, match_id: str) -> str | None:
    archive_path = root / f"{match_id}.json"
    metadata_path = root / f"{match_id}.meta.json"
    checksum_path = root / f"{match_id}.json.sha256"
    try:
        archive_bytes = archive_path.read_bytes()
        document = json.loads(archive_bytes)
        metadata = json.loads(metadata_path.read_text("utf-8"))
        archive_sha256 = hashlib.sha256(archive_bytes).hexdigest()
        source_sha256 = hashlib.sha256(_canonical_json(document.get("data"))).hexdigest()
        expected_checksum = f"{archive_sha256}  {archive_path.name}\n"
        if document.get("schema") != ARCHIVE_SCHEMA or document.get("version") != ARCHIVE_VERSION:
            return "归档正文格式不受支持"
        if document.get("source_sha256") != source_sha256:
            return "归档正文数据摘要不一致"
        if metadata.get("schema") != ARCHIVE_SCHEMA or metadata.get("version") != ARCHIVE_VERSION:
            return "元数据格式不受支持"
        if metadata.get("match_id") != match_id:
            return "元数据比赛 ID 不一致"
        if metadata.get("source_sha256") != source_sha256:
            return "元数据源摘要不一致"
        if metadata.get("sha256") != archive_sha256 or metadata.get("bytes") != len(archive_bytes):
            return "元数据文件摘要或大小不一致"
        if checksum_path.read_text("ascii") != expected_checksum:
            return "校验文件不一致"
    except FileNotFoundError:
        return "缺少归档正文、元数据或校验文件"
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return "归档文件无法解析"
    return None


def inspect_archives(db: Session, *, min_age_hours: int = 24, delete: bool = False) -> dict[str, Any]:
    root = settings.archive_path
    root.mkdir(parents=True, exist_ok=True)
    all_match_ids = set(db.scalars(select(Match.id)).all())
    expected_match_ids = set(db.scalars(select(Match.id).where(Match.status.in_(FINAL_MATCH_STATUSES))).all())
    cutoff = datetime.now(timezone.utc) - timedelta(hours=min_age_hours)
    files: dict[str, tuple[Path, int, datetime]] = {}
    unsafe_entries = 0
    unmanaged_entries = 0
    truncated = False

    for index, path in enumerate(root.iterdir()):
        if index >= MAX_SCAN_FILES:
            truncated = True
            break
        if path.name == ".locks" and path.is_dir() and not path.is_symlink():
            continue
        if path.is_symlink():
            unsafe_entries += 1
            continue
        if path.is_dir() or not _safe_regular_file(root, path):
            unsafe_entries += 1
            continue
        stat = path.stat()
        files[path.name] = (path, stat.st_size, datetime.fromtimestamp(stat.st_mtime, timezone.utc))
        if _match_id_for_archive_file(path.name) is None and not path.name.endswith(".part"):
            unmanaged_entries += 1

    invalid: list[dict[str, str]] = []
    complete = 0
    for match_id in sorted(expected_match_ids):
        reason = _validate_archive(root, match_id)
        if reason:
            invalid.append({"match_id": match_id, "reason": reason})
        else:
            complete += 1

    candidates: list[tuple[str, Path, int, datetime]] = []
    for name, (path, size, modified_at) in files.items():
        if modified_at > cutoff:
            continue
        match_id = _match_id_for_archive_file(name)
        if (match_id is not None and match_id not in all_match_ids) or name.endswith(".part"):
            candidates.append((name, path, size, modified_at))
    candidates.sort(key=lambda item: item[3])

    deleted_files = 0
    deleted_bytes = 0
    deleted_paths: list[str] = []
    if delete and not truncated:
        for name, path, size, _modified_at in candidates:
            if not _safe_regular_file(root, path):
                continue
            stat = path.stat()
            if datetime.fromtimestamp(stat.st_mtime, timezone.utc) > cutoff:
                continue
            path.unlink(missing_ok=True)
            deleted_files += 1
            deleted_bytes += size
            deleted_paths.append(name)

    disk = shutil.disk_usage(root)
    return {
        "root": str(root),
        "scanned_files": len(files),
        "scanned_bytes": sum(item[1] for item in files.values()),
        "expected_matches": len(expected_match_ids),
        "complete_archives": complete,
        "invalid_archive_count": len(invalid),
        "invalid_archives": invalid[:MAX_DETAIL_ITEMS],
        "orphan_candidate_count": len(candidates),
        "orphan_candidate_bytes": sum(item[2] for item in candidates),
        "orphan_candidates": [
            {"path": name, "bytes": size, "modified_at": modified_at.isoformat()}
            for name, _path, size, modified_at in candidates[:MAX_DETAIL_ITEMS]
        ],
        "unsafe_entries": unsafe_entries,
        "unmanaged_entries": unmanaged_entries,
        "truncated": truncated,
        "min_age_hours": min_age_hours,
        "delete_requested": delete,
        "cleanup_blocked_reason": "扫描超过文件数量上限，未执行删除。" if delete and truncated else "",
        "deleted_files": deleted_files,
        "deleted_bytes": deleted_bytes,
        "deleted_paths": deleted_paths[:MAX_DETAIL_ITEMS],
        "disk_total_bytes": disk.total,
        "disk_used_bytes": disk.used,
        "disk_free_bytes": disk.free,
    }
