# Round15 生产浏览器与产品测试报告

- 日期：2026-07-20
- 目标：`https://117.50.192.216`
- 工具：`agent-browser`
- 保护约束：不开始比赛，不触发 Agent/TTS/ASR，不修改 DebateStage、LiveKit、AudioWorklet、PCM 或任何音频文件。
- 状态：完成；4 个产品/恢复问题已在本地源码修复，待主线全量门禁、目录收敛、部署与生产复验

## 覆盖目标

- [x] 匿名赛事发现、规则、排行榜和观战入口
- [x] 注册/登录以及参赛意图恢复
- [x] 创建房间、房间号加入、席位、准备和房主返回控制
- [x] 个人中心活动房间和只读返回路径
- [x] 断网、请求失败和恢复反馈
- [x] 390×844 移动端、键盘与无障碍
- [x] 视觉层级、文案、产品一致性与字节系产品化完成度
- [x] 用户可见 平台、`/v2` 兼容入口与双版本痕迹
- [x] QA 房间已关闭，两个浏览器会话已退出
- [ ] 服务端 QA 账号标记/停用与残留 presence/session/control lease 清理（主线统一执行）

## 问题明细

### ISSUE-001（Low）注册密码不一致只在提交后由服务端报错

- 复现：在 `/register` 输入不同的“密码”和“确认密码”，提交表单。
- 实际：按钮在明知两次输入不一致时仍可提交，发出一次 `422` 请求后才显示“提交内容：两次输入的密码不一致。”。
- 影响：核心注册路径多一次不必要网络往返；弱网下用户要等待后才知道很容易在本地发现的错误，且“提交内容”前缀偏技术化。
- 证据：[`register-password-mismatch.png`](evidence/screenshots/register-password-mismatch.png)，网络记录为 `POST /api/auth/register 422`。
- 修复：确认密码输入后立即比对，不一致时在输入框下显示自然语言错误，设置 `aria-invalid`/`aria-describedby` 并禁用提交；表单提交时仍再次检查，兼容 Safari 自动填充未触发 React change 的情况。
- 验证：`auth-form.test.tsx` 5/5 通过，TypeScript 通过。待主线部署后生产复验。

### ISSUE-002（Low）返回大厅时本人席位短暂显示“离线”

- 复现：当前辩手从个人中心返回房间大厅，或刷新 lobby；在房间内容首次显示时立即观察本人席位。
- 实际：页头已显示“实时连接”且房主操作按钮可用，但本人席位仍短暂显示“已准备 · 离线”；约 0.5 秒后被 WebSocket presence 更新为“在线”。
- 影响：学生会怀疑自己是否已正常进入房间，也会让“实时连接”和席位状态在同一画面相互矛盾。
- 证据：[`lobby-own-seat-offline-flicker.png`](evidence/screenshots/lobby-own-seat-offline-flicker.png)，注释截图中本人正方 3 辩为“离线”，同一轮无障碍快照已更新为“在线”。
- 修复：lobby 在统计在线真人和渲染席位状态时，如实时通道已连接，将本人席位视为在线，消除 REST 首帧与 WebSocket presence 之间的假离线闪烁；通道真正断开时仍显示离线。
- 验证：lobby 与认证聚焦测试共 25/25 通过，TypeScript 通过。待部署后生产复验。

### ISSUE-003（Medium）只读页面加载失败被误报为“操作可能尚未提交”

- 复现：在隔离浏览器中中断 `GET /api/competitions`，打开首页。
- 实际：错误页显示“网络连接失败，本次操作可能尚未提交。请检查网络后重试。”首页加载是纯读操作，根本不存在“提交结果不确定”。
- 影响：弱网学生会误以为自己创建了房间或提交了其他写操作，而实际只是首页未加载；错误文案未区分读写语义。
- 证据：[`home-api-failure.png`](evidence/screenshots/home-api-failure.png)。
- 修复：API 客户端现在区分读写语义；`GET/HEAD/OPTIONS` 网络失败提示“暂时无法加载内容”，仅非幂等写操作保留“可能尚未提交”以供调用方进行幂等恢复。
- 验证：API 客户端、认证与 lobby 聚焦测试共 35/35 通过，TypeScript 通过。待部署后生产复验。

### ISSUE-004（Low）旧链接和错误 URL 落入英文默认 404

- 复现：访问旧系统常见链接 `/screen`、`/console` 或其他不存在的站内 URL。
- 实际：页头和页脚仍为中文稽下辩论，主体却只显示 Next.js 默认英文“This page could not be found.”，没有返回赛事大厅或查找房间的恢复路径。
- 影响：旧二维码、收藏夹链接或手输错误地址的学生会遇到与产品语言和视觉系统脱节的死路。
- 证据：[`legacy-screen-english-404.png`](evidence/screenshots/legacy-screen-english-404.png)。
- 版本痕迹检查：`/v2`、`/v2/` 都干净跳转唯一根路径，页面上没有 平台、旧系统切换器或双版本文案；问题仅是废弃链接的 404 恢复体验。
- 修复：新增统一中文 404，解释链接已过期/输入错误，强调所有赛事、房间和观战都从唯一赛事大厅进入，并提供“返回赛事大厅 / 查看排行榜”恢复操作；页面不显示 平台、旧版、新版等双轨概念。
- 验证：404、API 客户端、认证与 lobby 聚焦测试共 36/36 通过，TypeScript 通过。待部署后生产复验。

## 核心结果

- 参赛意图：匿名用户从 4v4 赛事详情点击“立即参赛”，经登录页切换到注册后，`next=/?participate=daily-4v4` 全程保留；注册完成后自动返回首页并重新打开原 4v4 参赛弹窗。
- 赛事发现：4v4 详情的“赛事介绍 / 排行榜 / 观战列表 / 规则说明”四个 ARIA tab 支持左右方向键切换，焦点、`aria-selected` 和 tabpanel 同步正确。
- 房间：测试房间 `977247`，辩题“信息爆炸时代，深度思考是否正在变得更稀缺？”；房主正方 3 辩、参赛者反方 2 辩，双方认领、准备和房主启动前控件正确，未点击确认开始。
- 单活动房间：已在 `977247` 参赛时尝试新建 1v1，服务端返回 `409`，弹窗显示“你已在房间 #977247 参赛”和“返回当前比赛”，无重复建房或丢失当前席位。
- 个人中心：活动房间卡片包含房间号、题目、“等待房主锁定席位并开始比赛”和返回大厅动作；房主可从 `/me` 恢复原房间。
- 错误恢复：仅在隔离浏览器中中断首页赛事 GET，页面出现清晰错误状态和“重新尝试”；恢复请求后点击重试，首页原位恢复，无刷新死循环。
- 移动端：390×844 下赛事详情、四 tab、辩题列表、8 席位大厅、房间信息与房主操作均没有横向溢出；“锁定席位并开始”与“关闭房间”视觉层级清晰。
- 版本痕迹：`/v2`、`/v2/` 均跳转 `https://117.50.192.216/`；`/v2/admin` 通过当前管理权限链路处理；公开页、认证页、赛事页、房间和个人中心均没有用户可见的 平台、旧版/新版或双版本切换。

## 视觉与产品评估

- 整体风格已形成稳定的深色竞技产品语言：高信息密度卡片、紧凑标签、有限高饱和强调色和清晰主操作，与字节系工具/竞技产品的紧凑、理性、即时反馈方向基本一致。
- 赛事详情首屏层级正确：赛事名 > 简介 > 席位/排名/赛季/观战摘要 > 立即参赛 > 内容 tab。
- lobby 将“辩题、准备/在线/AI 补位摘要、双方席位、房间规则、恢复操作”按真实开赛决策顺序排列，移动端也保持同一信息优先级。

## 证据索引

- [`competition-detail-desktop.png`](evidence/screenshots/competition-detail-desktop.png)
- [`register-password-mismatch.png`](evidence/screenshots/register-password-mismatch.png)
- [`owner-ready-lobby.png`](evidence/screenshots/owner-ready-lobby.png)
- [`me-active-room.png`](evidence/screenshots/me-active-room.png)
- [`lobby-own-seat-offline-flicker.png`](evidence/screenshots/lobby-own-seat-offline-flicker.png)
- [`home-api-failure.png`](evidence/screenshots/home-api-failure.png)
- [`legacy-screen-english-404.png`](evidence/screenshots/legacy-screen-english-404.png)
- [`mobile-lobby-390x844.png`](evidence/screenshots/mobile-lobby-390x844.png)
- [`mobile-competition-390x844.png`](evidence/screenshots/mobile-competition-390x844.png)

## 验证门禁

- Web 聚焦测试：4 个文件，36/36 通过。
- TypeScript：通过。
- 本轮新增覆盖：已登录认证页保护、密码确认本地校验、GET/写请求网络错误语义、本人席位在线首帧、统一中文 404。

## 未修风险

- 为保护冻结音频基线，本轮不开始比赛，未黑盒触发 Agent、TTS、ASR、麦克风录制或比赛内发言权。
- 生产当时没有可供匿名验证的正式已完成公开房间；`367133` 已被数据清理策略收紧为非公开测试数据，因此本轮不将它用作赛果公开性证据。
- 弱网使用浏览器内请求中断模拟，验证了失败与重试，没有稳定注入固定 1–3 秒响应延迟；真实高 RTT 可留给后续专项性能窗口。
- 服务端 QA 清理清单：账号 `qa_r15_owner_20260720`、`qa_r15_joiner_20260720`；房间 `977247` 已通过 UI 取消。需标记两账号 `is_test_account=true`、`is_active=false`，删除残留 sessions/presence/control lease，确认房间为测试数据。
