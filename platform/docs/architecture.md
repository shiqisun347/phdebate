# 稷下辩论平台架构

- PostgreSQL 保存用户、赛事、房间、追加式事件、发言、比赛结果和排行榜等权威业务数据。
- Redis 承载房间级实时消息、在线状态、控制租约、分布式锁和 Dramatiq 任务。
- FastAPI 负责认证和全部权限判定；匿名观众只能获得脱敏的公开房间投影。
- 独立 Match Engine 是比赛状态机的唯一权威执行者。创建房间时固定自动流程版本，后续规则修改不会影响进行中或历史比赛。
- Worker 执行归档、Agent、裁判和非交互式重试任务。
- Next.js 提供赛事大厅、房间大厅、比赛、观战、结果、个人中心和系统管理后台；比赛与观战复用同一个舞台组件。
- MOSS-Realtime、FunASR 和 LiveKit 组成实时语音链路，并受可靠音频基线保护。
- 生产网站只服务根路径 `/`，API、媒体和 WebSocket 分别使用 `/api`、`/media` 和 `/ws`；不存在版本选择器或平行版本入口。
- 独立 Debate Agent 通过 `/debate/api/debate` 和 `/debate/api/judge` 提供 REST/SSE 服务，不与主平台共享管理员会话或敏感配置。
