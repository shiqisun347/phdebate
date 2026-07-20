#!/usr/bin/env bash
set -euo pipefail

ROOT="${PHDEBATE_ROOT:-/home/ubuntu/sunsq/phdebate}"
SERVICE_USER="${PHDEBATE_SERVICE_USER:-ubuntu}"
SERVICE_GROUP="${PHDEBATE_SERVICE_GROUP:-ubuntu}"
RELEASE="${PHDEBATE_API_RELEASE:-$(date -u +%Y%m%dT%H%M%SZ)-api}"
RELEASES="$ROOT/runtime/api-releases"
RELEASE_DIR="$RELEASES/$RELEASE"
SOURCE_COMMIT_FILE="$ROOT/runtime/source-commit"
SOURCE_TREE_FILE="$ROOT/runtime/source-tree"

[[ "$RELEASE" =~ ^[A-Za-z0-9._-]+$ ]] || {
  echo "API release name may only contain letters, numbers, dot, underscore and dash." >&2
  exit 2
}
if [[ -e "$RELEASE_DIR" ]]; then
  echo "API release already exists: $RELEASE_DIR" >&2
  exit 1
fi
test -s "$SOURCE_COMMIT_FILE" || { echo "Missing source provenance: $SOURCE_COMMIT_FILE" >&2; exit 1; }
test -s "$SOURCE_TREE_FILE" || { echo "Missing source provenance: $SOURCE_TREE_FILE" >&2; exit 1; }

if [[ "$(id -u)" -eq 0 ]]; then
  install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 750 "$RELEASES"
  install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 750 "$RELEASE_DIR/apps"
else
  install -d -m 750 "$RELEASE_DIR/apps"
fi

cleanup() {
  if [[ ! -f "$RELEASE_DIR/.release-complete" ]]; then
    rm -rf "$RELEASE_DIR"
  fi
}
trap cleanup EXIT

rsync -a --delete \
  --exclude '__pycache__/' \
  --exclude '.pytest_cache/' \
  --exclude '*.pyc' \
  "$ROOT/apps/api/" "$RELEASE_DIR/apps/api/"
install -m 640 "$ROOT/apps/__init__.py" "$RELEASE_DIR/apps/__init__.py"

# Source syncs can preserve a developer workstation's numeric uid/gid. The
# immutable release must always be traversable by the configured service user.
if [[ "$(id -u)" -eq 0 ]]; then
  chown -R "$SERVICE_USER:$SERVICE_GROUP" "$RELEASE_DIR"
fi

MEDIA_ROOT="$ROOT/storage/audio" \
ARCHIVE_ROOT="$ROOT/storage/archives" \
RESEARCH_EXPORT_ROOT="$ROOT/storage/research-exports" \
BACKUP_STATUS_FILE="$ROOT/runtime/backup-status.json" \
PYTHONPATH="$RELEASE_DIR/apps/api" \
  "$ROOT/.venv/bin/python" -c 'from app.main import app; assert app.title'
find "$RELEASE_DIR" -type d -exec chmod 750 {} +
find "$RELEASE_DIR" -type f -exec chmod 640 {} +
"$ROOT/.venv/bin/python" "$ROOT/deploy/write-release-provenance.py" \
  --kind api \
  --release "$RELEASE" \
  --commit "$(tr -d '[:space:]' <"$SOURCE_COMMIT_FILE")" \
  --tree "$(tr -d '[:space:]' <"$SOURCE_TREE_FILE")" \
  --output "$RELEASE_DIR/.release-provenance.json"
if [[ "$(id -u)" -eq 0 ]]; then
  chown "$SERVICE_USER:$SERVICE_GROUP" "$RELEASE_DIR/.release-provenance.json"
fi
printf '%s\n' "$RELEASE" >"$RELEASE_DIR/.release-complete"
chmod 640 "$RELEASE_DIR/.release-complete"

echo "api_release_ready release=$RELEASE path=$RELEASE_DIR"
