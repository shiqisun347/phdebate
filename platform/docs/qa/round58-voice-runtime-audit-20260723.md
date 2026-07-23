# Round 58 实时语音与真人断线安全审计

> 审计日期：2026-07-23（补充验证于 2026-07-24）  
> 范围：Agent 流式文本、MOSS-Realtime、LiveKit 单音轨、浏览器播放、真人断线 60 秒安全暂停  
> 本轮只修改本地代码并运行测试，未部署、未重启生产服务。

## 结论

- 系统不存在仍在使用的“真人席位自动转为 AI”路径。比赛开始时只会把**从未被真人认领的空席**补为 AI；已开赛真人席位保持真人身份。
- 找到并修复一个会绕过 60 秒安全边界的真实问题：每个房间的主任务可能长时间阻塞在 Agent/MOSS 调用中，旧调度器会跳过仍有任务的房间，因此真人断线满 60 秒后，暂停可能要等外部调用返回才执行。在此期间，当前 AI 发言可能继续生成或播放。
- 修复后，断线超时检查独立于每房间主任务运行。真人断线达到 60 秒时，系统会暂停比赛、关闭当前 Speech、撤销当前 LiveKit generation、通知浏览器清空播放尾音，并取消 Agent/TTS 生成及预生成任务。
- 未发现其他可把断线真人替换为 AI 的旁路。已废弃的 `abandon-seat` 接口固定返回 `410`，历史 `ai_substitute` 数据仅由数据库迁移清理，不参与运行时流程。

## 修复内容

### 1. 断线计时不再依赖房间主任务返回

`MatchEngine.tick()` 现在在调度房间任务前执行独立的断线安全扫描。扫描范围包含 `preparing`、`running`、`paused` 和 `judging` 房间，并通过数据库行锁串行化状态更新。

达到 60 秒边界后会一次性执行：

1. 将比赛状态设为 `paused`；
2. 保留真人席位、用户身份及已确认文字；
3. 将当前 `speaking/synthesizing/playing` Speech 标记为 `interrupted`；
4. 发出 `participant.disconnect_timeout` 和 `match.paused`；
5. 对当前实时音频发出 `audio.rtc.interrupt`；
6. 撤销 LiveKit generation，并清除未播放音频；
7. 取消下一阶段 Agent 预生成及自由辩论推测任务。

### 2. Agent/TTS 在 60 秒边界主动 fail-closed

实时语音流水线每 50 ms 检查一次取消条件。取消条件现在会直接查询是否存在断线满 60 秒的真人，因此即使安全暂停写库短暂遇到行锁竞争，Agent/TTS/LiveKit 链路也不会继续放音。

取消预生成协程时，同时调用远端 Agent 的 interrupt 接口，防止本地任务结束后远端幂等任务仍继续占用资源并晚到回写。

### 3. 幂等键长度修复

生产历史日志显示，旧版真人断线事件的可读幂等键及其 `:match-paused` 后缀可能超过 PostgreSQL `VARCHAR(120)`，导致暂停事务失败。当前实现使用稳定身份哈希，两个幂等键均保持在字段限制内。

## 实时语音链路审计

| 环节 | 当前实现 | 结论 |
| --- | --- | --- |
| Agent | SSE 增量输出；关闭思考内容后，只将可朗读正文送入语音流水线 | 通过 |
| TTS text-in | 同一发言只建立一个 MOSS 原生上下文，持续发送 `text_delta`，不按句重连 | 通过 |
| TTS audio-out | MOSS PCM 持续输出，服务端保留有界启动缓冲 | 通过 |
| LiveKit | 每房间预建立 publisher，一场发言只使用同一 `agent-tts` WebRTC 音轨 | 通过 |
| 浏览器 | AudioWorklet 门控同一远端音轨；中断时清样本并使用短去爆音渐变 | 通过 |
| ASR | AudioWorklet 20 ms PCM 帧通过 WebSocket 双向传输，不依赖 MediaRecorder Blob | 通过 |
| 中断链 | Agent interrupt → TTS abort/clear → 服务端 generation 撤销 → 浏览器 flush | 通过 |
| 真人断线 | 60 秒内保留席位；满 60 秒自动安全暂停，不生成 AI 替代发言 | 修复后通过 |

## 生产只读证据

- 生产配置使用 `moss_realtime`、实时语音流水线和 LiveKit WebRTC；未启用多播放器兜底。
- 当前健康路径中，Agent 首个可读字符到首个 PCM 通常约 `1.13–1.40 s`；有浏览器可听上报的样本中，首个服务端 capture 到浏览器可听约 `0.30–0.56 s`，合计约 `1.55–1.83 s`，满足 3 秒目标。
- SDPA 生产基准 RTF 为 `0.887–0.915`，首个非静音 PCM 为 `521–638 ms`。FlashAttention 2 的现有金丝雀结果 RTF 为 `2.72–2.77`，在当前 RTX 3090 / MOSS 实现上更慢，因此生产继续使用 SDPA 是正确选择。
- 历史遥测曾出现 `20–89 s` 的首 PCM 队列饥饿样本；最新样本未复现。该问题仍应通过持续比赛遥测观察，不能只依赖离线音频质量判断。

## 验证结果

执行：

```text
ruff check app/services/match_engine.py tests/test_round45_no_ai_takeover_policy.py
pytest -q \
  tests/test_round45_no_ai_takeover_policy.py \
  tests/test_round57_human_recovery_clock.py \
  tests/test_round8_engine_scenarios.py \
  tests/test_round9_engine_scenarios.py
```

结果：

```text
Ruff: passed
Pytest: 30 passed
```

另行覆盖实时语音、LiveKit 和断线安全的组合回归：

```text
接口退役、断线暂停、LiveKit 与实时语音：46 passed
扩大组合回归：54 passed
```

新增回归场景：

- 房间主任务被外部 provider 阻塞时，独立安全扫描仍可在 60 秒边界暂停比赛；
- 正在播放的 AI 音轨立即撤销 generation，并产生浏览器清队列事件；
- TTS 取消谓词在断线超时落库前即返回 `true`；
- 取消 Agent 预生成会中断对应远端幂等任务；
- 真人席位类型、用户 ID、姓名和已确认文字保持不变。

## 已知限制

- 浏览器未解锁音频或页面不在线时，不会产生“实际可听”遥测；服务端首 PCM 仍可记录，但不能等价替代端到端可听时间。
- 当前 RTF 主要由基准和金丝雀任务记录，尚未为每次正式发言持久化完整 active RTF。建议后续只增加轻量数字遥测，不改变当前语音热路径。
- ASR 上游连接按真人发言会话建立，并非进入房间即预热；这不影响本次“断线不接管”规则，但仍是首个 ASR partial 延迟的可优化项。
- 音频归档关闭时，生成过程仍可能使用临时 WAV 工作文件，完成或中断后删除；它不是可访问的比赛录音，但仍有少量磁盘 I/O。

## 发布建议

该修复属于比赛安全 P0。部署前应在预发布环境做一次真实长 Agent 流：让真人断线发生在另一名 AI 正在生成和播放期间，确认第 60 秒出现暂停提示、声音停止、席位仍为真人，并在全部真人重连后只能由房主或管理员继续比赛。
