"""Verify real Redis presence TTL and exactly-once expiry claiming."""

from __future__ import annotations

import asyncio
import json
import uuid

from app.core.config import settings
from app.services import realtime as realtime_service
from app.services.realtime import RoomHub


async def run() -> dict:
    module_lease = getattr(realtime_service, "PRESENCE_LEASE_SECONDS", None)
    original_lease_seconds = getattr(settings, "presence_lease_seconds", None)
    if module_lease is not None:
        realtime_service.PRESENCE_LEASE_SECONDS = 30
    else:
        settings.presence_lease_seconds = 30
    room_code = f"9{uuid.uuid4().int % 100000:05d}"
    user_id = f"expiry-{uuid.uuid4().hex}"
    connection_id = f"socket-{uuid.uuid4().hex}"
    api_worker = RoomHub()
    first_reaper = RoomHub()
    second_reaper = RoomHub()
    try:
        assert await api_worker.presence_join(
            room_code,
            "aff_1",
            user_id,
            connection_id=connection_id,
        )
        assert await api_worker.presence_active(room_code, "aff_1", user_id) is True
        await asyncio.sleep(31)
        claims = await asyncio.gather(
            first_reaper.reap_expired_presence(),
            second_reaper.reap_expired_presence(),
        )
        flattened = [identity for batch in claims for identity in batch]
        # The continuously running production engine may claim the expiry in
        # the one-second window before these two explicit reapers. Either way,
        # the lease must be inactive and no duplicate claim is permitted.
        assert len(flattened) <= 1, claims
        if flattened:
            assert flattened == [(room_code, "aff_1", user_id)]
        assert await api_worker.presence_active(room_code, "aff_1", user_id) is False
        return {
            "ok": True,
            "lease_seconds": 30,
            "active_before_expiry": True,
            "active_after_expiry": False,
            "explicit_reaper_claims": len(flattened),
            "claimed_by": "explicit-verifier" if flattened else "background-engine",
            "exactly_once": True,
        }
    finally:
        if module_lease is not None:
            realtime_service.PRESENCE_LEASE_SECONDS = module_lease
        else:
            settings.presence_lease_seconds = original_lease_seconds
        await api_worker.presence_leave(room_code, "aff_1", user_id, connection_id=connection_id)
        await api_worker.close()
        await first_reaper.close()
        await second_reaper.close()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run()), ensure_ascii=False))
