#!/usr/bin/env bash
set -euo pipefail

PG_ISREADY="${PHDEBATE_PG_ISREADY:-/usr/bin/pg_isready}"
HOST="${PHDEBATE_POSTGRES_HOST:-127.0.0.1}"
PORT="${PHDEBATE_POSTGRES_PORT:-5433}"
DATABASE="${PHDEBATE_DATABASE:-phdebate}"
INTERVAL_SECONDS="${PHDEBATE_DATABASE_WAIT_INTERVAL_SECONDS:-2}"
TIMEOUT_SECONDS="${PHDEBATE_DATABASE_WAIT_TIMEOUT_SECONDS:-0}"

if [[ "${1:-}" == "--" ]]; then
  shift
fi
if [[ "$#" -eq 0 ]]; then
  echo "A command is required after the PostgreSQL readiness wait" >&2
  exit 2
fi
if [[ ! -x "$PG_ISREADY" ]]; then
  echo "pg_isready is not executable: $PG_ISREADY" >&2
  exit 2
fi

STARTED_AT="$(date +%s)"
NEXT_LOG_AT="$STARTED_AT"
while ! "$PG_ISREADY" -h "$HOST" -p "$PORT" -d "$DATABASE" -q; do
  NOW="$(date +%s)"
  ELAPSED=$((NOW - STARTED_AT))
  if (( TIMEOUT_SECONDS > 0 && ELAPSED >= TIMEOUT_SECONDS )); then
    echo "database_wait_timeout host=$HOST port=$PORT database=$DATABASE elapsed_seconds=$ELAPSED" >&2
    exit 1
  fi
  if (( NOW >= NEXT_LOG_AT )); then
    echo "database_waiting host=$HOST port=$PORT database=$DATABASE elapsed_seconds=$ELAPSED"
    NEXT_LOG_AT=$((NOW + 30))
  fi
  sleep "$INTERVAL_SECONDS"
done

echo "database_ready host=$HOST port=$PORT database=$DATABASE"
exec "$@"
