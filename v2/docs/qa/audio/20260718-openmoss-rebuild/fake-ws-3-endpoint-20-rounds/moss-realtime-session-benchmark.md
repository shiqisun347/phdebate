# MOSS-TTS-Realtime native session benchmark

- Generated: 2026-07-17T22:51:33.258074+00:00
- Candidate endpoints: 3 (addresses redacted)
- Transport: websocket
- Protocol: persistent WS start -> ready.audio -> sequenced text_delta/ack -> final/ack -> tail PCM -> audio_end -> released
- First PCM origin: first body text_delta/push send time (handshake is reported separately).
- Evidence boundary: transport/model only; CER, MOS and speaker drift remain separate gates.

| Concurrency | Success | Handshake P50/P95/max ms | Non-silent PCM P50/P95/max ms | RTF P50/P95/max | Gap P99 max ms | Lifecycle | Release gate |
|---:|---:|---:|---:|---:|---:|---|---|
| 1 | 20/20 | 1.808/4.641/17.444 | 0.66/0.761/0.898 | 0.588/0.598/0.605 | 115.724 | PASS | PASS |
| 2 | 40/40 | 1.43/2.71/3.346 | 0.675/1.271/1.337 | 0.604/0.653/0.683 | 115.168 | PASS | NO-GO |
| 3 | 60/60 | 1.618/4.525/10.618 | 0.797/1.501/10.266 | 0.637/0.687/0.719 | 122.758 | PASS | NO-GO |

Release gate: c1/c2/c3 each contain 20 batches; every request has non-silent PCM under 3s before final, first PCM P95 <=800ms, RTF P95 <=0.65, chunk-gap P99 <=200ms, and release ACK.
