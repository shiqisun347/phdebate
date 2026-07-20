# MOSS-TTS-Realtime native session benchmark

- Generated: 2026-07-17T21:53:29.604510+00:00
- Candidate endpoints: 3 (addresses redacted)
- Protocol: start -> incremental push + PCM stream -> final -> close
- Evidence boundary: transport/model only; CER, MOS and speaker drift remain separate gates.

| Concurrency | Success | Non-silent PCM P50/P95/max ms | RTF P50/P95/max | Gap P99 max ms | Lifecycle | Release gate |
|---:|---:|---:|---:|---:|---|---|
| 1 | 20/20 | 13.217/21.482/50.858 | 0.67/0.724/0.824 | 119.435 | PASS | NO-GO |
| 2 | 40/40 | 13.296/21.281/29.049 | 0.681/0.758/0.777 | 138.08 | PASS | NO-GO |
| 3 | 60/60 | 18.197/28.572/31.967 | 0.719/0.773/0.81 | 124.039 | PASS | NO-GO |

Release gate: c1/c2/c3 each contain 20 batches; every request has non-silent PCM under 3s before final, first PCM P95 <=800ms, RTF P95 <=0.65, chunk-gap P99 <=200ms, and release ACK.
