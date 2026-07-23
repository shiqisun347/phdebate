#!/usr/bin/env python3
"""Fail closed when a production engine restart would interrupt a match."""

from __future__ import annotations

import json
from typing import Any

from app.core.database import SessionLocal
from app.models.entities import Room
from sqlalchemy import select
from sqlalchemy.orm import selectinload

ACTIVE_MATCH_STATUSES = ("preparing", "running", "paused", "judging")


def active_match_summaries() -> list[dict[str, Any]]:
    with SessionLocal() as db:
        rooms = list(
            db.scalars(
                select(Room)
                .where(Room.status.in_(ACTIVE_MATCH_STATUSES))
                .options(selectinload(Room.seats))
                .order_by(Room.updated_at.desc())
            ).all()
        )
        return [
            {
                "code": room.code,
                "status": room.status,
                "updated_at": room.updated_at.isoformat(),
                "human_seats": [
                    {
                        "seat_key": seat.seat_key,
                        "display_name": seat.display_name,
                        "connected": seat.connected,
                    }
                    for seat in room.seats
                    if seat.occupant_type == "human"
                ],
            }
            for room in rooms
        ]


def main() -> None:
    active = active_match_summaries()
    if active:
        print(
            "engine_restart_blocked active_matches="
            + json.dumps(active, ensure_ascii=False, separators=(",", ":"))
        )
        raise SystemExit(2)
    print("engine_restart_gate_ok active_matches=0")


if __name__ == "__main__":
    main()
