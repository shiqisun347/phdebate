# Round 6 后端状态机与恢复措施审计

日期：2026-07-19

## 结论

Round 6 在隔离测试数据库和确定性 fake Provider 下复核了正常比赛以外的恢复路径。已有 1v1 人人、1v1 人机、4v4 多真人与多 AI 闭环继续通过；本轮新增四个房间同时处于健康、Agent 超时、裁判失败和中途终止四种状态的交错模拟。

发现并修复一个终局一致性缺口：AI 裁判请求进行中，房主终止比赛时，旧逻辑只把 Room 和 Match 标记为 `terminated`，正在运行的 `JudgeScorecard` 仍保持 `running`，直到外部裁判返回。如果裁判永久不返回，该记录会成为无法自动恢复的残留状态；引擎重启恢复也不会扫描已经终局的房间。

现在终止操作在同一个房间事务中调用既有 `interrupt_inflight_judging`：

- `JudgeScorecard.status` 立即变为 `interrupted`；
- reasoning 记录 `match_terminated`；
- 追加唯一的 `judge.interrupted` 事件；
- 迟到裁判结果不能覆盖 `terminated`；
- 同一终止幂等键重放不会重复追加事件。

## 可靠音频保护边界

本轮没有修改或调用真实：

- TTS、MOSS-Realtime；
- LiveKit；
- FunASR；
- AudioWorklet、浏览器 PCM 播放器；
- `debate-stage.tsx`；
- 任何可靠音频生成、传输或播放代码。

测试中的 Agent、TTS URL 和裁判均为内存 fake。fake TTS 只返回不存在的测试 URL，不创建或播放音频。

## 新增四房间并发恢复模拟

四个独立房间从真实建房、准备和开赛流程进入状态机，然后同时执行：

| 房间 | 注入状态 | 恢复措施 | 最终状态 |
| --- | --- | --- | --- |
| 健康房 | 无故障 | 自动推进 | `completed` |
| Agent 超时房 | 首次 Agent 抛出 `agent_timeout` | 房主使用“重试当前步骤”，重复请求按幂等键重放 | `completed` |
| 裁判失败房 | 首次裁判抛出 `judge_timeout` | 进入 `review_required`，管理员在 revision 校验下使用当前有效裁判配置重试；旧 revision 被拒绝 | `completed` |
| 中途终止房 | Agent 请求被阻塞时房主终止 | 终止请求幂等；迟到 Agent 结果失效 | `terminated` |

验证项：

- 三个可恢复房间各自只保存本房间辩题对应的 Agent 正文与裁判理由；
- Agent 首次失败记录不被当作有效发言，也不会污染重试后的历史；
- 裁判失败只改变自己的房间，健康房同时正常完成；
- 终止房没有 `match.completed`，迟到结果不能复活；
- 管理员使用旧裁判 revision 重复重试返回 409；
- 所有房间的 Room、Match、Scorecard 状态保持一致。

## 恢复矩阵复核

| 风险 | 覆盖与结果 |
| --- | --- |
| 1v1 人人、人机、4v4 多真人多 AI | Round 5 三形态交错闭环继续通过 |
| 准备、开始与取消竞态 | 权威结果唯一，无重复 Match |
| 暂停进行中的 Agent | 旧任务失效，继续后不会复活 |
| Agent 坏输出与超时 | TTS 前拦截或进入异常暂停；可幂等重试 |
| 真人断线 60 秒 | 中断遗留真人发言并由 AI 接替 |
| 真人返回 | 申请、房主审批与实时同步通过；终局请求自动过期 |
| 手动跳过和终止 | 活跃任务中断；迟到 Agent/裁判结果不能复活 |
| 裁判失败 | 进入 `review_required`，不发布排名；管理员可使用当前裁判配置重试 |
| 服务重启 | 仅中断引擎拥有的未完成任务，终局结果不回退 |
| 跨房间操作 | 房间、比赛和席位三层校验，错误标识返回 409/403 |
| 重复请求 | 控制、发言完成、Agent 重试、裁判 revision 均有幂等或并发版本保护 |

## 代码改动

### `apps/api/app/api/rooms.py`

在 `control/terminate` 的事务中增加：

```python
match_engine.interrupt_inflight_judging(db, room, reason="match_terminated")
```

复用了暂停流程已经验证的裁判中断实现，没有引入新服务或新状态。

### `apps/api/tests/test_round6_engine_recovery.py`

新增：

1. 裁判调用进行中终止，立即中断 Scorecard，并阻止迟到结果复活。
2. 四房间健康、Agent 超时、裁判失败、Agent 阻塞终止的并发恢复与隔离测试。

## 测试结果

Round 6 新增测试：

```text
2 passed
```

Round 5/6 与既有恢复矩阵定向测试：

```text
14 passed
```

完整后端：

```text
374 passed, 2 warnings in 158.24s
```

静态检查：

```text
ruff: All checks passed
```

两个 warning 是既有依赖弃用提示：Starlette `TestClient` 的 httpx 兼容提示，以及 Python 3.13 将移除 `audioop`，与本轮改动无关。

## 运维与产品建议

- 单场控制台在终止后应显示“裁判任务已中断”，而不是继续显示裁判运行中。
- 裁判失败恢复前必须存在一个启用的 JudgeProfile；无配置时接口明确返回 409。管理后台应把“启用裁判配置”作为直接修复入口。
- 继续保留 `Room → Match → Scorecard` 终局状态一致性作为发布门禁，避免只验证页面上的 Room 状态。
- 本轮仅提交代码与测试，未部署生产。
