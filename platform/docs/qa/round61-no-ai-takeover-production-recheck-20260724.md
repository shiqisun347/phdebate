# 真人断线暂停与禁止 AI 接管生产复核

日期：2026-07-24  
生产地址：`https://117.50.192.216`  
当前生产发布：`round60-no-ai-takeover-20260724`

## 结论

- 真人席位在比赛开始后始终保持 `human`，系统不会创建 `ai_substitute`，也不会把后续真人轮次交给 AI。
- 真人静默断网累计达到 60 秒后，服务端状态机自动把整场比赛切换为 `paused`。
- 暂停时保留房间、真人席位、阶段、剩余时间和已确认文字；全部真人重连后，必须由房主或系统管理员显式继续。
- 前端按钮和提示只是服务端规则的投影。绕过前端直接请求恢复，在仍有真人离线时同样会被拒绝。

## 自动化回归

执行：

```text
pytest tests/test_round45_no_ai_takeover_policy.py \
       tests/test_round46_disconnect_flow_boundaries.py \
       tests/test_round57_human_recovery_clock.py \
       tests/test_0034_retire_ai_takeover_migration.py
```

结果：`25 passed`。

覆盖固定席位、自由辩论、AI 正在发言、裁判阶段、账号停用、主动离开、59 秒未超时、60 秒超时、暂停恢复和历史迁移等边界。

## 生产黑盒证据

隔离测试房 `757885` 进入运行态后，真人 `aff_1` 静默断开。服务端随后产生以下有序事件：

1. `presence.disconnected`
2. `participant.disconnect_timeout`，其中 `grace_seconds = 60`
3. `match.paused`，原因为 `participant_disconnected`

复核结果：

- 房间状态：`running → paused`
- 暂停原因：`真人辩手断线超过 60 秒，比赛已安全暂停。全部真人重新连接后，由房主或管理员继续比赛。`
- 真人席位：`aff_1 / human / 原 user_id 保留`
- 永久 AI 席位：`neg_1 / ai`
- `seat.ai_substituted` 事件数：`0`

生产中的真实用户暂停房间未被修改；上述验证只使用自动化测试房。

## 运维约束

- 不得重新引入“断线后 AI 接替”“主动退出后 AI 接替”或把真人席位改为 AI 的接口。
- 不得用浏览器计时器决定 60 秒边界；Redis presence lease 与服务端状态机是权威来源。
- 暂停后不得自动恢复。恢复前必须确认所有真人已重连，并由房主或管理员执行继续。
- 历史终态数据可继续读取旧字段，但运行时代码不得创建新的 AI 替代席位或替代事件。
