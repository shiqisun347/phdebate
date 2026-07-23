# Iteration 27：OpenMOSS 实时 TTS 架构与候选淘汰

更新时间：2026-07-17。目标不是只替换模型，而是实现：从 AI 回合开始到浏览器首个可听采样最大不超过 3 秒；至少 3 路并发；无卡顿、无吞词、无音色漂移；提供多个中性普通话音色。

## 当前证据推翻了“只换 TTS 即可”的假设

现有链路严格串行：

```text
MatchEngine
  → 等 Agent 完整输出 4.674s
  → TTS 首 PCM P95 1.259s
  → 浏览器预缓冲 0.400s
  → 实际首声理论下限约 6.333s
```

因此必须把替换单元扩大为：

```text
AgentDeltaStream
  → 累计分词 / 可朗读片段组装
  → 持久 SpeechSynthesisSession.push_text(delta)
  → 连续 PCM
  → 有界 pacing + played/buffered ACK
  → AudioWorklet 实际输出
```

只把 LightTTS HTTP endpoint 换成另一个完整文本 TTS endpoint，无法达到 3 秒硬门。

## 候选判定

| 候选 | 官方证据 | 当前硬件适配 | 吞词/稳定性 | 决策 |
|---|---|---|---|---|
| MOSS-TTS-Realtime 1.7B + vLLM-Omni | L20 暖机 TTFB 180ms、RTF 0.51；ZH CER 1.07%、SIM 76.7；原生 text delta→audio delta、多轮音色一致 | 官方 A10G 配方 talker 约 6GB + codec 约 8GB；现机 3080 Ti 12GB 无法安全容纳 | 方向最匹配；但 vLLM codec stage 默认 max sequences=1，3 路必须实测 | 独立 24GB GPU 的首选正式后端 |
| CosyVoice3 Base 0.5B + Triton/TensorRT | 官方双流最低 150ms；ZH CER 1.21%、SIM 78.0；L20 4并发首块 P95 977.55ms | 体量与当前 12GB GPU 更匹配；需在 3080 Ti 实测显存和 P95 | Base 的音色相似度优于 RL；可缓存固定 zero-shot speaker | 当前服务器的主 canary 候选 |
| MOSS-TTS-Nano 0.1B | CPU/ONNX、vLLM 约 2GB、max sequences=4；约763MB ONNX全套 | 成本最低，资源适配最好 | 官方公开 issue 仍有吞句、短句重复、语速不均和标点处丢句；无公开 CER/SIM/TTFB | 不进入生产主线，仅保留低成本实验 |
| Fish Speech S2 Pro 4B | 质量与延迟强 | 24GB+ GPU，商用许可另有条件 | 当前成本不适配 | 淘汰 |
| ChatTTS | 中文、多 speaker、可流式 | 可运行 | 官方承认稳定性限制，权重非商用友好 | 淘汰 |

这里的“CosyVoice3 作为当前服务器主 canary”是结合当前 12GB GPU 与官方资源/并发证据做出的工程推断，尚不是生产通过结论。

## OpenMOSS 可直接复用的关键实现

1. `MOSS-TTS-Realtime/mossttsrealtime/streaming_mossttsrealtime.py`
   - `TextDeltaTokenizer` 累计全文重新分词，并 hold back 末尾 3 个不稳定 token。
   - `MossTTSRealtimeStreamingSession.push_text/end_text/drain` 保持一个连续语音上下文。
2. `example_llm_stream_to_tts.py`
   - Agent delta 直接进入 TTS 的正确参考流程。
3. `example_multiturn_stream_to_tts.py`
   - 跨轮 KV cache 与固定 voice prompt 的参考实现。
4. vLLM-Omni MOSS 部署配置
   - OpenAI-compatible `/v1/audio/speech`、raw PCM、异步 chunk、GPU memory 和 sequence 配置。

不能直接照搬 OpenMOSS `fast_api.py`：其服务明示 batch size=1；多个 session 共享可变 codec streaming 状态；并且对每个 Agent delta 单独 tokenizer.encode，而不是使用累计 `TextDeltaTokenizer`，存在边界错分、重复和吞词风险。

## Nano 被生产主线淘汰的直接证据

- [Issue #58：ONNX 吞字吞句严重](https://github.com/OpenMOSS/MOSS-TTS-Nano/issues/58)
- [Issue #60：短文本重复语音](https://github.com/OpenMOSS/MOSS-TTS-Nano/issues/60)
- [Issue #81：语速不均、短句重复](https://github.com/OpenMOSS/MOSS-TTS-Nano/issues/81)
- [Issue #87：标点处丢句](https://github.com/OpenMOSS/MOSS-TTS-Nano/issues/87)

这些 issue 截至本轮仍开放，直接违反“不吞词、不重复、稳定”的硬要求。调大 max frames、缩短 chunk 或换 seed 只能作为缓解，不构成生产正确性保证。

## 目标内部架构

```text
MatchEngine
  ├─ AgentDeltaStream
  ├─ SpeakableClauseAssembler
  │    ├─ 累计分词
  │    ├─ 10–16 中文 token 软边界
  │    ├─ 强标点立即提交
  │    └─ 禁止切断数字、英文、姓名
  └─ SpeechSynthesisSession
       ├─ AdmissionController（真实多 lease）
       ├─ TTSBackend
       │    ├─ CosyVoice3StreamingBackend
       │    ├─ MossRealtimeBackend
       │    └─ LightTTSFallbackBackend
       ├─ PCMArtifactWriter
       └─ AudioStreamPublisher
            ├─ 60–100ms PCM chunk
            ├─ server high-water/pacing
            ├─ client played/buffered ACK
            └─ final atomic WAV
```

比赛、数据库和浏览器对外契约继续保留 `Speech.audio_url`、stream generation、seq/PTS、最终 WAV 和 `/ws/rooms/{code}/audio`，模型类型不暴露给浏览器。

## 3 秒预算

| 环节 | P95 预算 |
|---|---:|
| 状态提交与 Agent 请求 | 150ms |
| Agent 首个可朗读片段 | 1,250ms |
| 片段组装与 TTS admission | 100ms |
| TTS 首 PCM | 750ms |
| 网络与服务端转发 | 100ms |
| 浏览器预缓冲 | 300ms |
| Worklet 实际输出 | 100ms |
| 合计 | 2,750ms |

留 250ms 抖动余量。验收必须从回合提交计到浏览器实际输出非静音采样，不使用“服务端已收到 PCM”代替。

## 统一发布硬门

- 3 路并发 TTS 首 PCM：P95 ≤800ms、P99 ≤1.2s。
- 端到端浏览器首声：P95 ≤2.8s、max ≤3.0s。
- 3 路并发 RTF P95 ≤0.65；30分钟播放 underrun/overflow=0。
- PCM 块间隔 P99 ≤200ms；非标点停顿=0。
- 中文 CER ≤2%；末句/末词缺失、重复短句、串音、换声均为0。
- 同辩位跨轮 speaker embedding 标准差 ≤0.03；首/中/末段差值 ≤0.05。
- 4–8 个音色使用固定 8–15 秒中性普通话参考音频；比赛开始后永久绑定；禁用方言、情绪指令和随机 speaker。
- 取消后 200ms 内停止播放；最终 WAV 与实际流内容一致。

## 一手资料

- [OpenMOSS MOSS-TTS](https://github.com/OpenMOSS/MOSS-TTS)
- [MOSS-TTS-Realtime 模型卡](https://github.com/OpenMOSS/MOSS-TTS/blob/main/docs/moss_tts_realtime_model_card.md)
- [MOSS-TTS-Nano](https://github.com/OpenMOSS/MOSS-TTS-Nano)
- [vLLM-Omni MOSS 配方](https://github.com/vllm-project/vllm-omni/blob/main/recipes/OpenMOSS/MOSS-TTS.md)
- [vLLM-Omni Realtime 配置](https://github.com/vllm-project/vllm-omni/blob/main/vllm_omni/deploy/moss_tts_realtime.yaml)
- [CosyVoice3 官方仓库与评测](https://github.com/FunAudioLLM/CosyVoice)
- [CosyVoice3 Triton/TensorRT 4并发基准](https://github.com/FunAudioLLM/CosyVoice/blob/main/runtime/triton_trtllm/README.Cosyvoice3.md)

固定审计版本：MOSS-TTS `ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af`；MOSS-TTS-Nano `11619374849c649486584e3b10ed55b176a924ee`；vLLM-Omni `7aa5c9a0901b7b9254052c4d342d0c3fa447eb95`；CosyVoice `074ca6dc9e80a2f424f1f74b48bdd7d3fea531cc`。

## 本轮已落地的代码基础

- `DebateAgentProvider.generate_stream()`：暴露 Agent SSE delta 与权威 final；既有 `generate()` 继续兼容完整文本调用。
- `SpeakableClauseAssembler`：为 CosyVoice 等分片式后端提供强标点、10–16字符软边界，并避免在英文、数字和技术 token 中间切断。
- `benchmark_streaming_tts_candidate.py`：对 OpenAI-compatible raw PCM 流统一测单路/三路首 PCM、RTF、块间隔、失败率并保存 WAV/JSON/Markdown；三路 transport 门为首PCM P95≤800ms、RTF P95≤0.65、块间隔P99≤200ms。
- 定向证据：Provider 全量 59 passed；Agent stream/片段组装/候选 benchmark 组合 10 passed；Ruff 与 py_compile 通过。

这些代码尚未接入 MatchEngine，生产行为没有改变。

## Iteration 28：Agent P95 与连续 TTS session 实证

对生产同配置 Agent 做了 7 次无业务写入的固定短辩题增量测量：首 delta P50/P95/max 为
1.460/4.935/6.186 秒；12 字为 1.666/5.112/6.438 秒；首个强标点可朗读子句为
1.820/5.112/6.438 秒；完整响应为 3.123/6.982/8.248 秒。由此可直接判定：当前即使
TTS 首 PCM 为 0，端到端 3 秒 P95 也无法达标。排除第一个明显冷态/排队样本后，6 个热态
样本首可朗读子句 P95 为 1.975 秒，只能说明常态方向可优化，不能删除真实尾延迟。

原始证据：[Agent delta latency](agent-delta-latency/README.md)、
[JSON](agent-delta-latency/agent-delta-latency.json)。

连接分解每组 6 个有效样本：新建 client 首可播 P95 2.519s；单一 keep-alive
client P95 3.992s；两波 3 路并发 P95 3.397s。TCP connect P95 只有 16.6ms/13.2ms，
持久 client 后 5 个串行请求和第二波 3 路请求都确认复用连接，但 HTTP open 仍可
同时达约 2.420s。这排除了 TCP/TLS 主瓶颈，把 P0 指向上游响应/推理排队。
证据：[Agent transport root cause](agent-delta-latency/agent-transport-root-cause/README.md)。

本地核心代码继续向目标架构推进，但仍未部署：

- `DebateAgentProvider` 改为进程级持久 HTTP keep-alive 连接池，应用关闭时显式释放；避免每个
  AI 回合重复创建客户端和连接。A/B/C 基准已证明这仅是低成本降噪，不是 P95 主修复。
- `LightTTSProvider.open_incremental_session()` 新增真正动态喂入的连续 websocket session；
  Agent 子句可在 final 前送达 TTS，所有子句共用同一个 prompt、admission lease、GPU slot、
  codec/voice 上下文和最终 WAV，不允许退化成每句单独 HTTP 合成。
- session 支持有界 32 子句背压、finish、abort、全任务 deadline、Redis admission、原有
  generation/partial WAV/audio stream 事件及最终原子发布。
- MatchEngine 已接入该流水线，但使用独立 `realtime_voice_pipeline_enabled=false`
  kill switch；新测试证明首个 Agent delta 子句已触发 TTS generation，而 Agent final
  仍被人为阻塞，完整文本 TTS 路径没有被调用；final 释放后再存储权威逐字稿和最终 WAV。
- 可朗读组装器增加 200ms 软超时：缓冲达到最小安全长度后，即使 Agent 没有新 delta
  或强标点，也会提交 10–24 字的安全前缀；不会通过取消 `anext()` 破坏上游 SSE，
  也不会从英文/数字 token 中间切断。
- 如果 TTS 在 Agent final 前失败，流水线会终止音频但继续排空 Agent 文本流，将权威 final
  保存到失败 Speech，从而支持不重新调用 Agent 的 TTS-only retry。Provider cancellation 仍会立即终止。
- Provider 全量测试现在为 61 passed；其中新增测试证明首个 Agent 子句在 `finish` 之前已发送，
  两个子句只使用一条 bi-stream 连接并生成同一最终 WAV；Ruff、py_compile 通过。
- 服务端音频 WebSocket 和 AudioWorklet 增加反压：新客户端声明 `flow_control`，
  Worklet 回传 buffered/played 毫秒；服务端首批最多发 20×40ms=800ms，收到
  feedback 后每批最多 8×40ms=320ms，客户端缓冲高水位 1.2s。这使 TTS 短时
  快于实时产生 PCM 时不再无界地灌入 3s ring buffer，同时保留旧客户端兼容。
- 流控定向证据：API 2 passed（含精确 20-frame 首批和 flow ACK 后续传）；
  Web player/worklet 2 files / 7 tests passed；Ruff 通过。
- 统一回归：API 313 passed / 22 warnings；Web 26 files / 190 tests passed；Next.js production build 通过。

当前生产行为仍未改变：上述修改只在本地工作树，MatchEngine 新分支的
kill switch 默认关闭，也没有启用生产 streaming flag。

CosyVoice3 隔离 canary 又确认了一个重要事实：生产当前已是
`Fun-CosyVoice3-0.5B-2512 + LightTTS + TensorRT`，不是需要再换成 CosyVoice3。在同一张
12GB 3080 Ti 上再起官方 PyTorch/gRPC 副本，即使 tokenizer 放 CPU，warmup 仍在安全
显存上限内 OOM，因此单路/三路/CER 均按资源门 `NOT RUN`，没有强行越界。878 次
watchdog 无生产活动，收尾后候选进程和端口归零，GPU 恢复基线。
证据：[CosyVoice3 canary](cosyvoice3-canary/README.md)。工程决策因此改为：直接优化当前常驻
TRT 服务，并在独立 GPU 上验证 active=2/3；不在生产卡上再加载一份相同模型。

对当前常驻 TRT 服务的首个 10 字、两段 bi-stream 请求在 80s 后仍为 0 PCM。
日志精确停在 encode→LLM prefill 之后，没有 LLM send、decode receive 或 token2wav；客户端断开后
请求仍每秒 `get out data ... 0`，并报 `can release False refcount 4`。根因是该 endpoint
收到 `finish` 后只 `await process_task`、不再读 WebSocket，所以断开不会触发
`WebSocketDisconnect`/abort，形成占满唯一 slot 的 orphan；ready 仍假绿。这与
[LightTTS issue #3](https://github.com/ModelTC/LightTTS/issues/3) 的卡死日志高度一致。

为恢复本次探针占用的 TTS 能力，在 active match=false、活动房为空、gate0/0 后，
只重启了 `jixia-lighttts` 一个服务。新进程完全 ready 后：V2 ready 连续 5/5，
8080 LISTEN，非合成 OpenAPI 3/3=200，启动自检完整通过 encode→LLM→decode→release，
req246 不再出现，GPU 恢复约 5GB 常驻，所有其他服务 PID/uptime 未变。

因此本地新增第三个独立安全门 `lighttts_bistream_enabled=false`。只开流式浏览器或实时
pipeline flag 都不会触发未验证的上游 bi-stream；必须先修复 orphan/abort 并通过实机门禁。
证据：[production bi-stream short clause](production-bistream-short-clause/README.md)。

## Iteration 29：MOSS 原生 session 候选接入与并发边界

固定源码审计进一步排除了 vLLM-Omni WebSocket：在 commit
`7aa5c9a0901b7b9254052c4d342d0c3fa447eb95` 中，所有 `input.text` 只追加到内存，直到
`input.done` 才创建唯一一次 engine request。它只能在完整文本已经结束后流出 PCM，不能把
Agent delta 提前变成首声，因此核心实时链路判定为 NO-GO。完整证据见
[vLLM-Omni session adapter audit](vllm-omni-session-adapter-audit.md)。

OpenMOSS 原生 `MossTTSRealtimeStreamingSession` 则是真正增量语义：达到 prefill token 阈值即可
从 `push_text` 产生音频帧，结束时执行 `end_text -> drain -> decoder.flush`。本地应用已新增默认
关闭的 `moss_realtime` 后端：

- 首个安全 Agent 子句调用 `/tts/session/start`，后续子句在同一个 turn 上调用
  `/tts/session/push`，同时持续读取 `/tts/session/{id}/audio` 的单声道 PCM16。
- 首 PCM 到达后沿用现有 generation/growing-WAV/AudioWorklet 通道；完成后校验时长、fsync 并
  原子发布最终 WAV，逐字稿仍以 Agent final 为权威。
- 取消、暂停、deadline 或客户端任务失败都会中止本地 reader、删除 `.part`，并在 HTTP client
  关闭前保证尝试 `/tts/session/close`；close ACK 已进入三路 benchmark 硬门。
- 音色使用服务端固定 prompt 文件映射；比赛过程中不重新采样 speaker，不为每个子句建立新
  session，避免句间音色状态重置。
- `REALTIME_VOICE_BACKEND` 显式选择 LightTTS 或 MOSS，所有新 flag 默认 false；当前生产配置和
  行为没有改变。

官方 MOSS FastAPI 示例虽为每个 session 创建线程和命令队列，但所有线程共享同一个 model/codec，
每个 turn 都进入 `codec.streaming(batch_size=1)`，没有全局 GPU semaphore，也没有并发安全证据。
`/close` 只 enqueue shutdown 后立即删除 session/返回，不能中断正在同步执行的 generation，且无
cancel ACK、worker join、超时和结构化 worker error。因此应用没有把“多线程”误判成“单实例可三并发”：

- 默认每个 endpoint 最多 1 个 active turn。
- `MOSS_TTS_REALTIME_URLS` 支持多个相互独立、各自通过 canary 的 endpoint；应用以有界 semaphore
  分片，目标 2–3 路由 2–3 个独立实例承担。
- fake 双 endpoint 测试确认两场同时启动时分别落到不同实例；单 endpoint 不会被三个 session
  并发压入未经验证的共享 codec context。
- 独立 GPU 到位后使用新增
  `scripts/benchmark_moss_realtime_sessions.py` 直接测原生 start/push/audio/close 协议的 1/2/3 路
  首 PCM、RTF、块间隔、失败率、close ACK、WAV 和 endpoint 分片。三路 transport 门仍为首 PCM
  P95 <=800ms、RTF P95 <=0.65、每请求 chunk-gap P99 <=200ms、零失败、close ACK 100%。

LightTTS lifecycle 的只读修复审计也已完成：API 层可以补 first-PCM/whole-session deadline、断连
watcher、guaranteed abort 和内部 readiness；但若 model RPC 在 abort grace 内仍不释放共享内存，
必须 fail-fast 重启完整 LightTTS 进程组，不能强行复用或只重启 LLM 子进程。ready 还必须暴露
active、oldest、first-PCM stalled、orphan 和 stage heartbeat。证据见
[LightTTS bi-stream lifecycle audit](lighttts-bistream-lifecycle-fix-audit.md)。

本轮验证：MOSS Provider/增量流水线/benchmark 定向 `79 passed`；API 全量
`322 passed, 22 warnings`；Ruff 与 py_compile 通过。未部署 MOSS endpoint，未开启任何实时 flag，
因此本轮不宣称真实 TTFB、CER、音色漂移或 2–3 路 GPU 容量达标，也没有伪造浏览器截图。

## 隔离 canary 安全门结果

生产预检发现房 `433825` 正在真实运行、`active_match_processing=true`，因此没有安装、下载、启动 MOSS Nano，也没有修改 V2、Agent、数据库、Nginx、Supervisor 或环境变量。所有生产服务与 LightTTS 保持健康。

证据：[MOSS Nano canary 安全报告](moss-nano-canary/README.md)、[preflight JSON](moss-nano-canary/preflight.json)。
