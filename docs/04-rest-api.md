# 04 · REST API

本章定义 MVP REST 接口。管理端控制优先走 REST，WebSocket 只负责实时同步和少量辩手状态上报。

## 1. 通用约定

Base URL：`/api`

所有管理端请求必须带：

```http
Authorization: Bearer <admin_or_host_token>
Idempotency-Key: <uuid>  # 对控制命令推荐
```

生产鉴权启用条件：

- `PHDEBATE_ENV=production`，或
- `PHDEBATE_AUTH_REQUIRED=1`。

开发态未启用鉴权时，上述 header 可以省略。学生投票公开接口不要求后台访问 token。

成功响应：

```json
{
  "ok": true,
  "data": {},
  "server_time_ms": 1760000000000
}
```

错误响应：

```json
{
  "ok": false,
  "error": {
    "code": "invalid_state",
    "message": "Current phase is not active.",
    "details": {}
  },
  "server_time_ms": 1760000000000
}
```

常用错误码：

| code | HTTP | 说明 |
| --- | --- | --- |
| `unauthorized` | 401 | 未登录或 token 无效 |
| `forbidden` | 403 | 权限不足 |
| `not_found` | 404 | 资源不存在 |
| `invalid_state` | 409 | 状态机不允许该操作 |
| `unsupported_audio_format` | 409 | 当前音频格式不能直接送 ASR |
| `invalid_audio_archive` | 409 | 音频归档为空或不可读 |
| `validation_error` | 422 | 参数不合法 |
| `agent_unavailable` | 503 | Agent 不可用 |
| `speech_service_error` | 503 | ASR/TTS 服务不可用 |

## 2. Match API

### POST `/api/matches`

创建比赛并生成默认 10 个环节。

请求：

```json
{
  "title": "中科院计算所第一届人机辩论赛",
  "topic": "AI 时代，我们更应该培养编程思维 / 提问思维",
  "affirmative_position": "更应该培养编程思维",
  "negative_position": "更应该培养提问思维",
  "organizer": "中国科学院计算技术研究所",
  "venue": "现场会场"
}
```

响应：

```json
{
  "ok": true,
  "data": {
    "match_id": "match_001",
    "status": "draft"
  },
  "server_time_ms": 1760000000000
}
```

### GET `/api/matches/{match_id}`

返回完整比赛配置和当前状态快照。前端初始化必须先调用该接口。

生产态要求 `admin`、`host`、`screen` 或匹配的 `speaker` token。

### GET `/api/public/matches/{match_id}/vote-options`

返回学生投票页需要的公开候选项，不包含完整比赛控制状态、审计日志、Agent URL 或内部事件。

响应：

```json
{
  "ok": true,
  "data": {
    "match": {
      "id": "match_001",
      "title": "中科院计算所第一届人机辩论赛",
      "topic": "AI 时代，我们更应该培养编程思维 / 提问思维"
    },
    "teams": [
      { "id": "team_aff", "side": "affirmative", "name": "智码战队", "position": "编程思维" }
    ],
    "speakers": [
      { "id": "spk_aff_3", "side": "affirmative", "seat": 3, "name": "林晚晴", "speaker_type": "human" }
    ],
    "vote_state": {
      "window_status": "open",
      "audience_count": 137,
      "judge_published": false,
      "audience_published": false
    }
  }
}
```

### PATCH `/api/matches/{match_id}`

MVP 更新比赛基础展示字段。比赛进入 `running` 后只允许修改不影响状态机的字段。

请求：

```json
{
  "title": "中科院计算所第一届人机辩论赛",
  "topic": "AI 时代，我们更应该培养编程思维 / 提问思维",
  "affirmative_position": "更应该培养编程思维",
  "negative_position": "更应该培养提问思维",
  "organizer": "中国科学院计算技术研究所",
  "venue": "现场会场"
}
```

队伍、辩手和赛制时长在 MVP 管理端可编辑；会破坏状态机的字段（持方、辩位、环节顺序、环节类型）不允许运行中修改。

### GET `/api/matches/{match_id}/audit-logs?limit=30`

返回最近审计日志，用于管理端事件日志面板和赛后复盘。`limit` 最大 200。

响应：

```json
{
  "ok": true,
  "data": {
    "items": [
      {
        "id": "audit_1844",
        "match_id": "match_001",
        "actor_type": "host",
        "actor_id": null,
        "action": "screen.scene_changed",
        "target_type": null,
        "target_id": null,
        "request": { "scene": "teams" },
        "result": "success",
        "error_message": null,
        "created_at": "2026-06-10T12:00:00Z"
      }
    ]
  }
}
```

### GET `/api/matches/{match_id}/preflight-report`

生成赛前体检报告。该接口需要 `host` 或 `admin` token，会合并当前比赛 snapshot、语音诊断、Agent 状态、投票状态、导出准备和生产鉴权配置，输出可在管理端直接展示的检查项。

响应：

```json
{
  "ok": true,
  "data": {
    "checked_at": "2026-06-10T20:55:00Z",
    "overall_status": "warn",
    "summary": "赛前体检有 4 项提醒，建议彩排确认。",
    "score": {
      "ok": 10,
      "warn": 4,
      "fail": 0,
      "total": 14
    },
    "sections": [
      {
        "id": "speech",
        "label": "语音链路",
        "status": "warn",
        "checks": [
          {
            "id": "speech_diagnostics",
            "label": "讯飞配置",
            "status": "warn",
            "detail": "mock_fallback · realtime off · auto manual",
            "action": "正式上场前补齐讯飞 ASR/TTS 环境变量，或确认降级方案可接受。"
          }
        ]
      }
    ],
    "next_actions": ["正式上场前补齐讯飞 ASR/TTS 环境变量，或确认降级方案可接受。"]
  }
}
```

### POST `/api/matches/{match_id}/exports`

生成赛后复盘 zip 导出包。MVP 导出包包含 `match.json`、`transcript.json`、`transcript.csv`、`events.jsonl`、`votes.json`、`audit_logs.jsonl`、`audio_manifest.json`，并尽量带上已经归档的音频分片。

响应：

```json
{
  "ok": true,
  "data": {
    "export_id": "match_001_20260610T120000Z_1844",
    "match_id": "match_001",
    "file_path": "apps/backend/storage/exports/match_001/match_001_20260610T120000Z_1844.zip",
    "download_url": "/api/matches/match_001/exports/match_001_20260610T120000Z_1844/download",
    "size_bytes": 10240,
    "entries": [
      { "path": "match.json", "size_bytes": 4096 }
    ],
    "created_at": "2026-06-10T12:00:00Z"
  }
}
```

### GET `/api/matches/{match_id}/exports/{export_id}/download`

下载指定导出包，响应类型为 `application/zip`。

### PATCH `/api/matches/{match_id}/teams/{team_id}`

更新队伍展示字段，不允许修改持方。

请求：

```json
{
  "name": "智码战队",
  "position": "编程思维",
  "description": "主张 AI 时代更应该培养编程思维"
}
```

### PATCH `/api/matches/{match_id}/speakers/{speaker_id}`

更新辩手展示和 Agent 接入字段，不允许修改持方、辩位、人类/AI 类型。人类辩手运行中只允许修改 `name`；AI 辩手可修改模型信息和 Agent URL。

请求：

```json
{
  "name": "玄思",
  "model_name": "Qwen-Max",
  "model_kind": "closed_source",
  "agent_endpoint": "http://127.0.0.1:8100"
}
```

### PATCH `/api/matches/{match_id}/phases/{phase_id}`

更新环节展示名称和时长配置，不允许修改环节顺序、持方、辩位或环节类型。若更新当前环节，服务端同步对应 clock 的 `total_seconds`，并把 `remaining_ms` 限制在新上限内。

普通环节请求：

```json
{
  "name": "正方一辩立论",
  "duration_seconds": 180
}
```

自由辩论请求：

```json
{
  "name": "自由辩论",
  "side_total_seconds": 240,
  "turn_seconds": 15
}
```

### POST `/api/matches/{match_id}/start`

将 `ready` 比赛启动为 `running`，进入第一个环节。

### POST `/api/matches/{match_id}/reset`

重置当前比赛流程到候场/第一环节，清空当前 transcript、音频归档和投票状态，但保留事件与审计历史。

### POST `/api/matches/{match_id}/pause`

暂停比赛及所有 running clocks。

### POST `/api/matches/{match_id}/resume`

恢复比赛和暂停前的 clocks。

### POST `/api/matches/{match_id}/finish`

结束比赛，停止计时、关闭学生投票窗口、中断未完成 Agent/TTS。

### GET/PATCH `/api/matches/{match_id}/integration-config`

读取或更新 ASR/TTS/Agent 结构化接入配置。密钥字段只回显脱敏状态，详见 [13-integration-io.md](13-integration-io.md)。

### POST `/api/matches/{match_id}/stage/commentary`

切到评委点评/合议大屏，并开启学生投票窗口。

### POST `/api/matches/{match_id}/stage/judge-result`

公布评委结果、关闭学生投票窗口，并切到结果大屏。

### POST `/api/matches/{match_id}/stage/audience-result`

公布学生投票结果，切到最终结果大屏，并将比赛标记为 `finished`。

## 3. Phase API

### POST `/api/matches/{match_id}/phases/{phase_id}/start`

开始指定环节。通常只能开始当前待进行环节；回退后的 `reopened` 环节也可开始。

请求：

```json
{
  "host_confirmed": true
}
```

### POST `/api/matches/{match_id}/phases/{phase_id}/skip`

跳过环节并停止该环节相关任务。

请求：

```json
{
  "reason": "现场流程调整"
}
```

### POST `/api/matches/{match_id}/phases/{phase_id}/next`

完成当前环节并进入下一环节。与 `skip` 不同，该接口把当前环节标记为 `completed`。

### POST `/api/matches/{match_id}/current-speech/reset`

重置当前发言，清空 active speech 并暂停相关时钟；已有 transcript 记录不删除。

### POST `/api/matches/{match_id}/phases/{phase_id}/rollback`

回退到指定环节。

请求：

```json
{
  "reason": "误操作，需要重新进行该环节"
}
```

响应 payload 必须包含被作废的 `speech_ids`。

## 4. Clock API

### POST `/api/matches/{match_id}/clocks/{clock_name}/pause`

暂停指定时钟。`clock_name` 可为 `main`、`affirmative_total`、`negative_total`、`turn`。

### POST `/api/matches/{match_id}/clocks/{clock_name}/resume`

继续指定时钟。

### POST `/api/matches/{match_id}/clocks/{clock_name}/adjust`

校准指定时钟。

请求：

```json
{
  "remaining_ms": 90000,
  "reason": "现场人工校准"
}
```

## 5. Speaker And Speech API

### POST `/api/matches/{match_id}/speakers/{speaker_id}/activate`

主持人指定发言人。固定环节必须匹配环节配置；自由辩论必须匹配当前轮次方。

### POST `/api/matches/{match_id}/speakers/{speaker_id}/pause-speaking`

人类辩手暂停本人当前发言，后端暂停相关时钟。

### POST `/api/matches/{match_id}/speakers/{speaker_id}/resume-speaking`

人类辩手继续本人暂停中的发言，后端恢复相关时钟。

### POST `/api/matches/{match_id}/speakers/{speaker_id}/skip-turn`

自由辩论中人类辩手跳过本轮；同方人类全部跳过或等待 5 秒无人开始时，后端随机选择本方 AI 接管。

请求：

```json
{
  "phase_id": "phase_free_debate",
  "mode": "host_designate"
}
```

### POST `/api/matches/{match_id}/speakers/{speaker_id}/start-speaking`

人类辩手开始本人发言。辩手端调用，需使用 speaker token。

请求：

```json
{
  "phase_id": "phase_aff_statement_3",
  "client_device_id": "console-mac-03"
}
```

### POST `/api/matches/{match_id}/speakers/{speaker_id}/stop-speaking`

人类辩手结束本人发言。

### POST `/api/matches/{match_id}/speakers/{speaker_id}/asr/partial`

ASR 服务或联调工具写入人类发言 partial。该接口只允许更新当前 active human speech；写入后广播 `asr.partial`，并更新大屏字幕。

请求：

```json
{
  "text": "实时转写中的片段",
  "latency_ms": 520
}
```

### POST `/api/matches/{match_id}/speakers/{speaker_id}/asr/final`

ASR 服务写入 final。该接口会更新当前 `Speech.content_final` 和最新 transcript segment，但不自动结束发言；结束仍由 `stop-speaking` 或主持人控制。

请求：

```json
{
  "text": "最终转写文本",
  "latency_ms": 660
}
```

### POST `/api/matches/{match_id}/speakers/{speaker_id}/asr/fail`

ASR 失败降级。系统保持比赛和时钟可继续运行，大屏显示转写不可用，管理端语音链路标红。

请求：

```json
{
  "reason": "xunfei unavailable"
}
```

### POST `/api/matches/{match_id}/speeches/{speech_id}/audio-chunks`

辩手端用 `multipart/form-data` 上传录音分片。该接口只接受人类辩手本人对应的 active speech；发言刚结束后的补充分片可追加到已存在的 transcript/audio asset。

前端默认优先使用 Web Audio 采集并上传 `audio/L16;rate=16000` PCM 分片，便于后续归档补识别；若浏览器不支持 PCM 采集，则退回 MediaRecorder `audio/webm;codecs=opus` 作为留档保底。

表单字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `speaker_id` | string | 是 | 上传者辩手 ID |
| `chunk_index` | integer | 是 | 从 0 开始递增；重复 index 覆盖同一分片 |
| `duration_ms` | integer | 否 | 分片时长，浏览器未知时可省略 |
| `file` | binary | 是 | 优先 `audio/L16;rate=16000`；可降级 `audio/webm;codecs=opus` |

成功后写入 `audio_assets`，广播 `audio.chunk_archived`，并返回最新 match snapshot。若分片是 PCM/L16、PCM、`audio/raw` 或 `application/octet-stream`，后端同时更新 `speech_service.asr.status = streaming`，并广播 `asr.audio_chunk_received`，用于管理端观测实时 ASR 输入状态。若 `PHDEBATE_ASR_REALTIME=1` 或讯飞 ASR 配置完整，后端会为该发言建立讯飞 ASR 长连接，广播 `asr.stream_started`，并将返回文本写入 `asr.partial` / `asr.final`。

### POST `/api/matches/{match_id}/speeches/{speech_id}/audio/complete`

辩手端停止录音归档后调用，标记该发言音频归档完成。

请求：

```json
{
  "speaker_id": "spk_aff_3",
  "auto_recognize": false
}
```

成功后广播 `audio.archive_completed`。

`auto_recognize` 可选，默认为 `false`。显式为 `true` 时，接口会等待 PCM/L16 归档补识别完成并返回识别后的最新 snapshot；如果归档格式不支持或讯飞服务不可用，返回对应错误。未显式传入时，若后端检测到讯飞 ASR 配置完整，或设置 `PHDEBATE_ASR_AUTO_RECOGNIZE=1`，会在后台自动触发补识别，不阻塞辩手端结束发言。设置 `PHDEBATE_ASR_AUTO_RECOGNIZE=0` 可强制关闭后台自动识别。

### POST `/api/matches/{match_id}/speeches/{speech_id}/asr/recognize`

主持人触发一次已归档人类发言的 ASR 补识别。该接口需要 `host` 或 `admin` token，用于“现场先录音、赛后或中场补转写”的 MVP 闭环。

MVP 只直接支持已经归档为 PCM/L16、PCM、`audio/raw` 或 `application/octet-stream` 的音频分片。辩手端前端会优先产生 PCM/L16 归档；若因浏览器能力退回 MediaRecorder `audio/webm;codecs=opus`，该归档不能直接送讯飞流式听写，需要先转码为 PCM/L16 后再调用本接口。

成功流程：

1. 读取 `audio_assets` 中该 `speech_id` 的分片并按 `chunk_index` 拼接。
2. 将 `speech_service.asr.status` 置为 `recognizing`，广播 `asr.archive_recognition_started`。
3. 调用讯飞 ASR WebSocket Gateway，默认以 `audio/L16;rate=16000`、`raw` 编码发送。
4. 成功后回填 active speech 或 transcript segment，写入 `speech_revisions`（若覆盖已有转写），广播 `asr.final`。

成功响应：

```json
{
  "ok": true,
  "data": {
    "result": {
      "speech_id": "speech_live",
      "text": "归档识别文本",
      "text_length": 6,
      "latency_ms": 345,
      "chunk_count": 2,
      "audio_bytes": 20480
    },
    "snapshot": {}
  }
}
```

不支持格式响应：

```json
{
  "ok": false,
  "error": {
    "code": "unsupported_audio_format",
    "message": "当前归档音频不是讯飞 ASR 可直接识别的 PCM/L16 格式；请使用实时 PCM 流或转码后再识别。",
    "details": {
      "speech_id": "speech_live",
      "mime_type": "audio/webm;codecs=opus"
    }
  }
}
```

### POST `/api/matches/{match_id}/speakers/{speaker_id}/tts/fail`

AI TTS 失败降级。默认进入纯文字展示，不阻塞比赛状态机。

正式 AI 发言由 `POST /api/matches/{match_id}/agent/{speaker_id}/run` 触发。若 `PHDEBATE_TTS_FORMAL=1` 或讯飞 TTS 配置完整，后端会在 Agent final 文本产生后调用讯飞 TTS，成功时写入 `audio_assets(source = agent_tts)` 并广播 `tts.synthesis_started`、`tts.audio_archived`、`tts.finished`；失败时保留 AI transcript，广播 `tts.failed`，并把 `speech_service.tts.degraded_to` 置为 `text_only`。

请求：

```json
{
  "reason": "speaker device unavailable",
  "text_only": true
}
```

### GET `/api/matches/{match_id}/speech/diagnostics`

检查语音链路现场配置。该接口需要 `host` 或 `admin` token，读取讯飞环境变量、音频归档目录和可用降级路径，不会返回密钥明文。

响应：

```json
{
  "ok": true,
  "data": {
    "checked_at": "2026-06-10T20:40:00Z",
    "overall_status": "mock_fallback",
    "provider": "mock",
    "asr": {
      "component": "asr",
      "status": "missing_config",
      "configured": [],
      "missing": ["XFYUN_APP_ID", "XFYUN_API_KEY", "XFYUN_API_SECRET", "XFYUN_ASR_URL"],
      "url": "",
      "auth_ready": false,
      "auth_preview": null,
      "runtime_config": {
        "open_timeout_s": 8,
        "close_timeout_s": 3,
        "final_timeout_s": 12
      },
      "detail": "缺少 XFYUN_APP_ID, XFYUN_API_KEY, XFYUN_API_SECRET, XFYUN_ASR_URL"
    },
    "tts": {
      "component": "tts",
      "status": "missing_config",
      "configured": [],
      "missing": ["XFYUN_APP_ID", "XFYUN_API_KEY", "XFYUN_API_SECRET", "XFYUN_TTS_URL"],
      "url": "",
      "auth_ready": false,
      "auth_preview": null,
      "detail": "缺少 XFYUN_APP_ID, XFYUN_API_KEY, XFYUN_API_SECRET, XFYUN_TTS_URL"
    },
    "audio_archive": {
      "status": "ready",
      "root_path": "apps/backend/storage/audio",
      "writable": true,
      "detail": "音频归档目录可写"
    },
    "realtime_asr": {
      "enabled": false,
      "mode": "auto_when_ready",
      "detail": "PCM/L16 分片只归档和补识别"
    },
    "auto_recognize": {
      "enabled": false,
      "mode": "auto_when_ready",
      "detail": "PCM/L16 归档完成后需主持人手动识别"
    },
    "formal_tts": {
      "enabled": false,
      "mode": "auto_when_ready",
      "detail": "AI 正式发言仅展示文字/模拟 TTS 状态"
    },
    "fallbacks": {
      "mock_agent": true,
      "manual_asr_controls": true,
      "text_only_tts": true,
      "audio_recording_without_asr": true
    },
    "next_steps": ["补齐讯飞环境变量：XFYUN_APP_ID, XFYUN_API_KEY, XFYUN_API_SECRET, XFYUN_ASR_URL, XFYUN_TTS_URL。"]
  }
}
```

### POST `/api/matches/{match_id}/speech/tts/probe`

主持人触发一次讯飞 TTS 试合成。该接口需要 `host` 或 `admin` token，不要求比赛正在运行；成功后把音频保存到 `PHDEBATE_AUDIO_DIR/diagnostics/`，更新 `speech_service.tts`，广播 `tts.started`、`tts.probe_completed`、`tts.finished`。缺少讯飞配置或服务返回错误时，系统标记 TTS 降级并返回 `speech_service_error`。

请求：

```json
{
  "text": "人机辩论赛语音合成自检。"
}
```

成功响应：

```json
{
  "ok": true,
  "data": {
    "result": {
      "probe": true,
      "mime_type": "audio/mpeg",
      "size_bytes": 24576,
      "chunk_count": 3,
      "latency_ms": 920,
      "file_path": "apps/backend/storage/audio/diagnostics/tts_probe_20260610T210000Z.mp3"
    },
    "snapshot": {}
  }
}
```

### POST `/api/matches/{match_id}/speech/asr/probe`

主持人触发一次讯飞 ASR 自检。该接口需要 `host` 或 `admin` token，不要求比赛正在运行；默认发送一段短静音 PCM，验证 ASR 签名、WebSocket 连接、服务响应和状态更新。也可传入 Base64 PCM 用于真实音频试识别。缺少讯飞配置或服务返回错误时，系统标记 ASR 失败并返回 `speech_service_error`。

请求：

```json
{
  "audio_base64": "",
  "format": "audio/L16;rate=16000",
  "encoding": "raw"
}
```

成功响应：

```json
{
  "ok": true,
  "data": {
    "result": {
      "probe": true,
      "text": "",
      "text_length": 0,
      "latency_ms": 450,
      "chunk_count": 5,
      "audio_bytes": 6400
    },
    "snapshot": {}
  }
}
```

### POST `/api/matches/{match_id}/speakers/{speaker_id}/request-ai-teammate`

自由辩论中，人类辩手请求 AI 队友发言。

请求：

```json
{
  "agent_speaker_id": "spk_aff_2",
  "phase_id": "phase_free_debate"
}
```

### PATCH `/api/matches/{match_id}/speeches/{speech_id}`

修正转写文本或标记发言有效/无效。

请求：

```json
{
  "content_final": "修正后的正式文本",
  "valid": true,
  "reason": "修正 ASR 错字"
}
```

## 6. Agent Control API

### POST `/api/matches/{match_id}/agent/{speaker_id}/health`

检查单个 AI 辩手的 `{agent_endpoint}/health`。该接口可在比赛前或比赛中调用；失败只更新 `agent_status` 和广播 `agent.failed`，不改变比赛状态。

响应：

```json
{
  "ok": true,
  "data": {
    "result": {
      "speaker_id": "spk_aff_2",
      "endpoint": "http://127.0.0.1:8100",
      "ok": true,
      "status": "ready",
      "model": "mock-agent-normal",
      "latency_ms": 24,
      "checked_at": "2026-06-10T12:00:00Z"
    },
    "snapshot": {}
  }
}
```

### POST `/api/matches/{match_id}/agents/health`

批量检查所有 AI 辩手，逐个更新 `agent_status`。

### POST `/api/matches/{match_id}/agent/{speaker_id}/retry`

重试 AI 发言。重试会创建新的 `task_id`，旧任务标记为 `discarded` 或 `failed`。

### POST `/api/matches/{match_id}/agent/{speaker_id}/interrupt`

中断 AI 生成和 TTS。

请求：

```json
{
  "task_id": "task_001",
  "reason": "主持人手动中断"
}
```

### POST `/api/matches/{match_id}/agent/{speaker_id}/manual-input`

AI 异常时由主持人人工代输入文本。成功后服务端会把文本作为正式 `manual` 来源 transcript 写入，广播 `agent.manual_input.accepted`、`agent.speech.final` 和 `speech.ended`，并按当前环节规则结束本次发言；自由辩论中会切换到对方轮次。

请求：

```json
{
  "content": "主持人代输入的发言文本",
  "reason": "agent_timeout"
}
```

成功响应中的 `data.recent_transcript[0]` 应包含本次 `manual` 文本，`current_speech` 应为 `null`。

## 7. Screen API

### POST `/api/matches/{match_id}/screen/scene`

切换大屏场景。

请求：

```json
{
  "scene": "live",
  "live_mode": "free"
}
```

`scene = live` 时 `live_mode` 必须为 `single`、`free` 或 `prep`。

## 8. Emergency API

### POST `/api/matches/{match_id}/emergency-stop`

紧急停止。该接口可在 `draft` 之外任意状态调用。

效果：

- 暂停或停止所有时钟。
- 中断所有 Agent SSE。
- 停止 TTS 播放。
- 锁定所有辩手麦克风。
- 写 `match.emergency_stopped` 和审计日志。

请求：

```json
{
  "reason": "现场设备异常"
}
```

## 9. Vote API

### POST `/api/matches/{match_id}/votes`

主持人代录评委票。

请求：

```json
{
  "voter_type": "judge",
  "voter_id": "judge_01",
  "items": [
    { "vote_type": "constructive", "target_side": "affirmative" },
    { "vote_type": "process", "target_side": "negative" },
    { "vote_type": "conclusion", "target_side": "affirmative" },
    { "vote_type": "best_speaker", "target_speaker_id": "spk_neg_2" }
  ]
}
```

### POST `/api/matches/{match_id}/audience-votes/open`

开启学生投票窗口，返回二维码 URL。

响应：

```json
{
  "ok": true,
  "data": {
    "vote_url": "/vote/match_001",
    "window_status": "open"
  },
  "server_time_ms": 1760000000000
}
```

### POST `/api/matches/{match_id}/audience-votes/close`

关闭学生投票窗口。

### POST `/api/matches/{match_id}/votes/publish`

公布投票结果。

请求：

```json
{
  "scope": "judge"
}
```

`scope = audience` 时必须已公布 `judge` 结果。

### POST `/api/public/matches/{match_id}/audience-votes`

学生扫码提交投票，无需登录，但必须带防重复 token 或浏览器指纹。

请求：

```json
{
  "token": "one-time-token",
  "winner_side": "affirmative",
  "best_speaker_id": "spk_neg_2",
  "client_fingerprint": "hash-from-browser"
}
```

错误：

- 投票窗口未开：`invalid_state`
- 重复投票：`forbidden`
- 目标辩手不属于该比赛：`validation_error`

## 10. Snapshot Shape

`GET /api/matches/{match_id}` 和 WebSocket snapshot 复用同一结构：

```json
{
  "match": {
    "id": "match_001",
    "status": "running",
    "screen_scene": "live",
    "live_mode": "free",
    "current_phase_id": "phase_free_debate"
  },
  "teams": [],
  "speakers": [],
  "phases": [],
  "clocks": [],
  "current_speech": null,
  "recent_transcript": [],
  "agent_status": [],
  "vote_state": {},
  "last_seq": 1842
}
```
