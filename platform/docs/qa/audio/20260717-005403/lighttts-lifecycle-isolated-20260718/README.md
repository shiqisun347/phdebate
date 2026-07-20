# LightTTS / CosyVoice3 lifecycle 隔离候选与故障注入

执行时间：2026-07-18 03:20–04:43 CST。证据类型：`newly_run`。运行模式：`minimal-reproducible-run`。

## 结论

已基于生产相同的 LightTTS commit `ad1c76e36614a1aa26629bc11246884bb7c4072c` 制作隔离候选，完成首 PCM 超时、动态整轮超时、统一 abort、真实 shared-request release 等待、orphan/readiness、阶段心跳和完整进程组 fail-fast 的最小集成补丁。

当前 consolidated 候选共 3 个 commit。完整 19 项隔离测试在全新 clone 从基线应用 consolidated patch 后通过：首 PCM 卡死、整轮卡死、finish 前后断连、Agent final 延迟、append 锁卡死、prefill/decode/token2wav RPC 卡死、abort 后不释放、readiness 503、ID 归一化、manager orphan backstop 和完整进程组退出均达到候选控制流预期。

最终复核目录为 `/tmp/lighttts-lifecycle-final-applycheck-c9615423`。补丁从基线 commit 通过 `git am` 依次应用 3 个 commit；使用 Python 3.12、先运行 OS 进程组测试的完整命令得到 `19/19 PASS in 3.557s`，定向 `py_compile` 与 `git diff --check` 通过。两个预备的默认顺序运行暴露了纯墙钟调度断言抖动，未修改源码；因此报告同时保留该测试稳定性风险。

真实 RTX 3080 Ti canary 的结论必须分开看：**recovery PASS，synthesis NO-GO**。真实 CosyVoice3 候选能够加载并达到 `/health/ready` 200；bi-stream 请求在 3 秒内仍没有任何首 PCM，触发 `FirstPcmTimeout`。随后 root 收到 SIGTERM，重型 encode/LLM/token2wav GPU 子进程被终止，生产 LightTTS 恢复运行，因此恢复机制按 canary 定义为 PASS；但实时合成本身仍是 NO-GO。该结果不授权部署，现网 bi-stream 和实时语音开关必须继续关闭。

## 可读候选

- 隔离 clone：`/tmp/lighttts-lifecycle-candidate-20260718`
- 上游基线：`ad1c76e36614a1aa26629bc11246884bb7c4072c`
- 候选 commits：
  - `a36a6376fb53fa3808cde6f664e76c7f8aa080c7` — lifecycle 与 fail-fast 主补丁
  - `c6d1d2743dc3566d66b32b1a0870426569d14110` — heartbeat request ID 归一化
  - `c9615423d2183f78e3f4fb02656faf2dba7c6a7a` — manager watchdog 回收 wedged orphan
- 当前候选 HEAD：`c9615423d2183f78e3f4fb02656faf2dba7c6a7a`
- 全新应用复核 clone：`/tmp/lighttts-lifecycle-final-applycheck-c9615423`
- 全新应用后的 HEAD：`1d76affe13a091a36201eeba7a47ab09a2e44e09`
- 可应用补丁：[0001-isolate-bistream-lifecycle-and-fail-fast-recovery.patch](0001-isolate-bistream-lifecycle-and-fail-fast-recovery.patch)
- consolidated patch SHA-256：`d9f9a58874173bbde42430164901b6ef1f54f69114fb5578b72782b7259085ed`
- 结构化结果：[result.json](result.json)
- 测试输出：[test-output.txt](test-output.txt)

## 生产只读定位

生产源码位于 `/home/ubuntu/sunsq/debateall/services/LightTTS`，模型位于 `/home/ubuntu/sunsq/debateall/services/models/Fun-CosyVoice3-0.5B-2512`。生产 Git worktree 干净；7 个关键文件的 SHA-256 与隔离 clone 基线逐一一致，因此补丁不是针对过时快照制作。

Supervisor 的 `jixia-lighttts` 早期只读快照结构：

```text
Supervisor
└─ LightTTS service root PID 188722 / PGID 188722
   ├─ resource tracker
   ├─ encode PID 188751            GPU 1356 MiB
   ├─ LLM/model PID 188752         GPU 1618 MiB
   ├─ token2wav/decode PID 188753  GPU 1910 MiB
   └─ gunicorn master PID 188865
      └─ worker PID 188866         GPU 254 MiB
```

全部模型、HTTP 和 GPU 进程都是 service root 的后代并共享 PGID `188722`。生产 Supervisor 已设置 `autorestart=true`、`stopasgroup=true`、`killasgroup=true`，这为完整进程组恢复提供了正确边界；但没有显式 `stopwaitsecs`，沿用约 10 秒默认值，而当前 `api_start.py` 的 SIGTERM 路径最多等待 HTTP 60 秒，二者不一致。候选把 root shutdown grace 收紧为 10 秒、Supervisor `stopwaitsecs` 设为 15 秒，并使用异常退出码触发重启。

对 `jixia-lighttts` 生产服务的访问始终为只读；没有复制补丁到生产、没有修改生产 Supervisor，也没有启用任何实时语音 flag。后续真实 GPU 请求只运行在隔离 clone/端口，不替换现网服务。

## 真实 GPU canary：recovery PASS / synthesis NO-GO

证据：[gpu-canary3.log](../../20260718-realtime-voice-rebuild/lighttts-gpu-canary/gpu-canary3.log)，SHA-256 `c4a770b5056d6a247b0f23865739a0d0c30d68a5407aa876284771884fbd7328`。

该 canary 使用隔离端口 `8083` 和真实 `Fun-CosyVoice3-0.5B-2512` 权重，未替换生产服务：

- encode、LLM 与 Audio Decoder 三个组件均完成真实 GPU 初始化；候选 `/health/ready` 返回 200。
- startup 非流式健康合成 req0 完成，产出音频并释放 shared request，说明权重、基础推理和 token2wav 可以工作。
- 真 bi-stream req1 在首文本进入后完成 encode 与 LLM prefill，但 3 秒内没有任何 PCM；20:33:12 触发 `FirstPcmTimeout`。因此 `first_pcm_before_finish` 和实时合成门都不通过，判定 **synthesis NO-GO**。
- abort 后 request 仍显示 `can release False / refcount 4`，root 于 20:33:14 收到 SIGTERM；20:33:24–25 强制停止 HTTP 和三个重型子进程。生产 LightTTS 随后重新启动并恢复服务，因此按 canary 的服务恢复门判定 **recovery PASS**。
- shutdown 日志仍报告 semaphore/shared-memory 清理 warning；该 recovery PASS 只表示超时能够进入完整恢复并恢复生产可用性，不等于 bi-stream 正确、不等于无资源清理债务，也不等于允许部署。

最终门禁仍是：真实 bi-stream 必须稳定产生 PCM、首包满足预算、重复 fault injection 无残留、Linux Supervisor 自动重启与音频质量均通过。当前不部署。

## 候选 lifecycle

```text
首个非空文本
  ├─ 启动 audio worker
  ├─ 立即启动 first-PCM deadline
  ├─ 立即启动从首文本计时的动态 whole-session deadline
  └─ 单一 WebSocket reader 持续接收 append / finish / disconnect
       ├─ append...（append/finish 共用请求锁）
       └─ finish 只标记 input_finished，不重置任何 deadline
            └─ 保留 finish 后 disconnect watcher 直至 audio worker 完成

disconnect / first-PCM timeout / whole timeout / endpoint exception
  → idempotent abort(reason)
  → readiness 503（abort_pending）
  → 等待 shared request 的真实 can_release + refcount 回收
       ├─ grace 内释放：安全结束
       └─ 未释放：mark orphan + orphan_total + readiness 503
                    → SIGTERM service root
                    → 停 Gunicorn + encode/LLM/token2wav/model children
                    → root 非零退出
                    → Supervisor 重启完整进程组
```

具体实现：

- `lifecycle.py` 是无 CUDA 依赖的 deadline、abort、health、heartbeat 和 restart 合约。
- first PCM 默认 3 秒；整轮上限为 `min(240s, 30s + visible_chars × 0.4s)`。两个 watchdog 都从第一段非空文本启动；Agent final 未到达时仍会主动 abort/restart，finish 不会重新计时。
- 客户端发出 `finish` 后仍保留接收 watcher，断连不再不可见。
- `append_bistream()` 的 append 与 finish 都在同一 shared request lock 下更新。
- abort 只设置共享请求的真实 aborted 状态，不伪造 `can_release`，并等待 manager 从 `req_id_to_out_inf` 中实际移除。
- abort pending、首 PCM stall、整轮 stall、orphan、组件 heartbeat 缺失或过期都会令 `/health/ready` 返回 503。
- heartbeat 覆盖 HTTP manager、encode、Router loop、prefill、decode 和 token2wav；每个事件带 PID、stage、request IDs 与 monotonic 时间。
- NumPy 等 integer-like request ID 在写 heartbeat、registry 与 JSON readiness 前统一转换为原生 `int`，关闭真实 GPU canary 中的 `int64 is not JSON serializable` 缺陷。
- manager recycle watchdog 不再只标记 orphan；发现超过 grace 仍 wedged 的 active request 会调用 service-root restart backstop，避免 endpoint task 已取消后永久假绿。
- prefill、decode 与 token2wav RPC 超时会请求 service root SIGTERM，而不是只杀 Gunicorn worker或单个 Router。
- service root 不再只等待 Gunicorn；初始化完成后任一 encode/LLM/decode 子进程死亡都会使 root 非零退出。
- 内置 health monitor 改读无合成的 `/health/ready`，不再用真实 TTS 高频探活。

## 故障注入结果

| 场景 | 结果 | 关键断言 |
|---|---|---|
| 正常首 PCM + 完成 | PASS | 不调用 abort |
| Agent final 人为延迟 2 秒 | PASS | finish 前已经产生首 PCM；同一 worker/session 持续等待 final |
| finish 不重置 deadline | PASS | 整轮超时仍锚定首文本时间，不因 finish 获得新预算 |
| finish 后客户端断开 | PASS | 统一 `ClientDisconnected` abort，确认 release |
| finish 前客户端断开 | PASS | 单一 reader 捕获，统一 abort，确认 release |
| 无首 PCM | PASS | `FirstPcmTimeout`，abort 后 release |
| finish 前无首 PCM | PASS | 不等待 Agent final，直接超时 abort/release |
| 已有 PCM 但整轮卡死 | PASS | `WholeSessionTimeout`，abort 后 release |
| 输入一直不 finish | PASS | 整轮 deadline 仍生效并 abort/release |
| append/finish 共享锁卡住 | PASS | 锁等待不能暂停 absolute deadline；到期后统一 abort/release |
| prefill 永不返回 | PASS | stage watchdog 请求完整重启 |
| decode 永不返回 | PASS | stage watchdog 请求完整重启 |
| token2wav 永不返回 | PASS | stage watchdog 请求完整重启 |
| abort 后资源不释放 | PASS | 抛出 `RestartRequired`，禁止伪造释放 |
| abort grace 内 | PASS | readiness HTTP 503，`abort_pending=[246]` |
| grace 后 orphan | PASS | readiness HTTP 503，`orphans=[246]`、`orphan_count=1` |
| NumPy/integer-like request ID | PASS | heartbeat 与 readiness 输出归一化为 JSON-safe 原生整数 |
| endpoint 已消失但 manager orphan 仍卡死 | PASS | recycle watchdog 请求 service-root restart backstop |
| 心跳缺失或过期 | PASS | readiness 失败 |
| OS 进程组恢复 | PASS | 隔离 root 退出码 70，3 个子 PID 全部消失 |

OS 进程组测试使用单独 session/PGID 启动 fake encode、LLM-GPU 与 token2wav-GPU 子进程。它证明 fail-fast 不会只留下 HTTP 或模型孤儿进程；这些进程不加载 CUDA，因此不等同于真实显存释放证据。

## 可重复命令

```bash
cd /tmp/lighttts-lifecycle-final-applycheck-c9615423
git rev-parse HEAD
/opt/homebrew/bin/python3.12 -m unittest -v \
  tests.test_lifecycle_process_group \
  tests.test_bistream_lifecycle
/opt/homebrew/bin/python3.12 -m py_compile \
  light_tts/server/lifecycle.py \
  light_tts/server/api_http.py \
  light_tts/server/api_start.py \
  light_tts/server/httpserver/manager.py \
  light_tts/server/tts_llm/manager.py \
  light_tts/server/tts_decode/manager.py \
  light_tts/server/tts_encode/manager.py
```

在新的干净 clone 上复核补丁：

```bash
git checkout ad1c76e36614a1aa26629bc11246884bb7c4072c
git am /Users/sunshiqi/code/phdebate/platform/docs/qa/audio/20260717-005403/lighttts-lifecycle-isolated-20260718/0001-isolate-bistream-lifecycle-and-fail-fast-recovery.patch
/opt/homebrew/bin/python3.12 -m unittest -v tests.test_lifecycle_process_group tests.test_bistream_lifecycle
```

## Evidence Inventory

- Status / Evidence Type：`newly_run`，2026-07-18。
- Inputs：生产相同 commit、生产只读 Supervisor/进程树、隔离 clone、fake request/shared-release、OS 子进程与真实 RTX 3080 Ti canary 日志。
- Output Artifacts：候选 commit、可应用 patch、结果 JSON、测试输出和本报告。
- Aggregation：19 个确定性测试；每个故障场景一次，属于机制验证，不是稳定性统计。另有一次真实 GPU timeout/recovery canary。
- Claim Readiness：API lifecycle、ID 归一化、manager orphan backstop 与服务恢复机制为内部 `paper_ready` 证据；真实 bi-stream 合成、首 PCM、音频质量、Linux Supervisor 自动重载和生产部署仍为 `blocked`。

## Protocol Risks 与下一门

1. 真实 GPU canary 已证明 timeout 能进入恢复，但没有产生任何 bi-stream PCM；恢复 PASS 不能替代合成 PASS。
2. canary shutdown 报告 leaked semaphore/shared-memory warning，仍需重复运行验证清理幂等和无跨请求污染。
3. macOS 19 项测试中两个预备默认顺序运行出现墙钟断言抖动；最终 Python 3.12 规范顺序 19/19，通过但测试阈值仍应在后续 CI 中改为更稳健的事件断言。
4. heartbeat 使用 `/tmp` 原子 JSON 文件作为最小跨进程实现；需要在隔离 Linux 长稳测试中确认 I/O、清理和多音色多进程命名行为。
5. RPC timeout 8–10 秒只是安全候选值，必须用真实健康 p99 校准，避免误杀正常长块。
6. 尚未跑成功的 10–16 字首块、16–28 字后续块、长发言、2/3 路队列和 TTS-only retry 真实模型非回归。

下一步不是部署，而是继续定位真实 bi-stream 在 prefill 后无首 PCM 的模型路径；修复后重新执行同一 GPU canary，要求产生 PCM、满足首包预算、重复 timeout/restart 无资源残留，再补 Linux Supervisor 自动恢复与音频质量。此前保持 `LIGHTTTS_BISTREAM_ENABLED=false` 与 `REALTIME_VOICE_PIPELINE_ENABLED=false`。
