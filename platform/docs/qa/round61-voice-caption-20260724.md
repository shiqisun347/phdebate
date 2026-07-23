# Round 61 实时语音与字幕专项审计

日期：2026-07-24  
范围：ASR、实时字幕、MOSS 文本输入/音频输出流、LiveKit 单音轨、打断与跨 worker 权威性。  
说明：本轮只改代码和测试，**未部署生产**。

## 结论

| 项目 | 结论 | 证据或剩余问题 |
| --- | --- | --- |
| ASR 真双向流 | 通过 | 浏览器 AudioWorklet 持续发送 16 kHz mono PCM16；服务端同时执行上行与 FunASR partial/final 下行。Round 59 生产实测 `duplex=true`。 |
| ASR 单一权威连接 | 已修复 | 新增 Redis owner-bound lease；两个 API worker 不再能为同一 `speech_id` 同时连接 FunASR。 |
| 单行电视剧式字幕 | 通过 | 舞台只有一个字幕投影，单行省略；匿名观众无文字稿。组件专项测试 96 项此前已通过。 |
| MOSS 真 text-in + audio-out 流 | 通过 | 一个发言只创建一个 MOSS context，Agent 稳定短语持续写入，同一 WebSocket 并发接收 PCM。不是“完整文本后下载音频”。 |
| 浏览器单一连续音轨 | 通过 | 只订阅 `agent-tts`，AudioWorklet 连续播放；不在一次发言中切换播放器。 |
| 双 worker LiveKit 唯一发布者 | **已修复 P0** | 生产最近 24 小时 43 次 publisher active 中出现 18 次 `DUPLICATE_IDENTITY`。新增房间级 Redis lease、媒体写入 fencing 和 engine affinity。 |
| 暂停/打断清队列 | 通过 | 同时撤销 generation、清应用队列、清 LiveKit SDK queue；浏览器 interrupt gate 丢弃旧 generation。暂停保留同一长寿命音轨，终止才释放 publisher lease。 |
| 人类断线 60 秒暂停 | 通过 | 断线检查在媒体 lease/reconcile 之前执行；不会因非本 worker 持有音轨而推迟。没有 AI 自动接管。 |
| Agent 首字到浏览器声音 ≤3 秒 | **未完全达标** | Round 59 有一次 3.097 秒；近 30 次生产记录仍有 3.646–8.471 秒离群值。不能宣称完全达标。 |
| 连续性与无卡顿 | 风险显著降低，需部署复测 | 本轮消除了能直接踢断正在播放音轨的跨 worker 重复 publisher；仍需生产灰度确认不再出现 `DUPLICATE_IDENTITY` 和中途终止。 |
| MOSS RTF < 1 | 基线通过但余量小 | 已有基线 P50/P95/max 为 0.919/0.979/0.995。 |
| FlashAttention 2 | **未启用** | 生产 MOSS manifest 仍为 `attention_implementation: sdpa`，与此前要求不一致。 |

## 本轮修复

### 1. ASR 跨 worker 唯一连接

- Redis key 使用 `speech_id` 的 SHA-256，避免异常标识形成无界 key。
- claim、refresh、release 都是 Lua 原子操作，并绑定随机 owner。
- 已被其他 worker 占用时在连接 FunASR 前拒绝。
- 生产 Redis 权威不可用时 fail closed，保留人工文字复核，不允许两个 final 竞争写入。

### 2. LiveKit 房间级 publisher lease 与 fencing

生产日志证明同一房间存在两个 `agent-audio:<room>` 发布者。LiveKit 会以 `DUPLICATE_IDENTITY` 关闭旧参与者，这会让浏览器正在播放的唯一音轨突然结束。

修复后的规则：

1. 每个房间只有一个 worker 能原子 claim 30 秒 publisher lease。
   同一 owner 的重复 claim 会原子续租，Redis 短故障恢复后无需等待旧 TTL；不同 owner 仍严格拒绝。
2. owner 每 10 秒刷新；Redis 明确拒绝或权威不可用时，本地立即 fail closed，清音频队列并断开旧 track。
3. 每次媒体写入最多缓存 1 秒 lease 校验；worker 长时间卡死后恢复时，必须先通过 owner-bound refresh，防止过期 owner 继续写 PCM。
4. Match Engine 仅在本 worker 持有 publisher lease 时推进该房间，从而保证 Agent/MOSS 生成和 LiveKit PCM publisher 位于同一进程。
5. owner 崩溃后 Redis TTL 到期，另一个 worker 可自动 claim 并重建 publisher。
6. 暂停只 abort 当前 generation、清空所有未播放样本，保留同一音轨供恢复；终止/房间离开媒体状态时断开 publisher 并 owner-bound release。
7. 人类断线超时检查位于上述 affinity 过滤之前，因此 60 秒自动暂停不依赖 publisher owner。

### 3. WebRTC 遥测修正

此前每轮发言记录了浏览器长寿命 track 的累计 `packetsLost` 和 `concealedSamples`，会把历史损失错误归因到当前发言。本轮改为：

- `packetsReceived`、`packetsLost`、`concealedSamples`：当前采样间隔增量；
- `packetsReceivedTotal`、`packetsLostTotal`、`concealedSamplesTotal`：显式累计值；
- track detach/reconnect 时重置增量基线。

## 生产只读证据

### LiveKit publisher

最近 24 小时：

- 25 个房间，43 次 `agent-audio` RTC session；
- 43 次全部通过 UDP active，近期连接时间约 9.8–14.7 ms；
- 18 次 `DUPLICATE_IDENTITY`，占 publisher 建连数约 42%；
- 没有近期 `PEER_CONNECTION_DISCONNECTED`；
- 因此近期主要问题不是 ICE 建连慢，而是两个 API worker 同时 prewarm 固定身份。

### 延迟

- Agent first delta → TTS first PCM：常见约 1.17–1.40 秒，部分为 1.67–2.24 秒；
- LiveKit capture → browser audible：常见约 0.34–0.77 秒；
- request → browser audible：常见约 1.62–2.13 秒，但仍有 3.646、4.124、4.385、6.573、8.471 秒离群值；
- Round 59 的 first-readable-delta → audible 有 3.097 秒记录，超过 3 秒目标 97 ms。

### 音频落盘

虽然生产配置 `MATCH_AUDIO_ARCHIVE_ENABLED=false`，服务器仍有：

- `storage/audio`：337 个音频文件，约 613.4 MiB；
- `docs/qa`：567 个音频测试附件，约 140.72 MiB。

这与“比赛只保存文字、不保存发言音频”的最终策略不完全一致。当前 provider 仍会创建临时/目标 WAV；需要在后续清理任务中区分固定主持提示音、测试证据和比赛临时音频，并在确认无引用后删除。本文不执行生产删除。

## 测试结果

- LiveKit registry + realtime hub：45 passed。
- 无 AI 接管、60 秒暂停、多房间、完整模拟、LiveKit/ASR 合集：61 passed。
- publisher lease、fencing、暂停/终止、断线专项筛选：34 passed。
- Web 音频与遥测：13 passed。
- Ruff：全部通过。
- 一个既有 `AudioStreamAbortRegistry` 0.1 秒调度测试曾在高并发本机负载下超时，立即隔离复跑通过；与本轮逻辑无关。

新增覆盖：

- 两个 RoomHub 对同一 ASR speech 原子竞争；
- ASR owner-bound refresh/release 与崩溃 TTL 接管；
- 两个 registry 对同一 LiveKit room 原子竞争，只允许一次 SDK connect；
- 过期 owner 在下一次媒体写入前被 fencing 并断开；
- owner 崩溃后另一 worker 接管；
- refresh 暂时不可用时旧 publisher fail closed，Redis 恢复后同 owner 立即重建且不同 owner 仍无法抢占；
- 暂停清 generation 但保留长寿命 publisher；
- 终止断开 track 并释放 owner-bound lease；
- Engine 只返回并调度本 worker 持有的媒体房间；
- 断线 60 秒暂停先于媒体 reconcile。

## 部署后必须执行的验收

1. 两 API worker 同时运行，创建 5 个房间并保持至少 30 分钟。
2. 生产 LiveKit 日志中新增 `DUPLICATE_IDENTITY` 必须为 0。
3. 每个房间只能存在一个 `agent-audio:<room>` 和一个 `agent-tts` publication。
4. 在 TTS 播放中暂停：500 ms 内停止，恢复前不得播放旧 PCM。
5. 强制杀死 publisher owner worker：30 秒 lease 到期后另一 worker接管；比赛应安全暂停或由房主重试当前发言，不能自动切播放器。
6. 人类辩手断网 59 秒不暂停，满 60 秒自动暂停；不改变席位类型、不创建 AI 替补。
7. 对每轮记录 first Agent readable delta、first PCM、first capture、browser audible；P95 必须 ≤3 秒，任何 >3 秒样本都要保留阶段耗时用于定位。
8. 连续完成一场 4v4 和一场 1v1，确认无中途音轨结束、撕裂、重复发言或旧音频复活。

## 尚未完成的事项

- 生产尚未部署本轮修复，不能把代码测试结论当作生产验收结论。
- FlashAttention 2 仍未启用。
- ≤3 秒目标仍有离群值，需要部署后按修正过的 interval telemetry 重新测量。
- 比赛音频临时文件清理策略需单独修复并执行受控清理。
- 本轮误创建的生产测试房间 `346525` 已完整删除，房间、比赛、席位、事件和测试账户均已确认不存在；清理后活跃房间为 3。
