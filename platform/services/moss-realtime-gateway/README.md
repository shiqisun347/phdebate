# MOSS-TTS-Realtime Gateway

An isolated, single-active gateway for the V2 realtime voice protocol. The
service is deployable but **disabled by default** and does not change V2 API or
production configuration.

## Fixed upstream

- OpenMOSS/MOSS-TTS commit:
  `ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af`
- MOSS-TTS-Realtime model revision:
  `6acbc7f161a0db71c291f2d0aaa9eee59334cab2`
- MOSS-Audio-Tokenizer revision:
  `3cd226ba2947efa357ef453bcad111b6eafba782`

The CUDA adapter lazily imports the upstream `MossTTSRealtimeStreamingSession`,
`AudioStreamDecoder`, and `MossTTSRealtimeTextStreamBridge`. One worker owns one
turn and enters `codec.streaming(batch_size=1)` exactly once. Text deltas call
`push_text`; finalization calls `end_text`, repeated `drain`, decoder `flush`,
then exits the codec context before the HTTP control response can report success.

## Protocol

- `POST /tts/session/start`
- `POST /tts/session/push`
- `GET /tts/session/{session_id}/audio`
- `POST /tts/session/close`
- `POST /tts/session/abort`
- `WS /tts/session/ws`
- `GET /health/live`
- `GET /health/ready`

Audio is mono PCM16 at 24kHz. Every endpoint permits strictly one active worker.
All control responses wait for the worker acknowledgement. `close`, final push,
and `abort` additionally wait for worker termination and codec-context exit.
Concurrent terminal requests join one atomic terminal operation, so a browser
disconnect racing with final/close cannot create a false orphan. Normal audio is
kept in a lossless per-turn backlog and EOF is signalled separately; slow
consumers cannot make final/close discard the tail.

If `MOSS_GATEWAY_API_KEY` is non-empty, every endpoint except `/health/live`
requires the header `X-MOSS-Gateway-Key`. The key is never included in health
responses or application logs.

### Persistent WebSocket contract

The V2 API service (not the browser) connects to `/tts/session/ws` with
`X-MOSS-Gateway-Key`. One WebSocket carries one turn and reuses the same
`GatewayRuntime`, model worker, codec context, and lossless audio backlog as the
HTTP compatibility API.

Client JSON messages are strictly ordered:

```text
start      {"type":"start","seq":0,"session_id":"...","voice":"debate_voice_1","user_text":"..."}
text_delta {"type":"text_delta","seq":1,"text":"正文增量"}
text_delta {"type":"text_delta","seq":2,"text":"后续正文"}
final      {"type":"final","seq":3}
```

The server replies `ready` with `next_seq:1` and nested audio metadata. Each
accepted `text_delta` receives `{"type":"ack","seq":N}` only after the real
worker ACK. `final` also receives an ACK, followed by all remaining binary
PCM16LE frames, then `audio_end`, then `released:true`. Raw binary server frames
are mono 24kHz PCM16LE.

`abort` may omit `seq` and can preempt a slow delta or final. It clears the
Runtime audio backlog, stops the WS audio pump, drops pending PCM in the bounded
WS egress queue, sends `audio_reset` so the downstream player flushes already
received audio, and only then sends `released:true`. Disconnect follows the same
abort path. `ping`/`pong` and `health` remain responsive while synthesis is in
progress.

Duplicate/gapped sequence numbers, repeated start/final, inbound binary frames,
malformed JSON, `thinking` at any nesting level, and over-limit text are rejected
and terminate the turn safely. `start` deliberately has no assistant text: all
spoken body text must arrive through sequenced `text_delta` messages.

## Required production configuration

```bash
export MOSS_GATEWAY_BACKEND=openmoss
export MOSS_GATEWAY_API_KEY='load-from-secret-store'
export MOSS_GATEWAY_PROMPT_DIR=/opt/phdebate/moss-prompts
export MOSS_GATEWAY_UPSTREAM_CHECKOUT=/opt/OpenMOSS/MOSS-TTS
export MOSS_GATEWAY_VOICE_PROMPTS_JSON='{
  "debate_voice_1":"debate_voice_1.wav",
  "debate_voice_2":"debate_voice_2.wav",
  "debate_voice_3":"debate_voice_3.wav",
  "debate_voice_4":"debate_voice_4.wav",
  "debate_voice_5":"debate_voice_5.wav",
  "debate_voice_6":"debate_voice_6.wav",
  "debate_voice_7":"debate_voice_7.wav",
  "debate_voice_8":"debate_voice_8.wav"
}'
export MOSS_GATEWAY_UPSTREAM_REVISION=ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af
export MOSS_GATEWAY_MODEL_REVISION=6acbc7f161a0db71c291f2d0aaa9eee59334cab2
export MOSS_GATEWAY_CODEC_REVISION=3cd226ba2947efa357ef453bcad111b6eafba782
export MOSS_GATEWAY_DEVICE=cuda:0
export MOSS_GATEWAY_ATTN_IMPL=sdpa
export MOSS_GATEWAY_MIN_GPU_TOTAL_MEMORY_GB=24
export MOSS_GATEWAY_MIN_GPU_FREE_MEMORY_GB=20
```

For a supervised, non-release diagnostic on a dedicated 10–23GB GPU, the hard
24GB configuration floor can be lowered only by setting
`MOSS_GATEWAY_ALLOW_SUB24GB_DIAGNOSTIC=true` together with explicit total/free
thresholds. Readiness exposes `sub24gb_diagnostic:true`; V2 production admission
must not enable such an endpoint until the full latency, quality, concurrency
and recovery gates pass. This override is intended to test whether replacing,
rather than co-hosting with, the legacy TTS can fit a smaller card.

The fixed Realtime checkpoint is already BF16 (about 4.34GiB), while the fixed
MOSS Audio Tokenizer checkpoint is FP32 (about 6.61GiB). On a 12GB card those
weights alone leave too little space for CUDA, KV cache, compiled graphs and
activations. An additional diagnostic-only placement can keep the Realtime
model on CUDA while running the unchanged FP32 codec on CPU:

```bash
export MOSS_GATEWAY_ALLOW_SUB24GB_DIAGNOSTIC=true
export MOSS_GATEWAY_MIN_GPU_TOTAL_MEMORY_GB=12
export MOSS_GATEWAY_MIN_GPU_FREE_MEMORY_GB=10
export MOSS_GATEWAY_DIAGNOSTIC_CODEC_DEVICE=cpu
```

This preserves codec precision and avoids the static codec VRAM allocation, but
it is not a release optimization. On the production 12-core Xeon, a corrected
real canary did not finish encoding one roughly 10-second prompt within 321
seconds and never reached first PCM. The setting remains available only to make
placement experiments explicit and auditable; it is rejected unless diagnostic
mode is enabled. BF16/FP16 codec casting and INT8/INT4 are deliberately not
offered because the pinned upstream codec contains mandatory FP32 paths and has
no published quality/streaming acceptance evidence for those conversions.

A second, mutually exclusive diagnostic keeps prompt encoding on the complete
FP32 CUDA codec, then moves only the encoder to CPU before loading the BF16
Realtime talker. The quantizer and decoder remain on the configured CUDA device:

```bash
export MOSS_GATEWAY_ALLOW_SUB24GB_DIAGNOSTIC=true
export MOSS_GATEWAY_MIN_GPU_TOTAL_MEMORY_GB=12
export MOSS_GATEWAY_MIN_GPU_FREE_MEMORY_GB=10
export MOSS_GATEWAY_DIAGNOSTIC_CODEC_ENCODER_OFFLOAD=true
unset MOSS_GATEWAY_DIAGNOSTIC_CODEC_DEVICE
```

This mode is deliberately ordered as complete codec CUDA load -> eight distinct
prompt encodes -> encoder CPU offload and CUDA cache release -> BF16 Realtime
model CUDA load. The pinned codec checkpoint contains approximately 3.303GiB of
encoder weights, 3.303GiB of decoder weights and 0.005GiB of quantizer weights;
the pinned BF16 Realtime checkpoint is approximately 4.344GiB. No module is
deleted or replaced. The fixed codec's `decode()` path reads only `quantizer`
and `decoder`, but its root `streaming()` context traverses the encoder too, so
this diagnostic enters decoder-only streaming contexts that mirror the pinned
upstream chunked-decode implementation. Startup fails closed if the encoder
shares parameters with the decode graph, any configured prompt is duplicated,
or any module is found on an unexpected device.

`/health/ready` exposes `diagnostic_codec_encoder_offload` and a `placement`
object containing the actual model/encoder/quantizer/decoder devices, streaming
context mode and number of cached prompt token sets. This remains a canary-only
path until a real 12GB run passes first-sound, quality, interruption, recovery
and multi-endpoint concurrency gates; the production defaults remain 24GB total,
20GB free and all-CUDA placement.

Prompt requests accept only a configured voice ID or an allowlisted WAV
basename. Absolute paths, traversal, symlinks, non-WAV files, and unlisted files
are rejected. The default OpenMOSS mode requires all eight fixed prompt entries.

OpenMOSS mode fails closed unless the API key, all eight prompts, upstream/model/
codec revisions and the actual Git checkout HEAD are fixed. The module must be
imported from `<checkout>/moss_tts_realtime`; a moving package elsewhere on
`PYTHONPATH` is rejected.

The gateway also refuses to start on a GPU below 24GB total memory or with less
than 20GB free before model load. This is intentional: a controlled canary on
the 12GB production GPU stopped LightTTS first and began with 11909MiB free, but
the fixed Realtime talker plus full MOSS Audio Tokenizer still exhausted the
card while moving the codec to CUDA. The CUDA path fixes SDPA, high matmul
precision and Ampere TF32 support; lower thresholds are diagnostic-only and
must not be used for release.

Prepare the upstream checkout and install its package at the fixed commit plus
the required CUDA/PyTorch stack:

```bash
git clone https://github.com/OpenMOSS/MOSS-TTS.git /opt/OpenMOSS/MOSS-TTS
git -C /opt/OpenMOSS/MOSS-TTS checkout ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af
python -m pip install -e /opt/OpenMOSS/MOSS-TTS/moss_tts_realtime
```

Then run from this directory:

```bash
python -m moss_realtime_gateway
```

The default bind is `127.0.0.1:8890`; place TLS/authenticated ingress in front of
it rather than exposing the process directly.

## Lifecycle and fail-fast behavior

- Startup validates all fixed prompts, verifies the imported source checkout and
  Git HEAD, loads the backend, caches all prompt tokens, and completes a real
  short synthesis for each of the eight voices before readiness becomes 200.
- Audio client disconnect and abort clear the Runtime and WS egress audio queues immediately.
- Terminal commands wait for actual worker acknowledgement and thread exit.
- Start with empty `assistant_text`, establish the audio GET, then push the first
  phrase. This prevents first audio from being blocked behind the control call.
- A worker exceeding `MOSS_GATEWAY_TERMINAL_GRACE_SECONDS` is marked orphaned;
  readiness remains 503 for the process lifetime.
- `MOSS_GATEWAY_FAIL_FAST_URL` optionally receives a redacted JSON callback for
  an orphan. It should point to a loopback supervisor sidecar, not a public URL.

Recommended Supervisor policy: use one gateway process per endpoint/GPU lease,
`autorestart=unexpected`, `stopasgroup=true`, `killasgroup=true`, a bounded
`stopwaitsecs`, and a local fail-fast sidecar that sends SIGTERM when the orphan
callback fires. Readiness 503 must remove the endpoint from admission before
Supervisor restarts it. Do not run multiple workers behind one endpoint: the
runtime intentionally enforces one active turn.

## Tests without a GPU

```bash
../../.venv/bin/python -m pytest -q tests
../../.venv/bin/python -m ruff check .
../../.venv/bin/python -m py_compile moss_realtime_gateway/*.py tests/*.py
```

The fake backend exercises warmup, authentication, fixed prompt validation,
single-active admission, PCM streaming, control acknowledgements, actual worker
exit, codec-context cleanup, abort/disconnect queue clearing, orphan readiness,
and fail-fast callback behavior.
