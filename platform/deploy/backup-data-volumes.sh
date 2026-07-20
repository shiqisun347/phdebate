#!/usr/bin/env bash
set -euo pipefail

ROOT="${PHDEBATE_ROOT:-/home/ubuntu/sunsq/phdebate}"
BACKUP_DIR="${PHDEBATE_DEPLOY_BACKUP_DIR:-$ROOT/runtime/deploy-backups}"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUTPUT="${1:-$BACKUP_DIR/${TIMESTAMP}-data-volumes.tar.gz}"
STAGING="$(mktemp -d "${TMPDIR:-/tmp}/phdebate-data-backup.XXXXXX")"
INCLUDE_PATHS=(storage assets/moss-prompts backups/reliable-audio-20260719-declick)

cleanup() {
  rm -rf "$STAGING"
  rm -f "$OUTPUT.part" "$OUTPUT.sha256.part"
}
trap cleanup EXIT

for relative in "${INCLUDE_PATHS[@]}"; do
  test -e "$ROOT/$relative" || {
    echo "missing required data path: $ROOT/$relative" >&2
    exit 1
  }
done

install -d -m 700 "$BACKUP_DIR"
umask 077

(
  cd "$ROOT"
  find "${INCLUDE_PATHS[@]}" -type f -print0 \
    | sort -z \
    | xargs -0 -r sha256sum
) >"$STAGING/files.sha256"

python3 - "$ROOT" "$STAGING/manifest.json" "${INCLUDE_PATHS[@]}" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

root = Path(sys.argv[1])
output = Path(sys.argv[2])
paths = sys.argv[3:]
items = []
for relative in paths:
    base = root / relative
    files = [item for item in base.rglob("*") if item.is_file()]
    items.append({
        "path": relative,
        "files": len(files),
        "bytes": sum(item.stat().st_size for item in files),
    })
output.write_text(json.dumps({
    "schema_version": 1,
    "created_at": datetime.now(timezone.utc).isoformat(),
    "root": str(root),
    "contents": items,
}, ensure_ascii=False, indent=2) + "\n")
PY

tar -C "$ROOT" -czf "$OUTPUT.part" "${INCLUDE_PATHS[@]}" \
  -C "$STAGING" manifest.json files.sha256
tar -tzf "$OUTPUT.part" >/dev/null
mv "$OUTPUT.part" "$OUTPUT"
(
  cd "$(dirname "$OUTPUT")"
  sha256sum "$(basename "$OUTPUT")" >"$(basename "$OUTPUT").sha256.part"
  mv "$(basename "$OUTPUT").sha256.part" "$(basename "$OUTPUT").sha256"
)
chmod 600 "$OUTPUT" "$OUTPUT.sha256"
echo "data_backup_ready file=$OUTPUT bytes=$(stat -c %s "$OUTPUT") sha256=$(sha256sum "$OUTPUT" | awk '{print $1}')"

