# GPU OpenMOSS Nano / MOSS-TTS-Realtime 部署与验收硬门

更新时间：2026-07-18（Asia/Shanghai）  
性质：只读源码与官方资料审计；**未修改代码、未部署、未租用 GPU、未触碰生产**。

## 1. 结论先行

1. **MOSS-TTS-Nano 必须在 GPU 上做 canary，但按当前官方实现不能成为正式比赛的主实时后端。**
   - Nano 的 PyTorch/CUDA、ONNX Runtime CUDA 和 vLLM-Omni 路径都能逐块输出音频。
   - 但官方 Nano 接口在启动一次生成时接收的是完整 `text`；vLLM-Omni 的 WebSocket 也只是累计全部 `input.text`，直到 `input.done` 才创建一次推理请求。因此它不是“LLM 首字出现后，delta 持续进入同一 TTS session”的真实增量输入。
   - 官方仓库尚未发布 Nano 的 TTS CER/WER/SIM 结果，并且仍有吞句、短句重复、语速不均、标点丢句和分片音色跳变的开放 issue。Nano 当前状态是 **GPU 实验 canary 可做，正式 4v4 主链路 NO-GO**。

2. **MOSS-TTS-Realtime 是符合核心语义的首选正式候选。**
   - 原生 `MossTTSRealtimeStreamingSession.push_text/push_text_tokens` 达到 12-token prefill 后即可生成音频，不等 Agent final；官方模型卡给出单张 L20、SDPA + `torch.compile`、暖机后 TTFB 180 ms、RTF 0.51，中文 CER 1.07%、SIM 76.7。
   - 正式链路必须使用原生 session 语义，不能用当前 vLLM-Omni `/v1/audio/speech/stream` 代替，因为后者在 `input.done` 前不推理。

3. **2–3 场同时进行时，容量模型是每场最多 1 条稳定发言流，外加回合切换时的短暂取消重叠。**
   - 4v4 比赛是轮流发言，不是每场 8 个辩手同时讲话；3 场的稳态要求是 3 条活跃 TTS 流。
   - 当前官方 Realtime FastAPI 明示 batch size=1；官方 issue 对“一个实例 5 并发导致音色失真和 CPU 尖峰”的回答是线程安全版本尚待发布。因此初始生产拓扑必须是 **1 个 endpoint 最多 1 条活跃流**，3 场配 3 个相互隔离的 endpoint。是否能把多个 endpoint 合并到同一张大显存 GPU，只能由实测证明，不能预设。

4. **正式浏览器传输必须是 WebRTC。**
   - TTS 服务输出连续 PCM，实时媒体层转为 Opus/WebRTC track；WebSocket PCM/AudioWorklet 仅保留诊断或降级，不作为正式赛 GO 证据。
   - 禁止 MediaRecorder + MP3 分片、逐句 `<audio>`、完整 WAV/MP3 下载后播放等路径进入正式验收。

5. **用户给出的硬时钟按浏览器 LLM 首字开始。**
   - `T0` = 浏览器首次实际渲染 LLM 回复字符。
   - `T audible` = Chrome/Safari 的 WebRTC 接收音轨首次实际播放连续非静音采样。
   - 发布硬门：暖态正式流程 **P95 ≤ 2.0 s，所有有效样本 max ≤ 2.5 s**；任何浏览器、任何三路并发样本超过 2.5 s 即 NO-GO。

## 2. 固定审计版本与一手证据

| 组件 | 固定版本 | 关键证据 |
|---|---|---|
| OpenMOSS/MOSS-TTS | [`ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af`](https://github.com/OpenMOSS/MOSS-TTS/commit/ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af) | [Realtime 模型卡](https://github.com/OpenMOSS/MOSS-TTS/blob/ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af/docs/moss_tts_realtime_model_card.md)、[原生 streaming session](https://github.com/OpenMOSS/MOSS-TTS/blob/ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af/moss_tts_realtime/mossttsrealtime/streaming_mossttsrealtime.py)、[官方 FastAPI](https://github.com/OpenMOSS/MOSS-TTS/blob/ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af/moss_tts_realtime/fast_api.py) |
| OpenMOSS/MOSS-TTS-Nano | [`11619374849c649486584e3b10ed55b176a924ee`](https://github.com/OpenMOSS/MOSS-TTS-Nano/commit/11619374849c649486584e3b10ed55b176a924ee) | [CUDA/ONNX CUDA 说明](https://github.com/OpenMOSS/MOSS-TTS-Nano/blob/11619374849c649486584e3b10ed55b176a924ee/README_zh.md)、[Nano GPU runtime](https://github.com/OpenMOSS/MOSS-TTS-Nano/blob/11619374849c649486584e3b10ed55b176a924ee/moss_tts_nano_runtime.py) |
| vLLM-Omni | [`7aa5c9a0901b7b9254052c4d342d0c3fa447eb95`](https://github.com/vllm-project/vllm-omni/commit/7aa5c9a0901b7b9254052c4d342d0c3fa447eb95) | [Nano deploy](https://github.com/vllm-project/vllm-omni/blob/7aa5c9a0901b7b9254052c4d342d0c3fa447eb95/vllm_omni/deploy/moss_tts_nano.yaml)、[Realtime deploy](https://github.com/vllm-project/vllm-omni/blob/7aa5c9a0901b7b9254052c4d342d0c3fa447eb95/vllm_omni/deploy/moss_tts_realtime.yaml)、[TTS WebSocket handler](https://github.com/vllm-project/vllm-omni/blob/7aa5c9a0901b7b9254052c4d342d0c3fa447eb95/vllm_omni/entrypoints/openai/serving_speech_stream.py) |

Hugging Face 官方模型快照只用于估算静态权重下限，不把文件大小等同于真实显存：

| 模型快照 | 固定 SHA | 权重/模型文件大小 |
|---|---|---:|
| `OpenMOSS-Team/MOSS-TTS-Realtime` | `6acbc7f161a0db71c291f2d0aaa9eee59334cab2` | 4.664 GB |
| `OpenMOSS-Team/MOSS-Audio-Tokenizer` | `3cd226ba2947efa357ef453bcad111b6eafba782` | 7.098 GB |
| Realtime 合计静态权重 | — | **约 11.76 GB**，尚未包含 activation、KV cache、CUDA workspace、编译缓存和碎片 |
| `OpenMOSS-Team/MOSS-TTS-Nano` | `44502f80dbf9743528fa921cc544d662c685ebec` | 234.7 MB |
| `OpenMOSS-Team/MOSS-Audio-Tokenizer-Nano` | `6aa02b01e445cc585582cf0ba480bc3ea6c8dd68` | 87.9 MB |
| Nano ONNX TTS + codec | `f52645...` + `ceff0d...` | 约 762.2 MB |

## 3. GPU 与显存门槛

### 3.1 MOSS-TTS-Nano GPU canary

推荐首轮机器：**NVIDIA L4 24 GB 或同级 Ampere/Ada 24 GB**。

- vLLM-Omni 官方配置注明已在 1×L4 24 GB 验证，也适用于 H20/A100/RTX 3090；配置注释称 AR LM + codec 约 2 GiB，`gpu_memory_utilization=0.3`，`max_num_seqs=4`。
- 独立 PyTorch/CUDA 单路 smoke 的绝对资源下限可从 8 GB 开始，但这不是并发/正式推荐；正式 Nano canary 统一使用 24 GB，避免显存不足被误判成模型质量或流式能力问题。
- 不把 CPU/ONNX CPU 结果作为本项目 Nano 的性能结论。ONNX CUDA 可作为第二个 A/B 后端，但必须确认每个 ONNX session 实际启用了 `CUDAExecutionProvider`。

Nano GPU 加速配置门：

1. PyTorch 路径使用 CUDA，Ampere+ 优先 BF16，其他支持卡使用 FP16。
2. `flash_attn` 可用时测试 `flash_attention_2`；同时保留 SDPA 对照。Nano runtime 在 CUDA + FP16/BF16 + `flash_attn` 条件满足时才允许 FA2。
3. vLLM-Omni 按官方固定版保持 `enforce_eager=true`；官方注释明确该 Nano adapter 尚未接入 CUDA graph，不能宣称启用 CUDA graph 加速。
4. vLLM 参数起点：`max_num_seqs=4`、`max_num_batched_tokens=4096`、`max_model_len=4096`、`gpu_memory_utilization=0.3`、关闭 prefix cache。
5. Nano PyTorch 服务内部用全局 `RLock` 包住完整 `inference_stream`，所以一个进程实际上串行；并发验证应使用 vLLM adapter 或独立进程，不得用多线程伪造并发。
6. vLLM Nano adapter 使用上游全局 RNG；源码明确提示并发 seeded requests 会争抢全局随机状态。并发门禁必须检查串音、请求互换和复现漂移，不能只看 HTTP 成功率。

### 3.2 MOSS-TTS-Realtime 正式候选

推荐首轮机器：**每个 endpoint 使用 1×L20 48 GB**。可接受的 canary 下限是 **24 GB Ampere+ 独占 GPU**，但只有显存、延迟和 30 分钟稳定性全部通过后才能保留。

理由：

- 官方 180 ms TTFB / 0.51 RTF 数据只在单张 L20、暖机、SDPA + `torch.compile` 条件下成立。
- Realtime 模型与 codec 的静态权重已约 11.76 GB。24 GB 是合理的技术 canary 下限，不是官方性能承诺；12 GB 卡没有足够生产余量。
- 为满足 3 场同时发言，初始拓扑按 3 个独立 endpoint 设计。最保守配置是 3×GPU；若尝试 48/80 GB GPU 上多进程共卡，仍必须把每个进程当独立 endpoint，并完整重跑三路门禁。

Realtime 推理加速基线：

1. **默认使用 BF16 + SDPA + `torch.compile(fullgraph=True)`。**这是官方 L20 性能数据使用的组合。
2. FlashAttention 2 仅作为对照实验。固定源码在 FA2 下切换 DynamicCache 并关闭 local-transformer compile；官方 issue 也说明 FA2 + StaticCache/compile 曾产生错误或不可懂音频。没有实测前不得假设 FA2 更快。
3. 使用 `torch.inference_mode()`，启动时完成模型加载、compile、codec 初始化和真实短句 warmup；readiness 在 warmup 完成前必须为 false。
4. 为每个固定辩手音色预编码并缓存 voice prompt tokens。正式比赛只引用固定 token，不在热路径重复编码参考音频。
5. 模型参数从官方推荐值开始：`temperature=0.8`、`top_p=0.6`、`top_k=30`、`repetition_penalty=1.1`、`repetition_window=50`。
6. prefill 使用官方 12 text tokens；不能为了更早出声无证据地降到 1–4 token。可测试 8/10/12，但低于 12 只有在 CER、吞词、重复、音色和 P95 同时通过时才允许。
7. codec 一个 turn 只进入一次 `codec.streaming(batch_size=1)`，直到结束或取消；不能对每个 LLM delta 反复重开 codec context。
8. 首音频 decode 可从 `initial_chunk_frames=1`、后续 6 frames 的官方 FastAPI 配置开始，最终以 WebRTC 无 underrun 实测选择；不能为追求 TTFB 输出大量 80 ms 以下碎片导致浏览器断续。

## 4. “真实流式增量”定义

以下四条缺一不可：

1. Agent final 尚未产生时，首个 LLM delta 已进入同一个 TTS turn。
2. 后续 delta 继续进入相同的模型 KV/voice/codec context，不为每个短句新建一次 TTS request。
3. Agent final 尚未产生时，浏览器已经从 WebRTC track 播放出非静音音频。
4. `finish` 只负责提交剩余 token、drain 和 flush，不是第一次触发推理。

MOSS-Realtime 原生 session 满足该语义：官方实现提供 `push_text`、`push_text_tokens`、`end_text`、`drain`，并在达到 prefill 阈值后生成音频。

当前 vLLM-Omni WebSocket **不满足**：源码开头和 handler 都明确把 `input.text` 追加到 `text_parts`，收到 `input.done` 后才 join 全文并调用一次 `_generate_and_send`。它能流式输出 PCM，但不能提前消费 Agent delta。

Nano 的官方 `inference_stream(text=...)` 同样在创建 generator 时接收完整文本。把 LLM 文本切成多次 Nano 请求属于“分片合成”，不是同一生成 session 的增量输入；它还会放大短句重复与跨片段音色跳变风险。

因此当前架构判定：

| 后端 | 音频流式输出 | LLM delta 真增量输入 | 正式主链路资格 |
|---|---:|---:|---|
| Nano PyTorch/CUDA | 是 | 否 | NO-GO |
| Nano ONNX CUDA | 是 | 否 | NO-GO |
| Nano + vLLM-Omni | 是 | 否；WS 等 `input.done` | NO-GO，可做吞吐/质量 canary |
| Realtime + vLLM-Omni WS | 是 | 否；WS 等 `input.done` | NO-GO |
| Realtime 原生 `MossTTSRealtimeStreamingSession` | 是 | **是** | 正式首选候选 |

## 5. 2–3 场比赛的并发拓扑

### 5.1 合理负载模型

- 每场比赛稳态只有 1 个当前发言者。
- 3 场 = 3 条稳态 TTS 流。
- 回合切换、暂停、结束比赛时，旧流可能在取消，新流已排队；测试必须加入最多 3 个短暂 shadow/cancel 任务，但不允许 6 条流都持续完整合成。
- 每场 8 个辩位固定音色，比赛中一个辩位的 voice prompt 永久不变。

### 5.2 Realtime 初始安全拓扑

```text
match A active speech -> endpoint A -> isolated model/session/codec/GPU lease
match B active speech -> endpoint B -> isolated model/session/codec/GPU lease
match C active speech -> endpoint C -> isolated model/session/codec/GPU lease
```

硬性要求：

- 每个 endpoint admission `active_turns <= 1`。
- endpoint 必须独立进程；不同 endpoint 不共享 Python model object、inferencer、codec streaming context 或随机状态。
- 负载均衡只在 turn 开始时分配，turn 内不得迁移 endpoint。
- 某 endpoint 不健康时只摘除该 endpoint；不能把正在生成的 turn 静默迁移并拼接另一音色。
- 至少保留一个可观测 pending 队列；没有可用 endpoint 时 fail fast 或按比赛规则等待，不得无界排队。

官方 FastAPI 不能原样作为生产服务：多个 session thread 共享同一模型/codec；`/close` 只 enqueue `shutdown` 后立即从 manager 删除并返回，没有 worker join/close ACK；活跃 turn 的 shutdown 路径也没有保证退出 codec context。正式包装层必须补齐 admission、deadline、取消确认、worker/process watchdog 和资源回收证明。

### 5.3 Nano 并发说明

vLLM Nano 配置声明 `max_num_seqs=4`，可用来做 1/2/3 路 GPU 压测；但这只证明 scheduler 可容纳多个 request，不证明：

- 真实 LLM delta 输入；
- seeded 并发无全局 RNG 干扰；
- 音色不串；
- 短句不重复/吞句；
- 三路浏览器首声 ≤2.5 s。

任何一项失败，Nano 立即退出正式候选，不继续用更多参数掩盖架构缺口。

## 6. WebRTC 正式音频链路门槛

正式链路固定为：

```text
LLM delta
  -> native Realtime TTS session
  -> PCM16/24 kHz mono
  -> realtime media gateway / SFU publisher
  -> Opus, 20 ms packetization
  -> WebRTC receiver track
  -> Chrome / Safari actual playout
```

要求：

- 浏览器在用户进入比赛时通过一次用户手势预解锁音频，不能把 autoplay block 算成模型问题后忽略。
- 一个 Speech generation 对应一个可取消的 WebRTC audio epoch；旧 epoch 的晚到包必须丢弃。
- 暂停：停止 playout 并暂停/取消生成，不能继续在后台积累数十秒音频。
- 修改 ASR 文字稿不应重写已经播放的音频；只影响尚未提交的发言文本或后续回合。
- 结束比赛/退出比赛：立即取消 TTS、撤销 track publisher、清空 jitter/buffer，并完成服务端资源回收。
- WebSocket PCM/AudioWorklet 可以作为诊断对照；正式 GO 报告必须来自 WebRTC，不得用它替代。

## 7. 首字到浏览器首声的 2.5 秒硬门

### 7.1 统一时钟

全部事件使用浏览器单调时钟或经校准的同一时间轴：

- `T0_llm_char`：回复区域第一次实际渲染非空 LLM 字符。
- `T1_tts_prefill`：TTS 收到足以启动 prefill 的稳定 token。
- `T2_first_pcm`：TTS endpoint 产出第一段连续非静音 PCM。
- `T3_first_webrtc_packet`：媒体层发送该 generation 的首个 Opus RTP 包。
- `T4_audible`：浏览器接收 track 的实际播放能量首次连续 40 ms 高于噪声门（建议 -50 dBFS），并用系统 loopback 录音或 WebRTC/audio energy 交叉确认。

唯一用户硬指标：`T4_audible - T0_llm_char`。

### 7.2 预算与发布阈值

| 段 | P95 目标 | 单样本硬上限 |
|---|---:|---:|
| 首字到 12 个稳定 TTS token/soft flush | 650 ms | 1,000 ms |
| TTS prefill 到首个非静音 PCM | 400 ms | 700 ms |
| PCM 到 WebRTC 浏览器实际首声 | 300 ms | 500 ms |
| 端到端 `T0 -> T4` | **≤2,000 ms** | **≤2,500 ms** |

测试样本：

- Chrome 单路 30 次、三路并发 30 次。
- Safari 单路 30 次、三路并发 30 次。
- 不删除慢样本，不排除第一波；服务 readiness 必须保证 compile/warmup 已完成。
- 服务冷启动期间不接比赛流量；重启后 readiness=false，完成全部固定音色 warmup 后才转 true。
- 任意有效样本 `T0 -> T4 > 2.5 s`、无声音、首块全静音、播放被 autoplay 拒绝，均为该轮失败。

## 8. 连续实时性能与 30 分钟稳定性

三路稳态并发门：

- TTS RTF P95 ≤0.65，P99 ≤0.80。
- endpoint 首 PCM P95 ≤500 ms，max ≤700 ms（从达到 prefill 起算）。
- PCM 生成块间隔 P99 ≤160 ms；单次 gap >250 ms 数量为 0。
- WebRTC 接收端 30 分钟 underrun、audible click/pop、非语义停顿、音频倒序、重复块均为 0。
- 浏览器 jitter buffer 不得持续增长；发送速度不得长期快于实时而积累数十秒不可取消音频。
- 3 场 × 至少 20 个 AI 回合；加入 10% pause/resume、10% early cancel、5% endpoint error 注入。
- 单 endpoint 故障不能污染另外两场的音色、文本、音频或会话状态。

## 9. 中文正确性、吞词和音色一致性

### 9.1 评测集

至少包含：

- 200 条普通话辩论语句：论点、反驳、数据、专有名词、中英混排。
- 50 条 1–8 字短句，必须包含“其实。”、“因此。”、“不对。”等 Nano 已暴露风险的输入。
- 30 条 100–250 字长发言。
- 30 条标点/格式陷阱：引号、书名号、破折号、省略号、括号、百分数、日期、英文缩写。
- 8 个固定辩位音色；每个音色覆盖首轮、中段、末轮和三路并发。
- LLM delta 形态包含 1–4 字碎片、50–600 ms 不规则间隔、无标点长串和 final 修正。

### 9.2 自动质量硬门

- 全集中文 CER ≤2.0%。
- 删除型错误率 ≤0.5%；末词/末句删除为 0。
- 短句/标点陷阱集：吞句、重复整句、循环念词、生成与输入无关内容均为 0。
- 任一单句 CER >10% 即人工复核；确认是模型错误则本轮 NO-GO。
- 使用与官方 Realtime 报告一致或可对齐的 speaker evaluator：ZH SIM 平均 ≥75%，P5 ≥70%。
- 同一辩位跨轮 speaker embedding 标准差 ≤0.03；同一长发言首/中/末段距离差 ≤0.05。
- 不同辩位不能串音；请求 A 的声音/文本出现在请求 B 中次数为 0。

### 9.3 主观听感硬门

- 至少 3 名听者盲听 100 条样本。
- 音色突变、拼接跳调、异常加速/减速、机械重复、爆音、明显断裂属于严重缺陷；严重缺陷率必须为 0。
- 自然度 MOS 平均 ≥4.0/5；任一固定辩位平均 <3.8 即失败。

Realtime 官方基线是中文 CER 1.07%、SIM 76.7，可作为合理性参考，但项目仍必须在自己的辩论语料、WebRTC 和三路并发条件下重测。Nano 没有公开 TTS CER/SIM，因此不能用“小模型、速度快”替代质量证据。

## 10. Nano 参数 canary 与切换规则

Nano 只允许三组有界实验，禁止无限调参：

1. 官方默认采样基线。
2. 官方 issue 建议的防截断组合：根据文本长度提高 `max_new_frames`，并降低每个 voice-clone chunk 的 token 上限。
3. 针对短句重复的保守采样 A/B：减小 audio temperature/top-p、适度提高 repetition penalty；这是项目实验假设，不是官方保证。

注意：

- ONNX `fixed` sampling 的参数固化在导出图中，UI 改参数不会改变固定图；如需调整必须重导 ONNX 并记录模型哈希。
- 提高 `max_new_frames` 可能修复末尾截断，也可能增加异常重复时长和取消成本；必须同时通过 2.5 s、RTF、重复和资源回收门。
- 把极短句合并到 8–16 个中文字可作为 canary 缓解，但如果因此等不到 LLM token、超过首声硬门，仍然失败。
- rolling prompt 可能减轻跨片段音色跳变，但会增加编码、延迟和错误传播；不能把它视为真实增量 session。

Nano 的正式 GO 条件是本文全部硬门，包括“final 前同一 session 增量输入”。按固定官方源码，该条件目前不成立；所以 Nano 的预设结论是 **NO-GO，除非后续官方或本项目新增并验证真正的 push-text session**。

## 11. 取消、暂停与资源回收硬门

每个 generation 必须有唯一 `generation_id/epoch`，取消链路从浏览器一直贯穿 TTS endpoint：

| 项目 | 硬门 |
|---|---:|
| 用户点击暂停/结束/退出到浏览器静音 | P95 ≤150 ms，max ≤250 ms |
| API 发出 cancel 到 TTS worker 停止生成 | P95 ≤300 ms，max ≤500 ms |
| endpoint active_turns 回到 0 | ≤1 s |
| GPU allocated/reserved memory 回到基线 | 5 s 内，偏差 ≤256 MiB 或 ≤5%（取更严格者） |
| 临时 WAV、generator、thread、queue、codec context | 全部释放；残留数 0 |
| 取消确认 | 必须有结构化 ACK；超时则 endpoint fail-fast 摘除/重启 |

回收压力测试：

- 1,000 次 start→首 PCM 前取消。
- 1,000 次首声后 0.2–2 s 随机取消。
- 300 次三路同时取消并立即开始下一回合。
- 结束后进程 RSS、GPU memory、线程数、文件数和 session registry 不得单调增长；增长 >2% 或存在 orphan 即 NO-GO。

固定版 OpenMOSS FastAPI 的 `/close` 没有 join/ACK，不能直接通过此门。正式 wrapper 必须等待 worker 真正退出 codec context；若同步 GPU 调用在 grace 内不能取消，应杀死并重建该独立 endpoint，而不是把疑似污染的实例继续分配给下一场。

## 12. GO / NO-GO 决策

### Nano

| 阶段 | 结论 |
|---|---|
| GPU 安装、单路音频输出、vLLM 1/2/3 路吞吐 | 可做 canary |
| 正式实时增量输入 | 当前官方实现不满足 |
| CER/SIM 证据 | 官方未发布；必须项目自测 |
| 开放质量风险 | 吞句、短句重复、语速不均、标点丢句、分片音色跳变 |
| 当前正式结论 | **NO-GO** |

只要 Nano 出现以下任一项，立即停止 Nano 主线并切 MOSS-Realtime：

- 任意 Chrome/Safari 三路样本首字到首声 >2.5 s；
- Agent final 前没有浏览器首声；
- 任意吞句、末句截断、短句循环或跨请求串音；
- CER >2%、ZH SIM <75%、音色漂移超门；
- 三路 RTF/块间隔/播放连续性不达标；
- cancel 后 generator/显存/临时文件不回收。

### MOSS-TTS-Realtime

Realtime 只有在以下条件全部满足时 GO：

1. 独立 GPU endpoint 完成 BF16 + SDPA + compile warmup，readiness 正确。
2. 原生 push-text session 在 Agent final 前产生并通过 WebRTC 播放音频。
3. Chrome/Safari 单路和三路的 `T0 -> T4` max 均 ≤2.5 s。
4. 三路使用隔离 endpoint，无线程安全、codec context 或 RNG 共享。
5. CER、吞词、重复、SIM、跨轮音色、30 分钟连续播放全部通过。
6. 取消/暂停/退出有 ACK，资源回收压力测试通过。

若 24 GB canary OOM、RTF 超门或 compile 后余量不足，直接升级 48 GB L20/L40S/A100 级别，不在正式卡上通过降质量、缩 codec 或无证据量化强行塞入。

## 13. 官方风险证据

Nano 官方仓库：

- [Issue #4：尚无公开 WER/SIM，维护者表示仍在评测](https://github.com/OpenMOSS/MOSS-TTS-Nano/issues/4)
- [Issue #58：ONNX 吞字吞句严重；维护者建议提高 max frames/减小 chunk，但未给出生产正确性保证](https://github.com/OpenMOSS/MOSS-TTS-Nano/issues/58)
- [Issue #60：短文本容易重复](https://github.com/OpenMOSS/MOSS-TTS-Nano/issues/60)
- [Issue #65：LLM 分片输入时音色不一致、衔接跳调](https://github.com/OpenMOSS/MOSS-TTS-Nano/issues/65)
- [Issue #81：短句重复和语速不均](https://github.com/OpenMOSS/MOSS-TTS-Nano/issues/81)
- [Issue #87：标点处丢句](https://github.com/OpenMOSS/MOSS-TTS-Nano/issues/87)

MOSS-TTS 官方仓库：

- [Issue #38：FA2 与 StaticCache/compile 的兼容性说明](https://github.com/OpenMOSS/MOSS-TTS/issues/38)
- [Issue #46：维护者确认 Realtime 可在完整文本得到前按 chunk 输入并先生成语音](https://github.com/OpenMOSS/MOSS-TTS/issues/46)
- [Issue #72：单实例并发出现音色失真；维护者称线程安全版本尚待发布](https://github.com/OpenMOSS/MOSS-TTS/issues/72)
- [Issue #74：warmup 后约 180 ms TTFB、0.5 RTF；新音色/无 warmup 会更慢](https://github.com/OpenMOSS/MOSS-TTS/issues/74)

## 14. 下一步执行顺序（需要独立 GPU 后）

1. 在 L4 24 GB 上部署隔离 Nano vLLM canary，只做 1/2/3 路、质量和回收验证；不接正式比赛流量。
2. 同时在 L20 48 GB 上部署原生 MOSS-Realtime 单 endpoint，固定一个音色完成 SDPA+compile warmup和 WebRTC 单路门。
3. 为 8 个固定辩位预编码 prompt tokens，并逐音色 warmup。
4. 扩成 3 个隔离 Realtime endpoint，执行 Chrome/Safari 三路、30 分钟和取消压力测试。
5. Nano 任一架构/质量门失败即停止；Realtime 全门通过后才允许灰度，现有 WS PCM 路径只作回滚/诊断。

当前只读审计最终判定：**Nano GPU canary 值得做，但不能阻塞核心路线；正式主线应直接以原生 MOSS-TTS-Realtime + 隔离 GPU endpoint + WebRTC 为目标。**
