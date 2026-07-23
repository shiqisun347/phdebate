# MOSS-Realtime 单一实时语音方案

更新时间：2026-07-18  
部署服务器：`117.50.192.216`

## 1. 当前方案

AI 辩手语音只走一条正式链路：

```text
Debate Agent SSE 正文 delta（thinking 不进入正文）
        ↓
稳定短语提交器
        ↓
同一轮唯一 MOSS-Realtime WebSocket 会话
        ↓
24 kHz / PCM16 连续音频
        ↓
LiveKit 重采样为 48 kHz / 20 ms 帧
        ↓
WebRTC Opus
        ↓
浏览器 AudioWorklet 连续播放
```

- 不等待完整 LLM 回复。
- 不按句重建 TTS 连接。
- 不拼接多个音频文件。
- 不传输分片 MP3。
- 每轮只绑定一个 `tts_session_id`、`voice_id` 和 `generation`。
- 真人语音识别继续使用 GPU FunASR。

## 2. 已完成的部署

- MOSS-Realtime 与 FunASR 均运行在新服务器 GPU。
- MOSS 实际显存约 12.3 GB，FunASR 约 2.4 GB，可在 RTX 3090 24 GB 上共存。
- MOSS Gateway、平台 API、比赛引擎、Worker、LiveKit、FunASR、PostgreSQL 和 Redis 均由 Supervisor 管理。
- 当前正式开关已启用：

```text
REALTIME_VOICE_PIPELINE_ENABLED=true
REALTIME_VOICE_BACKEND=moss_realtime
MOSS_TTS_REALTIME_ENABLED=true
MOSS_TTS_REALTIME_TRANSPORT=websocket
WEBRTC_AUDIO_ENABLED=true
WEBRTC_AUDIO_BACKEND=livekit
```

- MOSS Gateway 当前容量为 1：允许多个房间存在和观战，但同一时刻只允许一个房间占用本机 MOSS 推理。

### 为什么“1.7B MOSS”仍占用约 12.3 GB

`1.7B` 是项目对实时生成模型的规格标称，不是完整可运行语音栈的总内存。当前部署实际同时加载：

- MOSS-TTS-Realtime 权重：约 46.6 亿字节，权重文件包含约 23.32 亿个 BF16 张量元素。
- MOSS Audio Tokenizer/Codec：约 17.75 亿参数，当前为 FP32，权重约 71.0 亿字节。
- 运行时还需要 CUDA kernel、工作区、KV/cache、提示音 token、连续解码状态和音频队列。

因此不能用 `1.7B × 2 bytes` 估算完整服务显存。线上实测 MOSS 进程约 12.50 GB、FunASR 约 2.38 GB，总量在 RTX 3090 24 GB 内可稳定共存；此前认为 24 GB 无法运行完整 MOSS 的判断已经被实际部署推翻。

部署后必须执行 GPU 门禁：

```bash
./deploy/verify_gpu_voice_runtime.py
```

该脚本同时检查 Supervisor、真实进程参数、`nvidia-smi` 显存占用和 MOSS 全 CUDA placement。配置或实际进程任一回退 CPU 都会失败，并且不会输出 Gateway 密钥。

## 3. 真实浏览器验收结果

在公开观战页面、真实 Debate Agent、真实 MOSS、真实 LiveKit 和 Headless Chrome 上完成测量：

| 项目 | 实测 |
|---|---:|
| Agent 首个可读字符 → 浏览器非静音首声 | 1.983～2.688 秒 |
| 服务端首个 LiveKit capture → 浏览器首声 | 69～235 毫秒 |
| 活跃语音 Opus 码率 | 约 43～49 kbps |
| 空闲音轨（Opus DTX） | 约 0.9～1.35 kbps |
| 浏览器侧丢包 | 0 |
| 85.52 秒长发言 | 完整完成并自动推进下一阶段 |
| 中间静音 | 约 64～235 毫秒，符合标点自然停顿 |
| 控制事件 → AudioWorklet flush | 约 4 毫秒 |
| 控制事件 → 持续静音 | 不超过约 177 毫秒 |
| FunASR 真实 11.8 秒样本离线识别 | 约 474 毫秒服务延迟 |

已验证：

- 正方辩手使用 `debate_voice_1`，反方辩手使用 `debate_voice_5`。
- 换人时沿用同一条 LiveKit 音轨，但必须切换新的 generation 和固定席位音色。
- 暂停会同时中断 Agent、MOSS、服务端 PCM 队列和浏览器播放门。
- 恢复后生成新 generation，旧 generation 没有复活。
- 完整发言结束后生成 WAV、写入 `speech.completed`，状态机自动进入下一位辩手。

## 4. 音频压缩与带宽

需要压缩，但压缩只能位于网络和回放层，不能破坏生成链路的连续性。

### 实时网络

实时网络已经使用 WebRTC Opus：

- 目标上限 48 kbps，单声道语音。
- 开启 DTX，静音时降至约 1 kbps。
- 开启 RED，提高弱网恢复能力。
- 85 秒发言每位观众接收约 0.5 MB 音频数据，而不是约 4.1 MB 的原始 PCM。
- 500 位同时收听的观众，纯音频有效载荷约 24 Mbps；加上 RTP、DTLS、网络波动和信令，应按约 35～45 Mbps 出口预留。

浏览器不得接收原始 PCM 作为正式公网链路，也不得逐段下载并解码 MP3。

### 服务内部与归档

- MOSS → LiveKit 在服务器内部使用 PCM，避免重复有损编码和分片边界。
- 正常完成后保留 WAV，用于审计、ASR 回识、音质分析和可回溯证据。
- 历史页面后续应异步生成 48～64 kbps Opus 衍生文件供回放；WAV 继续作为受控原件，不直接作为大规模公开回放文件。
- 压缩任务不得阻塞首声、实时播放或比赛状态机。

## 5. 已修复的关键问题

1. 新服务器 LiveKit 仍广播老服务器 ICE 地址，导致信令成功但媒体失败；已改为新服务器地址。
2. Next standalone 发布遗漏 AudioWorklet 文件，导致浏览器静默回退；构建脚本已强制校验并复制 `public/`。
3. 浏览器只收到 Opus 包但远端音轨未进入稳定解码；已增加永久静音的 decoder sink，实际声音仍只经过 AudioWorklet。
4. LiveKit 起播缓冲跨阈值存在竞态；已等待真实首次 capture。
5. 最终 ACK 错误使用短增量超时，长发言会在正常生成时被中断；已改为整轮任务截止时间。
6. MOSS 网关把正常长发言和异常清理共用 60 秒；已增加独立 180 秒正常生成窗口。
7. 生成结束后 PCM 尾部排空只允许 3 秒；已改为 15 秒释放窗口。
8. TTS-only 重试曾回退到旧 LightTTS、发送整段文本并形成重试风暴；已统一使用同一 MOSS 增量会话。
9. Agent final 与已朗读文本只有空白差异时被误判改写；已允许纯排版差异，仍拒绝内容改写。
10. 暂停只停止上游但没有可靠清空浏览器尾音；已使用 generation 隔离与 AudioWorklet flush。

## 6. 控制协议

```text
start       创建本轮会话并绑定音色
text_delta  提交稳定正文短语，带连续 seq
final       文本输入结束，继续生成并排空尾部音频
audio       连续 PCM16
audio_end   本轮 PCM 已全部输出
abort       取消本轮并清空未播放音频
released    worker、codec、队列均已释放
```

`final` 的正常生成窗口和 `abort` 的异常清理窗口必须分开。长发言不能因为超过短控制 ACK 时间而被当作故障；真正的中断也不能等待完整发言自然结束。

## 7. 当前限制

- 一张 GPU 当前只支持一个活跃 MOSS 发言。多场比赛同时到达 AI 发言阶段时必须排队；需要真正并行时增加独立 GPU endpoint。
- MOSS 完整冷启动约需数分钟。比赛必须在 readiness 为 200 后才允许进入 AI 发言；端点隔离后应由运维守护自动重启并保持比赛安全暂停。
- 当前已完成单房间连续两位 AI、长发言、暂停和恢复验证，但仍需继续完成：
  - 8 个音色逐一 CER、吞字、重复和人工听感验收。
  - 20 轮连续发言和 30 分钟稳定性测试。
  - 多房间排队、公平性和取消排队测试。
  - 弱网、丢包、网络切换和移动端真机测试。
  - 历史回放 Opus 衍生文件与带宽压测。

## 8. 发布门槛

- Agent 首个可读字符到浏览器真实首声：每次不超过 3 秒。
- 正常发言期间无可感知卡顿、吞字、重复或音色漂移。
- 活跃网络音频保持 Opus，目标不超过 64 kbps；静音必须启用 DTX。
- 暂停、跳过或终止到浏览器持续静音不超过 250 毫秒。
- 中断后旧 generation 不得复活。
- 完成后必须存在 `speech.audio.ready`、`speech.completed` 和可追溯的 session、voice、generation 信息。
- MOSS `active=0`、`orphan_count=0` 后才能接收下一场。

任何一项失败，比赛应安全暂停并明确提示，不得静默切换到其他音色或旧 TTS 链路。
