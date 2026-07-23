#!/usr/bin/env bash
set -euo pipefail

if [[ "${OPENMOSS_DEPLOY_ENABLE:-}" != "I_UNDERSTAND_THIS_STARTS_A_GPU_GATEWAY" ]]; then
  echo "OpenMOSS deployment is disabled; set the exact OPENMOSS_DEPLOY_ENABLE acknowledgement" >&2
  exit 2
fi

usage() {
  echo "usage: $0 --endpoint NAME --port PORT --gpu-uuid UUID --gpu-fingerprint SHA256 --prompt-dir DIR --upstream-checkout DIR --model-path DIR --tokenizer-path DIR --codec-model-path DIR --api-key-file FILE --run-manifest FILE" >&2
  exit 2
}

ENDPOINT=""
PORT=""
GPU_UUID=""
GPU_FINGERPRINT=""
PROMPT_DIR=""
UPSTREAM_CHECKOUT=""
MODEL_PATH=""
TOKENIZER_PATH=""
CODEC_MODEL_PATH=""
API_KEY_FILE=""
RUN_MANIFEST=""

while (($#)); do
  case "$1" in
    --endpoint) ENDPOINT="${2:-}"; shift 2 ;;
    --port) PORT="${2:-}"; shift 2 ;;
    --gpu-uuid) GPU_UUID="${2:-}"; shift 2 ;;
    --gpu-fingerprint) GPU_FINGERPRINT="${2:-}"; shift 2 ;;
    --prompt-dir) PROMPT_DIR="${2:-}"; shift 2 ;;
    --upstream-checkout) UPSTREAM_CHECKOUT="${2:-}"; shift 2 ;;
    --model-path) MODEL_PATH="${2:-}"; shift 2 ;;
    --tokenizer-path) TOKENIZER_PATH="${2:-}"; shift 2 ;;
    --codec-model-path) CODEC_MODEL_PATH="${2:-}"; shift 2 ;;
    --api-key-file) API_KEY_FILE="${2:-}"; shift 2 ;;
    --run-manifest) RUN_MANIFEST="${2:-}"; shift 2 ;;
    *) usage ;;
  esac
done

[[ -n "$ENDPOINT" && -n "$PORT" && -n "$GPU_UUID" && -n "$GPU_FINGERPRINT" ]] || usage
[[ -n "$PROMPT_DIR" && -n "$UPSTREAM_CHECKOUT" && -n "$API_KEY_FILE" && -n "$RUN_MANIFEST" ]] || usage
[[ -n "$MODEL_PATH" && -n "$TOKENIZER_PATH" && -n "$CODEC_MODEL_PATH" ]] || usage

command -v flock >/dev/null || { echo "flock is required for exclusive GPU admission" >&2; exit 2; }
GPU_LOCK_ID="${GPU_UUID//[^A-Za-z0-9_-]/_}"
GPU_LOCK_DIR="${MOSS_GPU_LOCK_DIR:-/run/lock/phdebate-openmoss}"
[[ -d "$GPU_LOCK_DIR" && ! -L "$GPU_LOCK_DIR" && -w "$GPU_LOCK_DIR" ]] || {
  echo "protected GPU lock directory is missing or not writable: $GPU_LOCK_DIR" >&2
  exit 2
}
exec 9>"$GPU_LOCK_DIR/${GPU_LOCK_ID}.lock"
flock -n 9 || { echo "the selected GPU UUID is already leased by another gateway" >&2; exit 2; }

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
GATEWAY_DIR="$REPO_ROOT/services/moss-realtime-gateway"
PYTHON_BIN="${MOSS_GATEWAY_PYTHON:-$GATEWAY_DIR/.venv/bin/python}"
[[ -x "$PYTHON_BIN" ]] || { echo "gateway Python is not executable: $PYTHON_BIN" >&2; exit 2; }

"$PYTHON_BIN" "$SCRIPT_DIR/preflight.py" \
  --endpoint "$ENDPOINT" \
  --port "$PORT" \
  --gpu-uuid "$GPU_UUID" \
  --expected-gpu-fingerprint "$GPU_FINGERPRINT" \
  --prompt-dir "$PROMPT_DIR" \
  --upstream-checkout "$UPSTREAM_CHECKOUT" \
  --model-path "$MODEL_PATH" \
  --tokenizer-path "$TOKENIZER_PATH" \
  --codec-model-path "$CODEC_MODEL_PATH" \
  --api-key-file "$API_KEY_FILE" \
  --run-manifest "$RUN_MANIFEST"

export CUDA_DEVICE_ORDER=PCI_BUS_ID
export CUDA_VISIBLE_DEVICES="$GPU_UUID"
export MOSS_GATEWAY_BACKEND=openmoss
export MOSS_GATEWAY_API_KEY
MOSS_GATEWAY_API_KEY="$(<"$API_KEY_FILE")"
export MOSS_GATEWAY_PROMPT_DIR="$PROMPT_DIR"
export MOSS_GATEWAY_UPSTREAM_CHECKOUT="$UPSTREAM_CHECKOUT"
export MOSS_GATEWAY_MODEL_PATH="$MODEL_PATH"
export MOSS_GATEWAY_TOKENIZER_PATH="$TOKENIZER_PATH"
export MOSS_GATEWAY_CODEC_MODEL_PATH="$CODEC_MODEL_PATH"
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export HF_DATASETS_OFFLINE=1
export PYTHONPATH="$UPSTREAM_CHECKOUT/moss_tts_realtime:$GATEWAY_DIR${PYTHONPATH:+:$PYTHONPATH}"
export MOSS_GATEWAY_UPSTREAM_REVISION=ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af
export MOSS_GATEWAY_MODEL_REVISION=6acbc7f161a0db71c291f2d0aaa9eee59334cab2
export MOSS_GATEWAY_CODEC_REVISION=3cd226ba2947efa357ef453bcad111b6eafba782
export MOSS_GATEWAY_DEVICE=cuda:0
export MOSS_GATEWAY_ATTN_IMPL="${MOSS_GATEWAY_ATTN_IMPL:-sdpa}"
export MOSS_GATEWAY_MIN_GPU_TOTAL_MEMORY_GB=24
export MOSS_GATEWAY_MIN_GPU_FREE_MEMORY_GB=20
export MOSS_GATEWAY_ALLOW_SUB24GB_DIAGNOSTIC=false
export MOSS_GATEWAY_DIAGNOSTIC_CODEC_ENCODER_OFFLOAD=false
unset MOSS_GATEWAY_DIAGNOSTIC_CODEC_DEVICE
export MOSS_GATEWAY_REQUIRE_EIGHT_PROMPTS=true
export MOSS_GATEWAY_SAMPLE_RATE=24000
export MOSS_GATEWAY_CONTROL_ACK_TIMEOUT_SECONDS="${MOSS_GATEWAY_CONTROL_ACK_TIMEOUT_SECONDS:-30}"
export MOSS_GATEWAY_TERMINAL_GRACE_SECONDS="${MOSS_GATEWAY_TERMINAL_GRACE_SECONDS:-15}"
export MOSS_GATEWAY_FINAL_GENERATION_TIMEOUT_SECONDS="${MOSS_GATEWAY_FINAL_GENERATION_TIMEOUT_SECONDS:-180}"
export MOSS_GATEWAY_STARTUP_TIMEOUT_SECONDS=900
export MOSS_GATEWAY_FAIL_FAST_URL=
export MOSS_GATEWAY_WARMUP_TEXT='现在开始普通话实时语音预热。'
# Warm the exact server-owned user instruction separately from the assistant
# text stream. Both prefixes affect fixed-shape CUDA graph capture.
export MOSS_GATEWAY_WARMUP_USER_TEXT='请用自信、坚定、有说服力的中文辩论语气，语速比自然朗读快约百分之十，重音清晰，停顿简短，句尾有力，但不要喊叫或夸张。'
export MOSS_GATEWAY_PROMPT_CHUNK_SECONDS=0.24
export MOSS_GATEWAY_DECODE_CHUNK_FRAMES="${MOSS_GATEWAY_DECODE_CHUNK_FRAMES:-12}"
export MOSS_GATEWAY_DECODE_OVERLAP_FRAMES=0
export MOSS_GATEWAY_INITIAL_CHUNK_FRAMES="${MOSS_GATEWAY_INITIAL_CHUNK_FRAMES:-6}"
export MOSS_GATEWAY_DYNAMO_CACHE_SIZE_LIMIT="${MOSS_GATEWAY_DYNAMO_CACHE_SIZE_LIMIT:-64}"
export MOSS_GATEWAY_LOCAL_COMPILE_MODE="${MOSS_GATEWAY_LOCAL_COMPILE_MODE:-reduce-overhead}"
export MOSS_GATEWAY_LOCAL_COMPILE_DYNAMIC="${MOSS_GATEWAY_LOCAL_COMPILE_DYNAMIC:-false}"
export MOSS_GATEWAY_ASYNC_DECODER_ENABLED="${MOSS_GATEWAY_ASYNC_DECODER_ENABLED:-false}"
export MOSS_GATEWAY_ASYNC_DECODER_QUEUE_SIZE="${MOSS_GATEWAY_ASYNC_DECODER_QUEUE_SIZE:-2}"
export MOSS_GATEWAY_ASYNC_DECODER_JOIN_TIMEOUT_SECONDS="${MOSS_GATEWAY_ASYNC_DECODER_JOIN_TIMEOUT_SECONDS:-5}"
export MOSS_GATEWAY_MAX_LENGTH=10000
export MOSS_GATEWAY_TEMPERATURE=0.8
export MOSS_GATEWAY_TOP_P=0.6
export MOSS_GATEWAY_TOP_K=30
export MOSS_GATEWAY_DO_SAMPLE="${MOSS_GATEWAY_DO_SAMPLE:-true}"
export MOSS_GATEWAY_REPETITION_PENALTY=1.1
export MOSS_GATEWAY_REPETITION_WINDOW=50
export MOSS_GATEWAY_VOICE_PROMPTS_JSON='{"debate_voice_1":"candidate_voice_1.wav","debate_voice_2":"candidate_voice_2.wav","debate_voice_3":"candidate_voice_3.wav","debate_voice_4":"candidate_voice_4.wav","debate_voice_5":"candidate_voice_5.wav","debate_voice_6":"candidate_voice_6.wav","debate_voice_7":"candidate_voice_7.wav","debate_voice_8":"candidate_voice_8.wav"}'

cd "$GATEWAY_DIR"
"$PYTHON_BIN" -m uvicorn moss_realtime_gateway.app:app \
  --host 127.0.0.1 --port "$PORT" --workers 1 --log-level info &
GATEWAY_PID=$!

terminate_gateway() {
  trap - TERM INT HUP EXIT
  kill -TERM "$GATEWAY_PID" 2>/dev/null || true
  for _ in $(seq 1 20); do
    kill -0 "$GATEWAY_PID" 2>/dev/null || break
    sleep 1
  done
  if kill -0 "$GATEWAY_PID" 2>/dev/null; then
    kill -KILL "$GATEWAY_PID" 2>/dev/null || true
  fi
  wait "$GATEWAY_PID" 2>/dev/null || true
  exit 143
}
trap terminate_gateway TERM INT HUP EXIT

"$PYTHON_BIN" "$SCRIPT_DIR/wait_ready.py" \
  --url "http://127.0.0.1:$PORT/health/ready" \
  --api-key-file "$API_KEY_FILE" \
  --timeout-seconds "${MOSS_GATEWAY_READINESS_TIMEOUT_SECONDS:-900}" \
  --pid "$GATEWAY_PID" \
  --evidence "$RUN_MANIFEST.ready.json"

if wait "$GATEWAY_PID"; then
  STATUS=0
else
  STATUS=$?
fi
trap - TERM INT HUP EXIT
exit "$STATUS"
