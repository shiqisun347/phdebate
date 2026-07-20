# Round 14 生产规模与恢复运维审计

日期：2026-07-20
生产：`117.50.192.216`
边界：本轮没有修改或触发 TTS、MOSS 推理、LiveKit、AudioWorklet、PCM 和浏览器音频播放代码；
没有删除任何生产恢复材料，也没有在生产执行清理或重启。生产操作仅包括只读检查、清理预览和
匿名 WebSocket 观战负载。

## 结论

生产双 API、Engine、Worker、PostgreSQL、Redis、MOSS 和 FunASR readiness 在检查前后均为绿色；
500 个匿名观战 WebSocket 在 8 个真实公开房间上实现 100% 建连并同时保持，负载结束后没有新增
API 错误日志。GitHub 恢复提交、服务器源码 checkout、当前 API release 和 Round 13 恢复清单的
绑定关系一致。

本轮发现并修复了一个高风险清理缺陷：旧版 `prune-recovery-sets.sh` 会保留无法解释的 manifest，
却仍可能删除该 manifest 唯一引用的数据卷。现在只要存在任意 unsupported、incomplete 或 unsafe
manifest，整个清理流程立即 fail closed，`apply` 也不会删除任何文件。

当前最需要继续处理的运维风险不是应用 release，而是磁盘余量、备份失败告警和 Web 发布回滚：

- 根文件系统仅余约 `15.55 GB / 18.42%`，readiness 的失败阈值却只有 5%，预警过晚。
- Agent 备份发生过一次 PostgreSQL 启动窗口失败，Supervisor 随后重试成功，但 Agent 健康页没有
  独立暴露最后成功时间和最后失败时间。
- API 有双 Worker 自动回滚脚本；Web 仍依赖人工切换 `.web-current`，没有等价的健康门禁和自动回滚。
- release 本身没有写入不可变源码 commit；当前能通过源码树 diff 和恢复 manifest 证明一致，但后续
  应让构建脚本生成 release provenance，避免只依赖人工传入的 commit。

## 本轮代码改进

### 恢复批次清理 fail closed

改动：

- `deploy/prune-recovery-sets.sh`
- `deploy/tests/test_prune_recovery_sets.py`

清理前现在要求 schema 2 manifest 同时具有六类 artifact 的安全文件名、数字大小和 64 位 SHA-256。
任意旧格式、截断或非法 manifest 都会输出 `reason=unsafe-manifests-present` 并以零候选结束。

新增覆盖：

- 两个满足 rollback floor 的有效批次旁存在一个 schema 1 manifest 时，所有 manifest 和数据卷仍保留。
- 截断的 schema 2 manifest 同样阻止全部清理。
- 共享数据卷、最近批次、rollback floor、dry-run 和 apply 原有行为继续通过。
- macOS 自带 Bash 3.2 与 Linux 所需语法兼容，已通过 Bash 3.2 `bash -n` 和测试。

生产使用新脚本执行 dry-run 后发现 7 个旧 manifest 无法满足完整 schema 2 合同，因此结果正确地为：

```text
recovery_prune_complete mode=dry-run candidates=0 ... reason=unsafe-manifests-present
```

当前只有 Round 12、Round 13 两个完整新批次，尚未达到默认 `KEEP=3`。因此不能执行 apply。正确顺序是：

1. 先生成并完整恢复验证第三个 schema 2 批次；
2. 独立核对 7 个旧 manifest 及其数据卷是否仍有唯一价值；
3. 将旧格式批次迁移或人工隔离，而不是让清理脚本猜测；
4. 再次 dry-run，人工复核候选后才允许 apply。

### 500 WebSocket 负载工具安全化

改动：

- `scripts/load_watchers.py`
- `scripts/tests/test_load_watchers.py`
- `README.md`

原工具会让 500 个客户端同时发起 TLS 握手，且每个客户端从自身建连时起单独计时；这既可能把测试
变成握手突发攻击，也不能证明 500 个连接曾在同一时间保持。现在：

- 默认只允许 20 个并行握手，但握手完成立即释放槽位，已连接 socket 继续保持。
- 所有客户端完成尝试后才开始统一保持时间，报告新增 `peak_connected`。
- 远程目标必须显式传入 `--allow-public-load`，避免误把生产当本地测试。
- 客户端数、握手并发、时长和启动超时均有硬上限。
- 压测前逐房调用只读公开接口；任意房间不可匿名观战时，在打开 WebSocket 前失败。
- README 不再建议生产使用 `--insecure`；只有隔离的自签证书环境才可关闭证书校验。

本地 fake WebSocket 回归证明：握手并发限制为 2 时，5 个连接仍能同时保持，结束后 active 回到 0。

## 生产只读证据

### 服务和健康

- 两个 API readiness 均 `ok=true`，数据库、Redis、Engine、Worker、MOSS、FunASR、存储和备份全绿。
- Worker：1 个，队列 0，死信 0。
- 当前无 `preparing/running/judging` 比赛。
- Supervisor 中 V2、Agent、PostgreSQL、Redis、MOSS、FunASR、LiveKit 和 Nginx 均为 RUNNING。
- 500 WS 测试结束后的 readiness 仍为 `ok=true`，API stderr mtime 未变化，证明未产生新错误。

### WebSocket 规模门禁

由于生产只有 8 个可匿名访问的非测试公开房间，本轮没有通过写数据库伪造 20 个正式房间。使用这
8 个真实房间完成了 500 连接门禁：

```json
{
  "expected": 500,
  "connected": 500,
  "failed": 0,
  "success_rate": 100.0,
  "peak_connected": 500,
  "handshake_concurrency": 20,
  "latency_ms": {
    "median": 2084.74,
    "p95": 3965.9,
    "max": 4107.85
  },
  "wall_seconds": 16.15
}
```

这证明了当前 500 个同时保持的匿名 socket 容量，但不等价于“20 个同时运行比赛”的状态机、Agent、
ASR 和语音负载。完整 20 房门禁应在受控 QA 数据命名空间创建 20 个临时房间，完成后由校验工具按
ID 精确清理；不能复用学生正式房间做写入测试。

### 磁盘、release 和日志

- `df`：79 GB 文件系统，使用 61 GB，剩余约 15 GB，使用率 81%。容器 overlay 的 `du` 无法解释
  基础层占用，因此容量判断必须以 `df` 为准。
- inode 仅使用 5%，当前不是小文件/inode 风险。
- API release 约 424 KB，Web release 约 5.7 MB，不是主要磁盘来源。
- `prune-releases.sh dry-run`：10 个 release 全部小于 24 小时，候选为 0；没有执行 apply。
- 最大当前业务日志为 MOSS stderr，约 11.7 MB；其他 V2 日志均小于 200 KB。
- Supervisor 配置没有对多数 V2 日志显式写出 rotation 参数，虽然 Supervisor 有默认值，建议显式
  固定 `stdout/stderr_logfile_maxbytes` 和 backups，避免升级或环境差异改变上限。

建议将磁盘运行策略分成两级，而不是等到 5% 才让 readiness 失败：

- 20% 或 20 GB：管理后台和监控告警，但服务仍可用；
- 10% 或 8 GB：禁止生成新的恢复归档和大规模 QA，优先迁移已验证旧批次；
- 5%：readiness 失败并禁止新建/开始比赛。

MOSS 离线包、私密配置和可靠语音归档不能因磁盘紧张直接删除。真正的解决办法是把完整恢复批次
复制到另一台受控服务器或加密对象存储并在目标端校验，再按 fail-closed 流程清理源服务器旧批次。

### 数据库与索引

- `phdebate_v2` 约 14 MB，`debate_agent` 约 11 MB。
- 当前 schema 为 `0025_speech_result_pagination`，与已部署 API 的 expected revision 一致。
- PostgreSQL 没有 invalid/unready index。
- 最大业务表为 `match_events`，约 2.2 MB / 187 行；当前规模没有膨胀或 vacuum 压力。
- 已有 `(room_id, seq)` 唯一索引、speech `(match_id, created_at, id)` keyset 索引及主要外键索引。

当前样本量过小，`idx_scan=0` 不能证明索引无用，因此本轮没有删除任何索引。等正式数据达到至少
数万 event/speech 后，应依据 `pg_stat_statements` 和真实慢查询添加组合索引，不能按当前 71 个房间
的统计提前优化。

### 备份可见性

- V2 最近备份：`2026-07-20T11:31:29Z`，readiness 显示约 0.7 小时，dump 结构可读。
- V2 health 会在最后成功备份超过 36 小时时失败，并检查文件状态 JSON。
- Agent 每日备份在 PostgreSQL 启动窗口曾失败一次，Supervisor 重启后生成成功 dump；当前仅能从
  stderr 发现该失败，Agent health 不报告备份 freshness。

下一轮应让两套备份统一写入：`last_attempt_at`、`last_success_at`、`last_failure_at`、失败步骤、文件
大小和校验状态。最近一次失败晚于最近一次成功时，管理后台应立即告警；不能等 36 小时后才发现。
错误状态不得包含连接串、密码或 pg_dump 完整命令。

### 重启恢复与发布回滚

- Engine 启动会把属于 Engine 的 `synthesizing/playing` speech 和 `running` judge 标记为 interrupted，
  写入只追加事件并移除 `.part` 临时音频；已有单元/场景测试验证幂等执行。
- 本轮未在生产创建伪比赛和重启 Engine，因此“真实进程重启恢复”仍需单独维护窗口验证，不能用单元
  测试冒充生产证明。
- API rollout 已按 secondary → health → primary → public health 执行，失败时恢复两个旧链接。
- Web 没有同等级 rollout/rollback 工具。建议新增：候选端口影子启动、HTML/静态 chunk 检查、原子
  切换 `.web-current`、Supervisor restart、公共页面健康门禁、失败自动切回旧链接。
- 数据库迁移不能用代码回滚代替。部署含 migration 时必须先确认 migration 的 backward compatibility，
  否则 API 双 worker 滚动期间可能出现新旧 schema 不兼容。

### GitHub 与恢复一致性

- 服务器源码 checkout HEAD：`e323f97e31d8d2ab79fcb8919275bf54ff408239`。
- Round 13 manifest 的 `code_commit` 与该 HEAD 完全一致。
- manifest 的 deployed application commit `eca07666...` 是 HEAD 的祖先。
- 当前 API release 与服务器 GitHub checkout 的 `apps/api` 做排除缓存后的递归 diff，无差异。
- manifest 绑定的 API/Web release 与当前三个 active symlink 一致。
- 可靠音频 fingerprint 仍为受保护值 `3219138b...285285`。

缺口：release 目录只含 `.release-complete` 和构建产物，没有 `.source-commit` 或可验证的 source tree
digest。建议构建时写入只读 `.release-provenance.json`，至少包含 source commit、dirty=false、构建时间、
schema head、Node/Python 版本和关键源码 tree digest；恢复 manifest 应读取该文件，而不是接受任意手填
的 `PHDEBATE_DEPLOYED_APPLICATION_COMMIT`。

## 测试结果

```text
deploy/tests/test_prune_recovery_sets.py + scripts/tests/test_load_watchers.py
18 passed

deploy/tests 全量
37 passed

Ruff（load_watchers 及测试）
All checks passed

Bash 3.2 syntax
passed
```

## 后续执行顺序

1. 先完成第三个经过恢复演练的 schema 2 批次，并复制到异机受控存储。
2. 将磁盘 warning 提升到 20%，补充容量告警；不要直接删当前恢复材料。
3. 统一 V2/Agent 备份状态与失败告警。
4. 增加 Web 原子 rollout/rollback 和 release provenance。
5. 在独立 QA 命名空间执行 20 房 / 500 WS、Engine 真重启、数据库连接池和状态机并发门禁。
6. 所有验证完成后再次执行 recovery/release dry-run，由运维人工复核后再决定是否 apply。
