# phdebate V2 实时 Agent TTS：WebRTC 下行最小生产架构

- 日期：2026-07-18（Asia/Shanghai）
- 范围：Agent TTS 下行音频；Chrome / Safari；2–3 场并发；每场 8 席并允许观众订阅
- 约束：本报告只做架构与迁移设计，不修改生产、不部署
- 新时延口径：从 LLM 返回首个正文字符开始计时，浏览器必须在 2.5 秒内播放出非静音声音（P95）

## 结论

推荐采用**单节点自托管 LiveKit SFU + 每个比赛房间一个长驻 Agent 音频发布者 + 房间内所有辩手/观众订阅同一条 Opus 音轨**。

正式传输合同必须固定为：

- 文本 delta、比赛状态和 pause/terminate/exit 等控制：允许继续使用持久 WebSocket。
- Agent 正式音频下行：只允许 WebRTC/SRTP + Opus。
- 当前 PCM WebSocket/AudioWorklet：只能作为显式降级或回滚通道，不得再作为正式默认链路。
- 禁止 MediaRecorder + MP3 分片、MSE 音频分片或“短文件轮播”承担实时 Agent 播放。

不推荐把 `aiortc.RTCPeerConnection` 直接塞进 FastAPI，为每个浏览器建立一个服务器发送 PeerConnection。aiortc 可以做快速原型和诊断工具，但它会把 ICE/TURN、重连、每客户端 RTP/RTCP 状态、Opus 编码与生命周期管理全部压到当前 API/Engine 进程；同一段语音面向 8 席和观众时还会形成“每客户端一个发送器/编码路径”。LiveKit 的后端发布者只需把 PCM 注入一次，SFU 再向订阅者转发 RTP，更符合本项目“一场比赛一条权威 Agent 语音，多用户同步收听”的模型。

LiveKit 不是 TTS 推理加速器。总目标能否达到 2.5 秒，首先取决于 OpenMOSS-Realtime-TTS 从首段文字到首个 PCM 的 P95；WebRTC 只负责把“首个 PCM 已出现”到“浏览器真正出声”的部分稳定压在约 0.6 秒以内。

## 当前链路与替换边界

当前 Agent 下行链路是：

```text
Agent delta
  -> IncrementalVoicePipeline（200 ms 软切句）
  -> MOSS/LightTTS 增量会话
  -> 持续写 .wav.part
  -> FastAPI /ws/rooms/{code}/audio 轮询增长中的文件
  -> 每个浏览器接收 PCM16 WebSocket
  -> 浏览器逐客户端重采样
  -> AudioWorklet 自建 ring buffer 播放
```

关键现状：

- `IncrementalVoicePipeline` 已有 200 ms 软切句，适合保留：[/Users/sunshiqi/code/phdebate/platform/apps/api/app/services/realtime_voice.py](/Users/sunshiqi/code/phdebate/platform/apps/api/app/services/realtime_voice.py)
- MOSS 实时后端目前边读 PCM 边写 WAV，并在第一块 PCM 到达时发布 `audio.stream.started`：[/Users/sunshiqi/code/phdebate/platform/apps/api/app/services/providers.py](/Users/sunshiqi/code/phdebate/platform/apps/api/app/services/providers.py)
- 下行 WebSocket 每客户端读取同一增长文件，按 40 ms PCM 帧发送，应用层自己做 replay、flow control 和访问复核：[/Users/sunshiqi/code/phdebate/platform/apps/api/app/api/realtime.py](/Users/sunshiqi/code/phdebate/platform/apps/api/app/api/realtime.py)
- 浏览器为每个客户端维护 3 秒 ring buffer、400 ms 起播阈值和线性重采样：[/Users/sunshiqi/code/phdebate/platform/apps/web/lib/audio/pcm-stream-player.ts](/Users/sunshiqi/code/phdebate/platform/apps/web/lib/audio/pcm-stream-player.ts)、[/Users/sunshiqi/code/phdebate/platform/apps/web/public/worklets/pcm-ring-player.js](/Users/sunshiqi/code/phdebate/platform/apps/web/public/worklets/pcm-ring-player.js)
- 暂停、继续、提前结束、退出页面、放弃席位、结束发言后修改 ASR 文字已经有控制面，不需要因 WebRTC 重做：[/Users/sunshiqi/code/phdebate/platform/apps/api/app/api/rooms.py](/Users/sunshiqi/code/phdebate/platform/apps/api/app/api/rooms.py)、[/Users/sunshiqi/code/phdebate/platform/apps/web/components/debate-stage.tsx](/Users/sunshiqi/code/phdebate/platform/apps/web/components/debate-stage.tsx)

迁移后应变为：

```text
Agent delta
  -> IncrementalVoicePipeline
  -> OpenMOSS-Realtime-TTS（GPU）
  -> PCM Tee：一份写 WAV 归档，一份送 WebRTC sink
  -> 24 kHz -> 48 kHz 有状态流式重采样
  -> 48 kHz / mono / s16 / 20 ms（960 samples）
  -> LiveKit Python AudioSource（一次 Opus 编码/一次发布）
  -> LiveKit SFU（按房间隔离、向全部订阅者转发）
  -> Chrome/Safari 原生 WebRTC jitter buffer + HTMLAudioElement
```

现有 `/ws/rooms/{code}` 继续作为**比赛权威状态和控制事件**通道；只淘汰 Agent 音频的 `/ws/rooms/{code}/audio` 数据面。真人 ASR 上行可以暂时保持现状，因为它不阻塞 Agent 首声 2.5 秒目标；后续若要“所有实时音频统一 WebRTC”，再把麦克风轨订阅到 ASR worker。`MediaRecorder` 只能保留为真人发言的可选归档手段，不能承担实时传输或 MP3 分片播放。

## aiortc 与 LiveKit 对比

| 维度 | FastAPI + aiortc，每客户端 PeerConnection | 自托管 LiveKit，单路发布、多订阅 |
|---|---|---|
| PCM 注入 | 自定义 `AudioStreamTrack.recv()`，自己生成 `av.AudioFrame` | Python SDK `AudioSource.capture_frame()` 直接注入 PCM16 |
| 推荐帧 | aiortc 基类明确使用 20 ms packetization；需自己保证 PTS/time_base | 以 48 kHz 单声道、20 ms、960 samples 推送；SDK负责 WebRTC/Opus |
| 一场 8 席 + 观众 | 每客户端一个 PC、sender、ICE/DTLS/SRTP、RTCP 状态；通常也有独立编码路径 | 后端发布一次，SFU按订阅者转发；编码和发布者状态只有一份 |
| backpressure | 必须自行做有界队列；`MediaRelay(buffered=True)` 的代理队列没有业务级时延上限 | `AudioSource` 有 `queue_size_ms`、`queued_duration`、阻塞式 `capture_frame()`、`clear_queue()` |
| jitter buffer | 浏览器有原生 jitter buffer，但服务端信令、ICE/TURN、重连仍全自建 | 浏览器原生 jitter buffer；SDK已有房间、轨道、ICE restart、resume/full reconnect |
| 房间隔离 | 自己建立 PC registry、授权、清理、跨 worker 路由 | room 是原生隔离单元；JWT可限制 room、只订阅/只发布 |
| 暂停/结束 | 自己停止 track、清 sender、处理每个 PC 的残留队列 | publisher `clear_queue()`；客户端 detach/pause；必要时 unpublish 重置 |
| Safari autoplay | 自己处理 `play()` 拒绝和手势解锁 | 仍需手势，但 JS SDK提供 `canPlaybackAudio`、`AudioPlaybackStatusChanged`、`startAudio()` |
| NAT/TURN | 自己配置 aiortc ICE server、TURN 凭据、失败回退 | LiveKit内建 ICE/UDP、ICE/TCP，可配置 TURN/UDP、TURN/TLS及自动回退 |
| 运行隔离 | API worker承担长连接媒体负载；扩 worker 会放大状态一致性问题 | 独立 SFU；应用 API 只签 token 和做业务控制 |
| 适合本项目 | 只适合本地 PoC、单浏览器诊断、协议实验 | 生产主线 |

aiortc 官方源码把音频 packetization 定义为 20 ms，并要求自定义轨道按 `pts`、`sample_rate`、`time_base` 节奏返回帧；`MediaRelay` 只负责把源帧交给代理轨道，不替代房间、TURN、重连和生产级 SFU。[aiortc `mediastreams.py`（固定 commit）](https://github.com/aiortc/aiortc/blob/8a2864630bf417d977a06dda8d0254b1c1501bfb/src/aiortc/mediastreams.py)、[aiortc `media.py`（固定 commit）](https://github.com/aiortc/aiortc/blob/8a2864630bf417d977a06dda8d0254b1c1501bfb/src/aiortc/contrib/media.py)

LiveKit Python SDK 的 `AudioSource.capture_frame()` 在内部队列装不下整帧时会等待，且提供 `queued_duration`、`clear_queue()`、`wait_for_playout()`，正好覆盖 TTS 下行的背压、硬中断和播完确认。[LiveKit Python `audio_source.py`（固定 commit）](https://github.com/livekit/python-sdks/blob/0acc039f88ac42d82573ba37dc5007440fbdfd2a/livekit-rtc/livekit/rtc/audio_source.py)、[官方后端 PCM 发布说明](https://docs.livekit.io/transport/media/publish/)

## 推荐的最小生产拓扑

```text
                         ┌──────────────────────────────┐
LLM delta ──> MOSS TTS ─┤ RealtimeAudioSink (Engine)   │
                         │  - WAV archive sink          │
                         │  - 48k/20ms WebRTC sink      │
                         └──────────────┬───────────────┘
                                        │ one publisher/room
                                  Opus/SRTP uplink
                                        │
                              ┌─────────▼─────────┐
                              │ self-host LiveKit │
                              │ room debate:CODE  │
                              └──────┬─────┬──────┘
                                     │     │
                                Opus/SRTP  │  ...
                                  downlink │
                           ┌─────────▼──┐ ┌▼──────────┐
                           │ Chrome seat│ │Safari/view│
                           └────────────┘ └───────────┘

FastAPI:
  - 登录态/房间权限校验
  - 签发短 TTL subscribe-only token
  - 保持现有房间状态 WS 和 pause/resume/terminate/exit/ASR transcript API
```

### 房间和参与者约定

- LiveKit room：`debate:{room_code}`，不直接使用可枚举的公共 URL 作为授权依据。
- 浏览器 identity：`user:{user_id}:device:{device_id}`；同一席位多设备仍由现有 device-control lease 决定业务写权限。
- 发布者 identity：`agent-audio:{room_code}`，每场只允许一个活动发布者。
- 轨道：每个房间维持一条长驻 `agent-tts` audio track，避免每轮发言重新建轨和重新订阅造成首声抖动。
- 浏览器 token：`roomJoin=true`、`canSubscribe=true`、`canPublish=false`、`canPublishData=false`，TTL 建议 5 分钟；自托管 LiveKit 不会自动撤销旧 token，因此退出/踢出后 API 不再签发新 token，并保持 TTL 足够短。LiveKit 的 JWT 原生携带 room、identity 和 publish/subscribe 权限。[LiveKit token/grant 官方说明](https://docs.livekit.io/frontends/authentication/tokens/)
- 发布者 token：只允许加入指定房间和发布音频；API secret 只存在服务端环境。

### 发布者放置位置

最小迁移阶段，把 `LiveKitRoomAudioPublisher` 放在现有 **Engine 进程**，不要放进 FastAPI Web worker：

1. Engine 已经拥有 Agent/TTS 会话和取消语义，PCM 不需要再经 Redis 或 HTTP 绕一跳。
2. Engine 已有“重启时把活动 speech 标记 interrupted”的恢复策略，媒体生命周期可以和 speech 生命周期一致。
3. FastAPI 只保留 token endpoint 和业务 WS，避免 API 重启/扩 worker 复制 PeerConnection 或 publisher 状态。

若后续扩大到大量房间，再把 publisher 拆成独立媒体 worker，并通过有界本地 IPC/Redis Streams 接收 PCM；2–3 场阶段不值得增加这一步。

## 音频格式、背压与音色一致性

### 固定格式

- TTS 输入：OpenMOSS-Realtime-TTS 原生输出的单声道 PCM16，通常为 24 kHz（以真实响应头为准）。
- WebRTC sink：固定转换为 **48,000 Hz、mono、signed PCM16、20 ms/帧**。
- 每帧：`960 samples × 1 channel × 2 bytes = 1,920 bytes`。
- 使用一个**跨 chunk 保持相位/历史状态**的流式重采样器；禁止对每个 HTTP chunk 单独初始化重采样，否则会在边界出现爆音、丢样或节奏漂移。
- Python SDK接受 PCM16 `AudioFrame`，WebRTC 层协商 Opus；网页不接触 MP3、MSE 分片或自建 PCM ring buffer。

LiveKit 官方 Python 示例直接以 `AudioSource` 发布 48 kHz PCM；SDK的 `AudioFrame` 明确定义为 interleaved signed int16。[48 kHz 后端发布示例](https://github.com/livekit/python-sdks/blob/0acc039f88ac42d82573ba37dc5007440fbdfd2a/examples/publish_wave.py)、[`AudioFrame` 定义](https://github.com/livekit/python-sdks/blob/0acc039f88ac42d82573ba37dc5007440fbdfd2a/livekit-rtc/livekit/rtc/audio_frame.py)

### 背压

推荐参数：

- `AudioSource(queue_size_ms=100)`；不要使用默认 1000 ms。
- TTS PCM 到 WebRTC sink 之间再放一个最多 6 帧（120 ms）的有界队列。
- 正常路径：`await capture_frame(frame)`，让 SDK队列自然节流；不要无限缓存，也不要为追赶实时进度丢掉语音帧。
- 记录 `queued_duration`；若连续 2 秒高于 150 ms，标为 publisher backlog 告警。
- pause/terminate/abort：取消 TTS读取任务，清应用队列，调用 `AudioSource.clear_queue()`，并发 `audio.rtc.aborted` 权威事件。
- finish：先送完尾帧，再 `wait_for_playout()`；WAV 原子落盘仍作为结果页/归档资产。

### 音色一致性

WebRTC 不决定音色，但能避免客户端重复解码/重采样路径造成的听感差异。音色一致性仍必须由 TTS 层保证：

- 每个 debater 固定 `voice_id -> prompt/reference`，整轮发言只创建一个 OpenMOSS 原生 session。
- 不允许一句话一个独立 TTS request；所有 LLM delta 进入同一个持续上下文。
- WebRTC 发布者每场只保留一条媒体轨，但每轮 TTS session 的 generation、speech_id、voice_id 必须写日志并进入业务事件。
- 浏览器只播放 SFU转发的同一条编码音轨，所有席位听到的是同一个 RTP 源，不再各自重采样原始 24 kHz PCM。

## 2.5 秒首声预算

计时起点改为 `llm_first_text_delta_at`，浏览器终点必须使用“远端音轨出现首个非静音采样”，不能把 WebSocket 收包、track subscribed 或 `play()` Promise resolved 当作已经出声。

| 阶段 | P95 预算 | 门禁 |
|---|---:|---|
| 首字到稳定可送 TTS 的最小文本 | 0.20 s | 保留现有软切句 deadline；强标点可更早 |
| OpenMOSS session 首次文本到首个 PCM | 1.55 s | GPU 推理候选的核心门禁；超过则 WebRTC 无法挽救总目标 |
| PCM 重采样、20 ms framing、publisher queue | 0.15 s | queue P95 ≤100 ms；不得先攒 400 ms |
| LiveKit publisher -> SFU -> Chrome/Safari jitter buffer -> 非静音出声 | 0.50 s | 正常网络 P95；Safari 单列统计 |
| 余量 | 0.10 s | 调度、JS事件、时钟误差 |
| **总计** | **2.50 s** | 任何阶段超预算都不得灰度放量 |

WebRTC 路径的目标是把当前浏览器端 400 ms 人工起播阈值和每客户端 PCM 重采样去掉，交给浏览器原生 jitter buffer。调参只能作为 feature-detected hint，最终必须用 WebRTC stats 和真实网络验收实际结果。

更新后的具体策略是：以 100 ms 为浏览器初始 playout/jitter 目标，并在 80–120 ms 正常区间内自适应；只在真实丢包/隐藏采样持续发生时临时进入最高 200 ms 的保护区。`RTCRtpReceiver.jitterBufferTarget` 只会影响而非精确强制浏览器的实际目标，因此必须 feature-detect，并继续以 stats 的实际平均 delay 验证。LiveKit 当前 JS client还提供 `RemoteTrack.setPlayoutDelay(seconds)`，内部使用浏览器支持的 `playoutDelayHint`，可作为兼容入口。[W3C/MDN `jitterBufferTarget` 语义](https://developer.mozilla.org/en-US/docs/Web/API/RTCRtpReceiver/jitterBufferTarget)、[LiveKit `RemoteTrack.setPlayoutDelay`（固定 commit）](https://github.com/livekit/client-sdk-js/blob/ccb993956b8968106759792ce22bbb3956503ba0/src/room/track/RemoteTrack.ts)

客户端必须采集：

- `track_subscribed_at`
- `audio_element_play_requested_at`
- 首个非静音采样时间（Web Audio analyser 仅用于测量，不接管播放）
- `inbound-rtp`: `packetsLost`、`jitter`、`jitterBufferDelay / jitterBufferEmittedCount`、`concealedSamples`、`totalSamplesReceived`
- `RoomEvent.Reconnecting/Reconnected` 和音频恢复时间

服务端必须采集：

- `llm_first_text_delta_at`
- `tts_first_text_at`
- `tts_first_pcm_at`
- `rtc_first_capture_accepted_at`
- `AudioSource.queued_duration`
- room、speech、generation、voice、track SID、订阅者数

## 连续媒体时钟与 80–120 ms 自适应 jitter 策略

### 服务端连续时钟

每个比赛房间的 `agent-tts` 必须是长驻轨，不得每句或每轮重新 publish。publisher启动后以固定 **48 kHz / 20 ms / 960 samples** 节奏持续向 `AudioSource` 提交帧：

- 有 TTS PCM 时提交语音帧。
- 无 TTS PCM 时提交全零静音帧。
- pause/interrupt 后清掉旧语音，但静音时钟继续运行。
- RTP timestamp由 WebRTC栈在同一条轨上连续推进；应用层不能因 speech generation变化重置媒体时钟。

这样做有三个目的：

1. 浏览器在比赛进行期间一直保持已订阅、已解锁、已热身的 Opus decoder和 jitter buffer，首段真实语音不再支付“新轨订阅 + 新播放器起播”成本。
2. 每轮语音之间不会因重建轨道造成 Safari autoplay再次阻塞。
3. interrupt后可以立即以静音覆盖实时边缘，并在浏览器 flush完成后安全恢复。

publisher task必须是独立的高优先级 asyncio task，不在数据库事务、Agent迭代或文件 I/O循环中 sleep。建议启动时先允许 `AudioSource` 填入 5 帧静音（100 ms），随后依赖 `capture_frame()` 的阻塞背压维持节奏；应用 PCM队列仍限制为 6 帧。若事件循环一次落后超过 40 ms，记录 `publisher_clock_late`；不能通过瞬间提交大量过期语音帧来追钟。

静音帧会由 Opus高效编码；若所固定的 LiveKit SDK/浏览器组合暴露 DTX 设置，可以在 canary中开启，但连续时钟和正确性不能依赖 DTX 是否可用。

### 浏览器初始目标和自适应

每次 `TrackSubscribed` 后：

1. 初始 `target_ms = 100`。
2. 若 `RemoteTrack.setPlayoutDelay` 可用，调用 `track.setPlayoutDelay(0.1)`。
3. 若应用能安全取得标准 `RTCRtpReceiver` 且存在 `jitterBufferTarget`，设置为 `100` ms；该值只是 hint。
4. 每 1 秒读取一次 `getRTCStatsReport()`，以相邻采样差值计算本周期指标。

自适应规则：

```text
normal_min = 80 ms
normal_max = 120 ms
emergency_max = 200 ms

如果本周期 concealedSamples 增加、concealmentEvents 增加、packetsLost 增加，
或 audio element 触发 waiting/stalled：
    target += 20 ms
    正常最多 120 ms；连续 3 个异常周期可进入保护区，最多 200 ms

如果连续 15 秒：
    无新增 concealedSamples / packetsLost
    且 RTP jitter < 15 ms
    且实际平均 jitter-buffer delay稳定：
        target -= 10 ms，最低 80 ms

每次变化后至少保持 5 秒，避免振荡。
```

实际平均 jitter buffer delay用 stats 增量计算：

```text
delta(jitterBufferDelay) / delta(jitterBufferEmittedCount)
```

同时记录 `jitterBufferTargetDelay` 的增量（浏览器提供时），区分“应用请求的目标”和“浏览器/网络实际采用的目标”。Safari 与 Chrome必须分别出报表；不支持 hint 的浏览器保持原生自适应，但仍执行 stats告警和 Computer Use听感门禁。

### underrun处理

- 浏览器不再有应用自建 ring buffer，因此没有旧 worklet 的 `underrun` 回调。
- 正式 underrun信号来自 `concealedSamples`/`concealmentEvents` 增量、`waiting/stalled` 事件和首声/静音 gap探针。
- 单次 underrun：提高目标 20 ms，不重建轨。
- 连续 3 次 underrun：进入 160–200 ms保护区，并显示“网络波动，语音缓冲调整中”，但比赛状态仍由业务 WS权威决定。
- 持续 10 秒仍恶化：触发 LiveKit重连诊断；不能自动切到 PCM WebSocket，除非房间被明确置为降级模式。

## interrupt / flush 控制协议

音频媒体走 WebRTC，但 generation、interrupt和 flush确认走现有持久业务 WebSocket。建议新增以下 transport-neutral 消息：

```json
{
  "type": "audio.rtc.started",
  "room_code": "123456",
  "speech_id": "...",
  "generation": "32-hex",
  "track_sid": "TR_...",
  "voice_id": "debate_voice_1",
  "sample_rate": 48000,
  "frame_ms": 20,
  "server_first_capture_at": "ISO-8601"
}
```

```json
{
  "type": "audio.rtc.interrupt",
  "interrupt_id": "uuid",
  "speech_id": "...",
  "generation": "32-hex",
  "reason": "manual_pause|terminate|retry|stage_changed|provider_failed",
  "flush_guard_ms": 220,
  "server_sent_at": "ISO-8601"
}
```

```json
{
  "type": "audio.rtc.resume",
  "interrupt_id": "uuid",
  "generation": "new-32-hex",
  "resume_not_before": "ISO-8601"
}
```

可选客户端观测 ACK：

```json
{
  "type": "audio.rtc.flush_ack",
  "interrupt_id": "uuid",
  "generation": "old-32-hex",
  "client_flushed_at_ms": 123456.7
}
```

ACK仅用于指标和问题定位，服务端不能等待所有观众 ACK后才暂停比赛。

服务端收到/产生 interrupt 时必须按以下顺序执行：

1. 原子地把旧 generation 标为不可继续写入。
2. 取消 TTS上游读取和当前 `write_pcm`。
3. 清空应用 6 帧 PCM队列。
4. 丢弃重采样器和 framer中属于旧 generation的尾样本，防止旧尾音进入新发言。
5. 调用 `AudioSource.clear_queue()` 清除最多约 100 ms 的尚未发送音频。
6. publisher连续时钟立即改送静音帧。
7. 发布 `audio.rtc.interrupt` 和权威 room projection。
8. pause/terminate不自动恢复；retry/stage切换最早在 220 ms flush guard后发 `audio.rtc.resume` 并允许新 generation PCM进入。

浏览器收到 `audio.rtc.interrupt` 时必须：

1. 校验 speech/generation；重复 interrupt幂等。
2. 立即 `audio.pause()`。
3. `track.detach(audioElement)`，并把 `audioElement.srcObject = null`；必要时 `audioElement.load()` 清理 element级 playout队列。
4. 标记该 generation为 revoked，迟到的 `started`/业务事件不得重新 attach。
5. 发送可选 `flush_ack`。
6. 只有收到匹配 interrupt_id 的 `audio.rtc.resume`、达到 `resume_not_before` 且 room仍为 running时，才重新 attach同一条长驻 track。

detach负责“立即停止用户耳朵听到的声音”；服务端 `clear_queue()` 和 220 ms静音 guard负责消耗/覆盖已经进入网络和浏览器原生 jitter buffer的旧音频。两端必须同时做，单独任一端都不能构成可靠 interrupt。

terminate时不发 resume：客户端保持 detach，服务端 unpublish并断开 publisher。普通 speech自然结束时不做 flush，不重建轨，只让连续时钟从语音回到静音。

## 暂停、修改文字、结束、退出的语义

### 暂停比赛

1. 现有 control API 先提交权威 room 状态为 `paused` 并中断正在运行的 Agent/TTS job。
2. Engine 立即清空 WebRTC source queue。
3. 房间状态 WS 广播 `audio.rtc.aborted` 和新 room projection。
4. 浏览器收到 `paused` 后立即 `track.detach()` 或 pause 对应 audio element，目标是 250 ms 内停止可闻声音。
5. 继续比赛时不回放旧缓冲；由现有 retry/resume 规则生成新的 speech generation。

只调用服务端 `clear_queue()` 不足以保证浏览器马上安静，因为已经到达远端 jitter buffer 的包可能仍会播放；必须同时由权威状态驱动客户端停止本地 playout。

### 修改 ASR 文字

现有 UI 已有“结束发言并修改文字”和提交前 textarea，继续走原有 `/speech/finish` 与 transcript 同步逻辑。WebRTC Agent 下行不改变 ASR transcript 的权威来源。若将来把真人麦克风也迁到 LiveKit，仍应让 ASR final + 用户确认后的文本入库，不能让音轨元数据覆盖 transcript。

### 提前结束比赛

- 终止 Agent/TTS；`clear_queue()`；客户端 detach；unpublish `agent-tts`；publisher离开 LiveKit room。
- 现有 `terminated` 状态和赛果规则保持不变。
- LiveKit room 可以随后通过 RoomService 删除；删除房间会断开其中所有参与者。[LiveKit room 删除语义](https://docs.livekit.io/intro/basics/rooms-participants-tracks/rooms/)

### 退出比赛页面

- 只断开该浏览器的 LiveKit room 和业务 WS，不影响比赛、席位和后端 publisher。
- “放弃本场并由 AI 接替”仍走现有业务 API；不能把 WebRTC disconnect 等同于放弃席位。
- 重新进入时签发新短 TTL token，订阅当前长驻音轨，从实时边缘继续听，不回放离线期间的 Agent 音频。

## Chrome / Safari autoplay 与断线重连

Safari 和 Chrome 都可能拒绝脚本自动播放。LiveKit JS SDK明确要求在需要手势时，从 click/tap handler 调用 `room.startAudio()`；同一会话解锁后，后续音轨一般无需再次手势。应把现有“开启声音/点击播放”按钮直接接到这一 API，并监听 `AudioPlaybackStatusChanged`，不要假定 `track.attach()` 必然出声。[LiveKit JS autoplay 处理（固定 commit）](https://github.com/livekit/client-sdk-js/blob/ccb993956b8968106759792ce22bbb3956503ba0/README.md#browser-specifics)、[`HTMLMediaElement.play()` 拒绝语义](https://developer.mozilla.org/en-US/docs/Web/API/HTMLMediaElement/play)

客户端策略：

- 用户进入比赛页后尽早建 room，但声音按钮必须保留明确手势。
- `TrackSubscribed` 时 attach 到唯一隐藏/可访问的 `<audio playsInline>` 元素；不要为每个 speech 新建元素。
- 进入后台/再回前台时检查 `canPlaybackAudio`；Safari需要时再次显示“点击播放”。
- `Reconnecting` 显示“实时音频重连中”，暂停本地播放 UI；`Reconnected` 后等待 SDK自动重新订阅/恢复发布。
- 重连期间不做 WAV replay，避免听到已经过期的辩论内容。

LiveKit 官方说明会先尝试 signaling resume + ICE restart，失败时才 full reconnect，并提供 `Reconnecting`/`Reconnected` 事件；这比应用自己在 FastAPI 里实现每个 PeerConnection 的恢复状态机风险低。[连接与重连官方说明](https://docs.livekit.io/transport/connect/)

## 并发、CPU 与带宽

目标负载按以下两个档位验收：

1. 基线：3 场 ×（8 席 + 1 观众）= 27 个订阅者，3 个房间 publisher。
2. 放量门禁：3 场 ×（8 席 + 4 观众）= 36 个订阅者，3 个房间 publisher；另做每场 32 人突发加入。

若把 Opus 音频 payload 上限配置为 32 kbps，并按含 RTP/UDP/DTLS 开销的 60 kbps/订阅者做容量预算：

- 27 个订阅者约 1.62 Mbps SFU 下行预算。
- 36 个订阅者约 2.16 Mbps。
- 96 个订阅者（3 × 32）约 5.76 Mbps。
- TURN relay 最坏场景再预留约 2 倍网络余量。

这些数字只是规划上限，不是实测值。LiveKit 官方指出 SFU 负载主要由发布轨数、订阅者数和向每个订阅者发送的数据量决定，并提供 `lk load-test` 做实机测试；官方 16 核 audio-only benchmark 的规模远高于本项目，但仍必须在目标服务器上用本项目 bitrate 与 TURN 比例复测。[LiveKit benchmark](https://docs.livekit.io/transport/self-hosting/benchmark/)

建议初始将 LiveKit 与 GPU TTS 分离：

- GPU 机只做 OpenMOSS 推理和 PCM 输出。
- LiveKit 放 CPU/网络稳定的独立实例，至少 2 vCPU / 4 GB RAM 起步，最终规格以 36/96 订阅者实测 CPU、RSS、packet loss 为准。
- 不把 SFU 和当前单 worker FastAPI 绑在同一进程或同一个 Python event loop。

## 部署端口、TLS、Nginx 与 Redis

### 推荐单节点端口

- `7880/tcp`：LiveKit API/signaling，仅绑定内网或 localhost，由 `rtc.<domain>` 的 Nginx/负载均衡终止 TLS，外部使用 `wss://rtc.<domain>`。
- `7882/udp`：ICE/UDP mux，公网直达 LiveKit；小规模单节点比开放 50000–60000 端口范围简单。
- `7881/tcp`：ICE/TCP fallback，公网直达 LiveKit，不能作为普通 HTTP location 代理。
- `3478/udp`：可选 embedded TURN/UDP。
- `443/tcp`：推荐独立 `turn.<domain>`/独立公网 IP 提供 TURN/TLS；当前主站 Nginx 已占用同一 IP 的 443，不能简单再让 LiveKit绑定同一 socket。

LiveKit 官方端口表说明 7880 应放在 TLS 终止层之后，7881 是 ICE/TCP，7882 可作为 UDP mux；TURN/TLS、TURN/UDP需要另外开放。[端口和防火墙](https://docs.livekit.io/transport/self-hosting/ports-firewall/)

生产必须使用受信任 CA 的证书，自签证书不满足官方部署要求；Docker 部署时官方建议 host networking 以获得更好性能。[部署和 TLS](https://docs.livekit.io/transport/self-hosting/deployment/)

### Nginx

现有主站 Nginx可增加独立 `server_name rtc.<domain>`，只代理 signaling/API 到 `127.0.0.1:7880` 并保留 WebSocket upgrade。媒体 UDP/TCP 不经过当前 HTTP Nginx location。

不要把 LiveKit signaling 混到 `/ws/rooms/...` 的业务路径下；独立域名便于 CSP、证书、日志、限流和回滚。

### Redis

当前应用 Redis 为 `127.0.0.1:6380`、512 MB、`allkeys-lru`：[/Users/sunshiqi/code/phdebate/platform/deploy/redis.conf](/Users/sunshiqi/code/phdebate/platform/deploy/redis.conf)

单节点 LiveKit阶段**不要配置 LiveKit Redis**。LiveKit config sample 明确说明一旦设置 Redis，服务会自动进入分布式模式；本项目 2–3 场没有这个需求。[LiveKit config sample（固定 commit）](https://github.com/livekit/livekit/blob/28931e2f842762b431714c84a6d368e96fb85085/config-sample.yaml)

现有 Redis继续服务房间状态广播和应用协调。若未来上多个 LiveKit node，应使用独立 Redis 实例或至少独立 DB、独立内存/淘汰策略，不能让媒体路由状态与当前 `allkeys-lru` 应用缓存竞争。

## 默认关闭的最小代码迁移建议

以下是建议改动，不在本报告中实施：

### 后端

1. `apps/api/app/core/config.py`
   - `webrtc_audio_enabled: bool = False`
   - `webrtc_audio_backend: Literal["websocket_pcm", "livekit"] = "websocket_pcm"`
   - `livekit_url`、`livekit_api_key`、`livekit_api_secret`
   - `livekit_audio_sample_rate=48000`
   - `livekit_audio_frame_ms=20`
   - `livekit_audio_queue_ms=100`
   - `livekit_token_ttl_seconds=300`

2. 新增 `apps/api/app/services/realtime_audio_sink.py`
   - `RealtimeAudioSink.start(room_code, speech_id, generation, sample_rate)`
   - `write_pcm(pcm16)`
   - `finish()`
   - `abort()`
   - `TeeAudioSink(WavArchiveSink, LiveKitAudioSink)`

3. 新增 `apps/api/app/services/livekit_audio.py`
   - 每房间一个长驻 `Room`、`AudioSource`、`LocalAudioTrack`
   - 有状态 24k→48k 重采样器
   - 20 ms framer，尾帧零填充只用于媒体发送；WAV 归档不写填充样本
   - 100 ms source queue + 120 ms应用有界队列
   - `clear_queue()` 和 room/publisher 生命周期

4. `apps/api/app/services/providers.py`
   - MOSS `_read_audio()` 不再只写 growing WAV；把每个验证后的 PCM chunk 同时写入 sink。
   - 保留最终 WAV 原子发布和现有 TTS failure语义。

5. `apps/api/app/services/match_engine.py`
   - `audio.stream.started/aborted` 扩展为 transport-neutral 的 `audio.realtime.started/aborted`，payload包含 `transport=livekit`、track SID、generation。
   - pause/terminate/retry 调用 sink abort。

6. 新增 `POST /api/rooms/{code}/rtc-token`
   - 复用 `can_view_room`；短 TTL；只订阅 token；响应仅含 `url/token/room_name`。
   - 不把 API secret 或 publisher token 下发浏览器。

7. Engine shutdown
   - 逐房间 `clear_queue`、unpublish、disconnect；有上限的 drain，不阻塞无限期。

### 前端

1. 新增 `apps/web/lib/audio/livekit-room-audio.ts`
   - 连接一次 room，订阅 `agent-tts`，attach 唯一 audio element。
   - `unlock()` 在声音按钮 click 中调用 `room.startAudio()`。
   - 暂停/结束时 detach/pause；重连事件映射到现有 UI。
   - 采集首个非静音与 WebRTC stats。

2. `apps/web/components/debate-stage.tsx`
   - feature flag 为 LiveKit 时不再创建 `PcmStreamPlayer`。
   - 保留当前声音按钮、播放手势提示、pause/terminate/leave 控制。

3. 灰度期间保留旧文件
   - `pcm-stream-player.ts`、worklet、`/ws/rooms/{code}/audio` 暂不删除；只在 `webrtc_audio_backend=websocket_pcm` 时使用。
   - LiveKit通过全部门禁后再删除旧 PCM transport。

### 依赖

- API/Engine：固定版本的 `livekit` Python packages（`livekit-rtc`、`livekit-api`，按官方当前拆包确认）。
- Web：固定版本 `livekit-client`。
- 依赖必须写死版本并保留 license/SBOM；不要追 `latest` 自动升级。

## 测试矩阵与放量门禁

### 单元测试

| 项目 | 必测断言 |
|---|---|
| 流式重采样 | 任意 chunk 边界下总样本数误差 ≤1 frame；边界无不连续脉冲 |
| 20 ms framer | 每个非尾帧恰好 960 samples / 1920 bytes；奇数字节立即失败 |
| backpressure | source queue 100 ms + app queue 120 ms；生产者被 await 阻塞，不丢帧、不无限增长 |
| abort | 清应用队列、`clear_queue()`、旧 generation 不再进入 source |
| room registry | 同房间最多一个 publisher；不同房间 PCM 绝不串流 |
| token | 浏览器不能 publish；只能加入授权 room；匿名/无观看权限拒绝 |
| control | pause/terminate/leave 与现有业务状态一致，重复请求幂等 |
| autoplay | `startAudio()` 只由手势触发；blocked 状态显示“点击播放” |

### 集成测试（真实 LiveKit，不用 mock 代替）

1. 1 room / 1 publisher / 8 subscribers，连续 10 分钟 TTS/tone，检查 gap、丢包、RSS。
2. 3 rooms / 3 publishers / 36 subscribers，比赛语音交错开始，确认无串房。
3. 3 rooms / 每房 32 subscribers 突发加入，验证 CPU、带宽、连接成功率、首声。
4. 生产者以快于实时速度推 PCM，验证 `capture_frame` 背压和内存上限。
5. pause 在句中发生：所有 Chrome/Safari 250 ms 内停止；恢复后不播放旧缓冲。
6. terminate、仅退出页面、放弃席位分别验证，不能互相混淆。
7. Wi-Fi断开/恢复、网络切换、10% packet loss、100 ms RTT，验证 resume/ICE restart/full reconnect。
8. publisher/Engine 重启：当前 speech 安全 interrupted，房间状态不假完成，旧音频不复活。
9. 同一声纹至少连续 20 轮，做 speaker embedding 相似度和人工 AB；WebRTC 解码录音与服务端 WAV 对齐。
10. 真人“结束发言并修改文字”不受下行 WebRTC影响，提交文本和录音归档仍一致。

### Chrome / Safari Computer Use 门禁

- 浏览器：最新版 Chrome；当前 macOS Safari；至少一台 iPhone/iPad Safari（若正式支持移动端）。
- 每种浏览器 30 次冷进入 + 30 次已解锁会话。
- 计时：`llm_first_text_delta_at -> 首个非静音采样`。
- 总体和各浏览器分别计算 P50/P95/max，不能只看平均值。
- 首声总时延：P95 ≤2.5 s。
- `tts_first_pcm -> 首个非静音采样`：P95 ≤0.6 s。
- 正常网络连续音频：无 >120 ms 非预期静音 gap；无重复、倒序、截尾。
- pause/terminate：P95 ≤250 ms 停止可闻声音。
- 3 场并发 36 订阅者：连接成功率 100%，零串房，SFU CPU持续 <60%，无持续增长 RSS。
- Safari autoplay blocked 必须显示可操作按钮；点击一次后本会话后续 speech 可播放。
- 重连后只接实时边缘，不 replay 已过期的辩论音频。

### NO-GO 条件

任何一项发生都不得启用默认开关：

- 首声 P95 >2.5 s，或 Safari 单独 P95 >2.5 s。
- 3 场并发时任一房间串音、断音、声纹漂移或 TTS队列串行化。
- pause/terminate 后仍播放超过 500 ms。
- 浏览器必须依赖 MediaRecorder/MP3/MSE 分片才能播放 Agent实时语音。
- publisher queue、Engine RSS 或 SFU RSS 随轮次单调增长。
- 无可信 TLS、无 UDP/TCP fallback、Safari只能在开发环境工作。

## 分阶段迁移

1. **本地默认关闭**：实现 sink抽象、LiveKit publisher、token endpoint、前端 client；在门禁通过前不改变当前生产行为。进入任何“正式音频”灰度的房间后只能使用 WebRTC，旧 PCM WebSocket只能由显式降级/回滚开关启用。
2. **合成音 canary**：不接 TTS，用固定音调/已知 PCM 跑 Chrome/Safari 和 3-room负载，先证明 WebRTC数据面。
3. **OpenMOSS canary**：接单场一条真实 TTS；验证 48k重采样、首声、音色和 abort。
4. **影子发布**：旧 PCM WebSocket仍给用户播放，同时把同一 PCM 发布到不可见 LiveKit测试订阅者，比较 WAV/解码波形和指标。
5. **低流量灰度**：仅 QA room或显式房间 flag 使用 LiveKit；不做全局环境变量一刀切。
6. **3场正式门禁**：Chrome/Safari Computer Use + 36/96订阅负载通过后，再把 LiveKit设为默认。
7. **回滚观察期**：至少保留旧 PCM transport一个发布周期；出现问题按房间切回，不需要回滚 TTS核心代码。

## 官方证据版本

- aiortc commit：[`8a2864630bf417d977a06dda8d0254b1c1501bfb`](https://github.com/aiortc/aiortc/commit/8a2864630bf417d977a06dda8d0254b1c1501bfb)
- LiveKit Python SDK commit：[`0acc039f88ac42d82573ba37dc5007440fbdfd2a`](https://github.com/livekit/python-sdks/commit/0acc039f88ac42d82573ba37dc5007440fbdfd2a)
- LiveKit server commit：[`28931e2f842762b431714c84a6d368e96fb85085`](https://github.com/livekit/livekit/commit/28931e2f842762b431714c84a6d368e96fb85085)
- LiveKit JS client commit：[`ccb993956b8968106759792ce22bbb3956503ba0`](https://github.com/livekit/client-sdk-js/commit/ccb993956b8968106759792ce22bbb3956503ba0)

## 最终推荐

生产主线选 **LiveKit**，aiortc只保留为协议/诊断 PoC。最小实施顺序应是：先把现有 MOSS PCM 读取重构成 transport-neutral sink，在 Engine 中发布一条每房间长驻的 48 kHz/20 ms LiveKit音轨，再把浏览器播放从自建 PCM WebSocket/AudioWorklet切换为 LiveKit remote track。保留现有比赛状态 WS 和控制 API，由它们驱动 pause/terminate/exit 和本地 audio detach；WebRTC只负责媒体，不成为比赛状态的第二权威来源。
