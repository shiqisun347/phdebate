# CosyVoice3 原生 streaming generator 直连审计

审计时间：2026-07-18（Asia/Shanghai）  
范围：生产同款 `Fun-CosyVoice3-0.5B-2512`、生产 LightTTS commit
`ad1c76e36614a1aa26629bc11246884bb7c4072c`、当前 V2
`voice_runtime`/LiveKit 链。  
安全边界：只读生产源码、进程、GPU、依赖和 health；未调用新的 TTS
推理，未停止或重启服务，未修改生产配置，未部署。

## 决策

**暂不实现原生 CosyVoice3 直连 provider；当前方案为 NO-GO。**

这不是因为 CosyVoice3 不能流式产出 PCM。它的原生
`CosyVoice3Model.tts(..., stream=True)` 确实会按 token hop 生成连续 PCM，
也维护每个 utterance 的 mel/HiFT cache。问题在于：若把 Agent 增量文本作为
Python generator 传入，原生实现仍会进入 `CosyVoice3LM.inference_bistream()`，
即生产 LightTTS 卡死请求所使用的同一套 fill-token/text-cache 双流逻辑。绕过
LightTTS HTTP/WS router 只能绕过入口和 manager，不能绕过已定位到首次
bi-stream decode 的故障边界。

同时，原生模型没有满足当前正式协议的取消、释放和并发隔离契约。现在直接
包装为 V2 provider 会让“客户端已打断但模型线程继续运行、资源不释放、旧音频
复活”重新成为不可观测风险，不属于低风险适配。

因此本轮没有加入一个表面可选、实际不能安全 `abort` 的默认关闭 provider。
现有生产开关和路由保持不变。

## 1. 哪些路径真正能绕过 bi-stream 故障

| 候选调用 | 是否绕过 `inference_bistream()` | 能否消费 Agent 增量文本 | 结论 |
|---|---:|---:|---|
| `inference_zero_shot(full_text, stream=True)` | 是，使用普通 `llm.inference()` | 否，开始前需要完整短语/句子 | 已证明单路能流式返回 PCM，但不是双向增量 session |
| `inference_zero_shot(text_generator, stream=True)` | 否，进入 `llm.inference_bistream()` | 是 | 与已卡死的双流核心同源，不能作为绕过方案 |
| 多次完整短语 `stream=True` | 每次绕过 | 只能用多次独立 utterance 模拟 | prompt/LLM/flow cache 每段重建，不能证明块间音色与韵律连续，也不满足持久 session |

生产短文本 bi-stream 已将最后成功边界定位为：encode 完成、LLM prefill
完成，首次 LLM decode/output token 未出现；其后 0 PCM 且请求成为 orphan。
直接 Python text generator 会调用同一个 `inference_bistream()`，所以不能以
“不经过 router”推断故障已经消失。

完整文本 HTTP `stream=true` 的既有实测则说明另一条边界：单路 3 次全部
成功，首 PCM P95 为 1.259 秒；两路/三路首 PCM P95 分别恶化为 6.333 秒和
10.983 秒，RTF P95 为 1.795/2.651，最大块间隔 P95 约 0.86–0.91 秒。
这条路径可以保留为完整文本流式回滚研究，但不满足 2–3 场实时并发，也不能
替代 `Agent delta -> 持久 TTS session`。

## 2. 原生模型生命周期缺口

生产源码对应的原生 `CosyVoice3Model.tts()` 有以下硬缺口：

1. 每个请求启动一个 `threading.Thread` 执行 `llm_job()`，但没有 cancel
   token、deadline 或 abort 方法。
2. 流式 generator 使用每 100ms 轮询共享 token list；结束时执行无超时的
   `thread.join()`。
3. session 字典清理只位于 generator 正常走到末尾之后，没有覆盖
   `Generator.close()`、消费者取消、LLM 线程异常或 WebSocket 断开所需的
   `try/finally`。
4. `llm_job()` 若抛异常，线程可以结束而 `llm_end_dict[uuid]` 仍为 false；
   外层 generator 会永久轮询。这与现有“prefill 后 0 output、请求不释放”的
   失效形态一致。
5. 原生 generator 不提供 `text_delta ACK`、`final ACK`、`audio_reset`、
   `released`，无法证明 interrupt 后音频队列、模型 cache 和未播放文本都已
   清空。
6. 正常收尾会执行全局 `torch.cuda.empty_cache()` 和当前 CUDA stream
   synchronize；并发 session 下可能把一个请求的结束成本传播给其他请求。

当前 V2 `voice_runtime` 要求 `push_text()`、`finish()`、`abort()` 三个明确
阶段；持久 WS provider 还要求单调 seq/ACK、abort 后 `audio_reset` 和最终
`released`。原生 CosyVoice generator 不能满足这个接口的核心语义，简单放进
`asyncio.to_thread()` 只能避免阻塞 event loop，不能取消底层 GPU 工作。

## 3. 并发、锁和音色隔离

- 原生模型只有一个共享 `llm_context` CUDA stream；每个请求虽以 UUID 保存
  token/mel/HiFT cache，但 LLM 前向共享同一模型与同一 stream。
- 字典创建/删除使用一个 `threading.Lock`，token append/read 和 end flag
  没有完整的 session 临界区或异常传播通道。
- TensorRT estimator 通过 context pool 控制并发；默认
  `trt_concurrent=1`。增加 context 数只扩大 flow estimator 容量，不会自动
  解决共享 LLM stream、HiFT、显存和取消问题。
- 仓库自带 gRPC 示例的 `max_conc=4` 只是线程池/RPC 上限。当前生产源码快照
  的 gRPC/FastAPI server 只尝试 `CosyVoice`/`CosyVoice2`，没有构造
  `CosyVoice3`；调用又未传 `stream=True`，不能作为 CosyVoice3 三并发证据。
- 现有生产实测在 `running_max_req_size=1` 下呈明确串行，第三请求首 PCM 最慢
  11.342 秒。当前没有证据证明同一个原生 0.5B 实例的三线程推理能保持音色、
  无卡顿且每场等待不超过 3 秒。

正式接入前仍应坚持现有 endpoint 隔离原则：每个真实模型 endpoint 初始
`max_active=1`，只有在真实 c2/c3 质量与延迟门通过后才能提高单 endpoint
并发；不能用一个 Python 进程的线程池数代替容量证明。

## 4. 显存和部署容量

只读生产采样：

- GPU：RTX 3080 Ti 12GB；LightTTS 空闲时 used/free 约 5000/6914MiB。
- GPU 上已有 4 个相关 Python 进程，显存约 1340、1616、1764、254MiB。
- 模型目录约 11GB；主要资产包括 `llm.pt` 约 2.02GB、`flow.pt` 约
  1.33GB、TensorRT plan 约 1.33GB、speech tokenizer ONNX 约 969MB。
- 既有官方 PyTorch CosyVoice3 共存 canary 在完成加载后单进程约
  4.79GB；warmup 时额外申请 260MB 失败，未产生首 PCM。

所以在现有 12GB 卡上与生产 LightTTS 共存第二个原生实例已经明确 NO-GO。
停止 LightTTS 后让原生实例独占 12GB 是否能够完成单路推理，不能由本轮只读
审计否定；但即使单路能装下，也仍缺少三路容量、取消释放和双流正确性证据。
在这些问题解决前，不能把“可能独占加载成功”写成可部署结论。

## 5. 当前运行环境缺口

生产 LightTTS Python 环境能找到 `torch`、`torchaudio`、`onnxruntime`、
`HyperPyYAML`、`wetext`、FastAPI/Uvicorn，但：

- `cosyvoice.cli.cosyvoice` 顶层硬导入 `modelscope`，当前环境没有该包，直接
  import 失败；本地模型已存在，本可将该依赖改成按需导入，但这仍是上游 fork
  修改，不是零改动直连。
- 当前环境没有 gRPC Python 包；仓库的官方 gRPC 示例也不支持该 CosyVoice3
  构造路径。
- 直接 loader 与生产 LightTTS 分进程/TRT loader 不是同一个权重与显存布局，
  不能复用现有 5GB 驻留进程里的 Python 模型对象。

## 6. 若后续继续走 CosyVoice3，最低限度的正确实现

只有完成以下改造后，才值得新增默认关闭 provider：

1. 在隔离服务中 fork 原生 CosyVoice3，新增显式 `Session` 对象、线程安全的
   bounded token/audio queue、`threading.Event` cancel、异常通道和覆盖所有
   出口的 `try/finally`。
2. `llm_job()`、flow/HiFT 循环和每次 PCM emit 都检查 cancel/deadline；
   abort 必须等待 worker 退出并删除 UUID cache，超时则隔离 endpoint/退出
   进程，由 Supervisor 拉起，不能返回虚假 release。
3. 修复并独立验证 `inference_bistream()` 首次 decode 卡死；至少 20/20 次
   delayed-finish 在 finish 前产生非静音 PCM。
4. 8 个固定普通话音色在启动时预提取 prompt text/token/feature/embedding，
   session 只引用不可变 voice fingerprint，禁止每个 delta 重做 prompt 编码。
5. 网关复用当前已经实现的正式持久 WS 协议：`start -> ready ->
   text_delta/ack -> final/ack -> audio_end -> released`，abort 为
   `audio_reset -> released`。这样 V2/LiveKit/AudioWorklet 无需再引入一种生命周期。
6. 先用独占 GPU 跑单 endpoint c1；再验证 c2/c3 的首 PCM、浏览器首声、RTF、
   chunk gap、CER、20 轮音色漂移、打断残留和显存峰值。若单实例三路不过门，
   只能使用多个独立 endpoint/GPU，不得把第三场静默排队十秒。

在完成第 1–3 项之前，实现 V2 provider 只会增加不可取消的生产候选代码，
没有把最终目标变得更真，因此本轮选择不添加。

## 7. 证据索引

- [生产 bi-stream 短子句审计](../20260717-005403/production-bistream-short-clause/README.md)
  与 [结构化结果](../20260717-005403/production-bistream-short-clause/result.json)：
  prefill 后首次 decode 无输出、0 PCM、orphan、唯一槽被占用。
- [原生 HTTP PCM 1/2/3 并发基准](../20260717-005403/iteration19-lighttts-stream-1-2-3.md)：
  完整文本流式单路可用，但 2/3 路串行且 RTF 超门。
- [官方 PyTorch CosyVoice3 共存 canary](../20260717-005403/cosyvoice3-canary/README.md)：
  第二实例加载/暖机显存 NO-GO，未产生 PCM。
- [生产 API 源码快照](../20260717-005403/production-bistream-short-clause/source/api_http.py)：
  router、append、finish 和 disconnect/abort 边界。
- 当前 V2 适配入口：`apps/api/app/services/voice_runtime/pipeline.py`、
  `apps/api/app/services/providers.py`。

关键本地证据 SHA-256：

| 文件 | SHA-256 |
|---|---|
| `source/api_http.py` | `55956324ecd2b465c7a01c38e2b0f02c354504043f0c056d8c98f7950c5d6fce` |
| `production-bistream-short-clause/result.json` | `e7698ffd1624c1085216f70867dac6f68f8248f8958af1546b244fc9fd6e579b` |
| `iteration19-lighttts-stream-1-2-3.json` | `98cc9c4917524c2ab5d81ecf4a6e076961936fefe2ca79d61b6478a046f8e9d0` |

## 8. 审计后生产状态

- `jixia-lighttts`：RUNNING，未重启。
- LightTTS `/health`：HTTP 200。
- V2 `/api/health/ready`：`ok=true`、`active_match_processing=false`。
- admission：active 0、queue 0、max active 1、max pending 2。
- GPU：used/free 5000/6914MiB（末次采样）。

本报告只作候选技术决策，不授权部署或打开任何 realtime/bi-stream flag。
