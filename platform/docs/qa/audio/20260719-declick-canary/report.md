# WebRTC 边缘去点击修复验收

日期：2026-07-19

## 结论

已上线一个局部、可回滚的浏览器播放修复：LiveKit 音轨在新一轮激活、静音切换和
打断时，不再从数字零直接硬切到任意相位的语音波形，而是使用 5 ms 增益斜坡完成
切换。该调整不改变 MOSS 模型、音色、Prompt、PCM、Opus 码率、网络缓冲或服务器
队列，也不增加首声等待时间。

这项修改针对的是短促 click/pop。它不能修复 TTS 源 WAV 中本来就存在的粗糙音色，
因此 `debate_voice_5` 仍保持隔离，不重新启用。

## 根因与改动

旧 AudioWorklet 在以下时刻直接执行 `0 ↔ 原始波形`：

- 新的 TTS generation 激活；
- 用户开启或关闭声音；
- 暂停、跳过或终止当前发言。

若切换点不是零交叉，单个采样的幅度突变会被听成很短的杂音或撕裂。新实现使用
5 ms 线性增益斜坡消除边缘不连续，保持原有 220 ms 打断保护区；打断时最多只保留
5 ms 衰减尾部，不会让旧 generation 在保护区后复活。

涉及文件：

- `apps/web/public/worklets/livekit-interrupt-gate.js`
- `apps/web/lib/audio/livekit-interrupt-gate-worklet.test.ts`

## 自动化与真实链路结果

- 前端完整测试：198 项通过。
- 去点击单元测试：原模拟硬切幅度约 `0.75`；斜坡内最大相邻变化不超过 `0.15`。
- 生产真实链路：Agent → MOSS → LiveKit → Chromium AudioWorklet。
- 测试房间：`341715`，测试完成后已终止。
- Agent 首个可读字符到浏览器非静音：`2292.8 ms`，满足 `< 3000 ms`。
- 服务端首个 LiveKit capture 到浏览器非静音：`190.8 ms`。
- 稳态音频：`audio/opus`，约 `54–67 kbps`。
- RTP 丢包：`0`。
- `concealedSamples`：连接初期累计 `840`（约 `17.5 ms`），随后整段发言不再增长。
- 人工暂停到持续静音：`88.8 ms`，满足 `< 250 ms`。
- 打断后未观测到旧音频复活、浏览器错误或播放器重建。
- 测试清理后 MOSS：`active=0`、`pending=0`、`orphan_count=0`。

结构化证据：

- [浏览器门禁 JSON](./browser-first-sound.json)
- [浏览器截图](./browser-first-sound.png)

## 发布与回滚

- 当前 Web release：`audio-declick-20260719`。
- 上一可直接回滚 release：`public-live-status-20260719`。
- 新可靠音频指纹：
  `3219138b22878e766c94b9a1fa0e422211a6f27dd74afbbb93a8e45f7e295285`。
- 新基线：
  `backups/reliable-audio-20260719-declick/reliable-audio-baseline.json`。
- 修改前工作文件和原可靠基线保存在：
  `backups/audio-declick-predeploy-20260719/`。
- 如主观试听发现声音起伏、打断拖尾或兼容性回退，只需恢复上一 Web release；API、
  MOSS、LiveKit 和数据库均未改变。

## 边界

自动化能证明采样连续性、首声、丢包、concealment 和打断行为，但不能代替人在真实
扬声器上的主观试听。若后续仍听到明显杂音，请保留房间号、席位和大致时间，以便将
归档 WAV 与浏览器传输指标逐一对齐。没有可复现证据前，不继续叠加低通、降噪、扩大
缓冲或全链路后处理。
