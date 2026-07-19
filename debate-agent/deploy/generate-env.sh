#!/usr/bin/env bash
set -euo pipefail

ROOT="${DEBATE_AGENT_ROOT:-/opt/debate-agent}"
TARGET="$ROOT/.env"
if [[ -e "$TARGET" ]]; then
  echo "$TARGET already exists; refusing to overwrite secrets" >&2
  exit 1
fi

umask 077
admin_password="${ADMIN_PASSWORD:-$(openssl rand -base64 24 | tr -d '\n')}"
gateway_key="${BOOTSTRAP_GATEWAY_KEY:-dba_$(openssl rand -hex 32)}"
cat >"$TARGET" <<EOF
APP_ENV=production
APP_SECRET=$(openssl rand -hex 48)
ENCRYPTION_SECRET=$(openssl rand -hex 48)
POSTGRES_PASSWORD=$(openssl rand -hex 32)
REDIS_PASSWORD=$(openssl rand -hex 32)
ADMIN_ACCOUNT=${ADMIN_ACCOUNT:-agent_admin}
ADMIN_REAL_NAME=${ADMIN_REAL_NAME:-Agent系统管理员}
ADMIN_PASSWORD=$admin_password
BOOTSTRAP_GATEWAY_KEY=$gateway_key
SESSION_HOURS=12
MAX_CONCURRENT_GENERATIONS=8
REQUEST_TIMEOUT_SECONDS=180
MEMORY_ENABLED=true
MEMORY_CANDIDATE_MAX_CHARS=1200
MEM0_ENABLED=false
MEM0_CONFIG_JSON={}
AUTO_CREATE_SCHEMA=false
EOF
chmod 600 "$TARGET"
printf 'admin_account=%s\nadmin_password=%s\ngateway_key=%s\n' "${ADMIN_ACCOUNT:-agent_admin}" "$admin_password" "$gateway_key"
