# Iteration 21：参赛者退出、跨房占用与 ASR 人工修正生产审计

更新时间：2026-07-17 18:44 CST。状态：发现 2 个新 P1；生产未修改代码、未部署，未改变真实比赛控制状态。

## 本轮目标

补齐学生端以下真实链路：

```text
创建 1v1 训练赛
→ 真人发言
→ 结束发言并修改文字
→ 确认文本与录音归档一致
→ 退出/返回且不丢本地工作
```

本轮使用 Chrome 用户已有登录会话和新的 agent 临时标签；没有使用 AdsPower/SunBrowser。由于当前账号仍占用真实房间 278571 的真人席位，系统在创建阶段阻止了新房，未产生 Iteration 21 测试房或语音写入。

## 生产证据一：跨房真人席位唯一性正确生效

在首页打开“1v1 辩论训练赛”，填写唯一标识辩题并选择正方1辩，提交创建后得到：

> 你已在房间 #278571 参赛，请先返回该比赛或释放席位。

个人中心仍只显示 1 个进行中房间 278571；未显示 `QA-ITER21-ASR-编辑与退出保护-20260717`。公开 live rooms 中也没有该 marker。说明创建被原子阻止，没有留下半成品房间。

证据：[TC-991 创建阻止截图](../../screenshots/20260717-005403/TC-991-Iteration21-Chrome-跨房真人席位唯一性阻止建房.png)。

本地并发回归同时通过：

- 两个用户并发抢同一席位：一个 200、一个 409，只有一条 claim 事件。
- 同一用户两个设备并发抢两个房间：只有一个成功，最终活动真人席位恰好为 1。

## 生产证据二：退出页面确认本身安全

从创建拦截的“返回当前比赛”进入房 278571 的 `/debate`。当前临时标签不是控制设备，页面正确显示：

- “该席位已在其他设备接管”；主发言按钮 disabled。
- 点击顶部“退出比赛页面”打开 `alertdialog`，说明比赛继续、席位归属保留、离开较久时 AI 可能接替、可从“我的”返回。
- 点击取消后 dialog 关闭；未执行确认退出、接管设备、retry、pause、resume 或 terminate。

证据：[TC-990 退出确认截图](../../screenshots/20260717-005403/TC-990-Iteration21-Chrome-退出比赛页面确认与席位保留.png)。

Chrome console warning/error 为 0。最终房间仍为 `paused / remaining 172s`；本轮新增事件仅为 presence connected/disconnected。

## 新发现 P1：暂停房可无限期锁死非房主账号

当前实现的四条规则组合形成账号锁死：

1. `lobby/preparing/running/paused/judging` 都被视为跨房活动状态；只要席位仍是 `human`，用户不能创建或加入另一房。
2. `release-seat` 仅允许 `room.status == lobby`；开始后明确 409“比赛开始后不能释放席位”。
3. 开赛后普通参赛者没有 terminate/retry 等房间控制权；“退出比赛页面”只导航 `/me`，不改变席位。
4. 离线 AI 接替只处理 `preparing/running/judging`，不处理 `paused`；暂停房也不会被 engine 正常轮询推进。

真实房 278571 已暂停较长时间。18:41 CST 公开快照仍是：

```text
status=paused
remaining_seconds=172
aff_3 occupant_type=human
connected=false
```

因此：

- 房主本人尚可通过“提前结束比赛”解除占用，但这是终止整场比赛，不是退出席位。
- 非房主若房主不再回来，既不能释放席位，也不能触发暂停房 AI 接替，会无限期无法创建或加入任何新比赛。
- 创建弹窗提示“返回比赛或释放席位”具有误导性；开赛后不存在可执行的释放入口。

严重度：**P1**。它会阻断学生继续使用平台，且可由常见的服务异常自动暂停、房主离场或长期未处理房间触发。

## 最小产品方案

保留现有“退出比赛页面”（只导航、不丢席位），另外增加明确且不可混淆的：

> 退出本场并由 AI 接替

后端不变量：

- 仅非房主可在 `preparing/running/paused/judging` 调用；房主继续使用终止比赛，避免无房主比赛。
- 当前真人 speech 为 speaking 时返回 409，必须先完成或放弃当前发言。
- 在 user + room transaction lock 下，把 seat 从 `human` 原子转换为 `ai_substitute`。
- 保留 `user_id`、原席位、参赛和排名/审计归属；清 control lease、connected 和本地发言控制。
- 写入幂等 `seat.abandoned` / `seat.ai_substituted` 事件，包含 actor 与明确 reason。
- 转换成功后现有跨房检查自然解除，因为它只拦截 `occupant_type == human`。

前端保护：

- 使用独立危险确认框，明确“本场不能自行恢复、AI 将永久接替、已完成发言和赛果归属保留”。
- starting/capturing/finishing/pending transcript 时 disabled，并说明必须先提交或放弃本地工作。
- 不得把该动作折叠进普通“退出页面”；两个动作的可逆性完全不同。

不建议：

- 直接从活动状态集合删除 `paused`：会允许同一学生同时持有多个可恢复真人席位。
- 开赛后把席位清空为 `open`：会破坏比赛身份、历史归属和审计链。
- 为绕过锁死而恢复真实暂停房并等待 60 秒：会改变正式比赛进程。

## 第二个 P1：管理员恢复 AI 接替席位可破坏跨房唯一性

运行中离线 60 秒后，旧席位会从 `human` 变为 `ai_substitute`，仍保留 `user_id`。由于跨房检查只拦截 `occupant_type == human`，该学生此时可以创建或认领新房，这是预期的“解除占用”。

但当前 system admin 的恢复真人席位路径没有重新执行跨房活动席位检查，也没有获取 participant transaction lock。管理员若在学生已进入新房后恢复旧房，可使同一用户同时在两个活动房间成为 `human`；数据库没有跨房 user_id 唯一约束兜底。

严重度：**P1 DATA/ISOLATION**。它破坏平台宣称的“一名真人同一时刻只占一个活动房间”，也可能导致两个房间同时授权发言、设备控制和积分归属。

恢复必须：

- 锁定 participant user，再查询是否已有其他活动 `human` assignment。
- 发现冲突返回 409，不能恢复。
- 最好改为学生发起恢复请求、房主或管理员批准；仍需无活动发言、无另一活动真人席位。
- 若提供短时撤销放弃，仅允许 AI 尚未开始发言且用户尚未进入新房。

## ASR 人工修正证据状态

生产 E2E 未执行，原因是缺少不占用其他房间的隔离 QA 学生会话。创建新账号属于需要用户在动作前确认的账户操作；本轮没有代替用户创建账号，也没有释放/终止真实房 278571。

本地自动化证据：

- `DebateStage` 74 passed；其中 clean ASR 也能通过“结束发言并修改文字”强制停在“提交前核对发言文字”。
- ParticipateDialog + Lobby + DebateStage 合计 93 passed。
- API 4 passed：并发同席位抢占、同用户跨房唯一性、finish/audio 幂等、人工文本替换 ASR segment 并把同一 speech 的音频归档。
- 人工修正用例确认最终 `Speech.content`、唯一 final `TranscriptSegment.text` 等于人工文本，`audio_url` 与 `duration_seconds` 保留在同一 speech。

完整生产验收仍需一次性 QA 账号/房间，固定朗读：

> 这是一次真人麦克风与语音识别测试。蓝色风筝经过第七座桥。正方认为清晰证据比快速结论更重要。我的发言到此结束。

提交前只把“清晰证据”修改为“可靠证据”，以证明数据库文本来自人工修正，而音频仍保留原始真人录音。必须同时保存 MediaRecorder、ASR final、人工修正、finish、audio upload、麦克风释放和最终数据一致性证据；任一缺失都不能判定通过。

## 额外 QA 缺口

现有 `authenticated-lobby.spec.ts` 仍使用旧文案“立即参赛/创建房间”，当前 UI 已改为“创建比赛”；该用例默认又被 `E2E_MUTATING` 跳过。它只覆盖创建、认领、准备、释放和关闭，没有覆盖开始比赛、真人发言、两种结束路径或退出保护。因此相关 93 个单测全绿不能替代真实双用户浏览器 E2E。严重度：P2 QA。

当前结论：退出页面确认 PASS；正常创建/认领时跨房唯一性 PASS；暂停房退赛缺口 FAIL/P1；管理员恢复跨房冲突 FAIL/P1；生产真人 ASR 修改 E2E BLOCKED。
