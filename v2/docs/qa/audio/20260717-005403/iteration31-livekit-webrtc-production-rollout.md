# Iteration 31：LiveKit WebRTC 正式下行安全发布与浏览器回归

- 时间：2026-07-18 02:33–03:30 CST
- 目标：把正式 AI 音频下行从浏览器 PCM/WAV 播放切到 LiveKit WebRTC/Opus，同时保留已证实安全的一次性 LightTTS 生成路径；不得启用会卡死的 LightTTS bi-stream。
- 结论：Chrome 与 Safari 已成功建立正式 LiveKit 房间连接并完成用户手势声音解锁；WebRTC transport 可用。当前仍等待完整 WAV 才开始发布，不满足低延迟真双流门，因此已按 `docs/realtime-voice-rebuild.md` 进入下一阶段重构。

## 变更

- 后端新增每房间长驻 `agent-tts` publisher、48kHz mono 20ms PCM 帧、100ms `AudioSource` 队列、120ms 应用队列、连续时钟、finish drain 与 abort 双清队列。
- API 新增 subscribe-only 短 TTL `/api/rooms/{code}/rtc-token`；API 只签 token，Engine 独占 publisher。
- 前端新增 `livekit-client 2.20.1`、房间进入预连接、隐藏音频元素、声音解锁、220ms interrupt flush；实时 ASR 改为 AudioWorklet，MediaRecorder 仅用于归档。
- WS PCM 回退播放器由固定 400ms 改为初始 100ms、80–240ms 自适应缓冲。
- LightTTS 安全过渡：`LIGHTTTS_STREAMING_ENABLED=false` 时继续使用稳定 HTTP 一次性生成，完成 WAV 后再送入 LiveKit 连续时钟；绝不触发已知会产生 orphan 的 bi-stream。
- publisher 改为可见参与者，确保浏览器在收到 `agent-tts` track 前先获得 participant 更新。
- 浏览器显式 `singlePeerConnection:false`，规避 LiveKit Server 1.13.3 尚未包含的 single-PC answer 路由修复；保留 `prepareConnection()` 做 DNS/TLS 预热。
- Web release 构建会将上一版 `.next/static` 的 immutable chunks 以 `--ignore-existing` 合并到新 release，避免比赛中发布导致旧标签 `ChunkLoadError`。

## 生产配置

```dotenv
ENGINE_ENABLED=false
WEBRTC_AUDIO_ENABLED=true
WEBRTC_AUDIO_BACKEND=livekit
LIVEKIT_URL=ws://127.0.0.1:7880
LIVEKIT_PUBLIC_URL=wss://117.50.218.251
NEXT_PUBLIC_WEBRTC_AUDIO_ENABLED=true

LIGHTTTS_STREAMING_ENABLED=false
LIGHTTTS_BISTREAM_ENABLED=false
REALTIME_VOICE_PIPELINE_ENABLED=false
MOSS_TTS_REALTIME_ENABLED=false
```

LiveKit key/secret 已配置但不记录。`LIGHTTTS_MAX_ACTIVE=1` 和 Redis 全局 gate 保持原值。

## 部署与回滚

- 部署前备份：`/home/ubuntu/sunsq/phdebate-v2/runtime/deploy-backups/20260717-183325`
- 数据库备份：2026-07-18 02:33 CST，ready 报告记录 192,380 bytes。
- Python release：`/home/ubuntu/sunsq/phdebate-v2/.python-venvs/20260717T1845Z-webrtc-safe`
- 首个 WebRTC Web release：`runtime/web-releases/20260717T1848Z-webrtc-safe`
- 双 PC 修复 Web release：`runtime/web-releases/20260717T1920Z-livekit-dualpc`
- 前一 Python runtime：`.python-venvs/20260716-agent-gateway`
- 前一 Web 指针已记录在 `runtime/deploy-backups/20260717-183325/web-current.before.txt`。

## 本地验证

- API 全量（部署前安全过渡）：`341 passed, 22 warnings`
- 新 publisher 可见性专项：`10 passed`
- Web 全量：`28 files / 192 tests`
- LiveKit 前端专项：`1 passed`
- TTS 质量门脚本：`6 passed`
- Ruff、TypeScript、Next production build：通过
- 真实外部 SFU Python 媒体 smoke：5/5 房间收到非零 48kHz mono 音频。

## 浏览器发现与闭环

### 1. 公开 URL 重复 `/rtc`

- 失败：`LIVEKIT_PUBLIC_URL=wss://117.50.218.251/rtc` 导致 JS 客户端继续追加 RTC path，Chrome 控制台持续出现 `v1 RTC path not found`。
- 修复：改为根地址 `wss://117.50.218.251`，Nginx 继续代理 `/rtc`。

### 2. Chrome single-PC SDP/transceiver 失败

- 失败：`setLocalDescription/createOffer: Transceiver not found based on m-line index`，Server 同时出现 ICE/DTLS 协商冲突。
- 根因：Server 1.13.3 早于 LiveKit PR #4680；默认 single-PC subscriber answer 路由存在已知缺陷。
- 修复：前端 `singlePeerConnection:false`，恢复成熟的双 PeerConnection 模式；不部署未正式 release 的 Server main 二进制。
- 详细证据：[Chrome LiveKit transceiver 诊断](livekit-chrome-transceiver-diagnosis.md)。

### 3. 隐藏 publisher 的 participant/track 顺序风险

- 失败信号：Chrome曾记录 `Tried to add a track for a participant, that's not present`。
- 修复：`agent-audio:{room}` publisher 不再使用 hidden grant，浏览器可先接收 participant，再订阅 `agent-tts`。

### 4. Safari 旧标签 ChunkLoadError

- 失败：发布新 Web release 后，Safari 旧页面请求上一版 `3wwitj-97ja54.js`，新静态根没有旧哈希文件，页面提示已回退兼容播放。
- 修复：把上一 release immutable chunks 合并进当前 release，并固化到 `deploy/build-web-release.sh`。
- 回归：旧 chunk 返回 HTTP 200；Safari 错误文案消失，声音开关从 off 成功切为 on。

## Computer Use 生产证据

- [Chrome 首页](../../screenshots/20260717-005403/TC-RTC-01-Chrome-首页-WebRTC发布后.png)
- [Safari 首页](../../screenshots/20260717-005403/TC-RTC-02-Safari-首页-WebRTC发布后.png)
- [Chrome 双 PC 页面](../../screenshots/20260717-005403/TC-RTC-03-Chrome-dualPC回归.png)
- [Chrome WebRTC 已连接并解锁](../../screenshots/20260717-005403/TC-RTC-04-Chrome-WebRTC已连接并解锁.png)
- [Safari WebRTC 已连接并解锁](../../screenshots/20260717-005403/TC-RTC-05-Safari-WebRTC已连接并解锁.png)

生产 LiveKit room service 同时看到：

- `agent-audio:214317`，active，发布 `agent-tts`。
- 浏览器用户 participant，active。

双 PC 修复后没有新增 `Transceiver not found`、conflicting ICE、DTLS timeout 或 participant restart 错误。

## 残余风险与切换决策

- 当前安全过渡必须等待 Agent final 和完整 LightTTS WAV，无法满足“LLM 首字后浏览器 2.5 秒内出声”。
- 生产 LightTTS bi-stream 仍会 0 PCM 卡死、disconnect 后遗留 orphan、readiness 假绿，所有增量开关继续关闭。
- 房 214317 因独立 Debate Agent 500 已安全暂停；该失败与 WebRTC transport 无关，本轮未代替房主重试或终止。
- 按用户指令，当前效果仍不满足核心实时性能，下一阶段严格执行 `docs/realtime-voice-rebuild.md`：先修 LightTTS lifecycle/orphan/readiness，再拆分 `services/voice_runtime/`、建立 8 固定音色和真双流质量门。

