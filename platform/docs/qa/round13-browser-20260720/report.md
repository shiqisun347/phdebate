# Round 13 生产黑盒测试报告

| 项目 | 内容 |
|---|---|
| 日期 | 2026-07-20 |
| 目标 | `https://117.50.192.216` |
| 工具 | agent-browser / Chrome accessibility snapshot / Web Vitals |
| 范围 | 注册、登录、赛事详情、4v4 与 1v1 建房、房间搜索、抢座、准备、返回比赛、多设备接管、匿名观战、赛果发布边界、错误恢复、键盘、390/768/1440 响应式 |
| 明确排除 | 未开始比赛；未播放音频；未调用 Agent、TTS、ASR；未读取应用源码 |

## 结论

核心赛前流程整体可用：两个独立账号完成注册、登录、4v4 建房、六位房间号搜索、第二位真人认领、准备状态同步、个人中心返回房间、多设备显式接管、1v1 自定义辩题建房和安全关闭。已发布赛果允许匿名查看，未发布赛果拒绝匿名读取。桌面、平板和手机布局均未发现阻断性错位。

本轮发现 1 个仍待修复的中优先级键盘可访问性问题和 1 个低优先级文案重复问题；另发现 1 个请求风暴和 1 个房主权限转移问题，均由主代理在测试期间热修并在生产新状态下复测通过。

## 问题汇总

| 编号 | 优先级 | 状态 | 问题 |
|---|---|---|---|
| ISSUE-001 | Medium | Open | 参赛弹窗没有焦点陷阱，Tab 可进入被遮罩的后台页面 |
| ISSUE-002 | Critical | Fixed and verified | 暂停比赛匿名观战与结果页互相跳转，产生请求风暴 |
| ISSUE-003 | High | Fixed and verified | 房主席位超时释放时，房主权限静默转移给其他辩手 |
| ISSUE-004 | Low | Open | 个人中心“继续比赛”卡片重复显示“房间大厅” |

## 详细问题

### ISSUE-001：参赛弹窗焦点逃逸到后台页面

- 优先级：Medium
- 分类：Accessibility / UX
- 页面：`/competitions/daily-4v4`
- 复现视频：`/tmp/round13-browser/videos/issue-001-modal-focus-escape.webm`

打开“立即参赛”弹窗后连续按 Tab，焦点经过少量弹窗控件后先落到 `body`，随后进入弹窗后方的“跳到主要内容”、品牌、赛事大厅等链接。键盘用户可能在看不到目标的情况下操作后台页面；这也违反模态对话框应限制焦点范围的通用交互预期。

复现步骤：

1. 登录后打开 4v4 赛事详情。
2. 点击“立即参赛”。
3. 连续按 Tab 六次。
4. 焦点离开弹窗，落到后台“跳到主要内容”链接。

证据：

- 弹窗前：`/tmp/round13-browser/screenshots/issue-001-step-1.png`
- 弹窗打开：`/tmp/round13-browser/screenshots/issue-001-step-2.png`
- 后台元素获得焦点：`/tmp/round13-browser/screenshots/issue-001-result-background-focus.png`
- 活动元素记录：`/tmp/round13-browser/issue-001-active-background.txt`

建议：使用完整的 dialog focus trap；打开时把焦点放到关闭按钮或第一个模式按钮，Tab/Shift+Tab 循环于弹窗内，Escape 关闭后把焦点还给“立即参赛”。

### ISSUE-002：匿名观战与结果页形成无限跳转和请求风暴

- 优先级：Critical
- 分类：Functional / Performance
- 页面：`/rooms/551958/watch`
- 状态：生产热修后复测通过

初次复现时，匿名访问暂停比赛的观战页后，观战页把用户送往结果页；结果 API 返回 409 后结果页又送回观战页。约 5 秒内已出现大量重复 RSC、房间和结果 API 请求，页面一直停留在“正在连接公开观战”“正在整理比赛结果”“正在返回正确的比赛页面”之间。

证据：

- 复现视频：`/tmp/round13-browser/videos/issue-002-watch-result-loop.webm`
- 失败画面：`/tmp/round13-browser/screenshots/issue-002-result.png`
- 请求样本：`/tmp/round13-browser/issue-002-network-sample.txt`

热修复测：

- 相同匿名 URL 等待 5 秒后稳定停留在 `/watch`。
- 页面正常展示只读观战舞台，声音保持关闭。
- 只观察到 3 条相关请求，不再出现 `/watch` 与 `/result` 循环。
- 通过画面：`/tmp/round13-browser/screenshots/issue-002-hotfix-passed.png`
- 通过请求：`/tmp/round13-browser/issue-002-hotfix-network.txt`

### ISSUE-003：席位超时释放连带转移房主权限

- 优先级：High
- 分类：Authorization / Recovery
- 房间：`232417`（旧规则下产生，已关闭）
- 状态：规则已热修，并使用新房间复测通过

原房主 `QA13浏览器` 创建房间并认领正方一辩。离开房间大厅约两分钟后，席位按产品规则自动释放，但房主身份同时静默转移给仍在线的 `QA13协辩`。原房主返回后不能开始或关闭房间；新房主立即获得“锁定席位并开始”和“关闭房间”。重新认领原席位也不会恢复房主身份。

证据：

- 创建后原房主：`/tmp/round13-browser/screenshots/lobby-owner-desktop.png`
- 超时后原房主看到房主变为协辩：`/tmp/round13-browser/screenshots/lobby-owner-mobile-390.png`
- 原房主重新认领仍无控制权：`/tmp/round13-browser/screenshots/issue-003-original-owner-after-reclaim.png`

预期：席位租约和房间所有权应分别管理。普通席位超时只释放席位；房主离线时可允许其他用户继续准备，但不应在无明确规则、通知和确认的情况下转移终止/开赛权限。

热修复测：

- 新建 1v1 房间 `250649`，房主为 `QA13浏览器`，协辩为 `QA13协辩`。
- 房主离开房间大厅超过两分钟，协辩保持在线。
- 协辩视角仍显示房主为 `QA13浏览器`，没有获得开始或关闭权限；房主席位被保留并标记离线。
- 房主返回后仍拥有“开赛前席位管理”和“关闭房间”，没有发生权限移交。
- 协辩视角：`/tmp/round13-browser/screenshots/issue-003-hotfix-joiner-sees-owner-retained.png`
- 房主返回：`/tmp/round13-browser/screenshots/issue-003-hotfix-owner-controls-retained.png`

### ISSUE-004：继续比赛卡片重复状态文本

- 优先级：Low
- 分类：Content / UI polish
- 页面：`/me`

“继续比赛”卡片中连续出现两个“房间大厅”，一个像状态标签，另一个像页面类型，信息没有增加且使卡片显得未完成。

证据：`/tmp/round13-browser/screenshots/me-owner-active-room.png`

建议：保留一个状态标签，操作文案继续使用“返回房间大厅”。

## 已通过的重点场景

### 认证与账号

- 注册必填字段会把焦点移动到首个无效输入。
- 两次密码不一致时显示明确中文错误。
- 错误密码登录显示“账号或密码错误”，不泄露账号是否存在。
- 正确密码支持 Enter 提交。
- 新设备登录同一账号后不会直接抢占席位，必须点击“确认接管到当前设备”；旧设备随后变为只读。

### 赛事与房间

- 4v4 赛事介绍、排行榜、观战列表和规则四个页签正常切换。
- 建房前必须选择人类席位；4v4 题库可切换。
- 搜索框自动过滤非数字并限制为六位；五位时按钮禁用。
- 第二个真人可通过房间号进入并认领空席；双击抢座只发送一次成功请求。
- 准备状态在两位用户之间正确同步。
- `/me` 能显示进行中的房间并返回大厅。
- 1v1 允许自定义辩题；输入长度限制为 300 字；自定义题存在时题库选择正确禁用。
- 关闭未开始房间有确认，取消不改变房间，确认后返回赛事大厅并显示关闭提示。

### 观战与结果边界

- 等待中的未公开房间不允许匿名读取结果。
- 已完成并发布的房间 `367133` 可匿名查看胜负、评分、完整逐字稿和时间线。
- 结果页未自动下载录音，未点击播放时没有音频媒体请求，避免无意义带宽占用。
- `551958` 热修后匿名观战稳定，声音默认关闭。

### 响应式与性能

- 390×844：首页、房间大厅、赛果页顶部及长逐字稿均无横向溢出或控件遮挡。
- 768×1024：双方席位并排展示，房间信息和设备检查层级清楚。
- 1440×900：赛事详情、房间大厅和赛果信息密度合理。
- 首页 Web Vitals：TTFB 18.2 ms、FCP 64 ms、LCP 132 ms、CLS 0.06。
- 已发布赛果：TTFB 23.9 ms、FCP 104 ms、LCP 336 ms、CLS 0.06。
- 除刻意拦截的错误恢复测试外，浏览器未记录 JavaScript page error。

## QA 数据与清理清单

主代理应在服务器侧统一清理以下测试数据：

- 账号 `qa_r13_browser_20260720`，真实姓名 `QA13浏览器`。
- 账号 `qa_r13_joiner_20260720`，真实姓名 `QA13协辩`。
- 已关闭房间 `232417`。
- 已关闭房间 `230946`。
- 新规则回归房间 `250649`：未开赛，测试结束后关闭或删除。

所有 agent-browser 会话结束前均未点击任何播放按钮、麦克风测试、开始比赛或 Agent/ASR/TTS 操作。
