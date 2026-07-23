# OpenMOSS 12GB 实时 TTS 替代路径审计

审计日期：2026-07-18（Asia/Shanghai）  
范围：只读核查 OpenMOSS 官方仓库、官方模型仓库，以及 OpenMOSS README 明确推荐的 vLLM-Omni、SGLang-Omni。未修改业务代码、未部署或切换生产 TTS。  
目标：在单张 12GB GPU 的现实约束下，寻找能够接近“首声 <2.5 秒、多个正常普通话音色、2～3 场并发、不卡顿、不吞词、不漂移”的可验证路径。

## 结论先行

1. **12GB 上最值得立即验证的主候选，是 `MOSS-TTS-Realtime talker + GPU decoder-only codec + 预计算音色 token`。** 完整 Realtime talker+codec 的 checkpoint 下限约 10.955 GiB，已在空闲 RTX 3080 Ti 上真实 OOM；但实时解码只需要 codec 的 quantizer 和 decoder。固定 Realtime talker、decoder、quantizer 的静态权重估计约 7.654 GiB，理论上为 CUDA context、KV cache、流式状态和工作区留下约 3～4 GiB。官方 session 可以直接接受预计算的二维音色 token，官方 codec 的 decode 路径也只访问 quantizer+decoder，因此这不是改变模型语义的量化方案，而是去掉实时链路不需要的 encoder 常驻显存。
2. **MOSS-TTS-Nano 仍可试，但只能作为低资源备选，不能凭“0.1B”直接晋级。** vLLM-Omni 官方配置称 Nano 的 AR LM+codec 约 2 GiB、`max_num_seqs=4`，并确实按请求保存流式 generator；ONNX CPU 官方又宣称 M4 单核可流畅运行。可是 Nano 没有正文 delta session，当前一次请求仍接收完整文本；其主仓和本地真实 GPU canary 都已出现吞句、短句重复、音色错乱和块间音色漂移。它只有通过固定普通话音色的 20 轮质量门禁和三请求压力测试后，才可能成为 fallback。
3. **Local Transformer、VoiceGenerator、OpenMOSS/sglang Delay、llama.cpp Q4 当前都不是 12GB 正式实时主链路。** Local-1.7B 加 v1 codec 的 checkpoint 已约 12.312 GiB；Local-v1.5 加 v2 codec 约 16.387 GiB。VoiceGenerator 适合离线生成普通话参考音色，不适合每个短语在线生成。OpenMOSS/sglang 是 8B Delay 全模型路径，llama.cpp 的 8GB 路径是分阶段、整句生成后再解码，不是双向增量 TTS。
4. **2～3 场真实并发仍没有被任何 12GB 方案证明。** 官方 Realtime FastAPI 只支持 batch size 1；vLLM-Omni Realtime 的 talker 可调度 8 路，但 codec stage 仍是 1；vLLM-Omni Nano 的源码按请求逐个推进 Python generator，官方没有给出三路中文低延迟/音质结果。因此本报告给出“可试路径”，不是 GO 结论。

## 固定快照

| 项目 | 固定版本 | 本次用途 |
|---|---|---|
| [OpenMOSS/MOSS-TTS](https://github.com/OpenMOSS/MOSS-TTS) | [`ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af`](https://github.com/OpenMOSS/MOSS-TTS/commit/ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af) | Realtime、Local、VoiceGenerator、llama.cpp 接口语义 |
| [OpenMOSS/MOSS-TTS-Nano](https://github.com/OpenMOSS/MOSS-TTS-Nano) | [`11619374849c649486584e3b10ed55b176a924ee`](https://github.com/OpenMOSS/MOSS-TTS-Nano/commit/11619374849c649486584e3b10ed55b176a924ee) | Nano PyTorch/ONNX、流式与锁 |
| [OpenMOSS/MOSS-Audio-Tokenizer](https://github.com/OpenMOSS/MOSS-Audio-Tokenizer) | [`8c50ac4c5d7287d2ed6ea20a08c90ca439887d23`](https://github.com/OpenMOSS/MOSS-Audio-Tokenizer/commit/8c50ac4c5d7287d2ed6ea20a08c90ca439887d23) | codec 流式能力与权重边界 |
| [OpenMOSS/sglang](https://github.com/OpenMOSS/sglang) | [`6c3a99cf413f623541aae7ee14744f5a3c062a47`](https://github.com/OpenMOSS/sglang/commit/6c3a99cf413f623541aae7ee14744f5a3c062a47) | 官方 Delay fused serving |
| [OpenMOSS/llama.cpp](https://github.com/OpenMOSS/llama.cpp) | [`019bda336bfa5d0546fffdde24d8b24546da82c8`](https://github.com/OpenMOSS/llama.cpp/commit/019bda336bfa5d0546fffdde24d8b24546da82c8) | 量化 GGUF 低显存路径 |
| [vllm-project/vllm-omni](https://github.com/vllm-project/vllm-omni) | [`d09f549e862c58ca87c195d4050e77b20a126a38`](https://github.com/vllm-project/vllm-omni/commit/d09f549e862c58ca87c195d4050e77b20a126a38) | Realtime/Nano serving 与调度 |
| [sgl-project/sglang-omni](https://github.com/sgl-project/sglang-omni) | [`e795c35286cf37321e19d35cb6e8c56f6765eae1`](https://github.com/sgl-project/sglang-omni/commit/e795c35286cf37321e19d35cb6e8c56f6765eae1) | Local v1.5 流式、并发与取消架构参考 |

模型 revision 与文件清单来自 OpenMOSS-Team 官方 Hugging Face 模型仓库。下表是 checkpoint 文件字节，不是运行峰值；它只用于排除明显不可能的同卡组合。

| 模型组合 | 固定模型 revision | checkpoint 下限 | 12GB 判断 |
|---|---|---:|---|
| Realtime talker | [`6acbc7f...`](https://huggingface.co/OpenMOSS-Team/MOSS-TTS-Realtime/tree/6acbc7f161a0db71c291f2d0aaa9eee59334cab2) | 4.344 GiB | 单体可放入 |
| Audio Tokenizer v1 全量 | [`3cd226b...`](https://huggingface.co/OpenMOSS-Team/MOSS-Audio-Tokenizer/tree/3cd226ba2947efa357ef453bcad111b6eafba782) | 6.611 GiB | 与 Realtime 同卡无运行余量 |
| Realtime + 全 codec | 同上 | **10.955 GiB** | 已真实 OOM，淘汰 |
| Realtime + decoder+quantizer | 同上；按已下载 tensor 分组 | **约 7.654 GiB** | **可试，最高优先级** |
| Local-1.7B + v1 codec | [`12aa734...`](https://huggingface.co/OpenMOSS-Team/MOSS-TTS-Local-Transformer/tree/12aa734e4f11a7b3fdf4eb0ad2aa2029675ffc2e) + v1 codec | 12.312 GiB | 静态已超过 12GB，淘汰同卡全量 |
| Local-v1.5 + v2 codec | [`be7766a...`](https://huggingface.co/OpenMOSS-Team/MOSS-TTS-Local-Transformer-v1.5/tree/be7766a6735b98bd793f7c79fb720b4d0f5d13b8) + [`f6e20e5...`](https://huggingface.co/OpenMOSS-Team/MOSS-Audio-Tokenizer-v2/tree/f6e20e543b33d2c252a7ef71bdf8aa71e5ff9169) | 16.387 GiB | 淘汰 12GB 同卡 |
| VoiceGenerator + v1 codec | [`97521ec...`](https://huggingface.co/OpenMOSS-Team/MOSS-VoiceGenerator/tree/97521ec2b6f3ec5026ac1f5751f8fc302d82c2d4) + v1 codec | 10.549 GiB | 在线运行余量不足；仅离线使用 |
| Nano PyTorch + Nano codec | [`44502f8...`](https://huggingface.co/OpenMOSS-Team/MOSS-TTS-Nano-100M/tree/44502f80dbf9743528fa921cc544d662c685ebec) + [`6aa02b0...`](https://huggingface.co/OpenMOSS-Team/MOSS-Audio-Tokenizer-Nano/tree/6aa02b01e445cc585582cf0ba480bc3ea6c8dd68) | 约 0.300 GiB | 可试 |
| Nano ONNX TTS + codec | [`f52645c...`](https://huggingface.co/OpenMOSS-Team/MOSS-TTS-Nano-100M-ONNX/tree/f52645cb467506d8e18e746ddd59482685b74e58) + [`ceff0d0...`](https://huggingface.co/OpenMOSS-Team/MOSS-Audio-Tokenizer-Nano-ONNX/tree/ceff0d0749bfb3fa2d61149794ec6feef0d1e1ae) | 约 0.628 GiB | 可试；ORT 运行显存另计 |

## 候选一：Realtime talker + decoder-only codec

### 为什么这是 12GB 上唯一保留 Realtime 质量的真实路径

Realtime 是上游唯一同时具备这些能力的模型：

- 原生 `push_text(delta)` 增量文本 session；
- 12-token prefill、标点/短语缓存、跨轮 KV cache；
- 中文 1.07% Seed-TTS-eval CER、76.7 speaker SIM；
- 单 L20 暖机后 TTFB 180 ms、RTF 0.51。

证据：[Realtime 模型卡能力与延迟](https://github.com/OpenMOSS/MOSS-TTS/blob/ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af/docs/moss_tts_realtime_model_card.md#L1-L58)、[中文评测](https://github.com/OpenMOSS/MOSS-TTS/blob/ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af/docs/moss_tts_realtime_model_card.md#L236-L255)、[正文 delta session](https://github.com/OpenMOSS/MOSS-TTS/blob/ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af/moss_tts_realtime/mossttsrealtime/streaming_mossttsrealtime.py#L618-L729)。

完整 codec OOM 不代表必须放弃 Realtime。官方代码已经给出拆分所需的两个关键契约：

1. `MossTTSRealtimeStreamingSession.set_voice_prompt_tokens()` 可以直接接收二维音频 token；只有传 waveform 时才要求 codec encoder。[固定代码](https://github.com/OpenMOSS/MOSS-TTS/blob/ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af/moss_tts_realtime/mossttsrealtime/streaming_mossttsrealtime.py#L530-L582)
2. codec 的 decode 数据流是 `quantizer.decode_codes -> decoder`，不访问 encoder。[vLLM-Omni vendored 官方实现](https://github.com/vllm-project/vllm-omni/blob/d09f549e862c58ca87c195d4050e77b20a126a38/vllm_omni/model_executor/models/moss_tts/audio_tokenizer.py#L688-L698)

因此可构建以下诊断部署：

```text
离线/启动前一次性：
  普通话参考 WAV -> 固定 codec revision 的 encoder -> voice-token 文件

实时常驻 12GB GPU：
  Realtime BF16 talker
  + codec quantizer
  + codec decoder
  + 每房间 Realtime session/KV/text buffer
  + AudioStreamDecoder / PCM queue

不进入实时 GPU：
  codec encoder
  VoiceGenerator
  参考音频重复编码
```

实现上可以先采取低风险诊断方式：完整 codec 在 CPU 构造后，仅把 `quantizer` 与 `decoder` 移到 CUDA，固定音色直接加载 token 文件；不调用 `codec.encode()`。若验证成功，再做 decoder-only checkpoint loader，避免 CPU 端保留无用 encoder 权重。该路径保持 FP32 codec decode，不引入 INT8/FP16 codec 音质变量。

### 当前证据边界

- 本地完整 Realtime+codec 在 11,909 MiB 空闲 RTX 3080 Ti 上 OOM，见 [12GB exclusive canary](openmoss-realtime-12gb-exclusive-canary.md)。
- 完整 FP32 codec 放 CPU 虽可加载，但一个约 10 秒 prompt 超过 321 秒仍未编码完成，见 [CPU codec canary](openmoss-realtime-12gb-cpu-codec-canary.md)。decoder-only 路径避免每轮 CPU encode，但其 GPU decode 速度尚未实测。
- 官方 FastAPI 仍明确只支持 batch size 1。[模型卡](https://github.com/OpenMOSS/MOSS-TTS/blob/ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af/docs/moss_tts_realtime_model_card.md#L169-L180)
- vLLM-Omni 的 Realtime 配置仍把 codec stage 设为 `max_num_seqs=1`，其官方 recipe 使用 A10G 24GB，并记录 talker 约 6GB、codec decoder 约 8GB。[配置](https://github.com/vllm-project/vllm-omni/blob/d09f549e862c58ca87c195d4050e77b20a126a38/vllm_omni/deploy/moss_tts_realtime.yaml)、[recipe](https://github.com/vllm-project/vllm-omni/blob/d09f549e862c58ca87c195d4050e77b20a126a38/recipes/OpenMOSS/MOSS-TTS.md#L82-L116)
- vLLM-Omni 的标准 speech schema 只接受一个完整 `input: str`；`stream=true` 表示输出音频流，不是请求内追加正文 delta。[请求 schema](https://github.com/vllm-project/vllm-omni/blob/d09f549e862c58ca87c195d4050e77b20a126a38/vllm_omni/entrypoints/openai/protocol/audio.py#L51-L87)。截至本次审计，[“true streaming path”仍是开放 feature issue](https://github.com/vllm-project/vllm-omni/issues/5089)。

### 判定

**可试，P0。** 先证明单 session，再证明两 session 调度；三 session 只能由真实 RTF、codec slot 和显存数据决定，不能从 7.654 GiB 静态估算直接推出。

## 候选二：vLLM-Omni MOSS-TTS-Nano

### 可取之处

Nano 官方为 0.1B，支持中文、48kHz 双声道、音色克隆和流式输出；主仓同时称 4 核 CPU 可运行，ONNX CPU 在 MacBook Air M4 单核可流畅运行。[Nano 特性](https://github.com/OpenMOSS/MOSS-TTS-Nano/blob/11619374849c649486584e3b10ed55b176a924ee/README_zh.md#L84-L111)、[ONNX CPU](https://github.com/OpenMOSS/MOSS-TTS-Nano/blob/11619374849c649486584e3b10ed55b176a924ee/README_zh.md#L176-L204)

vLLM-Omni 的 Nano 配置是当前最明确的 12GB GPU serving 候选：

- AR LM+codec 约 2 GiB；
- `max_num_seqs=4`；
- 每个请求有独立 `inference_stream()` generator；
- 每个 forward 为每个活跃请求取一个音频 chunk；
- request cancel/timeout 时会关闭 generator 并执行清理。

证据：[部署配置](https://github.com/vllm-project/vllm-omni/blob/d09f549e862c58ca87c195d4050e77b20a126a38/vllm_omni/deploy/moss_tts_nano.yaml)、[按请求流式 generator](https://github.com/vllm-project/vllm-omni/blob/d09f549e862c58ca87c195d4050e77b20a126a38/vllm_omni/model_executor/models/moss_tts_nano/modeling_moss_tts_nano.py#L294-L425)、[请求完成/取消清理](https://github.com/vllm-project/vllm-omni/blob/d09f549e862c58ca87c195d4050e77b20a126a38/vllm_omni/model_executor/models/moss_tts_nano/modeling_moss_tts_nano.py#L516-L531)。

### 不能直接晋级的原因

1. **没有文本 delta session。** 一次 `inference_stream()` 仍接收完整 `text`，Agent 的后续正文只能排成另一条 synthesis request，无法像 Realtime 一样在同一 KV/韵律 session 内追加。[Nano runtime API](https://github.com/OpenMOSS/MOSS-TTS-Nano/blob/11619374849c649486584e3b10ed55b176a924ee/moss_tts_nano_runtime.py#L608-L707)
2. **官方普通 serving 是串行的。** PyTorch runtime 在整个 generator 生命周期持有 `RLock`；ONNX Web runtime 也有一把 `_execution_lock`。若不用 vLLM-Omni，2～3 场必须多进程/多实例，而不是一个官方 Web 进程。[PyTorch lock](https://github.com/OpenMOSS/MOSS-TTS-Nano/blob/11619374849c649486584e3b10ed55b176a924ee/moss_tts_nano_runtime.py#L608-L707)、[ONNX execution lock](https://github.com/OpenMOSS/MOSS-TTS-Nano/blob/11619374849c649486584e3b10ed55b176a924ee/app_onnx.py#L399-L459)
3. **vLLM-Omni 的“4 seq”不是已证明的四路稳定音频并行。** 当前 forward 仍按 Python 列表逐请求调用 `next(generator)`；源码还明确提示 upstream 使用全局 RNG，`max_num_seqs>1` 时并发 seed 会互相竞争。[固定代码](https://github.com/vllm-project/vllm-omni/blob/d09f549e862c58ca87c195d4050e77b20a126a38/vllm_omni/model_executor/models/moss_tts_nano/modeling_moss_tts_nano.py#L319-L330)、[逐请求推进](https://github.com/vllm-project/vllm-omni/blob/d09f549e862c58ca87c195d4050e77b20a126a38/vllm_omni/model_executor/models/moss_tts_nano/modeling_moss_tts_nano.py#L470-L509)。这是源码推断，必须用 c3 实测验证 head-of-line blocking、串音和输出确定性。
4. **中文质量风险是当前事实。** 仍开放的官方 issue 包括 [ONNX 吞字吞句 #58](https://github.com/OpenMOSS/MOSS-TTS-Nano/issues/58)、[短文本重复 #60](https://github.com/OpenMOSS/MOSS-TTS-Nano/issues/60)、[LLM 分块音色不一致 #65](https://github.com/OpenMOSS/MOSS-TTS-Nano/issues/65)、[短句重复/语速不均 #81](https://github.com/OpenMOSS/MOSS-TTS-Nano/issues/81)、[标点处丢句 #87](https://github.com/OpenMOSS/MOSS-TTS-Nano/issues/87)、[参考音色错乱 #88](https://github.com/OpenMOSS/MOSS-TTS-Nano/issues/88)。本地 ONNX-CUDA canary 也有一个普通话音色删除 19/28 字、CER 67.86%，见 [真实 Nano canary](nano-onnx-cuda-canary/README.md)。

### 两种可试部署

| 方式 | 目的 | 风险 | 判定 |
|---|---|---|---|
| 单个 vLLM-Omni Nano，`max_num_seqs=3` | 验证 12GB 上三请求 progressive PCM、取消和排队 | Python generator 轮转、全局 RNG、跨短语音色漂移 | **可试 P1** |
| 三个 ONNX CPU 进程，各绑独立 CPU core set | 完全隔离三房间，GPU 留给其他链路 | M4 单核结果不能外推至 12-core Xeon；本地用户报告中端 CPU 仍卡顿 | **可试 P2/fallback** |

Nano 只能使用通过门禁的固定普通话 prompt；出现吞尾、重复、漂移的 prompt 必须永久淘汰，不能靠重试掩盖。

## Local Transformer：优秀的 serving 参考，但不适合当前 12GB

### 1.7B Local

Local-1.7B 是 Qwen3-1.7B + 4 层 depth transformer，12.5Hz、32 RVQ，支持 `n_vq_for_inference` 调低码率；官方将其描述为天然适合流式，并报告较高中文 speaker similarity。[架构与可变码率](https://github.com/OpenMOSS/MOSS-TTS/blob/ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af/moss_tts_local/README.md#L22-L60)、[流式定位](https://github.com/OpenMOSS/MOSS-TTS/blob/ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af/moss_tts_local/README.md#L92-L100)

但减少 `n_vq` 只减少生成/码率，不会从 checkpoint 中删除 codec encoder/decoder 权重。Local-1.7B talker + v1 codec 的 checkpoint 已约 12.312 GiB，在 12GB 卡上还没算 CUDA context、KV、activation 就已越界；官方当前生产 serving 文档也没有给 1.7B Local 的 12GB 配置。

### 4B Local v1.5 / SGLang-Omni

SGLang-Omni 的实现非常值得借鉴：PCM 流式输出、参考音频 LRU/single-flight、每请求 state pool、最多 16 个 running request、持久 batched codec session、request abort callback。官方公开的中文 RTF 0.3306 和并发 16 结果来自 **2×H100**，不是 12GB。[Local cookbook 流式接口](https://github.com/sgl-project/sglang-omni/blob/e795c35286cf37321e19d35cb6e8c56f6765eae1/docs/cookbook/moss_tts_local.md#L135-L156)、[并发基准](https://github.com/sgl-project/sglang-omni/blob/e795c35286cf37321e19d35cb6e8c56f6765eae1/docs/cookbook/moss_tts_local.md#L251-L280)、[request abort](https://github.com/sgl-project/sglang-omni/blob/e795c35286cf37321e19d35cb6e8c56f6765eae1/sglang_omni/models/moss_tts_local/engine_builder.py#L131-L145)。

它的默认同卡配置使用 BF16 talker，并按 90% GPU 总内存与 15% codec reserve 设计；还明确提供第二 GPU codec variant。[配置](https://github.com/sgl-project/sglang-omni/blob/e795c35286cf37321e19d35cb6e8c56f6765eae1/sglang_omni/models/moss_tts_local/config.py#L18-L42)、[双 GPU variant](https://github.com/sgl-project/sglang-omni/blob/e795c35286cf37321e19d35cb6e8c56f6765eae1/sglang_omni/models/moss_tts_local/config.py#L161-L183)。Local-v1.5 + v2 codec 的 checkpoint 下限约 16.387 GiB，因此 **淘汰当前 12GB 同卡部署，但保留其 scheduler/slot/cancel 设计作为 Realtime decoder-only 重构参考**。

## VoiceGenerator：用于离线造音色，不用于实时短语

VoiceGenerator 是 1.7B `MossTTSDelay` 音色设计模型，可由中文/英文文本描述直接生成音色，不需要参考音频。[模型定位](https://github.com/OpenMOSS/MOSS-TTS/blob/ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af/docs/moss_voice_generator_model_card.md#L1-L27)

它适合一次性生成 4～8 条中性普通话参考音频：

- 男女声均可，但只要求清晰、平稳、无方言；
- 6～10 秒，录音干净，普通语速，完整句号结尾；
- 固定准确 transcript；
- 生成后人工试听，再分别预编码为 Realtime/Nano voice token；
- 运行时只使用固定 prompt/token，不在线调用 VoiceGenerator。

VoiceGenerator + v1 codec 的 checkpoint 下限约 10.549 GiB，在线运行没有足够工作区；它又是采样式音色设计模型，每短语重新生成会放大音色变化。**实时主链路淘汰，离线资产生成保留。**

## OpenMOSS/sglang：淘汰 12GB 实时主链路

OpenMOSS 自己的 `sglang` 仓库支持的是 fused `MOSS-TTS Delay`、SoundEffect、TTSD，不包含 Realtime 或 Nano。其 README 给出的 MOSS-TTS Delay 指标是 RTX 4090、单并发 45 token/s，接口返回生成结果后再解码/封装音频；模型是 8B talker 加完整 codec。[官方 README](https://github.com/OpenMOSS/sglang/blob/6c3a99cf413f623541aae7ee14744f5a3c062a47/README.md#L69-L139)

它可以提高大模型吞吐，但不能解决 12GB 静态权重、正文 delta、首声和三房间连续 PCM。**淘汰。**

## OpenMOSS/llama.cpp：低显存成立，但实时链路不成立

OpenMOSS README 宣称经内存优化后，8B Delay 模型可在 8GB GPU 上运行；Q4 first-class GGUF 约 5.2GB，并支持 ONNX codec。低显存来自量化、CPU/GPU offload和分阶段加载，而当前 first-class e2e 流程是：先生成完整 `raw.codes.bin`，再调用 ONNX decoder 输出 WAV。[官方 first-class 流程](https://github.com/OpenMOSS/llama.cpp/blob/019bda336bfa5d0546fffdde24d8b24546da82c8/docs/moss-tts-firstclass-e2e_zh.md#L178-L220)、[MOSS-TTS llama.cpp 后端](https://github.com/OpenMOSS/MOSS-TTS/blob/ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af/moss_tts_delay/llama_cpp/README.md#L186-L215)

当前没有持久 session、正文 delta、边生成边 PCM 解码、三请求 serving 或模型级 abort 的正式实现。**可用于离线音色/WAV 工具，不用于正式实时赛。**

## 中文尾字、吞句与音色风险

| 风险 | 官方证据 | 对 12GB 方案的约束 |
|---|---|---|
| Nano 吞句/截尾 | #58、#87 仍开放；本地 voice 2 删除 19/28 字 | Nano 必须 20 轮逐 voice 门禁；不允许用单次成功证明质量 |
| Nano 短句重复/停不下来 | #60、#81 | 第一块不能小到 1～2 个汉字；保留稳定标点和 10～16 字首块 |
| Nano 块间音色漂移 | #65 | Nano 不具备同一正文 delta session；固定 prompt 也必须实测跨块 embedding/听感 |
| MOSS Delay 家族尾字截断 | [MOSS-TTS #19](https://github.com/OpenMOSS/MOSS-TTS/issues/19) 中维护者承认存在概率性 bug并给出参考音频短于目标文本、加终止标点等建议 | VoiceGenerator/Delay 不能代替自动尾字门禁；建议只用于离线 prompt |
| Realtime 尾字 | 官方没有公开逐字边界、20轮中文吞尾指标 | 现有 `end_text -> drain -> decoder.flush` 必须保留，并用 TTS→ASR 首尾字门禁证明 |
| 并发串音/失真 | [MOSS-TTS #72](https://github.com/OpenMOSS/MOSS-TTS/issues/72) 报告单实例并行出现失真和 CPU spike；维护者当时承诺后续 thread-safe serving | 不直接复用“每 session 一个线程，共享一个 codec”；采用单 scheduler、请求 generation id 与 codec slot 隔离 |

## 12GB 决策矩阵

| 路径 | 首声潜力 | 普通话/多音色 | 2～3 并发 | 稳定性证据 | 决策 |
|---|---|---|---|---|---|
| Realtime + 完整 codec 同 GPU | 上游 180ms | 强 | batch1/codec1 | 本地 OOM | **淘汰** |
| Realtime + 完整 codec CPU | 不足 | 同模型 | 未到推理 | prompt encode >321s | **淘汰** |
| **Realtime + GPU decoder-only + 预计算 token** | **最高** | **强；固定4～8 prompt** | c2/c3 未证 | 模型语义成立，运行证据缺失 | **P0 可试** |
| vLLM-Omni Nano 单实例 | 高；官方 progressive chunk | 20语言、需固定 prompt | 配置 max4，真实 c3 未证 | 吞句/重复/漂移风险 | **P1 可试/备选** |
| Nano ONNX CPU 三进程 | 取决于 Xeon | 同 Nano | 进程级隔离可做到 | M4 结果不可外推 | **P2 可试/overflow** |
| Local-1.7B + 全 codec | 模型流式友好 | 质量强 | 无12GB serving | 静态 >12GB | **淘汰同卡** |
| Local-v1.5/SGLang-Omni | 强 | 强 | H100 c16 有证据 | 2×H100；静态16.4GiB | **淘汰12GB，保留架构参考** |
| VoiceGenerator 在线 | 不适合短语低延迟 | 可设计音色 | 未证 | 采样敏感、显存紧 | **只离线生成 prompt** |
| OpenMOSS/sglang Delay | 整句吞吐路径 | 强 | 单并发4090数据 | 非正文 delta/PCM session | **淘汰** |
| llama.cpp Q4 | 低显存 | 量化质量尚可 | 无 serving 证据 | 完整 codes 后解码 WAV | **淘汰实时** |

## 推荐实验顺序

### P0：Realtime decoder-only 12GB canary

1. 用固定 codec revision 离线生成 4～8 个普通话 prompt token 文件，并保存 WAV hash、transcript、codec SHA、token shape/hash。
2. Realtime talker 保持 BF16/SDPA；codec encoder 留在 CPU 或不实例化，只把 decoder+quantizer 以原始 FP32 放入 GPU。
3. 先运行一个常驻 session，完成 `push_text -> end_text -> drain -> decoder.flush`；禁止每块重建 codec streaming state。
4. 记录加载峰值/稳态显存、首个非静音 PCM、RTF、chunk gap、underrun、CER、首尾字、重复、RMS和音色 proxy。
5. c1 通过后再做 c2；只有 c2 的每路 RTF、排队和 cancel 都通过，才尝试 c3。
6. 任一时刻出现 OOM、尾字丢失、块间音色跳变或 cancel 后旧 PCM，立即 NO-GO，不接生产开关。

建议最低 canary 门槛：暖机后 TTS 首个非静音 PCM 每请求 <800ms，为 WebRTC、网络和浏览器 2.5 秒总预算保留余量；c1 RTF P95 ≤0.65；所有普通话文本首尾字符正确；20 轮/voice 无吞句、无重复循环；cancel ACK 后旧 generation PCM=0。

### P1：vLLM-Omni Nano c3 canary

1. 完全停止 LightTTS 后运行一个 Nano vLLM-Omni 实例，先 c1 后 c3；不把 `max_num_seqs=4` 当成通过。
2. 三个请求使用不同文本和不同固定普通话 prompt，检查串音、串文本、RNG干扰和 head-of-line blocking。
3. 首块采用 10～16 汉字或稳定逗号，后续 16～28 汉字；测试 1～4字短句必须单独覆盖，但不把它作为常规分块。
4. 按同一套质量门禁跑至少 20轮/voice；任何吞句/重复/音色漂移都维持 fallback NO-GO。

### P2：Nano ONNX CPU 三进程

只在 GPU Nano c3 或 Realtime c2/c3 不通过时执行。三个进程使用独立端口和 CPU affinity，分别限制 ORT intra/inter-op threads；测量 Xeon 上的每路 RTF、首包和系统 load。若三路同时 RTF≥1或出现 lead 持续为负，立即淘汰，不通过增加大缓冲掩盖。

## 证据缺口

- decoder-only Realtime 的真实 3080 Ti 加载峰值、`torch.compile` 额外显存和首 PCM；
- v1 codec 在共享权重下是否能安全维护 2～3 份独立 streaming state；
- 单 3080 Ti 上 c2/c3 的总 RTF与公平调度；
- vLLM-Omni Nano 三请求是否存在可听串扰、RNG交叉和长请求阻塞短请求；
- 固定 4～8 个普通话音色的 20 轮人工自然度、方言/情绪检查；
- 任一候选从 Agent 首正文字符到 Chrome/Safari 实际出声的 WebRTC 总链路数据。

## 最终建议

**当前不要再把“独立24GB GPU”视为唯一下一步。先做 Realtime decoder-only 的 12GB 实机 canary。** 该路径最接近用户要求，也最大限度保留已经实现的正文 delta、持久 session、打断和音色一致性语义。如果它在真实 GPU 上仍因 activation/compile OOM，或无法达到 c2，那么再将 vLLM-Omni Nano 与 ONNX CPU 三进程作为有严格质量门禁的 fallback；不要转向 Local-v1.5、VoiceGenerator 在线 serving、OpenMOSS/sglang Delay 或 llama.cpp 整句路径。
