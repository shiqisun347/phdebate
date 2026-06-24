#!/usr/bin/env bash
set -euo pipefail
# phdebate now uses FunASR for ASR. Qwen3-TTS must not block on the retired
# Qwen ASR service at 127.0.0.1:12301, otherwise every TTS restart can hang.
if [[ "${QWEN_TTS_WAIT_FOR_ASR:-0}" == "1" ]]; then
  for i in $(seq 1 120); do
    if curl -fsS --max-time 2 http://127.0.0.1:12301/health 2>/dev/null | grep -q '"backend_ready":true'; then
      break
    fi
    sleep 5
  done
fi
cd /root/autodl-tmp/qwen3-tts-openai
set -a
source /root/autodl-tmp/qwen3-tts-openai/.env
set +a
exec /root/autodl-tmp/qwen3-tts-openai/venv/bin/python -u phdebate_tts_server.py --host 127.0.0.1 --port 12302
