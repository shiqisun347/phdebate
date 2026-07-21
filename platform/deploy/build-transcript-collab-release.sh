#!/usr/bin/env bash
set -euo pipefail

ROOT="${PHDEBATE_ROOT:-/home/ubuntu/sunsq/phdebate}"
SOURCE="$ROOT/services/transcript-collab"
RELEASE="${PHDEBATE_COLLAB_RELEASE:-$(date -u +%Y%m%dT%H%M%SZ)}"
RELEASES="$ROOT/.transcript-collab-releases"
TARGET="$RELEASES/$RELEASE"
CURRENT="$ROOT/.transcript-collab-current"
NODE="$ROOT/runtime/node/bin/node"
NPM="$ROOT/runtime/node/bin/npm"

if [[ ! "$RELEASE" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "Invalid transcript collaboration release name." >&2
  exit 2
fi
if [[ ! -x "$NODE" || ! -x "$NPM" || ! -f "$SOURCE/package-lock.json" ]]; then
  echo "Transcript collaboration source or managed Node runtime is missing." >&2
  exit 2
fi
if [[ -e "$TARGET" ]]; then
  echo "Release already exists: $TARGET" >&2
  exit 2
fi

install -d -m 750 "$RELEASES" "$TARGET"
rsync -rlp --delete \
  --exclude='/node_modules/' \
  --exclude='/dist/' \
  --exclude='/.env' \
  "$SOURCE/" "$TARGET/"

(
  cd "$TARGET"
  "$NPM" ci --ignore-scripts
  "$NPM" run check
  "$NPM" prune --omit=dev --ignore-scripts
)

temporary="$CURRENT.new.$$"
ln -s "$TARGET" "$temporary"
mv -Tf "$temporary" "$CURRENT"
echo "transcript_collab_release=$RELEASE current=$TARGET"
