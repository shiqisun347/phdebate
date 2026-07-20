# Round 18 多真人、多 Agent 与多房间状态机审计

日期：2026-07-20  
范围：`platform/apps/api` 权威房间状态、故障恢复、房间隔离、实时身份与观战准入  
明确排除：冻结的 TTS、实时语音、浏览器音频播放实现；本轮未调用或修改这些文件

## 结论

本轮没有发现新的权威状态机缺陷。新增的三组集成场景全部通过，并与 Round 6–16 已有的故障恢复、长时并发测试一起构成以下闭环：

- 4v4 四真人加四 Agent、1v1 人人、1v1 人机可以同时创建、开始和运行，题目、席位、比赛记录与状态互不串房。
- 同席位并发抢占只有一个请求成功；重复开始只生成一个 `Match` 和一个 `room.locked` 事件。
- 单场暂停和恢复不会改变其他房间；跨房控制请求返回 403。
- 真人断线 59 秒仍保留真人席位；超过 60 秒后由 AI 接替。
- 被替补的真人可以通过真实房间 WebSocket 重新建立在线状态，再由房主审批恢复；未重新连接时审批会返回 409，并给出可操作原因。
- Agent 暂时失败可在本房间重试，其他房间继续推进；Judge 失败或非法结果只使本房进入人工复核，迟到的旧 Judge 任务不能覆盖新任务。
- 每个房间最多接纳 20 个观众；第 21 个观察者返回 WebSocket `4429`。房主、辩手和系统管理员不占观战名额，另一房间仍有独立的 20 个名额。
- Redis 原子观战租约、租约过期释放、跨 Worker presence、断线恢复和 WebSocket 用户投影隔离均有自动测试覆盖。

## 本轮新增测试

文件：`apps/api/tests/test_round18_engine_scenarios.py`

### 三种比赛形态并行

`test_4v4_human_ai_1v1_human_human_and_1v1_human_ai_run_in_parallel`

- 同时创建 4v4 日常赛、1v1 人人训练和 1v1 人机训练。
- 两名学生并发抢占同一个 `neg_1`，验证结果严格为一个 200、一个 409。
- 三个房间并发开始；重复点击开始返回 `replayed=true`。
- 4v4 最终为 4 个真人席位和 4 个 Agent 席位；1v1 人人为 2 个真人；1v1 人机为 1 个真人和 1 个 Agent。
- 每个房间只有一个 `Match`、一个 `room.locked`，三个题目保持独立。
- 暂停、恢复其中一个房间后，另外两个房间仍保持运行；跨房控制被拒绝。

### 60 秒断线保护和真实回连恢复

`test_disconnect_grace_ai_substitution_and_real_websocket_restore`

- 59 秒离线不触发替补。
- 61 秒离线触发且只触发一次 `seat.ai_substituted`。
- 真人可以先提交恢复申请；未重新连接时房主审批返回 409。
- 真人打开真实 `/ws/rooms/:code` 后，服务端权威 `connected=true`；房主随后审批成功。
- 关闭 WebSocket 后席位保持真人身份，同时在线状态正确回落为离线；恢复事件只写入一次。

### 20 人观战上限与角色豁免

`test_twenty_spectators_are_room_scoped_and_all_repair_roles_remain_exempt`

- 在一个运行中房间真实保持 20 条匿名观战 WebSocket。
- 房主、另一名真人辩手和系统管理员仍可进入，且不消耗观战名额。
- 已满房间的普通登录观察者收到 `4429`，不会获得房间快照。
- 同时验证另一个房间的匿名观战不受影响。
- 20 条连接释放后，原本被拒绝的观察者可正常进入。

## 既有证据复核

| 要求 | 权威自动化证据 |
| --- | --- |
| 4v4 并发入场和 AI 自动补位 | `test_round10_engine_scenarios.py::test_four_humans_can_claim_and_ready_a_4v4_room_concurrently_once`；Round18 三形态并行测试 |
| 四房抢座、并发开始、开始幂等 | `test_round14_engine_scenarios.py::test_four_rooms_race_for_seats_and_start_idempotently_without_cross_room_state` |
| 20 房、100+ 发言、1v1/4v4 混合长时运行 | `test_round15_engine_soak.py::test_twenty_room_mixed_format_long_soak_preserves_authority_and_recovers` |
| Agent 失败、重试和跨房隔离 | `test_round6_engine_recovery.py::test_four_rooms_recover_agent_and_judge_failures_without_cross_room_leakage`；`test_round8_engine_scenarios.py::test_parallel_agent_failure_recovery_is_room_scoped_and_settles_once` |
| 完整真人/Agent 生命周期 | `test_round12_lifecycle.py::test_round12_complete_mixed_human_agent_lifecycle_is_recoverable_and_room_isolated` |
| Judge 失败转人工复核 | Round6 四房恢复测试；`test_round16_engine_resilience.py::test_four_concurrent_rooms_isolate_malformed_judge_disconnect_and_manual_recovery` |
| Judge 暂停、重试、旧结果乱序返回 | `test_round14_engine_scenarios.py::test_paused_judge_retry_rejects_out_of_order_old_task_result` |
| Engine 重启清理孤儿任务 | `test_round14_engine_scenarios.py::test_engine_restart_invalidates_judge_task_and_preserves_event_sequence`；Round15 长时测试 |
| 断线 AI 接替和控制权转移 | `test_round9_engine_scenarios.py::test_running_owner_substitution_transfers_control_and_keeps_other_room_isolated` |
| 真人返回与席位恢复 | Round18 真实 WebSocket 恢复测试；`test_round9_engine_scenarios.py::test_restore_approval_requires_live_reconnection_before_reclaiming_ai_seat` |
| 跨房发言标识隔离、迟到文本保护 | `test_round14_engine_scenarios.py::test_cross_room_speech_ids_and_paused_late_finish_preserve_authoritative_data` |
| WebSocket 用户身份投影隔离 | `test_round16_engine_resilience.py::test_two_user_websockets_keep_identity_projection_isolated` |
| 跨 Worker presence 与租约回收 | `test_realtime_hub.py::test_presence_lease_is_atomic_across_room_hub_instances`；`test_presence_lease_refresh_and_expiry_are_globally_claimed_once` |
| 20 观众原子准入、释放和过期恢复 | Round18 真实 WebSocket 测试；`test_realtime_hub.py::test_spectator_limit_is_atomic_across_workers_and_releases_slots`；`test_spectator_lease_expiry_recovers_capacity` |

## 验证结果

### Round18 新增场景

```text
3 passed, 1 warning in 1.92s
```

### 状态机、恢复、Realtime 与多房组合回归

覆盖 Round 6、8、9、10、11、12、13、14、15、16、18、`test_multi_room_simulation.py` 和 `test_realtime_hub.py`：

```text
82 passed, 1 xfailed, 1 warning in 31.63s
```

### API 全量回归

从 `platform/` 执行：

```bash
PYTHONPATH=apps/api .venv/bin/python -m pytest -q apps/api/tests
```

结果：

```text
443 passed, 1 xfailed, 2 warnings in 97.05s
```

新增文件 Ruff 检查：

```text
All checks passed!
```

## 已知但未在本轮改动的事项

1. 唯一 xfail 是 `test_round11_engine_scenarios.py::test_frozen_realtime_pipeline_retrieves_agent_error_when_cancel_races_completed_anext`。它复现冻结实时语音管线在极窄取消竞态下的历史异步异常回收风险。本轮遵守“当前 TTS 和浏览器播放效果可靠，不再修改”的冻结要求，没有触碰该实现。
2. `StarletteDeprecationWarning` 提示未来应从当前 TestClient/httpx 兼容层迁移到 `httpx2`；当前不影响业务结果。
3. `audioop` 将在 Python 3.13 移除；警告来自 FunASR 基准脚本，不在本轮非音频状态机范围内。
4. 本轮 Agent、Judge、LightTTS 均使用确定性的进程内测试替身，验证的是平台状态机、失败边界和数据隔离；外部服务的真实网络延迟与内容质量应继续由生产灰度验证器承担。

## 文件变更

- 新增：`apps/api/tests/test_round18_engine_scenarios.py`
- 新增：`docs/qa/round18-engine-scenarios-20260720.md`
- 未修改任何冻结 TTS、实时语音或浏览器播放文件。
