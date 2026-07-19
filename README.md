# 稷下辩论平台

面向学生与赛事组织者的多人、人机自动辩论平台。当前生产实现位于 `v2/`，独立辩手
Agent 管理平台位于 `debate-agent/`。

## 项目结构

- `v2/apps/api`：FastAPI、PostgreSQL、房间与比赛接口
- `v2/apps/web`：Next.js 赛事大厅、房间、观战、结果和管理后台
- `v2/apps/engine`：自动比赛状态机
- `v2/apps/worker`：异步 Agent、裁判和归档任务
- `v2/services/moss-realtime-gateway`：MOSS 实时语音网关
- `v2/deploy`：Supervisor、Nginx、备份、发布和恢复工具
- `debate-agent`：独立 Debate Agent REST/SSE 服务与管理页面

## 生产约束

- TTS 只使用已经验证的 MOSS-Realtime 路径；可靠语音与浏览器播放文件有冻结指纹门禁。
- 房间状态按六位房间号隔离；API 使用双 Worker，比赛引擎只运行一个权威实例。
- 辩手 Agent 使用 RESTful POST/SSE，Thinking 内容不得进入发言和语音。
- 不使用 Qwen3 8B，不使用 6016 部署链路。
- 密码、API Key、数据库、学生音频、模型权重和生产 `.env` 不得提交到 GitHub。

## 开发与测试

具体启动、测试和部署命令见 [V2 README](v2/README.md) 与
[Debate Agent README](debate-agent/README.md)。

生产迁移和完整恢复见
[新服务器完整恢复手册](v2/docs/deploy/full-server-restore.md)。
