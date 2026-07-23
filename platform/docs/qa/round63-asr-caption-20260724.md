# Round 63：真人 ASR 双向流式与单行字幕专项

日期：2026-07-24  
范围：浏览器真人采音、ASR WebSocket 桥、FunASR partial/final、断线重连、字幕投影、观众文字隐私和资源清理。  
部署：未部署生产。

## 结论

当前正式真人语音识别链路是真双向流式，不使用 `MediaRecorder(timeslice)` 或 Blob 上传：

```text
麦克风 MediaStream
  → 单个 AudioWorklet（20 ms Float32 帧）
  → 连续重采样为 16 kHz / mono / PCM16
  → /ws/rooms/:code/asr 持续上行
  ⇄ API 与 FunASR 同一上下文并发读写
  ← partial / final 持续下行
  → 舞台唯一单行字幕
  → final 与人工确认文字写入比赛记录
```

本轮修复了两个会直接影响真实比赛完成度的问题：

1. FunASR 返回合法但空的 final 时，服务端过去忽略该消息，辩手点击“结束发言”后可能停留在“正在整理发言”约 30 秒。
2. 房间 WebSocket 断线重连后，旧连接留下的 ASR partial 可能继续覆盖新连接的权威快照，形成冻结旧字幕。

同时将手机字幕字号从 12px 提升到 14px，并把单行、截断和可读字号固化为 CSS 合约测试。

## 实现核验

### 浏览器上行

- 正式代码只使用 `AudioWorklet`；生产源码中没有 `MediaRecorder`、`BlobEvent` 或 `createScriptProcessor`。
- Worklet 以 20 ms 为目标聚合输入帧，发送 transferable `Float32Array`，不会积累 250/500/1000 ms 的录音 Blob。
- 主线程使用有状态流式重采样器输出 16 kHz PCM16；重采样状态跨 Worklet 帧保留。
- WebSocket ready 前最多保存 2 秒 PCM；ready 后按原顺序补发。
- 浏览器发送队列超过 512 KiB 时停止继续堆积过期音频，强制本轮结束后进行文字核对。
- 停止、暂停、阶段切换、终止和设备控制权丢失时，均停止 track、flush/stop Worklet、清空预滚队列、断开音频图并关闭 ASR WebSocket。

### 服务端双向桥

- API 只在用户、席位、当前 speech、stage、control lease 和协议 v2 全部匹配后连接 FunASR。
- FunASR 完成 `START` 与语言握手后，API 才向浏览器发送 ready。
- `browser_to_asr` 与 `asr_to_browser` 两个任务并发运行；现有测试证明在发送 `STOP` 前能收到 partial。
- 单条 PCM 必须非空、偶数字节且不超过 64 KiB。
- final 写入使用 speech 行锁；暂停、断线中断或阶段变化后的迟到 final 不能写入比赛。
- 同一 speech 使用进程内与 Redis owner-bound 双重单流租约，防止两个 API worker 同时连接 FunASR。

### 单行字幕与隐私

- 舞台只有一个 `.subtitle-stage p` 权威节点。
- 只接受当前 active `speech_id` 的事件；旧 speech、缺失 identity 和旧 socket 回调被拒绝。
- 累积 hypothesis 只投影最新可读短句，默认最多 28 个字符。
- CSS 强制 `white-space: nowrap`、`overflow: hidden`、`text-overflow: ellipsis`；手机端字号为 14px。
- 本地开始采集后立即显示“正在聆听你的发言…”，远端尚无 partial 时显示明确空态，不出现空白字幕框。
- 匿名及登录但未参赛的观众使用 public projection：`caption_segments=[]`、speech content 为空、实时 ASR/caption 事件只保留 envelope；观战页不挂载字幕或文字记录抽屉。

## 本轮修复

### 1. 空 final 立即结束等待

`apps/api/app/api/realtime.py` 现在先判断 final，再判断文字是否为空：

- 无语音活动：返回 `asr_rejected / silence`；
- 已检测到语音但无有效文字：返回 `asr_rejected / empty`；
- 两种情况都立即设置 final barrier 并结束上下文，不再等待 30 秒超时；
- 空内容不会写入 `Speech`、`TranscriptSegment` 或字幕表。

前端对 silence、empty、too-short、speech-inactive 和低可信度分别给出可行动提示，保留人工补充文字的恢复措施。

### 2. 重连清除旧 partial

`apps/web/lib/use-room.ts` 在新房间 WebSocket 打开时清除上一连接的 ephemeral live event；收到不带 event 的权威 snapshot 时再次清除。新连接只显示：

- 新 snapshot 内已经持久化的 final caption；或
- 新连接随后收到的 partial/final；或
- 明确的 listening/empty 状态。

断线前 partial 仍由辩手本地 ASR session 保留到人工核对文字，不会因为清除舞台展示而丢失恢复数据。

### 3. 官方文档纠偏

同步更新两份唯一官方文档：

- 明确空 final 立即进入文字核对；
- 明确房间重连不保留旧 partial；
- 删除“赛后录音、完整 WAV 回退、录音归档”等已淘汰描述；
- 明确正式比赛只保存确认文字、事件和脱敏语音性能遥测。

## 自动化证据

### API 专项

```text
49 passed
```

覆盖 ASR true-duplex、20 ms PCM、final 等待、空 final、静音、低置信度、单字幻觉、畸形 PCM、迟到 final、断线中断、字幕持久化和观众脱敏。

### Web

```text
54 test files passed
389 tests passed
0 failed
```

新增及保留的关键断言：

- 重连 snapshot 清除旧 partial；
- 同一 AudioWorklet 跨 ASR 重连复用；
- 重连期间 PCM 按序补发；
- 旧 socket 迟到结果不能覆盖新字幕；
- 单行 CSS 和手机 14px 字号；
- 观战页没有字幕与文字稿入口。

生产构建通过；ESLint 0 error、12 条既有 warning；API Ruff 通过。

### 共享工作树全量 API 情况

本次并行开发期间运行全量 API 得到：

```text
598 passed, 1 xfailed, 8 failed
```

8 个失败均位于房主断线后的 owner/control 转移与生产 retry 前置顺序，和 ASR、字幕或本轮文件无关；这是共享工作树中另一组并行状态机改动尚未收敛的证据，不能将本轮报告表述为全平台全绿。ASR/字幕专项 49 项全部通过，父任务整合时仍需先解决这 8 项再发布。

## 仍需真实设备验收

自动化已经证明协议、状态、队列和投影边界，但无头浏览器没有真实物理麦克风，不能替代学生设备验收。发布前仍需在隔离房间完成：

1. Chrome 与一台手机各说一段 30–60 秒中文，记录首个 partial P50/P95 和 final 延迟；
2. 人为断开 ASR 网络 0.5–1 秒，确认 Worklet 不重建、语音不静默丢失且结束后强制核对；
3. 静音后立即结束，确认不会停留“正在整理发言”超过正常网络往返；
4. 1920×1080 与 390×844 观察字幕始终单行、无多行堆叠、字号清晰；
5. 观众账号与匿名窗口确认任何 REST、WebSocket、结果页都得不到文字稿。

## 修改文件

- `apps/api/app/api/realtime.py`
- `apps/api/tests/test_platform.py`
- `apps/web/lib/use-room.ts`
- `apps/web/lib/use-room.test.tsx`
- `apps/web/components/debate-stage.tsx`
- `apps/web/app/globals.css`
- `apps/web/components/stage-caption-layout.test.ts`
- `docs/official/system.html`
- `docs/official/user.html`

