#!/usr/bin/env python3
"""Fail-closed admission check for one isolated OpenMOSS gateway endpoint."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import socket
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
SPEC_PATH = ROOT / "deployment-manifest.json"
FIXED_UPSTREAM_REVISION = "ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af"
FIXED_MODEL_REVISION = "6acbc7f161a0db71c291f2d0aaa9eee59334cab2"
FIXED_CODEC_REVISION = "3cd226ba2947efa357ef453bcad111b6eafba782"
EXPECTED_VOICES = {f"debate_voice_{index}" for index in range(1, 9)}


class PreflightError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_spec(path: Path = SPEC_PATH) -> dict[str, Any]:
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PreflightError(f"cannot read deployment manifest: {exc}") from exc
    if spec.get("schema_version") != 1:
        raise PreflightError("unsupported deployment manifest schema")
    if spec.get("upstream", {}).get("revision") != FIXED_UPSTREAM_REVISION:
        raise PreflightError("deployment manifest upstream revision is not fixed")
    if spec.get("models", {}).get("realtime", {}).get("revision") != FIXED_MODEL_REVISION:
        raise PreflightError("deployment manifest realtime model revision is not fixed")
    if spec.get("models", {}).get("codec", {}).get("revision") != FIXED_CODEC_REVISION:
        raise PreflightError("deployment manifest codec revision is not fixed")
    local_snapshots = spec.get("local_snapshots", {})
    if local_snapshots.get("required") is not True:
        raise PreflightError("deployment manifest must require local model snapshots")
    if local_snapshots.get("network_fallback_allowed") is not False:
        raise PreflightError("deployment manifest must forbid network model fallback")
    admission = spec.get("gpu_admission", {})
    if admission.get("minimum_total_memory_gib", 0) < 24:
        raise PreflightError("deployment manifest GPU floor must remain at least 24GiB")
    if admission.get("minimum_free_memory_gib", 0) < 20:
        raise PreflightError("deployment manifest free-memory floor must remain at least 20GiB")
    if admission.get("allow_sub24gb_diagnostic") is not False:
        raise PreflightError("deployment manifest must reject sub-24GB diagnostic mode")
    prompts = spec.get("prompts")
    if not isinstance(prompts, dict) or set(prompts) != EXPECTED_VOICES:
        raise PreflightError("deployment manifest must contain debate_voice_1..8")
    seen_filenames: set[str] = set()
    seen_hashes: set[str] = set()
    for voice_id, prompt in prompts.items():
        if not isinstance(prompt, dict):
            raise PreflightError(f"invalid prompt record: {voice_id}")
        filename = prompt.get("filename", "")
        checksum = prompt.get("sha256", "")
        if Path(filename).name != filename or not filename.endswith(".wav"):
            raise PreflightError(f"unsafe prompt filename: {voice_id}")
        if len(checksum) != 64 or any(char not in "0123456789abcdef" for char in checksum):
            raise PreflightError(f"invalid prompt SHA-256: {voice_id}")
        if filename in seen_filenames or checksum in seen_hashes:
            raise PreflightError("all eight prompt files and hashes must be distinct")
        seen_filenames.add(filename)
        seen_hashes.add(checksum)
    return spec


def validate_prompts(spec: dict[str, Any], prompt_dir: Path) -> dict[str, dict[str, str]]:
    root = prompt_dir.expanduser().resolve(strict=True)
    result: dict[str, dict[str, str]] = {}
    for voice_id, record in sorted(spec["prompts"].items()):
        raw_path = root / record["filename"]
        if raw_path.is_symlink():
            raise PreflightError(f"prompt symlink is forbidden: {voice_id}")
        try:
            path = raw_path.resolve(strict=True)
        except OSError as exc:
            raise PreflightError(f"prompt is missing: {voice_id}") from exc
        if path.parent != root or not path.is_file():
            raise PreflightError(f"prompt path escapes fixed directory: {voice_id}")
        actual = sha256_file(path)
        if actual != record["sha256"]:
            raise PreflightError(f"prompt SHA-256 mismatch: {voice_id}")
        result[voice_id] = {"filename": path.name, "sha256": actual}
    return result


def safe_snapshot_file(root: Path, relative_name: str, label: str) -> Path:
    relative = Path(relative_name)
    if relative.is_absolute() or ".." in relative.parts:
        raise PreflightError(f"unsafe {label} snapshot file path: {relative_name}")
    raw_path = root / relative
    try:
        path = raw_path.resolve(strict=True)
    except OSError as exc:
        raise PreflightError(f"missing {label} snapshot file: {relative_name}") from exc
    if not path.is_file() or path.stat().st_size <= 0:
        raise PreflightError(f"{label} snapshot file is empty or not regular: {relative_name}")
    return path


def snapshot_root(path: Path, label: str) -> Path:
    expanded = path.expanduser()
    if expanded.is_symlink():
        raise PreflightError(f"{label} snapshot directory must not be a symlink")
    try:
        root = expanded.resolve(strict=True)
    except OSError as exc:
        raise PreflightError(f"{label} snapshot directory is missing") from exc
    if not root.is_dir():
        raise PreflightError(f"{label} snapshot path must be a directory")
    return root


def weight_files(root: Path, label: str) -> dict[str, Path]:
    index_names = ("model.safetensors.index.json", "pytorch_model.bin.index.json")
    present_indexes = [name for name in index_names if (root / name).is_file()]
    if len(present_indexes) > 1:
        raise PreflightError(f"{label} snapshot contains multiple competing weight indexes")
    if present_indexes:
        index_path = safe_snapshot_file(root, present_indexes[0], label)
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
            mapped = sorted(set(index["weight_map"].values()))
        except (KeyError, OSError, TypeError, json.JSONDecodeError) as exc:
            raise PreflightError(f"invalid {label} weight index") from exc
        if not mapped or not all(isinstance(name, str) and name for name in mapped):
            raise PreflightError(f"empty or invalid {label} weight index")
        return {
            present_indexes[0]: index_path,
            **{name: safe_snapshot_file(root, name, label) for name in mapped},
        }
    candidates = sorted(root.glob("*.safetensors")) + sorted(root.glob("pytorch_model*.bin"))
    files = {path.name: safe_snapshot_file(root, path.name, label) for path in candidates}
    if not files:
        raise PreflightError(f"{label} snapshot has no safetensors or PyTorch weight file")
    return files


def snapshot_record(root: Path, logical_files: dict[str, Path]) -> dict[str, Any]:
    records = {name: {"bytes": path.stat().st_size, "sha256": sha256_file(path)} for name, path in sorted(logical_files.items())}
    fingerprint = hashlib.sha256(json.dumps(records, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {"path": str(root), "fingerprint_sha256": fingerprint, "files": records}


def validate_model_snapshot(path: Path, label: str) -> dict[str, Any]:
    root = snapshot_root(path, label)
    config_path = safe_snapshot_file(root, "config.json", label)
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PreflightError(f"invalid {label} config.json") from exc
    if not isinstance(config, dict) or not config:
        raise PreflightError(f"empty {label} config.json")
    logical_files = {"config.json": config_path}
    logical_files.update(weight_files(root, label))
    return snapshot_record(root, logical_files)


def validate_tokenizer_snapshot(path: Path) -> dict[str, Any]:
    label = "tokenizer"
    root = snapshot_root(path, label)
    config_path = safe_snapshot_file(root, "tokenizer_config.json", label)
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PreflightError("invalid tokenizer_config.json") from exc
    if not isinstance(config, dict) or not config:
        raise PreflightError("empty tokenizer_config.json")
    asset_names = ("tokenizer.json", "tokenizer.model", "spiece.model", "vocab.json", "vocab.txt")
    present_assets = [name for name in asset_names if (root / name).is_file()]
    if not present_assets:
        raise PreflightError("tokenizer snapshot has no supported tokenizer data file")
    optional_names = ("special_tokens_map.json", "added_tokens.json", "merges.txt")
    logical_files = {"tokenizer_config.json": config_path}
    for name in (*asset_names, *optional_names):
        if (root / name).is_file():
            logical_files[name] = safe_snapshot_file(root, name, label)
    return snapshot_record(root, logical_files)


def run_checked(command: list[str]) -> str:
    try:
        completed = subprocess.run(command, check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise PreflightError(f"command failed: {command[0]}") from exc
    return completed.stdout.strip()


def query_gpu(nvidia_smi: str, requested_uuid: str) -> dict[str, Any]:
    fields = [
        "uuid",
        "name",
        "memory.total",
        "memory.free",
        "driver_version",
        "vbios_version",
        "pci.bus_id",
        "compute_mode",
    ]
    output = run_checked([nvidia_smi, f"--query-gpu={','.join(fields)}", "--format=csv,noheader,nounits"])
    rows = list(csv.reader(output.splitlines(), skipinitialspace=True))
    matches = [row for row in rows if row and row[0].strip() == requested_uuid]
    if len(matches) != 1 or len(matches[0]) != len(fields):
        raise PreflightError("requested GPU UUID was not found exactly once")
    row = [value.strip() for value in matches[0]]
    try:
        total_mib = int(row[2])
        free_mib = int(row[3])
    except ValueError as exc:
        raise PreflightError("nvidia-smi returned invalid memory values") from exc
    identity = {
        "uuid": row[0],
        "name": row[1],
        "total_memory_mib": total_mib,
        "driver_version": row[4],
        "vbios_version": row[5],
        "pci_bus_id": row[6],
    }
    fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {
        **identity,
        "free_memory_mib": free_mib,
        "compute_mode": row[7],
        "fingerprint_sha256": fingerprint,
    }


def assert_gpu_idle(nvidia_smi: str, requested_uuid: str) -> None:
    command = [
        nvidia_smi,
        "--query-compute-apps=gpu_uuid,pid,process_name,used_memory",
        "--format=csv,noheader,nounits",
    ]
    output = run_checked(command)
    for row in csv.reader(output.splitlines(), skipinitialspace=True):
        if row and row[0].strip() == requested_uuid:
            raise PreflightError("selected GPU already has an active compute process")


def assert_port_available(host: str, port: int) -> None:
    if host not in {"127.0.0.1", "::1"}:
        raise PreflightError("gateway bind host must be loopback")
    if not 1024 <= port <= 65535:
        raise PreflightError("gateway port must be between 1024 and 65535")
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as connection_probe:
        connection_probe.settimeout(0.25)
        if connection_probe.connect_ex((host, port)) == 0:
            raise PreflightError(f"gateway port is unavailable: {port}")
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        # Uvicorn enables address reuse. Mirror that behavior so a clean gateway
        # restart is not rejected solely because the previous listener left
        # accepted TCP connections in TIME_WAIT.
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError as exc:
            raise PreflightError(f"gateway port is unavailable: {port}") from exc


def validate_secret_file(path: Path) -> None:
    if path.is_symlink():
        raise PreflightError("API key file must not be a symlink")
    try:
        metadata = path.stat()
    except OSError as exc:
        raise PreflightError("API key file is missing") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
        raise PreflightError("API key file must be a regular file with mode 0600 or stricter")
    if metadata.st_uid not in {0, os.geteuid()}:
        raise PreflightError("API key file must be owned by root or the gateway service user")
    secret = path.read_text(encoding="utf-8").strip()
    if len(secret) < 32 or any(char.isspace() for char in secret):
        raise PreflightError("API key must contain at least 32 non-whitespace characters")


def git_head(checkout: Path) -> str:
    return run_checked(["git", "-C", str(checkout), "rev-parse", "HEAD"])


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        temp_path = Path(handle.name)
    os.chmod(temp_path, 0o600)
    temp_path.replace(path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--gpu-uuid")
    parser.add_argument("--expected-gpu-fingerprint")
    parser.add_argument("--prompt-dir", type=Path, required=True)
    parser.add_argument("--upstream-checkout", type=Path)
    parser.add_argument("--model-path", type=Path)
    parser.add_argument("--tokenizer-path", type=Path)
    parser.add_argument("--codec-model-path", type=Path)
    parser.add_argument("--api-key-file", type=Path)
    parser.add_argument("--run-manifest", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--nvidia-smi", default="nvidia-smi")
    parser.add_argument("--static-only", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        spec = load_spec()
        prompts = validate_prompts(spec, args.prompt_dir)
        if args.static_only:
            print(json.dumps({"ok": True, "prompts": prompts}, sort_keys=True))
            return 0
        if not args.endpoint or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for char in args.endpoint):
            raise PreflightError("endpoint name contains unsupported characters")
        if not all(
            [
                args.gpu_uuid,
                args.expected_gpu_fingerprint,
                args.upstream_checkout,
                args.model_path,
                args.tokenizer_path,
                args.codec_model_path,
                args.api_key_file,
                args.run_manifest,
            ]
        ):
            raise PreflightError("GPU identity, checkout, three local snapshots, API key file, and run manifest are required")
        if not re.fullmatch(r"GPU-[A-Za-z0-9-]+", args.gpu_uuid):
            raise PreflightError("a full dedicated NVIDIA GPU UUID is required")
        assert_port_available(args.host, args.port)
        validate_secret_file(args.api_key_file)
        checkout = args.upstream_checkout.expanduser().resolve(strict=True)
        actual_head = git_head(checkout)
        if actual_head != FIXED_UPSTREAM_REVISION:
            raise PreflightError("OpenMOSS checkout HEAD does not match the fixed revision")
        snapshots = {
            "model": validate_model_snapshot(args.model_path, "realtime model"),
            "tokenizer": validate_tokenizer_snapshot(args.tokenizer_path),
            "codec": validate_model_snapshot(args.codec_model_path, "codec model"),
        }
        gpu = query_gpu(args.nvidia_smi, args.gpu_uuid)
        if gpu["fingerprint_sha256"] != args.expected_gpu_fingerprint:
            raise PreflightError("GPU fingerprint does not match the approved isolated device")
        admission = spec["gpu_admission"]
        gib = 1024
        if gpu["total_memory_mib"] < admission["minimum_total_memory_gib"] * gib:
            raise PreflightError("GPU total memory is below the fixed 24GiB floor")
        if gpu["free_memory_mib"] < admission["minimum_free_memory_gib"] * gib:
            raise PreflightError("GPU free memory is below the fixed 20GiB floor")
        if admission["require_no_compute_processes"]:
            assert_gpu_idle(args.nvidia_smi, args.gpu_uuid)
        payload = {
            "schema_version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "preflight_passed",
            "endpoint": args.endpoint,
            "bind": {"host": args.host, "port": args.port},
            "gpu": gpu,
            "upstream": {"checkout": str(checkout), "revision": actual_head},
            "models": spec["models"],
            "local_snapshots": snapshots,
            "prompts": prompts,
            "admission": admission,
            "runtime_parameters": spec["runtime_parameters"],
            "api_key": {"source": "file", "path": str(args.api_key_file), "value_recorded": False},
        }
        atomic_json(args.run_manifest, payload)
        print(json.dumps({"ok": True, "run_manifest": str(args.run_manifest)}, sort_keys=True))
        return 0
    except (OSError, PreflightError) as exc:
        print(f"openmoss_preflight_failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
