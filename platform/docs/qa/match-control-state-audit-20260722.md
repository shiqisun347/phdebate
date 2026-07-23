# 比赛控制与状态机审计（2026-07-22）

## 结论

本轮以“比赛能完成、真人席位永不被 AI 接管”为最高约束，审计了开始、准备、发言、自由辩论、暂停、继续、重试、跳过、终止、断线和旧席位恢复路径。

本轮修复了一个真实的跨事件竞态：真人断线满 60 秒后，旧 Agent/TTS 任务可能晚于断线事件写入 `provider.failed`。旧控制逻辑只查看最新事件，会丢失断线暂停语义，从而可能把房间错误归类为普通服务异常。现在断线边界只会被一次成功的房主/管理员 `resume` 或 `retry` 清除；WebSocket 重连本身不会清除边界或自动继续比赛。

## 已验证的硬性不变量

1. 运行中的真人席位保持 `occupant_type=human`、原 `user_id` 和姓名，不转成 `ai` 或 `ai_substitute`。
2. 真人 WebSocket 断线不足 60 秒时保留席位并显示宽限状态；满 60 秒后自动安全暂停。
3. 断线暂停会中断当前真人/AI 发言和裁判任务，并保留已经确认的真人文字。
4. 真人重新连接后房间仍为 `paused`，不会自动恢复。
5. 只有房主或系统管理员能够执行 `resume`/`retry`；任何仍离线或账号不可用的真人都会阻止 `resume`、`retry` 和 `skip`。
6. `/abandon-seat` 固定返回 410，参赛者不能选择 AI 接替。
7. `ai_substitute` 仅作为历史数据兼容值读取；引擎遇到它会暂停并阻止生成新发言。
8. 自由辩论无人举手时，只有该方本来就存在永久 AI 席位才允许 AI 发言；纯真人一方不会生成 AI 接替。
9. 比赛开始时仅把仍为 `open` 的空席填成永久 AI；已经锁定的真人席位不参与 AI 补位。
10. 重复开始、控制幂等键、跨房间隔离、零秒计时边界、自由辩论三秒窗口和裁判暂停恢复已有回归测试覆盖。

## 本轮代码修复

### 断线暂停上下文改为事件边界，而不是“最新事件”

新增 `participant_disconnect_pause_context()`：

- 查找最近一次成功 `control.resume`/`control.retry` 之后的全部 `participant.disconnect_timeout`；
- 普通 `provider.failed`、字幕、语音和 presence 事件不能清除断线边界；
- 重连只让手动恢复操作变为可执行，不会自动改变房间状态；
- 如果断线前后还存在服务失败，恢复动作明确要求 `retry`；纯断线暂停使用 `resume`；
- 两种动作都要求全部真人在线且账号有效。

房间序列化与控制 API 使用同一个判断函数，避免前端提示“可继续”而后端要求“重试”，或后端在乱序事件下绕过断线校验。

### 新增竞态回归测试

新增用例覆盖：

1. 两名真人开始比赛；
2. 一名真人离线并写入 60 秒断线超时；
3. 模拟旧 Provider 任务随后才写入失败事件；
4. 真人仍离线时 `retry` 必须返回 409；
5. 真人重连后房间仍保持暂停；
6. 房主显式 `retry` 后才恢复；
7. 席位类型、用户 ID 全程不变。

## 控制边界审计

| 操作 | 允许状态 | 关键保护 |
| --- | --- | --- |
| 开始 | `lobby` | 所有真人准备；只补 `open` 空席；固定 Match 和服务快照；重复开始不产生第二场 Match |
| 暂停 | `running` / `judging` | 中断 AI/裁判任务并冻结阶段、自由辩论和主持提示音计时；真人发言使用紧急安全暂停路径 |
| 继续 | `paused` | 仅人工暂停或纯断线暂停；全部真人必须在线；服务失败必须改用重试 |
| 重试 | 异常 `paused` | 全部真人必须在线；保留阶段和自由辩论剩余时间；裁判阶段恢复为 `judging` |
| 跳过 | `running` / `paused` / `judging` | 断线超时且仍有真人离线时禁止；裁判阶段进入人工复核而不伪造结果 |
| 终止 | `preparing` / `running` / `paused` / `judging` | 关闭发言、裁判和自由辩论申请，Match/Room 同步终止 |

开始后的 `preparing` 只同步确保第一条主持提示音可用，其他提示音后台预取；引擎轮询间隔默认 250ms。没有额外的业务倒计时或等待确认。若系统预设提示音缺失，比赛会明确暂停，而不是在不确定状态下继续。

## 测试证据

- `test_round45_no_ai_takeover_policy.py`：13 passed。
- 状态与控制相关组合：42 passed，覆盖 round 8/9/12/16/33/43/44/45。
- Ruff：本轮修改文件全部通过。
- 运行时代码搜索：不存在向 `ai_substitute` 赋值的路径；唯一 `seat.occupant_type = "ai"` 位于开赛时处理原本为 `open` 的空席。
- 运行时代码搜索：WebSocket/presence 模块不存在把房间状态改回 `running` 的逻辑，因此重连不会自动继续。
- API 全量测试尝试运行至 315 passed 后，一个既有 MOSS WebSocket 丢 ACK 时序用例发生一次 0.5 秒外层超时；该用例随即独立重跑为 1 passed。本轮未修改 Provider，实现证据以控制相关 42 项稳定通过为准；全量时序测试仍应在主任务统一环境中再跑一次。

执行命令：

```bash
.venv/bin/pytest -q \
  apps/api/tests/test_round8_engine_scenarios.py \
  apps/api/tests/test_round9_engine_scenarios.py \
  apps/api/tests/test_round12_lifecycle.py \
  apps/api/tests/test_round16_engine_resilience.py \
  apps/api/tests/test_round33_control_boundaries.py \
  apps/api/tests/test_round43_multi_match_flow_audit.py \
  apps/api/tests/test_round44_fault_injection_match_flow.py \
  apps/api/tests/test_round45_no_ai_takeover_policy.py
```

## 保留风险与后续建议

- 历史数据库仍可能存在 `ai_substitute` 记录。当前策略是只读展示、阻止执行，并允许管理员恢复为真人；没有删除历史审计数据。
- 普通暂停在真人正在发言时仍返回明确冲突，控制台需使用安全暂停以保存已确认文字并中断发言。这一双操作兼容现有前端和审计事件，但产品层可以后续合并为一个“暂停比赛”按钮。
- 本轮未部署生产；应由主任务在合并其他并行改动后统一运行 API 全量测试和生产灰度。
