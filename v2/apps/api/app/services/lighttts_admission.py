from __future__ import annotations

import asyncio
import inspect
import logging
import threading
import time
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, Union

import redis.asyncio as redis
from app.core.config import settings

logger = logging.getLogger(__name__)


class LightTTSAdmissionError(RuntimeError):
    pass


class LightTTSAdmissionUnavailable(LightTTSAdmissionError):
    pass


class LightTTSAdmissionQueueFull(LightTTSAdmissionError):
    pass


class LightTTSAdmissionQueueTimeout(LightTTSAdmissionError):
    pass


class LightTTSAdmissionCancelled(LightTTSAdmissionError):
    pass


class RedisClient(Protocol):
    async def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> Any: ...

    async def aclose(self) -> None: ...


CancelCallback = Callable[[], Union[bool, Awaitable[bool]]]
RedisClientFactory = Callable[[], RedisClient]


def estimate_queue_wait(
    *,
    active: int,
    queue_position: int,
    service_p50_seconds: float | None,
    service_p95_seconds: float | None,
) -> dict[str, float | int | None]:
    """Estimate time until service starts for a one-slot FIFO queue.

    ``queue_position`` is one-based among waiting jobs.  The estimator is pure
    and deliberately returns ``None`` until an environment-specific benchmark
    supplies service-time calibration; inventing an ETA is worse than showing
    only a position.
    """

    normalized_active = 1 if active else 0
    normalized_position = max(1, int(queue_position))
    slots_ahead = normalized_active + normalized_position - 1

    def estimate(service_seconds: float | None) -> float | None:
        if service_seconds is None or service_seconds <= 0:
            return None
        return round(slots_ahead * float(service_seconds), 3)

    return {
        "queue_position": normalized_position,
        "slots_ahead": slots_ahead,
        "eta_p50_seconds": estimate(service_p50_seconds),
        "eta_p95_seconds": estimate(service_p95_seconds),
    }


ENQUEUE_SCRIPT = """-- lighttts-admission-enqueue-v2
local active_key = KEYS[1]
local queue_key = KEYS[2]
local deadline_key = KEYS[3]
local sequence_key = KEYS[4]
local enqueued_key = KEYS[5]
local token = ARGV[1]
local max_pending = tonumber(ARGV[2])
local queue_timeout_ms = tonumber(ARGV[3])
local sequence_ttl_ms = tonumber(ARGV[4])
local now_parts = redis.call('TIME')
local now_ms = tonumber(now_parts[1]) * 1000 + math.floor(tonumber(now_parts[2]) / 1000)
local expired = redis.call('ZRANGEBYSCORE', deadline_key, '-inf', now_ms)
for _, expired_token in ipairs(expired) do
    redis.call('ZREM', queue_key, expired_token)
    redis.call('ZREM', deadline_key, expired_token)
    redis.call('ZREM', enqueued_key, expired_token)
end
local existing_deadline = redis.call('ZSCORE', deadline_key, token)
if existing_deadline then
    return {1, tonumber(existing_deadline)}
end
-- max_pending describes waiters behind the single active job.  When the slot
-- is idle, the queue may temporarily hold one extra token that is about to
-- acquire it; after acquisition the stable state is 1 active + max_pending.
local queue_capacity = max_pending
if redis.call('EXISTS', active_key) == 0 then
    queue_capacity = max_pending + 1
end
if redis.call('ZCARD', queue_key) >= queue_capacity then
    return {0, now_ms}
end
local sequence = redis.call('INCR', sequence_key)
local deadline_ms = now_ms + queue_timeout_ms
redis.call('ZADD', queue_key, sequence, token)
redis.call('ZADD', deadline_key, deadline_ms, token)
redis.call('ZADD', enqueued_key, now_ms, token)
redis.call('PEXPIRE', sequence_key, sequence_ttl_ms)
return {1, deadline_ms}
"""


ACQUIRE_SCRIPT = """-- lighttts-admission-acquire-v1
local active_key = KEYS[1]
local queue_key = KEYS[2]
local deadline_key = KEYS[3]
local enqueued_key = KEYS[4]
local token = ARGV[1]
local lease_ms = tonumber(ARGV[2])
local now_parts = redis.call('TIME')
local now_ms = tonumber(now_parts[1]) * 1000 + math.floor(tonumber(now_parts[2]) / 1000)
local expired = redis.call('ZRANGEBYSCORE', deadline_key, '-inf', now_ms)
for _, expired_token in ipairs(expired) do
    redis.call('ZREM', queue_key, expired_token)
    redis.call('ZREM', deadline_key, expired_token)
    redis.call('ZREM', enqueued_key, expired_token)
end
if not redis.call('ZSCORE', deadline_key, token) then
    return {-1, now_ms}
end
if redis.call('EXISTS', active_key) == 1 then
    return {0, now_ms}
end
local head = redis.call('ZRANGE', queue_key, 0, 0)
if not head[1] or head[1] ~= token then
    return {0, now_ms}
end
local acquired = redis.call('SET', active_key, token, 'PX', lease_ms, 'NX')
if not acquired then
    return {0, now_ms}
end
redis.call('ZREM', queue_key, token)
redis.call('ZREM', deadline_key, token)
redis.call('ZREM', enqueued_key, token)
return {1, now_ms + lease_ms}
"""


RECOVER_ACQUIRE_SCRIPT = """-- lighttts-admission-recover-acquire-v1
local active_key = KEYS[1]
local queue_key = KEYS[2]
local deadline_key = KEYS[3]
local enqueued_key = KEYS[4]
local token = ARGV[1]
local lease_ms = tonumber(ARGV[2])
local owns_active = redis.call('GET', active_key) == token
redis.call('ZREM', queue_key, token)
redis.call('ZREM', deadline_key, token)
redis.call('ZREM', enqueued_key, token)
if owns_active then
    redis.call('PEXPIRE', active_key, lease_ms)
    return 1
end
return 0
"""


REMOVE_QUEUED_SCRIPT = """-- lighttts-admission-remove-queued-v1
local removed = redis.call('ZREM', KEYS[1], ARGV[1])
redis.call('ZREM', KEYS[2], ARGV[1])
redis.call('ZREM', KEYS[3], ARGV[1])
return removed
"""


RENEW_SCRIPT = """-- lighttts-admission-renew-v1
if redis.call('GET', KEYS[1]) == ARGV[1] then
    redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[2]))
    return 1
end
return 0
"""


RELEASE_SCRIPT = """-- lighttts-admission-release-v1
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


SNAPSHOT_SCRIPT = """-- lighttts-admission-snapshot-v1
local active_key = KEYS[1]
local queue_key = KEYS[2]
local deadline_key = KEYS[3]
local enqueued_key = KEYS[4]
local now_parts = redis.call('TIME')
local now_ms = tonumber(now_parts[1]) * 1000 + math.floor(tonumber(now_parts[2]) / 1000)
local expired = redis.call('ZRANGEBYSCORE', deadline_key, '-inf', now_ms)
for _, expired_token in ipairs(expired) do
    redis.call('ZREM', queue_key, expired_token)
    redis.call('ZREM', deadline_key, expired_token)
    redis.call('ZREM', enqueued_key, expired_token)
end
local oldest_wait_ms = 0
local oldest = redis.call('ZRANGE', enqueued_key, 0, 0, 'WITHSCORES')
if oldest[2] then
    oldest_wait_ms = math.max(0, now_ms - tonumber(oldest[2]))
end
return {redis.call('EXISTS', active_key), redis.call('ZCARD', queue_key), oldest_wait_ms}
"""


async def _cancel_requested(callback: CancelCallback | None) -> bool:
    if callback is None:
        return False
    result = callback()
    return bool(await result) if inspect.isawaitable(result) else bool(result)


class LightTTSAdmissionMetrics:
    """Process-local, low-cardinality counters for the shared Redis gate."""

    EVENT_NAMES = ("enqueue", "acquire", "reject", "cancel", "timeout", "lease_lost")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: Counter[str] = Counter()
        self._started_at = time.time()

    def record(self, event: str) -> None:
        if event not in self.EVENT_NAMES:
            raise ValueError(f"unknown LightTTS admission event: {event}")
        with self._lock:
            self._counts[event] += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            counts = {event: int(self._counts[event]) for event in self.EVENT_NAMES}
        return {"events": counts, "process_started_at": self._started_at}


@dataclass
class LightTTSAdmissionLease:
    gate: RedisLightTTSAdmissionGate
    token: str
    client: RedisClient | None
    bypassed: bool = False
    _lost: asyncio.Event = field(default_factory=asyncio.Event)
    _renew_task: asyncio.Task[None] | None = None
    _released: bool = False

    @property
    def lost(self) -> bool:
        return self._lost.is_set()

    async def cancellation_requested(self, callback: CancelCallback | None) -> bool:
        return self.lost or await _cancel_requested(callback)

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        if self._renew_task is not None:
            self._renew_task.cancel()
            await asyncio.gather(self._renew_task, return_exceptions=True)
        if self.client is None:
            return
        try:
            await self.client.eval(RELEASE_SCRIPT, 1, self.gate.active_key, self.token)
        except Exception as exc:  # release is protected by lease expiry
            logger.warning("LightTTS admission release failed error_type=%s", type(exc).__name__)
        finally:
            await self.gate._close_client(self.client)

    async def abandon(self) -> None:
        """Stop renewing without releasing an uncertain in-flight GPU lease.

        This path is reserved for event-loop shutdown cancelling the internal
        drain task.  Redis keeps the token until TTL, preventing a replacement
        process from overlapping an inference whose HTTP disconnect may not
        stop GPU execution.
        """
        if self._released:
            return
        self._released = True
        if self._renew_task is not None:
            self._renew_task.cancel()
            await asyncio.gather(self._renew_task, return_exceptions=True)
        if self.client is not None:
            await self.gate._close_client(self.client)
        logger.error("LightTTS admission lease abandoned until TTL after unsafe shutdown cancellation")


class RedisLightTTSAdmissionGate:
    """A Redis-backed FIFO lease that caps LightTTS globally across processes."""

    def __init__(
        self,
        *,
        client_factory: RedisClientFactory | None = None,
        # The hash tag keeps all Lua keys in one slot if Redis is clustered.
        key_prefix: str = "jixia:{lighttts-admission-v1}",
        max_pending: int | None = None,
        queue_timeout_seconds: float | None = None,
        lease_seconds: float | None = None,
        poll_seconds: float = 0.1,
        service_p50_seconds: float | None = None,
        service_p95_seconds: float | None = None,
    ) -> None:
        self._client_factory = client_factory or self._default_client
        self.key_prefix = key_prefix
        self.max_pending = max_pending
        self.queue_timeout_seconds = queue_timeout_seconds
        self.lease_seconds = lease_seconds
        self.poll_seconds = poll_seconds
        self.service_p50_seconds = service_p50_seconds
        self.service_p95_seconds = service_p95_seconds
        self.metrics = LightTTSAdmissionMetrics()

    @property
    def active_key(self) -> str:
        return f"{self.key_prefix}:active"

    @property
    def queue_key(self) -> str:
        return f"{self.key_prefix}:queue"

    @property
    def deadline_key(self) -> str:
        return f"{self.key_prefix}:deadlines"

    @property
    def sequence_key(self) -> str:
        return f"{self.key_prefix}:sequence"

    @property
    def enqueued_key(self) -> str:
        return f"{self.key_prefix}:enqueued"

    @staticmethod
    def _default_client() -> RedisClient:
        return redis.from_url(settings.redis_url, decode_responses=True)

    @staticmethod
    async def _close_client(client: RedisClient) -> None:
        try:
            await client.aclose()
        except Exception:
            logger.debug("LightTTS admission Redis client close failed", exc_info=True)

    @staticmethod
    def _production_fail_closed() -> bool:
        return settings.app_env == "production"

    def _limits(self) -> tuple[int, float, float]:
        max_pending = self.max_pending if self.max_pending is not None else settings.lighttts_global_gate_max_pending
        queue_timeout = (
            self.queue_timeout_seconds
            if self.queue_timeout_seconds is not None
            else settings.lighttts_global_gate_queue_timeout_seconds
        )
        lease_seconds = self.lease_seconds if self.lease_seconds is not None else settings.lighttts_global_gate_lease_seconds
        return max_pending, queue_timeout, lease_seconds

    def record_cancel(self) -> None:
        self.metrics.record("cancel")

    def record_timeout(self) -> None:
        self.metrics.record("timeout")

    async def status_snapshot(self) -> dict[str, Any]:
        """Return global gauges plus process-local counters without job identifiers."""
        result = {
            "enabled": settings.lighttts_global_gate_enabled,
            "fail_closed": self._production_fail_closed(),
            "max_active": settings.lighttts_max_active,
            "max_pending": self._limits()[0],
            "queue_timeout_seconds": self._limits()[1],
            **self.metrics.snapshot(),
        }
        if not settings.lighttts_global_gate_enabled:
            return result | {
                "ok": True,
                "active": 0,
                "queue_depth": 0,
                "oldest_wait_seconds": 0.0,
                "next_queue_position": 0,
                "estimated_wait_seconds": {
                    "p50": 0.0,
                    "p95": 0.0,
                },
            }
        client: RedisClient | None = None
        try:
            client = self._client_factory()
            snapshot = await client.eval(
                SNAPSHOT_SCRIPT,
                4,
                self.active_key,
                self.queue_key,
                self.deadline_key,
                self.enqueued_key,
            )
            active = int(snapshot[0])
            queue_depth = int(snapshot[1])
            estimate = estimate_queue_wait(
                active=active,
                queue_position=queue_depth + 1,
                service_p50_seconds=self.service_p50_seconds,
                service_p95_seconds=self.service_p95_seconds,
            )
            return result | {
                "ok": True,
                "active": active,
                "queue_depth": queue_depth,
                "oldest_wait_seconds": round(float(snapshot[2]) / 1000, 3),
                "next_queue_position": estimate["queue_position"],
                "estimated_wait_seconds": {
                    "p50": estimate["eta_p50_seconds"],
                    "p95": estimate["eta_p95_seconds"],
                },
            }
        except Exception as exc:
            return result | {
                "ok": False,
                "active": None,
                "queue_depth": None,
                "oldest_wait_seconds": None,
                "next_queue_position": None,
                "estimated_wait_seconds": {"p50": None, "p95": None},
                "message": type(exc).__name__,
            }
        finally:
            if client is not None:
                await self._close_client(client)

    async def _remove_queued(self, client: RedisClient, token: str) -> None:
        try:
            await client.eval(REMOVE_QUEUED_SCRIPT, 3, self.queue_key, self.deadline_key, self.enqueued_key, token)
        except Exception as exc:
            logger.warning("LightTTS admission queue cleanup failed error_type=%s", type(exc).__name__)

    async def _renew(self, lease: LightTTSAdmissionLease, lease_ms: int) -> None:
        interval = max(0.05, min(10.0, lease_ms / 3000))
        while True:
            await asyncio.sleep(interval)
            try:
                renewed = await lease.client.eval(RENEW_SCRIPT, 1, self.active_key, lease.token, lease_ms)  # type: ignore[union-attr]
            except Exception as exc:
                logger.error("LightTTS admission lease renewal failed error_type=%s", type(exc).__name__)
                self.metrics.record("lease_lost")
                lease._lost.set()
                return
            if int(renewed) != 1:
                logger.error("LightTTS admission lease ownership lost")
                self.metrics.record("lease_lost")
                lease._lost.set()
                return

    async def _recover_uncertain_acquire(self, token: str, lease_ms: int) -> LightTTSAdmissionLease | None:
        """Adopt a lease when Redis committed SET but its reply was lost."""
        recovery_client: RedisClient | None = None
        try:
            recovery_client = self._client_factory()
            owns_active = await recovery_client.eval(
                RECOVER_ACQUIRE_SCRIPT,
                4,
                self.active_key,
                self.queue_key,
                self.deadline_key,
                self.enqueued_key,
                token,
                lease_ms,
            )
        except Exception as exc:
            if recovery_client is not None:
                await self._close_client(recovery_client)
            logger.error("LightTTS uncertain acquire recovery failed error_type=%s", type(exc).__name__)
            return None
        if int(owns_active) != 1:
            await self._close_client(recovery_client)
            return None
        self.metrics.record("acquire")
        lease = LightTTSAdmissionLease(gate=self, token=token, client=recovery_client)
        lease._renew_task = asyncio.create_task(self._renew(lease, lease_ms))
        logger.warning("LightTTS admission recovered committed acquire after response loss")
        return lease

    async def acquire(
        self,
        should_cancel: CancelCallback | None = None,
        *,
        deadline_monotonic: float | None = None,
    ) -> LightTTSAdmissionLease:
        max_pending, queue_timeout, lease_seconds = self._limits()
        if deadline_monotonic is not None:
            remaining = deadline_monotonic - asyncio.get_running_loop().time()
            if remaining <= 0:
                self.metrics.record("timeout")
                raise LightTTSAdmissionQueueTimeout("LightTTS whole-job deadline expired before enqueue")
            queue_timeout = min(queue_timeout, remaining)
        queue_timeout_ms = max(1, int(queue_timeout * 1000))
        lease_ms = max(1, int(lease_seconds * 1000))
        sequence_ttl_ms = max(queue_timeout_ms * 2, lease_ms * 2)
        token = uuid.uuid4().hex
        client: RedisClient | None = None
        try:
            client = self._client_factory()
            enqueued = await client.eval(
                ENQUEUE_SCRIPT,
                5,
                self.active_key,
                self.queue_key,
                self.deadline_key,
                self.sequence_key,
                self.enqueued_key,
                token,
                max_pending,
                queue_timeout_ms,
                sequence_ttl_ms,
            )
        except Exception as exc:
            if client is not None:
                await self._close_client(client)
            if self._production_fail_closed():
                self.metrics.record("reject")
                raise LightTTSAdmissionUnavailable("Redis admission gate is unavailable") from exc
            logger.warning("LightTTS admission unavailable; non-production request uses local scheduler error_type=%s", type(exc).__name__)
            return LightTTSAdmissionLease(gate=self, token=token, client=None, bypassed=True)
        if int(enqueued[0]) != 1:
            self.metrics.record("reject")
            await self._close_client(client)
            raise LightTTSAdmissionQueueFull("LightTTS admission queue is full")
        self.metrics.record("enqueue")

        try:
            while True:
                if await _cancel_requested(should_cancel):
                    await self._remove_queued(client, token)
                    self.metrics.record("cancel")
                    raise LightTTSAdmissionCancelled("LightTTS queued request was cancelled")
                try:
                    acquired = await client.eval(
                        ACQUIRE_SCRIPT,
                        4,
                        self.active_key,
                        self.queue_key,
                        self.deadline_key,
                        self.enqueued_key,
                        token,
                        lease_ms,
                    )
                except Exception as exc:
                    recovered = await self._recover_uncertain_acquire(token, lease_ms)
                    await self._close_client(client)
                    if recovered is not None:
                        return recovered
                    if self._production_fail_closed():
                        self.metrics.record("reject")
                        raise LightTTSAdmissionUnavailable("Redis admission gate is unavailable") from exc
                    logger.warning(
                        "LightTTS admission wait failed; non-production request uses local scheduler error_type=%s",
                        type(exc).__name__,
                    )
                    return LightTTSAdmissionLease(gate=self, token=token, client=None, bypassed=True)
                status = int(acquired[0])
                if status == 1:
                    self.metrics.record("acquire")
                    lease = LightTTSAdmissionLease(gate=self, token=token, client=client)
                    lease._renew_task = asyncio.create_task(self._renew(lease, lease_ms))
                    return lease
                if status == -1:
                    self.metrics.record("timeout")
                    raise LightTTSAdmissionQueueTimeout("LightTTS admission queue deadline expired")
                await asyncio.sleep(self.poll_seconds)
        except BaseException:
            await self._remove_queued(client, token)
            await self._close_client(client)
            raise


lighttts_admission_gate = RedisLightTTSAdmissionGate()
