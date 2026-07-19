# OpenMOSS MOSS-TTS-Nano ONNX-CUDA real GPU canary

Date: 2026-07-18 CST  
Decision: **NO-GO for the formal 4v4 realtime voice path**

## Evidence boundary

This was a real CUDA test on the production host's RTX 3080 Ti, but it ran in an isolated directory and loopback port. It was never connected to V2, Nginx, Supervisor, a room, or a production feature flag. The production health endpoint reported no active match, no active LightTTS lease and no queued speech before the canary. The canary process was stopped after the measurements and GPU memory returned from 11256 MiB to the original 5259 MiB.

After the canary, an older detached `8083` LightTTS canary was also found with no listening socket and no Supervisor ownership. SIGTERM did not stop it, so its isolated process group was force-stopped after command-line and port verification. Production `8080` remained healthy, V2 readiness stayed green, and baseline GPU memory improved further to 5000 MiB used / 6914 MiB free.

Pinned inputs:

- MOSS-TTS-Nano repository: `11619374849c649486584e3b10ed55b176a924ee`
- MOSS-TTS-Nano ONNX weights: `f52645cb467506d8e18e746ddd59482685b74e58`
- MOSS-Audio-Tokenizer-Nano ONNX weights: `ceff0d0749bfb3fa2d61149794ec6feef0d1e1ae`
- ONNX Runtime 1.23.2, CUDAExecutionProvider, fixed sampling, realtime codec decode, `max_new_frames=600`

## What passed

- After warmup, eight custom Mandarin prompts produced first PCM in 161.6–278.5ms.
- RTF was 0.423–0.506, so generation was faster than playback.
- Seven voices completed the reference sentence with FunASR round-trip CER 0 and no repeated bigram excess.
- Digital interrupt was fast: close response 5.1ms, audio stream ended after 5.7ms, and no PCM arrived after close began.
- The process released all additional GPU memory when stopped.

## Release blockers

1. `candidate_voice_2` stopped after “各位评委同学大家好”, deleting 19 of 28 normalized characters. Its CER was 0.6786. This reproduces the upstream missing-sentence risk on a real GPU and disqualifies that prompt/model combination.
2. The ONNX service serializes requests through one execution lock. It does not provide two or three simultaneous debate turns from one model instance.
3. GPU memory grew from 5259 MiB before the canary to 10744 MiB after the short voice runs and 11256 MiB after a long-text cancel test, leaving only 657 MiB free. It cannot safely co-host production LightTTS on this 12GB card.
4. Most observed network chunk-gap P99 values were 223–249ms. The chunks contain roughly the same amount of playable audio and may remain continuous with buffering, but the strict <=200ms diagnostic gate was not met and browser underrun was not measured for this candidate.
5. One sample per prompt does not prove 20-round voice stability or human naturalness. No MOS claim is made.

## Result table

| Voice | First PCM ms | RTF | Audio s | Gap P99 ms | CER | Deletions | Decision |
|---:|---:|---:|---:|---:|---:|---:|---|
| 1 | 278.5 | 0.437 | 7.52 | 229.0 | 0 | 0 | candidate |
| 2 | 203.1 | 0.506 | 3.36 | 181.6 | 0.6786 | 19 | reject |
| 3 | 225.0 | 0.423 | 7.84 | 235.8 | 0 | 0 | candidate |
| 4 | 161.6 | 0.428 | 6.96 | 240.2 | 0 | 0 | candidate |
| 5 | 187.1 | 0.427 | 6.24 | 229.8 | 0 | 0 | candidate |
| 6 | 203.8 | 0.434 | 6.48 | 230.9 | 0 | 0 | candidate |
| 7 | 192.3 | 0.440 | 6.24 | 223.6 | 0 | 0 | candidate |
| 8 | 188.0 | 0.441 | 6.88 | 249.2 | 0 | 0 | candidate |

Raw metrics are in [`results.json`](results.json). The WAV files in this directory are the exact downloaded canary outputs.

## Consequence for the architecture

Nano remains useful as a diagnostic/backup experiment, but it does not satisfy the formal-match stability and concurrency requirements. The primary path remains MOSS-TTS-Realtime on isolated 24GB+ GPUs, with one active turn per endpoint and two or three independent endpoints. The gateway now fails closed on GPUs below that floor.
