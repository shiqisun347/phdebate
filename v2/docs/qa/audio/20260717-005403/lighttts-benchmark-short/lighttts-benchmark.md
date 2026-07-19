# LightTTS 固定语料质量基准

- 执行时间（UTC）：2026-07-16T20:30:38.554591+00:00
- 语料版本：2026-07-17-v1
- Profile：full
- 音色：debate_voice_1, debate_voice_2, debate_voice_3, debate_voice_4
- 成功/请求：8/8
- 标准中文回转 CER 中位数 / P95：None / None
- 服务端完整 WAV 合成耗时 P50 / P95：1.131 s / 1.828 s
- RTF P50 / P95：1.286 / 1.597
- 样本 RMS 响度极差：10.61 dB

说明：合成耗时从应用 provider 调用开始到完整 WAV 原子落盘，包含排队、切分、重试和合并；它不是浏览器首帧或实际发声延迟。CER 是 LightTTS→FunASR 回转指标，必须结合人工试听，不能替代 MOS。

| ID | 音色 | 字符 | 分段 | 合成 s | 音频 s | RTF | 格式 | kbps | RMS dBFS | 峰值 dBFS | 最大内静音 ms | CER | 回转文本 | 结论 |
|---|---|---:|---:|---:|---:|---:|---|---:|---:|---:|---:|---:|---|---|
| ONE_CHARACTER | debate_voice_1 | 1 | 1 | 0.992 | 0.680 | 1.459 | WAV/NONE/24000Hz/16bit/1ch | 384.5 | -22.12 | -3.36 | 0 | 0.0% | 好。 | FAIL: RTF>=0.8 |
| FIVE_CHARACTERS | debate_voice_1 | 5 | 1 | 1.398 | 1.400 | 0.999 | WAV/NONE/24000Hz/16bit/1ch | 384.3 | -15.53 | -2.76 | 0 | 0.0% | 证据最重要。 | FAIL: RTF>=0.8 |
| ONE_CHARACTER | debate_voice_2 | 1 | 1 | 1.024 | 0.920 | 1.113 | WAV/NONE/24000Hz/16bit/1ch | 384.4 | -21.28 | -4.37 | 0 | 0.0% | 好。 | FAIL: RTF>=0.8 |
| FIVE_CHARACTERS | debate_voice_2 | 5 | 1 | 1.627 | 1.840 | 0.884 | WAV/NONE/24000Hz/16bit/1ch | 384.2 | -18.14 | -2.81 | 0 | 0.0% | 证据最重要。 | FAIL: RTF>=0.8 |
| ONE_CHARACTER | debate_voice_3 | 1 | 1 | 0.788 | 0.480 | 1.642 | WAV/NONE/24000Hz/16bit/1ch | 384.7 | -19.54 | -3.22 | 0 | 0.0% | 好。 | FAIL: RTF>=0.8 |
| FIVE_CHARACTERS | debate_voice_3 | 5 | 1 | 1.237 | 1.280 | 0.966 | WAV/NONE/24000Hz/16bit/1ch | 384.3 | -16.05 | -1.9 | 90 | 0.0% | 证据最重要。 | FAIL: RTF>=0.8 |
| ONE_CHARACTER | debate_voice_4 | 1 | 1 | 0.836 | 0.560 | 1.493 | WAV/NONE/24000Hz/16bit/1ch | 384.6 | -24.68 | -8.32 | 60 | 0.0% | 好。 | FAIL: RTF>=0.8 |
| FIVE_CHARACTERS | debate_voice_4 | 5 | 1 | 1.937 | 1.280 | 1.513 | WAV/NONE/24000Hz/16bit/1ch | 384.3 | -14.07 | -0.62 | 50 | 0.0% | 证据最重要。 | FAIL: RTF>=0.8 |

## 五项发布门槛评分框架

| 项目 | 分数 | 状态 | 证据边界 |
|---|---:|---|---|
| ASR 质量 | BLOCKED | BLOCKED | TTS→FunASR 回转不能把 FunASR 自身错误与 TTS 错误分离，需固定真人参考音频独立测量。 |
| TTS 内容完整性 | 20 | FAIL | 按合成成功率、标准中文 CER 中位数和 P95 自动评分。 |
| TTS 自然度 / MOS | BLOCKED | BLOCKED | 需要至少两个场景的人工试听与 MOS；自动波形指标不能替代自然度评分。 |
| TTS 实时性 | 40 | FAIL | 仅评价服务端完整 WAV 落盘耗时与 RTF，不代表浏览器首播延迟。 |
| 浏览器播放稳定性 | BLOCKED | BLOCKED | 需要 Computer Use、Network 和实际播放观测卡顿、缓冲、取消与旧音频复活。 |

## 自动门槛

- 标准中文 CER：中位数 ≤5%，P95 ≤10%。
- 长文本 RTF：建议 <0.8。这里统一报告所有样本，短句固定开销会使 RTF 更苛刻。
- 内部非语义静音：单次不超过 500ms；自动静音检测也会包含正常标点停顿，需人工复核波形和试听。
- 响度：跨样本 RMS 极差建议 ≤3dB，削波样本应为 0。
- MOS 与浏览器首播/卡顿必须另行人工和 Computer Use 验收，本脚本不虚构这两项分数。
