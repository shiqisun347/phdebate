from __future__ import annotations

from dataclasses import dataclass

from app.models.entities import Match, Room
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

ACTIVE_ROOM_STATUSES = frozenset({"lobby", "preparing", "running", "paused", "judging"})
TERMINAL_MATCH_STATUSES = frozenset({"completed", "review_required", "terminated"})

# A cancelled lobby normally has no Match.  If a historical race left one
# behind, the only safe terminal projection is terminated: the cancelled room
# must never be resurrected into a runnable match.
EXPECTED_MATCH_STATUS_BY_TERMINAL_ROOM = {
    "cancelled": "terminated",
    "terminated": "terminated",
    "completed": "completed",
    "review_required": "review_required",
}


@dataclass(frozen=True)
class RoomMatchConsistencyIssue:
    room_id: str
    room_code: str
    room_status: str
    match_id: str
    match_status: str
    issue_type: str
    expected_match_status: str | None
    repairable: bool

    def serialize(self) -> dict:
        return {
            "room_id": self.room_id,
            "room_code": self.room_code,
            "room_status": self.room_status,
            "match_id": self.match_id,
            "match_status": self.match_status,
            "issue_type": self.issue_type,
            "expected_match_status": self.expected_match_status,
            "repairable": self.repairable,
        }


def classify_room_match(room: Room, match: Match) -> RoomMatchConsistencyIssue | None:
    expected = EXPECTED_MATCH_STATUS_BY_TERMINAL_ROOM.get(room.status)
    if expected is not None and match.status != expected:
        return RoomMatchConsistencyIssue(
            room_id=room.id,
            room_code=room.code,
            room_status=room.status,
            match_id=match.id,
            match_status=match.status,
            issue_type="terminal_room_match_mismatch",
            expected_match_status=expected,
            repairable=True,
        )
    if room.status in ACTIVE_ROOM_STATUSES and match.status in TERMINAL_MATCH_STATUSES:
        # The room may still have a live stage/timer.  Do not silently move it
        # to a terminal state; this direction needs operator investigation.
        return RoomMatchConsistencyIssue(
            room_id=room.id,
            room_code=room.code,
            room_status=room.status,
            match_id=match.id,
            match_status=match.status,
            issue_type="terminal_match_active_room",
            expected_match_status=None,
            repairable=False,
        )
    return None


def scan_room_match_consistency(
    db: Session,
    *,
    room_code: str = "",
    limit: int = 200,
) -> list[RoomMatchConsistencyIssue]:
    candidate_filter = or_(
        Room.status.in_(EXPECTED_MATCH_STATUS_BY_TERMINAL_ROOM),
        (Room.status.in_(ACTIVE_ROOM_STATUSES) & Match.status.in_(TERMINAL_MATCH_STATUSES)),
    )
    statement = (
        select(Room, Match)
        .join(Match, Match.room_id == Room.id)
        .where(candidate_filter)
        .order_by(Room.updated_at.desc(), Room.id)
        .limit(limit)
    )
    if room_code:
        statement = statement.where(Room.code == room_code)
    return [
        issue
        for room, match in db.execute(statement).all()
        if (issue := classify_room_match(room, match)) is not None
    ]
