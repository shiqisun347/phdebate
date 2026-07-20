# Round 9 集成报告

日期：2026-07-20

## 本轮结果

- 生产黑盒完成三名参赛者、两个并行房间和匿名移动观战，未启动 Agent/TTS。
- 房主离线、主动退出或手动移交时，异常恢复权现在可以安全交给在线真人。
- 暂停和异常暂停房间不再绕过 60 秒断线 AI 接替规则。
- 真人席位恢复必须证明浏览器已经重新连接。
- 管理员不能开放缺少题库、自动流程或有效赛季的赛事。
- 训练赛可明确选择题库辩题或自定义辩题，请求不再同时携带两个来源。
- 开赛确认框、控制台状态、标题层级、请求竞态和错误反馈得到修复。

## 验证门禁

```text
API full regression: 393 passed, 2 warnings
Web unit/component: 35 files, 242 passed
Focused room UI: 48 passed
Next production build: passed
ESLint quiet: passed
Knip: clean
Deploy tests: 30 passed
Ruff: passed
Reliable audio baseline: 82 files verified
Reliable audio fingerprint: 3219138b22878e766c94b9a1fa0e422211a6f27dd74afbbb93a8e45f7e295285
```

生产公开只读接口以 20 个 keep-alive 并发、每个接口 100 次请求进行观察：平均约 249–374ms，
p95 约 487–812ms，全部返回 200。该结果包含公网网络延迟，不代表服务端纯处理耗时；本轮未
发现需要用高风险缓存或扩大改动面的性能瓶颈。

## 音频保护

没有修改 DebateStage、LiveKit、AudioWorklet、PCM 播放队列、MOSS gateway、音色、Prompt
音频或 TTS 参数。控制台新增的房主退出入口复用既有 REST API，不进入可靠播放文件。

## 发布状态

已部署到生产 release `round9-human-recovery-20260720`：API primary/secondary、Web、Engine 和
Worker 完成切换，数据库仍为 `0025_speech_result_pagination`，总 readiness 全绿且无活动比赛。

部署后使用两个新的隔离浏览器会话验证：

1. 训练赛创建窗口同时显示三个题库辩题和可选自定义辩题；选择第二个题库题目后房间主题正确。
2. 第二名真人加入后，房主控制台只列出在线真人接任者，并展示“让 AI 接替我的席位”。
3. 房主确认移交后，旧房主自动转入观战，新房主可以进入控制台；数据库只记录一次
   `room.owner_transferred`。

复测房间 `263748` 已取消并标记为 QA 数据；两个复测账号已禁用、标记为测试账号并撤销会话。

Round 9 恢复材料已重新生成：V2 数据库临时恢复 24 张表，Debate Agent 临时恢复 13 张表；
数据卷继续使用已隔离解包并逐文件校验的 380 文件归档。完整文件名与 SHA-256 见
`docs/deploy/full-server-restore.md` 和服务器最新 `recovery-set-*.manifest`。
