# FlashAttention 2 混合路径审计（2026-07-22）

## 结论

OpenMOSS/MOSS-TTS PR #52 已经正确修复了 `flash_attention_2` 下
`DynamicCache` 的使用，但该修复同时关闭了 local transformer 的固定
`torch.compile` CUDA Graph。生产 RTX 3090 的真实灰度已经证明：纯
`flash_attention_2` 可以完成任务，却把 active RTF 推到 `2.72–2.77`，并
产生约 `3.57–3.69 s` 的最大块间隔；当前不能替代生产 SDPA。

代码审计确认 local transformer 每个音频帧只沿 16 个 codec channel 递推，
而且每个 turn 会重复执行这段逻辑。该分支使用 FlashAttention 2 的收益很
小，却会失去 local `StaticCache` 和 fixed-shape CUDA Graph，这是 RTF 恶化
的主要可解释原因。不要尝试让 DynamicCache 直接进入现有 fullgraph 编译，
因为 PR #52 明确将该组合关闭，强行编译会重新引入 cache mutation/shape
guard 风险。

生产固定模型配置进一步支持这个判断：语言 backbone 为 28 层，每个音频
frame 前向一次；local transformer 为 4 层，但每个 frame 按 16 个 codebook
依次前向，相当于 64 次很短的 decoder-layer 调用，local 最大位置也只有
33。对这类短序列，减少 Python/launch 开销和复用固定图，比把 local attention
换成 FlashAttention 2 更关键。

## 当前生产决策

FlashAttention 2 和混合 attention 不进入生产。PR #52 本身已经修复了
DynamicCache 兼容性，但真实语音门禁仍未达到要求。原先 `reduce-overhead`
在不同工作线程间捕获/回放 CUDA Graph 时出现的 TLS 断言，已经由持久单一
推理线程修复。当前生产配置为：

```bash
MOSS_GATEWAY_ATTN_IMPL=sdpa
MOSS_GATEWAY_LOCAL_COMPILE_MODE=reduce-overhead
```

`disabled` 仍作为故障回退开关保留；正式路径继续使用更适合短序列 local
transformer 的 SDPA + fixed-shape CUDA Graph，而不是为启用 FlashAttention 2
放弃该编译路径。

## 本地验证

- Gateway 单元测试：`61 passed, 1 warning`。
- Ruff：`All checks passed`。
- 覆盖了配置 fail-closed、环境变量读取、placement 报告、只修改 local
  config 的混合补丁和禁止非法 attention split。
- 这一阶段未切换生产 endpoint，也未改 API/Web/Room 或浏览器音频链路；
  后续空闲窗口灰度结果单列如下。

## 生产灰度计划（仅在独立窗口执行）

1. 先复制当前 SDPA run manifest，使用独立 endpoint/端口，不替换现有
   supervisor 服务。
2. 设置上面的两个环境变量，完成八音色完整 warmup，并确认 readiness。
3. 固定同一音色、同一文本、同一 decode 参数，至少执行 3×3 单路回归，
   再执行长文本回归。
4. 记录首个非静音 PCM、active RTF、最大块间隔、1200 ms 连续播放
   underrun、close/release ACK、GPU 显存和异常日志。
5. 只有同时满足：active RTF < 1、首 PCM < 3 s、最大块间隔 ≤ 200 ms、
   0 underrun、8 音色通过、无 OOM/cache 错误，才允许进入下一轮浏览器
   LiveKit 灰度。
6. 任一指标失败立即停止候选 endpoint，生产保持 SDPA；不得临时启用多
   播放器兜底。

## 生产空闲窗口混合灰度结果

在确认平台没有活动比赛、MOSS endpoint 为 `active=0 / pending=0 / orphan=0`
后，曾短暂启用 global FlashAttention 2 + local SDPA。服务完成八音色预热，
readiness 正确报告 `attention_implementation=flash_attention_2`、
`local_attention_implementation=sdpa` 和 `local_compile_effective=true`。

真实长文本 WebSocket 请求没有通过：第一轮约 15 秒后返回 `gateway_error`，
后两轮立即返回同类错误，未产生可验收 PCM，也未获得 release ACK。因此混合
方案只能保留为实验代码，不能进入生产。

灰度随后按门禁恢复 SDPA。历史上因跨线程 CUDA Graph TLS 断言曾临时切换为
`SDPA + local_compile=disabled`；2026-07-22 持久推理线程上线后，局部 CUDA
Graph 已重新启用并通过八音色真实回归。正式结论仍是：当前 RTX 3090 不启用
FlashAttention 2；只有它同时优于现有 SDPA 的 RTF、首帧、连续播放和稳定性
门禁时，才允许再次进入生产。

## 历史生产安全模式 smoke（2026-07-22，已被持久线程灰度取代）

切换完成后网关健康检查为 `ready`，placement 明确报告：

```text
attention_implementation=sdpa
local_compile_mode=disabled
local_compile_effective=false
active=0 / pending=0 / orphan_count=0
```

单路真实 WebSocket 回归能够完整完成 `start → text_delta → final →
audio_end → released`，并获得 release ACK；未再出现 CUDA Graph assertion 或
`gateway_error`。但安全模式的性能仍未达到实时门禁：首个非静音 PCM 约
1.50 s，active RTF 约 2.60，最大连续交付间隔约 1.56 s，1200 ms 播放模型
检测到 13 次 underrun。因此它是“稳定可回溯”的生产保护配置，不是最终的
实时质量优化。该段只记录回退模式的历史表现；后续持久推理线程灰度结果已经
达到 active `RTF < 1`，但仍不能据此宣称所有长文本“完全不卡顿”。

## 上游依据

- [OpenMOSS/MOSS-TTS PR #52](https://github.com/OpenMOSS/MOSS-TTS/pull/52)
  合并提交为 `b56a2874`，其关键改动是 FlashAttention 2 下 local
  transformer 使用 `DynamicCache`，并在 streaming helper 中关闭局部
  compile。
- 当前生产 checkout `ad99ec5f` 已包含该修复；本次补丁只在网关侧显式
  分离 local attention 配置，不修改上游 checkout。
