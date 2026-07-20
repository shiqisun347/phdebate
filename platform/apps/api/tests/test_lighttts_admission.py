from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from app.core.config import settings
from app.services.lighttts_admission import (
    ACQUIRE_SCRIPT,
    ENQUEUE_SCRIPT,
    RECOVER_ACQUIRE_SCRIPT,
    RELEASE_SCRIPT,
    REMOVE_QUEUED_SCRIPT,
    RENEW_SCRIPT,
    SNAPSHOT_SCRIPT,
    LightTTSAdmissionCancelled,
    LightTTSAdmissionQueueFull,
    LightTTSAdmissionQueueTimeout,
    LightTTSAdmissionUnavailable,
    RedisLightTTSAdmissionGate,
    estimate_queue_wait,
)


class FakeRedisServer:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.active: dict[str, tuple[str, float]] = {}
        self.queues: dict[str, dict[str, int]] = {}
        self.deadlines: dict[str, dict[str, float]] = {}
        self.enqueued: dict[str, dict[str, float]] = {}
        self.sequences: dict[str, int] = {}
        self.unavailable = False

    def client(self) -> FakeRedisClient:
        return FakeRedisClient(self)

    @staticmethod
    def now_ms() -> float:
        return time.monotonic() * 1000

    def _cleanup(self, active_key: str, queue_key: str, deadline_key: str, enqueued_key: str) -> None:
        now = self.now_ms()
        active = self.active.get(active_key)
        if active is not None and active[1] <= now:
            self.active.pop(active_key, None)
        deadlines = self.deadlines.setdefault(deadline_key, {})
        queue = self.queues.setdefault(queue_key, {})
        enqueued = self.enqueued.setdefault(enqueued_key, {})
        for token, deadline in list(deadlines.items()):
            if deadline <= now:
                deadlines.pop(token, None)
                queue.pop(token, None)
                enqueued.pop(token, None)

    async def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> Any:
        if self.unavailable:
            raise ConnectionError("Redis unavailable")
        keys = [str(item) for item in keys_and_args[:numkeys]]
        args = list(keys_and_args[numkeys:])
        async with self.lock:
            if script == ENQUEUE_SCRIPT:
                active_key, queue_key, deadline_key, sequence_key, enqueued_key = keys
                token, max_pending, queue_timeout_ms, _sequence_ttl_ms = args
                self._cleanup(active_key, queue_key, deadline_key, enqueued_key)
                queue = self.queues.setdefault(queue_key, {})
                deadlines = self.deadlines.setdefault(deadline_key, {})
                if token in deadlines:
                    return [1, deadlines[token]]
                queue_capacity = int(max_pending) + int(active_key not in self.active)
                if len(queue) >= queue_capacity:
                    return [0, self.now_ms()]
                sequence = self.sequences.get(sequence_key, 0) + 1
                self.sequences[sequence_key] = sequence
                deadline = self.now_ms() + int(queue_timeout_ms)
                queue[str(token)] = sequence
                deadlines[str(token)] = deadline
                self.enqueued.setdefault(enqueued_key, {})[str(token)] = self.now_ms()
                return [1, deadline]
            if script == ACQUIRE_SCRIPT:
                active_key, queue_key, deadline_key, enqueued_key = keys
                token, lease_ms = str(args[0]), int(args[1])
                self._cleanup(active_key, queue_key, deadline_key, enqueued_key)
                deadlines = self.deadlines.setdefault(deadline_key, {})
                queue = self.queues.setdefault(queue_key, {})
                if token not in deadlines:
                    return [-1, self.now_ms()]
                if active_key in self.active:
                    return [0, self.now_ms()]
                head = min(queue, key=queue.__getitem__) if queue else None
                if head != token:
                    return [0, self.now_ms()]
                self.active[active_key] = (token, self.now_ms() + lease_ms)
                queue.pop(token, None)
                deadlines.pop(token, None)
                self.enqueued.setdefault(enqueued_key, {}).pop(token, None)
                return [1, self.now_ms() + lease_ms]
            if script == REMOVE_QUEUED_SCRIPT:
                queue_key, deadline_key, enqueued_key = keys
                token = str(args[0])
                removed = int(token in self.queues.setdefault(queue_key, {}))
                self.queues[queue_key].pop(token, None)
                self.deadlines.setdefault(deadline_key, {}).pop(token, None)
                self.enqueued.setdefault(enqueued_key, {}).pop(token, None)
                return removed
            if script == RENEW_SCRIPT:
                active_key = keys[0]
                token, lease_ms = str(args[0]), int(args[1])
                active = self.active.get(active_key)
                if active is not None and active[1] <= self.now_ms():
                    self.active.pop(active_key, None)
                    active = None
                if active is None or active[0] != token:
                    return 0
                self.active[active_key] = (token, self.now_ms() + lease_ms)
                return 1
            if script == RELEASE_SCRIPT:
                active_key = keys[0]
                token = str(args[0])
                active = self.active.get(active_key)
                if active is not None and active[0] == token:
                    self.active.pop(active_key, None)
                    return 1
                return 0
            if script == RECOVER_ACQUIRE_SCRIPT:
                active_key, queue_key, deadline_key, enqueued_key = keys
                token, lease_ms = str(args[0]), int(args[1])
                active = self.active.get(active_key)
                owns_active = active is not None and active[0] == token
                self.queues.setdefault(queue_key, {}).pop(token, None)
                self.deadlines.setdefault(deadline_key, {}).pop(token, None)
                self.enqueued.setdefault(enqueued_key, {}).pop(token, None)
                if owns_active:
                    self.active[active_key] = (token, self.now_ms() + lease_ms)
                    return 1
                return 0
            if script == SNAPSHOT_SCRIPT:
                active_key, queue_key, deadline_key, enqueued_key = keys
                self._cleanup(active_key, queue_key, deadline_key, enqueued_key)
                enqueued = self.enqueued.setdefault(enqueued_key, {})
                oldest_wait = self.now_ms() - min(enqueued.values()) if enqueued else 0
                return [int(active_key in self.active), len(self.queues.setdefault(queue_key, {})), oldest_wait]
        raise AssertionError("unknown admission script")


class FakeRedisClient:
    def __init__(self, server: FakeRedisServer) -> None:
        self.server = server
        self.closed = False

    async def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> Any:
        assert not self.closed
        return await self.server.eval(script, numkeys, *keys_and_args)

    async def aclose(self) -> None:
        self.closed = True


def gate(server: FakeRedisServer, **kwargs: Any) -> RedisLightTTSAdmissionGate:
    return RedisLightTTSAdmissionGate(
        client_factory=server.client,
        key_prefix="test:lighttts",
        max_pending=kwargs.pop("max_pending", 16),
        queue_timeout_seconds=kwargs.pop("queue_timeout_seconds", 1),
        lease_seconds=kwargs.pop("lease_seconds", 1),
        poll_seconds=kwargs.pop("poll_seconds", 0.01),
        **kwargs,
    )


async def test_global_gate_caps_multiple_provider_processes_at_one_active() -> None:
    server = FakeRedisServer()
    gates = [gate(server) for _ in range(3)]
    active = 0
    max_active = 0

    async def worker(index: int) -> None:
        nonlocal active, max_active
        lease = await gates[index % len(gates)].acquire()
        try:
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.02)
            active -= 1
        finally:
            await lease.release()

    await asyncio.gather(*(worker(index) for index in range(12)))
    assert max_active == 1
    assert server.active == {}


async def test_global_gate_bounds_pending_queue() -> None:
    server = FakeRedisServer()
    first_gate = gate(server, max_pending=1)
    first = await first_gate.acquire()
    waiting = asyncio.create_task(gate(server, max_pending=1).acquire())
    try:
        while not server.queues.get(first_gate.queue_key):
            await asyncio.sleep(0.01)
        with pytest.raises(LightTTSAdmissionQueueFull):
            await gate(server, max_pending=1).acquire()
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        await first.release()


async def test_idle_queue_reserves_the_active_slot_in_addition_to_max_pending() -> None:
    server = FakeRedisServer()
    admission = gate(server, max_pending=2)

    async def enqueue(token: str) -> int:
        client = server.client()
        try:
            result = await client.eval(
                ENQUEUE_SCRIPT,
                5,
                admission.active_key,
                admission.queue_key,
                admission.deadline_key,
                admission.sequence_key,
                admission.enqueued_key,
                token,
                2,
                1_000,
                2_000,
            )
            return int(result[0])
        finally:
            await client.aclose()

    accepted = await asyncio.gather(*(enqueue(f"burst-{index}") for index in range(4)))
    assert accepted.count(1) == 3
    assert accepted.count(0) == 1
    assert len(server.queues[admission.queue_key]) == 3


async def test_public_acquire_burst_forms_one_active_plus_two_waiters_in_fifo_order() -> None:
    server = FakeRedisServer()
    enqueue_count = 0
    all_enqueued = asyncio.Event()

    class BarrierClient(FakeRedisClient):
        async def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> Any:
            nonlocal enqueue_count
            result = await super().eval(script, numkeys, *keys_and_args)
            if script == ENQUEUE_SCRIPT:
                enqueue_count += 1
                if enqueue_count == 4:
                    all_enqueued.set()
                await all_enqueued.wait()
            return result

    admission = RedisLightTTSAdmissionGate(
        client_factory=lambda: BarrierClient(server),
        key_prefix="test:lighttts:burst",
        max_pending=2,
        queue_timeout_seconds=1,
        lease_seconds=1,
        poll_seconds=0.01,
    )
    release = asyncio.Event()
    acquisition_order: list[int] = []
    active = 0
    maximum_active = 0

    async def worker(index: int) -> str:
        nonlocal active, maximum_active
        try:
            lease = await admission.acquire()
        except LightTTSAdmissionQueueFull:
            return "rejected"
        acquisition_order.append(index)
        active += 1
        maximum_active = max(maximum_active, active)
        try:
            await release.wait()
            return "accepted"
        finally:
            active -= 1
            await lease.release()

    tasks = [asyncio.create_task(worker(index)) for index in range(4)]
    deadline = asyncio.get_running_loop().time() + 1
    while True:
        queue_depth = len(server.queues.get(admission.queue_key, {}))
        if admission.active_key in server.active and queue_depth == 2:
            break
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("burst did not stabilize at one active plus two waiters")
        await asyncio.sleep(0.01)
    release.set()
    results = await asyncio.gather(*tasks)
    assert results.count("accepted") == 3
    assert results.count("rejected") == 1
    assert acquisition_order == [0, 1, 2]
    assert maximum_active == 1
    assert server.active == {}
    assert server.queues.get(admission.queue_key, {}) == {}


def test_queue_wait_estimator_stays_empty_until_calibrated() -> None:
    assert estimate_queue_wait(
        active=1,
        queue_position=3,
        service_p50_seconds=None,
        service_p95_seconds=None,
    ) == {
        "queue_position": 3,
        "slots_ahead": 3,
        "eta_p50_seconds": None,
        "eta_p95_seconds": None,
    }
    assert estimate_queue_wait(
        active=1,
        queue_position=3,
        service_p50_seconds=4,
        service_p95_seconds=8,
    ) == {
        "queue_position": 3,
        "slots_ahead": 3,
        "eta_p50_seconds": 12.0,
        "eta_p95_seconds": 24.0,
    }


async def test_global_gate_expires_queue_deadline_and_removes_waiter() -> None:
    server = FakeRedisServer()
    holder = await gate(server).acquire()
    waiter_gate = gate(server, queue_timeout_seconds=0.08)
    try:
        with pytest.raises(LightTTSAdmissionQueueTimeout):
            await waiter_gate.acquire()
        assert server.queues.get(waiter_gate.queue_key, {}) == {}
        assert server.deadlines.get(waiter_gate.deadline_key, {}) == {}
    finally:
        await holder.release()


async def test_global_gate_removes_queued_cancellation_immediately() -> None:
    server = FakeRedisServer()
    holder = await gate(server).acquire()
    cancelled = False
    waiter_gate = gate(server)
    waiter = asyncio.create_task(waiter_gate.acquire(lambda: cancelled))
    try:
        while not server.queues.get(waiter_gate.queue_key):
            await asyncio.sleep(0.01)
        cancelled = True
        with pytest.raises(LightTTSAdmissionCancelled):
            await waiter
        assert server.queues.get(waiter_gate.queue_key, {}) == {}
        assert server.deadlines.get(waiter_gate.deadline_key, {}) == {}
        assert waiter_gate.metrics.snapshot()["events"]["cancel"] == 1
    finally:
        await holder.release()


async def test_global_gate_renews_lease_and_token_compare_prevents_stale_release() -> None:
    server = FakeRedisServer()
    admission = gate(server, lease_seconds=0.15)
    lease = await admission.acquire()
    await asyncio.sleep(0.35)
    assert server.active[admission.active_key][0] == lease.token
    assert server.active[admission.active_key][1] > server.now_ms()

    server.active[admission.active_key] = ("new-owner", server.now_ms() + 1_000)
    await lease.release()
    assert server.active[admission.active_key][0] == "new-owner"


async def test_global_gate_fails_closed_in_production_and_only_bypasses_in_non_production(monkeypatch) -> None:
    server = FakeRedisServer()
    server.unavailable = True
    admission = gate(server)
    monkeypatch.setattr(settings, "app_env", "production")
    with pytest.raises(LightTTSAdmissionUnavailable):
        await admission.acquire()

    monkeypatch.setattr(settings, "app_env", "test")
    lease = await admission.acquire()
    assert lease.bypassed is True
    await lease.release()


async def test_global_gate_snapshot_exposes_global_gauges_and_low_cardinality_events(monkeypatch) -> None:
    server = FakeRedisServer()
    admission = gate(server, service_p50_seconds=4, service_p95_seconds=8)
    monkeypatch.setattr(settings, "lighttts_global_gate_enabled", True)
    lease = await admission.acquire()
    waiter = asyncio.create_task(admission.acquire())
    try:
        while not server.queues.get(admission.queue_key):
            await asyncio.sleep(0.01)
        snapshot = await admission.status_snapshot()
        assert snapshot["ok"] is True
        assert snapshot["active"] == 1
        assert snapshot["queue_depth"] == 1
        assert snapshot["oldest_wait_seconds"] >= 0
        assert snapshot["next_queue_position"] == 2
        assert snapshot["estimated_wait_seconds"] == {"p50": 8.0, "p95": 16.0}
        assert snapshot["events"]["enqueue"] == 2
        assert snapshot["events"]["acquire"] == 1
        assert set(snapshot["events"]) == {"enqueue", "acquire", "reject", "cancel", "timeout", "lease_lost"}
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        await lease.release()

async def test_global_gate_records_active_lease_loss() -> None:
    server = FakeRedisServer()
    admission = gate(server, lease_seconds=0.12)
    lease = await admission.acquire()
    server.active.pop(admission.active_key)
    try:
        await asyncio.wait_for(lease._lost.wait(), timeout=0.5)
        assert admission.metrics.snapshot()["events"]["lease_lost"] == 1
    finally:
        await lease.release()


async def test_whole_job_deadline_bounds_redis_queue_wait() -> None:
    server = FakeRedisServer()
    admission = gate(server, queue_timeout_seconds=2)
    holder = await admission.acquire()
    try:
        deadline = asyncio.get_running_loop().time() + 0.06
        with pytest.raises(LightTTSAdmissionQueueTimeout):
            await admission.acquire(deadline_monotonic=deadline)
        assert admission.metrics.snapshot()["events"]["timeout"] == 1
        assert server.queues.get(admission.queue_key, {}) == {}
        assert server.enqueued.get(admission.enqueued_key, {}) == {}
    finally:
        await holder.release()


async def test_post_commit_acquire_response_loss_recovers_same_token_without_ghost_lock(monkeypatch) -> None:
    server = FakeRedisServer()
    created = 0

    class PostCommitResponseLossClient(FakeRedisClient):
        lost = False

        async def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> Any:
            result = await super().eval(script, numkeys, *keys_and_args)
            if script == ACQUIRE_SCRIPT and not self.lost:
                self.lost = True
                raise ConnectionError("response lost after Redis committed SET")
            return result

    def factory() -> FakeRedisClient:
        nonlocal created
        created += 1
        return PostCommitResponseLossClient(server) if created == 1 else FakeRedisClient(server)

    monkeypatch.setattr(settings, "app_env", "production")
    admission = RedisLightTTSAdmissionGate(
        client_factory=factory,
        key_prefix="test:lighttts:response-loss",
        max_pending=2,
        queue_timeout_seconds=1,
        lease_seconds=1,
        poll_seconds=0.01,
    )
    lease = await admission.acquire()
    try:
        assert lease.bypassed is False
        assert server.active[admission.active_key][0] == lease.token
        assert server.queues.get(admission.queue_key, {}) == {}
        assert admission.metrics.snapshot()["events"]["acquire"] == 1
    finally:
        await lease.release()
    assert server.active == {}


async def test_abandoned_shutdown_lease_is_not_released_and_blocks_until_ttl() -> None:
    server = FakeRedisServer()
    admission = gate(server, lease_seconds=0.15)
    lease = await admission.acquire()
    token = lease.token
    await lease.abandon()
    assert server.active[admission.active_key][0] == token
    with pytest.raises(LightTTSAdmissionQueueTimeout):
        await gate(server, queue_timeout_seconds=0.05).acquire()
    await asyncio.sleep(0.16)
    replacement = await gate(server).acquire()
    await replacement.release()
