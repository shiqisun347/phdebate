# Round 22 生产真实场景验证

日期：2026-07-21  
服务器：`117.50.192.216`  
发布：`round22-human-recovery-ux-20260721`  
范围：多房间隔离、真人恢复、异常控制、WebSocket 观战上限。  
排除：不调用 Agent、TTS、ASR，不修改冻结音频路径。

## 四房间并行隔离

在生产数据库创建四个带唯一前缀的临时 QA 房间，并同时保持各自 WebSocket：

1. 真人固定发言：成功开始真人发言，终止比赛时写入可审计的 `interrupted` 发言。
2. 自由辩论：超时后只切换本房间阵营，并产生 `free.turn_timed_out`。
3. AI 播放收尾：过期播放只完成本房间发言并推进到真人环节。
4. 暂停房间：保持暂停和 77 秒剩余时间，不收到其他三个房间的事件。

结果：

```text
parallel_match_isolation_verified human=speaking->interrupted free=aff->neg
playback=completed paused=77s websocket_cross_room=0 result_status=auditable
```

所有 WebSocket 消息均重新校验 `room.code` 和事件 `room_code`，跨房事件为 0。

## 真人掉线、AI 接替和管理员恢复

- 真人席位离线时间设置为 61 秒，比赛引擎将其转换为 `ai_substitute`。
- 原辩手 `/me` 状态为只读，未建立真实房间 WebSocket 时不能假装已经返回。
- 原辩手重新建立带登录 Cookie 的房间 WebSocket 后，系统管理员通过 Round 22 新控制入口恢复真人。
- 恢复后 `/me` 显示 `occupant_type=human` 和 `can_resume=true`。

结果：

```text
return_after_substitution_verified timeout=61s watch_only=1 admin_restore=1 resume_enabled=1
```

## 四房间并行异常控制

并行验证：

- 暂停会中断并失效旧 AI 发言任务，恢复后旧任务不能复活。
- 失败步骤重试保留原失败发言为 `failed_retried`。
- 暂停裁判会失效旧裁判 attempt，恢复后不能接受迟到判决。
- 剩余 0 秒的阶段暂停和恢复后仍为 0 秒，并正确推进下一阶段。
- 相同幂等键和相同载荷安全重放；相同键不同载荷返回 409。

结果：

```text
parallel_control_recovery_verified rooms=4 pause_invalidated_inflight=1
judge_attempt_invalidated=1 resume=3 failed_attempt_preserved=1
idempotency_conflicts=2 zero_second_preserved=1 cross_room_events=0
```

## 20 人观战

对公开暂停房间 `117519` 同时发起 20 个真实 WSS 连接，并保持 5 秒：

```text
expected=20
connected=20
failed=0
success_rate=100.0
peak_connected=20
median_handshake_ms=901.28
p95_handshake_ms=975.20
max_handshake_ms=975.20
```

这次测试从服务器自身经公开 HTTPS/WSS 地址访问，包含 TLS、Nginx、API 初始快照和 Redis presence 路径。
20 个连接同时握手时全部在 1 秒内完成，符合单房最多 20 人的当前产品规模。客户端脚本拒绝在单房请求
超过 20 个连接；后端的第 21 人拒绝逻辑继续由全量 API 测试覆盖。

## 清理与生产状态

- 所有脚本均在 `finally` 中删除临时 Match、Room、Seat、Speech、Event、Session 和 User。
- 数据库复核：`qa_users=0`、`qa_rooms=0`。
- 测试后 `/api/health` 正常，磁盘仍为 `59%`，没有遗留连接或测试数据。
- MOSS 的既有硬件审批门状态未改变；本验证没有调用或改动语音链路。
