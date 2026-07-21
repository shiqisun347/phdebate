# Round 19：单版本长期维护与恢复清理审计

日期：2026-07-21
范围：非语音运维脚本、恢复清单保留策略、管理员验收凭据命名、README 与本地生成物。
约束：不修改冻结的配置、Provider、Voice Runtime、DebateStage、浏览器音频、MOSS 目录；不操作生产；不提交、不部署。

## 结论

本轮解决了两个长期维护风险：

1. 单房最多 20 人观战后，README 仍指导运维人员发起 500 连接并关闭生产 TLS 校验；现已修正。
2. 早期不完整恢复清单会正确阻止自动删除，但此前只能永久阻塞清理或依赖危险的人工移动；现新增默认 dry-run、只移动不删除、引用保护失败关闭的受审计隔离流程。

旧管理员变量还不能一次性彻底删除。冻结配置仍以 `v2_admin_*` 字段载入 `.env` 并负责首次管理员创建；本轮不允许改该文件。生产验收脚本已先改用无版本的 `PHDEBATE_ADMIN_*`，旧名称只集中保留为过渡 fallback。运行时字段和 `.env.example` 应在未来获得明确语音基线变更许可后，一次完成 AliasChoices/双读迁移、生产环境切换和下一轮删除，不能在本轮制造“示例已改但应用读不到”的假兼容。

## 已实施修改

### 1. 管理员验收变量分阶段改名

新增 `scripts/operator_credentials.py`：

- 新脚本优先读取 `PHDEBATE_ADMIN_ACCOUNT`、`PHDEBATE_ADMIN_PASSWORD`。
- 旧 `V2_ADMIN_*` 只在一个兼容函数中作为 fallback。
- 缺失时返回固定错误，不把账号或密码写入输出。

接入脚本：

- `verify_admin_account_recovery.py`
- `verify_admin_pagination.py`
- `verify_automation_versioning.py`
- `verify_concurrent_review_revision.py`
- `verify_return_after_substitution.py`
- `deploy/verify_presence_recovery.py`（新变量优先，冻结 settings 仅作 fallback）

未修改 `deploy/verify-browser-lobby.sh` 的旧变量，因为该隔离启动脚本依赖冻结 Settings 完成管理员 seed；现在改名会让浏览器门禁失去管理员账号。该保留是已知兼容边界，不是第二套产品。

### 2. 不完整恢复清单受审计隔离

新增 `deploy/quarantine-recovery-manifests.py`：

- 默认 `dry-run`。
- `apply` 必须显式设置 `PHDEBATE_ALLOW_RECOVERY_MANIFEST_QUARANTINE=yes`。
- 只移动原清单到 `runtime/deploy-backups/quarantine/`，不删除清单和任何归档。
- 只有清单仍包含安全的数据卷文件名，且该数据卷真实存在时才允许隔离。
- 无数据卷引用、引用不安全、数据卷缺失或目标冲突时拒绝隔离，原清单继续阻止 prune。
- 为每个隔离清单生成权限 `0600` 的 JSON sidecar，记录原文件名、原清单 SHA-256、隔离时间、原因和必须保护的数据卷。
- 完整但 checksum 损坏的正式清单不会被当成“旧格式”隔离；它仍需要修复或重新生成。

`prune-recovery-sets.sh` 现在：

- 扫描隔离目录并永久保护隔离清单引用的数据卷。
- 隔离目录出现无法安全读取数据卷引用的清单时仍 fail-closed。
- `apply` 新增第二道显式确认：`PHDEBATE_ALLOW_RECOVERY_PRUNE=yes`。
- 仍只删除超出保留底线且通过完整索引校验的旧 manifest 和数据卷，不扩大删除范围。

`prune-source-archives.sh` 同样扫描隔离目录的文件引用，避免隔离清单引用的源码归档被其他保留任务删除。

完整操作顺序已写入 `docs/deploy/full-server-restore.md`：

1. quarantine dry-run；
2. 显式确认后只移动可安全隔离的清单；
3. 再次运行 recovery prune dry-run；
4. 人工核对保留底线与候选文件；
5. 显式确认后执行 prune apply。

### 3. README 容量和 TLS 指引

`README.md` 的 WebSocket 断连风暴示例由 `--clients 500 --insecure` 改为 `--clients 20`。

- 单房测试与服务端 20 人限制一致。
- 生产证书不得关闭 TLS 校验。
- `--insecure` 仅用于临时自签证书隔离环境。
- 跨房 500 连接仍可由 `load_watchers.py` 测试，但必须至少提供 25 个房间；这不是单房默认容量。

## 旧名称、路径与兼容项决策

| 项目 | 决策 | 原因 |
|---|---|---|
| 验收脚本直接读取 `V2_ADMIN_*` | 已改 | 运维入口可安全先迁移到 `PHDEBATE_ADMIN_*` |
| `core/config.py` 的 `v2_admin_*` | 保留 | 属于冻结可靠语音基线；同时承担 `.env` 解析和首次管理员 seed |
| `.env.example` 的 `V2_ADMIN_*` | 暂时保留 | 当前配置只识别这些字段；提前改示例会导致本地管理员不被创建 |
| `verify-browser-lobby.sh` 旧变量 | 暂时保留 | 它验证冻结 seed 兼容路径，改名后测试管理员无法生成 |
| schema 2 的 `v2_database` role | 永久只读兼容 | 旧恢复清单的协议字段，删除会破坏历史恢复验证 |
| `phdebate-v2` 路径检查 | 保留 | 用于发现旧根目录回归，不是生产默认路径 |
| `deploy/openmoss` 的 `PHDEBATE_V2_ROOT` | 未改 | 冻结可靠语音基线；必须随语音基线整体迁移而非局部替换 |
| `/v2` 不存在的测试 | 保留 | 它证明只运行一个公开产品入口 |
| 前端 package `2.0.0` | 保留 | npm 语义版本，不是并行部署或用户可见产品版本 |

推荐后续迁移顺序：

1. 经明确许可修改冻结 Settings，使 `PHDEBATE_ADMIN_*` 为正式字段、旧字段为 AliasChoices fallback。
2. 同一 release 同时切换 `.env.example`、Supervisor 私密环境和浏览器门禁。
3. 观察至少一个备份周期和一次服务器重启。
4. 下一 release 删除旧环境变量 fallback；schema 2 恢复 role 继续保留。

## 无用代码、依赖与测试产物

### 依赖

未删除生产依赖：

- `psycopg` 由 SQLAlchemy URL 动态加载，不能用源码 `import` 次数判断为未使用。
- `click`、`python-dotenv` 是 Uvicorn/Pydantic 启动链依赖。
- `cryptography`、Dramatiq、Redis、HTTPX、WebSockets 和 LiveKit 均有明确运行入口。
- 前端四个生产依赖均被当前应用使用；管理模块已动态拆包。

在没有锁文件级依赖追踪和影子启动证据前，机械删除“没有直接 import”的包风险高于收益。

### 本地生成物

发现 `.DS_Store`、`.next`、`node_modules`、`.pytest_cache`、`.ruff_cache`、`__pycache__` 和 `.pyc`。它们均已被 Git 忽略，不进入 GitHub 源码备份或生产 release。本轮不把清理本机缓存冒充源代码优化，也没有把任何服务器 dump/tar 下载到 Mac。

当前本机 `platform/.venv` 的可执行脚本 shebang 仍指向已删除的旧 `.../v2/.venv/bin/python`，说明该虚拟环境是不可迁移生成物。源码质量门应使用重新创建的 `.quality-venv` 或当前 Python；不能把 `.venv` 纳入恢复包，也不能在新服务器复制旧虚拟环境。

## 测试证据

- Deploy 全量测试：`60 passed`
- 管理员凭据、观战和断连脚本测试：`19 passed`
- Ruff（`deploy`、`scripts`）：通过
- `bash -n`：恢复集与源码归档清理脚本通过
- Python compile：新增和改动的运维脚本通过
- 可靠音频冻结基线：`82 files`，fingerprint `3219138b22878e766c94b9a1fa0e422211a6f27dd74afbbb93a8e45f7e295285`

新增恢复测试覆盖：

- prune apply 没有显式确认时拒绝执行；
- quarantine apply 没有显式确认时拒绝执行；
- 缺少安全数据引用的不完整清单拒绝隔离并继续 fail-closed；
- 可安全隔离的旧清单只移动、生成审计 sidecar；
- 隔离清单引用的数据卷不会被 prune；
- 隔离后其他已校验、超过保留底线的旧恢复集仍可正常清理；
- 隔离清单引用的源码归档仍受保护。

## 风险与边界

- 本轮没有在生产运行 quarantine 或 prune；服务器上的 7 份早期清单和数据未改变。
- 隔离不是删除授权。若 dry-run 对某份清单显示 `quarantine_refused`，必须补建清单或人工建立可验证映射，不能强制移动。
- 当前管理员运行时命名仍有冻结兼容债务，但已缩小到配置、seed 和专门兼容测试；不能声称已经完全消除。
- 恢复清理前后仍必须校验最新 schema 3 清单、数据库实际恢复、数据卷逐文件哈希、readiness 和磁盘审计。
