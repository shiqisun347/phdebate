# 09 · Voting

本章定义评委投票、学生扫码投票和结果公布时序。MVP 中评委线下投票，由主持人在管理端代录；学生使用手机扫码投票。

## 1. 投票类型

| `VoteType` | 投票目标 | 说明 |
| --- | --- | --- |
| `constructive` | `target_side` | 立论票 |
| `process` | `target_side` | 过程票 |
| `conclusion` | `target_side` | 结辩票 |
| `winner` | `target_side` | 优胜方，通常由前三类汇总得到，也允许主持人确认 |
| `best_speaker` | `target_speaker_id` | 最佳辩手 |

## 2. 评委投票

MVP 流程：

1. 评委现场填写或口头给出结果。
2. 主持人在管理端代录立论票、过程票、结辩票、正式优胜方和最佳辩手。
3. 系统保存为 `votes(voter_type = judge)`。
4. 主持人点击“公布评委结果”。
5. 大屏 `result` 场景展示评委票型、优胜方、最佳辩手。

AI 评委在 MVP 中可作为手动录入的 `voter_type = ai_judge`，不实现自动评分链路。

## 3. 学生投票

### 3.1 开启

主持人点击开启学生投票：

- `vote_windows.status = open`
- 生成或启用 `/vote/:matchId` URL
- 大屏 `intermission` 场景展示二维码

### 3.2 提交

学生提交内容：

```json
{
  "winner_side": "affirmative",
  "best_speaker_id": "spk_neg_2"
}
```

防重复策略：

- 优先使用一次性 token：适合现场发放或二维码附带。
- 如果没有 token，使用浏览器指纹 + IP 的弱防重复。
- MVP 不做校内统一身份认证。

### 3.3 关闭

主持人关闭窗口后：

- 新提交返回 `invalid_state`。
- 已提交结果仍可统计。
- 结果不自动公开。

## 4. 公布顺序

必须满足：

```text
学生投票开启/关闭 -> 评委结果录入 -> 公布评委结果 -> 公布学生结果
```

系统约束：

- `scope = audience` 的 `votes/publish` 必须检查 `judge_published_at IS NOT NULL`。
- 学生投票结果在评委结果公布前，管理端可显示票数但不展示倾向统计给大屏。
- 大屏结果页在评委结果未公布时不得展示优胜方、最佳辩手或评委票型。
- 大屏结果页中“同学投票”在未公布前显示“评委结果公布后展示”。

## 5. 结果汇总

评委票汇总：

```json
{
  "judge": {
    "constructive": { "affirmative": 2, "negative": 1 },
    "process": { "affirmative": 1, "negative": 2 },
    "conclusion": { "affirmative": 3, "negative": 0 },
    "winner_side": "affirmative",
    "best_speaker_id": "spk_neg_2"
  }
}
```

学生票汇总：

```json
{
  "audience": {
    "total": 137,
    "winner": { "affirmative": 83, "negative": 54 },
    "best_speaker": [
      { "speaker_id": "spk_neg_2", "count": 41 },
      { "speaker_id": "spk_aff_3", "count": 35 }
    ]
  }
}
```

优胜方计算规则 MVP 采用主持人确认优先：

- 系统根据评委票给出建议 `computed_winner_side`。
- 主持人确认后写入 `winner` 票或结果字段。
- 若现场规则临时调整，以主持人确认结果为准，并写审计。

## 6. 管理端状态

| 状态 | 可用动作 |
| --- | --- |
| `closed` 且未开过 | 开启学生投票 |
| `open` | 关闭学生投票 |
| `closed` 且未公布评委 | 录入/修改评委票、公布评委结果 |
| 已公布评委 | 公布学生结果、切结果页 |
| 已公布学生 | 只读查看 |

管理端评委票录入表单字段：

- 立论票：正方票数、反方票数。
- 过程票：正方票数、反方票数。
- 结辩票：正方票数、反方票数。
- 正式优胜方：主持人确认结果，优先于系统建议。
- 最佳辩手：从 8 位辩手中选择。

已公布结果后如需修改，MVP 允许主持人重新保存评委票并重新公布；正式比赛建议将修改原因写入现场记录。

## 7. 验收点

- 学生不能在关闭窗口后提交。
- 同一 token 不能重复提交。
- 未公布评委结果前，大屏不能显示学生倾向统计。
- 公布学生结果后，大屏 `result` 场景能展示同学投票模块。
- 评委票修改会记录审计日志。
