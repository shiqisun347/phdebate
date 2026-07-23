# Round 42：完整流程、ASR 字幕、主持音与移动控制修复

日期：2026-07-22  
生产环境：`https://117.50.192.216`  
API release：`round42-flow-asr-cues-20260722`  
Web release：`round42c-controls-ui-20260722`

## 本轮结果

- 阶段剩余时间不再用 `int()` 提前截断；截止时间仍在未来时至少显示 1 秒，固定阶段、自由辩论和主持提示音统一处理。
- 主持提示音期间提前请求当前 AI 阶段正文，提示结束后可直接消费预取结果。
- Engine 权威轮询从 1 秒缩短到 250 毫秒；当前系统最多 5 个房间，生产已确认实际配置为 `0.25`。
- ASR 保持一个 AudioWorklet 20 ms PCM context，经浏览器 WebSocket 和 API 持续上行 FunASR，同时接收 partial/final；上游协议握手完成后才向浏览器发送 ready。
- 重复 final 不再重复提交文字；舞台字幕只显示最新短句，最多 28 个字符，完整文字记录不受影响。
- 匿名观众继续不可见字幕和文字稿。
- 赛前公开观战链接改为脱敏等待页；匿名状态只显示题目、房间号和“已占用/待认领”，不显示姓名、在线和准备状态。
- 修复 `/media/_cues/*` 被错误当作房间号而返回 404 的问题；管理员预设女声现在可公开缓存并支持 Range 请求。
- 移动端房间大厅隐藏全局粘性导航；辩手页文字记录按钮移到固定控制栏上方。
- 比赛操作面板层级高于自由辩论举手队列，房主的暂停、终止和退出不再被遮挡。
- 双真人自由辩论无人举手且该方没有 AI 席位时，提示改为“自动交换发言方”，不再声称正在安排不存在的 AI 接替。

## 自动化门禁

- API：`524 passed, 1 xfailed`。
- Web：`327 passed, 21 skipped`。
- Next.js production build：通过。
- Ruff：通过。

## 生产浏览器证据

- `round42-production-browser/watch-lobby-final.png`：匿名赛前观战等待页，桌面/手机均可操作。
- `round42-production-browser/lobby-mobile-final.png`：移动大厅无粘性导航遮挡。
- `round42-production-browser/debate-mobile-controls-final.png`：文字记录位于控制栏上方，比赛操作按钮完整可点。
- 真实 1v1 房间 `477971`：开赛主持音正常进入“训练开始 → 正方立论”，没有加载/解码错误；移动端打开比赛操作并确认提前终止成功。
- 真实预设音 Range 探针返回 `206 Partial Content`、`audio/x-wav`、`Cache-Control: public, max-age=86400, immutable`。

本轮产生的临时用户和房间已统一标记为测试数据，不进入正式比赛数据口径。

## MOSS 推理与音频压缩结论

当前 24 kHz PCM 已通过单条 LiveKit Opus 音轨发送，最大码率 64 kbps。降低 Opus 码率只减少浏览器下行带宽，不会降低 MOSS 模型 RTF。生产继续保留现有音质参数；推理 A/B 只在独立环境测试 `decode_chunk_frames=15`、FP16 talker 和保持 FP32 的 codec fixed-shape/CUDA Graph，不与流程修复混合发布。

详细审计见 `moss-speed-quality-audit-20260722.md`。
