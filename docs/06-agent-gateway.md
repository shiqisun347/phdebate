# 06 · Agent Gateway

本章定义 AI 辩手接入协议、任务生命周期、上下文输入、流式输出、超时和 mock agent 契约。

## 1. 接入原则

- 每个 AI 辩手在 `speakers.agent_endpoint` 配置一个基础 URL。
- 后端主动调用 Agent；Agent 不直接访问比赛数据库，也不能直接改变比赛状态。
- 同一个 Agent 实现可以服务多个 AI 辩手，但必须按 `task_id` 幂等处理。
- MVP 支持正式任务 `official` 和固定环节提前下发 `prefetch`。

## 2. Agent 端点

### GET `{agent_endpoint}/health`

响应：

```json
{
  "ok": true,
  "status": "ready",
  "model": "model-name",
  "version": "2026-06-10",
  "latency_ms": 32
}
```

`status` 可选：`ready`、`busy`、`degraded`、`unavailable`。

### POST `{agent_endpoint}/speech`

下发发言任务。`output.stream = true` 时返回 `text/event-stream`；否则返回 JSON。

请求：

```json
{
  "model_name": "qwen3.6-plus",
  "debater_name": "穷理",
  "debate_position": "三辩",
  "current_stage": "自由辩论",
  "next_stage": "反方四辩总结",
  "holder": "反方",
  "debate_history": [
    {
      "stage": "正方一辩立论",
      "message": [{ "speaker": "正方一辩 · 林晚晴", "content": "发言文本" }]
    }
  ],
  "match_id": "match_001",
  "task_id": "task_001",
  "topic": "AI 时代，我们更应该培养编程思维 / 提问思维",
  "side": "negative",
  "speaker_id": "spk_neg_3",
  "speaker_name": "穷理",
  "speaker_role": "三辩",
  "phase": "free_debate",
  "phase_type": "free_debate",
  "turn_index": 14,
  "time_limit_seconds": 15,
  "remaining_seconds": 15,
  "target_chars": 70,
  "context": {
    "summary": "前序发言摘要",
    "transcript_tail": [],
    "opponent_claims": [],
    "own_claims": [],
    "host_notes": []
  },
  "output": {
    "stream": true,
    "language": "zh-CN"
  }
}
```

`model_name`、`debater_name`、`debate_position`、`current_stage`、`next_stage`、`holder`、`debate_history` 是正式 Agent 推荐消费字段；原有 `context.transcript_tail` 等字段继续作为兼容输入保留。

非流式响应：

```json
{
  "task_id": "task_001",
  "status": "completed",
  "content": "发言文本",
  "usage": {
    "model": "model-name",
    "latency_ms": 1200
  },
  "error": null
}
```

### POST `{agent_endpoint}/interrupt`

请求 Agent 中断生成。后端会同时关闭本地 SSE 连接并停止 TTS，因此该接口是尽力而为。

请求：

```json
{
  "task_id": "task_001",
  "reason": "host_interrupt"
}
```

响应：

```json
{
  "ok": true,
  "task_id": "task_001",
  "status": "interrupted"
}
```

## 3. SSE 流式事件

Agent SSE 每帧使用 `data: <json>`。

### delta

```json
{
  "type": "delta",
  "task_id": "task_001",
  "delta": "部分文本"
}
```

### final

```json
{
  "type": "final",
  "task_id": "task_001",
  "content": "完整文本",
  "usage": {
    "model": "model-name",
    "latency_ms": 1200
  }
}
```

### error

```json
{
  "type": "error",
  "task_id": "task_001",
  "error": {
    "code": "model_timeout",
    "message": "generation timeout"
  }
}
```

后端收到 `delta` 后广播 `agent.speech.delta`，按句切分送 TTS。收到 `final` 后写入 `speeches.content_final` 并广播 `agent.speech.final`。

主持人选择人工代输入时，不再等待 Agent；后端直接写入 `source = manual` 的 transcript，广播 `agent.manual_input.accepted` 与 `agent.speech.final`，并结束当前发言。

## 4. 任务生命周期

```text
created -> sent -> streaming -> completed
                    -> failed
                    -> timeout
                    -> interrupted
created/sent -> discarded
```

| 状态 | 触发 |
| --- | --- |
| `created` | 后端创建 `AgentTask` |
| `sent` | HTTP 请求发出 |
| `streaming` | 收到首个 delta |
| `completed` | 收到 final 或非流式成功响应 |
| `failed` | Agent 返回 error 或 HTTP 错误 |
| `timeout` | 超过配置的连接、首字或总生成超时 |
| `interrupted` | 主持人中断 |
| `discarded` | prefetch/speculative 未被使用或重试替换 |

## 5. 上下文构造

Agent Gateway 从 Transcript Service 构造上下文：

| 字段 | 来源 | MVP 要求 |
| --- | --- | --- |
| `summary` | 已完成环节摘要 | 可先由规则模板或人工摘要生成，不能为空字符串 |
| `transcript_tail` | 最近 N 条有效发言 | 默认最近 8 条 |
| `opponent_claims` | 主持人标记或摘要提取 | 可为空数组 |
| `own_claims` | 主持人标记或摘要提取 | 可为空数组 |
| `host_notes` | 主持人手动备注 | 可为空数组 |

`target_chars` 按中文 TTS 语速估算：

```text
target_chars = time_limit_seconds * chars_per_second
chars_per_second 默认 4.5
```

自由辩论 15 秒默认 60-75 字；3 分钟默认 720-900 字。

## 6. 时序策略

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `connect_timeout_ms` | 3000 | 建连超时 |
| `first_delta_timeout_ms` | 5000 | 自由辩论建议 3000-5000 |
| `total_timeout_ms` | 30000 | 常规长发言可放宽 |
| `max_retries` | 1 | 主持人手动重试不受此限制 |
| `stream` | true | MVP 默认流式 |
| `tts_start_policy` | `first_sentence` | 首句完整后开始 TTS |
| `timer_start_policy` | `tts_first_audio` | TTS 首字播出开始计时 |

当前 MVP 已在 Agent final 后接入正式 TTS 音频归档：启用 `PHDEBATE_TTS_FORMAL=1` 或讯飞 TTS 配置完整时，系统调用讯飞 TTS，成功后广播 `tts.audio_archived` 并把音频写入 `audio_assets(source = agent_tts)`。低延迟首句播放队列仍按上表作为后续优化目标。

固定环节提前下发：

- 前一位发言结束后，如果下一环节发言人是 AI，立即创建 `prefetch` 任务。
- 正式进入下一环节时，若 `prefetch` 可用，将其升级为 `official` 或复制内容到正式 Speech。
- 如果主持人回退或跳过，未使用任务标记为 `discarded`。

## 7. 错误与降级

| 错误 | 后端事件 | 主持人可选动作 |
| --- | --- | --- |
| 健康检查失败 | `agent.failed` | 重连、跳过、人工代输入 |
| 首字超时 | `agent.failed` | 重试、跳过、人工代输入 |
| 流式中断 | `agent.failed` | 使用已生成内容、重试、人工代输入 |
| 文本过长 | `speech.timeout` | 到点硬停、念完当前句、仅提醒 |
| TTS 失败 | `tts.failed` | 纯文字展示、重试 TTS、人工朗读 |

任何降级路径都要写 `AuditLog`。

## 8. Mock Agent

MVP 开发必须提供 mock agent，用于无真实模型时联调。

行为要求：

- `GET /health` 始终返回 `ready`，可通过 query 或环境变量模拟失败。
- `POST /speech` 支持 SSE，每 200-500ms 输出一个短 delta。
- 支持按 `target_chars` 截断内容。
- 支持 `interrupt` 后停止对应任务。
- 支持模拟超时、HTTP 500、流式中断。

推荐启动：

```text
python -m mock_agent --port 8100 --profile normal
python -m mock_agent --port 8101 --profile slow
python -m mock_agent --port 8102 --profile flaky
```

## 9. 安全要求

- 后端调用 Agent 时可附加共享密钥：`X-Phdebate-Agent-Token`。
- Agent endpoint、API key、模型参数只存后端，不下发到前端。
- Agent 返回内容必须作为不可信输入处理：前端纯文本渲染，不执行 HTML。
