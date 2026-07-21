from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
import uuid
from base64 import urlsafe_b64decode, urlsafe_b64encode
from collections import defaultdict
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import redis.asyncio as redis
from app.core.config import settings

logger = logging.getLogger(__name__)
SUBSCRIBER_QUEUE_SIZE = 16


def _bounded_env_number(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return min(maximum, max(minimum, value))


PRESENCE_LEASE_SECONDS = int(_bounded_env_number("PRESENCE_LEASE_SECONDS", 60, 30, 300))
PRESENCE_REDIS_TIMEOUT_SECONDS = _bounded_env_number("PRESENCE_REDIS_TIMEOUT_SECONDS", 1, 0.1, 5)
PRESENCE_KEY_PREFIX = "jixia:presence:leases:"
PRESENCE_INDEX_KEY = "jixia:presence:index"
SPECTATOR_LIMIT = 5
SPECTATOR_LEASE_SECONDS = PRESENCE_LEASE_SECONDS
SPECTATOR_KEY_PREFIX = "jixia:spectators:leases:"
SPECTATOR_GLOBAL_KEY = f"{SPECTATOR_KEY_PREFIX}global"

_PRESENCE_JOIN_SCRIPT = """
local now_ms = tonumber(ARGV[1])
local expires_ms = tonumber(ARGV[2])
local ttl_ms = tonumber(ARGV[3])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now_ms)
local before = redis.call('ZCARD', KEYS[1])
redis.call('ZADD', KEYS[1], expires_ms, ARGV[4])
redis.call('PEXPIRE', KEYS[1], ttl_ms)
local latest = redis.call('ZRANGE', KEYS[1], -1, -1, 'WITHSCORES')
redis.call('ZADD', KEYS[2], tonumber(latest[2]), ARGV[5])
return before == 0 and 1 or 0
"""

_PRESENCE_LEAVE_SCRIPT = """
local now_ms = tonumber(ARGV[1])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now_ms)
local removed = redis.call('ZREM', KEYS[1], ARGV[2])
local remaining = redis.call('ZCARD', KEYS[1])
if remaining == 0 then
    redis.call('DEL', KEYS[1])
    redis.call('ZREM', KEYS[2], ARGV[3])
else
    local latest = redis.call('ZRANGE', KEYS[1], -1, -1, 'WITHSCORES')
    redis.call('ZADD', KEYS[2], tonumber(latest[2]), ARGV[3])
    redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[4]))
end
return {removed, remaining}
"""

_PRESENCE_REFRESH_SCRIPT = """
local now_ms = tonumber(ARGV[1])
local expires_ms = tonumber(ARGV[2])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now_ms)
if not redis.call('ZSCORE', KEYS[1], ARGV[3]) then
    return 0
end
redis.call('ZADD', KEYS[1], expires_ms, ARGV[3])
redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[4]))
local latest = redis.call('ZRANGE', KEYS[1], -1, -1, 'WITHSCORES')
redis.call('ZADD', KEYS[2], tonumber(latest[2]), ARGV[5])
return 1
"""

_PRESENCE_REAP_SCRIPT = """
local candidates = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[1], 'LIMIT', 0, tonumber(ARGV[2]))
local expired = {}
for _, member in ipairs(candidates) do
    local lease_key = ARGV[3] .. member
    redis.call('ZREMRANGEBYSCORE', lease_key, '-inf', ARGV[1])
    if redis.call('ZCARD', lease_key) == 0 then
        redis.call('DEL', lease_key)
        redis.call('ZREM', KEYS[1], member)
        table.insert(expired, member)
    else
        local latest = redis.call('ZRANGE', lease_key, -1, -1, 'WITHSCORES')
        redis.call('ZADD', KEYS[1], tonumber(latest[2]), member)
    end
end
return expired
"""

_PRESENCE_ACTIVE_SCRIPT = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
return redis.call('ZCARD', KEYS[1]) > 0 and 1 or 0
"""

_SPECTATOR_JOIN_SCRIPT = """
local now_ms = tonumber(ARGV[1])
local expires_ms = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now_ms)
if redis.call('ZSCORE', KEYS[1], ARGV[4]) then
    redis.call('ZADD', KEYS[1], expires_ms, ARGV[4])
    redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[5]))
    return 1
end
if redis.call('ZCARD', KEYS[1]) >= limit then
    return 0
end
redis.call('ZADD', KEYS[1], expires_ms, ARGV[4])
redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[5]))
return 1
"""

_SPECTATOR_LEAVE_SCRIPT = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
local removed = redis.call('ZREM', KEYS[1], ARGV[2])
if redis.call('ZCARD', KEYS[1]) == 0 then
    redis.call('DEL', KEYS[1])
end
return removed
"""

_SPECTATOR_REFRESH_SCRIPT = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
if not redis.call('ZSCORE', KEYS[1], ARGV[3]) then
    return 0
end
redis.call('ZADD', KEYS[1], ARGV[2], ARGV[3])
redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[4]))
return 1
"""

@dataclass
class _LoopState:
    redis: redis.Redis | None = None
    redis_checked: bool = False
    redis_retry_at: float = 0.0
    listeners: dict[str, asyncio.Task] = field(default_factory=dict)
    listener_ready: dict[str, asyncio.Event] = field(default_factory=dict)
    client_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass(frozen=True)
class _Subscriber:
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue


@dataclass(frozen=True)
class _AudioAbortWaiter:
    loop: asyncio.AbstractEventLoop
    event: asyncio.Event


class AudioStreamAbortRegistry:
    """Process-local fast path for stopping an active audio sender.

    The database generation remains the cross-process authority. This registry
    removes the polling window when the match engine and audio websocket live in
    the same API process, and is safe when tests or workers use different event
    loops.
    """

    def __init__(self) -> None:
        self._waiters: dict[tuple[str, str, str], set[_AudioAbortWaiter]] = defaultdict(set)
        self._lock = threading.RLock()

    def register(self, room_code: str, speech_id: str, generation: str) -> _AudioAbortWaiter:
        waiter = _AudioAbortWaiter(loop=asyncio.get_running_loop(), event=asyncio.Event())
        with self._lock:
            self._waiters[(room_code, speech_id, generation)].add(waiter)
        return waiter

    def unregister(self, room_code: str, speech_id: str, generation: str, waiter: _AudioAbortWaiter) -> None:
        key = (room_code, speech_id, generation)
        with self._lock:
            waiters = self._waiters.get(key)
            if not waiters:
                return
            waiters.discard(waiter)
            if not waiters:
                self._waiters.pop(key, None)

    def abort(self, room_code: str, speech_id: str, generation: str) -> int:
        key = (room_code, speech_id, generation)
        with self._lock:
            waiters = tuple(self._waiters.get(key, ()))
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        delivered = 0
        for waiter in waiters:
            if waiter.loop.is_closed():
                continue
            try:
                if waiter.loop is current_loop:
                    waiter.event.set()
                else:
                    waiter.loop.call_soon_threadsafe(waiter.event.set)
                delivered += 1
            except RuntimeError:
                logger.debug("audio abort waiter loop already closed: %s", key, exc_info=True)
        return delivered


class RoomHub:
    """Room-scoped fanout with independent async resources for every event loop.

    The API, engine tests and maintenance tools may use the same module from
    different event loops. Redis clients, locks, queues and listener tasks must
    never cross those loop boundaries.
    """

    def __init__(self, *, redis_enabled: bool = True) -> None:
        self._redis_enabled = redis_enabled
        self._origin = uuid.uuid4().hex
        self._states: dict[asyncio.AbstractEventLoop, _LoopState] = {}
        self._local: dict[str, set[_Subscriber]] = defaultdict(set)
        self._presence: dict[tuple[str, str, str], int] = defaultdict(int)
        self._presence_leases: dict[tuple[str, str, str], set[str]] = defaultdict(set)
        self._spectator_leases: dict[str, set[str]] = defaultdict(set)
        self._states_lock = threading.RLock()

    def _state(self) -> _LoopState:
        loop = asyncio.get_running_loop()
        with self._states_lock:
            for stale_loop in [item for item in self._states if item.is_closed()]:
                self._states.pop(stale_loop, None)
            return self._states.setdefault(loop, _LoopState())

    @staticmethod
    def _presence_member(room_code: str, seat_key: str, user_id: str) -> str:
        payload = json.dumps([room_code, seat_key, user_id], separators=(",", ":")).encode()
        return urlsafe_b64encode(payload).decode().rstrip("=")

    @staticmethod
    def _decode_presence_member(member: str) -> tuple[str, str, str] | None:
        try:
            padded = member + "=" * (-len(member) % 4)
            values = json.loads(urlsafe_b64decode(padded).decode())
            if not isinstance(values, list) or len(values) != 3 or not all(isinstance(value, str) for value in values):
                return None
            return values[0], values[1], values[2]
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return None

    @staticmethod
    def _presence_lease_key(member: str) -> str:
        return f"{PRESENCE_KEY_PREFIX}{member}"

    async def _presence_eval(self, script: str, keys: list[str], args: list[Any]) -> Any | None:
        state = self._state()
        client = await self._client()
        if not client:
            return None
        try:
            return await asyncio.wait_for(
                client.eval(script, len(keys), *keys, *args),
                timeout=PRESENCE_REDIS_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            # A saturated Redis can miss this deliberately short realtime
            # deadline while the shared client and its Pub/Sub connections are
            # still healthy. Closing the whole client here disconnects every
            # room subscription on this worker and turns a lease delay into a
            # multi-room event stall. The caller applies its local failover and
            # the next heartbeat retries normally.
            logger.warning("room Redis presence operation timed out")
            return None
        except Exception as exc:
            logger.warning("room Redis presence operation failed: %s", exc)
            if state.redis is client:
                state.redis = None
                state.redis_retry_at = time.monotonic() + 5
                await self._close_client(client)
            return None

    async def presence_join(
        self,
        room_code: str,
        seat_key: str,
        user_id: str,
        *,
        connection_id: str | None = None,
    ) -> bool:
        key = (room_code, seat_key, user_id)
        with self._states_lock:
            if connection_id is None:
                self._presence[key] += 1
                local_first = self._presence[key] == 1
            else:
                leases = self._presence_leases[key]
                local_first = not leases
                leases.add(connection_id)
        if connection_id is None:
            return local_first
        member = self._presence_member(*key)
        now_ms = int(time.time() * 1000)
        lease_ms = PRESENCE_LEASE_SECONDS * 1000
        result = await self._presence_eval(
            _PRESENCE_JOIN_SCRIPT,
            [self._presence_lease_key(member), PRESENCE_INDEX_KEY],
            [now_ms, now_ms + lease_ms, lease_ms * 2, connection_id, member],
        )
        return local_first if result is None else bool(result)

    async def presence_leave(
        self,
        room_code: str,
        seat_key: str,
        user_id: str,
        *,
        connection_id: str | None = None,
    ) -> bool:
        key = (room_code, seat_key, user_id)
        with self._states_lock:
            if connection_id is None:
                count = self._presence.get(key, 0)
                if count <= 1:
                    self._presence.pop(key, None)
                    return True
                self._presence[key] = count - 1
                return False
            leases = self._presence_leases.get(key)
            existed_locally = bool(leases and connection_id in leases)
            if leases:
                leases.discard(connection_id)
                if not leases:
                    self._presence_leases.pop(key, None)
            local_last = existed_locally and key not in self._presence_leases
        member = self._presence_member(*key)
        result = await self._presence_eval(
            _PRESENCE_LEAVE_SCRIPT,
            [self._presence_lease_key(member), PRESENCE_INDEX_KEY],
            [int(time.time() * 1000), connection_id, member, PRESENCE_LEASE_SECONDS * 2000],
        )
        if result is None:
            return local_last
        removed, remaining = (int(result[0]), int(result[1]))
        if remaining:
            return False
        # A zero removal can happen when this connection joined during a Redis
        # outage. The process-local lease is the safest available fallback.
        return bool(removed) or local_last

    async def presence_refresh(self, room_code: str, seat_key: str, user_id: str, *, connection_id: str) -> bool:
        key = (room_code, seat_key, user_id)
        with self._states_lock:
            locally_active = connection_id in self._presence_leases.get(key, set())
        if not locally_active:
            return False
        member = self._presence_member(*key)
        now_ms = int(time.time() * 1000)
        lease_ms = PRESENCE_LEASE_SECONDS * 1000
        result = await self._presence_eval(
            _PRESENCE_REFRESH_SCRIPT,
            [self._presence_lease_key(member), PRESENCE_INDEX_KEY],
            [now_ms, now_ms + lease_ms, connection_id, lease_ms * 2, member],
        )
        if result is None:
            return True
        if result:
            return True
        # Redis may have restarted or the lease may have expired during an
        # event-loop stall. Re-register the still-live socket atomically.
        await self.presence_join(room_code, seat_key, user_id, connection_id=connection_id)
        return True

    async def reap_expired_presence(self, *, limit: int = 200) -> list[tuple[str, str, str]]:
        result = await self._presence_eval(
            _PRESENCE_REAP_SCRIPT,
            [PRESENCE_INDEX_KEY],
            [int(time.time() * 1000), limit, PRESENCE_KEY_PREFIX],
        )
        if result is None:
            return []
        decoded = [self._decode_presence_member(str(member)) for member in result]
        return [identity for identity in decoded if identity is not None]

    async def presence_active(self, room_code: str, seat_key: str, user_id: str) -> bool | None:
        member = self._presence_member(room_code, seat_key, user_id)
        result = await self._presence_eval(
            _PRESENCE_ACTIVE_SCRIPT,
            [self._presence_lease_key(member)],
            [int(time.time() * 1000)],
        )
        return None if result is None else bool(result)

    async def retry_expired_presence(
        self,
        room_code: str,
        seat_key: str,
        user_id: str,
        *,
        delay_seconds: int = 5,
    ) -> None:
        """Put a claimed expiry back when its database transition was busy."""
        client = await self._client()
        if not client:
            return
        member = self._presence_member(room_code, seat_key, user_id)
        try:
            await asyncio.wait_for(
                client.zadd(PRESENCE_INDEX_KEY, {member: int(time.time() * 1000) + delay_seconds * 1000}),
                timeout=PRESENCE_REDIS_TIMEOUT_SECONDS,
            )
        except Exception:
            logger.warning("failed to reschedule presence expiry: room=%s seat=%s", room_code, seat_key)

    @staticmethod
    def _spectator_key(room_code: str) -> str:
        del room_code
        return SPECTATOR_GLOBAL_KEY

    def _local_spectator_count(self) -> int:
        return sum(len(leases) for leases in self._spectator_leases.values())

    async def spectator_join(self, room_code: str, *, connection_id: str) -> bool:
        """Atomically reserve one of a room's public spectator slots.

        Production fails closed when Redis is unavailable so multiple API
        workers can never independently exceed the room-wide limit. Explicit
        Redis-free test/local hubs retain a process-local bounded fallback.
        """
        with self._states_lock:
            local = self._spectator_leases[room_code]
            if connection_id in local:
                return True
            if not self._redis_enabled:
                if self._local_spectator_count() >= SPECTATOR_LIMIT:
                    return False
                local.add(connection_id)
                return True

        now_ms = int(time.time() * 1000)
        lease_ms = SPECTATOR_LEASE_SECONDS * 1000
        result = await self._presence_eval(
            _SPECTATOR_JOIN_SCRIPT,
            [self._spectator_key(room_code)],
            [now_ms, now_ms + lease_ms, SPECTATOR_LIMIT, connection_id, lease_ms * 2],
        )
        if result is None and settings.app_env == "test":
            with self._states_lock:
                local = self._spectator_leases[room_code]
                if self._local_spectator_count() >= SPECTATOR_LIMIT:
                    return False
                local.add(connection_id)
            return True
        if not result:
            return False
        with self._states_lock:
            self._spectator_leases[room_code].add(connection_id)
        return True

    async def spectator_refresh(self, room_code: str, *, connection_id: str) -> bool:
        with self._states_lock:
            locally_active = connection_id in self._spectator_leases.get(room_code, set())
        if not locally_active:
            return False
        if not self._redis_enabled:
            return True
        now_ms = int(time.time() * 1000)
        lease_ms = SPECTATOR_LEASE_SECONDS * 1000
        result = await self._presence_eval(
            _SPECTATOR_REFRESH_SCRIPT,
            [self._spectator_key(room_code)],
            [now_ms, now_ms + lease_ms, connection_id, lease_ms * 2],
        )
        # This socket was already admitted under the global room limit. A
        # transient Redis timeout must not turn into a false 4429 eviction.
        # Keep the local lease and retry on the next heartbeat.
        if result is None:
            return True
        if result:
            return True
        # Redis may have restarted or the lease may have expired during an
        # event-loop stall. Re-register the still-live socket atomically. Do
        # not call spectator_join(): its local fast path would skip Redis.
        rejoined = await self._presence_eval(
            _SPECTATOR_JOIN_SCRIPT,
            [self._spectator_key(room_code)],
            [now_ms, now_ms + lease_ms, SPECTATOR_LIMIT, connection_id, lease_ms * 2],
        )
        # An unavailable Redis is tolerated only for this previously admitted
        # socket. A definite zero means the global room limit is now occupied.
        return True if rejoined is None else bool(rejoined)

    async def spectator_leave(self, room_code: str, *, connection_id: str) -> None:
        with self._states_lock:
            leases = self._spectator_leases.get(room_code)
            if leases:
                leases.discard(connection_id)
                if not leases:
                    self._spectator_leases.pop(room_code, None)
        if not self._redis_enabled:
            return
        await self._presence_eval(
            _SPECTATOR_LEAVE_SCRIPT,
            [self._spectator_key(room_code)],
            [int(time.time() * 1000), connection_id],
        )

    async def _client(self) -> redis.Redis | None:
        if not self._redis_enabled:
            return None
        state = self._state()
        if state.redis:
            return state.redis
        if state.redis_checked and time.monotonic() < state.redis_retry_at:
            return None
        async with state.client_lock:
            if state.redis:
                return state.redis
            if state.redis_checked and time.monotonic() < state.redis_retry_at:
                return None
            state.redis_checked = True
            client: redis.Redis | None = None
            try:
                client = redis.from_url(settings.redis_url, decode_responses=True)
                await asyncio.wait_for(client.ping(), timeout=PRESENCE_REDIS_TIMEOUT_SECONDS)
                state.redis = client
                state.redis_retry_at = 0
            except Exception:
                if client:
                    await self._close_client(client)
                state.redis = None
                state.redis_retry_at = time.monotonic() + 5
            return state.redis

    @staticmethod
    async def _close_client(client: redis.Redis) -> None:
        try:
            await client.aclose()
        except AttributeError:
            await client.close()
        except Exception:
            logger.debug("Redis client was already closed", exc_info=True)

    async def publish(self, room_code: str, message: dict[str, Any]) -> None:
        state = self._state()
        # Deliver locally before Redis subscription setup can race the event.
        await self._fanout(room_code, message)
        client = await self._client()
        redis_message = dict(message)
        redis_message["_room_hub_origin"] = self._origin
        payload = json.dumps(redis_message, ensure_ascii=False, default=str)
        if client:
            try:
                await client.publish(f"jixia:room:{room_code}", payload)
                return
            except Exception as exc:
                logger.warning("room Redis publish failed: %s error=%s", room_code, exc)
                if state.redis is client:
                    state.redis = None
                    state.redis_retry_at = time.monotonic() + 5
                    await self._close_client(client)

    async def heartbeat(self, service: str, *, ttl_seconds: int = 20) -> bool:
        state = self._state()
        client = await self._client()
        if not client:
            return False
        try:
            await client.set(f"jixia:heartbeat:{service}", str(time.time()), ex=ttl_seconds)
            return True
        except Exception as exc:
            logger.warning("service heartbeat failed: %s error=%s", service, exc)
            if state.redis is client:
                state.redis = None
                state.redis_retry_at = time.monotonic() + 5
                await self._close_client(client)
            return False

    @staticmethod
    def _queue_message(queue: asyncio.Queue, message: dict[str, Any]) -> None:
        try:
            queue.put_nowait(message)
        except asyncio.QueueFull:
            # Every outbound item is followed by a fresh authoritative room
            # projection. A slow client benefits from the newest state instead
            # of replaying hundreds of stale snapshots before catching up.
            while True:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            queue.put_nowait(message)

    async def _fanout(
        self,
        room_code: str,
        message: dict[str, Any],
        *,
        target_loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        current_loop = asyncio.get_running_loop()
        with self._states_lock:
            subscribers = [item for item in self._local.get(room_code, set()) if target_loop is None or item.loop is target_loop]
        for subscriber in subscribers:
            try:
                if subscriber.loop is current_loop:
                    self._queue_message(subscriber.queue, message)
                elif not subscriber.loop.is_closed():
                    subscriber.loop.call_soon_threadsafe(self._queue_message, subscriber.queue, message)
            except RuntimeError:
                logger.debug("room subscriber loop already closed: %s", room_code, exc_info=True)

    async def _listen_redis(self, room_code: str, state: _LoopState) -> None:
        while True:
            client = await self._client()
            if not client:
                # There is no cross-process channel to become ready. Unblock
                # local delivery immediately; the listener keeps retrying and
                # upgrades to Redis when it becomes available.
                ready = state.listener_ready.get(room_code)
                if ready:
                    ready.set()
                await asyncio.sleep(1)
                continue
            pubsub = client.pubsub()
            try:
                await pubsub.subscribe(f"jixia:room:{room_code}")
                ready = state.listener_ready.get(room_code)
                if ready:
                    ready.set()
                while True:
                    item = await pubsub.get_message(ignore_subscribe_messages=True, timeout=5)
                    if item and item.get("data"):
                        message = json.loads(item["data"])
                        if not isinstance(message, dict):
                            continue
                        origin = message.pop("_room_hub_origin", None)
                        if origin == self._origin:
                            continue
                        await self._fanout(room_code, message, target_loop=asyncio.get_running_loop())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("room Redis subscription failed: %s error=%s", room_code, exc)
                if state.redis is client:
                    state.redis = None
                    state.redis_retry_at = time.monotonic() + 1
                    await self._close_client(client)
                await asyncio.sleep(1)
            finally:
                try:
                    await pubsub.unsubscribe(f"jixia:room:{room_code}")
                    await pubsub.close()
                except Exception:
                    logger.debug("room Redis subscription was already closed: %s", room_code, exc_info=True)

    async def stream(self, room_code: str, *, initial_sync: bool = False) -> AsyncIterator[dict[str, Any]]:
        state = self._state()
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
        subscriber = _Subscriber(loop=loop, queue=queue)
        with self._states_lock:
            self._local[room_code].add(subscriber)
            if self._redis_enabled and room_code not in state.listeners:
                state.listener_ready[room_code] = asyncio.Event()
                state.listeners[room_code] = asyncio.create_task(
                    self._listen_redis(room_code, state),
                    name=f"jixia-room-events-{room_code}",
                )
            listener_ready = state.listener_ready.get(room_code)
        try:
            # A Redis PUBLISH is not replayable. Wait briefly for the worker's
            # subscription acknowledgement before declaring this stream ready,
            # otherwise a cross-process event can fall between the initial
            # database snapshot and SUBSCRIBE. Redis outages still degrade to
            # the authoritative sync and heartbeat path after the timeout.
            if listener_ready and not listener_ready.is_set():
                try:
                    await asyncio.wait_for(listener_ready.wait(), timeout=2)
                except asyncio.TimeoutError:
                    logger.warning("room Redis subscription readiness timed out: %s", room_code)
            # The room websocket sends its initial database snapshot before it
            # starts consuming this stream.  Register first, then ask the
            # websocket to take one more authoritative snapshot so an event
            # committed in that narrow hand-off window cannot remain invisible
            # until the 20-second heartbeat.
            if initial_sync:
                yield {"type": "_sync"}
            while True:
                try:
                    yield await asyncio.wait_for(queue.get(), timeout=20)
                except asyncio.TimeoutError:
                    yield {"type": "ping"}
        finally:
            listener: asyncio.Task | None = None
            with self._states_lock:
                self._local[room_code].discard(subscriber)
                same_loop_remaining = any(item.loop is loop for item in self._local[room_code])
                if not self._local[room_code]:
                    self._local.pop(room_code, None)
                if not same_loop_remaining:
                    listener = state.listeners.pop(room_code, None)
                    state.listener_ready.pop(room_code, None)
            if listener:
                listener.cancel()
                try:
                    await listener
                except asyncio.CancelledError:
                    pass

    async def close(self) -> None:
        loop = asyncio.get_running_loop()
        with self._states_lock:
            state = self._states.pop(loop, None)
            for room_code in list(self._local):
                self._local[room_code] = {item for item in self._local[room_code] if item.loop is not loop}
                if not self._local[room_code]:
                    self._local.pop(room_code, None)
        if not state:
            return
        listeners = list(state.listeners.values())
        for listener in listeners:
            listener.cancel()
        if listeners:
            await asyncio.gather(*listeners, return_exceptions=True)
        if state.redis:
            await self._close_client(state.redis)

    async def local_subscriber_count(self, room_code: str) -> int:
        with self._states_lock:
            return len(self._local.get(room_code, set()))


room_hub = RoomHub()
audio_stream_aborts = AudioStreamAbortRegistry()
