# TTS 质量门：8 音色与连续漂移扩展

更新时间：2026-07-18

## 实现范围

- `scripts/benchmark_tts_quality_gate.py`
  - 真实 TTS 运行默认要求固定 8 音色版本清单；不完整清单只能使用显式 diagnostic-only 参数继续，自动门仍会失败。
  - 固定映射：正方一至四辩对应 `debate_voice_1..4`，反方一至四辩对应 `debate_voice_5..8`。
  - 每音色输出 CER、删除型吞字率、首字/末字缺失、RMS、卡顿、PCM 块间隙、块边界静音以及 MOS 辅助分数。
  - 默认对每个真实音色连续执行 20 轮，报告 RMS 极差、每字时长 CV、过零率 CV、crest factor 极差和 CER P95。
  - 1/2/3 房并发均执行；2 房和 3 房分别报告相对单房 P50 的客户端观测排队增量。
  - 自动 CER 默认门槛收紧到 2%，首尾吞字和删除型吞字默认要求 0，PCM 块间隙 P99 最大值要求不超过 200ms。
  - 可选读取人工 MOS JSON，每音色计算均值；未提供人工评分时明确标记为 BLOCKED。
- `scripts/inventory_tts_voice_assets.py`
  - 只读 WAV 和两个精确的 LightTTS prompt 配置键。
  - 不记录逐字稿正文、服务器路径、其他环境变量或密钥。
  - 生成音频/逐字稿 SHA-256、音频格式和逐项合规检查。

## 证据边界

自动漂移代理只观察响度、时长、过零率和峰均比的一致性。它能发现明显的速度、音量和频谱形态变化，但不能证明说话人身份不变。正式发布仍需要：

1. 每个音色人工 MOS 平均分不低于 4/5。
2. 8 音色盲听区分率不低于 80%。
3. 每音色连续 20 轮人工听辨无可听音色漂移，或使用经过独立验证的 speaker embedding 门禁。

因此报告区分 `automatic gate` 与 `release_ready`；自动指标通过但人工证据缺失时，不能标记为可发布。

## 专项验证

执行：

```text
.venv/bin/python -m ruff check scripts/benchmark_tts_quality_gate.py scripts/inventory_tts_voice_assets.py scripts/tests/test_tts_quality_gate.py scripts/tests/test_inventory_tts_voice_assets.py
.venv/bin/python -m pytest -q scripts/tests/test_tts_quality_gate.py scripts/tests/test_inventory_tts_voice_assets.py
.venv/bin/python -m py_compile scripts/benchmark_tts_quality_gate.py scripts/inventory_tts_voice_assets.py
```

结果：

- Ruff：PASS。
- 专项单元测试：12 passed。
- py_compile：PASS。
- 默认 fake 模式完成 1/2/3 房、连续 20 轮漂移、取消和 JSON/Markdown 报告自测：自动门 PASS；人工 MOS/身份听辨按设计保持 BLOCKED。
- 未运行全量 API pytest，未修改或部署生产配置。
