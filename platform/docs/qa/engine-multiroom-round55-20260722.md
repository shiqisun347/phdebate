# Round 55：多房间完整比赛与恢复控制审计

日期：2026-07-22  
范围：比赛引擎、房间隔离、真人断线、房主恢复控制；不包含 Web UI 与 MOSS/TTS Provider 实现。

## 结论

本轮新增了一条“生产上限五房间”端到端自动化场景，并与已有完整流程、随机状态和故障恢复测试共同回归。结果证明：在服务适配器正常或返回明确可恢复异常时，五个房间可以独立推进；双真人席位不会被 AI 接管；断线 60 秒只暂停所属房间；房主的暂停、继续、重试、跳过和终止均产生独立、可审计的结果。

没有发现需要绕过权威状态机才能完成比赛的后端流程。此次没有修改 MOSS、TTS 或浏览器播放实现。

## 新增场景

新增测试：`apps/api/tests/test_round55_engine_multiroom.py`

同时运行五个房间：

| 房间 | 参与结构 | 注入事件 | 最终状态 |
| --- | --- | --- | --- |
| 双真人 1v1 | 正反双方均为真人 | 人工暂停/继续；反方断线 59 秒与 61 秒边界；重连后显式继续 | `completed` |
| 4v4 混合赛 | 两名真人、六个固定 AI 席位 | 两名真人发言；两个 Agent 席位并行推进 | `completed` |
| Agent 重试赛 | 一名真人、一个固定 AI 席位 | 首次 Agent 超时；房主重试当前步骤 | `completed` |
| 人工复核赛 | 一名真人、一个固定 AI 席位 | 自动发言完成后，房主跳过裁判 | `review_required` |
| 主动终止赛 | 真人等待发言 | 房主终止比赛 | `terminated` |

验证点：

- 五个房间从 `preparing` 进入首个阶段后都有有效阶段索引与有效倒计时，不出现无原因等待。
- 多个 Agent 调用确实并行，且所有内容只写入请求中的房间。
- 真人断线 59 秒时比赛继续等待本人；超过 60 秒时仅该房间暂停。
- 真人离线时继续比赛返回 `409`；全部真人重连后仍不会自动继续，必须由房主显式继续。
- 真人席位始终为 `human`，所有房间均无 `seat.ai_substituted` 事件，也无 `ai_substitute` 席位。
- Agent 失败只暂停所属房间；重试只重做当前步骤。
- 终态房间不存在 `speaking`、`synthesizing` 或 `playing` 残留发言。
- 房间与比赛终态一致，事件序号从 1 到 `room.seq` 连续且不串房。
- 正常完成的比赛均生成已批准裁判结果。

## 代码整理

- 更新 `MatchEngine` 中两处已经过时的“AI 接替真人”注释，明确当前唯一规则：真人席位全场保持真人；断线只触发安全暂停；Agent 预生成只适用于开赛时已经固定为 AI 的席位。
- 权威容量仍由 `room_capacity.MAX_ACTIVE_ROOMS = 5` 和创建房间时的事务锁保护；暂停房间也占用一个比赛容量，直到终止或完成，避免第六场绕过限制。

## 回归结果

执行：

```text
ruff check apps/api/app/services/match_engine.py apps/api/tests/test_round55_engine_multiroom.py
```

结果：`All checks passed`

核心完整流程与并发回归：

```text
pytest -q \
  apps/api/tests/test_round55_engine_multiroom.py \
  apps/api/tests/test_full_match_api_simulation.py \
  apps/api/tests/test_round43_multi_match_flow_audit.py \
  apps/api/tests/test_round45_no_ai_takeover_policy.py \
  apps/api/tests/test_round46_disconnect_flow_boundaries.py \
  apps/api/tests/test_multi_room_simulation.py \
  apps/api/tests/test_round15_engine_soak.py
```

结果：`49 passed`

容量、控制边界、异步任务失效与恢复回归：

```text
pytest -q \
  apps/api/tests/test_platform.py::test_system_allows_only_five_simultaneously_open_rooms_and_releases_capacity \
  apps/api/tests/test_round6_engine_recovery.py \
  apps/api/tests/test_round33_control_boundaries.py \
  apps/api/tests/test_round16_engine_resilience.py
```

结果：`15 passed`

合计本轮选择性后端回归：`64 passed`。

唯一警告为测试框架的 Starlette `TestClient` 兼容性弃用提示，不影响比赛运行时。

## 仍需与其他审计合并验证的项目

- 本测试使用确定性的进程内 Agent、TTS 和裁判替身，证明的是比赛状态机和跨房间隔离，不代表真实 MOSS 推理时延或浏览器 WebRTC 连续播放质量。
- 真实浏览器的单音轨、弱网、音频首包和长发言连续性应以本轮并行的音频/浏览器审计为准。
- UI 中房主按钮的文案、层级和可发现性应由前端审计确认；本轮已证明对应后端动作与错误边界可用。

