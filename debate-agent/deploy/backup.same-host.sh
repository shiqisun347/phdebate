#!/usr/bin/env bash
set -euo pipefail

ROOT="${DEBATE_AGENT_ROOT:-/home/ubuntu/sunsq/debate-agent}"
set -a
# shellcheck disable=SC1091
source "$ROOT/.env"
set +a
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
install -d -m 700 "$ROOT/backups"
umask 077
export PGPASSWORD="$POSTGRES_PASSWORD"
pg_dump \
  -h "${POSTGRES_HOST:-127.0.0.1}" \
  -p "${POSTGRES_PORT:-5433}" \
  -U "${POSTGRES_USER:-debate_agent}" \
  -d "${POSTGRES_DB:-debate_agent}" \
  -Fc >"$ROOT/backups/agent-$STAMP.dump.part"
mv "$ROOT/backups/agent-$STAMP.dump.part" "$ROOT/backups/agent-$STAMP.dump"
sha256sum "$ROOT/backups/agent-$STAMP.dump" >"$ROOT/backups/agent-$STAMP.dump.sha256"
find "$ROOT/backups" -type f -name 'agent-*.dump*' -mtime +14 -delete
echo "backup_ok agent-$STAMP.dump"
