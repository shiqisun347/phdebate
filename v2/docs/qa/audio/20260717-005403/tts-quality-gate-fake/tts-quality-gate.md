# Independent streaming TTS quality gate

- Generated: 2026-07-17T18:07:33.847070+00:00
- Mode: `fake` + `fake`
- Endpoints: {'tts': None, 'asr': None, 'redacted': True}
- Initial playback buffer simulation: 100 ms
- Queue delay is client-observed excess over single-room first-PCM P50; it is not server-side queue instrumentation.
- Reports never include endpoint paths/query strings, credential values, request headers, or payload extensions.

| Rooms | Success | First packet P50/P95/max ms | First char→PCM P50/P95/max ms | CER P95 | Swallowed P95 | Stutters | Boundary silence max ms | Queue P95 ms | RMS range dB |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 2/2 | 1.176/1.18/1.18 | 1.176/1.18/1.18 | 0.0 | 0.0 | 0 | 0.0 | 0.004 | 0.0 |
| 2 | 4/4 | 1.368/1.678/1.701 | 1.368/1.678/1.701 | 0.0 | 0.0 | 0 | 0.0 | 0.502 | 0.0 |
| 3 | 6/6 | 1.724/2.278/2.295 | 1.724/2.278/2.295 | 0.0 | 0.0 | 0 | 0.0 | 1.102 | 0.0 |

## Cancellation probe

- Result: completed
- Cancel latency: 0.061 ms
- Close acknowledged: True

## Automatic gates

| Check | Actual | Limit | Result |
|---|---:|---:|---|
| zero_request_failures | 0 | 0 | PASS |
| cer_p95 | 0.0 | 0.1 | PASS |
| swallowed_character_rate_p95 | 0.0 | 0.05 | PASS |
| first_char_to_first_pcm_p95_ms | 2.258 | 800 | PASS |
| stutter_count | 0 | 0 | PASS |
| chunk_boundary_silence_max_ms | 0.0 | 200 | PASS |
| rms_dbfs_range | 0.0 | 3 | PASS |
| three_room_queue_delay_p95_ms | 1.102 | 500 | PASS |
| cancel_latency_ms | 0.061 | 200 | PASS |

Overall: **PASS**
