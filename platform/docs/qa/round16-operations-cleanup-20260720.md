# Round 16 运维、恢复与唯一版本清理报告

日期：2026-07-20
范围：`platform/deploy`、恢复材料、release、日志和唯一版本残留。
冻结范围：未修改 `deploy/openmoss`、`run-moss-production.sh`、MOSS Gateway、TTS、ASR 或浏览器播放代码。

## 结论

- 生产根目录只有 `/home/ubuntu/sunsq/phdebate`，旧 `/home/ubuntu/sunsq/phdebate-v2` 不存在。
- 磁盘剩余 `17.36%`（约 14.65GB），属于预警而非停机状态。
- 当前主要容量占用不是业务日志，而是恢复材料：`runtime/deploy-backups` 约 16.50GB。
- 当前可靠恢复批次没有删除或改写；其六类材料已重新执行 SHA-256 验证。
- 恢复集清理此前只验证 manifest 字段格式，不能证明其引用文件仍然存在且未损坏。现已改为在删除任何候选前，统一验证所有恢复集的文件唯一性、大小与 SHA-256；任一异常都会失败关闭。
- 21 份旧源码压缩包占约 748.7MB。它们不再是代码回滚依据，但本轮只提供安全清理工具和预览，没有直接删除。
- 平台和 Nginx 的非语音日志现在都有明确的大小及历史份数上限；Nginx 日志从旧 `debateall` 路径归一到唯一平台运行目录。

## 生产只读审计

`audit-storage.py` 在生产服务器得到：

| 项目 | 数量/占用 |
| --- | ---: |
| 根磁盘 | 84.42GB，总剩余 14.65GB（17.36%） |
| deploy 恢复目录 | 16.50GB |
| 数据卷恢复包 | 10 份，3.94GB |
| 恢复 manifest | 11 份 |
| 历史源码归档 | 21 份，748.7MB |
| API release | 7 份，约 22.5MB 已分配空间 |
| Web release | 7 份，约 411.3MB 已分配空间 |
| 平台日志 | 20 个，约 12.7MB |
| 大于 100MB 的平台日志 | 0 |
| 旧 `phdebate-v2` 根目录 | 不存在 |

11GB 左右的 MOSS 离线包是完整迁移的重要材料，本轮明确保留。日志当前不是空间风险，但若不设上限，公开观战和 WebSocket 重连会让 Nginx access log 长期增长，因此提前补齐轮转策略。

以 24 小时作为仅供评估的预览条件，`prune-source-archives.sh dry-run` 找到 20 份候选，理论可回收 `744,266,598` 字节；最新一份仍作为额外回滚底线保留。默认策略实际使用 72 小时，因此当前输出为 0 个候选，不会过早清理。

## 已实现改进

### 1. 恢复索引完整性验证

- 新增 `verify-recovery-index.py`，一次验证多份 schema 2/3 manifest。
- 同一个 MOSS/可靠语音等共享大文件只计算一次 SHA-256，避免 11 份 manifest 重复读取约 11GB 文件。
- 验证以下条件：
  - manifest 字段不可重复、schema 受支持；
  - 文件名安全；
  - 每个角色的文件在允许目录中恰好出现一次；
  - 文件大小和 SHA-256 与 manifest 一致。
- `prune-recovery-sets.sh` 只有在整个索引验证成功后才会计算删除候选。任一损坏、缺失、重名或未知清单都会保留全部材料。

### 2. 旧源码归档安全清理

- 新增 `prune-source-archives.sh`，默认 `dry-run`。
- `apply` 必须额外设置 `PHDEBATE_ALLOW_SOURCE_ARCHIVE_PRUNE=yes`，避免误操作。
- 默认保留最新一份并要求至少 72 小时；manifest 引用的文件永久跳过。
- 匹配范围仅限历史源码 tar.gz，不匹配数据库、数据卷、私密配置、可靠语音或 MOSS 离线包。
- 输出候选数量和可回收字节，便于管理员在操作前做容量决策。

### 3. 磁盘与唯一版本审计

- 新增只读 `audit-storage.py`，输出机器可读 JSON。
- 同时报告逻辑大小与实际分配空间，避免稀疏文件或硬链接造成误判。
- 覆盖磁盘状态、平台/Agent 数据库备份、数据卷、源码归档、release、日志以及旧并行根目录。
- `--fail-on-critical` 可用于自动化门禁：磁盘进入临界状态或重新出现 `phdebate-v2` 根目录时返回失败。

### 4. 日志增长约束

- PostgreSQL、Redis、API 双实例、Engine、Worker、Web、Backup 和 Nginx Supervisor 日志均设置 `maxbytes` 与 3 份历史。
- 新增 `phdebate-nginx.logrotate.conf`：Nginx access/error log 每日检查、最大 100MB、保留 14 份并压缩，通过 USR1 安全重新打开日志。
- Nginx 日志路径统一为 `/home/ubuntu/sunsq/phdebate/runtime/logs`，不再依赖历史 `debateall` 路径。
- 没有使用覆盖整个日志目录的通配规则，因此不会触碰独立语音运行时日志策略。

### 5. 唯一版本公开说明

- `platform/README.md` 不再公开旧 `V2_*` 管理员变量名称，管理员凭据统一描述为服务器私密配置。
- 代码中保留的 `v2_database` 只用于读取 schema 2 历史恢复清单；少量 `v2_admin_*` 是冻结可靠基线内部兼容字段，不代表并行部署或公开产品版本。

## 验证结果

```text
platform/deploy/tests: 55 passed
新增/相关聚焦测试: 23 passed
Ruff platform/deploy（排除冻结 openmoss）: passed
bash -n platform/deploy/*.sh: passed
可靠音频基线: 82 files verified
fingerprint: 3219138b22878e766c94b9a1fa0e422211a6f27dd74afbbb93a8e45f7e295285
```

测试覆盖了恢复文件损坏时全量失败关闭、共享大文件只哈希一次、源码归档二次确认、manifest 引用保护、日志上限、唯一日志目录和只读磁盘审计。

## 上线操作顺序

1. 同步代码，但不运行任何 `apply` 清理。
2. 安装 Supervisor/Nginx 配置前执行 `nginx -t` 和 `supervisorctl reread`。
3. 安装 `/etc/logrotate.d/phdebate-nginx`，先执行 `logrotate -d`。
4. 重载服务后检查 readiness、当前恢复 manifest 和可靠音频基线。
5. 只运行 `audit-storage.py`、`prune-releases.sh dry-run`、`prune-recovery-sets.sh dry-run`、`prune-source-archives.sh dry-run`。
6. 等当前恢复批次在独立临时库/目录再次完成恢复演练，并达到保留期限后，再由管理员审阅输出决定是否执行 `apply`。

本轮没有删除生产服务器上的任何可靠恢复材料，也没有调整声音生成或浏览器播放链路。
