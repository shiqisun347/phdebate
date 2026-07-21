from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.api import rooms as rooms_api


class CleanSession:
    new: set = set()
    dirty: set = set()
    deleted: set = set()

    def __init__(self) -> None:
        self.rolled_back = False

    def rollback(self) -> None:
        self.rolled_back = True


@pytest.mark.asyncio
async def test_room_publish_releases_sync_database_transaction_before_awaiting_redis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = CleanSession()
    observed: list[tuple[bool, str, dict]] = []

    async def publish(code: str, payload: dict) -> None:
        observed.append((db.rolled_back, code, payload))

    monkeypatch.setattr(rooms_api.room_hub, "publish", publish)
    await rooms_api._publish(db, SimpleNamespace(code="123456", seq=17), "room.updated")

    assert observed == [
        (
            True,
            "123456",
            {"type": "room.updated", "room_code": "123456", "seq": 17},
        )
    ]


@pytest.mark.asyncio
async def test_room_publish_refuses_uncommitted_mutations(monkeypatch: pytest.MonkeyPatch) -> None:
    db = CleanSession()
    db.dirty = {object()}
    called = False

    async def publish(_code: str, _payload: dict) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(rooms_api.room_hub, "publish", publish)
    with pytest.raises(RuntimeError, match="clean committed"):
        await rooms_api._publish(db, SimpleNamespace(code="123456", seq=17), "room.updated")
    assert called is False
    assert db.rolled_back is False
