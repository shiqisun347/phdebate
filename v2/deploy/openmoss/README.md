# OpenMOSS isolated endpoint deployment tooling

This directory implements only step 1 preparation from
`docs/realtime-voice-rebuild.md`. It does not deploy anything by itself and does
not include a vLLM adapter.

The admission path is deliberately fail-closed:

- The native OpenMOSS checkout, Realtime model and codec revisions are fixed in
  `deployment-manifest.json`.
- Realtime model, tokenizer and codec paths are mandatory local directories.
  Network model IDs are never passed to the gateway, and Hugging Face plus
  Transformers offline modes are forced on.
- The eight current AISHELL-3 candidate WAV files are fixed by distinct SHA-256
  values. A missing, changed, duplicated or symlinked prompt is rejected.
- Each process requires one exact GPU UUID and an operator-approved fingerprint
  derived from UUID, model, total memory, driver, VBIOS and PCI bus ID.
- The selected GPU must have at least 24GiB total and 20GiB free memory. The
  new-server profile intentionally allows the measured GPU-resident FunASR
  process to coexist with MOSS; the free-memory floor remains fail-closed for
  unknown or excessive GPU use.
- The bind address is loopback, ports are explicit, sub-24GB diagnostics are
  forced off, and Supervisor programs default to `autostart=false`.
- API keys are read only from mode `0600` files. Key values are never placed in
  templates, run manifests, readiness evidence or telemetry.

`preflight.py` writes a mode `0600` run manifest containing revisions, prompt
hashes, endpoint port, GPU UUID/fingerprint and admission evidence. It rejects
symlinked or missing snapshot directories, requires valid model/codec
`config.json`, tokenizer configuration/data, and safetensors or PyTorch weights,
then records the path, size and SHA-256 of every critical config, tokenizer
asset, weight index and referenced weight shard. To obtain an
approved fingerprint without weakening admission, use the read-only provisioning
command below and record `fingerprint_sha256` in the protected Supervisor
environment. Do not substitute a moving GPU index:

```bash
deploy/openmoss/gpu_fingerprint.py --gpu-uuid GPU-... --require-idle
```

`run-endpoint.sh` requires the exact acknowledgement:

```text
OPENMOSS_DEPLOY_ENABLE=I_UNDERSTAND_THIS_STARTS_A_GPU_GATEWAY
```

It runs preflight, starts one Uvicorn worker, then waits up to 900 seconds for
authenticated readiness. Readiness must prove the fixed upstream revision,
native `openmoss` backend, completed warmup, capacity 1, zero orphans and
`sub24gb_diagnostic:false`; otherwise the process group exits for Supervisor to
handle. The ready response is saved beside the run manifest.
An OS file lock keyed by GPU UUID also closes the concurrent-preflight race: two
Supervisor programs cannot lease the same device even if they start together.
Provision `MOSS_GPU_LOCK_DIR` as a non-symlink directory writable only by the
gateway service account before starting Supervisor.

`supervisor-3-endpoints.conf` contains three independent gateway and telemetry
programs. Provide three dedicated GPU UUID/fingerprint pairs, three distinct
ports, three protected API-key file paths and all three validated local snapshot
paths through Supervisor's inherited environment. `supervisor.env.example` is
intentionally non-runnable until every
`REQUIRED_*` value and the explicit acknowledgement are replaced.

Local static verification (no GPU access and no deployment):

```bash
deploy/openmoss/test-static.sh
```
