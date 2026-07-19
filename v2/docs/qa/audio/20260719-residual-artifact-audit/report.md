# 残余杂音与撕裂音定向复核

日期：2026-07-19

## 结论

本轮不修改 MOSS 生成参数、LiveKit 发布时钟、Opus 编码、浏览器缓冲或
AudioWorklet。证据显示最明显的问题集中在 `debate_voice_5`，因此只将该音色临时
隔离：反方一辩改用已经通过连续性验证的 `debate_voice_8`。

这项调整会让 4v4 中反方一辩与反方四辩暂时共用同一音色，但不会增加首声延迟，
也不会改变已稳定的实时播放链路。待新的干净参考录音通过人工试听、首声、长发言、
中断和多轮一致性门禁后，再恢复八席独立音色。

## 证据

生产 1v1 房间最近两段反方一辩归档均来自 `debate_voice_5`：

| 样本 | 时长 | 峰值 | 每秒大于 0.2 的相邻采样跳变 | 8 kHz 以上能量占比 |
|---|---:|---:|---:|---:|
| `voice5-long.wav` | 73.52 s | -7.57 dBFS | 0.24 | 0.6981% |
| `voice5-short.wav` | 32.72 s | -3.06 dBFS | 44.19 | 0.7309% |

同一音色 12 个既有真实 MOSS 样本中：

- 大于 0.2 的相邻采样跳变中位数为每秒 32.26 次；
- P95 为每秒 121.06 次；
- 最大相邻跳变 P95 为 0.759。

作为替代的 `debate_voice_8` 既有真实样本中，上述大跳变为 0，最大相邻跳变 P95
为 0.1447。当前差异远大于网络抖动或普通编码误差，和用户听到的偶发毛刺、撕裂感
一致。

## 上线后验证

隔离规则上线后完成真实 1v1 房间的完整 Agent → MOSS → LiveKit → Chromium
AudioWorklet 验证：

- Agent 首个正文字符到浏览器非静音首声：`2797.9 ms`，通过三秒门禁；
- 服务端首次 LiveKit capture 到浏览器非静音：约 `333.9 ms`；
- 浏览器接收 `audio/opus`，RTP 丢包为 `0`；
- 73.20 秒完整生产归档峰值 `-4.09 dBFS`，无削波；
- 每秒大于 0.2 的相邻采样跳变为 `0.72` 次，显著低于问题样本的 `44.19` 次；
- 最大相邻采样跳变 `0.2809`，与此前通过验收的生产音色处于同一量级；
- 8 kHz 以上能量占比由问题样本的 `0.7309%` 降至 `0.1864%`。

证据文件：

- [`browser-voice8-final-gate.json`](./browser-voice8-final-gate.json)
- [`voice8-production-canary.json`](./voice8-production-canary.json)
- [`voice8-production-canary.wav`](./voice8-production-canary.wav)
- [`voice8-full-production-turn.wav`](./voice8-full-production-turn.wav)

验收房均已终止，MOSS active、pending 和 orphan 均回到 0。

## 未采用的方案

- 不继续增加播放缓冲：会直接损害三秒首声目标，且不能修复归档 WAV 中已经存在的
  源音频毛刺。
- 不提高 Opus 码率：既有离线复核表明更高码率未改善相邻采样跳变。
- 不加入实时低通、去点击或限幅：会改变音色、可能让声音发闷，并需要重新验证整条
  实时链路。
- 不修改浏览器播放器：生产归档 WAV 已包含异常，说明缺陷早于浏览器播放层。

## 回滚

恢复 `apps/api/app/services/voice_runtime/voices.py` 中 `neg_1` 到
`debate_voice_5` 即可；不涉及数据库迁移、MOSS 重启或媒体格式变化。
