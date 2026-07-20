# 实时语音可靠基线保护规则

当前 MOSS TTS、LiveKit 发布和浏览器播放链路已经通过真实长发言验收，属于冻结基线。

后续进行用户、房间、比赛流程、管理后台、排行榜、历史数据、UI 和性能改进时，不修改：

- MOSS Gateway、模型参数和固定音色；
- Agent 文本进入 TTS 的增量会话逻辑；
- PCM 重采样、分帧、LiveKit 发布与中断清队列；
- 浏览器 AudioWorklet、PCM 播放、LiveKit 音轨和播放缓冲；
- `DebateStage` 中与录音、ASR、TTS 播放和中断有关的代码。

受保护文件由 `scripts/create_reliable_audio_manifest.py` 逐文件记录 SHA-256。开始非音频改造前和发布前均执行：

```bash
.venv/bin/python scripts/create_reliable_audio_manifest.py \
  --verify backups/<可靠版本>/reliable-audio-baseline.json
```

只有用户明确要求修改语音链路，并重新完成首声、长发言连续性、中断、音色一致性、浏览器和多人并发门禁后，才生成新的可靠基线。

当前验收依据见 `docs/qa/audio/20260719-voice-confidence-ab/report.md`。

2026-07-19 在不修改生成、传输和浏览器播放实现的前提下，临时隔离了存在明显高频
毛刺的 `debate_voice_5`，反方一辩改用 `debate_voice_8`。新可靠基线为：

```bash
.venv/bin/python scripts/create_reliable_audio_manifest.py \
  --verify backups/reliable-audio-20260719-voice5-quarantine/reliable-audio-baseline.json
```

该基线包含 82 个受保护文件，指纹为
`d796e70cf988fdcb75bd133959b2a58ac6227086bd52faac446779c31a7855a5`。端到端首声、
完整长发言与音频连续性证据见
`docs/qa/audio/20260719-residual-artifact-audit/report.md`。
