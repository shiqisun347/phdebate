# Round 19：生产房间 357930“停滞”只读诊断

日期：2026-07-21（Asia/Shanghai）
房间：`357930`
操作边界：只读取公开房间接口、公开比赛列表、健康接口和本地源码；没有暂停、继续、跳过、终止、重试或写数据库；没有部署。

## 结论

房间 `357930` 没有停滞。观察到的三个现象分别来自：

1. API 时间戳使用 UTC，`2026-07-20T16:31:53+00:00` 换算为北京时间是 `2026-07-21 00:31:53`，并不是停留在“昨天”。
2. `remaining_seconds` 是依据当前阶段 deadline 与当前 UTC 时间实时计算的倒计时；阶段正在运行时非零完全正常，而且倒计时变化不会增加房间 `seq`。
3. `/api/live-rooms` 此前会把“最近一次暂停事件时间”作为 `paused_at` 返回给已经恢复运行的房间。前端只在 `status=paused` 时显示它，但直接查看 API 容易误以为房间仍停在旧暂停点。本轮已在非冻结公共接口代码中修正这一语义，未部署。

在只读观察期间，房间从自由辩论推进到反方四辩总结、正方四辩总结和 AI 裁判阶段，事件序号从 72 增长到 89，因此不存在状态机卡死证据。

## 生产只读证据

### 第一次采样

本机时间：`2026-07-21 00:33:29 +0800`，对应 UTC `2026-07-20 16:33:29Z`。

公开房间快照：

- `status=running`
- `seq=78`
- `current_stage_index=8`
- 当前阶段：反方四辩总结
- `remaining_seconds=169`
- 当前 AI 发言：`neg_4 / synthesizing`
- `failure_reason=""`
- `playback_started_at=2026-07-20T16:33:21.174847+00:00`

这里的播放开始时间就是北京时间 `2026-07-21 00:33:21`，距采样只有约 8 秒。

### 连续倒计时采样

| UTC 时间 | seq | stage | remaining | active speech |
|---|---:|---:|---:|---|
| 16:33:45 | 78 | 8 | 154 | `neg_4 / synthesizing` |
| 16:33:54 | 78 | 8 | 146 | `neg_4 / synthesizing` |
| 16:34:02 | 78 | 8 | 138 | `neg_4 / synthesizing` |

倒计时按墙钟持续下降。`seq` 不变是正确行为：没有新状态事件时，服务端无需每秒写数据库或广播事件。

### 后续推进证据

UTC `16:37:17`：

- `status=judging`
- `seq=89`
- `current_stage_index=10`
- 当前阶段：AI 裁判评议
- `remaining_seconds=25`
- 无 active speech
- `failure_reason=""`

同一时刻 readiness：

- 整体 `ok=true`
- Engine 心跳年龄 `0.28s`
- `active_match_processing=true`
- MOSS active `0`

房间已越过此前被认为停住的自由辩论和两个总结阶段，进入裁判流程。

### 追加事件日志证据

公开快照中的 append-only `recent_events` 显示：

- seq 56：自由辩论开始，UTC 16:27:26
- seq 57–61：正方一辩 AI 发言完成并切换反方
- seq 62–63：反方回合超时并切回正方
- seq 64–68：正方二辩 AI 发言完成并切换反方
- seq 69–70：反方回合超时并切回正方
- seq 71–72：正方三辩 AI 发言和 RTC 音频开始
- 后续只读采样已到 seq 78、最终到 seq 89

这些事件是数据库权威事件流的公开投影，比仅观察一个页面时间戳更能证明流程实际推进。

## 源码解释

### 时间统一使用 UTC

`room_service.now()` 返回 `datetime.now(timezone.utc)`。数据库 deadline、stage started、turn started、事件时间和接口 ISO 字符串都使用 UTC。

因此，浏览器或人工查看原始 JSON 时必须按时区显示，不能仅看日期部分判断是否陈旧。

### `remaining_seconds` 不依赖每秒写数据库

`room_service.remaining_seconds()` 使用：

```text
max(0, int((stage_deadline_at - now()).total_seconds()))
```

同一个 `seq` 下连续请求得到更小倒计时是设计行为，可避免每秒数据库写入、WebSocket 广播和多房间写锁竞争。

### AI 流式合成期间暂不推进

自由辩论中，如果存在带 generation 和 playback start 的 `synthesizing` Speech，Engine 会等待权威流结束，而不会在名义回合边界强行打断。Provider 自身的 timeout/cancel 负责处理真正卡住的生成。

第一次采样时 MOSS active=1、Speech 有 playback_started_at，随后该发言完成并推进阶段。这与正常流式生成一致，不是孤儿任务。

### Engine 扫描与错误隔离

Engine 每个 tick 查询 lobby/preparing/running/paused/judging 房间，并为每个房间建立独立任务。连续非瞬态异常达到阈值后会把房间安全暂停并写入 `engine.quarantined` 与 `failure_reason`；当前房间没有 failure reason，且实际持续产生事件。

## 实际发现的语义问题及本地修复

### 已恢复房间仍返回历史 `paused_at`

生产 `/api/live-rooms` 曾返回：

- `status=running`
- `paused_at=2026-07-20T16:17:31.947004+00:00`
- `updated_at=2026-07-20T16:31:53.612772+00:00`

查询中的暂停事件聚合是为了把长期暂停房间从公开列表中过期淘汰，因此必须保留最近暂停事件。但把该历史值原样返回给 running 房间会造成诊断歧义。

本地修改 `apps/api/app/api/public.py`：

- 暂停事件仍用于 freshness 查询。
- 只有 `room.status == "paused"` 时才向公开赛事详情和 `/api/live-rooms` 返回 `paused_at`。
- running/preparing/judging 房间返回 `paused_at=null`。
- 管理后台仍保留历史暂停时间用于审计和“长期暂停”判断，没有删除证据。

测试在 running 房间写入历史 pause event，验证公开列表和赛事详情均返回 `paused_at=null`；真正 paused 房间仍返回暂停时间。

## 根因候选分级

| 候选 | 判断 | 证据 |
|---|---|---|
| UTC 日期被当作北京时间日期 | 已确认 | UTC 7 月 20 日 16:31 = 北京时间 7 月 21 日 00:31 |
| 倒计时非零表示状态未推进 | 排除 | 169→154→146→138，随后阶段和 seq 都推进 |
| Engine 整体停止 | 排除 | Engine heartbeat 0.28–3.24 秒，seq 72→78→89 |
| MOSS 生成成为 orphan | 排除当前房间 | health orphan_count=0；active 从 1 回到 0；房间进入 judging |
| 房间连续异常但未隔离 | 排除 | `failure_reason` 为空，事件持续产生，未出现 quarantine 状态 |
| 历史 paused_at 误导诊断 | 已确认的接口语义问题 | running 房间仍返回旧 pause event；本地已修正 |
| WebSocket/浏览器缓存旧 snapshot | 当前无证据 | 直接 REST 多次读取均显示实时推进；如仅单个浏览器停留，应单独检查其 WS 重连与 seq |

## 安全建议

1. 管理后台和诊断工具显示绝对时间时同时显示本地时区，例如“北京时间 00:31（UTC 16:31）”。
2. 判断陈旧房间应使用复合条件，不能只看 ISO 日期：
   - status 是否为活动状态；
   - `updated_at`/最新事件年龄；
   - seq 在两次采样间是否增长；
   - remaining 是否按墙钟下降；
   - Engine heartbeat；
   - active provider、orphan_count 和 failure_reason。
3. 建议后续增加只读 stale-room health projection：只有活动房间在超过“当前阶段 deadline + Provider 最大容忍时间”仍无事件更新时才告警。
4. 不要通过自动跳过或终止来“修复”仅仅显示 UTC 或倒计时非零的房间。
5. 若真实房间满足 seq 长时间不变、remaining 已为 0、Provider active=0、无 per-room task 进展，再由管理员查看控制台并使用现有重试/安全暂停能力。

## 验证与边界

- 生产：只读 REST/health 采样，未执行任何写接口。
- 本地测试：`1 passed, 169 deselected`。
- Ruff：通过。
- 可靠语音基线：82 文件，fingerprint `3219138b22878e766c94b9a1fa0e422211a6f27dd74afbbb93a8e45f7e295285`，未改变。
- 生产 SSH 日志读取因当前会话没有可用服务器认证而未取得；公开 append-only 事件流、房间快照和 health 已提供足以排除本次“停滞”的直接证据。
- 本地语义修复未部署，生产当前仍可能对已恢复的 running 房间返回历史 `paused_at`。
