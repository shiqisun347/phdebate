#!/usr/bin/env bash
set -euo pipefail

ROOT="${PHDEBATE_ROOT:-/home/ubuntu/sunsq/phdebate}"
AGENT_ROOT="${PHDEBATE_AGENT_ROOT:-/home/ubuntu/sunsq/debate-agent}"
BACKUP_DIR="${PHDEBATE_DEPLOY_BACKUP_DIR:-$ROOT/runtime/deploy-backups}"
PLATFORM_DATABASE_BACKUP_DIR="${PHDEBATE_BACKUP_DIR:-$ROOT/runtime/backups}"
AGENT_DATABASE_BACKUP_DIR="${PHDEBATE_AGENT_BACKUP_DIR:-$AGENT_ROOT/backups}"
VERIFY_INDEX_SCRIPT="${PHDEBATE_RECOVERY_INDEX_VERIFY_SCRIPT:-$ROOT/deploy/verify-recovery-index.py}"
MODE="${1:-dry-run}"
KEEP="${PHDEBATE_RECOVERY_KEEP:-3}"
MIN_AGE_HOURS="${PHDEBATE_RECOVERY_MIN_AGE_HOURS:-24}"

if [[ "$MODE" != "dry-run" && "$MODE" != "apply" ]]; then
  echo "Usage: $0 [dry-run|apply]" >&2
  exit 2
fi
[[ "$KEEP" =~ ^[1-9][0-9]*$ ]] || {
  echo "PHDEBATE_RECOVERY_KEEP must be a positive integer." >&2
  exit 2
}
[[ "$MIN_AGE_HOURS" =~ ^[0-9]+$ ]] || {
  echo "PHDEBATE_RECOVERY_MIN_AGE_HOURS must be a non-negative integer." >&2
  exit 2
}
[[ -d "$BACKUP_DIR" ]] || {
  echo "recovery_prune_complete mode=$MODE candidates=0 preserved=0 keep=$KEEP min_age_hours=$MIN_AGE_HOURS"
  exit 0
}

now="$(date +%s)"
declare -a valid_rows=()
declare -a retained_manifests=()
declare -a protected_data=()
removed=0
preserved=0
unsafe_manifests=0

value_for() {
  local manifest="$1" key="$2"
  awk -F= -v key="$key" '$1 == key {sub(/^[^=]*=/, ""); print; found=1; exit} END {if (!found) exit 1}' "$manifest"
}

safe_filename() {
  [[ "$1" =~ ^[A-Za-z0-9._-]+$ && "$1" != .* ]]
}

array_contains() {
  local expected="$1" item
  shift
  for item in "$@"; do
    [[ "$item" == "$expected" ]] && return 0
  done
  return 1
}

manifest_is_complete() {
  local manifest="$1" role filename bytes checksum schema roles

  schema="$(value_for "$manifest" schema_version 2>/dev/null || true)"
  case "$schema" in
    2) roles="v2_database agent_database data_volumes private_config reliable_voice moss_offline" ;;
    3) roles="platform_database agent_database data_volumes private_config reliable_voice moss_offline" ;;
    *) return 1 ;;
  esac
  for role in $roles; do
    filename="$(value_for "$manifest" "artifact.$role.filename" 2>/dev/null || true)"
    bytes="$(value_for "$manifest" "artifact.$role.bytes" 2>/dev/null || true)"
    checksum="$(value_for "$manifest" "artifact.$role.sha256" 2>/dev/null || true)"
    safe_filename "$filename" || return 1
    [[ "$bytes" =~ ^[0-9]+$ ]] || return 1
    [[ "$checksum" =~ ^[A-Fa-f0-9]{64}$ ]] || return 1
  done
  return 0
}

while IFS= read -r row; do
  manifest="${row#* }"
  if ! manifest_is_complete "$manifest"; then
    echo "preserve kind=manifest file=$(basename "$manifest") reason=unsupported-or-incomplete"
    preserved=$((preserved + 1))
    unsafe_manifests=$((unsafe_manifests + 1))
    continue
  fi
  data_filename="$(value_for "$manifest" artifact.data_volumes.filename 2>/dev/null || true)"
  if ! safe_filename "$data_filename"; then
    echo "preserve kind=manifest file=$(basename "$manifest") reason=unsafe-data-artifact"
    preserved=$((preserved + 1))
    continue
  fi
  valid_rows+=("$row")
done < <(
  for manifest in "$BACKUP_DIR"/recovery-set-*.manifest; do
    [[ -f "$manifest" ]] || continue
    modified="$(stat -c %Y "$manifest" 2>/dev/null || stat -f %m "$manifest")"
    printf '%s %s\n' "$modified" "$manifest"
  done | sort -nr
)

# Verify the complete recovery index in one process. Shared heavyweight files
# such as the offline voice runtime are hashed once even when many manifests
# reference them. Any missing, ambiguous, truncated, or corrupt artifact stops
# the entire prune operation before a candidate can be removed.
if (( unsafe_manifests == 0 && ${#valid_rows[@]} > 0 )); then
  manifest_paths=()
  for row in "${valid_rows[@]}"; do
    manifest_paths+=("${row#* }")
  done
  if [[ ! -f "$VERIFY_INDEX_SCRIPT" ]] || ! python3 "$VERIFY_INDEX_SCRIPT" \
      --artifact-root "$BACKUP_DIR" \
      --artifact-root "$PLATFORM_DATABASE_BACKUP_DIR" \
      --artifact-root "$AGENT_DATABASE_BACKUP_DIR" \
      "${manifest_paths[@]}" >/dev/null 2>&1; then
    echo "preserve kind=recovery_index reason=artifact-verification-failed manifests=${#valid_rows[@]}"
    preserved=$((preserved + ${#valid_rows[@]}))
    unsafe_manifests=$((unsafe_manifests + 1))
  fi
fi

# A manifest that cannot be interpreted safely may still be the only index for
# a recovery artifact. Never delete around it: an operator must repair or
# explicitly quarantine the manifest before pruning can resume.
if (( unsafe_manifests > 0 )); then
  echo "recovery_prune_complete mode=$MODE candidates=0 preserved=$preserved keep=$KEEP min_age_hours=$MIN_AGE_HOURS reason=unsafe-manifests-present"
  exit 0
fi

index=0
for row in "${valid_rows[@]-}"; do
  [[ -n "$row" ]] || continue
  manifest="${row#* }"
  modified="${row%% *}"
  age_hours=$(( (now - modified) / 3600 ))
  data_filename="$(value_for "$manifest" artifact.data_volumes.filename)"
  if (( index < KEEP )) || (( age_hours < MIN_AGE_HOURS )); then
    retained_manifests+=("$manifest")
    if ! array_contains "$data_filename" "${protected_data[@]-}"; then
      protected_data+=("$data_filename")
    fi
    echo "preserve kind=manifest file=$(basename "$manifest") age_hours=$age_hours"
    preserved=$((preserved + 1))
  fi
  index=$((index + 1))
done

# Never prune data-volume archives unless enough valid recovery manifests exist
# to satisfy the configured rollback floor.
if (( ${#retained_manifests[@]} < KEEP )); then
  echo "recovery_prune_complete mode=$MODE candidates=0 preserved=$preserved keep=$KEEP min_age_hours=$MIN_AGE_HOURS reason=insufficient-valid-manifests"
  exit 0
fi

for row in "${valid_rows[@]-}"; do
  [[ -n "$row" ]] || continue
  manifest="${row#* }"
  if array_contains "$manifest" "${retained_manifests[@]-}"; then
    continue
  fi
  modified="${row%% *}"
  age_hours=$(( (now - modified) / 3600 ))
  echo "$MODE kind=manifest file=$(basename "$manifest") age_hours=$age_hours"
  if [[ "$MODE" == "apply" ]]; then
    find "$BACKUP_DIR" -maxdepth 1 -type f -name "$(basename "$manifest")" -delete
  fi
  removed=$((removed + 1))
done

while IFS= read -r archive; do
  filename="$(basename "$archive")"
  if array_contains "$filename" "${protected_data[@]-}"; then
    continue
  fi
  modified="$(stat -c %Y "$archive" 2>/dev/null || stat -f %m "$archive")"
  age_hours=$(( (now - modified) / 3600 ))
  (( age_hours >= MIN_AGE_HOURS )) || {
    echo "preserve kind=data_volumes file=$filename age_hours=$age_hours"
    preserved=$((preserved + 1))
    continue
  }
  echo "$MODE kind=data_volumes file=$filename age_hours=$age_hours"
  if [[ "$MODE" == "apply" ]]; then
    find "$BACKUP_DIR" -maxdepth 1 -type f \( -name "$filename" -o -name "$filename.sha256" \) -delete
  fi
  removed=$((removed + 1))
done < <(find "$BACKUP_DIR" -maxdepth 1 -type f -name '*-data-volumes.tar.gz' | sort)

echo "recovery_prune_complete mode=$MODE candidates=$removed preserved=$preserved keep=$KEEP min_age_hours=$MIN_AGE_HOURS"
