# Round19 数据恢复只读审查

日期：2026-07-21
范围：`MatchParticipant`、`SpeechCorrectionRequest`、迁移 `0028_speech_corrections`、权限、归档、幂等、历史兼容与查询性能。
结论：当前未发现仍未关闭的高风险数据越权；有 2 项中风险需要后续收口。

## 中风险

1. **0028 不回填旧比赛的开赛参赛快照**
   - 迁移只创建空的 `match_participants` 表；旧数据依赖 speech event actor 和当前 `RoomSeat` 兼容。
   - 已经发过言的旧参赛者通常可由事件恢复；但“开赛时参赛、尚未发言、后来席位被修复或转移”的用户没有可靠不可变关联，可能缺失个人历史和归档权限。
   - 不建议盲目按当前席位全量回填。应提供一次迁移审计：仅对身份唯一可证明的记录回填，其余输出 unresolved 报告供管理员处理。

2. **纠错记录可无上限累积，列表和归档可能膨胀**
   - 用户可反复创建、撤销或被拒绝申请；每条还保存完整原文和 `original_segments` JSON。
   - 房间个人列表无分页；管理员待审只取最早 500 条、最近记录只取 50 条；归档同步包含全部申请。
   - 建议增加每发言/每用户频率与总量上限、列表游标分页，并为后台队列增加 `(status, created_at)`/`(status, updated_at)` 复合索引。

## 已复核关闭

- 发言归属已采用 **speech event actor 优先、`MatchParticipant` 回退**；不会再用可变 `RoomSeat` 判定旧发言所有者。
- `MatchParticipant` 语义保持为开赛锁定时的人类席位快照；后续实际发言者应由逐发言 actor 表达，不应覆盖开赛快照。
- 纠错幂等键已纳入 `speech_id`，避免同房间不同发言复用幂等键串单。
- 比赛归档权限已统一采用 `MatchParticipant OR speech actor OR current RoomSeat`，席位转移后的原参赛者仍可下载。
- 已阻止“终止前提交 pending、终止后批准改写”的绕过；终止后仍允许管理员拒绝并关闭申请。
- participant archive 已按 `viewer_user_id` 过滤纠错申请，不再向其他参赛者暴露本人以外的申请理由和复核说明。
- scorecard 归档已增加 `judged_transcript_sha256`、`current_transcript_sha256`、判定 basis 和裁判后修正数量，可机器识别文本与评分的版本关系。
- 单发言仅一个 pending 申请的部分唯一索引同时覆盖 PostgreSQL 和 SQLite。
- 归档读取会按当前数据库 source hash 同步重建，不依赖异步 worker 才能看到已审批修正。

## 验证限制

- 完成了静态路径与数据流审查。
- 本地目标测试未能执行：`platform/.venv` 的解释器仍指向已删除的 `/Users/sunshiqi/code/phdebate/v2/.venv/bin/python`；系统 Python 又缺少 SQLAlchemy。未修改环境或业务代码。
- 修复后建议至少运行：`test_round19_data_recovery.py`、迁移升级/降级测试，以及 PostgreSQL 下的并发创建与审批测试。
