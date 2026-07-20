from __future__ import annotations

import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from app.core.config import settings
from app.models.entities import AudioAsset, AudioCue, Speech
from sqlalchemy import select
from sqlalchemy.orm import Session

MAX_SCAN_FILES = 100_000
MAX_DETAIL_ITEMS = 200


def _relative_reference(value: str) -> str | None:
    if not value.startswith("/media/"):
        return None
    raw = value.removeprefix("/media/").split("?", 1)[0].strip("/")
    candidate = PurePosixPath(raw)
    if not raw or candidate.is_absolute() or ".." in candidate.parts:
        return None
    return candidate.as_posix()


def referenced_media(db: Session) -> set[str]:
    values = set(db.scalars(select(Speech.audio_url).where(Speech.audio_url != "")).all())
    values.update(db.scalars(select(AudioAsset.storage_key).where(AudioAsset.storage_key != "")).all())
    values.update(db.scalars(select(AudioCue.audio_url).where(AudioCue.audio_url != "")).all())
    return {relative for value in values if (relative := _relative_reference(value)) is not None}


def _safe_regular_file(root: Path, path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def inspect_media(
    db: Session,
    *,
    min_age_hours: int = 24,
    include_stale_parts: bool = True,
    delete: bool = False,
) -> dict[str, Any]:
    root = settings.media_path
    root.mkdir(parents=True, exist_ok=True)
    references = referenced_media(db)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=min_age_hours)
    files: dict[str, tuple[Path, int, datetime]] = {}
    unsafe_entries = 0
    truncated = False

    for index, path in enumerate(root.rglob("*")):
        if index >= MAX_SCAN_FILES:
            truncated = True
            break
        if path.is_symlink():
            unsafe_entries += 1
            continue
        if path.is_dir():
            continue
        if not _safe_regular_file(root, path):
            unsafe_entries += 1
            continue
        stat = path.stat()
        relative = path.relative_to(root).as_posix()
        files[relative] = (path, stat.st_size, datetime.fromtimestamp(stat.st_mtime, timezone.utc))

    missing = sorted(reference for reference in references if reference not in files)
    candidates: list[tuple[str, Path, int, datetime]] = []
    for relative, (path, size, modified_at) in files.items():
        if relative in references or modified_at > cutoff:
            continue
        if path.name.endswith(".part") and not include_stale_parts:
            continue
        candidates.append((relative, path, size, modified_at))
    candidates.sort(key=lambda item: item[3])

    deleted_files = 0
    deleted_bytes = 0
    deleted_paths: list[str] = []
    if delete and not truncated:
        for relative, path, size, _modified_at in candidates:
            if not _safe_regular_file(root, path):
                continue
            stat = path.stat()
            if datetime.fromtimestamp(stat.st_mtime, timezone.utc) > cutoff:
                continue
            path.unlink(missing_ok=True)
            deleted_files += 1
            deleted_bytes += size
            deleted_paths.append(relative)
        for directory in sorted((path for path in root.rglob("*") if path.is_dir()), key=lambda item: len(item.parts), reverse=True):
            if directory != root:
                try:
                    directory.rmdir()
                except OSError:
                    pass

    disk = shutil.disk_usage(root)
    referenced_existing = references.intersection(files)
    return {
        "root": str(root),
        "scanned_files": len(files),
        "scanned_bytes": sum(item[1] for item in files.values()),
        "referenced_files": len(references),
        "referenced_existing_files": len(referenced_existing),
        "referenced_existing_bytes": sum(files[item][1] for item in referenced_existing),
        "missing_reference_count": len(missing),
        "missing_references": missing[:MAX_DETAIL_ITEMS],
        "orphan_candidate_count": len(candidates),
        "orphan_candidate_bytes": sum(item[2] for item in candidates),
        "orphan_candidates": [
            {"path": relative, "bytes": size, "modified_at": modified_at.isoformat()}
            for relative, _path, size, modified_at in candidates[:MAX_DETAIL_ITEMS]
        ],
        "unsafe_entries": unsafe_entries,
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
