# 稷下辩论平台

全新实现的自动化人机辩论平台。产品只围绕赛事、房间、比赛、参赛者、观众和比赛历史展开，提供赛事大厅、账号体系、房间席位、自动比赛引擎、公开观战、排行榜、个人历史和系统管理后台。

## 唯一官方文档

- [`docs/official/system.html`](docs/official/system.html)：系统架构、模块、接口、部署维护和单场故障处置。
- [`docs/official/user.html`](docs/official/user.html)：注册、建房、参赛、观战、结果和异常恢复。

这两份 HTML 是唯一规范性文档。`docs/qa`、`docs/incidents` 及其他 Markdown 文件仅作为测试证据、事故记录或历史方案；若实现发生变化，必须在同一变更中更新对应官方文档。

## 目录

```text
apps/api/      FastAPI API、数据库、WebSocket、服务适配器
apps/web/      Next.js 前端
apps/engine/   独立比赛引擎进程入口
apps/worker/   Dramatiq 异步任务入口
deploy/        Docker Compose、Supervisor、Nginx 部署文件
scripts/       初始化、迁移和验收脚本
```

## 本地启动

```bash
cp .env.example .env
python3 -m venv .venv
. .venv/bin/activate
pip install -r apps/api/requirements.txt
npm --prefix apps/web install

# 本地默认使用 SQLite；生产设置 PostgreSQL DATABASE_URL
PYTHONPATH=apps/api uvicorn app.main:app --reload --port 8200
npm --prefix apps/web run dev
```

前端默认运行在 `http://127.0.0.1:3200`，API 在 `http://127.0.0.1:8200`。首次启动会创建两个赛事；生产环境的系统管理员凭据必须通过服务器私密配置注入，不写入源码或公开文档。

## 生产服务

- PostgreSQL：权威业务数据
- Redis：房间广播、在线状态、控制租约和任务队列
- 独立辩手 Agent：生产通过同机 RESTful Gateway 调用 `POST /debate/api/debate`
- FunASR：默认 `ws://127.0.0.1:10095`
- MOSS-TTS-Realtime：当前唯一生产实时语音 Provider
- LiveKit：将连续 PCM 作为 WebRTC 音轨发布到辩手页和观战页

禁止接入 Qwen3 8B，部署不使用 6016 端口。

当前服务器的 FunASR 是本地轻量适配器，使用 `START` / PCM / `STOP` 命令协议；浏览器不能直接假设标准 FunASR JSON 握手，必须经过平台的 `/ws/rooms/:code/asr` 鉴权桥接。

## 检查

```bash
./scripts/quality.sh
```

质量门禁包含 Python 格式与静态检查、依赖漏洞检查、后端测试、前端组件/可访问性测试和 npm 审计。

本地 Python 低于 3.10 时，可让质量脚本使用独立的兼容虚拟环境：

```bash
PHDEBATE_PYTHON_BIN_DIR=/path/to/python-3.10-venv/bin ./scripts/quality.sh
```

一次性浏览器环境会使用临时 SQLite 数据库，验证桌面与手机端的双用户房间大厅，以及系统管理员全部后台栏目；结束后自动删除测试用户和房间数据：

```bash
PHDEBATE_TEST_PYTHON=/path/to/python-3.10-venv/bin ./deploy/verify-browser-lobby.sh
```

多房间状态机稳定性可用重复压力门禁验证；该流程使用测试数据库和模拟服务，不会调用外部 Agent：

```bash
PHDEBATE_TEST_PYTHON=/path/to/python-3.10-venv/bin \
PHDEBATE_SOAK_CYCLES=10 \
  ./deploy/verify-engine-soak.sh
```

只读观战压力测试（不会创建或修改比赛；根路径切换后使用站点根地址）：

```bash
.venv/bin/python scripts/load_watchers.py \
  --base-url https://117.50.192.216 \
  --rooms 123456 \
  --clients 5 \
  --handshake-concurrency 5 \
  --duration 10 \
  --allow-public-load
```

远程压测必须显式传入 `--allow-public-load`；系统最多同时开放 5 个房间，所有房间合计最多分配 5 个观战连接。
脚本先只读校验全部房间，再分批完成握手并同时保持所有连接，因此结果中的
`peak_connected` 才能证明真实并发连接数。只有使用临时自签证书的隔离环境才允许追加 `--insecure`，
生产证书测试不能关闭 TLS 校验。

模拟客户端在服务端发送初始快照时直接断开，用于回归 WebSocket 断连竞态：

```bash
.venv/bin/python scripts/verify_websocket_disconnect_storm.py \
  --base-url https://117.50.192.216 \
  --room 123456 \
  --clients 5
```

该脚本同样遵守全站合计最多 5 个观战连接的产品限制。生产证书测试不得使用 `--insecure`；该参数只用于
临时自签证书的隔离环境。

生产前端使用 Node.js 24 LTS 独立运行时。房间计时和状态扫描彼此独立，不会因为单个外部服务调用变慢而停止其他房间；Agent 与裁判调用默认全局最多并发 8 个。比赛只保存文字记录，不提供真人录音上传或比赛音频归档入口。

实时语音在生产环境只使用 MOSS-Realtime：Agent 正文 delta 进入同一轮双向 WebSocket，
连续 PCM 在服务器内部交给 LiveKit，再以 Opus 发送到浏览器。一轮固定一个 voice、session
和 generation，不允许中途拼接或静默切换。LightTTS、CosyVoice 与火山 TTS 仅保留历史
证据，不是生产回退。整体策略见
[`docs/realtime-voice-provider-strategy.md`](docs/realtime-voice-provider-strategy.md)，实现与验收见
[`docs/realtime-voice-rebuild.md`](docs/realtime-voice-rebuild.md)。

会创建临时数据并在 `finally` 中清理的生产隔离验收：

```bash
PYTHONPATH=apps/api .venv/bin/python scripts/verify_parallel_match_isolation.py \
  --base-url https://127.0.0.1

PYTHONPATH=apps/api .venv/bin/python scripts/verify_asr_bridge.py \
  --base-url https://127.0.0.1

PYTHONPATH=apps/api .venv/bin/python scripts/verify_tts_asr_roundtrip.py \
  --voices debate_voice_1,debate_voice_2,debate_voice_3,debate_voice_4

PYTHONPATH=apps/api .venv/bin/python scripts/verify_season_lifecycle.py

PYTHONPATH=apps/api .venv/bin/python deploy/verify_presence_recovery.py

PYTHONPATH=apps/api .venv/bin/python scripts/verify_parallel_control_recovery.py \
  --base-url https://117.50.192.216
```

多场真实语音验收必须在隔离环境执行，并使用新服务器的 MOSS endpoint。旧的 `verify_real_voice_multi_match.py` 仍包含 LightTTS 历史假设，只保留为迁移参考，不再作为 MOSS 发布证据。MOSS 正式门使用：

```bash
.venv/bin/python scripts/benchmark_moss_realtime_sessions.py \
  --endpoint http://127.0.0.1:8890 \
  --concurrency 1 --rounds 20

.venv/bin/python scripts/benchmark_moss_realtime_interrupt.py \
  --endpoint http://127.0.0.1:8890
```

生产前端必须通过以下脚本构建。脚本会加载服务器 `.env` 并校验部署模式对应的 base path，防止直接执行 `npm run build` 生成无法从反向代理路径访问的产物：

```bash
sudo ./deploy/prepare-runtime.sh
./deploy/build-web-production.sh
```

正式环境只构建根路径 release。新发布先生成独立不可变 release，通过健康门禁后再由 `activate-web-release.sh` 原子切换 `.web-current`；失败会自动恢复上一 release：

```bash
sudo ./deploy/prepare-runtime.sh
PHDEBATE_WEB_DEPLOYMENT_MODE=root ./deploy/build-web-release.sh
./deploy/build-transcript-collab-release.sh
./deploy/activate-web-release.sh <release-name>
```

API/Web release 可以滚动发布；比赛引擎和 worker 不得依据赛事列表判断是否可以重启。重启前必须直接检查数据库中的 `preparing/running/paused/judging` 房间：

```bash
./deploy/restart-engine-workers.sh
```

只要存在任一活动比赛，脚本就以非零状态退出且不会调用 Supervisor。不要绕过该门禁；等待比赛完成或由房主明确终止后再重启。

根路径 Nginx 位置模板位于 `deploy/nginx-root.locations.conf`；生产不提供版本化 URL 或平行前端入口。

Python 运行依赖升级必须使用版本化虚拟环境。脚本会先在独立端口完成影子启动与就绪检查，再原子切换 `.venv` 链接；API、比赛引擎或 worker 启动失败时会自动恢复上一版本：

```bash
sudo ./deploy/upgrade-python-runtime.sh
```

部署初始化和运行时升级都会验证应用用户与 PostgreSQL 对各自目录的访问权限。也可以单独执行：

```bash
sudo ./deploy/verify-runtime-permissions.sh
```

API 与比赛引擎由 `wait-for-postgres.sh` 启动：服务器重启或数据库短暂维护时，进程会等待 PostgreSQL 恢复再启动应用，不会因为耗尽 Supervisor 重试而永久进入 `FATAL`。

仅构建并验证候选环境、不切换服务时：

```bash
sudo PHDEBATE_SKIP_SWITCH=true ./deploy/upgrade-python-runtime.sh
```

比赛完成、终止、进入人工复核或管理员修正赛果后，worker 会在 `storage/archives` 生成完整 JSON 归档、元数据和 SHA-256 校验文件。参赛者、房主和系统管理员也可从结果页按需生成并下载；运行目录必须由服务账号持有，因此每次新部署或迁移数据卷后应先执行 `prepare-runtime.sh`。

生产 PostgreSQL 由 Supervisor 中的 `jixia-backup` 每天北京时间 03:17 自动备份。备份使用自定义归档格式、原子写入、SHA-256 校验和与 14 天保留策略。手工备份及恢复演练：

```bash
sudo ./deploy/backup-database.sh
sudo ./deploy/verify-backup-restore.sh
```
