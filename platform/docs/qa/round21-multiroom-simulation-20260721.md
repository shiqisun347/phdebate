# Round 21 多人、多 Agent、多房间流程审计

日期：2026-07-21  
范围：API、服务端权威房间状态、比赛恢复入口、观战隐私  
约束：不修改可靠音频基线、TTS、实时音频传输或浏览器播放代码；不部署生产环境。

## 结论

本轮对当前唯一版本进行了完整 API 回归，并把已有的并发与故障场景逐项映射到真实比赛流程。4v4 人机赛、1v1 人人赛、1v1 人机赛、并发抢座、准备与重复开始、AI 补位、自由辩论举手队列、暂停恢复、掉线后的 AI 接替与真人恢复、Agent/裁判失败后的人工修复、跨房间隔离和 20 人观战上限均有自动化证据且通过。

审计发现一项高优先级隐私缺陷：虽然观战页面已经隐藏文字稿入口，但服务端仍可能把当前发言的完整 `active_speech.content`、`speech.completed` 事件正文、终局结果/历史接口正文以及只读协同文字稿令牌交给非参赛观众。本轮已修复并补齐回归测试。实时、短生命周期的逐句字幕仍可见，历史或可回放文字稿不可见。

最终 API 全量结果：`475 passed, 1 xfailed, 2 warnings`，用时 52.85 秒。唯一 xfail 是冻结实时语音管线中已登记的取消竞态风险，本轮根据音频冻结约束没有修改。

## 场景覆盖与证据

| 真实场景 | 权威自动化证据 | 结果 |
| --- | --- | --- |
| 4v4：4 真人 + 4 Agent，同时运行 1v1 人人、1v1 人机 | `test_round18_engine_scenarios.py::test_4v4_human_ai_1v1_human_human_and_1v1_human_ai_run_in_parallel` | 通过；三种比赛独立创建、抢座、准备、并发开始，空席按赛事席位补 AI |
| 三种比赛完整结束并独立裁判 | `test_round5_engine_scenarios.py::test_round5_three_realistic_match_formats_complete_together_without_cross_room_state` | 通过；真人/Agent 发言数、胜负、裁判卡和房间题目不串房 |
| 20 个全 Agent 房间并发完成 | `test_multi_room_simulation.py::test_twenty_rooms_complete_concurrently_without_state_leakage` | 通过；Agent 有界并发，事件序号连续，结果和发言只属于本房间 |
| 24 个混合状态房间同时推进 | `test_multi_room_simulation.py::test_twenty_four_mixed_rooms_advance_only_their_authoritative_state` | 通过；暂停、等待真人、超时、播放、裁判、准备六类状态互不干扰 |
| 随机多人操作与状态机不变量 | `test_multi_room_simulation.py::test_seeded_random_multi_user_operations_preserve_room_invariants`，3 个固定种子、每个种子 320 次操作 | 通过；无重复比赛、无多活跃发言、单用户不能占多个活动席位、终态不可变 |
| 并发抢座 | `test_platform.py::test_concurrent_seat_claim_is_authoritative`、`test_round10_engine_scenarios.py::test_four_humans_can_claim_and_ready_a_4v4_room_concurrently_once`、Round 18 抢同一席位竞态 | 通过；同一席位只有一个成功者，失败返回 409，无 500 |
| 准备、开始和重复点击 | `test_platform.py::test_ready_and_start_race_is_retryable_without_duplicate_match`、`test_round14_engine_scenarios.py::test_four_rooms_race_for_seats_and_start_idempotently_without_cross_room_state` | 通过；开始幂等，每个房间最多一个 Match |
| 自由辩论 30 秒轮次和 3 秒申请窗口 | `test_round20_free_debate_queue.py` 全文件 | 通过；窗口边界、暂停/恢复、终止、重启恢复均受服务端状态约束 |
| 多真人同时举手、FIFO 和唯一发言权 | `test_round20_free_debate_queue.py::test_concurrent_requests_are_ordered_once_and_only_selected_human_can_speak` | 通过；顺序唯一、选中席位可发言、其他席位不可绕过 |
| 无人举手由 AI 接替 | `test_round20_free_debate_queue.py::test_no_request_resolves_to_bounded_ai_fallback_after_restart` | 通过；持久化窗口可在进程重启后继续并转入对侧 AI |
| AI 意愿判断和候选并行、真人举手取消推测任务 | Round 20 free-debate queue 和 agent-decision 测试 | 通过；拒绝发言时丢弃候选，判断失败 fail-open，真人请求优先且旧候选不能复活 |
| 暂停、恢复、跳过、终止 | Round 12 完整生命周期、Round 18 并行流程、`test_platform.py` 控制操作测试 | 通过；操作幂等，跨房操作者 403，旧任务结果不能改变新状态 |
| 真人断线 60 秒、AI 接替、真人回归 | Round 12、Round 18、Round 8/9/13 断线与恢复测试 | 通过；宽限期内保席，超时替换，回归需在线并经房主/管理员批准，旧设备失去发言权 |
| Agent 无效输出、超时或失败 | Round 5、6、8、12、16 故障恢复测试 | 通过；只暂停故障房间，不调用 TTS 处理坏文本；房主可重试、跳过或终止 |
| 裁判异常与人工复核 | Round 6/14/16 裁判竞态和恢复测试 | 通过；异常房间进入暂停或待复核，其他房间继续；迟到结果不覆盖人工决定 |
| 引擎重启与孤儿任务 | Round 12、14、15、16 恢复/浸泡测试 | 通过；孤儿发言被中断并可重新执行，事件序号保持连续 |
| WebSocket 身份与房间隔离 | `test_round16_engine_resilience.py::test_two_user_websockets_keep_identity_projection_isolated`、room hub 隔离测试 | 通过；房主和辩手各自获得独立投影，不复用序列化用户视图 |
| 最多 20 名观众 | Round 18 和 `test_platform.py::test_room_websocket_rejects_twenty_first_spectator` | 通过；第 21 名返回 4429，辩手、房主、管理员不占观众名额，其他房间有独立容量 |
| 所有观众不可见文字稿 | 本轮新增 Round 20 captions/collab 测试，并更新公共投影、终局结果和比赛历史断言 | 通过；当前完整正文、历史正文、终局正文、事件正文和协同令牌均不向匿名或登录观众开放；逐句字幕保留 |

## 本轮修复

### 1. 公共当前发言正文泄露

问题：公共房间投影的历史 `speeches[].content` 已被清空，但 `active_speech.content` 仍返回权威全文。Agent 往往会在音频尚未播放完时先写入完整正文，因此观众可以提前获取整段内容，也违背“观众不可见文字稿”。

修复：公共投影始终把 `active_speech.content` 置空。观众显示继续使用最多 40 段的展示字幕投影，不影响音频流或播放恢复元数据。

### 2. 公共事件携带发言正文

问题：`speech.completed` 等历史事件经过匿名投影时仍保留 `content`，可从首次快照的 `recent_events` 回放完整文字稿。

修复：从匿名事件字段白名单删除 `content`。辩手和管理员使用的授权事件投影仍保留正文；字幕事件使用独立 `text` 字段，不受影响。

### 3. 登录观众可获取协同文字稿令牌

问题：公共比赛允许任意登录用户查看房间，原接口只检查 `can_view_room`，因此非参赛观众能获得 `role=viewer` 的 Hocuspocus 令牌并读取协同文档。

修复：令牌只签发给以下身份：

- 当前房间席位绑定用户；
- 开赛时写入不可变 `MatchParticipant` 的本场真人辩手，包括后续被 AI 接替但仍需修正本人发言的原辩手；
- 房主或其他具备单场控制权的用户；
- 系统管理员。

普通登录观众现在返回 `403 观众不可查看文字稿。`。

### 4. 终局结果和比赛历史泄露完整正文

问题：公开比赛结束后，匿名用户可打开房间结果页，任意登录用户也可调用比赛历史接口；两处接口原样返回所有 `Speech.content`。这使“观战页隐藏文字稿”只成为前端遮挡，无法阻止观众读取或批量下载正文。

修复：`/api/rooms/{code}/result` 和 `/api/matches/{match_id}/history` 都复用服务端 `use_public_projection` 判定。匿名用户和非参赛登录观众获得空 `content`，真人参赛者、房主和系统管理员仍能查看并执行本人发言修正。音频回放、比分、裁判结论和公共时间线不受影响。

## 人类可用的修复措施审计

当前系统不依赖管理员直接改数据库。比赛出现异常时，正常产品路径包括：

1. 房主暂停/恢复，用于网络、设备或现场秩序异常；暂停保持阶段和自由辩论窗口剩余时间。
2. 对失败步骤执行重试；重试有幂等键，旧失败发言被标记为 `failed_retried`，不会与新结果并存为两个有效发言。
3. 跳过无法恢复的 Agent 或阶段；后台迟到结果有代际校验，不能回写到后续阶段。
4. 终止比赛；终止后所有发言、举手和迟到任务都不能复活比赛。
5. 真人断线后自动 AI 接替；真人返回后发起恢复申请，由在线房主/管理员批准，避免旧设备自动夺回麦克风。
6. 房主掉线或退出时把恢复控制权转给合格真人席位；无其他真人时，原房主即使选择 AI 接替本人席位仍保留故障控制能力。
7. Agent/裁判失败时只暂停当前房间，其他比赛继续；错误细节仅管理员可见，学生和观众获得可操作的通用提示。

## 验证命令

```bash
cd /Users/sunshiqi/code/phdebate/platform
.venv/bin/python -m pytest -q apps/api/tests
.venv/bin/python -m ruff check \
  apps/api/app/services/room_service.py \
  apps/api/app/api/rooms.py \
  apps/api/tests/test_round20_collab_token.py \
  apps/api/tests/test_round20_captions.py \
  apps/api/tests/test_platform.py
.venv/bin/python scripts/create_reliable_audio_manifest.py \
  --root . \
  --verify backups/reliable-audio-20260719-declick/reliable-audio-baseline.json
```

结果：

- API：`475 passed, 1 xfailed, 2 warnings in 52.85s`
- Ruff：`All checks passed!`
- 可靠音频基线：`files=82`，`fingerprint=3219138b22878e766c94b9a1fa0e422211a6f27dd74afbbb93a8e45f7e295285`

## 已知限制与下一步

- 唯一 xfail 是冻结实时语音管线的历史取消竞态：Agent 的异步生成刚失败、父任务同拍取消时，已完成的 `anext` 异常可能未被回收。该测试明确属于冻结音频改动窗口，本轮没有越界修改；正常房间隔离、人工跳过、终止以及迟到结果保护测试均已通过。
- 本轮的 Agent、裁判和 TTS 均使用确定性测试替身，证明状态机、故障恢复和数据隔离，不等价于外部网络服务的延迟/限流/断流实测。生产 Agent API 联调应在独立的非音频发布窗口执行，并保留固定题目、任务 ID 和房间 ID 的请求日志作为证据。
- 本报告只覆盖服务端和 API；浏览器视觉、移动端交互以及真实麦克风/扬声器场景由 Round 21 浏览器专项审计覆盖。两类证据不能互相替代。
- 未部署。本轮变更必须先进入完整备份分支并通过发布前数据库备份/恢复验证，再进入生产发布。
