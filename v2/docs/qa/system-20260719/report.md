# 稷下辩论平台整体 Dogfood 与改进报告

| 字段 | 内容 |
|---|---|
| 日期 | 2026-07-19 |
| 生产地址 | https://117.50.192.216 |
| 范围 | 参赛者端、多人房间、自动比赛、观战、结果、异常恢复、管理端、UI 与性能 |
| 音频边界 | 不改音频热路径；仅纠正生产 Supervisor 对可靠 MOSS 参数的运行时漂移 |

## 当前统计

| 严重级别 | 数量 |
|---|---:|
| Critical（未解决） | 0 |
| High（未解决） | 0 |
| Medium（未解决） | 0 |
| Low（未解决） | 0 |
| 已修复或按产品方向移除 | 47 |
| 待修复 | 0 |
| 累计记录 | 47 |

## 测试矩阵

- [x] 匿名首页、赛事、排行榜与公开观战
- [x] 注册、登录、退出、个人中心与返回比赛
- [x] 1v1 人机、1v1 人人、4v4 多人类 + AI 补位
- [x] 并发抢座、准备、开始和多设备接管
- [x] 真人发言、结束、ASR 修正、断线和 AI 接替
- [x] 房主暂停、继续、重试、跳过、终止和异常指引
- [x] 结果、历史、逐字稿、下载与排行榜
- [x] 管理端权限、赛事、题库、模板、Agent 接入和房间监管
- [x] MOSS 源音频、LiveKit/Opus、浏览器 PCM 连续性和运行参数防漂移
- [x] 桌面、手机、键盘、可访问性、性能和控制台

## 并行深度审计与成熟项目对照

本轮将真实浏览器、管理端一致性、后端权限投影和成熟项目架构对照拆成四条并行工作流，
再统一合并和生产复测。浏览器独立报告见
[并行浏览器深测报告](../browser-deep-20260719-agent/report.md)。设计取舍重点参考：

- [Colyseus Rooms](https://docs.colyseus.io/room) 的服务端权威房间与客户端视图隔离；
- [Lichess](https://github.com/lichess-org/lila) 的大厅、对局恢复和面向玩家的状态反馈；
- [boardgame.io](https://github.com/boardgameio/boardgame.io) 的服务端阶段状态机与客户端投影；
- [LiveKit Meet](https://github.com/livekit-examples/meet) 的房间身份、连接与重连交互；
- [DMOJ](https://github.com/DMOJ/online-judge) 的赛事、排行榜和管理域边界。

本轮只吸收可验证的设计原则，没有复制外部项目页面或业务代码。可靠音频热路径继续冻结，
所有改动均位于公开数据投影、参赛入口、键盘交互、管理端请求一致性和测试门禁。

## 产品范围调整：仅保留赛事平台

2026-07-19 根据产品方向确认，平台不再设置学校、课程、班级、教师工作台或教学活动。
所有用户统一围绕“赛事 → 房间 → 比赛 → 结果与历史”完成参赛、观战和运营；系统管理员
只管理全局赛事、用户、房间、自动流程、服务和审计。

本轮已完成：

- 删除 `/teacher*`、`/admin/classrooms` 页面和对应导航、个人中心卡片与管理入口；上述路径
  生产环境均返回 404。
- 删除教师、课堂、活动、全班同意和研究导出 REST API；旧接口生产环境均返回 404。
- 数据库升级到 `0022_remove_classroom_domain`，移除 11 张退役域表以及房间的活动归属列；
  迁移前后房间总数保持 45，历史房间、比赛、发言、字幕、音频和结果均未删除。
- 首页统一改为“参赛者自主组局”“邀请队友”，不再把产品限定为课堂或教学场景。
- 管理后台仅显示赛事相关模块；数据库结构检查不再向界面暴露内部迁移名称。
- 新建并关闭 QA 房间 `#594944`，确认创建、席位、分享、关闭和大厅返回链路正常，房间未被
  录音同意或教学权限阻断。

生产证据：

- [赛事化首页](screenshots/competition-only-home.png)
- [赛事化系统后台](screenshots/competition-only-admin-final.png)
- [个人比赛档案](screenshots/competition-only-me.png)

备份与验证：

- 迁移前数据库：`auto-20260719T041707Z.dump`，SHA-256
  `bf43216a7a83e77b737a9c5ab33eb5365ccd8418053eda656beb91a6f645ff5b`。
- 迁移后数据库：`auto-20260719T043931Z.dump`，SHA-256
  `543d6fbbddcfecc8b2c550419eb265769c2077f3e5268b8d66a9be409393d0bf`。
- 多人大厅与抢座回归后的最新数据库：`auto-20260719T052318Z.dump`，SHA-256
  `814af93df78de7220a37d7dc2f72ad84b6aded97e975bfa86c164159161266d5`。
- 源码回滚包：`20260719T041707Z-before-competition-only-source-v2.tar.gz`，SHA-256
  `9ce60a3ff6afb9102a40313c8b03cdf3308dc3b41e0ddc292bf47ae446ccefd9`。
- 部署后当前源码包：`20260719T044257Z-competition-only-current-source-v2.tar.gz`，SHA-256
  `03c821c0154eef3fdc73ed7c02eb0c36d6a2150b97be9e2f3aa0e096f243f61e`。
- 手机后台修复后的最新源码包：`20260719T045534Z-current-source-after-mobile-admin.tar.gz`，
  SHA-256 `643bd0ee0985509ea35c8299e8280e0dae1eb411234d920585bd6985c4e498ba`。
- 公开状态文案修复后的最新源码包：
  `20260719T050151Z-current-source-after-watchable-labels.tar.gz`，SHA-256
  `ccdeb901ab916c4380dbbbe23dfe7f378349e444ae155b0add146ed6c4fb14f2`。
- 结果事件本地化后的最新源码包：`20260719T051307Z-current-source-after-event-labels.tar.gz`，
  SHA-256 `0ce5461fcc644d705b06ce8253bc9c194713cc0a0962c71b449112c964f189af`。
- 生产 E2E 门禁更新后的最新源码包：
  `20260719T053106Z-current-source-after-e2e-gate.tar.gz`，SHA-256
  `29e2e6097ec7585d9df9dff5d64767d1e9f986f24f33a29cbaa5fdcc0790d1d7`。
- 本轮部署前数据库：`auto-20260719T054822Z.dump`，SHA-256
  `990efe91aebf4da34b69fd81c7e04508e859485103af2ed11dd9021fc5ea5fb9`；部署前源码包：
  `20260719T054822Z-before-competition-catalog-source.tar.gz`，SHA-256
  `29e2e6097ec7585d9df9dff5d64767d1e9f986f24f33a29cbaa5fdcc0790d1d7`。
- 本轮部署后数据库：`auto-20260719T055530Z.dump`，SHA-256
  `f737c8dd2292bf70ce5f3397aef513eebdc98a9805195e71da64e6a51db96336`；部署后源码包：
  `20260719T055530Z-current-source-after-competition-catalog.tar.gz`，SHA-256
  `4ca5772f7697a39873583104f31c9655dc0033a7e414dc1d050d455a7178261d`。
- 多房间并发验收并完成临时数据清理后的数据库：`auto-20260719T060006Z.dump`，SHA-256
  `a1ed3dd5d9b76be565b5e54d5b5a78ff07cc665ae108f51fdd31dc4f1df8c4be`。
- 字幕键盘可访问性发布前源码包：`20260719T060905Z-before-subtitle-keyboard-source.tar.gz`，
  SHA-256 `4ca5772f7697a39873583104f31c9655dc0033a7e414dc1d050d455a7178261d`。
- 字幕键盘可访问性发布后数据库：`auto-20260719T061141Z.dump`，SHA-256
  `8e5446f774b26f75d12a4497a583ea134e8e7ab8f340d45afe371674d40899fc`；源码包：
  `20260719T061142Z-current-source-after-subtitle-keyboard.tar.gz`，SHA-256
  `3927e2e7553239767df9d9745ccacd129b43dd319765f0c9a3c220bf2bc8aec9`。
- 并行审计部署前数据库：`auto-20260719T063842Z.dump`，SHA-256
  `9fba2b4d478bcd25ba49425137ef20663af7c8fc39c950850e1f85f4181aafb1`；源码包：
  `20260719T063842Z-before-parallel-qa-source.tar.gz`，SHA-256
  `3927e2e7553239767df9d9745ccacd129b43dd319765f0c9a3c220bf2bc8aec9`。
- 并行审计部署后数据库：`auto-20260719T064802Z.dump`，SHA-256
  `e935882228670641bd011fb864212fff8ed8a12e947a26d60e0017c7a3441278`；源码包：
  `20260719T065041Z-current-source-after-parallel-qa.tar.gz`，102,365,583 字节，SHA-256
  `6be14a2511850741563fb7ab51f63592c727e3634daafc595569c12a1dec08da`。
- 最新生产 release 为 `parallel-qa-20260719`；前端 `206 passed`、后端 `357 passed`，
  Next.js build、ESLint、Ruff、Python compile 均通过；npm 与 Python 生产依赖审计均为
  0 个已知漏洞。
- 最新生产 Playwright 门禁在 1920px 桌面与 390px 手机共 `10/10 passed`，新增覆盖匿名
  房间预检与公开房间直接观战；所有 V2、Agent、PostgreSQL、Redis、MOSS、LiveKit、
  FunASR 和 HTTPS 服务均为 RUNNING。
- 第二轮可靠性发布前数据库：`auto-20260719T071213Z.dump`，SHA-256
  `ec9386ceb4edde2a48f47e6c51087a7600e17da185b04449594808f2127a1be7`；源码包：
  `20260719T071213Z-before-round2-source.tar.gz`，SHA-256
  `bbb1394294ae2e8b1456eeadf2f9be64a9c744d57f7885eb1fe2d86a06fb4af8`。
- 第二轮发布后数据库：`auto-20260719T072203Z.dump`，SHA-256
  `becc64ea8d1271b3b22daee68b38d29a37cb4a23018392342ed188e1d94e5e47`；源码包：
  `20260719T072203Z-round2-reliability-source.tar.gz`，SHA-256
  `5d5048bc0200e171ced8401b533f94d1f1aebcfd949fa3b1ea2f11183841a1a7`。
- 第二轮 release 为 `round2-reliability-20260719`；前端 211、后端 360 项通过，生产
  Playwright 10/10、500 匿名观战 0 失败、500 次握手后立即断开 0 失败。详细报告见
  [第二轮并行可靠性报告](parallel-round2-20260719/report.md)。
- 第三轮发布前数据库：`auto-20260719T075038Z.dump`，SHA-256
  `89019ee54c9a0f01fa2a91833de182c7da6272ac5b18fd8bc0a206f1b794fd85`；源码包：
  `20260719T075039Z-before-round3-source.tar.gz`，SHA-256
  `a73a64f00afc4e89d78b0678a6534e0bffffb0314fcdb8ddab8320accd462fc9`。
- 第三轮发布后数据库：`auto-20260719T080154Z.dump`，SHA-256
  `af3728755652de90cd000f59d123a395010c4df7fc5209a1c00f33e5f35baea4`；源码包：
  `20260719T080154Z-round3-presence-restore-source.tar.gz`，SHA-256
  `744f2a8b1a7a8bf3f6179558fb0e54c6995b15687050ac3c3937680fb66c4e93`。
- 第三轮 release 为 `round3-presence-restore-20260719`；前端 214、后端 367 项通过，
  Alembic 0024、生产恢复申请、Redis 多实例 Presence、后台 14→1 请求、Playwright
  10/10 和 500 匿名观战均通过。详细报告见
  [第三轮并行扩展报告](parallel-round3-20260719/report.md)。
- 此前生产 Playwright 门禁在 1920px 桌面与 390px 手机项目共 `8/8 passed`；覆盖赛事大厅、
  赛事详情、匿名观战、登录注册、排行榜，以及退役教师/课堂路由的 404 回归。
- 本地后端 `353 passed`，前端 `197 passed`，生产四个主服务及 MOSS、LiveKit、FunASR
  均为 RUNNING；部署后健康请求未产生新增 API/Engine/Worker/Web stderr。
- 可靠音频清单仍为 82 个文件，指纹保持
  `3219138b22878e766c94b9a1fa0e422211a6f27dd74afbbb93a8e45f7e295285`；生产环境将部署前
  源码包中的 Config、Provider、Voice Runtime、Debate Stage、浏览器音频库、Worklet 和
  OpenMOSS 部署文件逐项与当前版本比较，无差异，核心 Worklet SHA-256 仍为
  `de373b01d9b5587bdbaa38115227d99a21b2ec3645f3046bfbda5068663ba219`。

下方 ISSUE-016 至 ISSUE-021 保留为产品方向调整前的历史证据，不再代表当前产品功能或
后续路线。

## 真实比赛流程验收进展

### 4v4 多真人与 AI 补位

- QA 房间 `#367182` 使用 3 个独立学生账号认领正方一辩、正方二辩和反方一辩，
  其余 5 席由系统自动生成 AI 辩手。
- 三个浏览器同时准备后，房主开始比赛，所有页面从大厅自动进入比赛舞台。
- 只有当前正方一辩看到可用“开始发言”，另外两个真人页面显示明确的禁用原因。
- 房主暂停、继续和提前终止均成功，跨房间状态、计时和席位没有串用。
- 验收房已终止，视频见
  [qa-4v4-three-human-start.webm](videos/qa-4v4-three-human-start.webm)。

### 真人发言、ASR 兜底、断线接替与恢复

QA 房间 `#192885` 使用独立 Chromium fake-media 设备完成真人链路：

- 赛前麦克风预检获得权限、检测到输入音量，并在结束后释放设备。
- 真人正方一辩成功开始录音和 ASR；测试音频无法形成有效中文时，页面没有丢弃发言，
  而是进入“补充发言文字”恢复状态。
- 用户手工补充 116 字发言并提交后，数据库记录为 `completed`，保存录音地址和一条最终
  transcript segment，状态机正常推进到反方 AI 立论。
- 浏览器关闭约 3 秒后重新打开，登录会话、真人席位、已提交内容和比赛进度均恢复。
- 自由辩论中再次关闭浏览器：30 秒时仍显示“真人·原辩手离线”；超过 60 秒后生成
  `seat.ai_substituted` 事件，舞台显示“AI 接替·系统验收学生甲”，比赛没有停摆。
- 原辩手返回后进入“房主观战·只读”，不能绕过接替状态直接发言；系统管理员控制台
  显示“恢复真人”按钮，点击后生成 `seat.human_restored` 事件并恢复真人席位。
- 管理员随后终止 QA 房间；MOSS 清理结果为 `active=0`、`pending=0`、`orphan_count=0`。

证据：

- [真人自由辩论断线前](screenshots/human-free-turn-before-disconnect.png)
- [60 秒后 AI 接替](screenshots/human-ai-substitution-after-60s.png)
- [短暂断线后返回比赛](screenshots/human-return-after-disconnect.png)

### 双人大厅、准备门禁与并发抢座复测

- QA 房间 `#257331` 由学生甲创建，学生乙通过六位房间号进入并认领反方席位；两端在
  500ms 内同步真人数量、AI 补位数量、席位姓名和准备状态。
- 两名真人均准备后，房主出现可用的“锁定席位并开始”；学生乙取消准备后，房主立即变为
  “等待全部真人准备”。学生乙释放席位后，房主可继续以 AI 补位方式开始；学生乙重新认领
  并刷新页面后，席位身份和未准备状态均恢复。
- 房主关闭房间后，两个浏览器均自动返回赛事大厅，没有残留错误、幽灵席位或继续比赛入口。
- QA 房间 `#151570` 使用学生乙、学生丙两个独立浏览器同时抢同一个反方席位。最终数据库
  权威状态只保留学生丙；学生乙收到“该席位已被占用”，并立即看到学生丙的席位状态，未
  出现双占、覆盖或跨用户控制权。
- 两个房间均在大厅阶段关闭，没有启动比赛、Agent、MOSS 或浏览器音频播放。

状态：通过。测试房间 `#257331`、`#151570` 均已清理为 `cancelled`。

### 结果、历史、归档和排行榜闭环

- QA 房间 `#192885` 的结果页完整显示 5 段发言、58 条时间线事件、真人逐字稿、AI
  逐字稿和每段历史音频；真人 WebM 在真实浏览器中达到 `readyState=4` 并持续播放，
  没有媒体错误或页面异常。
- “下载完整归档”接口返回 `200 application/json`，响应带附件文件名，归档 schema 为
  `jixia-debate-match-archive` v2，包含房间、赛事、比赛、席位、5 段发言、58 条事件、
  音频引用和 `source_sha256`。自动化工具未捕获 JSON 附件的下载事件，但同一登录会话
  内直接请求获得完整 40,643 字节响应，证明权限、内容和生成链路可用。
- 个人中心历史列表包含房间 `#192885`，可回到对应结果页；“继续比赛”不会错误保留已
  终止房间。
- QA 账号累计积分为 0，正式赛季排行榜仍显示“暂无排名数据”，终止的训练/验收比赛
  没有污染正式排名。
- 结果页在 390×844 与 1440×900 下均无横向溢出，控制台和页面错误为空。

证据：

- [结果页桌面完整记录](screenshots/result-192885-full.png)
- [结果页移动端](screenshots/result-192885-mobile-390x844.png)
- [历史音频播放](screenshots/result-192885-audio-playing.png)
- [个人中心历史记录](screenshots/me-history-after-terminated-match.png)
- [排行榜 QA 隔离](screenshots/rankings-qa-exclusion.png)

## 生产多用户、多房间与故障恢复复测

2026-07-19 在不调用 Agent、ASR、TTS 或裁判服务的前提下，直接对生产 HTTPS、WebSocket、
PostgreSQL 和比赛引擎执行三组可清理并发验证：

1. **多人大厅竞态**
   - 同一账号使用 4 个浏览器会话同时认领 4 个不同房间，只有 1 个请求成功，其余 3 个
     返回 409；数据库中该用户只有 1 个活动席位。
   - 6 个独立用户同时抢同一席位，只有 1 个成功，且 `seat.claimed` 事件只追加 1 次。
   - 同一房主两个设备同时提交“开始”和“关闭”，只有一个状态转换成功，另一个返回 409；
     不会同时产生比赛和取消事件。
2. **四房间并行状态与 WebSocket 隔离**
   - 真人发言房：成功开始发言，终止后结果页保留 `interrupted` 可审计记录。
   - 自由辩论房：仅本房间从正方切换到反方并记录 `free.turn_timed_out`。
   - 模拟 AI 播放房：仅本房间完成播放并推进到真人回应阶段。
   - 暂停房：保持暂停和 77 秒剩余时间，没有产生任何额外事件。
   - 四条 WebSocket 连接接收的事件 `room_code` 全部与自身房间一致，跨房事件为 0。
3. **引擎异常隔离与人工恢复**
   - 单房连续 3 次未知异常后仅该房间进入暂停隔离，正在进行的真人发言被安全中断；健康
     房间在同一轮调度中仍运行 3 次。
   - PostgreSQL 瞬时异常按 1、2、4、8 秒退避，不会误把房间标记为永久故障。
   - 房主执行“重试当前步骤”后比赛恢复运行，证明异常发生后存在可操作恢复路径。

清理核对：生产房间总数恢复为 48、用户总数恢复为 13，临时账号和临时房间均为 0；
生产 Playwright 桌面/手机再次 `8/8 passed`，可靠音频 82 文件指纹保持不变，全部核心服务
为 RUNNING。

## 问题

### ISSUE-047：源码备份把 GPU 虚拟环境打入归档

| 字段 | 内容 |
|---|---|
| 严重级别 | Medium |
| 分类 | Backup / Portability |

服务器源码目录含 `.venv-cu128` 和 `.quality-venv`，旧脚本只排除精确名称 `.venv`，导致
候选归档膨胀至 7.5GB。异常任务已停止并删除残留；脚本现自动识别服务器目录、排除全部
运行虚拟环境和运行时指针，并在无 `rg` 时使用 `grep` 扫描高置信密钥。最终纯源码包为
4.4MB，且不含 `.env`、数据卷、runtime 或 node_modules。状态：已修复。

### ISSUE-046：滚动发布使健康 API 在公网持续返回 502

| 字段 | 内容 |
|---|---|
| 严重级别 | High |
| 分类 | Deployment / Availability |

Nginx 的 `max_fails=1` 在两个 Worker 依次重启后把二者同时持久摘除，浏览器捕获首页四个
核心接口同秒 502。现禁用社区版 Nginx 的持久被动摘除，保留失败连接向另一 Worker 重试；
滚动脚本新增公网有限重试、自动回滚，并修复 `set -u` 临时链接变量展开错误。重新发布后
公网和直连健康门禁均通过，浏览器 5xx 为 0。状态：已修复并部署。

### ISSUE-045：匿名观战交接校验阻塞 ASGI 事件循环

| 字段 | 内容 |
|---|---|
| 严重级别 | High |
| 分类 | Realtime / Scalability |

每条匿名 WebSocket 在初始快照后同步查询数据库确认序列，观众突发时会阻塞新握手。校验
现在线程池执行，仍保留订阅交接一致性。双 API 各 250 直连均 100% 成功；公网 10×50
分布式测试 500/500 成功，5 房间 200 连接跨房事件为 0。状态：已修复并部署。

### ISSUE-044：单 API 进程限制扩容与无中断发布

| 字段 | 内容 |
|---|---|
| 严重级别 | High |
| 分类 | Architecture / Availability |

现已部署 primary/secondary 双 API，共享 PostgreSQL 和 Redis；REST 与纯状态 WebSocket
负载均衡，音频/ASR/媒体继续固定可靠 primary。引擎只运行一个独立进程，防止双调度。
生产验证两 Worker 数据、序列和 Presence 一致，连接分配约 254/254。状态：已修复并部署。

### ISSUE-043：含 `.env` 的源码备份权限过宽

| 字段 | 内容 |
|---|---|
| 严重级别 | High |
| 分类 | Backup security / Secrets |
| 地址 | `runtime/deploy-backups` |

历史源码包包含服务器 `.env`，但文件权限为 644，备份目录为 755。现已将所有源码包设为
root-only 600，目录设为 700；复核 group/other 可读源码包为 0。私密配置包同样保持 600。
状态：已修复。

### ISSUE-042：控制台仍显示已淘汰的 LightTTS 为当前运行服务

| 字段 | 内容 |
|---|---|
| 严重级别 | Low |
| 分类 | Product consistency / Operations |
| 地址 | `/rooms/:code/control` |

单场控制台仍显示“FunASR / LightTTS”，与生产实际使用的 MOSS 实时语音不一致。现已改为
“FunASR / MOSS 实时语音”，未修改任何语音实现。状态：已修复并部署。

### ISSUE-041：后台首屏加载全部 14 个管理接口

| 字段 | 内容 |
|---|---|
| 严重级别 | Medium |
| 分类 | Admin / Performance / Network efficiency |
| 地址 | `https://117.50.192.216/admin` |

后台首屏现只加载 dashboard，其他八个模块按首次访问加载并缓存；同域并发去重，回切不重复
请求，可显式刷新当前模块。生产浏览器验证首屏 14→1，进入比赛监管后 rooms 只请求一次。
状态：已修复并部署。

### ISSUE-040：Presence 只能在单 API 进程内正确计数

| 字段 | 内容 |
|---|---|
| 严重级别 | High |
| 分类 | Scalability / Realtime / Multi-worker |
| 地址 | `apps/api/app/services/realtime.py` |

旧 Presence 使用进程内字典，多个 API worker 会重复生成上线/离线事件并错误判断最后连接。
现改为 Redis ZSET 租约和 Lua 原子 join/leave/refresh/reap/active，支持 TTL、过期回收和 Redis
故障降级。生产 Redis 两实例验证通过，启动重置已关闭。状态：已修复并部署。

### ISSUE-039：AI 接替后的真人恢复依赖系统管理员

| 字段 | 内容 |
|---|---|
| 严重级别 | High |
| 分类 | Player recovery / Operations / Scale |
| 地址 | 观战页、个人中心、单场控制台 |

新增参赛者申请/撤销、房主审批、管理员审批和终局失效状态机。严格校验活跃发言、席位归属、
跨房间冲突、账号和权限；匿名投影不暴露申请。生产双用户 QA 流程完成申请、幂等重放、房主
批准和恢复，临时数据已清理。状态：已修复并部署。

### ISSUE-038：首页主标题在常用桌面宽度出现单字孤行

| 字段 | 内容 |
|---|---|
| 严重级别 | Low |
| 分类 | Visual design / Typography |
| 地址 | `https://117.50.192.216/` |

1440px 下“系统自动开赛”曾被浏览器拆成单独的“系”和下一行“统自动开赛”。标题现改为
三条稳定语义行，生产测量宽度为 652、584、438px。证据见
[修复后首页](parallel-round2-20260719/screenshots/home-hero-fixed-1440.png)。状态：已修复并部署。

### ISSUE-037：并行 pytest 进程会互相删除共享 SQLite 数据库

| 字段 | 内容 |
|---|---|
| 严重级别 | Medium |
| 分类 | Test infrastructure / Parallel QA |
| 地址 | `apps/api/tests/conftest.py` |

测试原来固定使用 `storage/pytest-v2.db`、统一媒体和归档目录，多进程会产生 `disk I/O
error` 和假 503。现在每个 pytest 进程使用独立临时运行目录并在 session 后清理；两组 API
测试并行运行分别 5/5、2/2 通过，残留目录为 0。状态：已修复。

### ISSUE-036：后台单项写操作会重复刷新全部管理数据

| 字段 | 内容 |
|---|---|
| 严重级别 | Medium |
| 分类 | Admin / Performance / Maintainability |
| 地址 | `https://117.50.192.216/admin` |

Agent 等单项修改原来会在 PATCH 后重新请求全部 14 组管理端点。现在按业务域刷新，典型操作
从约 15 个请求降至 3 个；三处分页统一为具名、可播报且有加载保护的共享组件。状态：已修复
并部署。

### ISSUE-035：终局参赛者缺少再次开赛闭环

| 字段 | 内容 |
|---|---|
| 严重级别 | Medium |
| 分类 | Player journey / Retention |
| 地址 | `/rooms/:code/result` |

新增同题再赛 API 和结果页入口，沿用赛事、题目、可见性与本人席位，严格校验参赛身份、
赛季、题库、重复参赛和幂等键。生产一次性 QA 流程完成开赛、终止、再赛、幂等重放、关闭
和清理。状态：已修复并部署。

### ISSUE-034：初始快照与实时订阅之间可能遗漏唯一一次房间更新

| 字段 | 内容 |
|---|---|
| 严重级别 | High |
| 分类 | Realtime / State isolation / Recovery |
| 地址 | `/ws/rooms/:code`、`apps/web/lib/use-room.ts` |

旧连接在发送初始快照后才注册 RoomHub；窗口内唯一状态更新可能直到 heartbeat 才可见。
动态切换房间时，旧状态也可能短暂残留。服务端现在先注册再执行权威 `seq`/可见性同步；
前端清空旧房间并拒绝迟到或跨房间 payload。发布后 500 个匿名观战连接 0 失败，生产 E2E
10/10 通过。状态：已修复并部署。

### ISSUE-033：匿名观战投影暴露内部语音诊断与供应商字段

| 字段 | 内容 |
|---|---|
| 严重级别 | High |
| 分类 | Security / Privacy / Public projection |
| 地址 | `/api/rooms/:code/public`、`/api/rooms/:code/result`、`/ws/rooms/:code` |
| 复现视频 | N/A |

**现象**

匿名观众虽然不能执行写操作，但旧公开投影沿用了参赛者房间结构，可能把 TTS session、
音色、采样格式、传输轨道、服务端时间点、Provider 诊断等内部字段送到浏览器。这些字段
不是观战必需内容，也扩大了外部服务和实时语音实现的暴露面。

**修复与复测**

- REST、结果页与 WebSocket 统一改为严格匿名 allowlist；参赛者投影保持兼容，管理员仍可
  查看完整诊断。
- 匿名只保留舞台渲染、字幕和连续播放需要的最小字段；Agent/TTS Endpoint、Provider、
  session、voice、transport、采样格式和内部延迟诊断全部剔除。
- 后端新增 REST、结果与实时投影回归；完整 API `357 passed`。
- 生产公开 REST 递归扫描和匿名 WebSocket 首帧扫描均未发现禁止字段；观战字幕、音频 URL、
  播放时间点和实时连接保持正常。

状态：已修复并部署。

### ISSUE-032：管理后台危险操作、分页和并发请求缺少一致性保护

| 字段 | 内容 |
|---|---|
| 严重级别 | Medium |
| 分类 | Admin / Reliability / UX |
| 地址 | `https://117.50.192.216/admin` |
| 复现视频 | N/A |

**现象**

用户停用、管理员角色、赛事/赛季状态和 QA 标记等高影响操作缺少统一确认；快速切换筛选或
分页时，较慢的旧响应可能覆盖新结果；超大页码返回空表而不是最后一页，未知房间状态也会
进入查询路径。

**修复与复测**

- 高影响操作增加明确确认和重复提交保护；加载期间禁用相关分页与筛选控件，错误使用
  `role=alert` 对辅助技术可见。
- 用户、房间和审计列表使用请求序号，过期响应不再覆盖当前视图。
- 后端分页把超大合法页码收敛到最后一页；房间状态改用严格白名单，未知值返回 422。
- 生产管理员会话验证：`status=classroom` 返回 422；`page=99999&page_size=20` 返回最后
  第 3 页及 9 条记录，而非空白页。

状态：已修复并部署。

### ISSUE-031：手机导航与赛事页签不满足完整键盘操作

| 字段 | 内容 |
|---|---|
| 严重级别 | Medium |
| 分类 | Accessibility / Navigation |
| 地址 | 首页导航、`/competitions/:slug` |
| 复现视频 | N/A |

**现象**

手机菜单打开后按 Escape 无法关闭；赛事详情虽然使用 tab 角色，但只能通过 Tab 逐个访问，
不支持 WAI-ARIA 页签常用的左右方向键、Home 和 End，也没有完整的 roving tabindex 与
tabpanel 关联。

**修复与复测**

- Escape 关闭手机菜单并把焦点归还菜单按钮。
- 赛事页签支持 ArrowLeft、ArrowRight、Home、End，补齐 `aria-controls`、tabpanel 和
  单焦点 roving tabindex。
- 生产 390px 真实浏览器复测：菜单关闭后焦点回到“打开导航菜单”；赛事介绍按右方向键
  后焦点与选中状态同步移动到“排行榜”。

状态：已修复并部署。

### ISSUE-030：匿名参赛、失效房间与会话加载反馈违反用户预期

| 字段 | 内容 |
|---|---|
| 严重级别 | Medium |
| 分类 | Onboarding / Room lifecycle / UX |
| 地址 | 首页参赛弹窗、`/rooms/:code/*` |
| 复现视频 | N/A |

**现象**

匿名用户输入不存在的房间号会先被强制送去登录；公开运行房间也不能直接观战。会话尚未
加载时，建房弹窗会短暂闪现完整题目和席位设置。已关闭房间的深链接静默回首页，用户不
知道比赛为什么消失。

**修复与复测**

- 匿名加入先调用公开房间预检：不存在和已取消显示原地错误；公开运行房直接进入观战；
  终局进入结果；只有确实需要身份的大厅/私密房才转登录。
- 已登录用户进入运行房时，仅本人拥有席位才进入辩手页，否则进入只读观战。
- 会话确认期间显示稳定占位，不再闪现可编辑设置；加入模式自动聚焦六位房间号。
- 已关闭房间回首页时显示包含房间号的明确说明，并清理地址栏临时参数。
- 生产 Playwright 在 1920px 与 390px 共 `10/10 passed`；关闭房 `#151570` 深链接可见
  “房间已关闭”说明，且最终 URL 已清理为首页。

状态：已修复并部署。

### ISSUE-029：实时字幕可滚动但键盘无法进入

| 字段 | 内容 |
|---|---|
| 严重级别 | Medium |
| 分类 | Accessibility / Watch / Debate UX |
| 地址 | `https://117.50.192.216/rooms/:code/watch` |
| 复现视频 | N/A |

**现象**

长发言会让舞台中央实时字幕形成内部滚动区域，但字幕 `<p>` 没有焦点入口。axe 在桌面和
手机观战页均报告 serious 级 `scrollable-region-focusable`；只使用键盘的学生无法进入该
区域并通过方向键或 Page Up/Page Down 阅读完整发言。

**修复与复测**

- 新增独立 `StageScrollAccessibility` 增强器，由观战页和辩手页加载，为实时字幕补充
  `tabindex=0`，离开页面时自动清理属性，并提供清晰的 `:focus-visible` 轮廓。
- 没有修改冻结的 `debate-stage.tsx`，也没有改动字幕文本、WebSocket、TTS、WebRTC、
  播放队列或中断逻辑。
- 新增组件单元测试；完整前端回归由 `197` 增至 `198 passed`，lint 和 Next.js 生产构建
  通过。
- 使用真实键盘从舞台品牌按两次 Tab，焦点准确进入字幕 `<p tabindex="0">`。
- 首页、赛事详情、观战、登录、注册和排行榜在 1440px 桌面与 390px 手机共 12 组
  WCAG 2 A/AA、2.1 A/AA axe 扫描全部零违规；生产 Playwright `8/8 passed`。
- 性能采样保持健康：移动端冷启动首页和赛事详情约 190KB，观战约 341KB；无横向溢出，
  FCP/LCP 均在数百毫秒级，CLS 低于 0.1。
- 生产 release：`subtitle-keyboard-20260719`；可靠音频 82 文件指纹保持
  `3219138b22878e766c94b9a1fa0e422211a6f27dd74afbbb93a8e45f7e295285`。

证据：[真实键盘焦点进入实时字幕](screenshots/watch-subtitle-keyboard-focus.png)。

状态：已修复并部署。

### ISSUE-028：可靠音频校验器把服务器虚拟环境和未同步元数据误判为语音漂移

| 字段 | 内容 |
|---|---|
| 严重级别 | Medium |
| 分类 | Release reliability / Backup |
| 地址 | `scripts/create_reliable_audio_manifest.py` |
| 复现视频 | N/A |

**现象**

新服务器的 MOSS 目录包含 `.venv-cu128`。原校验器只忽略名称完全等于 `.venv` 的目录，
会扫描虚拟环境中的数千个第三方文件；服务器上未同步的部署说明、静态测试和 egg-info 清单
也会造成假阳性，使可靠音频基线无法作为迁移和发布门禁使用。

**修复与复测**

- 校验器现在忽略所有以 `.venv` 开头的虚拟环境目录，并新增单元测试覆盖 `.venv-cu128`。
- 逐项比较剩余差异，确认核心 Worklet、播放器、LiveKit、Provider、Voice Runtime 和 MOSS
  运行代码均无变化；服务器实际参数仍为 `decode_chunk_frames=3`、`do_sample=true`。
- 只同步可靠基线中的部署说明、静态测试和生成元数据，不重启 MOSS、不修改模型、音色、
  WebRTC 或浏览器播放链路。
- 服务器最终通过 82 文件冻结清单，指纹保持
  `3219138b22878e766c94b9a1fa0e422211a6f27dd74afbbb93a8e45f7e295285`。

状态：已修复并在新服务器验证。

### ISSUE-027：后台仍可编辑旧系统归档，正式赛名称和赛事产品不一致

| 字段 | 内容 |
|---|---|
| 严重级别 | Medium |
| 分类 | Product consistency / Data integrity / Admin UX |
| 地址 | `https://117.50.192.216/admin` |
| 复现视频 | N/A |

**现象**

- 公开大厅统一显示“4v4 人机辩论正式赛”，后台赛事、自动流程和房间监管仍显示旧名称
  “4v4 人机辩论日常赛”。
- “旧系统历史比赛”标注为只读，却仍提供绑定赛季、添加题目和重新启用赛事操作；
  “旧系统只读流程”仍可创建新版本，REST API 也没有阻止绕过界面的修改。
- 管理员审计页继续直接展示 `classroom.member.upsert` 和 `classroom_membership`，用户角色与
  当前审计动作也以内部英文枚举显示。
- 阶段预设语音说明仍写“回退 LightTTS”，与当前 MOSS 实时 TTS 链路不一致。

**修复与复测**

- 数据库迁移 `0023_competition_catalog_cleanup` 和幂等 seed 将旧默认名称统一为
  “4v4 人机辩论正式赛”，但不会覆盖管理员自定义名称。
- 历史赛事和历史流程在前端显示“永久只读/只读归档”，不再渲染任何编辑按钮；后端对
  修改赛事、赛季、题库、题目状态、创建流程版本或绑定历史赛事统一返回 409。
- 管理端过滤已退役教师、课堂、教学活动、组织、同意和研究域的历史审计记录；保留数据库
  备份中的原始审计证据。当前赛事审计动作、目标和用户角色改为中文产品文案。
- 语音说明改为“回退 MOSS 实时 TTS”，仅修正文案，未修改语音实现。
- 完整回归：后端 `354 passed`、前端 `197 passed`、Next.js 生产构建通过、空库 Alembic
  升级到 `0023` 通过；生产 Playwright 桌面/手机共 `8/8 passed`，控制台和页面错误为空。
- 生产 release：`competition-catalog-20260719`；API、Web、Engine、Worker、PostgreSQL、
  Redis、MOSS、LiveKit、FunASR 和独立 Agent 服务全部 RUNNING。

证据：

- [正式赛名称与历史赛事只读](screenshots/admin-competition-catalog-readonly.png)
- [旧系统自动流程只读](screenshots/admin-legacy-flow-readonly.png)
- [赛事化中文审计日志](screenshots/admin-competition-only-audit.png)

状态：已修复并部署。

### ISSUE-026：生产 Playwright 门禁仍断言旧首页并硬编码已删除房间

| 字段 | 内容 |
|---|---|
| 严重级别 | Medium |
| 分类 | QA / Release reliability |
| 地址 | `apps/web/e2e/public-platform.spec.ts` |
| 复现视频 | N/A |

**现象**

只读生产 E2E 首次运行 6 项中 4 项失败：大厅用例仍查找早期文案“让每一次交锋”“选择你的
赛场”和旧赛事名称；观战用例硬编码已删除的房间 `#278571`。桌面和手机各产生两项假失败，
导致测试既不能证明当前版本，也会在正常清理测试房间后持续失效。

**修复与复测**

- 首页断言改为当前赛事化信息架构、正式赛名称、“参赛者自主组局”和“场可观战”，并明确
  禁止重新出现“场进行中”的错误统计语义。
- 观战用例从 `/api/live-rooms` 读取当前公开房间，存在房间时验证真实只读舞台；没有公开房间
  时明确跳过，而不是依赖永久测试数据。
- 新增 `/teacher`、`/teacher/consents`、`/admin/classrooms` 的生产 404 回归，同时验证页面
  没有教师/课堂产品入口和横向溢出。
- 更新后桌面 1920 与手机 390 两个项目共 8 项全部通过，耗时 13 秒；测试只读，没有创建
  账号、房间或语音任务。

状态：已修复。该变更只更新发布质量门禁，不需要重启生产服务或修改音频基线。

### ISSUE-025：自由辩论的屏幕阅读器把同阵营四席都称为“当前发言席位”

| 字段 | 内容 |
|---|---|
| 严重级别 | Low |
| 分类 | A11Y / Content |
| 地址 | `https://117.50.192.216/rooms/214317/watch` |
| 复现视频 | N/A |

**现象**

自由辩论通过阵营前缀高亮当前可发言的一方，例如 `activeSeatKey="neg_"` 会高亮全部四个
反方席位。视觉上可以理解为“反方本轮可发言”，但每张席位卡的隐藏辅助文本统一追加
“当前发言席位”，导致屏幕阅读器连续宣布四个不同席位都是唯一的当前发言席位。

**修复与复测**

- 新增独立 `StageSeatAccessibility`，不修改冻结的 `debate-stage.tsx`。自由辩论尚无实际
  发言者时，将原错误文本从可访问树隐藏，并追加“本轮可发言席位”；同时在阵营标题增加
  “当前可发言阵营”。组件不改视觉高亮、发言权限、状态机或任何音频代码。
- 暂停状态明确播报“本轮可发言席位，比赛已暂停”和“本轮轮到的阵营，比赛已暂停”，
  避免辅助技术用户误以为此刻仍可开始发言。
- 固定发言阶段及自由辩论已经产生 `active_speech` 时继续保留唯一的“当前发言席位”。
- 增强器不直接改写 React 管理的原文本，而使用隐藏原错误语义和追加等价辅助文本的方式，
  保证在线/离线等席位状态仍能随新快照正确更新；阶段、阵营切换和卸载时会完整清理。
- 已覆盖 1v1、4v4、正反方切换、固定阶段、自由辩论、已有实际发言、暂停、WebSocket
  快照更新、观战页、辩手页、卸载清理、键盘焦点和 axe。前端全量 225 项通过，生产构建
  成功，ESLint 0 error。

证据：[暂停自由辩论观战页](screenshots/paused-watch-mobile-audit.png)。

状态：已修复，等待随本轮版本部署。受保护的 `debate-stage.tsx`、AudioWorklet、TTS、
LiveKit 和浏览器播放实现均未修改。

### ISSUE-024：结果时间线直接显示内部事件名 `audio.rtc.started`

| 字段 | 内容 |
|---|---|
| 严重级别 | Low |
| 分类 | Content / Result history |
| 地址 | `https://117.50.192.216/rooms/192885/result` |
| 复现视频 | N/A |

**现象**

旧比赛结果页的其他事件均使用中文，但实时语音轨道开始事件直接显示内部枚举
`audio.rtc.started`。普通参赛者无法理解该事件，内部实现名也不应成为公开时间线文案。

**修复与复测**

- 补全实时语音、语音流、录音、Provider 重试、发言复用、退出席位和赛果生成等事件的
  中文名称。
- 未知事件不再直接回显内部枚举，而是按 audio、speech、stage、judge 等类别显示安全、
  可理解的状态更新名称；管理员仍可通过审计与后台日志定位具体内部事件。
- 生产结果页复测显示“实时语音播放开始”，不再包含 `audio.rtc.started`，50 条已加载事件
  中没有其他英文内部枚举，浏览器错误为空。
- 单元测试覆盖已知实时事件和未知事件 fallback；生产 release：`event-labels-20260719`。

状态：已修复并部署；只修改结果页事件展示映射，未修改音频生成、传输或播放逻辑。

### ISSUE-023：暂停房间被赛事卡片统计为“进行中”

| 字段 | 内容 |
|---|---|
| 严重级别 | Low |
| 分类 | Content / Status semantics |
| 地址 | `https://117.50.192.216/`、`/competitions/daily-4v4` |
| 复现视频 | N/A |

**现象**

公开赛事的 `live_count` 按产品规则包含 preparing、running、paused、judging 四类仍可进入
观战的房间，但首页和赛事详情统一显示“X 场进行中”。生产唯一公开房间当前为暂停状态，
因此页面同时出现“1 场进行中”和“比赛已暂停”，语义自相矛盾。

**修复与复测**

- 赛事卡片和详情元信息统一改为“X 场可观战”。
- 首页区块改为“公开比赛 / 可进入观战”，空状态改为“暂无可观战的公开比赛”；赛事详情
  对应区块改为“可观战的比赛”。
- 每个房间继续保留准确状态标签，例如暂停房间仍显示“比赛已暂停”，没有弱化异常状态。
- 单元测试加入暂停房间，明确禁止再次渲染“场进行中”；生产首页复测显示“1 场可观战”
  与“比赛已暂停”，浏览器错误为空。
- 生产 release：`watchable-labels-20260719`。

状态：已修复并部署；只修改公开页面状态文案，不改变房间统计、状态机或语音链路。

### ISSUE-022：手机后台把多数管理模块隐藏在无提示的横向滚动区

| 字段 | 内容 |
|---|---|
| 严重级别 | Medium |
| 分类 | UX / Mobile / Administration |
| 地址 | `https://117.50.192.216/admin` |
| 复现视频 | N/A |

**现象**

在 390px 宽度下，后台 9 个管理模块仍渲染为单行横向导航，容器宽度 364px、内容宽度
1048px。首屏只能看到“系统总览、比赛监管、用户管理”和一部分“赛事与题库”，其余
“自动流程、AI 与语音、媒体存储、结果复核、审计日志”完全位于屏幕外，页面没有滑动
提示。虽然辅助技术仍能枚举按钮，但普通手机用户容易误以为后续模块不存在。

**修复与复测**

- 手机端改为明确标注的“管理模块”下拉框，9 个模块均作为原生 option 展示；桌面端继续
  使用原有侧边导航，平板布局不变。
- 下拉选择复用统一切换逻辑；选择“审计日志”仍会加载最新审计数据，不只是切换静态内容。
- 390×844 生产复测：下拉框宽 340px、无横向溢出，原横向导航隐藏；选择“审计日志”后
  标题、表格和数据均正确出现，浏览器错误为空。
- 组件测试覆盖 9 个 option、默认值和选择后内容切换；生产 release：
  `admin-mobile-picker-20260719`。

证据：[修复后的手机后台模块选择](screenshots/admin-mobile-picker-fixed.png)。

状态：已修复并部署；未修改辩论舞台、TTS、LiveKit 或浏览器播放代码。

### ISSUE-001：首页冷启动发生严重布局跳动

| 字段 | 内容 |
|---|---|
| 严重级别 | Medium |
| 分类 | Performance / Visual |
| 地址 | `https://117.50.192.216/` |
| 复现视频 | [issue-001-home-cls.webm](videos/issue-001-home-cls.webm) |

**现象**

全新 Chromium 会话加载首页时，主视觉和下方内容在首屏渲染后发生明显位移。三次独立 Web Vitals 复测的 CLS 均为 `0.64`，远高于良好阈值 `0.1`；TTFB 为 17–24 ms、FCP 为 156–176 ms，说明问题不是服务器慢，而是客户端首次布局不稳定。

**复现步骤**

1. 使用全新浏览器会话打开首页。
2. 观察首屏标题、赛事区和直播区在数据或字体加载后的位移。
3. 页面稳定后的状态见 [issue-001-home-after-load.png](screenshots/issue-001-home-after-load.png)。
4. 结构化证据见 `performance/home-vitals-1.json` 至 `home-vitals-3.json`。

**预期**

首屏应为服务器输出的稳定骨架，字体和赛事数据加载不应推动现有内容；CLS 应低于 `0.1`，目标低于 `0.05`。

**修复与复测**

已发布 `ui-loading-stability-v2-20260719`：客户端页面的加载占位现在覆盖导航以下的
完整视口，页脚不会在数据返回前进入首屏再被整体推走。修复后三个全新 Chromium
上下文的 CLS 均为 `0.06446`，从 `0.64` 降低约 90%，达到 `< 0.1` 的良好门禁。
结构化结果见 [home-vitals-after-fix.json](performance/home-vitals-after-fix.json)。

状态：已修复并部署。剩余约 `0.064` 来自加载占位切换为真实首页内容，后续可通过
首页服务端数据获取或同构骨架继续压低，但不再阻塞使用。

### ISSUE-002：正式排行榜公开展示自动化测试账号

| 字段 | 内容 |
|---|---|
| 严重级别 | Medium |
| 分类 | Content / Data quality |
| 地址 | `https://117.50.192.216/rankings` |
| 复现视频 | N/A |

**现象**

第一赛季正式榜单当前展示“自动化测试甲”“自动化测试乙”，且二者拥有正式积分、胜场和平均评分。平台面向学生收集真实辩论数据，测试身份进入公开榜单会降低可信度，并污染赛季统计和后续研究数据。

**证据**

[rankings-desktop.png](screenshots/rankings-desktop.png) 与 `rankings-snapshot.txt`。

**预期**

生产验收用户和 QA 比赛必须标记为测试数据，不参与公开榜单、个人排名、赛季统计和研究导出；管理员仍可在专用 QA 视图中查看。

**修复与复测**

已发布 `0020_test_data_scope` 与 `qa-room-scope-20260719`：

- 用户增加 QA 身份标记；管理员可在用户管理中标记测试账号。
- QA 账号创建或加入房间时，整场自动成为 QA 测试数据。
- QA 房间不执行积分结算；历史房间改为 QA 时按正式比赛重新构建受影响选手的排行榜聚合。
- 公开排行榜过滤 QA 身份，研究导出在质量和授权过滤前排除 QA 房间。
- 管理员比赛监管增加正式/QA 筛选，并可将单个历史房间永久标记为 QA。
- 生产库现有 8 个验收账号和 20 个验收房间已经完成分类；正式排行榜复测显示“暂无排名数据”，不再出现自动化测试甲、乙。

证据：

- [修复后的公开排行榜](screenshots/rankings-after-qa-filter.png)
- [管理员 QA 身份管理](screenshots/admin-qa-data-scope.png)
- [管理员 QA 房间筛选](screenshots/admin-qa-room-filter.png)
- [单房间人工分类](screenshots/admin-room-manual-qa-classification.png)

状态：已修复并部署。

### ISSUE-020：切换教学活动时短暂保留上一活动的房间控制链接

| 字段 | 内容 |
|---|---|
| 严重级别 | Medium |
| 分类 | Functional / UX / Classroom operations |
| 地址 | `https://117.50.192.216/teacher` |
| 复现视频 | [issue-020-stale-activity-dashboard.webm](videos/issue-020-stale-activity-dashboard.webm) |

**现象**

教师从已有房间的活动切换到尚未开房的另一活动时，选中活动的标题会立即更新，但旧活动
的分组、房间号以及“观战/控制”链接会继续显示一小段时间，随后才被新活动看板替换。
在这段请求窗口内，页面同时显示新活动标题和旧活动房间，教师快速点击会进入错误比赛。

**复现步骤**

1. 打开已有房间的“QA 同意闭环 20260719”，页面显示房间 `#953246`：
   [切换前活动](screenshots/issue-020-step-1-first-activity.png)。
2. 点击房间数为 0 的“QA 多人辩论回归 20260719”。
3. 新活动标题已经出现，但旧活动的 `#953246`、“观战”和“控制”仍可点击：
   [错误的瞬时混合状态](screenshots/issue-020-step-2-stale-room.png)。
4. 等待活动看板请求完成后，旧房间才消失并显示“尚未开房”：
   [最终正确状态](screenshots/issue-020-step-3-corrected.png)。

**预期**

选中活动改变时应立即清空上一活动的看板和所有操作链接，显示与当前活动绑定的加载状态；
只有响应中的 `activity_id` 与当前选择一致时才能恢复房间操作，避免慢响应覆盖新选择。

状态：已随教师/教学活动功能域整体移除并完成生产验证；旧页面与接口均为 404。

### ISSUE-021：课堂与当前活动的录音同意范围文案不明确

| 字段 | 内容 |
|---|---|
| 严重级别 | Low |
| 分类 | Content / UX / Classroom operations |
| 地址 | `https://117.50.192.216/teacher` |
| 复现视频 | N/A |

**现象**

课堂卡片统计全班成员，活动看板只统计当前活动参与者，但页面分别显示“还缺 1 人同意”
和“0 待同意”，没有说明统计范围。教师容易把两个数字理解为数据不一致，而不是“全班
仍有一人未同意、当前活动的两位学生均已同意”。

证据：[范围不明确的教师看板](screenshots/continuation-teacher-desktop.png)。

**预期**

课堂卡片明确使用“全班录音同意已就绪 / 全班还缺 N 人同意”，活动摘要使用“本活动待
同意”，让两个合法但不同范围的统计可以同时被理解。

状态：已随课堂/全班同意功能域整体移除并完成生产验证；当前产品不存在该统计范围。

### ISSUE-019：教师看板把已关闭房间显示为内部英文状态 `cancelled`

| 字段 | 内容 |
|---|---|
| 严重级别 | Low |
| 分类 | Content / Classroom operations / Mobile |
| 地址 | `https://117.50.192.216/teacher` |
| 复现视频 | N/A |

**现象**

QA 房间关闭后，教师活动看板在手机端显示 `#953246 · cancelled`。其他房间状态均使用
“等待入场、比赛中、已暂停”等面向教师的中文词汇，只有关闭状态泄漏后端内部枚举，
降低界面一致性，也容易让非技术教师误解。

证据：[教师工作台手机完整页面](screenshots/issue-016-teacher-mobile-final.png)。

**预期**

教师看板统一显示“已关闭”，底层状态值继续保持 `cancelled`，不改变状态机或历史数据。

**修复与复测**

- 教师看板状态映射补充 `cancelled → 已关闭`，只改变显示文案，不修改房间状态值、
  状态机或历史记录。
- 新增组件测试，确认 `#123456 · 已关闭` 可见且页面不再渲染 `cancelled`。
- 生产手机端复测 `#953246 · 已关闭`，页面宽度 390px、无横向溢出。
- 发布 release：`classroom-status-label-20260719`；AudioWorklet SHA-256 保持不变。

证据：[修复后教师工作台手机页面](screenshots/issue-019-fixed-mobile.png)。

状态：历史问题曾修复；相关教师看板现已整体退役并从生产入口移除。

### ISSUE-016：教师数据采集功能无法从管理界面启用

| 字段 | 内容 |
|---|---|
| 严重级别 | High |
| 分类 | Classroom operations / Administration |
| 地址 | `https://117.50.192.216/admin`、`/teacher` |
| 复现视频 | [issue-016-teacher-unreachable.webm](videos/issue-016-teacher-unreachable.webm) |

**现象**

平台已经存在教师活动、知情同意和研究导出相关页面与后端测试，但当前产品界面无法让
学校管理员启用这些能力：

- 系统管理没有“学校/班级/教师”模块；
- 用户管理只显示 `user` 和 `system_admin`，操作只有设为管理员、停用、重置密码和
  QA 标记，没有授予教师或加入组织的入口；
- 当前系统管理员直接访问 `/teacher` 会在没有原因说明的情况下跳回赛事大厅；
- 主导航没有教师工作台入口。

因此，即使底层数据结构已经存在，管理员仍无法完成“创建教学活动 → 指定教师 →
邀请学生 → 管理同意 → 导出数据”的实际开通流程。对于需要组织大量学生比赛并持续
收集研究数据的学校场景，这是上线阻断问题。

**复现步骤**

1. 使用系统管理员登录并进入系统管理：[管理后台](screenshots/issue-016-admin-start.png)。
2. 打开用户管理，角色列只有 `user/system_admin`，没有教师授权或组织成员操作：
   [用户管理](screenshots/continuation-admin-users.png)。
3. 在同一管理员会话直接访问 `/teacher`。
4. 页面无提示跳回赛事大厅，也没有下一步操作入口：
   [教师入口被重定向](screenshots/issue-016-teacher-redirect.png)。

**预期**

系统管理员应能在管理后台创建学校/研究组织、把现有账号授权为教师或组织管理员、撤销
授权，并进入教师工作台验证配置。教师和系统管理员的权限边界、成员关系和审计事件必须
由服务端校验；普通学生不能通过修改前端路由获得教师数据。

**修复与生产复测**

- 新增系统管理员专用的“学校与课堂”页面，可创建学校、课堂，并按学生、教师、教学
  管理员、研究员四种最小权限维护现有账号；成员变更均写入管理员审计日志。
- 登录与会话响应增加服务端计算的 `teacher_access`；普通学生访问 `/teacher` 仍返回
  赛事大厅且主导航不显示教学入口，获得教师成员关系后刷新即可进入，不依赖前端角色
  伪造。
- 恢复教师工作台、知情同意政策和研究导出页面；教师可创建活动、预检并提交名单、
  使用固定随机种子分组、保存席位、批量开房和查看活动房间。
- 生产环境使用系统管理员把 QA 账号 `qa_student_a_1784403261` 授权为教师，并把
  `qa_student_b_1784403561` 加入 QA 灰度班。授权前教师账号访问 `/teacher` 被正确
  拒绝；授权后导航出现“教学活动”，工作台显示所属课堂和同意状态。
- 已创建 QA 活动“QA 多人辩论回归 20260719”，成功导入两名 QA 学生并完成确定性
  分组和席位保存。继续开房测试发现独立的 ISSUE-017，教师授权与基础课堂流程本身
  已可用。
- 完整回归：API `407 passed`，Web `206 passed`，TypeScript、ESLint、Next.js
  production build 和依赖审计通过；生产定向 API 测试 `11 passed`。
- 生产 release：`classroom-operations-20260719`；上线前 PostgreSQL 与源码备份位于
  `classroom-operations-20260719T022529Z`。可靠 AudioWorklet SHA-256 未变化。

证据：

- [学校与课堂管理](screenshots/issue-016-admin-classrooms-desktop.png)
- [教师授权完成](screenshots/issue-016-admin-teacher-assigned-final.png)
- [教师工作台](screenshots/issue-016-teacher-workspace-access.png)
- [教学活动创建](screenshots/issue-016-teacher-activity-created.png)
- [学生名单预检](screenshots/issue-016-roster-preview.png)
- [确定性分组预览](screenshots/issue-016-group-preview.png)

状态：历史问题曾修复；学校、课堂、教师授权与教学工作台现已整体退役。

### ISSUE-017：教师保存分组后刷新页面，批量开房入口永久消失

| 字段 | 内容 |
|---|---|
| 严重级别 | High |
| 分类 | Classroom operations / Recovery UX |
| 地址 | `https://117.50.192.216/teacher` |
| 复现视频 | [issue-017-provision-cta-lost.webm](videos/issue-017-provision-cta-lost.webm) |

**现象**

教师导入学生、预览分组并保存席位后，页面会短暂显示“批量创建 1 个房间”。只要刷新、
重新进入教师工作台或切换活动，分组和席位仍显示为已保存，但批量开房按钮消失。活动
看板只剩“尚未开房”，没有继续操作入口。

教师只能重新输入随机种子、再次预览并再次保存同一分组，才能临时恢复开房按钮。学校
批量组织比赛时，一次刷新、断网重连或隔天继续操作都会把工作流带入死胡同。

**复现步骤**

1. 在 QA 活动中已导入两名学生并保存席位；刷新后看板显示“2 人已分配、0 个房间”，
   但没有开房按钮：[保存状态无下一步](screenshots/issue-017-cta-step-1-saved-no-action.png)。
2. 重新输入相同随机种子并预览分组：
   [重新预览](screenshots/issue-017-cta-step-2-preview.png)。
3. 再次保存相同席位后，“批量创建 1 个房间”重新出现：
   [开房入口短暂出现](screenshots/issue-017-cta-step-3-action-visible.png)。
4. 刷新页面，席位仍在、房间仍为 0，但开房入口再次消失：
   [刷新后入口丢失](screenshots/issue-017-cta-result-after-reload.png)。

**预期**

服务端已保存且尚未开房的席位方案应在任何刷新、重新登录和设备切换后恢复可执行的
“批量创建房间”动作。前端不应依赖只存在于当前组件内存中的分组预览才能继续持久化
工作流；重复点击还必须保持幂等，不生成重复房间。

**修复与生产复测**

- 活动看板新增服务端权威 `provisioning` 状态，统一返回当前状态、说明、可否开房、
  `expected_revision` 和房间数；页面不再依赖当前组件内存中的分组预览。
- 已保存的完整席位方案在刷新、重新登录和切换设备后仍显示“可以批量开房”；未发布
  政策、等待学生同意、账号停用和已经开房均显示明确的持久化状态及下一步操作。
- QA 活动“QA 同意闭环 20260719”使用两名已同意学生完成分组保存。刷新教师页面后，
  “批量创建 1 个房间”仍存在，revision 从 1 正确恢复为 2。
- 点击后创建 QA 房间 `#953246`，活动看板显示观战和控制入口；两名学生的个人中心都
  出现该房间，可进入大厅并确认准备。房主页面在两人准备后正确启用“锁定席位并开始”。
- 本轮不启动比赛和 TTS；完成课堂开房、席位和权限验收后由 QA 房主关闭房间，没有
  留下运行中比赛。

证据：

- [修复后无政策状态仍可恢复](screenshots/issue-017-fixed-persistent-blocker.png)
- [刷新后开房入口仍存在](screenshots/issue-017-fixed-step-2-after-reload-final2.png)
- [房间创建并进入活动看板](screenshots/issue-017-fixed-step-3-room-created-final.png)
- [两名课堂学生已准备](screenshots/issue-018-classroom-room-ready.png)

状态：历史问题曾修复；教学活动批量开房现已退役，通用赛事建房流程继续保留。

### ISSUE-018：课堂开房要求学生先同意录音，但学生端没有同意入口

| 字段 | 内容 |
|---|---|
| 严重级别 | Critical |
| 分类 | Consent / Classroom operations / Data collection |
| 地址 | `https://117.50.192.216/me`、`/teacher` |
| 复现视频 | N/A |

**现象**

教师工作台会阻止未完成录音同意的课堂活动开房，后端也明确要求活动内所有学生同意当前
录音政策；但学生个人中心没有“我的课堂”“待处理同意”或任何录音政策入口。学生只有
进入已创建的比赛房间后才能在比赛舞台看到同意控件，而课堂房间又必须在同意完成后才
能创建，形成循环阻断。

生产 QA 班当前有两名学生待同意：教师工作台显示“还缺 2 人同意”，学生乙的
`/api/consents/me/classrooms` 已能读取所属 QA 灰度班及同意状态，但 `/me` 页面只包含
比赛、积分、历史和账号安全，完全没有可执行的同意动作。

证据：[学生个人中心缺少课堂同意入口](screenshots/issue-018-student-consent-missing.png)。

**影响**

- 新学校无法完成首个“发布政策 → 学生同意 → 批量开房 → 合规采集”的闭环；
- 教师只能看到缺失人数，无法给学生一个明确的处理路径；
- 课堂批量比赛和研究数据采集在正式环境中实际不可启动。

**预期**

学生个人中心应列出当前加入的课堂、学校、录音政策版本和同意状态，并复用现有安全
同意组件完成阅读、主动勾选、同意和撤回。教师工作台应把“未发布政策”“等待学生
同意”“可以开房”呈现为明确的持久化流程状态，并提供可复制或可说明的学生处理路径。

**修复与生产复测**

- 个人中心新增“我的课堂与录音同意”，并行加载课堂同意状态，不阻塞比赛历史、积分和
  账号安全内容。
- 每个课堂显示学校、政策版本和当前状态；没有政策时明确说明无需操作，发布政策后由
  学生主动打开完整内容、勾选并同意。现有同意组件仍支持撤回、版本变化刷新、焦点循环
  和服务端 CSRF/成员权限校验。
- 教师工作台将合规状态纳入开房流程：无政策时提供“发布录音政策”，有缺失同意时说明
  学生应前往“我的”，全部完成后才启用批量开房。
- 系统管理员在 QA 自动化学校发布不可变录音政策 v1；系统验收学生乙、丙分别在个人
  中心阅读、勾选并同意。活动看板随即从阻断状态变为“可以批量开房”，成功完成 ISSUE-017
  的后续开房闭环。
- 完整本地门禁：API `408 passed`、Web `209 passed`、ESLint 0 error、TypeScript、
  Next.js production build 和依赖审计通过。
- 生产 release：`classroom-consent-recovery-2-20260719`；数据库、自源码和上一 Web
  release 已备份到 `classroom-consent-recovery-20260719T025527Z`。
- AudioWorklet SHA-256 前后均为
  `de373b01d9b5587bdbaa38115227d99a21b2ec3645f3046bfbda5068663ba219`；未修改或重启
  MOSS、LiveKit、FunASR、Engine、Worker 和受保护音频文件。

证据：

- [学生端无政策说明](screenshots/issue-018-fixed-student-no-policy.png)
- [QA 录音政策 v1 发布](screenshots/issue-018-policy-published.png)
- [学生完整政策与主动勾选](screenshots/issue-018-consent-step-2-dialog.png)
- [学生乙已同意 v1](screenshots/issue-018-consent-step-3-granted-final2.png)
- [学生丙手机端已同意 v1](screenshots/issue-018-student-c-granted.png)
- [学生同意操作视频](videos/issue-018-student-consent-flow.webm)

状态：历史问题曾修复；课堂录音同意与教师工作台现已整体退役。

### ISSUE-014：Next.js 16 质量门禁失效，依赖锁与线上测试受环境污染

| 字段 | 内容 |
|---|---|
| 严重级别 | Low |
| 分类 | Engineering / Release safety |
| 地址 | 构建与测试工具链 |
| 复现视频 | N/A |

**现象**

- 项目升级到 Next.js 16 后仍执行已经移除的 `next lint`，因此 lint 门禁实际上不可用。
- macOS 生成的旧依赖锁缺少 Linux 所需的可选原生包，服务器执行干净 `npm ci` 失败。
- 原质量脚本对整个历史目录执行格式检查，会把冻结音频基线和 QA 证据也视为待重写文件；
  同时重复构建前端且没有为 `scripts` 设置正确的 Python 导入路径。
- 在生产机直接运行完整 API 套件时，测试继承线上 LiveKit 环境变量，两个本应验证
  “功能关闭/缺少配置”的用例失去隔离；中断测试后 LiveKit 原生 FFI 还会向已经关闭的
  pytest event loop 写日志。该现象属于测试进程生命周期问题，生产 API、比赛引擎和
  LiveKit 服务本身没有退出或降级。

**修复内容**

- 按 [Next.js 官方 ESLint 迁移说明](https://nextjs.org/docs/app/api-reference/config/eslint)
  改用 ESLint CLI 和 flat config；当前为 0 error，仍保留 14 条可见 warning，避免为了
  清零告警而重构已验证的实时音频与 WebSocket effect。
- 使用 Linux Node 22 工具链重新生成跨平台 lockfile；macOS dry-run 与服务器真实
  `npm ci` 均通过，依赖审计为 0 vulnerability。
- 质量脚本只覆盖可执行生产/测试代码，分离 `.quality-venv`，校验 Node 最低版本，修正
  `PYTHONPATH`，并移除重复构建。服务器部署仍使用项目自带 Node 22，不依赖系统旧 Node。
- LiveKit 测试显式固定开关、后端和空凭据，不再继承生产环境；音频文件的 20 项测试在
  本机和生产 Linux 环境全部通过。

**验证**

- 本机统一质量门禁：Ruff、安全扫描、依赖审计、`406 passed` API、`199 passed` Web、
  TypeScript、Next.js production build 全部通过。
- 服务器源码：干净安装、lint、类型检查、结果页定向测试、生产构建通过；LiveKit 音频
  文件 `20 passed`。
- 生产 API 与 Debate Agent 健康检查均为 ready；残留 pytest 进程已精准终止，没有触碰
  Supervisor 生产服务。
- 可靠音频清单仍为 82 个文件，指纹保持
  `3219138b22878e766c94b9a1fa0e422211a6f27dd74afbbb93a8e45f7e295285`。

状态：已修复并同步；本项仅修改开发质量门禁和测试隔离，不改变生产音频热路径。

### ISSUE-015：真人麦克风权限被拒后计时继续，缺少文字发言恢复入口

| 字段 | 内容 |
|---|---|
| 严重级别 | Medium |
| 分类 | Human debate / Recovery UX |
| 地址 | `https://117.50.192.216/rooms/596306/debate` |
| 复现视频 | [continuation-mic-denied-turn.webm](videos/continuation-mic-denied-turn.webm) |

**现象**

在双真人 1v1 QA 房间 `#596306` 中，正方真人进入自己的立论轮次后点击“开始发言”。
Chromium 拒绝麦克风权限，页面能够显示“请在地址栏允许麦克风后重试”，但是：

- 阶段倒计时仍然从 2 分钟持续减少；
- 发言按钮恢复成“开始发言”，没有文字发言入口；
- 非房主辩手没有暂停比赛的能力；
- 如果学校电脑的权限无法及时修复，学生只能等待本轮超时并丢失发言。

现有“ASR 未识别到内容后补充文字”只在已经成功开始录音后出现，无法覆盖
`getUserMedia` 在录音开始前被拒绝的场景。

**复现步骤**

1. 两个真人账号进入训练赛房间并分别认领正反方，全部准备后由房主开始比赛：
   [开始前](screenshots/continuation-two-human-start-before.png)。
2. 等待状态机进入正方真人立论，正方页面显示“轮到你发言”：
   [点击前](screenshots/continuation-mic-denied-before.png)。
3. 在麦克风权限为拒绝的浏览器中点击“开始发言”。
4. 页面只出现权限错误，计时继续且没有替代提交入口：
   [拒绝后](screenshots/continuation-mic-denied-after.png)。

**预期**

权限拒绝、设备缺失或浏览器不支持采集时，当前真人辩手应立即获得明确的文字发言兜底；
提交仍必须经过服务端当前房间、席位和轮次校验。文字兜底不应修改 ASR、TTS、LiveKit
或浏览器音频播放代码。

**修复内容**

- 新增独立 `TextSpeechFallback`，只在“当前真人辩手可发言且尚未开始录音”时显示
  “麦克风不可用？改用文字发言”。正常语音录制一旦开始，入口自动消失，不会与 ASR
  或录音提交争抢同一发言。
- 点击后先使用与语音发言相同的房间级 control lease 绑定当前设备，再通过现有
  `/speech/start` 锁定权威房间、席位和轮次；提交时继续使用 `/speech/finish` 的内容质量、
  幂等和轮次校验，不能通过前端覆盖跨房间或其他席位。
- 编辑器会在服务端成功锁定轮次后才开放，支持 20,000 字、字符计数、提交失败保留内容、
  关闭后重新进入。阶段在输入期间结束时，现有 `timed_out → late_finalized` 机制仍允许
  该次已锁定发言补交。
- 实现放在独立组件和 CSS module；冻结的 `debate-stage.tsx`、ASR worklet、LiveKit、TTS、
  Opus 和浏览器播放器文件均未修改。

**生产复测**

在同一双真人 QA 房间中，当反方总结轮到系统验收学生乙时：

1. 舞台主按钮正常显示“开始发言”，左下角同时出现非抢占式恢复入口：
   [文字兜底入口](screenshots/issue-015-text-fallback-launcher.png)。
2. 点击后服务端生成唯一真人 `speech.started`，编辑器显示“当前轮次已为你保留”：
   [文字编辑器](screenshots/issue-015-text-fallback-editor.png)。
3. 输入完整观点并提交后，状态机立即进入正方总结，舞台实时显示已提交正文：
   [提交完成](screenshots/issue-015-text-fallback-submitted.png)。
4. 房主终止 QA 比赛后，结果页保留该真人发言，比赛摘要为 1 段发言，时间线存在独立
   `发言开始 → 发言完成` 事件：[结果与历史](screenshots/issue-015-result-history.png)。

发布 release：`text-speech-fallback-20260719`。前端完整回归为 `202 passed`，TypeScript、
ESLint 和 Next.js production build 通过；新旧 release 的可靠播放 worklet SHA-256 均为
`de373b01d9b5587bdbaa38115227d99a21b2ec3645f3046bfbda5068663ba219`。82 文件可靠音频
清单指纹仍为 `3219138b22878e766c94b9a1fa0e422211a6f27dd74afbbb93a8e45f7e295285`。

状态：已修复并部署。

### ISSUE-009：管理后台表格在手机端被压缩成逐字竖排，操作列不可达

| 字段 | 内容 |
|---|---|
| 严重级别 | High |
| 分类 | Responsive / UX / Accessibility |
| 地址 | `https://117.50.192.216/admin` |
| 复现视频 | N/A |

**现象**

后台比赛监管和用户管理沿用桌面多列表格，但移动端没有设置合理最小宽度。390px 视口中
7 列比赛数据被强制压入 320px，赛事、状态和阶段逐字竖排；用户管理每行被撑到数百像素
高，真实姓名逐字竖排，账号、角色和操作被裁切。管理员无法在手机上完成控制、观战、
账号停用或权限管理。

修复前证据：

- [比赛监管逐字竖排](screenshots/admin-room-supervision-mobile-390x844.png)
- [用户管理逐字竖排](screenshots/admin-users-mobile-390x844.png)

**修复内容与复测**

- 后台 10 个数据表统一使用可聚焦、带可访问名称的横向滚动区域。
- 900px 以下表格使用至少 860px 的内容宽度，单元格不再逐字换行；页面本身保持 390px，
  只在表格内部滚动。
- 表格顶部明确显示“左右滑动查看完整表格 →”，滚动条和键盘焦点可见；表格内文字按钮
  保留至少 40px 高度。
- 生产复测：比赛表 `318/1009px`、最大可滚动 691px；用户表 `318/968px`、最大可滚动
  650px。左侧房间/姓名和最右侧控制、观战、账号操作均可到达。
- 前端完整回归 `199/199`、TypeScript 和生产构建通过。
- 发布 release：`admin-mobile-tables-20260719`，后续改进已合入当前 release。

证据：

- [比赛表滚动起点](screenshots/admin-room-mobile-scroll-start-fixed.png)
- [比赛表滚动终点](screenshots/admin-room-mobile-scroll-end-fixed.png)
- [用户表滚动起点](screenshots/admin-users-mobile-scroll-start-fixed.png)
- [用户表滚动终点](screenshots/admin-users-mobile-scroll-end-fixed.png)

状态：已修复并部署。

### ISSUE-010：后台仍将停用的 LightTTS 显示为当前服务，MOSS 使用内部键名

| 字段 | 内容 |
|---|---|
| 严重级别 | Medium |
| 分类 | Content / Configuration safety |
| 地址 | `https://117.50.192.216/admin`、`/admin/agent-access` |
| 复现视频 | N/A |

**现象**

生产实际运行 MOSS 实时语音，但系统总览同时显示 `moss_tts_realtime · 正常` 和
`LIGHTTTS · 未配置`；AI 与语音页仍把停用的 LightTTS 放在主要运行配置中，Agent
接入页也写着“LightTTS 音色”。管理员会误以为正式 TTS 缺少配置，甚至可能误改一条
已淘汰路径。

修复前证据：[旧语音配置界面](screenshots/admin-ai-voice-1440x900.png)。

**修复内容与复测**

- 总览将内部键名映射为“MOSS 实时语音合成”，显示 `1/1 个实时端点已就绪`。
- AI 与语音页新增 MOSS 只读运行卡片，明确模型、并发、流式会话和浏览器播放参数由
  服务器部署管理，本页面不会修改可靠语音基线。
- 未启用的兼容 LightTTS 不再出现在主要服务和配置区；数据库配置及回滚数据仍保留，
  只有明确重新启用时才显示。
- Agent 接入页改为中性的“语音音色标识”，不再将席位绑定到旧 TTS 名称。
- RESTful Agent 连接测试成功：`36 ms · ready`；普通学生访问 Agent 接入页仍被重定向。
- 发布 release：`admin-current-voice-detail-20260719`，后续审计改进已合入当前 release。

证据：

- [MOSS 当前服务总览](screenshots/admin-overview-current-moss.png)
- [MOSS 只读运行配置](screenshots/admin-ai-voice-current-moss.png)
- [Agent 连接测试成功](screenshots/admin-agent-access-test-success.png)

状态：已修复并部署；没有修改 TTS 模型、参数、编码、缓冲或播放代码。

### ISSUE-011：7 场终局比赛归档因旧正文格式失效

| 字段 | 内容 |
|---|---|
| 严重级别 | High |
| 分类 | Data integrity / Archive |
| 地址 | `https://117.50.192.216/admin` → 媒体存储 |
| 复现视频 | N/A（录制上下文重启，实际修复证据使用前后截图与审计日志） |

**现象**

媒体引用为 `190/190`，但应归档的 32 场终局比赛中只有 25 场通过校验。7 份归档统一
报告“归档正文格式不受支持”，会阻断后续完整导出、研究复核和比赛数据回溯。

**修复与验证**

- 修复前完整备份服务器 `storage/archives`，压缩包 SHA-256：
  `654ce99c36b5506f7d02e90c8a5aea8527e39f0eb4f14bfbebc75d8032a848d9`。
- 通过系统管理员的受保护接口重新生成全部缺失或校验失败归档，确认提示“已修复 7 份
  比赛归档”。
- 独立接口复核：`expected=32`、`complete=32`、`invalid=0`、`orphan=0`；媒体引用仍为
  `190/190`。
- 修复后再次备份归档，128 个文件，压缩包 SHA-256：
  `75769915a2df9b5e3bd7c6dfe48abc51b8af4696c86f7b8ab017e462102361af`。
- 四个主服务、数据库、Redis、MOSS、FunASR、备份和磁盘健康检查全部通过。

证据：

- [修复前 25/32](screenshots/issue-011-archive-before-final-2.png)
- [修复后 32/32](screenshots/issue-011-archive-after-final-2.png)
- [归档修复审计记录](screenshots/issue-011-archive-audit-refreshed.png)

状态：已修复；未删除比赛、发言、字幕、音频或数据库记录。

### ISSUE-012：同一管理会话内审计日志不会刷新，刚完成的敏感操作不可见

| 字段 | 内容 |
|---|---|
| 严重级别 | Medium |
| 分类 | Audit / UX |
| 地址 | `https://117.50.192.216/admin` → 审计日志 |
| 复现视频 | N/A |

**现象**

管理员完成 Agent 连接测试或归档修复后切换到审计日志，页面继续显示首次打开后台时的
缓存列表。刚执行的 `archives.repair` 不可见，只有整页刷新后才出现，削弱敏感操作的
即时可追溯性。

**修复与复测**

- 每次进入“审计日志”tab 都主动请求第一页最新数据，而不是继续使用首次载入缓存。
- 归档修复完成后立即刷新审计缓存，后续进入审计页无需整页刷新。
- 生产复测顶部记录为 `2026/7/19 08:20:21 · archives.repair · archive_storage · global`；
  网络请求 `/api/admin/audit?page=1&page_size=100&q=` 返回 200。
- 前端完整回归 `199/199`，当前 release：`admin-audit-refresh-20260719`。

证据：[实时刷新的审计页](screenshots/issue-011-archive-audit-refreshed.png)。

状态：已修复并部署。

### ISSUE-013：生产 MOSS 参数偏离可靠基线，长发言尾段撕裂并超时失败

| 字段 | 内容 |
|---|---|
| 严重级别 | High |
| 分类 | Audio / Reliability / Operations |
| 地址 | `https://117.50.192.216/rooms/612684/watch` |
| 复现视频 | N/A |

**现象**

生产 Supervisor 将 MOSS 覆盖为 `decode_chunk_frames=12`、`do_sample=false`，与已验收
清单的 `3/true` 不一致。QA 房间 `#612684` 的短篇立论生成超过 200 秒，源 PCM 尾段
最大相邻跳变约 1.3996，浏览器采样出现一次约 0.7901 的瞬时跳变，最终
`final_ack_timeout` 并安全暂停比赛。

**修复与复测**

- 完整备份修复前配置，只恢复两个可靠运行参数；没有修改模型、音色、LiveKit、Opus、
  缓冲或 AudioWorklet。
- 直接短文本首 PCM 362.3 ms，10.72 秒自然释放；长文本 57.28 秒自然释放，最大跳变
  0.2132，无削波和 `>0.4` 跳变。
- 真实 QA 房间 `#287138` 完成 83.12 秒 Agent → MOSS → LiveKit → Chromium 发言：
  源 WAV 最大跳变 0.2482，浏览器最大块内跳变 0.1470，RTP 丢包 0，启动 concealment
  3120 samples 后整轮不再增长。
- GPU 语音验证器增加可靠 Supervisor 参数校验；定向测试 10/10，生产验证通过。
- 两个 QA 房均已终止，MOSS `active=0`、`pending=0`、`orphan_count=0`，系统健康全绿。

证据：

- [完整修复报告](../audio/20260719-residual-noise/report.md)
- [结构化指标](../audio/20260719-residual-noise/summary.json)
- [轻微残余杂音再次复核](../audio/20260719-residual-noise-followup-2/report.md)

状态：已修复并部署；音频热路径和浏览器播放器保持冻结。

### ISSUE-003：同一浏览器刷新后不会自动恢复席位控制权，接管入口不够易发现

| 字段 | 内容 |
|---|---|
| 严重级别 | Medium |
| 分类 | Functional / Recovery |
| 地址 | `https://117.50.192.216/rooms/456392/debate` |
| 复现视频 | [human-microphone-start.webm](videos/human-microphone-start.webm) |

**现象**

真实 1v1 人人房中，甲、乙均登录、认领席位、准备并自动进入比赛。进入“正方立论”后，甲的主按钮正确显示“开始发言 / 轮到你发言”。随后同一浏览器页面重新挂载或刷新时，服务端对当前 control lease 返回 `409`，主按钮显示“其他设备已接管”，单纯刷新不能恢复。

继续复核后确认系统已有自助恢复：展开“比赛操作”可点击“确认接管到当前设备”，接管成功后当前页面恢复控制权，原设备会变为只读。问题因此不是无恢复能力，而是常见的刷新场景没有自动复用或解释租约，且恢复入口藏在二级面板，学生很容易误认为比赛已经无法继续。

同一页面的 LiveKit 日志还显示该用户先后建立两个不同 device identity 的连接，但 RTP 音频本身没有报错。本问题属于设备控制租约与页面生命周期，不修改冻结的音频播放实现。

**复现步骤**

1. 用户甲创建 1v1 训练房并认领正方，用户乙认领反方；双方准备后由甲开始。
2. 到“正方立论”阶段，甲看到可用的“开始发言”按钮：[human-mic-before.png](screenshots/human-mic-before.png)。
3. 页面随后重新获取 RTC token/control lease，服务端对新的 lease 返回 `409`。
4. 甲页面变为“其他设备已接管”：[human-mic-result.png](screenshots/human-mic-result.png)。
5. 刷新页面后仍不会自动恢复：[device-takeover-after-reload.png](screenshots/device-takeover-after-reload.png)。
6. 展开“比赛操作”并点击“确认接管到当前设备”可以恢复；该 1v1 验收房随后由房主正常提前终止。

**预期**

- 同一标签页刷新应优先复用已保存的 control lease；若因页面生命周期无法确认，应明确说明原因。
- 主按钮进入“其他设备已接管”状态时，应在按钮附近直接提供“本设备接管”入口，而不是只放在二级“比赛操作”面板。
- 接管失败时应说明“另一设备正在发言”或其他可操作原因，并给出房主/管理员恢复路径。

**修复与复测**

已发布 `0021_control_session_lease`：席位租约除了随机 lease 外，还绑定服务端
`UserSession`。同一浏览器登录会话刷新后即使页面生成新 lease，服务端也会自动替换
旧 lease，并向旧页面广播只读事件；不同设备登录仍必须明确确认接管，且另一设备正在
发言时继续禁止跨设备抢占。

真实浏览器复测使用 QA 房间 `#555107`：刷新前主按钮为“开始发言 / 轮到你发言”，
刷新后仍保持相同可用状态，没有出现“其他设备已接管”。服务端事件依次为
`seat.control_acquired(same_session_recovery=false)` 和
`seat.control_taken_over(same_session_recovery=true, forced=false)`。验收后房主正常提前
终止房间。

证据：

- [刷新前](screenshots/issue-003-fix-before-reload.png)
- [刷新后](screenshots/issue-003-fix-after-reload.png)
- [真实刷新复测视频](videos/issue-003-same-session-recovery-fixed.webm)

状态：已修复并部署。

### ISSUE-004：建房弹窗的提交按钮被页脚覆盖，按钮可见但无法点击

| 字段 | 内容 |
|---|---|
| 严重级别 | Medium |
| 分类 | Functional / Responsive UI |
| 地址 | `https://117.50.192.216/` |
| 复现视频 | [issue-004-dialog-footer-obstruction.webm](videos/issue-004-dialog-footer-obstruction.webm) |

**现象**

在 1280×800 桌面视口打开 4v4“创建比赛”弹窗后，底部提交按钮虽然可见，但实际点击
点被 `<footer class="site-footer">` 覆盖，浏览器自动化与真实指针事件都会落到页脚，
学生无法完成建房。截图见
[issue-004-dialog-footer-obstruction.png](screenshots/issue-004-dialog-footer-obstruction.png)。

根因是弹窗位于 `main(z-index:1)` 的堆叠上下文中；弹窗自身的 `z-index:100` 无法越过
后续页脚和粘性导航所在的外部堆叠层。弹窗高度又使用 `90vh`，没有采用移动浏览器更
可靠的动态视口单位。

**修复与复测**

已发布 `dialog-stack-fix-20260719`：

- 页面存在模态弹窗时将 `main` 堆叠上下文提升到导航和页脚之上；
- 弹窗最大高度改为 `calc(100dvh - 40px)`，并允许弹窗自身滚动；
- 打开参赛弹窗时锁定背景页面滚动，关闭后恢复原始 `body` 样式；
- 保留焦点陷阱、Escape 关闭和关闭后的焦点恢复。

同一 1280×800 场景复测中，弹窗完整居中，导航和页脚均在遮罩下方，提交按钮可直接
点击并成功进入房间大厅：

- [桌面修复截图](screenshots/issue-004-dialog-footer-fixed.png)
- [桌面修复视频](videos/issue-004-dialog-footer-fixed.webm)
- [390×844 手机弹窗顶部](screenshots/issue-004-dialog-mobile-top.png)
- [390×844 手机滚动后提交按钮](screenshots/issue-004-dialog-mobile-action.png)

两个真实点击复测房均由 QA 账号创建并立即关闭，没有进入正式榜单或研究数据。前端
`198` 个测试与生产构建通过；可靠音频基线指纹保持不变。

状态：已修复并部署。

### ISSUE-005：裁判服务恢复后，等待复核的比赛无法重新调用 AI 裁判

| 字段 | 内容 |
|---|---|
| 严重级别 | Medium |
| 分类 | Functional / Recovery |
| 地址 | `https://117.50.192.216/admin` |
| 复现视频 | [issue-005-judge-retry-recovery.webm](videos/issue-005-judge-retry-recovery.webm) |

**现象**

旧裁判地址故障后共有 9 场比赛进入 `review_required`。管理员可以人工填写赛果，但在
裁判服务恢复、切换到健康的本机 Agent 裁判后，没有重新执行 AI 裁判的入口。比赛
因此只能依赖人工重做评分，无法复用已经保存的完整发言记录自动恢复流程。

这不是简单地修改旧比赛的 endpoint：每场比赛会固定裁判配置快照，正在运行或已经
失败的比赛不应被后台配置静默改写。缺少的是一个显式、可审计、具备并发保护的
“使用当前启用裁判重新判定”恢复操作。

**修复内容**

已发布 `judge-retry-recovery-20260719`：

- 管理端结果复核列表增加“重试 AI 裁判”，仅系统管理员可见；
- 请求带 `expected_updated_at`，其他管理员已处理时返回冲突并自动刷新列表；
- 只允许房间、比赛和裁判记录同时处于 `review_required` 时重试；
- 重试时显式固定当前启用裁判的 endpoint、模型、Prompt 和超时快照；
- 房间和比赛恢复为 `judging`，裁判记录恢复为 `running`；
- 写入只追加事件 `judge.retry_requested` 和管理员审计动作 `judge.retry`；
- 独立比赛引擎沿用现有幂等裁判流程，成功后正常生成赛果、归档和积分，失败则重新
  回到人工复核，不会让旧任务结果复活。

**真实生产复测**

仅对已确认 `is_test_data=true` 的 QA 房间 `#311998` 执行恢复：

1. 修复前列表为 9 项待处理：[修复前](screenshots/issue-005-review-before.png)。
2. 点击重试后页面明确显示“已使用当前启用裁判重新排队”：
   [重新排队](screenshots/issue-005-review-requeued.png)。
3. 比赛引擎使用 `稷下本机 Agent 裁判`、`qwen3.7-plus` 和
   `http://127.0.0.1:18400/debate/api/judge` 完成判定。
4. 房间、比赛状态均变为 `completed`，裁判记录变为 `approved`，结果为反方胜
   `62.0 / 89.0`；列表减少到 8 项，比赛出现在近期已确认赛果：
   [恢复完成](screenshots/issue-005-review-after.png)。
5. 数据库事件依次包含 `judge.retry_requested`、`match.completed`，管理员审计记录存在。

随后同时重试另外 3 场明确标记为 QA 的比赛 `#441937`、`#367133`、`#155866`：

- 三场均使用各自独立比赛记录完成判定，分别生成不同的判定理由；
- 房间、比赛和裁判记录全部由 `review_required` 正常进入 `completed/approved`；
- 每个房间各有一条 `judge.retry_requested`、一条 `match.completed` 和独立审计记录；
- 待处理项目由 8 项降至 5 项，剩余全部为非 QA 正式比赛，本轮未擅自重判；
- 管理页并发状态改为按 scorecard ID 分别维护，连续重试多场时每一行都会保持禁用和
  “正在重新排队”状态，不会因最后一次点击覆盖前一行状态。

证据：

- [三场并行重试前](screenshots/multi-qa-judge-before.png)
- [三场已进入重试](screenshots/multi-qa-judge-requeued.png)
- [三场全部完成](screenshots/multi-qa-judge-after.png)
- [并行恢复视频](videos/multi-qa-judge-retry.webm)

本地完整 API 回归 `404 passed`，前端回归 `199 passed`，生产构建、API 健康检查、
浏览器控制台和页面错误检查均通过。可靠音频基线保持不变。

状态：已修复并部署。

### ISSUE-006：公开比赛列表隐藏暂停状态，故障暂停房间看起来仍在正常进行

| 字段 | 内容 |
|---|---|
| 严重级别 | Low |
| 分类 | Content / UX |
| 地址 | `https://117.50.192.216/` |
| 复现视频 | N/A |

**现象**

公开接口已经返回房间状态，但首页“正在进行”列表只展示赛事名称和阶段。房间
`#214317` 因旧 Agent 服务故障暂停在自由辩论，首页却只显示“4v4 人机辩论正式赛 ·
自由辩论”，观众会误以为比赛仍在正常发言并反复进入等待。

修复前证据：[暂停状态被隐藏](screenshots/issue-006-paused-room-hidden.png)。

**修复与复测**

已发布 `public-live-status-20260719`：

- 公开比赛行直接展示服务端权威状态；
- 运行中、赛前准备、暂停和裁判评议使用不同的紧凑状态标签；
- 暂停状态使用暖黄色，但文字始终保留，不只依赖颜色表达；
- 状态、赛事和阶段允许自动换行，390×844 手机页面不会截断。

生产复测中 `#214317` 明确显示“比赛已暂停”，同时保留“自由辩论”阶段：

- [桌面状态标签](screenshots/issue-006-paused-room-visible.png)
- [手机完整页面](screenshots/issue-006-paused-room-mobile.png)

该房间属于非 QA 正式数据，本轮没有擅自继续或终止；房主和管理员仍可从控制台按
现有故障恢复流程重试或提前结束。公开状态修复不修改比赛数据。

状态：已修复并部署。

### ISSUE-007：比赛引擎持有房间行锁时，WebSocket 重连触发 ASGI 异常并遗留后台任务

| 字段 | 内容 |
|---|---|
| 严重级别 | Medium |
| 分类 | Reliability / Reconnection |
| 地址 | `https://117.50.192.216/ws/rooms/:code` |
| 复现视频 | N/A |

**现象**

生产房间状态更新与学生页面重连同时发生时，比赛引擎可能正在持有 `rooms` 行锁。旧
WebSocket 处理器在已经完成握手后再次执行 `SELECT ... FOR UPDATE`，锁等待超过 5 秒
会抛出 PostgreSQL `LockNotAvailable`。异常继续冒泡后，FastAPI 尝试在 WebSocket
连接上发送 HTTP 错误响应，最终出现：

`Expected ASGI message 'websocket.send' or 'websocket.close', but got 'websocket.http.response.start'`

连接退出时，旧实现只调用子任务的 `cancel()`，没有等待发送器、接收器和 Redis 订阅
真正结束，API 日志同时出现多条 `Task was destroyed but it is pending!`。锁超时发生在
`presence_join` 之后时，还可能留下进程内在线计数，导致席位连接状态与真实浏览器数
不一致。

**修复内容**

- 已接受的 WebSocket 遇到数据库锁竞争时使用可重试关闭码 `1013`，不再进入 HTTP
  异常响应路径；现有前端会按指数退避自动重连。
- `presence_join` 成功后立即记录本地 join，任何后续异常都能通过统一清理路径撤销，
  不会泄漏在线计数。
- 初始连接、动态认领席位和断开释放分别覆盖锁竞争；断开写入失败会执行一次短暂重试。
- 正常断线和 ASGI 取消使用独立受保护清理任务，等待 sender、receiver、Redis listener
  全部结束后才退出处理器。
- 预期锁竞争只记录一行结构化告警，不再输出整段异常堆栈；真正的未知异常仍保留完整
  traceback 并使用 `1011` 关闭。

**测试与生产复测**

- 新增锁超时后关闭码、presence 计数清理和取消任务完整回收测试。
- 完整 API 回归：`406 passed`。
- QA 房间 `#123552` 上使用独立 PostgreSQL 事务持有房间行锁 15 秒：
  - 5 秒锁超时后页面显示“正在重连”，已有 REST 房间数据仍可见；
  - 锁释放后无需刷新或人工点击，页面自动恢复为“实时连接”；
  - 房间题目、席位、准备状态和房主权限均未丢失；
  - 新日志只有一行 `client may retry error=OperationalError`，没有新的 ASGI 协议异常或
    pending task。
- 验收房属于 QA 数据，复测后已关闭。
- 可靠音频基线仍为
  `3219138b22878e766c94b9a1fa0e422211a6f27dd74afbbb93a8e45f7e295285`，本修复未修改
  TTS、LiveKit 或浏览器播放文件。

证据：

- [锁竞争期间自动重连](screenshots/issue-007-ws-lock-reconnecting.png)
- [锁释放后自动恢复](screenshots/issue-007-ws-lock-recovered.png)

状态：已修复并部署。

### ISSUE-008：个人中心历史比赛在手机上逐字竖排，顶部触控目标过小

| 字段 | 内容 |
|---|---|
| 严重级别 | Medium |
| 分类 | UX / Responsive / Accessibility |
| 地址 | `https://117.50.192.216/me` |
| 复现视频 | N/A |

**现象**

个人中心的历史比赛沿用桌面五列表格。在 390×844 手机宽度下，房间、题目、状态、结果
和操作被强行压进 320px，导致“比赛已终止”“查看记录”等中文几乎逐字竖排；用户难以
快速扫描房间和辩题。顶部导航菜单和退出登录按钮的实际点击框约为 35–36px，也低于
常用的 44px 移动触控目标。

修复前证据：[手机历史表格逐字竖排](screenshots/me-history-mobile-390x844.png)。

**修复内容**

- 历史记录改为单一语义 `ul/li` 列表：桌面保留房间、辩题、状态、结果、操作五列，
  手机转为独立比赛卡片。
- 手机卡片以房间号和辩题为主要信息，完成日期作为辅助信息，状态与结果使用文字胶囊，
  “查看记录”固定为 96×44px 右上操作，不再挤压题目。
- 顶部菜单、账号入口和退出按钮最小高度统一为 44px；页面保持 390px 无横向溢出。
- 空历史状态不再渲染没有 `listitem` 的 ARIA list；使用原生列表语义后 axe 检查通过。

**测试与生产复测**

- 新增历史比赛语义列表、内容与精确返回链接测试。
- 前端完整回归 `199/199`，TypeScript 和 Next.js 生产构建通过。
- 生产移动端首两张卡片题目可用宽度为 288px，操作按钮均为 96×44px；导航菜单、
  账号入口和退出按钮高度均为 44px。
- 桌面端仍显示五列布局，1440px 视口无横向溢出。
- 发布 release：`mobile-history-polish-20260719`；上一可回滚 release：
  `mobile-history-20260719`，再上一可靠语音 release：`audio-declick-20260719`。
- 候选与当前可靠版的 `livekit-interrupt-gate.js` SHA-256 均为
  `de373b01d9b5587bdbaa38115227d99a21b2ec3645f3046bfbda5068663ba219`；可靠音频清单
  指纹仍为 `3219138b22878e766c94b9a1fa0e422211a6f27dd74afbbb93a8e45f7e295285`。

证据：

- [修复后手机卡片](screenshots/me-history-mobile-fixed-final-390x844.png)
- [修复后桌面五列](screenshots/me-history-desktop-fixed-1440x900.png)

状态：已修复并部署。
