# Round 22 生产磁盘清理与发布验收

日期：2026-07-21  
服务器：`117.50.192.216`  
范围：未使用备份、构建发布、下载缓存、淘汰运行时与非音频产品修复。  
约束：不修改可靠 TTS、浏览器播放、LiveKit、FunASR、正式比赛数据和当前 OpenMOSS 模型。

## 结果

- 根文件系统由 `84%` 降至 `59%`。
- 已用空间由约 `63GB` 降至 `45GB`，可用空间由 `13GB` 增至 `32GB`。
- 当前 Web/API 发布均为 `round22-human-recovery-ux-20260721`。
- 保留上一完整回滚发布 `round21-privacy-product-20260721`，并用 Node 22 语法检查和 API import 门验证可用。
- 可靠音频仍为 `82 files`，fingerprint：
  `3219138b22878e766c94b9a1fa0e422211a6f27dd74afbbb93a8e45f7e295285`。

## 已删除

- 旧重复整机包 `/home/ubuntu/sunsq/backups/phdebate-reliable-20260718T194814Z`，约 `17GB`。
  - 包含旧 OpenMOSS tar、旧 `phdebate-v2` 项目 tar、旧数据库和旧私密配置。
  - 无进程、Supervisor、Nginx 或当前恢复清单引用。
  - 删除前已确认新的 Round 21 完整恢复集存在；删除后又建立并验证 Round 22 组合恢复清单。
- root pip 下载缓存约 `2.5GB`，npm 内容缓存约 `231MB`。
- Web/API 的 Round 15、16、17、18 和 Round 20 历史发布；只保留当前与上一发布。
- Node `20.5.0`、`20.19.5` 目录及 Node 20 安装包；当前统一使用 Node `22.17.1`。
- `/root/phdebate/v2` 旧代码副本、`runtime/qa` 临时结果。
- MOSS gateway 未使用的 CPU `.venv` 与已损坏的 Round 7 环境；正式配置仍指向保留的 `.venv-cu128`。

服务器本地审计记录：

```text
/home/ubuntu/sunsq/phdebate/runtime/cleanup-round22-20260721.txt
```

## 明确保留

- `/opt/OpenMOSS/models` 约 `11GB`：当前唯一实时语音方案的模型，不作为“未使用模型”删除。
- `services/moss-realtime-gateway/.venv-cu128` 约 `7.5GB`：正式 MOSS Supervisor 配置的运行环境。
- `debateall` 约 `2.9GB`：生产 FunASR 正在从该目录运行。
- `runtime/deploy-backups/20260719-openmoss-offline.tar` 约 `11GB`：当前完整异机/重建恢复所需的离线模型包。
- PostgreSQL、Redis、比赛音频、归档、当前与上一 API/Web release、Node 22、GitHub source checkout。

因此剩余占用主要是“正在使用的模型 + 唯一完整离线恢复副本 + 正式运行依赖”，不是可以无风险继续清空的重复垃圾。

## 新的当前恢复入口

```text
runtime/deploy-backups/recovery-set-20260721T-round22-cleanup.manifest
runtime/deploy-backups/recovery-set-20260721T-round22-cleanup.manifest.sha256
```

清单绑定：

- GitHub 代码提交：`cbf52eedafe5a9b3599fb2eabaa72dfbdad9c925`
- 已部署应用提交：`e8604d19057147f450a62176bf98a02501692f74`
- 数据库 schema：`0031_caption_segments`
- 发布：`round22-human-recovery-ux-20260721`
- 平台数据库：`auto-20260721T061702Z.dump`
- Agent 数据库、数据卷、私密配置、可靠语音运行时与 OpenMOSS 离线包沿用已验证的最新材料。

完整 `verify-recovery-manifest.sh` 已重新读取六类文件并验证 SHA-256，结果：

```text
recovery_manifest_verified artifacts=6 code_commit=cbf52eedafe5a9b3599fb2eabaa72dfbdad9c925
```

## Round 22 产品与恢复修复

- 系统管理员可在单场控制台直接恢复已经重新连接的 AI 接替真人席位；后端继续校验管理员权限、连接、发言冲突和跨房占用。
- 原参赛者在 AI 接替后遇到 `review_required` 或 `terminated`，可以返回自己的结果；匿名观众仍留在脱敏观战。
- 跳过阶段增加包含阶段名称的不可撤销确认。
- 首页排行榜弱网重试采用 single-flight，退出失败有状态和重试，注册会话确认态不再引发布局跳动。
- `Room Lobby` 统一改为“赛前大厅”。
- schema 1 恢复清单增加非破坏性伴随清单工具；共享大归档在一次 audit 中只计算一次 SHA，并检测哈希期间文件变化。
- 新增只读公网监听审计，当前报告 2375、8888、8889、9191 为风险项；本轮未擅自关闭运维服务。

## 验证

- API：`475 passed, 1 xfailed, 2 warnings`。
- Web：`50 files, 324 passed`。
- Deploy：`74 passed`。
- TypeScript、Next.js build、Ruff 通过；ESLint 0 error，13 个既有冻结路径/PostCSS warning。
- 生产注册页 390×844：CLS `0`，24 个资源请求，console 无错误。
- 生产首页 390×844：CLS `0`，网络状态码无 `>=400`。
- 匿名房间 `117519`：无“文字记录/文字稿”入口，只保留比赛舞台和实时字幕投影。
- API、Web、Engine、Worker、FunASR、LiveKit、Agent、PostgreSQL、Redis、协同文字稿均为 RUNNING。

已知未改变项：MOSS 服务仍因冻结的 GPU UUID/VBIOS 审批门显示 `FATAL`，就绪接口因此为 503；数据库、schema、Redis、Engine、Worker、FunASR、存储和备份检查均通过。此次清理没有删除 MOSS 模型或正式 CUDA 环境。
