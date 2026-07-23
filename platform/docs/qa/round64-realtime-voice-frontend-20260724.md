# Round 64：实时语音前端恢复审计

日期：2026-07-24  
范围：浏览器实时语音库、LiveKit/RTC、AudioWorklet、ASR 恢复和舞台投影。未改房间控制页、API 比赛引擎或部署配置。

## 结论

本轮修复了两个可能造成“发言中途无声，随后服务端正常结束本轮”的前端问题：

1. LiveKit 权威音轨被取消订阅，或浏览器底层 `MediaStreamTrack` 直接进入 `ended` 时，旧代码只拆除音频节点，不触发重连。现在会清理唯一播放链、上报明确诊断并进入既有的 0.5 / 1 / 2 秒有界 RTC 恢复；恢复耗尽后仍不切换 PCM/WAV 播放器，页面明确要求房主重试当前步骤。
2. 旧发言的 `speech.interrupted` 事件可能晚于下一发言快照到达。旧实现会执行无 generation 的 `flush()`，误清空刚激活的新发言。现在中断必须绑定事件自身的 generation，或通过原 speech id 找到历史 generation；无法解析 generation 的真人中断不会清空当前 AI 音频。

同时补充了以下边界：

- 权威音轨在自动恢复后重新出现时，立即取消尚未执行的应用层重连，避免刚恢复又被主动断开。
- 暂停、裁判阶段和所有终态都会清空当前 generation，避免终态后残留 WebRTC 尾音。
- 观战模式即使错误收到 `active_speech.content`、字幕段和字幕事件，也不渲染文字稿、逐句字幕或文字记录入口，只保留声音与赛况画面。

## 保持不变的设计

- 浏览器只订阅一个名为 `agent-tts` 的 LiveKit 音轨。
- 隐藏的 `<audio muted>` 仅维持浏览器 RTP 解码，不形成第二条可听音轨；唯一可听输出仍是 `MediaStreamAudioSourceNode → AudioWorklet → AudioContext.destination`。
- generation flush 持续消费 WebRTC 输入并输出静音，避免暂停媒体元素后旧 jitter-buffer 尾音复活。
- 不启用完整 WAV、PCM WebSocket 或多个播放器兜底。
- ASR 继续使用 AudioWorklet 20ms PCM 块、有限预备缓冲、有限重连和人工文字核对，不使用 MediaRecorder Blob。

## 自动化验证

- 实时语音定向测试：17 passed。
  - 单权威音轨和重复轨道拒绝。
  - RTC 预连接、手势解锁、generation flush、旧 generation 禁止复活。
  - TrackUnsubscribed 与 MediaStreamTrack ended 的显式恢复。
  - 自恢复后取消待执行重连。
  - AudioWorklet 去点击声、完整 guard 和过期 epoch 拒绝。
- 观众无文字稿定向测试：1 passed。
- Web 全量：54 files、407 tests 全部通过。
- ESLint：0 errors；13 个既有 warnings。
- Next.js 生产构建：通过，包括 TypeScript 检查和全部页面生成。

## 无法在本机证明的指标

本轮没有真实 MOSS GPU、LiveKit 服务端、公网抖动和实体浏览器音频设备，因此没有宣称：

- Agent 首字到浏览器首声小于 3 秒；
- MOSS RTF 小于 1；
- 真实网络下完全无卡顿、撕裂音或吞字；
- 真实音色主观质量达标。

这些指标仍需在目标服务器上以真实 Agent、MOSS、LiveKit、Chrome/Safari 和网络整形测试完成。前端现在会保留诊断事件和明确恢复路径，便于区分推理慢、RTP 丢包、jitter concealment、浏览器轨道结束和应用层误清空。
