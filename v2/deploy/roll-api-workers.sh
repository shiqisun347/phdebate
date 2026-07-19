#!/usr/bin/env bash
set -euo pipefail

ROOT="${PHDEBATE_V2_ROOT:-/home/ubuntu/sunsq/phdebate-v2}"
RELEASE="${1:-}"
RELEASE_DIR="$ROOT/runtime/api-releases/$RELEASE"
PRIMARY_LINK="$ROOT/.api-primary"
SECONDARY_LINK="$ROOT/.api-secondary"
PRIMARY_SERVICE="jixia-v2-api"
SECONDARY_SERVICE="jixia-v2-api-secondary"
PRIMARY_URL="http://127.0.0.1:12340"
SECONDARY_URL="http://127.0.0.1:12342"
HEALTH_RETRIES="${PHDEBATE_V2_API_HEALTH_RETRIES:-30}"

usage() {
  echo "Usage: $0 <release-name>" >&2
  exit 2
}
[[ -n "$RELEASE" ]] || usage
[[ "$RELEASE" =~ ^[A-Za-z0-9._-]+$ ]] || usage
test -f "$RELEASE_DIR/.release-complete"

old_primary="$(readlink -f "$PRIMARY_LINK" 2>/dev/null || true)"
old_secondary="$(readlink -f "$SECONDARY_LINK" 2>/dev/null || true)"
switched_primary=false
switched_secondary=false

switch_link() {
  local link="$1"
  local target="$2"
  local temp="${link}.new.$$"
  ln -s "$target" "$temp"
  mv -Tf "$temp" "$link"
}

wait_healthy() {
  local url="$1" expected_instance="$2" attempt payload
  for ((attempt = 1; attempt <= HEALTH_RETRIES; attempt++)); do
    payload="$(curl --fail --silent --show-error --max-time 3 "$url/api/health" 2>/dev/null || true)"
    if "$ROOT/.venv/bin/python" -c \
      'import json,sys; p=json.loads(sys.argv[1]); assert p["ok"] is True and p["instance"] == sys.argv[2]' \
      "$payload" "$expected_instance" 2>/dev/null; then
      return 0
    fi
    sleep 1
  done
  echo "API worker failed health gate: instance=$expected_instance url=$url" >&2
  return 1
}

wait_public_healthy() {
  local attempt
  local url="${PHDEBATE_V2_PUBLIC_ORIGIN:-https://117.50.192.216}/api/health"
  for ((attempt = 1; attempt <= HEALTH_RETRIES; attempt++)); do
    if curl --fail --silent --show-error --max-time 5 "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "Public API failed post-rollout health gate: url=$url" >&2
  return 1
}

rollback() {
  local exit_code=$?
  trap - ERR
  echo "Rolling API deployment failed; restoring previous worker links." >&2
  if [[ "$switched_primary" == true && -n "$old_primary" && -d "$old_primary" ]]; then
    switch_link "$PRIMARY_LINK" "$old_primary"
    supervisorctl restart "$PRIMARY_SERVICE" || true
    wait_healthy "$PRIMARY_URL" api-primary || true
  fi
  if [[ "$switched_secondary" == true && -n "$old_secondary" && -d "$old_secondary" ]]; then
    switch_link "$SECONDARY_LINK" "$old_secondary"
    supervisorctl restart "$SECONDARY_SERVICE" || true
    wait_healthy "$SECONDARY_URL" api-secondary || true
  fi
  exit "$exit_code"
}
trap rollback ERR

# Secondary goes first so the established primary remains available while the
# candidate proves it can import, connect to PostgreSQL and serve requests.
switch_link "$SECONDARY_LINK" "$RELEASE_DIR"
switched_secondary=true
supervisorctl restart "$SECONDARY_SERVICE"
wait_healthy "$SECONDARY_URL" api-secondary

switch_link "$PRIMARY_LINK" "$RELEASE_DIR"
switched_primary=true
supervisorctl restart "$PRIMARY_SERVICE"
wait_healthy "$PRIMARY_URL" api-primary

# Exercise Nginx only after both direct ports have independently passed. A
# bounded retry absorbs the final worker's short socket hand-off interval.
wait_public_healthy
trap - ERR
echo "api_rollout_complete release=$RELEASE primary=$PRIMARY_URL secondary=$SECONDARY_URL"
