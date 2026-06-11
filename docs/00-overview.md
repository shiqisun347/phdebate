# 00 · Overview

本章定义 MVP 的共同语言：系统边界、技术栈、角色权限、核心枚举与命名约定。后续章节中的接口、数据库字段和前端状态必须复用这里的稳定标识。

## 1. MVP 目标

【MVP】实现一场线下 4v4 人机辩论赛从赛前配置、现场控制、实时展示、发言采集、AI 接入、语音播报、投票记录到结果公布的完整闭环。

MVP 只服务单场比赛，不实现赛事级排程、多房间并发、多赛制模板市场和完整评委端。所有延后能力只保留兼容字段或扩展点。

## 2. 技术栈

| 层级 | 选型 | 约定 |
| --- | --- | --- |
| 后端 | Python 3.11+ / FastAPI | REST、WebSocket、SSE、后台任务统一由 FastAPI 承载 |
| 前端 | Vite / React / TypeScript | 单工程三入口：`/screen`、`/console/:speakerId`、`/admin` |
| 存储 | SQLite | MVP 单机场景；SQL 与 ID 设计保留迁移 PostgreSQL 的空间 |
| 实时同步 | WebSocket | 后端为唯一权威状态源，客户端断线后按 `last_seq` 恢复 |
| Agent | HTTP + SSE | 每个 AI 辩手配置独立 `agent_endpoint` |
| ASR/TTS | 科大讯飞 | 流式听写、流式语音合成；失败时降级为计时和文字展示 |
| 部署 | 现场局域网单机 | 后端托管 API、WebSocket、静态前端和音频归档文件 |

## 3. 角色权限矩阵

| 能力 | 管理员 | 主持人 | 人类辩手 | AI Agent | 大屏/观众 | 学生投票 |
| --- | --- | --- | --- | --- | --- | --- |
| 创建/编辑比赛配置 | 允许 | 可读 | 禁止 | 禁止 | 禁止 | 禁止 |
| 启动/暂停/继续/结束比赛 | 允许 | 允许 | 禁止 | 禁止 | 只读 | 禁止 |
| 推进/回退/跳过环节 | 允许 | 允许 | 禁止 | 禁止 | 只读 | 禁止 |
| 指定发言人 | 允许 | 允许 | 仅本方自由辩论请求 | 禁止 | 只读 | 禁止 |
| 开始/结束本人发言 | 禁止 | 可代操作 | 仅本人且轮到时 | 禁止 | 只读 | 禁止 |
| 请求 AI 队友发言 | 禁止 | 允许 | 仅本方自由辩论 | 接收任务 | 只读 | 禁止 |
| 中断/重试/人工代输入 AI | 允许 | 允许 | 禁止 | 被动接收 | 只读 | 禁止 |
| 修正 transcript | 允许 | 允许 | 禁止 | 禁止 | 只读 | 禁止 |
| 录入评委票/公布结果 | 允许 | 允许 | 禁止 | 禁止 | 只读 | 禁止 |
| 提交学生票 | 禁止 | 禁止 | 禁止 | 禁止 | 禁止 | 允许 |
| 紧急停止 | 允许 | 允许 | 禁止 | 被中断 | 只读 | 禁止 |

## 4. 核心实体

| 实体 | 说明 |
| --- | --- |
| `Match` | 一场比赛的基础信息、状态、当前环节、当前大屏场景 |
| `Team` | 正方或反方队伍 |
| `Speaker` | 辩手；包括人类辩手和 AI 辩手 |
| `Phase` | 比赛流程环节，如立论、陈词、自由辩论、总结、点评合票 |
| `Clock` | 后端权威计时器；一个环节可挂多个命名时钟 |
| `Speech` | 一次正式发言记录，来源可以是人类 ASR、AI 文本或主持人手输 |
| `TranscriptSegment` | 实时 partial/final 文本片段，用于大屏、管理端和归档 |
| `AgentTask` | 一次 AI 发言任务，包含下发、流式返回、中断、重试状态 |
| `Vote` | 评委、AI 评委或学生投票记录 |
| `Event` | 场内全局有序事件，是状态恢复和审计的主要依据 |
| `AuditLog` | 管理员/主持人的操作审计记录 |

## 5. 核心枚举

### 5.1 MatchStatus

| 值 | 说明 |
| --- | --- |
| `draft` | 已创建但配置未完成 |
| `ready` | 配置完成，等待开始 |
| `running` | 比赛进行中 |
| `paused` | 主持人暂停比赛 |
| `intervention` | 人工干预中，如 AI 卡顿、设备异常、需手动修正 |
| `finished` | 比赛结束，结果可公布或已公布 |
| `archived` | 已归档，只读 |

### 5.2 PhaseStatus

| 值 | 说明 |
| --- | --- |
| `pending` | 未开始 |
| `active` | 当前环节已激活，等待发言人或准备动作 |
| `speaker_active` | 当前环节中有人或 AI 正在发言 |
| `waiting_next_speaker` | 等待下一位发言人，主要用于自由辩论 |
| `completed` | 正常完成 |
| `skipped` | 主持人跳过 |
| `reopened` | 回退后重新打开 |

### 5.3 Side

| 值 | 说明 |
| --- | --- |
| `affirmative` | 正方 |
| `negative` | 反方 |
| `neutral` | 评委、主持人、系统等无持方角色 |

### 5.4 SpeakerType

| 值 | 说明 |
| --- | --- |
| `human` | 人类辩手，使用辩手控制台采集麦克风 |
| `agent` | AI 辩手，由后端 Agent Gateway 调用 |

### 5.5 ClockState

| 值 | 说明 |
| --- | --- |
| `idle` | 未开始 |
| `running` | 正在倒计时 |
| `paused` | 暂停，保留剩余时间 |
| `expired` | 已到时 |
| `stopped` | 被主持人或系统停止 |

### 5.6 ScreenScene

| 值 | 说明 |
| --- | --- |
| `idle` | 候场页 |
| `opening` | 辩题与规则介绍 |
| `teams` | 阵容介绍 |
| `live` | 比赛实况 |
| `intermission` | 中场/评委合议/学生投票二维码 |
| `result` | 结果揭晓 |

### 5.7 LiveMode

| 值 | 说明 |
| --- | --- |
| `single` | 立论、陈词、总结等单人发言 |
| `free` | 自由辩论，展示双方总时钟和单次 15 秒时钟 |
| `prep` | AI 准备中，展示等待状态，不开始发言计时 |

### 5.8 SpeechSource

| 值 | 说明 |
| --- | --- |
| `human_asr` | 人类辩手音频经 ASR 产生 |
| `agent_text` | AI Agent 生成文本 |
| `manual` | 主持人人工代输入 |

### 5.9 VoteType

| 值 | 说明 |
| --- | --- |
| `constructive` | 立论票 |
| `process` | 过程票 |
| `conclusion` | 结辩票 |
| `winner` | 优胜方 |
| `best_speaker` | 最佳辩手 |

### 5.10 EventType

事件类型使用点分命名，第一段为领域，第二段为动作，必要时第三段为状态。

| 类型 | 说明 |
| --- | --- |
| `match.created` | 比赛创建 |
| `match.updated` | 比赛配置更新 |
| `match.started` | 比赛开始 |
| `match.paused` | 比赛暂停 |
| `match.resumed` | 比赛继续 |
| `match.finished` | 比赛结束 |
| `match.emergency_stopped` | 紧急停止 |
| `phase.started` | 环节开始 |
| `phase.completed` | 环节完成 |
| `phase.skipped` | 环节跳过 |
| `phase.rolled_back` | 环节回退 |
| `speaker.activated` | 指定当前发言人 |
| `speech.started` | 发言开始 |
| `speech.ended` | 发言结束 |
| `speech.timeout` | 发言超时 |
| `clock.started` | 时钟开始 |
| `clock.paused` | 时钟暂停 |
| `clock.resumed` | 时钟继续 |
| `clock.adjusted` | 时钟校准 |
| `clock.expiring_soon` | 时钟即将到时 |
| `clock.expired` | 时钟到时 |
| `asr.stream_started` | ASR 实时流式会话启动 |
| `asr.audio_chunk_received` | ASR 收到 PCM 音频分片 |
| `asr.partial` | ASR partial 文本 |
| `asr.final` | ASR final 文本 |
| `asr.failed` | ASR 失败 |
| `agent.connected` | Agent 健康检查成功 |
| `agent.task.created` | AI 任务下发 |
| `agent.speech.delta` | AI 流式文本增量 |
| `agent.speech.final` | AI 文本完成 |
| `agent.failed` | Agent 失败 |
| `agent.interrupted` | Agent 被中断 |
| `tts.started` | TTS 开始播报 |
| `tts.synthesis_started` | 正式 AI 发言 TTS 合成开始 |
| `tts.audio_archived` | 正式 AI 发言 TTS 音频已归档 |
| `tts.finished` | TTS 播报完成 |
| `tts.failed` | TTS 失败 |
| `screen.scene_changed` | 大屏场景切换 |
| `vote.window_opened` | 学生投票开启 |
| `vote.window_closed` | 学生投票关闭 |
| `vote.submitted` | 投票提交 |
| `vote.published` | 投票结果公布 |
| `audit.logged` | 管理操作审计写入 |

## 6. 命名与时间约定

- API、数据库、事件 payload 使用 `snake_case`。
- TypeScript 类型使用 `PascalCase`，字段保持后端 `snake_case`，前端不做隐式重命名。
- 所有持久化时间戳使用 UTC ISO8601 字符串；实时同步同时提供 `server_time_ms`。
- 后端 ID 使用带前缀的字符串，便于现场排查：`match_`、`team_`、`spk_`、`phase_`、`speech_`、`task_`、`evt_`。
- 所有主持人干预操作必须产生 `Event` 和 `AuditLog`。
