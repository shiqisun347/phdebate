# MOSS 残余杂音与运行参数漂移修复

日期：2026-07-19

## 结论

本轮没有增加低通、降噪、限幅、实时去点击或更大的播放缓冲，也没有修改模型、音色、
LiveKit、Opus、AudioWorklet 和 5 ms 边缘斜坡。

实际问题是生产 MOSS Supervisor 配置偏离了已验收的可靠基线：

- 异常运行参数：`decode_chunk_frames=12`、`do_sample=false`；
- 可靠基线参数：`decode_chunk_frames=3`、`do_sample=true`。

恢复可靠参数后，短文本、长文本和真实 Agent → MOSS → LiveKit → Chromium 链路均能
自然结束，源音频和浏览器解码轨道未再出现同等级的瞬时撕裂。

## 修复前复现

QA 房间 `#612684` 的反方立论使用 `debate_voice_8`。正文完成后，MOSS 仍持续生成超过
200 秒，最终因 `final_ack_timeout` 失败：

- 服务端临时 PCM 已超过 192 秒，仍未自然释放；
- 尾段源 PCM 最大相邻采样跳变约 `1.3996`；
- Chromium 解码轨道 52.95 秒采样中出现一次约 `0.7901` 的瞬时跳变；
- 同一 RTC 连接累计丢包为 `0`，检测时 concealment 为 `0`；
- 房间产生 `provider.failed` 并安全暂停，随后仅终止本次 QA 房。

这说明主要异常早于浏览器播放后处理，不能靠扩大缓冲或低通滤波正确修复。

## 已实施调整

生产 Supervisor 仅恢复以下两个可靠参数：

```text
MOSS_GATEWAY_DECODE_CHUNK_FRAMES="3"
MOSS_GATEWAY_DO_SAMPLE="true"
```

保留不变的部分包括：

- `initial_chunk_frames=6`、同步 decoder；
- MOSS 模型、Codec、8 个参考音色和席位映射；
- 24 kHz PCM、48 kHz LiveKit、64 kbps mono Opus；
- 服务端和浏览器缓冲、RED、DTX、打断保护；
- 浏览器 AudioWorklet，SHA-256 仍为
  `de373b01d9b5587bdbaa38115227d99a21b2ec3645f3046bfbda5068663ba219`。

## 修复后结果

### 直接 MOSS 会话

| 样本 | 音频时长 | 首 PCM | 最大相邻跳变 | `>0.4` 跳变 | 削波 |
|---|---:|---:|---:|---:|---:|
| 短文本 | 10.72 s | 362.3 ms | 0.1169 | 0 | 0 |
| 长文本 | 57.28 s | — | 0.2132 | 0 | 0 |

两次会话均收到 `released`，结束后 MOSS 为 `active=0`、`pending=0`、
`orphan_count=0`。

### 真实生产链路

QA 房间 `#287138` 完成真实 Agent → MOSS → LiveKit → Chromium 发言：

- 完整源 WAV：83.12 秒，自然完成；
- 源 WAV 峰值：-6.11 dBFS，无削波；
- 源 WAV 最大相邻跳变：0.2482，`>0.4` 为 0；
- 浏览器连续采样：85.80 秒；
- 浏览器最大块内相邻跳变：0.1470；
- 浏览器 `>0.18`、`>0.25`、`>0.4` 瞬时跳变均为 0；
- 传输为 `audio/opus`，活跃语音约 54–62 kbps；
- RTP 丢包为 0；启动 concealment 为 3120 samples（约 65 ms），之后整轮不再增长；
- QA 房测试后已终止，没有遗留 MOSS 会话或比赛处理任务。

## 防回归

`deploy/verify_gpu_voice_runtime.py` 现在会同时校验生产 Supervisor 的可靠运行参数。
若以后再次出现 `12/false`、异步 decoder 或错误首块配置，GPU 语音验收会直接失败，
不会只因进程在 GPU 上且健康接口返回 200 就误判通过。

- 定向自动化：10 项通过；
- 生产运行检查：通过；
- 当前系统健康：数据库、Redis、Engine、Worker、FunASR、MOSS 均正常；
- 当前 `active_match_processing=false`。

## 备份与回滚

修复前后 Supervisor 配置、验收 WAV 和旧检查脚本保存在：

`/home/ubuntu/sunsq/backups/phdebate-goal-progress-20260718T223000Z/audio-runtime-drift-20260719T004519Z`

- 修复前配置 SHA-256：
  `89fc1ea289891d7b3e76e0c219bae7c8f835b944a45cc77b5dae0b588209940f`；
- 修复后配置 SHA-256：
  `1b44ea731dbdcde5fedc96de39a2a32a64ef128ffe3aeb8abb01ba10ccaa4d85`。

若后续人工试听认为可靠基线反而更差，可恢复备份中的 `before` 配置并重启单个 MOSS
Supervisor 项；无需回滚 Web、API、数据库或音频文件。

## 当前边界

本轮证据支持保留可靠基线，不支持继续加入实时滤波。若仍听到杂音，请记录房间号、
席位和大致时间；下一次应按同样方法对齐源 WAV、RTC stats 和浏览器 PCM，而不是对整条
链路做无依据的大改。
