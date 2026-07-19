# 实时 TTS、WebSocket 与浏览器播放参考研究

更新时间：2026-07-17 17:10 CST（参考研究完成；容量实现继续迭代）

## 当前链路的关键诊断

生产 LightTTS 使用 RTX 3080 Ti 12GB，常驻显存约 5GB。服务实现已经提供：

- `POST /inference_zero_shot` 的 `stream=true`，以 `StreamingResponse` 连续返回 PCM16；
- `WS /inference_zero_shot_bistream`，支持追加文本、持续返回音频和断开时 abort；
- LLM continuous batching、decode batch 和独立 encode/LLM/decode 进程。

但当前平台 Provider 明确发送 `stream=false`，并通过 `httpx.Response.content` 等待整个响应，再把完整 WAV 发布给浏览器；Web 端使用单个 `HTMLAudioElement` 请求完整 WAV。生产 LightTTS 启动参数又把 `running_max_req_size`、`decode_max_batch_size` 和各并行进程都限制为 1，应用 Redis gate 也只允许 1 个 active。因此当前“合成完才播放、同一时刻只合成一条”不是模型的硬限制，而是平台和部署配置共同形成的限制。

生产只读探针随后验证了该判断：`stream=true` 单路首 PCM P95 为 1.259 秒，但 2/3 并发首块 P95 恶化到 6.333/10.983 秒，第二、第三请求呈串行等待。完整数据见 [LightTTS 1/2/3 并发基准](iteration19-lighttts-stream-1-2-3.md)。

## 一手参考实现

### 1. CosyVoice 原生流式合成

- [CosyVoice 官方仓库](https://github.com/FunAudioLLM/CosyVoice)声明支持 text-in/audio-out bi-streaming；官方 README 给出 gRPC `--max_conc 4` 的部署示例。
- [FastAPI server](https://github.com/FunAudioLLM/CosyVoice/blob/main/runtime/python/fastapi/server.py)直接把生成器包装为 `StreamingResponse`，逐块输出 PCM16，而不是先保存完整文件。
- [CosyVoice CLI](https://github.com/FunAudioLLM/CosyVoice/blob/main/cosyvoice/cli/cosyvoice.py)的 zero-shot 推理把 `stream` 继续传给模型生成器；文本前端仍先做标准化与拆分。
- [CosyVoice model](https://github.com/FunAudioLLM/CosyVoice/blob/main/cosyvoice/cli/model.py)为每个 session 保存 token、mel、HiFi-GAN source/speech cache，并在相邻流式块之间使用 Hamming 窗淡入淡出；token hop 逐步放大，在首包延迟和后续效率之间折中。
- [官方 gRPC server](https://github.com/FunAudioLLM/CosyVoice/blob/main/runtime/python/grpc/server.py)用 server-streaming response 逐块发送音频，并显式配置 `max_workers/maximum_concurrent_rpcs=max_conc`，默认 4。

可直接借鉴：保留同一个 TTS session/voice prompt 完成整段发言；使用模型自身的 token/mel/speech cache 和 overlap，不再把每 30 字当成彼此独立的完整 utterance；服务端每块立即转发并同时写入最终可归档音频。

生产使用的上游 [ModelTC/LightTTS](https://github.com/ModelTC/LightTTS) 还提供了更贴近当前部署的参考：

- [HTTP/WS 流式输出](https://github.com/ModelTC/LightTTS/blob/ad1c76e36614a1aa26629bc11246884bb7c4072c/light_tts/server/api_http.py#L159-L264)把 float waveform 直接转成 PCM16；bi-stream 只初始化一次 prompt，随后追加文本，断开时 abort。
- [bi-stream 客户端示例](https://github.com/ModelTC/LightTTS/blob/ad1c76e36614a1aa26629bc11246884bb7c4072c/test/test_bistream.py)可作为协议探针起点。
- [prompt 共享内存缓存](https://github.com/ModelTC/LightTTS/blob/ad1c76e36614a1aa26629bc11246884bb7c4072c/light_tts/server/core/objs/shm_speech_manager.py#L74-L137)以 prompt WAV MD5 复用 speech token、feature 和 speaker embedding；固定音色必须保持 WAV bytes 与 prompt text 完全一致。
- [continuous batching](https://github.com/ModelTC/LightTTS/blob/ad1c76e36614a1aa26629bc11246884bb7c4072c/light_tts/server/tts_llm/manager.py#L243-L305)能在 decode 期间接纳新请求；但 [token2wav manager](https://github.com/ModelTC/LightTTS/blob/ad1c76e36614a1aa26629bc11246884bb7c4072c/light_tts/server/tts_decode/manager.py#L46-L89)当前一次只取一个请求，CLI 的 decode batch 参数没有真正解除该瓶颈。
- [官方并发基准](https://github.com/ModelTC/LightTTS/blob/ad1c76e36614a1aa26629bc11246884bb7c4072c/README.md#performance-benchmarks)显示并发提升会显著抬高首块尾延迟，因此不能只把 `running_max_req_size` 从 1 调到大值。

### 2. 浏览器音频解锁与生命周期

- [LiveKit browser SDK](https://github.com/livekit/client-sdk-js)把自动播放能力建模为独立状态；`startAudio()` 必须在 click/tap 处理器中直接执行，并通过 `AudioPlaybackStatusChanged` 通知 UI。iOS 还使用持续静音音轨保持 audio session。
- [LiveKit Room.startAudio 源码](https://github.com/livekit/client-sdk-js/blob/main/src/room/Room.ts)统一恢复 AudioContext 和所有媒体元素；页面重新可见时再次尝试恢复播放。
- [GoogleChromeLabs Web Audio ring buffer](https://github.com/GoogleChromeLabs/web-audio-samples/blob/main/src/lib/free-queue/free-queue.js)使用固定容量读写索引和 `framesAvailable`，避免把整段长音频一次性送入 AudioWorklet。
- [AudioWorklet ring-buffer processor](https://github.com/GoogleChromeLabs/web-audio-samples/blob/main/src/audio-worklet/design-pattern/wasm-ring-buffer/ring-buffer-worklet-processor.js)说明浏览器渲染线程按固定 128-frame quantum 消费，生产者必须通过环形缓冲吸收网络/计算抖动。

可直接借鉴：浏览器只维护一个 generation-scoped player；流式 PCM 进入固定上限环形缓冲，达到启动水位后播放；阶段切换/暂停/中断以控制帧清空旧 generation；手势解锁、网络错误、decode 错误和正常 ended 分离。

### 3. WebSocket 中断、恢复与背压

- [Pipecat WebSocket interruption issue/fix](https://github.com/pipecat-ai/pipecat/issues/456)表明仅停止后端生成不够，必须向前端发送明确 interruption control frame，并立即清空已下发但尚未播放的 buffer，否则旧回答会继续播放或与新回答重叠。
- [ws heartbeat 示例](https://github.com/websockets/ws)用 ping/pong 识别半开连接；客户端和服务端都需要超时终止而不是无限等待。
- [ws API](https://github.com/websockets/ws/blob/master/doc/ws.md)暴露 `bufferedAmount`、pause/resume 和 stream 接口，可用于发送侧背压和有界队列。

可直接借鉴：所有音频控制消息携带 `room_code + speech_id + generation + chunk_seq`；客户端只接受当前 generation、按 seq 去重排序；`audio.abort` 必须清空 ring buffer；发送端超过高水位暂停读取 TTS generator，低于低水位再恢复。重连后不从内存猜测，重新获取权威 room snapshot，并从最终媒体 Range/当前流 generation 对齐。

## 建议目标架构

1. LightTTS 请求改为原生 `stream=true`，Provider 使用 `client.stream(...).aiter_bytes()`，记录首字节时间、chunk 数、空洞和总 RTF。
2. API/Engine 在收到第一个可播放块后发布 `audio.stream.started`，通过比赛 WebSocket 发送结构化 metadata + binary PCM chunks；同时顺序写入本地 raw PCM/WAV part，结束后做边界/完整性校验并原子发布最终 WAV。
3. 浏览器使用 AudioWorklet + 固定环形缓冲播放 PCM16 mono；建议起播水位 250–400ms、目标缓冲 500–800ms、上限 2s。缓冲不足发送 waiting 指标；恢复后依据服务端权威时间轴丢弃过时帧，避免慢网把比赛尾部拖丢。
4. `audio.start/chunk/end/abort` 全部带不可变 generation 和单调 `chunk_seq`；暂停、结束比赛、阶段切换和新 speech 必须先发 abort，再清旧 generation。
5. 晚加入/刷新不强行接半截内存流：若 final 已存在，用 HTTP Range + 权威 offset；若仍在流中，只从服务器保存的最近有界 replay window 接入，否则字幕继续、音频从下一个完整边界开始。
6. 音色一致性以“整段发言一个模型 session + 固定 prompt fingerprint”为准，禁止某个 chunk 单独切默认 prompt；模型原生 cache/overlap 负责块间连续性，平台只做最终归档校验，不再以 30 字独立 utterance 拼完整比赛发言。
7. 并发分两层：先把 LightTTS `running_max_req_size` 灰度到 2，验证 decode 单路瓶颈下的真实 TTFT；应用 global active 同步到 2，并让第三场进入短时、公平、可取消队列。实机三路达到门槛后再升 active=3。保留有界等待，但不做用户可见的容量编排模块。

## 2–3 场并发验收门

- 三个不同房间同时触发 30–60 字 AI 发言，成功率 100%，无跨房音频/文本/voice prompt 串流。
- 首个 PCM chunk：P50 ≤ 500ms、P95 ≤ 900ms；浏览器首声：P50 ≤ 800ms、P95 ≤ 1.5s。
- 三并发服务端 RTF P95 ≤ 0.8；单条任务不能长期饿死，队列等待 P95 ≤ 3s。
- 浏览器目标缓冲 0.5–0.8s；rebuffer ratio P95 < 5%，单次 stall P95 < 1s。
- generation 切换后 200ms 内停止旧音频；任何旧 chunk/reject/error callback 不得影响新 speech。
- 音频 chunk seq 不丢、不重、不乱；final WAV 的样本数等于已接受 PCM chunk 总样本数（扣除明确记录的最终边界处理），hash/manifest 可追溯。
- 同一 voice 的三并发样本 speaker similarity 与单并发基线无显著下降；平台拼接点无 >300ms 人工空洞、无削波、无重复/吞字。
- GPU 显存不 OOM，P99 首包无持续性 >1.5s 尾部离群；若三并发不达标，保持二并发并给第三场有界排队，而不是无声失败。

## 建议的最小流协议

为避免高频二进制帧拖慢现有 room snapshot/control，建议使用独立 `/rooms/{code}/audio` WebSocket；现有房间 WS 继续承载低频权威状态。

- `audio.start`：`room_code, speech_id, generation, codec=pcm_s16le, sample_rate=24000, channels=1, play_at_server_ms, first_seq, text_hash`。
- `audio.chunk`：JSON metadata `speech_id, generation, seq, pts_ms, sample_count, crc32`，紧接 binary payload；建议重打包为 40–100ms PCM。
- `audio.flush`：`generation, last_seq, total_samples, duration_ms`；只有 provider EOF/flush ack 且样本完整时发送。
- `audio.abort`：`generation, reason, heard_until_ms`；客户端原子清空 ring，旧 generation 永久失效。
- `audio.complete`：`generation, duration_ms, archive_url`；最终 WAV 原子发布后发送。
- client ack：`generation, last_contiguous_seq, played_pts_ms, buffered_ms`，只用于慢客户端隔离与恢复，不反控比赛状态机。

顺序唯一键为 `(speech_id, generation, seq)`；重复直接丢，gap 超过 300–500ms 进入 resync。所有暂停、提前结束、阶段切换和新 speech 都必须同时取消 provider generation 并广播 `audio.abort`；只停后端会让已下发缓冲继续播放，Pipecat、Deepgram 和 OpenAI Realtime 的 interruption/clear 设计都证明这两个动作必须分离。

## 缓冲、追赶与恢复规则

- 建议服务端在已有 250–400ms PCM 时发布 `audio.start`，`play_at_server_ms=server_now+300ms`；所有浏览器共享同一播放 epoch。
- 浏览器起播水位 250–400ms、目标 500–800ms、正常高水位 1.5s、硬上限 3s。静音时继续推进权威时间线并丢弃历史帧，取消静音不回放 backlog。
- 客户端目标 PTS 为 `estimated_server_now - play_at_server_ms`；落后 250–500ms 时直接跳到当前 chunk 边界，第一版不做 time-stretch。
- 服务端每 generation 保存 15–20s replay ring。24kHz mono PCM16 约 48KB/s，20s 约 0.96MB；3 房约 2.9MB，内存成本可控。
- 重连携带 `generation, last_contiguous_seq, played_pts_ms`；服务端只返回 `resume`、`catch_up` 或 `completed`。若最终 WAV 已存在，继续复用 HTTP Range + `playback_started_at/currentTime`；绝不从 seq 0 重播。
- 单个慢客户端达到高水位后切 catch-up/丢旧帧，不能反压 provider 或其他房间。发送队列以“毫秒音频”计量，不以消息数计量。

## 实施顺序

1. 先上线本轮 WAV 边界和现有 HTMLAudio 生命周期修复，消除当前硬切、旧回调和 Safari 解锁问题。
2. 在隔离端口直接调用现有 LightTTS `stream=true`，测首包、chunk 间隔、结束完整性和 1/2/3 并发，不先改比赛状态机。
3. 增加服务端流式 session/manifest 与 WebSocket 音频帧协议，并建立纯前端 synthetic PCM ring-buffer 测试。
4. 仅在 shadow/QA 房接入 AudioWorklet；完成 Chrome + Safari 手势、后台、弱网、断线和 interruption 回归后再切正式比赛。
5. 最后灰度 `running_max_req_size/decode_max_batch_size` 和应用 active=2→3，每一级都保留 Supervisor、源码和配置回滚点。
