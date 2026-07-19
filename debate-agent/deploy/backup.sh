#!/usr/bin/env bash
set -euo pipefail
ROOT="${DEBATE_AGENT_ROOT:-/opt/debate-agent}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$ROOT/backups"
umask 077
docker compose -f "$ROOT/docker-compose.yml" exec -T postgres pg_dump -U debate_agent -Fc debate_agent >"$ROOT/backups/agent-$STAMP.dump.part"
mv "$ROOT/backups/agent-$STAMP.dump.part" "$ROOT/backups/agent-$STAMP.dump"
sha256sum "$ROOT/backups/agent-$STAMP.dump" >"$ROOT/backups/agent-$STAMP.dump.sha256"
find "$ROOT/backups" -type f -name 'agent-*.dump*' -mtime +14 -delete
echo "backup_ok agent-$STAMP.dump"
