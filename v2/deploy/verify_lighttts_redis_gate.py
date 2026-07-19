#!/usr/bin/env python3
"""Exercise the LightTTS global gate against a real Redis without calling TTS.

The verifier uses a unique, disposable key prefix.  It is safe to run as a
pre-enable shadow check because it never calls the LightTTS HTTP endpoint and
never reads or writes match data.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import multiprocessing
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import redis.asyncio as redis

API_ROOT = Path(__file__).resolve().parents[1] / "apps" / "api"
sys.path.insert(0, str(API_ROOT))

from app.core.config import settings  # noqa: E402
from app.services.lighttts_admission import (  # noqa: E402
    ACQUIRE_SCRIPT,
    LightTTSAdmissionCancelled,
    LightTTSAdmissionQueueFull,
    LightTTSAdmissionQueueTimeout,
    LightTTSAdmissionUnavailable,
    RedisLightTTSAdmissionGate,
)

MAX_SCRIPT = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
local maximum = tonumber(redis.call('GET', KEYS[2]) or '0')
if current > maximum then
    redis.call('SET', KEYS[2], current)
    maximum = current
end
return maximum
"""


def client_factory(redis_url: str):
    return lambda: redis.from_url(redis_url, decode_responses=True)


class PostCommitResponseLossClient:
    def __init__(self, client: Any) -> None:
        self.client = client
        self.injected = False

    async def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> Any:
        result = await self.client.eval(script, numkeys, *keys_and_args)
        if script == ACQUIRE_SCRIPT and not self.injected:
            self.injected = True
            raise ConnectionError("injected response loss after ACQUIRE commit")
        return result

    async def aclose(self) -> None:
        await self.client.aclose()


def process_worker(redis_url: str, prefix: str, start: Any, results: Any, index: int) -> None:
    async def run() -> None:
        gate = RedisLightTTSAdmissionGate(
            client_factory=client_factory(redis_url),
            key_prefix=prefix,
            max_pending=16,
            queue_timeout_seconds=10,
            lease_seconds=2,
            poll_seconds=0.02,
        )
        await asyncio.to_thread(start.wait)
        lease = await gate.acquire()
        client = redis.from_url(redis_url, decode_responses=True)
        current_key = f"{prefix}:shadow-current"
        maximum_key = f"{prefix}:shadow-maximum"
        try:
            current = int(await client.incr(current_key))
            maximum = int(await client.eval(MAX_SCRIPT, 2, current_key, maximum_key))
            await asyncio.sleep(0.08)
            results.put({"index": index, "current": current, "maximum": maximum, "ok": current == 1})
        finally:
            await client.decr(current_key)
            await client.aclose()
            await lease.release()

    try:
        asyncio.run(run())
    except BaseException as exc:
        results.put({"index": index, "ok": False, "error": type(exc).__name__})


async def wait_for_queue(gate: RedisLightTTSAdmissionGate, minimum: int = 1) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + 2
    while asyncio.get_running_loop().time() < deadline:
        snapshot = await gate.status_snapshot()
        if snapshot.get("queue_depth", 0) >= minimum:
            return snapshot
        await asyncio.sleep(0.02)
    raise RuntimeError("timed out waiting for Redis admission queue")


async def verify_async(redis_url: str, prefix: str) -> dict[str, Any]:
    client = redis.from_url(redis_url, decode_responses=True)
    await asyncio.wait_for(client.ping(), timeout=2)
    gate = RedisLightTTSAdmissionGate(
        client_factory=client_factory(redis_url),
        key_prefix=prefix,
        max_pending=2,
        queue_timeout_seconds=5,
        lease_seconds=0.3,
        poll_seconds=0.02,
    )
    original_enabled = settings.lighttts_global_gate_enabled
    original_env = settings.app_env
    try:
        settings.lighttts_global_gate_enabled = True

        burst_rounds: list[dict[str, int]] = []
        for round_index in range(3):
            burst_release = asyncio.Event()

            async def burst_worker() -> str:
                try:
                    lease = await gate.acquire()
                except LightTTSAdmissionQueueFull:
                    return "rejected"
                try:
                    await burst_release.wait()
                    return "accepted"
                finally:
                    await lease.release()

            burst_tasks = [asyncio.create_task(burst_worker()) for _ in range(4)]
            burst_deadline = asyncio.get_running_loop().time() + 2
            while True:
                burst_snapshot = await gate.status_snapshot()
                if burst_snapshot["active"] == 1 and burst_snapshot["queue_depth"] == 2:
                    break
                if asyncio.get_running_loop().time() >= burst_deadline:
                    raise AssertionError(f"unexpected burst occupancy in round {round_index + 1}: {burst_snapshot}")
                await asyncio.sleep(0.02)
            reject_deadline = asyncio.get_running_loop().time() + 2
            while not any(task.done() for task in burst_tasks) and asyncio.get_running_loop().time() < reject_deadline:
                await asyncio.sleep(0.02)
            burst_release.set()
            burst_results = await asyncio.gather(*burst_tasks)
            if burst_results.count("accepted") != 3 or burst_results.count("rejected") != 1:
                raise AssertionError(f"idle burst round {round_index + 1} failed: {burst_results}")
            burst_final = await gate.status_snapshot()
            if burst_final["active"] != 0 or burst_final["queue_depth"] != 0:
                raise AssertionError(f"burst round {round_index + 1} left state behind: {burst_final}")
            burst_rounds.append(
                {
                    "round": round_index + 1,
                    "jobs": 4,
                    "accepted": burst_results.count("accepted"),
                    "rejected": burst_results.count("rejected"),
                    "maximum_active": burst_snapshot["active"],
                    "maximum_queue_depth": burst_snapshot["queue_depth"],
                    "final_active": burst_final["active"],
                    "final_queue_depth": burst_final["queue_depth"],
                }
            )

        holder = await gate.acquire()
        cancelled = False
        waiter = asyncio.create_task(gate.acquire(lambda: cancelled))
        queued_snapshot = await wait_for_queue(gate)
        cancelled = True
        try:
            await waiter
            raise AssertionError("queued cancellation unexpectedly acquired the gate")
        except LightTTSAdmissionCancelled:
            pass
        after_cancel = await gate.status_snapshot()
        if after_cancel["queue_depth"] != 0:
            raise AssertionError("cancelled waiter remained in Redis queue")

        deadline = asyncio.get_running_loop().time() + 0.08
        try:
            await gate.acquire(deadline_monotonic=deadline)
            raise AssertionError("whole-job deadline unexpectedly acquired the busy gate")
        except LightTTSAdmissionQueueTimeout:
            pass
        await holder.release()

        lost = await gate.acquire()
        await client.delete(gate.active_key)
        await asyncio.wait_for(lost._lost.wait(), timeout=1)
        await lost.release()
        if gate.metrics.snapshot()["events"]["lease_lost"] != 1:
            raise AssertionError("lease loss counter was not recorded")

        response_loss_clients = 0

        def response_loss_factory():
            nonlocal response_loss_clients
            response_loss_clients += 1
            base = redis.from_url(redis_url, decode_responses=True)
            return PostCommitResponseLossClient(base) if response_loss_clients == 1 else base

        response_loss_gate = RedisLightTTSAdmissionGate(
            client_factory=response_loss_factory,
            key_prefix=f"{prefix}:response-loss",
            max_pending=2,
            queue_timeout_seconds=2,
            lease_seconds=1,
            poll_seconds=0.02,
        )
        recovered = await response_loss_gate.acquire()
        recovered_token = await client.get(response_loss_gate.active_key)
        if recovered_token != recovered.token:
            raise AssertionError("post-commit response loss did not recover the same token")
        await recovered.release()
        if await client.exists(response_loss_gate.active_key):
            raise AssertionError("recovered acquire left a ghost active lock")

        settings.app_env = "production"
        unavailable = RedisLightTTSAdmissionGate(
            client_factory=client_factory("redis://127.0.0.1:1/15"),
            key_prefix=f"{prefix}:unavailable",
            poll_seconds=0.01,
        )
        try:
            await unavailable.acquire()
            raise AssertionError("production Redis failure bypassed the global gate")
        except LightTTSAdmissionUnavailable:
            pass

        return {
            "redis_reachable": True,
            "queued_cancel": True,
            "whole_job_queue_deadline": True,
            "active_lease_lost": True,
            "post_commit_acquire_response_loss_recovered": True,
            "production_redis_failure_fail_closed": True,
            "idle_burst_active_plus_pending": burst_rounds,
            "queued_snapshot": {
                "active": queued_snapshot["active"],
                "queue_depth": queued_snapshot["queue_depth"],
                "oldest_wait_seconds": queued_snapshot["oldest_wait_seconds"],
            },
            "events": gate.metrics.snapshot()["events"],
        }
    finally:
        settings.lighttts_global_gate_enabled = original_enabled
        settings.app_env = original_env
        keys = [key async for key in client.scan_iter(match=f"{prefix}*")]
        if keys:
            await client.delete(*keys)
        await client.aclose()


def verify_processes(redis_url: str, prefix: str, workers: int) -> dict[str, Any]:
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(target=process_worker, args=(redis_url, prefix, start, results, index))
        for index in range(workers)
    ]
    for process in processes:
        process.start()
    start.set()
    try:
        records = [results.get(timeout=20) for _ in processes]
        if not all(record.get("ok") for record in records):
            raise RuntimeError(f"multi-process gate verification failed: {records}")
        maximum = max(int(record["maximum"]) for record in records)
        if maximum != 1:
            raise RuntimeError(f"global LightTTS active count exceeded one: {maximum}")
        return {"workers": workers, "completed": len(records), "maximum_active": maximum}
    finally:
        for process in processes:
            process.join(timeout=5)
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)
        asyncio.run(cleanup_prefix(redis_url, prefix))


async def cleanup_prefix(redis_url: str, prefix: str) -> None:
    client = redis.from_url(redis_url, decode_responses=True)
    try:
        keys = [key async for key in client.scan_iter(match=f"{prefix}*")]
        if keys:
            await client.delete(*keys)
    finally:
        await client.aclose()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--redis-url", default=settings.redis_url)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not 2 <= args.workers <= 16:
        raise SystemExit("--workers must be between 2 and 16")
    if settings.lighttts_global_gate_lease_seconds < settings.lighttts_job_timeout_seconds:
        raise SystemExit("LightTTS lease must not be shorter than the whole-job timeout")
    run_id = f"jixia:{{lighttts-shadow-{int(time.time())}-{uuid.uuid4().hex[:8]}}}"
    async_result = asyncio.run(verify_async(args.redis_url, f"{run_id}:async"))
    process_result = verify_processes(args.redis_url, f"{run_id}:process", args.workers)
    payload = {
        "ok": True,
        "shadow_only": True,
        "lighttts_http_called": False,
        "feature_flag_default": settings.lighttts_global_gate_enabled,
        "configured_limits": {
            "max_active": settings.lighttts_max_active,
            "max_pending": settings.lighttts_global_gate_max_pending,
            "queue_timeout_seconds": settings.lighttts_global_gate_queue_timeout_seconds,
            "job_timeout_seconds": settings.lighttts_job_timeout_seconds,
            "lease_seconds": settings.lighttts_global_gate_lease_seconds,
            "lease_covers_job": settings.lighttts_global_gate_lease_seconds >= settings.lighttts_job_timeout_seconds,
        },
        "async_checks": async_result,
        "multi_process": process_result,
    }
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
