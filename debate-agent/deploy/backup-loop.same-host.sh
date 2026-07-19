#!/usr/bin/env bash
set -euo pipefail

ROOT="${DEBATE_AGENT_ROOT:-/home/ubuntu/sunsq/debate-agent}"
while true; do
  "$ROOT/deploy/backup.same-host.sh"
  sleep 86400
done
