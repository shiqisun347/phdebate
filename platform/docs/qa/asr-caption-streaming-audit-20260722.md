# ASR 双向流式与字幕体验专项审计（2026-07-22）

## 结论

本轮没有部署生产。当前本地实现已经满足以下链路要求：

- 浏览器真人采集使用 `AudioWorklet`，按约 20ms 发送单声道 PCM16，不使用 `MediaRecorder(timeslice)` 或完整 Blob 上传。
- API 在同一个 ASR context 内同时执行浏览器音频上行和 FunASR partial/final 下行；`STOP` 前可收到 partial，结束后等待 final。
- 舞台字幕只有一个 React 渲染源，只展示当前发言的最新短句；不再由 React 和独立 DOM 投影组件同时写同一个 `<p>`，避免重连、暂停和阶段切换时出现空白或旧字幕残留。
- live ASR/caption 事件必须携带当前 `speech_id`；缺失或属于上一轮发言的事件会被丢弃。
- 暂停、阶段切换、比赛结束和发言取消时，字幕投影会回到明确的空闲文案，不保留上一轮字幕。
- ASR 重连采用有界重试；重试预算用尽后不再无限显示“正在重连”，而是保留本次麦克风会话并明确提示结束后核对/补录文字。
- 浏览器 ASR WebSocket 设置 512KiB `bufferedAmount` 上限；弱网上行堵塞时不无限积累 PCM，而是停止继续排队并提示核对文字。
- 公开观战仍然是音频/赛况只读模式，公开 snapshot 不携带 `caption_segments`，匿名 WebSocket 只接收事件 envelope；页面不挂载文字稿入口或字幕节点。

## 代码改动

### 前端

- `apps/web/components/stage-caption-projection.tsx`
  - 新增 `StageSubtitle`，集中负责 projection、播放时间推进、单行裁剪和渲染。
  - `selectCaptionProjection` 对实时事件要求严格匹配当前 active speech 的 `speech_id`，防止迟到事件污染新轮次。
  - 使用小组件隔离 100ms AI 音频字幕计时更新，避免整页因为字幕时钟频繁重渲染。
- `apps/web/components/debate-stage.tsx`
  - 移除 `liveCaption` 状态和直接修改字幕 DOM 的副作用。
  - 辩手页统一渲染 `StageSubtitle`，真人本地 ASR 只传当前短句，完整文字仍保存在 transcript ref 供提交。
  - ASR 有界重连耗尽后展示可行动的失败提示，避免用户误以为系统仍在重连。
  - 对浏览器 WebSocket 增加发送高水位，防止弱网下 PCM 队列无限增长、内存上涨并在很久之后发送过期语音。
- `apps/web/app/rooms/[code]/debate/page.tsx`
  - 移除旧的独立 DOM 投影组件，避免两个渲染源竞争同一舞台节点。
- `apps/web/components/stage-caption-projection.test.tsx`
  - 增加缺少 `speech_id` 的迟到事件拒绝、暂停清空和真人累计 ASR 单行显示测试。

### 后端

- `apps/api/app/api/realtime.py`
  - 修正 ASR 协议错误提示，明确说明需要刷新或文字补录；不再错误地声称系统会保存录音。
- `apps/api/tests/test_platform.py`
  - 更新协议错误和 FunASR 故障提示断言。

## ASR 建连策略审计

当前 ASR WebSocket 是“每个真人 speech 一个隔离 context”，不是房间进入时就永久占用的共享连接：

1. 房间页面进入时预初始化 `AudioContext` 并加载 AudioWorklet；不提前申请麦克风权限。
2. 用户获得当前轮次后，先通过 `/speech/start` 创建并锁定 `Speech`，再建立 `/ws/rooms/:code/asr`。
3. API 校验当前用户、席位、control lease 和 `Speech.status=speaking` 后，才连接 FunASR、发送 `START`/`LANGUAGE` 并返回 `ready`。
4. 一个 speech 完成或中断后释放上游连接和本地 stream；重连只在同一个 speech 内进行，并带 generation 隔离。

这样做牺牲了每轮几十毫秒的 WebSocket 握手换取了席位和比赛隔离：空闲房间不会为每个学生长期占用 ASR 连接，旧轮次也不能把字幕写入新轮次。当前没有发现值得引入“房间级 standby ASR”复杂度的证据；如果 FunASR 以后出现可复用多轮 context 协议，应另做容量和鉴权设计，不能直接复用本轮 speech 连接。

## 验证证据

### 自动化测试

```text
API ASR/caption 定向：34 passed, 160 deselected
Web ASR/caption 定向：22 passed, 86 skipped；有界重连耗尽与发送高水位回归另行各 1 passed
Web production build：通过
Web ESLint：0 errors（已有 12 条历史 warning）
API Ruff：通过
```

定向 Web 覆盖：

- 20ms AudioWorklet PCM、16k 重采样、ready 前预滚、tail flush；
- STOP 前 partial、最终字幕等待、重复 final 去重；
- ASR WebSocket 重连、旧 socket 迟到事件忽略；
- 当前 speech 匹配、字幕单行裁剪、AI 音频时间字幕推进；
- 暂停/新 generation 清空旧字幕；
- 观众页不渲染字幕或文字稿入口。

### 浏览器只读复核

目标：`https://117.50.192.216/rooms/427792/watch`（未执行写操作）。

- 截图：[public-watch.png](asr-caption-streaming-audit-20260722/screenshots/public-watch.png)
- `.subtitle-stage` 数量：`0`
- `.stage-transcript-trigger` 数量：`0`
- `.stage-transcript-drawer` 数量：`0`
- 浏览器错误：`0`
- LiveKit 连接正常；控制台仅有正常连接状态日志，没有异常堆栈。

## 未改变的边界

- 本轮没有修改比赛状态机、计时、TTS、MOSS、LiveKit 播放队列或生产配置。
- 公开观战不会显示任何文字稿；有席位的参赛者仍可在授权页面核对自己的识别结果。
- 浏览器仍需用户主动点击发言以获得麦克风权限；这是隐私和浏览器策略要求，不应改为自动录音。

## 后续建议

1. 在真实学生设备上采集固定语料，测量首个 partial、最终 final、断线重连和中文 CER；本地单元测试不能替代真人麦克风质量。
2. 在一个隔离测试房间注入 FunASR 延迟、半开连接和重复 final，观察房主暂停/恢复操作是否符合比赛控制台预期。
3. 如果未来需要房间级预建 ASR，先为 FunASR 定义明确的 standby/bind 协议和连接上限，再实现；不能绕过当前 speech 鉴权。
