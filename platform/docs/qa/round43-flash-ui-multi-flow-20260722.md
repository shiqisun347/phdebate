# Round 43：FlashAttention 2 灰度、浅色 UI 与多比赛回归（2026-07-22）

## 结论

- OpenMOSS PR #52 的 FlashAttention 2 正确性修复已在生产 RTX 3090 上验证，但真实 active RTF 为 `2.72–2.77`，不满足实时门，生产已恢复 SDPA。
- SDPA 回归首个非静音 PCM 为 `521–638 ms`，active RTF 为 `0.887–0.915`，1200 ms 连续播放模拟无 underrun。
- MOSS readiness 当前为 `ready`，`active=0`、`pending=0`、`orphan=0`，attention 为 `sdpa`，固定局部 CUDA Graph 生效。
- 首页、登录、注册、赛事列表、房间大厅和个人中心的非舞台页面统一为浅色产品界面；生产 Web release 为 `20260722T083949Z-root`。
- 隔离的 2×1v1 + 2×4v4 多比赛测试完成，覆盖真人/AI 发言、暂停恢复、Agent 重试、裁判跳过、终止、结果和跨房隔离，联合回归 31 passed。

## 浏览器证据

- `round43-production-browser/home-desktop.png`
- `round43-production-browser/home-mobile.png`
- `round43-production-browser/login-mobile.png`
- `round43-production-browser/login-mobile-nav.png`

桌面 1440×900 与移动 390×844 均未发现导航遮挡、主按钮不可达、横向溢出或深浅主题混杂。移动首页保留比赛预览、双主操作与赛事卡，登录表单字段和注册入口完整可达。

## 生产状态

- API primary/secondary、Engine、Worker、Web、FunASR 和 MOSS 均为 RUNNING。
- API release：`round42-flow-asr-cues-20260722`。
- Web release：`20260722T083949Z-root`。
- FlashAttention 2 依赖 `2.8.3` 保留在 MOSS 独立虚拟环境；正式启动脚本默认 SDPA，可在隔离实验中显式覆盖。
- 两组临时基准 WAV 已删除，只保留文字指标和 QA 记录。

## 仍需继续

- 对一场新的 1v1 灰度房间执行完整 Agent → MOSS → LiveKit → 浏览器比赛流程，并人工听测音色、撕裂和语义完整性。
- 修复两个旧暂停房间长期占用 5 场容量的问题，并增加显式、可审计的 stale paused 处理。
- 修复历史房间 `722633` 的 Room/Match 状态不一致，补充管理员一致性扫描。
