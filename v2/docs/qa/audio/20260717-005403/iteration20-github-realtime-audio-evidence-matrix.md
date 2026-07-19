# Iteration 20：实时 TTS、WebSocket 与浏览器播放一手实现证据矩阵

更新时间：2026-07-17 18:27 CST。性质：只读研究；未修改代码、未部署。

## 核心结论

成熟实时语音链路普遍具备以下不变量：

1. 每一轮音频都有完整的 generation/context/speech identity，旧连接、旧帧和旧回调可被确定性丢弃。
2. `start`、`final`、`abort/clear` 是显式控制消息；连接关闭、文件出现或 producer 停止不能代替生命周期事件。
3. “服务器已发送”“客户端已缓冲”“用户已听到”是三个不同状态；取消、计时和断线恢复至少要知道 played PTS。
4. 标准浏览器 WebSocket 没有入站 backpressure；必须在服务端 pacing，并在客户端使用有界队列、高低水位、overflow/underrun 策略。
5. fresh join/reconnect 的 live position 由服务端根据真实数据面决定，不能让浏览器依据墙上时间猜 growing file offset。
6. EOF 时任何非零尾包都必须播放，即使不足常规 prime 水位。

## 证据矩阵

| 来源 | 一手证据 | 对本项目的直接启示 |
|---|---|---|
| LightTTS | 官方支持 HTTP streaming、WebSocket bi-stream、TTFT/RTF benchmark；`test_bistream.py` 的公开并发数为 1。[仓库](https://github.com/ModelTC/LightTTS)、[bi-stream 测试](https://github.com/ModelTC/LightTTS/blob/ad1c76e36614a1aa26629bc11246884bb7c4072c/test/test_bistream.py#L23-L100) | 协议能力存在，但官方 bi-stream 示例不能证明并发容量。 |
| LightTTS 调度 | LLM 层有 continuous batching；`running_max_req_size` 默认 30，但 Token2Wav `decode_max_batch_size` 当前只支持 1，TRT concurrent 也为 1。[CLI](https://github.com/ModelTC/LightTTS/blob/ad1c76e36614a1aa26629bc11246884bb7c4072c/light_tts/server/api_cli.py#L39-L67)、[decode manager](https://github.com/ModelTC/LightTTS/blob/ad1c76e36614a1aa26629bc11246884bb7c4072c/light_tts/server/tts_decode/manager.py#L45-L89)、[TRT RPC](https://github.com/ModelTC/LightTTS/blob/ad1c76e36614a1aa26629bc11246884bb7c4072c/light_tts/server/tts_decode/model_infer/model_rpc.py#L38-L54) | 调高应用 active 或 LLM request 数不能自动解除声学解码串行瓶颈。 |
| LightTTS benchmark | 4090D HTTP stream 并发 2 的 TTFT P90/P99 约 1.53/2.30s、RTF P90/P99 0.63/0.81；并发 4 退化到 4.37/5.80s、1.28/2.16。[官方数据](https://github.com/ModelTC/LightTTS#nvidia-geforce-rtx-4090d) | “请求成功”不等于多路实时；即使 4090D 也不能满足当前 0.9s TTFT P95 目标。 |
| LightTTS 长文本/并发 issue | 维护者和用户记录 bi-stream 累计上下文变长后的退化及高并发卡死。[长上下文](https://github.com/ModelTC/LightTTS/issues/8)、[并发问题](https://github.com/ModelTC/LightTTS/issues/3) | 必须覆盖 100/300 字、持续比赛时长和高并发取消，不能只测短句。 |
| CosyVoice Triton | 官方 Triton/TRT-LLM runtime 与 LLM/Token2Wav 分离式部署展示动态批处理和多 GPU 隔离路径。[单 GPU](https://github.com/FunAudioLLM/CosyVoice/blob/main/runtime/triton_trtllm/README.md)、[分离式](https://github.com/FunAudioLLM/CosyVoice/blob/main/runtime/triton_trtllm/README.DIT.md) | 要稳定支持 2–3 场同时实时，长期方向是 batched Token2Wav/Triton 或多 GPU，而不是单纯增大 semaphore。 |
| Deepgram TTS | 每 turn 有 `speech_id`；Started/Metadata 包围该 turn，Clear 后有 Cleared 确认。[生命周期](https://developers.deepgram.com/docs/flux-tts/state)、[Clear](https://developers.deepgram.com/docs/tts-ws-clear) | `audio.start/final/abort` 必须是权威事件；abort 最好有确认。 |
| Cartesia WS | `context_id` 隔离顺序，chunk 带 step time；cancel 只保证未开始请求，已生成请求可能继续。[Contexts](https://docs.cartesia.ai/use-the-api/tts-websocket/contexts)、[WS API](https://docs.cartesia.ai/api-reference/tts/websocket) | 关闭 LightTTS WS 不必然等于 GPU 已停止；释放 admission 前需要 ACK、EOF 或安全 drain 证据。 |
| ElevenLabs | 输出有 `isFinal`/alignment；建议 turn 末显式 flush；多 context 用 event/context identity 丢弃旧响应。[TTS WS](https://elevenlabs.io/docs/api-reference/text-to-speech/v-1-text-to-speech-voice-id-stream-input)、[Speech Engine](https://elevenlabs.io/docs/api-reference/speech-engine/speech-engine-upstream)、[Multi-context](https://elevenlabs.io/docs/eleven-api/guides/how-to/websockets/multi-context-web-socket) | 使用完整 generation；final 不能只是 socket close 的副作用。 |
| OpenAI Realtime 参考客户端 | 中断使用 listener 已听到的 sampleCount，同时 cancel response 并 truncate 已播放 item。[client.js](https://github.com/openai/openai-realtime-api-beta/blob/main/lib/client.js#L2641-L2705) | abort、计时和 reconnect 应依据 played PTS，而不是 wall-clock 或 last-sent PTS。 |
| LiveKit AudioSource | 有毫秒级队列上限；满队列时 producer 等待；暴露 queued duration、clear queue 和 wait for playout。[audio_source.py](https://github.com/livekit/python-sdks/blob/main/livekit-rtc/livekit/rtc/audio_source.py) | final（不再产生）与 drained（用户听完）必须区分；abort 必须清浏览器 ring。 |
| Twilio Media Streams | 音频按顺序缓冲，`clear` 清未播内容，`mark` 回执确认此前内容已播；官方有 overflow/pacing 指南。[消息](https://www.twilio.com/docs/voice/media-streams/websocket-messages)、[overflow](https://www.twilio.com/docs/api/errors/31931) | 增加轻量 playback ACK：played PTS + buffered samples；不能只凭 `send_bytes` 成功。 |
| GoogleChromeLabs AudioWorklet | ring 以 framesAvailable 管理，网络/DSP chunk 与 render quantum 分离；不足输出时静音。[示例](https://github.com/GoogleChromeLabs/web-audio-samples/blob/main/src/audio-worklet/design-pattern/wasm-ring-buffer/ring-buffer-worklet-processor.js) | underrun 应原地 rebuffer，不应立即重建 WS；EOF 低于水位也必须 tail-prime。 |
| 浏览器 WebSocket | 标准 `WebSocket` 无原生 backpressure，消费跟不上会堆内存/CPU。[MDN](https://developer.mozilla.org/en-US/docs/Web/API/WebSockets_API) | 继续用标准 WS 时必须有 server pacing、硬 buffer 上限、ACK 和 overflow 策略。 |
| Chrome/WebKit autoplay | Web Audio 应从真实手势内 create/resume，并检查实际 state；Safari 应假定有声媒体需要点击并处理 `play()` rejection。[Chrome](https://developer.chrome.com/blog/autoplay/)、[WebKit](https://webkit.org/blog/7734/auto-play-policy-changes-for-macos/) | Worklet primed 不等于 audible；UI 只能在 context running 且首帧已消费后显示声音已开。 |
| hls.js live edge | live sync 以服务端/播放列表的 live edge 减 safety delay；超出滑动窗口或延迟上限时 seek 回 live sync。[API](https://github.com/video-dev/hls.js/blob/master/docs/API.md) | 类比推导：growing WAV 的 accepted start 必须由服务端按真实 part size clamp。 |
| Pipecat | 中断由断线重连改为 explicit Clear；曾修复后端已中断但浏览器已缓存音频继续播放的问题。[Releases](https://github.com/pipecat-ai/pipecat/releases)、[Issue 456](https://github.com/pipecat-ai/pipecat/issues/456) | 停止 producer 不够；abort 必须抵达浏览器并 flush 对应 generation。 |

## 建议的最小协议

Fresh join 不由浏览器发送 wall-clock 推导的 `after_seq`：

```json
{
  "type": "subscribe",
  "speech_id": "...",
  "generation": "...",
  "mode": "live"
}
```

服务端从真实文件长度计算：

```text
available_samples = floor((file_size - 44) / 2)
live_start = align_down(max(0, available_samples - 400ms), frame_samples)
```

Reconnect 携带 requested position 时：

- 在 current live edge 之后：clamp 到 `live_start`。
- 落后超过 replay window（建议 2–3s）：clamp 到 `live_start`，`discontinuity=true`。
- 位于允许窗口内：从 requested seq 继续。
- 所有位置按完整 PCM frame 对齐。

`audio.start` 至少返回：

```json
{
  "requested_seq": 240,
  "accepted_seq": 74,
  "earliest_pts_samples": 0,
  "live_edge_pts_samples": 75840,
  "discontinuity": true
}
```

客户端每约 250ms 回报：

```json
{
  "type": "playback.ack",
  "generation": "...",
  "played_pts_samples": 67200,
  "buffered_samples": 9600
}
```

短断线可从 last-played 恢复；没有可信 ACK 或断线过久时由服务端重新选择 live edge。

## 生命周期顺序

中止：

1. 原子失效 generation。
2. 向浏览器发送 `audio.abort`；客户端立即 flush ring。
3. 请求上游 cancel。
4. 等待明确 abort ACK、EOF，或执行安全 drain。
5. 删除 part、释放 provider admission。

完成时区分：

- `audio.final`：该 generation 不再产生新 PCM。
- `speech.audio.ready`：最终 WAV 已原子归档且数据库提交完成。
- `playback.drained` / playback ACK：浏览器已经真正播放完毕。

## 浏览器发布门清单

- Chrome 新 profile/低 MEI、强制 deny/allow autoplay；Safari macOS“永不自动播放”和 iPhone Safari 首次进入。
- `addModule()` 人为延迟 0/100/1000/5000ms；一次真实点击最终可播，pending init 期间卸载后 context/node/port/WS 均归零。
- suspended/locked 时持续收到超过 ring 容量的 PCM，UI 不得假称已发声，也不得无限堆积或重连风暴。
- 输入 0.25x/0.8x/1x/1.2x/2x/10x burst，jitter 20–1500ms，2 分钟内 heap/ring/MessagePort 有硬上限，单次 underrun 原地恢复。
- 尾音 1/127/128/129/threshold±1 frames 全部播放，drained 恰一次。
- old generation 的 socket/binary/worklet 回调延迟到达全部丢弃；完整 generation identity 端到端验证。
- hidden 30s/5min、pagehide/pageshow/BFCache、Safari/iOS interrupted、路由 unmount 后无旧音频复活。
- 10 次“primed→underrun”触发 circuit breaker/fallback；只有稳定实际播放 10–30s 后才清失败窗口。

## 2–3 场容量实验

- 当前短期规划：生产保持 1 active；独立 GPU 验证 2 active + 第三场有界公平等待。
- 候选顺序：stock `running_max_req_size=1/2/3` → Triton/动态批 Token2Wav 单 GPU → LLM 与 Token2Wav 分 GPU或多实例分片。
- 每个候选：HTTP stream 与 WS bi-stream，1/2/3/4 同步并发各至少 30 轮；5/40/100/300 字、四音色、冷/热 prompt cache、一次性与每 100ms text append。
- 发布门：TTFT P95≤0.9s、浏览器首声 P95≤1.5s、RTF P95≤0.8、欠载为 0、显存余量≥20%、第三路可取消且不饿死；至少一场正式赛时长持续压测。

当前结论：stock LightTTS + 单张 RTX 3080 Ti 不应直接提高到 3 active；2–3 场同时实时需要独立证据和更强 Token2Wav 并行架构。
