from __future__ import annotations

import asyncio

import app.services.realtime as realtime_service
from app.services.realtime import AudioStreamAbortRegistry, RoomHub


class SharedPresenceRedis:
    """Small script-level Redis double shared by independent RoomHub objects."""

    def __init__(self) -> None:
        self.sorted_sets: dict[str, dict[str, float]] = {}

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None

    def _set(self, key: str) -> dict[str, float]:
        return self.sorted_sets.setdefault(key, {})

    async def zadd(self, key: str, mapping: dict[str, float]) -> int:
        target = self._set(key)
        added = sum(member not in target for member in mapping)
        target.update(mapping)
        return added

    async def eval(self, script: str, key_count: int, *values):
        keys = [str(value) for value in values[:key_count]]
        args = list(values[key_count:])
        if script == realtime_service._PRESENCE_JOIN_SCRIPT:
            now_ms, expires_ms, _ttl_ms, connection_id, member = args
            leases = self._set(keys[0])
            for token, score in list(leases.items()):
                if score <= float(now_ms):
                    leases.pop(token)
            first = not leases
            leases[str(connection_id)] = float(expires_ms)
            self._set(keys[1])[str(member)] = max(leases.values())
            return int(first)
        if script == realtime_service._PRESENCE_LEAVE_SCRIPT:
            now_ms, connection_id, member, _ttl_ms = args
            leases = self._set(keys[0])
            for token, score in list(leases.items()):
                if score <= float(now_ms):
                    leases.pop(token)
            removed = int(leases.pop(str(connection_id), None) is not None)
            if leases:
                self._set(keys[1])[str(member)] = max(leases.values())
            else:
                self.sorted_sets.pop(keys[0], None)
                self._set(keys[1]).pop(str(member), None)
            return [removed, len(leases)]
        if script == realtime_service._PRESENCE_REFRESH_SCRIPT:
            now_ms, expires_ms, connection_id, _ttl_ms, member = args
            leases = self._set(keys[0])
            for token, score in list(leases.items()):
                if score <= float(now_ms):
                    leases.pop(token)
            if str(connection_id) not in leases:
                return 0
            leases[str(connection_id)] = float(expires_ms)
            self._set(keys[1])[str(member)] = max(leases.values())
            return 1
        if script == realtime_service._PRESENCE_REAP_SCRIPT:
            now_ms, limit, prefix = args
            index = self._set(keys[0])
            candidates = [
                member
                for member, _score in sorted(index.items(), key=lambda item: item[1])
                if index[member] <= float(now_ms)
            ][: int(limit)]
            expired = []
            for member in candidates:
                lease_key = f"{prefix}{member}"
                leases = self._set(lease_key)
                for token, score in list(leases.items()):
                    if score <= float(now_ms):
                        leases.pop(token)
                if leases:
                    index[member] = max(leases.values())
                else:
                    self.sorted_sets.pop(lease_key, None)
                    index.pop(member, None)
                    expired.append(member)
            return expired
        if script == realtime_service._PRESENCE_ACTIVE_SCRIPT:
            now_ms = float(args[0])
            leases = self._set(keys[0])
            for token, score in list(leases.items()):
                if score <= now_ms:
                    leases.pop(token)
            return int(bool(leases))
        if script == realtime_service._SPECTATOR_JOIN_SCRIPT:
            now_ms, expires_ms, limit, connection_id, _ttl_ms = args
            leases = self._set(keys[0])
            for token, score in list(leases.items()):
                if score <= float(now_ms):
                    leases.pop(token)
            if str(connection_id) in leases:
                leases[str(connection_id)] = float(expires_ms)
                return 1
            if len(leases) >= int(limit):
                return 0
            leases[str(connection_id)] = float(expires_ms)
            return 1
        if script == realtime_service._SPECTATOR_REFRESH_SCRIPT:
            now_ms, expires_ms, connection_id, _ttl_ms = args
            leases = self._set(keys[0])
            for token, score in list(leases.items()):
                if score <= float(now_ms):
                    leases.pop(token)
            if str(connection_id) not in leases:
                return 0
            leases[str(connection_id)] = float(expires_ms)
            return 1
        if script == realtime_service._SPECTATOR_LEAVE_SCRIPT:
            now_ms, connection_id = args
            leases = self._set(keys[0])
            for token, score in list(leases.items()):
                if score <= float(now_ms):
                    leases.pop(token)
            return int(leases.pop(str(connection_id), None) is not None)
        raise AssertionError("unexpected Lua script")


async def test_room_hub_fans_out_and_releases_local_subscribers() -> None:
    hub = RoomHub(redis_enabled=False)

    async def receive_one() -> dict:
        stream = hub.stream("123456")
        try:
            return await stream.__anext__()
        finally:
            await stream.aclose()

    first = asyncio.create_task(receive_one())
    second = asyncio.create_task(receive_one())
    await asyncio.sleep(0)
    await hub.publish("123456", {"type": "stage.advanced", "seq": 7})

    assert await first == {"type": "stage.advanced", "seq": 7}
    assert await second == {"type": "stage.advanced", "seq": 7}
    assert await hub.local_subscriber_count("123456") == 0


async def test_presence_reference_count_only_disconnects_last_device() -> None:
    hub = RoomHub(redis_enabled=False)
    assert await hub.presence_join("123456", "aff_1", "user-1") is True
    assert await hub.presence_join("123456", "aff_1", "user-1") is False
    assert await hub.presence_leave("123456", "aff_1", "user-1") is False
    assert await hub.presence_leave("123456", "aff_1", "user-1") is True


async def test_presence_lease_is_atomic_across_room_hub_instances(monkeypatch) -> None:
    shared = SharedPresenceRedis()
    monkeypatch.setattr(realtime_service.redis, "from_url", lambda *_args, **_kwargs: shared)
    first_worker = RoomHub()
    second_worker = RoomHub()

    assert await first_worker.presence_join("123456", "aff_1", "user-1", connection_id="socket-a") is True
    assert await second_worker.presence_join("123456", "aff_1", "user-1", connection_id="socket-b") is False
    assert await first_worker.presence_leave("123456", "aff_1", "user-1", connection_id="socket-a") is False
    assert await second_worker.presence_leave("123456", "aff_1", "user-1", connection_id="socket-b") is True

    await first_worker.close()
    await second_worker.close()


async def test_presence_lease_refresh_and_expiry_are_globally_claimed_once(monkeypatch) -> None:
    shared = SharedPresenceRedis()
    clock = {"seconds": 1_000.0}
    monkeypatch.setattr(realtime_service.redis, "from_url", lambda *_args, **_kwargs: shared)
    monkeypatch.setattr(realtime_service.time, "time", lambda: clock["seconds"])
    api_worker = RoomHub()
    first_engine = RoomHub()
    second_engine = RoomHub()

    assert await api_worker.presence_join("654321", "neg_2", "user-2", connection_id="socket-a") is True
    clock["seconds"] += realtime_service.PRESENCE_LEASE_SECONDS - 1
    assert await api_worker.presence_refresh("654321", "neg_2", "user-2", connection_id="socket-a") is True
    clock["seconds"] += realtime_service.PRESENCE_LEASE_SECONDS - 1
    assert await first_engine.reap_expired_presence() == []
    clock["seconds"] += 2
    claims = await asyncio.gather(
        first_engine.reap_expired_presence(),
        second_engine.reap_expired_presence(),
    )
    assert sorted(claims, key=len) == [[], [("654321", "neg_2", "user-2")]]
    assert await first_engine.presence_active("654321", "neg_2", "user-2") is False

    await api_worker.close()
    await first_engine.close()
    await second_engine.close()


async def test_presence_lease_falls_back_to_process_local_reference_count_when_redis_is_down(monkeypatch) -> None:
    class DownRedis:
        async def ping(self) -> bool:
            raise ConnectionError("Redis unavailable")

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(realtime_service.redis, "from_url", lambda *_args, **_kwargs: DownRedis())
    hub = RoomHub()
    assert await hub.presence_join("123456", "aff_1", "user-1", connection_id="socket-a") is True
    assert await hub.presence_join("123456", "aff_1", "user-1", connection_id="socket-b") is False
    assert await hub.presence_leave("123456", "aff_1", "user-1", connection_id="socket-a") is False
    assert await hub.presence_leave("123456", "aff_1", "user-1", connection_id="socket-b") is True


async def test_spectator_limit_is_atomic_across_workers_and_releases_slots(monkeypatch) -> None:
    shared = SharedPresenceRedis()
    monkeypatch.setattr(realtime_service.redis, "from_url", lambda *_args, **_kwargs: shared)
    first_worker = RoomHub()
    second_worker = RoomHub()

    admissions = await asyncio.gather(
        *[
            (first_worker if index % 2 == 0 else second_worker).spectator_join(
                "123456", connection_id=f"watcher-{index}"
            )
            for index in range(realtime_service.SPECTATOR_LIMIT)
        ]
    )
    assert all(admissions)
    assert await second_worker.spectator_join("123456", connection_id="watcher-overflow") is False

    await first_worker.spectator_leave("123456", connection_id="watcher-0")
    assert await second_worker.spectator_join("123456", connection_id="watcher-overflow") is True
    assert await second_worker.spectator_refresh("123456", connection_id="watcher-overflow") is True

    await first_worker.close()
    await second_worker.close()


async def test_spectator_lease_expiry_recovers_capacity(monkeypatch) -> None:
    shared = SharedPresenceRedis()
    clock = {"seconds": 1_000.0}
    monkeypatch.setattr(realtime_service.redis, "from_url", lambda *_args, **_kwargs: shared)
    monkeypatch.setattr(realtime_service.time, "time", lambda: clock["seconds"])
    hub = RoomHub()

    for index in range(realtime_service.SPECTATOR_LIMIT):
        assert await hub.spectator_join("654321", connection_id=f"watcher-{index}") is True
    assert await hub.spectator_join("654321", connection_id="next") is False
    clock["seconds"] += realtime_service.SPECTATOR_LEASE_SECONDS + 1
    assert await hub.spectator_join("654321", connection_id="next") is True
    # A still-open socket whose Redis lease expired during an event-loop stall
    # may reclaim a free slot instead of being falsely evicted.
    assert await hub.spectator_refresh("654321", connection_id="watcher-0") is True

    await hub.close()


async def test_spectator_refresh_keeps_admitted_socket_on_transient_redis_timeout(monkeypatch) -> None:
    hub = RoomHub()
    hub._spectator_leases["123456"].add("watcher-1")

    async def unavailable(*_args, **_kwargs):
        return None

    monkeypatch.setattr(hub, "_presence_eval", unavailable)
    assert await hub.spectator_refresh("123456", connection_id="watcher-1") is True


async def test_spectator_refresh_rejoins_after_expired_or_restarted_redis_lease(monkeypatch) -> None:
    hub = RoomHub()
    hub._spectator_leases["123456"].add("watcher-1")
    results = iter([0, 1])
    scripts: list[str] = []

    async def evaluate(script, _keys, _args):
        scripts.append(script)
        return next(results)

    monkeypatch.setattr(hub, "_presence_eval", evaluate)
    assert await hub.spectator_refresh("123456", connection_id="watcher-1") is True
    assert scripts == [realtime_service._SPECTATOR_REFRESH_SCRIPT, realtime_service._SPECTATOR_JOIN_SCRIPT]


async def test_spectator_refresh_evicts_only_when_atomic_rejoin_hits_global_limit(monkeypatch) -> None:
    hub = RoomHub()
    hub._spectator_leases["123456"].add("watcher-1")
    results = iter([0, 0])

    async def evaluate(_script, _keys, _args):
        return next(results)

    monkeypatch.setattr(hub, "_presence_eval", evaluate)
    assert await hub.spectator_refresh("123456", connection_id="watcher-1") is False


async def test_presence_timeout_does_not_disconnect_healthy_room_subscriptions(monkeypatch) -> None:
    class SlowRedis:
        def __init__(self) -> None:
            self.closed = False

        async def ping(self) -> bool:
            return True

        async def eval(self, *_args) -> int:
            await asyncio.sleep(1)
            return 1

        async def aclose(self) -> None:
            self.closed = True

    shared = SlowRedis()
    monkeypatch.setattr(realtime_service.redis, "from_url", lambda *_args, **_kwargs: shared)
    monkeypatch.setattr(realtime_service, "PRESENCE_REDIS_TIMEOUT_SECONDS", 0.001)
    hub = RoomHub()

    result = await hub._presence_eval("return 1", ["lease"], [])

    state = hub._state()
    assert result is None
    assert state.redis is shared
    assert state.redis_retry_at == 0
    assert shared.closed is False
    await hub.close()


async def test_audio_abort_registry_wakes_all_local_stream_senders() -> None:
    registry = AudioStreamAbortRegistry()
    first = registry.register("123456", "speech-1", "generation-1")
    second = registry.register("123456", "speech-1", "generation-1")
    assert registry.abort("123456", "speech-1", "generation-1") == 2
    await asyncio.wait_for(asyncio.gather(first.event.wait(), second.event.wait()), timeout=0.1)
    registry.unregister("123456", "speech-1", "generation-1", first)
    registry.unregister("123456", "speech-1", "generation-1", second)
    assert registry.abort("123456", "speech-1", "generation-1") == 0


async def test_heartbeat_reconnects_after_redis_connection_failure(monkeypatch) -> None:
    created = 0

    class FakeRedis:
        def __init__(self, *, failing: bool) -> None:
            self.failing = failing
            self.closed = False

        async def ping(self) -> bool:
            return True

        async def set(self, *_args, **_kwargs) -> bool:
            if self.failing:
                raise ConnectionError("Redis is restarting")
            return True

        async def aclose(self) -> None:
            self.closed = True

    def from_url(*_args, **_kwargs):
        nonlocal created
        created += 1
        return FakeRedis(failing=created == 1)

    monkeypatch.setattr(realtime_service.redis, "from_url", from_url)
    hub = RoomHub()
    assert await hub.heartbeat("engine") is False
    state = hub._state()
    assert state.redis is None
    state.redis_retry_at = 0
    assert await hub.heartbeat("engine") is True
    assert created == 2
    await hub.close()
