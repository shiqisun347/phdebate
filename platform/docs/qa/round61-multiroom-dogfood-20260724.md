# Round 61 生产多房间黑盒测试报告

| 项目 | 内容 |
|---|---|
| 日期 | 2026-07-24（Asia/Shanghai） |
| 生产站点 | `https://117.50.192.216` |
| 测试方式 | `agent-browser` 多身份、多标签真实浏览器黑盒测试 |
| 重点 | 双真人、真人 + 永久 AI、混合 4v4 建房；固定阶段、自由辩论、断线暂停、重连、观战上限、隐私和跨房隔离 |
| 代码与部署 | 本轮未修改代码、未部署 |

## 结论

本轮已证明以下核心行为在生产环境成立：

- 1v1 双真人可以完成注册、建房、认领席位、准备、开赛、双方立论和进入自由辩论。
- 1v1 真人 + 永久 AI 可以自动补位，真人和 AI 均能完成立论与自由辩论发言。
- 真人断线超过 60 秒后，整场比赛自动暂停；真人席位仍为真人，没有产生 AI 接管事件。
- 真人重连后比赛保持暂停，不会自动恢复；房主点击“继续比赛”后才恢复。
- 全系统第 5 个匿名观众可进入，第 6 个匿名观众被明确拒绝。
- 匿名观众在比赛进行中及终止后均看不到任何完整文字稿；参赛者结果页仍能查看自己的完整记录。
- 两个房间可以同时保持不同题目、不同阶段和不同运行状态，未观察到阶段、字幕或控制事件串房。

本轮没有完成一场“正常总结 → AI 裁判 → 正常赛果”的新比赛：为优先验证断线策略，房间 `757885` 在验证恢复后由房主主动终止；房间 `522853` 因测试浏览器进程资源故障导致真人会话同时断开，随后按设计自动暂停。此前轮次的正常完整比赛证据不能替代本轮未覆盖项，因此总结与裁判仍标记为本轮未验证。

## 问题汇总

| 严重级别 | 数量 |
|---|---:|
| Critical | 0 |
| High | 0 |
| Medium | 1 |
| Low | 0 |
| **合计** | **1** |

## 房间与流程结果

| 房间 | 场景 | 结果 | 关键证据 |
|---|---|---|---|
| `522853` | 1v1 双真人 | 建房、双方认领、准备、开赛、正反立论、自由辩论通过；两名真人断线后自动暂停 | [建房](round61-multiroom-evidence/screenshots/room-a-lobby-created.png)、[双方准备](round61-multiroom-evidence/screenshots/room-a-owner-ready.png)、[进入自由辩论](round61-multiroom-evidence/screenshots/room-a-free-debate-entry.png)、[断线暂停](round61-multiroom-evidence/screenshots/cross-room-522853-paused.png) |
| `757885` | 1v1 真人 + 永久 AI | AI 自动补位；真人立论、AI 立论、双方自由辩论通过；断线暂停、重连保持暂停、房主恢复通过；最后主动终止清理 | [AI 补位开赛](round61-multiroom-evidence/screenshots/room-b-human-ai-started.png)、[自动暂停](round61-multiroom-evidence/screenshots/room-b-disconnect-autopause.png)、[重连仍暂停](round61-multiroom-evidence/screenshots/room-b-rejoin-still-paused.png)、[房主恢复](round61-multiroom-evidence/screenshots/room-b-owner-resumed.png) |
| `650040` | 1v1 真人 + AI 建房尝试 | 建房成功；测试浏览器进程失去会话后进入 `cancelled`，不作为产品失败证据 | 无可靠业务结论 |
| `675098` | 4v4 两真人 + 六 AI 建房尝试 | 建房、跨阵营认领成功；测试浏览器进程失去会话后进入 `cancelled`，不作为产品失败证据 | 无可靠业务结论 |

## 关键验收证据

### 1. 真人断线 60 秒自动暂停，且不允许 AI 接管

1. 房间 `757885` 在自由辩论中由 AI 正在发言时关闭唯一真人辩手浏览器。
2. 断开记录时间为 `2026-07-23T19:53:55Z`，在 `2026-07-23T19:55:14Z` 再次观察时，观战页已经显示：
   - “真人断线超过 60 秒，比赛已自动暂停”；
   - “仍在等待真人重新连接”；
   - 真人席位仍标记为真人，AI 仅保留原有永久 AI 席位。
3. 服务端独立核验的事件顺序为：`presence.disconnected → participant.disconnect_timeout(grace_seconds=60) → match.paused`；`seat.ai_substituted=0`，席位类型仍为 `human`。

证据：[自动暂停画面](round61-multiroom-evidence/screenshots/room-b-disconnect-autopause.png)。

### 2. 重连后不自动恢复，必须由房主继续

1. 使用同一持久浏览器会话重新进入房间。
2. 页面显示“全部真人已重新连接”，但同时明确显示“比赛进度已保存，等待房主继续”。
3. 发言按钮仍禁用，比赛没有自行恢复。
4. 房主打开底部“比赛控制”，点击“继续比赛”后，页面恢复为“比赛进行中”，当前真人可以重新开始发言。

证据：[重连仍暂停](round61-multiroom-evidence/screenshots/room-b-rejoin-still-paused.png)、[房主手动恢复](round61-multiroom-evidence/screenshots/room-b-owner-resumed.png)。

### 3. 全系统最多 5 个观众

使用一个匿名浏览器中的六个独立标签同时连接房间 `757885`：

- 第 1 至第 5 个标签均显示“实时连接”；
- 第 6 个标签显示“观战已满”；
- 页面提示“系统观战总人数已达 5 人，请稍后重试。当前连接不会自动重试，请稍后刷新页面。”

证据：[第 5 人正常进入](round61-multiroom-evidence/screenshots/spectator-fifth-admitted-no-transcript.png)、[第 6 人被拒绝](round61-multiroom-evidence/screenshots/spectator-sixth-denied.png)。

### 4. 匿名观众不可见文字稿

- 观战页没有“文字记录”入口，只提供声音、全屏和观看设置。
- 在比赛终止后，观战页仍只显示舞台投影，没有跳转到包含逐字稿的参赛者结果页。
- 浏览器正文精确检索两段已完成发言：匿名观战页返回 `human=false, ai=false`；参赛者结果页返回 `human=true, ai=true`。

证据：[匿名观战画面](round61-multiroom-evidence/screenshots/spectator-fifth-admitted-no-transcript.png)。

### 5. 跨房间隔离

同一测试时段内：

- `522853` 的题目为“学校是否应该限制学生使用生成式人工智能？”，处于真人断线暂停状态；
- `757885` 的题目为“大学课堂应不应该允许学生使用人工智能辅助写作？”，仍在运行并由 AI 进行自由辩论；
- 两个观战标签分别显示各自题目、席位、阶段和状态；未出现对方房间的字幕、倒计时或控制状态。

证据：[房间 522853 暂停投影](round61-multiroom-evidence/screenshots/cross-room-522853-paused.png)、[房间 757885 运行投影](round61-multiroom-evidence/screenshots/spectator-fifth-admitted-no-transcript.png)。

## Issues

### ISSUE-001：已终止比赛的观战页仍展示过期的可发言语义

| 字段 | 内容 |
|---|---|
| 严重级别 | Medium |
| 分类 | UX / 状态一致性 |
| URL | `/rooms/757885/watch` |
| 视频 | N/A（静态终态问题） |

**问题描述**

房主提前终止比赛后，匿名观战页顶部正确显示“比赛已终止”，但舞台仍保留自由辩论的运行态语义：

- 当前真人同时显示“真人 · 已断线”和“真人在线 · 本轮可发言席位”；
- 计时器显示 `00:00`，但辅助文案仍为“点击开始发言后计时”；
- 举手队列仍显示“反方可申请下一轮”；
- 中央赛况仍显示“当前辩手点击‘开始发言’后正式计时”。

这会让观众误以为终止后的比赛仍可继续。终态页面应冻结为“比赛已终止”，隐藏可发言阵营、举手队列和待开始计时语义，并统一在线状态。

**复现步骤**

1. 匿名进入一场进行中的比赛观战页。
2. 房主通过“比赛控制 → 提前结束比赛 → 确认提前结束”终止比赛。
3. 保持匿名观战页打开，等待终态事件同步。
4. 观察到终止标题与旧的自由辩论操作语义同时存在。

证据：[终止后观战页状态矛盾](round61-multiroom-evidence/screenshots/issue-001-terminated-watch-stale-state.png)。

## 浏览器与控制台观察

- 没有捕获到 JavaScript 异常或未处理 Promise 错误。
- LiveKit 连接能建立并正常断开。
- 控制台中的 `play() failed because the user didn't interact with the document first` 来自无人工交互的无头浏览器自动播放策略；观战页默认静音并提供“开启声音”，本轮不把它判定为产品故障。
- AI 发言时观战页只展示单行滚动字幕，没有泄露完整历史文字稿。

## 测试限制与未验证项

- 本轮没有使用真实麦克风，因此真人发言通过产品提供的“改用文字发言”路径完成；未评估真实学生环境的 ASR 准确率和语音首包延迟。
- 测试初期同时打开过多独立浏览器进程，导致测试工具自身的两个会话重启并丢失认证。相关房间 `650040`、`675098` 的取消不归因于产品；后续改为低并发、持久会话后验证稳定。
- 本轮没有新增一场正常完成至总结、AI 裁判和正常赛果的比赛，不能据此宣称正常全流程完全无缺陷。

## 清理状态

- `650040`、`675098`：已进入 `cancelled` 终态并释放容量。
- `757885`：已由房主通过页面提前结束，进入终止结果页。
- `522853`：已由系统管理员通过控制接口安全终止。
- 六个 `r61qa_*` 测试账号已统一标记为测试账号并停用；关联房间从正式排行榜和数据质量统计中排除。
- 本轮创建的浏览器页面均已关闭；带持久恢复能力的空闲守护进程会由 `agent-browser` 按自身生命周期回收。
