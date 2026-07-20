from __future__ import annotations

import asyncio
import importlib.util
import sys
from argparse import Namespace
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "verify_websocket_disconnect_storm.py"
SPEC = importlib.util.spec_from_file_location("verify_websocket_disconnect_storm", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


@pytest.mark.parametrize("clients", [0, 21, 500])
def test_disconnect_storm_refuses_counts_above_one_room_product_limit(clients: int) -> None:
    args = Namespace(
        base_url="http://127.0.0.1:1",
        room="123456",
        clients=clients,
        settle_seconds=0,
        insecure=False,
    )

    with pytest.raises(SystemExit, match="at most 20 spectators"):
        asyncio.run(MODULE.run(args))


def test_disconnect_storm_default_matches_product_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, int] = {}

    async def fake_run(args: Namespace) -> int:
        captured["clients"] = args.clients
        return 0

    monkeypatch.setattr(MODULE, "run", fake_run)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--room", "123456"])

    assert MODULE.main() == 0
    assert captured["clients"] == 20
