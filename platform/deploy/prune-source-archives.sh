#!/usr/bin/env bash
set -euo pipefail

ROOT="${PHDEBATE_ROOT:-/home/ubuntu/sunsq/phdebate}"
BACKUP_DIR="${PHDEBATE_DEPLOY_BACKUP_DIR:-$ROOT/runtime/deploy-backups}"
MODE="${1:-dry-run}"
KEEP="${PHDEBATE_SOURCE_ARCHIVE_KEEP:-1}"
MIN_AGE_HOURS="${PHDEBATE_SOURCE_ARCHIVE_MIN_AGE_HOURS:-72}"

if [[ "$MODE" != "dry-run" && "$MODE" != "apply" ]]; then
  echo "Usage: $0 [dry-run|apply]" >&2
  exit 2
fi
[[ "$KEEP" =~ ^[0-9]+$ ]] || {
  echo "PHDEBATE_SOURCE_ARCHIVE_KEEP must be a non-negative integer." >&2
  exit 2
}
[[ "$MIN_AGE_HOURS" =~ ^[0-9]+$ ]] || {
  echo "PHDEBATE_SOURCE_ARCHIVE_MIN_AGE_HOURS must be a non-negative integer." >&2
  exit 2
}
if [[ "$MODE" == "apply" && "${PHDEBATE_ALLOW_SOURCE_ARCHIVE_PRUNE:-}" != "yes" ]]; then
  echo "apply requires PHDEBATE_ALLOW_SOURCE_ARCHIVE_PRUNE=yes after reviewing dry-run output." >&2
  exit 2
fi
[[ -d "$BACKUP_DIR" ]] || {
  echo "source_archive_prune_complete mode=$MODE candidates=0 bytes=0 preserved=0"
  exit 0
}

referenced_by_manifest() {
  local filename="$1" manifest
  for manifest in "$BACKUP_DIR"/recovery-set-*.manifest; do
    [[ -f "$manifest" ]] || continue
    if awk -F= -v filename="$filename" '$1 ~ /^artifact\..*\.filename$/ && $2 == filename {found=1} END {exit !found}' "$manifest"; then
      return 0
    fi
  done
  return 1
}

now="$(date +%s)"
index=0
candidates=0
candidate_bytes=0
preserved=0

while IFS= read -r row; do
  [[ -n "$row" ]] || continue
  archive="${row#* }"
  modified="${row%% *}"
  filename="$(basename "$archive")"
  age_hours=$(( (now - modified) / 3600 ))
  bytes="$(stat -c %s "$archive" 2>/dev/null || stat -f %z "$archive")"

  if referenced_by_manifest "$filename"; then
    echo "preserve kind=source_archive file=$filename reason=recovery-manifest-reference"
    preserved=$((preserved + 1))
  elif (( index < KEEP )); then
    echo "preserve kind=source_archive file=$filename reason=rollback-floor age_hours=$age_hours"
    preserved=$((preserved + 1))
    index=$((index + 1))
  elif (( age_hours < MIN_AGE_HOURS )); then
    echo "preserve kind=source_archive file=$filename reason=minimum-age age_hours=$age_hours"
    preserved=$((preserved + 1))
    index=$((index + 1))
  else
    echo "$MODE kind=source_archive file=$filename age_hours=$age_hours bytes=$bytes"
    if [[ "$MODE" == "apply" ]]; then
      find "$BACKUP_DIR" -maxdepth 1 -type f \( -name "$filename" -o -name "$filename.sha256" \) -delete
    fi
    candidates=$((candidates + 1))
    candidate_bytes=$((candidate_bytes + bytes))
    index=$((index + 1))
  fi
done < <(
  while IFS= read -r archive; do
    modified="$(stat -c %Y "$archive" 2>/dev/null || stat -f %m "$archive")"
    printf '%s %s\n' "$modified" "$archive"
  done < <(
    find "$BACKUP_DIR" -maxdepth 1 -type f \
      \( -name '*-source.tar.gz' -o -name '*-source-*.tar.gz' \) -print
  ) | sort -nr
)

echo "source_archive_prune_complete mode=$MODE candidates=$candidates bytes=$candidate_bytes preserved=$preserved keep=$KEEP min_age_hours=$MIN_AGE_HOURS"
