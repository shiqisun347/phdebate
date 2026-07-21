#!/usr/bin/env python3
"""Inventory wildcard TCP listeners and fail on undocumented management ports.

This is deliberately read-only. It does not change firewall rules or stop a
service; operators make those changes only after confirming ownership and
rollback impact.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

HIGH_RISK_PORTS = {
    2375: "Docker Remote API without TLS can grant host-level control",
    8888: "Jupyter is an administrative development surface",
    8889: "File Browser is an administrative file surface",
    9191: "Unidentified administrative listener",
}


@dataclass(frozen=True)
class Listener:
    address: str
    port: int
    process: str

    @property
    def wildcard(self) -> bool:
        return self.address in {"0.0.0.0", "::", "*"}


def parse_endpoint(value: str) -> tuple[str, int] | None:
    value = value.strip()
    bracketed = re.fullmatch(r"\[(.*)]:(\d+)", value)
    if bracketed:
        return bracketed.group(1), int(bracketed.group(2))
    address, separator, port = value.rpartition(":")
    if not separator or not port.isdigit():
        return None
    return address, int(port)


def parse_ss_output(output: str) -> list[Listener]:
    listeners: list[Listener] = []
    for line in output.splitlines():
        fields = line.split(maxsplit=5)
        if not fields:
            continue
        # `ss -H -lntp` emits STATE RECV-Q SEND-Q LOCAL PEER PROCESS.
        # Captured fixtures may omit STATE, so accept both stable shapes.
        local_index = 3 if fields[0].upper() in {"LISTEN", "UNCONN"} else 2
        if len(fields) <= local_index:
            continue
        endpoint = parse_endpoint(fields[local_index])
        if endpoint is None:
            continue
        address, port = endpoint
        process = fields[5] if len(fields) > 5 else ""
        listeners.append(Listener(address=address, port=port, process=process))
    return listeners


def csv_ports(values: list[str]) -> set[int]:
    ports: set[int] = set()
    for value in values:
        for item in value.split(","):
            item = item.strip()
            if not item:
                continue
            port = int(item)
            if not 1 <= port <= 65535:
                raise ValueError(f"invalid TCP port: {port}")
            ports.add(port)
    return ports


def load_ss_output(path: Path | None) -> str:
    if path is not None:
        return path.read_text()
    completed = subprocess.run(
        ["ss", "-H", "-lntp"],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def unique_wildcard_listeners(listeners: list[Listener]) -> list[Listener]:
    """Collapse reuseport/forked worker sockets into one address/port row."""
    unique: dict[tuple[str, int], Listener] = {}
    for item in listeners:
        if not item.wildcard:
            continue
        key = (item.address, item.port)
        existing = unique.get(key)
        if existing is None or (not existing.process and item.process):
            unique[key] = item
    return sorted(unique.values(), key=lambda item: (item.port, item.address))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ss-output", type=Path, help="read captured ss output instead of the live host")
    parser.add_argument(
        "--allow-wildcard",
        action="append",
        default=[],
        metavar="PORTS",
        help="documented product ports, comma-separated; repeatable",
    )
    parser.add_argument(
        "--allow-risk-port",
        action="append",
        default=[],
        metavar="PORTS",
        help="temporary reviewed exception for a high-risk port; repeatable",
    )
    parser.add_argument("--strict", action="store_true", help="also fail on any undocumented wildcard port")
    parser.add_argument("--warn-only", action="store_true", help="report findings without a non-zero exit status")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args()

    try:
        allowed = csv_ports(args.allow_wildcard)
        risk_exceptions = csv_ports(args.allow_risk_port)
        listeners = parse_ss_output(load_ss_output(args.ss_output))
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        parser.error(str(exc))

    wildcard = unique_wildcard_listeners(listeners)
    findings: list[dict[str, object]] = []
    for item in wildcard:
        if item.port in HIGH_RISK_PORTS and item.port not in risk_exceptions:
            severity = "critical" if item.port == 2375 else "high"
            findings.append(
                {
                    **asdict(item),
                    "severity": severity,
                    "reason": HIGH_RISK_PORTS[item.port],
                    "action": "bind to loopback/private management network or remove the host mapping",
                }
            )
        elif args.strict and item.port not in allowed and item.port not in risk_exceptions:
            findings.append(
                {
                    **asdict(item),
                    "severity": "medium",
                    "reason": "wildcard listener is not present in the reviewed product-port allowlist",
                    "action": "document the port or restrict its bind address",
                }
            )

    report = {
        "ok": not findings,
        "warn_only": args.warn_only,
        "wildcard_listeners": [asdict(item) for item in wildcard],
        "findings": findings,
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"public_listener_audit ok={report['ok']} wildcard={len(wildcard)} findings={len(findings)}")
        for finding in findings:
            print(
                f"{str(finding['severity']).upper()} {finding['address']}:{finding['port']} "
                f"{finding['reason']} process={finding['process'] or 'unknown'}"
            )
    return 0 if args.warn_only or not findings else 1


if __name__ == "__main__":
    raise SystemExit(main())
