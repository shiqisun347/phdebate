#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi

source .venv/bin/activate
python -m pip install -r apps/backend/requirements.txt
npm install
npm --prefix apps/frontend install
export PHDEBATE_AGENT_BASE_URL="${PHDEBATE_AGENT_BASE_URL:-http://127.0.0.1:8100}"

cleanup() {
  jobs -p | xargs -r kill
}
trap cleanup EXIT

(cd apps/mock_agent && PYTHONPATH=. ../../.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8100 --reload) &
(cd apps/backend && PYTHONPATH=. ../../.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload --reload-dir app) &
(cd apps/frontend && npm run dev -- --host 0.0.0.0 --port 5174) &

wait
