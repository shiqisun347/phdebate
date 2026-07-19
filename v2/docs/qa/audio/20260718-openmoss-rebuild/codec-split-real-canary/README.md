# OpenMOSS codec encoder-offload 12GB real canary

日期：2026-07-18（Asia/Shanghai）

结论：**NO-GO**。该路径解决了 12GB 显存装载问题，但没有解决推理性能；同一进程第二轮热态比首轮更慢，不能用于实时辩论，也没有部署到生产。

## 固定版本与 placement

- GPU：NVIDIA GeForce RTX 3080 Ti，PyTorch 可见总显存 11912.62MiB。
- OpenMOSS upstream：`ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af`。
- Realtime model：`6acbc7f161a0db71c291f2d0aaa9eee59334cab2`。
- Audio tokenizer：`3cd226ba2947efa357ef453bcad111b6eafba782`。
- attention：SDPA；talker：CUDA；codec encoder：CPU；quantizer/decoder：CUDA。
- codec streaming context：decoder-only；8 个固定音色 prompt 已在 encoder 下卡前预编码并缓存。

成功 placement：

```json
{
  "mode": "diagnostic_encoder_cpu_decoder_cuda",
  "realtime_model": "cuda:0",
  "codec_encoder": "cpu",
  "codec_quantizer": "cuda:0",
  "codec_decoder": "cuda:0",
  "codec_streaming_context": "decoder_only",
  "prompt_tokens_cached": 8
}
```

完整 codec 启动后 GPU used/free 为约 7475/4437MiB；encoder offload、8 个 prompt 缓存和 talker 加载后为约 8437/3475MiB。由此证明拆分 placement 在 12GB 上能够驻留，但只证明内存可行。

## 同进程冷轮与热轮

两轮使用同一 backend、同一模型对象、同一 codec 对象和同一音色；第一轮用于触发编译/缓存，第二轮才是热态测量。

| 指标 | 冷轮 | 第二轮热态 | 验收门 |
|---|---:|---:|---:|
| 首 PCM | 113.732s | 175.936s | ≤0.8s |
| 合成墙钟 | 130.328s | 196.531s | — |
| 音频时长 | 5.44s | 6.72s | — |
| RTF | 23.957 | 29.246 | ≤0.65 |
| PCM chunks | 14 | 17 | — |
| 最大块间隔 | 10.752s | 13.769s | ≤0.25s（本 canary 的保守门） |
| P95 块间隔 | 0.590s | 0.566s | ≤0.2s（正式门） |

第二轮热态三项自动门均失败：

```json
{
  "first_pcm_le_0_8s": false,
  "rtf_le_0_65": false,
  "max_inter_chunk_gap_le_0_25s": false
}
```

因此前一轮约 40.7 秒首包、RTF 19.6 不能归因于一次性编译；同进程热轮反而更差。继续调小 phrase、初始缓冲或浏览器队列不会改变模型侧 176 秒首包的数量级。

## 两次 fail-closed 预检

正式模型运行前有两次无推理失败，均由退出 trap 自动恢复生产：

1. 首次误用了生产 LightTTS Python 3.10 环境，OpenMOSS 依赖的 Transformers API 不匹配；随后改用隔离 Python 3.12 canary 环境，并只在 canary 进程内暴露官方 `transformers.initialization` 模块。
2. `nvidia-smi` 标称 12288MiB，但 PyTorch 以 GiB 计算为约 11.63GiB；诊断门槛写成 12GiB 时按设计 fail-closed。真实运行使用 11GiB diagnostic floor，生产默认 24GB/20GB floor 未变。

这两次均发生在模型推理前，不计入性能样本，也没有修改生产 Python 包、OpenMOSS checkout 或 V2 配置。

## 生产恢复核验

每次维护前均确认：

- 主站 readiness `ok=true`。
- `active_match_processing=false`。
- LightTTS admission `active=0 / queue_depth=0`。

最终退出后独立核验：

- `jixia-lighttts` Supervisor `RUNNING`。
- `http://127.0.0.1:8080/health` 返回 `Ok`。
- 公共 `/api/health/ready` 返回 `ok=true`。
- `active_match_processing=false`，admission `0/0`。
- GPU used/free 恢复为约 5000/6914MiB。

所有 `LIGHTTTS_STREAMING_ENABLED`、`LIGHTTTS_BISTREAM_ENABLED`、`REALTIME_VOICE_PIPELINE_ENABLED` 和 `MOSS_TTS_REALTIME_ENABLED` 继续保持关闭；未部署该候选。

## 证据

- [完整 JSON](codec-split-warm-turns-result.json)
- [冷轮 WAV](codec-split-cold-compile.wav)
- [热轮 WAV](codec-split-warm-measured.wav)
- [完整日志](codec-split-warm-turns.log)
- [前一轮单次结果](codec-split-voice1-result.json)
- [前一轮 WAV](codec-split-voice1.wav)
- [可复现 canary 脚本](../run_codec_split_warm_canary.py)

## 决策

停止以下 12GB 正式候选：

- 原版 talker + 完整 codec 全 CUDA：OOM。
- talker CUDA + 完整 codec CPU：prompt 编码超过 321 秒。
- talker CUDA + codec encoder CPU、decoder CUDA：热态首 PCM 175.936 秒、RTF 29.246。

后续严格按 `docs/realtime-voice-rebuild.md`：转到独立 24GB+ GPU，先用固定上游原生 session + 当前持久 WS gateway 跑真实单 endpoint 20 轮，再比较 vLLM-Omni runtime；单路通过后才扩到 2–3 个独立 endpoint、8 音色质量和浏览器真实首声门。
