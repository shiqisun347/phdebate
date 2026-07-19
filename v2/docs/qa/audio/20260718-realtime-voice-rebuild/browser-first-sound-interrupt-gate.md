# Chrome/Safari 浏览器首声与打断门禁

测试时间：2026-07-18（Asia/Shanghai）  
范围：只读审查、独立浏览器诊断脚本与合成 PCM 烟测；未部署、未启动生产比赛、未使用 AdsPower/SunBrowser。

## 结论

已实现可重复的 Chrome/Chromium 数字音频门禁，直接观察 WebRTC `MediaStream` 经浏览器解码后的非静音 PCM，并在用户手势解锁播放后测量：

- `audio.rtc.started` 到首个连续非静音 WebAudio 样本；
- 服务端 `server_first_capture_at` 到浏览器首个非静音样本的跨时钟估算；
- 可选的 Agent 首个正文 delta 时间到服务端首帧、浏览器首声；
- `audio.rtc.interrupt` 到 `<audio>` pause/load/srcObject 清空；
- 中断到持续静音，以及静音后旧 generation 是否再次出现声音。

探针不使用 `MediaRecorder`、MP3、音频文件分片或固定 400ms 缓冲。烟测音源由 Web Audio Oscillator 生成连续 PCM，通过 `MediaStream` 进入与 LiveKit 相同的浏览器媒体图。

当前生产仍不能形成完整的“Agent 首个可朗读 delta → 浏览器首声”自动验收结论：现有 `audio.rtc.started` 已包含 `server_first_capture_at`，但没有首个正文 delta 的服务端时间戳。门禁会明确返回 `BLOCKED_MISSING_AGENT_DELTA_TIMESTAMP`，不会用 TTS 开始、最终正文或 thinking 时间冒充起点。

## 现有播放路径审查

Chrome/Safari 的正式下行路径位于：

- `apps/web/lib/audio/livekit-room-audio.ts`
- `apps/web/components/debate-stage.tsx`

确认的行为：

1. 房间进入时调用 LiveKit `prepareConnection()` 与 `connect()`，浏览器为只订阅端。
2. 仅接收名称为 `agent-tts` 的远端音轨，设置 100ms playout delay。
3. 用户点击“开启比赛声音”后调用 `room.startAudio()` 和 `<audio>.play()`。
4. 收到 `speech.interrupted`、`audio.rtc.interrupt`、`audio.realtime.aborted` 或暂停/终止状态时，立即 detach、pause、清空 `srcObject`，220ms 后才允许重新 attach。
5. LiveKit 连接成功时停用旧的 PCM WebSocket 回退播放器；回退播放器本身使用 AudioWorklet 环形缓冲，初始 100ms，并在 underrun 后自适应提高目标。
6. ASR 使用 AudioWorklet；现有 `MediaRecorder` 只保存赛后录音，不参与实时 ASR 或 Agent 播放。

## 自动门禁文件

- 浏览器注入探针：`scripts/browser/realtime_audio_probe.js`
- 可重复执行器：`scripts/run_browser_realtime_audio_gate.mjs`
- 无文件音频烟测页：`scripts/tests/realtime_audio_probe_fixture.html`
- 探针烟测：`scripts/tests/run_realtime_audio_probe_smoke.mjs`

执行真实房间首声门禁：

```bash
node scripts/run_browser_realtime_audio_gate.mjs \
  --url "https://<host>/rooms/<code>/watch" \
  --mode first-sound \
  --timeout-ms 45000 \
  --output docs/qa/audio/20260718-realtime-voice-rebuild/browser-first-sound.json
```

执行真实房间打断门禁：先运行下列命令；音频已出声后，由受控的另一浏览器/控制端暂停或结束 canary 比赛。脚本自身不会触发生产比赛控制。

```bash
node scripts/run_browser_realtime_audio_gate.mjs \
  --url "https://<host>/rooms/<code>/watch" \
  --mode interrupt \
  --timeout-ms 60000 \
  --output docs/qa/audio/20260718-realtime-voice-rebuild/browser-interrupt.json
```

如果首个正文 delta 时间来自同一服务端的独立指标，可临时传入 ISO 时间：

```bash
--agent-first-delta-at "2026-07-18T04:00:00.123+08:00"
```

正式实现应由 `audio.rtc.started` 携带该值，避免人工拼接。

## Chrome 实测

### 合成连续 PCM 首声

证据：

- `browser-first-sound-gate-fixture.json`
- `browser-first-sound-gate-fixture.png`

结果：

| 指标 | 结果 | 门槛 |
|---|---:|---:|
| Agent 首正文时间 → 服务端首帧 | 299ms | 诊断分段 |
| 服务端首帧 → 浏览器非静音样本 | 221.9ms | 诊断分段 |
| Agent 首正文时间 → 浏览器非静音样本 | 520.9ms | ≤2500ms |
| 播放上下文 | running | 必须 running |
| 结果 | PASS | |

### 合成连续 PCM 打断

证据：

- `browser-interrupt-gate-fixture.json`
- `browser-interrupt-gate-fixture.png`

结果：

| 指标 | 结果 | 门槛 |
|---|---:|---:|
| Agent 首正文时间 → 浏览器非静音样本 | 801.3ms | ≤2500ms |
| interrupt 事件 → pause | 0.3ms | ≤250ms |
| interrupt 事件 → `srcObject` 清空/数字静音 | 14.3ms | ≤250ms |
| 静音后旧 generation 再出声 | 未发现 | 必须为 0 次 |
| 旧音频复现观察窗 | 750ms | 必须完成 |
| 结果 | PASS | |

上述是探针和浏览器媒体图的能力验证，不代表当前生产 TTS 已达到相同首包性能。

### 生产只读观察

证据：

- `browser-production-passive-observation.json`
- `browser-production-passive-observation.png`

房间 `214317` 已因上游服务异常暂停，观察窗口内没有新的 `audio.rtc.started` 或活动音轨。页面实时房间 WebSocket 正常建立，脚本未启动、恢复或改变比赛。结果为：

`BLOCKED_NO_ACTIVE_RTC_TURN`

这不是首声性能失败；必须在受控 canary 的新 AI 发言轮次重新执行。

## 门禁判定语义

| 状态 | 含义 |
|---|---|
| `PASS` | 有活动 RTC 轮次、首正文时间、浏览器非静音样本，且全部阈值通过 |
| `BLOCKED_NO_ACTIVE_RTC_TURN` | 观察期没有 `audio.rtc.started`，不得判为性能失败 |
| `BLOCKED_MISSING_AGENT_DELTA_TIMESTAMP` | 浏览器首声可测，但缺失严格定义的首正文 delta 起点 |
| `FAIL_BROWSER_FIRST_SOUND` | 已收到 RTC 开始事件，但浏览器未检测到解码后的非静音样本 |
| `FAIL_END_TO_END_FIRST_SOUND` | 有完整起止时间，但超过 2500ms |
| `FAIL_INTERRUPT_FLUSH` | 打断前确有声音，但 250ms 内未 flush/静音，或旧音频在静音后复现 |

## 服务端统一计时字段

不新增持久事件、不改变比赛 seq 的最小方案：

1. `IncrementalVoicePipeline` 在收到第一个非空正文 `delta` 时只触发一次诊断回调。
2. `match_engine` 用服务端 UTC 记录 `agent_first_readable_delta_at`。
3. 复用现有 `audio.rtc.started` payload，加上该字段；thinking/reasoning 继续在 Agent provider 层过滤，永不触发此时间戳。
4. 若要消除浏览器与服务端墙钟偏差，再为房间 WS 的诊断 ping 增加可选的 server time 回显，用最小 RTT 样本估算 offset/uncertainty；普通 heartbeat 保持不变。

该最小方案已在本地实现并通过定向回归：

- `IncrementalVoicePipeline` 只在第一个非空正文 delta 触发一次回调；thinking/reasoning 事件不会触发。
- `match_engine` 记录服务端 UTC `agent_first_readable_delta_at`，并把它加入 `audio.rtc.started` / `audio.stream.started` payload。
- `public_event_payload` 已允许该诊断字段下发。
- 定向 API 回归 `21 passed`；最终完整 API 回归 `353 passed`。

该改动尚未部署，因此生产被动观察仍可能返回 `BLOCKED_MISSING_AGENT_DELTA_TIMESTAMP`；下一次受控 canary 发布后即可使用同一探针形成严格端到端门禁。

## Safari 可验证边界

Computer Use 能可靠验证：

- Safari 进入房间并显示实时连接；
- 用户点击后声音按钮从“开启”变为“关闭”；
- 页面没有 LiveKit 初始化/播放错误；
- 暂停/终止后 UI 收到房间事件并停止本轮播放。

Computer Use 无法读取 Safari 内部 WebAudio RMS、`RTCRtpReceiver` 统计或系统扬声器波形，因此不能仅凭按钮状态宣称“2.5 秒内人耳已听见”。Safari 精确门禁应在受控 canary 中执行：

1. AI 轮次前进入 watch 页面并点击“开启比赛声音”。
2. 用 Safari Web Inspector 在页面加载前注入同一探针，或由测试构建显式加载探针。
3. 等待 `audio.rtc.started` 与非静音样本，保存 `report()` JSON。
4. 音频持续可测时由另一控制端暂停；确认 250ms 内 `srcObject` 清空或 RMS 连续低于阈值。
5. 静音后至少观察 750ms；没有新 generation 时不得再次检测到非静音样本。
6. 另外用人工听感确认音色、吞字、爆音和系统音量；该步骤补充数字门禁，不替代它。

在没有 Safari Web Inspector/测试构建注入的情况下，Safari 只能给出连接与播放控制 PASS，精确 audible latency 必须标为 BLOCKED。

## 验证命令

```bash
node --check scripts/browser/realtime_audio_probe.js
node --check scripts/run_browser_realtime_audio_gate.mjs
node --check scripts/tests/run_realtime_audio_probe_smoke.mjs
node scripts/tests/run_realtime_audio_probe_smoke.mjs
```

烟测结果：

```json
{
  "ok": true,
  "first_sound_ms": 899.4,
  "interrupt_to_flush_ms": 1,
  "interrupt_to_silence_ms": 12.2
}
```

## 最终 Web 回归与浏览器门禁（2026-07-18 04:40–04:47 CST）

本轮没有修改生产逻辑或部署。测试期间同一工作区还有其他并行任务运行，因此保留默认并发 Vitest 的超时结果，并使用单文件与单 worker 复验区分稳定断言失败和资源争用。

### Web 单测

| 命令 | 结果 | 耗时 | 说明 |
|---|---|---:|---|
| `npm run test:web` | FAIL：191/192，1 个 5 秒超时 | 61.64s | `lobby/page.test.tsx` 的赛季关闭用例超时；无断言不匹配 |
| `npm exec -- vitest run 'app/rooms/[code]/lobby/page.test.tsx' --reporter=default`（`apps/web`） | PASS：10/10 | 23.08s wall；Vitest 9.84s | 同一失败文件单独复验通过，目标用例 847ms |
| `npm run test:web`（第二次默认并发） | FAIL：180/192，12 个 5 秒超时 | 99.86s | 超时分散在 7 个文件；大量本来低于 1 秒的用例被拖到 5 秒，符合并发资源争用特征 |
| `npm --prefix apps/web run test -- --maxWorkers=1 --reporter=default` | PASS：28 files，192/192 | 20.66s wall；Vitest 17.65s | 最终确定性 Web 单测结果 |

判定：功能回归 PASS；默认并发测试配置在当前多任务机器负载下存在超时敏感性，应作为 CI 稳定性问题保留，不应将两次并发 FAIL 隐去。

### Production build

命令：

```bash
npm run build:web
```

结果：PASS，9.69s。

- Next.js 16.2.10 production build 编译成功（3.4s）。
- TypeScript PASS（4.1s）。
- 12/12 静态页生成完成。
- 动态房间路由 `/control`、`/debate`、`/lobby`、`/result`、`/watch` 均完成构建。

### Chrome synthetic smoke

命令：

```bash
node scripts/tests/run_realtime_audio_probe_smoke.mjs
```

结果：PASS，7.93s。

```json
{
  "ok": true,
  "first_sound_ms": 499.4,
  "interrupt_to_flush_ms": 0,
  "interrupt_to_silence_ms": 15.3
}
```

### Chrome synthetic first-sound gate

命令：

```bash
node scripts/run_browser_realtime_audio_gate.mjs \
  --url "file:///Users/sunshiqi/code/phdebate/v2/scripts/tests/realtime_audio_probe_fixture.html" \
  --mode first-sound \
  --timeout-ms 10000 \
  --output docs/qa/audio/20260718-realtime-voice-rebuild/browser-first-sound-final-20260718.json
```

结果：PASS，3.35s。

| 指标 | 结果 |
|---|---:|
| Agent 首正文时间 → 服务端首帧 | 300ms |
| 服务端首帧 → 浏览器非静音样本 | 146.9ms |
| Agent 首正文时间 → 浏览器非静音样本 | 446.9ms |
| RTC event → 浏览器非静音样本（浏览器单调时钟） | 145.9ms |

证据绝对路径：

- `/Users/sunshiqi/code/phdebate/v2/docs/qa/audio/20260718-realtime-voice-rebuild/browser-first-sound-final-20260718.json`
- `/Users/sunshiqi/code/phdebate/v2/docs/qa/audio/20260718-realtime-voice-rebuild/browser-first-sound-final-20260718.png`

### Chrome synthetic interrupt gate

命令：

```bash
node scripts/run_browser_realtime_audio_gate.mjs \
  --url "file:///Users/sunshiqi/code/phdebate/v2/scripts/tests/realtime_audio_probe_fixture.html" \
  --mode interrupt \
  --local-interrupt-button-name "中断音频" \
  --timeout-ms 10000 \
  --output docs/qa/audio/20260718-realtime-voice-rebuild/browser-interrupt-final-20260718.json
```

结果：PASS，5.47s。

| 指标 | 结果 | 门槛 |
|---|---:|---:|
| Agent 首正文时间 → 浏览器非静音样本 | 554.0ms | ≤2500ms |
| interrupt event → pause | 0.7ms | ≤250ms |
| interrupt event →数字静音 | 13.2ms | ≤250ms |
| 750ms 观察窗内旧 generation 再出声 | 0 次 | 必须为 0 次 |

证据绝对路径：

- `/Users/sunshiqi/code/phdebate/v2/docs/qa/audio/20260718-realtime-voice-rebuild/browser-interrupt-final-20260718.json`
- `/Users/sunshiqi/code/phdebate/v2/docs/qa/audio/20260718-realtime-voice-rebuild/browser-interrupt-final-20260718.png`

### Safari 状态

Safari 精确 audible latency 继续为 BLOCKED：Computer Use 可以验证实时连接、声音解锁按钮、错误状态与暂停后的 UI 变化，但不能读取 Safari 页面内部 WebAudio RMS、解码后非静音样本时间或系统扬声器波形。未使用按钮状态代替“人耳已经听见”的性能证据。

解除 BLOCKED 仍需满足前文任一方案：Safari Web Inspector/测试构建在页面加载前注入同一探针，或使用受控音频回环采集；随后同时保存 `report()` JSON 与人工听感记录。
