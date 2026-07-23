# OpenMOSS GPU 可复现验收矩阵与现有工具缺口

更新时间：2026-07-18（Asia/Shanghai）  
性质：只读代码与 QA 资产审计；本轮未运行 GPU 推理、未修改生产代码、未部署。

## 1. 结论

现有工具已经能够复用来验证：

- MOSS 原生 `start → incremental push → PCM stream → close` session；
- 首 PCM、RTF、HTTP PCM chunk arrival gap、close ACK；
- TTS→ASR 回转 CER、删除型吞字、首尾吞字、插入型多字；
- 100ms 初始缓冲下的离线 underrun/stutter 模拟；
- 1/2/3 路并发的客户端观测排队增量；
- 每音色 20 轮的响度、时长、过零率、峰均比漂移代理；
- LiveKit 20ms PCM 时钟、有限队列、首帧 capture、generation 撤销和房间隔离；
- Chrome 解码后非静音首样本、interrupt 后 flush/静音以及旧 generation 复现。

但当前还不能直接宣布 OpenMOSS GPU 可发布，原因是这些证据尚未由一个统一 run ID 串成真实链路，且缺少以下硬证据：

1. GPU 型号、驱动、CUDA、模型 commit、推理参数、显存、利用率、OOM/重启的持续遥测。
2. “20/20 每个请求首 PCM <3s”的逐请求硬门；现有 MOSS benchmark 只在摘要里判断三路 P95≤800ms。
3. 活跃 LiveKit generation 内由 TTS 供给不足造成的 silence insertion/underrun 计数；LiveKit 当前会按连续时钟补静音，因此仅看 RTP 连续不能证明 TTS 没有卡顿。
4. 重复整词、重复短语、循环片段的专门门；现有质量门计算字符插入率，但没有把重复指标纳入自动 gate。
5. 经验证的 speaker embedding 或 20 轮人工身份听辨；当前自动漂移只是声学代理。
6. 从比赛控制动作开始，到 TTS session close ACK、服务端两级队列清空、浏览器静音的统一中断时钟。
7. 2–3 个真实比赛房间通过完整 Agent→OpenMOSS→LiveKit→Chrome/Safari 链路同时运行的 20 轮编排。

因此验收应分为“GPU endpoint 组件门”“质量门”“LiveKit 媒体门”“真实浏览器/比赛门”四层，任何一层不能用另一层替代。

## 2. 固定验收对象

每次验收必须在报告中固定并哈希：

| 类别 | 必填项 |
|---|---|
| GPU | 型号、总显存、驱动、CUDA runtime、PyTorch、compute capability、MIG/共享状态 |
| 模型 | MOSS-TTS-Realtime commit、模型权重 revision、audio tokenizer revision |
| 服务 | 服务包装层 commit、启动命令摘要、endpoint 数量、每 endpoint admission 上限 |
| 推理 | dtype、SDPA/FA2、`torch.compile`、prefill token 数、initial/subsequent chunk frames、sampling 参数 |
| 音色 | 固定 `debate_voice_1..8` 映射、每个 prompt 音频与逐字稿 SHA-256、服务端 prompt token cache 版本 |
| 媒体 | LiveKit Server/client 版本、Opus/20ms 路径、source/app queue 配置、浏览器版本 |
| 语料 | corpus manifest SHA-256、ASR 模型 revision、文本规范化版本 |

当前 `docs/qa/audio/20260718-realtime-voice-rebuild/voice-assets-manifest.json` 仍显示：

- 固定 8 音色中只有 4 个存在音频/提示配置；
- `compliant=0/8`；
- `debate_voice_5..8` 缺失。

仓库已有 8 个 AISHELL3 candidate，但它们仍是候选资产。正式 GPU gate 前必须生成一个新的、完整合规的固定 8 音色 manifest，并确认相同 prompt 已安装到每个 MOSS endpoint；不能用旧的 0/8 manifest 绕过正式门。

## 3. 发布验收矩阵

### G0：GPU、版本与暖机

| 项目 | 样本/时长 | 硬门 | 现有工具 | 缺口 |
|---|---:|---|---|---|
| GPU 实际执行 | 启动一次 | 模型与 codec tensor 均在 CUDA；无 CPU-only 冒充 | 无 | 必须新增 GPU preflight |
| 显存 | idle/warmup/c1/c2/c3 | 无 OOM；c3 峰值后仍保留预设安全余量，建议≥15% | 无 | 必须新增 NVML/nvidia-smi 采样 |
| readiness | 每次重启 | compile、8 音色 prompt cache、真实短句 warmup 完成前 false | 服务自身 | benchmark 不验证 readiness 状态机 |
| 版本固定 | 每个 run | commit/revision/config/manifest SHA 完整 | 现有报告只记部分参数 | 必须新增 run manifest |
| 稳定性 | 30min c3 | 服务不重启、不泄漏 session/线程/显存 | 无 | 必须新增 watchdog 与进程遥测 |

### G1：20/20 首 PCM 与基础流式能力

| 项目 | 样本 | 硬门 | 现有工具 | 处理方式 |
|---|---:|---|---|---|
| 单路首 PCM | c1 20 个 session | **20/20 <3000ms**；同时要求 P95≤800ms | `benchmark_moss_realtime_sessions.py` | 可复用 records；必须新增逐请求 20/20 gate 或独立 post-check |
| 首 PCM 内容 | 同上 | 首 chunk 必须含连续非静音 PCM，不能只是全零 pre-roll | 无 | 必须新增 first-non-silent PCM 时间 |
| 增量语义 | 每个 session | final 前收到 PCM；后续 delta 使用同一 session/context | MOSS session benchmark 部分覆盖 | 必须记录 first PCM 相对 final push 的先后关系 |
| close 回收 | 全部 session | close ACK 100%；session/worker 数回到 baseline | benchmark 有 close ACK | worker/GPU 资源 baseline 必须新增 |
| RTF | c1/c2/c3 | c3 P95≤0.65，P99≤0.80 | benchmark 已有 | 可直接复用 |

“20/20”不允许按 P95 替代：任何一个有效请求 ≥3000ms、无 PCM、首 PCM 全静音或异常结束，G1 即失败。慢样本不能删除。

### G2：PCM gap、underrun 与连续播放

| 项目 | 场景 | 硬门 | 现有工具 | 证据边界/缺口 |
|---|---|---|---|---|
| endpoint chunk gap | c1/c2/c3 | 每请求 chunk-gap P99≤200ms；单 gap>250ms 为0 | MOSS benchmark、quality gate | HTTP chunk 边界可能被代理合并，不等于模型 frame cadence |
| 离线播放模拟 | 初始 buffer 80/100/120ms | 100ms 下 stutter=0、underrun=0；80ms 记录灰度结果 | `benchmark_tts_quality_gate.py` | 可复用，但它不是浏览器真实 jitter buffer |
| chunk 边界静音 | 所有质量样本 | max≤200ms；>200ms count=0 | quality gate | 可直接复用 |
| LiveKit 活跃期补静音 | c1/c2/c3 30min | active generation 内意外 silence insertion=0 | 无 | 必须在 publisher 增加只读指标或外部 PCM tap |
| 浏览器实际卡顿 | Chrome/Safari c1/c3 | underrun、click/pop、异常静音=0 | browser probe 只测首声/中断 | 必须扩展多轮能量时间线和 WebRTC stats |
| jitter buffer | Chrome/Safari c1/c3 | 不持续增长；无数秒不可取消 backlog | 无 | 必须新增 `RTCRtpReceiver.getStats()` 采样 |

LiveKit publisher 当前在应用队列为空时发送 20ms 静音帧。该设计保持 RTP 时钟稳定，但会把 TTS 供给不足表现为“连续 RTP 中的可听静音”。因此 LiveKit packet loss=0 不能替代 active-generation silence/underrun=0。

### G3：CER、吞词、重复与音频质量

| 项目 | 语料 | 硬门 | 现有工具 | 缺口 |
|---|---|---|---|---|
| CER | 完整项目 corpus × 8 音色 | 全集 CER P95≤2%；任何单句>10%人工复核 | quality gate | 脚本一次只接受一条 `--text`，必须新增 corpus orchestrator |
| 删除型吞词 | 同上 | P95≤0.5%；首字/末字/末句缺失=0 | quality gate 已计算 | 默认参数为更严的0，可复用 |
| 插入/多字 | 同上 | insertion rate P95≤0.5% | quality gate 已计算但未 gate | 必须新增 automatic check |
| 整词/短语重复 | 短句、标点、长句 | 重复整句、循环短语、连续重复 n-gram=0 | 无专门 gate | 必须新增重复检测与人工复核标记 |
| 无关内容/串文本 | c2/c3 | A 文本出现在 B 输出=0 | ASR 可辅助 | 必须新增跨请求混淆矩阵 |
| 音量一致性 | 全部样本 | RMS range≤3dB，无削波 | quality gate | 削波样本率应新增明确 gate |
| 人工 MOS | 每音色 | 平均≥4.0/5，任一音色<3.8失败 | 可读取 MOS JSON | 需要真实盲听数据 |

建议 corpus 至少包含：

- 200 条普通话辩论句；
- 50 条 1–8 字短句；
- 30 条 100–250 字长发言；
- 30 条标点、数字、日期、中英混排陷阱；
- 不规则 delta：1–4 字碎片、50–600ms 间隔、无标点长串和 final 修正。

### G4：20 轮音色漂移

| 项目 | 样本 | 硬门 | 现有工具 | 缺口 |
|---|---:|---|---|---|
| 自动声学代理 | 8音色×20连续轮 | 每音色20/20成功；RMS range≤3dB；时长/字 CV≤0.10；ZCR CV≤0.15；crest range≤3dB；首尾吞字=0 | quality gate | 可直接复用 |
| speaker identity | 8音色×20轮 | 每轮更接近自己的 prompt/centroid；最近其他音色 margin 达到校准阈值 | 无 | 必须新增 speaker embedding gate |
| 轮内漂移 | 长句首/中/末 | 同一句首/中/末身份无突变 | 无 | 必须分段 embedding |
| 人工身份听辨 | 8音色×20轮 | 音色切换/串音=0；固定席位识别率≥80% | quality gate 明确 BLOCKED | 必须人工盲听或经验证 embedding 替代 |

speaker embedding 阈值不能凭空固定。应先用同一 prompt 说话人的多条真实录音建立 positive 分布、8 个不同说话人建立 negative 分布，然后冻结：

- generated→own centroid 的 P5 下限；
- own centroid 相对 nearest-other centroid 的 P5 margin；
- 同一长句首/中/末最大距离。

校准集、evaluator revision 和阈值必须写入 run manifest。

### G5：interrupt ≤250ms

| 时钟段 | 样本 | 硬门 | 现有工具 | 缺口 |
|---|---:|---|---|---|
| candidate close | 20 次 | close request→stream EOF/worker ACK≤200ms | quality gate Moss adapter | 当前是客户端 cancel + close ACK，不证明 worker/GPU context 退出 |
| 服务端清队列 | 20 次 | interrupt request→app queue+AudioSource queue cleared≤100ms | LiveKit 单测验证逻辑 | 无生产时钟/指标 |
| 浏览器 flush | 20 次 | `audio.rtc.interrupt`→pause/srcObject clear/持续静音≤250ms | browser probe | 可复用真实 canary；synthetic 结果不能替代生产 |
| 旧 generation | 每次750ms观察 | 静音后旧音频复现=0 | browser probe | 可复用 |
| 完整链路 | 20 次 | 控制动作→浏览器持续静音≤250ms | 无 | 必须新增统一 interrupt_id 与跨层时间线 |
| 资源回收 | 每次 | 活跃 session、线程、GPU allocation 在2s内回 baseline | 无 | 必须新增 endpoint introspection/watchdog |

现有 LiveKit 实现会：撤销 generation、清应用队列、清 AudioSource queue、等待一个20ms frame 后再次清队列；单测也验证 revoked generation 不能继续写入。这证明机制存在，但不能证明真实 GPU+网络+浏览器全链路的250ms时限。

### G6：2–3 场并发

合理负载是每场最多一个当前 AI 发言流；2场=2条流，3场=3条流。

| 场景 | 样本 | 硬门 |
|---|---:|---|
| c2 endpoint | 20 个并发 batch，共40 session | 40/40成功；每请求首 PCM<3s；无串音/串文本；queue P95≤500ms |
| c3 endpoint | 20 个并发 batch，共60 session | 60/60成功；每请求首 PCM<3s；P95≤800ms；RTF P95≤0.65；无 starvation |
| c3 30min | 3场×至少20 AI回合 | 无 OOM/重启/显存持续增长；PCM underrun=0；各场失败隔离 |
| fault injection | 10% cancel、5% endpoint fault | 单 endpoint 故障不污染其他两场；排队可取消；无跨 generation 音频 |
| browser c3 | Chrome/Safari 每浏览器至少20轮 | WebRTC 首声、连续播放、interrupt 均通过 |

`benchmark_moss_realtime_sessions.py` 和 quality gate 可生成 c2/c3 endpoint 负载，但不创建真实房间，也不经过 LiveKit/浏览器，因此只覆盖 G6 的第一层。

### G7：真实浏览器首声

| 场景 | 样本 | 硬门 | 现有工具 | 缺口 |
|---|---:|---|---|---|
| Chrome c1 | 20 个真实 AI 回合 | Agent首个正文delta→连续非静音首声 P95≤2.0s，20/20 max≤2.5s | browser probe | 需要真实OpenMOSS canary批量runner |
| Chrome c3 | 3房×20回合 | 每房20/20 max≤2.5s；无串房音频 | browser probe | 需要三房控制/观战编排 |
| Safari c1/c3 | 同Chrome | 同一门槛 | Computer Use只能验证连接/按钮 | 必须使用Web Inspector/测试构建探针或受控回环 |
| PCM→浏览器分段 | 全部样本 | server first capture→首声 P95≤500ms | browser probe | 跨时钟必须校准或同时报告浏览器单调时钟边界 |

synthetic fixture 只证明探针和浏览器媒体图可工作；它不能替代真实 OpenMOSS、LiveKit SFU 和公网浏览器证据。

## 4. 现有脚本复用清单

### 4.1 可直接复用

#### `scripts/benchmark_moss_realtime_sessions.py`

可复用：

- 原生 session 生命周期；
- 增量文本 push；
- c1/c2/c3 与 endpoint round-robin；
- 首 PCM、RTF、chunk gap、WAV、SHA-256、close ACK；
- 每请求原始 records。

不能证明：

- GPU 真实执行或显存健康；
- first PCM 非静音；
- 20/20 max<3s 自动判定；
- CER、吞词、重复、音色漂移；
- 浏览器/WebRTC；
- worker 实际退出。

建议首轮命令骨架：

```bash
.venv/bin/python scripts/benchmark_moss_realtime_sessions.py \
  --endpoint http://127.0.0.1:<endpoint-a> \
  --endpoint http://127.0.0.1:<endpoint-b> \
  --endpoint http://127.0.0.1:<endpoint-c> \
  --voice-prompt debate_voice_1=<remote-prompt-1.wav> \
  --voice-prompt debate_voice_2=<remote-prompt-2.wav> \
  --voice-prompt debate_voice_3=<remote-prompt-3.wav> \
  --voice-prompt debate_voice_4=<remote-prompt-4.wav> \
  --voice-prompt debate_voice_5=<remote-prompt-5.wav> \
  --voice-prompt debate_voice_6=<remote-prompt-6.wav> \
  --voice-prompt debate_voice_7=<remote-prompt-7.wav> \
  --voice-prompt debate_voice_8=<remote-prompt-8.wav> \
  --concurrency 1 --concurrency 2 --concurrency 3 \
  --rounds 20 \
  --chunk-characters 12 \
  --delta-delay-seconds 0.05 \
  --output-dir <run>/native-session
```

该命令会得到 c1=20、c2=40、c3=60 个 session，正好覆盖 20 个并发 batch。必须另做逐请求 `<3000ms` post-check，不能只看现有 `transport_gate_passed`。

#### `scripts/benchmark_tts_quality_gate.py`

可复用：

- `moss-session` adapter；
- FunASR/OpenAI ASR；
- CER、deletion/swallow、insertion/extra、首尾吞字；
- chunk boundary silence、gap、stutter、underrun；
- c1/c2/c3 排队增量；
- 8 音色 manifest；
- 每音色20轮声学漂移代理；
- cancel probe、close ACK；
- JSON/Markdown 和密钥/endpoint 脱敏。

不能直接证明：

- 多文本 corpus；
- 重复整词/短语/循环；
- speaker identity；
- GPU；
- LiveKit/WebRTC；
- 完整 interrupt≤250ms。

正式运行必须使用完整合规的 8 音色 manifest，不能使用 `--allow-incomplete-voice-manifest` 生成发布结论。

#### `scripts/benchmark_streaming_tts_candidate.py`

只用于 Nano/vLLM/OpenAI-compatible PCM canary：首 PCM、RTF、chunk gap、c1/c3 和 WAV。它不证明原生 MOSS Realtime 增量 session，也不应作为正式主链路 GO 证据。

#### Browser probe

文件：

- `scripts/browser/realtime_audio_probe.js`
- `scripts/run_browser_realtime_audio_gate.mjs`

可复用：

- `audio.rtc.started` 中的 `agent_first_readable_delta_at` 与 `server_first_capture_at`；
- Chrome 解码后连续非静音样本；
- 用户手势解锁后的首声；
- interrupt→pause/srcObject清空/持续静音；
- 750ms旧音频复现观察窗。

当前代码已经把 `agent_first_readable_delta_at` 写入 `audio.rtc.started`，因此真实 canary 可以形成 Agent正文delta→浏览器首声时间。仍需新增批量多轮、c2/c3编排、WebRTC stats 和 Safari 探针加载方式。

#### LiveKit path 与单测

可复用：

- `StreamingPcm16Resampler` 的跨 chunk 连续性；
- 48kHz/20ms 固定 framing；
- 100ms source queue、120ms app queue的有限队列语义；
- 首个非静音 packet capture 时间；
- generation revoke、两次 AudioSource queue clear；
- 两房并发隔离；
- publisher prewarm。

单测是机制证据，不是 GPU/真实 SFU/浏览器性能证据。

## 5. 必须新增的工具/指标

### P0：没有这些就不能执行正式 gate

1. `benchmark_openmoss_gpu_acceptance.py`（新 orchestrator）
   - 生成统一 run ID；
   - 调用 native session benchmark、quality gate、GPU monitor、browser runner；
   - 支持 corpus JSON；
   - 固定 c1/c2/c3 20 batches；
   - 对每请求执行 max<3s、zero failure、zero silent-first-chunk；
   - 汇总为单一 `acceptance.json/md`，任何子门失败即非零退出。

2. GPU monitor
   - 1s 或更高频率记录 timestamp、GPU util、memory used/total、temperature、power、SM clock、process PID/memory；
   - 记录 Xid、OOM、服务 PID/重启次数；
   - 输出 CSV/JSON，与每个 session 的 monotonic timestamp 对齐。

3. Corpus runner 与重复检测
   - quality gate 支持 corpus manifest，而不是一次一条 `--text`；
   - 增加 insertion rate 自动 gate；
   - 增加重复字符、重复词、重复 n-gram、整句循环检测；
   - c2/c3 建立 expected→ASR transcript 混淆矩阵，发现串文本。

4. Endpoint resource introspection
   - active/pending session、worker/thread/process 数；
   - session close requested/acknowledged/worker exited 时间；
   - prompt token cache 命中；
   - readiness/warmup 状态；
   - GPU allocation 回 baseline 时间。

5. LiveKit active-generation 指标
   - TTS PCM 入队字节/帧；
   - active generation 中 publisher 因队列空而补静音的帧数和最长连续时长；
   - app/source queue depth P50/P95/max；
   - first PCM received、first capture、abort requested、queues cleared 时间；
   - revoked generation 晚到写入/包数量。

6. 真实房间多轮 browser runner
   - 通过 QA provisioning 创建隔离 canary，不触碰正式比赛；
   - Chrome/Safari c1/c2/c3；
   - 20轮首声、连续能量、interrupt；
   - Chrome 收集 WebAudio+WebRTC stats；Safari 使用测试构建/Web Inspector 注入同一探针；
   - 保存每轮 JSON、截图、浏览器版本和控制端时间线。

### P1：质量发布前必须补齐

7. Speaker embedding gate
   - 固定 evaluator revision；
   - prompt/真实同说话人校准集；
   - 20轮 generated→own centroid、nearest-other margin；
   - 长句首/中/末 embedding；
   - 输出 per-voice分布和跨音色混淆矩阵。

8. 人工盲听工具
   - 随机化、隐藏 voice ID/轮次/并发状态；
   - 每音色 MOS、身份选择、严重缺陷标签；
   - 至少3名听者；
   - 原始评分不可只保留均值。

9. Fault/concurrency driver
   - 10% early cancel、5% endpoint kill/timeout；
   - 排队取消与公平性；
   - 单 endpoint 故障不污染其他房间；
   - 服务恢复后 readiness/warmup 再接流量。

## 6. 推荐执行顺序

```text
G0 GPU/version/readiness
  -> G1 native session 20/20
  -> G2 PCM continuity at c1/c2/c3
  -> G3 corpus quality
  -> G4 8 voices × 20 drift
  -> G5 direct + integrated interrupt
  -> G6 3 matches / 30 min / fault injection
  -> G7 Chrome/Safari real WebRTC gate
  -> human MOS + identity
  -> release decision
```

前一层失败时停止扩大流量，但仍保存原始证据。禁止在失败后只重跑成功样本、删除冷样本或更换参数而不生成新的 run ID。

## 7. 证据目录约定

每次 GPU run 建议保存：

```text
docs/qa/audio/20260718-openmoss-rebuild/runs/<run-id>/
  run-manifest.json
  gpu/
    nvidia-smi.csv
    processes.jsonl
    service-events.jsonl
  native-session/
    moss-realtime-session-benchmark.json
    moss-realtime-session-benchmark.md
    samples/*.wav
  quality/
    tts-quality-gate.json
    tts-quality-gate.md
    corpus-results.jsonl
  speaker/
    embeddings.npz
    speaker-gate.json
  livekit/
    publisher-metrics.jsonl
    room-timeline.jsonl
  browser/
    chrome/<round>/*.json
    chrome/<round>/*.png
    safari/<round>/*.json
    safari/<round>/*.png
  manual/
    mos-raw.json
    identity-raw.json
  acceptance.json
  acceptance.md
```

endpoint 地址和凭据必须脱敏；模型/音色/corpus SHA 必须保留。

## 8. GO / NO-GO 规则

GO 需要同时满足：

- G0–G7 全部自动门通过；
- c1 20/20首 PCM<3s；
- c2 40/40、c3 60/60成功；
- CER、吞词、重复、串文本全部达标；
- 8音色×20轮自动漂移与speaker identity通过；
- 完整 interrupt 20/20≤250ms；
- Chrome、Safari 真实WebRTC门通过；
- 人工MOS和身份听辨通过；
- 30min三场并发无OOM、重启、欠载或资源泄漏。

以下任一情况直接 NO-GO：

- 任何有效首 PCM样本≥3s或无声音；
- 任何旧generation在中断静音后复现；
- 串文本、串音色、重复整句或末句吞失；
- c3出现排队饿死、OOM、服务重启；
- 仅有HTTP endpoint数据而没有真实LiveKit/浏览器证据；
- Safari仅验证按钮状态，未获得实际非静音采样时间；
- 自动漂移通过但speaker identity/人工门仍缺失。

## 9. 当前状态

| 项目 | 状态 |
|---|---|
| Native MOSS session benchmark | 已实现，待真实GPU endpoint |
| Streaming TTS quality gate | 已实现主要指标，待corpus/repeat/speaker扩展 |
| Agent正文delta时间 | 已进入 `audio.rtc.started` payload |
| Chrome browser probe | 已实现并通过synthetic门，待真实OpenMOSS canary批量运行 |
| Safari精确可听时间 | BLOCKED，需Web Inspector/测试构建探针或受控回环 |
| LiveKit机制 | 已有单测与synthetic浏览器证据，待活跃期欠载指标和真实c3 |
| 固定8音色正式manifest | BLOCKED，当前manifest为0/8 compliant |
| OpenMOSS GPU endpoint | BLOCKED，当前无本次验收数据 |
| Speaker identity / 人工MOS | BLOCKED |

最终判定：验收设计已经具备，现有脚本可承担大部分组件级证据；正式 OpenMOSS GPU 发布仍必须新增统一 orchestrator、GPU/LiveKit运行时指标、corpus重复检测、speaker identity 和真实多房浏览器编排。
