# 12 · Milestones

本章定义 MVP 开发阶段、交付物和边界。每个阶段结束时都应能独立演示或验证。

## 1. M0 · 需求冻结与文档基线

交付物：

- `README.md` 需求基线确认。
- `docs/00-12` 开发规格确认。
- 原型页面确认：大屏、管理端、辩手端。

验收：

- MVP 范围明确。
- 延后能力不阻塞当前实现。
- 后端、前端、Agent 开发者对接口命名无歧义。

## 2. M1 · 后端骨架

交付物：

- FastAPI 项目初始化。
- SQLite DDL 和种子赛制。
- Match/Team/Speaker/Phase CRUD。
- Event Service 和 `seq` 分配。
- 基础 WebSocket snapshot。

验收：

- 可创建一场比赛并读取完整快照。
- `docs/02-data-model.md` 中核心表可迁移创建。
- WebSocket 连接首包为 snapshot。

## 3. M2 · 状态机与计时器

交付物：

- Flow Engine。
- Timer Service。
- 比赛开始/暂停/继续/结束。
- 环节开始/跳过/回退。
- 固定环节单时钟和自由辩论三时钟。

验收：

- 不接前端也能用 API 跑完流程。
- 计时器 deadline 同步正确。
- 自由辩论交替计时正确。

## 4. M3 · 前端三入口联调

交付物：

- `/screen` 大屏场景。
- `/console/:speakerId` 辩手控制台。
- `/admin` 管理端监控与基础控制。
- WebSocket 重连恢复。

验收：

- 管理端推进环节，大屏和辩手端同步变化。
- 辩手端只在合法状态显示可操作按钮。
- 大屏覆盖 `idle`、`teams`、`live.single`、`live.free`、`live.prep`、`result`。

## 5. M4 · Agent 与语音链路

交付物：

- Agent Gateway。
- mock agent。
- SSE delta 接入。
- AI 文本上屏。
- 讯飞 ASR partial/final。
- 讯飞 TTS 播放。
- 音频归档。

验收：

- mock agent 跑通 AI 发言。
- 人类麦克风发言能转写并归档音频。
- TTS 首字播出触发 AI 发言计时。
- ASR/TTS 失败能降级。

## 6. M5 · 投票与结果

交付物：

- 管理端评委票录入。
- 学生投票页。
- 防重复 token 或弱指纹。
- 结果公布顺序控制。
- 大屏 `intermission` 和 `result` 投票展示。

验收：

- 学生结果不能早于评委结果公布。
- 评委票、学生票可汇总。
- 最终结果可上大屏。

## 7. M6 · 现场演练与加固

交付物：

- 紧急停止。
- 审计日志覆盖危险操作。
- 导出 transcript/events/votes/audio。
- 现场部署脚本或运行手册。
- 全链路彩排记录。

验收：

- 真实设备完整跑完一场。
- 断线、Agent 超时、ASR/TTS 失败、主持人误操作均有处理路径。
- 比赛结束后可导出归档材料。

## 8. MVP 明确包含

- 单场比赛配置和运行。
- 固定 10 环节赛制，参数可配置。
- 大屏展示和场景切换。
- 管理端流程控制、AI 干预、投票录入。
- 人类辩手极简控制台。
- 后端权威状态机和多时钟计时。
- WebSocket 快照 + `seq` 续传。
- Agent HTTP + SSE 标准接口和 mock agent。
- 人类 ASR、AI TTS、音频归档。
- 自由辩论 `host_designate` 与 `teammate_control`。
- 固定环节 AI 提前下发。
- 学生扫码投票和公布时序。
- 事件日志与审计日志。

## 9. MVP 明确不包含

- 完整评委独立端。
- 多场赛事排程和赛程管理。
- 自动评分和 AI 评委自动调用。
- 自由辩论 `ai_auto` 自动接管。
- 自由辩论投机预生成。
- 多语言和复杂权限系统。
- 视频录制和视频归档。

## 10. 推荐开发顺序

1. 先实现数据模型、事件和状态机，不从 UI 开始堆逻辑。
2. 用 mock agent 和假 ASR/TTS 跑通流程。
3. 再接真实前端三入口。
4. 最后替换真实讯飞和真实 Agent。
5. 每次接入新外部服务，都保留 mock 和降级开关。

