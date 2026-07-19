# Independent streaming TTS quality gate

- Generated: 2026-07-17T23:13:48.158375+00:00
- Mode: `fake` + `fake`
- Endpoints: {'tts': [], 'asr': None, 'redacted': True}
- Fixed voice-set version: self-test/unversioned
- Initial playback buffer simulation: 100 ms
- Queue delay is client-observed excess over single-room first-PCM P50; it is not server-side queue instrumentation.
- Reports never include endpoint paths/query strings, credential values, request headers, or payload extensions.

| Rooms | Success | First packet P50/P95/max ms | First char→PCM P50/P95/max ms | First char→non-silent P50/P95/max ms | CER P95 | Swallowed P95 | Extra P95 | Adjacent loops | Stutters | Gap P99 max ms | Boundary silence max ms | Queue/Non-silent queue P95 ms | RMS range dB |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 2/2 | 1.337/1.391/1.397 | 1.337/1.391/1.397 | 1.337/1.391/1.397 | 0.0 | 0.0 | 0.0 | 0 | 0 | 2.075 | 0.0 | 0.057/0.057 | 0.0 |
| 2 | 4/4 | 1.43/1.849/1.901 | 1.43/1.849/1.901 | 1.43/1.849/1.901 | 0.0 | 0.0 | 0.0 | 0 | 0 | 14.675 | 0.0 | 0.512/0.512 | 0.0 |
| 3 | 6/6 | 1.864/2.378/2.425 | 1.864/2.378/2.425 | 1.864/2.378/2.425 | 0.0 | 0.0 | 0.0 | 0 | 0 | 23.854 | 0.0 | 1.041/1.041 | 0.0 |

## Per-voice content and MOS auxiliary metrics

Automatic proxy scores are waveform/content diagnostics, not MOS and not a substitute for listening.

| Voice | Success | CER P95 | Swallowed P95 | Extra P95 | Adjacent loops | Head/Tail swallow | First/Last mismatch | Stutters | RMS range dB | Active RMS stddev max dB | Auto proxy /5 | Human MOS |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| selftest | 32/32 | 0.0 | 0.0 | 0.0 | 0 | 0/0 | 0/0 | 0 | 0.003 | 0.213 | 5.0 | None (0) |

## Continuous 20-round voice drift proxy

| Voice | Success | RMS range dB | Duration/char CV | ZCR CV | Crest range dB | Timbre cosine P95 | Spectral centroid CV | Low-frequency CV | CER P95 | Boundary swallow | Proxy |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| selftest | 20/20 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0 | PASS |

## Cancellation probe

- Result: completed
- Cancel latency: 0.016 ms
- Close acknowledged: True
- Remote released: True
- Release active/orphans: 0/0

## Automatic gates

| Check | Actual | Limit | Result |
|---|---:|---:|---|
| zero_request_failures | 0 | 0 | PASS |
| cer_p95 | 0.0 | 0.02 | PASS |
| swallowed_character_rate_p95 | 0.0 | 0.0 | PASS |
| repeated_or_extra_character_rate_p95 | 0.0 | 0.0 | PASS |
| unexpected_adjacent_repetition_count | 0 | 0 | PASS |
| leading_or_trailing_swallow_samples | 0 | 0 | PASS |
| first_or_last_character_mismatch_samples | 0 | 0 | PASS |
| first_char_to_first_pcm_p95_ms | 2.052 | 800 | PASS |
| first_char_to_first_pcm_max_ms | 2.425 | <3000 | PASS |
| first_char_to_first_non_silent_pcm_p95_ms | 2.052 | 1000 | PASS |
| first_char_to_first_non_silent_pcm_max_ms | 2.425 | <3000 | PASS |
| leading_silence_max_ms | 0.0 | 250 | PASS |
| stutter_count | 0 | 0 | PASS |
| chunk_boundary_silence_max_ms | 0.0 | 200 | PASS |
| pcm_chunk_gap_p99_max_ms | 23.854 | 200 | PASS |
| rms_dbfs_range | 0.003 | 3 | PASS |
| per_voice_rms_dbfs_range_max | 0.003 | 3 | PASS |
| active_window_rms_dbfs_stddev_max | 0.213 | 6 | PASS |
| two_room_queue_delay_p95_ms | 0.512 | 500 | PASS |
| two_room_non_silent_queue_delay_p95_ms | 0.512 | 500 | PASS |
| three_room_queue_delay_p95_ms | 1.041 | 500 | PASS |
| three_room_non_silent_queue_delay_p95_ms | 1.041 | 500 | PASS |
| continuous_voice_drift_proxy | 1 | 1 | PASS |
| twenty_round_timbre_cosine_distance_p95_max | 0.0 | 0.08 | PASS |
| cancel_latency_ms | 0.016 | 200 | PASS |

## Manual release gates

- human_mos_all_voices: BLOCKED — Automatic acoustic proxies are not MOS; every real voice still requires human ratings.
- twenty_round_human_voice_identity: BLOCKED — Requires human listening or a separately validated speaker-embedding system; proxy metrics are auxiliary only.

Automatic gate: **PASS**
Release ready including manual gates: **NO**
