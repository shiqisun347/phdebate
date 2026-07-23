#!/usr/bin/env bash
set -euo pipefail

ROOT="${PHDEBATE_ROOT:-/home/ubuntu/sunsq/phdebate}"
BACKUP_DIR="${PHDEBATE_BACKUP_DIR:-$ROOT/runtime/backups}"
DATABASE="${PHDEBATE_DATABASE:-phdebate}"
PORT="${PHDEBATE_POSTGRES_PORT:-5433}"
RETENTION_DAYS="${PHDEBATE_BACKUP_RETENTION_DAYS:-14}"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
FINAL="$BACKUP_DIR/auto-$TIMESTAMP.dump"
TEMP="$FINAL.part"
CHECKSUM_TEMP="$FINAL.sha256.part"
CHECKSUM_FINAL="$FINAL.sha256"
STATUS_TEMP="$ROOT/runtime/backup-status.json.part"
STATUS_FINAL="$ROOT/runtime/backup-status.json"
STATUS_WRITER="$ROOT/deploy/backup_status.py"

install -d -o postgres -g postgres -m 750 "$BACKUP_DIR"
cd /tmp
umask 077
exec 9>"$BACKUP_DIR/.backup.lock"
if ! flock -n 9; then
  echo "backup_skipped reason=another_backup_is_running"
  exit 0
fi

backup_succeeded=false
cleanup() {
  exit_code=$?
  rm -f "$TEMP" "$CHECKSUM_TEMP" "$STATUS_TEMP"
  if [[ "$backup_succeeded" != true && "$exit_code" -ne 0 ]]; then
    python3 "$STATUS_WRITER" --status-file "$STATUS_FINAL" --state failed --error-code backup_command_failed || true
  fi
}
trap cleanup EXIT

python3 "$STATUS_WRITER" --status-file "$STATUS_FINAL" --state running

runuser -u postgres -- pg_dump -p "$PORT" -Fc --no-owner --no-privileges -f "$TEMP" "$DATABASE"
pg_restore -l "$TEMP" >/dev/null
chmod 600 "$TEMP"
(
  cd "$BACKUP_DIR"
  sha256sum "$(basename "$TEMP")" | sed 's/\.part$//' >"$CHECKSUM_TEMP"
)
mv "$TEMP" "$FINAL"
mv "$CHECKSUM_TEMP" "$CHECKSUM_FINAL"
chmod 600 "$FINAL" "$CHECKSUM_FINAL"

find "$BACKUP_DIR" -maxdepth 1 -type f -name 'auto-*.dump' -mtime "+$RETENTION_DAYS" -delete
find "$BACKUP_DIR" -maxdepth 1 -type f -name 'auto-*.dump.sha256' -mtime "+$RETENTION_DAYS" -delete

BYTES="$(stat -c %s "$FINAL")"
SHA256="$(sha256sum "$FINAL" | awk '{print $1}')"
python3 "$STATUS_WRITER" --status-file "$STATUS_FINAL" --state succeeded --artifact "$FINAL" --retention-days "$RETENTION_DAYS"
backup_succeeded=true
echo "backup_ok file=$(basename "$FINAL") bytes=$BYTES sha256=$SHA256 retention_days=$RETENTION_DAYS"
