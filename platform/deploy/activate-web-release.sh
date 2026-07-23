#!/usr/bin/env bash
set -euo pipefail

ROOT="${PHDEBATE_ROOT:-${PHDEBATE_ROOT:-/home/ubuntu/sunsq/phdebate}}"
RELEASE="${1:-}"
RELEASE_DIR="$ROOT/runtime/web-releases/$RELEASE"
CURRENT_LINK="$ROOT/.web-current"
SERVICE="${PHDEBATE_WEB_SERVICE:-${PHDEBATE_WEB_SERVICE:-jixia-web}}"
DIRECT_ORIGIN="${PHDEBATE_WEB_DIRECT_ORIGIN:-${PHDEBATE_WEB_DIRECT_ORIGIN:-http://127.0.0.1:12341}}"
PUBLIC_ORIGIN="${PHDEBATE_PUBLIC_ORIGIN:-${PHDEBATE_PUBLIC_ORIGIN:-https://117.50.192.216}}"
HEALTH_RETRIES="${PHDEBATE_WEB_HEALTH_RETRIES:-${PHDEBATE_WEB_HEALTH_RETRIES:-30}}"
LOCK_FILE="$ROOT/runtime/.web-deploy.lock"

usage() {
  echo "Usage: $0 <release-name>" >&2
  exit 2
}
[[ -n "$RELEASE" && "$RELEASE" =~ ^[A-Za-z0-9._-]+$ ]] || usage
[[ "$HEALTH_RETRIES" =~ ^[1-9][0-9]*$ ]] || { echo "PHDEBATE_WEB_HEALTH_RETRIES must be positive." >&2; exit 2; }
test -f "$RELEASE_DIR/.release-complete"
test -f "$RELEASE_DIR/.release-provenance.json"
test -f "$RELEASE_DIR/public/release.json"
test -f "$RELEASE_DIR/server.js"

exec 9>"$LOCK_FILE"
flock -n 9 || { echo "Another Web deployment is running." >&2; exit 3; }

old_release_dir="$(readlink -f "$CURRENT_LINK" 2>/dev/null || true)"
if [[ -z "$old_release_dir" || ! -d "$old_release_dir" || ! -f "$old_release_dir/.release-complete" ]]; then
  echo "Refusing deployment without a complete rollback release." >&2
  exit 1
fi
case "$old_release_dir" in
  "$ROOT/runtime/web-releases/"*) ;;
  *) echo "Refusing rollback release outside the managed release directory." >&2; exit 1 ;;
esac
old_release="$(basename "$old_release_dir")"
switched=false

record_value() {
  local record="$1" key="$2"
  "$ROOT/.venv/bin/python" -c \
    'import json,sys; value=json.load(open(sys.argv[1], encoding="utf-8"))[sys.argv[2]]; assert isinstance(value,str); print(value)' \
    "$record" "$key"
}

validate_record() {
  local directory="$1" expected_release="$2" record commit tree actual_release
  record="$directory/.release-provenance.json"
  actual_release="$(record_value "$record" release)"
  commit="$(record_value "$record" source_commit)"
  tree="$(record_value "$record" source_tree)"
  [[ "$actual_release" == "$expected_release" ]]
  [[ "$commit" == "$(tr -d '[:space:]' <"$ROOT/runtime/source-commit")" ]]
  [[ "$tree" == "$(tr -d '[:space:]' <"$ROOT/runtime/source-tree")" ]]
}

switch_link() {
  local target="$1" temporary
  temporary="${CURRENT_LINK}.new.$$"
  ln -s "$target" "$temporary"
  "$ROOT/.venv/bin/python" -c 'import os,sys; os.replace(sys.argv[1], sys.argv[2])' "$temporary" "$CURRENT_LINK"
}

wait_healthy() {
  local origin="$1" directory="$2" expected_release="$3" attempt payload base_path
  base_path="$(record_value "$directory/.release-provenance.json" base_path)"
  for ((attempt = 1; attempt <= HEALTH_RETRIES; attempt++)); do
    payload="$(curl --fail --silent --show-error --max-time 5 \
      -H 'Cache-Control: no-cache' \
      "$origin${base_path}/release.json?deployment=$expected_release" 2>/dev/null || true)"
    if "$ROOT/.venv/bin/python" -c \
      'import json,sys; p=json.loads(sys.argv[1]); assert p["schema_version"] == 1 and p["release"] == sys.argv[2] and p["service"] == "phdebate-web"' \
      "$payload" "$expected_release" 2>/dev/null \
      && curl --fail --silent --show-error --max-time 5 \
        -H 'Cache-Control: no-cache' "$origin${base_path}/?deployment=$expected_release" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "Web release failed health gate: release=$expected_release origin=$origin" >&2
  return 1
}

rollback() {
  local exit_code=$?
  trap - ERR
  if [[ "$switched" == true ]]; then
    echo "Web deployment failed; restoring release=$old_release" >&2
    switch_link "$old_release_dir"
    supervisorctl restart "$SERVICE" || true
    wait_healthy "$DIRECT_ORIGIN" "$old_release_dir" "$old_release" || true
    wait_healthy "$PUBLIC_ORIGIN" "$old_release_dir" "$old_release" || true
  fi
  exit "$exit_code"
}
trap rollback ERR

validate_record "$RELEASE_DIR" "$RELEASE"
switch_link "$RELEASE_DIR"
switched=true
supervisorctl restart "$SERVICE"
wait_healthy "$DIRECT_ORIGIN" "$RELEASE_DIR" "$RELEASE"
wait_healthy "$PUBLIC_ORIGIN" "$RELEASE_DIR" "$RELEASE"

trap - ERR
echo "web_rollout_complete release=$RELEASE previous=$old_release direct=$DIRECT_ORIGIN public=$PUBLIC_ORIGIN"
