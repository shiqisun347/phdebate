#!/usr/bin/env bash
set -euo pipefail

ROOT="${PHDEBATE_V2_ROOT:-/home/ubuntu/sunsq/phdebate-v2}"
BACKUP_DIR="${PHDEBATE_V2_DEPLOY_BACKUP_DIR:-$ROOT/runtime/deploy-backups}"
OPENMOSS_ROOT="${PHDEBATE_OPENMOSS_ROOT:-/opt/OpenMOSS}"
PROMPT_ROOT="${PHDEBATE_MOSS_PROMPT_ROOT:-/opt/phdebate/moss-prompts}"
PYTHON_BIN="${PHDEBATE_MOSS_PYTHON:-$ROOT/services/moss-realtime-gateway/.venv-cu128/bin/python}"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUTPUT="${1:-$BACKUP_DIR/${TIMESTAMP}-reliable-voice-runtime.tar.gz}"
STAGING="$(mktemp -d "${TMPDIR:-/tmp}/phdebate-voice-runtime.XXXXXX")"

cleanup() {
  rm -rf "$STAGING"
  rm -f "$OUTPUT.part" "$OUTPUT.sha256.part"
}
trap cleanup EXIT

test -x "$PYTHON_BIN"
test -d "$OPENMOSS_ROOT/models/MOSS-TTS-Realtime"
test -d "$OPENMOSS_ROOT/models/MOSS-Audio-Tokenizer"
test -d "$OPENMOSS_ROOT/MOSS-TTS/.git"
test -d "$PROMPT_ROOT"
test -f "$ROOT/deploy/openmoss/deployment-manifest.json"

install -d -m 700 "$BACKUP_DIR"
umask 077

git -c "safe.directory=$OPENMOSS_ROOT/MOSS-TTS" -C "$OPENMOSS_ROOT/MOSS-TTS" rev-parse HEAD \
  >"$STAGING/openmoss-revision.txt"
"$PYTHON_BIN" -m pip freeze --all >"$STAGING/pip-freeze.txt"
nvidia-smi -q >"$STAGING/nvidia-smi-q.txt"
nvidia-smi --query-gpu=uuid,name,driver_version,vbios_version,memory.total \
  --format=csv,noheader >"$STAGING/gpu-fingerprint-input.csv"
(
  cd "$OPENMOSS_ROOT"
  find models MOSS-TTS -type f -print0 | sort -z | xargs -0 -r sha256sum
) >"$STAGING/openmoss-files.sha256"
(
  cd "$PROMPT_ROOT"
  find . -type f -print0 | sort -z | xargs -0 -r sha256sum
) >"$STAGING/prompt-files.sha256"
cp "$ROOT/deploy/openmoss/deployment-manifest.json" "$STAGING/deployment-manifest.json"
cp "$ROOT/backups/reliable-audio-20260719-declick/reliable-audio-baseline.json" \
  "$STAGING/reliable-audio-baseline.json"

python3 - "$OPENMOSS_ROOT" "$PROMPT_ROOT" "$STAGING/runtime-inventory.json" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

openmoss = Path(sys.argv[1])
prompts = Path(sys.argv[2])
output = Path(sys.argv[3])

def inventory(path: Path) -> dict:
    files = [item for item in path.rglob("*") if item.is_file()]
    return {"path": str(path), "files": len(files), "bytes": sum(item.stat().st_size for item in files)}

output.write_text(json.dumps({
    "schema_version": 1,
    "created_at": datetime.now(timezone.utc).isoformat(),
    "openmoss": inventory(openmoss),
    "realtime_model": inventory(openmoss / "models/MOSS-TTS-Realtime"),
    "codec_model": inventory(openmoss / "models/MOSS-Audio-Tokenizer"),
    "prompts": inventory(prompts),
    "model_files_packaged": False,
    "model_recovery": "Restore the separately mirrored /opt/OpenMOSS tree and verify openmoss-files.sha256.",
}, ensure_ascii=False, indent=2) + "\n")
PY

tar -C "$STAGING" -czf "$OUTPUT.part" .
tar -tzf "$OUTPUT.part" >/dev/null
mv "$OUTPUT.part" "$OUTPUT"
(
  cd "$(dirname "$OUTPUT")"
  sha256sum "$(basename "$OUTPUT")" >"$(basename "$OUTPUT").sha256.part"
  mv "$(basename "$OUTPUT").sha256.part" "$(basename "$OUTPUT").sha256"
)
chmod 600 "$OUTPUT" "$OUTPUT.sha256"
echo "voice_runtime_capture_ready file=$OUTPUT bytes=$(stat -c %s "$OUTPUT") sha256=$(sha256sum "$OUTPUT" | awk '{print $1}')"
