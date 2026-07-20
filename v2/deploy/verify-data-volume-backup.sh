#!/usr/bin/env bash
set -euo pipefail

ROOT="${PHDEBATE_V2_ROOT:-/home/ubuntu/sunsq/phdebate-v2}"
ARCHIVE="${1:-}"
if [[ -z "$ARCHIVE" ]]; then
  ARCHIVE="$(find "$ROOT/runtime/deploy-backups" -maxdepth 1 -type f -name '*-data-volumes.tar.gz' | sort | tail -n 1)"
fi
[[ -n "$ARCHIVE" && -f "$ARCHIVE" ]] || {
  echo "Usage: $0 <data-volumes.tar.gz>" >&2
  exit 2
}

WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/phdebate-data-restore-check.XXXXXX")"
cleanup() {
  rm -rf "$WORK_DIR"
}
trap cleanup EXIT

while IFS= read -r entry; do
  case "$entry" in
    /*|../*|*/../*|*/..)
      echo "Unsafe archive member: $entry" >&2
      exit 1
      ;;
  esac
done < <(tar -tzf "$ARCHIVE")

tar -xzf "$ARCHIVE" -C "$WORK_DIR"
test -s "$WORK_DIR/manifest.json"
test -s "$WORK_DIR/files.sha256"
(
  cd "$WORK_DIR"
  sha256sum -c files.sha256
) >/dev/null

files="$(wc -l <"$WORK_DIR/files.sha256" | tr -d ' ')"
bytes="$(stat -c %s "$ARCHIVE" 2>/dev/null || stat -f %z "$ARCHIVE")"
echo "data_restore_verified archive=$(basename "$ARCHIVE") files=$files bytes=$bytes"

