# MOSS-TTS-Realtime RTX 3080 Ti memory-path audit

Date: 2026-07-18

Scope: fixed OpenMOSS commit `ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af`,
fixed Realtime model revision `6acbc7f161a0db71c291f2d0aaa9eee59334cab2`,
fixed codec revision `3cd226ba2947efa357ef453bcad111b6eafba782`, and the local isolated gateway.
No deployment or production default was changed.

## Root cause

The 12GB failure is a static-weight problem before meaningful streaming work begins.
Inspection of the exact downloaded safetensors gives:

| Component | Checkpoint dtype | Static weight bytes | GiB |
| --- | --- | ---: | ---: |
| MOSS-TTS-Realtime | BF16 | 4,663,881,728 | 4.343 |
| MOSS Audio Tokenizer encoder | FP32 | 3,549,573,120 | 3.303 |
| MOSS Audio Tokenizer decoder | FP32 | 3,549,573,120 | 3.303 |
| MOSS Audio Tokenizer quantizer | FP32 | 5,386,240 | 0.005 |
| Total model + codec | mixed | 11,768,414,208 | 10.959 |

An RTX 3080 Ti exposes 12,288MiB total, not a full 12GiB available to tensors.
The static weights therefore leave less than 1GiB for the CUDA context, allocator
fragmentation, Qwen KV cache, SDPA/compiled local-transformer workspaces, codec streaming
state, and activations. Loading order cannot change this final resident set.

The pinned upstream Realtime model card reports its latency on an L20 with SDPA and
`torch.compile`, not on a 12GB card. It also states that the released server supports only
batch size one. See the [fixed model card](https://github.com/OpenMOSS/MOSS-TTS/blob/ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af/docs/moss_tts_realtime_model_card.md).

## Options audited

| Option | Memory result | Quality / realtime risk | Decision |
| --- | --- | --- | --- |
| Realtime FP16 instead of BF16 | Same bytes | No memory benefit; weaker numerical margin | Reject |
| Codec BF16/FP16 | Codec would halve static bytes | Pinned codec repeatedly forces FP32 tensors, norms, codebook distance and residual quantization; no upstream Realtime quality acceptance exists | Reject |
| INT8/INT4 / bitsandbytes | Could reduce model memory | No pinned Realtime + streaming codec implementation or CER/voice-drift evidence | Reject |
| `device_map` / layer CPU offload for the AR model | Can reduce static VRAM | Per-token PCIe transfers threaten RTF/TTFB; upstream Realtime examples use one model device | Reject |
| Disable `torch.compile` | May reduce peak/reserved memory | Upstream recommends compile for realtime speed; does not solve 10.959GiB static weights | Reject |
| Entire FP32 codec on CPU | Removes 6.611GiB codec weights from VRAM without changing codec precision | 1.6B codec CPU decode may fail RTF/first-PCM gates | Implement as explicit diagnostic only |
| Codec encoder CPU, decoder GPU | Runtime static VRAM would be about 7.65GiB | Promising and precision-preserving, but mixed-module placement is not an upstream documented serving contract; codec-wide streaming context traverses both halves | Do not implement without isolated real-codec validation |
| Codec on a second GPU | Removes codec from the model GPU and preserves GPU decode | Requires a second independently validated device and admission model | Future isolated-host option |

The pinned upstream `AudioStreamDecoder` already accepts an explicit device and moves audio
tokens there before calling codec decode. The gateway also pre-encodes and caches all eight
voice prompts. These properties make an entire CPU codec a bounded diagnostic adaptation;
they do not prove it is fast enough. See the
[fixed streaming implementation](https://github.com/OpenMOSS/MOSS-TTS/blob/ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af/moss_tts_realtime/mossttsrealtime/streaming_mossttsrealtime.py).

## Implemented diagnostic control

New setting:

```text
MOSS_GATEWAY_DIAGNOSTIC_CODEC_DEVICE=cpu
```

It is rejected unless `MOSS_GATEWAY_ALLOW_SUB24GB_DIAGNOSTIC=true`. Only `cpu` is accepted;
arbitrary unverified placements cannot enter through configuration. Defaults remain:

- backend disabled;
- codec on the primary CUDA device when OpenMOSS is explicitly enabled;
- 24GB total / 20GB free formal startup floor;
- fixed SDPA path.

In diagnostic CPU-codec mode:

- Realtime model stays BF16 on `MOSS_GATEWAY_DEVICE`;
- codec stays its original FP32 on CPU;
- prompt waveform is moved to the codec device for encoding;
- generated audio tokens are moved to the codec device for streaming decode;
- readiness exposes `diagnostic_codec_device` so the endpoint cannot masquerade as a formal
  same-GPU candidate.

## Reproduce the weight accounting

Run against the exact downloaded model and codec directories:

```bash
python - <<'PY'
from collections import defaultdict
from safetensors import safe_open

files = {
    "model": ["model/model.safetensors"],
    "codec": [
        "codec/model-00001-of-00002.safetensors",
        "codec/model-00002-of-00002.safetensors",
    ],
}
for label, paths in files.items():
    groups = defaultdict(int)
    for path in paths:
        with safe_open(path, framework="pt", device="cpu") as handle:
            for key in handle.keys():
                tensor = handle.get_tensor(key)
                group = key.split(".", 1)[0] if label == "codec" else "model"
                groups[group] += tensor.numel() * tensor.element_size()
    print(label, {key: (value, value / 1024**3) for key, value in groups.items()})
PY
```

## Isolated 12GB diagnostic command

This command is for a drained, dedicated QA GPU only. It must not be pointed at production
admission and must begin with at least 10GiB free.

```bash
export PYTHONPATH=/opt/OpenMOSS/MOSS-TTS/moss_tts_realtime
export MOSS_GATEWAY_BACKEND=openmoss
export MOSS_GATEWAY_API_KEY='qa-secret'
export MOSS_GATEWAY_PROMPT_DIR=/opt/phdebate/moss-prompts
export MOSS_GATEWAY_UPSTREAM_CHECKOUT=/opt/OpenMOSS/MOSS-TTS
export MOSS_GATEWAY_UPSTREAM_REVISION=ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af
export MOSS_GATEWAY_MODEL_REVISION=6acbc7f161a0db71c291f2d0aaa9eee59334cab2
export MOSS_GATEWAY_CODEC_REVISION=3cd226ba2947efa357ef453bcad111b6eafba782
export MOSS_GATEWAY_DEVICE=cuda:0
export MOSS_GATEWAY_ATTN_IMPL=sdpa
export MOSS_GATEWAY_ALLOW_SUB24GB_DIAGNOSTIC=true
export MOSS_GATEWAY_MIN_GPU_TOTAL_MEMORY_GB=12
export MOSS_GATEWAY_MIN_GPU_FREE_MEMORY_GB=10
export MOSS_GATEWAY_DIAGNOSTIC_CODEC_DEVICE=cpu
export MOSS_GATEWAY_VOICE_PROMPTS_JSON='{"debate_voice_1":"debate_voice_1.wav","debate_voice_2":"debate_voice_2.wav","debate_voice_3":"debate_voice_3.wav","debate_voice_4":"debate_voice_4.wav","debate_voice_5":"debate_voice_5.wav","debate_voice_6":"debate_voice_6.wav","debate_voice_7":"debate_voice_7.wav","debate_voice_8":"debate_voice_8.wav"}'

cd /opt/phdebate/v2/services/moss-realtime-gateway
python -m moss_realtime_gateway
```

From a second shell, first inspect readiness and then collect 20 single-session rounds:

```bash
curl -fsS -H 'X-MOSS-Gateway-Key: qa-secret' http://127.0.0.1:8890/health/ready

cd /opt/phdebate/v2
export MOSS_TTS_REALTIME_API_KEY='qa-secret'
PYTHONPATH=apps/api:. .venv/bin/python scripts/benchmark_moss_realtime_sessions.py \
  --endpoint http://127.0.0.1:8890 \
  --transport websocket \
  --voice-prompt debate_voice_1=debate_voice_1.wav \
  --concurrency 1 \
  --rounds 20 \
  --output-dir docs/qa/audio/<run-id>-12gb-cpu-codec
```

The benchmark utility expects the full c1/c2/c3 release matrix and may exit nonzero for a
single-endpoint diagnostic even after writing evidence. Judge this experiment from the c1
records: non-silent first PCM, RTF, gap P99, release acknowledgement, CER, swallowed-word and
voice-consistency gates all remain mandatory.

## Verification

```text
../../.venv/bin/ruff check moss_realtime_gateway tests
All checks passed!

../../.venv/bin/python -m py_compile moss_realtime_gateway/*.py tests/*.py

../../.venv/bin/pytest -q tests
29 passed, 1 warning
```

The warning is the existing Starlette/httpx deprecation.

## Conclusion

Same-device MOSS-TTS-Realtime plus the fixed FP32 codec is a hard no-go on 12GB, even when
the card is otherwise empty. CPU codec placement remains transparent and fail-closed, but the
real corrected canary could not finish encoding one roughly 10-second prompt within 321
seconds on the production 12-core Xeon and never reached first PCM. That diagnostic path is
therefore also no-go. The formal path remains an isolated 24GB+ GPU endpoint with an
accelerated codec.
