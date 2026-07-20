# 8 个固定辩论音色资产盘点

- 盘点时间：2026-07-18 03:40 CST
- 来源：生产 LightTTS，只读检查；未修改、复制或播放提示音
- 隐私边界：不记录服务器路径，不记录逐字稿正文，只记录文件/逐字稿 SHA-256 和校验结果
- 当前音色集合版本：`549f3dcc3962b2027c2433c6767e57b7aff4e6f23eef78dfd4ee1a32a7d9a5ce`
- 结论：**NO-GO**。8 个音色中只有 4 个存在，满足全部规范的音色为 0 个。

## 固定席位映射

| 席位 | 固定音色 |
|---|---|
| 正方一至四辩 | `debate_voice_1` 至 `debate_voice_4` |
| 反方一至四辩 | `debate_voice_5` 至 `debate_voice_8` |

## 资产与版本清单

| 席位 | 音色 | WAV SHA-256 | 逐字稿 SHA-256 | 格式 | 时长 | 峰值 | 逐字稿/结束标记 | 结论 |
|---|---|---|---|---|---:|---:|---|---|
| aff_1 | `debate_voice_1` | `342641905615bbc53c742f04a4e56c96289242c2a166b64db7f4d663fb7dbade` | `fd1e89239c8f1b3c59d32f4bdb5a5fe3601c6c39e5153fbd4895c64cf5fa36de` | PCM16 / 22050Hz / mono | 3.936s | 0.0dBFS | 已配置 / 有 | NO-GO |
| aff_2 | `debate_voice_2` | `7033e9fe0170f4b8e65ee3fc641960f5efd4451917065c2f8efe4d27d4e66ac2` | `4ef2c949f93b8f9422a2367ad763622ce204225177d76faeef27eae4bcd7bef9` | PCM16 / 22050Hz / mono | 6.571s | 0.0dBFS | 已配置 / 有 | NO-GO |
| aff_3 | `debate_voice_3` | `8e303aafcc91014fc9117c38aa29952f0559b3c786e1d8bea13e46a92d2597c8` | `f6cd69dec0c8d2eaacb15f6deb92a41565f44a3e45c576655b583646c5361e26` | PCM16 / 22050Hz / mono | 3.959s | 0.0dBFS | 已配置 / 有 | NO-GO |
| aff_4 | `debate_voice_4` | `eb709fb56bc73d0fd4b360d2476de7307ac9c323565831e43523842dc7acc652` | `b436a6be3fbcd53dd8dd9d3f64a40184590b487f123bdec534c83e12d1bc6a9c` | PCM16 / 22050Hz / mono | 5.341s | 0.0dBFS | 已配置 / 有 | NO-GO |
| neg_1 | `debate_voice_5` | 缺失 | 缺失 | - | - | - | 缺失 | NO-GO |
| neg_2 | `debate_voice_6` | 缺失 | 缺失 | - | - | - | 缺失 | NO-GO |
| neg_3 | `debate_voice_7` | 缺失 | 缺失 | - | - | - | 缺失 | NO-GO |
| neg_4 | `debate_voice_8` | 缺失 | 缺失 | - | - | - | 缺失 | NO-GO |

## 缺口

1. `debate_voice_5..8` 的 WAV 与匹配逐字稿全部缺失。
2. 现有 `debate_voice_1..4` 均为 22050Hz，不是要求的 24000Hz。
3. 现有四段时长均不足 8 秒；要求是 8–15 秒。
4. 现有四段峰值约 0dBFS，未满足峰值不高于 −3dBFS。
5. 现有四段具备独立逐字稿及 `<|endofprompt|>`，但本次只验证配置、哈希和标记存在；“逐字稿与音频完全一致”仍需人工复核或独立 ASR 对齐。
6. 未发现正式的人工 MOS、8 音色盲听区分率、连续 20 轮音色身份检查记录。

在补录并审核 8 个真实中性普通话提示音前，不应把当前集合标记为生产合规，也不能以生成或复制的伪音色填补缺口。
