# Round 11 多房间比赛引擎与恢复审计

日期：2026-07-20

## 本轮边界

- 只审计比赛状态机、REST 控制、Agent 任务生命周期、真人断线恢复、归档与排行榜一致性。
- 未修改 DebateStage、TTS/MOSS、LiveKit、AudioWorklet、PCM、浏览器播放队列或任何音频质量参数。
- `admin.py` 与 `match_archive.py` 未修改。
- 真人录音上传接口只补充“终局归档重新排队”，不改变录音、编码、验证或播放行为。

## 新发现并修复的问题

### 1. 弱网迟到文字恢复后，正式归档没有刷新

`completed` / `review_required` 仍允许超时真人发言从浏览器本地恢复，这是 DebateStage 明确提供的学生补救能力：比赛可能在录音和 ASR 最终文字到达前先完成，不能直接丢弃学生已经完成的证据。问题在于此前恢复成功后不会刷新已经产生的比赛归档，历史页与归档源数据会不一致。

修复：

- 保留 `completed` / `review_required` 的 `timed_out` late-finalize 能力，与冻结的 DebateStage 恢复按钮契约一致。
- late-finalize 成功后，如果比赛已经完成或进入复核，重新排队该 `match_id` 的归档任务。
- `terminated` / `cancelled` 继续作为不可写边界，紧急终止后的陈旧请求仍返回 409。

验证：

- 分别构造 `completed` 与 `review_required` 的超时真人发言。
- 先生成归档，再模拟弱网页面恢复本地文字。
- 均成功保留文字并只排队当前 `match_id`；归档源摘要随权威发言记录更新。

### 2. 真人录音晚于极速裁判完成时，正式归档可能漏掉音频引用

真人文字完成与录音上传是两个请求。最后一位真人结束发言后，自动裁判可能先完成并排队生成归档，随后浏览器才上传录音。此前第二个请求会更新 `Speech.audio_url`，却不会刷新已经生成的终局归档。

修复：

- 当录音附加到 `completed`、`review_required` 或 `terminated` 比赛时，提交事务后重新排队该场比赛的归档任务。
- 不改变上传格式、文件大小、媒体验证、ASR、TTS 或网页播放逻辑。

验证：

- 先完成比赛并生成无录音归档。
- 再模拟真人录音异步到达。
- 确认只排队当前 `match_id`，归档源摘要发生变化，音频 URL 和时长进入权威记录。

## 新增竞态与隔离测试

文件：`apps/api/tests/test_round11_engine_scenarios.py`

### 真人提交与紧急终止同时到达

- 两个独立 HTTP 客户端同拍提交 `speech/finish` 与 `control/terminate`。
- 允许的线性化结果只有两种：真人先完成再终止，或终止先成功并拒绝迟到完成。
- 最终始终满足：房间与 Match 同为 `terminated`、不存在活动发言、终止事件之后没有 `speech.completed` / `speech.late_finalized` / `stage.started`。

### Agent 返回与人工跳过同时发生

- 两个房间并行调用 Agent。
- 房间 A 的 Agent 故意阻塞；房主跳过当前阶段后才释放旧结果。
- 房间 B 同时正常完成。
- 验证 A 的迟到结果只留下 `interrupted` 空内容记录，不能写入下一阶段；B 的内容、阶段和记录保持独立。

### 隐藏大厅 WebSocket 拒绝路径

- 直接执行“Socket 已 accept，随后发现匿名用户不能读取等待大厅”的隐私分支。
- 当前实现调用 WebSocket close 4401 并正常返回，不会在 accept 后尝试发送 HTTP denial response。
- 现有端到端测试也覆盖：匿名等待大厅 4401、私密房间越权 4403、恶意 Origin 4403。

## 冻结路径中的已确认风险

生产旧日志曾出现：

```text
Task exception was never retrieved
async_generator_asend
ProviderError('辩手 Agent 调用失败')
```

本轮构造了确定性失败注入，确认当前 `IncrementalVoicePipeline.run()` 仍存在一个极窄取消竞态：

1. `anext(agent async generator)` 已经以 `ProviderError` 完成；
2. 父 pipeline 在同一事件循环拍被取消；
3. 清理逻辑只 `gather` 未完成的 `pending_event`，没有取走已完成 task 的异常；
4. asyncio 因而报告 `Task exception was never retrieved`。

该问题不会跨房写入结果，但会制造未回收任务告警，并削弱故障可观测性。可靠修复位于被冻结的 `services/voice_runtime/pipeline.py`。遵守本轮“当前可靠 TTS/播放不修改”的要求，没有改动该文件；测试以非严格 `xfail` 保存最小复现，不会让发布门禁变红。后续只能在独立音频变更窗口进行纯任务回收修复，并重新跑音频指纹、首声延迟、连续播放和中断全链路回归。

## 已有能力复核

以下不变量已有 Round 5–10 测试覆盖，本轮组合回归全部通过：

- 20 个房间并行完成且事件序列、Agent 内容、裁判结果不串房。
- 24 个准备中、真人等待、超时、播放中、裁判中和暂停房间并发推进。
- Agent 超时后房主幂等重试，其他房间继续完成。
- Agent 无效内容、thinking/reasoning 泄漏拦截。
- 暂停、跳过、终止后迟到 Agent/裁判结果不能复活旧任务。
- 引擎重启后中断旧 AI Speech / Judge，并可复用只完成文本、未发布音频的 Agent 结果。
- 真人断线 60 秒后 AI 接替；真人真实重连、无活动发言、无跨房占座后才可申请并审批恢复。
- 房主断线、停用、主动退出后的控制权移交；旧房主不能继续控制。
- 一个用户不能同时占用多个活动房间；一个房间最多一个活动 Speech。
- 排行榜初次结算幂等，测试房间不进入正式排行榜。

## 验证结果

聚焦测试：

```text
tests/test_round11_engine_scenarios.py
6 passed, 1 xfailed
```

Round 6/8/9/10/11 组合回归：

```text
27 passed, 1 xfailed
```

完整 API 测试：

```text
409 passed, 1 xfailed
```

静态检查：

```text
ruff check app/api/rooms.py tests/test_round11_engine_scenarios.py
All checks passed
```

可靠音频基线：82 个受保护文件通过，指纹仍为
`3219138b22878e766c94b9a1fa0e422211a6f27dd74afbbb93a8e45f7e295285`。

`xfail` 仅为上述冻结语音 pipeline 取消竞态的可复现审计项，不代表新增失败。
