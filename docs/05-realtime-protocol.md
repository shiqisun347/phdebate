# 05 · Realtime Protocol

本章定义 WebSocket 协议、事件信封、断线续传和前端订阅规则。

## 1. WebSocket 连接

统一入口：

```text
GET /ws/matches/{match_id}?channel=admin|screen|speaker&last_seq=1842&speaker_id=spk_aff_3
```

鉴权：

- `admin`：需要管理员或主持人 token。
- `screen`：使用只读 token 或局域网白名单。
- `speaker`：需要 `speaker_id` 与 speaker token。
- 浏览器 WebSocket 使用查询参数传 token：`&token=<token>`。

连接建立后，服务端必须先发送 `snapshot`，再发送增量事件。

## 2. 消息信封

所有服务端消息使用统一结构：

```json
{
  "type": "agent.speech.delta",
  "match_id": "match_001",
  "seq": 1843,
  "server_time_ms": 1760000000000,
  "payload": {}
}
```

特殊消息：

```json
{
  "type": "snapshot",
  "match_id": "match_001",
  "seq": 1842,
  "server_time_ms": 1760000000000,
  "payload": {
    "state": {},
    "missed_events": []
  }
}
```

## 3. 断线续传

客户端保存最后处理成功的 `seq`。

重连规则：

1. 客户端用 `last_seq` 建立 WebSocket。
2. 服务端读取当前快照和 `seq > last_seq` 的事件。
3. 若缺失事件数量在服务端保留窗口内，返回 `snapshot + missed_events`。
4. 若事件过多或无法补齐，返回 `snapshot + reset_required = true`，客户端丢弃本地派生状态。

快照是权威状态；事件用于动画、日志和局部更新。客户端不得只依赖事件流恢复状态。

## 4. 频道视图

| channel | 可见内容 | 过滤规则 |
| --- | --- | --- |
| `admin` | 完整状态、事件、审计摘要、错误 | 不过滤敏感运行状态，但不发送 API Key 明文 |
| `screen` | 大屏展示聚合状态 | 不发送后台配置、token、审计详情 |
| `speaker` | 当前辩手身份、可操作状态、计时、提示 | 只发送该辩手相关状态和公开比赛信息 |

## 5. 服务端事件 payload

### `phase.started`

```json
{
  "phase_id": "phase_aff_constructive_1",
  "phase_key": "aff_constructive_1",
  "name": "正方一辩立论",
  "display_order": 1,
  "total_phases": 10
}
```

### `speaker.activated`

```json
{
  "phase_id": "phase_free_debate",
  "speaker_id": "spk_aff_3",
  "side": "affirmative",
  "turn_index": 14,
  "assignment_mode": "teammate_control"
}
```

### `clock.started`

```json
{
  "clock_name": "turn",
  "state": "running",
  "remaining_ms": 15000,
  "deadline_at": "2026-06-10T12:00:15.000Z"
}
```

### `asr.partial`

```json
{
  "speech_id": "speech_001",
  "speaker_id": "spk_aff_3",
  "text": "真正能驱动 AI 解决复杂问题的",
  "is_final": false
}
```

### `asr.final`

```json
{
  "speech_id": "speech_001",
  "speaker_id": "spk_aff_3",
  "text": "真正能驱动 AI 解决复杂问题的，恰恰是把大问题拆成可执行步骤的能力。",
  "is_final": true
}
```

### `audio.chunk_archived`

```json
{
  "audio_asset_id": "audio_speech_001",
  "speech_id": "speech_001",
  "speaker_id": "spk_aff_3",
  "chunk_index": 4,
  "chunk_count": 5,
  "size_bytes": 98304,
  "file_path": "apps/backend/storage/audio/match_001/free_debate/speech_001/chunk_00004.pcm",
  "pcm_ready": true
}
```

### `asr.audio_chunk_received`

PCM/L16 分片已进入 ASR 输入队列或可补识别归档。该事件不代表已产生文本。

```json
{
  "audio_asset_id": "audio_speech_001",
  "speech_id": "speech_001",
  "speaker_id": "spk_aff_3",
  "chunk_index": 4,
  "chunk_count": 5,
  "size_bytes": 98304,
  "file_path": "apps/backend/storage/audio/match_001/free_debate/speech_001/chunk_00004.pcm",
  "pcm_ready": true
}
```

### `asr.stream_started`

后端已为该发言建立讯飞 ASR 长连接。只有 `PHDEBATE_ASR_REALTIME=1` 或讯飞 ASR 配置完整时才会触发。

```json
{
  "speech_id": "speech_001",
  "speaker_id": "spk_aff_3"
}
```

### `audio.archive_completed`

```json
{
  "audio_asset_id": "audio_speech_001",
  "speech_id": "speech_001",
  "speaker_id": "spk_aff_3",
  "chunk_count": 9,
  "size_bytes": 176128,
  "file_path": "apps/backend/storage/audio/match_001/free_debate/speech_001"
}
```

### `agent.speech.delta`

```json
{
  "task_id": "task_001",
  "speech_id": "speech_002",
  "speaker_id": "spk_neg_3",
  "delta": "对方辩友混淆了工具与思维："
}
```

### `agent.speech.final`

```json
{
  "task_id": "task_001",
  "speech_id": "speech_002",
  "speaker_id": "spk_neg_3",
  "content": "对方辩友混淆了工具与思维：拆解步骤是 AI 的强项，而决定拆什么、为何拆，恰恰来自好的提问。",
  "usage": {
    "model": "Kimi-K2",
    "latency_ms": 1600
  }
}
```

### `agent.manual_input.accepted`

```json
{
  "speech_id": "speech_102",
  "speaker_id": "spk_aff_2",
  "side": "affirmative",
  "content": "主持人代输入的发言文本",
  "reason": "agent_timeout",
  "source": "manual"
}
```

### `vote.published`

```json
{
  "scope": "judge",
  "winner_side": "affirmative",
  "best_speaker_id": "spk_neg_2",
  "summary": {
    "constructive": { "affirmative": 2, "negative": 1 },
    "process": { "affirmative": 1, "negative": 2 },
    "conclusion": { "affirmative": 3, "negative": 0 }
  }
}
```

## 6. 客户端上行消息

管理命令优先走 REST。WebSocket 上行只保留轻量状态：

### `speaker.heartbeat`

```json
{
  "type": "speaker.heartbeat",
  "payload": {
    "speaker_id": "spk_aff_3",
    "mic_permission": "granted",
    "device_label": "MacBook microphone"
  }
}
```

### `speaker.mic_error`

```json
{
  "type": "speaker.mic_error",
  "payload": {
    "speaker_id": "spk_aff_3",
    "message": "Microphone permission denied"
  }
}
```

## 7. 顺序与幂等

- 服务端消息按 `seq` 单调递增。
- 客户端发现 `seq` 跳号，应立即重连并带上最后成功处理的 `seq`。
- 同一事件重复到达时，客户端按 `seq` 去重。
- 前端渲染倒计时使用最新同名 `clock` 状态覆盖旧状态。

## 8. Screen 聚合状态

大屏不直接推导复杂业务，而接收后端聚合：

```json
{
  "scene": "live",
  "live_mode": "free",
  "topic": "AI 时代，我们更应该培养编程思维 / 提问思维",
  "phase": {
    "name": "自由辩论",
    "display_order": 7,
    "total_phases": 10
  },
  "current_speaker": {
    "id": "spk_aff_3",
    "name": "林晚晴",
    "side": "affirmative",
    "seat": 3,
    "speaker_type": "human"
  },
  "next_hint": "反方发言",
  "clocks": {
    "affirmative_total": {},
    "negative_total": {},
    "turn": {}
  },
  "subtitle": {
    "speaker_label": "正方三辩 · 林晚晴",
    "tag": "实时转写",
    "text": "对方辩友说提问思维是起点……",
    "is_partial": true
  }
}
```

## 9. 心跳

- 客户端每 5 秒发送 WebSocket ping 或 `client.ping`。
- 服务端 15 秒未收到心跳可标记对应 console 离线。
- Agent 心跳不走此 WebSocket，由 Agent Gateway 轮询 `GET /health` 并广播 `agent.connected` 或 `agent.failed`。
