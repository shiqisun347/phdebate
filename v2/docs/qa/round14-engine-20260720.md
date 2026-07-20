# Round14 API / Match Engine 多房间恢复审计

日期：2026-07-20
范围：V2 API、match-engine、房间权威状态、裁判任务与数据一致性。
明确排除：TTS、LiveKit、AudioWorklet、PCM、浏览器音频播放和语音参数均未修改。

## 结论

本轮确认并修复了一个高风险真实竞态：自动裁判任务被暂停后重新启动时，较早的请求如果稍后返回，可能被误认为当前请求并写入旧结果。修复后，每次自动裁判调用都有数据库持久化的唯一 `task_id`；只有仍持有当前任务标识的响应可以结算比赛。暂停、引擎重启会先使旧标识失效，因此乱序、重复和迟到响应不能复活。

新增 Round14 场景测试全部通过，完整 API 测试结果为 `421 passed, 1 xfailed`，Ruff 全部通过。唯一 xfail 是既有的冻结语音流水线窄窗口取消竞态，本轮未触碰。

## 已修复问题

### P0：暂停/恢复后的旧裁判结果可能覆盖新任务

旧流程只用 `JudgeScorecard.status == "running"` 判断返回结果是否有效：

1. 裁判任务 A 开始，scorecard 为 `running`。
2. 房主暂停比赛，A 被标记 `interrupted`。
3. 房主恢复比赛，裁判任务 B 复用同一 scorecard，并再次设为 `running`。
4. A 先于 B 返回；旧代码看到 `running`，会把 A 的过期结果结算为正式结果。

影响包括胜负错误、排行榜错误、归档与发言证据不一致，以及真正的 B 结果被丢弃。

修复内容：

- `JudgeScorecard` 新增持久化 `task_id`。
- 每次自动裁判开始生成新的 UUID，并记录 `judge.started` 审计事件。
- 成功与失败结果都必须同时匹配 `status == running` 和当前 `task_id`。
- 手动暂停和进程重启中断裁判时清空权威任务标识，并在 `judge.interrupted` 中保留被中断任务 ID。
- 新增 Alembic `0026_judge_task_identity`；兼容当前项目“0001 使用最新 metadata 建新库”的历史迁移方式，支持新库、旧库、降级和再次升级。

## 新增场景验证

测试文件：`apps/api/tests/test_round14_engine_scenarios.py`

### 四房并发大厅

- 同时创建 4 个不同辩题房间。
- 每个房间两名真人并发抢同一个席位，严格只有一人成功。
- 所有真人分别准备。
- 房主两个设备并发点击开始，两个请求都得到幂等响应。
- 每个房间只生成 1 个 Match、1 个 `room.locked`，事件序号连续且互不串房。

### 多设备与重复开始

- 同一登录身份的两个浏览器会话同时开始比赛。
- 房间行锁和固定启动幂等键保证一次真实写入、一次重放。
- 不会重复 AI 补位、重复生成 Match 或重复推进状态。

### 暂停期间迟到真人请求

- 真人发言已超时、房间随后暂停。
- 弱网设备补交精确 speech ID 的最终文本时，文本和 TranscriptSegment 被保留。
- 迟到请求不会推进暂停中的阶段，也不会改变其他房间的活动发言。

### 跨房 token / task / event 隔离

- 两个房间故意使用外观相同的设备 lease。
- 房间 B 使用房间 A 的 speech ID 提交时返回 409。
- B 的 Speech、TranscriptSegment 和 MatchEvent 均保持不变。
- 房间 A 的 `speech.late_finalized` 不会出现在 B 的事件流。

### 裁判任务乱序

- 任务 A 启动后暂停比赛，随后恢复并启动任务 B。
- B 运行期间先释放 A 的旧响应。
- A 不得改变 Room、Match、Scorecard、胜负或事件终态。
- B 返回后只产生一次 `match.completed`，正式胜负和理由来自 B。

### 引擎重启恢复

- 构造持久化中的 `running` 裁判任务及旧进程 task ID。
- `recover_inflight_engine_tasks()` 将任务改为 `interrupted` 并清空权威 task ID。
- 中断事件保留旧 task ID，房间事件序号仍为从 1 到 `room.seq` 的连续序列。

## 与既有场景的联合覆盖

本轮相关回归同时运行 Round10–Round13、multi-room simulation，共 `52 passed, 1 xfailed`。结合既有测试，以下目标仍保持通过：

- 房主关闭大厅和终止比赛后的不可变边界。
- 房主离线后的在线真人控制权转移，以及单人房保留恢复出口。
- 同席位双设备接管、旧 lease 拒绝、发言中禁止强行接管。
- 固定阶段 AI 接替后，真人旧设备不能重复提交同一权威轮次。
- 自由辩论允许同席位多轮发言，超时补交不会误伤后续轮次。
- 真人断线 60 秒后 AI 接替，真人返回需显式申请并由房主批准。
- 服务重启中断孤儿 AI 发言和孤儿裁判任务，并可从持久状态继续。
- 20 个并发 AI 房间和 24 个混合状态房间的状态、事件、胜负与内容隔离。
- 终止、取消后控制、席位、发言和 WebSocket 连接不污染终态。
- RatingChange 初始结算幂等，Match、Scorecard、Room 终态一致。

## 门禁结果

```text
Round14 tests:                         4 passed
Round10–14 + multi-room regression:   52 passed, 1 xfailed
Full API suite:                       421 passed, 1 xfailed
Ruff:                                 All checks passed
Alembic 0026:                         upgrade / downgrade / upgrade passed
```

测试警告仅包括 Starlette TestClient 的上游弃用提示和 Python `audioop` 弃用提示，不是本轮回归。

## 后续建议

- 生产部署必须先执行 Alembic 0026，再切换 API/engine 进程。
- 部署时先确认没有处于裁判调用中的活跃比赛；若存在，应暂停并等待恢复窗口。
- 部署后用不调用真实 Agent/TTS 的专用测试房执行一次“裁判暂停 → 恢复 → 旧响应迟到”的灰度验证。
- 继续保留单实例 match-engine；本次任务标识解决外部响应乱序，不替代生产进程的单实例约束。
