# 比赛全流程断线与恢复边界审计（Round 3）

## 目标

本轮只审计后端比赛状态机与实时 ASR 边界，验证以下不可破坏规则：

- 任意真人辩手断线达到 60 秒，整场比赛自动暂停。
- 真人席位始终保留为 `human`，不得创建 AI 接替席位或继续无人监管的自动流程。
- 真人重新连接只恢复在线状态，不自动恢复比赛。
- `resume`、`retry` 和断线暂停期间的 `skip` 必须再次校验全部真人在线且账号可用。
- 暂停时必须中断当前真人、永久 AI 或裁判任务，保留已经确认的 ASR 文字。
- 多个真人相继或同时断线时事件必须幂等，多房间之间不得串状态或阻塞正常推进。

## 发现并修复的问题

### ASR 最终字幕与断线暂停存在竞态

ASR WebSocket 在收到最终识别结果时，会先调用 `_asr_stream_active` 校验，再调用 `persist_asr_final` 写入。若真人断线暂停恰好发生在两者之间，写入函数会因为 Speech 已变为 `interrupted` 而拒绝结果，但旧桥接代码仍继续向浏览器和房间发布成功的 `asr` 最终字幕。

这会造成浏览器显示一段权威数据库没有接受的文字，刷新或赛后记录中又消失，破坏字幕、发言内容和审计记录的一致性。

修复内容：

1. `persist_asr_final` 使用 Speech 行锁，与比赛引擎对同一发言的中断提交串行化。
2. `_asr_stream_active` 新增房间必须为 `running`、Speech 必须仍属于当前阶段的校验；暂停房间和旧阶段不能继续 ASR。
3. 最终结果持久化返回失败时，桥接层发送 `asr_rejected`，原因为 `speech_inactive`，不再发布成功字幕。
4. 已确认最终文字仍由断线暂停路径保留；暂停后迟到的 ASR 最终结果不会继续扩展文字稿。

## 新增覆盖

新增 `tests/test_round46_disconnect_flow_boundaries.py`，覆盖：

1. 两个真人同时超时：生成两条各席位 `participant.disconnect_timeout`，只生成一条 `match.paused`。
2. 同一批过期状态重复执行引擎 tick：房间 `seq` 不增加，不重复写入断线事件。
3. 只重连一个真人：`resume` 和 `retry` 均返回 409，并明确指出仍离线辩手。
4. 全部真人重连：房间仍保持 `paused`，必须由房主显式 `resume`。
5. 真人发言中断：无研究 Transcript 时，从最终 ASR Caption 保留已确认文字；Speech 变为 `interrupted`。
6. 暂停后迟到 ASR：不写入 Transcript，不改变已保存 Speech 内容。
7. ASR 校验/写入竞态：持久化失败时浏览器收到 `asr_rejected: speech_inactive`，不会收到虚假的最终成功字幕。
8. 两个房间并行：断线房间暂停时，健康房间按自己的截止时间进入下一阶段。
9. 非发言真人超时：当前永久 AI 播放也被中断，比赛不会让 AI 独自继续。

## 自动化证据

- Round 46 新增测试：`5 passed`。
- ASR、字幕、断线和无 AI 接管选择性回归：`50 passed, 165 deselected`。
- 引擎、自由辩论、多房间、断线恢复综合回归：`68 passed`。
- Ruff：`app/api/realtime.py` 与新增测试文件全部通过。

综合回归包含：

- `test_round8_engine_scenarios.py`
- `test_round9_engine_scenarios.py`
- `test_round12_lifecycle.py`
- `test_round15_engine_soak.py`
- `test_round16_engine_resilience.py`
- `test_round18_engine_scenarios.py`
- `test_round20_captions.py`
- `test_round20_free_debate_queue.py`
- `test_round33_control_boundaries.py`
- `test_round43_multi_match_flow_audit.py`
- `test_round44_fault_injection_match_flow.py`
- `test_round45_no_ai_takeover_policy.py`
- `test_round46_disconnect_flow_boundaries.py`

## 审计结论

当前后端已通过自动化证据证明：准备期、固定发言、自由辩论、永久 AI 播放和裁判阶段只要存在真人超时离线，比赛都会进入安全暂停；重连不会自动继续；恢复接口无法绕过离线真人校验；多真人和多房间事件保持隔离与幂等。

本轮未部署。线上仍需在统一发布后复测真实 WebSocket 断线 61 秒、ASR 最终结果同时到达和两个并行房间的时序。
