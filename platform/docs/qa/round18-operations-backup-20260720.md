# Round 18：备份恢复复核与服务器空间整理

日期：2026-07-20

## 当前可恢复版本

- GitHub 分支：`backup/production-20260720-round12`
- 源码与恢复说明提交：`1a243f677cb3d9ac8c4f9df1b43eb94efdd2f434`
- 当前部署应用提交：`31959b093bfc2e1022113bb4209fb2d2c12dee3b`
- API/Web release：`round17-spectator-cap-20260720`
- 恢复清单：`runtime/deploy-backups/recovery-set-20260720T1540Z-round17.manifest`
- 数据库 schema：`0027_speech_data_disposition`

恢复清单的六个文件已重新逐项校验，包括约 11 GB 的 MOSS 离线包。平台数据库
`auto-20260720T154013Z.dump` 不只通过外层 SHA-256，还实际恢复到临时 PostgreSQL 数据库：

- 表数量：25
- Alembic revision：`0027_speech_data_disposition`
- 临时恢复数据库在验证结束后已删除

Debate Agent 数据库、比赛数据卷、私密配置、可靠语音运行时和 MOSS 离线包沿用已经校验的同批恢复材料；
恢复清单明确记录文件名、字节数和 SHA-256，未把不同批次文件按“最新”临时拼接。

## 可靠语音冻结

- 受保护文件：82 个
- 指纹：`3219138b22878e766c94b9a1fa0e422211a6f27dd74afbbb93a8e45f7e295285`
- 本轮未修改 Agent→MOSS→LiveKit→浏览器播放链路。

## 安全空间整理

服务器此前保留了大量已经由 GitHub 快照替代的源码 tar 包和九代不可变应用 release。本轮使用有界清理脚本，
先 dry-run，再 apply：

- 源码 tar：从 21 个降为 1 个，释放约 744 MB；当前源码恢复仍以 GitHub commit 为权威。
- API release：从 9 个降为 3 个。
- Web release：从 9 个降为 3 个。
- 当前 release、上一版和唯一平台切换版均保留，可继续回滚。
- 没有删除数据库、学生数据、音频、MOSS 模型、私密配置或恢复清单引用的文件。

磁盘可用空间从约 16.7% 提升到 18.06%。仍处于 20% 预警线以下，但高于 10% 故障线。

旧恢复清单中有 7 份属于不完整或不支持的早期格式。`prune-recovery-sets.sh` 正确 fail-closed，未在无法证明
引用关系时删除这些清单及其数据卷。后续应先建立受审计的旧清单隔离/迁移流程，再释放剩余历史数据归档，
不能手工按文件名删除。

## 运行状态

- Readiness：通过
- PostgreSQL、Redis、Engine、Worker、MOSS、FunASR：通过
- 活动比赛：0
- 单一平台：旧 `phdebate-v2` 根目录不存在，`/v2` 返回 404
- Supervisor 所有生产服务保持 RUNNING
