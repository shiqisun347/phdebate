# Round 64：2 真人 + 6 Agent 完整 4v4 比赛与故障恢复专项

日期：2026-07-24  
范围：API、match-engine、预设主持提示、固定发言、自由辩论、Agent 重试、真人断线、裁判、文字记录和跨房间隔离。  
部署状态：仅本地实现与验证，未修改 Web，未部署生产环境。

## 结论

新增了一条从开场到裁判完成的完整 4v4 权威状态机测试：房间内恰好 2 名真人和 6 个固定 Agent，经历全部正式阶段、4 轮交替自由辩论、一次 Agent 超时重试和一次真人断线 61 秒暂停/恢复，最终正常完成并形成完整文字记录和赛果。

测试同时运行第二个不同辩题的 Agent 房间作为隔离哨兵。主房间发生 Agent 故障、暂停、重试、断线和多轮发言时，第二房间的状态、事件序号、文字内容和裁判结果均保持不变。

本轮发现并修复一个会污染正式赛程的状态机问题：自由辩论总预算已归零时，无人中选的三秒申请窗口仍可能启动一个额外 Agent 回合。修复后，0 秒边界不再选择真人、不再启动 Agent、不再生成额外发言，下一次引擎处理直接结束自由辩论。

完整 API 回归结果：

```text
594 passed, 13 skipped, 1 xfailed, 2 warnings in 96.58s
```

## 新增测试

测试文件：`platform/apps/api/tests/test_round64_complete_mixed_match.py`

### 比赛阵容

- 真人：`aff_1`、`neg_2`。
- 固定 Agent：`aff_2`、`aff_3`、`aff_4`、`neg_1`、`neg_3`、`neg_4`。
- 开赛后席位总数固定为 8，测试明确验证不存在 `ai_substitute` 或 `seat.ai_substituted`。
- 第二房间使用不同辩题和独立 Agent 发言、裁判结果，作为全程跨房间污染检测哨兵。

### 完整流程覆盖

- 所有主持提示均使用已生成且可读取的 WAV 预设资源。
- 开场与规则提示。
- 正方一辩真人立论。
- 反方一辩 Agent 立论。
- 正反双方二辩驳论。
- 正反双方三辩质询。
- 自由辩论连续 4 个真人回合，覆盖申请、选中、换方和最终时间耗尽。
- 反方四辩 Agent 总结。
- 正方四辩 Agent 总结。
- AI 裁判生成胜方、队伍分和理由。
- 房间和 Match 均进入 `completed`，JudgeScorecard 为 `approved`。

最终主房间包含 12 条有效完成发言：

- 6 条固定 Agent 发言；
- 2 条固定真人发言；
- 4 条自由辩论真人发言。

另保留 1 条 `failed_retried` Agent 尝试作为不可覆盖的故障审计记录，但该失败内容不进入有效发言和裁判文字记录。

### Agent 超时与重试

- 只让主房间“反方一辩立论”的第一次 Agent 调用抛出 `agent_timeout`。
- 主房间进入安全暂停并产生且仅产生 1 个 `provider.failed` 事件。
- 房主执行 `control/retry` 后，同一阶段重新生成并成功完成。
- 指定 Agent 阶段调用次数严格为 2；第二房间对应 Agent 阶段严格为 1。
- 第二房间无 `provider.failed`，结果和事件序号不变化。

### 真人断线 60 秒策略

- 在 `neg_2` 真人固定发言阶段将连接时间推进到断线 61 秒。
- 主房间自动暂停，产生且仅产生 1 个 `participant.disconnect_timeout`。
- 真人席位、用户 ID 和原房主 `owner_id` 全部保留。
- 未创建任何 AI 接管席位或接管事件。
- 真人恢复连接后，由原房主继续比赛，仍从原阶段完成发言。

### 文字记录和赛果完整性

- 每条有效 Speech 都有非空、可审计的文字内容。
- 六个固定 Agent 席位都至少形成一条有效发言。
- 裁判输入只包含当前房间文字记录。
- 不存在 `speaking`、`synthesizing` 或 `playing` 残留发言。
- MatchEvent 序号从 1 到房间最终 `seq` 连续，无缺口和重复。
- 第二房间内容不含主房间号，主房间内容不含第二房间号。
- 两个不同辩题分别得到独立 JudgeScorecard。

## 本轮发现并修复的问题

### P0：自由辩论 0 秒后仍会多生成一个 Agent 回合

根因位于 `resolve_intermission`：

- 自由辩论最后一轮结束后，三秒申请窗口仍需被解析；
- 此时 `free_stage_remaining_seconds` 已经是 0；
- 若没有真人申请中选，旧逻辑仍设置 `force_ai_fallback` 和 `ai_preparing`；
- `preparing_stage_remaining_seconds` 又被最小值保护重新抬到 1 秒；
- 下一次引擎处理因此生成一条额外 Agent 发言，导致赛程、文字记录和裁判输入都多一轮。

修复位于 `platform/apps/api/app/services/free_turn_queue.py`：

- 在解析申请前先确定权威剩余总时长。
- 剩余时间为 0 时，所有待处理申请以 `stage_time_elapsed` 结束，不再选中真人。
- 清除 `force_ai_fallback`、`ai_preparing` 和所有准备时钟。
- `free.intermission_resolved` 增加 `stage_exhausted=true`，并明确 `fallback=false`。
- 下一次引擎处理只结束自由辩论，不启动任何真人或 Agent 发言。

## 验证结果

新增完整 4v4 专项：

```text
uv run pytest -q tests/test_round64_complete_mixed_match.py -x
1 passed, 1 warning in 0.86s
```

自由辩论、完整赛程、无 AI 接管和多房间组合回归：

```text
uv run pytest -q \
  tests/test_round20_free_debate_queue.py \
  tests/test_round45_no_ai_takeover_policy.py \
  tests/test_full_match_api_simulation.py \
  tests/test_round55_engine_multiroom.py \
  tests/test_round62_full_4v4_state_machine.py \
  tests/test_round63_five_room_stress.py \
  tests/test_round64_complete_mixed_match.py

34 passed, 1 warning in 6.64s
```

代码检查：

```text
uv run ruff check app/services/free_turn_queue.py tests/test_round64_complete_mixed_match.py
All checks passed!
```

最终完整 API 回归：

```text
uv run pytest -q
594 passed, 13 skipped, 1 xfailed, 2 warnings in 96.58s
```

跳过项为当前测试环境中需要可选外部运行条件的用例；唯一 xfail 为既有预期失败。本轮未新增 warning、skip 或 xfail。

## 发布前建议

- 生产冒烟至少跑一场 2 真人 + 6 Agent 4v4，特别观察自由辩论最后一轮是否直接进入总结。
- 核对最终有效 Speech 数量应等于实际赛程发言数，失败重试记录只能保留为终态审计行，不能进入裁判输入。
- 模拟任意非房主真人断线超过 60 秒，确认比赛暂停、席位不变、无 AI 接管。
- 同时保留第二个运行房间，确认主房间暂停和重试不会改变其 `seq`、阶段或文字记录。
