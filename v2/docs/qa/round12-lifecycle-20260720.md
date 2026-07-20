# Round 12：完整比赛生命周期与多房间恢复验证

日期：2026-07-20
范围：V2 API、房间状态机、真人控制、AI 接替、赛后数据链路
音频边界：只使用返回空音频地址的进程内 Mock TTS；未调用生产 Agent/Judge/TTS，未生成或播放音频，未修改 MOSS、TTS、LiveKit、AudioWorklet、PCM、浏览器播放或 `DebateStage`。

## 结论

新增一条真实业务风格的完整生命周期自动化测试，将此前分散的状态机能力串成一条可重复验证的证据链。测试同时运行三个独立房间，主房间完成从大厅、开赛、真人发言、暂停恢复、真人掉线、AI 接替、Agent 故障、人工重试、引擎重启恢复、真人回归、自由辩论、总结、AI 裁判，到结果、历史、排行榜、归档和再次比赛的全过程；另外两个房间分别验证人工跳过和紧急终止。

本轮没有发现需要修改生产后端的新增缺陷。完整链路证明现有非音频状态机在本轮场景下符合预期。新增测试文件：

- `apps/api/tests/test_round12_lifecycle.py`

## 场景拓扑

| 房间 | 赛事/阵容 | 注入事件 | 终局 |
| --- | --- | --- | --- |
| A | 4v4 正式赛；2 真人 + 6 AI | 暂停/恢复、反方真人掉线、AI 接替、Agent 首次失败、人工重试、引擎重启、真人恢复、双方自由辩论 | `completed`，正方获胜并计入排行榜 |
| B | 1v1 训练赛；1 真人 + 1 AI | 与 A 并发推进；进入裁判后房主人工跳过 | `review_required` |
| C | 1v1 训练赛；1 真人 + 1 AI | 与 A、B 同时离开 preparing；房主因设备故障紧急终止 | `terminated` |

所有外部依赖均为进程内确定性 Mock：

- Agent 正文包含房间号、当前辩题和阶段，用于检测串房；
- A 房间反方接替阶段首次抛出可重试 `agent_timeout`；
- Judge 返回与当前辩题绑定的独立结论；
- TTS 返回空地址，使状态机走文本完成分支，不创建、解码、压缩、传输或播放音频。

## 验证矩阵

| 要求 | 权威断言 |
| --- | --- |
| lobby/start | 三个房间均通过真实 REST API 创建、认领、准备和开始；并发从 `preparing` 进入各自第 0 阶段 |
| 混合真人 + AI | 正式赛保留两个真人席位，其他六席由开赛逻辑自动补齐 |
| 真人暂停/恢复 | 房主在真人发言前执行 pause/resume，恢复后可正常取得控制权并提交发言 |
| 真人断线 | 将反方真人离线时间推进超过 60 秒，Engine 自动产生 `ai_substitute`，原用户 ID 保留以支持回归 |
| AI 接替 | 接替 Agent 使用原席位 `neg_1` 完成反方立论，生成内容只包含 A 房间辩题 |
| Agent 失败重试 | 首次 Agent 超时仅暂停 A；B 同时正常推进到 judging；房主 `control/retry` 后 A 原阶段成功重做 |
| 服务重启 | 在 `aff_ai` 注入孤儿 `speaking` 任务，`recover_inflight_engine_tasks()` 将其标记为 `interrupted`，新 Engine pass 安全重做该阶段 |
| 真人回归 | 原真人重新连接，创建恢复申请，房主审批后席位从 `ai_substitute` 恢复为 `human` |
| 自由辩论 | 正方真人发言后轮转至反方；恢复后的反方真人发言后轮转回正方；总阶段到期后进入总结 |
| 裁判和结算 | A 生成 approved scorecard、winner、RatingChange 和 LeaderboardEntry；胜方 3 分，负方 0 分 |
| 人工修复出口 | B 在裁判环节人工 skip 后进入 review_required；C terminate 后没有活动 Speech |
| 房间隔离 | Agent 内容、Judge 理由、Speech、MatchEvent、Match ID 和状态均按 room_id 隔离；故障不传播到其他房间 |
| 结果与历史 | 房间 result、比赛 history、公开 match result、个人中心 history 相互一致，并验证分页 `has_more` |
| 排行榜 | `/api/rankings?competition_slug=daily-4v4` 返回与数据库结算一致的胜负、场次和积分 |
| 归档 | completed、review_required、terminated 三类终局均可建立 SHA-256 归档，参赛者可下载且响应校验值一致 |
| 再次比赛 | 正式赛参赛者以幂等键创建同题 rematch，重复请求返回同一个新房间，不会重复创建 |

## 自动化结果

### 新增生命周期用例

```text
1 passed in 0.65s
```

### 相关引擎与恢复回归

覆盖 Round 6、9、10、11、多房间模拟和本轮生命周期：

```text
52 passed, 1 xfailed in 14.83s
```

### 完整 API 回归

从 V2 根目录运行，显式加入项目和 API 包路径：

```bash
PYTHONPATH=.:apps/api .venv/bin/pytest -q apps/api/tests
```

结果：

```text
411 passed, 1 xfailed in 35.94s
```

唯一 xfail 是 Round 11 已记录的冻结实时语音 pipeline 极窄取消竞态；它位于本轮禁止修改的音频冻结区，本轮未扩大风险，也未以跳过测试掩盖新问题。

### 静态检查

```text
Ruff: All checks passed
```

### 可靠音频冻结基线

```text
audio_baseline_verified files=82
fingerprint=3219138b22878e766c94b9a1fa0e422211a6f27dd74afbbb93a8e45f7e295285
```

## 观察与后续建议

1. 当前状态机已具备关键人工修复出口：暂停/恢复、异常重试、跳过、终止、AI 接替和席位恢复。生产 UI 应继续确保这些动作显示具体影响和二次确认，不能只依赖操作人员记忆。
2. 完整测试确认结果、历史、排行榜和归档可以形成数据采集闭环。后续批量学生赛重点应持续监控 `review_required` 比例、掉线接替次数、恢复成功率和 Agent 重试次数，以识别网络或服务质量退化。
3. 已知 xfail 必须继续限定在独立音频变更窗口处理。在用户要求保持当前 TTS 和浏览器播放效果不变期间，不应为了消除测试标记而修改冻结实时语音路径。
