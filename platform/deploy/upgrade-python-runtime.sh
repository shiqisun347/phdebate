#!/usr/bin/env bash
set -euo pipefail

ROOT="${PHDEBATE_ROOT:-/home/ubuntu/sunsq/phdebate}"
PYTHON_BIN="${PHDEBATE_PYTHON_BIN:-/usr/bin/python3}"
SERVICE_USER="${PHDEBATE_SERVICE_USER:-ubuntu}"
SERVICE_GROUP="${PHDEBATE_SERVICE_GROUP:-ubuntu}"
SUPERVISORCTL="${PHDEBATE_SUPERVISORCTL:-supervisorctl}"
SHADOW_PORT="${PHDEBATE_SHADOW_PORT:-12342}"
API_PORT="${PHDEBATE_API_PORT:-12340}"
RELEASE="${PHDEBATE_RELEASE:-$(date -u +%Y%m%dT%H%M%SZ)}"
RELEASES="$ROOT/.python-venvs"
RELEASE_DIR="$RELEASES/$RELEASE"
LOCK_FILE="$RELEASES/.upgrade.lock"
SERVICES=(jixia-api jixia-engine jixia-worker)
SHADOW_LOG="$ROOT/runtime/logs/python-runtime-shadow-$RELEASE.log"
SWITCH_STARTED=false
PREVIOUS_TARGET=""
CURRENT_IS_DIRECTORY=false

if [[ "$(id -u)" -eq 0 ]]; then
  install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 750 "$RELEASES"
else
  install -d -m 750 "$RELEASES"
fi

assert_runtime_access() {
  local postgres_data_dir="${PHDEBATE_POSTGRES_DATA_DIR:-$ROOT/runtime/postgres}"
  local postgres_user

  test -x "$ROOT"
  run_as_service_user test -x "$ROOT" || {
    echo "Service user $SERVICE_USER cannot traverse the application root: $ROOT" >&2
    return 1
  }
  if [[ -d "$postgres_data_dir" && "$(id -u)" -eq 0 ]]; then
    postgres_user="$(stat -c '%U' "$postgres_data_dir")"
    if [[ "$postgres_user" == "UNKNOWN" || -z "$postgres_user" ]]; then
      echo "Unable to determine PostgreSQL data owner: $postgres_data_dir" >&2
      return 1
    fi
    runuser -u "$postgres_user" -- test -r "$postgres_data_dir/global/pg_control" || {
      echo "PostgreSQL cannot read its control file through the shared runtime path: $postgres_data_dir" >&2
      return 1
    }
  fi
}
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "python_runtime_upgrade_skipped reason=another_upgrade_is_running" >&2
  exit 1
fi

if [[ -e "$RELEASE_DIR" ]]; then
  echo "Release already exists: $RELEASE_DIR" >&2
  exit 1
fi

run_as_service_user() {
  if [[ "$(id -u)" -eq 0 ]]; then
    runuser -u "$SERVICE_USER" -- "$@"
  else
    "$@"
  fi
}

assert_runtime_access

wait_for_url() {
  local url="$1"
  local attempts="${2:-60}"
  local response
  for ((attempt = 1; attempt <= attempts; attempt += 1)); do
    if response="$(curl -fsS --max-time 3 "$url" 2>/dev/null)"; then
      printf '%s\n' "$response"
      return 0
    fi
    sleep 0.5
  done
  echo "Timed out waiting for $url" >&2
  return 1
}

stop_services() {
  "$SUPERVISORCTL" stop "${SERVICES[@]}"
}

start_services() {
  "$SUPERVISORCTL" start "${SERVICES[@]}"
}

rollback() {
  local status="$?"
  if [[ "$SWITCH_STARTED" == true && -n "$PREVIOUS_TARGET" ]]; then
    echo "python_runtime_rollback target=$PREVIOUS_TARGET" >&2
    "$SUPERVISORCTL" stop "${SERVICES[@]}" >/dev/null 2>&1 || true
    if [[ -e "$PREVIOUS_TARGET" ]]; then
      rm -f "$ROOT/.venv.rollback-link"
      ln -s "$PREVIOUS_TARGET" "$ROOT/.venv.rollback-link"
      mv -Tf "$ROOT/.venv.rollback-link" "$ROOT/.venv"
    elif [[ ! -e "$ROOT/.venv" ]]; then
      echo "python_runtime_rollback_failed reason=previous_environment_missing" >&2
    fi
    "$SUPERVISORCTL" start "${SERVICES[@]}" >/dev/null 2>&1 || true
  fi
  exit "$status"
}
trap rollback ERR

"$PYTHON_BIN" -m venv "$RELEASE_DIR"
"$RELEASE_DIR/bin/pip" install --disable-pip-version-check --quiet --upgrade pip
"$RELEASE_DIR/bin/pip" install --disable-pip-version-check --quiet -r "$ROOT/apps/api/requirements.txt"
"$RELEASE_DIR/bin/pip" check
if [[ "$(id -u)" -eq 0 ]]; then
  chown -R "$SERVICE_USER:$SERVICE_GROUP" "$RELEASE_DIR"
fi

run_as_service_user test -x "$RELEASE_DIR/bin/python"
run_as_service_user "$RELEASE_DIR/bin/python" -m uvicorn --version
run_as_service_user "$RELEASE_DIR/bin/python" -m dramatiq --version
run_as_service_user env PYTHONPATH="$ROOT/apps/api" "$RELEASE_DIR/bin/python" -c "from app.main import app; print(app.title)"

(
  cd "$ROOT/apps/api"
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
  exec "$RELEASE_DIR/bin/python" -m uvicorn app.main:app \
    --host 127.0.0.1 --port "$SHADOW_PORT" --no-access-log --log-level warning
) >"$SHADOW_LOG" 2>&1 &
SHADOW_PID=$!
cleanup_shadow() {
  if [[ -n "${SHADOW_PID:-}" ]]; then
    kill "$SHADOW_PID" >/dev/null 2>&1 || true
    wait "$SHADOW_PID" >/dev/null 2>&1 || true
    SHADOW_PID=""
  fi
}
trap cleanup_shadow EXIT
wait_for_url "http://127.0.0.1:$SHADOW_PORT/api/health"
wait_for_url "http://127.0.0.1:$SHADOW_PORT/api/health/ready"
cleanup_shadow
trap - EXIT

if [[ "${PHDEBATE_SKIP_SWITCH:-false}" == "true" ]]; then
  echo "python_runtime_verified release=$RELEASE switch=skipped"
  trap - ERR
  exit 0
fi

if [[ -L "$ROOT/.venv" ]]; then
  PREVIOUS_TARGET="$(readlink -f "$ROOT/.venv")"
elif [[ -d "$ROOT/.venv" ]]; then
  PREVIOUS_TARGET="$RELEASES/legacy-$RELEASE"
  CURRENT_IS_DIRECTORY=true
else
  echo "Current virtual environment is missing: $ROOT/.venv" >&2
  exit 1
fi

SWITCH_STARTED=true
stop_services
if [[ "$CURRENT_IS_DIRECTORY" == true ]]; then
  mv "$ROOT/.venv" "$PREVIOUS_TARGET"
fi
rm -f "$ROOT/.venv.next-link"
ln -s "$RELEASE_DIR" "$ROOT/.venv.next-link"
mv -Tf "$ROOT/.venv.next-link" "$ROOT/.venv"
start_services
wait_for_url "http://127.0.0.1:$API_PORT/api/health"
wait_for_url "http://127.0.0.1:$API_PORT/api/health/ready"
"$SUPERVISORCTL" status "${SERVICES[@]}"

SWITCH_STARTED=false
trap - ERR
echo "python_runtime_upgraded release=$RELEASE previous=$PREVIOUS_TARGET"
