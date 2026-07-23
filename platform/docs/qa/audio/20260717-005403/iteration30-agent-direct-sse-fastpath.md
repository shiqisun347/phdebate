# Iteration 30：Debate Agent 原始 SSE 低延迟主路径

执行时间：2026-07-18 00:19–00:46 CST。证据类型：`newly_run`。

## 结论

Debate Agent 的主要额外延迟来自 LiteLLM 流式适配，不是 V2→Agent 网络、Redis 或 Prompt
准备。生产已将辩手的延迟合格 OpenAI-compatible 模型改为原始 SSE；裁判与非流式请求继续
使用 LiteLLM。Iteration 30 发布当时曾将 `qwen3.6-flash` 作为灰度 canary；**当前生产主模型已为
`qwen-plus`**，`qwen3.6-flash` 的下列 6 次数据仅保留为历史发布证据。

历史发布后三路、两波共 6 个 `qwen3.6-flash` 真实 Agent API 请求：

- 首正文 P95：`2.352s`。
- 首 10 个汉字 P50/P95/max：`1.876/2.398/2.466s`。
- 完整响应 P95：`3.985s`。
- 6/6 completed；无错误、无 `<think>` 泄露，输出均为正常中文辩论正文。

2026-07-18 02:01 CST 对当前 `qwen-plus` 完成 10 波×3 并发、共 30 个无业务写入的直接上游
SSE 复测：首正文 P50/P95/max 为 `1.067/2.779/2.962s`，首 10 个汉字为
`1.404/3.271/3.272s`，完整响应为 `2.585/4.233/4.347s`。30/30 收到 `[DONE]`，thinking
泄露为 0。第 1 波三路共同出现空闲冷态；排除该波后的 27 个热态样本，首正文 P95/max 为
`1.485/2.076s`。因此 startup-only warmup 不能证明可跨约 44 分钟空闲持续保温。

该复测从 LLM 请求开始计时，不是“LLM 首字→浏览器实际出声”指标；TTS endpoint、传输、
AudioWorklet 和真实播放仍未合并验收，因此总发布结论仍为 NO-GO。完整证据见
[post-warm 30 次复测](agent-postwarm-30/README.md)。

## 根因分解

使用相同生产 Prompt、同一主机、同一上游 gateway 和 `qwen-plus` 三路对照：

| 路径 | 首 10 字 P95 |
|---|---:|
| 原始 OpenAI SSE | 1.675s |
| LiteLLM `acompletion(stream=True)` | 3.374s |
| 旧 Agent API（LiteLLM） | 3.451s |
| 新 Agent API（原始 SSE，qwen-plus，首波） | 3.039s |
| 新 Agent API（原始 SSE，qwen-plus，第二波） | 2.832s |

LiteLLM 在该 gateway 上直到首内容出现才返回 streaming response object；原始 `httpx.stream`
可以立即持有连接并消费 SSE。差值约 1.7 秒。Redis 新连接 8 次总计 `7.2ms`，持久连接 8 次
总计 `1.1ms`，不是秒级差值来源。

## 模型候选实测

历史 Qwen3 候选使用当时生产 Prompt、`enable_thinking=false`、180 tokens 上限；当前
`qwen-plus` 复测读取生产 Prompt/preset/Provider，并只统计可见正文 delta。

| 模型/模式 | 首 10 字 P50/P95/max | 完整 P95 | 判断 |
|---|---:|---:|---|
| qwen3.6-flash 热态串行 8 次 | 1.090/1.240/1.256s | 3.486s | 最快热态 |
| qwen3.6-flash 原始三路 6 次 | 1.477/1.890/1.935s | 11.143s | 首段快，曾有完整长尾 |
| qwen-plus 热态串行 8 次 | 1.386/1.994/2.127s | 4.245s | 稳定但串行稍慢 |
| qwen-plus 原始三路 6 次 | 1.547/1.675/1.685s | 4.495s | 三路稳定 |
| qwen3.6-flash 新 Agent API 三路两波 | 1.876/2.398/2.466s | 3.985s | 历史发布 canary，非当前模型 |
| qwen-plus post-warm 10 波×3 并发 | 1.404/3.271/3.272s | 4.233s | 当前生产模型；首波存在空闲冷态 |

`qwen3.6-flash` 的第一次独立串行探针曾出现 4.455 秒首句；当前 `qwen-plus` 30 次复测的第 1
波也出现首正文最高 2.962 秒、首 10 字约 3.272 秒的共同冷态。外部模型仍有冷态/排队风险，
不能把历史 6 次 canary 或排除首波后的热态子集当作长期 SLO。

## 代码修复

- `DebateAgentEngine._stream_openai_api()` 直接解析 OpenAI SSE，忽略 `reasoning_content`，仅转发
  `delta.content`，并保存 usage/final。
- 只有 `DIRECT_OPENAI_STREAM_MODELS` 明确列出的模型走该路径；当前默认
  `qwen-plus,qwen3.6-flash`。Judge 和其他模型保留 LiteLLM 兼容路径。
- Agent 复用有界 HTTP keep-alive client；Debate REST fallback 也不再每请求新建 client。
- Redis interrupt/RPM 使用进程级连接，interrupt 每个 task 最多每 100ms 查询一次；显式 interrupt
  会立即把本地 cache 置为 true。
- 主 Provider 已输出部分正文后若流中断，返回 `partial_stream_failed` 并停止，禁止把第二 Provider
  的文本拼接到可能已经播放的前缀。
- FastAPI shutdown 显式关闭 HTTP/Redis client。

## 验证与发布

- Agent 自动化：`12 passed, 1 warning`。
- 本地 Ruff、py_compile：通过。
- 历史发布后 Agent API 三路两波：6/6 completed、无 think、无 error。
- 当前 `qwen-plus` post-warm 10 波×3：30/30 completed、无 think、首 10 字 P95 `3.271s`；
  AgentTask `118→118`、running `0→0`、MemoryItem `84→84`，每波前均确认无活动比赛。
- Agent health：ready；database/redis/2 active providers 均 ok。
- V2 ready：ok；`active_match_processing=false`。
- `jixia-agent-api` 仅此服务重启；V2 API/Engine/Worker/Web PID 与 uptime 未变化。

备份与回滚：

- Agent DB：`/home/ubuntu/sunsq/debate-agent/backups/agent-20260717T161955Z.dump`，SHA-256 `ee8be092410474d7a41f429aca30c6991708fb1a06a49f492315b7bfe5781d45`
- Provider 配置快照：`/home/ubuntu/sunsq/debate-agent/runtime/provider-before-qwen-plus-20260717T161956Z.json`
- 源码包：`/home/ubuntu/sunsq/debate-agent/runtime/deploy-backups/20260717T163004Z-before-direct-sse.tar.gz`，SHA-256 `5b5bf99846a4cfa83965c1a3b7ef0d6c7cced573b59084af288bf1d64b6676f0`
- 当前发布包：`/home/ubuntu/sunsq/debate-agent/runtime/releases/20260718T0030-agent-direct-sse.tar.gz`，SHA-256 `0a3a1a293db5e271b13f582d533cb2314e2da6f47895121f94b63a838d966098`
- 历史回滚模型：`qwen3.7-plus`；当前模型：`qwen-plus`。

## 剩余门禁

1. 30 次、10 波三路门已完成；下一步验证持续保温或专属推理容量能否消除长空闲后的首波冷态。
2. 独立 GPU MOSS endpoint 到位后，从回合提交计到 Chrome/Safari 实际非静音采样，验证 P95≤2.8s、max≤3s。
3. 4v4 全赛制覆盖立论、质询、自由辩论、总结，检查当前 `qwen-plus` 的事实谨慎性、回应质量和跨轮一致性。
4. 真实 2–3 场同时跑满 Agent+TTS，验证完整响应长尾不会占满 Agent semaphore 或拖住后续回合。
