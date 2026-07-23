#!/usr/bin/env bash
set -euo pipefail

ROOT="${PHDEBATE_ROOT:-/home/ubuntu/sunsq/phdebate}"
SUPERVISORCTL="${PHDEBATE_SUPERVISORCTL:-supervisorctl}"

cd "$ROOT"
set -a
# shellcheck disable=SC1091
source "$ROOT/.env"
set +a

PYTHONPATH="$ROOT/apps/api" \
  "$ROOT/.venv/bin/python" "$ROOT/deploy/assert-no-active-matches.py"

"$SUPERVISORCTL" restart jixia-engine jixia-worker
"$SUPERVISORCTL" status jixia-engine jixia-worker
echo "engine_worker_restart_complete"
