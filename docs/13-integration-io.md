# 13 · Integration IO

本章定义 ASR、TTS 和 Agent 的结构化调试配置、输入输出约定和密钥处理规则。管理端只保存非敏感模板和脱敏配置状态；真实密钥通过后端环境变量或一次性表单输入写入，不从接口回显明文。

## 1. 配置接口

### GET `/api/matches/{match_id}/integration-config`

返回 ASR、TTS 和音色预设。Agent 配置由 Agent 管理接口维护，不在这里混管。`secrets` 只返回 `configured` 和 `redacted` 状态。

```json
{
  "asr": {
    "enabled": true,
    "provider": "alicloud",
    "endpoint": "wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model=qwen3-asr-flash-realtime",
    "settings": {
      "model": "qwen3-asr-flash-realtime",
      "input_audio_format": "pcm",
      "sample_rate": 16000,
      "language": "zh"
    },
    "secrets": {
      "app_id": { "configured": false, "redacted": "" },
      "api_key": { "configured": false, "redacted": "" },
      "api_secret": { "configured": false, "redacted": "" },
      "alicloud": {
        "api_key": { "configured": true, "redacted": "********" },
        "workspace_id": { "configured": false, "redacted": "" }
      },
      "xfyun": {
        "app_id": { "configured": false, "redacted": "" },
        "api_key": { "configured": false, "redacted": "" },
        "api_secret": { "configured": false, "redacted": "" }
      }
    }
  },
  "tts": { "provider": "alicloud", "settings": { "model": "qwen3-tts-flash-realtime", "response_format": "mp3" } },
  "voice_presets": [
    { "id": "voice_alicloud_neil_debater", "provider": "alicloud", "voice": "Neil", "enabled": true, "is_default": true }
  ]
}
```

### PATCH `/api/matches/{match_id}/integration-config`

允许更新 `asr`、`tts` 和 `voice_presets`。ASR/TTS 支持 `enabled`、`provider`、`endpoint`、`lang`、`voice`、`settings` 和 `secrets`。空 secret 表示不更新；传入新值会覆盖后端保存值，但后续读取仍只返回脱敏状态。

## 2. Agent 提供方模式（provider_type）

每个 Agent 配置（`agent_configs[*]`）通过 `provider_type` 选择调用方式：

| provider_type | 调用方式 | 用途 |
| --- | --- | --- |
| `rest_api` | 后端 POST `{endpoint}/speech`，REST + SSE 流式返回 | 正式 Agent / 自建服务 / mock agent |
| `openai_sdk` | 后端用 OpenAI 兼容 SDK 调 `{base_url}/chat/completions`（流式） | qwen3.6-plus 等模型的测试接入 |

两种模式向上层产出**相同的事件流**（见 §3），比赛控制系统对模型实现保持解耦。

### 2.1 openai_sdk（测试接入 qwen3.6-plus）

- `base_url`：`https://dashscope.aliyuncs.com/compatible-mode/v1`
- `model_name`：`qwen3.6-plus`
- `api_key_env`：密钥只读环境变量名（如 `DASHSCOPE_API_KEY`），**不保存明文 Key**。
- 后端把下方结构化字段拼成 system/user 两条消息（`debate_history` 作为发言记录，要求模型直接以本方该辩位身份发言），用 `chat.completions.create(stream=True)` 流式取 `delta`，对外转成统一事件。
- 缺少 `api_key_env` 对应环境变量时返回 `openai_sdk_no_key`，进入人工干预降级，不静默改用占位文本。

## 3. Agent 结构化输入

`rest_api` 模式后端 POST `{endpoint}/speech`；`openai_sdk` 模式由后端转写为对话消息。两者输入同一份结构化 payload，核心字段与 `请求体(1).json` 样例对齐：

```json
{
  "model_name": "qwen3.6-27b",
  "debater_name": "乾元",
  "debate_position": "二辩",
  "debate_topic": "AI 的迅猛发展提升了/降低了人类创作者存在的意义",
  "current_stage": "自由辩论",
  "next_stage": "反方四辩总结",
  "holder": "正方",
  "debate_history": [
    {
      "stage": "正方一辩立论",
      "message": [
        { "speaker": "正方一辩", "content": "发言文本" }
      ]
    },
    {
      "stage": "自由辩论",
      "message": [
        { "speaker": "正方二辩", "content": "……" },
        { "speaker": "反方二辩", "content": "……" }
      ]
    }
  ],
  "max_token": 699,
  "output": { "stream": true, "language": "zh-CN" }
}
```

- `debate_history` 是**最重要**的字段：全局唯一、按阶段聚合的发言记录。`stage` 为环节名；`message` 是该环节内按时间顺序的发言数组；`speaker` 为「方+辩位」（如 `正方一辩`，不含姓名）；自由辩论会把多次往返聚合在同一个 `自由辩论` 阶段里。
- `max_token`：本次发言的 token 上限，由后端按发言限时与 TTS 语速**确定性推导**，约束 Agent 回复尽量不超时。公式：
  - `char_budget = time_limit_seconds × spoken_chars_per_sec(默认 4.5) × speech_rate(音色预设语速)`
  - `max_token = round(char_budget × tokens_per_char(默认 0.75) × margin(默认 1.15))`，再钳制到 `[64, 4096]`。
  - 可调环境变量：`PHDEBATE_TTS_SPEAKING_CPS`、`PHDEBATE_AGENT_TOKENS_PER_CHAR`、`PHDEBATE_AGENT_MAX_TOKEN_MARGIN`。
  - `openai_sdk` 模式会把 `max_token` 直接作为 `chat.completions.create(max_tokens=…)`；`rest_api` Agent 应自行据此限制输出。

路由/时控兼容字段（`rest_api` Agent 可选用，`openai_sdk` 忽略）：`match_id`、`task_id`、`speech_id`、`speaker_id`、`agent_config_id`、`agent_provider_type`、`time_limit_seconds`、`remaining_seconds`、`target_chars`，以及 `other_info`（含 `speech_rate`、`chars_per_second`、`char_budget` 等推导明细，便于对账）。

## 4. Agent 输出

SSE 每帧为 `data: <json>`：

```text
{ "type": "delta", "task_id": "task_001", "delta": "部分文本" }
{ "type": "final", "task_id": "task_001", "content": "完整文本", "usage": { "model": "model-name", "latency_ms": 1200 } }
{ "type": "error", "task_id": "task_001", "error": { "code": "model_timeout", "message": "generation timeout" } }
```

后端将 `delta` 实时广播到大屏字幕；`final` 写入正式 transcript，并进入 TTS 归档或文字降级。

## 5. ASR/TTS 输入输出

- ASR 输入优先为浏览器上传的 `audio/L16;rate=16000` PCM 分片，后端按当前 provider 转发到实时 ASR WebSocket。
- ASR 输出统一写入 `asr.partial` / `asr.final` 事件，并更新 transcript；当前大屏不展示实时转写。
- TTS 输入为 Agent final 文本或管理端试合成文本。正式发言优先使用 AI 辩手绑定的 `tts_voice_preset_id`，未绑定则回退当前 provider 的默认音色预设。
- TTS 输出为音频归档文件、`audio_assets(source = agent_tts)` 和 `tts.started` / `tts.audio_archived` / `tts.finished` 事件。

## 6. 密钥规则

- 不在前端代码、文档样例或 Git 仓库中写入真实 `APPID`、`APIKey`、`APISecret` 或模型 key。
- 本地联调可使用 `DASHSCOPE_API_KEY` / `DASHSCOPE_WORKSPACE_ID` 或 `XFYUN_APP_ID`、`XFYUN_API_KEY`、`XFYUN_API_SECRET`、`XFYUN_ASR_URL`、`XFYUN_TTS_URL` 等环境变量。
- 管理端结构化表单可用于现场一次性输入或确认配置状态，但接口读取只能看到脱敏状态。
