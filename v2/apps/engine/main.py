from __future__ import annotations

import asyncio
import logging

from app.core.database import SessionLocal, create_schema
from app.services.match_engine import match_engine, recover_inflight_engine_tasks
from app.services.realtime import room_hub
from app.services.seed import seed_database


async def main() -> None:
    create_schema()
    with SessionLocal() as db:
        seed_database(db)
    recovered = recover_inflight_engine_tasks()
    if recovered["speeches"] or recovered["judges"]:
        logging.getLogger(__name__).warning(
            "engine_restart_recovered speeches=%s judges=%s",
            recovered["speeches"],
            recovered["judges"],
        )

    async def heartbeat() -> None:
        while True:
            await room_hub.heartbeat("engine")
            await asyncio.sleep(5)

    heartbeat_task = asyncio.create_task(heartbeat(), name="jixia-engine-heartbeat")
    try:
        await match_engine.run()
    finally:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
        await room_hub.close()


if __name__ == "__main__":
    asyncio.run(main())
