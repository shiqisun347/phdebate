# Debate Agent 生产首可播 P95 只读审计

审计时间：2026-07-17。证据类型：`preexisting_artifact + source_audit`。本轮未修改 Agent/V2 代码、配置或生产服务。

## 结论

当前瓶颈不是 TCP、TLS、Nginx 缓冲或 SSE `yield`，而是以下两个串行瓶颈叠加：

> 实现进展：本报告完成后，下文 P0 #1 已在本地 V2 工作树落地：MatchEngine 可用
> 默认关闭的 kill switch 将 Agent delta 送入同一条连续 TTS session，并有定向端到端测试。
> 未部署，生产仍保持原有完整文本路径。下文文件行号是实现前快照，根因数据和 P0 #2–#6 不受影响。

1. **上游模型在响应头前的排队、prefill 或首 token 计算**：三路并发时 `HTTP open residual` P95 为 `2.967s`，而 TCP P95 只有 `13.1ms`。Agent 首个可播子句 P95 因而达到 `3.397s`。
2. **主 V2 把 Agent SSE 重新聚合成完整正文后才调用 TTS**：三路并发时完整正文 P95 为 `4.851s`，比首可播子句 P95 晚约 `1.454s`；串行新连接模式则晚约 `1.732s`。这部分等待可以通过 Agent→TTS 流水线直接消除。

因此，“只继续优化 SSE 转发”不会达到端到端 3 秒目标。优先级必须是：

1. 让 TTS 从 Agent 的稳定增量文本开始，而不是等完整响应；
2. 把发言请求迁移到可控、持续预热、至少稳定承载三路并发的低延迟模型服务；
3. 缩短实时发言 prompt，并让稳定前缀可被模型服务缓存；
4. 再处理 Agent API 内部的 DB/Redis/client 小开销。

## 权威证据

- [串行生产基准](agent-delta-latency/README.md)：7 次直接上游请求，首可播子句 P50 `1.820s`、P95 `5.112s`；首轮出现 `6.438s` 长尾。排除首轮后的热态首可播 P95 为 `1.975s`，说明冷态/外部排队可以制造数秒尾延迟。
- [传输根因 JSON](agent-delta-latency/agent-transport-root-cause/agent-transport-root-cause.json)：新 client、持久 client、三路并发各 6 次。
- [快速路径发布证据](iteration26-agent-fastpath-deployment.md)：关闭 Qwen thinking 后，生产首内容由 `21.173s` 降至 `3.376s`，说明 hidden reasoning 曾是 P0；该问题已处理，但剩余延迟仍不满足目标。

### 传输分解

| 模式 | TCP/TLS P95 | HTTP open residual P95 | 首 delta P95 | 首可播 P95 | 完整响应 P95 |
|---|---:|---:|---:|---:|---:|
| 每次新建 client | 16.6ms | 2.023s | 2.426s | 2.519s | 4.250s |
| 持久 client | 7.6ms | 3.269s | 3.631s | 3.992s | 4.880s |
| 三路并发 | 13.1ms | 2.967s | 3.157s | 3.397s | 4.851s |

附加分解：

| 模式 | 响应头→首 delta P95 | 首 delta→首可播 P95 | 响应头→首可播 P95 |
|---|---:|---:|---:|
| 每次新建 client | 428ms | 271ms | 514ms |
| 持久 client | 371ms | 692ms | 923ms |
| 三路并发 | 219ms | 306ms | 480ms |

由此可见，三路并发首可播 `3.397s` 中约 `2.967s` 已消耗在响应头之前。连接复用没有改善尾延迟；本轮持久连接反而更慢，说明主要变量是上游当时的排队/计算状态，而不是握手。

## 当前生产请求链路

1. V2 创建 Speech，按发言时长计算 `max_token`，然后进入全局 provider semaphore：[/Users/sunshiqi/code/phdebate/v2/apps/api/app/services/match_engine.py:751](/Users/sunshiqi/code/phdebate/v2/apps/api/app/services/match_engine.py:751)、[/Users/sunshiqi/code/phdebate/v2/apps/api/app/services/match_engine.py:778](/Users/sunshiqi/code/phdebate/v2/apps/api/app/services/match_engine.py:778)。
2. V2 使用进程级持久 `httpx.AsyncClient` 请求 Agent SSE：[/Users/sunshiqi/code/phdebate/v2/apps/api/app/services/providers.py:76](/Users/sunshiqi/code/phdebate/v2/apps/api/app/services/providers.py:76)、[/Users/sunshiqi/code/phdebate/v2/apps/api/app/services/providers.py:175](/Users/sunshiqi/code/phdebate/v2/apps/api/app/services/providers.py:175)。
3. Agent 创建任务、准备人设/Prompt/Memory，返回 StreamingResponse：[/Users/sunshiqi/code/phdebate/debate-agent/apps/api/app/main.py:622](/Users/sunshiqi/code/phdebate/debate-agent/apps/api/app/main.py:622)、[/Users/sunshiqi/code/phdebate/debate-agent/apps/api/app/main.py:647](/Users/sunshiqi/code/phdebate/debate-agent/apps/api/app/main.py:647)。
4. Agent 进入全局 generation semaphore，检查 RPM，然后通过 LiteLLM 请求上游：[/Users/sunshiqi/code/phdebate/debate-agent/apps/api/app/agent_engine.py:430](/Users/sunshiqi/code/phdebate/debate-agent/apps/api/app/agent_engine.py:430)、[/Users/sunshiqi/code/phdebate/debate-agent/apps/api/app/agent_engine.py:436](/Users/sunshiqi/code/phdebate/debate-agent/apps/api/app/agent_engine.py:436)、[/Users/sunshiqi/code/phdebate/debate-agent/apps/api/app/agent_engine.py:485](/Users/sunshiqi/code/phdebate/debate-agent/apps/api/app/agent_engine.py:485)。
5. Agent 每收到一个正文 delta 就立即 `yield`：[/Users/sunshiqi/code/phdebate/debate-agent/apps/api/app/agent_engine.py:488](/Users/sunshiqi/code/phdebate/debate-agent/apps/api/app/agent_engine.py:488)、[/Users/sunshiqi/code/phdebate/debate-agent/apps/api/app/main.py:657](/Users/sunshiqi/code/phdebate/debate-agent/apps/api/app/main.py:657)。Nginx 和应用均关闭 buffering：[/Users/sunshiqi/code/phdebate/debate-agent/deploy/nginx.same-host.locations.conf:1](/Users/sunshiqi/code/phdebate/debate-agent/deploy/nginx.same-host.locations.conf:1)、[/Users/sunshiqi/code/phdebate/debate-agent/apps/api/app/main.py:677](/Users/sunshiqi/code/phdebate/debate-agent/apps/api/app/main.py:677)。
6. 但 V2 的 `generate()` 把全部 delta 累加到 final 后才返回：[/Users/sunshiqi/code/phdebate/v2/apps/api/app/services/providers.py:114](/Users/sunshiqi/code/phdebate/v2/apps/api/app/services/providers.py:114)。MatchEngine 随后才把状态改为 `synthesizing` 并调用 LightTTS：[/Users/sunshiqi/code/phdebate/v2/apps/api/app/services/match_engine.py:775](/Users/sunshiqi/code/phdebate/v2/apps/api/app/services/match_engine.py:775)、[/Users/sunshiqi/code/phdebate/v2/apps/api/app/services/match_engine.py:815](/Users/sunshiqi/code/phdebate/v2/apps/api/app/services/match_engine.py:815)、[/Users/sunshiqi/code/phdebate/v2/apps/api/app/services/match_engine.py:900](/Users/sunshiqi/code/phdebate/v2/apps/api/app/services/match_engine.py:900)。

## 瓶颈逐项审计

### 1. 请求队列与并发门

- Agent 进程内 semaphore 默认上限为 8：[/Users/sunshiqi/code/phdebate/debate-agent/apps/api/app/config.py:21](/Users/sunshiqi/code/phdebate/debate-agent/apps/api/app/config.py:21)、[/Users/sunshiqi/code/phdebate/debate-agent/apps/api/app/agent_engine.py:94](/Users/sunshiqi/code/phdebate/debate-agent/apps/api/app/agent_engine.py:94)。V2 的 provider semaphore 默认也是 8：[/Users/sunshiqi/code/phdebate/v2/apps/api/app/core/config.py:64](/Users/sunshiqi/code/phdebate/v2/apps/api/app/core/config.py:64)、[/Users/sunshiqi/code/phdebate/v2/apps/api/app/services/match_engine.py:277](/Users/sunshiqi/code/phdebate/v2/apps/api/app/services/match_engine.py:277)。
- 三路基准不会在这两个上限为 8 的本地门上排队。因此三路 P95 `3.397s` 不能归因于当前本地 semaphore。
- 风险在于这两个上限只限制应用并发，不代表上游 GPU 能稳定承载 8 路。若上游实际最佳并发低于 8，当前配置会把排队转移到不可观测的上游，恰好符合响应头前 residual 增大的现象。
- Judge 与 Debate 共用 Agent 同一个 semaphore：[/Users/sunshiqi/code/phdebate/debate-agent/apps/api/app/agent_engine.py:251](/Users/sunshiqi/code/phdebate/debate-agent/apps/api/app/agent_engine.py:251)、[/Users/sunshiqi/code/phdebate/debate-agent/apps/api/app/agent_engine.py:285](/Users/sunshiqi/code/phdebate/debate-agent/apps/api/app/agent_engine.py:285)。正式赛结束时裁判长请求可能占用发言容量，但不是当前固定辩题基准的原因。

判定：**本地 gate 不是当前三路直接基准的瓶颈；上游容量与排队是未观测 P0。**

### 2. 模型服务与 GPU batching

- 当前模型是 `qwen3.7-plus`、thinking=false、基准 max_tokens=180。仓库只配置上游地址、模型名、重试和 RPM，未包含 GPU、vLLM/SGLang continuous batching、prefix caching、chunked prefill、`max_num_seqs` 或排队策略。
- TCP 只需 10–17ms，而响应头前需要 1.2–3.3s；该区间只能主要来自上游请求等待、prefill 和首 token 计算。
- 三路并发的 open residual P95 从新 client 的 `2.023s` 增至 `2.967s`，增加约 `944ms`，是明确的并发退化。
- 现有证据无法区分“云 API 队列”和“自部署 GPU batching/prefill”，因为上游没有回传 queue/prefill/TTFT server timing。

判定：**当前最大单点瓶颈。必须获得上游 queue、prefill、decode 指标，或迁移到可控模型服务后再调优。**

### 3. Prompt 长度和 prefill

- 默认 Prompt 模板本身不长：system 源模板 233 字符、user 源模板 146 字符：[/Users/sunshiqi/code/phdebate/debate-agent/apps/api/app/seed.py:20](/Users/sunshiqi/code/phdebate/debate-agent/apps/api/app/seed.py:20)。
- 真正可增长的是历史与记忆。默认 MemoryPolicy 允许 `max_context_chars=12000`、最近 16 条窗口：[/Users/sunshiqi/code/phdebate/debate-agent/apps/api/app/models.py:116](/Users/sunshiqi/code/phdebate/debate-agent/apps/api/app/models.py:116)。渲染时约 60% 给历史、每类记忆约 20%：[/Users/sunshiqi/code/phdebate/debate-agent/apps/api/app/agent_engine.py:175](/Users/sunshiqi/code/phdebate/debate-agent/apps/api/app/agent_engine.py:175)。
- 基准输入只有一条短历史且没有 `match_id`，不会加载比赛/长期记忆；即使如此新 client 首可播 P95 仍为 `2.519s`。所以“长 prompt”不是这两份固定输入基准的主要根因，但在比赛后半程会放大 prefill 尾延迟。
- 当前 system message 含辩手名、辩题、当前/下一环节等动态字段：[/Users/sunshiqi/code/phdebate/debate-agent/apps/api/app/seed.py:20](/Users/sunshiqi/code/phdebate/debate-agent/apps/api/app/seed.py:20)。这会缩短跨请求完全相同的稳定前缀，不利于上游 prefix cache。
- `prepare()` 每次请求分别加载配置和渲染上下文，至少打开两次数据库 session，并重新加载 Persona/Prompt/Preset/Policy/providers：[/Users/sunshiqi/code/phdebate/debate-agent/apps/api/app/agent_engine.py:101](/Users/sunshiqi/code/phdebate/debate-agent/apps/api/app/agent_engine.py:101)、[/Users/sunshiqi/code/phdebate/debate-agent/apps/api/app/agent_engine.py:132](/Users/sunshiqi/code/phdebate/debate-agent/apps/api/app/agent_engine.py:132)。Mem0 开启时其同步 `search()` 也位于渲染路径：[/Users/sunshiqi/code/phdebate/debate-agent/apps/api/app/agent_engine.py:167](/Users/sunshiqi/code/phdebate/debate-agent/apps/api/app/agent_engine.py:167)、[/Users/sunshiqi/code/phdebate/debate-agent/apps/api/app/mem0_bridge.py:47](/Users/sunshiqi/code/phdebate/debate-agent/apps/api/app/mem0_bridge.py:47)。

判定：**固定短基准不是 prompt-bound；真实长比赛存在明显 prefill 风险，且当前结构没有为 prefix cache 优化。**

### 4. max_tokens 与采样

- Agent 发送 `min(payload.max_token, preset.max_tokens)`：[/Users/sunshiqi/code/phdebate/debate-agent/apps/api/app/agent_engine.py:452](/Users/sunshiqi/code/phdebate/debate-agent/apps/api/app/agent_engine.py:452)。V2 按秒计算为 `min(900, max(120, duration*1.8))`：[/Users/sunshiqi/code/phdebate/v2/apps/api/app/services/match_engine.py:751](/Users/sunshiqi/code/phdebate/v2/apps/api/app/services/match_engine.py:751)。45 秒自由辩论通常为 120，120 秒环节约为 216，并不是默认 preset 的 700。
- 基准使用 180 tokens，输出约 101–118 个汉字。减小 max_tokens 主要减少总生成时间、KV 预算和上游调度压力，不足以解释响应头前 2–3 秒，但在高并发服务中可能改善调度尾延迟。
- 当前标准采样为 temperature 0.72、top_p 0.9：[/Users/sunshiqi/code/phdebate/debate-agent/apps/api/app/seed.py:92](/Users/sunshiqi/code/phdebate/debate-agent/apps/api/app/seed.py:92)。采样本身不是秒级 TTFT 根因。
- Qwen thinking 已由服务端预设强制控制，默认 false：[/Users/sunshiqi/code/phdebate/debate-agent/apps/api/app/agent_engine.py:469](/Users/sunshiqi/code/phdebate/debate-agent/apps/api/app/agent_engine.py:469)。发布证据已证明这是一次巨大收益修复，不应回退。

判定：**thinking 已修；max_tokens 应按环节继续收紧，但不是当前最大 P0。**

### 5. SSE flush、Nginx 与客户端复用

- Agent 每个上游 delta 立即向下游 `yield`，没有应用层聚合：[/Users/sunshiqi/code/phdebate/debate-agent/apps/api/app/agent_engine.py:488](/Users/sunshiqi/code/phdebate/debate-agent/apps/api/app/agent_engine.py:488)。
- StreamingResponse 带 `X-Accel-Buffering: no`，Nginx 也设置 `proxy_buffering off`：[/Users/sunshiqi/code/phdebate/debate-agent/apps/api/app/main.py:677](/Users/sunshiqi/code/phdebate/debate-agent/apps/api/app/main.py:677)、[/Users/sunshiqi/code/phdebate/debate-agent/deploy/nginx.same-host.locations.conf:1](/Users/sunshiqi/code/phdebate/debate-agent/deploy/nginx.same-host.locations.conf:1)。
- 三路模式从响应头到首可播 P95 仅 `480ms`；SSE 传输并没有制造 2–3 秒主延迟。
- V2→Agent 已复用 keep-alive 连接池：[/Users/sunshiqi/code/phdebate/v2/apps/api/app/services/providers.py:76](/Users/sunshiqi/code/phdebate/v2/apps/api/app/services/providers.py:76)。直接传输基准中持久连接没有更快，因此继续投入连接池优化不是 P0。
- Agent 的 `debate_api` 回退路径每次新建 AsyncClient：[/Users/sunshiqi/code/phdebate/debate-agent/apps/api/app/agent_engine.py:361](/Users/sunshiqi/code/phdebate/debate-agent/apps/api/app/agent_engine.py:361)。当前生产基准 provider_kind 为 `openai`，没有走该路径。

判定：**SSE/Nginx/client reuse 不是当前主瓶颈。**

### 6. Agent API 进程与同步工作

- Supervisor 只启动一个 Uvicorn worker：[/Users/sunshiqi/code/phdebate/debate-agent/deploy/supervisor.same-host.conf:1](/Users/sunshiqi/code/phdebate/debate-agent/deploy/supervisor.same-host.conf:1)。异步 SSE 本身不需要多个 worker；直接增加 worker 会把进程内 semaphore 从“总共8”变成“每个 worker 各8”，可能进一步压垮上游。
- 每个 delta 前都会调用 `_interrupted()`，其实现每次创建 Redis client、GET 后关闭：[/Users/sunshiqi/code/phdebate/debate-agent/apps/api/app/agent_engine.py:316](/Users/sunshiqi/code/phdebate/debate-agent/apps/api/app/agent_engine.py:316)、[/Users/sunshiqi/code/phdebate/debate-agent/apps/api/app/agent_engine.py:488](/Users/sunshiqi/code/phdebate/debate-agent/apps/api/app/agent_engine.py:488)。这会增加每块输出的往返开销和事件循环负载，但只发生在上游已经返回 chunk 后，不能解释响应头前的主延迟。
- RPM 检查也每次创建/关闭 Redis client：[/Users/sunshiqi/code/phdebate/debate-agent/apps/api/app/agent_engine.py:345](/Users/sunshiqi/code/phdebate/debate-agent/apps/api/app/agent_engine.py:345)。

判定：**应修，但收益主要在流畅性/吞吐，不是当前首可播 P95 的第一根因。**

## P0 修复清单（按预期收益、风险排序）

| 顺序 | P0 项 | 预期收益 | 风险 | 实施要点 | 验收证据 |
|---:|---|---|---|---|---|
| 1 | Agent delta 直接驱动 TTS | 三路并发按现有数据可提前约 1.45s；新 client 可提前约 1.73s | 中高：必须解决分词边界、取消、尾部 flush 和 TTS 失败后的完整正文保存 | MatchEngine 改用 `debate_agent.generate_stream()`；累计全文重新分词，保留末端不稳定 token；达到 10–16 汉字、强标点或 150–250ms 最大等待即首次 flush；Agent final 仍落库 | `Agent request start → TTS first PCM` P95，三路并发；音频/正文强制对齐无吞词 |
| 2 | 独立的低延迟“发言模型池”，与 Judge/后台任务隔离 | 目标消除当前 2–3s response-header residual；最大潜在收益约 1–2s | 中：模型质量和事实性需重新门禁 | 使用非 reasoning、小模型或明确低延迟档；持续预热；专用 endpoint/凭据/容量；Judge 保留大模型 | 30 次冷/热 + 10 波三并发，首 delta P95 ≤1.5s、max ≤2.0s |
| 3 | 让上游容量真实覆盖三路，并暴露 queue/prefill/TTFT | 消除三路并发相对单路约 0.94s 的 open-residual 退化 | 中：需要模型服务配置或供应商支持 | 自部署时启用 continuous batching、prefix caching、chunked prefill；根据实测设置 max_num_seqs/max_num_batched_tokens；外部 API 则要求 queue/TTFT 指标和稳定容量等级 | 三路首可播 P95 不高于单路 P95 +300ms；每次响应记录 queue/prefill/TTFT |
| 4 | 冷态消除和容量预热 | 可消除现有 6s 级偶发首轮长尾 | 低 | 服务启动 warmup；周期性极短非业务请求；模型实例缩容策略禁止冷启动进入正式赛流量 | 含冷态30次样本 max≤2.0s，实例切换时不中断 |
| 5 | 实时发言 Prompt fast path + 可缓存稳定前缀 | 短输入预计是百毫秒级；比赛后半程长上下文收益可能更大 | 中：过度裁剪会降低回应质量 | system 仅保留稳定规则/人格；题目、环节、历史放 user；发言路径只带最近关键轮次和短摘要；禁用实时长记忆检索或后台预取 | 分阶段 prompt token 数、prefill P95；长比赛回应质量回归 |
| 6 | 按环节收紧 max_tokens，并设首句长度契约 | 降低总生成和高并发调度压力；为 TTS 更快获得可播句 | 低中 | 自由辩论优先 80–120 token；Prompt 要求首句 12–24 汉字内形成完整观点和标点；不要仅依赖 max token 截断 | 首强标点 P95、截断率、完整语义人工抽检 |

### 不应作为 P0 的改动

- 继续调整 TCP keep-alive：握手 P95 只有 13–17ms，且持久 client 未改善结果。
- 增加 SSE 人工 flush/空白 padding：应用和 Nginx 已逐块无缓冲输出。
- 盲目增加 Uvicorn workers：会复制进程内并发门，可能加重上游排队。
- 只降低 temperature/top_p：不会解决响应头前数秒等待。
- 仅把 Agent semaphore 从 8 降到 3：如果上游只能串行，第三路仍会超时；必须先提供真实三路算力或动态 batching。

## P1/P2 后续项

1. 复用单个 Redis pool；interrupt 状态采用本地 Event + 低频 Redis 轮询，避免每个 delta 新建 client。
2. 缓存已发布且版本不变的 Persona/Prompt/Preset/Policy/provider 快照，后台管理更新时失效。
3. 合并 `_load_context` 与 `_render_messages` 的数据库读取，减少每请求 session/round trip。
4. 给 Agent stream 增加分阶段时间戳：`request_received`、`prepared`、`semaphore_acquired`、`upstream_headers`、`first_delta`、`first_playable`、`done`。
5. 将 Judge 与 Debate 拆成不同 semaphore 和不同 provider pool。
6. `debate_api` 回退路径改为进程级持久 AsyncClient；这是可靠性/小延迟优化，不是当前主因。

## 完成目标所需门禁

端到端 3 秒不能用 Agent 单点基准代替。至少需要：

1. 30 次串行样本，覆盖服务重启后的冷态、持续热态和实例切换。
2. 10 波三路同时请求，包含正式比赛长度的历史上下文。
3. Agent 首个稳定可播文本 P95 ≤ `1.5s`，max ≤ `2.0s`。
4. Agent request start → 浏览器 AudioWorklet 实际首声 P95 ≤ `3.0s`。
5. 三路同时播放整段无 underrun；正文和音频无吞词、重复、尾字丢失。
6. 将 queue、prefill、decode、TTS 首 PCM 和浏览器 buffer 分别记录，任何一次失败都能定位到具体阶段。

## 证据强度与限制

- 结论“网络连接和 SSE 不是主因”有直接 trace 支持。
- 结论“V2 等待 Agent 完整正文才启动 TTS”由当前源码直接证明。
- “上游 queue/prefill/GPU batching 中哪一个占比最高”目前无法细分；代码和产物没有上游 server timing。该项属于**明确定位到上游、内部成分待补测**。
- 每种传输模式只有 6 次，适合根因方向判断，不足以形成正式生产 SLO。
- 固定短输入没有覆盖比赛后半程的长 prompt；当前报告将其列为放大风险，未把它误判为短基准的已证实主因。
