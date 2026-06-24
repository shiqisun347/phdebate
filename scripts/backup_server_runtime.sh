#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="${PHDEBATE_ROOT_DIR:-/root/autodl-tmp/phdebate}"
BACKUP_DIR="${PHDEBATE_BACKUP_DIR:-/root/autodl-tmp/phdebate-runtime-backups}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT_DIR="${BACKUP_DIR}/${STAMP}"

mkdir -p "${OUT_DIR}"

STORAGE_DIR="${ROOT_DIR}/apps/backend/storage"
DB_PATH="${STORAGE_DIR}/phdebate.sqlite3"
DB_COPY="${OUT_DIR}/phdebate.sqlite3"

if [[ -f "${DB_PATH}" ]]; then
  python3 - "${DB_PATH}" "${DB_COPY}" <<'PY'
import sqlite3
import sys

source, target = sys.argv[1], sys.argv[2]
src = sqlite3.connect(source)
dst = sqlite3.connect(target)
with dst:
    src.backup(dst)
dst.close()
src.close()
PY
fi

tar -C "${ROOT_DIR}/apps/backend" \
  --exclude='storage/phdebate.sqlite3' \
  --exclude='storage/phdebate.sqlite3-wal' \
  --exclude='storage/phdebate.sqlite3-shm' \
  -czf "${OUT_DIR}/backend-storage-files.tgz" storage

tar -C / --ignore-failed-read -czf "${OUT_DIR}/deploy-and-service-code.tgz" \
  etc/supervisor/conf.d \
  etc/nginx \
  livekit.yaml \
  root/autodl-tmp/qwen3-tts-openai/phdebate_tts_server.py \
  root/autodl-tmp/qwen3-tts-openai/start_after_asr.sh \
  root/autodl-tmp/qwen3-tts-webui/app.py \
  root/autodl-tmp/phdebate/serve_realtime_ws_phdebate.py

(
  cd "${OUT_DIR}"
  sha256sum backend-storage-files.tgz deploy-and-service-code.tgz > SHA256SUMS.txt
  if [[ -f phdebate.sqlite3 ]]; then
    sha256sum phdebate.sqlite3 >> SHA256SUMS.txt
  fi
)

echo "${OUT_DIR}"
