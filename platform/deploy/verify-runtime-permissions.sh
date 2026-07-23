#!/usr/bin/env bash
set -euo pipefail

ROOT="${PHDEBATE_ROOT:-/home/ubuntu/sunsq/phdebate}"
SERVICE_USER="${PHDEBATE_SERVICE_USER:-ubuntu}"
POSTGRES_DATA_DIR="${PHDEBATE_POSTGRES_DATA_DIR:-$ROOT/runtime/postgres}"

run_as_user() {
  local user="$1"
  shift
  if [[ "$(id -u)" -eq 0 ]]; then
    runuser -u "$user" -- "$@"
  elif [[ "$(id -un)" == "$user" ]]; then
    "$@"
  else
    echo "Cannot verify access for $user without root privileges" >&2
    return 1
  fi
}

test -d "$ROOT"
run_as_user "$SERVICE_USER" test -x "$ROOT"

if [[ -d "$POSTGRES_DATA_DIR" ]]; then
  POSTGRES_USER="$(stat -c '%U' "$POSTGRES_DATA_DIR")"
  if [[ "$POSTGRES_USER" == "UNKNOWN" || -z "$POSTGRES_USER" ]]; then
    echo "Unable to determine PostgreSQL data owner: $POSTGRES_DATA_DIR" >&2
    exit 1
  fi
  run_as_user "$POSTGRES_USER" test -x "$ROOT/runtime"
  run_as_user "$POSTGRES_USER" test -r "$POSTGRES_DATA_DIR/global/pg_control"
fi

case "$ROOT/.python-venvs" in
  "$ROOT/runtime"/*)
    echo "Python release directory must not be nested under the shared runtime directory" >&2
    exit 1
    ;;
esac

echo "runtime_permissions_ok root=$ROOT postgres_data=$POSTGRES_DATA_DIR"
