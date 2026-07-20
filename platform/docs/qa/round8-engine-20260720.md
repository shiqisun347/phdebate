# Round 8 多房间比赛引擎与异常恢复审计

日期：2026-07-20

## 范围与保护边界

本轮只审计房间、席位、比赛状态机、REST 控制权限、Agent 异常恢复和结果一致性。未修改 TTS、LiveKit、AudioWorklet、PCM 播放、MOSS gateway、`voice_runtime`、`providers`、服务配置或可靠语音基线文件。

## 发现与修复

### 高风险：恢复旧发言可绕过暂停和阶段权限

`POST /api/rooms/:code/speech/start` 对“已有同席位真人发言”的恢复分支，原先在 `speaking_permission` 和当前阶段校验之前返回 `resumed=true`。如果异常状态留下 `speaking` 记录，客户端可绕过前端禁用按钮，在比赛已暂停或阶段已变化后直接恢复旧发言。

修复：

- 恢复已有发言也必须通过服务端 `speaking_permission`；该校验使用显式
  `ignore_active_speech=True`，只忽略正在恢复的这一行，不依赖 SQLAlchemy 实例是否恰好缓存
  `_active_speech`。暂停、裁判、终局、AI 接替和非当前轮次均不可绕过。
- 恢复前校验 `Speech.stage_key` 等于权威当前阶段；过期发言返回 409，并提示刷新或由房主处理异常步骤。
- 保留正常的同设备刷新恢复能力，不改变控制租约逻辑。

回归覆盖：`test_stale_human_speech_cannot_bypass_pause_or_stage_authority`。

## 新增真实场景测试

新增 `apps/api/tests/test_round8_engine_scenarios.py`：

1. 三场 Agent 比赛同时推进，其中一场首次超时：
   - 健康房间独立进入裁判并完成，不被故障房间阻塞。
   - 故障房间安全暂停，保存剩余时间和 `provider.failed` 的错误码、可重试属性。
   - 房主使用幂等键重试，同一请求只执行一次。
   - 失败尝试保留为 `failed_retried`，成功尝试形成新记录，不覆盖审计历史。
   - 每场只产生一个 `match.completed`，裁判结果与题目严格按房间隔离。
2. 真人断线超过 60 秒：
   - 仅目标席位自动切换为 `ai_substitute`，AI 完成当轮并只推进一次。
   - 对照房间不产生替补事件，证明跨房隔离。
   - 原辩手用幂等请求申请恢复，重复提交只返回同一申请。
   - 房主审批后，真人身份和席位绑定恢复。
3. 人为构造暂停状态和过期阶段的遗留真人发言：
   - 暂停时恢复返回 403。
   - 阶段不匹配时恢复返回 409。
   - 数据库中的权威比赛阶段和旧发言均不被请求篡改。

## 已验证的既有能力

本轮同时复跑并确认：

- 20 个房间并发完成，Agent 调用有界并行，事件序列连续且无串房。
- 24 个暂停、等待真人、真人超时、播放中、裁判中、准备中混合房间只推进各自权威状态。
- 随机多人抢座、释放、准备、开始、暂停、恢复、跳过、终止操作保持单席位、单比赛、单活跃发言等不变量。
- 同一用户跨房并发抢座仅一个成功；同席位并发抢座仅一个成功。
- 准备/开始以及开始/取消竞态只得到一个权威结果，不生成重复 Match。
- Agent 坏输出在 TTS 前被拒绝；Agent 超时、裁判失败可重试；迟到 Agent/裁判结果不能复活已跳过或已终止比赛。
- 引擎重启中断遗留 AI/裁判任务并复用已生成文字；房间故障隔离和连续异常隔离有效。
- 真人掉线 AI 接替、主动恢复申请、审批、跨房冲突、活动发言冲突和终局限制有效。
- 控制、发言完成和故障重试的幂等键重放不重复推进状态或结算结果。

## 验证结果

```text
pytest round5/round6/round8/multi-room suites
35 passed

高风险竞态与恢复聚焦回归
8 passed, 155 deselected

Ruff（修改文件）
All checks passed

git diff --check
passed

完整 API 回归
384 passed, 2 warnings

可靠语音冻结基线
82 files verified
fingerprint 3219138b22878e766c94b9a1fa0e422211a6f27dd74afbbb93a8e45f7e295285
```

## 变更文件

- `apps/api/app/api/rooms.py`
- `apps/api/app/services/room_service.py`
- `apps/api/tests/test_round8_engine_scenarios.py`
- `docs/qa/round8-engine-20260720.md`
