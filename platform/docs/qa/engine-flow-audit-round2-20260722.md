# 比赛引擎流程审计：断线暂停事件幂等键修复

## 发现

真实浏览器回归房间在真人断线超过 60 秒后，状态机连续三次写入失败并进入 `engine.quarantined`。服务器日志显示 PostgreSQL 报错：`value too long for type character varying(120)`。

断线暂停事件的幂等键由房间 UUID、席位 UUID、毫秒时间戳和 `:match-paused` 后缀组成，超过 `match_events.idempotency_key VARCHAR(120)` 的约束。结果是本应安全暂停的比赛被错误包装成服务异常，且事件没有可靠落盘。

## 修复

- 使用房间、席位和断线时间组成稳定身份。
- 使用 SHA-256 摘要生成固定长度幂等键。
- `participant.disconnect_timeout` 与 `match.paused` 两个事件均保持幂等、可重放且不超过数据库长度限制。
- 真人席位仍保持 `human`，不会生成 `seat.ai_substituted`。

## 验证

- `tests/test_round45_no_ai_takeover_policy.py`：13 passed。
- 新增断线事件幂等键长度断言：所有相关键 `<= 120`。
- 生产日志中已确认原错误根因；修复后需重新部署并用临时房间验证 61 秒断线路径。

## 当前规则

真人断线 60 秒后，比赛暂停并保留席位、已确认文字和阶段进度；真人重新连接后由房主或管理员显式继续。旧历史 `ai_substitute` 仅兼容读取，不属于当前流程。

## 本轮相邻流程审计（2026-07-22）

### 关注范围

- 开赛请求返回 `preparing` 后，首条系统提示音准备、首阶段进入和主持提示音结束后的正式阶段切换。
- 固定阶段、自由辩论申请窗口、AI 生成/语音准备期间的计时冻结。
- 房主暂停、继续、重试、跳过、重置和终止的状态边界。
- 多房间并行处理时，单房间的 Provider/计时异常不能改变其他房间。
- 真人席位在断线、重连和控制权租约变更时仍保持 `human`，60 秒超时只暂停，不触发 AI 接管。

### 当前确认的流程语义

1. 房主点击开始后，房间进入 `preparing`；引擎只准备首阶段必需提示音，成功后进入阶段 0。后续提示音在比赛进行期间预取，不阻塞其他房间。
2. 带主持提示音的发言阶段先以 `host_announcement_pending` 锁定目标阶段；提示音结束后才开始正式发言计时。提示音准备、Agent 生成和 TTS 合成不会消耗真人发言时长。
3. AI 生成/语音准备期间写入 `ai_preparing` 与冻结剩余秒数，`stage_deadline_at` 置空；首帧播放后重新建立权威播放截止时间。
4. 正数小于 1 秒的剩余时间显示为 `1`，只有真正到达截止时间才显示 `0` 并推进阶段，避免提前一秒跳段。
5. 真人断线达到 60 秒后由引擎写入 `participant.disconnect_timeout` 和 `match.paused`，保留真人身份；重新连接不会自动恢复，必须由房主/管理员显式继续。

### 本轮发现与修复

异常暂停后执行 `control/retry` 时，旧实现使用 `room.paused_remaining_seconds or 30` 设置新截止时间。若阶段在暂停前已经到达 `00:00`，显式的 `0` 会被当成假值，重试后凭空恢复 30 秒，造成用户看到倒计时倒退和“开局/阶段莫名等待”。现已改为保留精确冻结值：`0` 仍然立即到期，只有缺失值（旧数据）才使用 1 秒的安全默认值。

### 自动化证据

- `tests/test_full_match_api_simulation.py`、`tests/test_round24_start_readiness.py`、`tests/test_round33_control_boundaries.py`、`tests/test_round43_multi_match_flow_audit.py`、`tests/test_round45_no_ai_takeover_policy.py`：`26 passed`。
- 新增 `test_retry_preserves_expired_countdown_instead_of_restarting_stage`：验证异常重试不会将显式 `0` 秒重置为 30 秒，并在下一次引擎 tick 进入下一阶段。
- 目标测试单独执行：`1 passed`。
- 扩展回归 `round8/9/15/16/18/20/24/33/43/44/45`（断线、暂停恢复、自由辩论、并发多房间和完整流程）：`59 passed`。
- Ruff：`app/api/rooms.py`、`app/services/match_engine.py`、`tests/test_round33_control_boundaries.py` 全部通过。

### 部署环境验证

- 生产临时房间 `745456` 使用真实 PostgreSQL 复测 61 秒断线：房间状态变为 `paused`，依次写入 `participant.disconnect_timeout`、`match.paused`；对应幂等键长度为 87 和 100，均小于 120；没有 `engine.quarantined` 和 `seat.ai_substituted`。
- 真实 1v1 人机比赛 `420528` 完成开场、双方立论、自由辩论、双方总结和裁判，共 7 次发言、71 个事件；期间暂停/继续和断线重连成功，最终状态为 `completed`。
- 真实 4v4 混合比赛 `707326` 完成全部 8 个固定发言阶段、自由辩论和裁判，共 12 次发言、117 个事件；期间暂停/继续和断线重连成功，最终状态为 `completed`。
- Round 51 发布后日志未再出现 `StringDataRightTruncation`、`engine.quarantined` 或 `provider.failed`。
