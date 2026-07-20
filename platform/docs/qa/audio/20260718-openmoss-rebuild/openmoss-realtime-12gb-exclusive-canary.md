# OpenMOSS Realtime 12GB exclusive-GPU canary

- Date: 2026-07-18 (Asia/Shanghai)
- Candidate: OpenMOSS Realtime TTS isolated canary
- GPU: NVIDIA GeForce RTX 3080 Ti, 12GB
- Production change: none
- Release decision: **NO-GO**

## Purpose

This canary tested whether the real OpenMOSS Realtime model and codec could be initialized on the existing 12GB production GPU after temporarily stopping LightTTS and giving the candidate exclusive access to the device.

It was an isolated capacity check only. No V2 production feature flag, routing, Nginx configuration, Supervisor program, or deployment was changed.

## Initial exclusive-GPU state

After LightTTS was stopped, the RTX 3080 Ti reported:

| Metric | Value |
|---|---:|
| Total GPU memory | 12,288 MiB |
| Free GPU memory | 11,909 MiB |

This was the maximum practical free-memory condition available on the current production host.

## Initialization result

The canary progressed through loading both model components:

- TTS model: 403 weight tensors loaded.
- Codec model: 1,600 weight tensors loaded.

Initialization then failed while moving the codec to CUDA. At the failure point:

| Metric | Value |
|---|---:|
| GPU memory used | 11.62 GiB |
| GPU memory free | 5.44 MiB |
| Failed additional allocation | 20 MiB |

The CUDA out-of-memory error occurred before the service became ready and before any PCM audio was generated.

## Quality and latency evidence boundary

Because initialization failed before synthesis:

- no first-PCM or first-non-silent latency was measured;
- no TTS→ASR CER was measured;
- no swallowed-word, repetition, boundary-silence, stutter, loudness, or voice-drift gate was run;
- no one-room, two-room, or three-room real-model concurrency result exists from this canary;
- no browser playback evidence was produced.

Therefore this run provides GPU-capacity evidence only. It must not be cited as evidence for audio quality, latency, stability, cancellation behavior, or concurrency.

## Cleanup and production restoration

The canary used a cleanup trap. After the failed initialization, LightTTS was restored and the following checks passed:

| Check | Restored state |
|---|---|
| LightTTS Supervisor state | `RUNNING` |
| LightTTS port 8080 health | `ok` |
| GPU memory used | 5,008 MiB |
| GPU memory free | 6,906 MiB |
| Main site `/api/health/ready` | `ok` |
| Database schema | `0019` |
| `active_match_processing` | `false` |
| TTS gate active / queued | `0 / 0` |

No production match was active during the post-canary health check.

## Decision

**NO-GO on the existing 12GB RTX 3080 Ti, including under exclusive-GPU conditions.**

The failure is not a LightTTS co-residency artifact: with 11,909 MiB initially free, the real OpenMOSS Realtime stack still exhausted the device while placing the codec on CUDA. The configured fail-closed minimum of an isolated 24GB-class GPU remains justified.

The next real-model acceptance run must use an isolated GPU with sufficient headroom to initialize the complete TTS and codec stack and then execute the full quality gate, cancellation gate, and independent two-to-three-endpoint concurrency gate. Until that evidence exists, all OpenMOSS Realtime production switches must remain disabled.
