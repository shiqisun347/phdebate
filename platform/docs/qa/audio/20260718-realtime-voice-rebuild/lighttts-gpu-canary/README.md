# LightTTS 真双流 GPU Canary

测试时间：2026-07-18（Asia/Shanghai）  
实例：与生产隔离的 `127.0.0.1:8083` canary；测试结束后已关闭，生产 `8080` 已恢复。  
模型：生产同款 `Fun-CosyVoice3-0.5B-2512`，RTX 3080 Ti 12GB。  
生产开关：测试前后均保持 `REALTIME_VOICE_PIPELINE_ENABLED=false`、`LIGHTTTS_STREAMING_ENABLED=false`、`LIGHTTTS_BISTREAM_ENABLED=false`、`MOSS_TTS_REALTIME_ENABLED=false`。

## 结论

当前 LightTTS/CosyVoice3 真双流为 **NO-GO**，不得部署或启用。

- 13 个中文字符作为第一块提交；人为等待 2 秒，再提交 18 个字符和 `finish`。
- `finish` 前后均未收到 PCM：`0` 块、`0` 字节。
- 请求开始于日志时间 `20:33:09.774`；首 PCM watchdog 在 `20:33:12.774` 精确触发，约 3 秒。
- abort 后请求仍未释放，独立 manager watchdog 在 `20:33:14.855` 向整个进程组发送 SIGTERM，约 2.08 秒恢复宽限。
- 客户端在 5204.2ms 后收到异常关闭；canary 端口关闭，GPU 显存回收，生产实例随后恢复 ready。
- 关闭过程中记录到共享内存和 semaphore 泄漏警告；虽然进程组最终被强制清理，这仍是不能上线的重要证据。

因此，生命周期修复只证明“卡死后能失败并完整重启”，没有证明“能实时合成”。验收要求是 20/20 次在延迟 `finish` 前产生音频、首 PCM P95 ≤800ms；本次第一次真实请求即 0 PCM，直接失败。

## 证据

| 文件 | SHA-256 | 用途 |
|---|---|---|
| `result.json` | `93b810d8c9d187d2c43af816a737f8236ec9358d745ce491ac60cd8a7078faee` | 客户端计时与 PCM 计数；不包含音频或提示词正文 |
| `gpu-canary3.log` | `c4a770b5056d6a247b0f23865739a0d0c30d68a5407aa876284771884fbd7328` | 最终 canary：首 PCM 超时、orphan 恢复、进程组关闭 |
| `gpu-canary2.log` | `bd45946d4d44c876342bcdafb2e798019a8778342b453475e296c48c6dc01f6c` | 修复 readiness request-id 类型前的中间尝试 |
| `gpu-canary.log` | `c5d33530d409b764fbf7c3911ced8c1f6c34c6571661a9c9e7f307239c83d962` | 暴露 NumPy `int64` heartbeat request-id 的初次尝试 |
| `run_bistream_gpu_canary.py` | `e286bc7f2b98bbe50f335085da425a2940dbab1b24541804b26ae6aec045cefb` | 延迟 finish、仅保存时序/计数的复现脚本 |

提示音内容、提示词正文、合成 PCM 和凭据均未写入 QA 证据。`prompt_sha256` 只用于确认同一输入资产。

## 生命周期修复验证范围

真实 GPU canary 已验证：

- 首 PCM 绝对超时可触发；
- abort 后未释放会被标记并进入恢复；
- HTTP endpoint 自身卡住时，独立 manager watchdog 仍能发起完整进程组重启；
- 生产服务能够在隔离 canary 结束后恢复。

尚未通过：

- `finish` 前首 PCM；
- 首 PCM P95/P99；
- PCM 块间隔、30 分钟连续播放；
- 2–3 场并发；
- 8 音色 CER、自然度和漂移。

下一后端应按 `docs/realtime-voice-rebuild.md` 的同一 session/PCM/WebRTC 接口接入，但必须在独立 GPU 上先通过 20/20 延迟-finish canary，才可进入 V2 灰度。
