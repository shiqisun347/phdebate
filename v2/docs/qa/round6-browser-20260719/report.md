# Round 6 真实用户浏览器测试报告

| 项目 | 内容 |
|---|---|
| 日期 | 2026-07-19 |
| 生产地址 | https://117.50.192.216 |
| 工具 | agent-browser 0.32.1 / Chrome 150 |
| 视口 | 桌面 1440×900；手机 390×844 |
| 范围 | 注册登录、赛事详情、建房/搜索/抢座/准备、双设备控制权、刷新恢复、权限、断网反馈、关闭清理 |
| 音频约束 | 全程未主动点击音频或麦克风控件；未执行长流程比赛 |

## 执行摘要

- Round 5 的两项重点回归均已修复并连续验证两次：同账号双设备接管会让旧设备实时只读；断网写操作使用中文可执行提示，不再暴露 `Failed to fetch`。
- 注册、登录、1v1 创建/搜索/认领/准备、4v4 多真人占席与 AI 补位预览、刷新恢复、匿名/非房主权限和手机响应式布局均符合预期。
- 稳定发现 1 项中等级别问题：开赛没有二次确认，房主单击后会立即进入不可逆的自动比赛流程。
- QA 房间 `514893`、`836622` 已关闭；`245719` 已通过应急控制终止，不再出现在公开比赛列表。
- QA 账号为 `QA-R6甲`、`QA-R6乙`，名称明确标识为 QA。产品没有自助删除账号入口，未使用管理后台删除账号，避免扩大权限范围。

## 结果汇总

| 严重级别 | 数量 |
|---|---:|
| Critical | 0 |
| High | 0 |
| Medium | 1 |
| Low | 0 |
| **合计** | **1** |

## Round 5 回归检查

### 1. 同账号双设备控制权：通过

第一台设备持有房间控制权时，第二台设备只显示“确认接管到当前设备”，准备、开赛和关闭操作均为禁用状态。第二台确认接管后，第一台无需刷新即自动变为只读。反向接管再次验证，行为一致。

- 新设备接管前：[device2-lobby-before-takeover.png](screenshots/device2-lobby-before-takeover.png)
- 第一次接管后新设备可控：[device-control-step2-new-active.png](screenshots/device-control-step2-new-active.png)
- 第一次接管后旧设备只读：[device-control-step3-old-readonly.png](screenshots/device-control-step3-old-readonly.png)
- 第二次反向接管旧设备只读：[device-control-second-pass-device2-readonly.png](screenshots/device-control-second-pass-device2-readonly.png)
- 过程视频：[device-control-takeover-pass.webm](videos/device-control-takeover-pass.webm)

### 2. 断网写操作反馈：通过

在房间准备操作前开启浏览器离线模式，连续两次提交均显示：

> 网络连接失败，本次操作可能尚未提交。请检查网络后重试。

页面同时显示“网络连接已断开，恢复联网后将自动重连”和“立即重连”，没有出现 `Failed to fetch`。恢复网络后可以立即重连，席位和准备状态保持一致。

- 第一次断网结果：[offline-result1.png](screenshots/offline-result1.png)
- 第二次断网结果：[offline-result2.png](screenshots/offline-result2.png)
- 恢复联网：[offline-recovered.png](screenshots/offline-recovered.png)
- 过程视频：[offline-error-localized-pass.webm](videos/offline-error-localized-pass.webm)

## 场景结果

| 场景 | 结果 | 说明 |
|---|---|---|
| 注册与自动登录 | 通过 | 两个独立账号注册后均进入已登录赛事大厅。 |
| 赛事详情 | 通过 | 赛事介绍、排行榜、观战列表、规则说明四个 Tab 可切换，ARIA 选中状态正确。 |
| 1v1 创建房间 | 通过 | 自定义辩题、选边、六位房间号、邀请入口均正常。 |
| 无效房间搜索 | 通过 | `000000` 明确显示“房间不存在”。 |
| 有效房间搜索 | 通过 | 第二名学生通过 `514893` 进入并认领反方席位。 |
| 抢座与准备同步 | 通过 | 认领后双方状态通过 WebSocket 同步，房主仅在所有真人准备后可开赛。 |
| 刷新恢复 | 通过 | 房主刷新后账号、席位、准备状态和控制权完整恢复。 |
| 4v4 多真人 + AI 补位预览 | 通过 | 两名真人分别占正方2辩和反方3辩，页面正确显示 6 个空席将由 AI 补齐。 |
| 匿名权限 | 通过 | 匿名用户无抢座、准备和房间控制能力；访问控制台会被重定向到只读观战页。 |
| 非房主权限 | 通过 | 已登录非房主访问控制台同样重定向到观战页。 |
| 关闭确认与关闭后返回 | 通过 | 关闭未开始房间有确认提示；关闭后旧链接返回大厅并显示房间已关闭。 |
| 手机端 390×844 | 通过 | 首页、赛事详情和房间大厅无横向溢出，`scrollWidth = innerWidth = 390`，导航菜单可开合。 |
| 页面加载 | 通过 | 生产实测主页 DCL 约 91–191 ms、load 约 100–329 ms；未发现超过 3 秒的首屏。 |

关键截图：

- 桌面赛事详情：[competition-1v1-desktop.png](screenshots/competition-1v1-desktop.png)
- 1v1 双方准备完成：[both-ready.png](screenshots/both-ready.png)
- 4v4 两真人、六 AI 补位边界：[4v4-two-humans-ready-six-ai.png](screenshots/4v4-two-humans-ready-six-ai.png)
- 手机首页：[mobile-home-390x844.png](screenshots/mobile-home-390x844.png)
- 手机房间大厅：[mobile-lobby-390x844.png](screenshots/mobile-lobby-390x844.png)
- 匿名控制台权限：[anon-control-boundary.png](screenshots/anon-control-boundary.png)

## 问题

### ISSUE-001：开赛操作没有二次确认，单击即进入自动比赛

| 字段 | 内容 |
|---|---|
| 严重级别 | Medium |
| 分类 | UX / Functional |
| 页面 | `/rooms/:code/lobby` |
| 复现次数 | 2 |
| 视频 | [issue-001-start-no-confirm-repro2.webm](videos/issue-001-start-no-confirm-repro2.webm) |

**问题描述**

房主点击“锁定席位并开始”后，没有出现二次确认或开赛摘要，第一次验证时页面立即进入 `/debate`，自动流程开始。此时 4v4 房间只有两名真人，其余六席由 AI 补齐；误触会立即创建正式比赛、锁定席位并可能产生 Agent/TTS 成本和无效比赛记录。

关闭未开始房间和终止比赛均已有原生确认框，因此开赛缺少确认与同产品的高风险操作模式不一致。

**双次复现**

1. 房间 `245719`：两名真人准备后单击“锁定席位并开始”，无确认直接进入辩论页。发现后立即进入控制台终止，未继续长流程。
2. 房间 `836622`：一名真人准备、七个 AI 补位；点击前开启离线模式。单击后 `dialog status` 明确返回“无对话框”，请求直接发出并显示网络失败。离线拦截证明客户端没有确认，同时避免第二次真正开赛。

**证据**

- 第二次复现点击前：[issue-001-repro2-step1.png](screenshots/issue-001-repro2-step1.png)
- 第二次复现点击后无确认、请求已发出：[issue-001-repro2-result.png](screenshots/issue-001-repro2-result.png)
- 第一次开赛后已终止：[4v4-test-terminated.png](screenshots/4v4-test-terminated.png)

**建议**

开赛前使用产品内模态框展示辩题、真人名单、AI 补位数量、计分属性和“开始后席位锁定”的后果；要求房主再次点击“确认并开始”。提交期间按钮进入不可重复点击状态。正式赛还可要求勾选“我已确认真人席位与辩题”。

## 未计入问题的观察

- 终止 `245719` 时 LiveKit 曾在控制台输出一次重连期间收到 leave request 的错误；页面没有崩溃，比赛成功终止，未进行第二次有风险复现，因此不计入问题数。
- 手机顶部账号名称在 390 px 宽度下被省略为 `QA-R…`，但账号入口、退出按钮和导航均可操作，属于合理的窄屏压缩。
- `agent-browser network route` 对本次 HTTPS Fetch 未产生拦截效果，断网回归最终使用浏览器离线模式完成；这属于测试工具行为，不是产品问题。

## 测试数据与清理

| 数据 | 最终状态 |
|---|---|
| 账号 `qa_r6_a_0719` / `QA-R6甲` | QA 标识账号；已退出并关闭浏览器会话 |
| 账号 `qa_r6_b_0719` / `QA-R6乙` | QA 标识账号；已退出并关闭浏览器会话 |
| 房间 `514893` | 已关闭；旧链接返回大厅并提示已关闭 |
| 房间 `245719` | 已终止；控制台应急操作禁用，不在公开比赛列表 |
| 房间 `836622` | 已关闭；离线开赛请求未到达服务器 |

二进制截图和视频仅位于本地 QA 证据目录，并由 Git 忽略；本报告 Markdown 保留进入代码备份分支。
