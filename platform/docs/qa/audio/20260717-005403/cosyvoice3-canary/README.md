# CosyVoice3 Base 0.5B 隔离 canary

结论：**NO-GO（同一张 12GB GPU 上并存第二个官方 PyTorch CosyVoice3 实例）**。

这不是模型质量结论，而是明确的资源与部署结论。生产 LightTTS 已经使用 `Fun-CosyVoice3-0.5B-2512 + TensorRT`；在不停止生产服务的安全约束下，再启动一个官方 PyTorch/gRPC 副本无法完成 warmup，更不能安全验证单路和三并发。

## 安全边界

- 候选仅位于 `/home/ubuntu/sunsq/cosyvoice3-canary`，监听 `127.0.0.1:50073`。
- 未修改 V2、Agent、数据库、Nginx、Supervisor、`.env` 或生产模型文件；权重仅以只读软链接复用。
- 每个候选进程组均受 watchdog 保护，最长 15 分钟。watchdog 每秒检查 readiness、`active_match_processing`、四类活动房状态以及 LightTTS gate；任一异常立即终止候选。
- 四轮共保存 878 个 watchdog 样本，生产不安全样本为 0。
- 结束后 3/3 readiness 通过，活动房为空，gate 0/0，候选进程为 0，端口 50073 已关闭，GPU 恢复为 used 5128MB / free 6786MB。

## 权重与环境发现

服务器已有完整模型缓存：

- 模型：`/home/ubuntu/sunsq/debateall/services/models/Fun-CosyVoice3-0.5B-2512`，约 11GB。
- 当前生产命令已经指向该模型，使用 LightTTS、`--load_trt`、单 HTTP worker、`running_max_req_size=1`、`decode_max_batch_size=1`。
- 生产常驻显存约 5.1GB，GPU 为 RTX 3080 Ti 12GB。
- 官方代码 commit：`074ca6dc9e80a2f424f1f74b48bdd7d3fea531cc`。
- 候选源码归档 SHA-256：`cb413716b786b903b287c2354853502dc44d162cb3b4222c87e8d0b962d72b77`。
- 固定普通话参考音频 SHA-256：`c7b31d6dbe7cc6a716dded00550db5b50940bf209e424e4ad207b12e657c8ff6`。

## 四轮结果

| 轮次 | 结果 |
|---|---|
| 默认官方 PyTorch + CUDA ONNX tokenizer | 52.433s 完成加载；进程显存 4790MB，整卡仅余 1991MB。未发请求即主动终止。 |
| tokenizer 改为 CPU，主模型仍 GPU | 常驻显存降至 3752MB，整卡余 3029MB；发现官方 gRPC 示例与 Torchaudio 2.9 不兼容，Tensor 被当作媒体源，报 `video_tensor must be kUInt8`。 |
| request-local 16k PCM WAV 适配 | 官方前端可读取参考音频；CosyVoice3 要求 prompt 包含 `<|endofprompt|>`，缺失时模型线程断言且 RPC 悬挂。 |
| WAV 适配 + prompt 规范化 | 48.342s 完成加载；warmup 时进程升至约 4572MB、整卡余约 2209MB，随后在 4.19GB PyTorch 安全上限内申请额外 260MB 失败。首个 PCM 尚未产生。 |

最终 OOM 证据见 [final server log](remote-results/run-20260717T145714Z-final-adapter/server.log)，官方示例兼容错误见 [Torchaudio failure log](remote-results/run-20260717T144628Z-cpu-tokenizer/server.log)。各轮 ready 与 watchdog 原始证据均位于 [remote-results](remote-results/)。结构化结论见 [summary.json](summary.json)。

## 指标判定

- 单路首 PCM、RTF、块间隔：`NOT RUN / NO PCM`，被资源安全门阻止。
- 三并发成功率、首 PCM P95、RTF、块间隔：`NOT RUN`，不能提高显存上限强跑。
- FunASR 回转 CER：`NOT RUN`，因为没有生成任何 PCM；不以空数据或参考音频代替候选结果。
- `max_conc=4` 只证明 gRPC 线程池允许四个 RPC，不证明 12GB 卡上的模型可承载四路推理。

## 与生产现状的关系

直接替换为第二个官方 PyTorch 服务不是正确路线。生产已经是 CosyVoice3，只是被配置为单 active、两个 pending。已有生产 canary 的三条短句全部成功，但实际为 `active=1 / queue=2` 串行完成，耗时分别为 2.074s、3.915s、5.696s，见 [production active+pending result](../lighttts-real-active-plus-pending-canary/result.json)。这无法满足每场等待不超过 3 秒，也不是三路实时并发。

现有质量回转同样还未过门：8/8 合成成功，但完整 WAV RTF median 0.772 / P95 0.891，中文 CER P95 0.4，见 [production quality benchmark](../lighttts-benchmark-iteration19-postdeploy/lighttts-benchmark.json)。这些是“当前驻留后端需要优化”的证据，不是再加载一份相同模型的理由。

建议下一步：

1. 优先直接基准和优化当前常驻的 `CosyVoice3 + LightTTS + TRT`，补真实流式首 PCM、块间隔与三路 simultaneous generation 观测。
2. 在独立 GPU 上测试提高 LightTTS `running_max_req_size`、decode batch 和 V2 `max_active`；通过后再迁移配置，不能在当前生产卡上边比赛边试。
3. 如果当前 LightTTS 架构无法稳定批处理，才在独立 GPU 比较 MOSS-TTS-Nano / MOSS-TTS-Realtime 或 vLLM-Omni，不要在这张 12GB 卡上部署第二个 PyTorch CosyVoice3。
4. 发布门仍应是三路首 PCM P95 ≤600ms、端到端首声 P95 ≤3s、每路 RTF <1、无浏览器 underrun、CER ≤2%、同一音色无可听漂移。
