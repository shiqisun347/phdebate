# 2026-07-19 可靠版本备份

本备份冻结了通过真实 Agent → MOSS → LiveKit → Chromium 长发言门禁的生产版本。
后续系统功能和 UI 改造不得覆盖这份回滚点。

## 本地备份

目录：`backups/reliable-20260718T191839Z`

- `phdebate-source-20260718T191839Z.tar.gz`：不含密钥和运行数据的源码包。
- `reliable-audio-baseline.json`：82 个受保护音频文件的 SHA-256 基线。
- `tts-browser-acceptance-report.md`：首声、连续性、丢包和浏览器验收记录。
- `openmoss-deployment-manifest.json`：固定模型、代码和音色资产清单。
- `auto-20260718T194814Z.dump`：生产 PostgreSQL `pg_dump -Fc` 备份。
- `VERSIONS.txt`、`CONTENTS.txt`、`SHA256SUMS`：服务器版本与归档清单副本。

源码、音频基线、部署清单和数据库转储均已在本机重新校验 SHA-256。

### 语音音色隔离增量基线

目录：`backups/reliable-audio-20260719-voice5-quarantine`

- 只调整反方一辩的默认音色映射，将存在明显高频毛刺的 `debate_voice_5` 临时隔离；
- MOSS、LiveKit、Opus 与浏览器播放器实现未修改；
- 82 个受保护文件的新基线指纹为
  `d796e70cf988fdcb75bd133959b2a58ac6227086bd52faac446779c31a7855a5`；
- 真实浏览器首声 `2797.9 ms`，73.20 秒完整语音的高幅采样跳变由问题样本每秒
  `44.19` 次降至 `0.72` 次。

## 服务器完整备份

目录：`/home/ubuntu/sunsq/backups/phdebate-reliable-20260718T194814Z`

权限为仅 root 可读，约 17 GB，包含：

- `phdebate-v2-project.tar.gz`：生产项目、依赖、已发布 Web、音频和运行资产；排除正在写入的 PostgreSQL/Redis 目录、日志和临时 QA 数据。
- `openmoss-runtime-and-models.tar`：`/opt/OpenMOSS` 的固定源码、Realtime 模型和 Audio Tokenizer 模型。
- `system-config-and-secrets.tar.gz`：Supervisor、Nginx 与 `/opt/phdebate` 私密配置；不得下载到公共目录或提交版本库。
- `auto-20260718T194814Z.dump`：一致性数据库转储。
- `SHA256SUMS`：上述归档、数据库和清单的完整校验值。

所有服务器归档已经执行一次完整 `sha256sum -c`。数据库另行恢复到临时数据库，34 张
public 表逐表核对成功，Alembic 版本为 `0019_audio_streaming`。

### 当前课堂与同意闭环版本

目录：
`/home/ubuntu/sunsq/backups/phdebate-goal-progress-20260718T223000Z/current-complete-20260719T031354Z`

这是在学校/课堂管理、教师工作台、学生录音同意和刷新可恢复批量开房完成生产验收后
生成的最新恢复点。目录权限为 `700`，文件权限为 `600`，包含：

- `phdebate-v2-source-clean.tar.gz`：当前源码、文档和私密 `.env`，排除依赖、模型、
  运行数据与历史 QA 媒体；
- `web-release.tar.gz`：当前可直接回切的 Next standalone release
  `classroom-status-label-20260719`；
- `phdebate-v2.dump`：发布与 QA 合规闭环完成后的 PostgreSQL 一致性转储；
- `supervisor-config.tar.gz`、`nginx-config.tar.gz`：当前服务与反向代理配置；
- `supervisor-status.txt`、`api-health.json`、`gpu-voice-runtime.json`：备份时的运行状态；
- `audio-worklet.sha256`：可靠播放器 SHA-256，仍为
  `de373b01d9b5587bdbaa38115227d99a21b2ec3645f3046bfbda5068663ba219`；
- `SHA256SUMS`：所有主要恢复文件已通过 `sha256sum -c`。

该增量恢复点不替代 17 GB 的模型与完整运行资产备份；恢复最新业务版本时，应先使用
完整可靠备份恢复模型和基础运行环境，再覆盖本节的源码、Web release、数据库和系统
配置。

## 恢复顺序

1. 在新目录解压 `phdebate-v2-project.tar.gz`，不要直接覆盖仍在运行的目录。
2. 将 `openmoss-runtime-and-models.tar` 解压到 `/opt/OpenMOSS`。
3. 仅在受控服务器上恢复 `system-config-and-secrets.tar.gz`，复核文件权限后再启动服务。
4. 新建 PostgreSQL 数据库并恢复 `auto-20260718T194814Z.dump`，执行
   `deploy/verify-backup-restore.sh` 再切换连接串。
5. 启动服务后检查 `/api/health/ready`，然后依次执行固定音色、首声、60 秒连续播放、
   中断清队列和浏览器门禁。
6. 确认无误后再切换 Nginx；回滚只切换服务目录和数据库连接，不修改备份本身。
