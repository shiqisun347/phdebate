# 多比赛自动流程审计（2026-07-22）

## 结论

本轮在隔离 SQLite 测试环境中验证了四个同时存在的比赛：两个 `1v1 辩论训练赛` 和两个 `4v4 人机辩论日常赛`。测试覆盖真人发言、AI 发言、AI Agent 失败重试、房主暂停/继续、跳过裁判转人工复核、紧急终止、AI 裁判、结果查询、事件序列和跨房间数据隔离。

新增的端到端审计用例通过：

```text
1 passed, 1 warning in 0.76s
```

将该用例与五房间模拟、生命周期恢复和引擎恢复回归一起运行：

```text
31 passed, 1 warning in 10.00s
```

此前 API 全量回归也通过：

```text
524 passed, 1 xfailed, 2 warnings in 95.17s
```

## 新增用例

文件：`apps/api/tests/test_round43_multi_match_flow_audit.py`

四个房间的最终状态如下：

| 房间 | 赛事 | 场景 | 预期终态 |
| --- | --- | --- | --- |
| one | 1v1 | 真人正方立论 → AI 反方回应 → AI 裁判 | `completed` |
| four | 4v4 | 多席位 AI 发言，正方一辩真人总结；首次 Agent 超时后房主 retry | `completed` |
| review | 4v4 | 多席位 AI 发言，裁判阶段由房主 skip | `review_required` |
| terminate | 1v1 | 开局后房主紧急终止 | `terminated` |

用例同时检查：

- 四房间并行推进时，每个 `MatchEvent.room_id` 与房间一致，`seq` 从 1 连续递增。
- AI Agent payload 为每个房间带独立 `match_id`、`room_code`、`task_id`，生成文本包含本房间题目；裁判输入不包含其他房间内容。
- Agent 第一次失败只暂停 `four`，不会影响 `one`、`review` 或 `terminate`；retry 后该房间正常完成。
- `one` 暂停期间，其他房间仍可推进；resume 后真人发言可以继续。
- `review` 跳过裁判后进入 `review_required`，没有伪造完成分数。
- `terminate` 进入不可写终态，且没有残留 `speaking`、`synthesizing` 或 `playing` 发言。
- 四个房间的 `/result` 都返回与房间终态一致的比赛结果。

## 已有相关回归覆盖

- `test_multi_room_simulation.py`：五房间并发、混合暂停/真人等待/过期/播放/裁判状态、随机多人操作、裁判和语音服务快照隔离、LightTTS 重试。
- `test_round12_lifecycle.py`：4v4 排名赛完整流程、真人掉线 60 秒后的 AI 接替、Agent 失败与 retry、进程恢复、席位恢复、自由辩论、裁判、skip、terminate、结果/历史/排行榜/归档/重赛。
- `test_round6_engine_recovery.py`：Agent/裁判运行中 terminate、幂等重放和过期结果不能复活比赛。
- `test_round20_agent_prefetch.py`：主持预设音期间的 Agent 预取和失效边界。
- `test_round33_control_boundaries.py`：控制动作状态边界和异常恢复。

## 本轮未修改

遵照审计范围，本轮只增加 API 测试和 QA 文档，没有修改生产服务、Web、TTS 或 ASR 代码。当前新增测试是确定性的进程内 Provider fake，不代表真实 Agent、MOSS、浏览器音频链路已在此用例中验证；真实服务验收仍应使用现有生产 smoke/浏览器报告。

## 后续关注

1. 继续用浏览器完成真人多设备场景：两个真人同时抢座、同一席位接管、浏览器断线后恢复到只读观战。
2. 用真实 Agent/MOSS 运行至少一场 1v1 和一场 4v4，记录首字延迟、音频连续性、ASR 双向帧和字幕行更新。
3. 将此用例纳入发布前 API gate；如果全局并发上限调整，保持“至少两个 1v1 + 两个 4v4 并行”的覆盖。
