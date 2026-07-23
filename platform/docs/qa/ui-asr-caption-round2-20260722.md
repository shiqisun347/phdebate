# 辩手/观战舞台 UI 与 ASR Round 2 审计

日期：2026-07-22  
范围：`apps/web` 辩手页、公开观战页、自由辩论举手区、实时字幕投影，以及 ASR 浏览器到服务端的双向流协议

## 结论

本轮修复了两个会直接影响比赛推进的界面问题：

1. 自由辩论举手面板原来使用低于 `.stage-page` 的层级，可能被整块比赛舞台盖住；现在面板明确位于舞台之上，并固定在底部控制栏上方。
2. 字幕原来通过横向滚动追到长文本尾部，长句更新时会产生跳动和“不是电视剧字幕”的感觉；现在由单一投影节点渲染、固定一行、超出时省略，不再横向滚动。

同时将赛前状态从“正在连接比赛服务/自动”改成“比赛准备中/准备中”，避免把未开始的等待误解为计时已经开始。

## ASR 双向流核验

当前 ASR 不是 `MediaRecorder` Blob 上传，也不是先录完再识别：

- 浏览器使用 `AudioWorklet` 以 20ms PCM 帧采集，统一转换为 16kHz、单声道、`pcm_s16le`。
- 浏览器通过一个 ASR WebSocket 持续上行 PCM；同一连接同时接收 `partial` 和 `final` 识别结果。
- 服务端 `asr_websocket` 使用 `browser_to_asr` 和 `asr_to_browser` 两个并发任务，浏览器继续发送音频时即可向浏览器下发实时识别结果。
- 发送 `finish` 后保持上游连接等待最终识别，不会因为上行任务结束立即取消下行任务。
- 浏览器对旧 WebSocket 的迟到结果进行 generation 校验，重连后不会把旧字幕写入新发言。
- 浏览器对 socket `bufferedAmount` 设有 512KiB 高水位，网络发送缓慢时转为人工核对，而不是无限积累 PCM。

对应证据：

- API `test_asr_bridge_forwards_20ms_pcm_while_streaming_partial_results`：25 个 20ms、640 字节 PCM 帧在同一上下文中收到 partial，并在 `finish` 后收到 final。
- Web `DebateStage > reconnects the duplex ASR stream during capture and ignores late frames from the old socket`：旧连接迟到字幕被丢弃。
- Web `DebateStage > bounds the browser ASR socket buffer instead of accumulating PCM indefinitely`：高水位保护通过。

## UI 改动

### 字幕

文件：`apps/web/components/stage-caption-projection.tsx`、`apps/web/app/globals.css`

- `StageSubtitle` 是舞台唯一的字幕 DOM 节点，保留严格的 `speech_id` 匹配和音频时间投影。
- 新增 `data-caption-line="single"` 作为可测试的产品契约。
- 去除 `scrollLeft = scrollWidth` 和横向滚动；使用 `overflow: hidden`、`text-overflow: ellipsis`、`white-space: nowrap`。
- `compactCaptionLine` 继续只取累计 ASR 假设中的最新短句，最大 28 个字符；完整文字仍保留在发言提交路径中，不影响记录。
- ASR 返回空文本时清除上一条舞台字幕，避免结束或短暂空结果后旧句残留。
- 浅色舞台的字幕卡片收紧为桌面 112px、手机 82px，避免遮住计时和舞台主体；临时识别和“正在聆听”使用不同的浅蓝色状态。

### 举手与底部控制

文件：`apps/web/components/free-turn-queue.module.css`

- 举手面板 `z-index: 91`，高于固定舞台 `z-index: 80`。
- 桌面端距离底部控制栏 118px，移动端距离底部控制栏 176px，并保留安全区间距。
- 交互模式默认只展示“举手/取消举手”动作，队列明细继续折叠；观战模式只读展示。
- 原有底部主按钮（开始/结束发言、声音、全屏、操作）未移出 `stage-controls`，不改变 WebRTC 单音轨或 no-AI-takeover 控制逻辑。

### 赛前提示

文件：`apps/web/components/debate-stage.tsx`

- 标题改为“比赛准备中”。
- 计时圆环显示“准备中”，不再显示“自动”这种无法判断含义的数字替代文案。
- 辅助提示改为“席位已锁定，系统正在准备实时语音和开场提示；准备完成后会自动进入第一阶段。”

## 验证结果

### Web 组件测试

命令：

```bash
cd platform/apps/web
npm test -- --run \
  components/stage-caption-projection.test.tsx \
  components/debate-stage.test.tsx \
  components/free-turn-queue.test.tsx
```

结果：3 个文件通过，81 个用例通过，21 个按环境跳过；无失败。

重点覆盖：

- 单一字幕节点、暂停/换代清屏、累计 ASR 只显示最新短句。
- ASR 双向流重连、迟到帧隔离、结束信号和 socket 高水位。
- 固定底部举手动作、队列折叠、观战只读、三秒申请窗口。
- 只有服务端授予当前轮次和当前设备控制权时才启用发言按钮。
- 60 秒真人断线自动暂停提示，不显示 AI 接管选项。

### API 双向 ASR 测试

命令：

```bash
cd platform/apps/api
../../.venv/bin/pytest -q tests/test_platform.py \
  -k 'asr_bridge_forwards_20ms_pcm_while_streaming_partial_results or asr_websocket_accepts_current_device_lease'
```

结果：3 passed，188 deselected；仅有 Starlette/httpx 的既有弃用警告。

### 构建与静态检查

- `npm run build`：通过，Next.js 编译、TypeScript、静态页面生成均通过。
- `npm run lint`：0 errors；12 条既有 warning（主要是旧测试变量和 hooks 依赖提示），本轮没有新增错误。
- 生产首页 `https://117.50.192.216/` 可达，赛事大厅、赛事卡片、登录和排行榜入口正常加载。

## 尚未在本轮声称完成的项目

- 没有伪造活跃比赛或强行改动生产房间，因此没有把“生产真实麦克风/扬声器音质”冒充为本轮 UI 测试结果；应使用真实登录席位继续做一场完整比赛回归。
- 当前改动只改变界面和浏览器字幕投影，不改变 ASR 上游服务、WebRTC 单音轨、LightTTS 或房间状态机。
- lint 中的 12 条 warning 仍需另开代码清理任务处理；它们不阻塞本轮构建。

## 后续建议

1. 使用两个真人席位和一个匿名观战连接跑完一场自由辩论，确认举手面板与底部控制在 1920×1080、iPhone 宽度下都不遮挡字幕。
2. 记录 ASR partial 到浏览器绘制的延迟和 reconnect 次数；如果 partial 高频造成屏幕闪烁，应在服务端或客户端按 80–120ms 节流，而不是恢复多行字幕。
3. 房主控制台继续保持“异常才操作”的设计，正常阶段不增加主持人式手动按钮。
