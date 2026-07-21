# 稷下辩论平台：服务器备份与完整恢复说明

本文采用“GitHub 只保存源码，新服务器保存私密运行数据”的唯一恢复策略。Mac 不是备份介质，
不保存数据库、模型、生产配置、学生音频、恢复清单或服务器归档。恢复操作在 Linux 服务器之间
完成；源码从 GitHub 取得，私密数据从受控服务器或加密对象存储取得。

## 0. 生产拓扑与恢复原则

当前生产不是 Docker Compose：平台、Debate Agent、PostgreSQL 5433、Redis 6380、MOSS、Nginx
均由 Supervisor 管理。仓库中的 Compose 只用于开发，灾备时不得与 Supervisor 混用，否则会造成
端口、数据库和数据卷冲突。

固定恢复顺序为：

1. 校验 GitHub commit 和私密恢复批次；
2. 安装系统依赖，恢复私密配置但不启动业务；
3. 初始化 PostgreSQL/Redis，恢复 平台 与 Agent 两个数据库；
4. 隔离解包数据卷，验证逐文件哈希后再同步到正式目录；
5. 构建 平台 与 Agent release；
6. 恢复 FunASR、LiveKit、MOSS 依赖和配置；
7. 按依赖顺序启动 Supervisor 服务；
8. 安装 Nginx、重新签发新 IP 证书，最后开放流量；
9. 执行数据库、跨房、实时音频和浏览器恢复验收。

任何一步失败都停止推进，不覆盖旧服务器数据，也不允许新旧两套生产数据库同时写入。

## 1. 恢复材料

### GitHub 源码

- 仓库：`https://github.com/shiqisun347/phdebate`
- 快照分支和 commit 必须从本次 `recovery-set-*.manifest` 读取。文中的历史分支仅是示例，
  不得代替清单。
- 分支必须是 orphan 快照，不继承旧 `main` 历史。
- 恢复时应使用服务器恢复清单记录的 commit SHA，不只依赖可移动分支名。

GitHub 快照不得包含 `.env`、密码、API Key、Cookie、数据库、模型、音频、证书私钥、
运行日志或测试视频。当前 GitHub 仓库是公开仓库，任何推送内容都按公开源码处理。

### 生产服务器私密备份

备份根目录：

```text
/home/ubuntu/sunsq/phdebate/runtime/deploy-backups
```

以下是 2026-07-20 唯一平台版本切换后已完成恢复演练的当前批次。后续若生成更新批次，仍必须以人工确认的
同一份 `recovery-set-*.manifest` 为准，不得把不同批次的“最新文件”临时拼在一起：

本轮绑定清单为
`runtime/deploy-backups/recovery-set-20260720T1613Z-round18.manifest`，权限为 `0600`。源码位于
GitHub 分支 `backup/production-20260720-round12`，当前部署应用提交为
`95242c81f25106e1a88d3d75e2c0925d9405c2ec`；恢复时仍应读取清单，不能手工抄写该 SHA。

| 内容 | 文件 | SHA-256 |
| --- | --- | --- |
| 平台数据库 | `runtime/backups/auto-20260720T161223Z.dump` | `44a0dcef2abf6b57ce9be78e89c049533c5445ed729733fae6e7844477693788` |
| 比赛数据卷 | `20260720T1505Z-round16-data-volumes.tar.gz` | `bbb5808c320ebc95fa2e76644f386060d7b3be0b2b0aeb558de8a05871a2ee36` |
| 私密配置 | `20260720T1506Z-round16-private-config.tar.gz` | `099a0d1ad28ada43760e2e9a68626fc202233da0fafb9bb1ce0be9ce537f4646` |
| 可靠语音清单 | `20260719T091423Z-reliable-voice-runtime.tar.gz` | `6f80f2a43a9fee22316a3d2adf49d54d8e75d0f57b6262f6a9d42f62fbb9ddc9` |
| MOSS 离线目录 | `20260719-openmoss-offline.tar` | `bec995daf334694aa7dd48b9cf603bd5f2f7f3e7d8527660cafcc8136ee456d8` |
| Debate Agent 数据库 | `/home/ubuntu/sunsq/debate-agent/backups/agent-20260720T150501Z.dump` | `602016b40e0bd73218b339d3b0c9460551235ba36ddf8de621419b643ffa9562` |

`data-volumes` 包含 `storage`、MOSS prompt 资产和可靠音频基线；MOSS 离线包约 11GB。
所有私密文件应为 `0600`，目录应为 `0700`。

这些文件如果只放在同一块生产磁盘上，不构成磁盘灾难备份。应再复制到另一台受控服务器或
加密对象存储，并在目标端重新计算 SHA-256。禁止复制到 Mac 或公开 GitHub。

## 2. 当前恢复能力边界

源码、两套 PostgreSQL 数据、比赛数据卷、MOSS 模型、Prompt、生产 `.env` 和 Supervisor
配置已有恢复材料。以下外部运行时如果未进入同一私密恢复批次，就不能宣称“完整恢复已验证”：

- FunASR 程序、模型、Python 环境和 Supervisor 配置。
- LiveKit 二进制、YAML、API Key/Secret、端口和 ICE 配置。
- Nginx 当前配置；新 IP 的 HTTPS 证书应重新签发，不复用旧 IP 证书。
- PostgreSQL 角色、密码、`pg_hba.conf` 和 Debate Agent 所需的 pgvector。
- 与 MOSS `pip-freeze` 匹配的 CUDA 12.8/PyTorch wheel 来源。

FunASR 模型、LiveKit 密钥和私密配置不得进入 GitHub。它们应进入新服务器的私密归档或受控
对象存储，并由恢复清单的后续版本绑定。若选择重新下载安装，必须记录版本、来源和文件哈希。

在真正的空 Ubuntu 服务器完成一次恢复演练前，本手册只是可执行流程，不是灾难恢复证明。
演练证据只存新服务器的受限目录或受控审计系统，不下载到 Mac。

## 3. 源服务器校验

schema 3 清单不记录源服务器绝对路径，可随恢复材料移动。把清单和六个归档复制到新服务器的
受限目录并完成第 5 节源码 checkout 后执行：

```bash
/home/ubuntu/sunsq/phdebate-source/platform/deploy/verify-recovery-manifest.sh \
  /srv/phdebate-recovery/recovery-set-本批次.manifest \
  /srv/phdebate-recovery
```

必须看到六个 `artifact_verified`。清单自身的 SHA-256 应通过独立的受控通道核对；不能把清单
和它的哈希旁文件只放在同一介质上，便宣称具备防篡改能力。

校验文件中的路径是相对校验文件所在目录生成的，必须进入对应目录执行：

```bash
cd /home/ubuntu/sunsq/phdebate/runtime/backups
sha256sum -c auto-20260720T161223Z.dump.sha256

cd /home/ubuntu/sunsq/phdebate/runtime/deploy-backups
sha256sum -c 20260720T1505Z-round16-data-volumes.tar.gz.sha256
sha256sum -c 20260720T1506Z-round16-private-config.tar.gz.sha256
sha256sum -c 20260719T091423Z-reliable-voice-runtime.tar.gz.sha256
sha256sum -c 20260719-openmoss-offline.tar.sha256

cd /home/ubuntu/sunsq/debate-agent/backups
sha256sum -c agent-20260720T150501Z.dump.sha256
pg_restore -l agent-20260720T150501Z.dump >/dev/null
```

数据卷还必须完成一次隔离解包和逐文件校验，不能只验证外层压缩包：

```bash
cd /home/ubuntu/sunsq/phdebate
./deploy/verify-data-volume-backup.sh \
  runtime/deploy-backups/20260720T1505Z-round16-data-volumes.tar.gz
```

平台 数据库还应完成临时数据库恢复验证：

```bash
cd /home/ubuntu/sunsq/phdebate
./deploy/verify-backup-restore.sh runtime/backups/auto-20260720T161223Z.dump
```

Debate Agent 的 dump 包含 pgvector 扩展定义。空服务器还原时先以 PostgreSQL 超级用户安装
`vector` 扩展，并由超级用户执行首次恢复；不要为了让 `postgres` 读取备份而放宽长期备份文件
权限。可以把单个 dump 临时复制到仅 `postgres` 可读的 `/tmp` 文件，恢复完成后立即删除：

```bash
install -o postgres -g postgres -m 600 \
  /home/ubuntu/sunsq/debate-agent/backups/agent-20260720T150501Z.dump \
  /tmp/debate-agent-restore.dump
runuser -u postgres -- createdb -p 5433 -O debate_agent debate_agent_restore_check
runuser -u postgres -- pg_restore -p 5433 --exit-on-error \
  --no-owner --no-privileges -d debate_agent_restore_check \
  /tmp/debate-agent-restore.dump
find /tmp -maxdepth 1 -type f -name debate-agent-restore.dump -delete
```

本轮实际演练结果为：平台 25 张表、Alembic `0027_speech_data_disposition`；Debate Agent 13 张表、
Alembic `0001_initial`；数据卷 396 个文件。演练使用独立临时数据库，没有覆盖生产数据库。

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
RECOVERY_MANIFEST=/srv/phdebate-recovery/recovery-set-本批次.manifest
EXPECTED_CODE_BRANCH="$(awk -F= '$1=="code_branch" {print substr($0,index($0,"=")+1)}' "$RECOVERY_MANIFEST")"
EXPECTED_CODE_COMMIT="$(awk -F= '$1=="code_commit" {print $2}' "$RECOVERY_MANIFEST")"
test -n "$EXPECTED_CODE_BRANCH" && test -n "$EXPECTED_CODE_COMMIT"
git clone --branch "$EXPECTED_CODE_BRANCH" --single-branch \
  https://github.com/shiqisun347/phdebate.git \
  /home/ubuntu/sunsq/phdebate-source
test "$(git -C /home/ubuntu/sunsq/phdebate-source rev-parse HEAD)" = "$EXPECTED_CODE_COMMIT"

install -d -o ubuntu -g ubuntu -m 0750 \
  /home/ubuntu/sunsq/phdebate /home/ubuntu/sunsq/debate-agent
PHDEBATE_ROOT=/home/ubuntu/sunsq/phdebate \
  /home/ubuntu/sunsq/phdebate-source/platform/deploy/sync-github-source.sh \
  /home/ubuntu/sunsq/phdebate-source dry-run
PHDEBATE_ROOT=/home/ubuntu/sunsq/phdebate \
  /home/ubuntu/sunsq/phdebate-source/platform/deploy/sync-github-source.sh \
  /home/ubuntu/sunsq/phdebate-source apply
rsync -a /home/ubuntu/sunsq/phdebate-source/debate-agent/ /home/ubuntu/sunsq/debate-agent/
chown -R ubuntu:ubuntu /home/ubuntu/sunsq/debate-agent
```

确认 dry-run 不会触碰私密目录后才执行紧随其后的 apply。

不要对整个 平台 根目录执行递归 `chown`：`runtime/postgres` 必须保持 `postgres:postgres`，MOSS
目录也有独立服务账号。运行目录权限由 `deploy/prepare-runtime.sh` 建立。

## 6. 恢复私密配置（仍不启动服务）

不要直接把未知 tar 覆盖到 `/`。先在隔离目录预览并验证没有绝对路径或 `..` 成员：

```bash
install -d -m 0700 /root/phdebate-private-restore
tar -tzf /srv/phdebate-recovery/私密配置归档.tar.gz
tar -tzf /srv/phdebate-recovery/私密配置归档.tar.gz \
  | awk '$0 ~ /^\// || $0 ~ /(^|\/)\.\.($|\/)/ {bad=1} END {exit bad}'
tar -xzf /srv/phdebate-recovery/私密配置归档.tar.gz \
  -C /root/phdebate-private-restore --no-same-owner --no-same-permissions
```

确认归档只包含两套 `.env`、Supervisor 私密配置和 `/opt/phdebate/secrets` 后，逐项用 `install`
写入目标路径。平台/Agent `.env` 为 `ubuntu:ubuntu 0600`；密钥文件按实际读取服务设定所有者。
目录使用 `0700/0750`，不要把目录 chmod 为 `0600`。不要在命令行、shell history、恢复日志或
本文中写密码。

更换 IP 时更新 `.env`、Nginx、LiveKit URL、Agent/Judge Endpoint，并审计数据库中的
ProviderConfig、JudgeProfile 和活动比赛 service snapshot。迁移窗口内停止新建比赛并等待活动
比赛结束。

## 7. 初始化 PostgreSQL 5433 与 Redis 6380

Supervisor 期望数据目录为 `runtime/postgres`，空服务器必须先执行 `initdb`：

```bash
install -d -o root -g root -m 0751 /home/ubuntu/sunsq/phdebate/runtime
install -d -o postgres -g postgres -m 0700 /home/ubuntu/sunsq/phdebate/runtime/postgres
runuser -u postgres -- /usr/lib/postgresql/14/bin/initdb \
  -D /home/ubuntu/sunsq/phdebate/runtime/postgres \
  --encoding=UTF8 --locale=C.UTF-8
```

根据恢复后的两套 `.env` 创建 平台 和 Debate Agent 的数据库、角色并设置密码。使用交互式
`psql` 的 `\password` 或权限为 `0600` 的临时 `.pgpass`，不要把密码放进 shell history。
Debate Agent 数据库还必须执行：

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

先只启动 PostgreSQL 和 Redis 并确认：

```bash
pg_isready -h 127.0.0.1 -p 5433
redis-cli -h 127.0.0.1 -p 6380 ping
```

## 8. 恢复数据库

数据库、角色和扩展准备完成后：

```bash
read -r -p '平台 database role: ' PLATFORM_DB_ROLE
read -r -p 'Agent database role: ' AGENT_DB_ROLE
test -n "$PLATFORM_DB_ROLE" && test -n "$AGENT_DB_ROLE"

runuser -u postgres -- createdb -p 5433 --owner="$PLATFORM_DB_ROLE" phdebate
runuser -u postgres -- pg_restore -p 5433 --exit-on-error \
  --no-owner --no-privileges --role="$PLATFORM_DB_ROLE" \
  -d phdebate /srv/phdebate-recovery/平台数据库.dump

runuser -u postgres -- createdb -p 5433 --owner="$AGENT_DB_ROLE" debate_agent
runuser -u postgres -- psql -p 5433 -d debate_agent \
  -c 'CREATE EXTENSION IF NOT EXISTS vector'
runuser -u postgres -- pg_restore -p 5433 --exit-on-error \
  --no-owner --no-privileges --role="$AGENT_DB_ROLE" \
  -d debate_agent /srv/phdebate-recovery/Agent数据库.dump
```

把示例中的角色名替换为 `.env` 连接串中的真实角色。恢复对象必须归应用角色所有；以 postgres
恢复却不转移所有权，会导致应用启动后无法读取 `alembic_version` 或业务表。

恢复后比较迁移版本，不能只执行 `current`：

```bash
cd /home/ubuntu/sunsq/phdebate
set -a; source .env; set +a
PYTHONPATH=apps/api .venv/bin/alembic -c apps/api/alembic.ini current
PYTHONPATH=apps/api .venv/bin/alembic -c apps/api/alembic.ini heads

cd /home/ubuntu/sunsq/debate-agent/apps/api
../../.venv/bin/alembic current
../../.venv/bin/alembic heads
```

`current` 必须与 `heads` 一致。恢复已有数据库后不要先运行 `upgrade head` 来掩盖错批次；先核对
清单记录的 schema，再决定是否执行经过审核的迁移。

## 9. 恢复数据卷和 MOSS

```bash
install -d -m 0700 /srv/phdebate-data-stage
tar -xzf /srv/phdebate-recovery/数据卷.tar.gz \
  -C /srv/phdebate-data-stage --no-same-owner --no-same-permissions
(cd /srv/phdebate-data-stage && sha256sum -c files.sha256)

cd /home/ubuntu/sunsq/phdebate
rsync -a --checksum /srv/phdebate-data-stage/storage/ storage/
rsync -a --checksum /srv/phdebate-data-stage/assets/moss-prompts/ assets/moss-prompts/
rsync -a --checksum /srv/phdebate-data-stage/backups/reliable-audio-20260719-declick/ \
  backups/reliable-audio-20260719-declick/
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

平台：

```bash
cd /home/ubuntu/sunsq/phdebate
python3.12 -m venv .python-venvs/restore-20260719
.python-venvs/restore-20260719/bin/pip install -r apps/api/requirements.txt
ln -sfn .python-venvs/restore-20260719 .venv

install -d -m 0750 runtime/node/bin
ln -sfn "$(command -v node)" runtime/node/bin/node

npm --prefix apps/web ci
PHDEBATE_API_RELEASE=restore-20260719 ./deploy/build-api-release.sh
PHDEBATE_WEB_DEPLOYMENT_MODE=root \
PHDEBATE_RELEASE=restore-20260719 ./deploy/build-web-release.sh
PHDEBATE_COLLAB_RELEASE=restore-20260719 ./deploy/build-transcript-collab-release.sh

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

历史源码压缩包不再作为代码回滚依据，GitHub 快照分支才是唯一源码恢复点。先执行只读磁盘审计和
源码归档清理预览；`apply` 除了显式参数外还要求二次确认环境变量，且只匹配源码归档命名，不会
触碰数据库、数据卷、私密配置、可靠语音或 MOSS 离线包：

```bash
./deploy/audit-storage.py
./deploy/prune-source-archives.sh dry-run
PHDEBATE_ALLOW_SOURCE_ARCHIVE_PRUNE=yes \
PHDEBATE_SOURCE_ARCHIVE_KEEP=1 \
PHDEBATE_SOURCE_ARCHIVE_MIN_AGE_HOURS=72 \
  ./deploy/prune-source-archives.sh apply
```

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

生产配置使用 Supervisor；不要启动仓库中的 Docker Compose。将以下受版本控制的配置安装到
`/etc/supervisor/conf.d/`：平台、Agent、MOSS 和 HTTPS Nginx。FunASR/LiveKit 的私密配置从受限
归档恢复。Nginx 主配置使用 `deploy/jixia-nginx-root.conf`，但必须先替换新 IP 和证书路径并
执行 `nginx -t -c ...`。

同时安装平台自带的 Nginx 日志轮转规则，避免匿名观战和 WebSocket 重连产生的访问日志无限增长：

```bash
install -m 0644 deploy/phdebate-nginx.logrotate.conf /etc/logrotate.d/phdebate-nginx
logrotate -d /etc/logrotate.d/phdebate-nginx
```

Supervisor 管理的平台与 Nginx 进程日志已经设置单文件大小和历史份数上限。不要用覆盖整目录的
通配 logrotate 规则，以免干扰独立语音运行时自己的日志策略。

## 12. 分阶段启动

不要直接 `supervisorctl update` 后让所有服务同时自动启动。按顺序逐项安装配置并启动：

1. PostgreSQL 5433、Redis 6380。
2. Debate Agent API/Web。
3. FunASR、LiveKit。
4. MOSS-Realtime，等待 authenticated readiness。
5. 平台 API primary/secondary。
6. Match Engine、Worker、平台 Web。
7. Nginx HTTPS。

每一步先检查日志和本机健康接口，再进入下一步。

为避免 `supervisorctl update` 因 `autostart=true` 一次启动全部服务，恢复时先把配置复制到临时
目录，将 `autostart=true` 改为 `autostart=false` 后安装；`reread && update` 后按上述顺序显式
`start`。全部验收通过后再安装原始配置恢复重启自愈能力。

## 13. 恢复验收

所有验证命令必须显式传入新服务器地址和本次 QA 房间，禁止使用脚本默认旧 IP：

```bash
cd /home/ubuntu/sunsq/phdebate
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
- 平台 与 Debate Agent 数据库临时恢复及逐表计数。

## 14. Mac 清理与日常备份原则

- Mac 只保留工作源码，不保留服务器 dump、tar、模型和私密配置。
- 临时浏览器测试、Git alternate index 和下载目录在验收后删除；不得以“方便回滚”为由在
  Mac 留存生产压缩包副本。代码回滚点只使用 GitHub 快照分支。
- Mac 扫描命令只报告并删除误落地的生产归档，不把其内容加入 Git：

  ```bash
  find /Users/sunshiqi/code/phdebate -type f \
    \( -name '*.dump' -o -name '*.dump.sha256' -o -name 'recovery-set-*.manifest' \
       -o -name '*-data-volumes.tar.gz' -o -name '*-private-config.tar.gz' \) -print
  ```

  删除前只核对路径和文件类型，不打开或打印私密内容。GitHub 分支只包含已审查源码和文档。
- 源码备份更新到 GitHub orphan 分支前必须执行 ignore 检查、密钥扫描和大文件扫描。
- 平台 DB 每日备份不足以构成完整恢复；Agent DB、数据卷和私密配置也要进入同一恢复批次。
- MOSS/FunASR 模型不需每天复制，但每次 revision、模型或依赖变化后必须重新生成离线包。
- 每个恢复批次应生成一个只读 manifest，绑定源码 commit、所有文件 SHA-256、数据库 Alembic
  版本、GPU 指纹和创建时间。

数据卷恢复包不得无限累积占满生产磁盘。完成新批次的清单校验和数据库/数据卷恢复演练后，先
执行只读预览；工具默认至少保留最近 3 个有效 schema 2/3 恢复批次，并保护这些清单引用的数据卷：

```bash
./deploy/prune-recovery-sets.sh dry-run
PHDEBATE_ALLOW_RECOVERY_PRUNE=yes \
  ./deploy/prune-recovery-sets.sh apply
```

该工具在把任何恢复集计入保留底线前，会重新校验其六类文件的唯一性、大小和 SHA-256。只要存在
缺失、重复或校验失败的文件，整个清理过程就会失败关闭，不删除任何批次。它也不删除 MOSS 离线
包、私密配置、可靠语音归档、平台/Agent 数据库备份或未识别的旧格式清单。数据库仍由各自的
14 天保留任务管理；release 使用 `prune-releases.sh` 独立保留最近回滚版本。磁盘清理前后都必须
执行 readiness、`audit-storage.py` 和当前恢复清单校验。

早期不完整清单不能直接删除，也不能为了让清理继续而手工移出扫描目录。先运行受审计隔离工具：

```bash
./deploy/quarantine-recovery-manifests.py dry-run
PHDEBATE_ALLOW_RECOVERY_MANIFEST_QUARANTINE=yes \
  ./deploy/quarantine-recovery-manifests.py apply
```

工具只处理“结构不完整但仍能安全定位现有数据卷”的清单：原文件移动到权限受限的
`runtime/deploy-backups/quarantine/`，同时写入包含原清单 SHA-256、隔离原因和受保护数据卷的
审计 sidecar。它不会删除任何清单或归档。无法定位安全数据卷引用、数据卷已缺失或目标文件冲突时，
隔离会被拒绝，原清单继续让清理 fail-closed。`prune-recovery-sets.sh` 和源码归档清理都会继续扫描
隔离目录中的引用，因此隔离不是绕过保护。隔离完成后必须再次 dry-run，人工确认保留底线和候选文件，
再显式设置 `PHDEBATE_ALLOW_RECOVERY_PRUNE=yes` 执行清理。

完成三类备份和恢复验证后，使用版本化工具生成绑定清单；私密归档路径必须显式传入，避免误选：

```bash
PHDEBATE_CODE_BRANCH='本批次 GitHub 快照分支' \
PHDEBATE_CODE_COMMIT='GitHub 快照不可变 SHA' \
PHDEBATE_DEPLOYED_APPLICATION_COMMIT='实际发布所用 SHA' \
PHDEBATE_DATABASE_SCHEMA=0027_speech_data_disposition \
PHDEBATE_RELIABLE_AUDIO_FINGERPRINT='可靠音频基线指纹' \
PHDEBATE_PRIVATE_CONFIG_BACKUP="$PWD/runtime/deploy-backups/私密配置归档" \
PHDEBATE_RELIABLE_VOICE_BACKUP="$PWD/runtime/deploy-backups/可靠语音归档" \
PHDEBATE_MOSS_OFFLINE_BACKUP="$PWD/runtime/deploy-backups/MOSS离线归档" \
  ./deploy/create-recovery-manifest.sh
```

工具会自动选择最新 平台 DB、Agent DB 和数据卷，确认六个文件都存在，重新计算 SHA-256，并以
`0600` 写入可迁移的 schema 3 清单。清单记录角色、文件名、大小和 SHA-256，不写源服务器绝对
路径。生成后用 `verify-recovery-manifest.sh` 复验，并人工核对源码提交与当前 release 链接。

## 15. 回滚

- 代码回滚只切换 `.api-primary`、`.api-secondary`、`.web-current`。
- 数据库恢复到新数据库核验后再切换连接，不覆盖原备份。
- 语音回滚只恢复固定 revision、Prompt、模型和参数，不自动切回 LightTTS。
- 新服务器验收失败时保持旧服务器只读，禁止两个生产数据库同时写入。
- 只有新服务器完成数据库计数、四房并发、实时音频与浏览器验收后才切换 DNS/Nginx 入口；切换
  失败只回退流量入口，不把新数据库覆盖回旧数据库。
