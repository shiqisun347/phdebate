#!/usr/bin/env python3
"""Fail closed when the production ASR/TTS stack is not really on CUDA.

This verifier deliberately checks both configuration and live process evidence.
It never prints gateway credentials and only talks to the authenticated MOSS
health endpoint over loopback HTTP.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


class VerificationError(RuntimeError):
    pass


MOSS_RELIABLE_RUNTIME_ENV = {
    "MOSS_GATEWAY_DECODE_CHUNK_FRAMES": "3",
    "MOSS_GATEWAY_INITIAL_CHUNK_FRAMES": "6",
    "MOSS_GATEWAY_DO_SAMPLE": "true",
    "MOSS_GATEWAY_ASYNC_DECODER_ENABLED": "false",
}


def run(*command: str) -> str:
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise VerificationError(f"command failed ({' '.join(command)}): {detail}")
    return completed.stdout


def require_running(program: str) -> str:
    status = run("supervisorctl", "status", program).strip()
    if not re.search(r"\bRUNNING\b", status):
        raise VerificationError(f"{program} is not RUNNING: {status}")
    return status


def find_process(needle: str) -> tuple[int, str]:
    matches: list[tuple[int, str]] = []
    for raw in run("ps", "-eo", "pid=,args=").splitlines():
        raw = raw.strip()
        if not raw or needle not in raw or "verify_gpu_voice_runtime.py" in raw:
            continue
        pid_text, _, args = raw.partition(" ")
        try:
            matches.append((int(pid_text), args.strip()))
        except ValueError:
            continue
    if len(matches) != 1:
        raise VerificationError(f"expected exactly one live process containing {needle!r}, got {len(matches)}")
    return matches[0]


def gpu_process_memory() -> dict[int, int]:
    output = run(
        "nvidia-smi",
        "--query-compute-apps=pid,used_memory",
        "--format=csv,noheader,nounits",
    )
    result: dict[int, int] = {}
    for raw in output.splitlines():
        parts = [part.strip() for part in raw.split(",")]
        if len(parts) != 2:
            continue
        try:
            result[int(parts[0])] = int(parts[1])
        except ValueError:
            continue
    return result


def read_moss_health(url: str, key_file: Path) -> dict[str, Any]:
    if not url.startswith(("http://127.0.0.1:", "http://[::1]:")):
        raise VerificationError("MOSS health URL must use loopback HTTP")
    key = key_file.read_text(encoding="utf-8").strip()
    if not key:
        raise VerificationError("MOSS API key file is empty")
    request = urllib.request.Request(url, headers={"X-MOSS-Gateway-Key": key})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.load(response)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise VerificationError(f"MOSS health request failed: {type(exc).__name__}") from exc
    if not isinstance(payload, dict):
        raise VerificationError("MOSS health response is not an object")
    return payload


def verify_funasr_config(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    command_match = re.search(r"^command=(.+)$", text, flags=re.MULTILINE)
    if not command_match:
        raise VerificationError("FunASR supervisor config has no command")
    command = command_match.group(1).strip()
    device_match = re.search(r"(?:^|\s)--device(?:=|\s+)([^\s]+)", command)
    if not device_match or not device_match.group(1).startswith("cuda"):
        raise VerificationError("FunASR supervisor command is not pinned to CUDA")
    visible_match = re.search(r'CUDA_VISIBLE_DEVICES=\\?"([^"\\]+)', text)
    if not visible_match or visible_match.group(1).strip() in {"", "-1"}:
        raise VerificationError("FunASR CUDA_VISIBLE_DEVICES is missing or disables CUDA")
    return {"device": device_match.group(1), "cuda_visible_devices": visible_match.group(1)}


def verify_moss_config(path: Path) -> dict[str, str]:
    """Reject production Supervisor overrides that drift from the audio baseline."""

    text = path.read_text(encoding="utf-8")
    configured = dict(re.findall(r'\b([A-Z][A-Z0-9_]+)="([^"]*)"', text))
    for name, expected in MOSS_RELIABLE_RUNTIME_ENV.items():
        actual = configured.get(name)
        if actual != expected:
            raise VerificationError(
                f"MOSS supervisor {name} is {actual!r}, expected reliable baseline {expected!r}"
            )
    return dict(MOSS_RELIABLE_RUNTIME_ENV)


def verify_moss_health(payload: dict[str, Any]) -> dict[str, Any]:
    required = {
        "ok": True,
        "status": "ready",
        "backend": "openmoss",
        "model_warmed": True,
        "warmed_up": True,
        "orphan_count": 0,
    }
    for key, expected in required.items():
        if payload.get(key) != expected:
            raise VerificationError(f"MOSS health {key!r} is {payload.get(key)!r}, expected {expected!r}")
    placement = payload.get("placement")
    if not isinstance(placement, dict) or placement.get("mode") != "production_all_cuda":
        raise VerificationError("MOSS placement is not production_all_cuda")
    for component in ("realtime_model", "codec_encoder", "codec_quantizer", "codec_decoder"):
        value = str(placement.get(component) or "")
        if not value.startswith("cuda"):
            raise VerificationError(f"MOSS {component} is not on CUDA: {value!r}")
    return {
        "status": payload["status"],
        "backend": payload["backend"],
        "model_warmed": payload["model_warmed"],
        "orphan_count": payload["orphan_count"],
        "capacity": payload.get("capacity"),
        "placement": {key: placement.get(key) for key in (
            "mode",
            "realtime_model",
            "codec_encoder",
            "codec_quantizer",
            "codec_decoder",
            "codec_streaming_context",
        )},
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--funasr-program", default="jixia-funasr-asr")
    parser.add_argument("--moss-program", default="jixia-moss-realtime")
    parser.add_argument("--funasr-config", type=Path, default=Path("/etc/supervisor/conf.d/jixia-funasr.conf"))
    parser.add_argument("--moss-config", type=Path, default=Path("/etc/supervisor/conf.d/jixia-moss-realtime.conf"))
    parser.add_argument("--funasr-process-needle", default="local_funasr_ws.py")
    parser.add_argument("--moss-process-needle", default="moss_realtime_gateway.app:app")
    parser.add_argument("--moss-health-url", default="http://127.0.0.1:8890/health/ready")
    parser.add_argument("--moss-api-key-file", type=Path, default=Path("/opt/phdebate/secrets/moss_gateway_key"))
    parser.add_argument("--minimum-funasr-gpu-mib", type=int, default=512)
    parser.add_argument("--minimum-moss-gpu-mib", type=int, default=4096)
    args = parser.parse_args()

    try:
        supervisor = {
            "funasr": require_running(args.funasr_program),
            "moss": require_running(args.moss_program),
        }
        funasr_config = verify_funasr_config(args.funasr_config)
        moss_config = verify_moss_config(args.moss_config)
        funasr_pid, funasr_args = find_process(args.funasr_process_needle)
        moss_pid, _moss_args = find_process(args.moss_process_needle)
        if not re.search(r"(?:^|\s)--device(?:=|\s+)cuda", funasr_args):
            raise VerificationError("live FunASR process is not running with --device cuda")
        gpu_memory = gpu_process_memory()
        funasr_mib = gpu_memory.get(funasr_pid, 0)
        moss_mib = gpu_memory.get(moss_pid, 0)
        if funasr_mib < args.minimum_funasr_gpu_mib:
            raise VerificationError(f"FunASR PID {funasr_pid} has only {funasr_mib} MiB on GPU")
        if moss_mib < args.minimum_moss_gpu_mib:
            raise VerificationError(f"MOSS PID {moss_pid} has only {moss_mib} MiB on GPU")
        moss_health = verify_moss_health(read_moss_health(args.moss_health_url, args.moss_api_key_file))
    except (OSError, VerificationError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2

    print(json.dumps({
        "ok": True,
        "supervisor": supervisor,
        "funasr": {**funasr_config, "pid": funasr_pid, "gpu_memory_mib": funasr_mib},
        "moss": {
            "pid": moss_pid,
            "gpu_memory_mib": moss_mib,
            "runtime_config": moss_config,
            "health": moss_health,
        },
        "credentials_exposed": False,
    }, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
