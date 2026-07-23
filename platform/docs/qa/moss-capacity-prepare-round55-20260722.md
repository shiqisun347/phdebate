# Round 55：MOSS 实时语音容量前置门控回归

日期：2026-07-22

## 结论

实时语音运行时现在必须先完成 MOSS 容量预占、endpoint readiness、WebSocket 连接、上游 `ready` 契约以及 LiveKit 房间准备，随后才允许比赛引擎开始消费 Agent 文本流。并发房间不会再先暴露 Agent 首字、再在单卡 TTS 队列中等待几十秒。

本报告验证的是调用时序和资源释放不变量；真实服务器上的“Agent 首字到浏览器首音小于 3 秒”仍需使用同一 `speech_id` 的端到端生产遥测单独验收。

## 新增回归覆盖

1. `LightTTSRuntime.open_session()` 阻塞到 provider `prepare()` 完成，未就绪时不返回会话。
2. TTS 未 ready 时，`IncrementalVoicePipeline` 不创建或消费 Agent 的第一个事件。
3. MOSS WebSocket `prepare()` 在收到上游 `ready` 前保持阻塞；ready 后仍不发送任何 `text_delta`。
4. 单 endpoint 已被第一房间预占时，第二房间 `prepare()` 排队且不会创建第二个上游 turn。
5. 第一房间在 prepare 后被暂停/取消时，会执行 abort、收到释放确认并归还 endpoint；第二房间随后才能完成 prepare。
6. prepare 期间取消会终止后台 provider job，不遗留运行任务或锁定 semaphore。
7. provider 在 ready 前失败时，原始 `ProviderError` 原样传播。
8. `open_session()` 的 prepare 失败或取消都会调用底层 abort；即使 abort 自身也失败，仍保留原始异常或 `CancelledError`。
9. 旧的“上游仍 active”测试已按新时序校正：错误在准备/首次 push 阶段暴露，不再延迟到 `finish()`。

## 自动验证

### 新增门控测试

```text
8 passed, 103 deselected
```

覆盖 `prepare`、Agent iterator 门控、失败清理和容量释放。

### MOSS 与统一运行时相关回归

```text
33 passed, 78 deselected
```

覆盖 WS/HTTP、连接池、readiness、取消、超时、增量会话和统一运行时。

### Provider 与语音运行时完整回归

```text
111 passed
```

唯一输出为测试依赖 `fastapi.testclient` 的既有弃用 warning，不影响本轮行为。

### 静态检查

```text
ruff: All checks passed
```

## 必须持续保持的时序

```text
预占全局容量
→ 选择并锁定已暖机 endpoint
→ 建立/复用 MOSS WebSocket
→ 校验上游 ready 与音频格式
→ 准备 LiveKit 房间
→ prepare 返回
→ 开始消费 Agent 流
→ 首个稳定文本块进入同一 TTS context
→ 连续 WebRTC 音轨播放
```

任何后续重构若让 Agent iterator 在 `prepare()` 返回前被消费，都应视为首音 SLA 的回归。
