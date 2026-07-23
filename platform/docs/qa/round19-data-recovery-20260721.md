# Round 19 比赛数据采集与真人自救闭环审计

日期：2026-07-21
范围：比赛身份快照、发言纠错、历史返回、管理员复核、终止边界、归档与数据权限
排除：冻结 TTS、实时语音与浏览器播放文件；本轮未修改这些文件

## 结论

审计发现并修复了两个真实的非语音数据缺陷：

1. 真人发言一旦完成，系统没有“本人申请纠错—管理员复核—保留原始证据—更新归档”的产品级后端闭环，只能直接修改数据库。
2. 历史和部分归档授权依赖可变化的 `RoomSeat.user_id`。AI 接替、真人恢复或席位修复后，原参赛者可能丢失历史/归档权限，当前席位用户也可能被错误当成旧发言作者。

本轮已完成可供 Web 接入的后端闭环，但尚未开发参赛者提交和管理员审批 UI，因此它是完成并验证的 API/数据基础设施，不是已经面向最终用户开放的完整界面功能。

## 修复设计

### 不可变参赛身份

- 新增 `MatchParticipant`，在开赛锁定时保存 `match_id / room_id / seat_key / user_id / display_name`。
- `/api/me` 历史查询使用 `MatchParticipant`，并为 0028 前历史数据兼容：
  - 当前 `RoomSeat`；
  - 具体真人发言事件的 `actor_user_id`。
- 比赛归档下载权限采用同一不可变身份规则，保留房主和系统管理员原有权限。
- 归档增加 `participant_snapshots`；参与者投影不暴露用户 ID。

### 发言作者授权

发言本身没有 `speaker_user_id`，因此授权不能读取当前席位。

权威顺序为：

1. 优先读取该具体 speech 的 `speech.started / speech.completed / speech.late_finalized` 追加事件 `actor_user_id`。
2. 仅在旧比赛缺失具体事件时，回退到开赛 `MatchParticipant` 席位快照。

这能正确处理同一席位在不同阶段由不同真人实际发言的场景；开赛者不能修改后续接替者的发言，后续实际发言者也不能修改开赛者的旧发言。

### 真人纠错闭环

新增接口：

- `GET /api/rooms/:code/speech-correction-requests`
- `POST /api/rooms/:code/speeches/:speechId/correction-requests`
- `POST /api/rooms/:code/speech-correction-requests/:requestId/cancel`
- `GET /api/admin/speech-corrections`
- `POST /api/admin/speech-corrections/:requestId/approve`
- `POST /api/admin/speech-corrections/:requestId/reject`

核心约束：

- 只能申请修正自己的、已完成的真人发言。
- 同一发言最多一个 pending 申请。
- 幂等键包含 `room_id + speech_id + user_id + supplied key`；同一用户在不同 speech 使用相同浏览器幂等键不会错误重放。
- 管理员审批使用 `expected_updated_at` 乐观锁；旧页面、重复审批和并发覆盖返回 409。
- 批准前再次确认原发言没有被其他流程修改。
- 批准后保存 `original_content` 和完整 `original_segments`，再同步更新 `Speech.content` 与当前 `TranscriptSegment`。
- 所有操作写入 `MatchEvent`；管理员处理写入不含修正正文的 `AdminAuditLog`。
- `room result` 和 `match history` 中每条 speech 增加服务端计算的 `can_request_correction`，Web 无需依靠 403 猜测作者身份和可用状态。

### 终止边界

- cancelled/terminated 比赛不能新建纠错申请。
- 若申请在终止前已创建，终止后管理员批准也返回 409，不能改写 `Speech`。
- 管理员仍可 reject，以关闭遗留 pending 工作项。
- `terminated` 已从可重建纠错归档状态集合中移除，保持原有“终止后比赛证据只读”语义。

### 裁判与修正文本来源

批准发言纠错不会自动重判或修改积分。归档新增明确来源信息：

- 每条 correction 包含 `after_judging`。
- scorecard 增加 `transcript_provenance`：
  - `basis`: `current` 或 `pre_correction`；
  - `judged_transcript_sha256`；
  - `current_transcript_sha256`；
  - `correction_after_judging_count`。

归档通过逆向应用“裁判完成后批准的纠错”重建裁判当时文本哈希，明确标识“裁判基于修正前文本评分”。本轮不自动重判、不覆盖原始评分、不改积分。

### 归档隐私

- 系统管理员 research archive 包含完整纠错证据、原始 segments 和内部用户 ID。
- participant archive 现在接收当前查看者 `user_id`：
  - 仅保留查看者本人纠错的原文、拟修正文和原因；
  - 完全省略其他参赛者的 correction 记录；
  - 不暴露 requester/reviewer 用户 ID。
- 双参赛者测试证明房主不能通过自己的参与者归档读取另一位学生的纠错内容。

## 数据一致性证据

| 数据 | 已验证行为 |
| --- | --- |
| `MatchEvent` | 申请、撤销、批准、拒绝各自追加事件；幂等重放不重复追加 |
| `Speech` | 仅管理员批准后更新；拒绝、撤销、终止后审批均不改变正文 |
| `TranscriptSegment` | 批准后与最终 Speech 内容同步；原 segments 完整保存在 correction 审计行 |
| `JudgeScorecard` | 发言纠错不自动覆盖胜方、分数或理由；归档记录评分文本哈希来源 |
| `RatingChange` / 排行榜 | 发言纠错不触发积分变化；赛果修正继续使用既有补偿积分机制 |
| Archive | 数据变化后源哈希变化并重建；研究/参与者投影权限不同且可验证 |
| 历史返回 | 席位转移后原参赛者仍能在 `/api/me` 返回比赛并下载参与者归档 |
| 测试数据 | 继续由现有 `room.is_test_data` 门禁排除排行榜、生产归档索引与数据质量统计 |

## 0028 迁移验证

迁移：`0028_speech_corrections`

实际执行了全新 SQLite 数据库：

```text
empty -> upgrade head
0028_speech_corrections
match_participants
speech_correction_requests
```

索引包含：

```text
uq_speech_correction_pending
uq_speech_correction_idempotency
uq_match_participant_seat
uq_match_participant_user
```

随后执行：

```text
0028 -> downgrade 0027
version = 0027_speech_data_disposition
两个新增表均不存在
```

最后再次执行：

```text
0027 -> upgrade head
version = 0028_speech_corrections
match_participants 与 speech_correction_requests 均恢复
```

upgrade、downgrade、再次 upgrade 全部成功。

## 自动测试

新增：`apps/api/tests/test_round19_data_recovery.py`

覆盖：

- 当前席位已转移时，具体 speech actor 仍是唯一作者依据。
- 同一席位后续由另一真人发言时，事件 actor 优先于开赛快照。
- 请求幂等、相同 key 不同内容冲突、相同 key 不同 speech 相互独立。
- 匿名、其他参赛者、本人、管理员权限边界。
- 撤销、拒绝、旧 revision、重复审批。
- pending 先创建、比赛后终止时 approve 被拒，reject 可关闭申请。
- Speech、Segment、Event、Audit、Judge、历史、归档一致性。
- 席位转移后原非房主参赛者仍能下载归档。
- participant archive 只包含当前查看者本人的纠错证据。
- `can_request_correction` 由服务端准确投影。
- 裁判文本哈希来源为 `pre_correction`，且 judged/current hash 不同。

结果：

```text
3 passed, 1 warning in 2.58s
```

相关归档、隐私、Judge review、数据质量与完整生命周期组合回归：

```text
13 passed, 1 warning in 5.95s
```

本轮所有 Python 文件 Ruff：

```text
All checks passed!
```

唯一警告为既有 Starlette TestClient/httpx 迁移提醒，不影响本轮业务断言。

## 仍需产品层完成

- Web 参赛者页面需要展示“申请修正”、申请状态、撤销入口和服务端 `can_request_correction`。
- Web 管理后台需要增加纠错队列、原文/拟修正文对比、批准/拒绝及 revision 冲突刷新。
- 上线前仍应运行 API 全量回归和 PostgreSQL staging migration；本轮按要求未部署。

## 文件变更

- `apps/api/alembic/versions/0028_speech_corrections.py`
- `apps/api/app/models/entities.py`
- `apps/api/app/schemas/requests.py`
- `apps/api/app/services/speech_correction.py`
- `apps/api/app/services/match_archive.py`
- `apps/api/app/services/participant_archive.py`
- `apps/api/app/services/room_service.py`
- `apps/api/app/api/rooms.py`
- `apps/api/app/api/public.py`
- `apps/api/app/api/admin.py`
- `apps/api/tests/test_round19_data_recovery.py`
- `docs/qa/round19-data-recovery-20260721.md`
