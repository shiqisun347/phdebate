# 多真人 / 多 Agent 辩论流程故障注入审计

日期：2026-07-22  
范围：API 权威状态机、房主恢复路径、跨房隔离；本轮未修改产品源码。

## 结论

当前自动比赛主链路在隔离测试中可以覆盖 1v1、4v4、多真人、多 Agent、自由辩论、暂停恢复、Agent/TTS 故障、断线接替、裁判和结果落库。新增 4 个此前缺少的真实边界测试全部通过，并与既有 41 个相关流程测试一起回归通过。

仍发现 1 个明确的产品文案缺口：未轮到某席位时，API 当前可能返回 `当前轮到 反方2辩`。它不影响权限安全，但空格、阿拉伯数字和页面使用的“反方二辩”不一致，属于应修复的用户体验问题。

## 本轮新增覆盖

新增文件：`apps/api/tests/test_round44_fault_injection_match_flow.py`

1. **开局无莫名等待**
   - 使用真实的系统预生成主持提示音文件和 `AudioCue` 记录。
   - 一次引擎处理即从 `preparing` 进入 `running`。
   - 明确禁止再次调用 LightTTS 或 MOSS。
   - 倒计时使用音频真实时长加传输保护，不错误回退到模板 8 秒。
   - 事件序号连续，存在 `audio.cue.ready(source=preset)`。

2. **4v4 当前真人发言权限**
   - 四名真人占据 `aff_1`、`aff_2`、`neg_1`、`neg_2`。
   - 固定环节指定 `neg_2` 时，只有该用户获得 `can_speak=true`。
   - 其他席位直接调用 `/speech/start` 仍被服务端 403 拒绝。
   - 当前席位完成设备租约后可以正常开始发言。

3. **全真人自由辩论无人举手**
   - 1v1 人人房间没有任何 AI 席位。
   - 三秒申请窗口无人申请后，不尝试调用不存在的 AI。
   - 该方获得有界等待，时间耗尽后进入另一方申请窗口，状态机不会永久卡住。

4. **断线恰好 60 秒**
   - 房主在边界时刻由 `human` 转为 `ai_substitute`。
   - 在线真人继承房主修复权限。
   - 只替换断线席位，并记录 `seat.ai_substituted`。

## 既有覆盖复核

| 场景 | 权威测试 |
| --- | --- |
| 1v1 人机、1v1 人人、4v4 多真人/多 Agent 并行 | `test_round18_engine_scenarios.py`、`test_round43_multi_match_flow_audit.py` |
| 五房间长时逻辑时钟与跨房隔离 | `test_round15_engine_soak.py` |
| 多人同时举手、顺序选择、非选中者无权限 | `test_round20_free_debate_queue.py` |
| 无人举手后 AI 接替、重启恢复 | `test_round20_free_debate_queue.py` |
| 倒计时最后一秒、截止边界、迟到发言拒绝 | `test_round33_control_boundaries.py` |
| 断线 59/61 秒、AI 接替、真人恢复 | `test_round18_engine_scenarios.py`、`test_round13_engine_scenarios.py` |
| Agent 失败后房主重试、幂等、跨房不扩散 | `test_round6_engine_recovery.py`、`test_round43_multi_match_flow_audit.py` |
| TTS 失败后安全暂停、重试当前步骤 | `test_platform.py::test_ai_tts_failure_pauses_current_stage_and_retry_restarts_it` |
| 主持提示音失败后重试回到 preparing | `test_platform.py::test_cue_generation_failure_pauses_and_retry_returns_to_preparing` |
| 暂停、恢复、跳过、终止边界 | `test_round8_engine_scenarios.py`、`test_round43_multi_match_flow_audit.py` |
| 裁判成功、失败、人工复核、结果完成 | `test_round6_engine_recovery.py`、`test_round12_lifecycle.py`、`test_round16_engine_resilience.py` |

## 执行结果

```text
.venv/bin/ruff check apps/api/tests/test_round44_fault_injection_match_flow.py
All checks passed!

新增边界测试：
4 passed, 1 warning in 3.23s

多房间、自由辩论、计时、暂停恢复与故障注入组合回归：
32 passed, 1 warning in 8.39s

完整生命周期、裁判与恢复补充回归：
9 passed, 1 warning in 2.73s

主持提示音 / AI TTS 指定故障测试：
3 passed, 196 deselected, 1 warning in 0.68s
```

唯一警告来自 Starlette 对旧 `httpx` TestClient 兼容层的弃用提示，不是比赛流程失败。

## 待修复与限制

### P1：席位提示文案不统一

- 当前：`当前轮到 反方2辩`
- 期望：`当前轮到反方二辩`
- 影响：权限正确，但学生会看到不自然且与舞台标签不一致的提示。
- 建议：让 `speaking_permission` 复用页面统一席位标签格式，并增加精确文案断言。

### 测试边界

- 本轮验证的是 API 权威状态、数据库事件和恢复控制，不等价于真实浏览器麦克风、ASR 声学质量或 WebRTC 音轨听感验收。
- MOSS、LiveKit、浏览器播放的真实链路需要生产浏览器和音频指标单独验收；这些外部链路异常不会被本轮 mock provider 测试证明为正常。
- 本轮未制造生产数据，也未操作现有生产房间。
