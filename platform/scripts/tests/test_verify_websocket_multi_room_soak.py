from __future__ import annotations

import importlib.util
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
        "rooms": 20,
        "clients_per_room": 20,
        "cycles": 3,
        "handshake_concurrency": 50,
        "slow_clients_per_room": 5,
        "hold_seconds": 5,
        "between_cycle_seconds": 0,
        "abrupt_ratio": 0.5,
    }
    values.update(overrides)
    return Namespace(**values)


def test_default_soak_matches_twenty_rooms_and_twenty_spectators() -> None:
    MODULE.validate_args(args())
    assert MODULE.MAX_SPECTATORS_PER_ROOM == 20


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("rooms", 0, "--rooms"),
        ("rooms", 21, "--rooms"),
        ("clients_per_room", 0, "--clients-per-room"),
        ("clients_per_room", 21, "--clients-per-room"),
        ("cycles", 0, "--cycles"),
        ("cycles", 21, "--cycles"),
        ("handshake_concurrency", 0, "--handshake-concurrency"),
        ("handshake_concurrency", 101, "--handshake-concurrency"),
        ("slow_clients_per_room", 21, "--slow-clients-per-room"),
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
