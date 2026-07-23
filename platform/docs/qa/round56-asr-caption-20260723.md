# Round 56：ASR 与单行字幕专项审计

日期：2026-07-23  
范围：真人浏览器采音、FunASR WebSocket 桥、最终文字持久化、舞台单行字幕、文字发言恢复入口。

## 结论

当前真人语音主链路已是双向流式：浏览器使用 `AudioWorklet` 连续采集浮点 PCM，流式重采样为 16 kHz 单声道 PCM16，经同一个 WebSocket 持续上行；后端同时向 FunASR 写入音频并读取 partial/final。生产代码不使用 `MediaRecorder`、Blob 分片或真人录音上传。

本轮修复了两个会影响真实比赛的竞态：

1. ASR 断线重连过去会销毁并重建 AudioWorklet，重连间隙的语音没有采集；断线前尚未 final 的文字也可能被新连接的 final 静默覆盖。
2. 文字发言提交成功或阶段切换后，旧的“改用文字完成本轮发言”弹窗可能继续覆盖新阶段。

两项均已修复并增加精确回归测试。

## 当前主链路

```text
麦克风 MediaStream
  → 预加载的 AudioContext + AudioWorklet（20 ms 连续块）
  → 连续重采样到 16 kHz / mono / PCM16
  → 房间 ASR WebSocket
  ⇄ FastAPI ASR bridge
  ⇄ FunASR WebSocket（同一发言上下文内持续读写）
  → partial 单行字幕
  → final 写入 Speech + TranscriptSegment + CaptionSegment
  → speech/finish 提交权威完整文字
```

浏览器进入比赛页面时会预加载 AudioContext 与 worklet 模块；只有用户点击开始发言后才请求麦克风权限、创建权威 speech，并立即建立 ASR WebSocket。这样不会在未获得用户操作和服务端轮次授权前占用麦克风。

## 本轮实现

### 1. ASR 协议绑定具体发言与阶段

- ASR wire protocol 升级为 v2。
- authenticate 必须包含 `speech_id`、`stage_key`、编码、采样率和席位控制 lease。
- 后端在连接 FunASR 前校验 speech、stage、seat、用户、lease 和当前房间状态。
- ready 回包再次携带 `speech_id`、`stage_key` 和协议版本；浏览器发现不匹配会立即停止该连接并要求核对文字。
- 解决同一席位快速进入下一阶段时，旧连接误绑定新 speech 的风险。

### 2. 同一真人发言只保留一个采集图

- ASR WebSocket 临时断开时，不再停止 AudioWorklet、不再断开 MediaStream source，也不重新创建 AudioContext。
- 重连期间仍连续采集 PCM，并使用已有的 2 秒有界 preroll 缓冲。
- 新 WebSocket 通过鉴权和 upstream ready barrier 后，按原顺序补发缓冲 PCM。
- 重连仍限制为 3 次指数退避；超过预算后保留明确的人工文字核对措施，不出现无限“正在重连”。

### 3. 断线前 interim 不再静默丢失

- 连接断开时，把当时尚未 final 的 partial 保留到本轮本地文字基线。
- 新连接恢复后继续识别后半段。
- 由于断线前 partial 不是可信 final，本轮结束后强制打开文字核对，不自动静默提交。
- 旧 socket 的迟到消息、旧 worklet generation 和错误 stage/speech id 都不能更新当前字幕或提交内容。

### 4. PCM 与控制帧校验

- 后端拒绝空 PCM、奇数字节 PCM 和超过 64 KiB 的单条音频消息。
- 只接受精确的 `{"type":"finish"}` 控制消息。
- tail flush 完成后才发送 finish；WebSocket 顺序保证最后一块 PCM 位于 STOP 之前。
- upstream final 返回前保持读任务存活，final 经数据库行锁校验 speech 仍为 `speaking` 后才持久化。

### 5. 电视剧式单行字幕

- 舞台只有一个字幕 DOM 节点。
- 只投影 active `speech_id` 的最新 ASR/Agent 片段。
- 累积 ASR hypothesis 经 `compactCaptionLine` 只保留最新可读短句，默认上限 28 个字符。
- CSS 强制 `white-space: nowrap`、溢出隐藏和省略；不会形成多行文字墙。
- pause、terminate、stage/speech/generation 切换后立即清除旧字幕。
- 匿名观众和观战页继续不显示任何文字稿或实时字幕。

### 6. 旧文字弹窗自动清理

- 绑定 speech 完成、被中断、超时、被其他 speech 替换、阶段/席位变化或比赛进入终态时，旧文字弹窗立即停止渲染。
- 同一清理流程删除 binding、草稿、错误状态和幂等键，下一阶段不会继续显示“改用文字完成本轮发言”。
- 正常提交成功后仍由提交响应路径立即关闭并清空。

## 恢复措施

| 情况 | 用户看到的处理方式 |
|---|---|
| 麦克风权限被拒绝 | 明确提示在浏览器站点权限中开启麦克风；也可使用文字发言 |
| ASR 短暂断线后恢复 | PCM 有界缓存并补发；保留断线前文字，结束后必须核对 |
| ASR 三次重连失败 | 麦克风发言可结束，系统保留已有文字并要求手动补充 |
| final 超时或低可信度/静音 | 不静默提交，打开文字核对框 |
| speech/stage 已变化 | 旧音频和旧消息被拒绝；旧文字弹窗自动关闭清空 |
| 真人断线 60 秒 | 比赛自动暂停，不由 AI 接管；重连后由房主继续 |

## 验证结果

### API

- ASR/字幕/断线专项：`32 passed`
- API 全量：`570 passed, 1 xfailed`
- Ruff：通过

覆盖内容包括：

- 20 ms PCM 在 STOP 前持续上行，同时收到 partial。
- upstream final 等待与持久化。
- 重复 final 幂等。
- 错误 speech/stage identity 在 upstream 连接前拒绝。
- 设备 lease 变化、暂停和 speech interruption 后的迟到 final 拒绝。
- 静音、低置信度、过短幻觉拒绝。
- 畸形 PCM 不转发。
- 房间和观众投影隔离。

### Web

- Web 全量：`350 passed, 21 skipped`
- ASR、字幕与文字恢复专项：`94 passed, 21 skipped`
- ESLint：通过
- Next.js production build：通过

新增关键断言：

- ASR 重连前后只有一个 AudioWorklet 实例。
- 重连期间产生的 PCM 在新 ready 后补发。
- 断线前 partial 与重连后 final 同时出现在人工核对框中。
- 旧 socket 的迟到字幕不显示。
- protocol v2 authenticate 包含正确 speech/stage identity。
- stage/speech 变化后旧文字弹窗和草稿自动消失。

## 仍需生产验收

自动化测试使用可控的 PCM 和 WebSocket 双端替身，已经证明竞态与状态边界；部署后仍应使用真实 Chrome 麦克风完成一次固定发言和一次自由辩论，检查：

1. FunASR partial 首次出现时间和连续性。
2. 人为切断网络 0.5–1 秒后的 PCM 补发与人工核对内容。
3. 点击结束后最终离线结果返回时间。
4. 1920×1080 和 390×844 下字幕始终只有一行。
5. 提交文字后下一阶段不再出现旧恢复弹窗。

此次未修改大厅、单场控制台或全局视觉样式。

## 文件清单

- `apps/api/app/api/realtime.py`
- `apps/api/tests/test_platform.py`
- `apps/api/tests/test_round46_disconnect_flow_boundaries.py`
- `apps/web/components/debate-stage.tsx`
- `apps/web/components/debate-stage.test.tsx`
- `apps/web/components/text-speech-fallback.tsx`
- `apps/web/components/text-speech-fallback.test.tsx`
- `apps/web/components/text-speech-fallback.module.css`
- `scripts/verify_asr_bridge.py`
- `scripts/verify_parallel_asr_streams.py`
