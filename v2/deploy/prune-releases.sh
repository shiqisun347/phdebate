#!/usr/bin/env bash
set -euo pipefail

ROOT="${PHDEBATE_V2_ROOT:-/home/ubuntu/sunsq/phdebate-v2}"
MODE="${1:-dry-run}"
KEEP="${PHDEBATE_RELEASE_KEEP:-5}"
MIN_AGE_HOURS="${PHDEBATE_RELEASE_MIN_AGE_HOURS:-24}"

if [[ "$MODE" != "dry-run" && "$MODE" != "apply" ]]; then
  echo "Usage: $0 [dry-run|apply]" >&2
  exit 2
fi
[[ "$KEEP" =~ ^[1-9][0-9]*$ ]] || {
  echo "PHDEBATE_RELEASE_KEEP must be a positive integer." >&2
  exit 2
}
[[ "$MIN_AGE_HOURS" =~ ^[0-9]+$ ]] || {
  echo "PHDEBATE_RELEASE_MIN_AGE_HOURS must be a non-negative integer." >&2
  exit 2
}

now="$(date +%s)"
removed=0
preserved=0

prune_group() {
  local label="$1" directory="$2"
  shift 2
  local -a protected=() rows=()
  local link target row modified path age_hours index=0 is_protected is_complete

  for link in "$@"; do
    target="$(realpath "$link" 2>/dev/null || true)"
    [[ -n "$target" ]] && protected+=("$target")
  done
  [[ -d "$directory" ]] || return 0

  while IFS= read -r row; do
    rows+=("$row")
  done < <(
    for path in "$directory"/*; do
      [[ -d "$path" ]] || continue
      modified="$(stat -c %Y "$path" 2>/dev/null || stat -f %m "$path")"
      printf '%s %s\n' "$modified" "$path"
    done | sort -nr
  )

  for row in "${rows[@]}"; do
    path="${row#* }"
    modified="${row%% *}"
    age_hours=$(( (now - modified) / 3600 ))
    is_protected=false
    for target in "${protected[@]-}"; do
      if [[ "$path" == "$target" ]]; then
        is_protected=true
        break
      fi
    done

    is_complete=false
    if [[ -f "$path/.release-complete" || ( "$label" == "web" && -f "$path/server.js" && -d "$path/.next/static" ) ]]; then
      is_complete=true
    fi
    if [[ "$is_complete" != true ]]; then
      echo "preserve kind=$label release=$(basename "$path") reason=incomplete-marker-missing"
      preserved=$((preserved + 1))
      continue
    fi

    if (( index < KEEP )) || [[ "$is_protected" == true ]] || (( age_hours < MIN_AGE_HOURS )); then
      echo "preserve kind=$label release=$(basename "$path") age_hours=$age_hours"
      preserved=$((preserved + 1))
    else
      echo "$MODE kind=$label release=$(basename "$path") age_hours=$age_hours"
      if [[ "$MODE" == "apply" ]]; then
        rm -rf -- "$path"
      fi
      removed=$((removed + 1))
    fi
    index=$((index + 1))
  done
}

prune_group api "$ROOT/runtime/api-releases" "$ROOT/.api-primary" "$ROOT/.api-secondary"
prune_group web "$ROOT/runtime/web-releases" "$ROOT/.web-current"
echo "release_prune_complete mode=$MODE candidates=$removed preserved=$preserved keep=$KEEP min_age_hours=$MIN_AGE_HOURS"
