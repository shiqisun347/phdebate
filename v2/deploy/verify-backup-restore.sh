#!/usr/bin/env bash
set -euo pipefail

ROOT="${PHDEBATE_V2_ROOT:-/home/ubuntu/sunsq/phdebate-v2}"
BACKUP_DIR="${PHDEBATE_V2_BACKUP_DIR:-$ROOT/runtime/backups}"
SOURCE_DATABASE="${PHDEBATE_V2_DATABASE:-phdebate_v2}"
PORT="${PHDEBATE_V2_POSTGRES_PORT:-5433}"
BACKUP="${1:-}"

if [[ -z "$BACKUP" ]]; then
  BACKUP="$(find "$BACKUP_DIR" -maxdepth 1 -type f -name 'auto-*.dump' -printf '%T@ %p\n' | sort -nr | head -n 1 | cut -d' ' -f2-)"
fi
if [[ -z "$BACKUP" || ! -f "$BACKUP" ]]; then
  echo "No automatic backup file is available." >&2
  exit 1
fi
BACKUP="$(readlink -f "$BACKUP")"
cd /tmp

CHECKSUM="$BACKUP.sha256"
if [[ ! -f "$CHECKSUM" ]]; then
  echo "Missing checksum file: $CHECKSUM" >&2
  exit 1
fi
(
  cd "$BACKUP_DIR"
  sha256sum -c "$(basename "$CHECKSUM")"
)
pg_restore -l "$BACKUP" >/dev/null

RESTORE_DATABASE="phdebate_restore_check_$(date -u +%Y%m%d%H%M%S)_$$"
WORK_DIR="$(mktemp -d)"
cleanup() {
  runuser -u postgres -- dropdb -p "$PORT" --if-exists "$RESTORE_DATABASE" >/dev/null 2>&1 || true
  rm -rf "$WORK_DIR"
}
trap cleanup EXIT

runuser -u postgres -- createdb -p "$PORT" "$RESTORE_DATABASE"
runuser -u postgres -- pg_restore -p "$PORT" --exit-on-error --no-owner --no-privileges -d "$RESTORE_DATABASE" "$BACKUP"

TABLES="$(runuser -u postgres -- psql -p "$PORT" -d "$RESTORE_DATABASE" -Atc "select tablename from pg_tables where schemaname='public' order by tablename")"
for TABLE in $TABLES; do
  SOURCE_COUNT="$(runuser -u postgres -- psql -p "$PORT" -d "$SOURCE_DATABASE" -Atc "select count(*) from \"$TABLE\"")"
  RESTORED_COUNT="$(runuser -u postgres -- psql -p "$PORT" -d "$RESTORE_DATABASE" -Atc "select count(*) from \"$TABLE\"")"
  printf '%s|%s\n' "$TABLE" "$SOURCE_COUNT" >>"$WORK_DIR/source.counts"
  printf '%s|%s\n' "$TABLE" "$RESTORED_COUNT" >>"$WORK_DIR/restored.counts"
done

diff -u "$WORK_DIR/source.counts" "$WORK_DIR/restored.counts"
SOURCE_REVISION="$(runuser -u postgres -- psql -p "$PORT" -d "$SOURCE_DATABASE" -Atc 'select version_num from alembic_version')"
RESTORED_REVISION="$(runuser -u postgres -- psql -p "$PORT" -d "$RESTORE_DATABASE" -Atc 'select version_num from alembic_version')"
test "$SOURCE_REVISION" = "$RESTORED_REVISION"
echo "restore_verified backup=$(basename "$BACKUP") database=$RESTORE_DATABASE tables=$(printf '%s\n' "$TABLES" | wc -l) alembic=$RESTORED_REVISION"
