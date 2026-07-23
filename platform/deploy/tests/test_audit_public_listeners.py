from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "audit-public-listeners.py"
SPEC = importlib.util.spec_from_file_location("audit_public_listeners", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_parses_ipv4_ipv6_and_ignores_malformed_rows() -> None:
    listeners = MODULE.parse_ss_output(
        """
LISTEN 0 4096 0.0.0.0:2375 0.0.0.0:* users:((\"dockerd\",pid=1,fd=3))
LISTEN 0 128 [::]:443 [::]:* users:((\"nginx\",pid=2,fd=4))
LISTEN 0 128 127.0.0.1:12340 0.0.0.0:* users:((\"python\",pid=3,fd=5))
broken row
"""
    )

    assert [(item.address, item.port, item.wildcard) for item in listeners] == [
        ("0.0.0.0", 2375, True),
        ("::", 443, True),
        ("127.0.0.1", 12340, False),
    ]
    assert "dockerd" in listeners[0].process


def test_fixture_shape_without_state_is_supported() -> None:
    listeners = MODULE.parse_ss_output("0 4096 *:8888 *:* users:((\"jupyter\",pid=4,fd=6))")
    assert len(listeners) == 1
    assert listeners[0].address == "*"
    assert listeners[0].port == 8888
    assert listeners[0].wildcard


def test_csv_ports_validates_range_and_deduplicates() -> None:
    assert MODULE.csv_ports(["80,443", "443"]) == {80, 443}

    try:
        MODULE.csv_ports(["0"])
    except ValueError as exc:
        assert "invalid TCP port" in str(exc)
    else:
        raise AssertionError("invalid port was accepted")


def test_reuseport_workers_are_reported_once_per_address_and_port() -> None:
    listeners = MODULE.parse_ss_output(
        """
LISTEN 0 4096 0.0.0.0:443 0.0.0.0:* users:((\"nginx\",pid=2,fd=20))
LISTEN 0 4096 0.0.0.0:443 0.0.0.0:* users:((\"nginx\",pid=2,fd=21))
LISTEN 0 4096 [::]:443 [::]:* users:((\"nginx\",pid=2,fd=22))
"""
    )
    assert [(item.address, item.port) for item in MODULE.unique_wildcard_listeners(listeners)] == [
        ("0.0.0.0", 443),
        ("::", 443),
    ]
