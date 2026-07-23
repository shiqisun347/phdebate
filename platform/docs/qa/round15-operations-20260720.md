# Round 15 运维可靠性审计与改进

日期：2026-07-20  
范围：数据库备份可见性、磁盘容量门禁、Web 发布回滚、发布来源追踪，以及“系统只有唯一版本”的无损命名迁移建议。  
明确排除：未修改 TTS、ASR、LiveKit、AudioWorklet、PCM、浏览器播放队列或任何可靠音频基线文件；未连接、部署或修改生产服务器。

## 结论

本轮关闭了 Round 14 遗留的四项运维风险：

1. 平台 原先只能看到主数据库最近一次成功备份，Debate Agent 备份失败完全不可见。
2. 磁盘只在剩余 5% 时才使 readiness 失败，没有提前告警区间；这个阈值不足以给音频、归档和数据库备份留出处置时间。
3. Web 只有构建脚本，发布仍依赖人工修改 `.web-current` 并重启，缺少候选健康门禁和自动回滚。
4. API/Web release 只有人为名称，无法证明构建对应哪个 Git commit 和 tree。

现在的行为是：

- 主数据库和 Agent 数据库各自发布原子状态文件；状态区分 `running`、`succeeded`、`failed`。
- 失败状态会保留上一次成功时间、文件大小和 SHA-256，不会把失败覆盖成“没有记录”，也不会记录数据库密码、连接串或命令输出。
- 最近一次执行失败但上次成功仍在 36 小时内时，管理端显示“预警”，API readiness 仍为 200；超过最大新鲜度后才返回 503。这样既能让管理员看见备份故障，也不会因一次短暂备份故障摘除所有线上 API。
- 磁盘默认在剩余 20% 时预警，在剩余 10% 时 readiness 失败。可通过 `HEALTH_DISK_WARNING_FREE_PERCENT` 和 `HEALTH_DISK_FAILURE_FREE_PERCENT` 配置；预警阈值不会被解析为低于失败阈值。
- Web 发布使用文件锁防止并发部署，校验候选完整标记、来源记录和当前同步源码，采用原子软链接替换，依次验证直连 Node 和公网 Nginx。任一门禁失败都会恢复旧链接、重启并验证旧版本。
- 源码同步记录 `runtime/source-commit` 和 `runtime/source-tree`。API/Web 构建把两者写入 `.release-provenance.json`；Web 同时提供非敏感 `/release.json`，API `/api/health` 返回只允许列出的 release、commit、tree 和构建时间。
- `verify-release-provenance.py` 会在 Git checkout 中证明 tree 确实属于指定 commit；伪造、缺字段、错误 service/release 或未知对象都失败关闭。

## 备份状态契约

状态文件不包含目录绝对路径、数据库名、用户名、密码或 stderr。成功示例：

```json
{
  "schema_version": 1,
  "state": "succeeded",
  "last_attempt_at": "2026-07-20T12:00:00+00:00",
  "last_success_at": "2026-07-20T12:00:00+00:00",
  "file": "agent-20260720T120000Z.dump",
  "bytes": 123456,
  "sha256": "...",
  "retention_days": 14
}
```

失败只增加固定、低敏感度的 `error_code=backup_command_failed`。Agent 备份循环成功后等待 24 小时，失败后默认 5 分钟重试，而不是快速崩溃直至 Supervisor 进入 FATAL。Agent 备份也新增互斥锁和 `pg_restore -l` 结构检查，避免并发手工备份和不可读取的 dump 被标记为成功。

平台 健康接口默认读取：

- 主数据库：`BACKUP_STATUS_FILE`。
- Agent 数据库：`AGENT_BACKUP_STATUS_FILE`，默认兼容当前服务器的 `/home/ubuntu/sunsq/debate-agent/runtime/backup-status.json`。

## 安全 Web 发布操作

在目录扁平化完成后，应优先使用无版本名称变量：

```bash
export PHDEBATE_ROOT=/home/ubuntu/sunsq/phdebate
export PHDEBATE_WEB_SERVICE=phdebate-web
export PHDEBATE_PUBLIC_ORIGIN=https://117.50.192.216

PHDEBATE_RELEASE=round15-20260720-root \
  "$PHDEBATE_ROOT/deploy/build-web-release.sh"

"$PHDEBATE_ROOT/deploy/activate-web-release.sh" round15-20260720-root
```

构建脚本的旧变量名称由主线扁平化任务统一迁移；新的激活脚本已优先读取 `PHDEBATE_ROOT`、`PHDEBATE_WEB_SERVICE`、`PHDEBATE_WEB_DIRECT_ORIGIN`、`PHDEBATE_PUBLIC_ORIGIN` 和 `PHDEBATE_WEB_HEALTH_RETRIES`，仅为当前生产过渡兼容旧变量和旧 Supervisor 名称。

发布前必须存在：

- 当前完整 rollback release；
- `runtime/source-commit` 与 `runtime/source-tree`；
- 候选 `.release-complete`、`.release-provenance.json`、`public/release.json` 和 `server.js`；
- 候选 provenance 与当前同步源码完全一致。

脚本不会删除旧 release 或恢复集。清理仍由独立、保守的 prune 工具完成。

## 唯一版本命名的无损迁移建议

目标是产品、目录、进程和文档只呈现一个 `phdebate`，但不能把“去掉 v2 字样”变成一次同时改目录、数据库、Cookie、Supervisor 和 Nginx 的高风险切换。

### 1. 先稳定身份和数据，不改数据库内容

- 先做完整恢复集并验证主库、Agent 库、数据卷和可靠音频指纹。
- PostgreSQL 物理数据库当前名即使含 `_v2`，也只是内部存储名称，不代表仍有两个产品版本。第一阶段保留数据库名和数据目录，避免无收益的停机改名。
- 若最终必须改为 `phdebate`：先 `pg_dump -Fc`，恢复到新数据库，核对 Alembic revision、表数、关键业务行数和抽样 SHA，再只切 `DATABASE_URL`；旧库只读保留一个观察周期。不要直接覆盖或删除旧库。

### 2. Cookie 已具备平滑迁移基础

- 当前正式 Cookie 已是 `jixia_session` / `jixia_csrf`，旧 `jixia_v2_*` 仅作为迁移读取兼容，并在刷新会话时删除。
- 扁平化时不要再次改 Cookie 名，否则会让所有学生在比赛中掉线或重新登录。
- 至少保留旧 Cookie 读取兼容一个最长 session 周期；确认访问日志中旧 Cookie 已消失后再移除兼容代码。

### 3. 文件系统先建立 canonical root，再切进程

- 准备 `/home/ubuntu/sunsq/phdebate`，同步代码并让 `runtime`、`storage`、可靠音频和模型数据仍指向同一份权威数据，禁止复制后形成两套可写数据。
- 在 canonical root 构建新的 API/Web immutable releases，并验证 provenance。
- 旧 `/home/ubuntu/sunsq/phdebate` 在观察期只作为兼容路径或只读回滚入口；不得让两个 engine/worker 同时消费同一 Redis/数据库。

### 4. Supervisor 按角色逐个切换

- 新名称建议：`phdebate-postgres`、`phdebate-redis`、`phdebate-api`、`phdebate-api-secondary`、`phdebate-engine`、`phdebate-worker`、`phdebate-web`、`phdebate-backup`。
- API 可以按 secondary → primary 滚动切换；每个实例必须核对 `/api/health` 的 instance 和 provenance。
- Engine、Worker、Backup 不能新旧同时运行。停止旧进程、确认没有残留 PID/租约，再启动新名称。
- PostgreSQL/Redis 若仍使用原数据目录和端口，只做 Supervisor program 名称变更：先停旧 program，再启动新 program，严禁两个 postmaster/redis-server 指向同一数据目录。
- Web 使用本轮的原子激活脚本；Nginx upstream 在 API 双实例健康后再从内部 `jixia_api` 名称整理为 `phdebate_api`。内部 upstream 改名与用户 URL 无关，应单独验证 `nginx -t` 后 reload。

### 5. 最后清除兼容名称

- 连续观察至少一个完整比赛周期，确认 API/Web provenance、备份状态、Engine 心跳、Worker 死信、WebSocket 和恢复演练均正常。
- 再删除旧 Supervisor 配置、旧环境变量别名、旧目录兼容链接和文档中的“平台”。
- 每次只删除一种兼容层，并保留可直接切回的 immutable release 和数据库备份；不要在同一次发布中删除旧 Cookie、旧 DB 和旧 runtime。

## 验证证据

本地验证结果：

- `deploy/tests`、新增健康测试及 readiness 回归：47 passed。
- 运维定向测试：21 passed，覆盖备份失败保留成功证据、release tree 篡改拒绝、Web 成功切换、公网门禁失败自动回滚、源码 tree 记录和构建失败关闭。
- 管理端组件：3 passed。
- TypeScript：`npx tsc --noEmit` 通过。
- ESLint：0 errors；13 个既有 warning 全部位于冻结音频组件测试、冻结音频组件和 PostCSS，本轮未修改。
- Ruff：通过。
- 所有相关 Shell：`bash -n` 通过。

生产部署和生产备份不在此 lane 执行，由主线在合并全部 Round 15 改动后统一完成。
