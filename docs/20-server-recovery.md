# Server Recovery Notes

This document records the deploy/runtime split for the `117.50.221.11` phdebate server.

## What Is In Git

- phdebate application code.
- Supervisor templates under `deploy/`.
- FunASR streaming service code under `deploy/serve_realtime_ws_phdebate.py`.
- Qwen3-TTS OpenAI-compatible service code under `deploy/qwen3-tts-openai/`.
- Qwen3-TTS non-secret env template under `deploy/qwen3-tts-openai/qwen3-tts.env.example`.

## What Is Not In Git

Runtime state is intentionally not committed because it can contain tokens, match data, audio, exports, and local-only service state.

The important persisted runtime files are under:

```text
/root/autodl-tmp/phdebate/apps/backend/storage/
```

Key files:

- `phdebate.sqlite3`: match state, speakers, agent configs, speech history, app state.
- `integration.json`: ASR/TTS provider settings and voice presets.
- `rulesets.json`: saved rulesets.
- `xiaoqi.json`: Xiaoqi/judging settings.
- `runtime_auth.json`: hashed runtime tokens.
- `fallback_audio_manifest.json`: fallback audio metadata.
- `audio/`, `exports/`, `images/`: generated audio, exports, uploaded/static runtime images.

## Local Backup Made Before Shutdown

Before the planned shutdown on 2026-06-25, a full runtime backup was downloaded outside the git repo:

```text
/Users/sunshiqi/code/autodl_debate/server_runtime_backup_20260625_042443/
```

Files:

- `phdebate-backend-storage.tgz`
- `server-config-and-tts-code.tgz`
- `SHA256SUMS.txt`

Verify after copy:

```bash
cd /Users/sunshiqi/code/autodl_debate/server_runtime_backup_20260625_042443
shasum -a 256 -c SHA256SUMS.txt
```

## Restore Sketch

On a new server, after cloning this repo and installing dependencies:

```bash
sudo mkdir -p /root/autodl-tmp/phdebate/apps/backend
sudo tar -C /root/autodl-tmp/phdebate/apps/backend -xzf phdebate-backend-storage.tgz
sudo tar -C / -xzf server-config-and-tts-code.tgz
sudo cp deploy/phdebate-stack.supervisor.conf /etc/supervisor/conf.d/phdebate-stack.conf
sudo cp deploy/funasr-nano-asr.conf /etc/supervisor/conf.d/funasr-nano-asr.conf
sudo supervisorctl reread
sudo supervisorctl update
sudo supervisorctl restart phdebate qwen3-tts qwen3-tts-webui funasr-nano-asr
```

Keep secrets out of git. If a fresh Qwen3-TTS environment is needed, copy:

```bash
cp deploy/qwen3-tts-openai/qwen3-tts.env.example /root/autodl-tmp/qwen3-tts-openai/.env
```

Then adjust machine-specific paths or tokens locally.
