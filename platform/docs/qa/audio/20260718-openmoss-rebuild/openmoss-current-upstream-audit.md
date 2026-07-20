# OpenMOSS 当前上游审计：实时 TTS、增量会话、取消与并发

审计日期：2026-07-18（Asia/Shanghai）  
范围：只读审计 OpenMOSS 官方 GitHub 仓库，以及其 README 明确指向的 vLLM-Omni serving 实现；未修改本地业务代码、未部署模型。  
方法：使用 GitHub CLI 固定 HEAD，阅读固定提交中的 README、模型卡、流式 session、FastAPI/Gradio serving、ONNX/TensorRT 后端，并核查当前开放 issue/PR。

## 结论先行

1. **主方案应选 MOSS-TTS-Realtime，不应以 MOSS-TTS-Nano 作为正式赛主链路。** Realtime 模型有真正的增量文本 session：`push_text(delta) -> audio frames`，内置标点/短语聚合、12-token prefill、多轮 KV cache 复用；官方单 L20、SDPA + `torch.compile` 的暖机结果为 TTFB 180 ms、RTF 0.51。它也明确支持中文和音色 prompt。
2. **OpenMOSS 主仓自带 `fast_api.py` 只能作为参考实现，不能直接视为生产 serving。** 它使用 HTTP `start/push/audio/close`，不是持久 WebSocket；`push` 绕过 session 的短语聚合；`close` 只是把 `shutdown` 排到命令队列并立即返回，不能抢占正在生成的 turn，也不会清空已排队 PCM；多个 session 线程共享同一个 model 和 codec，但没有全局 GPU/codec 调度锁，线程安全没有上游保证。
3. **vLLM-Omni 是目前更可信的 GPU serving 基线。** 当前配置明确给 MOSS-TTS-Realtime 使用异步调度、共享内存 codec streaming，在 A10G 24GB 上记录约 6GB talker + 8GB codec、约 180 ms 首音频；但标准 `/v1/audio/speech` 请求仍是“完整 input、增量音频输出”，尚不能据此认定它原生接受 Agent 文本 delta。要实现所需链路，仍需在服务侧保留一个可增量更新的 request/session 层，或验证 vLLM-Omni 的 streaming-input API 后再接入。
4. **Nano 当前更适合轻量演示、离线/浏览器朗读或质量降级 canary。** 官方 PyTorch runtime 用一把 `RLock` 包住整个流式推理，实际串行；当前 Web 实时任务还写死走 CPU。Nano 不提供 `push_text(delta)` 式文本会话，而是一次接收完整文本后分块。最近仍开放的 issue 覆盖吞字/丢句、短句重复、音色错乱、语速漂移、分块音色不一致和卡顿。
5. **2～3 场并发不能仅凭上游参数宣称通过。** vLLM-Omni Realtime 配置的 talker `max_num_seqs=8`，但 codec stage 是 `max_num_seqs=1`；这说明它具备排队/调度框架，却仍可能在 codec 阶段形成串行瓶颈。必须用三房间中文长回合实测首包、RTF、排队延迟、吞字、重复、音色漂移与 cancel barrier。

## 固定快照

三个核心 OpenMOSS 仓库目前都没有 GitHub Release 或 tag，因此部署和复现实验必须固定 commit SHA，不能只固定 `main`。

| 仓库 | 默认分支 | 固定 HEAD | HEAD 时间 | GitHub Release/tag |
|---|---|---|---|---|
| [OpenMOSS/MOSS-TTS](https://github.com/OpenMOSS/MOSS-TTS) | `main` | [`ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af`](https://github.com/OpenMOSS/MOSS-TTS/commit/ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af) | 2026-06-22 | 无 |
| [OpenMOSS/MOSS-TTS-Nano](https://github.com/OpenMOSS/MOSS-TTS-Nano) | `main` | [`11619374849c649486584e3b10ed55b176a924ee`](https://github.com/OpenMOSS/MOSS-TTS-Nano/commit/11619374849c649486584e3b10ed55b176a924ee) | 2026-07-14 | 无 |
| [OpenMOSS/MOSS-Audio-Tokenizer](https://github.com/OpenMOSS/MOSS-Audio-Tokenizer) | `main` | [`8c50ac4c5d7287d2ed6ea20a08c90ca439887d23`](https://github.com/OpenMOSS/MOSS-Audio-Tokenizer/commit/8c50ac4c5d7287d2ed6ea20a08c90ca439887d23) | 2026-06-16 | 无 |
| [vllm-project/vllm-omni](https://github.com/vllm-project/vllm-omni)（OpenMOSS README 推荐的外部 serving） | `main` | [`d09f549e862c58ca87c195d4050e77b20a126a38`](https://github.com/vllm-project/vllm-omni/commit/d09f549e862c58ca87c195d4050e77b20a126a38) | 2026-07-17 | 本报告只固定 commit |

## 需求矩阵

| 能力 | MOSS-TTS-Realtime 原生库 | OpenMOSS `fast_api.py` | MOSS-TTS-Nano 当前主仓 | vLLM-Omni 当前实现 |
|---|---|---|---|---|
| Agent 文本 delta 增量输入 | **有**，`session.push_text(delta)` | 有 `push`，但先自行 token 化并调用 `push_text_tokens` | **无原生会话**，一次接收完整 text | 标准 speech API 未证明支持文本 delta；输出可流式 |
| 稳定逗号/短语首块 | **有**，默认最少 8 字，标点切分，32 字缓冲 | **绕过了该逻辑** | 完整文本后按 token budget 分块 | 需在调用层实现/验证 streaming input |
| 正文/Thinking 分离 | 不识别 LLM 语义，调用方职责 | 调用方职责 | 调用方职责 | 调用方职责 |
| 连续音频流 | 24k PCM waveform chunks | HTTP chunked raw PCM | 48k 双声道 PCM chunks | `/v1/audio/speech` 流式音频、codec streaming |
| 持久 WS | 无 | 无 | 无；多 HTTP endpoint | 框架有流式/中止基础设施，但 MOSS speech recipe 使用 HTTP API |
| 可抢占 cancel | **无显式 cancel API** | **不满足**，`shutdown` 排队 | close 仅标记 job 并结束消费，非模型级 cancel token | 框架有 request abort 路径；MOSS delta-session 级语义仍需验证 |
| 房间预热 | 模型/codec 可常驻，prompt token 可缓存 | lifespan 只加载 backend；prompt 首次进入才编码 | 有启动 warmup | 模型/codec 常驻 serving |
| 普通话 | **支持**，20 语言含中文 | 同模型 | 支持，20 语言含中文 | 同模型 |
| 多音色 | reference audio / prompt tokens | prompt audio token cache | voice clone / voice presets | `ref_audio` / voice profile |
| 2～3 并发 | 上游无线程安全保证 | 共享 model/codec，多线程，风险高 | runtime 串行 | 有 scheduler；仍需实测 codec stage 排队 |

## MOSS-TTS-Realtime：可直接复用的核心

### 1. 原生增量 session 与短语聚合

官方模型卡明确展示 LLM `text_deltas` 逐块进入 `session.push_text(delta)`，随后增量解码音频，结束时执行 `end_text -> drain -> decoder.flush`。[模型卡固定链接](https://github.com/OpenMOSS/MOSS-TTS/blob/ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af/docs/moss_tts_realtime_model_card.md#L182-L217)

`MossTTSRealtimeStreamingSession` 的默认策略与当前目标高度一致：

- 句末和停顿符号包括 `。！？!?…`、中英文逗号、分号、冒号、破折号、换行等；
- 默认 `min_text_chunk_chars=8`、`text_buffer_size=32`、`prefill_text_len=12`；
- 找到稳定标点后才 tokenize，未找到标点且达到缓冲长度时才退化到空格切分；
- prefill 完成后，后续 token 逐步推进生成。

证据：[session 切分与默认参数](https://github.com/OpenMOSS/MOSS-TTS/blob/ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af/moss_tts_realtime/mossttsrealtime/streaming_mossttsrealtime.py#L476-L528)、[`push_text/end_text`](https://github.com/OpenMOSS/MOSS-TTS/blob/ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af/moss_tts_realtime/mossttsrealtime/streaming_mossttsrealtime.py#L618-L647)、[切分与 prefill](https://github.com/OpenMOSS/MOSS-TTS/blob/ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af/moss_tts_realtime/mossttsrealtime/streaming_mossttsrealtime.py#L649-L709)。

可行动结论：

- 第一段可直接以“稳定标点 + 最少字符数”为基础，业务层再增加中文特殊规则；
- 第一段不应等完整句，推荐以约 8～16 个汉字或稳定逗号为首个 commit；
- 后续块可逐渐放大到 16～32 个汉字，以降低 tokenizer/GPU 调度开销；
- SSE 解析层必须只转发最终正文 delta，`reasoning_content`、`thinking`、工具调用和隐藏草稿不得进入该 session。

### 2. 首包、RTF、语言与音色

官方结果是在单张 L20、完成暖机、开启 SDPA + `torch.compile` 后得到：TTFB 180 ms、RTF 0.51；另以 vLLM 部署 Qwen3.5-9B，12-token 首段为 197 ms，合计 377 ms。[官方性能表](https://github.com/OpenMOSS/MOSS-TTS/blob/ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af/docs/moss_tts_realtime_model_card.md#L49-L58)

该模型卡列出 20 种语言，包括中文；并宣称基于参考音频的高保真音色克隆和多轮音色一致性。[语言与音色能力](https://github.com/OpenMOSS/MOSS-TTS/blob/ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af/docs/moss_tts_realtime_model_card.md#L10-L21)、[20 语言表](https://github.com/OpenMOSS/MOSS-TTS/blob/ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af/docs/moss_tts_realtime_model_card.md#L35-L47)

注意：这些是上游单模型/单卡指标，不包含 phdebate 的 SSE 聚合、网络、WebRTC、浏览器 jitter buffer 和 AudioWorklet 调度。因此验收仍应按“LLM 第一个正文字符出现 -> 浏览器实际出声”计时，目标不超过 2.5 秒。

### 3. 多轮上下文

原生 session 的 `reset_turn(reset_cache=False)` 会保留 inferencer KV cache；官方多轮示例也明确 turn 0 清缓存、turn 1+ 复用缓存。[多轮说明](https://github.com/OpenMOSS/MOSS-TTS/blob/ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af/docs/moss_tts_realtime_model_card.md#L220-L230)、[session reset](https://github.com/OpenMOSS/MOSS-TTS/blob/ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af/moss_tts_realtime/mossttsrealtime/streaming_mossttsrealtime.py#L587-L616)

但主仓 `fast_api.py` 在每个 turn 都重新创建 inferencer，并传 `reset_cache=True`，因此它并没有把官方多轮 KV 复用能力完整暴露出来。[FastAPI turn 初始化](https://github.com/OpenMOSS/MOSS-TTS/blob/ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af/moss_tts_realtime/fast_api.py#L667-L687)、[reset_cache=True](https://github.com/OpenMOSS/MOSS-TTS/blob/ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af/moss_tts_realtime/fast_api.py#L729-L776)

## OpenMOSS `fast_api.py`：不能直接用于正式赛的原因

### 1. 不是持久 WebSocket

服务协议是四组 HTTP endpoint：`/tts/session/start`、`/push`、`/{id}/audio`、`/close`；音频是 `StreamingResponse` 输出 `pcm_s16le`。[HTTP API](https://github.com/OpenMOSS/MOSS-TTS/blob/ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af/moss_tts_realtime/fast_api.py#L885-L990)

这可以作为服务内原型，但不满足目标中的“持久 WS 控制/文本链路”。正式音频面向浏览器仍应使用 WebRTC；如果保留 TTS WS，它更适合承载 session 控制、正文 delta、状态和 cancel ACK，而不是替代 WebRTC 音频传输。

### 2. FastAPI `push` 绕过短语聚合

`_handle_push_text` 直接 `tokenizer.encode(text)`，再调用 `push_text_tokens`，没有走 `session.push_text` 的标点/短语缓存。[FastAPI push 实现](https://github.com/OpenMOSS/MOSS-TTS/blob/ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af/moss_tts_realtime/fast_api.py#L795-L811)

因此不能把任意 1 字 SSE delta 原样 POST 给该服务，否则会增加碎片调度，且首段稳定性完全依赖上游调用方。phdebate 必须在 Agent SSE 和 TTS 之间保留独立 phrase aggregator。

### 3. close 不是可抢占 cancel

`close` 只把 `shutdown` 写入 session 命令队列、从 manager 删除 session，然后立即返回“session closed”。如果 worker 正在 `finish_turn` 的逐步 drain 循环中，`shutdown` 要等当前命令完成后才会被读取；代码也没有 abort flag、generation id barrier、GPU request abort、codec clear 或 PCM queue drain。[worker 命令循环](https://github.com/OpenMOSS/MOSS-TTS/blob/ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af/moss_tts_realtime/fast_api.py#L552-L583)、[finish drain](https://github.com/OpenMOSS/MOSS-TTS/blob/ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af/moss_tts_realtime/fast_api.py#L813-L847)、[close](https://github.com/OpenMOSS/MOSS-TTS/blob/ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af/moss_tts_realtime/fast_api.py#L981-L990)

所以目标中的完整打断必须由业务 serving 新增：

1. 每轮绑定 `generation_id`；
2. interrupt 后立即拒绝旧 generation 的新 delta；
3. 抢占/终止模型 scheduler request；
4. 清服务端未发送 PCM/Opus 队列；
5. 关闭或重置该轮 codec streaming state；
6. 浏览器收到 cancel ACK 后执行 AudioWorklet flush；
7. 只有在旧 generation 不再可能产出音频后才返回 `cancel_ack`；
8. 只把已实际播放/确认提交的文本写入对话上下文，未播放尾部剔除。

### 4. 当前多线程共享 model/codec 的线程安全未被证明

backend 使用 `lru_cache(maxsize=1)`，所有 session worker 取得同一 model、processor 和 codec；每个 session 又启动独立线程，并可能同时进入同一个 codec 对象的 `streaming(batch_size=1)` context。[共享 backend](https://github.com/OpenMOSS/MOSS-TTS/blob/ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af/moss_tts_realtime/fast_api.py#L271-L304)、[每 session 线程](https://github.com/OpenMOSS/MOSS-TTS/blob/ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af/moss_tts_realtime/fast_api.py#L516-L560)、[共享 codec streaming context](https://github.com/OpenMOSS/MOSS-TTS/blob/ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af/moss_tts_realtime/fast_api.py#L708-L721)

上游模型卡同时写明 FastAPI 当前只支持 batch size 1；Gradio demo 的默认并发限制也是 1。[batch size 1](https://github.com/OpenMOSS/MOSS-TTS/blob/ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af/docs/moss_tts_realtime_model_card.md#L169-L180)、[Gradio concurrency=1](https://github.com/OpenMOSS/MOSS-TTS/blob/ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af/moss_tts_realtime/app.py#L1360-L1377)

这是源码推断而非上游已确认 bug：**不能假定同一 model/codec 被多个 Python 线程同时驱动是安全的。** 正式 serving 应由单一 GPU scheduler 调度多个逻辑 session，或为每个可并发 codec session提供严格隔离的状态；不要直接复制“每房间一个线程，共享同一 codec”的做法。

### 5. 暖机范围不完整

FastAPI lifespan 会加载并缓存 model/codec，参考音频编码也有最多 8 项缓存。[backend 暖机](https://github.com/OpenMOSS/MOSS-TTS/blob/ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af/moss_tts_realtime/fast_api.py#L324-L425)

但它没有在房间进入时创建会话、建立长连接、预编码该房间固定音色、执行首轮 compile/decoder warmup。目标实现应在进入房间后完成这些动作，并把 prompt tokens 按 `voice_id + audio hash + model SHA` 缓存。

## vLLM-Omni serving：更适合作为 GPU 基线，但仍需补 delta session

MOSS-TTS-Nano README 仍链接旧的 `examples/online_serving/moss_tts_nano/README.md` 路径；该路径在 2026-07-18 的 vLLM-Omni HEAD 已不存在，当前入口迁移到了 `examples/online_serving/text_to_speech/moss_tts_nano/` 和 `vllm_omni/deploy/*.yaml`。因此集成时必须固定 vLLM-Omni commit，不能照抄旧链接。

当前 vLLM-Omni MOSS-TTS-Realtime 配置具有：

- `async_chunk: true`；
- talker stage `max_num_seqs=8`、异步调度；
- codec streaming 通过 SharedMemoryConnector 连接；
- codec chunk 为 15 frames；
- codec stage `max_num_seqs=1`。

证据：[Realtime deploy config](https://github.com/vllm-project/vllm-omni/blob/d09f549e862c58ca87c195d4050e77b20a126a38/vllm_omni/deploy/moss_tts_realtime.yaml)

当前 recipe 给出 A10G 24GB 部署：talker 峰值约 6GB、codec decoder 约 8GB，首音频约 180 ms，并通过 OpenAI-compatible `/v1/audio/speech` 流式输出。[MOSS recipe](https://github.com/vllm-project/vllm-omni/blob/d09f549e862c58ca87c195d4050e77b20a126a38/recipes/OpenMOSS/MOSS-TTS.md#L82-L116)

行动建议：

- GPU 生产 serving 优先从该实现做 3-session 压测，而不是从 OpenMOSS `fast_api.py` 的共享多线程模型起步；
- 不要把 `max_num_seqs=8` 等同于 8 路稳定音频并发，codec stage 仍是 1；
- 标准 speech endpoint 的 `input` 是完整文本。只有在验证其 streaming-input/request-update 对 MOSS-TTS-Realtime 可用后，才能把 Agent delta 直接接入；否则需要在 vLLM 前增加一个会话层，或为 MOSS Realtime 实现专用的增量 request adapter；
- cancel 要落到 vLLM request abort，并以 generation barrier 包裹，而不仅是断开客户端响应流。

## MOSS-TTS-Nano：当前不适合作为正式赛主方案

### 1. 模型能力与加速路径

Nano 是 0.1B 自回归 TTS，输出 48kHz 双声道，支持中文在内的 20 种语言；对应 Audio Tokenizer Nano 约 20M 参数。官方同时支持 PyTorch CUDA、ONNX Runtime CPU 和 ONNX CUDA。[Nano 特性](https://github.com/OpenMOSS/MOSS-TTS-Nano/blob/11619374849c649486584e3b10ed55b176a924ee/README_zh.md#L84-L110)、[ONNX CUDA](https://github.com/OpenMOSS/MOSS-TTS-Nano/blob/11619374849c649486584e3b10ed55b176a924ee/README_zh.md#L178-L250)

其 PyTorch runtime 可选择 FlashAttention 2/SDPA，并为 CUDA 浏览器播放实现了动态 decode frame budget：低 lead 时从 4、6、8 到 12 frames 逐级调整，而不是固定大缓冲。[动态 decode budget](https://github.com/OpenMOSS/MOSS-TTS-Nano/blob/11619374849c649486584e3b10ed55b176a924ee/moss_tts_nano_runtime.py#L235-L259)

这部分思路值得借鉴到浏览器连续时钟队列：初始 80～120 ms 灰度，按 underrun/lead 自适应增加或下降，不要固定 400 ms。

### 2. 不是真正的文本 delta session

`synthesize_stream` 的输入是完整 `text`，之后由 `model.inference_stream` 输出音频事件；没有 `push_text(delta)`、`end_text` 或可持续追加正文的 session API。[Nano stream API](https://github.com/OpenMOSS/MOSS-TTS-Nano/blob/11619374849c649486584e3b10ed55b176a924ee/moss_tts_nano_runtime.py#L608-L707)

这也是开放 issue [#65 大模型流式回答时分块音色不一致](https://github.com/OpenMOSS/MOSS-TTS-Nano/issues/65) 的实际背景。维护者建议稳定 prompt，或把上一块尾部音频滚动作为下一块 prompt；这是一种补偿策略，不等价于原生增量上下文。

### 3. 当前 runtime 串行化，Web 实时路径还强制 CPU

`NanoTTSService.synthesize_stream` 在整个 generator 生命周期内持有实例 `RLock`，因此同一 runtime 上的请求是串行的。[全程 RLock](https://github.com/OpenMOSS/MOSS-TTS-Nano/blob/11619374849c649486584e3b10ed55b176a924ee/moss_tts_nano_runtime.py#L145-L162)、[stream 持锁](https://github.com/OpenMOSS/MOSS-TTS-Nano/blob/11619374849c649486584e3b10ed55b176a924ee/moss_tts_nano_runtime.py#L661-L707)

当前 `app.py` 的实时 job 又明确以 `requested_execution_device="cpu"` 调用 runtime manager，且 CPU 执行有独占 `_cpu_execution_lock`。[CPU 独占锁](https://github.com/OpenMOSS/MOSS-TTS-Nano/blob/11619374849c649486584e3b10ed55b176a924ee/app.py#L248-L352)、[实时任务写死 CPU](https://github.com/OpenMOSS/MOSS-TTS-Nano/blob/11619374849c649486584e3b10ed55b176a924ee/app.py#L2284-L2344)

仍开放的 [PR #34](https://github.com/OpenMOSS/MOSS-TTS-Nano/pull/34) 正在尝试给 Web UI 增加 GPU 选择；[PR #41](https://github.com/OpenMOSS/MOSS-TTS-Nano/pull/41) 同时尝试 OpenAI speech API、GPU auto-detect，以及修复客户端断开时的锁/队列问题。两者截至审计日均未合并。这进一步说明主仓当前 Web serving 不应直接承担正式赛 2～3 房间低延迟并发。

vLLM-Omni 的 Nano 配置虽然标注在单 L4 24GB 验证、约 2GiB、`max_num_seqs=4`，但 `async_chunk` 仍是 false，且使用上游 `inference_stream()`、未接 CUDA Graph。[Nano deploy config](https://github.com/vllm-project/vllm-omni/blob/d09f549e862c58ca87c195d4050e77b20a126a38/vllm_omni/deploy/moss_tts_nano.yaml)

### 4. close 不是完整取消屏障

Nano Web close 会设置 `job.is_closed=True` 并尝试向 audio queue 放入结束标记；后台循环只在收到下一个模型 event 后检查 closed 并 break。[job close](https://github.com/OpenMOSS/MOSS-TTS-Nano/blob/11619374849c649486584e3b10ed55b176a924ee/app.py#L416-L444)、[event 后检查](https://github.com/OpenMOSS/MOSS-TTS-Nano/blob/11619374849c649486584e3b10ed55b176a924ee/app.py#L2336-L2344)、[HTTP close](https://github.com/OpenMOSS/MOSS-TTS-Nano/blob/11619374849c649486584e3b10ed55b176a924ee/app.py#L2697-L2710)

它没有把 cancel token 传进 `model.inference_stream`，也没有 generation id、服务端旧 PCM 清空确认和浏览器 flush ACK。因此仍不满足全链路打断要求。

### 5. 当前开放质量问题

以下问题截至 2026-07-18 仍为 OPEN，不能当作已修复：

- [#87 会出现丢句现象](https://github.com/OpenMOSS/MOSS-TTS-Nano/issues/87)，2026-07-10，新问题，报告在引号、破折号等标点处丢句；
- [#88 根据上传音色制作语音错乱](https://github.com/OpenMOSS/MOSS-TTS-Nano/issues/88)，2026-07-10，报告参考音色输出完全不对；
- [#81 语速不均、短句重复](https://github.com/OpenMOSS/MOSS-TTS-Nano/issues/81)，报告“其实”生成约 17 秒并重复多次；
- [#60 ONNX 短文本容易重复](https://github.com/OpenMOSS/MOSS-TTS-Nano/issues/60)，报告“你好”偶发十几秒；维护者建议加标点、调参或重试；
- [#58 ONNX 吞字吞句严重](https://github.com/OpenMOSS/MOSS-TTS-Nano/issues/58)，维护者将部分结尾截断归因于 `Max New Frames`，建议从 375 提高到 600、缩短文本块；
- [#65 LLM 分块播报音色不一致](https://github.com/OpenMOSS/MOSS-TTS-Nano/issues/65)，报告块间音色和音调跳变；
- [#21 CPU 流式卡顿](https://github.com/OpenMOSS/MOSS-TTS-Nano/issues/21)，用户报告中端 CPU lead 为负，另有用户报告 3090 短文本 RTF > 1；
- [#74 显存高且实时输出断续](https://github.com/OpenMOSS/MOSS-TTS-Nano/issues/74)；
- [#77 中文生僻字读错](https://github.com/OpenMOSS/MOSS-TTS-Nano/issues/77)，维护者确认小模型对生僻字鲁棒性较差，并表示后续版本考虑拼音输入。

上述 issue 是用户报告，不等同于统一基准；但数量、时间新鲜度和问题类型已经足以支持一个工程决策：**Nano 必须先通过自动 CER/吞字/重复/音色稳定性门禁，才可从 canary 升级为正式主链路。**

## MOSS-Audio-Tokenizer：延迟下限与部署注意点

官方 tokenizer 是 1.6B、24kHz mono、12.5Hz token frame、32 层 RVQ；Nano tokenizer 约 20M、48kHz stereo。仓库支持 PyTorch streaming，`chunk_duration=0.08`，且 streaming chunking 只支持 batch size 1。[模型与后端](https://github.com/OpenMOSS/MOSS-Audio-Tokenizer/blob/8c50ac4c5d7287d2ed6ea20a08c90ca439887d23/README.md#L68-L89)、[streaming usage](https://github.com/OpenMOSS/MOSS-Audio-Tokenizer/blob/8c50ac4c5d7287d2ed6ea20a08c90ca439887d23/README.md#L174-L219)

开放 issue [#6](https://github.com/OpenMOSS/MOSS-Audio-Tokenizer/issues/6) 中，维护者给出的理论最小算法延迟是一帧，即 80 ms；模型严格因果，不依赖未来帧，每个 causal transformer block 的历史上下文最长约 10 秒。

仓库还提供 ONNX Runtime CUDA/TensorRT 和原生 TensorRT engine 路径；TensorRT 构建脚本明确建议 Ampere+ 默认 TF32，以平衡速度和音质，并提醒 FP16 可能产生音频 artifact。[ONNX/TensorRT 后端](https://github.com/OpenMOSS/MOSS-Audio-Tokenizer/blob/8c50ac4c5d7287d2ed6ea20a08c90ca439887d23/README.md#L221-L274)、[TensorRT 构建说明](https://github.com/OpenMOSS/MOSS-Audio-Tokenizer/blob/8c50ac4c5d7287d2ed6ea20a08c90ca439887d23/trt/build_engine.sh)

行动结论：

- 80～120 ms 初始浏览器缓冲有上游算法帧长依据；
- 不要用固定 400 ms 作为唯一策略，应记录 underrun、audio lead 和 chunk arrival jitter，自适应到 80/120/160/240 ms 等档位；
- TensorRT/低精度的任何加速都必须和 CER、块间静音、音量一致性、爆音/毛刺一起验收，不能只看 RTF；
- batch size 1 是 codec API 约束之一，2～3 房间并发必须由 scheduler 排队或多 codec state 隔离处理。

## 推荐落地架构

```text
Agent SSE
  -> 仅提取正文 content delta
  -> generation_id + phrase aggregator
  -> 持久控制通道（WS）
  -> GPU TTS scheduler
       -> 每房间独立 MOSS-TTS-Realtime session/KV/text buffer
       -> 每轮独立 codec streaming state
       -> PCM frame queue
  -> WebRTC audio track（正式链路）
  -> 浏览器连续时钟 jitter queue
  -> AudioWorklet
```

关键约束：

- **模型选择**：MOSS-TTS-Realtime 为 primary；Nano 只做 A/B、低资源 fallback 或 canary。
- **GPU**：先以 24GB A10G/L4/3090 级别做单卡 3-session 验收；vLLM-Omni 当前数据表明 Realtime 约 14GB 峰值有空间，但必须实测。
- **调度**：一个 GPU scheduler 管理多个逻辑 session，避免多个 Python 线程同时驱动共享 codec；若继续使用原生 OpenMOSS 库，至少需要全局调度和 per-session codec state。
- **预热**：房间进入时完成模型常驻检查、voice prompt 预编码、session 创建、codec context 准备、一次不对外播放的 warmup；连接跨 turn 保持，不重复握手、鉴权和 prompt 上传。
- **传输**：服务内部可保留 PCM；浏览器正式音频使用 WebRTC/Opus，不使用 MediaRecorder + MP3 分片播放。MediaRecorder 仅用于赛后录音。
- **打断**：Agent interrupt、TTS scheduler abort、服务端队列清空、WebRTC sender/track generation 切换、AudioWorklet flush 必须由同一 `generation_id` 串起来。
- **上下文**：维护 `generated_text`、`committed_text`、`played_text` 三个游标；取消后只保留业务定义允许的已播放或已提交部分，未播放尾部不得进入下一轮模型上下文。

## 必须执行的自动验收

### 单房间

- LLM 首个正文字符 -> TTS 首 PCM；
- LLM 首个正文字符 -> 浏览器实际出声，p95 <= 2.5 s；
- TTS 回转 ASR CER；
- 首字/尾字吞字率；
- 重复字、重复短句和异常超长输出率；
- 音色 embedding 相似度/漂移；
- 块间静音 p50/p95/max；
- 音量 LUFS/RMS 一致性；
- underrun、AudioWorklet starvation、浏览器卡顿次数；
- interrupt -> 最后一帧旧音频停止的延迟；
- cancel ACK 后旧 generation 音频为 0。

### 三房间并发

- 三个房间同时进行中文 4v4 长回合；
- 各房间独立音色，交叉污染为 0；
- 记录各房间 TTFP、首声、RTF、codec 排队、GPU 利用率/显存峰值；
- 任一房间 cancel 不得阻塞或清空其他房间；
- 单房间长文本不得使另外两房间发生 head-of-line blocking；
- 运行至少 30 分钟，统计 p50/p95/p99 与失败率，而不是只跑单句 demo。

### Nano 晋级门槛

在同一中文集上，Nano 必须同时满足：

- CER 和吞字率不劣于事先设定阈值；
- 短句重复/停不下来为 0；
- 3-session 排队仍满足首声目标；
- 固定 8 个辩手音色的跨块和跨轮漂移通过；
- cancel barrier 通过。

任何一项不通过，Nano 保持 canary，不进入正式赛 primary。

## 最终决策

- **立即固定**：OpenMOSS/MOSS-TTS `ad99ec5...` 的 `MossTTSRealtimeStreamingSession` 语义和 vLLM-Omni `d09f549...` 的 Realtime GPU serving 配置作为当前研发基线。
- **不要固定**：OpenMOSS `fast_api.py` 的共享多线程 serving、Nano 当前 CPU Web streaming、Nano 分块滚动 prompt 作为正式主链路。
- **下一工程步骤**：先完成一个 MOSS-TTS-Realtime 的三房间 GPU 验收服务，优先验证 vLLM-Omni 能否承载增量 text request；若不能，在其 scheduler 前实现专用的 delta session adapter，同时实现 generation-level abort barrier。
- **产品上线条件**：只有“首声 <=2.5s + 三房间并发 + cancel 无残音 + 中文 CER/吞字/重复/音色漂移全通过”后，才把核心链路视为固定。
