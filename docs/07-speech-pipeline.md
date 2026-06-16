# 07 · Speech Pipeline

本章定义人类 ASR、AI TTS、音频归档和降级路径。MVP 选用科大讯飞流式听写和流式语音合成。

## 1. 总体链路

```text
人类辩手:
Console microphone -> backend audio stream -> Xunfei ASR
  -> asr.partial/asr.final -> Transcript -> Screen subtitle
  -> audio archive

AI 辩手:
Agent delta/final -> sentence splitter -> Xunfei TTS
  -> tts.started/finished -> audio archive
  -> Screen subtitle -> Speech record
```

## 2. 人类发言 ASR

### 2.1 开始条件

人类控制台点击“开始发言”后，后端必须先校验：

- 比赛为 `running`。
- 当前环节允许该辩手发言。
- 该辩手麦克风未被主持人锁定。
- 没有其他 active speech。

校验通过后：

- 创建 `Speech(source = human_asr)`。
- 启动对应时钟。
- 建立 ASR 会话。
- 广播 `speech.started`。

### 2.2 音频上传

MVP 辩手端优先使用 Web Audio 读取麦克风 PCM 采样，转为 16kHz Int16 PCM 后分片上传；若浏览器不支持该链路，再退回 MediaRecorder 分片留档：

- 首选编码：`audio/L16;rate=16000`，文件名 `chunk_00000.pcm`。
- 保底编码：`audio/webm;codecs=opus`，文件名 `chunk_00000.webm`。
- 分片：200-500ms。
- 每片带 `speech_id`、`speaker_id`、`chunk_index`。

后端必须保存原始音频或转码后的归档音频，路径写入 `audio_assets.file_path`。

注意：MediaRecorder 的 `webm/opus` 归档适合留档和导出，但不能直接作为讯飞流式听写的 PCM 输入。MVP 中若要触发归档补识别，上传端必须提供 PCM/L16、PCM、`audio/raw` 或后端先转码；否则接口应返回 `unsupported_audio_format`，比赛流程继续可用。

### 2.3 ASR 文本

- partial：可上屏，但只写 `transcript_segments`，不覆盖 `speeches.content_final`。
- final：写 `transcript_segments`，追加或合并到 `speeches.content_raw`。
- 主持人修正后，写 `speeches.content_final` 和 `speech_revisions`。

### 2.4 归档补识别

管理端“识别归档”调用 `POST /api/matches/{match_id}/speeches/{speech_id}/asr/recognize`，适用于现场网络或 ASR 临时不可用时的补偿链路。

处理规则：

- 按 `audio_assets.chunks[].chunk_index` 拼接该发言音频。
- 识别期间设置 `speech_service.asr.status = recognizing`，并广播 `asr.archive_recognition_started`。
- 成功后写回 active speech 或最新 transcript segment，广播 `asr.final`。
- 若覆盖已有转写文本，写入 `speech_revisions`，`reason = archive_asr_recognition`。
- 若归档为空，设置 ASR 为 `failed` 并返回 `invalid_audio_archive`。
- 若归档是 `webm/opus` 等非 PCM 输入，返回 `unsupported_audio_format`，不改变比赛状态机。

当 `audio/complete` 请求显式传入 `auto_recognize = true` 时，后端同步执行上述补识别并返回最新 snapshot。若未显式传入，后端会在讯飞 ASR 配置完整时后台自动补识别；也可用环境变量 `PHDEBATE_ASR_AUTO_RECOGNIZE=1/0` 强制开启或关闭。后台自动识别失败只更新 ASR 降级状态和事件，不阻塞辩手端结束发言。

### 2.5 实时流式 ASR

当 `PHDEBATE_ASR_REALTIME=1`，或讯飞 ASR 必要环境变量均已配置时，后端在收到第一段 PCM/L16 分片后创建讯飞 ASR 长连接：

- 第一段 PCM 作为 `status = 0` 发送，后续分片作为 `status = 1` 发送。
- `audio/complete` 时发送结束帧 `status = 2` 并等待最终识别结果。
- 识别过程中的文本增量写入 `asr.partial`，最终文本写入 `asr.final`。
- 如果长连接失败或结束帧后超过 `XFYUN_ASR_FINAL_TIMEOUT_S` 未返回 final，系统写入 `asr.failed`，保留音频归档，并可继续走归档补识别或人工修订。
- 若 `PHDEBATE_ASR_REALTIME=0`，PCM 分片只做归档和补识别，不启动长连接。

## 3. AI TTS

### 3.1 句子切分

Agent 流式文本按中文标点切句：

```text
。！？；\n
```

短句可合并，避免 TTS 过碎；超过 40-60 字应尽快送 TTS，降低首声延迟。

### 3.2 播放与计时

- TTS 合成首段音频成功并开始播放时，广播 `tts.started`。
- `tts.started` 是 AI 发言计时起点。
- TTS 播完且无剩余文本时，广播 `tts.finished` 并结束发言。
- 若时钟到时，根据超长策略执行。

MVP 中，`PHDEBATE_TTS_FORMAL=1` 或讯飞 TTS 配置完整时，后端会在 AI final 文本产生后调用讯飞 TTS 合成正式发言音频，保存到 `PHDEBATE_AUDIO_DIR/{match_id}/{phase_key}/{speech_id}/tts_{task_id}.*`，并登记为 `audio_assets(source = agent_tts, status = completed)`。成功后广播 `tts.audio_archived`，`tts.finished` 会带 `audio_asset_id`、`latency_ms` 和 `status = completed`。若未开启正式 TTS，则保留文字展示和模拟 TTS 状态，比赛流程不受影响。

当前 MVP 已完成正式 AI 发言音频归档；真实扩声播放队列仍由后端所在机器或主持人控制台在现场集成时承接。若浏览器端播放，需要确保自动播放权限在现场彩排中验证。

管理端“语音链路 / ASR 自检”会调用真实讯飞 ASR WebSocket Gateway，默认发送一段短 PCM 静音验证签名、连接和服务响应；也可通过 API 传入 `audio_base64` 做真实音频试识别。该能力用于赛前验证账号、网络和 ASR 服务可达性，不替代辩手端实时转写流。

管理端“语音链路 / TTS 试合成”会调用真实讯飞 TTS WebSocket Gateway，合成一段固定自检文本并保存到 `PHDEBATE_AUDIO_DIR/diagnostics/`。该能力用于赛前验证账号、网络、签名、合成返回和本地音频落盘，不替代正式 AI 发言播放队列。

## 4. 讯飞配置

配置只存后端环境变量或加密配置：

```text
XFYUN_APP_ID=
XFYUN_API_KEY=
XFYUN_API_SECRET=
XFYUN_ASR_URL=
XFYUN_TTS_URL=
XFYUN_ASR_OPEN_TIMEOUT_S=8
XFYUN_ASR_CLOSE_TIMEOUT_S=3
XFYUN_ASR_FINAL_TIMEOUT_S=12
PHDEBATE_ASR_REALTIME=
PHDEBATE_ASR_AUTO_RECOGNIZE=
PHDEBATE_TTS_FORMAL=
```

可配置项：

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `asr_language` | `zh_cn` | 中文普通话 |
| `asr_domain` | `iat` | 流式听写 |
| `tts_voice` | 现场确认 | 可按 AI 辩手配置不同音色 |
| `tts_speed` | 50 | 影响 `target_chars` 估算 |
| `tts_volume` | 70 | 现场扩声调试 |
| `tts_pitch` | 50 | 默认中性 |

具体参数以实际采购的讯飞产品线为准。

管理端可调用 `GET /api/matches/{match_id}/speech/diagnostics` 检查当前进程是否已配置讯飞 ASR/TTS 环境变量、音频归档目录是否可写，以及当前是否只能使用 mock/人工降级链路。诊断结果不返回密钥明文。

管理端结构化调试表单调用 `GET/PATCH /api/matches/{match_id}/integration-config` 管理 ASR/TTS/Agent 的非敏感接口模板。密钥字段只允许写入或清空，不从后端回显明文；完整输入输出约定见 [13-integration-io.md](13-integration-io.md)。

后端讯飞适配层负责：

- 使用 `XFYUN_API_KEY` 与 `XFYUN_API_SECRET` 生成 WebAPI WebSocket 握手 URL，签名算法为 `hmac-sha256`。
- 构造 ASR 首帧/中间帧/结束帧 payload，音频默认 `audio/L16;rate=16000`、`raw` 编码。
- 构造 TTS 文本 payload，文本使用 UTF-8 后 Base64 编码。
- 解析 ASR `ws/cw/w` 文本片段和 TTS Base64 音频片段。
- ASR WebSocket Gateway 负责发送 PCM 分片、聚合识别文本、计算延迟和分片数，并在实时流、自检和归档补识别中更新 ASR 状态。
- TTS WebSocket Gateway 负责发送单段文本、聚合返回音频、计算延迟和分片数，并在试合成与正式 AI 发言中落盘归档。
- 不在前端或诊断接口中返回 `API_SECRET`、完整 `authorization` 或音频服务密钥。

## 5. 降级策略

| 环节 | 失败 | 降级 |
| --- | --- | --- |
| 人类 ASR | 讯飞不可用 | 只计时和录音，大屏显示“转写不可用” |
| 人类录音 | 浏览器无麦克风权限 | 主持人可代开始/结束计时，记录设备异常 |
| AI TTS | 合成失败 | 大屏展示文字，主持人选择继续或人工朗读 |
| TTS 播放 | 现场扬声器异常 | 停止 TTS，改为主持人朗读或只展示文字 |
| 音频归档 | 磁盘写入失败 | 不阻塞比赛，但管理端红色告警 |

降级不应让状态机卡死。所有降级都要写事件和审计。

## 6. 音频归档

目录默认随 SQLite 存储目录创建，也可通过 `PHDEBATE_AUDIO_DIR` 指定绝对路径或相对项目根目录的路径。目录结构建议：

```text
storage/audio/{match_id}/{phase_key}/{speech_id}/chunk_00000.pcm
```

命名示例：

```text
storage/audio/match_001/free_debate/speech_014/chunk_00000.pcm
storage/audio/match_001/neg_constructive_1/speech_002.mp3
```

归档字段：

- `audio_assets.mime_type`
- `audio_assets.duration_ms`
- `audio_assets.size_bytes`
- `audio_assets.file_path`
- `audio_assets.chunks[].file_path`
- `audio_assets.chunks[].chunk_index`

MVP 不保存视频。

## 7. 管理端可观测性

管理端“语音链路”必须展示：

- ASR 服务状态、最近延迟、当前会话数。
- TTS 服务状态、播放队列、当前播报 speaker。
- 大屏连接状态。
- 辩手端在线数和麦克风权限异常。

事件日志至少记录：

- `asr.partial` 可不全部显示，但应有 final。
- `asr.final`
- `asr.failed`
- `asr.archive_recognition_started`
- `tts.started`
- `tts.synthesis_started`
- `tts.audio_archived`
- `tts.finished`
- `tts.failed`

## 8. 现场彩排检查

正式比赛前必须完成：

- 每台辩手电脑浏览器麦克风授权。
- 每台辩手电脑与后端时钟同步误差可接受。
- 讯飞云 API 在现场网络可达。
- 扩声设备可以播放 TTS，音量不啸叫。
- 断网或讯飞失败时，主持人知道如何切换到降级流程。
