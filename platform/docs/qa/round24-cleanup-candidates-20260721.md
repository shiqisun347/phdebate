# Round 24 单版本清理候选审计

审计日期：2026-07-21  
范围：本机 `/Users/sunshiqi/code/phdebate/platform`、生产 `/home/ubuntu/sunsq/phdebate`、当前发布与恢复集。  
原则：只读盘点；没有删除、移动或覆盖任何文件，没有重启生产服务，没有修改冻结 TTS、LiveKit 或浏览器播放实现。

## 结论

当前公开系统只有一个 `platform` 产品版本。生产根目录约 **26 GiB**，根分区使用率 **59%**；主要占用不是第二套业务源码，而是：

- `runtime/deploy-backups` 约 **16 GiB**，其中 **11.0 GiB** 是当前完整恢复仍需要的 MOSS 离线包，约 **4.04 GiB** 是 11 份旧数据卷归档；
- `services/moss-realtime-gateway` 约 **7.5 GiB**，主体是当前 RTX 3090 CUDA 运行环境，正在承载 MOSS，不能清理；
- `apps/web` 约 **824 MiB**，其中生产源码树的 `node_modules` 约 **751 MiB**、`.next` 约 **71 MiB**；
- `storage/audio` 约 **575 MiB**，是比赛数据，不得按目录日期或房间号直接删除。

本机 `platform` 约 **2.4 GiB**。其中约 **1.4 GiB** 是 Web 依赖、**230 MiB** 是 `.next`、**218 MiB** 是 Python 虚拟环境、**390.2 MiB** 是未进入 Git 的 QA 二进制证据。它们大多可再生成，但本轮按要求没有实际清理。

冻结音频基线复验通过：

```text
audio_baseline_verified files=82 fingerprint=3219138b22878e766c94b9a1fa0e422211a6f27dd74afbbb93a8e45f7e295285
```

## 立即处理但不是“删除”的运行风险

### Transcript collaboration 的发布链接已丢失

生产 `jixia-transcript-collab` 仍显示 `RUNNING` 且 `127.0.0.1:12400` 仍监听，但 Supervisor 配置依赖的：

```text
/home/ubuntu/sunsq/phdebate/.transcript-collab-current
/home/ubuntu/sunsq/phdebate/.transcript-collab-releases
```

当前都不存在。现有 PID 是在目录被移除前启动的进程，因此仍能短暂运行；下一次进程或服务器重启会因工作目录不存在而启动失败。任何后续清理前都应先重新建立不可变 release 和 current symlink。

建议由主线运维执行：

```bash
cd /home/ubuntu/sunsq/phdebate
test -x runtime/node/bin/node
test -s services/transcript-collab/package-lock.json
test -s .env
PHDEBATE_COLLAB_RELEASE=round24-collab-recovery-20260721 \
  ./deploy/build-transcript-collab-release.sh
test -L .transcript-collab-current
readlink -f .transcript-collab-current
supervisorctl restart jixia-transcript-collab
supervisorctl status jixia-transcript-collab
curl -fsS http://127.0.0.1:12400/health
curl -fsS http://127.0.0.1:12400/ready
```

验证点：current 链接必须指向 `.transcript-collab-releases/round24-collab-recovery-20260721`；Supervisor 连续运行至少 30 秒；`/health` 与 `/ready` 均返回 200；随后用真实房间的房主/参赛者身份取得一次 `transcript-collab-token` 并完成 WebSocket 连接，匿名观众仍必须被拒绝。

## 安全候选清单

“Tracked”以 GitHub 备份分支中的 Git tree 为准，不以本机 `main` 的脏工作树状态判断；`platform/` 在本机 `main` 下显示未跟踪是历史分支结构，不能据此认定整目录可删。

| 候选 | 位置与大小 | Git 状态 | 类型 | 建议 | 风险 | 删除前/后验证 |
| --- | --- | --- | --- | --- | --- | --- |
| 本机 Web 依赖 | `apps/web/node_modules`，约 1.4 GiB | ignored / untracked | build dependency | 可清理并由 `npm ci` 重建 | 低到中；离线时无法测试/构建 | 保存 `package-lock.json`；重建后 `npm ls --depth=0`、Web test/build |
| 本机重复包管理器残留 | `apps/web/node_modules/.ignored` 277 MiB、`.pnpm` 576 MiB | ignored / untracked | duplicate dependency cache | 高优先级本机候选；当前正式链是 `package-lock.json` + npm | 低；不要删整个源码 | 清理后 `npm ci`；确认 `.ignored`/`.pnpm` 不再生成；`npm test`、`npm run build` |
| 本机 Web 构建物 | `apps/web/.next` 230 MiB、`tsconfig.tsbuildinfo` | ignored / untracked | build output | 可清理 | 低 | `npm run build` 能重新生成；不影响当前生产 release |
| 本机测试与工具缓存 | `test-results` 3 MiB、`.hypothesis` 2.8 MiB、pytest/ruff 缓存、`__pycache__` | ignored / untracked | runtime/test cache | 可清理 | 低 | 清理后运行对应测试；不得匹配冻结目录中的源码文件 |
| 本机虚拟环境 | `platform/.venv` 218 MiB | ignored / untracked | build/runtime dependency | 可在记录依赖后重建；不是可迁移备份 | 中；当前本地测试会暂时不可用 | 用 `uv.lock`/requirements 重建并运行 API 全测；不要碰 `services/moss-realtime-gateway` 冻结目录 |
| 本机 QA 二进制证据 | `docs/qa` 未跟踪 1,501 文件、390.2 MiB；其中 PNG 约 200.8 MiB、WAV 约 141.9 MiB、WebM 约 17 MiB | ignored / untracked；同目录 157 个文本文件、约 1.4 MiB 已 tracked | test evidence | 报告已提炼且证据无需长期保存时，可转移到服务器外的受控对象存储后清理本机副本 | 中；音频质量问题的原始证据可能无法复盘 | 建 manifest+SHA-256；抽样下载复验；保留 tracked Markdown；明确排除 `assets/moss-prompts` |
| `.DS_Store` | 本机共 322 个，大多位于 `node_modules`；源码树外部约十余个 | ignored / untracked | OS metadata | 可清理 | 极低 | `find platform -name .DS_Store` 应为 0；Git tree 不变 |
| 生产源码 Web `.next` | `apps/web/.next` 71 MiB | ignored / untracked | build output | 可清理；当前运行来自 `.web-current` 的 round23 release | 低 | 先确认 `.web-current` 指向完整 release、首页健康；清理后执行一次隔离 build |
| 生产源码 Web `node_modules` | `apps/web/node_modules` 751 MiB | ignored / untracked | deployment build dependency | 不作为第一批删除；可由 `npm ci` 重建，但 `build-web-release.sh` 当前直接硬链接它 | 中到高 | 删除前在独立目录 `npm ci` 并完成 release build；保留 lockfile、Node 22；不能只凭“非运行进程 cwd”删除 |
| 生产旧 Web release | `runtime/web-releases/round21-*` 约 59 MiB；round22 约 59 MiB 是当前回滚候选，round23 为 current | untracked runtime | immutable release | round21 达龄且 dry-run 选中后可清理 | 中 | 只能用 release prune 脚本；确认 `.web-current`、保留 current+previous、HTTP/静态 chunk smoke |
| 生产旧 API release | `runtime/api-releases/round21-*` 约 3.8 MiB；round22 被 primary/secondary 引用 | untracked runtime | immutable release | round21 达龄且 dry-run 选中后可清理 | 中 | 确认 `.api-primary`/`.api-secondary`；禁止手工删除被 symlink 引用目录；API readiness |
| 生产旧数据卷归档 | 除 round21 当前归档外 11 份，共 4.04 GiB | untracked runtime | recovery artifact | 最大的安全收益候选，但当前尚不可直接删 | 高 | 先完成 schema1 companion 写入、恢复索引验证和 prune dry-run；仅删除不再被保留清单引用的归档 |
| 生产旧恢复清单/sidecar | 14 个旧 manifest 约 20 KiB，加对应 sha256 | untracked runtime | recovery metadata | 随数据卷由清理器一起处理，不单删 | 高 | 清单和归档必须原子成组处理；保留最近 3 个完整恢复点 |
| 生产早期私密配置小包 | 旧 `.env`/private-config 包共约 49 KiB | untracked runtime | sensitive recovery artifact | 空间收益可忽略；只随对应恢复集生命周期处理 | 高（安全与回滚） | 不输出内容；确认新恢复集私密配置可解密/恢复后再按策略销毁 |
| 生产测试残留 | `storage/pytest-audio` 12 KiB、`storage/audio/qa-diagnostic` 4 KiB | untracked runtime | test data | 可在确认没有数据库引用后清理 | 低 | 查询 `Speech`/`AudioAsset` 引用为 0；API/归档导出测试 |
| 生产 `/tmp` 测试残留 | `/tmp/pgvector` 7.8 MiB、pytest 临时目录约 3.9 MiB、临时源码约 4.3 MiB、Node compile cache约 23 MiB | untracked runtime | temporary/build cache | 可清理；不是平台恢复物 | 低 | 排除正在使用的 PID/cwd；重启不依赖；`lsof +D` 或等价检查 |
| MOSS TorchInductor `/tmp` 缓存 | `/tmp/torchinductor_moss` 277 MiB | untracked runtime | active compiled cache | **不要清理**；会触发约 20 分钟重新编译和比赛暂停风险 | 高 | 只有计划维护窗口、完整 warmup 和 readiness 门时才允许重建 |
| 生产学生/比赛音频 | `storage/audio` 575 MiB、43 个目录 | untracked runtime data | authoritative product data | **不是清理候选**，需要先实现数据库引用与保留策略审计器 | 极高 | 按 `AudioAsset`/`Speech`/Match 关系做 mark-and-sweep dry-run；结果页、导出、研究数据复核 |

## 恢复集：为什么 4.04 GiB 现在仍不能直接删

当前完整恢复点 `recovery-set-20260721T-round22-cleanup.manifest` 已引用并校验：平台数据库、Agent 数据库、round21 数据卷、私密配置、可靠语音恢复包和 11.0 GiB MOSS 离线包。MOSS 离线包是恢复当前 GPU 服务的必要资产，不是重复模型。

旧恢复清理仍被 7 个 schema 1 清单阻塞：

```text
recovery_prune_complete mode=dry-run candidates=0 preserved=7 keep=3
reason=unsafe-manifests-present
```

当前仓库已经有 `deploy/upgrade-schema1-recovery-manifests.py`。只读 `audit` 已成功核对 7 个清单及其数据卷，全部为 `write-required`，说明可以进入受控转换，但生产尚未写入 `manifest-upgrades` companion，不能绕开门禁手删文件。

建议执行顺序：

1. 再次完整验证 round22 cleanup manifest 与六类 artifact；
2. 运行 schema1 upgrade 的 `audit` 并保存报告；
3. 在批准的 `manifest-upgrades` 目录执行 `write`，绝不覆盖原清单；
4. 运行 recovery index 校验；
5. 运行 `prune-recovery-sets.sh dry-run`，人工核对候选 basename、大小、SHA-256 与保留 3 个恢复点；
6. 仅在 dry-run 无 unsafe manifest、当前恢复集仍完整时执行 apply；
7. 再次验证当前恢复集，并检查磁盘、数据库、Redis、MOSS、API、Engine、Worker、Web。

## 旧业务和旧语音代码判定

### 教师、课堂与课程

当前产品页面和路由中没有教师、课堂、课程入口。以下命中不能按关键词删除：

- Alembic `0013`、`0014` 创建历史表，`0022` 删除旧领域；它们是数据库从旧版本升级到当前 schema 的连续迁移链；
- 管理/API 里的 retired prefix 只用于过滤旧审计记录；
- E2E 的 `/teacher`、`/classroom` 断言用于防止旧入口回归；
- ASR 基准里“教师/课堂”只是识别语料或辩题内容。

结论：运行时教学功能已经淘汰；现有迁移和负向测试应保留，不属于可安全删除的业务代码。

### `/v2` 与旧 Cookie/恢复角色

没有 `/v2` 产品入口。`jixia_v2_*` Cookie 兼容读取、`V2_ADMIN_*` fallback、schema 2 的 `v2_database` 角色和 `/v2` 404 测试仍承担迁移兼容或防回归作用。它们空间收益接近零，当前不应为了“看不到 V2 字符串”而删除。应先统计 30 天兼容读取命中为零，再单独移除。

冻结目录 `deploy/openmoss/supervisor.env.example` 中仍有 `PHDEBATE_V2_ROOT` 历史变量名，但该目录属于冻结音频范围，本轮不改；应在未来明确批准的新音频发布中迁移，而不是借清理绕过基线。

### LightTTS、VolcEngine、CosyVoice

生产只使用 MOSS-Realtime，旧 Provider 不应重新启用；但当前旧实现并非一组可直接独立删除的死文件：

- `providers.py`、`core/config.py`、`voice_runtime/**`、MOSS gateway 均在冻结 82 文件范围；
- `match_engine.py`、`system_health.py`、大量恢复/故障测试仍引用 LightTTS 兼容类型与错误码；
- `LightTTSProvider` 仍被 MOSS provider 的部分 WAV 校验、取消和辅助方法复用；
- MOSS-only 验证器依赖旧配置名来阻止生产回退。

可作为下一次“非生产工具整理”候选的是约 **110 KiB** 的旧 benchmark/probe 脚本，例如 `benchmark_volcengine_tts.py`、`benchmark_lighttts_capacity.py`、`benchmark_lighttts_quality.py`、`analyze_lighttts_splices.py`、`verify_lighttts_scheduler.py`、`verify_lighttts_redis_gate.py`。但它们是 tracked 源码；应先：

1. 从 README 的日常命令移除旧 `verify_tts_asr_roundtrip.py`；
2. 建立 `scripts/legacy_voice/README.md`，标注只读历史与禁止生产调用；
3. 确认 MOSS 正式门完整覆盖首声、连续性、中断、恢复和并发；
4. 再决定归档到 Git 历史或从当前 tree 删除。

本轮不建议动它们，因为收益不足 1 MiB，风险远高于磁盘收益，也会违反用户冻结可靠语音链路的要求。

## 重复依赖与构建链

本机 Web 依赖树同时出现 npm 正常目录、pnpm store 和 pnpm 生成的 `.ignored`：

- `node_modules/.pnpm` 576 MiB；
- `node_modules/.ignored` 277 MiB；
- 根级正常依赖仍约数百 MiB；
- `npm ls --depth=0` 只有一个约束外依赖 `@emnapi/wasi-threads`，其余顶层版本与 `package.json` 一致。

生产没有 `.pnpm`/`.ignored`，说明这是本机切换包管理器产生的重复副本。正式部署脚本和 `package-lock.json` 使用 npm；应统一规定 `npm ci`，避免再次在同一 `node_modules` 上运行 pnpm。`pnpm-workspace.yaml` 是 tracked 文件，但仓库没有 `pnpm-lock.yaml`，若确认无工具依赖它，可在独立改动中删除；删除前必须跑 Web 全测、构建和部署脚本测试。

## 明确保留

- 冻结 82 文件和其可靠音频 manifest；
- `assets/moss-prompts`、MOSS CUDA 环境、MOSS 模型与 `/tmp/torchinductor_moss` warm cache；
- 当前 round23 Web、round22 API，以及至少一个可启动的 previous release；
- round22 cleanup 完整恢复清单引用的全部 artifact；
- PostgreSQL、Redis、房间归档、学生音频和研究导出；
- `services/transcript-collab` 源码与新建的不可变 release；
- `/home/ubuntu/sunsq/debateall`（FunASR 当前运行环境）和 `/home/ubuntu/sunsq/debate-agent`（独立 Agent 服务），不能因不在 `platform` 源码树内而删除。

## 建议的低风险执行批次

1. **先修复 transcript-collab release 链接**，做健康、就绪和真实 WebSocket 验证。
2. 清理本机 `.next`、test cache、`.DS_Store`、pnpm `.ignored/.pnpm` 重复副本；重建 npm 依赖并跑 Web 全测。
3. 清理生产源码树 `.next` 和 `/tmp` 的非 MOSS 测试缓存；不动 source `node_modules`。
4. 完成 schema1 companion 写入与 recovery index 验证，再由 prune 工具释放最多约 4.04 GiB 旧数据卷。
5. release 达龄后由 prune 脚本删除 round21 API/Web，不手工删除 symlink 目标。
6. 另行设计带 dry-run、数据库引用和保留策略的音频 mark-and-sweep 工具；在工具完成前不清理 575 MiB 比赛音频。

## 本轮未执行

- 未删除任何本机或生产文件；
- 未生成 schema1 companion 或执行 recovery/release apply；
- 未重启 transcript collaboration；
- 未修改 README、旧 TTS 脚本或包管理配置；
- 未修改任何冻结 TTS、LiveKit、MOSS 或浏览器播放路径。
