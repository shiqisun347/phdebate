#!/usr/bin/env bash
set -euo pipefail

V2_ROOT="${PHDEBATE_V2_SOURCE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
ROOT="$(cd "$V2_ROOT/.." && pwd)"
AGENT_ROOT="${PHDEBATE_AGENT_SOURCE_DIR:-$ROOT/debate-agent}"
OUTPUT="${1:-$ROOT/phdebate-source-$(date -u +%Y%m%dT%H%M%SZ).tar.gz}"
STAGING="$(mktemp -d "${TMPDIR:-/tmp}/phdebate-source.XXXXXX")"

cleanup() {
  rm -rf "$STAGING"
}
trap cleanup EXIT

for required in "$V2_ROOT/apps" "$V2_ROOT/services/moss-realtime-gateway" "$V2_ROOT/deploy/openmoss" "$AGENT_ROOT/apps"; do
  if [[ ! -e "$required" ]]; then
    echo "missing required source path: $required" >&2
    exit 1
  fi
done

EXCLUDES=(
  --exclude=.env
  --exclude=.web-current
  --exclude=.api-primary
  --exclude=.api-secondary
  --exclude=.venv
  --exclude='.venv-*'
  --exclude=.quality-venv
  --exclude=node_modules
  --exclude=.next
  --exclude=__pycache__
  --exclude=.pytest_cache
  --exclude=.ruff_cache
  --exclude=.hypothesis
  --exclude='*.egg-info'
  --exclude='*.pyc'
  --exclude='*.db'
  --exclude='*.sqlite3'
  --exclude='*.tsbuildinfo'
  --exclude=.DS_Store
  --exclude=test-results
  --exclude=playwright-report
  --exclude=coverage
  --exclude=storage
  --exclude=backups
  --exclude=runtime
  --exclude=docs/qa
)

mkdir -p "$STAGING/phdebate"
rsync -a "${EXCLUDES[@]}" "$V2_ROOT/" "$STAGING/phdebate/v2/"
rsync -a "${EXCLUDES[@]}" "$AGENT_ROOT/" "$STAGING/phdebate/debate-agent/"

if find "$STAGING" -type f \( \
  -name '.env' -o -name '*.db' -o -name '*.sqlite3' -o -name '*.pyc' -o -name '*.tsbuildinfo' \
\) -print -quit | grep -q .; then
  echo "forbidden generated or runtime file entered the source bundle" >&2
  exit 1
fi

if find "$STAGING" -type d \( \
  -name node_modules -o -name .next -o -name .venv -o -name __pycache__ \
  -o -name .pytest_cache -o -name .ruff_cache -o -name .hypothesis -o -name storage \
\) -print -quit | grep -q .; then
  echo "forbidden generated or runtime directory entered the source bundle" >&2
  exit 1
fi

if command -v gitleaks >/dev/null 2>&1; then
  gitleaks detect --no-git --redact --source "$STAGING" >/dev/null
elif command -v rg >/dev/null 2>&1 && rg -l --hidden \
  -g '!**/build-migration-source-bundle.sh' \
  'BEGIN (RSA|OPENSSH|EC) PRIVATE KEY|cpa__[A-Za-z0-9_-]{20,}|3oCsZ52814XfY9b7' \
  "$STAGING" >/dev/null; then
  echo "high-confidence secret detected in the source bundle" >&2
  exit 1
elif ! command -v rg >/dev/null 2>&1 && grep -RIlE \
  --exclude=build-migration-source-bundle.sh \
  'BEGIN (RSA|OPENSSH|EC) PRIVATE KEY|cpa__[A-Za-z0-9_-]{20,}|3oCsZ52814XfY9b7' \
  "$STAGING" >/dev/null; then
  echo "high-confidence secret detected in the source bundle" >&2
  exit 1
fi

mkdir -p "$(dirname "$OUTPUT")"
COPYFILE_DISABLE=1 tar -C "$STAGING" -czf "$OUTPUT" phdebate

if command -v shasum >/dev/null 2>&1; then
  shasum -a 256 "$OUTPUT" > "$OUTPUT.sha256"
else
  sha256sum "$OUTPUT" > "$OUTPUT.sha256"
fi

echo "source bundle: $OUTPUT"
echo "checksum: $OUTPUT.sha256"
