#!/usr/bin/env bash
set -euo pipefail

ROOT="${DEBATE_AGENT_ROOT:-/home/ubuntu/sunsq/debate-agent}"
set -a
# shellcheck disable=SC1091
source "$ROOT/.env"
set +a
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
install -d -m 700 "$ROOT/backups"
install -d -m 750 "$ROOT/runtime"
umask 077
exec 9>"$ROOT/backups/.backup.lock"
if ! flock -n 9; then
  echo "backup_skipped reason=another_backup_is_running"
  exit 0
fi
STATUS_FILE="$ROOT/runtime/backup-status.json"
STATUS_WRITER="$ROOT/deploy/backup_status.py"
STATUS_GROUP="${DEBATE_AGENT_STATUS_GROUP:-ubuntu}"
FINAL="$ROOT/backups/agent-$STAMP.dump"
TEMP="$FINAL.part"
write_status() {
  python3 "$STATUS_WRITER" --status-file "$STATUS_FILE" "$@"
  chgrp "$STATUS_GROUP" "$STATUS_FILE"
  chmod 640 "$STATUS_FILE"
}
backup_succeeded=false
cleanup() {
  exit_code=$?
  rm -f "$TEMP"
  if [[ "$backup_succeeded" != true && "$exit_code" -ne 0 ]]; then
    write_status --state failed --error-code backup_command_failed || true
  fi
}
trap cleanup EXIT
write_status --state running
export PGPASSWORD="$POSTGRES_PASSWORD"
pg_dump \
  -h "${POSTGRES_HOST:-127.0.0.1}" \
  -p "${POSTGRES_PORT:-5433}" \
  -U "${POSTGRES_USER:-debate_agent}" \
  -d "${POSTGRES_DB:-debate_agent}" \
  -Fc >"$TEMP"
pg_restore -l "$TEMP" >/dev/null
mv "$TEMP" "$FINAL"
sha256sum "$FINAL" >"$FINAL.sha256"
find "$ROOT/backups" -type f -name 'agent-*.dump*' -mtime +14 -delete
write_status --state succeeded --artifact "$FINAL" --retention-days 14
backup_succeeded=true
echo "backup_ok agent-$STAMP.dump"
