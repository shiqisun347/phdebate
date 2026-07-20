from __future__ import annotations

from datetime import datetime, timezone

from app.core.security import as_utc
from app.models.entities import Season


def season_is_open(season: Season | None, *, at: datetime | None = None) -> bool:
    if not season or not season.is_active:
        return False
    current = at or datetime.now(timezone.utc)
    return as_utc(season.starts_at) <= current and (season.ends_at is None or current < as_utc(season.ends_at))


def serialize_season(season: Season) -> dict:
    return {
        "id": season.id,
        "name": season.name,
        "slug": season.slug,
        "starts_at": season.starts_at.isoformat(),
        "ends_at": season.ends_at.isoformat() if season.ends_at else None,
        "is_active": season.is_active,
        "is_open": season_is_open(season),
        "created_at": season.created_at.isoformat(),
        "updated_at": season.updated_at.isoformat(),
    }
