#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"

bash -n "$SCRIPT_DIR/run-endpoint.sh" "$SCRIPT_DIR/test-static.sh"
python3 -m py_compile "$SCRIPT_DIR/preflight.py" "$SCRIPT_DIR/wait_ready.py" "$SCRIPT_DIR/gpu_telemetry.py" "$SCRIPT_DIR/gpu_fingerprint.py"
python3 "$SCRIPT_DIR/preflight.py" \
  --static-only \
  --endpoint static-test \
  --port 8890 \
  --prompt-dir "$REPO_ROOT/assets/moss-prompts/audio" >/dev/null

python3 - "$SCRIPT_DIR" <<'PY'
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from preflight import (
    PreflightError,
    assert_port_available,
    validate_model_snapshot,
    validate_tokenizer_snapshot,
)
import socket

with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    model = root / "model"
    tokenizer = root / "tokenizer"
    codec = root / "codec"
    for directory in (model, tokenizer, codec):
        directory.mkdir()
    (model / "config.json").write_text(json.dumps({"model_type": "moss"}))
    (model / "model.safetensors").write_bytes(b"fixed-model-weight")
    (codec / "config.json").write_text(json.dumps({"model_type": "moss-codec"}))
    (codec / "pytorch_model.bin").write_bytes(b"fixed-codec-weight")
    (tokenizer / "tokenizer_config.json").write_text(json.dumps({"tokenizer_class": "Fixed"}))
    (tokenizer / "tokenizer.json").write_text(json.dumps({"version": "1.0"}))

    model_record = validate_model_snapshot(model, "realtime model")
    codec_record = validate_model_snapshot(codec, "codec model")
    tokenizer_record = validate_tokenizer_snapshot(tokenizer)
    assert len(model_record["fingerprint_sha256"]) == 64
    assert "model.safetensors" in model_record["files"]
    assert "pytorch_model.bin" in codec_record["files"]
    assert "tokenizer.json" in tokenizer_record["files"]

    (model / "model.safetensors").write_bytes(b"")
    try:
        validate_model_snapshot(model, "realtime model")
    except PreflightError:
        pass
    else:
        raise AssertionError("empty model weights were accepted")

    model_link = root / "model-link"
    model_link.symlink_to(model, target_is_directory=True)
    try:
        validate_model_snapshot(model_link, "realtime model")
    except PreflightError:
        pass
    else:
        raise AssertionError("symlinked snapshot directory was accepted")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        occupied_port = listener.getsockname()[1]
        try:
            assert_port_available("127.0.0.1", occupied_port)
        except PreflightError:
            pass
        else:
            raise AssertionError("an active listener was accepted")
PY

python3 - "$SCRIPT_DIR" <<'PY'
import json
import re
import sys
from pathlib import Path

root = Path(sys.argv[1])
manifest = json.loads((root / "deployment-manifest.json").read_text())
assert manifest["upstream"]["revision"] == "ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af"
assert manifest["models"]["realtime"]["revision"] == "6acbc7f161a0db71c291f2d0aaa9eee59334cab2"
assert manifest["models"]["codec"]["revision"] == "3cd226ba2947efa357ef453bcad111b6eafba782"
assert len(manifest["prompts"]) == 8
assert len({entry["sha256"] for entry in manifest["prompts"].values()}) == 8
assert manifest["gpu_admission"]["minimum_total_memory_gib"] >= 24
assert manifest["gpu_admission"]["allow_sub24gb_diagnostic"] is False
assert manifest["gpu_admission"]["require_no_compute_processes"] is False
assert manifest["runtime_parameters"]["sample_rate"] == 24000
assert manifest["runtime_parameters"]["attention_implementation"] == "sdpa"
assert manifest["runtime_parameters"]["prompt_chunk_duration_seconds"] == 0.24
assert manifest["runtime_parameters"]["decode_chunk_frames"] == 3
assert manifest["runtime_parameters"]["initial_chunk_frames"] == 6
assert manifest["runtime_parameters"]["dynamo_cache_size_limit"] == 64
assert manifest["runtime_parameters"]["async_decoder_enabled"] is False
assert manifest["runtime_parameters"]["async_decoder_queue_size"] == 2
assert manifest["runtime_parameters"]["do_sample"] is True
assert manifest["local_snapshots"]["required"] is True
assert manifest["local_snapshots"]["network_fallback_allowed"] is False

supervisor = (root / "supervisor-3-endpoints.conf").read_text()
assert len(re.findall(r"^\[program:openmoss-gateway-[123]\]$", supervisor, re.MULTILINE)) == 3
assert len(re.findall(r"^\[program:openmoss-telemetry-[123]\]$", supervisor, re.MULTILINE)) == 3
assert "autostart=false" in supervisor
assert "MOSS_GATEWAY_API_KEY=" not in supervisor
for option in ("--model-path", "--tokenizer-path", "--codec-model-path"):
    assert supervisor.count(option) == 3

runner = (root / "run-endpoint.sh").read_text()
assert "OpenMOSS-Team/" not in runner
assert "HF_HUB_OFFLINE=1" in runner
assert "TRANSFORMERS_OFFLINE=1" in runner
assert 'MOSS_GATEWAY_PROMPT_CHUNK_SECONDS=0.24' in runner
assert 'MOSS_GATEWAY_DECODE_CHUNK_FRAMES="${MOSS_GATEWAY_DECODE_CHUNK_FRAMES:-3}"' in runner
assert 'MOSS_GATEWAY_INITIAL_CHUNK_FRAMES="${MOSS_GATEWAY_INITIAL_CHUNK_FRAMES:-6}"' in runner
assert 'MOSS_GATEWAY_DYNAMO_CACHE_SIZE_LIMIT="${MOSS_GATEWAY_DYNAMO_CACHE_SIZE_LIMIT:-64}"' in runner
assert 'MOSS_GATEWAY_ASYNC_DECODER_ENABLED="${MOSS_GATEWAY_ASYNC_DECODER_ENABLED:-false}"' in runner
assert 'PYTHONPATH="$UPSTREAM_CHECKOUT/moss_tts_realtime:$GATEWAY_DIR' in runner

environment = (root / "supervisor.env.example").read_text()
for variable in ("MOSS_MODEL_PATH=", "MOSS_TOKENIZER_PATH=", "MOSS_CODEC_MODEL_PATH="):
    assert variable in environment

for path in root.iterdir():
    if path.is_file():
        text = path.read_text(errors="ignore")
        assert ("s" + "k-") not in text
PY

if OPENMOSS_DEPLOY_ENABLE=disabled "$SCRIPT_DIR/run-endpoint.sh" >/dev/null 2>&1; then
  echo "run-endpoint.sh unexpectedly accepted disabled deployment" >&2
  exit 1
fi

echo "openmoss deployment tooling static checks passed"
