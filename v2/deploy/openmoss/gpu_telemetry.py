#!/usr/bin/env python3
"""Emit redacted JSONL telemetry for one approved GPU UUID."""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import time
from datetime import datetime, timezone

FIELDS = [
    "timestamp",
    "uuid",
    "utilization.gpu",
    "utilization.memory",
    "memory.total",
    "memory.used",
    "memory.free",
    "temperature.gpu",
    "power.draw",
]


def sample(binary: str, requested_uuid: str, endpoint: str) -> dict[str, object]:
    completed = subprocess.run(
        [binary, f"--query-gpu={','.join(FIELDS)}", "--format=csv,noheader,nounits"],
        check=True,
        capture_output=True,
        text=True,
    )
    rows = [row for row in csv.reader(completed.stdout.splitlines(), skipinitialspace=True) if row]
    matches = [row for row in rows if len(row) == len(FIELDS) and row[1].strip() == requested_uuid]
    if len(matches) != 1:
        raise RuntimeError("approved GPU UUID was not found exactly once")
    row = [value.strip() for value in matches[0]]
    return {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "endpoint": endpoint,
        "gpu": {
            "driver_timestamp": row[0],
            "uuid": row[1],
            "utilization_gpu_percent": float(row[2]),
            "utilization_memory_percent": float(row[3]),
            "memory_total_mib": int(row[4]),
            "memory_used_mib": int(row[5]),
            "memory_free_mib": int(row[6]),
            "temperature_c": float(row[7]),
            "power_draw_w": float(row[8]),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--gpu-uuid", required=True)
    parser.add_argument("--interval-seconds", type=float, default=5)
    parser.add_argument("--nvidia-smi", default="nvidia-smi")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.interval_seconds < 1:
        print("telemetry interval must be at least one second", file=sys.stderr)
        return 2
    try:
        while True:
            print(
                json.dumps(sample(args.nvidia_smi, args.gpu_uuid, args.endpoint), sort_keys=True),
                flush=True,
            )
            if args.once:
                return 0
            time.sleep(args.interval_seconds)
    except (OSError, RuntimeError, subprocess.CalledProcessError, ValueError) as exc:
        print(f"openmoss_gpu_telemetry_failed: {type(exc).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
