# Round 64：4v4 多用户全流程、实时语音恢复与参考图 UI 综合验收

日期：2026-07-24  
状态：本地实现与回归完成；是否部署以生产环境活动房间检查为准。

## 结论

- 真实浏览器完成一场 `2 真人 + 6 AI` 的 4v4 正式赛，房间 `#484518` 从大厅、准备、固定发言、自由辩论、双方总结到裁判完整结束。
- 最终房间和 Match 均为 `completed`，裁判为 `approved`，反方获胜；数据库共有 12 条完成发言、112 条比赛事件，没有空文字发言。
- 修复自由辩论总时长归零后仍可能多启动一个 Agent 回合的问题。
- 修复 LiveKit 音轨被取消订阅或底层轨道 `ended` 后页面静默无声的问题；恢复失败仍坚持单一 WebRTC 播放链，不切换备用播放器。
- 修复旧发言的迟到中断事件误清空下一条 AI 音轨的问题。
- 修复终态结果页仍建立房间 WebSocket、占用全局 5 个观众名额并显示错误“立即重连”提示的问题。
- 删除仍上传真人录音的旧并发脚本，将真人端到端验证改为只检查文字持久化、幂等提交和录音上传入口下线。
- 按根目录 `image.png` 完成桌面、移动端和文字记录抽屉的截图对比，并统一“正方红、反方蓝”的阵营视觉语义。

## 浏览器真实比赛证据

本地房间：`#484518`。

阵容：

- 真人：正方一辩、反方一辩。
- AI：其余 6 个席位。
- 真人固定阶段和自由辩论均通过参赛页面提交文字。
- 空席在开赛时由固定 AI 辩手填充，没有发生真人断线后的 AI 接管。

数据库终态：

```text
room.status       completed
match.status      completed
winner            neg
judge.status      approved
speeches          12
events            112
AI completed      9
human completed   3
empty completed   0
```

截图：

- `round64-browser-4v4-owner-result.png`
- `round64-browser-4v4-participant-result.png`
- `round64-browser-4v4-anon-result.png`
- `round64-browser-4v4-anon-result-no-reconnect.png`

匿名结果页没有文字稿，也不再显示“实时连接已断开”或“立即重连”。终态结果通过 REST 手动刷新，不占用实时观战连接。

## `image.png` 页面映射与视觉校准

参考画布：`1672 × 941`。

### 页面映射

| 参考区域 | 当前实现 |
|---|---|
| 顶部品牌、房间号、辩题和连接状态 | `stage-topbar` |
| 左右双方四席 | `team-column` 与 `SeatCard`，支持 1v1/4v4、真人/AI、在线和当前发言状态 |
| 中央当前发言人和波形 | `speaker-focus`，展示姓名首字、席位、人类/AI 与连续波形视觉 |
| 阶段和权威计时 | `stage-status` 与 `timer-orb` |
| 发言内容 | 辩手页只显示当前实时字幕；观众页只显示赛况，不显示文字稿 |
| 底部操作带 | 唯一主发言按钮、声音、暂停、全屏和设置 |
| 右侧文字记录 | 仅参赛者和授权控制者可打开抽屉；观众不挂载入口 |

复杂角色立绘属于外部视觉资产。当前系统没有真实用户头像资产，因此保留稳定的姓名首字头像，不从参考截图裁切或伪造人物图片。

### 截图验证

- 桌面辩手页：`round64-image-reference-evidence/debate-reference.png`
- 桌面文字记录抽屉：`round64-image-reference-evidence/debate-reference-transcript.png`
- 手机辩手页：`round64-image-reference-evidence/debate-mobile.png`
- 桌面观战页：`round64-image-reference-evidence/watch-reference.png`

验证视口：

```text
桌面 1672 × 941：scrollWidth=1672，scrollHeight=941
手机 390 × 844：scrollWidth=390，scrollHeight=844
```

控制区全部位于视口内，页面没有横向溢出。观战页没有“文字记录”按钮，也没有发言文字内容。

截图对比发现并修复了旧阵营色残留：正方标题和边线为红色，但旧头像、焦点和高亮仍为蓝色；反方相反。现已统一为正方红、反方蓝，“本人席位”使用独立青绿色内框，避免被误认为当前发言席位。

## 状态机与故障恢复

### 4v4 权威状态机

新增 `test_round64_complete_mixed_match.py`，覆盖：

- 全部预设女声主持提示。
- 2 真人 + 6 固定 Agent。
- 固定发言、自由辩论、总结和裁判。
- Agent 首次超时、安全暂停、房主重试和成功恢复。
- 真人断线 61 秒、自动暂停、原席位保留、重连后由房主继续。
- 第二房间作为隔离哨兵，验证事件、文字、状态和裁判不串房。

### 实时音轨

- 浏览器只接收一个 `agent-tts` LiveKit 音轨。
- `TrackUnsubscribed` 或 `MediaStreamTrack ended` 触发 0.5/1/2 秒有界恢复。
- 自恢复音轨出现后取消尚未执行的应用层重连。
- 中断必须绑定对应 generation；旧事件不能清空新发言。
- 暂停、裁判和终态清空当前 generation，避免尾音复活。
- 不启用 WAV、PCM WebSocket 或多播放器兜底。

## 控制与安全边界

- 运行中不提供房主移交。
- 服务异常暂停和真人断线暂停不能通过“跳过”掩盖。
- 服务异常并伴随真人断线时，必须先等待全部真人返回，再重试当前步骤。
- 本页有尚未提交文字时，暂停、重试、提前结束和离开均被阻止并显示原因。
- AI 生成/合成与 AI 播放使用不同状态和不同恢复动作。
- 所有观众不可见文字稿；匿名结果页也不暴露内容。

## 旧入口清理

- `scripts/e2e_human_flow.py` 不再生成 WAV 或上传真人录音。
- 新脚本验证文字写入、重复提交幂等、冲突提交返回 409、OpenAPI 不暴露真人录音上传路径、旧路径返回 404。
- 删除 `scripts/verify_parallel_audio_uploads.py`，避免继续维护已经退役的录音归档能力。
- 数据库中的历史 `audio_url` 字段仍保留，用于预设主持音和旧记录兼容；这不代表真人录音上传入口重新开放。

## 最终自动化结果

Web：

```text
54 test files passed
410 tests passed
ESLint: 0 errors, 13 existing warnings
Next.js production build: passed
```

API：

```text
594 passed, 13 skipped, 1 xfailed, 2 warnings
Ruff: All checks passed
```

## 尚需真实生产环境证明的指标

本轮本地 Agent 与 TTS 使用测试替身，没有真实 MOSS GPU、LiveKit 服务端、公网抖动和学生麦克风，因此本报告不宣称：

- Agent 首字到浏览器首声小于 3 秒。
- MOSS RTF 小于 1。
- 真实公网下完全没有卡顿、撕裂音、吞字或音色漂移。

上述指标必须在目标服务器上用真实 Agent、MOSS、LiveKit、Chrome/Safari、网络整形和实体扬声器继续验收。生产环境存在运行中比赛时不得部署或重启服务。
