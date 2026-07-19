# Debate Agent startup warmup 后 10 波×3 并发复测

执行时间：2026-07-18 02:01:02–02:01:35 CST。证据类型：`newly_run`。运行模式：`minimal-reproducible-run`。

## 结论

生产当前主模型为 `qwen-plus`。本轮完成 10 个顺序波次、每波 3 个并发流，共 30 个只读直接上游 SSE 请求；30/30 收到 `[DONE]`，没有正文缺失，没有 `<think>`、`reasoning_content` 或其他 thinking 内容泄露。

包含首波空闲冷态的请求级结果：

| 里程碑 | P50 | P95 | max |
|---|---:|---:|---:|
| 首个正文 delta | 1.067s | 2.779s | 2.962s |
| 10 个汉字 | 1.404s | 3.271s | 3.272s |
| 首个强标点短句 | 1.565s | 3.272s | 3.337s |
| 完整响应 | 2.585s | 4.233s | 4.347s |

启动 warmup 只发生在 Agent 服务启动时；本轮开始时服务已运行约 44 分钟。第 1 波三路同时出现空闲冷态：首正文 `2.749–2.962s`、10 汉字 `3.271–3.272s`、完整响应 `4.211–4.347s`。因此，**startup-only warmup 不能证明能够跨较长空闲期持续消除首波长尾**。

作为诊断，排除第 1 波后剩余 27 个热态样本：

| 里程碑 | P50 | P95 | max |
|---|---:|---:|---:|
| 首个正文 delta | 1.061s | 1.485s | 2.076s |
| 10 个汉字 | 1.391s | 1.682s | 2.076s |
| 首个强标点短句 | 1.560s | 1.921s | 2.077s |
| 完整响应 | 2.552s | 3.142s | 3.226s |

该热态子集只用于定位冷态影响，不能替代包含首波的主结果。第 7 波单路仍出现约 `2.076s` 的首正文/10 汉字长尾，说明即使连接已热也仍存在上游排队或推理抖动。

本轮测量从 LLM 请求发起开始计时，不是用户最新定义的“LLM 首字 → 浏览器实际出声”指标；因此它不能直接判定 2.5 秒浏览器首声门禁。它证明的是：Agent 启动 warmup 改善服务启动阶段，但若要控制用户等待时间，仍需考虑持续保温或可预测的专属推理容量。

## 逐波长尾

| 波次 | 首正文 max | 10 汉字 max | 完整响应 max | 观察 |
|---:|---:|---:|---:|---|
| 1 | 2.962s | 3.272s | 4.347s | 三路共同空闲冷态 |
| 2 | 1.054s | 1.682s | 2.552s | 热态 |
| 3 | 1.174s | 1.417s | 2.949s | 热态 |
| 4 | 1.485s | 1.501s | 2.862s | 热态 |
| 5 | 1.319s | 1.332s | 3.226s | 首段正常，完整响应偏长 |
| 6 | 1.061s | 1.569s | 2.665s | 热态 |
| 7 | 2.076s | 2.076s | 2.925s | 单路首段长尾 |
| 8 | 1.292s | 1.511s | 2.615s | 热态 |
| 9 | 1.043s | 1.265s | 2.649s | 热态 |
| 10 | 1.072s | 1.392s | 2.523s | 热态 |

## 安全核对

- 开始前主站 ready，`active_match_processing=false`；Agent `running` task 为 `0`。
- 每一波开始前均重新检查主站 idle，12 次健康检查全部通过；结束后再次确认 `active_match_processing=false`。
- AgentTask 总数 `118→118`，running `0→0`；MemoryItem 总数 `84→84`。
- 没有调用会创建业务 AgentTask/Memory 的 Debate Agent 控制路由；仅直接请求生产当前配置的 OpenAI-compatible SSE 上游。
- 没有修改配置、代码、数据库、服务或部署，没有重启任何服务。
- 原始 JSON 和脚本均不保存 Provider endpoint、API key、Gateway secret 或模型正文，只保存不可逆响应 SHA-256。
- 结束后主站、Agent health 均为 ready；Agent health 仍记录启动 warmup 为 3/3 成功，模型 `qwen-plus`，当时延迟约 `2.469–2.474s`。

## Evidence Inventory

- Status / Evidence Type：`newly_run`，2026-07-18。
- Aggregation：30 个请求级样本；10 个顺序波次，每波 3 个并发流；P95 使用全部请求的线性插值分位数。
- Workdir：生产 `/home/ubuntu/sunsq/debate-agent/apps/api`，使用生产虚拟环境；测试脚本仅暂存在 `/tmp`，结束后删除。
- Inputs：固定短辩题与一条短历史发言；读取生产当前人设、已发布 Prompt、preset 和主 Provider；模型 `qwen-plus`，上限 180 tokens。
- Output Artifacts：[原始 JSON](agent-postwarm-30.json)、[可复核并发脚本](run_agent_postwarm_concurrent.py)。
- Claim Readiness：`paper_ready` 仅限“本时段、固定短输入、直接上游、10 波×3 并发的 Agent 文本延迟”；端到端浏览器实际出声、长上下文、多房间 Agent+TTS 综合容量均仍为 `blocked`。

## Protocol Risks

1. 30 个请求比小样本更能观察尾部方向，但仍不足以建立长期生产 SLO；不同时间段、供应商负载和地域网络可能改变结果。
2. 为保证零业务写入，本轮绕过 Agent API scheduler、RPM 计数、持久化、interrupt 和 fallback；只验证生产配置的直接 LLM SSE。
3. 本轮不包含短语聚合、TTS 首 PCM、服务端音频队列、WebRTC/WS 传输、浏览器 AudioWorklet 或实际扬声器首声。
4. 固定短上下文不代表比赛后半程长上下文、不同赛程 Prompt、2–3 个完整房间同时运行的复合压力。
5. startup warmup 距离本轮约 44 分钟；结果可证明其不能维持该空闲时长后的持续热态，但不能确定供应商冷态、排队和网络抖动各自的占比。
