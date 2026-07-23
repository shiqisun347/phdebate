# Round 55：LiveKit 连续音轨与抖动遥测修复

日期：2026-07-22  
范围：LiveKit 发布端、浏览器 RTC 统计、语音遥测持久化  
部署：本轮未部署

## 结论

已修复生产证据中两项可复现问题：

1. 比赛暂停后，引擎会把房间从媒体活跃集合移除并关闭 LiveKit publisher；恢复比赛时重新发布 `agent-tts`，因此 track SID 改变。现在从 `preparing` 开始到 `running / paused / judging` 结束，房间复用同一个 publisher。暂停只撤销当前语音 generation 和清空播放队列，不再销毁房间音轨。
2. 浏览器此前把 WebRTC 的累计值 `jitterBufferDelay` 直接当作当前延迟上报，比赛越长数值越大。现在同时记录平均、最近统计区间和累计原始值，旧字段继续存在但语义修正为平均延迟。

## 修复内容

### 单一连续 LiveKit 音轨

- `MatchEngine._LIVEKIT_ROOM_STATUSES` 固定为 `preparing / running / paused / judging`。
- 引擎 reconciliation 在暂停状态仍保留房间 publisher 和原始 track SID。
- `lobby` 尚未进入比赛媒体生命周期，不提前发布；终止或完成后正常关闭，避免永久占用连接。
- 浏览器原有的单轨保护、AudioWorklet 播放链和 generation flush 逻辑保持不变。

资源取舍：暂停期间 publisher 会继续维持 LiveKit 连续媒体时钟。系统上限只有 5 个房间，这一有界成本优先于恢复时重新协商音轨造成的中断、重复播放器和 track SID 漂移。

### 可解释的 jitter buffer 指标

浏览器每秒读取 RTCStats，计算：

- `jitter_buffer_delay_ms`：兼容字段，现为累计平均延迟。
- `jitter_buffer_delay_avg_ms`：`jitterBufferDelay / jitterBufferEmittedCount`，比赛生命周期平均值。
- `jitter_buffer_delay_current_ms`：相邻 RTCStats 样本的 delay 增量 / emitted 增量，代表最近约 1 秒区间。
- `jitter_buffer_delay_total_ms`：WebRTC 原始累计 delay，仅用于审计和回溯，不再作为实时质量结论。
- `jitter_buffer_emitted_count`：累计发出样本计数。

服务端兼容旧网页：如果收到旧格式的累计 `jitter_buffer_delay_ms` 与 emitted count，会自动还原 total，并把兼容字段规范化为平均值；无需数据库迁移。

## 验证

### API

```text
../../.venv/bin/pytest -q tests/test_livekit_audio.py tests/test_voice_telemetry_api.py
24 passed, 1 warning
```

新增覆盖：

- 暂停 reconciliation 后 publisher 对象、adapter 和 track SID 均不变化。
- 旧浏览器累计 jitter 报告被规范化为平均、当前和 total 三种明确口径。

```text
../../.venv/bin/ruff check app/services/match_engine.py app/services/voice_telemetry.py tests/test_livekit_audio.py tests/test_voice_telemetry_api.py
All checks passed!
```

### Web

```text
npm test -- lib/audio/livekit-room-audio.test.ts lib/audio/voice-telemetry.test.ts
13 passed
```

新增覆盖：

- 首个 RTC 样本 `2s / 100` 得到平均和当前值 `20ms`。
- 后续样本 `3.2s / 140` 得到生命周期平均 `22.857ms`、最近区间平均 `30ms`，而不是错误的 `3200ms`。
- 旧事件格式 `jitterBufferDelaySeconds + jitterBufferEmittedCount` 仍能生成正确报告。

```text
npm run build
Next.js production build passed
```

ESLint 无新增错误；仅保留文件中既有的 `_participant` 未使用警告。

## 后续生产验收

部署后用同一测试房间执行一次“AI 发言中暂停 → 保持暂停超过两个 engine tick → 房主恢复 → 下一次 AI 发言”，浏览器诊断应满足：

- 整个过程只有一个 `track-subscribed` 的 LiveKit track SID。
- 暂停时只出现 generation flush，不出现 publisher participant 离开或新 `agent-tts` publication。
- 恢复后声音继续走同一 AudioWorklet/MediaStream 播放链。
- 管理端 `jitter_buffer_delay_ms` 与 `avg_ms` 相同，`current_ms` 可随网络变化，`total_ms` 单调累计但不参与实时延迟告警。

