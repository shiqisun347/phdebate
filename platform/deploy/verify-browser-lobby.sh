#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PHDEBATE_TEST_PYTHON:-$ROOT/.venv/bin/python}"
API_PORT="${PHDEBATE_TEST_API_PORT:-18200}"
WEB_PORT="${PHDEBATE_TEST_WEB_PORT:-13200}"
WORK_DIR="$(mktemp -d)"
API_LOG="$WORK_DIR/api.log"
WEB_LOG="$WORK_DIR/web.log"
API_PID=""
WEB_PID=""

cleanup() {
  for pid in "$WEB_PID" "$API_PID"; do
    if [[ -n "$pid" ]]; then
      kill "$pid" >/dev/null 2>&1 || true
      wait "$pid" >/dev/null 2>&1 || true
    fi
  done
  rm -rf "$WORK_DIR"
}
trap cleanup EXIT

wait_for_url() {
  local url="$1"
  local log="$2"
  for _ in $(seq 1 80); do
    if curl -fsS --max-time 2 "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.25
  done
  echo "Timed out waiting for $url" >&2
  tail -80 "$log" >&2 || true
  return 1
}

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Python runtime is not executable: $PYTHON_BIN" >&2
  exit 1
fi

(
  cd "$ROOT/apps/api"
  exec env \
    APP_ENV=test \
    APP_SECRET=browser-lobby-test-secret \
    DATABASE_URL="sqlite:///$WORK_DIR/browser-lobby.db" \
    REDIS_URL=redis://127.0.0.1:1/15 \
    PUBLIC_ORIGIN="http://127.0.0.1:$WEB_PORT" \
    ALLOWED_ORIGINS="http://127.0.0.1:$WEB_PORT" \
    COOKIE_SECURE=false \
    AGENT_MOCK=true \
    AGENT_API_URL=http://127.0.0.1:1/api/debate \
    ENGINE_ENABLED=false \
    PRESENCE_RESET_ON_STARTUP=false \
    ARCHIVE_ROOT="$WORK_DIR/archives" \
    MEDIA_ROOT="$WORK_DIR/audio" \
    BACKUP_STATUS_FILE="$WORK_DIR/backup-status.json" \
    V2_ADMIN_ACCOUNT=browser_admin \
    V2_ADMIN_REAL_NAME=浏览器管理员 \
    V2_ADMIN_PASSWORD=Browser-admin-1234 \
    "$PYTHON_BIN" -m uvicorn app.main:app --host 127.0.0.1 --port "$API_PORT" --no-access-log
) >"$API_LOG" 2>&1 &
API_PID=$!
wait_for_url "http://127.0.0.1:$API_PORT/api/health" "$API_LOG"

(
  cd "$ROOT/apps/web"
  exec env \
    NEXT_PUBLIC_BASE_PATH= \
    NEXT_PUBLIC_API_ORIGIN="http://127.0.0.1:$API_PORT" \
    API_INTERNAL_ORIGIN="http://127.0.0.1:$API_PORT" \
    ./node_modules/.bin/next dev --hostname 127.0.0.1 --port "$WEB_PORT"
) >"$WEB_LOG" 2>&1 &
WEB_PID=$!
wait_for_url "http://127.0.0.1:$WEB_PORT/" "$WEB_LOG"

cd "$ROOT/apps/web"
E2E_BASE_URL="http://127.0.0.1:$WEB_PORT/" \
E2E_MUTATING=true \
E2E_ADMIN_ACCOUNT=browser_admin \
E2E_ADMIN_PASSWORD=Browser-admin-1234 \
  ./node_modules/.bin/playwright test e2e/authenticated-lobby.spec.ts e2e/admin-console.spec.ts
