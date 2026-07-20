#!/usr/bin/env bash
set -euo pipefail

ROOT="${PHDEBATE_ROOT:-/home/ubuntu/sunsq/phdebate}"
SERVICE_USER="${PHDEBATE_SERVICE_USER:-ubuntu}"
SERVICE_GROUP="${PHDEBATE_SERVICE_GROUP:-ubuntu}"

if [[ "$(id -u)" -eq 0 ]]; then
  # `runtime` is a shared traversal boundary. Service-specific children keep
  # their own restrictive ownership; never make the parent private to one
  # service account because PostgreSQL, Redis and the web/API processes all
  # need to cross it.
  install -d -o root -g root -m 751 "$ROOT/runtime"
  install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 750 \
    "$ROOT/runtime/logs" \
    "$ROOT/runtime/redis" \
    "$ROOT/.python-venvs"
  install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 750 \
    "$ROOT/storage/audio" \
    "$ROOT/storage/archives"
  chown -R "$SERVICE_USER:$SERVICE_GROUP" \
    "$ROOT/storage/audio" \
    "$ROOT/storage/archives"
else
  test -x "$ROOT/runtime"
  install -d -m 750 \
    "$ROOT/runtime/logs" \
    "$ROOT/runtime/redis" \
    "$ROOT/.python-venvs"
  install -d -m 750 \
    "$ROOT/storage/audio" \
    "$ROOT/storage/archives"
fi

"$(dirname "$0")/verify-runtime-permissions.sh"
echo "runtime_ready root=$ROOT service_user=$SERVICE_USER"
