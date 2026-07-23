# Agent 传输与上游排队根因分解

执行时间：2026-07-17 22:45–22:46 CST。证据类型：`newly_run`。模式：`minimal-reproducible-run`。

## 结论

**连接池或简单预热不能把当前 Agent 首可播片段 P95 压到 1.25 秒。** 当前主要延迟不在 TCP/TLS 建连，而在请求发出后等待上游响应头和正文生成；3 路并发时该等待明显恶化。

- A：每次新建 HTTP client，首可播 P50/P95/max 为 `1.808/2.519/2.535s`。
- B：单一持久 keep-alive client，首可播为 `2.970/3.992/4.276s`。第 1 次建立连接，后 5 次均确认复用，但没有获得延迟改善。
- C：两波 3 路同步请求，首可播为 `2.884/3.397/3.517s`。第 2 波三路均复用第一波建立的连接，仍未接近 1.25 秒。
- TCP connect P95 仅为 A `16.6ms`、B 首连 `10.2ms`、C 首波 `13.2ms`；当前 Provider 链没有 TLS trace 事件。连接耗时相对 2–4 秒首可播延迟可以忽略。
- A 的 HTTP open 残余等待 P95 为 `2.023s`，C 为 `2.967s`。该残余包括请求写入与上游响应头等待；结合 3 路并发的同步延迟，证据与上游 Gateway/推理调度排队一致。
- 未发现固定“每组首请求必慢”的冷态：A 最慢是第 6 次，B 第 1 次反而最快、最慢为第 2 次。此前出现的 6 秒首轮更像非确定性上游负载或排队长尾，而不是可由一次固定 warm-up 消除的稳定冷启动。

因此目标判定：

- `persistent all P95 <= 1.25s`：FAIL
- 排除持久客户端首轮后 `warm P95 <= 1.25s`：FAIL，实际 `4.049s`
- 连接池 + 预热足以解决：**证据否定**
- Claim Readiness：关于“建连不是主因、当前连接池/预热不足”的结论为 `paper_ready`；关于上游内部具体是哪一级队列仍为 `weaken_claim`，需要 Provider 侧排队与 GPU 调度指标进一步区分。

## 协议

- 聚合单位：单次独立流式请求。
- 每组样本：6；总有效请求 18。
- 固定输入、Prompt、模型 preset、Provider 与上一轮 Agent 增量基准一致；模型 `qwen3.7-plus`，`enable_thinking=false`，180 tokens 上限。
- A `new_client`：每次请求新建并关闭 `httpx.AsyncClient`。
- B `persistent`：同一个 client 串行完成 6 次请求，连接池上限为 1。
- C `concurrent_3`：同一个 client、连接池上限 3；执行两波，每波同时发起 3 个请求。
- 每个响应在收到 `[DONE]` 后继续排空到 EOF，确保连接可以返回池中，避免探针主动破坏 keep-alive。
- TCP connect、TLS 由 `httpcore` trace 事件实测；`HTTP open` 是发起请求到收到响应头；`open residual = HTTP open - TCP - TLS`。
- 首 delta 是首个可见正文；首可播是正文首次出现 `。！？；` 或半角等价强标点；full 是响应完全结束。
- P95 使用请求级样本线性插值。

## 汇总

| Mode | 新建连接数 | TCP connect P50/P95/max | HTTP open P50/P95/max | 首 delta P50/P95/max | 首可播 P50/P95/max | full P50/P95/max |
|---|---:|---:|---:|---:|---:|---:|
| A 新 client | 6/6 | 11.4/16.6/17.2ms | 1.549/2.035/2.054s | 1.670/2.426/2.470s | 1.808/2.519/2.535s | 3.139/4.250/4.596s |
| B keep-alive | 1/6 | 首连 10.2ms | 2.738/3.269/3.313s | 2.738/3.631/3.795s | 2.970/3.992/4.276s | 3.947/4.880/5.000s |
| C 3 路同步 | 3/6 | 首波 12.8/13.2/13.2ms | 2.610/2.980/3.040s | 2.693/3.157/3.276s | 2.884/3.397/3.517s | 4.193/4.851/4.853s |

当前 Provider trace 中没有 `start_tls` 事件，因此 TLS 统计为 `count=0`，不是把未知值写成 0ms。

## 逐次证据

### A：每次新建客户端

| Run | TCP | HTTP open | open residual | 首 delta | 首可播 | full |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 14.8ms | 1.979s | 1.965s | 2.470s | 2.471s | 3.210s |
| 2 | 17.2ms | 1.386s | 1.368s | 1.626s | 1.627s | 3.035s |
| 3 | 11.3ms | 1.713s | 1.702s | 1.713s | 1.889s | 3.069s |
| 4 | 10.7ms | 1.328s | 1.317s | 1.328s | 1.537s | 2.839s |
| 5 | 9.9ms | 1.205s | 1.195s | 1.445s | 1.726s | 3.212s |
| 6 | 11.6ms | 2.054s | 2.042s | 2.294s | 2.535s | 4.596s |

### B：单一持久客户端

| Run | 连接 | HTTP open | 首 delta | 首可播 | full |
|---:|---|---:|---:|---:|---:|
| 1 | 新建，10.2ms | 1.052s | 1.090s | 1.853s | 3.031s |
| 2 | 复用 | 3.313s | 3.795s | 4.276s | 5.000s |
| 3 | 复用 | 2.087s | 2.125s | 2.428s | 3.445s |
| 4 | 复用 | 2.833s | 2.834s | 3.058s | 4.518s |
| 5 | 复用 | 3.138s | 3.138s | 3.138s | 3.904s |
| 6 | 复用 | 2.642s | 2.643s | 2.883s | 3.990s |

持久客户端的第 1 次是本组最快请求，明确反驳“只要固定预热一次，后续必然变快”的假设。

### C：两波 3 路同步请求

| Run | Wave | 连接 | HTTP open | 首 delta | 首可播 | full |
|---:|---:|---|---:|---:|---:|---:|
| 1 | 1 | 新建 | 3.040s | 3.276s | 3.517s | 4.853s |
| 2 | 1 | 新建 | 2.801s | 2.801s | 3.038s | 4.845s |
| 3 | 1 | 新建 | 2.799s | 2.799s | 2.800s | 4.074s |
| 4 | 2 | 复用 | 2.420s | 2.586s | 2.901s | 4.312s |
| 5 | 2 | 复用 | 2.420s | 2.586s | 2.867s | 4.071s |
| 6 | 2 | 复用 | 2.420s | 2.421s | 2.659s | 4.071s |

第二波三条复用连接的 HTTP open 几乎同时为 `2.420s`，而 TCP/TLS 为 0；这是同步等待上游调度或批处理的强信号。它不能单独证明 Provider 内部队列实现，但可以排除客户端连接建立是这段等待的来源。

## 根因判断

### 连接建立抖动：否

TCP 建连集中在约 10–17ms，未出现秒级长尾。A 的 TCP P95 只占 HTTP open P95 的约 0.8%。当前链路没有 TLS trace，因此也不存在可通过 TLS session reuse 回收的秒级预算。

### 固定冷态：未观察到

三个模式都没有“每组第 1 次固定最慢”的模式。B 第 1 次虽承担唯一建连，反而首 delta 和 HTTP open 最快。一次固定 warm-up 不能解释或消除后续第 2、4、5 次的长等待。

### 上游排队或推理调度：证据支持

这是基于 trace 的工程推断：主要时间位于连接完成后到响应头/首正文之间；并发 3 路时 HTTP open 和首可播增加；第二波复用连接仍产生三路同步等待。要区分 API Gateway 排队、模型实例排队、continuous batching 或 Provider 限流，需要上游 request queue、batch formation、prefill 和 first-token 服务端时间戳。

## 安全与生产核对

- 有效运行开始、结束时主站活动比赛均为 0，Agent running task 为 0。
- AgentTask `43→43`，MemoryItem `84→84`。
- 未调用 Agent 业务控制平面，未创建 AgentTask、Memory、房间或比赛。
- 未修改、重启任何服务；`jixia-agent-api`、V2 API/Engine/Worker/Web 全程保持 `RUNNING`。
- V2 health 为 ok，Agent health 为 ready。
- 原始 JSON 与报告未保存实际 Provider endpoint、密钥或 Gateway secret。

## 无效 pilot 说明

正式结果前有两个未纳入统计的 pilot：第一个因 TLS 指标为空导致汇总脚本报错，没有生成结果；修正空指标后，第二个 pilot 在 `[DONE]` 时提前退出响应，导致连接无法返回池中。发现该测量偏差后，探针改为继续读取至 EOF并完整重跑 A/B/C。本文和 JSON 只使用最终有效重跑。

## Evidence Inventory

- Status：`newly_run`。
- Command：生产机本地执行 `run_transport_root_cause.py --samples 6`；凭据未回显。
- Workdir：生产 Agent 当前虚拟环境；产物保存至本地 QA 目录。
- Inputs：固定短辩题和生产当前 Agent 配置。
- Output Artifacts：[原始 JSON](agent-transport-root-cause.json)、[可复核脚本](run_transport_root_cause.py)。
- Metrics Used：TCP/TLS、HTTP open、open residual、首 delta、首强标点可播、full 的请求级 P50/P95/max。
- Claim Readiness：连接根因结论 `paper_ready`；上游内部队列层级 `weaken_claim`。

## Protocol Risks / Remaining Blockers

1. 每组 6 个样本适合根因诊断，不足以定义生产 SLO；需要随机交错 A/B 顺序并各跑至少 30 次，降低时间段负载偏差。
2. A→B→C 顺序固定，B 的绝对值可能受到前序请求和时段负载影响；因此不能声称 keep-alive 导致变慢，只能证明本轮未带来改善。
3. `open residual` 还包括请求发送，不等于纯服务器 queue time；但请求体固定且 TCP 已单独计时，秒级差异不可能由 10ms 建连解释。
4. 直接上游请求绕过 Agent API scheduler、RPM 计数和 fallback；这是满足无业务写入边界的必要限制。
5. 若目标要求首可播 P95 ≤ 1.25s，需要替换或自托管能提供服务端排队指标、低 TTFT SLO 和 3 路并发保障的推理服务；仅调整 HTTP client 无法达到。
