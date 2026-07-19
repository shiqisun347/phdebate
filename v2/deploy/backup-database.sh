#!/usr/bin/env bash
set -euo pipefail

ROOT="${PHDEBATE_V2_ROOT:-/home/ubuntu/sunsq/phdebate-v2}"
BACKUP_DIR="${PHDEBATE_V2_BACKUP_DIR:-$ROOT/runtime/backups}"
DATABASE="${PHDEBATE_V2_DATABASE:-phdebate_v2}"
PORT="${PHDEBATE_V2_POSTGRES_PORT:-5433}"
RETENTION_DAYS="${PHDEBATE_V2_BACKUP_RETENTION_DAYS:-14}"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
FINAL="$BACKUP_DIR/auto-$TIMESTAMP.dump"
TEMP="$FINAL.part"
CHECKSUM_TEMP="$FINAL.sha256.part"
CHECKSUM_FINAL="$FINAL.sha256"
STATUS_TEMP="$ROOT/runtime/backup-status.json.part"
STATUS_FINAL="$ROOT/runtime/backup-status.json"

install -d -o postgres -g postgres -m 750 "$BACKUP_DIR"
cd /tmp
umask 077
exec 9>"$BACKUP_DIR/.backup.lock"
if ! flock -n 9; then
  echo "backup_skipped reason=another_backup_is_running"
  exit 0
fi

cleanup() {
  rm -f "$TEMP" "$CHECKSUM_TEMP" "$STATUS_TEMP"
}
trap cleanup EXIT

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
COMPLETED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf '{"last_success_at":"%s","file":"%s","bytes":%s,"sha256":"%s","retention_days":%s}\n' \
  "$COMPLETED_AT" "$(basename "$FINAL")" "$BYTES" "$SHA256" "$RETENTION_DAYS" >"$STATUS_TEMP"
mv "$STATUS_TEMP" "$STATUS_FINAL"
chmod 644 "$STATUS_FINAL"
echo "backup_ok file=$(basename "$FINAL") bytes=$BYTES sha256=$SHA256 retention_days=$RETENTION_DAYS"
