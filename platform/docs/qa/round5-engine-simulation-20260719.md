# Round 5 多真人、多 Agent、多房间状态机模拟报告

日期：2026-07-19

## 结论

本轮在隔离测试数据库中完成了 1v1 人人、1v1 人机、4v4 多真人与 AI 补位三种比赛形态的并行闭环模拟。三场比赛均从真实建房、认领席位、准备、开始进入比赛状态，经过真人发言或确定性 fake Agent 发言，最终产生独立裁判结果；没有发生跨房间状态、发言、裁判结果或事件串写。

发现并修复一个真实状态机缺口：Agent 返回 `……!!!` 一类“非空但不可发言”的坏输出时，旧逻辑会继续调用 TTS，并最终把问题误报为语音服务故障。现在非实时分支会在调用 TTS 前校验最终文本；实时分支会在 `generate_stream` 与既有 pipeline 之间缓存尚未确认的开头，只有累计正文达到最小有效标准后才开始释放。完全没有有效正文时，session 的 `push_text` 和 `finish` 均不会被调用。失败会安全暂停比赛，并记录：

- `provider=agent`
- `code=agent_invalid_output`
- `retryable=true`

房主随后可以使用“重试当前步骤”恢复比赛。验证中同一个幂等键重复重试只执行一次，修复后的 Agent 输出可以继续进入裁判并完成比赛。

## 安全边界

本轮严格未调用或修改以下可靠生产链路：

- 真实 TTS、MOSS-Realtime 与 LightTTS 服务
- LiveKit
- FunASR 服务
- AudioWorklet、PCM 播放器和浏览器播放队列
- `debate-stage.tsx`

所有 AI 文本、语音 URL 和裁判结果均由测试内确定性 fake 提供。fake TTS 只返回不存在的测试 URL，未生成、读取或播放音频。

## 新增完整场景

### 1v1 人人训练赛

- 两个独立登录用户创建和加入同一房间。
- 双方准备并由房主开始。
- 两个真人分别获得自己的设备控制租约。
- 空 ASR/空文字提交返回 422，当前发言保持可恢复状态。
- 补充有效文字后正常完成发言。
- 同一个完成请求重复发送时返回幂等重放，不重复写入发言或事件。
- 双方完成后进入裁判并生成结果。

### 1v1 人机训练赛

- 真人占正方席位，反方空席在开始时自动补为 AI。
- 真人发言结束后状态机自动调用 fake Agent。
- fake Agent 内容包含本房间辩题，用于验证历史与请求上下文隔离。
- AI 阶段完成后独立进入裁判并生成结果。

### 4v4 多真人与 AI 补位正式赛

- 四名真人分别认领 `aff_1`、`aff_2`、`neg_1`、`neg_2`。
- 其余四个席位在开始时自动由 AI 填满。
- 流程交替覆盖真人固定席位和 AI 固定席位。
- 两个 AI 发言分别绑定本房间辩题，未混入另外两场比赛内容。
- 最终结果、发言数量和裁判理由均与本房间对应。

### 多房间交错执行

三场比赛不是顺序跑完，而是在真人阶段、AI 阶段和裁判阶段交错推进。额外将人人赛的 `speech_id` 提交到人机赛完成接口，服务端返回 409，证明房间、比赛和席位三层校验有效。

## 故障和人工恢复覆盖

| 场景 | 权威行为 | 证据 |
| --- | --- | --- |
| Agent 非法输出 | TTS 前拦截，比赛暂停，标记 Agent 可重试错误 | 新增 Round 5 测试 |
| Agent 超时后房间终止 | 迟到结果不能复活已终止比赛 | `test_agent_failure_cannot_resurrect_terminated_room` |
| Agent 结果晚于跳过操作 | 迟到结果不能覆盖新阶段 | `test_late_agent_result_cannot_resurrect_skipped_stage` |
| ASR/文字为空 | 返回 422，发言仍保持进行中，允许人工补录 | 新增 Round 5 完整人人赛 |
| 真人断线超过 60 秒 | 中断遗留真人发言并由 AI 接替 | `test_presence_expiry_interrupts_abandoned_human_speech_and_allows_ai_takeover` |
| 真人返回申请恢复 | 房主审批后恢复席位并实时同步 | `test_participant_restore_request_owner_approval_and_realtime_sync` |
| 暂停进行中的 AI 任务 | 旧生成失效，继续后不会复活 | `test_pause_resume_invalidates_inflight_ai_generation` |
| 跳过当前阶段 | 活跃发言被明确中断 | `test_skip_closes_active_speech` |
| 重复控制请求 | 同一幂等键只写一次事件 | `test_control_operation_is_idempotent` |
| 重复大厅操作和终止 | 准备/开始/取消具有权威结果 | `test_lobby_actions_are_idempotent_and_owner_can_cancel` |
| 引擎重启 | 仅恢复引擎拥有的未完成任务 | `test_engine_restart_interrupts_only_engine_owned_inflight_tasks` |
| 单房间连续异常 | 只隔离和暂停故障房间 | `test_scheduler_quarantines_only_the_repeatedly_failing_room` |
| 20 个 Agent 房间并行 | 全部完成且无状态泄漏 | `test_twenty_rooms_complete_concurrently_without_state_leakage` |

## 实现改动

### `apps/api/app/services/match_engine.py`

- 非实时 Agent 内容在进入 TTS 前执行 `usable_transcript(..., require_substantive=True)`。
- 实时 Agent 增加正文事件门：忽略结构化 `analysis`、`thinking`、`reasoning` 事件及对应 channel，缓存初始 delta，累计到两个有效可见字符后立即向既有 pipeline 释放，不等待完整回复。
- 实时流最终仍没有有效正文时抛出 `agent_invalid_output`；验证中 fake session 的 `push_text=0`、`finish=0`。
- 非字符串、空白、纯标点、控制字符、过度符号和明显重复内容均作为 Agent 错误处理；已经确认并释放有效正文后若最终文本发生不安全修改，仍由既有 pipeline 的前缀一致性保护处理。
- `provider.failed` 事件补充机器可读的 `code` 和 `retryable`，控制台可以给出正确的修复建议。

### `apps/api/tests/test_round5_engine_scenarios.py`

- 新增三种比赛形态同时闭环测试。
- 新增跨房间 `speech_id` 拒绝、空文本恢复及完成请求幂等验证。
- 新增 Agent 坏输出在 TTS 前拦截、重复重试幂等及恢复后完成结果验证。
- 新增生产 realtime-enabled fake session：验证纯标点与思考事件零 push/finish，并验证有效正文在 final 到达前低延迟释放。

## 测试结果

定向 Round 5 场景：

```text
3 passed
```

Round 5 故障与恢复矩阵：

```text
14 passed
```

完整后端测试：

```text
371 passed, 2 warnings in 72.02s
```

静态检查：

```text
ruff: All checks passed
```

两个 warning 均为既有依赖弃用提示：Starlette `TestClient` 的 httpx 兼容提示，以及 Python 3.13 将移除 `audioop`；不是本轮状态机回归。

## 后续建议

- 将 `agent_invalid_output` 映射到控制台明确文案：“Agent 未返回有效发言，可重试当前步骤”，避免用户误以为 TTS 故障。
- 保留真实外部 Agent 的协议与压力灰度，但不要与确定性状态机回归混跑；外部网络不稳定不应降低核心流程测试的可重复性。
- 后续真实语音验收继续使用已经冻结的可靠音频基线，本报告不构成对音质、首音延迟或播放连续性的重新评估。
