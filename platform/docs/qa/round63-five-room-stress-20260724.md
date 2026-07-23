# Round 63：五房间并行比赛压力与恢复专项

日期：2026-07-24  
范围：最多 5 个房间、多真人与多 Agent、全局观众容量、MOSS/TTS 队列、异常恢复和赛果隔离。  
部署状态：仅本地实现与验证，未部署生产环境。

## 结论

五房间并行比赛的权威状态、观众容量、Agent 内容、MOSS 语音槽、恢复操作和最终赛果均能保持房间隔离。本轮压力回归实际发现并修复了一项严重控制权回归：运行中的真人房主断线 60 秒后，系统一度会把房主自动转移给在线队友。现在恢复为正式规则：只暂停比赛，真人席位、身份和原房主控制权都不变化。

完整 API 回归结果：

```text
605 passed, 1 xfailed
```

## 新增五房间组合测试

测试文件：`platform/apps/api/tests/test_round63_five_room_stress.py`

### 1. 五房间与全局五观众上限

- 同时创建 5 个开放房间成功。
- 第 6 个房间返回 409，不能突破系统上限。
- 在 5 个不同房间各建立 1 个观战连接后，第 6 个观众返回 WebSocket 4429。
- 房主和辩手连接不占用观众名额。
- 任一观众离开后，释放出的全局名额可立即被其他房间使用。
- 任一房间关闭后，第 6 个房间可正常创建。

### 2. 五个 MOSS 房间共用一个真实语音槽

- 使用正式 `MossTTSRealtimeProvider` 调度边界，而不是只测试自建假队列。
- 5 个房间同时调用 `prepare()`。
- 第一个房间占用语音槽，其余 4 个房间排队，不提前接受 Agent 文本。
- 每次释放后，下一个房间依次取得语音槽。
- 峰值活跃 MOSS 任务始终为 1。
- 5 个房间全部获得服务，没有队列拒绝、槽泄漏或永久锁死。

### 3. 多 Agent、TTS 排队和单房间故障恢复

- 5 个不同辩题同时进入 Agent 发言。
- Agent 调用实际并行，TTS 播放准备保持单槽有界。
- 指定一个房间首次 Agent 调用失败，其余 4 个房间继续完成并产生独立结果。
- 房主重试失败房间后，只重做该房间的失败步骤。
- 失败房间保留 `failed_retried` 审计记录，新的完整发言正常进入裁判。
- 每场发言内容只包含自己的房间号和辩题。
- 每场裁判结果、胜方、理由、事件序号和比赛状态均独立一致。

### 4. 真人断线及房主恢复操作隔离

- 五个真人比赛房间同时运行。
- 第一房间房主断线 61 秒后，只有第一房间暂停，其余四场的状态、阶段和事件序号不变化。
- 断线真人席位始终保持 `human`，未产生 `seat.ai_substituted`。
- 即使同房间有在线真人队友，也不会自动获得房主权；其恢复请求返回 403。
- 原房主在真人未全部重连时继续比赛返回 409。
- 全部真人恢复后，原房主可以从原阶段继续。
- 另四个房间分别执行暂停/继续、跳过和终止，操作不会改变其他房间。

## 本轮发现并修复的真实问题

### P0：运行中房主断线后被静默转移控制权

根因位于 `MatchEngine._expire_presence`：

- 达到 60 秒断线阈值后，系统先正确暂停房间；
- 随后又调用 `_transfer_disconnected_owner`，把房主权转给在线真人队友；
- 原房主返回后所有控制请求从应有的状态冲突 409 变成权限错误 403；
- 在线队友可在没有显式移交的情况下恢复、跳过或终止比赛。

修复：

- 运行、准备、暂停和评审中的真人断线只执行安全暂停。
- 真人席位、用户身份和 `owner_id` 均保持不变。
- 只有赛前大厅的 120 秒离线席位清理可以沿用既定房主转移策略。
- 比赛开始后的房主变更只能通过显式“移交房主”接口或系统管理员完成。

该修复恢复了 Round 9、13、16、44 和 45 的既有安全边界。

### 测试建模修正：生产重试必须显式在线

三个生产重试测试在切换 `APP_ENV=production` 后仍使用测试环境默认的离线席位，因此新在线校验会先于“服务快照缺失”或“MOSS 未暖机”返回。测试现已显式建立在线真人 presence，确保每个案例只验证自己的目标故障。

启动在线状态重置测试也改为先统计所有遗留的在线真人，再断言重置数量，避免共享测试数据库中的合法前置数据造成脆弱的固定数量断言。

## 验证命令

五房间新增专项：

```text
uv run pytest -q tests/test_round63_five_room_stress.py
```

结果：4 passed。

房间、观众、TTS 与不接管策略组合回归：

```text
uv run pytest -q \
  tests/test_round63_five_room_stress.py \
  tests/test_round55_engine_multiroom.py \
  tests/test_realtime_hub.py \
  tests/test_lighttts_admission.py \
  tests/test_round45_no_ai_takeover_policy.py \
  tests/test_providers.py
```

结果：149 passed。

最终以完整 API 回归为权威证据：

```text
uv run pytest -q
```

结果：605 passed，1 xfailed。

代码检查：

```text
uv run ruff check \
  app/services/match_engine.py \
  tests/test_round63_five_room_stress.py \
  tests/test_platform.py \
  tests/test_round16_engine_resilience.py \
  tests/test_round24_start_readiness.py
```

结果：All checks passed。

## 发布前检查

- 本轮没有操作生产服务器。
- 发布时应再次确认生产环境 `MAX_ACTIVE_ROOMS=5`、全局 `SPECTATOR_LIMIT=5`。
- MOSS 网关必须保持一条正式活跃语音槽时的排队超时足够覆盖前方四场比赛。
- 生产冒烟必须确认：在线队友不会在房主断线后自动成为房主，原房主重连后仍拥有控制权。
