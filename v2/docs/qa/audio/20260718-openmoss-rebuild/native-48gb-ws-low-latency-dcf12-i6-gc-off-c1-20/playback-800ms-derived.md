# 800ms 连续时钟播放复算

- 来源：同目录 `moss-realtime-session-benchmark.json` 的 20 条真实 `chunk_timeline`。
- 规则：从首个正文 `text_delta` 发送时刻计时；按 `arrival_ms` 到达并累加 `duration_ms`，首次累计音频达到 800ms 时，在使阈值成立的 chunk 到达时起播。起播后维护连续播放时钟；下一块晚于当前播放尾时，差值计为 underrun。
- 20/20 请求均由前两块 `480ms + 560ms = 1040ms` 触发起播。

| 指标 | GC-off | GC-off 前 | 变化 |
|---|---:|---:|---:|
| 首正文→起播 P50 | 1914.243ms | 1670.108ms | +244.135ms |
| 首正文→起播 P95 | 2163.240ms | 2130.871ms | +32.369ms |
| 首正文→起播 max | 2230.155ms | 2137.265ms | +92.890ms |
| 首 PCM→起播 P50 | 783.951ms | 717.926ms | +66.025ms |
| 首 PCM→起播 P95 | 986.091ms | 886.627ms | +99.464ms |
| 首 PCM→起播 max | 1150.093ms | 904.419ms | +245.674ms |
| 有 underrun 的请求 | 1/20 | 2/20 | -1 场 |
| underrun 次数 | 1 | 2 | -1 次 |
| underrun 总时长 | 152.524ms | 1434.280ms | -89.4% |
| 最长 underrun | 152.524ms | 1359.964ms | -88.8% |

唯一 GC-off underrun 为 `request_index=5 / debate_voice_6`。加入当前正文聚合 200ms 硬截止后，推导的首个可朗读字符→服务端连续时钟起播 P95/max 约为 `2363.240/2430.155ms`。该结果未包含真实 LiveKit、WebRTC jitter buffer、Chrome/Safari 解码与扬声器可听延迟，因此只能作为配置选择依据，不能替代浏览器 2.5 秒发布门。

结论：关闭自动循环 GC 显著削减极端停顿，但没有改善模型中位吞吐；800ms 预缓冲把 20 轮中的 19 轮变为无 underrun，仍不足以宣称连续播放发布通过。
