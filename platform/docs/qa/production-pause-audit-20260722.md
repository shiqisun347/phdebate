# 生产暂停比赛只读审计（2026-07-22）

> 审计目的：确认生产环境中所有 `preparing`、`running`、`paused`、`judging` 房间的真实状态、暂停原因和恢复风险。
>
> 本次审计只读访问 `117.50.192.216` 的生产 PostgreSQL、服务进程和日志；没有调用房间控制接口，没有暂停、恢复、重试、终止、清理或修改任何用户房间。

## 结论

生产数据库在 **2026-07-22 07:49:27 UTC（15:49:27 Asia/Shanghai）** 没有正在推进的 `preparing`、`running` 或 `judging` 房间，只有 2 个 `paused` 房间：

| 房间 | 赛事/题目 | 房间状态 | Match 状态 | 当前阶段 | 暂停原因 | 最近一次导致暂停的时间 |
| --- | --- | --- | --- | --- | --- | --- |
| `117519` | 4v4 日常赛 / AI 与人类创作者存在意义 | `paused` | `running` | `-1`（开场前准备） | 没有可用且已暖机的 MOSS-TTS-Realtime endpoint | 2026-07-21 03:02:40 UTC |
| `427792` | 4v4 日常赛 / 热爱还是稳定 | `paused` | `running` | `free_debate`（自由辩论） | MOSS-TTS-Realtime 全任务超时 | 2026-07-21 18:03:50 UTC |

两场暂停均是昨天旧服务运行期间留下的历史异常，不是今天 `round42-flow-asr-cues-20260722` 发布后新产生的异常。当前 MOSS 网关实际健康检查为 HTTP 200，`active=0`、`pending=0`、`orphan_count=0`，模型已暖机。因此，当前“无法继续”的直接原因是房间持久化的异常暂停状态，而不是当前 MOSS 服务仍未就绪。

## 逐房间证据

### 房间 117519：开局前被语音就绪检查暂停

- 创建时间：2026-07-21 03:02:25 UTC。
- `room.locked` 事件记录 `lighttts=false`，随后开场阶段连续 3 次产生 `provider.failed`，错误码均为 `moss_tts_not_ready`。
- 房间从未进入第一个模板阶段：`current_stage_index=-1`，没有 `Speech` 记录。
- 失败后房主/客户端仍有少量断线重连事件，但没有新的比赛推进事件；最后更新时间为 2026-07-21 17:47:46 UTC。
- 当前 `failure_reason` 为“没有可用且已暖机的 MOSS-TTS-Realtime endpoint。”。

判断：这是 MOSS 服务尚未暖机时开始比赛造成的历史记录。现在网关已就绪，理论上可以由房主执行“重试当前步骤”；如果产品不希望用户继续使用跨发布遗留房间，应在 UI 中明确提示“该房间使用旧服务配置，请结束并创建新比赛”。

### 房间 427792：自由辩论中的 MOSS 全任务超时

- 创建时间：2026-07-21 17:46:43 UTC。
- `aff_1` 的立论语音成功完成，时长约 69.021 秒。
- `neg_1` 立论曾出现失败，房主进行了跳过/恢复操作，房间随后进入自由辩论。
- 自由辩论开始后，`aff_1` AI 发言生成 212 个字符，但 `Speech.status=failed`，没有开始播放；对应 `provider.failed` 错误码为 `moss_tts_job_timeout`。
- 房间保存了自由辩论的剩余时间（约 238 秒）和当前方轮剩余时间（约 28 秒），说明暂停快照本身完整；最后客户端只是 `neg_1` 的连接/断开事件，不是自动恢复事件。
- 当前 `failure_reason` 为“MOSS-TTS-Realtime 全任务超时。”。

判断：这是旧版本 TTS 任务超时后的安全暂停，按当前控制器规则不能直接“恢复”，必须“重试当前步骤”。恢复时应保留自由辩论剩余时间，重新生成失败的 AI 发言；当前 API 代码已经有该保护逻辑。

## 状态一致性审计

全库状态计数如下：

```text
rooms:   cancelled 44, completed 22, paused 2, review_required 5, terminated 38
matches: completed 22, review_required 5, running 3, terminated 37
```

发现一个与本次暂停房间相邻、但不在“当前活动房间”列表中的历史一致性问题：

- 房间 `722633` 的状态为 `terminated`，但对应 `Match.status` 仍为 `running`。
- 该房间在 2026-07-21 18:25:26 UTC 发生 MOSS 超时，随后有 `seat.ai_substituted`，没有对应的 `match.terminated` 事件。
- 这不是本次审计所操作的房间，也没有在生产中修复；它需要单独的管理员数据一致性修复工具，不能通过普通房主按钮静默修改。

这说明比赛状态更新在异常/席位接替竞争条件下可能出现“Room 已终止、Match 仍运行”的旧数据。当前控制器的正常 `terminate` 路径会同时写入两者，但应增加启动时/管理员后台的只读一致性扫描和显式修复流程。

## 服务与语音运行时证据

- 生产 API：`round42-flow-asr-cues-20260722`，健康检查 `ok=true`。
- MOSS 网关：`/health/live` 返回 `{"status":"live"}`。
- MOSS 网关：带内部鉴权的 `/health/ready` 返回 HTTP 200，关键字段：

  ```text
  status=ready, model_warmed=true, active=0, pending=0,
  capacity=1, orphan_count=0, warmed_up=true,
  placement.mode=production_all_cuda, realtime_model=cuda:0
  ```

- 当前进程和 Supervisor 服务均处于运行状态：API primary/secondary、engine、worker、MOSS gateway、FunASR、LiveKit、PostgreSQL、Redis、Web 均 `RUNNING`。
- 日志中的 `moss_tts_not_ready` 记录来自旧 API 进程/旧发布或房间首次失败历史；本次审计未发现与当前两个房间在 07:28 API 发布之后对应的新 `provider.failed` 事件。

## 容量影响

创建比赛的生产容量谓词 `ACTIVE_PARTICIPANT_STATUSES` 包含 `lobby、preparing、running、paused、judging`，上限为 5。因此，两个长期暂停房间目前仍占用 **2/5** 个开放比赛名额，实际只剩 3 个名额。此行为目前与代码定义一致，但对用户不友好：旧故障房间若无人处理，会长期阻塞新比赛创建。

## 建议（按优先级）

### P0：在房间控制页明确恢复动作

1. 对 `paused + failure_reason` 展示“服务已恢复/可重试”状态，而不是普通的“已暂停”。
2. 只显示一个主操作“重试当前步骤”，旁边说明：会取消旧任务、保留当前阶段剩余时间、重新生成失败发言；不要让用户误点“恢复”后得到 409。
3. 对 `current_stage_index=-1` 的旧房间显示“开场准备失败，重试会重新执行开场准备”；如果服务快照过旧或缺失，显示“请结束并新建比赛”。
4. 重试完成后通过 WebSocket 广播 `control.retry` 和新的阶段状态，确保多页面不需要手动刷新。

### P1：防止历史暂停房间永久占用名额

1. 增加只读“暂停房间健康卡片”：暂停时长、失败服务、最后事件、是否可重试。
2. 暂停超过可配置阈值（建议 2 小时）且没有连接/恢复请求时，标记为 `stale_paused` 投影；不要自动删除或自动终止用户比赛。
3. 房主确认后可终止并释放容量；管理员可对真正遗留的房间执行带审计日志的终止。
4. 容量提示应显示“开放房间 2/5（其中暂停 2）”，避免用户误以为有 5 场都在正常进行。

### P1：修复 Room/Match 状态一致性

1. 在管理员后台增加一致性扫描：`Room.status` 与 `Match.status` 必须遵循终态映射；将 `terminated room + running match` 作为高优先级异常。
2. 修复 `seat.ai_substituted`、引擎隔离、终止操作之间的事务竞争，确保 `match.terminated` 与 `room.terminated` 使用同一事务或幂等补偿事件。
3. 修复工具必须是显式、可预览、可审计的补偿操作，不在启动时未经确认地批量改写历史记录。

### P2：生产验收

在当前 MOSS 就绪状态下新建一个专用 1v1 灰度房间，验证：开局预设音 → Agent 首字 → 单 LiveKit 音轨连续播放 → AI/人类轮转 → 自由辩论 → 裁判 → 结果。验收完成前不要把 `117519` 或 `427792` 作为新的回归基准。

## 只读审计方法

- 通过 SSH 进入生产主机，仅执行 `SELECT`、健康检查、进程状态和日志读取。
- 查询所有房间状态属于 `preparing/running/paused/judging` 的记录，并关联 `Match`、最近 40 条 `MatchEvent`、最近 12 条 `Speech`。
- 额外检查全库 Room/Match 状态计数和终态一致性。
- 生产 MOSS `/health/ready` 使用内部已有鉴权头访问；报告不记录 API key、Cookie、用户姓名、邮箱、席位租约指纹或其他凭据。

