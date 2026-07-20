#!/usr/bin/env bash
set -euo pipefail

ROOT="${PHDEBATE_ROOT:-/home/ubuntu/sunsq/phdebate}"
SERVICE_USER="${PHDEBATE_SERVICE_USER:-ubuntu}"
SERVICE_GROUP="${PHDEBATE_SERVICE_GROUP:-ubuntu}"
MODE="${PHDEBATE_WEB_DEPLOYMENT_MODE:-root}"
RELEASE="${PHDEBATE_RELEASE:-$(date -u +%Y%m%dT%H%M%SZ)-$MODE}"
BUILDS="$ROOT/runtime/web-builds"
RELEASES="$ROOT/runtime/web-releases"
BUILD_DIR="$BUILDS/$RELEASE"
RELEASE_DIR="$RELEASES/$RELEASE"
SOURCE_COMMIT_FILE="$ROOT/runtime/source-commit"
SOURCE_TREE_FILE="$ROOT/runtime/source-tree"

[[ "$MODE" == "root" ]] || { echo "Only the canonical root deployment mode is supported." >&2; exit 1; }
BASE_PATH=""
if [[ -e "$BUILD_DIR" || -e "$RELEASE_DIR" ]]; then
  echo "Web release already exists: $RELEASE" >&2
  exit 1
fi
[[ "$RELEASE" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "Web release name contains unsupported characters." >&2; exit 2; }
test -s "$SOURCE_COMMIT_FILE" || { echo "Missing source provenance: $SOURCE_COMMIT_FILE" >&2; exit 1; }
test -s "$SOURCE_TREE_FILE" || { echo "Missing source provenance: $SOURCE_TREE_FILE" >&2; exit 1; }

if [[ "$(id -u)" -eq 0 ]]; then
  install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 750 "$BUILDS" "$RELEASES"
else
  install -d -m 750 "$BUILDS" "$RELEASES"
fi

run_as_service_user() {
  if [[ "$(id -u)" -eq 0 ]]; then
    runuser -u "$SERVICE_USER" -- "$@"
  else
    "$@"
  fi
}

cleanup() {
  rm -rf "$BUILD_DIR"
}
trap cleanup EXIT

run_as_service_user mkdir -m 750 "$BUILD_DIR"
run_as_service_user rsync -a \
  --exclude node_modules \
  --exclude .next \
  --exclude test-results \
  "$ROOT/apps/web/" "$BUILD_DIR/"
# Turbopack rejects a project-level node_modules symlink that escapes the build
# root. A hard-linked tree stays within the root without duplicating package
# data on the same filesystem. Root performs this copy on deployments whose
# synchronized dependency files retain a different numeric owner; the service
# user only needs read access to dependencies and still owns all build output.
if [[ "$(id -u)" -eq 0 ]]; then
  cp -al "$ROOT/apps/web/node_modules" "$BUILD_DIR/node_modules"
else
  cp -a "$ROOT/apps/web/node_modules" "$BUILD_DIR/node_modules"
fi

set -a
# shellcheck disable=SC1091
source "$ROOT/.env"
set +a
export NEXT_PUBLIC_BASE_PATH="$BASE_PATH"
export PATH="$ROOT/runtime/node/bin:$PATH"

run_as_service_user env \
  PATH="$PATH" \
  NODE_ENV=production \
  NEXT_PUBLIC_BASE_PATH="$NEXT_PUBLIC_BASE_PATH" \
  NEXT_PUBLIC_API_ORIGIN="${NEXT_PUBLIC_API_ORIGIN:-}" \
  NEXT_PUBLIC_WEBRTC_AUDIO_ENABLED="${NEXT_PUBLIC_WEBRTC_AUDIO_ENABLED:-false}" \
  API_INTERNAL_ORIGIN="${API_INTERNAL_ORIGIN:-http://127.0.0.1:12340}" \
  npm --prefix "$BUILD_DIR" run build
run_as_service_user rm -rf "$BUILD_DIR/.next/standalone/.next/static"
run_as_service_user cp -a "$BUILD_DIR/.next/static" "$BUILD_DIR/.next/standalone/.next/static"
# Existing browser tabs can still request immutable chunks from the previous
# release after the symlink switches.  Keep those hashed assets alongside the
# new build so a live match is not forced into a ChunkLoadError during deploy.
CURRENT_RELEASE="$(readlink -f "$ROOT/.web-current" 2>/dev/null || true)"
if [[ -n "$CURRENT_RELEASE" && -d "$CURRENT_RELEASE/.next/static" ]]; then
  run_as_service_user rsync -a --ignore-existing \
    "$CURRENT_RELEASE/.next/static/" \
    "$BUILD_DIR/.next/standalone/.next/static/"
fi
run_as_service_user mv "$BUILD_DIR/.next/standalone" "$RELEASE_DIR"
if [[ -d "$BUILD_DIR/public" ]]; then
  run_as_service_user cp -a "$BUILD_DIR/public" "$RELEASE_DIR/public"
fi

"$ROOT/.venv/bin/python" "$ROOT/deploy/write-release-provenance.py" \
  --kind web \
  --release "$RELEASE" \
  --commit "$(tr -d '[:space:]' <"$SOURCE_COMMIT_FILE")" \
  --tree "$(tr -d '[:space:]' <"$SOURCE_TREE_FILE")" \
  --base-path "$BASE_PATH" \
  --output "$RELEASE_DIR/.release-provenance.json"
if [[ "$(id -u)" -eq 0 ]]; then
  chown "$SERVICE_USER:$SERVICE_GROUP" "$RELEASE_DIR/.release-provenance.json"
fi
run_as_service_user install -m 640 "$RELEASE_DIR/.release-provenance.json" "$RELEASE_DIR/public/release.json"

test -f "$RELEASE_DIR/server.js"
test -f "$RELEASE_DIR/public/worklets/livekit-interrupt-gate.js"
printf '%s\n' "$RELEASE" >"$RELEASE_DIR/.release-complete"
chmod 640 "$RELEASE_DIR/.release-complete"
echo "web_release_ready release=$RELEASE mode=$MODE base_path=${BASE_PATH:-/} path=$RELEASE_DIR"
