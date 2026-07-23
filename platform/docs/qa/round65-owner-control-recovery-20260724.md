# Round 65：房主控制、开赛等待与异常恢复专项

日期：2026-07-24  
范围：API、match-engine、房主/参赛者权限、开赛准备、倒计时、暂停、恢复、重试、终止、真人断线和完整 4v4 流程。  
部署状态：仅本地实现与验证，未修改 Web，未部署生产环境。

## 结论

房主和普通参赛者的控制边界、真人发言保护、60 秒断线暂停、服务重试、终止后的不可恢复边界以及完整 4v4 多人多 Agent 流程均通过回归。

本轮发现并修复一项开赛等待阶段的控制缺口：房主点击开始后，房间进入 `preparing` 且首阶段尚未建立，此时原接口不能暂停。如果只放开暂停而不修正恢复逻辑，`current_stage_index=-1` 的房间会被恢复为无当前阶段的 `running`，随后引擎可能把比赛误判为流程结束并进入 `review_required`。

修复后：

- 房主可以在 `preparing` 阶段正常暂停。
- 准备阶段没有虚假的一秒倒计时。
- 暂停期间引擎不会进入首阶段、创建发言或结束比赛。
- 恢复后回到 `preparing`，由原有提示音准备和首阶段进入逻辑继续处理。
- 手工暂停和真人断线暂停使用同一个安全恢复边界。

完整 API 回归结果：

```text
596 passed, 13 skipped, 1 xfailed, 2 warnings in 103.77s
```

## 修改文件

- `platform/apps/api/app/api/rooms.py`
- `platform/apps/api/tests/test_round65_owner_control_recovery.py`
- `platform/docs/qa/round65-owner-control-recovery-20260724.md`

## 新增测试

### 1. 开赛准备期间暂停与恢复

测试：`test_owner_can_pause_and_resume_initial_match_preparation_without_finishing_match`

覆盖：

- 房主开始比赛后房间处于 `preparing`、阶段索引为 `-1`。
- 房主暂停成功，重复相同幂等请求只重放结果，不产生第二条控制事件。
- 暂停状态不伪造倒计时。
- 暂停期间连续执行引擎处理，房间仍为 `paused`，阶段索引仍为 `-1`。
- Match 保持 `running`，没有 Speech，也不会进入 `review_required`。
- 房主恢复后房间回到 `preparing`，而不是无阶段的 `running`。
- 下一次引擎处理正常进入首个开场阶段并建立正确倒计时。
- `control.pause` 和 `control.resume` 各且仅各产生一条事件。

### 2. 房主权限、真人紧急暂停与终止

测试：`test_only_owner_controls_live_human_recovery_and_termination`

覆盖：

- 普通参赛者直接调用暂停和终止接口均返回 403。
- 真人正在发言时，普通暂停返回 409，不能静默丢弃真人文字。
- 房主使用 `safe-pause` 后，当前发言变为 `interrupted`，当前阶段重新等待同一真人开始。
- 恢复后阶段和剩余时间保留，真人可重新建立一条发言。
- 房主在真人发言过程中终止比赛，活动发言被关闭，Room 和 Match 同时进入 `terminated`。
- 重复相同终止幂等请求只返回重放结果。
- 终止后到达的迟到发言提交返回 409，不能复活比赛或修改终态。
- 后续引擎处理不会改变终止状态和阶段索引。
- 全程不存在 `seat.ai_substituted`，两个真人席位始终保持 `human`。

## 既有完整流程回归

本轮没有再复制一套脆弱的 4v4 流程测试，而是将以下权威完整比赛测试纳入组合回归：

- `test_round62_full_4v4_state_machine.py`：完整正式阶段、多人和多 Agent、提示音、自由辩论、总结与裁判。
- `test_round64_complete_mixed_match.py`：2 真人 + 6 Agent、Agent 超时重试、真人断线 61 秒暂停恢复、跨房间隔离和完整赛果。

组合回归同时包含：

- 开赛就绪和服务等待。
- 正数倒计时最后一秒边界。
- 暂停/恢复后倒计时不增加。
- 异常重试不把 0 秒重新变成新倒计时。
- 真人断线前 60 秒冻结等待，超过 60 秒自动暂停。
- 真人重连后必须由房主或管理员显式恢复。
- 不允许永久 Agent 或临时 Agent 接管断线真人。
- 运行中房主断线不转移 `owner_id`。

## 验证结果

新增专项：

```text
uv run pytest -q tests/test_round65_owner_control_recovery.py -x
2 passed, 1 warning in 0.59s
```

控制、计时、断线和完整 4v4 组合回归：

```text
uv run pytest -q \
  tests/test_round24_start_readiness.py \
  tests/test_round33_control_boundaries.py \
  tests/test_round45_no_ai_takeover_policy.py \
  tests/test_round56_human_timer.py \
  tests/test_round57_human_recovery_clock.py \
  tests/test_round63_control_timing.py \
  tests/test_round62_full_4v4_state_machine.py \
  tests/test_round64_complete_mixed_match.py \
  tests/test_round65_owner_control_recovery.py

39 passed, 1 warning in 5.45s
```

代码检查：

```text
uv run ruff check app/api/rooms.py tests/test_round65_owner_control_recovery.py
All checks passed!
```

完整 API 回归：

```text
uv run pytest -q
596 passed, 13 skipped, 1 xfailed, 2 warnings in 103.77s
```

跳过项依赖当前环境未启用的可选外部运行条件；唯一 xfail 为既有预期失败。本轮未增加 warning、skip 或 xfail。

## 最终控制规则

- 开赛前：房主可以开始或关闭房间；其他参赛者不能控制比赛。
- 开赛准备中：房主可以暂停、恢复或终止；恢复必须回到准备流程。
- 正常运行中：房主可以暂停、恢复、跳过异常步骤或终止。
- 真人正在发言：普通暂停和跳过被拒绝；需要使用紧急暂停保护剩余时间和已识别文字。
- 服务异常暂停：使用重试，不能使用普通恢复绕过失败步骤。
- 真人断线暂停：所有真人恢复后使用继续比赛；不得使用 Agent 接管。
- 终止后：Room、Match 和发言状态均不可恢复，迟到写入被拒绝。
