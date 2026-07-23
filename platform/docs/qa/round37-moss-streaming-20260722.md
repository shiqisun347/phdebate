# Round 37：MOSS 真双向流式与单 WebRTC 音轨验收

日期：2026-07-22  
环境：`117.50.192.216` 生产服务器  
范围：MOSS-TTS-Realtime、Agent 增量文本、LiveKit 单轨发布、浏览器起播预算

## 已修复

- 首个 Agent 增量过短时不再立即启动 MOSS；首段至少积累 12 个可朗读字符，或等到完整短句结束，避免预填充未满足时触发超时并留下 GPU orphan。
- MOSS 启动预热拆分为 `warmup_text`（助手正文）和 `warmup_user_text`（正式辩论语气指令），八个音色均在 `ready` 前使用生产 instruction 预热，避免首次正式发言冷编译。
- LiveKit 起播缓冲由 1200ms 校准为 800ms；浏览器正式 AI 语音仍只订阅一条 `agent-tts` WebRTC 音轨，不启用 PCM/WAV/HTMLAudio 回退。
- 首段测试与后续文本持续进入同一 MOSS WebSocket/context；没有逐句重连。

## 生产测量

单 GPU、串行、正式辩论 instruction、八个音色各 1 次：

| 指标 | 结果 |
| --- | ---: |
| 首 PCM P50 / P95 / 最大 | 590 / 623 / 625 ms |
| RTF P50 / P95 / 最大 | 0.919 / 0.979 / 0.995 |
| 活跃 RTF P95 | 0.966 |
| 成功 / 失败 | 8 / 0 |
| WebSocket 释放确认 | 8 / 8 |
| orphan | 0 |

重复 5 号音色 5 次：成功 5/5，首 PCM P95 602ms，RTF 最大 0.929，800ms 连续播放模型无欠载。

LiveKit 直连探针：`audio.stream.started` 事件为 `transport=livekit`，单一 track SID，起播约 1.93s，MOSS 最终 `ready/active=0/orphan=0`。

浏览器验收：在临时房间 `635838` 中先点击“开启比赛声音”，再发布同一条 LiveKit 音轨；页面观察到 1 个 `audio` 元素、`paused=false`、`readyState=4`、播放时间持续增长，控制台无错误。LiveKit 信令 URL 带 `auto_subscribe=0`。未解锁声音时 Chrome 正确返回 autoplay `NotAllowedError`，页面保留明确的“开启比赛声音”按钮；该错误不是播放器回退或音频撕裂。

## 解释

原先的 15 秒 `gateway_error/push_ack_timeout` 不是网络播放器切换问题，而是生产 user instruction 没有预热导致固定形状 CUDA 编译落在首个实时请求；同时首段过短会让预填充等待与应用层超时互相放大。两处现已移到启动期或文本聚合层处理。

## 仍需人工听感门禁

自动指标不能替代真人听感。上线前仍需在 Chrome/手机实际订阅 LiveKit 音轨，确认连续播放、无撕裂、无杂音、音色一致；若完整卡顿，使用房主“重置当前发言”，不切换播放器。

## 依赖阻塞

完整人机比赛的外部 Debate Agent 上游仍返回 `auth_unavailable/llm_unavailable`；这不是本轮 MOSS/LiveKit 修复可以在本机解决的问题。
