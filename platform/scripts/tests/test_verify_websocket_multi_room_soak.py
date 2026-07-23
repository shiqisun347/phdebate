from __future__ import annotations

import importlib.util
import inspect
import sys
from argparse import Namespace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps" / "api"))
SCRIPT = ROOT / "scripts" / "verify_websocket_multi_room_soak.py"
SPEC = importlib.util.spec_from_file_location("verify_websocket_multi_room_soak", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def args(**overrides) -> Namespace:
    values = {
        "rooms": 5,
        "clients_per_room": 1,
        "cycles": 3,
        "handshake_concurrency": 50,
        "slow_clients_per_room": 1,
        "hold_seconds": 5,
        "between_cycle_seconds": 0,
        "abrupt_ratio": 0.5,
    }
    values.update(overrides)
    return Namespace(**values)


def test_default_soak_matches_five_rooms_and_five_spectators() -> None:
    MODULE.validate_args(args())
    assert MODULE.MAX_ACTIVE_ROOMS == 5
    assert MODULE.MAX_TOTAL_SPECTATORS == 5


def test_soak_rejects_more_than_five_spectators_across_different_rooms() -> None:
    with pytest.raises(SystemExit, match="combined cannot exceed 5"):
        MODULE.validate_args(args(rooms=2, clients_per_room=3))


def test_abrupt_disconnect_cleanup_waits_past_the_authoritative_redis_lease() -> None:
    default_timeout = inspect.signature(MODULE.wait_for_lease_cleanup).parameters["timeout_seconds"].default
    assert default_timeout > MODULE.SPECTATOR_LEASE_SECONDS


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("rooms", 0, "--rooms"),
        ("rooms", 6, "--rooms"),
        ("clients_per_room", 0, "--clients-per-room"),
        ("clients_per_room", 6, "--clients-per-room"),
        ("cycles", 0, "--cycles"),
        ("cycles", 21, "--cycles"),
        ("handshake_concurrency", 0, "--handshake-concurrency"),
        ("handshake_concurrency", 101, "--handshake-concurrency"),
        ("slow_clients_per_room", 6, "--slow-clients-per-room"),
        ("hold_seconds", 301, "--hold-seconds"),
        ("between_cycle_seconds", 301, "--between-cycle-seconds"),
        ("abrupt_ratio", 1.1, "--abrupt-ratio"),
    ],
)
def test_soak_bounds_fail_before_mutating_production(field: str, value: float, message: str) -> None:
    with pytest.raises(SystemExit, match=message):
        MODULE.validate_args(args(**{field: value}))


def test_percentiles_are_stable_for_empty_and_small_samples() -> None:
    assert MODULE.percentile([], 0.95) is None
    assert MODULE.percentile([9, 1, 5], 0.5) == 5
    assert MODULE.percentile([9, 1, 5], 1) == 9
