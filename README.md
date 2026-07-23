# 稷下辩论平台

面向学生与赛事组织者的多人、人机自动辩论平台。仓库只保留一套正式平台源码，位于
`platform/`；`debate-agent/` 是独立部署、通过 REST/SSE 为比赛提供辩手能力的 Agent 服务。

## 项目结构

- `platform/apps/api`：FastAPI、PostgreSQL、房间与比赛接口
- `platform/apps/web`：Next.js 赛事大厅、房间、观战、结果和管理后台
- `platform/apps/engine`：服务端权威自动比赛状态机
- `platform/apps/worker`：异步 Agent、裁判和归档任务
- `platform/services/moss-realtime-gateway`：MOSS 实时语音网关
- `platform/deploy`：Supervisor、Nginx、备份、发布和恢复工具
- `debate-agent`：独立 Debate Agent 服务与管理页面

## 生产约束

- 正式网站只使用根路径，不提供版本选择器或任何版本化平行入口。
- TTS 使用已经验证的 MOSS-Realtime 链路；可靠语音与浏览器播放文件受冻结指纹保护。
- 房间状态按六位房间号隔离；API 使用双实例，比赛引擎只有一个权威实例。
- 辩手 Agent 使用 RESTful POST/SSE，Thinking 内容不得进入发言和语音。
- 不使用 Qwen3 8B，不使用 6016 部署链路。
- 密码、API Key、数据库、学生音频、模型权重和生产 `.env` 不得提交到 GitHub。

开发、测试和部署见 [平台 README](platform/README.md)；完整恢复流程见
[服务器恢复手册](platform/docs/deploy/full-server-restore.md)。
