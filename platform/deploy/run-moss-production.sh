#!/usr/bin/env bash
set -euo pipefail

# Cloud GPU UUIDs can change after a VM reboot even when the assigned physical
# RTX 3090, VBIOS and memory remain the same. Resolve that volatile UUID while
# keeping the stable hardware identity fail-closed.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
OPENMOSS_DIR="$SCRIPT_DIR/openmoss"
PYTHON_BIN="${MOSS_GATEWAY_PYTHON:-$ROOT/services/moss-realtime-gateway/.venv-cu128/bin/python}"
NVIDIA_SMI="${NVIDIA_SMI:-nvidia-smi}"
EXPECTED_NAME="NVIDIA GeForce RTX 3090"
EXPECTED_MEMORY_MIB="24576"
EXPECTED_VBIOS="94.02.26.88.08"

mapfile -t gpu_rows < <(
  "$NVIDIA_SMI" --query-gpu=uuid,name,memory.total,vbios_version --format=csv,noheader,nounits \
    | awk -F',' -v name="$EXPECTED_NAME" -v memory="$EXPECTED_MEMORY_MIB" -v vbios="$EXPECTED_VBIOS" '
      {
        for (i = 1; i <= NF; i++) { gsub(/^[[:space:]]+|[[:space:]]+$/, "", $i) }
        if ($2 == name && $3 == memory && $4 == vbios) print $1
      }
    '
)
if (( ${#gpu_rows[@]} != 1 )); then
  echo "openmoss_production_gpu_resolution_failed: expected exactly one approved RTX 3090" >&2
  exit 2
fi
gpu_uuid="${gpu_rows[0]}"
gpu_fingerprint="$("$PYTHON_BIN" "$OPENMOSS_DIR/gpu_fingerprint.py" --gpu-uuid "$gpu_uuid" \
  | "$PYTHON_BIN" -c 'import json,sys; print(json.load(sys.stdin)["fingerprint_sha256"])')"
[[ "$gpu_fingerprint" =~ ^[0-9a-f]{64}$ ]] || {
  echo "openmoss_production_gpu_resolution_failed: invalid fingerprint" >&2
  exit 2
}

exec "$OPENMOSS_DIR/run-endpoint.sh" \
  --endpoint moss-0 \
  --port 8890 \
  --gpu-uuid "$gpu_uuid" \
  --gpu-fingerprint "$gpu_fingerprint" \
  --prompt-dir /opt/phdebate/moss-prompts \
  --upstream-checkout /opt/OpenMOSS/MOSS-TTS \
  --model-path /opt/OpenMOSS/models/MOSS-TTS-Realtime \
  --tokenizer-path /opt/OpenMOSS/models/MOSS-TTS-Realtime \
  --codec-model-path /opt/OpenMOSS/models/MOSS-Audio-Tokenizer \
  --api-key-file /opt/phdebate/secrets/moss_gateway_key \
  --run-manifest /home/ubuntu/sunsq/phdebate/runtime/openmoss/moss-0.json
