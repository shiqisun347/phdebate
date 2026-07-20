from __future__ import annotations

from collections.abc import Iterable

from app.models.entities import Room, RoomCodeReservation
from sqlalchemy import delete, select
from sqlalchemy.orm import Session


def release_verification_room_codes(db: Session, room_ids: Iterable[str]) -> list[str]:
    """Remove room-code reservations belonging only to disposable verifiers.

    Application flows must never call this helper: real room codes remain
    permanently reserved even after archival. Production verification scripts
    invoke it immediately before deleting the temporary rooms they created.
    """

    normalized = sorted(set(room_ids))
    if not normalized:
        return []
    codes = list(db.scalars(select(Room.code).where(Room.id.in_(normalized))).all())
    if codes:
        db.execute(delete(RoomCodeReservation).where(RoomCodeReservation.code.in_(codes)))
    return codes
