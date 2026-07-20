# Agent delta → realtime TTS latency audit

Date: 2026-07-18

Scope: Agent SSE body filtering, `SpeakableClauseAssembler`, first-readable-body timestamp,
incremental TTS submission, and interrupt transcript exclusion. No deployment, model, or
MOSS gateway changes were made.

## Result

The aggregation layer now starts its hard latency budget at the first non-empty body delta,
not after ten characters have already accumulated.

- Preferred first chunk: 10–16 characters, or an earlier complete strong-punctuation phrase.
- Later preferred windows: 16–22–28 characters, capped at 28.
- Hard wait: 200ms by default. If the Agent stalls below ten characters, the available short
  body prefix is submitted at the deadline instead of waiting indefinitely.
- Cancellation polling no longer causes an accidental early flush before the text deadline.
- `thinking`, `reasoning`, `analysis`, and compound event types such as
  `response.reasoning_summary_text.delta` are excluded from body deltas and cannot start the
  first-readable timestamp.
- The timestamp callback still runs immediately before the first body delta enters the
  assembler/TTS session. `audio.rtc.started` / `audio.stream.started` carries it as
  `agent_first_readable_delta_at`.
- Interrupt cancels the Agent iterator and TTS session. The locally buffered, unsubmitted
  suffix is discarded; interrupted speeches remain outside match history because history only
  reads `Speech.status == "completed"`.

## Measured local aggregation latency

Twenty runs per case, Python event-loop clock, default `maximum_wait_seconds=0.2`:

| Case | Minimum | Median | Maximum | First chunk |
| --- | ---: | ---: | ---: | --- |
| Agent stalls at four body characters | 200.114ms | 200.129ms | 200.223ms | `短正文` |
| Sixteen body characters already available | 0.061ms | 0.067ms | 0.195ms | 16 characters |

Chunk-growth diagnostic:

- 80 punctuation-free Mandarin characters: `[16, 22, 28, 14]`.
- A long first sentence no longer overruns the first window: `[16, 13]`.

This proves the assembler consumes no more than about 0.2s of the 2.5s
first-body-delta-to-browser-sound budget. It does not by itself prove the remaining TTS,
WebRTC, decoder, and browser portion; that remains a real-GPU/browser acceptance gate.

## Verification

```text
.venv/bin/ruff check \
  apps/api/app/services/voice_runtime/text.py \
  apps/api/app/services/voice_runtime/pipeline.py \
  apps/api/tests/test_realtime_voice.py \
  apps/api/tests/test_voice_runtime.py

All checks passed!
```

```text
.venv/bin/pytest -q apps/api/tests/test_realtime_voice.py \
  apps/api/tests/test_voice_runtime.py apps/api/tests/test_providers.py \
  -k 'realtime or agent_provider or text_runtime or clause_assembler or incremental_pipeline'

34 passed, 77 deselected
```

```text
.venv/bin/pytest -q apps/api/tests/test_platform.py \
  -k 'realtime_voice_pipeline or realtime_voice_pause_interrupts_agent'

3 passed, 137 deselected
```

```text
PYTHONPATH=apps/api:. .venv/bin/pytest -q apps/api/tests

377 passed, 22 warnings
```

The warnings are existing Starlette, `audioop`, and Alembic deprecations; no test failed.
