from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "verify_multi_room_ws.py"
SPEC = importlib.util.spec_from_file_location("verify_multi_room_ws", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


@pytest.mark.parametrize("connections", [0, 6, 40])
def test_connections_per_room_cannot_exceed_product_limit(connections: int) -> None:
    with pytest.raises(SystemExit, match="at most 5 spectators"):
        MODULE.validate_args(["123456"], connections, 20)


def test_default_product_limit_is_five() -> None:
    assert MODULE.MAX_SPECTATORS_PER_ROOM == 5
    MODULE.validate_args(["123456"], MODULE.MAX_SPECTATORS_PER_ROOM, 20)


def test_duplicate_room_codes_are_rejected() -> None:
    with pytest.raises(SystemExit, match="duplicates"):
        MODULE.validate_args(["123456", "123456"], 1, 1)


def test_more_than_five_simultaneous_rooms_are_rejected() -> None:
    with pytest.raises(SystemExit, match="cannot exceed 5"):
        MODULE.validate_args([f"{index:06d}" for index in range(6)], 1, 6)


@pytest.mark.parametrize("concurrency", [0, 101])
def test_handshake_concurrency_is_bounded(concurrency: int) -> None:
    with pytest.raises(SystemExit, match="--concurrency"):
        MODULE.validate_args(["123456"], 1, concurrency)
