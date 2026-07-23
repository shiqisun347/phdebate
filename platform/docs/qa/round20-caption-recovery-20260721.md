# Round 20：逐句字幕断线恢复

## 结论

逐句字幕现在拥有独立的展示投影存储。AI 文本不再只依赖 WebSocket
瞬时事件；刷新或断线重连后，房间快照会恢复当前发言最近 40 个稳定短句。
该机制不修改冻结语音链路，也不把 Agent 文本生成时间伪装成音频时间。

## 实现

- 新增 `caption_segments`（Alembic `0031_caption_segments`），按房间、发言、
  来源和顺序隔离，外键随房间或发言删除而 `CASCADE` 清理。
- AI 字幕先把 Agent 事件交给权威 TTS 流，再在后台串行落库和广播；`feed`
  不等待数据库，最终事件之后才排空写入链，因此不进入首声热路径。
- AI 的 `presentation_offset_ms` 仅表示“文字何时可展示”，对外明确标记
  `timing_basis=agent_text`；它不写入 `TranscriptSegment`。
- ASR 最终短句同时保留原研究转写，并写一份独立展示投影；同一发言内完全
  相同的重复 final 会被去重，不会重复拼接全文。不同 speech ID 绝不互相去重。
- 房间快照只读取当前 active speech 的字幕，避免跨发言、跨房间串字；迁移前
  已开始的人类发言仍可回退读取既有 `TranscriptSegment`。
- 匿名和登录观战共用既有脱敏字幕字段，不增加房间内部信息暴露。

## 数据边界

`CaptionSegment` 没有被加入比赛归档、AI 裁判输入、计分卡或研究导出。
这些链路继续只使用 `Speech.content` 与 `TranscriptSegment`。因此 AI 展示字幕
可恢复，但不会污染音频对齐、逐字稿或研究数据。

## 验证

- `apps/api/tests/test_round20_captions.py`：4 passed。
- 覆盖 AI 断线恢复、AI/研究转写隔离、房间隔离、外键清理声明、ASR 恢复、
  ASR 重复 final 去重和匿名事件脱敏。
- 相关 Python 文件 Ruff：通过。
- 曾启动独立临时 SQLite 的完整 Alembic 往返命令，但该外层执行被中断，未把
  它计作已完成证据；最终合并门禁应统一再执行 `upgrade head → downgrade 0030
  → upgrade head`。

## 残余边界

- 当前只恢复稳定短句，不恢复 ASR interim；这是有意选择，避免刷新后展示已
  被识别器推翻的临时文字。
- 仅返回当前发言最近 40 段，防止快照无限增长；完整比赛逐字稿仍由原转写链路
  负责。
