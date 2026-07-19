# 第二轮并行可靠性、参赛闭环与后台性能报告

日期：2026-07-19  
生产环境：https://117.50.192.216  
生产 release：`round2-reliability-20260719`

## 边界

- 未修改 `debate-stage.tsx`、浏览器音频库、AudioWorklet、MOSS、LiveKit、FunASR、TTS
  Provider 或语音运行参数。
- 发布时只重启 V2 API 和 Web；Engine、Worker、MOSS、LiveKit、FunASR 均保持运行。
- 发布前后使用可靠音频清单验证 82 个受保护文件。

## 并行审计结果

### 1. 实时房间状态恢复

- 修复“发送初始快照”和“注册 RoomHub 订阅”之间的竞态窗口。订阅建立后立即执行一次
  权威 `seq` 与可见性同步，不再依赖最长约 20 秒的 heartbeat 才恢复遗漏状态。
- 匿名房间在窗口中改为私密时立即关闭匿名连接。
- Next 动态路由复用组件时立即清除上一房间的状态、事件和连接标记。
- 旧 WebSocket 的迟到回调不会覆盖新房间；跨房间 payload 会被拒绝并以 4002 重连。

### 2. 学生再次参赛闭环

- 新增 `POST /api/rooms/:code/rematch`。
- 原真人参赛者可在终局结果页点击“同题再来一场”，沿用赛事、题目、可见性和本人席位，
  其余席位重新开放。
- 服务端校验终局状态、参赛身份、赛事和赛季状态、正式赛题库、席位规则、跨房间重复参赛
  与幂等键；匿名观众无法使用。
- AI 接替用户在个人中心看到“返回观战并等待管理员恢复”，不再误以为可以直接恢复控制。
- 生产一次性 QA 流程真实完成“开赛 → 终止 → 再赛 → 幂等重放 → 关闭 → 数据清理”，
  输出 `rematch_flow_verified`，临时用户和房间均为 0。

### 3. 管理后台请求与分页

- Agent、预设语音等单域修改不再重新请求全部 14 组后台数据。典型操作从约 15 个请求降到
  PATCH + 本域 GET + audit 共 3 个请求，下降约 80%。
- 赛事、题库、自动流程、裁判、Provider 和结果复核仅刷新相关域。
- 房间、用户、审计分页抽成共享组件，增加具名导航、`aria-live` 页码播报、边界和加载禁用。
- 生产后台“比赛监管”确认存在 `比赛监管分页` 导航。

### 4. 并行测试基础设施

- 旧 `conftest.py` 固定使用 `storage/pytest-v2.db`、统一媒体和归档目录。两个 pytest 进程
  同时启动时会相互删除数据库，产生大量 `disk I/O error` 和假 503。
- 每个 pytest 进程现在使用独立临时运行目录，数据库、媒体、归档和 backup status 均隔离，
  session 结束后自动清理。
- 两组 API 测试并行运行分别 `5 passed` 与 `2 passed`，临时目录残留为 0。

### 5. 首页视觉修复

- 1440px 首页主标题会把“系统”拆成单独的“系”和下一行“统自动开赛”，形成明显孤字。
- 标题改为三个受控语义行，生产测量宽度分别为 652、584、438px，不再出现孤字。
- 证据：[修复后的首页主视觉](screenshots/home-hero-fixed-1440.png)。

## 回归与压力测试

- 前端：29 个测试文件，`211 passed`。
- 后端：`360 passed`。
- Next.js production build、ESLint、Ruff、Python compileall 全部通过。
- 比赛引擎 soak：3 轮 × 9 个多房间场景，共 27 个场景全部通过。
- 生产 Playwright：1920px 桌面与 390px 手机共 `10/10 passed`。
- 生产 500 个匿名观战 WebSocket：`500 opened / 0 failed`，保持 12 秒后全部释放。
- 生产 500 个握手后立即断开的 WebSocket：`500 connected / 0 failed`，未出现 ASGI、
  pending task 或服务退出。
- 500 连接期间服务器 load average 约 0.37，API RSS 约 174MiB，API 和 Web 持续 RUNNING。
- 首页冷加载约 191KB、TTFB 约 83ms、CLS 0；观战约 170KB、TTFB 约 31ms、CLS 0。

## 备份与回滚

- 发布前数据库：`auto-20260719T071213Z.dump`，SHA-256
  `ec9386ceb4edde2a48f47e6c51087a7600e17da185b04449594808f2127a1be7`。
- 发布前源码：`20260719T071213Z-before-round2-source.tar.gz`，SHA-256
  `bbb1394294ae2e8b1456eeadf2f9be64a9c744d57f7885eb1fe2d86a06fb4af8`。
- 发布前 Web 指针保存在 `20260719T0717-before-round2-web.txt`，原 release 为
  `parallel-qa-20260719`。
- 发布后数据库：`auto-20260719T072203Z.dump`，SHA-256
  `becc64ea8d1271b3b22daee68b38d29a37cb4a23018392342ed188e1d94e5e47`。
- 发布后源码：`20260719T072203Z-round2-reliability-source.tar.gz`，104,388,808 字节，
  SHA-256 `5d5048bc0200e171ced8401b533f94d1f1aebcfd949fa3b1ea2f11183841a1a7`。
- 可靠音频清单：82 个文件，指纹
  `3219138b22878e766c94b9a1fa0e422211a6f27dd74afbbb93a8e45f7e295285`。

## 仍需继续处理

- RoomHub presence 仍为单 API 进程内计数；未来扩展多 API worker 前需迁移到 Redis 原子
  presence。
- Redis Pub/Sub 不能持久重放断线期间事件；当前依靠权威快照恢复，后续可改为 Redis
  Streams 或独立房间事件游标。
- 管理后台文件仍约 2680 行，应继续拆为按标签懒加载的独立模块；首屏也应从 14 个端点改为
  dashboard-first。
- AI 接替后的真人恢复仍要求系统管理员操作。面向大规模学生赛事，应增加“原选手申请恢复，
  房主确认”的房间级流程。

目标保持进行中，上述项目不是完成声明。
