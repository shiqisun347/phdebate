from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.deps import optional_user
from app.models.entities import AudioAsset, Match, Speech, User
from app.services.room_service import can_view_room, load_room

router = APIRouter(tags=["media"])


@router.get("/media/{code}/{filename}")
def room_media(
    code: str,
    filename: str,
    user: User | None = Depends(optional_user),
    db: Session = Depends(get_db),
) -> FileResponse:
    room = load_room(db, code)
    if not can_view_room(db, room, user):
        raise HTTPException(status_code=401 if not user else 403, detail="无权访问该房间音频。")
    if Path(filename).name != filename or filename.endswith(".part"):
        raise HTTPException(status_code=404, detail="音频文件不存在。")

    storage_key = f"/media/{room.code}/{filename}"
    referenced_speech = db.scalar(select(Speech.id).where(Speech.room_id == room.id, Speech.audio_url == storage_key))
    referenced_asset = db.scalar(
        select(AudioAsset.id)
        .join(Match, Match.id == AudioAsset.match_id)
        .where(Match.room_id == room.id, AudioAsset.storage_key == storage_key)
    )
    if not referenced_speech and not referenced_asset:
        raise HTTPException(status_code=404, detail="音频文件不存在。")

    room_directory = (settings.media_path / room.code).resolve()
    target = room_directory / filename
    if target.is_symlink() or target.resolve().parent != room_directory or not target.is_file():
        raise HTTPException(status_code=404, detail="音频文件不存在。")
    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    cache_control = (
        "public, max-age=3600"
        if room.visibility == "public" and not room.is_test_data
        else "private, no-store"
    )
    return FileResponse(target, media_type=media_type, headers={"Cache-Control": cache_control, "X-Content-Type-Options": "nosniff"})
