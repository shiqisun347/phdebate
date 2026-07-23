# Round 5：非音频 UI、React 性能与产品设计审计

日期：2026-07-19  
范围：赛事大厅、排行榜、房间恢复、结果页、管理后台；可靠舞台和语音路径冻结。

## 产品设计方向

目标用户是需要在短时间内创建、加入、恢复和管理大量辩论比赛的学生与赛事管理者。页面的首要任务不是展示装饰，而是让用户快速判断“我在哪场比赛、现在是什么状态、下一步能做什么”。

本轮沿用现有深蓝赛事品牌，不做像素模仿。设计准则为：

- 高信息密度但保持清晰分组，标题、状态、主操作三层必须一眼可辨。
- 状态使用稳定文案与语义属性，不依赖颜色或动效表达。
- 只保留首页一处品牌性轨道动效，且已有 `prefers-reduced-motion` 降级；管理页和业务列表不增加装饰动画。
- 移动端优先保留主操作和当前状态，宽表允许明确横向滚动，不强行压缩字段。

## 成熟项目调研结论

### Lichess

[Lichess WebSocket 实现](https://github.com/lichess-org/lila/blob/master/ui/lib/src/socket.ts)使用版本序号检查、事件缺口重试、确认后重发、离线/重连状态以及延迟平滑值。可落地原则是“连接状态必须是产品状态，而不是控制台细节”。本系统已有心跳、序号快照、缺口重取、离线文案和幂等写操作，方向一致；后续可以把重连次数和最后同步时间加入非舞台控制台。

### DMOJ

[DMOJ 赛事列表](https://github.com/DMOJ/online-judge/blob/master/templates/contest/list.html)按正在参与、可参加、未来和历史赛事分区，并在不可逆开始操作前明确警告。可落地原则是“按用户下一步任务分区，不按数据库字段堆卡片”。当前首页已区分赛事、公开观战和榜单，个人中心区分继续比赛与历史；未来赛事数量增长后应增加进行中/可报名/已结束筛选。

### LiveKit Meet

[LiveKit Meet 房间客户端](https://github.com/livekit-examples/meet/blob/main/app/rooms/%5BroomName%5D/PageClientImpl.tsx)把入会前检查、连接凭证和房间实例分层，并用 `useMemo` / `useCallback` 固定昂贵房间对象与连接参数。可落地原则是“先确认设备和身份，再创建昂贵实时连接”。当前系统已具备麦克风预检和席位身份绑定；本轮不修改冻结实时路径。

### Colyseus

[Colyseus Room SDK](https://github.com/colyseus/colyseus/blob/master/packages/sdk/src/Room.ts)采用有上限的消息缓冲、指数退避、最大重试次数、重连令牌和明确的 drop/reconnect 事件。当前 `useRoom` 已有退避、心跳、房间错投影保护和权威快照；建议后续增加最大连续重连提示和人工“重新载入房间”降级入口。

### boardgame.io

[boardgame.io Lobby Client](https://github.com/boardgameio/boardgame.io/blob/main/src/lobby/client.ts)严格区分创建、加入、释放大厅席位、永久离场和再赛；[Lobby Connection](https://github.com/boardgameio/boardgame.io/blob/main/src/lobby/connection.ts)也阻止同一玩家同时加入另一场。当前系统已分别实现大厅释放、开赛后 AI 接替、真人恢复申请和同用户跨房间真人席位限制。

## 已发现并修复

### 1. 排行榜首屏请求瀑布

原流程先获取赛事和赛季，设置选择状态后，再由第二个 effect 获取排行榜，首屏需要两个网络阶段，并且容易重复请求默认榜单。

现在赛事、赛季和默认榜单通过一个 `Promise.all` 同时开始；使用已加载查询键避免状态初始化后再次请求同一榜单。定向测试确认首屏只产生三个并行请求。

### 2. 长结果记录的屏外渲染

结果页可能同时包含大量逐字稿、音频控件、积分记录和事件时间线。此前所有条目都会立即参与布局与绘制。

现在 `.transcript-item`、`.timeline-item` 和 `.match-history-row` 使用 `content-visibility:auto` 与固有尺寸提示。浏览器可以跳过屏外条目的渲染，同时保持滚动条尺寸稳定。数据和 DOM 语义不变。

### 3. 管理后台模块状态不够明确

桌面侧栏只有视觉 active class，屏幕阅读器无法可靠确认当前模块；加载状态也不是实时状态区域。

现在：

- 管理侧栏拥有明确导航名称。
- 当前模块使用 `aria-current="page"`。
- 导航按钮与统一内容区域通过 `aria-controls` / `aria-labelledby` 关联。
- “正在载入 / 数据已载入”使用 polite live status。

这与高信息密度后台的目标一致：状态明确，但不增加弹窗和动画。

## 当前性能与结构证据

- 前端应用代码约 15,688 行 TypeScript/TSX（包含测试）。
- `admin/page.tsx` 约 2,401 行，是当前最大的非冻结业务组件。
- `result/page.tsx` 约 568 行，长记录渲染已在本轮缓解。
- 13 个页面中 11 个是完整 Client Component，公开页面仍依赖加载屏和 hydration 后请求。
- 当前构建中管理页路由专属原始 JS 块约 74 KB；体积尚可，但 2,401 行单组件使变更影响面和重渲染边界过大。
- 构建产物中舞台相关专属块约 76 KB；本轮按可靠音频保护要求不处理。

## 后续优先级

### P1：继续拆分管理后台

目前仅系统总览、用户和房间面板已抽离。应继续按赛事、自动流程、AI 配置、媒体、复核、审计拆成独立组件，并用动态导入按当前模块加载。目标不是单纯减少行数，而是让每个模块只订阅自己的状态、请求和表单，避免任意输入导致整页 2,401 行树重新渲染。

### P1：公开页面改为 Server Component 外壳

赛事大厅、赛事详情和排行榜是公开读数据，适合服务端首屏获取并将交互部分拆为小型 Client Island。这样可减少空白加载屏、客户端 JavaScript 和 hydration 后请求。迁移时应保留当前“次要面板失败不阻断创建比赛”的局部失败边界。

### P2：结果页分段加载逐字稿

`content-visibility` 降低绘制成本，但完整逐字稿仍一次传输。比赛规模扩大后应让 Speech 与 Event 都采用游标分页，提供“继续加载”，并保留当前音频 `preload="none"`。

### P2：管理模块 URL 状态

后台刷新后会返回系统总览。后续应把当前模块写入 `/admin?tab=rooms`，支持刷新、复制链接和浏览器前进/后退；实现时应与模块动态导入一起完成，避免在当前巨型组件继续叠加 effect。

### P2：比赛断线状态升级

借鉴 Lichess/Colyseus，可在控制台显示最后成功同步时间、连续重试次数和“使用 REST 重新获取权威状态”操作。舞台本身保持冻结，先在控制台和大厅验证。

## 验证结果

- 前端全量：33 个测试文件、222 项测试通过。
- 本轮定向：11 项测试通过。
- Next.js 生产构建通过。
- TypeScript、ESLint 通过。
- Agent Reach v1.5.0，已是最新版本。
- 未修改 `DebateStage`、`apps/web/lib/audio`、AudioWorklet、TTS、MOSS、LiveKit 或 FunASR 实现。
