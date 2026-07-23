#!/usr/bin/env bash
set -euo pipefail

# Synchronize a reviewed GitHub checkout into the live source tree without
# touching server-owned configuration, data, build outputs or voice runtimes.
SOURCE="${1:-/home/ubuntu/sunsq/phdebate-source}"
DESTINATION="${PHDEBATE_ROOT:-/home/ubuntu/sunsq/phdebate}"
MODE="${2:-apply}"

if [[ "$MODE" != "apply" && "$MODE" != "dry-run" ]]; then
  echo "Usage: $0 [source checkout] [apply|dry-run]" >&2
  exit 2
fi
if [[ ! -d "$SOURCE/platform" || ! -f "$SOURCE/platform/README.md" ]]; then
  echo "Refusing to sync: source is not a phdebate GitHub checkout: $SOURCE" >&2
  exit 2
fi
if [[ ! -d "$SOURCE/.git" ]]; then
  echo "Refusing to sync an unversioned source directory: $SOURCE" >&2
  exit 2
fi

SOURCE_PLATFORM="$(cd "$SOURCE/platform" && pwd -P)"
DESTINATION="$(cd "$DESTINATION" && pwd -P)"
if [[ "$SOURCE_PLATFORM" == "$DESTINATION" ]]; then
  echo "Refusing to synchronize a directory onto itself." >&2
  exit 2
fi

commit="$(git -C "$SOURCE" rev-parse --verify HEAD)"
tree="$(git -C "$SOURCE" rev-parse --verify 'HEAD^{tree}')"
if ! git -C "$SOURCE" diff --quiet || ! git -C "$SOURCE" diff --cached --quiet; then
  echo "Refusing to deploy a checkout with tracked local changes." >&2
  exit 2
fi

rsync_args=(
  -rlp --checksum --delete --itemize-changes
  --exclude='/.env'
  --exclude='/runtime/'
  --exclude='/**/runtime/'
  --exclude='/storage/'
  --exclude='/backups/'
  --exclude='/.venv/'
  --exclude='/.python-venvs/'
  --exclude='/.quality-venv/'
  --exclude='/**/.venv*/'
  --exclude='/**/__pycache__/'
  --exclude='/**/*.pyc'
  --exclude='/**/*.egg-info/'
  --exclude='/.api-primary'
  --exclude='/.api-secondary'
  --exclude='/.web-current'
  --exclude='/.transcript-collab-current'
  --exclude='/.transcript-collab-releases/'
  --exclude='/apps/web/node_modules/'
  --exclude='/apps/web/.next/'
  --exclude='/assets/moss-prompts/audio/'
)
if [[ "$MODE" == "dry-run" ]]; then
  rsync_args+=(--dry-run)
fi

echo "source_commit=$commit source_tree=$tree mode=$MODE"
rsync "${rsync_args[@]}" "$SOURCE_PLATFORM/" "$DESTINATION/"

if [[ "$MODE" == "apply" ]]; then
  printf '%s\n' "$commit" >"$DESTINATION/runtime/source-commit"
  printf '%s\n' "$tree" >"$DESTINATION/runtime/source-tree"
  chmod 640 "$DESTINATION/runtime/source-commit"
  chmod 640 "$DESTINATION/runtime/source-tree"
fi
