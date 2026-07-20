#!/usr/bin/env python3
"""Wait for a fully warmed, authenticated OpenMOSS readiness response."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

FIXED_UPSTREAM_REVISION = "ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af"


def write_evidence(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        temp_path = Path(handle.name)
    os.chmod(temp_path, 0o600)
    temp_path.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--api-key-file", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=900)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    if not args.url.startswith(("http://127.0.0.1:", "http://[::1]:")):
        print("readiness URL must use loopback HTTP", file=sys.stderr)
        return 2
    key = args.api_key_file.read_text(encoding="utf-8").strip()
    deadline = time.monotonic() + args.timeout_seconds
    last_error = "not attempted"
    while time.monotonic() < deadline:
        try:
            os.kill(args.pid, 0)
        except OSError:
            print("gateway exited before readiness", file=sys.stderr)
            return 2
        request = urllib.request.Request(args.url, headers={"X-MOSS-Gateway-Key": key})
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                payload = json.load(response)
            valid = all(
                [
                    payload.get("ok") is True,
                    payload.get("status") == "ready",
                    payload.get("backend") == "openmoss",
                    payload.get("upstream_revision") == FIXED_UPSTREAM_REVISION,
                    payload.get("model_warmed") is True,
                    payload.get("warmed_up") is True,
                    payload.get("capacity") == 1,
                    payload.get("orphan_count") == 0,
                    payload.get("sub24gb_diagnostic") is False,
                ]
            )
            if valid:
                write_evidence(
                    args.evidence,
                    {
                        "schema_version": 1,
                        "observed_at": datetime.now(timezone.utc).isoformat(),
                        "status": "ready",
                        "url": args.url,
                        "health": payload,
                        "api_key_recorded": False,
                    },
                )
                print(json.dumps({"ok": True, "evidence": str(args.evidence)}, sort_keys=True))
                return 0
            last_error = "health response did not satisfy fixed readiness contract"
        except (OSError, ValueError, urllib.error.URLError) as exc:
            last_error = type(exc).__name__
        time.sleep(1)
    print(f"readiness timeout: {last_error}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
