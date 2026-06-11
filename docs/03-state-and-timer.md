# 03 · State And Timer

本章定义比赛状态机、环节状态机、自由辩论调度和权威计时器。实现时 Flow Engine 与 Timer Service 必须作为后端唯一状态源。

## 1. 比赛状态机

```text
draft -> ready -> running -> paused -> running -> finished -> archived
                 running -> intervention -> running
                 running -> finished
                 paused  -> finished
                 any active state -> intervention
```

| 当前状态 | 命令 | 下一个状态 | 约束 |
| --- | --- | --- | --- |
| `draft` | `configure` | `ready` | 必须有辩题、双方队伍、8 位辩手、10 个环节 |
| `ready` | `start_match` | `running` | 初始化第一个环节但不自动开始麦克风 |
| `running` | `pause_match` | `paused` | 暂停所有 running clocks |
| `paused` | `resume_match` | `running` | 恢复暂停前的 running clocks |
| `running` | `enter_intervention` | `intervention` | 通常由 AI/ASR/TTS 异常或主持人触发 |
| `intervention` | `resolve_intervention` | `running` | 必须给出处理动作：重试、跳过、代输入、作废 |
| `running`/`paused` | `finish_match` | `finished` | 结束所有时钟、关闭学生投票窗口 |
| `finished` | `archive_match` | `archived` | 归档后只读 |

## 2. 环节状态机

```text
pending -> active -> speaker_active -> completed
                  -> waiting_next_speaker -> speaker_active
active  -> skipped
completed -> reopened -> active
```

| 状态 | 说明 | 可用命令 |
| --- | --- | --- |
| `pending` | 等待进入 | `start_phase` |
| `active` | 环节已进入，等待发言人或 AI 准备 | `activate_speaker`、`skip_phase` |
| `speaker_active` | 发言中 | `stop_speaking`、`force_stop_speech`、`pause_clock` |
| `waiting_next_speaker` | 自由辩论等待下一轮 | `activate_speaker`、`complete_phase` |
| `completed` | 正常完成 | `rollback_to_phase` |
| `skipped` | 跳过 | `rollback_to_phase` |
| `reopened` | 回退重开 | `start_phase` |

固定发言环节的合法发言人由 `phase.side + phase.speaker_seat` 唯一确定。自由辩论允许当前轮次方任意本方辩手发言，但同一时刻只能有一个发言人。

## 3. 时钟模型

普通环节创建一个时钟：

| name | 用途 |
| --- | --- |
| `main` | 当前发言剩余时间 |

自由辩论创建三个时钟：

| name | 用途 |
| --- | --- |
| `affirmative_total` | 正方自由辩论总时间 |
| `negative_total` | 反方自由辩论总时间 |
| `turn` | 单次发言 15 秒上限 |

## 4. Deadline 同步算法

后端保存 `remaining_ms` 和 `deadline_at`。时钟开始或继续时：

```text
deadline_at = now_utc + remaining_ms
state = running
```

暂停时：

```text
remaining_ms = max(0, deadline_at - now_utc)
deadline_at = null
state = paused
```

到时时：

```text
remaining_ms = 0
deadline_at = null
state = expired
emit clock.expired
```

前端展示时：

```text
display_remaining_ms = max(0, deadline_at - client_estimated_server_now)
```

WebSocket 每次事件都带 `server_time_ms`，前端用它校准本地偏移。1Hz tick 只作为校准和日志，不作为主同步机制。

## 5. 固定环节流程

1. 主持人开始环节，Flow Engine 设置 `phase.status = active`。
2. 如果发言人为人类：辩手端进入 `ready`，等待本人点击开始。
3. 如果发言人为 AI：Agent Gateway 进入 `prep`，开始生成/TTS 准备。
4. 人类点击开始或 TTS 首字播出时，Timer Service 启动 `main`。
5. 发言结束、到时或主持人强制结束时，停止 `main`，写入 `Speech`。
6. 如果配置为自动推进且无异常，进入下一环节；MVP 默认由主持人确认推进。

## 6. 自由辩论流程

### 6.1 轮次状态

自由辩论状态由以下字段聚合：

```json
{
  "current_turn_side": "affirmative",
  "turn_index": 14,
  "active_speaker_id": "spk_aff_3",
  "assignment_mode": "teammate_control",
  "affirmative_total_remaining_ms": 151000,
  "negative_total_remaining_ms": 185000,
  "turn_remaining_ms": 11000
}
```

实现时所有剩余时间字段都应为数字毫秒值，不使用格式化字符串。

### 6.2 轮次开始

- 正方先手，`current_turn_side = affirmative`。
- 进入某方轮次时，只有该方辩手控制台可用。
- `host_designate`：主持人在管理端点名发言人。
- `teammate_control`：本方人类可点击开始本人发言，也可点击“让 AI 队友发言”。
- 任一发言人开始后，锁定其他辩手按钮。

### 6.3 轮次计时

人类或 AI 正式开始发言时：

- 启动本方总时钟。
- 重置并启动 `turn` 时钟。
- 对方总时钟保持暂停。

发言结束时：

- 暂停本方总时钟。
- 停止 `turn`。
- 切换 `current_turn_side` 到对方。
- `phase.status = waiting_next_speaker`。

### 6.4 到时处理

| 场景 | MVP 行为 |
| --- | --- |
| `turn` 到 5 秒 | 发 `clock.expiring_soon` 提醒，大屏/管理端可提示 |
| `turn` 到 0 | 发 `speech.timeout`，默认等待主持人确认结束或强制结束 |
| 某方总时钟到 0 | 该方不可继续发言，另一方可指定一名辩手使用剩余时间连续陈词 |
| 双方总时钟均到 0 | 自由辩论环节完成 |

## 7. AI 计时策略

默认策略：

- AI 生成和 TTS 准备阶段为 `prep`，不计入发言时长。
- TTS 播出第一个字时发 `tts.started`，同时启动发言时钟。
- 常规环节 `preparing_timeout_seconds = 10`；自由辩论 `preparing_timeout_seconds = 5`。
- 超长处理默认 `finish_current_sentence`：到时响铃，允许念完当前句，然后停止 TTS。

当前 MVP 的正式 TTS 先完成音频归档，`tts.started` 仍用于驱动大屏进入 AI 发言态；接入真实扩声播放队列后，应把计时起点校准到首段音频实际开始播放。

固定环节如果下一位是 AI，前一位发言结束后立即创建 `prefetch` AgentTask。主持人正式开始该环节时，如果任务已完成或已有可播句子，直接进入 TTS 播放。

## 8. 回退与跳过

### 8.1 跳过

跳过当前环节时：

- 当前 running clocks 全部 `stopped`。
- 当前进行中的 ASR、Agent、TTS 全部中断。
- `phase.status = skipped`。
- 追加 `phase.skipped`、`audit.logged`。

### 8.2 回退

回退到某环节时：

- 目标环节及之后所有 `speeches.valid = 0`，`invalid_reason = rollback`。
- 目标环节设置为 `reopened`，之后环节重置为 `pending`。
- 当前 clocks 停止并按目标环节重新初始化。
- 追加 `phase.rolled_back`，payload 必须包含 `from_phase_id`、`to_phase_id`、`invalidated_speech_ids`。

## 9. 合法性校验

所有控制命令必须经过 Flow Engine：

- 比赛不是 `running` 时，除 `resume_match`、`finish_match`、`emergency_stop` 外不允许推进。
- 固定环节只能激活指定辩位。
- 自由辩论只能激活当前轮次方辩手。
- 人类辩手只能开始自己的发言。
- AI Agent 不能直接改变比赛状态，只能返回内容或状态。
- 紧急停止可在任意非归档状态执行。
