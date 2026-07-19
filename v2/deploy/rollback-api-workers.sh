#!/usr/bin/env bash
set -euo pipefail

ROOT="${PHDEBATE_V2_ROOT:-/home/ubuntu/sunsq/phdebate-v2}"
RELEASE="${1:-}"
[[ "$RELEASE" =~ ^[A-Za-z0-9._-]+$ ]] || {
  echo "Usage: $0 <previous-release-name>" >&2
  exit 2
}
test -f "$ROOT/runtime/api-releases/$RELEASE/.release-complete"
exec "$ROOT/deploy/roll-api-workers.sh" "$RELEASE"
