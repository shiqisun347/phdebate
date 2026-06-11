# 人机辩论赛控制系统 · 开发文档

本目录是面向实现的**开发规格（development spec）**，把上层需求与原型落地为可直接编码的技术契约。

## 文档与需求的关系

- 上一级 [`../README.md`](../README.md) 是**需求基线**（做什么、为什么）。
- `prototype/` 是**视觉与交互原型**（长什么样）。
- 本目录 `docs/` 是**实现规格**（怎么做）。

**冲突处理**：当本目录与需求文档表述不一致时，以本目录为准（因为接口/字段在这里定稿），并在对应章节回链需求条目说明差异原因。需求级别的范围变更仍需先改 `../README.md`。

## 阅读顺序

### 所有人先读
1. [00-overview.md](00-overview.md) — 技术栈、术语表、角色权限矩阵
2. [01-architecture.md](01-architecture.md) — 整体架构、目录结构、数据流

### 后端工程师
3. [02-data-model.md](02-data-model.md) — 数据库
4. [03-state-and-timer.md](03-state-and-timer.md) — 状态机与计时器
5. [04-rest-api.md](04-rest-api.md) — REST 接口
6. [05-realtime-protocol.md](05-realtime-protocol.md) — WebSocket 协议
7. [06-agent-gateway.md](06-agent-gateway.md) — Agent 接入
8. [07-speech-pipeline.md](07-speech-pipeline.md) — 讯飞语音

### 前端工程师
- [01-architecture.md](01-architecture.md) → [05-realtime-protocol.md](05-realtime-protocol.md) → [04-rest-api.md](04-rest-api.md) → [08-frontend.md](08-frontend.md) → [09-voting.md](09-voting.md)

### 联调 / 测试 / 现场
- [06-agent-gateway.md](06-agent-gateway.md)（mock agent）→ [10-security-and-deploy.md](10-security-and-deploy.md) → [11-testing-acceptance.md](11-testing-acceptance.md) → [12-milestones.md](12-milestones.md)

## 文档清单

| 文档 | 内容 |
| --- | --- |
| [00-overview.md](00-overview.md) | 项目概述、技术栈定稿、术语表、角色权限矩阵 |
| [01-architecture.md](01-architecture.md) | 系统架构、九模块职责、monorepo 目录、三条数据流时序 |
| [02-data-model.md](02-data-model.md) | SQLite DDL、实体关系、事件溯源约定 |
| [03-state-and-timer.md](03-state-and-timer.md) | 比赛/环节状态机、多时钟模型、deadline 同步 |
| [04-rest-api.md](04-rest-api.md) | REST 端点完整规格（请求/响应/错误码/鉴权） |
| [05-realtime-protocol.md](05-realtime-protocol.md) | WebSocket 频道、消息信封、seq+快照续传 |
| [06-agent-gateway.md](06-agent-gateway.md) | Agent 标准接口、时序/延迟策略、mock agent 契约 |
| [07-speech-pipeline.md](07-speech-pipeline.md) | 讯飞 ASR/TTS、音频归档、降级路径 |
| [08-frontend.md](08-frontend.md) | 三入口前端规格、原型映射、状态渲染 |
| [09-voting.md](09-voting.md) | 评委/学生投票、公布时序 |
| [10-security-and-deploy.md](10-security-and-deploy.md) | 权限、紧急停止、部署、讯飞云依赖 |
| [11-testing-acceptance.md](11-testing-acceptance.md) | 分模块验收、测试策略、现场演练 |
| [12-milestones.md](12-milestones.md) | 开发阶段、里程碑、MVP 边界 |

## 需求 → 文档对照表（可追溯性）

| 需求文档章节 | 实现规格落点 |
| --- | --- |
| §2 建设目标 | 00、01 |
| §4 用户角色 | 00（角色权限矩阵） |
| §5.2 比赛流程 | 03（环节状态机）、02（phases 表种子数据） |
| §5.3 自由辩论规则 | 03（多时钟 + 调度三机制） |
| §5.3.1 发言调度 | 03、08（辩手端 teammate_control） |
| §5.4 评委与投票 | 09、02（votes/audience_votes） |
| §6.1 大屏展示页 | 08（scene/mode）、对接 `prototype/screen-claude.html` |
| §6.2 辩手控制台 | 08（state）、对接 `prototype/console.html` |
| §6.3 学生投票页 | 09、08 |
| §6.4 管理页面 | 08（三 tab）、对接 `prototype/admin.html` |
| §7.1 状态机 | 03 |
| §7.2 计时器 | 03（多时钟 + deadline 同步） |
| §7.3 实时事件总线 | 05（事件清单 + 续传） |
| §7.4 上下文管理 | 06（Agent 输入上下文）、02（transcript） |
| §7.5 语音链路 | 07 |
| §7.6 Agent 接口 | 06 |
| §7.7 AI 时序策略 | 06 |
| §8 系统架构 | 01 |
| §9 数据模型 | 02 |
| §10 API 草案 | 04（REST）、05（WS） |
| §11 权限与安全 | 10 |
| §12 MVP 范围 | 12（里程碑）、各文档 MVP 标注 |
| §13 验收标准 | 11 |
| §15 实施建议 | 12 |

## 文档约定

- **MVP 标注**：与 MVP 直接相关的能力用 `【MVP】` 标注；延后能力用 `【延后】` 标注（只保留接口位）。
- **稳定标识**：实体/字段/事件/端点名一旦在本目录定义，即为契约，跨文档引用时保持同名。
- 所有时间戳为 UTC ISO8601 或毫秒级 Unix 时间戳（各接口明确标注）；展示层时区在前端处理。
