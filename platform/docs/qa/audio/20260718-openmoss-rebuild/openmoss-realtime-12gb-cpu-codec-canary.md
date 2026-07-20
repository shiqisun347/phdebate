# OpenMOSS Realtime 12GB GPU + CPU codec diagnostic canary

Date: 2026-07-18 (Asia/Shanghai)

Decision: **NO-GO**

## Purpose

After the fixed Realtime model plus codec failed to fit on an exclusive RTX 3080 Ti,
the gateway added one fail-closed diagnostic placement:

```text
Realtime talker: BF16, cuda:0
MOSS Audio Tokenizer: original FP32, CPU
```

This mode required both `MOSS_GATEWAY_ALLOW_SUB24GB_DIAGNOSTIC=true` and
`MOSS_GATEWAY_DIAGNOSTIC_CODEC_DEVICE=cpu`. The production default, 24GB/20GB
startup floor and all production realtime flags remained unchanged.

## Canary 1: real interface discovery

The first isolated run stopped LightTTS, loaded the fixed model and codec, and reached:

| Metric | Value |
|---|---:|
| Backend startup | 26.212s |
| GPU used after startup | 4,774MiB |
| GPU free after startup | 7,140MiB |

This proved that moving the unchanged FP32 codec to CPU removes the 12GB CUDA OOM.
The run then exposed a real gateway adapter defect before synthesis:

```text
AttributeError: 'list' object has no attribute 'dim'
```

The fixed codec revision requires a tensor shaped `(batch, channels, samples)` and
returns an object with `audio_codes`. The gateway still used an older upstream helper
contract that passed a Python list.

## Fix and regression gate

The gateway was corrected to:

- pass a batched 3-D waveform tensor to `codec.encode()`;
- read `audio_codes` from the pinned codec result object;
- keep prompt encoding and streaming decoding on the declared codec device.

A focused regression test now reproduces the pinned tensor/result contract. Gateway
tests increased to `29 passed, 1 warning`; Ruff and py_compile passed.

## Canary 2: corrected CPU codec performance

The corrected candidate again loaded without GPU OOM. During the single
`candidate_voice_1` prompt encode:

| Observation | Result |
|---|---|
| Prompt duration | about 10 seconds |
| Elapsed before manual cutoff | 321 seconds |
| Prompt encode completed | no |
| First PCM reached | no |
| GPU utilization during wait | 0% |
| GPU used/free during wait | about 4,868–4,890 / 7,046–7,024MiB |
| Python CPU utilization | about 85–94% |
| Python RSS growth | about 5.4GiB → 7.6GiB |
| Process state at cutoff | uninterruptible CPU/kernel wait observed (`D`) |

The run was stopped after the bounded five-minute diagnostic window. It never entered
the actual streaming decoder, so no PCM, RTF, CER or voice-quality result exists.

Even if all eight prompt codes were precomputed offline, that would only hide startup
cost. The same 1.6B FP32 codec still owns streaming audio decode, and this canary could
not complete one reference encode within 321 seconds on the production 12-core Xeon.
It is therefore not credible as a sub-2.5-second realtime path.

## Cleanup and production restoration

The diagnostic process was terminated and the existing service was independently
restored and checked:

| Check | Restored result |
|---|---|
| LightTTS Supervisor | `RUNNING` |
| `127.0.0.1:8080/health` | `Ok` |
| GPU used/free | 5,000 / 6,914MiB |
| Main `/api/health/ready` | `ok=true` |
| Schema | `0019_audio_streaming` |
| `active_match_processing` | `false` |
| LightTTS active/queue | `0/0` |

No production match, route, feature flag or deployment was changed.

## Final decision

`MOSS-TTS-Realtime talker on 12GB GPU + full FP32 codec on CPU` is **NO-GO** for
production realtime debate speech. It solves static VRAM placement but fails before
the first PCM performance gate by more than two orders of magnitude.

The remaining formal path is an isolated 24GB+ GPU runtime, with the codec on an
accelerator, followed by the full 8-voice, 20-round, browser first-sound, interrupt and
2–3 match concurrency gates.
