# 新服务器迁移与代码清理方案

更新时间：2026-07-19

当前状态：生产已迁移到 `117.50.192.216`，根路径、PostgreSQL、Redis、LiveKit、
MOSS-Realtime、FunASR、主平台和 Debate Agent 均在新服务器运行。本文现在用于约束源码
整理和下一次可重建迁移，不再描述尚未发生的服务器切换。

## 1. 当前问题

当前工作目录不是可直接复制到新服务器的发布包：

- 旧 `main` 仍保留 V1 历史；当前 V2 与 Debate Agent 已通过无父提交的生产快照分支备份，
  避免把旧数据库和凭据历史带入当前代码恢复点。
- 工作目录包含 `.venv`、`node_modules`、`.next`、缓存、测试数据库、测试报告和运行时音频。
- README 曾保存明文服务器或模型凭据，不能继续作为凭据载体。
- V2 仍保留不可达的 LightTTS 兼容代码；它位于可靠音频基线保护范围，不能在普通业务
  迭代中直接删除。
- README、Computer Use 测试提示和 Provider 策略曾继续把火山 TTS/LightTTS 描述为
  当前或可选路径，容易让后续维护者重新启用已淘汰方案。
- `docs/qa` 包含大量有价值的测试证据，但不属于生产运行文件。
- 学校、课堂、教师工作台、教学活动、全班同意和研究导出曾形成一套独立产品域；该域已在
  `0022_remove_classroom_domain` 中从生产运行时和数据库移除，旧迁移只作为历史结构记录保留。

迁移前必须先形成一个干净、可复现、无秘密、无运行数据的源码仓库，再单独迁移数据库和媒体数据。

## 2. 目标目录

```text
phdebate/
├── platform/          # 当前 v2：Web、API、Engine、Worker
├── debate-agent/      # 独立 Debate Agent 服务
├── services/
│   └── moss-realtime-gateway/
├── deploy/            # Nginx、Supervisor、数据库、Redis、LiveKit、MOSS
├── docs/              # 当前有效架构、运维和迁移文档
└── archive/           # 不进入生产包的旧设计与 QA 证据索引
```

首次迁移不强制立即改目录名。应先让现有 `v2/` 正常运行，再通过独立重构提交完成目录调整，避免重命名与服务切换同时发生。

## 3. 分类处理

### 必须进入源码仓库

- `v2/apps/api`、`v2/apps/web`、`v2/apps/engine`、`v2/apps/worker`。
- `v2/services/moss-realtime-gateway` 的源码、测试和锁文件。
- `v2/deploy/openmoss`、数据库迁移、Nginx、Supervisor、LiveKit 和备份脚本。
- `v2/assets/moss-prompts` 的文本清单、来源、许可证和 SHA-256 元数据。固定音色 WAV 作为
  服务器数据卷备份，不进入公开 GitHub。
- `debate-agent` 的 API、Web、迁移、部署文件和测试。
- 当前有效的 README、架构、MOSS 方案、安全和运维文档。

### 单独作为数据迁移

- PostgreSQL 自定义格式备份及 SHA-256。
- Redis 只迁移确有必要的持久数据；在线状态、锁、短期任务和缓存不迁移。
- 比赛音频、逐字稿导出、归档 JSON、音色资产和对象存储目录。
- Debate Agent 的 PostgreSQL、Prompt、Profile、Memory 和审计数据。
- MOSS 模型、Tokenizer、Codec 和 8 个固定音色作为独立只读数据卷迁移，不放入公开
  GitHub；来源、许可证和校验元数据保留在源码中。

### 归档但不进入生产包

- `v2/docs/qa` 的历史报告、截图、WAV 和浏览器证据。
- 旧服务器日志、崩溃文件、旧 release 和数据库备份目录。
- LightTTS/CosyVoice 的历史测试材料。
- 旧 V1 源码只保留 Git tag 或只读压缩归档，不继续混在 V2 工作树。

### 可以安全重建，不迁移

- `.venv`、`node_modules`、`.next`、`*.egg-info`。
- `__pycache__`、`.pytest_cache`、`.ruff_cache`、`.hypothesis`。
- `test-results`、Playwright report、coverage、`*.tsbuildinfo`、`.DS_Store`。
- 本地 SQLite 测试数据库、临时 WAV、`.part`、PID、socket 和 lock 文件。
- 已有 release 压缩包及其重复副本；迁移时重新生成并校验。

## 4. LightTTS/CosyVoice 删除边界

新服务器已经只运行 MOSS-Realtime，没有 LightTTS、CosyVoice 或火山 TTS Supervisor
服务；数据库中的 `lighttts` Provider 也处于停用状态。当前残留分为两类：

1. **可以立即清理**：错误 README、过时 Provider 策略、测试提示中的旧路径、生产源码
   包中的历史 QA 大文件和明确不参与部署的候选材料。
2. **必须延后到音频发布窗口**：`config.py`、`providers.py`、`voice_runtime` 和比赛引擎中
   仍受可靠音频清单保护的兼容代码。删除它们会改变 82 个受保护文件的指纹，必须重新
   完成首声、长发言、连续性、中断、多音色和并发门禁，不能混入普通 UI/业务发布。

下一次音频发布窗口按以下顺序处理：

1. 把通用 TTS session、endpoint pool、generation、归档与取消语义从 LightTTS 命名迁移
   为 Provider-neutral 或 MOSS 命名，并保持行为不变。
2. 删除 LightTTS HTTP/bi-stream Provider、Redis admission、专用配置字段和停用数据库
   Provider；历史 Speech、AudioAsset 和 WAV 保持可读。
3. 删除专用 benchmark/mock/迁移脚本，保留一份只读历史归档与事故索引。
4. 运行完整后端、前端、引擎、MOSS、LiveKit 和浏览器音频发布门，生成新的可靠基线。
5. 只有新基线和生产 canary 均通过后才发布；失败时回滚代码与配置，不修改历史数据。

需要重点清理的代码区域：

- `apps/api/app/services/lighttts_admission.py`
- `apps/api/app/services/voice_runtime/lighttts.py`
- `apps/api/app/services/providers.py` 中的 LightTTS 分支
- `apps/api/app/core/config.py` 中的 `LIGHTTTS_*` 配置
- `apps/api/app/services/match_engine.py` 中的 LightTTS 重试命名和逻辑
- `deploy/verify_lighttts_*`、`scripts/*lighttts*` 及对应测试
- Admin/Control 页面中的 LightTTS 专属状态和文案

删除时必须把通用能力保留下来：TTS endpoint 池、排队、generation、归档、LiveKit publisher、健康检查和房间隔离应改名为通用 MOSS 语义，而不是随 LightTTS 一起删除。

## 5. 凭据清理

- README、示例配置、测试报告和发布包不得包含 SSH 密码、API Key、Cookie、数据库密码或 LiveKit secret。
- 所有已经进入明文文件或终端记录的凭据都视为已泄露，迁移前轮换。
- 新服务器使用权限为 `0600` 的 secret 文件或系统密钥服务；Supervisor 只引用路径。
- `.env` 不进入 Git、不进入源码包；只提供无真实值的 `.env.example`。
- 迁移包生成后运行 secret scan，发现高置信秘密立即失败。

## 6. 迁移步骤

1. [x] 新服务器从独立 V2 与 Debate Agent 目录启动，不复制旧依赖目录。
2. [x] PostgreSQL、Redis、LiveKit、MOSS、FunASR、API、Engine、Worker 和 Web 已运行。
3. [x] 根路径已切换到 `117.50.192.216`，旧服务器不再是生产入口。
4. [x] 完整源码、数据库、Supervisor、Nginx、Web release 和可靠音频指纹已制作恢复备份。
5. [x] README、实时 Provider 策略和 Computer Use 测试提示已改为 MOSS-only。
6. [x] 增加 `scripts/verify_moss_only_production.py`，拒绝重新启用旧 TTS 服务或流式开关。
7. [x] 删除教师/课堂专用页面、API、服务、脚本和 11 张数据库表；历史比赛数据保持可读。
8. [x] 建立无父提交、无密钥、无数据库、无音频和无模型的 GitHub 生产快照分支，纳入
   当前 `v2/` 与 `debate-agent/` 源码。
9. [ ] 在下一次音频发布窗口删除受保护的 LightTTS 兼容代码并生成新可靠基线。
10. [x] QA Markdown 报告保留用于追溯；截图、视频、WAV 和其他大文件由服务器数据备份
    保存，GitHub 快照明确排除。

## 7. 完成标准

- 新服务器可仅凭 Git 源码、锁文件、secret 文件和数据备份完整重建。
- 源码包不包含依赖目录、构建产物、测试数据库、运行音频、历史 QA 大文件和明文秘密。
- 旧 V1 与 LightTTS/CosyVoice 不再参与新比赛运行。
- 数据库、比赛音频、归档、Agent 配置和 8 个 MOSS 音色均有数量与 SHA-256 对账。
- 回滚只切换入口，不覆盖新数据库，也不把新写入反向同步到旧库。
