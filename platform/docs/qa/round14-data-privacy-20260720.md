# Round 14 数据采集、隐私与归档审计

日期：2026-07-20
范围：正式/QA 数据隔离、匿名观战、历史与结果权限、媒体缓存、排行榜、逐字稿/音频/segment 质量检查、研究归档与学生下载档案。
排除范围：未修改 DebateStage、LiveKit、AudioWorklet、PCM、MOSS、TTS、ASR 或比赛状态机。

## 结论

本轮确认并修复了三类真实的数据泄漏边界：QA 房间进入公开赛事列表、QA 房间可被匿名观战，以及参赛学生下载到管理员研究归档中的内部模型和运行配置。修复后：

- `Room.is_test_data=true` 的房间不会计入公开赛事 `live_count`，不会出现在赛事详情或全站观战列表。
- QA 房间即使 `visibility=public`，匿名用户和非本房普通用户也无法通过 REST、结果、历史、媒体、主 WebSocket 或音频 WebSocket读取；本房参赛者、房主和系统管理员仍可访问。
- 正在公开观战的房间被管理员重新标为 QA 后，既有匿名 WebSocket 会立即收到 `4401` 并断开，不再继续接收缓存快照。
- 管理员把账号标为 QA 而批量改标其历史/当前房间时，也会逐房发布权限变更并立即断开匿名观众。
- QA 房间媒体只允许 `private, no-store`，不会进入浏览器或代理共享缓存。
- 未发布赛事不能通过 `/api/rankings?competition_slug=...` 绕过赛事详情的公开限制读取榜单。
- 普通参赛者与房主下载的是确定性的“参与者档案投影”；系统管理员仍下载完整研究归档。

## 已修复问题

### P0：QA 房间从公开入口泄漏

原行为：

- 公开赛事卡片的 `live_count` 会统计 QA 房间。
- `/api/live-rooms` 和赛事详情 `live_rooms` 会列出 QA 房间。
- QA 房间开始后，匿名用户可凭六位房间号获取公开快照、结果和媒体。
- `PublicSnapshotCache` 只检查 `visibility/status`，不检查 `is_test_data`。

修复：

- 三个公开列表查询统一增加 `Room.is_test_data=false`。
- `can_view_room` 对 QA 房间采用严格成员权限：参赛者、房主、系统管理员可读；其他用户拒绝。
- 匿名主 WebSocket 在读缓存前、每次推送前和 ping 时重新检查权威权限。
- PublicSnapshotCache 的权威轻查询和快照生成均加入 `is_test_data`。
- 单房数据范围改标和账号级批量改标都会向每个受影响房间发布更新，避免已连接观众停留在旧权限状态。
- 音频 WebSocket、历史、结果和媒体原本都调用 `can_view_room`，因此同步获得完整隔离。

### P0：参与者下载的研究归档暴露内部配置

原归档包含：

- `judge_snapshot`、`service_snapshot` 和服务 Endpoint。
- `agent_profile_id`、其他学生 `user_id`、事件 `actor_user_id`。
- 原始事件 payload 中的 task、lease、idempotency 和 provider 错误细节。
- scorecard 审核管理员 ID。

修复：

- 新增确定性参与者投影，保留比赛题目、公开赛制、阶段模板、席位和显示名、发言、逐字稿 segment、音频 URL、比分、裁判理由、积分变化和可理解时间线。
- 删除用户/管理员内部 ID、Agent/Judge/Service 配置、Profile、任务控制字段、原始 provider 失败和内部错误 payload。
- 参与者投影使用独立内容 SHA-256 与 ETag；`X-Archive-Projection: participant` 明确其语义。
- 管理员研究归档保持原始完整度，使用 `X-Archive-Projection: research`。
- 两类档案都使用 `private, no-store`；底层源数据哈希继续通过 `X-Archive-Source-SHA256` 标识，未将研究归档 SHA 伪装成投影 SHA。

### P1：QA 媒体可进入共享缓存

原行为：媒体 Cache-Control 只依据 `visibility=public`，公开可见的 QA 房间会返回 `public, max-age=3600`。

修复：仅正式且公开的房间允许 public cache；QA 和 private 房间统一返回 `private, no-store`。

### P1：隐藏赛事榜单可被直接查询

原行为：赛事详情检查 `Competition.is_public`，但排行榜按 slug 查询未检查，知道 slug 即可读取未发布赛事榜单。

修复：排行榜赛事查询同时要求 `Competition.is_public=true`，否则按不存在返回 404。

## 已验证的现有保护

- 管理员 `/api/admin/data-quality` 默认只统计正式数据，区分 completed/review/terminated，检查真人发言 transcript、audio URL 和 TranscriptSegment 覆盖率，并提供异常样本 ID。
- 管理员归档索引默认排除 QA，只有显式 `include_test_data=true` 才导出；CSV 文本字段具有公式注入保护。
- 排行榜默认排除测试账号和已封禁账号；将账号标记为 QA 会重算相关正式榜单。
- 历史分页游标包含比赛边界，不能拿 A 比赛游标读取 B 比赛。
- 私密房间的 REST、WebSocket、结果、历史、归档和媒体均执行房间级权限校验。
- 匿名结果不返回参与者 `user_id`；非管理员看不到裁判内部评分细节。
- 研究归档不会保存密码哈希、会话 token 或 Agent Gateway 密钥密文。
- 管理员具有归档完整性检查、修复损坏归档、清理孤儿文件和按赛事/赛季分批导出索引的手段。

## 测试证据

新增 `tests/test_round14_data_privacy.py`，覆盖：

1. QA 房间不进入 catalog/live/detail 及 live_count。
2. 匿名、外部登录用户、本房房主在 REST 权限上的 401/403/200 边界。
3. 主 WebSocket 匿名 4401、外人 4403、房主正常连接。
4. room result、match result、history、archive 的身份矩阵。
5. QA 媒体匿名/外人拒绝、房主成功且 `private,no-store`。
6. 参与者档案中模型配置、内部 URL、user/actor/profile/task/lease/idempotency/provider error 全部消失。
7. 管理员研究归档保留 Prompt、Endpoint 和内部诊断，但仍剔除密钥密文。
8. 参与者与研究归档分别使用与自身内容一致的 SHA。
9. 运行中房间被改标 QA 后，已连接匿名观众立即断开。
10. 账号被标为 QA 并批量影响房间时，已连接匿名观众立即断开。
11. 未发布赛事排行榜返回 404。

执行结果：

```text
tests/test_round14_data_privacy.py: 4 passed
完整 API 回归: 426 passed, 1 known xfail
Ruff（API 应用与本轮测试）: all checks passed
Python compileall（本轮应用文件）: passed
```

原有归档测试已随新双 SHA 语义更新：底层研究归档与参与者投影分别校验各自内容哈希和共同源数据哈希。

## 尚需产品与合规决策

以下不是可由代码擅自决定的细节，仍应进入上线清单：

- 平台目前公开展示真实姓名及排行榜。学生特别是未成年人使用前，应明确展示数据采集范围、公开展示范围、保存期限和删除/申诉渠道。
- 现有数据质量页能发现 transcript/audio/segment 缺失，但音频丢失通常无法自动重建。建议给异常比赛增加“待补采/不可恢复/已人工核验”处置状态，形成可追踪修复闭环。
- 应确定原始音频、逐字稿、参与者档案和研究归档的差异化保留周期；当前归档清理工具只有文件年龄策略，没有业务级保留政策。
- 若后续向教师、研究人员或第三方提供批量数据，应新增独立数据导出角色、用途审批和导出水印，不应共享系统管理员账号。
