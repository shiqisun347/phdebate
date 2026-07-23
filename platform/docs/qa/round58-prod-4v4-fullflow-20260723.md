# Round 58 生产 4v4 混合辩论黑盒验收

| 项目 | 结果 |
|---|---|
| 测试时间 | 2026-07-24 00:10–00:34（Asia/Shanghai） |
| 生产地址 | `https://117.50.192.216` |
| Web release | `round57c-caption-playback-20260723` |
| source commit | `261bad7642b4eeea51419a06d456920298e68e7c` |
| 房间 | `199964` |
| 阵容 | 4v4：正方一辩真人、反方一辩真人，其余 6 席为开赛时永久 AI |
| 观众 | 1 名匿名观众；测试结束已关闭 |
| 结束状态 | 房主主动“提前结束比赛”，房间进入 `terminated` 结果页 |
| 结论 | 主流程实测到自由辩论入口；发现 1 个 P1、1 个 P2。因生产发布窗口要求释放比赛，本轮未执行自由辩论实际发言、总结、裁判、正常赛果和 60 秒断线验收，不能据此宣称完整全流程通过。 |

## 执行边界与清理结果

- 本轮严格使用生产页面黑盒操作，没有读取应用源码、修改代码或部署。
- 只创建 1 个房间、1 个匿名观众，未突破“最多 5 个房间、全站最多 5 个观众”。
- 测试账号为本轮新注册的两个临时普通用户；没有使用管理员权限。
- 房主在自由辩论入口主动终止房间，房间结果页显示“比赛已终止”，不会计入正常胜负结算。
- `round58-owner`、`round58-participant`、`round58-watch` 三个浏览器会话均已关闭，匿名观众连接已释放。
- 本轮没有执行“真人离线满 60 秒”的验收。不得把本报告当作该机制的生产通过证据。

## 流程验收矩阵

| 流程 | 结果 | 生产证据与观察 |
|---|---|---|
| 首页与公开赛事 | 通过 | 首页正确展示 4v4 正式赛、1v1 训练赛、公开比赛和排行榜摘要。[01-home.png](assets/round58-prod-4v4-fullflow/01-home.png) |
| 普通用户注册 | 通过 | 两个独立账号均可用“登录账号 + 真实姓名 + 密码”完成注册，注册后自动登录并返回赛事大厅。[03-owner-registered.png](assets/round58-prod-4v4-fullflow/03-owner-registered.png)、[07-participant-registered.png](assets/round58-prod-4v4-fullflow/07-participant-registered.png) |
| 创建 4v4 房间 | 通过 | 房主可选择题库辩题和自己的正方一辩席位；未选席位时提交按钮禁用。[04-create-room-dialog.png](assets/round58-prod-4v4-fullflow/04-create-room-dialog.png) |
| 房间号搜索与加入 | 通过 | 第二名用户通过六位房间号 `199964` 搜索并进入同一大厅。[08-search-room.png](assets/round58-prod-4v4-fullflow/08-search-room.png)、[09-participant-lobby.png](assets/round58-prod-4v4-fullflow/09-participant-lobby.png) |
| 抢座与身份绑定 | 通过 | 第二名用户认领反方一辩；双方姓名、阵营、辩位和在线状态对两端一致。[10-participant-seat-claimed.png](assets/round58-prod-4v4-fullflow/10-participant-seat-claimed.png) |
| 麦克风预检失败恢复 | 通过 | Headless 浏览器拒绝麦克风权限后，页面给出明确说明；预检不是准备硬门禁，仍可确认准备。[06-owner-mic-check.png](assets/round58-prod-4v4-fullflow/06-owner-mic-check.png) |
| 准备与开赛门禁 | 通过 | 非房主准备后只能等待；两名真人都准备后，房主才看到“开始比赛”。[11-participant-ready.png](assets/round58-prod-4v4-fullflow/11-participant-ready.png)、[12-owner-ready-start-enabled.png](assets/round58-prod-4v4-fullflow/12-owner-ready-start-enabled.png) |
| 开赛确认与 AI 补位 | 通过 | 二次确认明确告知锁定 2 位真人并由 AI 补齐 6 个空席；开赛后两队各 4 席完整，真人和 AI 标识清楚。[13-start-confirmation.png](assets/round58-prod-4v4-fullflow/13-start-confirmation.png)、[15-owner-debate-opening.png](assets/round58-prod-4v4-fullflow/15-owner-debate-opening.png) |
| 主持提示与自动推进 | 通过 | 页面先进入“开场与规则”，随后无需主持人操作自动进入正方一辩立论；阶段提示显示“相关辩手做好准备”。[15-owner-debate-opening.png](assets/round58-prod-4v4-fullflow/15-owner-debate-opening.png) |
| 当前真人发言权限 | 通过 | 正方一辩看到可用“开始发言”，反方一辩同时看到禁用的“等待轮次”；当前席位、按钮和中央舞台一致。[16-participant-debate-opening.png](assets/round58-prod-4v4-fullflow/16-participant-debate-opening.png) |
| 房主暂停与恢复 | 通过 | 房主控制面板只提供暂停、提前结束和退出；暂停后双方同步显示“比赛已暂停”，恢复后回到同一阶段且计时仍等待真人开始。[17-owner-control-open.png](assets/round58-prod-4v4-fullflow/17-owner-control-open.png)、[18-owner-paused.png](assets/round58-prod-4v4-fullflow/18-owner-paused.png)、[19-participant-sees-pause.png](assets/round58-prod-4v4-fullflow/19-participant-sees-pause.png)、[21-owner-resumed.png](assets/round58-prod-4v4-fullflow/21-owner-resumed.png) |
| 普通辩手越权控制 | 通过 | 普通辩手的“更多操作”只有退出页面，没有暂停、继续或终止比赛入口。[20-participant-more-actions.png](assets/round58-prod-4v4-fullflow/20-participant-more-actions.png) |
| 真人无麦克风文字恢复 | 通过 | 两名真人均可锁定当前轮次、提交本轮文字并正常完成发言，服务端推进到下一阶段。[22-owner-text-fallback.png](assets/round58-prod-4v4-fullflow/22-owner-text-fallback.png)、[23-owner-text-ready-submit.png](assets/round58-prod-4v4-fullflow/23-owner-text-ready-submit.png)、[25-negative-turn-ready.png](assets/round58-prod-4v4-fullflow/25-negative-turn-ready.png) |
| AI 固定阶段 | 通过（抽样） | 正反二辩及后续 AI 阶段可自动生成、播放并推进；阶段内字幕为中央单行短句，没有整段多行堆叠。[28-ai-stage-single-line-caption.png](assets/round58-prod-4v4-fullflow/28-ai-stage-single-line-caption.png) |
| 自由辩论入口 | 部分通过 | 自动进入自由辩论，双方阵营、举手队列和底部控制栏出现；但发现当前轮次显示 `00:00`，详见 ISSUE-001。[29-free-debate-owner.png](assets/round58-prod-4v4-fullflow/29-free-debate-owner.png)、[30-free-debate-participant.png](assets/round58-prod-4v4-fullflow/30-free-debate-participant.png) |
| 匿名观战权限 | 通过 | 开赛前只能看等待页；开赛后只有声音、全屏、观看设置，页面没有“文字记录”按钮，也没有任何真人或 AI 发言正文/字幕。[14-anonymous-watch-lobby.png](assets/round58-prod-4v4-fullflow/14-anonymous-watch-lobby.png)、[26-anonymous-watch-running.png](assets/round58-prod-4v4-fullflow/26-anonymous-watch-running.png) |
| 房主异常终止与提示 | 通过 | 操作前有“不可恢复”的二次确认；确认后所有参赛者进入 `result`，结果标记“比赛已终止”、不计入正常胜负。[31-terminate-confirmation.png](assets/round58-prod-4v4-fullflow/31-terminate-confirmation.png)、[32-room-terminated.png](assets/round58-prod-4v4-fullflow/32-room-terminated.png) |
| 总结、AI 裁判、正常结果 | 未测 | 为释放生产 `active_match_processing`，按发布窗口要求在自由辩论入口终止。 |
| 真人断线 60 秒自动暂停 | 未测 | 未执行离线计时、席位类型和事件日志检查；没有产生该项生产证据。 |

## ISSUE-001（P1）：自由辩论尚未开始时，主操作按钮显示“本轮剩余 00:00”

| 字段 | 内容 |
|---|---|
| 严重级别 | P1 |
| 类别 | 功能状态表达 / 核心操作 UX |
| 页面 | `/rooms/199964/debate` |
| 复现稳定性 | 同一时刻在房主端和普通辩手端均稳定复现 |

### 复现步骤

1. 建立 4v4 房间，正反一辩为真人，其他 6 席由 AI 补齐。
2. 完成双方一辩真人立论和前续 AI 固定阶段。
3. 等待系统自动进入“自由辩论”。
4. 观察当前正方真人的底部主按钮，以及反方真人的等待按钮。

### 实际结果

- 中央总时长正确显示 `05:00`，并提示“当前辩手点击开始发言后正式计时”。
- 当前正方的可点击主按钮却显示“轮到你发言 · 本轮剩余 `00:00`”。
- 反方同时显示“自由辩论当前轮到正方 · 本轮剩余 `00:00`”。
- 用户无法判断点击后是否仍有发言时间，核心动作与计时状态互相矛盾。

证据：[29-free-debate-owner.png](assets/round58-prod-4v4-fullflow/29-free-debate-owner.png)、[30-free-debate-participant.png](assets/round58-prod-4v4-fullflow/30-free-debate-participant.png)。

### 期望结果

- 若自由辩论单轮初始时长为 30 秒，开局应显示 `00:30`。
- 若单轮倒计时必须等点击后才初始化，按钮在开始前不应显示 `00:00`，可改为“开始本轮发言”；点击成功后再展示真实倒计时。
- 服务端和前端必须使用同一权威剩余时间，零秒时不得仍给出看似可用的主操作。

### 本轮未验证的后果

本轮为释放发布窗口，没有继续点击该按钮，因此尚未证明它会导致服务端拒绝或自由辩论无法继续。当前证据足以证明核心操作状态表达错误，但不能升级为“流程必然阻塞”的 P0 结论。

## ISSUE-002（P2）：真人文字发言在结果页被改写为英文逗号，文字记录格式不一致

| 字段 | 内容 |
|---|---|
| 严重级别 | P2 |
| 类别 | 数据呈现 / 文字记录保真度 |
| 页面 | `/rooms/199964/result` |
| 复现稳定性 | 两名真人提交的中文发言均出现 |

### 复现步骤

1. 在真人“改用文字完成本轮发言”窗口提交包含中文逗号 `，` 的内容。
2. 终止比赛并进入结果页。
3. 对比提交前文本和“完整辩论文字记录”。

### 实际结果

- 提交窗口保留中文标点：[23-owner-text-ready-submit.png](assets/round58-prod-4v4-fullflow/23-owner-text-ready-submit.png)。
- 结果页中两名真人的中文逗号被转换为英文逗号 `,`；同页 AI 发言仍使用中文标点，记录风格不一致：[32-room-terminated.png](assets/round58-prod-4v4-fullflow/32-room-terminated.png)。

### 期望结果

- 文字应按用户实际提交内容原样保存和展示；如确需标准化，应保留原文并把标准化文本作为独立字段。
- 该平台以收集学生辩论数据为目标，不应在没有说明的情况下修改原始发言文字。

## 非缺陷观察

- 匿名观众首次加载时，浏览器控制台出现一次 `NotAllowedError: play() failed because the user didn't interact with the document first`。页面默认静音并明确提供“开启比赛声音”，点击由用户解锁音频符合浏览器自动播放策略，本轮不记为产品缺陷。
- Headless 浏览器没有真实麦克风，预检提示权限被拒绝；页面提供文字恢复路径，固定阶段可以继续推进。本轮不能据此评价 ASR 识别质量。
- LiveKit 参赛者和匿名观众均成功连接到 `debate:199964`；本轮未进行主观 TTS 音质、卡顿、撕裂音或首包延迟测量。

## 后续必须补测

以下项目仍是“完整比赛可用”结论的必要证据，不能由本轮部分流程替代：

1. 修复 ISSUE-001 后，实际完成至少两轮自由辩论：开始、倒计时、对方举手、三秒换方、取消举手、重复申请和轮次归属。
2. 跑完正反总结、AI 裁判、正常结果和积分更新；刷新结果页后内容仍一致。
3. 真人席位离线满 60 秒：房间自动 `paused`，席位仍为 `human`，原 `user_id` 不变，且不存在 `seat.ai_substituted`。
4. 房主恢复后，真人必须重新点击开始发言，冻结计时不能在暂停期间或恢复瞬间偷跑。
5. 使用真实浏览器和麦克风补测 ASR 双流式字幕，以及在一场完整比赛中持续监听 TTS/LiveKit 音轨。
