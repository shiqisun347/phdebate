# 08 · Frontend

本章定义 React 前端的三入口页面、原型映射、状态渲染和交互约束。

## 1. 前端入口

| 路径 | 说明 | 原型 |
| --- | --- | --- |
| `/screen?match_id=match_001` | 大屏展示页 | `prototype/screen-claude.html` |
| `/console/:speakerId?match_id=match_001` | 人类辩手控制台 | `prototype/console.html` |
| `/admin?match_id=match_001` | 管理端 | `prototype/admin.html` |
| `/vote/:matchId` | 学生投票页 | 新建移动端轻量页面 |

前端应共享：

- `types/contract.ts`：后端契约类型。
- `api/client.ts`：REST 封装。
- `realtime/useMatchSocket.ts`：WebSocket 快照和事件处理。
- `state/derive.ts`：只读派生状态。

## 2. 大屏展示页

主原型：`prototype/screen-claude.html`。

### 2.1 Scene 映射

| `ScreenScene` | 页面内容 | MVP |
| --- | --- | --- |
| `idle` | 候场页：Logo、赛事标题、辩题、双方队伍 | 必做 |
| `opening` | 辩题与规则介绍 | 可用 `idle` 变体实现 |
| `teams` | 双方阵容、人类/AI 标识、模型名称 | 必做 |
| `live` | 比赛实况 | 必做 |
| `intermission` | 评委合议、学生投票二维码、当前票数 | 必做 |
| `result` | 按公布状态展示优胜方、最佳辩手、票型、学生投票结果 | 必做 |

### 2.2 LiveMode 映射

| `LiveMode` | 原型状态 | 数据来源 |
| --- | --- | --- |
| `single` | 实况·立论 | 当前 phase、current_speaker、`clock.main` |
| `prep` | 实况·AI准备 | AgentTask 状态、等待时长、AI speaker |
| `free` | 实况·自由辩 | 自由辩论三个 clocks、turn_index、current_turn_side |

### 2.3 字幕规则

- 人类发言显示 `实时转写`，partial 使用弱色，final 正常显示。
- AI 发言显示 `AI 发言` 或 `待播报`。
- `prep` 模式字幕显示“AI 正在生成发言，内容将在开始播报时同步显示……”。
- 字幕必须纯文本渲染，禁止把 Agent 文本作为 HTML 插入。

### 2.4 结果公布规则

- `intermission` 场景必须展示可扫码的 `/vote/:matchId` 二维码。
- `result` 场景在 `judge_published = false` 时只显示“结果待公布”，不得展示优胜方、最佳辩手或评委票型。
- `audience_published = false` 时，同学投票只显示待公布提示；只有公布学生结果后才展示倾向统计和同学最佳。

### 2.4 视觉约束

- 大屏按 16:9 设计，优先适配 1920x1080。
- 背景使用 `prototype/assets/stage-bg.png` 或同等主题背景，文字区域必须有压暗层。
- 页面只读，无流程控制按钮。
- 所有时间数字使用等宽数字或 `font-variant-numeric: tabular-nums`。

## 3. 辩手控制台

原型：`prototype/console.html`。

### 3.1 状态映射

| UI 状态 | 条件 | 主按钮 |
| --- | --- | --- |
| `waiting` | 当前不是本人/本方可发言 | 禁用：“未到你的发言环节” |
| `ready` | 固定环节轮到本人，尚未开始 | 启用：“开始发言” |
| `speaking` | 本人正在发言 | 启用：“结束发言” |
| `free` | 自由辩论轮到本方且无人发言 | “开始发言” + “让 AI 队友发言” |
| `locked` | 主持人锁定麦克风或比赛暂停 | 禁用并展示原因 |
| `offline` | WebSocket 断开 | 禁用并自动重连 |

### 3.2 允许操作

- `start-speaking`：仅本人可点。
- `stop-speaking`：仅本人发言中可点；主持人强制结束走管理端。
- `request-ai-teammate`：仅自由辩论、本方轮次、人类辩手可点。
- 音频归档：本人发言中优先启动 Web Audio PCM/L16 采集，按 500ms 分片调用 `audio-chunks`；浏览器不支持时退回 `MediaRecorder` webm 留档；停止录制后调用 `audio/complete`。
- `speaker.heartbeat`：定期上报连接和麦克风权限。
- `speaker.mic_error`：麦克风权限或设备异常时上报。

### 3.3 不显示内容

辩手控制台不显示完整辩论文本、不显示管理按钮、不显示 Agent URL/API Key、不提供复杂编辑器。

## 4. 管理端

原型：`prototype/admin.html`。

### 4.1 比赛监控 Tab

必须包含：

- 流程时间线：10 个环节、当前环节、已完成/跳过状态。
- 当前环节控制：当前发言人、下一轮提示、时钟。
- 控制按钮：暂停/继续、回退、跳过、下一环节、校准时钟、手动响铃、强制结束。
- 赛前体检报告：后端聚合比赛基础、页面设备、Agent、语音、投票、导出和生产鉴权检查项。
- 指定发言人：自由辩论中按当前轮次方展示可选辩手。
- AI 干预：健康检查、重试、中断、人工代输入；人工代输入保存后进入正式发言流。
- 实时转写/发言流：ASR partial/final、AI final、修正、标记无效。
- Agent 状态：心跳、生成中、失败、首字延迟。
- 语音链路：ASR/TTS、音频归档分片数/大小/路径、大屏连接、辩手端在线数。
- 事件日志：按 `seq` 展示。

### 4.2 投票与结果 Tab

必须包含：

- 评委投票录入表：立论票、过程票、结辩票、最佳辩手。
- 学生投票状态：未开/投票中/已关闭、票数、二维码入口。
- 公布按钮：先公布评委结果，再允许公布学生结果。
- 结果摘要：优胜方、最佳辩手、票型。
- 切大屏到 `result` 场景。

### 4.3 比赛设置 Tab

必须包含：

- 基础信息：比赛名称、辩题、双方立场。
- 队伍与辩手：持方、辩位、人类/AI、模型、Agent URL。
- 赛制与自由辩论：流程总览，环节名称/时长编辑，自由辩论双方总时间和单次上限编辑。
- AI 时序策略：计时起点、准备超时、超长处理、提前下发。
- 语音与大屏：ASR/TTS 服务、TTS 发音人、背景图、主色。
- 现场入口分发：管理端、大屏、辩手端、学生投票页链接；支持主机地址调整、token 填写、二维码生成、复制链接、打印分发清单、生成/轮换本地 token 套件、批量导入 token 配置，并输出可复制的 `PHDEBATE_*` 环境变量片段和只含 SHA-256 哈希的 token 文件。

运行中设置页应锁定会破坏状态机的字段，如辩位、环节顺序、已开始环节时长。

## 5. 学生投票页

路径：`/vote/:matchId`

移动端优先：

- 展示辩题、双方立场。
- 选择优胜方。
- 选择最佳辩手。
- 提交后展示“已收到投票”。
- 投票窗口关闭时展示“投票已关闭”。

不得实时展示投票结果。

## 6. 前端状态处理

前端 store 至少包含：

```ts
type MatchClientState = {
  snapshot: MatchSnapshot | null;
  lastSeq: number;
  socketStatus: "connecting" | "open" | "closed" | "reconnecting";
  serverTimeOffsetMs: number;
};
```

事件处理规则：

- 收到 `snapshot`：整体替换快照。
- 收到普通事件：先校验 `seq`，再局部更新或触发重新拉快照。
- 倒计时：用 `deadline_at + serverTimeOffsetMs` 本地刷新。
- 断线：页面保留最后状态，但控制按钮禁用，显示重连提示。

## 7. 可访问性与稳定性

- 关键按钮必须有禁用态和加载态，避免重复点击。
- 管理端危险操作必须二次确认：紧急停止、回退、标记无效、结束比赛。
- 大屏文字必须在 16:9 和常见投屏缩放下不遮挡。
- 辩手端按钮尺寸适合触控板/触屏，降低误触。
- 所有错误提示应给出主持人可执行动作，而不是只显示技术错误。
