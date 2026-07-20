# OpenMOSS fixed-codec split placement audit

- Date: 2026-07-18 (Asia/Shanghai)
- Scope: source audit, checkpoint accounting, gateway implementation and local regression tests
- Production deployment/change: none
- Current decision: **CANARY-WORTHY, NOT RELEASE-PROVEN**

## Question

Can the fixed MOSS Audio Tokenizer first encode all eight voice prompts on a 12GB
GPU, then move its encoder to CPU while keeping the quantizer and decoder on GPU,
and only then load the BF16 MOSS-TTS-Realtime talker?

The implementation must not delete an upstream module or assume that the root codec
streaming context ignores the encoder.

## Fixed source evidence

The audit read the exact fixed artifacts configured by the gateway:

| Artifact | Revision | Audited file SHA-256 |
|---|---|---|
| OpenMOSS/MOSS-TTS | `ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af` | `streaming_mossttsrealtime.py`: `04e894d3588b907a5d2eda16607033cc575e641a81b3d9d771b454b343eec63d` |
| OpenMOSS-Team/MOSS-Audio-Tokenizer | `3cd226ba2947efa357ef453bcad111b6eafba782` | `modeling_moss_audio_tokenizer.py`: `65cae7744845f1b8ac65957e918cea508efe331a38e87b882b7530b6c8d7caa5` |

The fixed codec source establishes the following call graph:

```text
codec.decode(codes)
  -> codec._decode_frame(codes)
     -> codec.quantizer.decode_codes(codes)
     -> for module in codec.decoder: module(...)
```

`decode()` and `_decode_frame()` do not call `codec.encoder`.

However, the root `codec.streaming(batch_size=1)` implementation invokes
`self.apply(_start)`, which traverses every child module, including the encoder.
Consequently, it would still initialize encoder streaming state after an encoder
CPU offload. The split implementation does **not** call that root context. It enters
only the top-level decoder modules' `streaming()` contexts, matching the fixed
upstream chunked-decode implementation. Prompt audio is supplied with
`session.set_voice_prompt_tokens()`, so the session does not call waveform encoding
during a debate turn.

No encoder, decoder or quantizer module is deleted, replaced with `Identity`, or
monkey-patched. Before offload, startup also proves that encoder parameters do not
share Python parameter objects with the quantizer/decoder graph.

## Fixed checkpoint accounting

Read-only `safetensors.safe_open(...).get_slice()` metadata inspection was run on
the already-downloaded fixed production-canary artifacts. It did not load a model,
allocate GPU memory, stop a service or change deployment state.

| Component | Tensor count | Static weight size |
|---|---:|---:|
| Codec encoder | 685 | 3.3029GiB FP32 |
| Codec decoder | 685 | 3.3029GiB FP32 |
| Codec quantizer | 230 | 0.0050GiB FP32 |
| Complete codec | 1,600 | 6.6108GiB FP32 |
| Realtime talker | 403 | 4.3436GiB BF16 |

After encoder offload, the intended static CUDA weights are approximately
`3.3079 + 4.3436 = 7.6515GiB`. On a 12GiB card this leaves roughly 4.35GiB before
CUDA context, compiled graphs, KV cache and activations. This is materially more
credible than the complete all-CUDA stack that previously OOMed near 11.62GiB, but
metadata accounting alone does not prove runtime fit or latency.

## Implemented diagnostic path

The gateway now supports an explicit, mutually exclusive setting:

```bash
MOSS_GATEWAY_ALLOW_SUB24GB_DIAGNOSTIC=true
MOSS_GATEWAY_DIAGNOSTIC_CODEC_ENCODER_OFFLOAD=true
```

The default 24GB total / 20GB free floor and all-CUDA placement remain unchanged.
The new path executes this exact order:

1. verify fixed OpenMOSS checkout and fixed model/codec revisions;
2. load the complete FP32 codec on CUDA;
3. encode eight distinct allowlisted prompt WAV files and cache CPU token arrays;
4. verify encoder/quantizer/decoder parameter separation and CUDA placement;
5. move only the encoder to CPU, synchronize, collect and empty the CUDA cache;
6. verify decoder and quantizer are still wholly on the configured CUDA device;
7. load the BF16 Realtime talker on CUDA;
8. warm all eight voices using cached prompt tokens and decoder-only streaming.

`/health/ready` now returns a placement object such as:

```json
{
  "diagnostic_codec_encoder_offload": true,
  "placement": {
    "mode": "diagnostic_encoder_cpu_decoder_cuda",
    "realtime_model": "cuda:0",
    "codec_encoder": "cpu",
    "codec_quantizer": "cuda:0",
    "codec_decoder": "cuda:0",
    "codec_streaming_context": "decoder_only",
    "prompt_tokens_cached": 8
  }
}
```

Startup fails closed for duplicate prompt paths, missing/parameterless fixed codec
subgraphs, shared encoder/decode parameters, or unexpected device placement. Whole-
codec CPU placement and encoder-only offload cannot be enabled together.

## Regression evidence

```text
pytest services/moss-realtime-gateway/tests/test_gateway.py
34 passed, 1 warning

ruff check services/moss-realtime-gateway/moss_realtime_gateway \
  services/moss-realtime-gateway/tests
All checks passed
```

Coverage includes configuration fail-closed behavior, environment opt-in, parameter
separation, post-offload device invariants, prompt-before-offload-before-talker order,
decoder-only streaming context selection, placement health fields and the existing
gateway protocol/lifecycle suite.

## Real 12GB canary command

The following command is intended for the existing isolated canary directory after
copying the current gateway source to
`/home/ubuntu/sunsq/moss-realtime-12gb-canary-20260718/gateway-codec-split`.
It does not change V2 routing or feature flags. On the production host it must be run
only in an approved maintenance window after confirming no active match, with a trap
that restores LightTTS.

```bash
cd /home/ubuntu/sunsq/moss-realtime-12gb-canary-20260718
API_KEY="$(openssl rand -hex 24)"
export MOSS_TTS_REALTIME_API_KEY="$API_KEY"
export MOSS_GATEWAY_BACKEND=openmoss
export MOSS_GATEWAY_API_KEY="$API_KEY"
export MOSS_GATEWAY_PROMPT_DIR="$PWD/prompts"
export MOSS_GATEWAY_UPSTREAM_CHECKOUT="$PWD/MOSS-TTS"
export MOSS_GATEWAY_MODEL_PATH="$PWD/model"
export MOSS_GATEWAY_TOKENIZER_PATH="$PWD/model"
export MOSS_GATEWAY_CODEC_MODEL_PATH="$PWD/codec"
export MOSS_GATEWAY_UPSTREAM_REVISION=ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af
export MOSS_GATEWAY_MODEL_REVISION=6acbc7f161a0db71c291f2d0aaa9eee59334cab2
export MOSS_GATEWAY_CODEC_REVISION=3cd226ba2947efa357ef453bcad111b6eafba782
export MOSS_GATEWAY_DEVICE=cuda:0
export MOSS_GATEWAY_ATTN_IMPL=sdpa
export MOSS_GATEWAY_ALLOW_SUB24GB_DIAGNOSTIC=true
export MOSS_GATEWAY_MIN_GPU_TOTAL_MEMORY_GB=12
export MOSS_GATEWAY_MIN_GPU_FREE_MEMORY_GB=10
export MOSS_GATEWAY_DIAGNOSTIC_CODEC_ENCODER_OFFLOAD=true
unset MOSS_GATEWAY_DIAGNOSTIC_CODEC_DEVICE
export MOSS_GATEWAY_STARTUP_TIMEOUT_SECONDS=1800
export MOSS_GATEWAY_VOICE_PROMPTS_JSON='{
  "debate_voice_1":"candidate_voice_1.wav",
  "debate_voice_2":"candidate_voice_2.wav",
  "debate_voice_3":"candidate_voice_3.wav",
  "debate_voice_4":"candidate_voice_4.wav",
  "debate_voice_5":"candidate_voice_5.wav",
  "debate_voice_6":"candidate_voice_6.wav",
  "debate_voice_7":"candidate_voice_7.wav",
  "debate_voice_8":"candidate_voice_8.wav"
}'
export PYTHONPATH="$PWD/MOSS-TTS/moss_tts_realtime:$PWD/gateway-codec-split"

venv/bin/uvicorn moss_realtime_gateway.app:app \
  --host 127.0.0.1 --port 18890 --log-level info
```

After readiness reports the exact placement above, run the existing formal WS tool
from a checkout containing the benchmark script:

```bash
MOSS_TTS_REALTIME_API_KEY="$API_KEY" .venv/bin/python \
  scripts/benchmark_moss_realtime_sessions.py \
  --endpoint http://127.0.0.1:18890 \
  --voice-prompt debate_voice_1=unused.wav \
  --voice-prompt debate_voice_2=unused.wav \
  --voice-prompt debate_voice_3=unused.wav \
  --voice-prompt debate_voice_4=unused.wav \
  --voice-prompt debate_voice_5=unused.wav \
  --voice-prompt debate_voice_6=unused.wav \
  --voice-prompt debate_voice_7=unused.wav \
  --voice-prompt debate_voice_8=unused.wav \
  --concurrency 1 --rounds 20 --timeout-seconds 180 \
  --output-dir docs/qa/audio/20260718-openmoss-rebuild/codec-split-real-c1
```

One gateway has intentional capacity one. Formal two-to-three-match evidence still
requires two or three independent GPU endpoints; it cannot be claimed from concurrent
requests sent to this single process.

## Decision boundary

The split is source-valid and memory-plausible, and it directly addresses both prior
12GB failures: all-CUDA OOM and unusably slow whole-codec CPU prompt encoding. It is
therefore the next justified real canary rather than an unverified module-deletion
hack.

It is **not** release evidence yet. The objective remains unproven until the real run
demonstrates model readiness, browser first sound within 2.5 seconds after the first
readable Agent delta, RTF and chunk-gap gates, eight-voice CER/quality stability,
full interrupt/flush recovery, and two-to-three independent endpoint concurrency.
