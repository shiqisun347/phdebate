# 双 API Worker 安全部署

本方案只扩展业务 REST 与房间状态 WebSocket。可靠语音链路保持原样：

- `/ws/rooms/:code/audio`、`/ws/rooms/:code/asr` 仍固定到主实例 `12340`。
- `/media` 仍固定到主实例。
- 不修改 TTS、MOSS、LiveKit、FunASR、AudioWorklet 或浏览器播放器。
- 独立 `jixia-v2-engine` 是唯一比赛引擎；两个 API 均强制
  `ENGINE_ENABLED=false`。
- API 滚动重启不得执行全局 Presence 清空；两个 API 均强制
  `PRESENCE_RESET_ON_STARTUP=false`。

## 端口与职责

| 进程 | 端口 | instance | 职责 |
| --- | ---: | --- | --- |
| `jixia-v2-api` | 12340 | `api-primary` | REST、房间状态 WS、可靠语音 WS |
| `jixia-v2-api-secondary` | 12342 | `api-secondary` | REST、房间状态 WS |
| `jixia-v2-engine` | 无 HTTP 端口 | — | 唯一状态机和 Presence 过期回收 |

两个 API 使用同一个 PostgreSQL、Redis 和代码版本。Redis Lua 租约确保同一席位的多个
WebSocket 分布在不同实例时，只有最后一个连接离开才会写入离线状态。
API release 位于项目根目录之外的不可变目录，因此 Supervisor 显式固定 `MEDIA_ROOT`、
`ARCHIVE_ROOT`、研究导出和备份状态的绝对路径，避免相对路径被错误解析到 release 内部。

## 首次从单实例灰度

先完成数据库和源码备份，并记录当前 Nginx、Supervisor 配置。不要直接用完整配置覆盖
正在工作的单实例；按以下顺序保持全程有可用实例：

1. 构建不可变 API release：

   ```bash
   PHDEBATE_V2_API_RELEASE=round4-dual-api \
     /home/ubuntu/sunsq/phdebate-v2/deploy/build-api-release.sh
   ln -sfn /home/ubuntu/sunsq/phdebate-v2/runtime/api-releases/round4-dual-api \
     /home/ubuntu/sunsq/phdebate-v2/.api-secondary
   ```

2. 只安装 `phdebate-v2-api-secondary.supervisor.conf`，执行
   `supervisorctl reread && supervisorctl update`。原 `12340` 不动。
3. 直连验证 `12342`：

   ```bash
   curl --fail http://127.0.0.1:12342/api/health
   PYTHONPATH=/home/ubuntu/sunsq/phdebate-v2/apps/api \
     /home/ubuntu/sunsq/phdebate-v2/.venv/bin/python \
     /home/ubuntu/sunsq/phdebate-v2/deploy/verify_dual_api_workers.py \
     --room-code <一个公开运行中房间号>
   ```

4. 安装包含 `jixia_v2_api` upstream 的 Nginx 配置，先运行 `nginx -t`，再 reload。
   连续请求 `/api/health` 应能看到两个不同的 `instance`。
5. 将 `.api-primary` 指到同一 release。移除临时 secondary 配置，安装完整
   `phdebate-v2.supervisor.conf`，再执行 Supervisor reread/update。此时 secondary 已经承接
   REST 和状态 WS，主实例可安全重启。
6. 再次运行双实例验证、真实两设备 Presence 流程和 WebSocket 断线重连测试。

若第 2–4 步失败，恢复旧 Nginx 配置并停止 secondary 即可；主实例从未被修改。若第 5 步
失败，secondary 仍可服务 REST 和房间状态 WS，恢复旧 Supervisor 配置并重启 primary。

## 后续滚动发布与回滚

Alembic 迁移必须先满足向前、向后版本兼容，禁止在同一轮删除旧代码仍会读取的列。构建
release 后运行：

```bash
deploy/roll-api-workers.sh <new-release>
```

脚本先切 secondary，直连 `/api/health` 验证进程身份和数据库连接，再切 primary，最后验证
公开 Nginx 入口。任一健康门失败会自动恢复之前的两个 symlink 并重启旧版本。

显式回滚：

```bash
deploy/rollback-api-workers.sh <previous-release>
```

滚动期间已有状态 WebSocket 会断开一次，由前端重连到仍健康的实例。Presence 租约不会因
单个 API 重启被全局清空。音频和 ASR WebSocket 始终在 primary，因此涉及语音的发布仍应
安排在无活跃真人发言时；本方案本身不改变其路由和实现。

## 发布门槛

- `12340`、`12342` 的 `/api/health/live` 返回各自 instance。
- 两端 `/api/health` 均为 200，证明数据库连接有效。
- 两端 `/api/competitions` 的赛事 slug 完全一致。
- 同一公开房间直连两个状态 WS，初始 `seq` 一致。
- 两个独立 `RoomHub` 的 Redis Presence：首连接为 first、次连接非 first、首连接离开不离线、
  最后连接离开才离线。
- Nginx REST 和精确的状态 WS 路由到 upstream；`/audio`、`/asr`、`/media` 仍指向 12340。
- Supervisor 中只有一个 engine，且两个 API 都禁用 engine 和 startup presence reset。
