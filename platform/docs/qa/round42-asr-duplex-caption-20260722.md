# Round 42：真人 ASR 双向流式与舞台字幕验收

日期：2026-07-22

## 结论

- 正式真人采集链路为 `AudioWorklet → 20 ms Float32 → 16 kHz PCM16 → 浏览器 WebSocket → API WebSocket → FunASR WebSocket`。
- 正式链路不依赖 `MediaRecorder(timeslice)`，也不在结束发言后才整段上传音频。
- API 在同一个 ASR context 中并发执行音频上行和 partial/final 下行；测试已证明 `STOP` 之前能够收到 partial。
- 舞台字幕只显示最新短句，默认最多 28 个字符；完整识别文字仍单独保留用于最终提交。
- 匿名及登录观众继续使用公开投影：没有 `caption_segments`、`active_speech.content`、历史发言文字或 ASR/caption 事件正文。

## 本轮修复

1. FunASR 上游连接、`START` 和 `LANGUAGE:zh-CN` 完成后，API 才向浏览器发送 `ready`。避免上游仍未建立时浏览器误以为字幕链路可用并提前清空预滚缓存。
2. 浏览器忽略同一 ASR session 的重复 final，防止同一句被追加两次并提交到比赛记录。
3. 新增 `compactCaptionLine` 展示投影：折叠换行、选取累计识别结果中的最新分句、限制单行长度。它不参与 transcript 持久化或提交。
4. 保留 generation 隔离和有限重连：旧 socket 的迟到 partial/final 不会覆盖新 socket 字幕。

## 自动化证据

- API ASR：覆盖当前设备鉴权、真实上游 ready 屏障、20 ms PCM 帧、STOP 前 partial、final 持久化、静音/低置信度拒绝、迟到 final 和连接释放。
- Web：覆盖 AudioWorklet 帧顺序、16 kHz 重采样、ready 前预滚、tail flush、断线重连、旧连接迟到消息、重复 final、短句字幕和完整 transcript 提交。
- 观众隐私：公开 room snapshot 的 `caption_segments=[]`，ASR/caption 实时事件仅保留事件 envelope，watch 页面不挂载 transcript drawer。
- 回归结果：Web 全量 `326 passed / 21 skipped`；ASR、caption 与 benchmark 相关 API `33 passed`；Next.js production build 和 TypeScript 检查通过。
- 生产只读浏览器检查：`/rooms/117519/watch` 没有 `.subtitle-stage`、transcript drawer 或“文字稿”文案，仅显示只读赛况和比赛声音入口。

本轮没有改变比赛状态机、TTS、控制台或比赛计时逻辑。
