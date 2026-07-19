#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
fi
if [[ -x "$ROOT/runtime/node/bin/node" ]]; then
  export PATH="$ROOT/runtime/node/bin:$PATH"
fi

"$ROOT/scripts/quality.sh"

cd "$ROOT/apps/web"
mkdir -p .next/standalone/.next
rm -rf .next/standalone/.next/static
cp -a .next/static .next/standalone/.next/static

cd "$ROOT"
PYTHONPATH=apps/api .venv/bin/python -m compileall -q apps/api/app apps/engine apps/worker
