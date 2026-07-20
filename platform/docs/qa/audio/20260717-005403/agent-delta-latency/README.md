# 生产 Debate Agent 增量延迟基准

执行时间：2026-07-17 22:33–22:34 CST。证据类型：`newly_run`。运行模式：`minimal-reproducible-run`。

## 结论

当前 Agent 快速路径在热态下已经能较快地产生可送入 TTS 的中文片段，但不能满足“每次等待 Agent 发言不超过 3 秒”的端到端要求。

- 7 次固定短辩题请求中，首个可见 delta 为 P50 `1.460s`、P95 `4.935s`、max `6.186s`。
- 累计 12 个中文字符为 P50 `1.666s`、P95 `5.112s`、max `6.438s`。
- 首个强标点可朗读子句为 P50 `1.820s`、P95 `5.112s`、max `6.438s`。
- 完整响应为 P50 `3.123s`、P95 `6.982s`、max `8.248s`。
- 第 1 次出现明显冷态或上游排队长尾；排除首轮后，6 个热态样本的首 delta P95 为 `1.890s`，首强标点 P95 为 `1.975s`。这是辅助诊断，不替代包含冷态的主结果。
- 全部 7 次均收到 `[DONE]`，输出 104–117 个中文字符，未发现 `<think>`、`reasoning_content` 等思考内容泄露。

端到端 3 秒预算判定：**NO-GO**。主样本中 Agent 单独产生可朗读子句的 P95 已为 `5.112s`，尚未加入 Agent→TTS 切分、TTS 首 PCM、服务端转发、浏览器缓冲和 AudioWorklet 播放；即使按 P50 `1.820s`，留给其余语音链路的预算也只有约 `1.180s`，不足以证明稳定满足目标。

## 协议

- 聚合单位：一次独立、串行的流式 LLM 请求。
- 样本数：7。
- 输入：固定辩题“中学生使用生成式人工智能辅助学习利大于弊”，固定一条反方短发言，要求正方作约 120 字的自由辩论回应。
- 配置：读取生产当前启用的人设、已发布 Prompt、模型参数和直接 LLM Provider；模型为 `qwen3.7-plus`，`enable_thinking=false`，请求上限 180 tokens。
- 调用方式：在生产机上直接请求当前配置的 OpenAI-compatible SSE 上游，不调用 `/debate/api/debate`，因此不进入 AgentTask、Memory、比赛房间或主站状态机。
- `HTTP open`：开始请求到收到 HTTP 响应头。
- `first delta`：开始请求到首个非空、面向观众可见的正文 delta。
- `10/12/16 中文字符`：累计 Unicode 汉字数首次达到对应阈值。
- `首强标点`：累计正文首次出现 `。！？；` 或其半角形式，可作为第一段可朗读子句的保守边界。
- `full`：开始请求到 SSE 完成。
- P95：对 7 个请求级样本采用线性插值分位数。

## 汇总

| 里程碑 | P50 | P95 | max | 3 秒 P95 门禁 |
|---|---:|---:|---:|---|
| HTTP open | 1.221s | 4.910s | 6.150s | FAIL |
| 首 delta | 1.460s | 4.935s | 6.186s | FAIL |
| 10 个中文字符 | 1.666s | 5.078s | 6.389s | FAIL |
| 12 个中文字符 | 1.666s | 5.112s | 6.438s | FAIL |
| 16 个中文字符 | 1.792s | 5.426s | 6.796s | FAIL |
| 首强标点可朗读子句 | 1.820s | 5.112s | 6.438s | FAIL |
| 完整响应 | 3.123s | 6.982s | 8.248s | 不作为首声门禁 |

## 逐次结果

| Run | HTTP open | 首 delta | 10 字 | 12 字 | 16 字 | 首强标点 | full | 中文字符 | DONE |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 1 | 6.150s | 6.186s | 6.389s | 6.438s | 6.796s | 6.438s | 8.248s | 108 | 是 |
| 2 | 1.221s | 1.460s | 1.534s | 1.534s | 1.848s | 1.848s | 3.015s | 111 | 是 |
| 3 | 1.151s | 1.390s | 1.792s | 1.792s | 1.792s | 1.792s | 3.745s | 113 | 是 |
| 4 | 1.507s | 1.507s | 1.666s | 1.666s | 1.666s | 1.785s | 2.937s | 105 | 是 |
| 5 | 1.060s | 1.149s | 1.299s | 1.510s | 1.513s | 1.510s | 2.821s | 117 | 是 |
| 6 | 2.017s | 2.018s | 2.018s | 2.018s | 2.230s | 2.018s | 4.028s | 104 | 是 |
| 7 | 1.181s | 1.213s | 1.545s | 1.545s | 1.551s | 1.820s | 3.123s | 107 | 是 |

## 安全与生产前后核对

- 开始前：主站 `preparing/running/paused/judging` 活动比赛为 0；Agent running task 为 0；V2 与 Agent 健康接口均正常。
- 结束后：活动比赛仍为 0；AgentTask 总数 `43→43`，running `0→0`，MemoryItem 总数 `84→84`。
- `jixia-agent-api`、`jixia-api`、`jixia-engine`、`jixia-worker`、`jixia-web` 均保持 `RUNNING`；两个健康接口保持 ready/ok。
- 未创建 AgentTask、Memory、房间或比赛，未调用业务 Agent 路由，未修改生产配置、服务或代码。
- JSON 和报告均不包含实际 Provider endpoint、API key 或 Gateway secret。

## Evidence Inventory

- Status / Evidence Type：`newly_run`，2026-07-17。
- Command：生产机本地运行只读脚本 `run_agent_delta_benchmark.py --runs 7`；命令中的 SSH 凭据未回显、未写入产物。
- Workdir：生产 `/home/ubuntu/sunsq/debate-agent`；结果保存回本地 QA 目录。
- Environment：生产 Agent 当前 Python 虚拟环境和当前数据库配置；主站与 Agent 健康检查均通过。
- Inputs：固定短辩题、固定历史短句、生产当前人设/Prompt/模型 preset/Provider。
- Output Artifacts：[原始 JSON](agent-delta-latency.json)、[可复核脚本](run_agent_delta_benchmark.py)。
- Claim Readiness：`paper_ready` 仅限“本时段、该固定短输入、串行直接上游 Agent 文本延迟”；“端到端语音低于 3 秒”仍为 `blocked`。

## Protocol Risks

1. 7 个串行样本能识别当前方向和明显长尾，但不足以形成稳定的生产 SLO 尾延迟估计；正式容量门应至少覆盖 30 次冷/热样本和 2/3 场同步并发。
2. 该基准隔离的是 Agent 文本生成，不包含 Agent→TTS 流水线、TTS TTFT、PCM 传输、浏览器缓冲和真实扬声器首声。
3. 直接上游请求复用了生产 Prompt、模型和参数，但绕过 Agent API 的 scheduler、RPM Redis 计数、任务持久化与回退逻辑；该设计是为了满足无业务写入边界。
4. 固定短输入不能代表比赛后半程的长上下文和多 Agent 并发。
5. 第 1 个请求的 6 秒级长尾可能来自上游冷态或排队；当前证据不能进一步区分两者。

## 下一步门禁

1. 将 Agent 连接做持续预热或使用稳定的低延迟推理服务，要求包含冷态的首强标点 P95 ≤ `1.5s`、max ≤ `2.0s`，为 TTS/播放保留至少 1 秒。
2. 以 10–16 个中文字符或首强标点作为增量 TTS 首次 flush 条件，同时设置 150–250ms 最大等待，避免短语迟迟没有标点。
3. 下一轮必须直接测 `Agent 请求开始 → 浏览器 AudioWorklet 首 PCM 实际播放`，并覆盖 2/3 场同步并发、长上下文、取消和恢复；验收值为 P95 ≤ `3.0s`，且整段无欠载、吞词或音色漂移。
