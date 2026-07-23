# 浏览器深度回归报告（续测）

| 项目 | 内容 |
| --- | --- |
| 测试日期 | 2026-07-22 |
| 线上地址 | https://117.50.192.216 |
| 工具 | agent-browser 0.32.1 / Chrome 150 |
| 会话 | `browser-control-qa` |
| 账号 | 专用一次性 QA 账号（未用于创建比赛） |
| 原则 | 只读检查；未执行开始、暂停、继续、跳过、重试、终止、移除席位等破坏性操作 |

## 结论摘要

公开大厅、赛事详情、注册登录、个人中心、参赛入口、房间搜索、观战页和权限拦截均可正常访问。桌面端和手机端的浅色界面、固定底部工具栏、观战脱敏状态基本符合预期；页面加载速度良好，未发现新的 JavaScript exception、Unhandled rejection 或静态资源 4xx/5xx。

当前线上没有可用于只读验证的“运行中”比赛。公开房间 `427792` 当前处于“服务异常暂停”，因此本轮无法安全地验证完整自动比赛、真人发言、倒计时推进、双流式 ASR、单行实时字幕和发言按钮亮起/禁用的运行态行为。这不是通过浏览器可以自行修复的测试条件，需要房主明确重试当前步骤，或提供一个专用可回收 QA 房间。

## 已覆盖页面与场景

| 场景 | 结果 | 证据 |
| --- | --- | --- |
| 首页桌面端（1440×900） | 通过；导航、赛事卡片、公开比赛、排行榜入口均可用 | [home-desktop.png](browser-continuation-assets/screenshots/home-desktop.png) |
| 首页手机端（390×844） | 通过；导航折叠、主按钮、赛事卡片和底部页脚不溢出 | [home-mobile.png](browser-continuation-assets/screenshots/home-mobile.png) |
| 4v4 赛事详情 | 通过；介绍、排行榜、观战列表、规则说明四个 Tab 可切换 | [competition-4v4-desktop.png](browser-continuation-assets/screenshots/competition-4v4-desktop.png) |
| 赛事排行榜 Tab | 通过；排名、积分、战绩、平均评分、有效场次有明确空/有数据状态 | [competition-4v4-rankings.png](browser-continuation-assets/screenshots/competition-4v4-rankings.png) |
| 赛事观战列表空状态 | 通过；明确显示暂无可观战公开比赛及全站观战上限 | [competition-4v4-watchers.png](browser-continuation-assets/screenshots/competition-4v4-watchers.png) |
| 登录失败 | 通过；保留登录页并显示“账号或密码错误”，没有泄漏服务端错误 | [login-invalid.png](browser-continuation-assets/screenshots/login-invalid.png) |
| 注册、登录和个人中心 | 通过；真实姓名显示、历史/继续比赛/安全设置入口存在 | [me-desktop.png](browser-continuation-assets/screenshots/me-desktop.png) |
| 创建/加入入口 | 通过；选择创建或输入六位房间号，未选席位时按钮禁用 | [create-entry-modal.png](browser-continuation-assets/screenshots/create-entry-modal.png) |
| 选择人类席位 | 通过；选择席位后创建按钮才启用，没有要求用户配置 AI | [create-seat-selected.png](browser-continuation-assets/screenshots/create-seat-selected.png) |
| 无效房间号 `000000` | 通过；回到大厅并保留弹窗，显示可理解的“没有找到房间”提示 | [invalid-room-error.png](browser-continuation-assets/screenshots/invalid-room-error.png) |
| 已暂停公开观战页桌面端 | 通过；题目、双方席位、冻结计时、异常状态、声音/全屏/设置和举手队列清晰展示；无文字稿 | [room-427792-lobby-as-outsider.png](browser-continuation-assets/screenshots/room-427792-lobby-as-outsider.png) |
| 已暂停公开观战页手机端 | 可用但有视觉改进项；固定底部工具栏可操作，布局未溢出 | [room-427792-mobile.png](browser-continuation-assets/screenshots/room-427792-mobile.png) |
| 非房主访问控制台 | 通过；最终切换到 `/rooms/427792/watch?notice=no-control`，只读观战，不暴露控制按钮 | [control-unauthorized.png](browser-continuation-assets/screenshots/control-unauthorized.png) |
| `/rooms/:code/debate` 非席位访问 | 通过；自动转到观战页，未出现麦克风或比赛操作能力 |
| `/admin` 普通用户访问 | 通过；回到大厅并显示“该页面仅限系统管理员访问” |
| `/debate` | 通过；独立 Debate Agent 登录页可加载 |
| 公开文字稿脱敏 | 通过；观战页正文没有逐字稿、字幕历史或隐藏全文可见内容 |

## 发现的问题

### ISSUE-001：线上没有可完成全流程的运行中比赛

| 字段 | 内容 |
| --- | --- |
| 严重级别 | critical（当前验收阻断） |
| 类型 | functional / availability |
| 地址 | https://117.50.192.216/rooms/427792/watch |
| 状态 | 待房主或管理员处理；本轮未执行重试 |

**现象**

公开房间 `427792` 显示“比赛因临时服务异常暂停”，阶段为“自由辩论 · 等待恢复”，计时冻结在 `03:58`。观战页提示等待房主在控制台处理。由于当前没有其他运行中的公开比赛，无法在不执行破坏性控制的前提下验证完整比赛链路。

**影响**

无法通过线上浏览器证据确认以下 P0 行为：开局准备是否只等待必要时间、固定阶段倒计时是否连续、真人发言按钮是否严格按轮次亮起、ASR 双流字幕是否实时更新、AI 发言的下一阶段预生成是否正确，以及比赛结束/结果页是否自动到达。

**建议**

提供一个“QA 可回收房间”标记：可由测试账号开始，最多 1v1 或 4v4，结束后自动归档；同时保留房主的显式重试能力。这样可以反复验证完整流程而不污染公开赛事，也不需要对真实比赛执行暂停或终止。

### ISSUE-002：手机观战页舞台中部存在较大的无信息留白

| 字段 | 内容 |
| --- | --- |
| 严重级别 | low（视觉/信息密度） |
| 类型 | visual / responsive UX |
| 地址 | https://117.50.192.216/rooms/427792/watch |
| 视口 | 390×844 |
| 证据 | [room-427792-mobile.png](browser-continuation-assets/screenshots/room-427792-mobile.png) |

**现象**

在 1v1 暂停观战状态，标题、状态、计时和恢复提示位于上半屏，双方席位却被固定到接近底部工具栏的位置，中间约有一大段空白。功能没有溢出，但手机用户需要在较大垂直距离内寻找双方席位，比赛信息密度偏低。

**建议**

手机端可将“状态/计时/当前发言信息/双方席位”改为 `auto auto` 的紧凑流式布局；只有在需要展示字幕时为字幕区域保留最小高度。固定底部工具栏仍保持不变。应同时覆盖 1v1、4v4（四个席位）和正在发言三种状态，避免简单压缩导致字幕或按钮被遮挡。

## 运行态项目：本轮无法确认

以下项目必须在专用运行中 QA 房间补测，不能根据当前暂停页推断已完成：

1. 房主开始后的准备阶段是否有单一、可解释的状态变化，且不会莫名等待。
2. 固定轮次和自由辩论的倒计时是否在服务端权威时间上连续递减，暂停/恢复后不跳变。
3. 真人只有轮到本人时才可举手/发言，其他席位按钮有具体禁用原因。
4. ASR 是否为双流实时链路；字幕是否始终只显示当前一行并平滑替换，而非多行堆叠。
5. AI 文本是否在当前阶段稳定后提前生成，TTS 是否进入同一连续音轨且无重复连接。
6. 真人发言中断、60 秒断线自动暂停、重连后房主继续、服务异常重试和比赛结束结果页。
7. 4v4 多真人、多 AI 席位与最多 5 位全站观众的并发边界。

## 性能与错误观测

- 首页本轮测得 `TTFB≈23ms`、DOM 完成约 `31ms`；观战页 `TTFB≈119ms`、DOM 完成约 `144ms`（受实时房间初始化影响）。
- 浏览页面后未发现新的 JavaScript exception、Unhandled rejection 或浏览器错误。
- LiveKit 在进入观战页时连接成功；离开页面时有明确 disconnect 日志。后续页面不再产生新的实时连接日志。
- 无效房间号由页面显示可理解的业务错误，而不是空白页或未处理 404。
- 公开观战页没有展示文字稿或字幕历史，符合“观众不可见文字稿”要求。

## 后续回归顺序

1. 先恢复或创建专用 QA 运行房间，完成一场 1v1 全流程，再进行 4v4。
2. 记录每个阶段的服务端时间戳、浏览器字幕更新时间、首个音频样本和按钮状态。
3. 在同一房间用两个真人浏览器会话模拟席位接管、断线 60 秒和房主继续；另开观众会话验证脱敏。
4. 依据 ISSUE-002 做手机布局微调，并用 390×844、768×1024、1440×900 三个视口截图回归。

