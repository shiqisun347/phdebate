# 生产浏览器回归（Round 3）

- 环境：`https://117.50.192.216`
- 日期：2026-07-19（Asia/Shanghai）
- 工具：agent-browser / Chrome 150
- 视口：1440×900、390×844
- 范围：admin 懒加载、赛事大厅、赛事详情、登录注册、观战与辩手权限、键盘和移动端布局
- 音频约束：未点击开启声音，未修改 TTS、MOSS、LiveKit、AudioWorklet、播放器或比赛舞台保护路径

## 结论

核心公开流程、管理员权限边界、观战页面、移动端导航和后台标签懒加载均符合预期。生产回归发现并保留了一个滚动部署期间的 502 缺陷证据；服务恢复后重新执行公开页及后台回归，未再出现 5xx。

本轮另修复一项后台加载态误导：首次进入空缓存模块时，不再同时显示“正在载入”和“共 0 条 / 没有结果”。

## 通过项

### Admin 懒加载

- 管理员重新载入 `/admin` 后，仅请求 `GET /api/admin/dashboard`，状态 200。
- 首次进入比赛监管仅请求 `/api/admin/rooms?...`。
- 从比赛监管返回总览再回切比赛监管，请求记录为 0，证明缓存生效。
- 点击“刷新当前模块”后只重新请求当前房间域。
- 首次进入“AI 与语音”并行请求 agents、judges、providers、audio-cues 四个对应接口，没有加载 users、rooms、competitions 或 audit。
- 匿名访问 `/admin` 自动返回赛事大厅。
- 桌面证据：[admin-rooms-desktop.png](screenshots/admin-rooms-desktop.png)
- 移动端完成态证据：[admin-rooms-mobile-loaded.png](screenshots/admin-rooms-mobile-loaded.png)
- 原加载期问题证据：[admin-rooms-mobile.png](screenshots/admin-rooms-mobile.png)

### 赛事大厅与赛事详情

- 首页赛事卡片、公开比赛、排行榜摘要、登录和注册入口均可访问。
- 1440×900 与 390×844 未发现重叠、横向溢出或按钮不可点击。
- 赛事详情 tabs 具备 tab/tabpanel 语义；ArrowRight 可切到排行榜，End 可切到规则说明。
- 移动导航菜单可用 Escape 关闭，并将焦点返回“打开导航菜单”按钮。
- 证据：[home-desktop.png](screenshots/home-desktop.png)、[home-mobile.png](screenshots/home-mobile.png)、[competition-desktop.png](screenshots/competition-desktop.png)

### 登录与注册

- 登录表单字段具有可访问名称与 required 语义。
- 错误账号密码返回明确的“账号或密码错误”。
- 注册密码不一致停留在注册页并显示明确反馈，没有创建账号。
- 证据：[login-mobile.png](screenshots/login-mobile.png)、[login-invalid-feedback.png](screenshots/login-invalid-feedback.png)、[register-mobile.png](screenshots/register-mobile.png)、[register-mismatch-feedback.png](screenshots/register-mismatch-feedback.png)

### Watch / Debate

- 匿名观战房间 `551958` 可连接，舞台在桌面和手机布局正常。
- 未持有比赛席位的登录用户访问 `/debate` 会降级到 `/watch`，不会获得辩手控制权。
- 固定轮次的无障碍树只将“反方一辩”描述为当前发言席位，未错误标记同阵营其他席位。
- 证据：[watch-desktop.png](screenshots/watch-desktop.png)、[watch-mobile.png](screenshots/watch-mobile.png)
- 当前生产没有正在自由辩论阶段的公开房间，因此没有通过浏览器对“自由辩论当前可发言席位”做活跃态验证；本地 `stage-seat-accessibility` 7 项测试及舞台 75 项测试均通过。

## 问题与处理

### ISSUE-PROD-001：滚动 API 时 Nginx 被动摘除造成主页核心接口 502

- 严重度：高（部署窗口）
- 状态：已修复并部署；rollout gate、`max_fails` 与公网健康重试已完成，恢复后重新回归通过
- 时间：2026-07-19 16:41:13（08:41:13 UTC）
- 现象：主页同时请求 session-state、live-rooms、rankings、competitions，四项同秒返回 Nginx 502，页面显示“页面暂时无法打开”。约一分钟后重试时三项恢复，rankings 仍有一次独立 502，页面正确降级并提供“重新载入排行榜”。
- 证据：网络请求 ID `45241.402`–`45241.405`，HTTP 502，响应 `Server: nginx/1.18.0 (Ubuntu)`；后续恢复回归的 5xx 请求列表为空。
- 归因：经主任务确认，发生在 API 滚动重启期间，不是常态业务请求触发。

### ISSUE-UI-001：后台首次懒加载同时显示空数据结论

- 严重度：低
- 状态：已修复并随 `round4-final-ui-20260719` 发布
- 复现：后台首次进入“比赛监管”，加载条显示“正在载入比赛监管”，同时标题显示“共 0 个房间”，表格显示“没有符合条件的房间”。
- 修复：Rooms、Users、Audit 在首次加载且无缓存时显示明确加载文案，并抑制空结果提示；真实空响应完成后才显示空状态。
- 改动：
  - `apps/web/components/admin/rooms-panel.tsx`
  - `apps/web/components/admin/users-panel.tsx`
  - `apps/web/app/admin/page.tsx`
  - 新增/扩展对应组件测试

## 自动化验证

- 前端全量：33 个测试文件、222 项测试全部通过。
- 新增加载态专项：RoomsPanel、UsersPanel 及 AdminPage 共 4 项通过。
- Next.js production build、TypeScript 和 ESLint quiet 通过。
- 浏览器恢复后：首页、Admin、Rooms、Agents 标签均无 5xx 或页面异常。
