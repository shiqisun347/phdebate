# Round 47：完整比赛 API 流程模拟

日期：2026-07-22

## 目标

在不连接外部 Agent、TTS 或 MOSS 网关的情况下，使用 FastAPI 权威接口和真实比赛引擎，验证以下流程：

- 1v1 双真人：大厅、准备、开始、固定轮次、自由辩论、总结、裁判和结果。
- 1v1 人机：真人席位与开局自动填充的 AI 席位可以顺序推进到裁判结果。
- 4v4：至少两个真人席位，剩余席位由开局时自动补成 AI；真人发言和 AI 发言不会跨房间串线。
- 同时处理两个房间时，Agent 调用、Speech、MatchEvent 和最终状态只归属于发起它们的房间。
- 自由辩论三秒申请窗口无人申请时，纯真人比赛仍然允许下一方真人发言。

## 发现并修复的问题

`resolve_intermission()` 在三秒申请窗口没有有效申请时，无论目标阵营是否存在 AI，都写入 `force_ai_fallback=true`。纯真人比赛随后被 `speaking_permission()` 判定为“系统正在安排 AI 发言”，下一方真人的发言按钮被禁用，必须等完整回合计时结束，造成自由辩论看似卡死。

现在仅当目标阵营存在永久 AI 席位时才写入 `force_ai_fallback`。纯真人比赛会清除该标志，并从申请窗口结束时开始一个普通真人回合；真人可以立即发言，超时后再按正常规则进入下一轮。

这不改变“AI 自动补齐开局空席”的规则，也不允许 AI 接管已经认领的真人席位。

## 验证命令

```bash
cd /Users/sunshiqi/code/phdebate/platform/apps/api
../../.venv/bin/pytest -q \
  tests/test_full_match_api_simulation.py \
  tests/test_round43_multi_match_flow_audit.py \
  tests/test_round44_fault_injection_match_flow.py \
  tests/test_round44_stale_state_consistency.py \
  tests/test_round45_no_ai_takeover_policy.py \
  tests/test_multi_room_simulation.py \
  tests/test_round33_control_boundaries.py
```

结果：`58 passed, 1 warning`（55.21 秒）。

完整 API 回归：

```bash
../../.venv/bin/pytest -q
```

结果：`550 passed, 1 xfailed, 2 warnings`（267.27 秒）。其中 `xfailed` 为测试套件原有的明确预期失败，不是本轮新增回归。

静态检查：

```bash
../../.venv/bin/ruff check \
  app/services/free_turn_queue.py \
  tests/test_full_match_api_simulation.py \
  tests/test_round44_fault_injection_match_flow.py
```

结果：`All checks passed!`

## 新增回归测试

- `tests/test_full_match_api_simulation.py::test_complete_human_vs_human_flow_reaches_result_without_manual_stage_mutation`
- `tests/test_full_match_api_simulation.py::test_complete_human_vs_ai_and_four_v_four_room_isolation`
- `tests/test_round44_fault_injection_match_flow.py::test_all_human_free_turn_without_requests_keeps_rotating_after_bounded_turn` 已更新为验证纯真人按钮可用。

## 未覆盖项

- 本轮没有修改或连接 MOSS 网关，也没有宣称真实 GPU 音频播放质量通过。
- 浏览器 WebSocket、ASR 双流和 LiveKit 播放仍需由浏览器/生产环境专项验证。
- 测试使用受控的 Agent/TTS/Judge mock，外部服务异常恢复不属于本轮模拟范围。
