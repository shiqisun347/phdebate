# Round 20：自由辩论 AI 发言判断与并发候选

## 实现结果

- 独立 Agent 服务新增 `POST /debate/api/should-speak`。接口沿用 Gateway Key，要求独立 `task_id`，返回 `should_speak`、简短原因和重放标记。
- 对方发言完成、三秒补充申请窗口开始后，主平台立即同时启动非权威推测任务：
  1. `decision-<room/stage/turn/seat fingerprint>` 是否发言判断；
  2. `candidate-<same fingerprint>` 隐藏候选发言生成。
- 判断为否时立即中断候选任务，不创建 `Speech`、不进入 TTS，记录 `free.agent_intent_resolved` 和 `free.agent_candidate_discarded`，然后进入下一方三秒申请窗口。
- 判断为是时复用已生成候选文本创建正式 `Speech`，避免第二次 LLM 调用；候选失败则回到原有正式 Agent 生成路径。
- 三秒内出现真人申请时立即中断并丢弃推测任务；窗口结束仍无真人时消费同一任务或等待其完成，不重复请求。
- 判断接口超时、不可达或格式异常时 fail-open，继续原有 AI fallback，不暂停比赛。

## 数据与隔离

- 决策和候选任务 ID 含房间、阶段、轮次、席位指纹；同 ID 不同请求由 Agent 服务返回 409。
- `should_speak` 与 `debate_candidate` 都不会写入比赛或长期 Memory；被丢弃候选不会污染下一轮上下文。
- 比赛暂停、终止、换阶段或换轮时，平台每 100ms 校验权威房间状态，失效后中断远端任务并取消本地等待。
- 决定写入房间事件日志，包含 fallback 标记和两个任务 ID，便于回溯。

## 接口契约

请求与 `/api/debate` 字段兼容，额外固定：

```json
{
  "task_type": "should_speak",
  "task_id": "decision-...",
  "match_id": "...",
  "room_code": "123456",
  "max_token": 48,
  "output": {"stream": false, "language": "zh-CN"}
}
```

响应：

```json
{
  "task_id": "decision-...",
  "should_speak": false,
  "reason": "当前论点已经充分，没有新增反驳",
  "replayed": false
}
```

## 验证

- 平台定向测试：11 passed。覆盖 REST 路径、鉴权和房间字段、服务失败 fail-open、比赛失效取消、否决即时丢弃、三秒窗口内提前启动、跨进程可见的真人申请中断及窗口结束只复用一次。
- 平台 Ruff：通过。
- 独立 Agent 代码：`compileall` 通过。
- 独立 Agent 新接口测试已加入，覆盖严格 JSON、幂等重放、请求冲突和不写 Memory。当前本机共用平台虚拟环境未安装 `litellm`，因此需在 Agent 完整依赖环境或 CI 中执行。

## 边界

- 本轮只预生成文本，不提前占用正式 TTS/GPU 语音槽；只有肯定判断后的正式 `Speech` 才进入冻结的可靠语音链路。
- 决策最多等待 5 秒。首版从对方发言完成、三秒窗口开始时启动；尚未提前到对方仍在发言的阶段，避免长发言期间产生大量最终不会采用的外部 LLM 用量。
- 当前每轮沿用既有确定性 AI 席位选择；否决后换边进入下一轮，不在同一方连续遍历多个 AI，避免无上限调用和自由辩论停死。
