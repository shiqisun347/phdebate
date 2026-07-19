# 真人席位恢复闭环

## 使用场景

真人辩手断线超过保护时间、主动退出，或被系统切换为 `ai_substitute` 后，AI 会继续完成后续发言。原辩手返回时不会自动抢回麦克风，而是通过明确申请恢复控制，避免与正在发言的 AI、其他设备或另一场比赛冲突。

## 用户流程

1. 原辩手在个人中心或本场观战页点击“申请恢复真人席位”。
2. 当前有发言时按钮保持禁用；发言结束后才允许申请。
3. 房主或系统管理员在 `/rooms/:code/control` 查看待审批申请。
4. 管理者可批准或暂不批准；申请者也可在处理前撤销。
5. 批准后席位恢复为 `human`，房间 WebSocket 立即同步，辩手重新进入比赛页取得控制租约。
6. 比赛完成、终止或进入人工复核时，未处理申请持久化为 `expired`。

## 服务端约束

- 仅席位原始 `user_id` 对应的登录用户可以申请和撤销。
- 仅房主和 `system_admin` 可以审批。
- 比赛必须处于 `preparing`、`running`、`paused` 或 `judging`。
- 席位必须仍为 `ai_substitute`，账号必须可用。
- 整个房间当前不得存在 `speaking`、`synthesizing` 或 `playing` 发言。
- 同一用户不得在其他大厅或活动比赛中拥有真人席位。
- 创建请求支持 `X-Idempotency-Key`；重复提交返回原请求，不生成重复事件。
- 请求状态为 `pending / approved / rejected / cancelled / expired`，所有变化进入只追加比赛事件流。
- 匿名观众和无关参赛者看不到申请、申请者身份或审批状态。

## REST API

- `POST /api/rooms/:code/seat-restore-requests`
- `POST /api/rooms/:code/seat-restore-requests/:id/cancel`
- `POST /api/rooms/:code/seat-restore-requests/:id/approve`
- `POST /api/rooms/:code/seat-restore-requests/:id/reject`

房间投影中的 `seat_restore_requests` 按查看者过滤：申请者只能看到自己的申请，房主和系统管理员可看到本房间申请，匿名投影始终为空。

## 验证范围

- API：申请、重复提交、撤销、拒绝、房主批准、越权、跨房间真人席位冲突、活跃发言保护、终局持久失效。
- 实时：控制台已连接 WebSocket 时，申请事件和带权限的最新房间投影同步到达。
- Web：观战悬浮恢复面板、个人中心入口、控制台审批、移动布局和 axe 可访问性。
- 迁移：`0024_seat_restore_requests` 在已有数据库、全新数据库及降级后重升场景均可执行。

本功能未修改 `DebateStage`、浏览器音频库、AudioWorklet、TTS、MOSS、LiveKit 或 FunASR 实现。
