# Round 63：双真人 1v1 完整浏览器流程

| 字段 | 内容 |
|---|---|
| 日期 | 2026-07-24 |
| 本地环境 | `http://localhost:13320` |
| 房间 | `#891204` |
| 参赛者 | 沈知行（正方一辩）、顾明澈（反方一辩） |
| 流程 | 注册 → 建房 → 房间号加入 → 双方准备 → 立论 → 自由辩论 → 总结 → AI 裁判 → 结果 |

## 结果概览

- 房间和比赛最终均为 `completed`，双方结果页一致显示“正方胜利”。
- 双方立论和总结的文字应急发言均完整保存。
- 匿名结果页不展示“完整辩论文字记录”。
- 发现 1 个会造成正式比赛数据丢失的 P0 问题：自由辩论文字应急发言在短轮次中未保存，最终被记录为超时空发言。

## ISSUE-001【P0】：自由辩论文字应急发言丢失

**现象**

页面明确允许当前辩手点击“麦克风不可用？改用文字发言”，输入框也接受了内容；但自由辩论轮次在提交动作生效前被服务端超时切换。结果页仍可完成比赛，却没有保存用户刚刚输入的文字。

**复现步骤**

1. 进入自由辩论，页面显示“点击开始发言后计时 · 单轮时长 00:08”。
2. 点击“改用文字发言”，在文本框输入内容并尝试提交。
3. 页面切换到对方或下一阶段，看似流程正常继续。
4. 检查权威数据库，3 条自由辩论 Speech 全部为 `timed_out`、`chars=0`。

**权威数据证据**

```text
stage_key    seat_key  chars  status
free_debate  aff_1     0      timed_out
free_debate  neg_1     0      timed_out
free_debate  aff_1     0      timed_out
```

**交互证据**

- [自由辩论文字输入与阶段切换视频](videos/issue-001-free-turn-expiry.webm)
- [房主结果页](screenshots/owner-result.png)
- [参赛者结果页](screenshots/participant-result.png)

**预期**

- 进入文字应急模式后，服务器必须给出明确且足够的提交窗口，不能继续使用已消耗的语音轮次截止时间。
- 已输入并提交的内容必须以服务端确认成功为准；提交未确认前不能以无提示方式切换阶段。
- 如果截止时间与提交并发，服务端应通过原子状态/幂等键决定唯一结果，并明确告诉用户“已提交”或“本轮已结束”，不能静默丢失。

**修复与复验**

已修改文字应急组件：服务端将发言标记为 `timed_out` 后，不再关闭并清空本地编辑器；即使比赛已经切换到对方、下一阶段或结果生成阶段，仍允许把内容补写到原 `speech_id`。页面明确显示“补交已超时发言”，服务端继续校验席位、控制权和幂等键。

修复后使用第二个双真人房间 `#582037`，将自由辩论单轮缩短为 3 秒：输入内容后故意等待超时，编辑器和全文仍保留；点击补交后权威数据库结果为：

```text
stage_key    seat_key  chars  status
free_debate  aff_1     35     completed
```

修复证据：

- [超时后全文和补交按钮仍保留](screenshots/issue-001-fixed-timeout-draft-retained.png)
- [补交完整操作视频](videos/issue-001-fixed-late-finalize.webm)

## 已通过证据

- [房主结果页](screenshots/owner-result.png)
- [另一名参赛者结果页](screenshots/participant-result.png)
- [匿名结果页无文字稿](screenshots/anonymous-result.png)
