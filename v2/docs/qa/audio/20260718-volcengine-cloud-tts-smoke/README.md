# 火山引擎双向流式云 TTS 快速验证

验证时间：2026-07-18  
接口：`wss://openspeech.bytedance.com/api/v3/tts/bidirection`  
资源：`seed-tts-2.0`  
音频：24 kHz、单声道、PCM16（按真实返回字节验证）

API Key 只通过进程环境的静默输入提供，未写入源码、命令参数、报告、JSON、WAV 或日志。报告中的 Key 必须保持为空。

## 结论

本轮结果支持把火山引擎双向流式 TTS 进入 V2 集成 canary：

- 长立论单路 3/3、四路并发 4/4 成功。
- 所有长立论均在 `FinishSession` 前返回非静音 PCM，真双向门通过。
- 长立论四路并发首个非静音音频 P95 为 322.203 ms。
- 100 ms 初始播放缓冲模拟中，单路和四路均未出现 underrun。
- 8 个普通话 2.0 音色全部完成合成，没有削波样本。
- `CancelSession` 到 `SessionCanceled` 确认约 45.298 ms。

这只是“允许接入开发”的 GO，不是生产发布 GO。尚缺 FunASR 回转 CER、真人试听 MOS、LiveKit/Chrome 实际播放、打断后浏览器持续静音、20 房间长稳和账号真实并发配额验证。

## 主要结果

| 场景 | 成功 | 真双向 | 首个非静音 P50/P95 | RTF P50/P95 | 100ms underrun |
| --- | ---: | ---: | ---: | ---: | ---: |
| 200+ 字立论，单路 | 3/3 | 3/3 | 307.267 / 620.258 ms | 0.120 / 0.121 | 0 |
| 200+ 字立论，四路 | 4/4 | 4/4 | 284.246 / 322.203 ms | 0.123 / 0.127 | 0 |
| 短辩论文本，单路 | 3/3 | 3/3 | 471.749 / 474.176 ms | 0.223 / 0.260 | 0 |

WebSocket 音频消息的最大到达间隔达到 410–612 ms，但音频块本身包含足够的可播放时长，因此 100 ms 连续播放模拟没有耗尽缓冲。正式浏览器门仍需用 LiveKit 与 `RTCRtpReceiver.getStats()` 验证，不能只依据离线模拟。

## 八音色结果

| 音色 | 首音频 | RTF | RMS dBFS | 峰值 dBFS | 削波 |
| --- | ---: | ---: | ---: | ---: | ---: |
| `zh_female_vv_uranus_bigtts` | 302.277 ms | 0.220 | -20.16 | -7.73 | 0 |
| `zh_female_xiaohe_uranus_bigtts` | 303.081 ms | 0.210 | -20.71 | -4.21 | 0 |
| `zh_female_cancan_uranus_bigtts` | 267.290 ms | 0.238 | -22.19 | -8.46 | 0 |
| `zh_female_qingxinnvsheng_uranus_bigtts` | 286.019 ms | 0.190 | -21.20 | -8.39 | 0 |
| `zh_male_m191_uranus_bigtts` | 301.309 ms | 0.217 | -19.85 | -3.47 | 0 |
| `zh_male_taocheng_uranus_bigtts` | 275.442 ms | 0.198 | -22.50 | -5.42 | 0 |
| `zh_male_liufei_uranus_bigtts` | 283.399 ms | 0.203 | -20.20 | -5.95 | 0 |
| `zh_male_gaolengchenwen_uranus_bigtts` | 272.837 ms | 0.198 | -22.64 | -7.62 | 0 |

这些指标只证明格式、延迟、响度和基础稳定性。音色自然度、情绪、吞字、姓名和术语准确性必须通过 ASR 与人工盲听确认。

## 打断

测试在文本增量输入后发送 `CancelSession`：

- 首音频：340.274 ms。
- Cancel ACK：45.298 ms。
- 取消前已经收到约 3.948 秒可播放 PCM。

因此供应商取消速度满足服务端控制要求，但浏览器仍必须同时清空 LiveKit/播放器中已经缓存的音频。只发送 `CancelSession` 不足以保证用户在 250 ms 内听不到尾音。

## 证据

- `raw/long-c1.json`
- `raw/long-c4.json`
- `raw/short-c1.json`
- `raw/cancel.json`
- `samples/*.wav`
- `samples.sha256`

## 下一轮发布门

1. 将云 Provider 接入现有 `SpeechSynthesisSession`，保持一个发言一个 WS session。
2. PCM 同时写 `.wav.part` 和 LiveKit generation；完成后原子发布 WAV。
3. 运行各辩论阶段语料的 FunASR 回转，CER P95 目标不超过 2%。
4. 使用 8 音色各 20 轮，检查吞字、重复、漂移和人工 MOS。
5. 实测 Chrome/手机首声、弱网、100 ms 缓冲和 250 ms 打断。
6. 创建四个真实房间同时发言，并从控制台确认账号并发配额与计费字符。

官方文档：<https://www.volcengine.com/docs/6561/2532486?lang=zh>
