# 13 · Integration IO

本章定义 ASR、TTS 和 Agent 的结构化调试配置、输入输出约定和密钥处理规则。管理端只保存非敏感模板和脱敏配置状态；真实密钥通过后端环境变量或一次性表单输入写入，不从接口回显明文。

## 1. 配置接口

### GET `/api/matches/{match_id}/integration-config`

返回 ASR、TTS、Agent 三类接入配置。`secrets` 只返回 `configured` 和 `redacted` 状态。

```json
{
  "asr": {
    "enabled": true,
    "provider": "xfyun",
    "endpoint": "wss://office-api-ast-dx.iflyaisol.com/",
    "method": "WEBSOCKET",
    "headers_template": {},
    "payload_template": { "audio_format": "audio/L16;rate=16000", "encoding": "raw" },
    "timeout_seconds": 12,
    "secrets": {
      "app_id": { "configured": true, "redacted": "********" },
      "api_key": { "configured": true, "redacted": "********" },
      "api_secret": { "configured": true, "redacted": "********" }
    }
  }
}
```

### PATCH `/api/matches/{match_id}/integration-config`

允许更新 `enabled`、`provider`、`endpoint`、`method`、`headers_template`、`payload_template`、`timeout_seconds` 和 `secrets`。空 secret 表示不更新；传入新值会覆盖后端保存值，但后续读取仍只返回脱敏状态。

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

`rest_api` 模式后端 POST `{endpoint}/speech`；`openai_sdk` 模式由后端转写为对话消息。两者输入同一份结构化 payload，核心字段如下（与 `需求 2.md` 约定一致）：

```json
{
  "model_name": "qwen3.6-plus",
  "debater_name": "乾元",
  "debate_position": "二辩",
  "debate_topic": "AI 时代，我们更应该培养编程思维 / 提问思维",
  "current_stage": "正方一辩立论",
  "next_stage": "反方一辩立论",
  "holder": "正方",
  "debate_history": [
    {
      "stage": "正方一辩立论",
      "message": [
        { "speaker": "正方一辩 · 林晚晴", "content": "发言文本" }
      ]
    }
  ],
  "output": { "stream": true, "language": "zh-CN" }
}
```

`debate_history` 是**最重要**的字段：它是全局唯一辩论过程按阶段聚合的结构化文本，供 Agent 据此生成本阶段发言。

路由/时控兼容字段（`rest_api` Agent 可选用，`openai_sdk` 忽略）：`match_id`、`task_id`、`speech_id`、`speaker_id`、`agent_config_id`、`agent_provider_type`、`time_limit_seconds`、`remaining_seconds`、`target_chars`。

## 4. Agent 输出

SSE 每帧为 `data: <json>`：

```text
{ "type": "delta", "task_id": "task_001", "delta": "部分文本" }
{ "type": "final", "task_id": "task_001", "content": "完整文本", "usage": { "model": "model-name", "latency_ms": 1200 } }
{ "type": "error", "task_id": "task_001", "error": { "code": "model_timeout", "message": "generation timeout" } }
```

后端将 `delta` 实时广播到大屏字幕；`final` 写入正式 transcript，并进入 TTS 归档或文字降级。

## 5. ASR/TTS 输入输出

- ASR 输入优先为浏览器上传的 `audio/L16;rate=16000` PCM 分片，后端可实时转发到讯飞 ASR WebSocket。
- ASR 输出统一写入 `asr.partial` / `asr.final` 事件，并更新大屏字幕和 transcript。
- TTS 输入为 Agent final 文本或管理端试合成文本。
- TTS 输出为音频归档文件、`audio_assets(source = agent_tts)` 和 `tts.started` / `tts.audio_archived` / `tts.finished` 事件。

## 6. 密钥规则

- 不在前端代码、文档样例或 Git 仓库中写入真实 `APPID`、`APIKey`、`APISecret` 或模型 key。
- 本地联调优先使用 `XFYUN_APP_ID`、`XFYUN_API_KEY`、`XFYUN_API_SECRET`、`XFYUN_ASR_URL`、`XFYUN_TTS_URL`、`PHDEBATE_AGENT_BASE_URL` 等环境变量。
- 管理端结构化表单可用于现场一次性输入或确认配置状态，但接口读取只能看到脱敏状态。
