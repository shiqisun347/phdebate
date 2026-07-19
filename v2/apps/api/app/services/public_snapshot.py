from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

from app.core.database import SessionLocal
from app.models.entities import Room
from app.services.room_service import load_room, serialize_room
from sqlalchemy import select


@dataclass(frozen=True)
class SnapshotEntry:
    snapshot: dict[str, Any] | None
    initial_message: str | None
    seq: int
    created_at: float


class PublicSnapshotCache:
    """Coalesce identical anonymous room snapshots without caching privileged views."""

    def __init__(self, *, max_age_seconds: float = 1.0, max_rooms: int = 512) -> None:
        self.max_age_seconds = max_age_seconds
        self.max_rooms = max_rooms
        self._entries: dict[str, SnapshotEntry] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()
        self._database_loads: dict[str, int] = {}

    def clear(self) -> None:
        self._entries.clear()
        self._locks.clear()
        self._database_loads.clear()

    def database_loads(self, code: str) -> int:
        return self._database_loads.get(code, 0)

    def _valid(self, entry: SnapshotEntry | None, expected_seq: int | None, current: float) -> bool:
        if not entry or current - entry.created_at > self.max_age_seconds:
            return False
        return expected_seq is None or entry.seq >= expected_seq

    async def _room_lock(self, code: str) -> asyncio.Lock:
        async with self._guard:
            return self._locks.setdefault(code, asyncio.Lock())

    async def get(self, code: str, *, expected_seq: int | None = None) -> dict[str, Any] | None:
        entry = await self._get_entry(code, expected_seq=expected_seq)
        return entry.snapshot

    async def initial_message(self, code: str) -> str | None:
        entry = await self._get_entry(code)
        return entry.initial_message

    async def get_if_newer(self, code: str, *, known_seq: int) -> dict[str, Any] | None:
        """Return a fresh public snapshot only when authority advanced.

        This deliberately performs only a narrow room metadata query. It is
        used once while handing a websocket from its cached initial snapshot
        to live pub/sub, closing that race without serializing the same public
        room hundreds of times during a spectator connection burst.
        """

        # SQLAlchemy's synchronous session must not run on the ASGI event-loop
        # thread. A spectator burst otherwise serializes hundreds of hand-off
        # checks and prevents new WebSocket handshakes from being accepted.
        row = await asyncio.to_thread(self._load_room_authority, code)
        if row is None or row.visibility != "public":
            return None
        if int(row.seq) <= known_seq:
            return await self.get(code, expected_seq=known_seq)
        return await self.get(code, expected_seq=int(row.seq))

    @staticmethod
    def _load_room_authority(code: str) -> Any:
        with SessionLocal() as db:
            return db.execute(select(Room.seq, Room.visibility).where(Room.code == code)).one_or_none()

    async def _get_entry(self, code: str, *, expected_seq: int | None = None) -> SnapshotEntry:
        current = time.monotonic()
        entry = self._entries.get(code)
        if self._valid(entry, expected_seq, current):
            return entry

        lock = await self._room_lock(code)
        async with lock:
            current = time.monotonic()
            entry = self._entries.get(code)
            if self._valid(entry, expected_seq, current):
                return entry
            with SessionLocal() as db:
                room = load_room(db, code)
                snapshot = serialize_room(db, room, None, public=True) if room.visibility == "public" else None
                initial_message = (
                    json.dumps({"type": "snapshot", "room": snapshot}, ensure_ascii=False, separators=(",", ":"))
                    if snapshot is not None
                    else None
                )
                entry = SnapshotEntry(snapshot=snapshot, initial_message=initial_message, seq=room.seq, created_at=current)
            self._entries[code] = entry
            self._database_loads[code] = self._database_loads.get(code, 0) + 1
            await self._prune(current, keep=code)
            return entry

    async def _prune(self, current: float, *, keep: str) -> None:
        if len(self._entries) <= self.max_rooms:
            return
        async with self._guard:
            expired = [code for code, entry in self._entries.items() if code != keep and current - entry.created_at > self.max_age_seconds]
            for code in expired:
                self._entries.pop(code, None)
                self._locks.pop(code, None)
                self._database_loads.pop(code, None)
            if len(self._entries) <= self.max_rooms:
                return
            oldest = sorted(
                ((code, entry.created_at) for code, entry in self._entries.items() if code != keep),
                key=lambda item: item[1],
            )
            for code, _created_at in oldest[: max(0, len(self._entries) - self.max_rooms)]:
                self._entries.pop(code, None)
                self._locks.pop(code, None)
                self._database_loads.pop(code, None)


public_snapshot_cache = PublicSnapshotCache()
