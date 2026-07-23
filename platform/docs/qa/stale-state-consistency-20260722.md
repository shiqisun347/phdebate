# 长期暂停容量与 Room/Match 状态一致性修复报告

日期：2026-07-22

## 修复结论

本轮完成三类后端修复，未修改、删除或终止任何生产用户房间：

1. 长期暂停房间继续占用全局 5 场容量，但现在会被明确标记为 `stale_paused`，房主和管理员能看到暂停时长、容量占用以及可执行的“重试/恢复/终止”动作。
2. 增加只读 Room/Match 一致性扫描和显式补偿接口。扫描不会写数据库；补偿仅允许将已经终态的 Room 对应 Match 补成同一终态，并记录比赛事件和管理员审计日志。
3. 修复真人通过 REST 开赛但房间 WebSocket 尚未建立时，被立即 AI 接替的问题。比赛开始会重置真人席位的断线计时，确保从权威开赛时间起获得完整 60 秒恢复宽限。

同时统一固定轮次的席位提示为自然中文，例如 `当前轮到反方二辩`，不再显示 `当前轮到 反方2辩`。

## 长期暂停投影和容量

- 新增配置 `stale_paused_after_minutes`，默认 120 分钟，可配置范围 15 分钟至 7 天。
- 不自动删除、不自动终止长期暂停房间。
- 房主/管理员房间投影新增：
  - `pause_health.paused_at`
  - `pause_health.paused_duration_seconds`
  - `pause_health.is_stale`
  - `pause_health.capacity_consuming`
  - `pause_health.recommended_action`
  - `pause_health.can_terminate_to_release_capacity`
- 匿名观众不获得上述内部恢复信息。
- `GET /api/admin/rooms` 增加：
  - 每个房间的 `is_stale_paused`、`capacity_consuming`、`available_actions` 等字段。
  - 全局 `capacity`：上限、已开放、剩余、暂停及长期暂停数量。
- 全局长期暂停数量使用“最后一次房间写入时间 + 无真人在线”作为保守口径，避免在管理列表请求中扫描完整事件表；单房间暂停时间仍使用暂停/隔离/服务失败事件精确展示。

房主原有 `POST /api/rooms/{code}/control/terminate` 会在同一事务内更新 Room 与 Match，并立即释放容量。没有新增自动清理策略。

## Room/Match 一致性扫描与补偿

### 只读扫描

```http
GET /api/admin/consistency/room-matches?room_code=722633
```

扫描覆盖：

- `terminated/completed/review_required/cancelled Room` 与 Match 终态不一致；
- Match 已终态但 Room 仍处于 `lobby/preparing/running/paused/judging`。

第二类异常不会自动修复，因为房间可能仍有计时器、语音或用户控制状态，需要人工调查。

### 显式补偿

```http
POST /api/admin/consistency/room-matches/{roomCode}/repair
X-CSRF-Token: ...
X-Idempotency-Key: ...

{
  "expected_room_status": "terminated",
  "expected_match_status": "terminated",
  "reason": "补偿历史异常竞争导致的终态缺失"
}
```

安全约束：

- 仅系统管理员可调用；
- 使用房间事务锁和 Match 行锁；
- 请求必须携带扫描时观察到的 Room 状态和目标 Match 状态；
- 状态已变化时返回 409，要求重新扫描；
- 只允许安全的“终态 Room → 对应 Match 终态”方向；
- 幂等键重复调用不会产生第二条事件或审计记录；
- 补偿写入 `consistency.match_status_compensated` MatchEvent 和同名 AdminAuditLog；
- 补偿后重新生成比赛归档索引，不覆盖原始比赛事件和发言记录。

## 60 秒断线宽限修复

根因是席位在创建/认领大厅时已经写入 `disconnected_at`。若用户在大厅停留超过 60 秒，再通过 REST 开赛而 WebSocket 尚未建立，引擎会把大厅累计的离线时间误当成比赛离线时间，在第一个引擎 tick 立即执行 `seat.ai_substituted`。

修复后：

- `start` 锁定比赛时，对所有真人席位重建比赛期断线时钟；
- 已连接真人保持 `disconnected_at=null`；
- 尚未建立实时连接的真人从 `room.started_at` 开始计时；
- 59 秒内引擎不会接替；达到 60 秒后仍按现有规则 AI 接替；
- 大厅 2 分钟未锁定席位释放规则不变。

## 测试证据

新增 `test_round44_stale_state_consistency.py`，覆盖：

- 长期暂停仅投影、不自动删除；
- 房主明确终止后容量减少；
- 匿名观众不获得内部暂停健康信息；
- 管理员容量聚合；
- `terminated Room + running Match` 的只读扫描；
- 显式、可审计、幂等的状态补偿；
- 同一幂等键更换参数被拒绝；
- `active Room + terminal Match` 只报告、不自动修改；
- REST 开赛且无 WebSocket 时完整保留 60 秒宽限；
- 超过 60 秒后正常 AI 接替；
- 固定轮次中文席位提示。

相关回归：20 项通过。

全量 API 测试：

```text
534 passed, 1 xfailed, 2 warnings in 98.40s
```

Ruff 对本轮全部变更文件检查通过。测试仅使用隔离 SQLite 运行时，没有操作生产房间。

## 主要变更文件

- `apps/api/app/api/admin.py`
- `apps/api/app/api/rooms.py`
- `apps/api/app/core/config.py`
- `apps/api/app/schemas/requests.py`
- `apps/api/app/services/room_capacity.py`
- `apps/api/app/services/room_match_consistency.py`
- `apps/api/app/services/room_service.py`
- `apps/api/tests/test_round44_stale_state_consistency.py`
- `apps/api/tests/test_platform.py`
