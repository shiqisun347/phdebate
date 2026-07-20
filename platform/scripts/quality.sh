#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
if [[ -n "${PHDEBATE_PYTHON_BIN_DIR:-}" ]]; then
  PY="$PHDEBATE_PYTHON_BIN_DIR"
elif [[ -x "$ROOT/.quality-venv/bin/python" ]]; then
  PY="$ROOT/.quality-venv/bin"
else
  PY="$ROOT/.venv/bin"
fi
if [[ -x "$ROOT/runtime/node/bin/node" ]]; then
  export PATH="$ROOT/runtime/node/bin:$PATH"
fi
node -e '
const [major, minor] = process.versions.node.split(".").map(Number);
if (major < 20 || (major === 20 && minor < 19)) {
  console.error(`Node ${process.versions.node} is unsupported; use Node >=20.19.0.`);
  process.exit(2);
}
'
for tool in ruff bandit pip-audit pytest; do
  if [[ ! -x "$PY/$tool" ]]; then
    echo "Missing quality tool: $PY/$tool" >&2
    echo "Create .quality-venv and install requirements-quality.txt." >&2
    exit 2
  fi
done

cd "$ROOT"
# Legacy QA evidence and the frozen realtime-audio baseline intentionally keep
# their byte-for-byte history. Formatting the whole repository would rewrite
# verified artifacts, so the executable production/test surface is linted
# without using a full-tree formatter as a false release gate.
"$PY/ruff" check \
  apps/api/app \
  apps/api/tests \
  apps/engine \
  apps/worker \
  deploy \
  scripts \
  services/moss-realtime-gateway
# High-severity findings block releases. Existing low/medium findings are
# reviewed separately; several are intentional bounded subprocess/streaming
# clients and must not turn the entire quality entrypoint into a permanent
# false-red gate.
"$PY/bandit" -c pyproject.toml -r apps/api/app apps/engine apps/worker -q -lll
"$PY/pip-audit" -r apps/api/requirements.txt

PYTHONPATH="$ROOT:$ROOT/apps/api" "$PY/pytest" -q apps/api/tests

cd "$ROOT/apps/web"
npm run lint
npx tsc --noEmit
npm test
npm run build
npm audit --audit-level=high
