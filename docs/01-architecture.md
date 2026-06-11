# 01 · Architecture

本章定义 MVP 的系统架构、代码组织、模块职责和核心数据流。实现时应保持“后端权威状态 + 前端只读渲染/受控命令”的原则。

## 1. 总体架构

```text
┌──────────────────────────────────────────────────────────┐
│                     现场局域网单机服务                    │
│                                                          │
│  React static files                                      │
│  /screen  /console/:speakerId  /admin  /vote/:matchId    │
│                                                          │
│  FastAPI                                                 │
│  REST API · WebSocket · SSE client · ASR/TTS adapters    │
│                                                          │
│  SQLite · audio archive · event log                      │
└───────────────┬───────────────────────┬──────────────────┘
                │                       │
        Human speaker laptops       AI Agent services
        browser + microphone        HTTP / SSE endpoints
```

【MVP】所有页面和 API 由同一台机器提供，现场设备通过局域网访问。Agent 可以运行在同机或同网段其他机器，只要求后端能访问其 `agent_endpoint`。

## 2. 推荐目录结构

```text
phdebate/
  apps/
    backend/
      app/
        main.py
        api/
        core/
        models/
        services/
        realtime/
        integrations/
        tests/
      migrations/
      storage/
        audio/
        exports/
    frontend/
      src/
        routes/
          screen/
          console/
          admin/
          vote/
        components/
        api/
        realtime/
        state/
        types/
      public/
  docs/
  prototype/
  references/
```

如果初期不建 monorepo，也应保留上述模块边界，避免把状态机、计时器、WebSocket 广播、Agent 调用写进路由函数。

## 3. 后端模块

| 模块 | 责任 | 不负责 |
| --- | --- | --- |
| Match Service | 比赛配置 CRUD、队伍/辩手/环节初始化 | 不推进流程 |
| Flow Engine | 状态机、环节推进、发言合法性校验、自由辩论调度 | 不直接访问前端 |
| Timer Service | 权威时钟、deadline 计算、暂停/继续/校准/到时事件 | 不做页面倒计时动画 |
| Event Service | 分配 `seq`、持久化事件、广播快照和增量 | 不执行业务规则 |
| Transcript Service | ASR/AI/manual 文本归档、修订、有效性标记 | 不调用 Agent |
| Agent Gateway | 健康检查、发言任务下发、SSE 消费、中断、重试 | 不保存最终裁判结果 |
| Speech Service | 讯飞 ASR/TTS 适配、音频归档、降级 | 不决定发言人 |
| Vote Service | 评委票、学生票、防重复 token、公布顺序 | 不控制比赛环节 |
| Admin Service | 权限、口令、审计、紧急停止 | 不渲染 UI |

## 4. 前端入口

| 路径 | 用户 | 原型 | 实时频道 | 交互 |
| --- | --- | --- | --- | --- |
| `/screen` | 观众/投屏 | `prototype/screen-claude.html` | screen/state/events | 只读；由管理端切场景 |
| `/console/:speakerId` | 人类辩手 | `prototype/console.html` | speaker/state/events | 开始/结束本人发言、自由辩论请求 AI 队友 |
| `/admin` | 主持人/管理员 | `prototype/admin.html` | admin/state/events | 全量控制、配置、投票、干预 |
| `/vote/:matchId` | 学生观众 | 无原型，轻量移动页 | REST 轮询或短连接 | 提交优胜方和最佳辩手 |

前端所有页面启动后先拉取 REST 快照，再建立 WebSocket。WebSocket 重连时携带本地最后收到的 `seq`。

## 5. 状态来源

后端是唯一权威来源：

- `Match.status` 决定比赛是否可控制。
- `Phase.status` 决定当前环节和可用操作。
- `Clock.deadline_at` 和 `server_time_ms` 决定倒计时显示。
- `Event.seq` 决定客户端恢复顺序。
- `ScreenScene` 和 `LiveMode` 决定大屏布局。

前端可以本地平滑倒计时，但不得把本地倒计时结果回写为权威剩余时间。

## 6. 核心数据流

### 6.1 主持人推进环节

```text
Admin UI
  POST /api/matches/{match_id}/phases/{phase_id}/start
FastAPI route
  Admin Service 校验权限并写审计
Flow Engine 校验状态迁移
Timer Service 初始化时钟
Event Service 写 phase.started / clock.started
WebSocket 广播 snapshot_patch + event
Screen / Console / Admin 更新渲染
```

### 6.2 人类辩手发言

```text
Console UI 点击开始发言
  POST /api/matches/{match_id}/speakers/{speaker_id}/start-speaking
Flow Engine 校验当前发言人
Timer Service 启动对应 clock
Speech Service 建立 ASR 会话
Event Service 广播 speech.started
Console 采集麦克风并上传音频流
ASR partial -> asr.partial -> 大屏字幕
ASR final -> TranscriptSegment + Speech.content_final
Console 点击结束或时钟到时
  speech.ended / clock.paused 或 phase.completed
```

### 6.3 AI 辩手发言

```text
Flow Engine 激活 AI speaker
Agent Gateway 创建 AgentTask
POST {agent_endpoint}/speech
Agent SSE delta -> agent.speech.delta
Transcript Service 追加 AI 文本片段
Speech Service 按句送 TTS
TTS 首字播出 -> Timer Service 开始计时
TTS 完成或到时策略触发 -> speech.ended
异常 -> agent.failed / tts.failed -> 主持人干预
```

### 6.4 投票与结果

```text
Admin 开启学生投票
Vote Service 创建窗口和二维码 URL
Screen intermission 展示 /vote/:matchId
Audience 提交 vote_token + winner + best_speaker
Admin 录入评委票
Admin 先公布评委结果
Admin 再公布学生结果
Screen result 场景展示最终结果
```

## 7. 运行端口建议

| 环境 | 前端 | 后端 | 备注 |
| --- | --- | --- | --- |
| 开发 | Vite `5174` | FastAPI `8000` | Vite 代理 `/api` 和 `/ws` |
| 现场 | FastAPI `8000` | FastAPI `8000` | 后端托管打包后的静态文件 |

现场访问示例：

- 大屏：`http://<host-ip>:8000/screen?match_id=match_001`
- 管理端：`http://<host-ip>:8000/admin`
- 辩手端：`http://<host-ip>:8000/console/spk_aff_3`
- 投票页：`http://<host-ip>:8000/vote/match_001?token=...`

## 8. 延后扩展位

【延后】以下能力保留接口空间但不在 MVP 实现完整逻辑：

- 评委独立在线投票端：复用 Vote Service，增加 `judge` 身份认证。
- 多场比赛/赛事排程：在 `matches` 上增加赛事归属，不改变单场状态机。
- 自由辩论 `ai_auto`：复用 `speaker.activated` 与 AgentTask，只增加无人按麦超时触发器。
- 投机预生成：复用 AgentTask，增加 `task_type = speculative`，未确认时不写入正式 Speech。
