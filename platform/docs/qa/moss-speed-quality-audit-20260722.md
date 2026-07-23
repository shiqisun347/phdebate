# MOSS-Realtime 推理速度与音频质量降级审计

日期：2026-07-22  
范围：当前 RTX 3090 24GB、MOSS-TTS-Realtime、单路真双向文本输入/PCM 输出；只读审计，未修改或重启生产。

## 结论

**仅压缩浏览器音频或降低 WebRTC 码率，不能明显加速 MOSS 推理。** 当前链路已经把 MOSS 的 24 kHz 单声道 PCM16（约 384 kbps）在服务器本机送入 LiveKit，并以单条 64 kbps mono Opus 音轨发给浏览器；继续降到 48/32 kbps只会再省 25%/50% 下行带宽，几乎不改变 TTS RTF，还会重新引入金属感和细小撕裂风险。

当前生产方向已经接近 PyTorch 原生后端的安全上限：BF16 talker、FP32 codec、SDPA、固定形状 `torch.compile(reduce-overhead)`、固定 inferencer、关闭运行期 GC、Prompt codes 预缓存、同一 context 持续输入。现有实测八音色 RTF P50/P95/最大为 `0.919/0.979/0.995`；另一组优化后记录为 `0.845/0.883/0.889`，长文本 RTF `0.805`。这说明当前已经实时，但对 1.1 倍播放的余量仍小。

**大幅加速只有两条路：**换成支持 MOSS-Realtime 真增量上下文的高性能 runtime，或改变 Realtime 模型/codec 的计算图。现阶段 vLLM、SGLang、Nano 均不能无损替换现有“同一连接持续 `text_delta`、同时出音频、可立即 abort/release”的协议，因此不能直接切生产。

> 参数冻结提醒：历史 2026-07-19 残余杂音报告记录过 `decode_chunk_frames=3 / do_sample=true`，最新 2026-07-22 gateway README/验收记录使用 `decode_chunk_frames=12`。A/B 前必须从实际 Supervisor 环境读取并保存参数指纹，不能把历史配置当作当前基线。

## 哪些调整只省带宽，不省推理

| 调整 | 推理 RTF | 带宽/传输 | 结论 |
|---|---:|---:|---|
| Opus 64 → 48 kbps | 约 0% | 下行约 -25% | 仅弱网 A/B；不是推理优化 |
| Opus 64 → 32 kbps | 约 0% | 下行约 -50% | 中文合成音更易金属化，不建议正式赛 |
| PCM16 改成 MP3/AAC 后再传浏览器 | 约 0%，且增加编码 CPU/延迟 | 可省带宽 | 当前 WebRTC 已有 Opus，重复压缩反而更差 |
| 浏览器降低采样精度/音量 | 0% | 近似 0% | 只改变播放质量 |
| 24 kHz PCM 在 API↔MOSS 内部改 16 kHz | 不可直接做 | 内部流量较小 | 模型/codec 固定 24 kHz；会增加重采样，不能加速模型 |

当前实现证据：`apps/api/app/core/config.py` 固定 LiveKit 48 kHz/20 ms、mono Opus 64 kbps，DTX 关闭、RED 开启；`services/moss-realtime-gateway` 输出固定 24 kHz mono PCM16。

## 当前真实瓶颈

MOSS-Realtime 每约 80 ms 音频生成 1 个 frame。每个 frame 先跑 1.7B backbone，再由 local transformer **串行生成 16 个 RVQ codebook token**，随后 FP32 MOSS Audio Tokenizer 解码波形。

现有阶段观测（10 个长文本 turn）累计：

- `talker_step`：1011 次，共约 45.0 s，平均 44.5 ms；
- `decoder_yield`：97 次，共约 7.89 s，平均 81.3 ms；
- `prefill`：10 次，共约 0.91 s。

因此主瓶颈仍是 talker/backbone + 16 层 local generation；codec 约占可见计算时间的 15% 左右。即使把 codec 理想加速 2 倍，整轮理论收益也只有约 7%–8%，不能带来“翻倍”。

### 精度与采样参数

- **BF16 talker → FP16**：3090 同样有 Tensor Core，但没有证据表明 FP16 会显著快于 BF16；合理预期是 `0%–10%`，同时需验证数值稳定、音色和吞字。只能隔离 A/B。
- **FP32 codec → BF16/FP16/INT8**：当前固定 codec 存在必须保持 FP32 的路径，上游没有 Realtime 低精度 codec 的质量与 streaming 证据。本项目也已明确禁止直接 cast。即使成功，受 codec 占比限制，整轮收益通常不会“大幅”。
- **`do_sample=false`**：仅省掉少量 top-k/top-p/随机采样开销，无法减少 backbone 和 16 次 local transformer。历史生产曾在 `do_sample=false` 下出现超长生成、尾段撕裂和 `final_ack_timeout`，应保持 `true`。
- **减少 RVQ 层**：上游为 `MOSS-TTS-Local-Transformer` 提供 `n_vq_for_inference`，但 MOSS-TTS-Realtime 是固定 16 codebook 架构，未公开同等可变码率接口或训练保证。不能把 Local 模型的 32→8 层做法直接套到 Realtime。

## 内核与批次选项

| 候选 | 是否保持真 text-in/audio-out | 预期收益 | 风险/判定 |
|---|---|---:|---|
| 保持 SDPA + StaticCache + fixed-shape `torch.compile` | 是 | 当前基线 | **保留**。官方 L20 数据也是此路径：TTFB 180 ms、RTF 0.51 |
| FlashAttention2 + DynamicCache，关闭 local compile | 是（原生 session） | 未知，可能反而更慢 | Issue #38/PR #52 只解决正确性；FA2 与 StaticCache 不兼容，切 DynamicCache 后上游会禁用 local `torch.compile`。只做隔离 canary |
| `decode_chunk_frames` 12 → 15 | 是 | 整体大概率低个位数 | 只减少 codec 调用/固定开销，不减少 talker；可做首个安全 A/B |
| `decode_chunk_frames` 12 → 20/24 | 是 | 可能 3%–8%，收益递减 | PCM 批次更大、打断尾巴更长、队列抖动风险上升；不应直接生产 |
| `initial_chunk_frames` 6 → 12 | 是 | 不降低 RTF | 既有实验首 PCM P95 从约 1.25 s 恶化到约 2.0 s；否决 |
| async decoder/独立 CUDA stream | 是 | 理论可重叠 talker/codec | 当前同 GPU实测争用后 RTF更差；保持关闭 |
| 缩短 10 s 音色 Prompt | 是 | 既有短 Prompt 未获得延迟收益 | 可能损害音色稳定，不再作为主优化方向 |
| 只优化 top-k 后的 top-p 采样 | 是 | 预计低个位数 | 可保持精确采样语义，优先于关闭采样；需 kernel 级 canary |
| 固定 6/12/15 shape 编译 codec decoder/CUDA Graph | 是 | 预计 3%–10% | 不降精度的较优研发候选；需验证 streaming state、音质与 abort |

## 替代 runtime 可行性

### vLLM-Omni

官方已支持 `MossTTSRealtime`，A10G 配方声明约 180 ms 首音频并使用 `codec_chunk_frames=15`。但当前 WebSocket 实现明确把所有 `input.text` 缓存到 `input.done`，之后才一次生成音频；这是“文本收完后 audio-out streaming”，不是当前系统要求的真双向流式。生成期间也缺少现有 gateway 的 `audio_reset/released` 原子语义。

**判定：性能 canary 值得做，但不可直接替换。** 必须先在 engine 层实现持续 append、同一 generation、生成期 abort 和 release ACK。

### SGLang-Omni

当前官方文档支持 `MossTTSDelay` 与 `MossTTSLocal`，尚未支持 `MossTTSRealtime`。OpenMOSS issue #74 也说明 Realtime 是后续计划。

**判定：暂不进入 A/B。**

### MOSS-TTS-Nano

Nano 是约 100M 的不同模型与不同 Audio Tokenizer，48 kHz stereo，确实更轻、更快；但它不是 MOSS-Realtime 的多轮上下文模型，也没有当前持续 delta session 的等价合约。项目既有 Nano canary 还出现过 1/8 音色严重删字。

**判定：可作为独立低成本 TTS 产品线，不是当前 Realtime 的透明加速后端。**

## 安全 A/B 矩阵

所有 A/B 必须在独立端口/GPU lease，生产参数不热切换；同一 8 音色、同一中文语料、同一 WebSocket 协议、每组至少 20 轮。

| 组 | 变更 | 主要指标 | 通过条件 |
|---|---|---|---|
| A0 | 当前 SDPA/BF16/FP32/sample=true/DCF12/I6 | 基线 | 20/20 release，无 orphan |
| A1 | DCF15，其他完全不变 | RTF、首 PCM、underrun、abort 尾巴 | RTF P95 至少改善 5%，首 PCM不恶化 >100 ms，0 撕裂/吞字 |
| A2 | BF16 talker → FP16 | RTF、CER、音色相似、NaN/Inf | RTF P95 至少改善 8%，CER/音色/稳定性不退化 |
| A3 | FA2 + DynamicCache、local compile off | RTF、首 PCM、显存、输出一致性 | 必须显著优于 A0；任何短音频/不可懂立即淘汰 |
| A4 | codec decoder 固定 shape compile，仍 FP32 | decoder stage、整体 RTF、chunk gap | 整体 RTF P95至少改善 5%，PCM 边界/打断全部通过 |
| A5 | Opus 48 kbps | 浏览器弱网、金属感、丢包 | 只作为带宽档位；不以 RTF 作为收益 |

共同硬门：首可朗读 Agent delta 到浏览器首声 <3 s；整轮零卡顿、零中途结束；RTF P95 <0.9 且最好 ≤0.8；最大相邻采样跳变、CER、吞头吞尾、重复、20 轮音色漂移、reset 后旧 PCM=0 全部通过。

## 建议顺序

1. **不要为“加速推理”降低 Opus 码率。** 当前 64 kbps 已经不是瓶颈。
2. 先做 `DCF12 vs DCF15` 和 FP16 talker 的隔离 A/B；两者都不应预期翻倍。
3. 研发优先做“FP32 codec decoder 固定 shape compile/CUDA Graph”和精确的 top-k/top-p kernel 优化，比盲目降低 codec 精度更安全。
4. vLLM-Omni 仅建立旁路原型；未补齐真 append/cancel/release 前不得接生产。
5. 若目标是把 RTF 从约 `0.9` 大幅降到 `0.5–0.6`，应准备 L20/L40S/A100 类 GPU或完成专用 runtime 适配，而不是依赖音频压缩。

## 一手来源

- OpenMOSS 官方 Realtime 模型卡与 SDPA + `torch.compile` 指标：<https://github.com/OpenMOSS/MOSS-TTS/blob/main/docs/moss_tts_realtime_model_card.md>
- FlashAttention2/StaticCache 问题与 DynamicCache 方案：<https://github.com/OpenMOSS/MOSS-TTS/issues/38>
- 上游修复 PR #52：<https://github.com/OpenMOSS/MOSS-TTS/pull/52>
- vLLM-Omni MOSS 配方：<https://github.com/vllm-project/vllm-omni/blob/main/recipes/OpenMOSS/MOSS-TTS.md>
- vLLM 当前 streaming speech handler（缓存到 `input.done`）：<https://github.com/vllm-project/vllm-omni/blob/main/vllm_omni/entrypoints/openai/serving_speech_stream.py>
- SGLang-Omni 当前 MOSS 支持范围：<https://github.com/sgl-project/sglang-omni/blob/main/docs/cookbook/moss_tts_local.md>
- MOSS-TTS-Nano 官方仓库：<https://github.com/OpenMOSS/MOSS-TTS-Nano>
- 本地生产链路说明：`services/moss-realtime-gateway/README.md`
- 最新真双向与单 WebRTC 音轨验收：`docs/qa/round37-moss-streaming-20260722.md`
- 历史采样/分块漂移事故：`docs/qa/audio/20260719-residual-noise/report.md`
