#!/usr/bin/env python3
"""Print the canonical identity and fingerprint for one read-only GPU UUID."""

from __future__ import annotations

import argparse
import json
import sys

from preflight import PreflightError, assert_gpu_idle, query_gpu


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gpu-uuid", required=True)
    parser.add_argument("--nvidia-smi", default="nvidia-smi")
    parser.add_argument("--require-idle", action="store_true")
    args = parser.parse_args()
    try:
        gpu = query_gpu(args.nvidia_smi, args.gpu_uuid)
        if args.require_idle:
            assert_gpu_idle(args.nvidia_smi, args.gpu_uuid)
        print(json.dumps(gpu, sort_keys=True, indent=2))
        return 0
    except PreflightError as exc:
        print(f"openmoss_gpu_fingerprint_failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
