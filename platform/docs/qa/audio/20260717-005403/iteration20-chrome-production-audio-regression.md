# Iteration 20：Chrome 现网音频生命周期回归

执行时间：2026-07-17 18:18–18:26 CST。范围：只读/本地播放状态；未修改比赛、代码或生产配置。

## 环境

- Chrome 150.0.7871.115，用户已有登录会话。
- 生产 release：`20260717T1640-audio-boundary-playback`，流式 feature flag 未启用。
- 测试页面：公开暂停房 `/rooms/278571/watch`、已完成训练赛 `/rooms/566139/result`。
- Chrome 操作结束后已把所用标签恢复到原首页；没有使用 AdsPower/SunBrowser。

## WATCH 声音解锁

1. 打开房间 278571 的 `/watch`。
2. 权威页面状态为“实时连接 / 比赛已暂停 / 正方一辩立论 / 02:52”。
3. 点击唯一的“开启比赛声音”；按钮立即变为 pressed 的“关闭比赛声音”。
4. 因房间暂停且没有当前权威音频，页面没有创建或播放 HTMLAudio；无错误提示。
5. 再次点击恢复“开启比赛声音”，没有触发重试、继续、暂停或提前结束等房间写操作。

证据：[TC-987 截图](../../screenshots/20260717-005403/TC-987-Iteration20-Chrome-现网完整WAV声音解锁.png)。

## 结果页音频互斥

房间 566139 有 9 个带 controls 的音频元素。选择 40.84 秒“反方立论录音”：

- 约 0.9 秒后，该音频 `paused=false`、`currentTime≈0.39s`、`duration=40.84s`。
- 其余 8 条均保持 paused。

随后点击 25.72 秒的下一条“自由辩论录音”：

- 前一条立即变为 `paused=true`，停在约 10.67 秒。
- 新音频 `paused=false`，约 0.53 秒。
- 同一时刻恰好只有一条播放，没有重叠。

证据：[TC-988 截图](../../screenshots/20260717-005403/TC-988-Iteration20-Chrome-结果页音频互斥.png)。

## 离页停止与返回不复活

在第二条音频仍播放时直接导航到赛事大厅：

- 首页 `audioCount=0`。
- 浏览器返回结果页并完成 hydration 后，9 条音频全部 `paused=true`、`currentTime=0`。
- 没有旧音频自动恢复，也没有跨页面继续播放。

证据：[TC-989 截图](../../screenshots/20260717-005403/TC-989-Iteration20-Chrome-离页返回音频不复活.png)。

## 网络与控制台

- 本轮 Chrome 捕获的 console warning/error：0。
- 两个 WAV 的 0–4095 bytes Range GET 均返回 `206 Partial Content`、`Accept-Ranges: bytes`、正确 `Content-Range` 和 `audio/x-wav`。
- 40.84 秒样本总长 1,960,364 bytes；25.72 秒样本总长 1,234,604 bytes，与 24kHz mono PCM16 WAV 量级一致。
- 媒体路由不支持 HEAD（405）但支持浏览器需要的 GET Range；本轮播放、切换和返回均未受影响。
- 18:26 CST `/api/health/ready` 为 `ok=true`：database/schema/Redis/engine/worker/LightTTS/FunASR/storage/backup 正常，LightTTS active=0、queue=0、configured max active=1，`active_match_processing=false`。
- 房间 278571 最终仍为 `paused / remaining 172s / seq 102`；本轮新增 seq 101/102 仅为房主席位在 watch 连接与离开产生的 `presence.connected/disconnected`，没有 control/retry/speech/provider 写事件。

## 结论

生产完整 WAV + HTMLAudio 回退路径在 Chrome 仍通过：用户手势开关状态正确、结果页全局音频互斥、离页停止、返回不复活、Range 播放可用。该结果不能替代未部署 AudioWorklet 流式候选的 live-edge、短尾音、backpressure 和 Safari 发布门；Iteration 20 的两个 P0 仍保持 OPEN。
