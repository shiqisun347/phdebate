# TTS automatic quality gate audit — schema v4

- Audit date: 2026-07-18 (Asia/Shanghai)
- Scope: `scripts/benchmark_tts_quality_gate.py` and its isolated tests only.
- Safety boundary: no production deployment, no provider/gateway protocol change, and no real TTS/ASR model invocation in this audit.
- Executable self-test evidence: `tts-quality-gate-selftest-v4/tts-quality-gate.json` and `tts-quality-gate-selftest-v4/tts-quality-gate.md`.

## Result

The gate now has executable fields and fail-closed checks for every requested automatic measurement. The deterministic fake run proves the measurement pipeline, concurrency orchestration, report schema, and threshold wiring execute correctly; it does **not** prove the quality or latency of OpenMOSS, LightTTS, FunASR, a browser, or production hardware.

The 20-round voice check is intentionally reported as an acoustic/timbre drift proxy. It now includes an amplitude-normalized spectral fingerprint in addition to RMS, duration, zero-crossing rate, and crest factor. It still does not claim speaker-verification accuracy. A real release remains blocked until human listening or a separately validated speaker-embedding system passes.

## Requirement coverage

| Requirement | Automatic evidence and gate | Status / boundary |
|---|---|---|
| TTS→ASR CER | Per sample `cer`, edit counts, global/P95 and per-voice summaries; `cer_p95` gate | Covered. Real evidence requires a real TTS endpoint plus FunASR/OpenAI-compatible ASR. |
| Swallowed text | Deletion-only rate, leading/trailing deletion counts, explicit boundary sample count | Covered. |
| Last character / boundary corruption | `first_character_match`, `last_character_match`, common prefix/suffix; `first_or_last_character_mismatch_samples` gate | Newly covered; catches boundary substitutions that deletion-only logic misses. |
| Repeated or extra text | Insertion-only rate plus unexpected adjacent repeated n-gram cycles | Covered. Legitimate source repetitions and single-character Mandarin forms are not blindly rejected. |
| Playback stalls | Arrival-aware continuous-clock simulation with configurable initial buffer; `stutter_count`, `underrun_ms`, gap distributions | Covered as endpoint/network simulation. Browser AudioWorklet underruns remain a separate browser gate. |
| Silence between chunks | Adjacent chunk trailing+leading silence distribution and hard maximum | Covered at PCM chunk boundaries. Normal semantic pauses inside a chunk are not mislabeled as transport boundary gaps. |
| Volume consistency | Global RMS range, worst same-voice RMS range, and worst active-window RMS standard deviation | Strengthened; both cross-sample and within-utterance variation are gated. |
| 20-round voice drift | Per voice: exactly configured sequential rounds, spectral cosine distance to that voice's centroid, spectral-centroid CV, low-frequency CV, RMS range, duration/character CV, ZCR CV, crest range and boundary loss | Strengthened. Default is 20 rounds; fewer than 20 cannot pass the dedicated timbre gate. Still an acoustic proxy, not speaker verification/MOS. |
| Two-room queue delay | Excess first-PCM and first-non-silent latency over one-room P50 baseline | Covered. Client-observed inferred delay, not server queue instrumentation. |
| Three-room queue delay | Same two independent checks for concurrency 3 | Covered. CLI requires scenarios 1 and 3 and defaults to 1/2/3. |
| First packet | Request-start and first-text-submit clocks; P50/P95/P99/max, including a hard per-request maximum | Covered. |
| First non-silent PCM | Detects the first packet containing an active 10 ms PCM window; reports request/text-relative latency, leading silence, P95 and hard maximum | Newly covered; a fast silent packet can no longer satisfy the audible-data gate. |

## New or materially strengthened report fields

- Schema version increased from 3 to 4.
- Per request:
  - `first_non_silent_packet_ms`
  - `first_char_to_first_non_silent_pcm_ms`
  - `first_character_match` / `last_character_match`
  - `common_prefix_characters` / `common_suffix_characters`
  - `audio.leading_silence_ms` / `audio.trailing_silence_ms`
  - `audio.first_non_silent_chunk_index`
  - `audio.first_non_silent_stream_offset_ms`
  - `audio.timbre.spectral_signature`
  - `audio.timbre.sampled_spectral_centroid_hz`
  - `audio.timbre.dominant_low_frequency_hz`
- Per concurrency:
  - first-non-silent distributions
  - `inferred_non_silent_queue_delay_ms`
- Per voice:
  - first/last-character mismatch counts
  - worst active-window RMS standard deviation
- Per 20-round drift series:
  - spectral cosine-distance distribution
  - spectral-centroid coefficient of variation
  - dominant low-frequency coefficient of variation

## Tests and executable evidence

Commands run from `/Users/sunshiqi/code/phdebate/v2`:

```text
.venv/bin/ruff check scripts/benchmark_tts_quality_gate.py scripts/tests/test_tts_quality_gate.py
All checks passed!

.venv/bin/python -m pytest -q scripts/tests/test_tts_quality_gate.py
19 passed, 1 warning in 1.09s

.venv/bin/python -m pytest -q scripts/tests
25 passed, 1 warning in 3.98s
```

The warning is Python's existing `audioop` deprecation notice; it is not a test failure. Migration away from `audioop` is needed before Python 3.13.

The complete fake gate was also executed with one/two/three-room scenarios and 20 sequential drift rounds:

```text
.venv/bin/python scripts/benchmark_tts_quality_gate.py \
  --tts-mode fake --asr-mode fake \
  --rounds 2 --warmup-requests 1 --drift-rounds 20 \
  --cancel-after-first-pcm-ms 0 \
  --output-dir docs/qa/audio/20260718-openmoss-rebuild/tts-quality-gate-selftest-v4 \
  --overwrite
```

Result: automatic gate PASS, 25/25 automatic checks passed. `release_ready` correctly remains false because fake audio has no human MOS and no validated speaker-identity evidence.

## Real-candidate acceptance boundary

For an actual OpenMOSS candidate, the same script must be run with the candidate session endpoints, all fixed voice prompts, and a real ASR adapter. Acceptance evidence must retain:

- all eight fixed voices;
- 20 sequential drift rounds per voice;
- concurrency 1, 2 and 3;
- zero request failures, zero swallowed/extra/repeated content, zero simulated stalls;
- first text→first PCM P95 within 800 ms and every request below 3 seconds;
- first text→first non-silent PCM P95 within 1 second and every request below 3 seconds;
- two/three-room inferred queue-delay P95 within 500 ms;
- real human listening or a separately validated speaker-embedding gate.

The self-test artifacts must not be cited as evidence that a real model passes these thresholds.
