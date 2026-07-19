# Native 48GB real-canary evidence inventory

Source host: `btbu-6201`  
Source root: `/home/ubuntu/sunsq/moss-realtime-48gb-canary-20260718`  
Copied: 2026-07-18, read-only, with existing local files preserved.

All runs use the fixed OpenMOSS/model/codec revisions, SDPA, one 48GB CUDA GPU,
the Realtime model and complete codec on `cuda:0`, full codec streaming context,
and eight cached voice prompts. GPU usage after prompt preparation was about
12,296.75MiB (12.01GiB). Warmup/compile turns are excluded from the measured
release gates.

| Evidence directory | Distinguishing configuration | First PCM measured-1 / measured-2 | RTF measured-1 / measured-2 | Gap P99 measured-1 / measured-2 | Playback underrun | Result |
| --- | --- | ---: | ---: | ---: | --- | --- |
| `native-48gb-results` | Initial all-CUDA implementation; per-turn compiled-inferencer class lifecycle | Not recorded | Not recorded | Not recorded | Not recorded | **FAIL** — `RecompileLimitExceeded: cache_size_limit reached` |
| `native-48gb-results-stable` | Stable/reused inferencer; run-default DCF6/ICF1; playback simulation not yet recorded | 384.8ms / 254.2ms | 0.717 / 0.720 | 328.3ms / 327.8ms | Not recorded | **FAIL** — first PCM PASS; RTF and gap FAIL |
| `native-48gb-results-dcf3` | Stable/reused inferencer; decode chunk frames 3, initial chunk frame 1 | 388.3ms / 256.2ms | 0.851 / 0.931 | 200.8ms / 623.3ms | Not recorded | **FAIL** — first PCM PASS; RTF and gap FAIL |
| `native-48gb-results-dcf6-playback` | Stable/reused inferencer; decode chunk frames 6, initial chunk frame 1; continuous-clock simulation at 80/100/120ms | 384.8ms / 259.7ms | 0.723 / 0.801 | 332.8ms / 764.3ms | measured-1: 0/0/0; measured-2: 1/1/1, maximum underrun 88.8/68.8/48.8ms | **FAIL** — first PCM PASS; RTF, gap, and measured-2 playback FAIL |

Gate thresholds serialized by the canary are first PCM ≤0.8s, RTF ≤0.65,
and inter-chunk gap P99 ≤0.2s. None of the four runs is a release PASS.

Notes:

- `DCF` means `decode_chunk_frames`; `ICF` means `initial_chunk_frames`.
- DCF/ICF were not serialized in the older stable JSON; its DCF6/ICF1 label is
  taken from the run's gateway defaults and is consistent with the later
  explicitly named DCF6 artifact.
- The initial run produced a warmup WAV but failed before a completed turn record
  could be appended to JSON.
- The stable and DCF3 JSON versions predate the playback-simulation field, so
  underrun status cannot be reconstructed from those artifacts.
- DCF6 playback measured-1 stays queued at every tested initial buffer. Measured-2
  still underruns once even at 120ms, so increasing the initial buffer within the
  requested 80–120ms range does not make this run continuous.
- The copied raw set contains 18 files. A local-versus-remote SHA-256 manifest
  comparison completed with no differences.
