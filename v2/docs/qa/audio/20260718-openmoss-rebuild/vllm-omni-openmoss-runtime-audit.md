# vLLM-Omni × MOSS-TTS-Realtime 运行时审计

- 审计日期：2026-07-18
- 审计方式：只读审计 GitHub 官方仓库与官方源码；未修改项目代码，未部署服务
- vLLM-Omni 审计提交：[`d09f549e862c58ca87c195d4050e77b20a126a38`](https://github.com/vllm-project/vllm-omni/commit/d09f549e862c58ca87c195d4050e77b20a126a38)
- OpenMOSS/MOSS-TTS 审计提交：[`ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af`](https://github.com/OpenMOSS/MOSS-TTS/commit/ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af)
- 目标合约：`Agent body delta → 持久 WS → 连续 PCM/Opus`，并支持按 endpoint 精确 `abort/clear`、无串音、2–3 场并发

## 结论

**NO-GO：当前 vLLM-Omni 的 MOSS-TTS-Realtime 不能作为项目持久双向 TTS 合约的直接替代运行时。**

它已经具备“两阶段 talker → codec”和 PCM 分块输出的基础设施，但当前 OpenAI/WebSocket serving 层仍然是“一次完整文本对应一次有限生命周期请求”：WebSocket 收到的 `input.text` 只做字符串累积，直到 `input.done` 才启动合成；合成期间同一连接不再读取控制消息；完成后直接发送 `session.done` 并退出。因此它不能保持项目所需的同一连接多轮 `text_delta/final/abort` 合约，也不能把 LLM 正文 delta 在同一个正在运行的 TTS turn 中逐步送入模型。

此外，当前源码对 Realtime 在线输入路径有明确保留说明：现有 `prompt_audio_array` 路径只对短 prompt “大致对齐”，完整 Realtime processor 尚未接入。对应 E2E 测试当前仍因公开 issue 被整体 skip。即使暂不考虑 12GB 显存，当前 HEAD 也不应直接进入正式链路。

## 合约逐项判定

| 项目要求 | 当前官方实现 | 判定 |
|---|---|---|
| 持久 WebSocket，多轮复用 | `/v1/audio/speech/stream` 每连接只接收一组文本；`input.done` 后生成一次，随后 `session.done` 并返回 | **不支持** |
| Agent 正文 delta 直接增量进入同一 TTS turn | `input.text` 仅追加到 `text_parts`；必须等待 `input.done` 后拼成 `full_text` 才调用生成 | **不支持** |
| 连续 PCM | `async_chunk: true`，talker 音频码按 chunk 交给持久 codec streaming session，HTTP/WS 可逐块发 PCM | **部分支持** |
| 连续 Opus | streaming 校验只接受 PCM/WAV；`opus` 只在通用响应格式枚举中存在，没有实时 Opus chunk path | **不支持** |
| 同连接 `abort/cancel` | WS 协议只有 `session.config`、`input.text`、`input.done`；没有 `input.cancel`/`abort`；生成时接收循环被阻塞 | **不支持** |
| 底层精确中止 talker + codec | engine 有 request-id abort，orchestrator 会向所有 stage 转发；codec 有 `on_requests_finished` 清理 stream slot/pending codes | **底层部分具备，API 未暴露** |
| 中止后发出 `audio_reset/released`，阻止旧音频继续播放 | 无 generation/epoch 标记，无 `audio_reset`/`released` 事件，也不能撤回已经写入 socket/浏览器的 PCM | **不支持** |
| 多 endpoint 隔离 | request id 唯一；跨请求取错音频码问题已由合并的 PR #4415 修复 | **正确性基础存在** |
| 2–3 场并发且流畅 | Realtime 默认 Stage 0 `max_num_seqs=8`，但 codec Stage 1 `max_num_seqs=1`；并行 codec batching RFC 仍开放 | **未满足/未验证** |
| 12GB GPU | 官方 recipe 只给出单 A10G 24GB；talker 约 6GB、codec decoder 约 8GB，两个 stage 同卡 | **不支持官方 12GB 路径** |
| 24GB GPU | 官方标注 1×A10G 24GB，且 L4 测试标记也是 24GB 级别 | **官方目标配置** |
| 正式在线 Realtime 输入正确性 | serving 源码明确称当前短 prompt fallback 不是完整 Realtime processor；相关 online/offline E2E 仍 skip | **未达到正式准入** |

## 关键源码证据

### 1. WebSocket 不是“文本 delta → 立即语音”的真增量链路

官方 handler 的模块说明直接写明：文本通过 WebSocket 增量到达，但会“buffers it until `input.done`”，之后才调用一次现有 TTS pipeline。实际实现中，`input.text` 只执行 `text_parts.append(text)`；只有收到 `input.done` 才将全部片段拼为 `full_text` 并进入 `_generate_and_send()`。生成完成后发送 `session.done`，连接处理函数返回。

源码：

- [`serving_speech_stream.py` 模块说明与协议](https://github.com/vllm-project/vllm-omni/blob/d09f549e862c58ca87c195d4050e77b20a126a38/vllm_omni/entrypoints/openai/serving_speech_stream.py#L1-L32)
- [`input.text` 只缓存、`input.done` 才生成](https://github.com/vllm-project/vllm-omni/blob/d09f549e862c58ca87c195d4050e77b20a126a38/vllm_omni/entrypoints/openai/serving_speech_stream.py#L99-L159)

这与项目要求的 `Agent SSE body delta → 短语聚合 → 持久 WS → TTS` 不等价。项目可以先在自己的网关聚合短语再把完整短语交给它，但那会变成“每个短语一个新 TTS request”，而不是在同一生成 turn 中追加正文 delta。

### 2. 上游 MOSS 模型本身支持 `push_text(delta)`，但 vLLM-Omni serving 没有接入该能力

OpenMOSS 官方原生示例提供 `session.push_text(delta)`、`end_text()`、`drain()` 和多轮 KV cache 复用。这证明模型能力本身存在。问题在于 vLLM-Omni 的 speech API 没有把 WebSocket `input.text` 映射到这个持续 session，而是等待完整文本后创建普通 request。

源码：

- [OpenMOSS 单轮 LLM delta → `session.push_text`](https://github.com/OpenMOSS/MOSS-TTS/blob/ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af/docs/moss_tts_realtime_model_card.md#L183-L218)
- [OpenMOSS 多轮 KV cache 复用说明](https://github.com/OpenMOSS/MOSS-TTS/blob/ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af/docs/moss_tts_realtime_model_card.md#L220-L229)

因此，把真增量能力带入 vLLM-Omni 不是配置开关，而需要新的 session/append serving 与调度适配。

### 3. 当前 Realtime 在线 prompt 构建仍是临时 fallback

`_build_moss_tts_params()` 对 `realtime` 分支有明确注释：`AutoProcessor` 无法自动发现 Realtime processor，Realtime prompt 格式也不同；当前继续使用旧 `prompt_audio_array` 路径，该形状只对短 prompt “lines up well enough”，完整支持需要单独接入 processor module，而此处没有实现。

源码：

- [Realtime fallback 与“full Realtime support”未接入说明](https://github.com/vllm-project/vllm-omni/blob/d09f549e862c58ca87c195d4050e77b20a126a38/vllm_omni/entrypoints/openai/serving_speech.py#L1891-L1908)

这会直接影响长文本、增量文本、参考音频条件和声音一致性，不能用“HTTP 能返回 PCM”代替内容正确性验证。

### 4. PCM 分块基础设施是真实存在的

Realtime deploy config 开启 `async_chunk: true` 和 `codec_streaming: true`，Stage 0 的 raw codec rows 以 request-id 维护缓冲并按 `codec_chunk_frames` 送往 Stage 1。Stage 1 使用持久 `_MossCodecStreamSession` 保存 codec streaming state，并按 slot 解码为连续 PCM chunk。OpenAI serving 会将每个解码 chunk 转为 PCM bytes 后立即 yield。

源码：

- [Realtime deploy config](https://github.com/vllm-project/vllm-omni/blob/d09f549e862c58ca87c195d4050e77b20a126a38/vllm_omni/deploy/moss_tts_realtime.yaml)
- [talker → codec raw async chunk processor](https://github.com/vllm-project/vllm-omni/blob/d09f549e862c58ca87c195d4050e77b20a126a38/vllm_omni/model_executor/stage_input_processors/moss_tts.py#L225-L341)
- [codec persistent streaming session](https://github.com/vllm-project/vllm-omni/blob/d09f549e862c58ca87c195d4050e77b20a126a38/vllm_omni/model_executor/models/moss_tts/modeling_moss_tts_codec.py#L31-L116)
- [serving 层逐块转 PCM 并 yield](https://github.com/vllm-project/vllm-omni/blob/d09f549e862c58ca87c195d4050e77b20a126a38/vllm_omni/entrypoints/openai/serving_speech.py#L2700-L2797)

所以这里的否决原因不是“没有流式 PCM”，而是**输入端、session 生命周期、取消协议和并发准入不符合项目合约**。

### 5. abort 底层可用，但官方 WS 无法在生成时消费 cancel

底层 `AsyncOmniEngine.abort()` 会发送 `AbortRequestMessage`；orchestrator 将 request-id 中止转发到所有 stage pool 并清理 request state。MOSS codec 还有 `on_requests_finished()`，用于在断连或 engine abort 没有终止 payload 时释放 stream slot 和 pending codes。

源码：

- [engine abort API](https://github.com/vllm-project/vllm-omni/blob/d09f549e862c58ca87c195d4050e77b20a126a38/vllm_omni/engine/async_omni_engine.py#L1445-L1455)
- [orchestrator 跨 stage abort](https://github.com/vllm-project/vllm-omni/blob/d09f549e862c58ca87c195d4050e77b20a126a38/vllm_omni/engine/orchestrator.py#L578-L599)
- [codec abort/异常完成清理](https://github.com/vllm-project/vllm-omni/blob/d09f549e862c58ca87c195d4050e77b20a126a38/vllm_omni/model_executor/models/moss_tts/modeling_moss_tts_codec.py#L597-L632)

但是 WebSocket 协议没有 cancel 消息，而且 `_generate_and_send()` 是在消息接收协程内同步 await 的。生成期间没有第二个 reader task，客户端即使发送自定义 `abort` 也不会被读取。当前只有 `WebSocketDisconnect` 异常路径显式调用 `engine_client.abort(request_id)`。

源码：

- [WS 仅在 disconnect 时调用 abort](https://github.com/vllm-project/vllm-omni/blob/d09f549e862c58ca87c195d4050e77b20a126a38/vllm_omni/entrypoints/openai/serving_speech_stream.py#L201-L282)

即使补一个 API cancel，也还缺项目要求的 `audio_reset → browser AudioWorklet flush → released` 和 generation/epoch 隔离，已送到浏览器的旧 PCM 不会自动消失。

### 6. 多请求串音修复已合并，但 codec 并发能力仍是瓶颈

此前 MOSS talker 在 continuous batching 下会把 request 0 的 audio codes 路由给其他请求，造成跨请求音频污染。该问题已由合并的 [PR #4415](https://github.com/vllm-project/vllm-omni/pull/4415) 修复，当前输出使用 batch-aligned per-request list。

但默认 Realtime 配置的 Stage 1 仍为 `max_num_seqs: 1`。这意味着同一 codec decoder 实例一次只有一个 active stream slot，其他请求只能排队/缓冲。Stage-1 多请求 batch codec 仍是开放 RFC [#4316](https://github.com/vllm-project/vllm-omni/issues/4316)。它不等于 2–3 个 endpoint 会串音，但意味着 2–3 场同时说话时的首包等待、RTF 和卡顿风险没有被解决。

更关键的是，当前 MOSS Realtime 的 offline/online E2E 测试文件仍整体标记 skip，原因指向开放 issue [#4700](https://github.com/vllm-project/vllm-omni/issues/4700)：

- [offline Realtime E2E skip](https://github.com/vllm-project/vllm-omni/blob/d09f549e862c58ca87c195d4050e77b20a126a38/tests/e2e/offline_inference/test_moss_tts_realtime.py#L66-L75)
- [online speech E2E skip](https://github.com/vllm-project/vllm-omni/blob/d09f549e862c58ca87c195d4050e77b20a126a38/tests/e2e/online_serving/test_moss_tts_expansion.py#L28-L39)

因此当前 HEAD 没有可作为生产准入依据的正式 c1/c2/c3 在线回归结果。

### 7. 24GB 是当前官方部署档，不是 12GB 优化路径

vLLM-Omni 官方 recipe 对 MOSS-TTS-Realtime 给出的硬件是 `1x A10G 24GB`，并估算 talker 约 6GB、codec decoder 约 8GB。默认两个 stage 在同一 `cuda:0`：Stage 0 `gpu_memory_utilization: 0.60`，Stage 1 `0.12`。官方 E2E 也以单 L4 为硬件标记。

源码：

- [官方 A10G 24GB recipe 与显存说明](https://github.com/vllm-project/vllm-omni/blob/d09f549e862c58ca87c195d4050e77b20a126a38/recipes/OpenMOSS/MOSS-TTS.md#L66-L101)
- [Realtime stage 显存配置](https://github.com/vllm-project/vllm-omni/blob/d09f549e862c58ca87c195d4050e77b20a126a38/vllm_omni/deploy/moss_tts_realtime.yaml#L20-L49)

结论是：**24GB 是官方支持/验证方向；12GB 没有官方同卡配置或量化/encoder-offload recipe。** 对本项目已经观察到的 12GB whole-codec OOM，vLLM-Omni 当前实现没有直接提供解决方案。

### 8. 官方社区状态仍把“true streaming path”列为未解决请求

当前 recipe 将模型 maintainer 标为 Community；截至审计日，仓库仍有开放 issue [#5089 “MOSS-TTS-Realtime support and true streaming path in vLLM-Omni”](https://github.com/vllm-project/vllm-omni/issues/5089)，且没有 maintainer 回复。这与源码中的临时 Realtime serving fallback、跳过 E2E 状态一致。

## 对 phdebate 当前方案的影响

### 可复用部分

若未来有 24GB+ 独占 GPU，以下部分值得作为实现参考或实验 backend：

1. talker/codec 两阶段 async-chunk 管线；
2. request-id 隔离后的多请求 talker continuous batching；
3. codec persistent streaming state 与 finish/abort slot cleanup；
4. raw PCM StreamingResponse/WS binary frame 输出；
5. MOSS 原生 `push_text(delta)` / multi-turn KV reuse 设计。

### 不能直接替换的部分

当前项目已实现的以下合约不能交给 vLLM-Omni 官方 WS：

1. 同一预热 WS 中连续提交多段正文 delta；
2. `start/ready/text_delta/final/abort/released` 生命周期；
3. 按 endpoint/generation 清除服务端音频队列；
4. 中止后向浏览器发 `audio_reset` 并阻止旧 chunk 继续消费；
5. 2–3 场比赛同时说话时的低排队延迟；
6. 12GB RTX 3080 Ti 单卡部署。

### 如果强行适配，所需工作不是薄封装

至少需要：

1. 新建真正的持久 session handler，分离 reader、generation 和 writer task；
2. 将 `input.text` 映射为同一 request/session 的 append update，而非 `text_parts` 缓冲；
3. 为 Realtime 正确接入 upstream processor/session，而不是 `prompt_audio_array` fallback；
4. 增加 `abort` 消息、generation epoch、跨 stage connector 清理完成确认和 `released` ack；
5. 将每个音频 chunk 标记 endpoint/generation，丢弃 stale epoch；
6. 将 Stage 1 从 `max_num_seqs=1` 提升为至少 3 个 streaming slots，并完成 c1/c2/c3 RTF、TTFP、underrun、串音测试；
7. 若保留 12GB，仍需独立解决 codec/talker 显存布局。

这已经是运行时级改造，风险和维护面明显大于接入一个现成 OpenAI-compatible TTS endpoint。

## 最终建议

1. **不要把当前 vLLM-Omni MOSS-TTS-Realtime 设为生产候选。**
2. 可在 24GB+ GPU 上做独立实验，但准入前必须先修正 Realtime processor path 并解除 E2E skip。
3. 即使单请求 TTFP/RTF 合格，也必须单独验证：
   - delta 到达后无需 `input.done` 即开始出音；
   - 同连接可多轮复用；
   - abort ack 后无任何旧 generation PCM；
   - c3 同时流式时每路 RTF < 1、无串音、排队延迟满足浏览器 2.5 秒目标。
4. 在这些条件未满足前，继续保留项目现有 persistent WS + endpoint isolation 合约，把 vLLM-Omni 仅视为参考实现，不视为可直接部署的核心语音链路。

