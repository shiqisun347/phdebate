# Round15 引擎并发与长时仿真

日期：2026-07-20  
范围：比赛引擎、房间生命周期、API 回归测试  
明确排除：TTS、ASR、LiveKit、AudioWorklet、PCM、DebateStage 和浏览器音频播放

## 结论

本轮完成 20 个房间的确定性逻辑时间 soak：10 场 1v1、10 场 4v4，共推进至少 100 段发言和 20 次裁判流程。场景同时包含真人开篇、AI 自动补位、暂停/恢复、暂停期间离线 60 秒后的 AI 接替、引擎重启恢复、裁判任务重试与乱序返回，以及并发房间事件隔离。

发现并修复一个真实的可恢复性缺陷：暂停房间此前不属于引擎轮询集合。Redis 虽能记录真人离线，但后续的超时 AI 接替和房主控制权转移不会自动执行，房间可能在暂停状态长期失去可操作人。现在 `paused` 房间继续接受低成本生命周期处理，但不会推进比赛阶段，也不会进入活动语音发布集合。

同时修复一个测试隔离缺陷：Round14 四房测试曾扫描整个测试数据库，并以题目片段判定串房；后续测试只要使用相似题目就会产生顺序依赖误报。现在仅比较该测试创建的四个房间及精确题目。

## 仿真覆盖

- 20 个独立六位房间号，1v1 与 4v4 各 10 场。
- 4v4 从空席自动补齐 AI，逐席推进 8 个发言阶段；1v1 推进 2 个发言阶段。
- 真人发言通过正式控制租约、开始和结束接口完成。
- 4 个房间在暂停期间让房主离线超过 60 秒，验证 AI 接替、控制权转移、阶段和暂停计时保持不变。
- 4 个房间人工暂停后恢复，验证继续从原阶段推进。
- 4 个房间模拟进程在 Agent 文本落盘、音频发布前重启，验证旧任务中断、当前阶段内容单次复用、其他房间不受影响。
- 4 个房间同时启动裁判任务 A，暂停后启动任务 B，再先释放 A；验证 A 不得覆盖 B，最终只接受 B。
- 14 轮并发逻辑 tick，包含超过正常完成所需的空转轮次，验证完成状态幂等稳定。
- 每房间验证事件序号连续、事件 `room_id` 唯一、无残留 `speaking/synthesizing/playing`、裁判结果和发言文本不串房。

## 验证证据

```text
ruff: All checks passed
focused engine/API regression: 39 passed, 1 warning in 14.30s
```

聚焦回归集合：

- `test_round15_engine_soak.py`
- `test_round14_engine_scenarios.py`
- `test_round13_engine_scenarios.py`
- `test_round12_lifecycle.py`
- `test_multi_room_simulation.py`

## 修改文件

- `apps/api/app/services/match_engine.py`
- `apps/api/tests/test_round15_engine_soak.py`
- `apps/api/tests/test_round14_engine_scenarios.py`
- `docs/qa/round15-engine-soak-20260720.md`

## 风险与后续

- 本测试采用确定性逻辑时间推进，不替代生产环境数小时墙钟 soak；生产 soak 应使用专用 QA 账号和房间，避免污染排行榜。
- 测试使用缺失 WAV 走无音频完成分支，目的是冻结并避开可靠语音基线；TTS 的时延、音质和浏览器播放由独立冻结基线与语音专项门禁负责。
- 当前源码仍位于历史目录名 `v2/`；本轮没有增加任何用户可见的 平台 文案，也没有执行目录重命名。唯一版本目录和服务命名扁平化由主线统一处理。
