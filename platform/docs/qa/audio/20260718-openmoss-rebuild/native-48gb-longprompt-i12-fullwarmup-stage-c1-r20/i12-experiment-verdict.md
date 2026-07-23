# I12 实验判定：NO-GO，转入 R1/R2 执行内核重建

判定时间：2026-07-18（Asia/Shanghai）

## 结论

`decode_chunk_frames=12 / initial_chunk_frames=12`（下称 I12）被否决，不得作为正式实时语音配置，也不再继续通过扩大首块、缩短 prompt 或调整起播缓冲掩盖推理不足。

I12 的唯一正向结果是把 100ms 连续播放模拟中的卡顿样本从 I6 的 `20/20` 降到 `11/20`，最长单次卡顿从 `670.094ms` 降到 `243.812ms`。代价是首个非静音 PCM P95 从 `1248.667ms` 恶化到 `1996.391ms`，并且 `0/20` 请求能在 `final` 前产生 PCM。I12 没有解决实时计算能力，实质是等待 12 个 talker frame、先积累 960ms 音频后才首次解码。

短 prompt 已经没有得到延迟收益，I12 又证明扩大首块只能交换“较少卡顿”和“更晚首声”。配置级优化空间至此耗尽，满足 [实时语音重建方案](../../../../realtime-voice-rebuild.md) 中启动 R1（固定形状、可预热的推理状态）和 R2（talker/decoder 解耦）的触发条件。

## 对比口径

三组均为单个真实 GPU endpoint、WebSocket 正式协议、并发 1、20 轮、8 个固定音色轮转、每个正文块 12 字、块间延迟 50ms、`finish_delay=0`。其中 short prompt 与 I12 JSON 记录了相同的 36 字文本 SHA-256：`19b47735093938a941edd2559fcccb8df18944940cece91b63c42636785f0b67`。

I6 基线来自较早 schema，未写入文本 SHA 和原生阶段观测，因此只能比较端到端传输/模型指标，不能用它做逐阶段严格配对分析。

| 指标 | I6 基线：长 prompt，DCF12/I6，GC-off | Short prompt：DCF12/I6，full warmup | I12：长 prompt，DCF12/I12，full warmup | I12 判定 |
|---|---:|---:|---:|---|
| 成功 / close / release | 20/20 / 20/20 / 20/20 | 20/20 / 20/20 / 20/20 | 20/20 / 20/20 / 20/20 | 协议正常退出 |
| `final` 前首 PCM | 20/20 | 20/20 | **0/20** | **失败** |
| 首非静音 PCM P50 | 1099.005ms | 1142.655ms | **1720.557ms** | 恶化 |
| 首非静音 PCM P95 | 1248.667ms | 1303.188ms | **1996.391ms** | 超 800ms 门 `1196.391ms` |
| 首非静音 PCM P99 | 1264.257ms | 1385.072ms | **2087.674ms** | 超 1.2s 门 `887.674ms` |
| 首非静音 PCM 最大值 | 1268.154ms | 1405.543ms | **2110.495ms** | 失败 |
| 请求开始到首 PCM P50 | 1394.928ms | 1505.300ms | **2012.641ms** | 恶化 |
| 请求开始到首 PCM P95 | 1655.206ms | 1778.249ms | **2410.814ms** | 几乎耗尽浏览器 2.5s 总预算 |
| 请求开始到首 PCM 最大值 | 1710.491ms | 1884.089ms | **2454.687ms** | 加 200ms 正文聚合后已超过 2.5s |
| RTF P95 | 0.992 | 0.927 | **0.923** | 超 0.65 门 |
| Active RTF P95 | 0.977 | 0.913 | **0.910** | 仍缺少实时安全余量 |
| 单请求 chunk-gap P99 最大值 | 1124.817ms | 1061.455ms | **1171.719ms** | 超 200ms 门，且较两组都差 |
| 100ms 模拟：发生 underrun 的请求 | 20/20 | 20/20 | **11/20** | 有改善但仍失败 |
| 100ms 模拟：underrun 次数 | 47 | 47 | **12** | 不为 0 |
| 100ms 模拟：最长 underrun | 670.094ms | 627.030ms | **243.812ms** | 不为 0 |
| Benchmark lifecycle gate | FAIL | FAIL | **FAIL** | NO-GO |
| Benchmark release gate | NO-GO | NO-GO | **NO-GO** | NO-GO |

原始证据：

- [I6 基线 JSON](../native-48gb-ws-low-latency-dcf12-i6-gc-off-c1-20/moss-realtime-session-benchmark.json)
- [Short prompt JSON](../native-48gb-shortprompt-fullwarmup-stage-c1-r20/moss-realtime-session-benchmark.json)
- [I12 JSON](moss-realtime-session-benchmark.json)

## I12 阶段证据

I12 首块固定为 `960ms`。20 轮原生阶段观测显示：

| 阶段 | P50 | P95 | P99 | 最大值 |
|---|---:|---:|---:|---:|
| Prefill | 100.875ms | 118.879ms | 121.265ms | 121.861ms |
| 全部 talker step | 45.018ms | 76.190ms | 78.121ms | 82.783ms |
| 全部 decoder yield | 78.392ms | 138.002ms | 141.059ms | 148.432ms |
| 首 12 个 talker step 合计 | 884.305ms | 917.389ms | 925.391ms | 927.391ms |
| 首次 decoder yield | 117.414ms | 141.988ms | 147.143ms | 148.432ms |
| 首块在 turn 内可见 | 1504.081ms | 1641.159ms | 1709.089ms | 1726.072ms |

这组数据排除了“首包主要由 WebSocket 发送造成”的解释：仅首 12 个 talker step 的中位耗时已经达到 `884.305ms`，首次 decoder 又增加 `117.414ms`，首块在模型 turn 内的中位可见时间为 `1504.081ms`。I12 降低部分播放卡顿，是因为首次交付了更长音频，而不是 talker 或 decoder 变快。

## 为什么立即触发 R1/R2

### R1：固定形状、可预热的每声线推理状态

需要在 MOSS Gateway 执行内核中完成：

- 为 8 个固定声线建立不可变的 system + prompt prefix state/KV，每轮只复制 turn 私有可变状态，避免重复固定前缀工作，并保证 abort、下一轮和不同声线之间不污染。
- 将 talker history 限定为与 `repetition_window=50` 一致的固定窗口或 ring buffer，消除递增长度 tensor、重复全历史 stack 和运行期 shape 变化。
- warmup 后禁止新的 Dynamo recompile，并用逐 step CUDA Event 证明 post-warmup 快路径稳定。
- 在保持采样语义的前提下验证 top-k 候选内 top-p，减少每个 80ms frame 中 16 个 RVQ codebook 的全词表排序成本。

触发依据：短 prompt 没有降低端到端首包；I12 中 talker step P95 仍为 `76.190ms`，仅 talker 已接近或超过每帧 80ms 的实时预算，无法靠首块或播放缓冲修复。

### R2：talker 与 decoder 解耦

需要保持一个原生 turn、一个 codec context 和一个固定音色，同时重构内部调度：

- talker 将逐帧 audio token 写入有界 generation-scoped ring；首批恢复为 4–6 frame，稳态使用 12 frame。
- decoder 通过独立 worker 与 CUDA Event 消费 token；显存允许时验证独立 CUDA stream 的真实 overlap，不能继续同步阻塞 talker 主循环。
- decoder 产物立即进入现有 PCM/LiveKit 连续时钟队列；必须同时检查 queue lead、插入静音和 underrun，禁止只把大 PCM 块切成 20ms 来伪造低 gap。
- abort 清空 token、decoder output、Gateway audio queue 和 LiveKit 队列；等待 worker/codec/CUDA 工作真实退出后才允许 `released:true`。

触发依据：I12 首次 decoder P50 为 `117.414ms`，首块等待 12 个 talker frame 后才解码；这种串行结构只能在首声和播放稳定性之间交换，无法同时满足 800ms 首 PCM、0 underrun 和 active RTF P95 `≤0.65`。

R1/R2 只替换 Gateway 内部执行内核。现有正文-only 分块、200ms deadline、持久鉴权 WS、严格 seq/ACK、endpoint 隔离、原子 release、LiveKit generation 预缓冲、AudioWorklet interrupt gate、WAV 归档和质量门继续复用。

## 证据边界

- 本报告只覆盖真实 GPU 上的 MOSS 模型、Gateway 持久 WebSocket 和 PCM 产出；没有证明真实 Chrome/Safari 首声、LiveKit/Opus 传输延迟或浏览器声卡播放时间。
- `request start -> first PCM` 已包含连接/握手与模型首包，但不包含 LLM 首个可朗读字符之前的时间，也不包含正文聚合、LiveKit 和浏览器播放。表中“加 200ms 后超过 2.5s”只是保守算术推导，不是浏览器实测。
- 100ms underrun 是基于 PCM 到达时间线的连续时钟模拟，不是 30 分钟真实浏览器播放证据；I12 仍有 `11/20` 请求卡顿，不能据此启用生产。
- I6 JSON 来自较早 schema，没有原生阶段观测和文本 SHA；short prompt 与 I12 才能进行相同文本、阶段观测口径的直接比较。
- 本轮没有执行 CER、术语 CER、吞字/重复、音色漂移、盲听或 MOS；20 个 WAV 只是待质量验证样本。
- 本轮只有单 endpoint、并发 1；没有提供 2–3 个独立真 GPU endpoint、多房间或 30 分钟稳定性证据。
- `20/20 close/release` 只证明本轮协议资源正常释放；Benchmark 的 lifecycle/release gate 仍因首包、RTF、gap 和 `final` 前 PCM 条件失败。

## 远端资源清理

2026-07-18 11:51:44 CST 通过 SSH 对 canary 主机进行只读复核：

- 未发现仍在运行的 `moss-realtime-48gb-canary`、`moss_realtime_gateway` 或 `uvicorn` 长驻进程；`pgrep` 唯一匹配项是本次复核命令自身。
- 未发现 `8080` 或 `18xxx` 端口的相关监听器。
- 两张 GPU 利用率均为 `0%`；GPU 0 显存 `15MiB used / 24078MiB free`，GPU 1 显存 `15MiB used / 48505MiB free`。

因此本轮 I12 canary 的进程、端口和 GPU 显存资源已经清理。本检查不等价于生产应用、LightTTS 或主站 readiness 的完整健康验收，也不对未检查的其他服务作状态声明。
