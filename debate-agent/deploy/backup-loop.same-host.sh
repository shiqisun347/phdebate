#!/usr/bin/env bash
set -euo pipefail

ROOT="${DEBATE_AGENT_ROOT:-/home/ubuntu/sunsq/debate-agent}"
while true; do
  if "$ROOT/deploy/backup.same-host.sh"; then
    sleep "${DEBATE_AGENT_BACKUP_INTERVAL_SECONDS:-86400}"
  else
    sleep "${DEBATE_AGENT_BACKUP_RETRY_SECONDS:-300}"
  fi
done
