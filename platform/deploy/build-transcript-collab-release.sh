#!/usr/bin/env bash
set -euo pipefail

ROOT="${PHDEBATE_ROOT:-/home/ubuntu/sunsq/phdebate}"
SOURCE="$ROOT/services/transcript-collab"
RELEASE="${PHDEBATE_COLLAB_RELEASE:-$(date -u +%Y%m%dT%H%M%SZ)}"
RELEASES="$ROOT/.transcript-collab-releases"
TARGET="$RELEASES/$RELEASE"
CURRENT="$ROOT/.transcript-collab-current"
SERVICE_USER="${PHDEBATE_SERVICE_USER:-ubuntu}"
SERVICE_GROUP="${PHDEBATE_SERVICE_GROUP:-ubuntu}"
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
node_major="$($NODE -p 'Number(process.versions.node.split(".")[0])')"
if [[ ! "$node_major" =~ ^[0-9]+$ || "$node_major" -lt 22 ]]; then
  echo "Transcript collaboration requires the managed Node.js runtime to be version 22 or newer." >&2
  exit 2
fi
if [[ -e "$TARGET" ]]; then
  echo "Release already exists: $TARGET" >&2
  exit 2
fi

completed=false
cleanup() {
  if [[ "$completed" != true ]]; then
    rm -rf "$TARGET"
  fi
}
trap cleanup EXIT

if [[ "$(id -u)" -eq 0 ]]; then
  install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 750 "$RELEASES" "$TARGET"
else
  install -d -m 750 "$RELEASES" "$TARGET"
fi
rsync -rlp --delete \
  --exclude='/node_modules/' \
  --exclude='/dist/' \
  --exclude='/.env' \
  "$SOURCE/" "$TARGET/"

(
  cd "$TARGET"
  # npm's executable uses /usr/bin/env node for package lifecycle commands.
  # Pin PATH as well as the npm binary so builds cannot silently fall back to
  # an older system Node after the managed runtime has passed admission.
  export PATH="$ROOT/runtime/node/bin:$PATH"
  "$NPM" ci --ignore-scripts
  "$NPM" run check
  "$NPM" prune --omit=dev --ignore-scripts
)

# A deployment shell can inherit a restrictive umask from secret provisioning.
# Normalize the immutable release so Supervisor's unprivileged service account
# can traverse and read it without making the source or dependencies writable.
if [[ "$(id -u)" -eq 0 ]]; then
  chown -R "$SERVICE_USER:$SERVICE_GROUP" "$TARGET"
fi
find "$TARGET" -type d -exec chmod 750 {} +
find "$TARGET" -type f -exec chmod 640 {} +

temporary="$CURRENT.new.$$"
ln -s "$TARGET" "$temporary"
mv -Tf "$temporary" "$CURRENT"
completed=true
echo "transcript_collab_release=$RELEASE current=$TARGET"
