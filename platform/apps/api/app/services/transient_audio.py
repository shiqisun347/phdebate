from __future__ import annotations

from typing import Any

from app.core.config import settings
from app.services.media_storage import referenced_media
from sqlalchemy.orm import Session


def cleanup_transient_match_audio(db: Session) -> dict[str, Any]:
    """Remove abandoned per-match audio while preserving cues and history.

    Fixed host cues live below ``_cues`` and are intentionally reusable.  A
    legacy speech/audio asset that is still referenced by the database is also
    retained.  Everything else in a room directory is either an interrupted
    spool or an obsolete unreferenced match recording when text-only mode is
    active.
    """

    root = settings.media_path
    root.mkdir(parents=True, exist_ok=True)
    references = referenced_media(db)
    removed_files = 0
    removed_bytes = 0
    removed_paths: list[str] = []

    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(root)
        if relative.parts and relative.parts[0] == "_cues":
            continue
        relative_key = relative.as_posix()
        transient_name = (
            path.name.endswith(".part")
            or path.name.endswith(".source.wav")
            or path.name.endswith(".spool.wav")
        )
        obsolete_unreferenced_match_file = (
            not settings.match_audio_archive_enabled and relative_key not in references
        )
        if not transient_name and not obsolete_unreferenced_match_file:
            continue
        try:
            size = path.stat().st_size
            path.unlink(missing_ok=True)
        except OSError:
            continue
        removed_files += 1
        removed_bytes += size
        if len(removed_paths) < 200:
            removed_paths.append(relative_key)

    for directory in sorted(
        (item for item in root.rglob("*") if item.is_dir()),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        if directory == root or directory.name == "_cues":
            continue
        try:
            directory.rmdir()
        except OSError:
            pass

    return {
        "removed_files": removed_files,
        "removed_bytes": removed_bytes,
        "removed_paths": removed_paths,
    }
