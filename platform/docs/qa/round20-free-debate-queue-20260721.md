# Round 20 自由辩论真人举手队列

日期：2026-07-21
范围：服务端权威举手队列、三秒换边窗口、真人选择与 AI fallback
排除：外部 Agent“是否发言”决策、冻结 TTS/浏览器播放器

## 实现结果

- 新增 Alembic `0029_free_turn_requests` 和 `FreeTurnRequest`。
- 真人可在对方实际开始发言后申请下一轮，也可取消自己的 pending 申请。
- 真人发言以 `speech.started` 后开放；AI 必须存在权威 `playback_started_at`，Agent/TTS 准备阶段不能提前举手。
- 发言完成后进入持久化三秒 intermission，不再立即换边。
- 窗口截止后按服务端 `requested_at + id` 选择最早且仍在线、仍为对应真人席位的申请；其余请求过期。
- 无申请时设置权威 AI fallback；存在对应 AI 席位则进入既有 AI 发言流程，没有 AI 席位时把空轮限制在三秒内，避免无限等待。
- 每席位、每阶段、每轮最多一个 pending；幂等键绑定房间、阶段、轮次、席位和用户。
- 暂停会冻结 intermission 剩余毫秒，恢复后继续；终止会使 pending 全部过期。
- 队列和窗口完全持久化，Engine 重启后可从数据库和 stage snapshot 恢复并裁决。
- 房间 snapshot 新增 `free_turn_queue`：
  - 脱敏 `side / seat_key / order / requested_at / is_me`；
  - `window_deadline_at / window_remaining_ms`；
  - `my_request / can_request / request_reason`；
  - `target_side / target_turn_seq`。
- `speaking_permission` 只允许选中的真人席位发言；intermission 和 AI fallback 期间真人按钮保持禁用并返回具体原因。

## 接口

- `POST /api/rooms/:code/free-turn-requests`
- `POST /api/rooms/:code/free-turn-requests/:requestId/cancel`

所有写操作校验登录用户、房间、自由辩论阶段、下一阵营、真人席位、在线状态、窗口、轮次和幂等键。

## 测试覆盖

新增 `apps/api/tests/test_round20_free_debate_queue.py`：

- 两名真人并发申请，服务端顺序稳定且只选中一个。
- 同轮幂等重放不重复创建。
- 队列显示 ISO 申请时间和权威序号。
- 非选中真人不能绕过按钮直接发言。
- 取消后重新申请、跨房取消拒绝。
- 暂停/恢复三秒窗口、截止边界、终止过期。
- 无申请时 AI fallback，重启后无需进程内状态即可恢复。
- AI 尚在生成时申请返回 409；实际播放开始后允许申请。

同时调整既有自由辩论测试以符合显式三秒 intermission。

验证结果：

```text
10 passed, 193 deselected, 1 warning in 2.47s
All checks passed!
```

迁移实际验证：

```text
empty -> 0029 upgrade: success
0029 -> 0028 downgrade: success
0028 -> 0029 upgrade: success
```

## 文件

- `apps/api/alembic/versions/0029_free_turn_requests.py`
- `apps/api/app/models/entities.py`
- `apps/api/app/services/free_turn_queue.py`
- `apps/api/app/services/match_engine.py`
- `apps/api/app/services/room_service.py`
- `apps/api/app/api/rooms.py`
- `apps/api/tests/test_round20_free_debate_queue.py`
- 更新既有自由辩论回归测试

## 后续阶段

本轮未实现外部 Agent 对“是否申请发言”的决策协议。第二阶段可以让 Agent 通过同一服务端队列提交意愿，但不得绕过当前权威窗口、排序和轮次校验。
