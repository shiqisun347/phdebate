# Round 25 恢复清单升级与安全清理审计

审计时间：2026-07-21T12:13:58Z  
生产目录：`/home/ubuntu/sunsq/phdebate/runtime/deploy-backups`  
审计原则：只读生产备份目录；没有删除、移动或覆盖生产恢复文件，没有创建生产
`manifest-upgrades`，没有修改或重启 TTS、MOSS、ASR、Web 播放及比赛服务。

## 结论

**现在不能直接执行 `prune apply`。** 生产仍有 7 份 schema 1 清单且没有正式 companion，
因此现有清理器正确地以 `unsafe-manifests-present` 关闭删除路径，当前候选为 0。

7 份旧清单本身没有损坏。`upgrade-schema1-recovery-manifests.py audit` 已逐字节验证它们引用的
全部六类 artifact，并准确映射到 5 份旧数据卷。把 companion 仅写入 `/tmp` 后，完整恢复索引验证为：

```text
recovery_index_verified manifests=18 unique_artifacts=51 hashed_files=51
```

按 24 小时默认策略、在本次审计时刻模拟得到的可删除集合为 **9 个物理文件，
1,181,423,397 bytes（1.100286 GiB）**。随着归档达到 24 小时，保持 `KEEP=3` 时最终可释放集合为
**24 个物理文件，2,367,725,215 bytes（2.205116 GiB）**。另外
**1,969,136,632 bytes（1.833901 GiB）** 的 5 份数据卷仍被 7 份 schema 1 历史恢复点保护；
现有策略故意不把它们变成删除候选。

因此 Round 24 所称约 4.04 GiB 旧数据卷可以拆成：

- 2.205116 GiB：完成 companion 写入并满足龄期后可以由现有工具安全清理；
- 1.833901 GiB：仍是 schema 1 历史恢复点的权威数据，当前不能删除。

## 生产现状与恢复底线

- 根分区：79 GiB，总使用 46 GiB，可用 31 GiB，使用率 60%；没有紧急空间压力。
- 生产 `manifest-upgrades/`：不存在。
- 当前完整恢复点：`recovery-set-20260721T-round24-final-current.manifest`。
- 当前清单的 sidecar 在备份目录内运行 `sha256sum -c` 通过。
- 当前恢复点六类 artifact 全部通过大小和 SHA-256 验证：平台数据库、Agent 数据库、数据卷、
  私密配置、可靠语音运行时、11.8 GB OpenMOSS 离线包。
- 当前恢复点记录的可靠音频指纹仍为
  `3219138b22878e766c94b9a1fa0e422211a6f27dd74afbbb93a8e45f7e295285`。
- 最新三个有效清单是两个 Round 24 恢复点和 Round 22 cleanup 恢复点。两个 Round 24 清单共享
  平台数据库和数据卷，但 Agent 数据库不同，因此它们是两个可验证的完整恢复时点；`KEEP=3`
  仍保留三个完整 manifest，不是三个同内容文件。

## Schema 1 映射结果

| 旧清单 | 已验证并保护的数据卷 | 状态 |
| --- | --- | --- |
| `recovery-set-20260719T1015Z.manifest` | `20260719T101446Z-data-volumes.tar.gz` | companion write-required |
| `recovery-set-20260719T1057Z-round6.manifest` | `20260719T105628Z-data-volumes.tar.gz` | companion write-required |
| `recovery-set-20260719T1357Z-round7.manifest` | `20260719T135612Z-data-volumes.tar.gz` | companion write-required |
| `recovery-set-20260720T072642Z.manifest` | `20260720T070152Z-data-volumes.tar.gz` | companion write-required |
| `recovery-set-20260720T084642Z.manifest` | `20260720T070152Z-data-volumes.tar.gz` | companion write-required |
| `recovery-set-20260720T0932Z-round10.manifest` | `20260720T0930Z-round10-data-volumes.tar.gz` | companion write-required |
| `recovery-set-20260720T1013Z-round11.manifest` | `20260720T0930Z-round10-data-volumes.tar.gz` | companion write-required |

Upgrade audit 摘要：

```text
schema1_upgrade_complete mode=audit manifests=7 written=0 existing=0
```

`quarantine-recovery-manifests.py dry-run` 对这 7 份清单全部拒绝隔离：旧格式使用 checksum 行，
没有 `artifact.data_volumes.filename` 字段，工具无法在不依赖 upgrade 验证的情况下建立安全引用。
这是预期的 fail-closed 行为；此处应该使用 upgrade companion，而不是强行 quarantine。

## 当前 24 小时策略的精确删除集

以下是 companion 只存在于 `/tmp` 时，对真实生产 artifact 执行的默认 dry-run 结果。逻辑候选为
2 份 manifest 和 3 份数据卷；考虑 checksum sidecar 后是 9 个物理文件：

```text
20260719T091243Z-data-volumes.tar.gz
20260719T091243Z-data-volumes.tar.gz.sha256
20260720T1050Z-round12-data-volumes.tar.gz
20260720T1050Z-round12-data-volumes.tar.gz.sha256
20260720T1132Z-round13-data-volumes.tar.gz
20260720T1132Z-round13-data-volumes.tar.gz.sha256
recovery-set-20260720T1052Z-round12.manifest
recovery-set-20260720T1052Z-round12.manifest.sha256
recovery-set-20260720T1134Z-round13.manifest
```

其中 `20260719T091243Z-data-volumes.tar.gz` 没有被任何现存 recovery manifest 引用；另外两份数据卷
分别只被将同时删除的 Round 12、Round 13 清单引用。

注意：年龄是滚动条件。实际执行前必须重新生成 dry-run，不能照抄本节静态列表；如果 Round 14 等
文件届时超过 24 小时，它们会合法进入下一批候选。

## 达龄后的完整可释放集

以 `MIN_AGE_HOURS=0` 仅做上界模拟，`KEEP=3` 不变。除上一节文件外，后续可进入候选的是：

```text
20260720T1235Z-round14-data-volumes.tar.gz
20260720T1235Z-round14-data-volumes.tar.gz.sha256
20260720T1352Z-round15-single-platform-data-volumes.tar.gz
20260720T1352Z-round15-single-platform-data-volumes.tar.gz.sha256
20260720T1505Z-round16-data-volumes.tar.gz
20260720T1505Z-round16-data-volumes.tar.gz.sha256
recovery-set-20260720T1236Z-round14.manifest
recovery-set-20260720T1354Z-round15-single-platform.manifest
recovery-set-20260720T1354Z-round15-single-platform.manifest.sha256
recovery-set-20260720T1507Z-round16.manifest
recovery-set-20260720T1507Z-round16.manifest.sha256
recovery-set-20260720T1540Z-round17.manifest
recovery-set-20260720T1613Z-round18.manifest
recovery-set-20260721T-round21.manifest
recovery-set-20260721T-round21.manifest.sha256
```

Round 16、17、18 三个清单共享 Round 16 数据卷；只有三份清单都不再被保留时，该数据卷才进入
候选。Round 21 清单可以删除，但它的数据卷仍被保留的 Round 22 cleanup 清单引用，因此 Round 21
数据卷不会被删除。

## 明确保留集

### 当前/近期完整恢复点

- `recovery-set-20260721T-round24-final-current.manifest` 及 sidecar；
- `recovery-set-20260721T-round24-final.manifest` 及 sidecar；
- `recovery-set-20260721T-round22-cleanup.manifest` 及 sidecar；
- 上述清单引用的两个数据卷、数据库、私密配置、可靠语音包和 OpenMOSS 包。

### Schema 1 历史恢复点

以下 5 份数据卷及 sidecar 合计 1.833901 GiB，现有 companion 策略永久保护：

```text
20260719T101446Z-data-volumes.tar.gz
20260719T105628Z-data-volumes.tar.gz
20260719T135612Z-data-volumes.tar.gz
20260720T070152Z-data-volumes.tar.gz
20260720T0930Z-round10-data-volumes.tar.gz
```

以及 7 份原 schema 1 清单、各自存在的 sidecar、正式写入后对应的 7 份 schema 3 companion。

### 非本任务清理范围

- OpenMOSS 离线包、CUDA 环境、TorchInductor warm cache；
- 冻结的 82 个实时音频/浏览器播放文件；
- 平台和 Agent 数据库备份、私密配置、可靠语音运行时；
- 学生比赛音频、字幕/研究数据；
- API/Web/transcript-collab releases。

## 清理器修复

发现一个确定的清理一致性缺陷：旧脚本删除 recovery manifest 时不删除它的
`.manifest.sha256`，会留下看似有效但没有目标文件的恢复元数据。本轮已在本地修复：

- `deploy/prune-recovery-sets.sh` apply 使用精确 basename，在同一顶层边界同时删除 manifest 和
  可选 `.sha256`；
- 不使用通配前缀，不会匹配其他清单；
- dry-run 行为不变；
- 被保留 manifest 的 sidecar 保持不变。

验证：

```text
28 passed in 4.91s
audio_baseline_verified files=82 fingerprint=3219138b22878e766c94b9a1fa0e422211a6f27dd74afbbb93a8e45f7e295285
```

该修复尚未部署生产。建议先合并并部署，再执行任何 prune apply；否则旧生产脚本虽然不会误删
业务数据，但会留下过期 manifest sidecar。

## 推荐执行门与回滚方式

### 执行前置条件

1. 合并并部署本轮 sidecar 修复；再次运行对应 deploy tests。
2. 再次验证 `round24-final-current` 六类 artifact 和自身 sidecar。
3. 运行 schema 1 `audit`，输出与本报告 7 对 5 映射完全一致。
4. 使用显式确认执行 schema 1 `write`，只在
   `runtime/deploy-backups/manifest-upgrades/` 创建 `0600` companion 和审计报告，不覆盖旧清单。
5. 显式运行完整 recovery index；必须仍为 18 个清单且全部 artifact 校验通过。
6. 运行默认 `prune-recovery-sets.sh dry-run`，保存 stdout、候选文件 size/SHA-256 和磁盘状态。
7. 人工确认候选中不得出现 Round 24、Round 22、schema 1 清单/数据卷、可靠语音包或 OpenMOSS 包。
8. 只有上述条件全部成立，才能单独授权 apply。

### 回滚边界

`prune apply` 是物理删除，**在同一服务器上没有“撤销删除”能力**。它不会影响当前服务，因此
运行态回滚只需停止后续清理，不需要切换应用 release。若要求能恢复本批被删的历史恢复点，必须在
apply 前将本次 dry-run 的精确物理文件集复制到服务器之外的受控备份或对象存储，并逐文件校验
SHA-256；不要备份到本机 Mac。只有确认外部副本可读取后，清理本身才可逆。

即使不制作额外历史副本，保留的三个完整 manifest 仍可按
`docs/deploy/full-server-restore.md` 恢复当前和近期平台；但这不是对被删除旧历史点的一比一回滚。

## 本轮生产变更声明

- 未执行 schema 1 production write；
- 未执行 quarantine apply；
- 未执行 prune apply；
- `/tmp` companion 模拟与报告已在审计后删除；
- 生产恢复文件的名称、字节数、mtime 和内容均未由本轮改动；
- 未修改冻结音频实现，82 文件基线复验一致。

