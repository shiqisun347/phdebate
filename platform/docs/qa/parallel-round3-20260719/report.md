# 第三轮：真人恢复、多 Worker Presence 与后台懒加载

日期：2026-07-19  
生产环境：https://117.50.192.216  
生产 release：`round3-presence-restore-20260719`

## 音频冻结边界

- 未修改 `DebateStage`、浏览器音频库、AudioWorklet、MOSS、LiveKit、FunASR、TTS Provider
  或语音运行参数。
- Presence 子任务曾把两个非语音参数放入受保护的统一配置文件，音频清单立即拒绝发布；
  参数随后迁移到实时模块环境读取，`config.py` 恢复逐字节一致。
- 最终可靠音频清单仍为 82 个文件，指纹
  `3219138b22878e766c94b9a1fa0e422211a6f27dd74afbbb93a8e45f7e295285`。

## 真人席位恢复闭环

- 新增 `SeatRestoreRequest` 和 Alembic `0024_seat_restore_requests`。
- AI 接替的原辩手可从观战页或个人中心申请、撤销恢复；房主或系统管理员在单场控制台批准
  或拒绝。
- 批准后通过房间 WebSocket 同步，原选手获得“返回比赛”入口。
- 服务端验证原始 user_id、比赛活动态、席位仍为 `ai_substitute`、全房无活跃发言、账号
  可用且用户没有其他活动真人席位。
- 支持申请幂等、重复审批/撤销和比赛终局自动失效；匿名及无关用户看不到申请身份。
- 生产一次性 QA 流程真实完成：双用户入房、开赛、模拟 AI 接替、申请、幂等重放、房主
  批准、真人恢复、终止和清理。输出：
  `seat_restore_flow_verified request=1 replay=1 owner_approved=1 restored=1`。
- 验收后临时用户、房间和申请记录均为 0；生产房间和用户总数恢复为 49、13。

## Redis 多 API Worker Presence

- 每条 WebSocket 使用唯一 connection id，在 Redis ZSET 中建立 60 秒租约。
- Lua 原子执行 join、leave、refresh、reap 和 active；多个 worker 只在首次连接和最后断开时
  改变权威在线状态。
- sender heartbeat 续租；Engine 原子认领过期身份并在房间行锁内二次确认，只追加一次
  `presence.disconnected`。
- Redis 操作超时为 1 秒；Redis 不可用时保留进程内引用计数作为安全降级。
- 生产 `.env` 已设：
  - `PRESENCE_RESET_ON_STARTUP=false`
  - `PRESENCE_LEASE_SECONDS=60`
  - `PRESENCE_REDIS_TIMEOUT_SECONDS=1`
- 使用两个独立 `RoomHub` 实例连接生产 Redis 验证首次加入、第二连接、非最后断开、最后断开
  和清理，输出：`redis_presence_multi_instance_verified ... active_after=0`。

## 管理后台按需加载

- 旧后台首屏并行调用 dashboard、users、rooms、competitions 等 14 个接口。
- 现在首屏只请求 `/api/admin/dashboard`，请求数从 14 降至 1，减少约 93%。
- 八个其他模块首次打开时才加载对应数据；已加载模块回切不重复请求。
- 同域并发请求去重，列表继续使用请求序号防旧响应覆盖；提供“刷新当前模块”。
- 未访问审计模块时，其他写操作不再额外加载审计接口。
- 生产浏览器验证：首次打开后台资源列表只有 dashboard；进入比赛监管后增加 rooms，返回并
  再次进入时 rooms 请求数仍为 1。

## 过时内容清理

- 单场控制台仍显示“FunASR / LightTTS”，与当前 MOSS 正式语音链路不一致。
- 已改为“FunASR / MOSS 实时语音”，仅修改状态文案和测试，不涉及语音代码。

## 测试与生产验收

- 前端：30 个测试文件，`214 passed`。
- 后端：`367 passed`。
- Next.js production build、ESLint、Ruff、Python compileall 全部通过。
- Alembic：全新 DB upgrade、downgrade 到 0023、再次 upgrade 到 0024 均通过；生产数据库
  当前为 `0024_seat_restore_requests (head)`。
- 生产 Playwright：1920px 桌面与 390px 手机 `10/10 passed`。
- 生产匿名观战：500 个 WebSocket，`500 opened / 0 failed`，保持 12 秒后正常释放。
- 平台 API、Engine、Worker、Web、PostgreSQL、Redis、Agent、MOSS、LiveKit、FunASR、HTTPS
  均为 RUNNING。

## 备份与回滚

- 发布前数据库：`auto-20260719T075038Z.dump`，SHA-256
  `89019ee54c9a0f01fa2a91833de182c7da6272ac5b18fd8bc0a206f1b794fd85`。
- 发布前源码：`20260719T075039Z-before-round3-source.tar.gz`，SHA-256
  `a73a64f00afc4e89d78b0678a6534e0bffffb0314fcdb8ddab8320accd462fc9`。
- 发布前私密配置：`20260719T075039Z-before-round3-private-config.tar.gz`，SHA-256
  `f9d2c8f4b91c87c6fcaf9ba222a762426a471d26460ce0b1e48e3c4a065c5875`。
- 前一 Web release 为 `round2-reliability-20260719`，指针已记录在
  `20260719T0755-before-round3-web.txt`。
- 发布后数据库：`auto-20260719T080154Z.dump`，SHA-256
  `af3728755652de90cd000f59d123a395010c4df7fc5209a1c00f33e5f35baea4`。
- 发布后源码：`20260719T080154Z-round3-presence-restore-source.tar.gz`，105,124,070
  字节，SHA-256 `744f2a8b1a7a8bf3f6179558fb0e54c6995b15687050ac3c3937680fb66c4e93`。
- 发布后私密配置：`20260719T080154Z-round3-private-config.tar.gz`，SHA-256
  `8cfd698fdf237b25105794247d2065b95004c830a45ea400b4859096c7aa1a7b`，权限 600。
- 备份权限审计发现历史源码包包含 `.env` 且为 644；现已将全部 `*source*.tar.gz` 调整为
  root-only 600，并把 `runtime/deploy-backups` 调整为 700。复核结果：可被 group/other 读取
  的源码包为 0。

## 继续改进项

- 当前生产仍是单 API 进程；Redis 租约已经消除 Presence 架构阻碍，但正式增加多个 worker
  前还需执行 Nginx/进程级滚动发布和真实浏览器跨 worker 粘性/非粘性验证。
- 管理后台数据已按需加载，但 `page.tsx` 仍很大，应继续拆为按模块动态加载的组件文件。
- 恢复申请目前由房主或系统管理员处理；后续可增加超时提醒和房主离线时的可配置自动批准。

目标保持进行中，本报告不是完整目标的完成声明。
