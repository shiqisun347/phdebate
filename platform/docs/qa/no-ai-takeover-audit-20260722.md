# 禁止 AI 自动接管真人席位：策略审计

日期：2026-07-22  
范围：API 引擎、房间控制接口、Web 文案与测试；历史数据库记录只按兼容显示处理，不把历史 `ai_substitute` 当作新策略。

## 结论

当前代码已经覆盖主要新语义：真人断线达到 60 秒后保留 `human` 身份、暂停整场、保留可恢复的比赛阶段和已识别文字，不再写入新的 `seat.ai_substituted` 事件。主动“放弃本场并由 AI 接替”接口也已返回 410。

首轮验收发现两个阻断问题；并行修复后已完成针对性复测：

1. **P0（已修复）：多个真人同时超时断线时，纯断线暂停可能被误标为服务异常。** 第二个 `participant.disconnect_timeout` 事件看到房间已经有断线暂停产生的 `failure_reason`，将 `prior_requires_retry` 置为 true。现在已改为识别“真人断线暂停”专属原因，全部真人返回后可直接继续。
2. **P0（已修复）：历史 `ai_substitute` 席位可能触发新的 Agent 发言。** 固定阶段和自由辩论候选现在只允许永久 `ai`；历史席位只保留展示/人工恢复兼容，不再执行新的 Agent/TTS。

## 验收测试

新增 [test_round45_no_ai_takeover_policy.py](../../apps/api/tests/test_round45_no_ai_takeover_policy.py)，覆盖：

- 59.5 秒仍运行，60 秒后整场暂停；真人席位、用户归属和阶段索引不变。
- 当前真人发言被安全中断并保留已确认文字；没有 Agent 调用、AI 发言或 `seat.ai_substituted` 事件。
- 混合真人/永久 AI 房间中，真人超时后必须先暂停，永久 AI 不得继续推动比赛。
- 多真人同时断线、部分返回、全部返回后的继续比赛权限与状态。
- 管理员停用账号的安全边界：立即暂停、不转移房主、不替换席位（这是安全操作例外，不是普通断线宽限）。
- 主动接管接口返回 410，不能释放真人参赛占用。
- 历史 `ai_substitute` 只读兼容不能触发新的 Agent 发言。

同时更新了旧 `test_platform.py`、`test_round44_fault_injection_match_flow.py` 中仍断言“60 秒转为 AI”的验收，改为“60 秒暂停并保持 human”。

首轮针对性回归（修复前）：

```text
12 passed, 2 failed
```

两个失败均已由并行修复覆盖：

```text
test_multiple_disconnected_humans_can_resume_only_after_everyone_returns
  -> 已修复：断线暂停原因不再被第二个超时事件污染

test_legacy_ai_substitute_is_never_used_to_generate_a_new_speech
  -> 已修复：旧接替席位从固定/自由 AI 执行候选中移除
```

修复后复测：

```text
test_round45_no_ai_takeover_policy.py: 8 passed
针对性旧断言回归: 14 passed
断线/生命周期/自由辩论跨轮次回归: 53 passed
```

## 运行时路径审计

### 已符合新规则

- `app/services/match_engine.py::_expire_presence`：普通真人 `human` 断线达到 60 秒调用 `_pause_for_participant_disconnect`，不改变 `occupant_type`。
- `_pause_for_participant_disconnect`：保存剩余时间、清理活跃语音、保留真人已确认文字、写入 `participant.disconnect_timeout` 和 `match.paused`。
- `app/api/rooms.py::abandon_started_seat`：统一返回 410，明确取消主动 AI 接管。
- `app/api/rooms.py::start_room`：开赛时从比赛开始时间重新计算真人断线宽限，避免大厅阶段的旧 `disconnected_at` 立即触发暂停。
- 控制接口在 `resume` / `retry` 前重新校验仍有超时离线真人；公开观战投影不暴露文字稿。

### 必须修复或明确产品决策

| 风险 | 位置 | 影响 | 建议 |
|---|---|---|---|
| 历史接替席位执行化 | `match_engine.py:1328` | 首轮发现旧 `ai_substitute` 会走固定阶段 `_ai_speech` | 已修复：仅允许 `occupant_type == "ai"` 执行；旧席位只读/人工复核 |
| 自由辩论候选含旧接替席位 | `match_engine.py:2921`、`room_service.py:310` | 首轮发现旧席位可能被选为自由辩论 AI | 已修复：候选集合只允许永久 AI |
| 多断线事件优先级 | `match_engine.py:3547`、控制接口 `pause_boundary` | 首轮发现第二个超时污染 `failure_reason`，恢复死路 | 已修复：断线专属原因不会被后续纯断线覆盖；真实 provider failure 才要求 retry |
| 停用账号 | `_expire_presence:3643-3696` | 账号停用立即暂停是安全行为，但已停用房主的比赛只能管理员处理 | 在控制台明确“账号停用立即安全暂停”；保留管理员终止/恢复入口，不能转 AI |
| 旧恢复接口 | `api/admin.py:2171`、`services/seat_restore.py` | 旧记录仍有恢复申请和管理员恢复按钮 | 保留历史只读/人工恢复兼容，但禁止新比赛生成 `ai_substitute`；文案标记“历史兼容” |

## 1v1 / 4v4 / 自由辩论边界

- **1v1 人机**：空席产生的永久 `ai` 是合法 AI 对手；真人席位断线 60 秒后必须暂停，不允许永久 AI 趁机继续发言。
- **1v1 双真人**：任一真人超时即暂停整场，另一真人不能让比赛自动越过该轮；双方返回后由房主继续。
- **4v4 混合**：一名真人断线即暂停全房间，不得只暂停该席位，也不得把房主权限或真人身份转给 AI。真人与永久 AI 的席位必须保持可区分。
- **4v4 全真人**：无人举手的自由辩论只能结束当前申请窗口或交换轮次；不能因为“无人申请”而创建 AI 接替发言。
- **含永久 AI 的自由辩论**：仅当该方本来就有永久 AI 席位、且没有真人断线超时暂停时，AI 才能按模板发言；任何真人超时事件优先冻结房间。
- **账号停用**：停用是安全事件，建议立即安全暂停（不等待 60 秒）；不释放真人席位、不自动转移到 AI。已停用房主造成的房间只能由管理员恢复或终止，这是预期的权限边界，需要在 UI 说明。

## 文案与文档清理清单

下列内容仍描述旧策略，不能作为新产品行为说明：

- `docs/seat-restoration.md`：正文仍写“断线超过保护时间后切换为 `ai_substitute`”。应标记为历史数据库兼容说明或移出官方文档入口。
- `docs/deploy/full-server-restore.md`：验收清单仍写“AI 接替”，改为“真人断线暂停、管理员处理历史接替记录”。
- `docs/CODEX_COMPUTER_USE_FULL_TEST_PROMPT.md` 及早期 QA 报告：很多段落描述“AI 接替”是历史测试记录，必须加醒目标记，避免被 Coding Agent 当成当前需求。
- Web `/me`、控制台和 `seat-restore-panel` 中的“AI 已接替”仅可用于旧数据；新比赛不得出现该入口。当前控制台已有“历史只读状态”提示，但建议将恢复按钮和旧席位单独置于“历史兼容”折叠区。
- 赛事大厅、官方 user.html 已写明“60 秒后自动暂停，不会由 AI 接替”，应保持为唯一当前产品规则。

## 后续验收门槛

发布前必须继续执行：

1. `test_round45_no_ai_takeover_policy.py` 全部通过。
2. API 全量回归，筛选所有含 `ai_substitute` 的旧测试，逐个标记为历史兼容或改为新规则。
3. 1v1 双真人、4v4 混合、4v4 全真人自由辩论各做一次真实 WebSocket 断线 61 秒测试；检查房间状态、席位身份、事件日志和 Agent 调用计数。
4. 账号停用房主场景必须有管理员可见的安全暂停、恢复/终止路径，不能出现无限占用且无可操作人的房间。
5. 部署前搜索运行时代码，禁止新增 `occupant_type = "ai_substitute"` 或等价数据库写入；历史迁移和只读投影可保留。
