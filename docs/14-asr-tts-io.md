# 14 · ASR / TTS 交互协议（Provider 架构）

本章定义后端与语音服务的输入输出约定。语音服务不再写死讯飞：运行时配置可选择 `alicloud` 或 `xfyun`，默认使用阿里云百炼 Qwen-ASR / Qwen-TTS。真实密钥只写入 gitignored 的运行时配置或环境变量，不写入仓库、文档和前端响应。

固定流程：

1. 人类辩手发言：浏览器上传 `16k / 16bit / mono` PCM 分片，后端按当前 ASR provider 建立实时识别流。大屏当前不显示实时转写，只保留最终发言内容。
2. AI 辩手发言：Agent 文本进入正式 transcript；若正式 TTS 开启且配置可用，后端按 AI 辩手绑定的音色预设合成音频、归档并发送给大屏播放。
3. 发言结束提示：大屏只显示 `发言完毕`，不再拼接“辩位 · 名称 发言完毕”。

## 1. 统一 Provider 层

后端入口：

- `app/services/speech_gateway.py`
- `select_asr_gateway()`
- `select_tts_gateway(voice_preset_id="", speaker=None)`

调用方包括：

- `/ws/asr-test/{match_id}`
- `/ws/tts-test/{match_id}`
- `MatchStore.probe_asr`
- `MatchStore.probe_tts`
- 人类发言实时 ASR / 归档补识别
- AI 正式 TTS 合成 / 句子级 TTS

错误统一为可读 provider 错误。TTS 不会因为某个音色失败而自动切换到另一个发音人；若服务商返回 license、额度、模型或音色错误，应直接暴露错误，便于定位真实原因。

## 2. 阿里云 Qwen 默认配置

### ASR

- Provider：`alicloud`
- Model：`qwen3-asr-flash-realtime`
- Endpoint：`wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model=qwen3-asr-flash-realtime`
- 鉴权：`Authorization: Bearer <DASHSCOPE_API_KEY>`
- 音频：`pcm`，`16000Hz`，mono，16-bit
- VAD：`server_vad`，`threshold=0.0`，`silence_duration_ms=400`

输入帧：

```json
{ "type": "input_audio_buffer.append", "audio": "<base64-pcm>" }
```

识别结果解析：

- `conversation.item.input_audio_transcription.text`：partial
- `conversation.item.input_audio_transcription.completed`：final
- `session.finished`：会话结束

### TTS

- Provider：`alicloud`
- Model：`qwen3-tts-flash-realtime`
- Endpoint：`wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model=qwen3-tts-flash-realtime`
- 鉴权：`Authorization: Bearer <DASHSCOPE_API_KEY>`
- Mode：`server_commit`
- 默认输出：`mp3`，`24000Hz`，mono

说明：阿里云文档支持 PCM 输出；当前项目默认用 MP3，是为了浏览器流式播放和大屏回放更稳定。若改为 PCM，需要同步调整前端播放器。

音频结果解析：

- `response.audio.delta`：base64 音频分片
- `response.done` / `session.finished`：合成结束

## 3. 音色预设

`IntegrationConfig.voice_presets` 维护可用音色。辩手管理页只能选择已启用、且匹配当前 TTS provider 的预设，不能创建音色。音色预设只管理表达层参数，协议层参数从当前 TTS 通用配置继承。

音色预设字段：

- `voice`：服务商音色，下拉选择，阿里云默认提供 `Neil`、`Cherry`、`Ethan`、`Serena`。
- `speech_rate`：语速，默认 `1.0`。
- `volume`：音量，默认 `70`。
- `pitch_rate`：音调，默认 `1.0`。
- `instructions`：表达风格指令；仅在切换到 `qwen3-tts-instruct-flash-realtime` 时发送，普通 `qwen3-tts-flash-realtime` 不发送。
- `model`、`response_format`、`sample_rate`、`mode`、`language_type`：由当前 TTS 通用配置自动填充，不在音色编辑器中手动维护。

进入阿里云 TTS 前，后端会统一清洗 Agent 文本：去除 Markdown、链接、代码符号、装饰分隔线和明显不适合朗读的符号，并把 `AI`、`ASR`、`TTS`、`Qwen` 等常见英文术语转成更适合中文播报的表达，降低异常发音。

初始推荐：

| 用途 | voice | 名称 | 说明 |
| --- | --- | --- | --- |
| 主持 / 系统播报 | `Cherry` | 芊悦 | 亲切自然，适合清晰提示 |
| AI 辩手默认男声 | `Neil` | 阿闻 | 平直清晰、字正腔圆，适合正式辩论和立论陈词 |
| AI 辩手备用男声 | `Ethan` | 晨煦 | 标准普通话，带部分北方口音；作为备用男声保留 |
| AI 辩手默认女声 | `Serena` | 苏瑶 | 温和清晰，适合总结陈词或稳健表达 |

不默认使用 `Chelsie`，因为其描述偏二次元角色，不适合正式辩论场景；不再默认使用 `Ethan`，因为其音色描述包含部分北方口音，英文术语和严肃辩论文本里更容易出现听感波动。

## 4. 讯飞兼容

讯飞仍保留为可选 provider：

- `xfyun` ASR：`app/services/xfyun_rtasr.py` 与老版 IAT gateway。
- `xfyun` TTS：`app/services/xfyun_gateway.py`，支持超拟人 schema。
- `LiccCheck failed ... licc limit`：这是讯飞侧 license / 额度 / 授权问题。代码不会自动切发音人，应核对 AppID/APIKey 服务授权、当前 `vcn` 是否开通、字符/并发额度是否已用尽。

## 5. 配置入口

管理端：

- `GET /api/matches/{match_id}/integration-config`
- `PATCH /api/matches/{match_id}/integration-config`
- `GET /api/matches/{match_id}/speech/diagnostics`
- `/admin` → 语音引擎页

环境变量：

```text
DASHSCOPE_API_KEY=
DASHSCOPE_WORKSPACE_ID=
XFYUN_APP_ID=
XFYUN_API_KEY=
XFYUN_API_SECRET=
XFYUN_ASR_URL=
XFYUN_TTS_URL=
PHDEBATE_ASR_REALTIME=
PHDEBATE_ASR_AUTO_RECOGNIZE=
PHDEBATE_TTS_FORMAL=
```

生产建议通过语音引擎页写入运行时配置，或通过 systemd / 环境变量注入。前端只显示 `已配置`，不会回显密钥明文。
