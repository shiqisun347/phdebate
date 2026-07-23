# Round 20：逐句字幕后端第一阶段

日期：2026-07-21

## 目标

解决 AI 发言长时间显示占位文字、结束时才短暂闪现全文的问题，同时保持已经冻结的 Agent → MOSS 双向流式语音和浏览器连续播放器不变。

## 实现

- AI Agent delta 继续先交给权威语音管线，字幕处理位于其后，不进入首声延迟热路径。
- 使用与实时语音相同的稳定短句切分策略：完整句立即输出，逗号片段满足长度后输出，结束时补齐剩余文本。
- 每个片段使用由 speech ID 和顺序生成的稳定 segment ID，通过房间 WebSocket 发布 `caption.segment`。
- ASR 继续使用现有 `asr` 房间事件，AI 与真人字幕在前端统一投影。
- 匿名和普通观众只获得 speech、seat、segment、文字、final 和时间基准，不暴露 Provider 诊断字段。
- 房间快照新增 `caption_segments`，用于恢复现有持久化 ASR final 片段；查询返回最新 40 条而非最早 40 条。

## 重要边界

AI 的实时 caption 首期是展示事件，不写入 `TranscriptSegment`：

- Agent 文本时间不是音频播放时间，不能伪装成音频对齐的研究数据。
- 不让字幕写入失败中断语音。
- 不让暂停/终止竞态留下被误认为权威文字的 AI segment。
- 最终权威文本仍是 `Speech.content`；研究归档不使用临时 caption 事件替代它。

后续若需要 AI 字幕断线后逐句完整恢复，将新增独立 `CaptionSegment` 模型，明确 `source`、`timing_basis`、generation、版本和 superseded 状态，而不是复用 ASR 的 TranscriptSegment。

## 验证

- AI 两句流式文本发布为两个可读片段，而不是最终全文一次发布。
- 临时 AI caption 不修改权威 transcript 表。
- 活跃发言快照超过 40 条 ASR segment 时返回最新 40 条并保持时间顺序。
- 匿名投影删除调试字段并保留必要字幕字段。
- 定向 Ruff 与测试：`2 passed`。

未部署，未修改任何冻结语音或浏览器音频文件。
