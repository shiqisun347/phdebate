# 稷下辩论平台：服务器备份与完整恢复说明

本文采用“GitHub 只保存源码，新服务器保存私密运行数据”的策略。Mac 不保存数据库、模型、
生产配置、学生音频或服务器归档。

## 1. 恢复材料

### GitHub 源码

- 仓库：`https://github.com/shiqisun347/phdebate`
- 快照分支：`backup/production-20260719-round7`
- 当前已部署应用提交：`cef6e572fd4f955fbe28f321017886889caafb68`；完整恢复应以服务器
  `recovery-set-*.manifest` 中的 `code_commit` 为准，确保同时取得部署工具和恢复文档更新。
- 分支必须是 orphan 快照，不继承旧 `main` 历史。
- 恢复时应使用服务器恢复清单记录的 commit SHA，不只依赖可移动分支名。

GitHub 快照不得包含 `.env`、密码、API Key、Cookie、数据库、模型、音频、证书私钥、
运行日志或测试视频。当前 GitHub 仓库是公开仓库，任何推送内容都按公开源码处理。

### 生产服务器私密备份

备份根目录：

```text
/home/ubuntu/sunsq/phdebate-v2/runtime/deploy-backups
```

当前已校验材料：

| 内容 | 文件 | SHA-256 |
| --- | --- | --- |
| V2 数据库 | `runtime/backups/auto-20260719T135612Z.dump` | `b8a12bb33b09f647a1484bc18c2c92edc265fe3d23cf93d12560e62b6eaa26cf` |
| 比赛数据卷 | `20260719T135612Z-data-volumes.tar.gz` | `fa75c3726b8232ec4728132e8506e0265eb34a328a9587649257efaecdcbfc2d` |
| 私密配置 | `20260719T094349Z-full-private-config.tar.gz` | `f5a0be379496ae7f7ea3d2575ec3d61c5d3764de4f12946f3ea6c163225f29b0` |
| 可靠语音清单 | `20260719T091423Z-reliable-voice-runtime.tar.gz` | `6f80f2a43a9fee22316a3d2adf49d54d8e75d0f57b6262f6a9d42f62fbb9ddc9` |
| MOSS 离线目录 | `20260719-openmoss-offline.tar` | `bec995daf334694aa7dd48b9cf603bd5f2f7f3e7d8527660cafcc8136ee456d8` |
| Debate Agent 数据库 | `/home/ubuntu/sunsq/debate-agent/backups/agent-20260719T135643Z.dump` | `1dcc33aee9c14949ac53b480d809d1b5b3c5fd709e5f4552ec7e79916b2f3967` |

`data-volumes` 包含 `storage`、MOSS prompt 资产和可靠音频基线；MOSS 离线包约 11GB。
所有私密文件应为 `0600`，目录应为 `0700`。

这些文件如果只放在同一块生产磁盘上，不构成磁盘灾难备份。应再复制到另一台受控服务器或
加密对象存储，并在目标端重新计算 SHA-256。禁止复制到 Mac 或公开 GitHub。

## 2. 当前恢复能力边界

源码、两套 PostgreSQL 数据、比赛数据卷、MOSS 模型、Prompt、生产 `.env` 和 Supervisor
配置已经有备份。完整恢复还必须准备并核对：

- FunASR 程序、模型、Python 环境和 Supervisor 配置。
- LiveKit 二进制、YAML、API Key/Secret、端口和 ICE 配置。
- Nginx 当前配置；新 IP 的 HTTPS 证书应重新签发，不复用旧 IP 证书。
- PostgreSQL 角色、密码、`pg_hba.conf` 和 Debate Agent 所需的 pgvector。
- 与 MOSS `pip-freeze` 匹配的 CUDA 12.8/PyTorch wheel 来源。

在真正的空 Ubuntu 服务器完成一次恢复演练前，本手册是“可执行恢复流程”，不是已经完成的
灾难恢复证明。演练必须保存命令日志、服务状态和业务验收报告。

## 3. 源服务器校验

校验文件中的路径是相对校验文件所在目录生成的，必须进入对应目录执行：

```bash
cd /home/ubuntu/sunsq/phdebate-v2/runtime/backups
sha256sum -c auto-20260719T135612Z.dump.sha256

cd /home/ubuntu/sunsq/phdebate-v2/runtime/deploy-backups
sha256sum -c 20260719T135612Z-data-volumes.tar.gz.sha256
sha256sum -c 20260719T094349Z-full-private-config.tar.gz.sha256
sha256sum -c 20260719T091423Z-reliable-voice-runtime.tar.gz.sha256
sha256sum -c 20260719-openmoss-offline.tar.sha256

cd /home/ubuntu/sunsq/debate-agent/backups
sha256sum -c agent-20260719T135643Z.dump.sha256
pg_restore -l agent-20260719T135643Z.dump >/dev/null
```

数据卷还必须完成一次隔离解包和逐文件校验，不能只验证外层压缩包：

```bash
cd /home/ubuntu/sunsq/phdebate-v2
./deploy/verify-data-volume-backup.sh \
  runtime/deploy-backups/20260719T135612Z-data-volumes.tar.gz
```

V2 数据库还应完成临时数据库恢复验证：

```bash
cd /home/ubuntu/sunsq/phdebate-v2
./deploy/verify-backup-restore.sh runtime/backups/auto-20260719T135612Z.dump
```

## 4. 准备空服务器

目标基线：Ubuntu 22.04、RTX 3090 24GB、Driver 595.80、Python 3.12.11、Node 22.23.1、
PostgreSQL 14、Redis 6、Nginx 1.18、Supervisor 4.2.1。

先安装 GPU 驱动并重启。Ubuntu 22.04 默认源没有 Python 3.12，不能直接假设
`apt install python3.12` 成功；应使用经过审核的软件源或源码构建，并确认版本：

```bash
python3.12 --version
node --version
nvidia-smi
```

安装基础依赖和 pgvector 后，停用不使用的默认 PostgreSQL/Redis 服务，避免与 Supervisor
管理的 5433/6380 实例冲突：

```bash
apt-get update
apt-get install -y git rsync curl ca-certificates nginx supervisor \
  postgresql-14 postgresql-client-14 postgresql-14-pgvector redis-server ffmpeg
systemctl disable --now postgresql redis-server || true

id moss >/dev/null 2>&1 || useradd --system --home /opt/OpenMOSS --shell /usr/sbin/nologin moss
install -d -o ubuntu -g ubuntu -m 0750 /home/ubuntu/sunsq
install -d -o moss -g moss -m 0750 /opt/OpenMOSS /opt/phdebate/moss-prompts
install -d -o root -g root -m 0700 /opt/phdebate/secrets
```

## 5. 恢复源码

从恢复清单取得不可变 commit SHA：

```bash
EXPECTED_CODE_COMMIT='从服务器 recovery-set manifest 读取'
git clone --branch backup/production-20260719-round7 --single-branch \
  https://github.com/shiqisun347/phdebate.git \
  /home/ubuntu/sunsq/phdebate-source
test "$(git -C /home/ubuntu/sunsq/phdebate-source rev-parse HEAD)" = "$EXPECTED_CODE_COMMIT"

PHDEBATE_V2_ROOT=/home/ubuntu/sunsq/phdebate-v2 \
  /home/ubuntu/sunsq/phdebate-source/v2/deploy/sync-github-source.sh \
  /home/ubuntu/sunsq/phdebate-source apply
rsync -a /home/ubuntu/sunsq/phdebate-source/debate-agent/ /home/ubuntu/sunsq/debate-agent/
chown -R ubuntu:ubuntu /home/ubuntu/sunsq/phdebate-v2 /home/ubuntu/sunsq/debate-agent
```

## 6. 初始化 PostgreSQL 5433

Supervisor 期望数据目录为 `runtime/postgres`，空服务器必须先执行 `initdb`：

```bash
install -d -o root -g root -m 0751 /home/ubuntu/sunsq/phdebate-v2/runtime
install -d -o postgres -g postgres -m 0700 /home/ubuntu/sunsq/phdebate-v2/runtime/postgres
runuser -u postgres -- /usr/lib/postgresql/14/bin/initdb \
  -D /home/ubuntu/sunsq/phdebate-v2/runtime/postgres \
  --encoding=UTF8 --locale=C.UTF-8
```

按恢复后的 `.env` 创建 V2 和 Debate Agent 角色、设置密码、配置本机 TCP 认证，并在 Agent
数据库执行：

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

不要把密码直接写在 shell history。先只安装并启动 `jixia-v2-postgres`，确认
`pg_isready -h 127.0.0.1 -p 5433` 后再恢复数据库。

## 7. 安全恢复私密配置

不要直接把未知 tar 覆盖到 `/`。先在隔离目录预览：

```bash
install -d -m 0700 /root/phdebate-private-restore
tar -tzf 20260719T094349Z-full-private-config.tar.gz
tar -xzf 20260719T094349Z-full-private-config.tar.gz \
  -C /root/phdebate-private-restore
```

确认归档中只有两套 `.env`、Supervisor 配置和 `/opt/phdebate/secrets` 后，逐项 `install`
到目标路径。V2/Agent `.env` 应由 `ubuntu:ubuntu` 持有且为 `0600`；MOSS 运行密钥应由实际
读取它的服务账号持有。目录用 `0700/0750`，不要把目录 chmod 为 `0600`。

更换 IP 时必须更新 `.env`、Nginx、LiveKit URL、Agent/Judge Endpoint，并审计数据库中的
ProviderConfig、JudgeProfile 和活动比赛 service snapshot。迁移前应停止新建比赛并等待活动
比赛结束，避免继续调用旧 IP。

## 8. 恢复数据库

数据库、角色和扩展准备完成后：

```bash
runuser -u postgres -- createdb -p 5433 phdebate_v2
runuser -u postgres -- pg_restore -p 5433 --exit-on-error \
  --no-owner --no-privileges -d phdebate_v2 auto-20260719T135612Z.dump

PGPASSWORD='从安全配置临时读取' pg_restore \
  -h 127.0.0.1 -p 5433 -U debate_agent --exit-on-error \
  --no-owner --no-privileges -d debate_agent agent-20260719T135643Z.dump
```

恢复后比较迁移版本，不能只执行 `current`：

```bash
cd /home/ubuntu/sunsq/phdebate-v2
set -a; source .env; set +a
PYTHONPATH=apps/api .venv/bin/alembic -c apps/api/alembic.ini current
PYTHONPATH=apps/api .venv/bin/alembic -c apps/api/alembic.ini heads
```

## 9. 恢复数据卷和 MOSS

```bash
cd /home/ubuntu/sunsq/phdebate-v2
tar -xzf 20260719T135612Z-data-volumes.tar.gz
sha256sum -c files.sha256
./deploy/prepare-runtime.sh

tar -xf 20260719-openmoss-offline.tar -C /opt
chown -R moss:moss /opt/OpenMOSS

install -d -m 0700 /tmp/reliable-voice-runtime
tar -xzf 20260719T091423Z-reliable-voice-runtime.tar.gz \
  -C /tmp/reliable-voice-runtime
cd /opt/OpenMOSS
sha256sum -c /tmp/reliable-voice-runtime/openmoss-files.sha256
cd /opt/phdebate/moss-prompts
sha256sum -c /tmp/reliable-voice-runtime/prompt-files.sha256
```

MOSS Python 环境必须使用清单对应的 CUDA 12.8/PyTorch wheel 和固定 OpenMOSS revision。
`pip-freeze.txt` 可能包含本地 editable 路径，不能盲目执行后就宣称恢复成功；应先恢复相同路径，
再安装并执行 `pip check`、GPU preflight 和真实语音门禁。

源码同步不得直接对生产目录执行无排除项的 `rsync --delete`。必须使用
`deploy/sync-github-source.sh`；它会保留 `.env`、数据卷、运行目录、API/Web release 链接、
所有 `.venv*`（包括 MOSS CUDA 环境）、Node 构建产物和服务器端 Prompt 音频。正式同步前先用
`dry-run` 检查变更列表。

## 10. 构建并建立发布链接

V2：

```bash
cd /home/ubuntu/sunsq/phdebate-v2
python3.12 -m venv .python-venvs/restore-20260719
.python-venvs/restore-20260719/bin/pip install -r apps/api/requirements.txt
ln -sfn .python-venvs/restore-20260719 .venv

install -d -m 0750 runtime/node/bin
ln -sfn "$(command -v node)" runtime/node/bin/node

npm --prefix apps/web ci
PHDEBATE_V2_API_RELEASE=restore-20260719 ./deploy/build-api-release.sh
PHDEBATE_V2_WEB_DEPLOYMENT_MODE=root \
PHDEBATE_V2_RELEASE=restore-20260719 ./deploy/build-web-release.sh

ln -sfn "$(pwd)/runtime/api-releases/restore-20260719" .api-primary
ln -sfn "$(pwd)/runtime/api-releases/restore-20260719" .api-secondary
ln -sfn "$(pwd)/runtime/web-releases/restore-20260719" .web-current
```

实际 release 目录名以构建脚本输出为准，创建链接前用
`find runtime/api-releases runtime/web-releases -maxdepth 1` 确认。不要猜目录。

新版本连续通过健康检查并完成服务器备份后，可以预览旧 release 清理范围：

```bash
./deploy/prune-releases.sh dry-run
PHDEBATE_RELEASE_KEEP=5 PHDEBATE_RELEASE_MIN_AGE_HOURS=24 \
  ./deploy/prune-releases.sh apply
```

工具始终保留当前 API primary/secondary、当前 Web、最近五个版本、24 小时内版本以及无法确认
完整性的人工目录。不得直接对 `runtime/*-releases` 执行通配符删除。

Debate Agent：

```bash
cd /home/ubuntu/sunsq/debate-agent
python3.12 -m venv .venv
.venv/bin/pip install -r apps/api/requirements.txt
npm --prefix apps/web ci
npm --prefix apps/web run build

install -d -m 0750 runtime/logs .web-releases/restore-20260719
cp -a apps/web/.next/standalone/. .web-releases/restore-20260719/
install -d .web-releases/restore-20260719/.next
cp -a apps/web/.next/static .web-releases/restore-20260719/.next/static
ln -sfn "$(pwd)/.web-releases/restore-20260719" .web-current
```

## 11. FunASR、LiveKit、Nginx 和证书

恢复 FunASR 和 LiveKit 的固定二进制、模型、配置和密钥后，先在本机端口验收。LiveKit 必须
开放并核对 HTTPS/WSS、TCP 和 UDP ICE 端口。Nginx 配置从源码模板重新安装并替换新 IP。

新 IP 使用 Certbot 的 IP short-lived profile 重新签发证书并配置高频续期。不要恢复旧 IP 的
证书私钥作为新 IP 正式证书。

## 12. 分阶段启动

不要直接 `supervisorctl update` 后让所有服务同时自动启动。按顺序逐项安装配置并启动：

1. PostgreSQL 5433、Redis 6380。
2. Debate Agent API/Web。
3. FunASR、LiveKit。
4. MOSS-Realtime，等待 authenticated readiness。
5. V2 API primary/secondary。
6. Match Engine、Worker、V2 Web。
7. Nginx HTTPS。

每一步先检查日志和本机健康接口，再进入下一步。

## 13. 恢复验收

所有验证命令必须显式传入新服务器地址和本次 QA 房间，禁止使用脚本默认旧 IP：

```bash
cd /home/ubuntu/sunsq/phdebate-v2
python3 scripts/create_reliable_audio_manifest.py --verify \
  backups/reliable-audio-20260719-declick/reliable-audio-baseline.json

./deploy/openmoss/test-static.sh
.venv/bin/python deploy/verify_gpu_voice_runtime.py
curl -fsS https://新服务器IP/api/health
curl -fsS https://新服务器IP/api/health/ready
curl -fsS https://新服务器IP/debate/api/health
```

随后创建专用 QA 数据，至少完成：

- 1v1 人人、1v1 人机、4v4 四真人加四 AI。
- 四个房间并行推进，WebSocket、字幕、Agent、TTS、ASR 不串房。
- 同席位双设备接管，旧设备写操作返回 409。
- Agent 首正文到浏览器首音小于 3 秒，声音连续且打断后队列清空。
- 服务重启、断线恢复、AI 接替、结果、排行榜、历史和归档下载。
- V2 与 Debate Agent 数据库临时恢复及逐表计数。

## 14. Mac 清理与日常备份原则

- Mac 只保留工作源码，不保留服务器 dump、tar、模型和私密配置。
- 临时浏览器测试、Git alternate index 和下载目录在验收后删除；不得以“方便回滚”为由在
  Mac 留存生产压缩包副本。代码回滚点只使用 GitHub 快照分支。
- 源码备份更新到 GitHub orphan 分支前必须执行 ignore 检查、密钥扫描和大文件扫描。
- V2 DB 每日备份不足以构成完整恢复；Agent DB、数据卷和私密配置也要进入同一恢复批次。
- MOSS/FunASR 模型不需每天复制，但每次 revision、模型或依赖变化后必须重新生成离线包。
- 每个恢复批次应生成一个只读 manifest，绑定源码 commit、所有文件 SHA-256、数据库 Alembic
  版本、GPU 指纹和创建时间。

## 15. 回滚

- 代码回滚只切换 `.api-primary`、`.api-secondary`、`.web-current`。
- 数据库恢复到新数据库核验后再切换连接，不覆盖原备份。
- 语音回滚只恢复固定 revision、Prompt、模型和参数，不自动切回 LightTTS。
- 新服务器验收失败时保持旧服务器只读，禁止两个生产数据库同时写入。
