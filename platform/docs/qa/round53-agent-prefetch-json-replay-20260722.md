# Round 53：Agent 预生成 JSON 回放兼容修复

日期：2026-07-22

## 生产现象

生产引擎曾记录：

```text
Agent text prefetch failed room=349807 error_type=StreamConsumed
```

该异常不会直接破坏当前阶段，但会使下一位 AI 辩手的预生成失效；进入下一阶段后只能重新等待 Agent 完整生成，增加无意义的现场停顿。

## 根因

平台请求 Agent 时设置了 `output.stream=true`，旧代码因此直接按 SSE 读取响应，即使网关实际返回的是 `application/json`。

独立 Agent 的幂等任务已经完成时，可以合法地直接返回缓存 JSON。旧代码先通过 `aiter_lines()` 消耗 JSON 响应，发现没有 SSE delta 后又调用 `aread()`，由 httpx 抛出 `StreamConsumed`。

## 修复

- 响应解析方式只以实际 `Content-Type` 为准。
- `text/event-stream` 使用增量 SSE 解析。
- `application/json` 使用单次 JSON 读取，即使请求原本希望流式输出。
- 不改变正常真流式 Agent → TTS 主链路。

## 自动化证据

- 新增 `test_agent_provider_accepts_json_idempotency_replay_when_stream_was_requested`。
- Agent Provider 专项：`9 passed`。
- Ruff：通过。

