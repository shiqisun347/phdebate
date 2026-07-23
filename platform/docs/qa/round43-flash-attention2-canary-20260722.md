# MOSS-Realtime FlashAttention 2 生产灰度记录（2026-07-22）

## 目标

在不改变双向流式协议、MOSS 模型、24 kHz PCM、LiveKit 单 WebRTC 音轨和浏览器播放链的前提下，将 MOSS-Realtime attention backend 从 SDPA 灰度为 `flash_attention_2`，并验证兼容性、RTF、首段音频延迟、显存和连续播放质量。

## 上游依据

- OpenMOSS/MOSS-TTS PR #52 已于 2026-03-02 合并，标题为 `Fix dynamic_cache when using flash_attention_2 in moss_tts_realtime`。
- 生产固定的 OpenMOSS 提交 `ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af` 提交于 2026-06-22，晚于 PR #52，且生产 checkout 已确认包含 `DynamicCache`、`_use_dynamic_local_cache` 与 `flash_attention_2` 分支。
- PR #52 的关键行为不是让 FlashAttention 2 继续使用原来的静态缓存，而是在该模式下为 local transformer 使用 `DynamicCache`，并关闭其局部 `torch.compile` CUDA Graph。

## 实施变更

- 网关配置允许 `sdpa` 和 `flash_attention_2`，仍拒绝 `eager` 等未批准模式。
- 生产部署清单仍固定为 `sdpa`；`run-endpoint.sh` 支持通过环境变量显式覆盖，FlashAttention 2 仅作为实验开关。
- 健康信息增加 `attention_implementation` 和 `local_compile_effective`，避免 FlashAttention 2 模式仍误报 CUDA Graph 生效。
- 保持 codec decoder 同步、12-frame decode、单任务并发、24 kHz PCM、LiveKit Opus 单音轨和 180 ms 浏览器 playout delay 不变。

## 依赖构建

- 服务器 PyTorch 为 `2.9.1+cu128`，Transformers 为 `5.0.0`，GPU 为 RTX 3090。
- 服务器初始没有安装 `flash-attn`。
- 第一次源码安装发现系统默认 CUDA 13.0 与 PyTorch cu128 不匹配，未安装、未影响在线网关。
- 第二次固定 `CUDA_HOME=/usr/local/cuda-12.8`，并只为 RTX 3090 的 `sm_86` 构建，避免编译无关架构。

## 实际结果

真实网关单路 WebSocket 3 轮（同一 RTX 3090、同一音色、同一文本）结果：

- 首个非静音 PCM：`1425.807 / 1431.513 / 1466.088 ms`。
- active RTF：`2.7692 / 2.7705 / 2.7227`。
- 最大块间到达间隔：约 `3.57–3.69 s`。
- 1200 ms 连续播放模拟：每轮 `7–8` 次 underrun，最大 underrun 约 `1.67–1.79 s`。
- 所有任务均完成 close ACK/release ACK，没有 CUDA OOM 或 DynamicCache 崩溃。

结论：PR #52 的修复已验证“可以启动并完成任务”，但当前硬件与实时增量路径下 FlashAttention 2 不满足 RTF<1 和连续播放门，因此生产已回退 SDPA + 固定 CUDA Graph。`flash_attention_2` 代码和依赖仍保留，后续只有在新的内核/混合 attention 方案达到门槛后才重新灰度。

回退后的 SDPA 同文本、同音色 3 轮回归：首个非静音 PCM `521–638 ms`，active RTF `0.887–0.915`，1200 ms 连续播放模拟 `0` 次 underrun；网关 readiness placement 为 `attention_implementation=sdpa`、`local_compile_effective=true`。

## 发布门槛

FlashAttention 2 只有同时满足以下条件才保留为正式路径：

1. 完整八音色暖机成功，网关 readiness 为 200。
2. 实际 health placement 显示 `attention_implementation=flash_attention_2`、`local_compile_effective=false`。
3. 长文本 RTF 小于 1，首段非静音 PCM 在 3 秒预算内。
4. 连续生成无异常结束、NaN、CUDA OOM、cache shape 错误或 orphan task。
5. WebRTC 端仍只有一个 `agent-tts` 音轨，不启用替代播放器。

如任一门槛失败，只回退 attention backend 到 SDPA；不切换 TTS 模型或浏览器播放器。
