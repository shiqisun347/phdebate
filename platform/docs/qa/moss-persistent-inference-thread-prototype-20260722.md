# MOSS 持久推理线程原型审计（2026-07-22）

## 结论

当前 `reduce-overhead` 崩溃的直接触发条件已经定位并做了最小原型修复：旧实现分别通过 `asyncio.to_thread` 完成模型启动和预热，再为每次发言创建新的 session worker 线程执行 CUDA Graph。PyTorch CUDA Graph 的部分状态是线程局部状态，因此预热捕获线程与正式回放线程不同，会触发 `torch._inductor.cudagraph_trees` 的 TLS 断言。

原型在网关内部新增单线程 `PersistentBackendExecutor`。以下操作现在固定在同一个持久线程执行：

- backend startup；
- 八音色 warmup 和 CUDA Graph 首次捕获；
- open turn；
- initial audio、text delta、finish 的生成器创建与完整迭代；
- codec context close；
- backend shutdown。

每场发言原有 session worker 仍只负责协议命令、ACK 和音频队列，不直接进入模型或 CUDA 路径。打断信号允许跨线程设置取消事件，以便正在运行的生成立即观察到取消；实际 abort/close 清理仍回到持久推理线程串行执行。

本地阶段只证明线程一致性；随后已在生产 RTX 3090 的空闲窗口完成部署和真实 GPU 灰度，结果见下文。

## 代码范围

- `platform/services/moss-realtime-gateway/moss_realtime_gateway/runtime.py`
  - 新增 `PersistentBackendExecutor`；
  - startup、warmup、turn、shutdown 统一调度；
  - 确保惰性 CUDA 音频迭代也发生在推理线程；
  - 健康状态增加 `backend_execution_thread_alive`。
- `platform/services/moss-realtime-gateway/tests/test_gateway.py`
  - 新增生命周期线程一致性测试；
  - 同时验证生成器函数体（不只是函数调用）运行在同一线程；
  - 断线测试改为等待权威 `worker_exited`，不再把 codec context 退出误当成完整 release ACK。

未修改 API、Web 或比赛状态机；生产只切换了 MOSS 网关的推理线程实现和 local compile 开关。

## 本地验证

```text
pytest: 48 passed, 1 warning
ruff check: passed
ruff format --check: passed
openmoss deployment static checks: passed
```

测试覆盖正常流式增量、最终生成、关闭、主动打断、WebSocket 断线、并发 terminal 命令、超时孤儿保护、上下文退出和单活动会话约束。

## 生产 RTX 3090 灰度结果

生产配置：

```text
MOSS_GATEWAY_ATTN_IMPL=sdpa
MOSS_GATEWAY_LOCAL_COMPILE_MODE=reduce-overhead
MOSS_GATEWAY_LOCAL_COMPILE_DYNAMIC=false
```

- 八音色完整预热约 `337 s`，低于 600 秒门限；启动期间无 OOM、CUDA Graph TLS 断言、RecompileLimitExceeded 或 orphan。
- readiness 明确报告 `local_compile_effective=true`、`backend_execution_thread_alive=true`、8 个 prompt 全部缓存，最终 `active=0 / pending=0 / orphan=0`。
- 同音色真实 WebSocket 3 轮：首个非静音 PCM P95 `798 ms`，active RTF P95 `0.989`、最大 `0.994`，3/3 close ACK、3/3 release，1200 ms 连续播放模型 0 underrun。
- 八音色各 1 轮交替：8/8 成功、8/8 final 前产生 PCM、8/8 close/release；首个非静音 PCM P95 `581 ms`，active RTF P50/P95/最大为 `0.979/0.995/0.996`，1200 ms 连续播放模型 0 underrun。
- 灰度结束后 API readiness 为 200，推理线程仍存活，endpoint 无活动、排队或孤儿任务。

因此持久推理线程已经修复原 CUDA Graph TLS 崩溃，并使当前 SDPA 路径达到严格的 `active RTF < 1`。不过性能余量仍很薄，尚未达到本文建议的 P95 `< 0.90`；24 轮八音色长文本 soak、真人听测和浏览器长发言端到端仍是后续质量门，不能把本轮结果表述为“任何文本都绝不卡顿”。

## GPU 灰度门禁

必须在独立 canary 端口运行，不能先覆盖当前生产进程。建议使用生产同款 RTX 3090、同一固定模型/codec revision，并设置：

```text
MOSS_GATEWAY_ATTN_IMPL=sdpa
MOSS_GATEWAY_LOCAL_COMPILE_MODE=reduce-overhead
MOSS_GATEWAY_LOCAL_COMPILE_DYNAMIC=false
```

只有同时满足以下门禁才允许替换生产：

1. readiness 显示 `local_compile_effective=true`、`local_compile_mode=reduce-overhead`、`backend_execution_thread_alive=true`，且 active/pending/orphan 均为 0；
2. 启动和八音色预热不超过 600 秒，日志中没有 `cudagraph_trees`、`_is_key_in_tls`、recompile、OOM 或 cache-limit 错误；
3. 八个音色分别连续运行至少 3 次长文本，共至少 24 轮；所有轮次 active RTF < 1.0，P95 建议 < 0.90，为浏览器 1.1 倍播放留出余量；
4. 从首个文本 delta 到首个 PCM 的服务端 P95 < 2.3 秒，端到端浏览器首声 < 3 秒；
5. 1.1 倍播放、1200 ms 起播缓冲下不得出现 underrun，连续 PCM 不得出现异常零段、重复帧、撕裂或超过 250 ms 的非语言空洞；
6. 至少 20 轮交替音色 soak 后 GPU 已分配显存增长 < 500 MiB，Dynamo graph/guard 数量不随每轮线性增长；
7. 在首段、生成中段和 finish 阶段分别执行 abort，均须在 2 秒内 release，下一轮可正常开始，不得残留旧音频；
8. 任一后续门禁失败都回退生产 `local_compile_mode=disabled`，并保存 canary 日志和 benchmark JSON，不能用单次成功代替统计结果。

## 风险

- `ThreadPoolExecutor` 无法强制中断已经进入 CUDA kernel 的任务；现有超时仍应由 Supervisor 进程级 fail-fast 兜底。
- 该网关容量仍为单活跃发言。持久线程解决 TLS 一致性，不增加 GPU 并发容量。
- endpoint slot 仍会保留到 codec close 和 session release ACK 全部完成；后续会话不会与旧上下文重叠。
- RTF 是否低于 1 还取决于模型、codec decode batch、GPU 时钟和音色 prompt；必须以生产同机 canary 数据判定。
