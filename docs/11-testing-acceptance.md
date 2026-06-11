# 11 · Testing Acceptance

本章定义测试策略、验收场景和现场演练清单。MVP 通过标准是：单场比赛可稳定跑完，异常可被主持人处理，状态不会在各页面之间分裂。

## 1. 测试分层

| 层级 | 内容 |
| --- | --- |
| 单元测试 | 状态机、计时器、投票汇总、Agent payload 构造 |
| 集成测试 | REST 控制命令、WebSocket 续传、SQLite 事务 |
| 端到端测试 | 管理端推进、辩手端发言、大屏渲染、投票公布 |
| 现场演练 | 真实设备、麦克风、投屏、讯飞、Agent |

## 2. 后端验收

### 2.1 状态机

- `draft -> ready -> running -> finished` 正常流转。
- 非法状态命令返回 `invalid_state`。
- 暂停比赛会暂停所有 running clocks。
- 继续比赛会恢复暂停前的 clocks。
- 回退会作废目标环节及之后发言，但不删除历史。

### 2.2 计时器

- 普通环节 `main` 倒计时准确。
- 暂停后 `remaining_ms` 不继续减少。
- 恢复后 `deadline_at` 重新计算。
- 自由辩论只运行当前发言方总时钟和 `turn` 时钟。
- 到时产生 `clock.expired` 和必要的 `speech.timeout`。

### 2.3 事件与续传

- 每个事件有单调递增 `seq`。
- WebSocket 首包为 `snapshot`。
- 客户端带 `last_seq` 重连可收到缺失事件。
- `seq` 跳号时客户端能重新拉快照。

## 3. Agent 验收

- mock agent 能跑通所有 AI 发言环节。
- 流式 delta 能实时上屏。
- TTS 首字播出才启动 AI 发言计时。
- Agent 首字超时后，管理端显示错误并允许重试/跳过/代输入。
- 主持人人工代输入会写入正式 transcript，并结束当前 AI 发言。
- 主持人中断 Agent 后，SSE 和 TTS 都停止。
- 固定环节提前下发可减少 AI 准备等待。

## 4. Speech 验收

- 人类点击开始后，ASR partial 能上屏。
- ASR final 写入正式 transcript。
- 主持人可以修正 transcript，并保留 revision。
- 辩手端优先生成 PCM/L16 录音分片写入 `storage/audio`，数据库记录路径；不支持 PCM 采集的浏览器可退回 webm 留档。
- PCM/L16 分片上传后，管理端 ASR 状态能显示正在接收 PCM 输入，事件流包含 `asr.audio_chunk_received`。
- `PHDEBATE_ASR_REALTIME=1` 或讯飞 ASR 配置完整时，PCM/L16 分片能启动实时 ASR 会话，事件流包含 `asr.stream_started`，并能产生 `asr.partial` / `asr.final`。
- ASR 长连接结束帧后若超过 `XFYUN_ASR_FINAL_TIMEOUT_S` 未收到 final，系统必须写入 `asr.failed`，保留音频归档，允许主持人补识别或人工修订。
- 管理端“语音链路 / 配置检查”能显示讯飞变量完整性、音频目录可写性和降级路径。
- 管理端“ASR 自检”在真实讯飞配置下能完成 WebSocket 连接和识别响应；未配置时应显示可读错误且不阻塞比赛流程。
- 管理端“识别归档”能把辩手端 PCM/L16 归档音频补识别为 transcript；`webm/opus` 归档应返回格式错误并提示需要实时 PCM 流或转码。
- `audio/complete` 显式 `auto_recognize = true` 时能自动回填 transcript；讯飞 ASR 配置完整或 `PHDEBATE_ASR_AUTO_RECOGNIZE=1` 时后台自动补识别不阻塞辩手端。
- 管理端“TTS 试合成”在真实讯飞配置下能生成诊断音频并写入 `PHDEBATE_AUDIO_DIR/diagnostics/`；未配置时应显示可读错误且不阻塞比赛流程。
- `PHDEBATE_TTS_FORMAL=1` 或讯飞 TTS 配置完整时，AI 正式发言 final 文本能生成 TTS 音频，写入 `audio_assets(source = agent_tts)`，事件流包含 `tts.synthesis_started`、`tts.audio_archived`、`tts.finished`。
- ASR 失败时可降级为只计时和录音。
- TTS 失败时可降级为纯文字展示，AI transcript 仍保留并允许比赛继续。

## 5. 前端验收

### 5.1 大屏

- `idle`、`teams`、`live`、`intermission`、`result` 场景可切换。
- `live.single` 显示单人倒计时。
- `live.prep` 显示 AI 思考中。
- `live.free` 显示双方总时间和 15 秒 turn clock。
- `intermission` 展示真实学生投票二维码。
- `result` 在评委未公布时不泄露优胜方、最佳辩手或票型。
- 字幕不遮挡、不溢出，16:9 下可读。

### 5.2 辩手端

- 非本人发言时按钮禁用。
- 轮到本人时按钮高亮。
- 发言中可结束发言。
- 自由辩论本方轮次可请求 AI 队友。
- WebSocket 断开时控制按钮禁用并提示重连。

### 5.3 管理端

- 流程时间线与当前环节一致。
- 时钟、发言人、大屏、辩手端状态一致。
- 危险操作有确认。
- Agent 状态和语音链路错误可见。
- 事件日志按 `seq` 展示。

## 6. 投票验收

- 主持人可开启/关闭学生投票。
- 学生可提交优胜方和最佳辩手。
- 重复 token 提交被拒绝。
- 主持人可通过管理端表单录入立论票、过程票、结辩票、优胜方和最佳辩手。
- 未公布评委结果前，学生结果不能上大屏。
- 公布评委结果后，才能公布学生结果。

## 7. 端到端主流程

必须用 mock agent 至少跑通一次完整赛程：

1. 创建比赛并配置 8 位辩手。
2. 大屏切到 `idle`。
3. 开始比赛。
4. 依次完成 6 个固定发言环节。
5. 自由辩论至少 6 轮：人类、AI、主持人点名、队友请求 AI 都覆盖。
6. 完成双方四辩总结。
7. 进入点评合票。
8. 开启学生投票并提交样例票。
9. 录入评委票。
10. 先公布评委结果，再公布学生结果。
11. 大屏切到 `result`。
12. 导出 transcript、events、votes。

开发/部署前可以先运行轻量 smoke：

```bash
npm run smoke
npm run smoke:browser
```

该脚本会访问当前 `PHDEBATE_BASE_URL` 或默认 `http://127.0.0.1:8000`，并实际调用 REST 接口完成大屏切换、自由辩论人类发言 ASR 模拟、学生/评委投票、公布结果和导出下载。生产鉴权开启时设置：

```bash
PHDEBATE_SMOKE_TOKEN=<host_or_admin_token> npm run smoke
```

浏览器 smoke 会访问当前 `PHDEBATE_BROWSER_BASE_URL` 或默认 `http://127.0.0.1:5174`，实际打开管理端、大屏、辩手端和学生投票页。生产鉴权开启时设置：

```bash
PHDEBATE_BROWSER_SMOKE_TOKEN=<host_or_admin_token> npm run smoke:browser
```

## 8. 异常场景

| 场景 | 期望 |
| --- | --- |
| 大屏刷新 | 自动恢复当前状态 |
| 辩手端刷新 | 恢复身份和按钮状态 |
| WebSocket 断开 10 秒 | 重连后状态一致 |
| Agent 健康检查失败 | 管理端红色告警，可代输入 |
| ASR 失败 | 不影响计时和流程推进 |
| TTS 失败 | 文字继续上屏，主持人可处理 |
| 主持人误点跳过 | 可回退重开，历史不删除 |
| 紧急停止 | 所有时钟/Agent/TTS/麦克风停止或锁定 |

## 9. 现场演练清单

正式比赛前至少一次全链路彩排：

- 使用真实大屏或投影设备。
- 使用真实辩手电脑和麦克风。
- 管理端“现场演练清单”无红色失败项。
- 管理端“赛前体检报告”无红色失败项；若仍有黄色提醒，主持人必须确认降级方案。
- 管理端“语音链路 / 配置检查”在真实讯飞环境下为“真实服务就绪”；无讯飞账号时必须明确显示 mock 降级可用。
- 后端讯飞适配层单元测试覆盖 WebAPI 签名 URL、ASR/TTS payload 构造和返回解析。
- 至少用一段辩手端 PCM/L16 归档音频验证“识别归档”成功路径，并用一段 `webm/opus` 归档验证可读降级提示。
- `npm run smoke` 在现场主机上通过。
- `npm run smoke:browser` 在现场主机上通过，至少覆盖管理端、大屏、一个辩手端和学生投票页。
- 生产鉴权开启时，`PHDEBATE_TOKEN_FILE` 哈希 token 文件可授权管理端/大屏/辩手端，错误辩手 token 不能操作他人。
- 使用真实讯飞账号。
- 使用至少一个真实 Agent 和一个 mock agent。
- 测量 AI 自由辩论从对方结束到 TTS 出声的耗时。
- 验证主持人知道如何重试、中断、代输入、紧急停止。
- 验证音频归档不会占满磁盘。

通过标准：

- 完整流程无手工改数据库。
- 所有异常都有 UI 可见状态和主持人处理路径。
- 大屏、管理端、辩手端状态一致。
