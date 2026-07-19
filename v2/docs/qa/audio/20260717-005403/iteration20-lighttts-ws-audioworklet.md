# Iteration 20：LightTTS bi-stream、独立音频 WebSocket 与 AudioWorklet 审计

更新时间：2026-07-17 18:27 CST。状态：**NO-GO；仅审计本地候选，未部署、未修改生产配置。**

## 审计范围

本轮按用户要求把 TTS、WebSocket 和网页播放视为核心发布门。审计对象是工作区中尚未部署的候选链路：

```text
LightTTS /inference_zero_shot_bistream
  → engine 追加 generation-scoped .wav.part
  → 独立 /ws/rooms/{code}/audio 尾随共享文件并发送 PCM
  → 浏览器 AudioWorklet 环形缓冲
  → EOF 后修补 WAV 头并原子发布最终归档
```

本轮只做代码、协议、自动化和容量证据审计；没有继续实现，没有部署，没有开启 `LIGHTTTS_STREAMING_ENABLED`，也没有修改生产 `LIGHTTTS_MAX_ACTIVE=1`。

## 阻断结论

### P0-1：late join / 重连游标可能越过 growing part 的 live edge

- 前端依据 `Date.now() - playback_started_at` 推导 `after_seq`；服务端直接把它换算成 WAV data offset，没有按当前 `.part` 已写入的完整帧数 clamp。
- 当合成速度慢于实时、上游中途 stall，或 final-ready 触发重连时，请求游标可能落在文件未来位置。
- 若 final WAV 仍短于该游标，客户端可收到零 PCM 和从虚构 PTS 推导的 `total_samples`；当前 generation 仍有效时不会及时切回普通 WAV。
- 必须由服务端返回 authoritative live-edge PTS，并把 future seek clamp 到 `max(0, complete_frames - prebuffer_frames)`；过旧或不连续游标必须显式 gap/reset，不能静默伪造连续时间线。

### P0-2：小于 400ms 的最终尾音会永久静音

- Worklet 只有在 `enqueue` 时检查是否达到 400ms prime 水位；`end` 只设置 `ended=true`。
- late join 或 final 尾包不足 400ms 时，最后一次 enqueue 后再到 end，状态会永久停在 `available>0 / primed=false / ended=true`，既不播放也不发送 drained。
- Node 状态机仿真已稳定复现：4800 samples 留在 ring，messages 为空。
- `end` 时只要仍有缓存就必须允许 tail-prime，或在 `process()` 中显式处理 `ended && available>0`。

## 其他开放缺陷

### P1

1. 自由辩论计时：首 PCM 已恢复自由辩论剩余 deadline；final WAV 再次清 preparation 得到 `None` 后，会把赛段 deadline 覆盖为本条 AI 音频结束时间，可能使 300 秒自由辩论约十几秒后提前结束。
2. 缺少传输 pacing/high-water：API 可把 growing file 中当前可读内容连续灌入 socket，主线程也会无界投递 MessagePort；3 秒 ring 可能 overflow。重试计数又在每次 `started` 立即清零，形成 started→overflow→重连风暴而不进入可靠 fallback。
3. Safari/Chrome 自动播放：Worklet 的“缓冲已 prime”不等于 AudioContext 已实际 running；若页面尚未用户手势解锁，UI 可能错误清除“需要开启声音”，同时后台继续收流直至 overflow。
4. 初始 subscribe 未先验证 JSON 是 object；数组、null 或畸形输入可能触发未处理异常。
5. standalone engine 收到 SIGTERM 时未主动 stop/drain admission lease；活动 TTS 期间重启可能遗留最长约 330 秒的 Redis 租约。
6. engine restart 虽会清 DB generation 和 part，但只落库 abort/event、不主动 publish room snapshot；普通 room WS 最坏约等 20 秒心跳才刷新。
7. 上游取消仅以关闭 bi-stream WS 作为完成条件，尚无服务端 abort acknowledgement 或 GPU 推理确实停止的探针证据。

### P2

- 初始化期间 unmount/dispose 可能留下晚完成的 AudioContext/Worklet；首次用户点击恰逢 `addModule()` 也存在竞态。
- 自动退避期复用全局 playback pending 会禁用声音按钮，用户反而无法静音或重新手势解锁。
- 24-byte binary header 只传 generation 的 32-bit 截断值；socket 虽绑定完整 generation，但协议层缺端到端完整 identity。
- replay flag 在整个重连 socket 中保持 1；`final_seq` 在 partial 与整帧结尾语义不一致；final/abort 控制帧缺少 URL、duration 或 reason。
- final rename 后未 fsync 父目录，普通进程崩溃和并发读取原子性正常，但断电级持久性不完整。

## 已确认的正向证据

- LightTTS 官方 bi-stream 的 prompt→分段 text→finish→PCM 接收顺序与候选客户端一致。
- bi-stream PCM 与最终 WAV 内容一致；取消路径会 abort、删除 part，且不发布 final。
- 独立音频 WS 不占用现有 room snapshot 高频队列；每个客户端使用独立文件描述符与发送超时。
- 每秒复验 session、用户 active、房间查看权限和 generation；公开房转私密后现有匿名音频连接会关闭。
- active snapshot 持久 generation、sample rate 和 `playback_started_at`，单次 started event 丢失仍可 discovery。
- feature flag 默认关闭；生产保持完整 WAV + HTMLAudio 路径和 `LIGHTTTS_MAX_ACTIVE=1`。

## 已执行测试

- Provider/API/engine 定向：5 passed；另一次 stream 相关筛选 14 passed；engine restart 1 passed。
- Web `PcmStreamPlayer + DebateStage`：78 passed；另一组 UI 定向合计 82 passed。
- 既有完整门禁记录：API 290 passed、Web 182 passed、TypeScript/Ruff/py_compile/Next build 通过。
- 以上测试均没有覆盖并捕获两个 P0，因此“现有测试全绿”不能作为启用依据。

## 2–3 场并发容量结论

- 生产直接 `stream=true` 基准：单路首 PCM P95 1.259s；两路 6.333s、RTF P95 1.795；三路 10.983s、RTF P95 2.651，呈近似串行。
- 当前生产机同机第二实例 NO-GO：单实例约占 5.1/12 GiB GPU、约 10.1 GiB PSS，主机无 swap；同机双开没有安全余量。
- LightTTS 上游虽支持 `running_max_req_size` 和 LLM 批处理，但 `decode_max_batch_size` 的官方 CLI 明确注明当前只支持 1；只把应用 active 改成 2/3 不能证明 token-to-wave 解码可并行实时。
- 生产 active=2 冷切也 NO-GO：服务真实 ready 约需 80 秒，Supervisor `RUNNING` 早于模型真实可用；没有独立证据时不得以生产重启做容量实验。
- 唯一可接受路径是在独立 GPU/主机用相同 commit/model/config 做 active=2/3，记录 TTFT、RTF、GPU 显存/利用率、欠载、排队公平、取消和 FunASR 争用；生产继续 active=1。

## GitHub / 官方实现交叉依据

- LightTTS 官方仓库提供 HTTP streaming、WebSocket bi-stream、TTFT/RTF 基准，并把 encode、LLM、decode 拆为独立进程；其公开 4090D 基准在 2/4 workers 下 stream TTFT P90 约 1.53/4.37 秒，说明“请求成功”与“多路实时”是两件事。
- LightTTS 官方 `api_cli.py` 暴露 `running_max_req_size`、`router_max_wait_tokens`，但 `decode_max_batch_size` 注释为当前仅支持 1，需要针对真实 token-to-wave 解码瓶颈验证。
- GoogleChromeLabs Web Audio 样例把 WebAudio 128-frame render quantum 与其他块大小之间用有界 ring 解耦；不足输出时明确产生静音，而不是假设网络永不 burst/stall。
- MDN/WebKit 都要求从真实用户手势内 create/resume/play，并检查 `AudioContext.state` 或 `play()` rejection；“数据已经进入 Worklet”不能证明 Safari 已经发声。

一手资料：

- https://github.com/ModelTC/LightTTS
- https://github.com/ModelTC/LightTTS/blob/main/test/test_bistream.py
- https://github.com/ModelTC/LightTTS/blob/main/light_tts/server/api_cli.py
- https://github.com/FunAudioLLM/CosyVoice
- https://github.com/GoogleChromeLabs/web-audio-samples/tree/main/src/audio-worklet/design-pattern
- https://developer.mozilla.org/en-US/docs/Web/API/Web_Audio_API/Best_practices
- https://webkit.org/blog/7734/auto-play-policy-changes-for-macos/

更完整的 Deepgram、Cartesia、ElevenLabs、OpenAI Realtime、LiveKit、Twilio、hls.js、Pipecat、LightTTS/CosyVoice 容量与 Safari/Chrome 发布门矩阵见 [Iteration 20 GitHub/官方实现证据矩阵](iteration20-github-realtime-audio-evidence-matrix.md)。成熟实现共同说明：`sent != buffered != played`，建议增加约 250ms 的 `playback.ack(played_pts_samples, buffered_samples)`，并把 fresh join 的 accepted start 完全交给服务端 live edge。

## 启用门槛

1. 关闭两个 P0，并增加 future live-edge、slow synthesis、growing part→rename 和 sub-400ms tail 自动化。
2. 增加 server pacing/high-water、客户端稳定窗口与有限重试；覆盖 suspended AudioContext 超过 ring 容量、页面隐藏/恢复和慢 MessagePort。
3. 修复自由辩论 deadline、malformed subscribe、SIGTERM lease drain，并验证上游断连确实停止 GPU。
4. 在独立 GPU 完成 active=2/3；不得在当前生产机同机双开或直接冷切试验。
5. 通过 Chrome 与 Safari Computer Use 的真实首声、静音/恢复、断线追赶、短尾音、退出/暂停/结束比赛回归后，才允许 QA 房灰度。

当前决策：`LIGHTTTS_STREAMING_ENABLED=false`；**不部署、不启用、不提高生产并发。**
