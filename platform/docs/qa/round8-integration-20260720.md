# Round 8 集成报告

日期：2026-07-20

## 交付内容

- 修复真人恢复旧发言时可绕过比赛暂停和当前阶段的服务端权限漏洞。
- 增加三房并发 Agent 超时隔离、幂等重试、结果单次结算、真人断线 AI 接替及恢复审批测试。
- 统一首页、赛事卡片、赛事详情计数和观战列表的长期暂停房间下架策略；详情计数不再受
  20 条展示上限截断。
- 修复 SPA 进入赛事详情保留旧滚动位置、390px 标签逐字换行、匿名结果页 CTA、复制反馈、
  主标题层级、当前导航状态和 skip link。
- 增加默认 dry-run 的 release 修剪工具，保护当前 API/Web、最近回滚版本、24 小时内版本和
  未确认完整性的人工目录。
- 增加数据卷备份隔离解包与逐文件 SHA-256 验证工具；服务器 Round 7 数据卷 380 个文件通过。

## 集成门禁

```text
API full regression: 384 passed, 2 warnings
Web unit/component: 35 files, 238 passed
Next production build: passed
ESLint quiet: passed
Knip: clean
Deploy tests: 29 passed
Ruff: passed
Alembic: 0025_speech_result_pagination (single head)
Playwright discovery: 14 tests parsed
Reliable audio baseline: 82 files verified
Reliable audio fingerprint: 3219138b22878e766c94b9a1fa0e422211a6f27dd74afbbb93a8e45f7e295285
```

## 生产浏览器结果

部署前的生产 dogfood 覆盖匿名、登录、个人中心、创建房间、两用户抢座/准备/取消/释放、关闭
房间、观战、赛果、桌面和 390px 手机。确认的三个公开体验问题均已在本地代码修复并增加回归：

1. 跨路由滚动位置错误继承；
2. 手机赛事标签逐字换行；
3. 赛事详情继续推广长期暂停房间，与首页状态不一致。

生产 QA 房间 `968296` 已关闭并标记为测试数据。两个 QA 账号已禁用、标记为测试账号并撤销
全部登录会话，历史记录保留用于审计。

## 发布状态

Round 8 已部署到生产 release `round8-recovery-product-20260720`。API 双实例、Web、Engine、
Worker、FunASR 和 MOSS readiness 全绿，当前无活动比赛处理。部署后 agent-browser 已复核桌面
滚动恢复、390px 标签布局、匿名结果页 CTA 和公开房间新鲜度一致性。完整证据见
`docs/qa/round8-postdeploy-20260720/report.md`。

本轮生产服务器在发布前发生整机重启，暴露 Nginx 非版本化配置缺失和 MOSS 固定旧 GPU UUID
两个恢复缺口；两项均已改为版本化、开机自动恢复配置，详情见
`docs/incidents/2026-07-20-production-handshake-outage.md`。

## 保护边界

本轮没有修改 DebateStage、LiveKit、AudioWorklet、PCM 播放队列、MOSS gateway、Agent/TTS
Provider、`voice_runtime`、Prompt 音频或可靠语音配置。所有音频保护文件保持既有指纹。
