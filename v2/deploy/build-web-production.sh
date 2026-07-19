#!/usr/bin/env bash
set -euo pipefail

ROOT="${PHDEBATE_V2_ROOT:-/home/ubuntu/sunsq/phdebate-v2}"

set -a
# shellcheck disable=SC1091
source "$ROOT/.env"
set +a

DEPLOYMENT_MODE="${PHDEBATE_V2_WEB_DEPLOYMENT_MODE:-parallel}"
case "$DEPLOYMENT_MODE" in
  parallel)
    EXPECTED_BASE_PATH="/v2"
    ;;
  root)
    EXPECTED_BASE_PATH=""
    ;;
  *)
    echo "Unsupported web deployment mode: $DEPLOYMENT_MODE" >&2
    exit 1
    ;;
esac
if [[ "${NEXT_PUBLIC_BASE_PATH:-}" != "$EXPECTED_BASE_PATH" ]]; then
  echo "NEXT_PUBLIC_BASE_PATH must be '$EXPECTED_BASE_PATH' for deployment mode '$DEPLOYMENT_MODE'." >&2
  exit 1
fi

export PATH="$ROOT/runtime/node/bin:$PATH"
cd "$ROOT/apps/web"
node --version
npm run build
rm -rf .next/standalone/.next/static
cp -a .next/static .next/standalone/.next/static
echo "web_build_ready mode=$DEPLOYMENT_MODE base_path=${NEXT_PUBLIC_BASE_PATH:-/}"
