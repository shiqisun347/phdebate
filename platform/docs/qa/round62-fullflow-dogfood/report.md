# Dogfood Report: 稷下辩论平台完整比赛流程

| Field | Value |
|-------|-------|
| **Date** | 2026-07-24 |
| **App URL** | http://127.0.0.1:13220 |
| **Session** | round62-fullflow-owner |
| **Scope** | 注册、赛事详情、创建房间、房主/辩手大厅、比赛控制、比赛现场、移动端和异常恢复 |

## Summary

| Severity | Count |
|----------|-------|
| Critical | 0 |
| High | 1 |
| Medium | 1 |
| Low | 0 |
| **Total** | **2** |

两项问题均已在本轮修复并完成浏览器复验。

## 已通过的真实操作场景

- 两个独立登录会话分别以房主和参赛者身份进入 `1v1` 房间 `#868928`。
- 参赛者通过六位房间号搜索、认领反方席位并确认准备。
- 房主和参赛者准备状态实时同步；普通参赛者仅显示“取消准备”，不出现房主设置。
- 参赛者关闭浏览器后，房主立即看到该真人离线，“开始比赛”变为“等待 1 位真人上线”且不可点击。
- 参赛者重新登录后返回原房间，并可明确确认接管原席位；系统没有生成 AI 替补。
- 两人均在线后房主可确认开赛，双方自动进入同一比赛舞台。
- 房主比赛控制弹窗可进入完整控制台，参赛者没有越权控制入口。
- 匿名观战页不展示文字稿或文字记录入口。
- 在 390×844 视口复验参赛弹窗与观战舞台；返回当前比赛入口和底部观战控制均可见、无遮挡。

证据：

- [双人均已准备](screenshots/multi-user-ready.png)
- [匿名观战无文字稿](screenshots/anonymous-watch-no-transcript.png)
- [移动端参赛弹窗](screenshots/issue-002-fixed-mobile-final.png)
- [移动端观战舞台](screenshots/anonymous-watch-mobile.png)

## Issues

### ISSUE-001：房主从辩手页无法返回完整比赛控制台

| Field | Value |
|-------|-------|
| **Severity** | high |
| **Category** | ux / functional |
| **URL** | http://127.0.0.1:13220/rooms/769024/debate |
| **Repro Video** | [issue-001-repro.webm](videos/issue-001-repro.webm) |

**Description**

房主从单场控制台点击“返回辩手页面”后，底部“比赛控制”只打开包含“提前结束比赛”和“仅退出比赛页面”的简化弹窗，没有“进入完整控制台”入口。正常比赛出现 Agent、TTS、ASR 或阶段异常时，房主无法主动找到重试、跳过和状态检查工具，只能依赖偶发错误横幅或手动输入 URL。

**Repro Steps**

1. 在单场控制台查看当前自动步骤。
   ![Step 1](screenshots/issue-001-step-1.png)

2. 点击“返回辩手页面”。
   ![Step 2](screenshots/issue-001-step-2.png)

3. 点击底部“比赛控制”。弹窗没有完整控制台入口。
   ![Result](screenshots/issue-001-result.png)

**Resolution**

已在房主的比赛控制弹窗中增加“进入完整比赛控制台”，并补充组件回归测试。修复后浏览器证据：

![Fixed](screenshots/issue-001-fixed.png)

---

### ISSUE-002：已有未结束比赛时仍允许完整执行创建流程

| Field | Value |
|-------|-------|
| **Severity** | medium |
| **Category** | ux / functional |
| **URL** | http://127.0.0.1:13220/ |
| **Repro Video** | [issue-002-repro.webm](videos/issue-002-repro.webm) |

**Description**

用户已经占有房间 `#769024` 的真人席位，但首页仍允许打开创建流程、选择辩题和席位，并提交“创建比赛”。服务端最后才返回“你已在房间参赛”。既然后端明确禁止一个用户同时占用两场比赛，前端应在打开参赛窗口时直接展示“返回当前比赛”，并禁用新的创建表单。

**Repro Steps**

1. 已有未结束比赛时返回赛事大厅，点击“创建 4v4 比赛”。
   ![Step 1](screenshots/issue-002-step-1.png)

2. 创建表单仍允许选择新辩题和新席位。
   ![Step 2](screenshots/issue-002-step-2.png)

3. 提交后才显示“你已在房间参赛”。
   ![Result](screenshots/issue-002-result.png)

**Resolution**

已将当前参赛房间加入全局登录态投影。参赛窗口打开后会直接禁用创建入口、隐藏新房间配置表单，并提供“返回当前比赛”；服务端仍保留最终权限校验。修复后浏览器证据：

![Fixed](screenshots/issue-002-fixed.png)

---
