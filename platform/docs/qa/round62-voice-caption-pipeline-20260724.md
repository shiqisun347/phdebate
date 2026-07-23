# Round 62 ASR、字幕与实时语音链路专项复核

日期：2026-07-24  
范围：浏览器 ASR 采集、实时字幕、Agent→MOSS-Realtime、LiveKit→浏览器、暂停/重置清队列。  
边界：本轮只修改本地代码、测试和文档，**未连接、未部署、未重启生产环境**。

## 结论

| 验收项 | 当前结论 | 关键证据 |
| --- | --- | --- |
| ASR 不使用 MediaRecorder Blob | 通过 | 正式页面使用 20 ms AudioWorklet PCM；服务端与 FunASR 同时上行 PCM、下行 partial/final。生产源码中无 `MediaRecorder`，该名字只存在于测试替身。 |
| ASR 是连续双向流 | 通过 | 浏览器复用一套 AudioWorklet 图，WebSocket 断线时有界重连；服务端在上游握手后才发 `ready`，两个方向并发运行。 |
| 字幕只显示最新稳定一行 | 通过 | 舞台只有一个 `data-caption-line="single"` 投影；累计 ASR 被压缩为最新分句，最多 28 个字符；CSS 强制单行省略。 |
| Agent 文本持续送入同一 TTS context | 通过 | 首个会话在 Agent 流消费前准备；稳定短语按最长 200 ms 等待持续 `push_text`；MOSS 同一 WebSocket 发送连续 `text_delta` 并并发接收 PCM。 |
| 浏览器只听一条连续 WebRTC 音轨 | 通过（代码与测试） | 每房间仅发布 `agent-tts`；浏览器拒绝重复 publication/track，唯一可听路径经过一个 AudioWorklet gate。主持提示音是独立的预生成固定 cue，不是 AI 发言的第二播放器。 |
| 暂停/重置清完整队列 | 通过（代码与测试） | Agent interrupt、MOSS abort/audio queue clear、LiveKit generation revoke/SDK queue clear、浏览器 gate flush 形成完整链路，旧 generation 不能复活。 |
| 真双向流配置不可回退到完整 WAV | 已加固 | 生产配置校验现在强制 `MOSS_TTS_STABLE_PLAYBACK_ENABLED=false`、`MATCH_AUDIO_ARCHIVE_ENABLED=false`。 |
| Agent 首字到浏览器可听 ≤3 秒 | **本轮不能重新证明** | 本地环境没有可用 LiveKit/MOSS GPU 链路；Round 61 的历史生产数据仍有 3.646–8.471 秒离群值，部署和真实浏览器灰度前不能宣称 SLA 已完全达标。 |
| 无卡顿、撕裂、途中结束 | 代码风险显著降低，仍需生产听测 | 单音轨、清队列和 publisher fencing 已有专项覆盖；音色、撕裂和真实网络抖动不能由单元测试替代主观听测与端到端遥测。 |

## 本轮修复的确定问题

### 增量分块会吞掉英文词间空格

原有分句器为了寻找稳定边界会对每个分块执行 `strip/lstrip`，LightTTS 与 MOSS 会话又会再次 `strip`。当 Agent 文本含英文术语且分块落在空格附近时，实际送入 TTS 的内容可能从：

```text
large language model
```

变成：

```text
largelanguagemodel
```

持久化的最终文字稿仍然正确，因此普通“文字稿等于 Agent final”测试看不出问题，但合成语音会连读或发音异常。

修复后：

1. 流水线用忽略空白的紧凑游标选择安全边界；
2. 每个分块在发送 TTS 前重新映射回 Agent 原始文本的精确切片；
3. LightTTS/MOSS 会话仅拒绝纯空白帧，不再裁掉有效分块首尾空格；
4. 新增跨多分块英文术语回归测试，要求所有 TTS chunk 拼接后与 Agent final 完全一致。

涉及文件：

- `apps/api/app/services/voice_runtime/pipeline.py`
- `apps/api/app/services/providers.py`
- `apps/api/tests/test_realtime_voice.py`
- `apps/api/tests/test_voice_runtime.py`

## 架构证据

### ASR：AudioWorklet PCM 双向流

- `apps/web/components/debate-stage.tsx`
  - `ASR_CAPTURE_CHUNK_MS = 20`；
  - 创建 `jixia-asr-pcm-capture` AudioWorklet；
  - 采集结果重采样为 16 kHz mono PCM16，以二进制帧持续发送；
  - WebSocket `bufferedAmount` 超限时 fail closed，避免无限堆积导致延迟；
  - 停止发言时先 flush Worklet 尾帧，再只发送一次 `finish`。
- `apps/web/public/worklets/asr-pcm-capture.js`
  - 按 20 ms 有序产出 Float32 帧，并支持最终尾帧 flush。
- `apps/api/app/api/realtime.py`
  - `/ws/rooms/{code}/asr` 在 FunASR 上游握手成功后才通知浏览器 `ready`；
  - `browser_to_asr` 与 `asr_to_browser` 并发；
  - partial/final 严格绑定当前 `speech_id`、席位、控制租约和分布式 ASR lease；
  - 暂停后晚到的 final 不会写入或广播。

### 字幕：一个权威投影，不展示滚动文字稿

- `apps/web/components/stage-caption-projection.tsx`
  - 只接受当前 active speech 的字幕；
  - AI 字幕按真实 `playback_started_at` 与片段时间投影，不会在音频开始前闪出完整回答；
  - 页面只渲染一个字幕 `<p>`。
- `apps/web/lib/caption-line.ts`
  - 对累计 ASR 只保留最新可读分句；
  - 默认最多 28 个字符，超出时只保留最新尾部。
- `apps/web/app/globals.css`
  - `white-space: nowrap`、`overflow: hidden`、`text-overflow: ellipsis`。

### TTS 与浏览器：一个 context、一条音轨、一个可听播放器

- `apps/api/app/services/voice_runtime/pipeline.py`
  - Agent 正文 delta 最长等待 200 ms 即提交稳定短语；
  - 同一 `SpeechSynthesisSession` 从 prepare 到 finish 全程复用；
  - thinking/reasoning 不进入朗读链路；
  - interrupt 中止会话并丢弃尚未确认的尾部。
- `apps/api/app/services/providers.py`
  - MOSS 会话在消费 Agent 首个 delta 前完成持久 WebSocket 握手；
  - 连续 `text_delta` 使用序号和 ACK，同一 receiver 并发消费 PCM；
  - 不为每个短语重复 DNS/TCP/TLS/模型 warm-up。
- `apps/api/app/services/livekit_audio.py`
  - 每房间一个长寿命 `agent-tts` LocalAudioTrack；
  - 20 ms PCM 帧送入 LiveKit，浏览器由 Opus/WebRTC jitter buffer 接收；
  - generation、应用队列与 SDK source queue 在清理时串行化。
- `apps/web/lib/audio/livekit-room-audio.ts`
  - 只订阅 `agent-tts`，重复发布和重复轨道被拒绝；
  - 唯一远端音轨进入一个 AudioWorklet gate；
  - 隐藏 `<audio>` 被静音，只负责维持浏览器解码，不构成第二条可听路径。
- `services/moss-realtime-gateway`
  - WebSocket command receiver 与 audio sender 并发；
  - abort 清 runtime audio queue、后端 decoder queue 和 sender pending PCM，并发送 `audio_reset`。

## 自动化验证结果

### API 语音、ASR、LiveKit、字幕、遥测

```text
106 passed, 2 warnings
```

覆盖：增量 Agent/TTS、英文空格、MOSS 会话、LiveKit 单 publisher/清队列、ASR 双向桥、字幕、语音遥测与基准协议。

### Web 音频、字幕与断线恢复

```text
8 test files passed
113 tests passed
```

覆盖：

- 20 ms AudioWorklet ASR 与尾帧 flush；
- 不依赖 MediaRecorder 或音频上传；
- 同一 AudioWorklet 图跨 ASR 重连复用；
- 单行字幕与 active speech 隔离；
- LiveKit 只订阅一个 `agent-tts`；
- 重复轨道拒绝、generation flush、旧音频不可复活；
- 语音遥测增量口径。

### MOSS 网关

```text
60 passed, 1 warning
```

覆盖：同一会话 text-in/audio-out、单活跃 turn、PCM 流、abort、`audio_reset`、队列清理和低延迟 bridge。

### 生产配置防回退检查

```text
7 passed
```

重新启用完整 WAV 稳定播放或比赛音频归档时，校验器会拒绝通过。

### 静态检查

- Python Ruff：通过。
- 本轮相关 TypeScript ESLint：0 error，存在 7 个既有 warning（6 个 React Hook 依赖、1 个未使用参数），与本轮 Python 流式空格修复无关，未在该专项中扩大修改范围。

### 全量旁路回归

- Web 全量：53 个测试文件、385 项测试全部通过。
- API 全量首次运行：597 passed、1 xfailed、1 failed。唯一失败是既有的“开赛前真人连接状态”测试在全套高负载下被异步 presence 刷新为离线；它不经过本轮修改的语音文本路径。
- 对该失败用例立即隔离复跑：1 passed。此现象应记录为测试隔离/时序债务，不把它伪装成本轮语音修复已造成的回归，也不能凭一次隔离通过宣称该全量测试波动已修复。

## 本地浏览器检查

使用真实 Chromium 自动化打开本地首页和匿名观战页：

- [首页截图](round62-voice-caption-browser/screenshots/home.png)
- [观战页截图](round62-voice-caption-browser/screenshots/watch.png)

结果：页面无 JavaScript exception；仅有 React 开发环境 HMR/DevTools 日志。该本地房间处于准备状态且没有本地 LiveKit/MOSS 服务，因此声音按钮按设计保持不可用。本轮浏览器检查只能证明页面基本可达和舞台结构正常，**不能替代真实音频播放验收**。

## 部署后必须完成的 P0 验收

1. 在真实 MOSS GPU、LiveKit 和普通 Chrome 下连续完成一场 1v1 与一场 4v4；期间不得出现音轨中断、重复发言或旧尾音复活。
2. 对每次 AI 发言记录 `agent_first_readable_delta`、`tts_first_ingest`、`tts_first_pcm`、`livekit_first_capture`、`browser_first_audible`；要求端到端 P95 ≤3 秒，任何单次 >3 秒都必须保留分段耗时。
3. 播放中至少执行暂停、重置当前发言、终止比赛各 5 次；500 ms 内停止声音，恢复前不得听到旧 generation。
4. 用含英文专有名词、数字、缩写和中英文混排的辩题听测，确认词间空格不再被吞、音色不因 chunk 边界突变。
5. 以 5 个房间、总计最多 5 名观众并发运行，连续监测 LiveKit publication；每房间始终只能有一个 `agent-audio:<room>` publisher 和一个 `agent-tts` publication。
6. 对 Wi-Fi 抖动、浏览器后台/前台切换、移动端锁屏恢复进行听测；记录 `packetsLost`、`concealedSamples`、jitter buffer 平均延迟和 underrun，不用“文件回放正常”代替实时链路结论。

## 尚未关闭的风险

- 本轮没有新的生产端到端音频样本；Round 61 记录的 >3 秒离群值仍是开放问题。
- 音色“自信”、轻微杂音、撕裂和主观连贯性必须在真实扬声器/耳机上盲听，单元测试只能防止结构性断流和重复播放。
- 现有 ESLint warning 应在独立前端稳定性任务中逐个审计，不能机械补依赖导致音频 effect 重建。
- 生产部署前必须运行 `verify_moss_only_production.py`；本轮未读取或修改任何生产配置。
