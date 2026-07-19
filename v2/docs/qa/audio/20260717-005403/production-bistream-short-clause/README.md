# 生产 CosyVoice3 + LightTTS/TRT 短子句 bi-stream 审计

结论：**NO-GO，生产 streaming flag 必须继续关闭。**

本轮没有获得慢但可用的首 PCM，而是在第一个有效模型请求上复现了更严重的问题：10 个汉字的首段在 500ms 后追加第二段并发送 `finish`，请求完成 encode 和 LLM prefill 后永久停在首次 LLM decode/output 之前，0 PCM、0 WAV。客户端断开后，请求继续成为 orphan，占住唯一的 `running_max_req_size=1` 槽位；V2 readiness 与 Redis gate 仍显示健康。

发现 orphan 后立即停止剩余矩阵。没有机械执行 6 个单路和两波三并发，因为后续结果都会被已污染的单槽服务支配，且会进一步影响生产可用性。

## 环境与安全门

- 开始前：`active_match_processing=false`，`preparing/running/paused/judging=[]`，LightTTS gate `0/0`。
- GPU：RTX 3080 Ti，used 5128MB / free 6786MB / utilization 0%。
- 调用：仅 `127.0.0.1:8080/inference_zero_shot_bistream`；未修改代码、配置、数据库、Nginx 或 `.env`。
- 该调用直接旁路 V2 admission gate，因此 gate 0/0 不能证明模型空闲或请求已释放。
- 生产 LightTTS commit：`ad1c76e36614a1aa26629bc11246884bb7c4072c`，仓库 `ModelTC/LightTTS`。
- 模型：`Fun-CosyVoice3-0.5B-2512`，float16 + TRT。
- 关键容量配置：`httpserver_workers=1`、`running_max_req_size=1`、`decode_max_batch_size=1`、encode/LLM/decode 各 1 个进程。

完整启动命令与 Supervisor 配置见 [runtime metadata](evidence/runtime-metadata.txt) 和 [Supervisor program](evidence/supervisor-program.conf)。源文件 SHA-256 见 [source hashes](evidence/source-hashes.txt)。

## 实际执行

计划是 10/12/16 汉字首段各重复两次，再做两波三路同步；实际只允许一个请求进入模型：

| 请求 | 阶段 | 结果 |
|---|---|---|
| req246 / 10 字 | 15:11:00.922 接收；00.929 encode；00.934 发往 LLM；00.936 LLM 接收；01.015 prefill 完成 | 此后没有 LLM output、decode receive、token2wav、PCM、finalize 或 release。约 90 秒后仍为 0 PCM。 |
| req247 / 12 字 | 15:12:30.896 仅在 HTTP/WS ingress 分配 request id | 未进入 encode/LLM，未计为模型请求；req246 已占唯一槽。随后停止测试。 |

首段和第二段协议严格按要求执行：连接后立即发送首段，500ms 后追加“因此结论不能只看眼前效果，必须检验长期影响。”，随后发送 `finish`。

因此指标只能如实记为：

- 首文本→首 PCM：无值，90 秒观察窗口内 0 PCM。
- 首 PCM 块：0 bytes。
- PCM gap、总 RTF、连续性：不可计算。
- 最终 WAV：不存在。
- FunASR CER、末词“长期影响”、重复检测：`NOT_RUN_NO_PCM`。
- 三路并发：未执行；单路已破坏服务内部状态，再发三路不构成有效容量测试。

结构化数据见 [result.json](result.json)，原始 req246/247 日志见 [req246 timeline](evidence/req246-timeline.log) 和 [req247 timeline](evidence/req247-timeline.log)。

## 故障定位

日志将边界定位到 **LLM prefill 已完成、第一次 LLM decode/output token 尚未完成**：

1. encode 正常完成，semantic length 108、text length 36 已发给 LLM。
2. LLM 收到请求，初始 `input_len=1` 是 bi-stream 模式的设计：文本先放 `text_cache`，初始 prompt 只有 SOS；调度时再由 `try_to_fill_text()` 组装。
3. `Prefill Batch ... req_ids:[246]` 已输出，说明 `model_rpc_client.prefill()` 返回。
4. 后续没有任何 `tts_llm Send`，也没有 decode manager 收到 token，更没有 token2wav。故障位于首次 `model_rpc_client.decode()` 或其 bi-stream 状态机；在不加内部 instrumentation 的情况下，不能进一步声称是某个 CUDA kernel 或特定 token 判断。

源码证据：

- [api_http.py](source/api_http.py) 的 bi-stream endpoint 在收到 `finish` 后跳出 receive loop，然后无限等待 `process_task`。
- 同一 endpoint 只在仍执行 `receive_json()` 时捕获 `WebSocketDisconnect` 并调用 abort；finish 后不再读取 socket。
- [httpserver-manager.py](source/httpserver-manager.py) 的 abort 只设置 `is_aborted`，资源释放仍要求 `can_release()`。
- [req.py](source/req.py) 保存 bi-stream 的 `WAIT_FOR_TEXT`、append、finish 和 text/audio mix 状态。
- [tts-llm-manager.py](source/tts-llm-manager.py) 在 prefill 后进入 decode，并且只有产生足够 token 或 finish 时才向 token2wav 发送数据。

生命周期故障链如下：

```text
首段 → append 第二段 → finish
                    ↓
endpoint 停止 receive，await process_task
                    ↓
LLM 首次 decode 无输出，send_wav 无数据可发送
                    ↓
客户端断开无法被 receive 捕获，abort 不执行
                    ↓
req246: can_release=False / refcount=4，持续每秒空轮询
```

这也解释了为何 readiness 假绿：当前 V2 readiness 只看到 LightTTS 端口可连接和 V2 gate 0/0，不知道服务内部存在 orphan request。

## 最小恢复

req246 在客户端关闭后仍持续空轮询，已实际占用唯一槽。获得明确授权后，于 `2026-07-17T15:17:44Z` **只执行一次**：

```text
supervisorctl restart jixia-lighttts
```

未重启 V2、Agent、FunASR、Nginx、数据库或任何其他 Supervisor 服务。

恢复证据：

- 新 LightTTS PID：177404。
- `2026-07-17T15:18:51.798Z` 日志出现 `Application ready! Server is now accepting requests`。
- 启动自检 req0 完整经过 encode → LLM → decode → yield → release；这是服务自带启动检查，本轮恢复后没有人工发送合成请求。
- V2 ready 连续 5/5：`true / active_match_processing=false / gate 0/0`。
- 活动房仍为空。
- loopback `/openapi.json` 3/3 HTTP 200；调用前后新增 TTS 请求数均为 0。
- Application ready 之后 req246 日志数为 0。
- 新 stderr 中 `ERROR|Traceback|CRITICAL|Exception` 为 0。
- GPU 恢复至 used 5000MB / free 6914MB / utilization 0%。
- LightTTS、FunASR、V2 API/Engine/Worker/Web/Postgres/Redis/Backup、Agent API/Web/Backup 全部 RUNNING；其他服务 PID 和 uptime 未变化。

恢复原始日志见 [recovery log](evidence/recovery-log.log)。

## 修复门槛

在重新启用或测试生产 bi-stream 前，至少需要：

1. 服务端 whole-session deadline；超时必须进入 guaranteed abort/finally cleanup。
2. 收到 `finish` 后仍并行监听 socket disconnect/cancel，不能只等待音频 worker。
3. abort 能强制释放停在首 PCM 前的 LLM 请求和共享内存引用。
4. readiness 暴露内部 active request、最长 request age、orphan count 和各阶段 heartbeat。
5. 为 append、finish、prefill、first decode、fill token、append-prefill、首 token2wav、finalize、release 增加 request-scoped 日志。
6. 修复后先在隔离实例完成 6 个单路与两波三并发，再进入真实浏览器 AudioWorklet 测试。

当前不能将“连接成功”或“端口 ready”解释为 bi-stream 可用，更不能宣称已满足 3 秒首声、无卡顿或三路并发。
