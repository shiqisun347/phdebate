# Round 54 生产环境实时语音浏览器黑盒回归

| 项目 | 结果 |
|---|---|
| 测试日期 | 2026-07-22 |
| 生产地址 | `https://117.50.192.216` |
| Web 发布 | `round53-realtime-mobile-controls-20260722` |
| API 发布 | `round53-agent-asr-consistency-20260722` |
| 测试房间 | `335185`、`467467` |
| 房间标识 | 题目均包含 `[QA语音回归]` 或 `[QA移动声音解锁]` |
| 浏览器 | Headless Chrome 150，桌面端与 390×844 手机端 |
| 结论 | 单场发言期间浏览器只存在一个 WebRTC 音频元素和一条远端音轨；暂停、终止能够清理音频；手机端声音解锁有效。但首音体验仍明显超过 3 秒目标且缺少可验证的端到端指标，暂停恢复也没有保持同一条持续音轨。 |

## 严重度汇总

| 严重度 | 数量 |
|---|---:|
| P0 / Critical | 0 |
| P1 / High | 2 |
| P2 / Medium | 1 |
| P3 / Low | 0 |

## ISSUE-001：浏览器可感知的 AI 首音等待约 12.3–12.4 秒，且当前遥测不能验证“首字符后 3 秒”目标

| 字段 | 内容 |
|---|---|
| 严重度 | P1 / High |
| 分类 | 性能、首音、可观测性 |
| 房间 | `335185` |
| 证据 | [完整 HAR](browser-audio-round54-20260722/round54-audio.har)、[AI 首音与播放录像](browser-audio-round54-20260722/videos/ai-first-audio-webrtc.webm) |

### 观测结果

HAR 中两个可比较的用户可感知时间段：

1. 房主开始比赛：`14:53:23.849`；首个 `browser_first_audible`：`14:53:36.168`，相差约 **12.32 秒**。
2. 真人反方完成发言：`14:56:06.023`；下一次 AI `browser_first_audible`：`14:56:18.448`，相差约 **12.43 秒**。

这两个数据包含自动阶段切换、Agent 生成和 TTS 首音，不等同于“Agent 返回首字符到声音”的纯 TTS 延迟，因此不能据此单独断言 TTS 花费了 12 秒。但它们能证明真实用户从操作/轮次切换到听见 AI 的整体等待仍明显大于 3 秒。

当前 `browser_first_audible` 只上报：

```json
{
  "event": "browser_first_audible",
  "metrics": {
    "rms": 0.0352,
    "peak": 0.1123,
    "audio_time_seconds": 11.8693
  }
}
```

`audio_time_seconds` 是持续 MediaStream 的播放时间，而不是从 Agent 首字符开始计算的延迟。后续发言上报过 `174.152` 和 `41.8533`，更说明它不能作为首音 SLA。

### 建议

- 同一 `speech_id/generation` 记录服务器端 `agent_first_delta_at`、`tts_text_first_ingest_at`、`publisher_first_audio_frame_at`。
- 浏览器继续记录 `browser_first_audible_at`，由服务端计算各段延迟和总延迟。
- 管理控制台直接展示 `Agent 首字 → TTS 首帧 → WebRTC 发布 → 浏览器可听` 四段耗时。
- 在没有上述数据前，不应宣称已满足“Agent 首字符后 3 秒内播放”。

## ISSUE-002：暂停后恢复会创建新的 LiveKit 音轨，不符合严格的“单条持续 WebRTC 音轨”要求

| 字段 | 内容 |
|---|---|
| 严重度 | P1 / High |
| 分类 | WebRTC、音轨生命周期、架构一致性 |
| 房间 | `335185` |
| 证据 | [暂停状态](browser-audio-round54-20260722/screenshots/paused-ai-audio.png)、[完整播放录像](browser-audio-round54-20260722/videos/ai-first-audio-webrtc.webm) |

### 观测结果

- 首段 AI 发言期间仅存在一个 `<audio>` 元素和一条远端音轨：`TR_AM4JsagUjeBz22`。
- 同一段连续播放期间音轨 ID 保持不变，没有出现多播放器或多音轨叠加。
- 房主暂停后，浏览器中的 `<audio>` 元素立即变为 `0`，说明播放队列和订阅已清理。
- 继续比赛后的下一段 AI 发言重新出现一条远端音轨，但 ID 变为 `TR_AMePqdKmnMsJmk`。

因此当前实现满足“任意时刻只有一条音轨”，但不满足更严格的“进入房间时提前建立并在整场比赛中保持同一条连续音轨”。暂停/恢复仍包含重新发布或重新订阅过程。

### 建议

- 如果产品要求严格保持一条持续音轨，应让 publisher 生命周期覆盖整场房间，暂停时发送静音帧或清空 TTS context，而不是 unpublish track。
- 浏览器始终订阅同一 track SID，只在音频帧层面清空和恢复。
- 若更换音轨是有意的安全清队列策略，应更新正式技术约束为“任意时刻最多一条音轨”，避免验收定义冲突。

## ISSUE-003：`jitter_buffer_delay_ms` 上报的是累计值，字段名称会误导性能判断

| 字段 | 内容 |
|---|---|
| 严重度 | P2 / Medium |
| 分类 | 语音遥测、指标口径 |
| 房间 | `335185` |
| 证据 | [完整 HAR](browser-audio-round54-20260722/round54-audio.har) |

### 观测结果

`browser_network_sample` 连续上报：

- `jitter_ms`: 3–13 ms
- `packets_lost`: 0
- `bitrate_bps`: 约 22.7–69.7 kbps
- `jitter_buffer_delay_ms`: 84,326,400 到 1,736,889,600
- `jitter_buffer_emitted_count`: 519,360 到 10,071,360

数千万到十亿级的 `jitter_buffer_delay_ms` 显然不是用户可感知的实时延迟，而是累计 `jitterBufferDelay × 1000`。例如第一组：

`84,326,400 / 519,360 ≈ 162.4 ms`

这才接近该时点每个已输出音频单元的平均 jitter buffer delay。

### 建议

- 将现字段重命名为 `jitter_buffer_delay_total_ms`。
- 新增 `jitter_buffer_delay_avg_ms = total / emitted_count`。
- 控制台优先展示平均值、P95、最近 5 秒变化，不直接展示累计量。

## 已通过的音频行为

### WebRTC 单音轨与播放状态

- AI 发言播放中浏览器只有一个 `<audio>` 元素。
- `<audio>.srcObject` 只有一条 `remote audio` 轨道。
- 轨道状态为 `live`，音频元素 `paused=false`、`muted=false`。
- 连续发言期间音轨 ID 不变。
- WebSocket/LiveKit 阶段和网页阶段一致，当前发言席位正确指向 AI。
- 未观察到 MP3 分片播放器、多个 `audio.play()` 音源叠加或并行音轨。

证据：[AI 播放桌面端](browser-audio-round54-20260722/screenshots/ai-speaking-desktop.png)

### 暂停与终止清理

- AI 发言阶段点击暂停后，比赛状态立即变为暂停。
- 暂停后浏览器 `<audio>` 元素数量为 `0`。
- 终止房间 `335185` 后，房主页面立即进入结果页且音频元素为 `0`。
- 终止房间 `467467` 后，匿名观众的旧音轨在约 1.5 秒内被移除，没有继续播放整段剩余音频。
- 两个 QA 房间均已终止，没有遗留运行中的语音任务页面。

证据：

- [暂停后清音频](browser-audio-round54-20260722/screenshots/paused-ai-audio.png)
- [终止结果页](browser-audio-round54-20260722/screenshots/terminated-result.png)

### 手机端声音解锁

- 匿名手机观众首次进入时，浏览器因 autoplay policy 拒绝播放，控制台产生预期的 `NotAllowedError` warning。
- 页面明确显示“开启比赛声音”，不是静默无声。
- 解锁前：一条 live track 已订阅，但 `<audio paused=true>`、`currentTime=0`。
- 用户点击声音按钮后，按钮变为“关闭比赛声音”。
- 解锁后：`paused=false`，`currentTime` 开始增长，仍只有同一条音轨。
- 未出现需要刷新才能播放的问题。

证据：

- [手机端声音未解锁](browser-audio-round54-20260722/screenshots/mobile-watch-locked.png)
- [手机端声音已解锁](browser-audio-round54-20260722/screenshots/mobile-watch-unlocked.png)
- [解锁操作录像](browser-audio-round54-20260722/videos/mobile-sound-unlock-2.webm)

## Console 与 Network

- 未捕获 JavaScript exception 或 unhandled promise rejection。
- LiveKit 连接成功，版本 `1.13.3`。
- `rtc-token`、房间接口、控制接口均返回 200。
- `voice-telemetry` 请求持续返回 202。
- AI 播放期间样本 `packets_lost=0`。
- 唯一稳定出现的 warning 是未发生用户手势前的 autoplay `NotAllowedError`，页面已提供解锁按钮，因此属于预期浏览器行为。
- HAR 共记录 84 个请求，没有使用 network route 或 mock。

## 附加观察

手机观众在录像工具造成的连续快速重连后曾显示“系统观战总人数已达 5 人”，但原页面仍短暂保有 live track。由于录像工具会重启捕获上下文并产生多个新的 guest identity，这一现象可能由测试工具放大，本轮不计入正式缺陷；建议后续用普通 Chrome 手动快速刷新复测观众席位释放时间。

## 测试清理

- 房间 `335185` 已终止并进入结果页。
- 房间 `467467` 已终止并进入结果页。
- 房主已退出登录。
- 房主和匿名手机观众浏览器会话均已关闭。
- 没有遗留运行中的 QA 房间或浏览器会话。

