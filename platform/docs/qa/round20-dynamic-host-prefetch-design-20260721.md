# Round20 动态主持播报与 Agent 预取设计审计

日期：2026-07-21
性质：后端只读设计审计；仅额外实现了 AudioCue 文本一致性保护，未部署、未修改冻结实时语音链。

## 结论

第一阶段应采用“显式主持播报阶段 + 预设完整 WAV + 播报期间只预取下一位 AI 的文本”方案。

- 主持播报继续走现有 `announcement`、`AudioCue`、`AudioAsset` 和 `audio.cue.ready`，不改 MOSS/LiveKit/浏览器连续播放链。
- 每个正式发言阶段前插入一个短 `announcement` 阶段。主持 WAV 在开赛前准备，进入该阶段时由现有前端播放。
- 播报期间可以调用 Agent 预取下一个固定 AI 席位的完整文本；真正进入发言阶段后，仍建立一次原生 MOSS 双向流式会话并连续合成，保持同音色、可打断和首声延迟。
- 不建议在共享的两路正式语音容量上预生成 AI 完整音频。后台 TTS 会占住正式比赛槽位，且失效内容会浪费 GPU；预开未启动的 MOSS WebSocket 和 Agent 文本预取已经能移除大部分冷启动延迟。

## 当前系统已经覆盖的能力

### AudioCue 与 announcement

- 自动流程支持 `announcement | speech | free | judging` 四类阶段；管理员可创建版本化流程，旧房间保留自己的 `template_snapshot`。
- 阶段可带 `cue` 文本。房间进入 `preparing` 后，`_prepare_cues()` 会为所有带 cue 的阶段准备房间级 WAV。
- 管理员可按阶段 key 上传 `AudioCue` 预设 PCM WAV；命中时复制到房间目录，不调用 TTS。
- 没有预设时，系统使用当前语音 Provider 后台生成，创建确定性的 `AudioAsset(kind="cue:<stage_key>")`，并写入幂等的 `audio.cue.ready` 事件。
- 缺文件会失效并重建；生成期间终止房间会取消并删除产物；失败会安全暂停到“重试当前步骤”。
- 前端只在当前阶段为 `announcement`、存在匹配 `stage.started` 且开始未超过 10 秒时播放 cue；离开阶段立即停止，历史 cue 不会复活。

### AI 发言与实时语音

- `_ai_speech()` 已把 Agent task id 绑定为 Speech id，固定比赛快照、Agent profile、席位 voice 和历史。
- 正式链路为 Agent delta → 单个 MOSS 双向会话 → 连续 PCM/LiveKit；同一轮不会分段换音色。
- Agent/TTS 准备期间冻结阶段与自由辩论计时，首个权威音频开始后恢复计时。
- 暂停、跳过、终止和阶段失效会中断 Speech、Agent、服务端流和浏览器权威播放；重试仅复用“紧邻且从未发布”的完整文本。
- MOSS 每 endpoint 当前强制最多一个活动会话；若产品上限为两场正式语音，应配置两个独立 endpoint、`max_active=2`、`max_active_per_endpoint=1`。
- Provider 已有未启动 WebSocket 池。预开连接不占活动推理容量，比后台生成整段音频更适合作为 TTS warm-up。

## 主持播报实施方式

### 模板结构

管理员保存新流程版本时，将每个固定发言阶段表达为：

```text
host_before_aff_1_case (announcement)
aff_1_case             (speech)
host_before_neg_1_case (announcement)
neg_1_case             (speech)
...
```

第一阶段使用完整、通用且可复用的播报，例如“接下来进入正方一辩立论，请正方一辩开始发言”。题目和真实姓名继续在大屏显示，不拼接多个音频片段，避免音色漂移、爆音和块间间隙。

建议规则：

- key 必须稳定且版本内唯一，推荐 `host_before_<target_stage_key>`。
- `target_stage_key` 只作为模板元数据，不能由浏览器决定。
- judging 前也可加入“比赛发言结束，正在生成裁判结果”的播报。
- 自由辩论内部的 `free.side_changed` 首期不插入主持语音，否则会改变连续计时和抢答节奏；只播报进入自由辩论这一阶段。
- 已经显式存在 announcement 的相邻位置不再自动重复插入。

### 音频时长

当前 announcement 按固定 `duration` 推进，而不是按浏览器 `ended` 推进。上线前必须保证 WAV 不长于阶段时长，否则切阶段会截断。

推荐在 `_prepare_cues()` 读取最终 WAV 时长，并对房间快照执行以下二选一策略：

1. 严格模式：音频超过 `duration - 0.5s` 时准备失败，要求管理员缩短音频或调整新模板版本。
2. 自动模式：仅对 announcement 的房间运行快照把 duration 调整为 `ceil(wav_duration + 0.5s)`，并写审计事件。

正式赛更推荐严格模式，避免模板总时长静默变化。

## Agent 文本预取

### 允许预取的场景

仅在主持 announcement 已开始且下一阶段满足以下条件时预取：

- 下一阶段是固定 `speech`，不是自由辩论或 judging。
- 目标席位当前是 `ai` 或 `ai_substitute`。
- 上一有效发言已经提交，历史哈希稳定。
- 房间仍为 running，未暂停、跳过或终止。

自由辩论席位受在线状态、阵营轮换和真人恢复影响，首期继续在轮次真正到达时生成。

### 持久模型

建议新增独立 `AgentPrefetch`，不要提前创建 `Speech`，避免预取文本进入历史、活跃发言查询和裁判输入。

核心字段：

- `match_id`、`room_id`、`target_stage_key`、`target_stage_index`、`seat_key`
- `task_id`、`status`（pending/running/completed/consumed/cancelled/invalidated/failed）
- `history_sha256`、`agent_profile_key/version`、`service_snapshot_sha256`
- `content`、usage、latency、错误码、created/updated/completed/consumed 时间

唯一键应覆盖 `(match_id, target_stage_index, seat_key, history_sha256, profile_fingerprint)`。`task_id` 使用上述字段生成 UUIDv5，并直接传给 Agent Gateway，保证引擎重启或重复 tick 不会重复计费和串结果。

### 消费

进入目标 speech 阶段时，在房间锁内重新计算 history/profile/service 指纹：

- completed 且全部一致：原子标记 consumed，创建正常 Speech，写 `speech.content.prefetched`，把文本交给现有单会话 MOSS 管线。
- running：取消预取并走当前实时 Agent 流，避免阶段无限等待。
- 指纹不一致：标记 invalidated，走当前实时 Agent 流。
- failed/cancelled：直接走当前路径，不让预取失败暂停比赛。

预取只是优化，不能成为比赛正确性的前置依赖。

### 取消与恢复

- pause：取消 running 预取；completed 可保留，但 resume 后必须重新校验历史和席位指纹。
- skip：取消当前目标阶段及已经失去可达性的预取。
- terminate/cancel：取消该房间全部 running/pending 预取并调用 Agent interrupt。
- 真人恢复、AI 接替、管理员改席位、发言纠错：依赖指纹变化自动失效。
- Engine restart：running 改为 interrupted/cancelled；completed 保留等待重新校验，不自动写入 Speech。

建议事件：`agent.prefetch.started/completed/consumed/invalidated/cancelled/failed`。事件不携带完整 Prompt、密钥或正文。

## 两场语音容量下的调度

- 两个正式 MOSS endpoint 各只允许一个 active session；任何配置都不得把单 endpoint 并发提高到 2。
- 主持 cue 使用已经准备好的 WAV，不占 MOSS 活动会话。
- Agent 文本预取只占 Agent 并发，不占语音槽；另设小型低优先级 semaphore（建议 1–2），并继续受现有全局 provider semaphore 限制。
- 下一阶段到来时的正式 Agent/TTS 永远优先于预取。预取排队不能暂停房间，也不能占满全部 Agent 槽。
- TTS 侧只做 idle WebSocket warm-up。若未来必须预生成 AI WAV，应使用独立后台 endpoint/容量池；不能复用这两路正式比赛容量。

## 本轮低风险实现

已在非冻结的 `_prepare_cues()` 匹配层增加保护：只有 `AudioCue.key` 和规范化后的 `AudioCue.text` 都与当前 cue 一致时才复制预设 WAV；同 key 动态文本不再误播旧音频，而会回退到当前 Provider 生成。

新增定向测试覆盖“opening key 相同、选手播报文本不同”时必须合成新音频。`py_compile` 已通过；完整 pytest 因本地 `.venv` 仍指向已删除的 `v2/.venv` 且系统 Python 缺 SQLAlchemy，未执行。

## 验收门

- 每次固定阶段切换只播一次主持音频，刷新、重连、延迟授权和历史事件均不会重播旧 cue。
- WAV 全程不被下一阶段截断；结束后不残留下载或播放器实例。
- 四个房间并行时预取内容、task id、历史和人设不串房。
- 暂停、跳过、终止、席位恢复和 transcript 修正后，旧预取不能被消费。
- 预取命中时，从目标阶段开始到浏览器首声仍小于 3 秒，且整轮保持一个 voice/session/generation。
- 两场正式 AI 发言同时进行时，第三场预取不会占用语音槽或增加前两场卡顿。
- Agent 预取失败、超时或服务重启后，比赛可无损回退现有 `_ai_speech()`。
