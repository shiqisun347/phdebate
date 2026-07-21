# Round 20 动态需求集成验收

日期：2026-07-21

状态：本地实现与回归通过，尚未部署。

## 已实现

- 内置赛事开场及每个目标阶段都有主持人口播；使用同一阶段索引内的服务端门控，口播结束前真人、Agent 和裁判都不能启动。
- 自由辩论单次发言上限为 30 秒，发言之间有权威三秒窗口。
- 真人申请支持再次点击取消、FIFO 排序、服务器时间、席位举手图标和顺序；错误阵营、错误席位和跨房请求被拒绝。
- 三秒窗口开始且尚无人申请时，AI 是否发言判断与隐藏候选并发启动；真人申请、暂停、跳过、终止和换阶段会使其失效。
- 确定的下一固定 AI 阶段支持文字预取；不提前调用 TTS，不占用正式 LiveKit/语音声道。
- AI 与 ASR 使用逐句字幕；独立 `CaptionSegment` 使当前发言刷新和断线后可恢复，同时不污染研究转写时间。
- 右侧文字记录抽屉支持当前阶段查看；Yjs/Hocuspocus 支持多人协同草稿、awareness、重连、逐发言权限和 PostgreSQL 持久化。
- 草稿不能直接覆盖权威记录；参赛者提交修正申请，管理员审核后才更新，并保留原文和审计证据。

## 关键设计修正

最初把主持人口播插成额外阶段会改变历史 stage index，并导致真人发言、ASR 和控制流程大量回归。最终实现不增加阶段：目标阶段在口播期间临时投影为 `announcement`，保留原 key/index，口播完成后恢复真实 kind 和完整计时。该设计通过全量 API 回归。

## 验证结果

- 平台 API：`473 passed, 1 xfailed`。
- 平台 Web：`50 files, 315 passed`；TypeScript、Next build 通过；ESLint 0 error，13 条均为冻结语音代码或既有 PostCSS warning。
- 部署与运维脚本：`112 passed`。
- Transcript Collab：`14 passed`，包含真实双 Provider、越权拒绝、跨房隔离和 token 到期重连；npm audit 0。
- Debate Agent API：`15 passed`；Web：`1 passed`，Next build 通过。
- Alembic：`upgrade head → downgrade 0030 → upgrade head` 通过，最终 head `0031_caption_segments`。
- 冻结语音核心：官方清单 `82 files` 全部通过，fingerprint 仍为 `3219138b22878e766c94b9a1fa0e422211a6f27dd74afbbb93a8e45f7e295285`。

## TTS 语速与现场杂音判断

用户反馈“比赛中有撕裂/卡顿，但赛后录音正常”更符合实时传输、浏览器调度或播放 underrun，而不是模型生成的 WAV 本身损坏。当前正式路径已经是 LiveKit/WebRTC 连续媒体，且可靠语音核心处于冻结状态。

本轮没有直接把浏览器播放率改为 1.1，也没有对实时 PCM 做临时重采样。原因是这种后处理会改变时钟和缓冲边界，反而可能放大当前的撕裂声。正式部署后应先用真实比赛同时记录：服务端音频捕获时间、LiveKit 首帧、浏览器首声、jitter/packet loss、AudioContext underrun 与赛后 WAV 对照。只有确认当前后端支持原生语速控制且 A/B 不增加杂音，才把 1.1 作为生产参数发布。

## 部署前门禁

- 生产无活跃比赛。
- 先备份 PostgreSQL，并验证恢复。
- 生成新的 `TRANSCRIPT_COLLAB_HMAC_SECRET`，不得复用 APP_SECRET。
- 构建 transcript-collab release，迁移到 0031，再加载 Supervisor 和精确 `/collab` Nginx WebSocket 路由。
- 部署后执行双浏览器协同、匿名观战、真人抢答、AI fallback、主持人口播和 20 观众上限验收。
