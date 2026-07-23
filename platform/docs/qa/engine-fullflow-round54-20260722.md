# Round54 双真人完整比赛与引擎长流程审计

日期：2026-07-22  
范围：本地后端、完整比赛 verifier；未部署、未写入生产数据。

## 结论

双真人 `1v1` 比赛已通过一条完整的服务端权威流程：开场、双方立论、自由辩论、双方总结、裁判、比赛完成。流程中同时验证了一次人工暂停/恢复和一次 30 秒短断线返回。

本轮未发现 AI 自动接管真人席位。60 秒内返回不会暂停比赛；超过 60 秒的行为继续由既有 Round45/46 回归保证为“安全暂停比赛、保留真人席位、必须由房主在所有真人返回后手动恢复”。

## 本轮补强

### 双真人完整比赛集成测试

在 `apps/api/tests/test_full_match_api_simulation.py` 的权威 API 流程中增加：

- 开场阶段执行一次 `control/pause` 和 `control/resume`，恢复后继续原阶段。
- 反方真人模拟断线 30 秒后返回；比赛保持 `running`，席位仍为 `human`。
- 正方立论完成后提交一条迟到 ASR final；服务端返回 `False`，已完成发言文字不被覆盖。
- 比赛完成后校验事件 `seq` 严格连续。
- 明确禁止以下事件出现在成功结果中：
  - `provider.failed`
  - `engine.quarantined`
  - `participant.disconnect_timeout`
  - `seat.ai_substituted`
- 校验本场全部 `Speech.speaker_type` 均为 `human`。

### 完整比赛 verifier

`scripts/verify_complete_match.py` 的 `1v1-two-human` 场景增加严格验收：

- 若出现任何 AI 发言，立即判定失败。
- 请求暂停/恢复演练时，结果必须包含 `control.pause`、`control.resume`。
- 请求短断线重连演练时，必须真实完成 WebSocket 断开和恢复，并包含 `presence.disconnected`、`presence.connected`。
- 重连等待不再自动调用恢复接口，避免 verifier 掩盖意外暂停。
- 记录实际断线时长；达到或超过 60 秒直接失败。
- 短断线成功场景中若出现 `participant.disconnect_timeout`，立即失败。
- 重连后必须仍为 `running`，且不得存在 `failure_reason`。

同时修复 verifier 单元测试构造参数落后于脚本接口的问题，并新增 AI 发言、断线超时、未实际执行控制演练三类失败用例。

## 验证结果

```text
双真人完整比赛 + Round46 断线/ASR 边界：6 passed
Round43-46 引擎、故障注入、无 AI 接管、完整赛程回归：30 passed
完整比赛 verifier 单元测试：15 passed
Ruff（本轮 verifier 与集成测试）：passed
verifier CLI --help：passed
```

执行命令：

```bash
cd platform/apps/api
../../.venv/bin/pytest -q \
  tests/test_full_match_api_simulation.py \
  tests/test_round43_multi_match_flow_audit.py \
  tests/test_round44_fault_injection_match_flow.py \
  tests/test_round44_stale_state_consistency.py \
  tests/test_round45_no_ai_takeover_policy.py \
  tests/test_round46_disconnect_flow_boundaries.py

cd platform
.venv/bin/pytest -q scripts/tests/test_verify_complete_match.py
.venv/bin/ruff check \
  scripts/verify_complete_match.py \
  scripts/tests/test_verify_complete_match.py \
  apps/api/tests/test_full_match_api_simulation.py
```

## ASR 迟到 final 契约

本轮在完整比赛内验证：发言已经通过 `/speech/finish` 完成后，竞态到达的 ASR final 不得再写入权威发言文字。既有 Round46 还覆盖了“断线导致发言被中断后”的同一拒绝契约。因此完成和中断两个终态均不会被迟到识别结果污染。

## 未执行生产完整赛

本轮没有在生产环境创建测试比赛。原因是本地自动化已经覆盖目标流程，而生产执行还需要先确认活动房间容量，并确保所有测试账户、比赛和结果能被正确分类和清理。为避免占用真实用户最多 5 个房间/全站最多 5 个观众的容量，本轮保持只读、不部署。

## 后续建议

- 在发布候选环境运行一次 `verify_complete_match.py --scenario 1v1-two-human --exercise-controls --exercise-reconnect-once`，保存 JSON 报告。
- 生产灰度前确认测试数据分类开关和活动房间容量，再运行同一 verifier；不要让 verifier 自动修复暂停状态。
- 浏览器端真实麦克风、ASR WebSocket 和 60 秒实际等待仍应由端到端浏览器测试补充，本轮重点是服务端状态机与持久化契约。
