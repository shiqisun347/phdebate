# MOSS-TTS-Realtime native session benchmark

- Generated: 2026-07-18T02:16:33.556900+00:00
- Candidate endpoints: 1 (addresses redacted)
- Transport: websocket
- Protocol: persistent WS start -> ready.audio -> sequenced text_delta/ack -> final/ack -> tail PCM -> audio_end -> released
- First PCM origin: first body text_delta/push send time (handshake is reported separately).
- Evidence boundary: transport/model only; CER, MOS and speaker drift remain separate gates.

| Concurrency | Success | Handshake P50/P95/max ms | Non-silent PCM P50/P95/max ms | RTF/active P95 | Gap P99 max ms | 100ms underruns/max ms | Lifecycle | Release gate |
|---:|---:|---:|---:|---:|---:|---:|---|---|
| 1 | 1/1 | 399.619/399.619/399.619 | 839.589/839.589/839.589 | 1.244/0.964 | 2325.353 | 3/2351.072 | FAIL | NO-GO |

Release gate: c1/c2/c3 each contain 20 batches; every request has non-silent PCM under 3s before final, first PCM P95 <=800ms, RTF P95 <=0.65, chunk-gap P99 <=200ms, and release ACK.
