# Round 15：唯一版本结构与运行时迁移

## 结论

仓库和生产部署不再维护两套辩论平台。当前正式系统是唯一平台版本，公开页面只使用根路径，源码目录统一为 `platform/`，独立 Debate Agent 继续位于 `debate-agent/`。

## 本轮调整

- 将正式平台源码目录从版本化名称改为 `platform/`。
- 删除旧的平行前端位置模板、旧迁移打包脚本和已经失效的双版本迁移说明。
- Web 构建只允许根路径模式；发布仍使用不可变 release、健康门禁和原子回滚，不再通过第二套 URL 做灰度。
- Supervisor 程序统一为 `jixia-api`、`jixia-api-secondary`、`jixia-engine`、`jixia-worker`、`jixia-web`、`jixia-backup`、`jixia-postgres` 和 `jixia-redis`。
- 新生产根目录为 `/home/ubuntu/sunsq/phdebate`，数据库和角色统一为 `phdebate`。
- 新登录 Cookie 使用 `jixia_session` 与 `jixia_csrf`。已有浏览器的旧 Cookie 会在第一次读取 session 时无感升级，然后删除旧 Cookie；不会创建第二套用户或比赛数据。
- 新恢复清单使用 schema 3 和 `platform_database` 角色；校验与安全清理工具仍能读取已有 schema 2 清单，避免历史恢复材料失效。
- 管理端新增赛后数据异常处置记录，可把缺失逐字稿、录音或分段明确标记为待补采、不可恢复或已人工核验，同时保留原始数据事实和审计日志。

## 可靠语音边界

本轮没有修改 DebateStage、LiveKit、AudioWorklet、PCM 播放、MOSS 推理、TTS/ASR Provider 或浏览器音频调度。目录调整后重新执行可靠音频清单验证，82 个受保护文件和指纹必须保持不变。

少数受保护文件内部仍包含历史配置字段或协议注释中的旧名称。它们不是公开入口或平行运行版本；在没有独立语音变更窗口和完整音频回归前，不为了文本清理而改写这些文件。

## 验证门禁

- API 全量测试、Alembic head 和 Ruff。
- Web 全量组件测试、TypeScript、ESLint 和 Next.js 生产构建。
- 部署、恢复、备份、release provenance、Web 自动回滚及 shell 语法测试。
- 20 房间混合 1v1/4v4 soak，覆盖暂停恢复、真人掉线 AI 接替、引擎重启和裁判乱序。
- 新旧 Cookie 平滑升级测试。
- Nginx 配置必须不存在版本化页面、API、媒体或 WebSocket location。
- 生产切换前后均确认没有活跃比赛，并生成同批次数据库、数据卷和恢复清单。

## 回滚原则

应用回滚只切换不可变 API/Web release；数据库迁移不会覆盖旧备份。生产目录和数据库改名完成后，不重新建立旧名称软链接或第二套服务，以免再次形成双版本状态。若新 release 失败，仍在同一正式根目录内切回上一 release。
