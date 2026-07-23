# Round 21 当前生产版本恢复集

日期：2026-07-21  
生产服务器：`117.50.192.216`  
源码分支：`backup/production-20260720-round12`  
已部署应用提交：`f35e7b98ab67e796d0f491abcb86baf8baad12cb`

## 结论

本恢复集在新服务器本地生成，没有复制到 Mac，也没有把密钥、数据库、模型或音频放入公开 GitHub。GitHub 只保存可审计源码、测试和恢复说明；私密恢复材料保存在权限为 `0700/0600` 的服务器目录中，并由私密 `recovery-set-*.manifest` 绑定。

可靠 TTS、实时音频和浏览器播放基线没有修改。82 个冻结文件验证通过，指纹仍为：

`3219138b22878e766c94b9a1fa0e422211a6f27dd74afbbb93a8e45f7e295285`

## 私密恢复材料

| 角色 | 文件 | 字节 | SHA-256 |
| --- | --- | ---: | --- |
| 平台 PostgreSQL | `runtime/backups/auto-20260721T033506Z.dump` | 534860 | `3aae517651e1213953417758001aad3b27202d4ad7b5de3d801a0983936d7212` |
| Debate Agent PostgreSQL | `/home/ubuntu/sunsq/debate-agent/backups/agent-20260721T030658Z.dump` | 301616 | `3af422953ef9f4c7df8fff3723ac05be8e94e4d2782ac947f392f48ff003aeb6` |
| 比赛数据卷 | `runtime/deploy-backups/20260721T-round21-data-volumes.tar.gz` | 478794125 | `692f4321078a440a94850879a78118583b32e0a63b535af92f2c26b4091bf675` |
| 私密配置 | `runtime/deploy-backups/20260721T-round21-private-config.tar.gz` | 5233 | `d352eafa338e9c8dcf59b7c656d9a502e74c6464bb084277ec103f976746eda2` |
| 可靠语音运行时清单 | `runtime/deploy-backups/20260721T-round21-reliable-voice-runtime.tar.gz` | 23738 | `ebbc5f51a89e364d2dd3db18499738ba95a9cdbc0d5ef0ca3335beef47b94e75` |
| OpenMOSS 离线目录 | `runtime/deploy-backups/20260719-openmoss-offline.tar` | 11809177600 | `bec995daf334694aa7dd48b9cf603bd5f2f7f3e7d8527660cafcc8136ee456d8` |

数据卷包含 `storage`、MOSS Prompt 资产和可靠音频基线。私密配置包含平台和 Debate Agent 环境文件、Supervisor、Nginx、LiveKit、FunASR 与受限密钥目录；归档成员已检查，不包含绝对路径或 `..` 路径。

## 恢复验证证据

- 平台数据库：隔离恢复成功，30 张表，Alembic `0031_caption_segments`，逐表行数与生产一致。
- Debate Agent 数据库：隔离恢复成功，13 张表，Alembic `0001_initial`，逐表行数与生产一致。
- 数据卷：隔离解包成功，436 个文件，内部逐文件 SHA-256 校验通过。
- 私密配置、可靠语音清单和 OpenMOSS 离线包：外层 SHA-256 校验通过。
- 可靠音频：82 个文件与冻结 manifest 完全一致。
- 源码：GitHub 不可变提交与生产发布 provenance 可核对。

## 边界与后续操作

- 恢复材料仍位于生产服务器同一磁盘，能够应对代码或配置误操作，但不能替代磁盘灾难的异机副本。后续应复制到另一台受控服务器或加密对象存储，并在目标端重新计算哈希；禁止复制到 Mac 或公开仓库。
- 根盘当前约 83% 使用率。旧恢复集暂不能直接删除，因为 7 个 schema 1 清单尚未安全转换和验证。
- MOSS 服务当前因新服务器 GPU UUID/VBIOS 与冻结批准清单不一致而拒绝启动。该问题没有通过放宽检查或修改音频代码规避。
- 完整空机恢复步骤继续以 `platform/docs/deploy/full-server-restore.md` 为准；实际恢复必须使用私密 manifest 记录的源码提交、已部署提交和六个归档哈希。
