# Round 21 仓库与生产清理审计

审计时间：2026-07-21（生产服务器约 03:38–03:45 UTC）  
范围：当前 GitHub 备份分支、本机工作目录、`117.50.192.216` 生产目录、发布物、恢复集和公开入口。  
原则：本轮只做证据审计，没有删除本机或服务器文件，没有修改冻结语音与浏览器播放路径。

## 结论

当前公开产品已经是单一版本：GitHub 正式分支顶层只有 `platform/`、`debate-agent/` 和需求文档；生产只有
`/home/ubuntu/sunsq/phdebate` 一个平台根目录，旧 `phdebate-v2` 根目录不存在。`/v2`、`/teacher`、
`/classroom` 均返回 404，没有 6016 监听、Qwen3 8B 进程或模型目录。

真正的清理风险不在业务源码，而在恢复物：根分区已使用 83%，可用约 14 GiB；平台审计脚本报告
`free_percent=16.46`、状态为 `warning`。约 16 GiB 的平台部署恢复物和约 17 GiB 的早期完整备份占据主要空间。
现有恢复集清理器又被 7 个旧 schema 1 清单阻塞，因此不能直接执行自动删除。

## 已验证的当前状态

| 项目 | 证据 | 判定 |
| --- | --- | --- |
| GitHub 备份 | `backup/production-20260720-round12` 远端提交 `1d4e0f759ef289559cc87b7832fa699d9976362f` | 当前代码已进入 GitHub |
| 仓库顶层 | 正式分支仅 `.gitignore`、`README.md`、`platform/`、`debate-agent/`、`动态需求.md` | 没有第二套旧前后端源码 |
| 生产根目录 | `/home/ubuntu/sunsq/phdebate` 存在；`phdebate-v2` 不存在 | 单一平台目录成立 |
| 当前发布 | API 主/副实例及 Web 均指向 `round20-private-transcript-20260721` | 三个入口使用同一发布代次 |
| 版本入口 | `/v2` 为 404；`/v2/` 先 308 到 `/v2` 后为 404 | 无可使用的 V2 产品入口；可后续让带斜杠路径直接 404 |
| 教学入口 | `/teacher`、`/classroom` 为 404 | 生产不再提供教师/课堂产品入口 |
| 6016 | 无监听、无进程、无服务器文件命中 | 未使用 6016 部署链路 |
| Qwen3 8B | 无生产进程或模型目录；源码命中仅为禁止校验和回归测试 | 未接入 Qwen3 8B |
| 服务状态 | API、Web、Engine、Worker、Agent、FunASR、LiveKit、协同服务运行；MOSS 为 FATAL | 清理不能解决 MOSS 硬件批准不匹配；readiness 仍为 503 |

## 必须保留

以下内容看似“旧”，但删除会破坏恢复、数据链或安全门禁。

1. 冻结可靠语音范围：
   - `platform/apps/api/app/core/config.py`
   - `platform/apps/api/app/services/livekit_audio.py`
   - `platform/apps/api/app/services/providers.py`
   - `platform/apps/api/app/services/voice_runtime/**`
   - `platform/apps/web/components/debate-stage.tsx`
   - `platform/apps/web/lib/audio/**`
   - `platform/apps/web/public/worklets/**`
   - `platform/assets/moss-prompts/**`
   - `platform/deploy/openmoss/**`
   - `platform/services/moss-realtime-gateway/**`

2. Alembic `0013_classroom_foundation.py`、`0014_activity_teacher_mvp.py` 和
   `0022_remove_classroom_domain.py`。前两个是历史迁移，后一个负责删除旧领域；它们共同构成可重放的数据库迁移链，
   不是仍在运行的课堂功能。

3. `RETIRED_AUDIT_ACTION_PREFIXES` / `RETIRED_AUDIT_TARGET_TYPES`。它们用于过滤历史课堂审计数据，防止旧概念重新出现在管理页面。

4. `jixia_v2_*` Cookie 兼容读取、`V2_ADMIN_*` fallback、schema 2 的 `v2_database` 恢复角色和 `/v2` 404 测试。
   这些是向单版本迁移的兼容和防回归机制，不是第二套产品。后续只能按“先停止写入、观察兼容窗口、再删除读取”的流程移除。

5. `debateall/`。目录约 2.9 GiB，但当前 `jixia-funasr-asr` 的 Python 环境、模型和日志实际从这里运行，不能按旧项目目录删除。

6. 当前 API/Web release、PostgreSQL、Redis、比赛归档、学生音频、数据库备份，以及仍被有效恢复清单引用的
   `20260719-openmoss-offline.tar` 和可靠语音恢复包。

7. Qwen3 8B 的服务端拒绝校验、管理接口拒绝测试和 README 禁用说明。这些命中证明禁令被执行，不代表系统正在使用该模型。

## 可安全清理，但应由受控清理任务执行

### 本机生成物

下列内容不在 GitHub 正式分支内，可重新生成；删除前仍应排除冻结语音目录：

- `platform/apps/web/node_modules`，约 1.4 GiB；
- Web `.next`、TypeScript build info、pytest/ruff/Hypothesis 缓存、`__pycache__`、`.DS_Store`；
- 本机失效或可重建的虚拟环境；当前 `platform/.venv` 已有历史路径不可迁移问题；
- `platform/docs/qa` 中被根 `.gitignore` 明确排除的截图、视频、浏览器 JSON、临时音频和探针日志，当前本机约
  385 MiB、1,625 个文件。文本 `.md` 报告已经进入 GitHub；大体积证据如仍需留存，应先转移到服务器受控归档，而不是保留为 Mac 备份；
- 空的本机 `app/teacher/**`、`app/admin/classrooms/` 目录。它们没有被 Git 跟踪，不会生成生产路由。

不得把 `platform/assets/moss-prompts/audio/*.wav` 或任何冻结路径混入上述清理。

### 生产发布物

生产现有 7 个 API release 和 7 个 Web release，总量分别约 25 MiB 和 399 MiB。`prune-releases.sh dry-run`
当前没有候选，因为所有版本仍受“保留最近 5 个”和“至少 24 小时”规则保护。达到最小年龄后，可使用脚本先 dry-run，
再删除不被 `.api-primary`、`.api-secondary`、`.web-current` 引用的最旧 release；不要手工按目录名删除。

`phdebate-source-round20` 是 13 MiB 的 Git 工作副本，不是运行依赖。确认未来发布统一从 GitHub 克隆且当前
`1d4e0f7` 可恢复后可重建，不是当前磁盘压力重点。

## 必须先归档或修复索引，不能直接删除

### 旧完整备份

`/home/ubuntu/sunsq/backups/phdebate-reliable-20260718T194814Z` 约 17 GiB，其中：

- 约 11 GiB 的 `openmoss-runtime-and-models.tar`；
- 约 5.1 GiB 的旧名 `phdebate-v2-project.tar.gz`；
- 数据库、私密配置、版本表和 SHA-256 清单。

它是早期“可靠语音”完整恢复点，不能因名称含 V2 就直接删除。应先把当前 Round 21 恢复集完整生成、校验、做一次
隔离恢复，并确认新恢复集包含同等的模型、配置、数据库和数据卷；随后将该旧恢复点转移到独立服务器/对象存储，
确认远端校验值后才可从生产盘移除。

### 恢复集清理阻塞

`runtime/deploy-backups` 约 16 GiB，其中约 11 GiB 是 MOSS 离线包，另有 12 份约 376–457 MiB 的数据卷归档。
`prune-recovery-sets.sh dry-run` 没有删除候选，原因是 7 个 schema 1 清单采用旧的
`<sha256> <absolute-path>` 行格式，当前结构化解析器将其判为 `unsupported-or-incomplete`。
`quarantine-recovery-manifests.py dry-run` 同样拒绝全部 7 个，因为无法从旧格式取得安全的
`artifact.data_volumes.filename`。

因此，下一步应新增一次性的 schema 1 转换/隔离工具：只接受位于批准目录内的 basename，核对实际大小和 SHA-256，
生成 schema 3 清单或带保护元数据的 quarantine sidecar。转换后先运行完整 recovery index 校验，才允许保留最近 3 个
有效恢复集并清理未被引用的旧数据卷。不要绕过 `unsafe-manifests-present` 门禁手工删除归档。

Round 21 数据卷、私密配置和可靠语音包在审计期间仍陆续生成，尚未看到最终完整 manifest 时不得参与清理。

## 需整理而非立即删除的源码/部署方案

1. `debate-agent/deploy/bootstrap.sh`、`renew-ip-certificate.sh`、`nginx.conf`、`docker-compose.yml` 仍含旧
   `117.50.218.251`。当前同机生产使用 `supervisor.same-host.conf` 和 `nginx.same-host.locations.conf`，这些旧 IP 文件
   不在热路径，但 README 把它们描述为未来独立服务器方案。建议后续改成必须显式提供 `DEBATE_AGENT_IP` 并从模板生成
   Nginx 配置；完成前标记为“独立迁移参考”，不要误用于当前服务器，也不要简单替换成当前 IP 后继续制造硬编码。

2. 历史 LightTTS、CosyVoice、火山 TTS、MOSS 试验文档和探针可保留为只读证据，但不应出现在日常部署入口或 README
   的执行路径中。当前 README 已明确唯一生产 Provider 为 MOSS，旧 `verify_real_voice_multi_match.py` 也已标记为迁移参考。

3. `/v2/` 当前会由框架规范化为 `/v2` 再返回 404。若要求所有版本化 URL 第一次响应即为 404，可在非音频 Nginx
   配置中同时精确拒绝 `/v2` 和 `/v2/`；这是低优先级一致性改进，不代表当前存在第二版本。

## 额外运维风险

只读端口盘点发现服务器网络命名空间内有以下监听：Docker Remote API `0.0.0.0:2375`、Jupyter
`0.0.0.0:8888`、File Browser `0.0.0.0:8889` 和未知服务 `0.0.0.0:9191`。本机访问 2375 可无 TLS 获取
Docker 版本信息，其他三个端口分别返回 302/200/200。这证明至少容器内部没有做到 loopback 隔离；本轮没有完成公网
防火墙可达性判定，因此不能声称它们已公网暴露，但应立即由服务器层核对安全组和宿主端口映射。Docker 2375 若可从
非可信网络访问等同于主机控制权限，应改为 Unix socket 或 TLS，并限制 Jupyter/File Browser/9191 到管理网段。

该风险应通过网络与 Supervisor 配置修复，不能用“删目录”代替。

## 推荐执行顺序

1. 等待 Round 21 恢复集生成完整 manifest，验证数据库、Agent 数据库、数据卷、私密配置、可靠语音和 MOSS 离线包。
2. 在隔离目录完成一次恢复演练，并确认 GitHub 提交 `1d4e0f7` 与恢复清单相互指向。
3. 修复 schema 1 清单转换/隔离能力，重新运行 recovery index 和所有 prune dry-run。
4. 将 17 GiB 早期完整恢复点转移到服务器外的受控存储；校验成功后再释放生产磁盘。
5. 按脚本保留策略清理旧 data-volume archives 和 release；目标先把根分区使用率降到 70% 以下。
6. 清理本机可再生成依赖、缓存和已忽略 QA 二进制证据，严格排除冻结音频路径。
7. 参数化 Debate Agent 独立部署模板，并核对 2375/8888/8889/9191 的宿主映射和安全组。
8. MOSS FATAL 作为独立发布阻塞处理：只更新经批准的硬件身份或恢复匹配硬件，不借清理工作修改冻结语音实现。

## 本轮未执行

- 未删除服务器或本机任何文件；
- 未应用 release、source archive、recovery set prune；
- 未移动或重写旧恢复清单；
- 未修改任何冻结语音、TTS、LiveKit 或浏览器播放文件；
- 未把 MOSS readiness 503 误报为清理完成。
