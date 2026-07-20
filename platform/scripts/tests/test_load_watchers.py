from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from argparse import Namespace
from pathlib import Path

import pytest
import websockets

SCRIPT = Path(__file__).resolve().parents[1] / "load_watchers.py"
SPEC = importlib.util.spec_from_file_location("load_watchers", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def args(**overrides) -> Namespace:
    values = {
        "base_url": "http://127.0.0.1:12340",
        "clients": 500,
        "duration": 10.0,
        "handshake_concurrency": 20,
        "startup_timeout": 180.0,
        "allow_public_load": False,
    }
    values.update(overrides)
    return Namespace(**values)


def test_remote_load_requires_explicit_operator_confirmation() -> None:
    with pytest.raises(SystemExit, match="--allow-public-load"):
        MODULE.validate_args(args(base_url="https://debate.example"))
    MODULE.validate_args(args(base_url="https://debate.example", allow_public_load=True))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("clients", 0, "--clients"),
        ("clients", 2001, "--clients"),
        ("handshake_concurrency", 0, "--handshake-concurrency"),
        ("handshake_concurrency", 101, "--handshake-concurrency"),
        ("duration", 0, "--duration"),
        ("duration", 301, "--duration"),
        ("startup_timeout", 0, "--startup-timeout"),
        ("startup_timeout", 601, "--startup-timeout"),
    ],
)
def test_load_bounds_fail_before_opening_connections(field: str, value: int, message: str) -> None:
    with pytest.raises(SystemExit, match=message):
        MODULE.validate_args(args(**{field: value}))


def test_metrics_distinguish_total_handshakes_from_simultaneously_held_connections() -> None:
    metrics = MODULE.Metrics(latencies_ms=[1.0, 2.0, 3.0], peak_active=3)
    summary = metrics.summary(3)
    assert summary["connected"] == 3
    assert summary["peak_connected"] == 3


def test_staged_handshakes_release_slots_while_all_connections_remain_held() -> None:
    async def scenario() -> None:
        async def handler(connection) -> None:
            code = connection.request.path.rsplit("/", 1)[-1]
            await connection.send(json.dumps({"type": "snapshot", "room": {"code": code}}))
            async for _message in connection:
                pass

        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            metrics = MODULE.Metrics(expected=5)
            all_attempted = asyncio.Event()
            release = asyncio.Event()
            slots = asyncio.Semaphore(2)
            tasks = [
                asyncio.create_task(
                    MODULE.watcher(
                        index,
                        f"ws://127.0.0.1:{port}",
                        ["123456"],
                        False,
                        metrics,
                        slots,
                        all_attempted,
                        release,
                    )
                )
                for index in range(5)
            ]
            await asyncio.wait_for(all_attempted.wait(), timeout=5)
            assert metrics.errors == []
            assert metrics.active == 5
            assert metrics.peak_active == 5
            release.set()
            await asyncio.gather(*tasks)
            assert metrics.active == 0

    asyncio.run(scenario())


def test_connection_that_drops_during_hold_is_counted_as_failure() -> None:
    async def scenario() -> None:
        async def handler(connection) -> None:
            await connection.send(json.dumps({"type": "snapshot", "room": {"code": "123456"}}))

        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            metrics = MODULE.Metrics(expected=1)
            all_attempted = asyncio.Event()
            release = asyncio.Event()
            task = asyncio.create_task(
                MODULE.watcher(
                    0,
                    f"ws://127.0.0.1:{port}",
                    ["123456"],
                    False,
                    metrics,
                    asyncio.Semaphore(1),
                    all_attempted,
                    release,
                )
            )
            await asyncio.wait_for(all_attempted.wait(), timeout=5)
            await asyncio.wait_for(task, timeout=5)
            assert len(metrics.errors) == 1
            assert "disconnected while held" in metrics.errors[0]

    asyncio.run(scenario())
