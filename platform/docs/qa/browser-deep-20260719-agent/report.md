# 生产端深度浏览器 QA（匿名桌面与手机）

| 项目 | 内容 |
|---|---|
| 日期 | 2026-07-19 |
| 目标 | `https://117.50.192.216` |
| 范围 | 首页、赛事详情、参赛入口、登录注册跳转、房间异常态、暂停观战、移动导航、键盘与性能感受 |
| 数据影响 | 只读；未注册账号、未创建房间、未操作比赛；未触碰 TTS/ASR/WebRTC/播放器代码 |

## 结论

未发现 Critical 或 High。发现 4 个 Medium、1 个 Low。首页和观战页当前网络下加载较快：首页约 286ms/192KB，暂停观战页约 299ms/345KB；未观察到 JavaScript 异常或失败请求。

| 严重度 | 数量 |
|---|---:|
| Critical | 0 |
| High | 0 |
| Medium | 4 |
| Low | 1 |
| 合计 | 5 |

## 问题

### ISSUE-001：手机导航菜单不响应 Escape

- 严重度：Medium
- 类别：可访问性 / 导航
- URL：`/`
- 状态：本地已修复并新增焦点恢复回归测试，等待合并部署

复现：

1. 使用 390×844 视口打开首页。
2. 点击“打开导航菜单”。
3. 按 Escape。
4. 菜单仍保持展开，按钮仍为 `aria-expanded=true`。

证据：[打开菜单](screenshots/issue-001-step-2-retry.png)、[按 Escape 后仍展开](screenshots/issue-001-result-retry.png)。该问题重复验证两次。

建议：与观战设置面板保持一致，支持 Escape 关闭；打开后把焦点移到关闭按钮或首个导航项，关闭后把焦点还给菜单触发按钮，并考虑背景内容的焦点约束。

### ISSUE-002：无效六位房间号在登录前没有校验

- 严重度：Medium
- 类别：功能 / UX
- URL：`/competitions/daily-4v4`

复现：匿名用户打开“立即参赛”→“搜索房间”，输入确定不存在的 `000000`，系统直接跳转到 `/login?next=%2Frooms%2F000000%2Flobby`。用户完成登录后才会发现房间不存在，形成一次无价值的认证往返。

证据：[房间号输入界面](screenshots/mobile-search-room.png)、[不存在房间的最终错误态](screenshots/mobile-invalid-room.png)。

建议：登录前先调用公开、脱敏的房间存在性/赛事匹配校验；不存在、已取消、已结束分别给出明确结果。只有有效且可加入的房间才进入登录流程。

### ISSUE-003：匿名参赛弹窗会闪现完整建房配置，再突然收缩

- 严重度：Medium
- 类别：加载状态 / 视觉稳定性
- URL：`/competitions/daily-4v4`

复现：匿名手机用户点击“立即参赛”后，弹窗先展示辩题和 8 个席位，按钮显示“正在确认登录状态…”；约 1.6 秒后这些配置全部消失，只保留“登录或注册后创建”。这会造成明显布局跳变，并让用户误以为可以先配置再登录。

证据：[刚打开时的完整配置](screenshots/issue-003-immediate.png)、[鉴权完成后收缩](screenshots/issue-003-after-auth.png)。

建议：打开弹窗前复用页面已知的认证快照，或在确认期间只展示固定尺寸 skeleton；匿名态从第一帧直接展示登录/注册 CTA，不渲染不可操作的完整建房表单。

### ISSUE-004：赛事详情 Tab 不支持方向键切换

- 严重度：Medium
- 类别：可访问性 / 键盘
- URL：`/competitions/daily-4v4`
- 状态：本地已修复并覆盖 ArrowLeft、ArrowRight、Home、End，等待合并部署

复现：键盘聚焦并选中“排行榜”Tab，按 ArrowRight；选中项仍停留在“排行榜”，没有移动到“观战列表”。当前控件声明了 `tablist/tab` 语义，却未实现对应的 WAI-ARIA 键盘行为。

证据：[按 ArrowRight 后仍选中排行榜](screenshots/issue-004-tabs-arrow.png)。

建议：实现 Left/Right、Home/End 的 roving tabindex，并在激活后同步 `aria-selected` 和对应 tabpanel；或改用已有成熟、经过键盘测试的 Tabs primitive。

### ISSUE-005：已取消房间链接静默跳回首页

- 严重度：Low
- 类别：错误恢复 / 内容
- URL：`/rooms/257331/watch`、`/rooms/594944/watch`

两个已取消 QA 房间的深链接都会直接变为 `/`，没有 toast、原因或可恢复选项。对于分享链接、历史书签和刷新返回场景，用户会误以为点错链接。

证据：[257331 静默回首页](screenshots/cancelled-257331.png)、[594944 静默回首页](screenshots/cancelled-594944.png)。

建议：保留房间 URL 并展示专用终态：“该比赛已取消”，提供返回赛事、查看个人历史等操作；已结束房间应优先跳结果页，不应和取消态统一处理。

## 已通过验证

- 匿名创建比赛会保留 `next=/?participate=daily-4v4`；登录与注册之间继续保留该返回地址。
- 错误密码反馈明确且无控制台异常。
- 注册表单使用原生必填与最短 8 位密码校验。
- 暂停比赛 `#551958` 可匿名进入；实时连接建立成功；观战设置可用 Escape 关闭。
- 不存在房间的直接观战页有错误说明、重试和返回赛事大厅入口。
- 首页、赛事详情、排行榜和观战列表在 390px 手机宽度下未观察到横向溢出。
- 当前抽样页面无 JavaScript exception；LiveKit 连接日志正常。
