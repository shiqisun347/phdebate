# OpenMOSS native 48GB real canary

## 结论

**NO-GO。** 这组证据证明固定版本的 MOSS-TTS-Realtime 可以在单张约 48GB CUDA GPU 上以完整模型、完整 codec 和 8 个音色 prompt 运行，也证明稳定 inferencer、逐 step 低延迟 bridge 和真实 gateway interrupt 均解决了明确的控制流问题。但目前单路真实 WebSocket 仍未达到首个非静音 PCM、RTF、块间隔和连续播放门槛；真实 c2/c3、LiveKit 浏览器链路和语音质量门也尚未完成，因此不能据此部署或宣称已满足 2–3 场并发比赛。

## 固定版本与硬件边界

- 源主机：`btbu-6201`；原始来源目录：`/home/ubuntu/sunsq/moss-realtime-48gb-canary-20260718`。
- GPU 证据只足以确认单张 CUDA GPU、可见总显存 `48,519.06 MiB`。现有产物没有记录 GPU 型号，本文不推断具体型号。
- OpenMOSS upstream：`ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af`。
- Realtime model revision：`6acbc7f161a0db71c291f2d0aaa9eee59334cab2`。
- codec revision：`3cd226ba2947efa357ef453bcad111b6eafba782`。
- attention：SDPA。
- placement：Realtime model、codec encoder、quantizer、decoder 均为 `cuda:0`，使用完整 codec streaming context；8 个 prompt 已缓存。
- prompt 准备后 GPU 使用约 `12,296.75 MiB`。编译预热轮不计入正式延迟门。

固定版本、placement、显存和早期同进程结果见 [evidence-inventory.md](raw/evidence-inventory.md) 与 [首次失败 JSON](first-dynamic-class-failure/openmoss-native-48gb-canary.json)。

## 失败尝试与修复证据

### 1. 每轮动态 inferencer class：失败

最初实现每轮创建新的 `CancellableInference` subclass。真实日志显示 Torch Dynamo 因 `type(self)` 被释放而反复失效，最终触发：

```text
RecompileLimitExceeded: cache_size_limit reached
last reason: Cache line invalidated because type(L['self']) got deallocated
```

该轮只留下编译预热 WAV，没有可计入门槛的完整 measured turn。证据见 [canary.log](first-dynamic-class-failure/canary.log)。

### 2. Stable inferencer：修复重复编译，但性能仍未过门

修复后在 backend 生命周期内复用同一个 inferencer，并在每轮替换独立 cancel event、执行 `reset_generation_state(keep_cache=False)`。同进程真实 measured turn 不再出现上述 recompilation failure：

| 配置 | measured-1 首 PCM / RTF / gap P99 | measured-2 首 PCM / RTF / gap P99 | 判定 |
| --- | --- | --- | --- |
| stable，DCF6/ICF1 | 384.8ms / 0.717 / 328.3ms | 254.2ms / 0.720 / 327.8ms | 首 PCM 通过；RTF 与 gap 失败 |
| DCF3/ICF1 | 388.3ms / 0.851 / 200.8ms | 256.2ms / 0.931 / 623.3ms | RTF 失败；第二轮 gap 失败 |
| DCF6/ICF1 + playback simulation | 384.8ms / 0.723 / 332.8ms | 259.7ms / 0.801 / 764.3ms | 第二轮在 80/100/120ms 初始缓冲均发生一次 underrun |

这里的 release 门为首 PCM不超过800ms、RTF不超过0.65、gap P99不超过200ms。详细数据与 WAV 位于 [raw evidence inventory](raw/evidence-inventory.md) 所列目录。

### 3. Low-latency bridge：首帧更早可见，但单路仍失败

固定 upstream bridge 会先同步构造完整 pending audio-token 列表，再开始 decoder yield。gateway 专用 adapter 改为在 prefill 和每个 `inferencer.step` 后立即执行 sanitize、送入 `AudioStreamDecoder` 并 yield；finish 也逐 step drain，最后 flush。它保留固定版本的分段、分词、EOS/invalid token 和 cancel 边界，不修改 upstream checkout。

真实单路 WebSocket 20 轮对比结果如下。两轮测试的 finish delay 不同，因此只能作为阶段性工程证据，不能视为严格隔离 A/B：

| 真实 WS 运行 | 成功/释放 | 非静音首 PCM P95 | RTF P95 | gap P99 max | 100ms playback | 判定 |
| --- | ---: | ---: | ---: | ---: | --- | --- |
| 原 bridge，c1×20 | 20/20 | 1,437.0ms | 1.295 | 2,423.8ms | 未记录 | NO-GO |
| low-latency bridge，c1×20，finish delay 0 | 20/20 | 885.9ms | 1.024 | 846.3ms | 20/20 请求 underrun，共76次，最大476.6ms | NO-GO |

证据：[原 bridge benchmark](../native-48gb-ws-c1-20/moss-realtime-session-benchmark.md)、[low-latency benchmark](../native-48gb-ws-low-latency-c1-20-finish0/moss-realtime-session-benchmark.md)。

### 4. DCF12 + I6：没有形成 release candidate

目录标签中的 `DCF12 + I6` 表示 `decode_chunk_frames=12`、`initial_chunk_frames=6`。该配置的 c1×20 虽然 20/20 成功、均在 final 前收到 PCM并完成 release，但非静音首 PCM P95为 `1,235.5ms`、RTF P95为 `0.991`、gap P99 max为 `3,282.2ms`，100ms playback 20/20 请求均 underrun。因此这不是对 low-latency c1 结果的改进。

证据：[DCF12+i6 benchmark](../native-48gb-ws-low-latency-dcf12-i6-c1-20/moss-realtime-session-benchmark.md)。DCF/I6 参数来自运行目录标签；benchmark JSON 本身没有单独序列化这两个 gateway 参数。

### 5. GC-off：已完成 c1×20，仍为 NO-GO

`native-48gb-ws-low-latency-dcf12-i6-gc-off-c1-20` 目录已经包含完整 benchmark 和样本。该轮 20/20 成功并释放，但非静音首 PCM P95为 `1,248.7ms`、RTF P95为 `0.992`、gap P99 max为 `1,124.8ms`；100ms playback 20/20 请求 underrun，共47次，最大670.1ms。关闭 GC 没有使当前组合达到门槛。

证据：[GC-off benchmark](../native-48gb-ws-low-latency-dcf12-i6-gc-off-c1-20/moss-realtime-session-benchmark.md)。`GC-off` 配置归属来自运行目录标签，benchmark JSON 没有独立记录 Python GC 状态，故本文不把它当作可自证的参数元数据。

## 真实 interrupt：gateway WS 5/5 通过

在已经观察到首 PCM 后发送 abort，共执行5次真实 gateway WebSocket interrupt：

- `audio_reset`：`102.249–196.206ms`；
- `released`：`102.351–196.276ms`；
- 5/5 的 `released_status=aborted`、`released=true`；
- 5/5 在 reset 前观察到的 post-abort PCM frame 为0；
- 每次结束后 `health_active=0`、`health_orphan_count=0`。

因此 gateway 层“abort → 清音频 → audio_reset → released”在这5次样本中达到250ms门。但这不是完整浏览器打断证明：它没有覆盖 Agent 未播放文本回滚、真实 LiveKit 房间、浏览器 AudioWorklet flush 和扬声器尾音。

证据目录：[native-48gb-real-interrupt](../native-48gb-real-interrupt/)。

## LiveKit 800ms 的证据边界

当前 `moss_tts_livekit_start_buffer_ms=800` 的 frame 对齐、app queue约束和 provider 首次写入聚合已有本地自动化测试。它证明本地代码按800ms起始缓冲配置组织 PCM，不证明：

- 真实 LiveKit server 已接入这台48GB native gateway；
- Chrome 或 Safari 已通过 WebRTC 播放真实 OpenMOSS 音频；
- 800ms 缓冲下没有网络抖动、浏览器 underrun 或中断尾音；
- Agent 首正文到浏览器实际出声满足2.5秒预算。

因此“LiveKit 800ms”在本轮只能标记为**本地测试通过，真实端到端未测**，不能与上述真实 gateway WS benchmark 合并为生产结论。

## 尚未关闭的 NO-GO 缺口

1. 单路真实 WS release gate仍失败：当前最好的 low-latency c1×20 非静音首 PCM P95 `885.9ms`、RTF P95 `1.024`、gap P99 max `846.3ms`，均高于 `800ms / 0.65 / 200ms` 门槛。
2. 没有真实 native c2/c3 端点各20批证据，不能证明至少2–3场比赛并发，也没有 starvation/多房间排队延迟结论。
3. 没有把 native gateway、LiveKit publisher、真实 LiveKit server、Chrome/Safari、AudioWorklet 和扬声器串成一次可复核的端到端测试。
4. 5/5 interrupt只证明 gateway WS 边界；完整 Agent interrupt、TTS cancel、服务端队列清空、LiveKit generation revoke、浏览器 flush、未播放文本剔除仍需联合实测。
5. 这些 benchmark 明确不覆盖 CER、吞字/重复、块间静音、音量一致性、MOS/听感、8音色一致性和跨轮 speaker drift；尚无 TTS→ASR 回转质量门。
6. 尚未测 Agent 首个正文 delta到浏览器首个非静音样本的真实总延迟，因此不能宣称满足“LLM 首字后2.5秒内出声”。

在上述缺口关闭前，48GB native OpenMOSS 路径保持 canary/NO-GO，不部署为正式赛主链路。
