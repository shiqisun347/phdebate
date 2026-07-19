# LightTTS bi-stream lifecycle 修复审计

审计对象：生产 `ModelTC/LightTTS` commit `ad1c76e36614a1aa26629bc11246884bb7c4072c`。本轮只读源码和既有 req246 日志，没有再次请求生产 TTS，没有修改或重启生产。

结论：req246 不是普通的“首 PCM 很慢”，而是两个问题叠加：

1. bi-stream 请求在 LLM prefill 返回后卡在首次 decode/output token 之前；精确的模型内部原因尚需 instrumentation。
2. API lifecycle 在 `finish` 后停止接收 WebSocket，导致断连、deadline 和 abort 都无法回收这个 silent worker；最终形成 `can_release=False / refcount=4` 的 orphan。

最小修复必须同时覆盖 API lifecycle 与模型 RPC fail-fast。只修 WebSocket 超时可以保护客户端，但不能保证已经卡在 CUDA/model RPC 内的共享内存安全释放。

## 已验证事实

req246 的最后成功阶段：

- 15:11:00.922：HTTP/WS manager 接收请求。
- 15:11:00.929：encode 接收。
- 15:11:00.934：semantic length 108、text length 36 发给 LLM。
- 15:11:00.936：LLM router 接收，bi-stream 初始 input length 为 1。
- 15:11:01.015：`Prefill Batch ... req_ids:[246]`，说明 `model_rpc_client.prefill()` 已返回。
- 后续没有 `tts_llm Send`、decode manager token、token2wav、PCM 或 finalize。
- 客户端关闭后仍每秒空轮询，并每 20 秒报告 `can release False refcount 4`，直至单服务重启。

原始证据：[req246 timeline](production-bistream-short-clause/evidence/req246-timeline.log)、[生产基准报告](production-bistream-short-clause/README.md)。

## 当前代码的 lifecycle 缺口

### 1. `finish` 后断连不可观察

[api_http.py](production-bistream-short-clause/source/api_http.py) 收到 `finish` 后立即 break，随后只执行 `await process_task`。`WebSocketDisconnect` 只能在前面的 `receive_json()` 抛出，但 finish 后不再调用 receive。

如果模型没有任何输出，`send_wav` 也不会调用 WebSocket send，因此客户端关闭在服务端两个方向都不可见。`finally` 又必须等 `process_task` 返回后才能执行，形成循环等待。

### 2. 没有 first-PCM 和 whole-session deadline

当前 endpoint 可无限等待 process task。V2 侧即使取消调用，也只能关闭 socket；服务内部仍可能保留请求。deadline 必须在 LightTTS 服务端存在，不能只依赖外层调用者。

whole-session deadline 不应固定为过短常数。建议：

- first PCM 硬门：3 秒，另按健康 p99 留少量内部容差。
- whole session：由调用方 deadline 与基于文本长度的服务端上限取较小值，例如 `min(caller_deadline, base + visible_chars * seconds_per_char)`，并设置合理的 30–240 秒边界。
- abort grace：约 2 秒；超出即判 orphan 并进入 fail-fast 恢复。

### 3. finish 标记没有使用共享请求锁

[httpserver-manager.py](production-bistream-short-clause/source/httpserver-manager.py) 为追加文本获取 `get_req_lock_by_index`，但 `finish` 直接写 `req.bistream_input_finished=True`。该字段会被 LLM 调度进程读取，用来决定 WAIT_FOR_TEXT、append-prefill 和 EOS 行为。应在同一请求锁下写 finish，并记录 `input_finished_at` 和 stage，避免跨进程状态可见性与竞态不明确。

这是一项高价值修复，但当前证据不足以断言它就是 req246 first-decode hang 的唯一原因。

### 4. abort 只写 flag，没有 release SLA

manager 的 abort 只设置 `req.is_aborted=True`。资源回收仍依赖 model/router/decode 将 `can_released_mark`、refcount 和输出状态推进到可释放条件。

如果 model RPC 正卡在 prefill/decode，API 不能安全地自行把共享内存索引放回池中；强制复用仍被模型进程引用的 slot 会造成跨请求污染或崩溃。因此需要：

- 幂等 `abort(request_id, reason)`；
- `wait_released(request_id, abort_grace)`；
- grace 超时标记 orphan；
- orphan 时 fail-fast 重启完整 LightTTS 进程组，而不是伪造 release。

### 5. router/model RPC 没有 timeout

[tts-llm-manager.py](production-bistream-short-clause/source/tts-llm-manager.py) 直接 await `model_rpc_client.prefill()` 和 `decode()`。req246 的 prefill 已返回，第一处缺失边界是后续 decode。

应分别给 prefill/decode 增加 request/batch-scoped watchdog，日志至少包含 batch id、request ids、stage、elapsed、GPU memory 和当前 bi-stream status。

只让 LLM router 子进程抛异常或退出不够：`api_start` 只在启动时确认子进程 alive，之后没有父进程持续监控子模块。router 单独死亡会留下仍在监听但不可用的 HTTP 服务。因此 model RPC timeout 必须触发完整服务进程组退出，让 Supervisor autorestart。

### 6. readiness 是假绿

req246 存在期间：

- V2 ready 为 true；
- LightTTS gate 为 0/0；
- TCP 8080 可连接；
- 实际唯一模型槽被 orphan 占用。

LightTTS 应新增不做合成的 `/health/live` 和 `/health/ready`：

- live：HTTP worker/event loop 存活。
- ready：子进程 heartbeat 正常、内部 active count、oldest age、first-PCM stalled、whole-session stalled、abort pending、orphan count 均通过。

V2 readiness 应读取 LightTTS ready，而不是只测端口。不得用真实合成作为高频 readiness。

## 最小 patch 分层

| 修复 | API/HTTP 层可完成 | 需要模型/Router 代码 | 需要完整 LightTTS 重启加载 |
|---|---:|---:|---:|
| first PCM deadline | 是 | 否 | 是 |
| 动态 whole-session deadline | 是 | 否 | 是 |
| finish 后并行监听 disconnect | 是 | 否 | 是 |
| finally 中 guaranteed abort + task cancel | 是 | 否 | 是 |
| finish 字段加共享请求锁 | manager/shared request | LLM 会读取 | 是 |
| abort 后等待真实 release | manager | 否 | 是 |
| stuck prefill/decode watchdog | 否 | 是 | 是 |
| 卡死后安全恢复 | API 可发起 fail-fast | 必须终止仍持有 GPU/shared state 的模型进程 | 是，且应重启完整进程组 |
| internal active/orphan ready | HTTP manager 可汇总基础状态 | 精确 stage/heartbeat 需 Router/Decode 上报 | 是 |
| V2 消费新的 ready | V2 API | 否 | 需要 V2 发布/重启 |

“需要重启”是指代码部署后必须重启服务加载新代码；对于已发生且 abort grace 内不释放的请求，也必须运行时重启完整 LightTTS 进程组。模型权重不需要修改。

## 推荐控制流

```text
accept → init/prompt → first text → append... → finish
                                   │
                                   ├─ audio worker
                                   ├─ disconnect watcher（finish 后仍保留）
                                   ├─ first-PCM deadline
                                   └─ whole-session deadline

任一失败
  → idempotent abort(reason)
  → cancel/await local tasks
  → wait real shared-state release (abort grace)
       ├─ released: close session and report error
       └─ not released: mark orphan/readiness=503 → fail-fast full service restart
```

正常完成也必须在 finally 中 await 所有 watcher/task，避免后台任务泄漏。

## 模型阶段 instrumentation

每个 request 至少记录以下单调时间：

1. accepted
2. first_text_received
3. append_received
4. finish_received
5. encode_start / encode_done
6. prefill_start / prefill_done
7. first_decode_start / first_decode_done
8. fill_token / WAIT_FOR_TEXT
9. append_prefill_start / done
10. first_llm_output
11. first_token2wav_start / first_pcm
12. final_pcm / release
13. abort_requested / abort_observed_by_router / released

req246 目前只能定位到 6 与 7 之间。没有这些字段前，不应把根因武断归为 TRT、CUDA、短文本、finish race 中的任意一个。

## 补丁草案与 fake 验证

- 集成草案：[lighttts-bistream-lifecycle-fix.patch](lighttts-bistream-lifecycle-fix.patch)
- 可执行参考状态机：[lighttts-bistream-lifecycle-reference.py](lighttts-bistream-lifecycle-reference.py)
- 纯 fake 测试：[test_lighttts_bistream_lifecycle_fake.py](test_lighttts_bistream_lifecycle_fake.py)
- 测试输出：[fake test output](lighttts-bistream-lifecycle-fake-test-output.txt)

fake 测试覆盖：

- 正常首 PCM + 完成不触发 abort；
- finish 后断连触发 abort 并释放；
- first PCM timeout；
- 已有首 PCM 后 whole-session timeout；
- abort grace 后仍未释放则要求完整进程重启；
- readiness 对 first-PCM stall 和 orphan 返回失败，release 后恢复。

执行结果：6/6 通过。测试不导入生产 LightTTS、不访问网络、不使用 GPU。

## 发布门

建议顺序：

1. 在独立 LightTTS 实例应用 patch，先用 fake + 单元测试验证 lifecycle。
2. 用故障注入让 prefill/decode 永不返回，确认 first-PCM timeout、abort grace、readiness 503 和 Supervisor 自动恢复。
3. 验证正常长发言不会被 whole-session deadline 误杀。
4. 再执行 10/12/16 字各两次单路与两波三并发，验证 0 orphan、0 ghost shared-memory slot。
5. 最后才进行 FunASR CER、末词、重复、AudioWorklet underrun 和 30 分钟多轮音色漂移测试。

在上述门槛全部通过前，生产 `LIGHTTTS_STREAMING_ENABLED` 应保持关闭。
