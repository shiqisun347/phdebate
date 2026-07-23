# Round 65：ASR 真双向流式、字幕生命周期与观战隐私

日期：2026-07-24  
范围：浏览器真人发言采集、ASR WebSocket、舞台字幕、断线重连、观战脱敏  
部署：本轮未部署

## 结论

- 浏览器真人语音链路已经是 `AudioWorklet → 20ms PCM → WebSocket → FunASR` 的持续上行，同时服务端持续把 partial/final 结果下发；不是 MediaRecorder Blob 上传，也不是录完再识别。
- API 的浏览器上行与 ASR 下行由两个并发协程处理，满足真双工；相关回归测试覆盖了“PCM 仍在上传时已经收到 partial”。
- 观众（匿名和已登录但未参赛）拿到的房间快照不包含 `caption_segments`，`active_speech.content` 为空；实时 `asr` / `caption.segment` 事件只保留事件类型和序号，不包含文字、speech id 或时序元数据。
- 舞台字幕只展示当前发言的最新短句，默认最多 28 个字符并单行省略，不把完整段落不断堆在比赛画面上。
- 修复了 ASR 重连累计文本重复、重连期间残留旧字幕、final 后迟到 partial 覆盖最终字幕三个生命周期问题。

## 参考图对应的字幕区域约束

参考根目录 `image.png`，本轮只抽取与字幕/文字记录相关的设计原则，不复制右侧完整文字记录面板到观战页面：

- 比赛舞台是主视觉，字幕必须短、稳定、可快速扫读。
- 当前发言状态和当前短句保持唯一焦点，不在舞台堆叠历史段落。
- 完整文字仅用于参赛者结束发言后的核对和授权角色的记录功能；观众不可读取文字稿。
- 断线重连时清除旧的临时字幕，显示“正在聆听你的发言…”，避免把断线前假设误认为仍在更新。

## 代码改动

### ASR 文本安全合并

新增 `apps/web/lib/asr-text.ts`：

- 若服务商重发包含旧前缀的累计假设，采用最新累计值，不重复拼接。
- 若重连结果只包含后半句，按最长可靠重叠拼接。
- 单个普通汉字的巧合重复不视为重叠，避免吞字；单个相同标点可去重。

该合并逻辑同时用于：

- WebSocket 重连前保存被中断的 partial。
- 重连后接收最终 final 并形成提交文字。

### final / partial 顺序保护

`apps/web/components/debate-stage.tsx` 现在在一个 ASR context 得到权威 final 后，忽略同一 socket 队列里任何重复 final 或迟到 partial。最终字幕和待提交文字不会被旧假设覆盖。

### 重连字幕清理

ASR socket 意外断开并进入有限重试时：

- 保留已识别内容供赛后核对。
- 清空舞台上的旧 partial。
- 复用同一个 AudioWorklet 图，麦克风采集不中断。
- 新 socket 未就绪期间继续保留有界 pre-roll PCM，避免无限占用内存。

## 隐私审计

已核对以下服务端路径：

- 公共房间序列化不查询和不返回字幕段。
- 公共活动发言不返回发言正文。
- 匿名与已登录观众 WebSocket 都使用公共事件投影。
- `asr`、`caption.segment` 的文字、身份、计时与服务诊断字段全部移除。
- 前端 watch 模式即使误收到带文字的对象，也不会渲染字幕或文字稿入口。

## 验证结果

### 通过

- ASR、字幕投影与文本合并定向前端测试：`108 passed`。
- API ASR、双工、断线、迟到 final、观战脱敏相关测试：`36 passed, 1 skipped`。
- Next.js production build：通过。
- ESLint：`0 errors`，存在 13 个既有 warning，本轮没有新增 error。
- `git diff --check`：通过。

新增/调整的关键测试包括：

- 累计假设重连不重复文字。
- 增量重连按可靠重叠拼接且不吞掉巧合字符。
- final 后迟到 partial 不覆盖最终字幕。
- 重连期间不保留断线前旧字幕。
- 单个 AudioWorklet 图跨 WebSocket 重连持续工作。
- 20ms PCM 持续上行时 partial 可同时下行。
- 匿名和登录观众均无法取得文字稿。

### 全量测试中的并行改动问题

前端全量测试共 `414 passed, 1 failed`。唯一失败为：

`app/rooms/[code]/lobby/page.test.tsx > keeps a ready participant's only fixed action relevant to their own seat`

失败原因是当前共享工作区的大厅并行改动把“取消准备”按钮替换成禁用的“等待房主开始”，与本轮 ASR/字幕代码无关。ASR 定向测试、相关组件全套 91 个测试和 production build 均通过。

## 文件

- `apps/web/lib/asr-text.ts`
- `apps/web/lib/asr-text.test.ts`
- `apps/web/components/debate-stage.tsx`
- `apps/web/components/debate-stage.test.tsx`

