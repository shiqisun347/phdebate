# V2 OpenMOSS / MOSS-TTS-Realtime adapter gap audit

更新时间：2026-07-18（Asia/Shanghai）  
审计范围：当前仓库中的 OpenMOSS/MOSS realtime 代码、配置、测试、benchmark、README 与已有 QA 证据。  
执行边界：只读审计；除本报告外未修改代码、配置或部署，未运行 GPU 推理。

## 1. 结论

当前 V2 已经具备一个方向正确、默认关闭的 **MOSS 原生逻辑 session 客户端骨架**：Agent SSE 正文 delta 会先经过稳定短语聚合，然后以 `start + push + continuous PCM GET + close` 的方式送入同一个上游 MOSS turn；首个 PCM 可直接进入 growing WAV 和 LiveKit PCM 时钟，最终 WAV 在 close ACK 后原子发布。

但它还不是可发布的 OpenMOSS 生产适配器，当前总判定为 **NO-GO**：

1. 仓库中只有客户端，没有可部署、可固定版本、可观测资源释放的 OpenMOSS GPU 服务包装层；现有协议也不是一个双向持久 WS，而是多个控制 POST 加一个长连接音频 GET。
2. 所有实时开关默认关闭，没有任何真实 MOSS benchmark 产物；现有 MOSS 单测全部使用 fake `httpx.MockTransport`。
3. 代码没有保证“首个正文 delta 到浏览器首声每次都不超过 3 秒”。短语聚合在不足 10 字且无强标点时可以无限等待后续 delta；现有 benchmark 只测首文本提交后的首 PCM，不等于浏览器首声。
4. 固定 8 音色只存在于辅助映射和质量脚本；实际 seed 只循环使用 `debate_voice_1..4`，`.env.example` 也只配置 4 个 MOSS prompt，运行时 `voice_id` 没有强制限定为 8 个固定值。
5. interrupt 已能触发 Agent 请求、TTS abort、LiveKit generation revoke 和浏览器 flush，但 Agent ACK 被忽略；MOSS abort 路径不要求 close/release ACK 成功；本地 semaphore 会在上游资源是否真正释放未知的情况下归还。
6. 应用侧已经有最多 3 路、每 endpoint 默认 1 路的分片调度，但只用 fake 双 endpoint 验证过；没有真实三 endpoint、三比赛、20 轮、故障隔离和 GPU 资源回线证据。
7. room entry 只预建浏览器 AudioContext/LiveKit publisher；MOSS endpoint 没有 readiness、prompt cache warmup、模型 warmup、连接池预连或房间级预留。

## 2. 当前实际链路

```text
Debate Agent HTTP SSE
  -> providers.py 过滤 thinking/reasoning，只产出正文 delta
  -> SpeakableClauseAssembler
       首块默认 10–16 字；后续每块增长 6 字，最大 28 字
       强标点立即提交；达到安全长度后最多等 200ms
  -> IncrementalVoicePipeline
       已提交前缀不可被 Agent final 改写
  -> RealtimeTTSProviderRouter
       REALTIME_VOICE_BACKEND=moss_realtime 时选择 MOSS provider
  -> MossTTSRealtimeProvider
       POST /tts/session/start（第一块 + prompt 文件名）
       GET  /tts/session/{id}/audio（持续 PCM16）
       POST /tts/session/push（后续块）
       POST /tts/session/push is_final=true
       POST /tts/session/close
  -> PCM 同时写 growing WAV，并写入 LiveKit 48kHz/20ms 时钟
  -> 浏览器 LiveKit/Opus 播放；完成后原子发布最终 WAV
```

关键源码证据：

- 正文-only SSE：`apps/api/app/services/providers.py:201-269`、`apps/api/app/services/voice_runtime/text.py:158-182`。
- 短语聚合：`apps/api/app/services/voice_runtime/text.py:22-134`。
- 单逻辑 turn 的增量 session：`apps/api/app/services/providers.py:2011-2169`。
- PCM 流到 WAV/LiveKit：`apps/api/app/services/providers.py:1834-1983`。
- session 队列和 abort：`apps/api/app/services/providers.py:2196-2310`。
- 比赛引擎接入 Agent stream：`apps/api/app/services/match_engine.py:915-969`。
- LiveKit 房间预连与本地 flush：`apps/web/lib/audio/livekit-room-audio.ts:36-150`。

## 3. Objective-to-code gap matrix

| 目标 | 当前状态 | 已有代码/测试证据 | 关键缺口与发布要求 |
|---|---|---|---|
| Agent delta → 单 session PCM | **部分实现** | `generate_stream()` 只输出正文 delta；assembler 后调用一个 `MossTTSRealtimeIncrementalSession`；单测 `test_moss_realtime_incremental_session_streams_one_continuous_turn` 验证 final 前已有 PCM、后续文本走同一 session | 只是“同一服务端逻辑 session”，不是一个双向持久 WS：每块仍是独立 HTTP POST，音频另走 GET。每个 turn 新建 `httpx.AsyncClient`，没有跨 turn keepalive/preconnect 保证。没有真实 OpenMOSS endpoint 结果，也没有服务端实现/固定 commit/deploy artifact |
| 首声 ≤3s（从 Agent 首正文 delta 起） | **未证明，代码无硬保证** | `agent_first_readable_delta_at` 已写入 `audio.rtc.started`；浏览器 gate 可测解码后非静音样本；质量脚本可测 first-text→first-PCM | assembler 只有达到 10 字后才启动 200ms soft deadline；不足 10 字且无强标点时可等待任意久。MOSS benchmark 只 gate 三路 P95≤800ms，不要求 20/20 `<3000ms`，也不测首 PCM 是否非静音。现有浏览器 PASS 来自 synthetic PCM，不是 OpenMOSS。必须增加逐请求硬门、首非静音 PCM、真实浏览器链路 |
| 连续播放、不中断 | **机制存在，真实证据缺失** | 同一 audio GET 连续读取 PCM；LiveKit 使用 48kHz/20ms 时钟、有限队列；fake 单测验证同一 WAV 中保留两个 PCM 段；质量脚本有 chunk gap、边界静音和 100ms buffer stutter 模拟 | 没有真实 MOSS chunk cadence、长发言、标点边界或浏览器能量时间线结果。LiveKit 在上游断粮时会补静音，RTP 连续不代表听感连续。需要 active-generation underrun/silence-insertion 指标和真实 Chrome/Safari 长轮次测试 |
| 不吞词 / CER | **测试工具已有，运行证据为零** | `benchmark_tts_quality_gate.py` 已计算 CER、删除型吞字、首尾吞字、插入/替换；默认 CER P95≤2%、删除率 0 | 没有任何真实 MOSS `tts-quality-gate.json`；仓库仅有 fake 质量门结果。MOSS benchmark 和质量脚本使用固定字符切块，不复现生产 assembler 的标点/慢 delta/final 修订。运行时只有“音频时长过短”粗门，不能阻止部分吞词。必须使用真实 corpus、8 音色、生产分块语义；增加重复整词/循环短语自动门 |
| 固定多音色 | **设计 8 个，运行态实际 4 个且可越界** | `VOICE_IDS` 和 4v4 seat mapping 定义 `debate_voice_1..8`；质量脚本强制完整 8 音色 manifest | `seed.py:109-120` 把 8 个 Agent profile 循环绑定到 1..4；`.env.example:35` 只列 1..4；Match Engine 直接使用 profile 的任意 `voice_id`，admin schema 也未调用 `validate_voice_id()`；MOSS provider 对未配置 voice 自动拼成 `<voice>.wav`。必须固定 8 个 ID、8 份 prompt、manifest SHA，并在 API/启动时 fail closed |
| 普通话、中性音色 | **仅有候选资产** | 已准备 AISHELL-3 4男4女静态合规候选；质量脚本支持每音色 MOS 与 20 轮漂移代理 | 候选尚未安装到 MOSS endpoint，也未通过逐字稿听校、方言/角色腔筛查、MOS、盲听区分率、speaker identity。生产库存报告仍是 0/8 compliant。代码不能单靠 ID 证明中性普通话；必须冻结音色版本和人工验收证据 |
| interrupt ACK / 资源释放 | **局部机制，端到端不成立** | Agent 有 2s interrupt API；pipeline 会先调用 Agent interrupt，再 abort TTS；MOSS 正常 finish 必须 close ACK 后才发布 WAV；abort 单测验证 fake close、删除 part；LiveKit revoke/queue clear 和浏览器 flush 已有单测 | `debate_agent.interrupt()` 返回 bool，但 Match Engine 不检查；abort 的 close 失败只记 warning，仍释放应用 semaphore；close HTTP ACK 不证明 OpenMOSS worker/codec/GPU context 已退出；没有 active session/线程/显存回 baseline 指标；浏览器使用 `interrupt_id` 但不回 ACK。必须将 Agent ACK、MOSS drained ACK、队列清空时间和浏览器静音串成统一 interrupt timeline |
| 2–3 场 endpoint 分片并发 | **调度已写，只有 fake 2 路证据** | `_ensure_pool()` 全局上限 3，每 endpoint 默认 semaphore=1；`MOSS_TTS_REALTIME_URLS` round-robin 抢占空闲 endpoint；fake 测试验证两请求落到两个 host；benchmark 支持 1/2/3 路和多 endpoint round-robin | 没有 3 endpoint unit case、排队 cancel、坏 endpoint 隔离、endpoint readiness/circuit breaker、3 场真实房间/LiveKit/浏览器证据；没有任何真实 benchmark JSON。质量脚本只接受一个 TTS endpoint，无法精确复现 V2 三 endpoint pool，除非前面有等价负载均衡器 |
| room entry 预热 | **MOSS 缺失** | Web 进入房间时预建 ASR AudioWorklet、兼容播放器和 LiveKit subscriber；Engine 会预建 LiveKit publisher；质量脚本有排除在统计外的 warmup request | MOSS 没有 `/health/ready` 契约、模型/codec warmup、8 prompt token cache、endpoint 预连、房间级 session reservation；每轮在拿到第一文本后才新建 HTTP client、握手、start session。没有认证配置或 prompt cache 版本。必须在 room preparing 阶段只做安全预热，不提前占用长期 GPU turn |

## 4. 配置、文档和证据一致性问题

### 4.1 默认关闭是正确的，但当前配置不能直接开启

`apps/api/app/core/config.py:48-67` 和 `.env.example:26-39` 将 realtime pipeline 与 MOSS 开关默认设为 false，这是当前证据条件下正确的 fail-closed 行为。不能为了联调直接在生产打开。

开启前至少还缺：

- 固定的 OpenMOSS upstream commit、模型 revision、CUDA/PyTorch/推理参数；
- endpoint readiness/warmup/metrics 合约；
- endpoint 鉴权与 TLS/内网信任边界；
- 完整 8 prompt 映射及其 manifest SHA；
- 真实 1/2/3 路和 interrupt 证据目录。

### 4.2 README 只描述了候选，不是操作手册

`README.md:98` 正确说明了逻辑 session、每 endpoint 默认 1 active 和三 endpoint 分片，但没有：

- OpenMOSS 服务如何部署、固定哪个 commit、使用什么 GPU/compile/attention 参数；
- 如何检查 ready、warm、active session、GPU worker 已释放；
- 如何安装并核对 8 个 prompt；
- 如何执行真实 MOSS quality/browser release gate；
- 故障 endpoint 如何摘除和恢复。

当前可操作信息分散在历史 `docs/qa/audio/20260717-005403/` 中，不应作为生产 runbook 的唯一来源。

### 4.3 当前真实证据边界

- API 最终完整回归：`353 passed`，证明本地逻辑回归通过，不证明 GPU/音质/浏览器性能。
- MOSS provider 测试：全部为 fake transport；只证明客户端控制顺序和本地文件清理。
- MOSS transport benchmark：脚本存在，但 `docs/qa` 中没有真实 `moss-realtime-session-benchmark.json`。
- MOSS quality gate：脚本存在，但仓库只有 fake `tts-quality-gate.json`。
- 浏览器首声/interrupt：synthetic PCM 能力门通过；真实生产观察没有活动 RTC turn；无 OpenMOSS 真实结果。
- 音色：生产库存 0/8 compliant；AISHELL-3 8 音色只是静态候选。

## 5. 最小必须修改文件清单

以下是达到目标所需的最小产品面，不包含风格性重构。

### P0：运行时与 GPU 服务合约

1. `apps/api/app/services/providers.py`
   - 按 endpoint 复用持久 `AsyncClient`/连接池，不在每个 turn 内新建客户端；
   - 增加 ready/warm/metrics 探针和健康感知分片；
   - abort 必须获取“worker/codec 已 drained”的权威 ACK，失败时 endpoint 不得立即重新接单；
   - 记录 queue、first text、first non-silent PCM、close/drain、active baseline；
   - 强制 prompt manifest/version 与 PCM 参数一致。

2. `apps/api/app/core/config.py` 与 `.env.example`
   - 增加 MOSS 鉴权、readiness/warmup/metrics URL 或约定；
   - 强制 8 个 prompt 映射完整且 ID 只能是 `debate_voice_1..8`；
   - 固定 voice manifest SHA、endpoint 数量/容量和 release thresholds；
   - 配置校验必须阻止“max_active=3 但只有一个单路 endpoint”等伪容量。

3. 新增独立 OpenMOSS 服务包装与部署目录，例如 `deploy/moss-realtime/`
   - 固定 upstream commit、模型/codec revision、GPU/dtype/attention/compile 参数；
   - 实现 lifecycle-aware `/health/ready`、`/warmup`、session introspection；
   - `close` 只有在 command queue、decoder、worker 和 GPU context 真正退出后才能 ACK；
   - 输出 active/pending/session/worker、首 PCM、underrun、显存和错误指标。

4. `apps/api/app/services/match_engine.py`
   - room preparing 时并行预热可用 endpoint，但不提前创建长期占槽 turn；
   - 检查并记录 Agent interrupt ACK；
   - 将统一 `interrupt_id` 贯穿 Agent、TTS、LiveKit 和 browser 事件；
   - 首声超 3 秒、endpoint 未 ready 或 drain 失败时 fail closed/暂停比赛，禁止静默降级成假实时。

5. `apps/api/app/services/voice_runtime/pipeline.py`
   - 为“首个正文 delta → 首次 TTS 提交”增加硬 deadline；慢 delta/短句策略必须显式且可测试；
   - 将 Agent ACK、TTS drain ACK、已提交/未提交文本状态作为结构化 interrupt 结果上报；
   - benchmark 与生产使用同一 assembler，而不是另写固定字符切块。

### P0：8 音色运行态绑定

6. `apps/api/app/services/voice_runtime/voices.py`、`apps/api/app/services/seed.py`
   - seed 的 8 个 profile 一一绑定 `debate_voice_1..8`；
   - 实际 seat/profile 绑定复用同一固定 catalog；
   - 加载时核对 8 prompt/manifest，不完整即禁止 MOSS realtime enabled。

7. `apps/api/app/schemas/requests.py`、`apps/api/app/api/admin.py`
   - 管理 API 修改 `voice_id` 时调用固定 catalog validator，禁止任意字符串和缺失 prompt。

### P0：验证工具和回归

8. `scripts/benchmark_moss_realtime_sessions.py`
   - 增加每请求 `<3000ms` 20/20 硬门、first-non-silent PCM、final 前首 PCM、active baseline/drain 回线；
   - 加故障 endpoint、排队取消和三 endpoint 公平性记录。

9. `scripts/benchmark_tts_quality_gate.py`
   - 接受多 endpoint 并使用与 V2 相同的分片逻辑；
   - 支持 corpus 而不是单条文本；复用生产 assembler/delta timing；
   - 增加重复 n-gram/循环、插入率、削波和跨房串文本 gate；
   - cancel latency 必须以服务端 drained 状态为终点，不能以取消本地 stream task 为终点。

10. `apps/api/tests/test_providers.py`、`apps/api/tests/test_realtime_voice.py`、`apps/api/tests/test_platform.py`、`apps/api/tests/test_moss_realtime_benchmark.py`、`scripts/tests/test_tts_quality_gate.py`
    - 三 endpoint/三 turn；排队中取消；一个 endpoint 故障不污染其他房间；
    - close ACK 假绿、drain timeout、semaphore 不得过早归还；
    - Agent interrupt false/timeout；短 delta 首提交 deadline；8 音色越界和 manifest 缺失；
    - 完整比赛 interrupt 后未播放文本不进 history，旧 generation 不复活。

### P1：浏览器 ACK 与运行手册

11. 若“interrupt ACK”要求覆盖浏览器：`apps/web/lib/audio/livekit-room-audio.ts`、`apps/web/components/debate-stage.tsx` 和对应 API/WS ack 路由
    - 浏览器在 detach/pause/srcObject clear 完成后回传 `interrupt_id` ACK；服务端记录但不能等待 ACK 才执行本地撤销。

12. `README.md` 与 `docs/realtime-voice-rebuild.md`
    - 增加唯一、当前有效的 OpenMOSS deploy/runbook、开关顺序、回滚方式和真实 release gate 命令；
    - 删除/修正只配置 4 音色的示例，明确真实证据目录和 NO-GO 条件。

## 6. 验证命令

### 6.1 当前代码可立即运行的回归

```bash
cd /Users/sunshiqi/code/phdebate/platform

env PYTHONPATH=apps/api .venv/bin/python -m pytest -q \
  apps/api/tests/test_realtime_voice.py \
  apps/api/tests/test_voice_runtime.py \
  apps/api/tests/test_moss_realtime_benchmark.py

env PYTHONPATH=apps/api .venv/bin/python -m pytest -q \
  apps/api/tests/test_providers.py \
  -k 'moss_realtime or realtime_tts_router'

.venv/bin/python -m pytest -q scripts/tests/test_tts_quality_gate.py

env PYTHONPATH=apps/api .venv/bin/python -m pytest -q apps/api/tests
```

最后一条必须使用 `python -m pytest`，否则当前 console entrypoint 可能无法导入仓库根目录的 `scripts`。

### 6.2 真实三 endpoint transport gate

```bash
.venv/bin/python scripts/benchmark_moss_realtime_sessions.py \
  --endpoint http://127.0.0.1:<moss-a> \
  --endpoint http://127.0.0.1:<moss-b> \
  --endpoint http://127.0.0.1:<moss-c> \
  --voice-prompt debate_voice_1=debate_voice_1.wav \
  --voice-prompt debate_voice_2=debate_voice_2.wav \
  --voice-prompt debate_voice_3=debate_voice_3.wav \
  --voice-prompt debate_voice_4=debate_voice_4.wav \
  --voice-prompt debate_voice_5=debate_voice_5.wav \
  --voice-prompt debate_voice_6=debate_voice_6.wav \
  --voice-prompt debate_voice_7=debate_voice_7.wav \
  --voice-prompt debate_voice_8=debate_voice_8.wav \
  --concurrency 1 --concurrency 2 --concurrency 3 \
  --rounds 20 \
  --chunk-characters 12 \
  --delta-delay-seconds 0.05 \
  --output-dir docs/qa/audio/20260718-openmoss-rebuild/<run-id>/native-session
```

当前脚本仍需人工/外部 post-check 确认所有 records 都 `<3000ms`；仅 `transport_gate_passed=true` 不足以发布。

### 6.3 真实 8 音色质量门

现有质量脚本只接受一个 `--tts-endpoint`。只有在该 URL 前面是已经验证与 V2 分片等价的三 endpoint pool/load balancer 时，下面命令才能代表三场容量；否则必须先按第 9 项改造脚本。

```bash
.venv/bin/python scripts/benchmark_tts_quality_gate.py \
  --tts-mode moss-session \
  --asr-mode funasr-ws \
  --tts-endpoint http://127.0.0.1:<validated-moss-pool> \
  --asr-endpoint ws://127.0.0.1:<funasr> \
  --voice-manifest /absolute/path/to/compliant-eight-voice-manifest.json \
  --voice-prompt debate_voice_1=debate_voice_1.wav \
  --voice-prompt debate_voice_2=debate_voice_2.wav \
  --voice-prompt debate_voice_3=debate_voice_3.wav \
  --voice-prompt debate_voice_4=debate_voice_4.wav \
  --voice-prompt debate_voice_5=debate_voice_5.wav \
  --voice-prompt debate_voice_6=debate_voice_6.wav \
  --voice-prompt debate_voice_7=debate_voice_7.wav \
  --voice-prompt debate_voice_8=debate_voice_8.wav \
  --concurrency 1 --concurrency 2 --concurrency 3 \
  --rounds 20 --drift-rounds 20 --warmup-requests 8 \
  --initial-buffer-ms 100 \
  --max-first-pcm-ms 800 --max-cer 0.02 --max-swallowed-rate 0 \
  --max-stutters 0 --max-chunk-gap-ms 200 --max-cancel-ms 200 \
  --output-dir docs/qa/audio/20260718-openmoss-rebuild/<run-id>/quality
```

禁止使用 `--allow-incomplete-voice-manifest` 生成发布结论。

### 6.4 真实比赛浏览器首声与 interrupt

```bash
node scripts/run_browser_realtime_audio_gate.mjs \
  --url 'https://<host>/rooms/<code>/watch' \
  --mode first-sound \
  --sound-limit-ms 3000 \
  --timeout-ms 60000 \
  --output docs/qa/audio/20260718-openmoss-rebuild/<run-id>/browser-first-sound.json

node scripts/run_browser_realtime_audio_gate.mjs \
  --url 'https://<host>/rooms/<code>/watch' \
  --mode interrupt \
  --sound-limit-ms 3000 \
  --interrupt-limit-ms 250 \
  --stale-guard-ms 750 \
  --timeout-ms 60000 \
  --output docs/qa/audio/20260718-openmoss-rebuild/<run-id>/browser-interrupt.json
```

必须用受控真实 OpenMOSS AI turn，synthetic fixture 只能证明探针能力。

## 7. 推荐最小实施顺序

1. 先完成可观测、可真正 drain 的独立 OpenMOSS GPU 服务包装，并冻结 8 音色 manifest。
2. 再修 V2 provider 的持久连接、健康分片和 interrupt ACK；此时仍保持全部生产开关关闭。
3. 用真实 endpoint 先跑 c1 20/20、8 音色 CER/吞词/漂移，再跑 c2/c3 分片。
4. 最后接一个受控 QA 房，验证 Agent delta→浏览器首声、连续播放和 interrupt；通过后才按 QA 房→1v1→4v4 小流量灰度。

在上述证据完整之前，当前 `REALTIME_VOICE_PIPELINE_ENABLED=false`、`MOSS_TTS_REALTIME_ENABLED=false` 应继续保持。
