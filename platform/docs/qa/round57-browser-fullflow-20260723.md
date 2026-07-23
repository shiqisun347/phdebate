# Round 57 生产浏览器完整比赛黑盒测试报告

| 项目 | 结果 |
|---|---|
| 日期 | 2026-07-23（Asia/Shanghai） |
| 生产站点 | `https://117.50.192.216` |
| Web 发布 | `round55b-disconnect-a11y-hotfix-20260722` |
| API 发布 | `round55-human-disconnect-pause-20260722` |
| 测试工具 | agent-browser 0.32.1，隔离的房主、第二真人、匿名观战会话 |
| 生产操作 | 仅创建测试房间和正常比赛操作；未部署、未重启服务 |
| 收尾状态 | `/api/live-rooms` 返回空列表，没有继续占用生产房间 |

## 结论

1v1 双真人比赛能够从注册、建房、认领席位、准备、开赛、真人文字发言、自由辩论、双方总结、AI 裁判一直完成到结果页。1v1 真人 + AI 的 AI 补位、真人断线保留席位、超过 60 秒自动暂停、真人重连后房主手动恢复也能工作。4v4 混合比赛能够以两名真人和六名 AI 正常锁定并启动，并推进到 AI 二辩发言。

当前生产版仍不能判定为“完整比赛无问题”。自由辩论计时、AI 实时字幕、结果文字记录存在三个直接影响比赛公平性或数据完整性的高优先级问题。另有两个状态同步/解释问题。它们与 Round 56 发现一致，说明本地后续修复尚未进入当前生产发布。

## 覆盖结果

| 场景 | 房间 | 结果 | 证据 |
|---|---:|---|---|
| 1v1 双真人完整比赛 | `462372` | **完成，有高优先级问题**。四次固定阶段真人发言成功，自动进入裁判和结果页 | [完整结果页](assets/round57-browser-fullflow/screenshots/1v1-complete-result-owner.png) |
| 1v1 真人 + AI | `584746` | **通过断线恢复链路**。反方由 AI 自动补位；真人断线后席位未被 AI 接管；超时暂停；重连后房主继续 | [开始状态](assets/round57-browser-fullflow/screenshots/1v1-human-ai-started.png)、[重连暂停状态](assets/round57-browser-fullflow/screenshots/disconnect-reconnected-owner.png)、[继续比赛菜单](assets/round57-browser-fullflow/screenshots/disconnect-resume-menu.png) |
| 4v4 混合比赛 | `611506` | **通过启动与首轮 AI 发言验证**。两名真人、六名 AI，正反方席位正确；真人一辩完成后进入 AI 正方二辩驳论 | [两名真人准备](assets/round57-browser-fullflow/screenshots/4v4-two-humans-ready.png)、[六 AI 补位并开赛](assets/round57-browser-fullflow/screenshots/4v4-mixed-started-owner.png) |
| 匿名观战权限 | `462372` | **通过**。赛中无“文字记录”入口；赛后明确提示观众不能查看字幕和发言文字 | [匿名观战画面](assets/round57-browser-fullflow/screenshots/1v1-watch-initial.png) |
| 真人断线不被 AI 接管 | `584746` | **通过**。重连后仍为原真人席位，页面明确显示“真人断线超过 60 秒，比赛已自动暂停” | [真人重连后的权威状态](assets/round57-browser-fullflow/screenshots/disconnect-reconnected-owner.png) |
| 移动端 390×844 | 本地当前工作树 mock | **通过**。文字记录已进入底部工具栏，验证器同时断言按钮位于控制栏安全区且不与席位重叠 | [固定阶段移动端](assets/round56-room-control-ux/debate-mobile.png)、[自由辩论移动端](assets/round56-room-control-ux/debate-free-mobile.png) |
| 多房间数据隔离 | Round 56 生产证据 | **通过，未重复占用生产房间**。房间 `141327` 与 `733951` 的题目、阶段、结果、历史和断线状态互不串用 | [Round 56 报告](round56-prod-fullflow-dogfood-20260723.md) |
| 服务就绪 | 生产健康接口 | **通过**。数据库、Redis、Engine、Worker、MOSS、FunASR、存储和备份均为 `ok` | 2026-07-23 22:21 CST `/api/health/ready` |

## 发现的问题

### ISSUE-001：自由辩论在真人点击发言前已经消耗总计时和单轮计时

| 字段 | 内容 |
|---|---|
| 严重级别 | **High / P0** |
| 分类 | 比赛流程、公平性、计时 |
| 页面 | `/rooms/462372/debate` |

**实际表现**

进入自由辩论后，页面仍显示“等待下一位辩手”和“开始发言”，真人没有点击发言，但总计时和本轮计时持续下降。本轮归零后系统直接切换阵营，形成没有任何有效发言的空轮次。本次测试中自由辩论从约 `03:59 / 00:29` 自动下降，并在真人没有发言时多次轮转。

**预期**

真人轮次应在用户点击“开始发言”并成功建立本次发言后才启动总计时和单轮计时；等待麦克风、思考、断线恢复和文字模式准备均不应消耗发言时间。

**证据**

- [未发言但单轮只剩 6 秒](assets/round57-browser-fullflow/screenshots/issue-free-clock-before-wait.png)
- Round 56 首次稳定复现：[自由辩论未开始即计时](assets/round56-prod-fullflow/screenshots/issue-001-free-debate-clock-before-first-speech.png)

**影响**

真人可能尚未开口就失去本轮，比赛时间和阵营轮换失真，直接破坏公平性与完整比赛可用性。

---

### ISSUE-002：AI 正在发言时没有逐句字幕

| 字段 | 内容 |
|---|---|
| 严重级别 | **High** |
| 分类 | 实时字幕、无障碍、人机辩论 |
| 页面 | `/rooms/611506/debate` |

**实际表现**

4v4 房间的 AI 正方二辩“乾元”已进入“正在发言”，倒计时开始，但中央字幕仍显示“当前发言暂时没有逐句字幕”。Round 56 的 1v1 人机完整赛同样整轮稳定复现。

**预期**

Agent 的流式文本应投影为一行稳定短语字幕；字幕更新不能因为同序号的语音遥测或房间快照而被丢弃。匿名观众继续保持不可见字幕，不能因此关闭参赛者字幕。

**证据**

- Round 56 生产截图：[AI 发言无逐句字幕](assets/round56-prod-fullflow/screenshots/issue-003-ai-timer-before-caption-audio.png)
- 本轮 4v4 浏览器文本状态确认 AI 已发言、字幕仍为空。

---

### ISSUE-003：结果页声称“已加载 4/4”，实际只显示两条发言

| 字段 | 内容 |
|---|---|
| 严重级别 | **High** |
| 分类 | 数据完整性、比赛历史、结果页 |
| 页面 | `/rooms/462372/result` |

**实际表现**

本场比赛摘要显示发言数为 `4`，文字记录标题显示“已加载 4 / 4”，但页面 DOM 和完整截图中只有正反方立论两条，正方总结、反方总结均不存在；“申请修正发言文字”按钮也只有两个。AI 裁判理由实际引用了双方总结内容，说明总结已参与裁判，但结果页未呈现。

**预期**

参赛者结果页必须展示所有已完成发言；“已加载 N/N”应与可见记录数量一致。任何分页、去重或阶段过滤都不能静默丢失总结发言。

**证据**

- [完整结果页长截图：已加载 4/4，但仅两张记录卡](assets/round57-browser-fullflow/screenshots/1v1-result-full-owner.png)
- 浏览器 DOM 核验：`申请修正发言文字` 按钮数为 `2`，正文不包含“正方总结”或“反方总结”。

---

### ISSUE-004：旧阶段的文字发言弹窗在阶段切换后继续遮挡页面

| 字段 | 内容 |
|---|---|
| 严重级别 | **Medium** |
| 分类 | 状态同步、恢复操作、UX |

**实际表现**

提交固定阶段文字发言后，比赛已经切换到下一阶段，旧弹窗仍变为“上一轮文字已停止提交”并留在最上层。轮到同一真人的新阶段时，用户必须先点击“清除上一轮草稿”，才能再次选择文字发言。

**预期**

提交成功或 `speech_id/stage_key` 改变后应自动关闭旧弹窗并清除其交互状态，只保留轻量成功提示。

**证据**

- Round 56 截图：[旧文字弹窗遮挡下一阶段](assets/round56-prod-fullflow/screenshots/issue-004-stale-text-dialog-next-stage.png)
- 本轮在反方总结前再次稳定复现，并需要手动清除后才能继续比赛。

---

### ISSUE-005：匿名观战把真人断线暂停错误描述为服务异常

| 字段 | 内容 |
|---|---|
| 严重级别 | **Medium** |
| 分类 | 状态一致性、错误解释、观战体验 |
| 页面 | `/rooms/584746/watch` |

**实际表现**

同一房间中，真人页面正确显示“真人断线暂停”“真人断线超过 60 秒，比赛已自动暂停”，匿名观战页却显示“服务异常暂停”“比赛因临时服务异常暂停”。

**预期**

公开投影应使用脱敏但准确的原因，例如“参赛者暂时离线，比赛已安全暂停”，避免把正常保护机制误报为系统故障。

**证据**

- [观战页错误状态](assets/round57-browser-fullflow/screenshots/disconnect-0s-watch.png)
- [同一房间真人页正确状态](assets/round57-browser-fullflow/screenshots/disconnect-reconnected-owner.png)

## 已确认正常的关键行为

- 房主和第二真人可以分别注册、登录、认领固定席位并准备。
- 开赛前需要二次确认，不会因误点立即锁定。
- 固定真人阶段在点击发言前不启动该阶段倒计时。
- 无麦克风环境可以使用文字发言完成比赛，阶段绑定能阻止旧文字提交到新阶段。
- 匿名观众赛中没有文字记录入口，赛后也无法查看逐字稿。
- 真人断线不会由 AI 接管，原席位和身份被保留。
- 断线暂停后重连不会自动继续，房主必须明确点击“继续比赛”。
- 4v4 空席位能够自动补为稳定命名的 AI 辩手，普通用户不需要配置模型或人设。
- AI 裁判能够生成胜负、团队分、个人分和详细理由。
- MOSS、FunASR、Engine、Worker 与数据库健康检查均通过；当前没有生产活动房间。

## 本轮未作为通过依据的部分

- agent-browser 的无头 Chrome 无法代表真实物理麦克风，本轮没有用真人声音验证 FunASR 端到端识别质量。ASR 协议、断线重连和单行字幕的自动化证据见 [Round 56 ASR 报告](round56-asr-caption-20260723.md)。
- 4v4 本轮验证到首个 AI 二辩发言后即由房主正常提前结束，避免长期占用生产房间；完整多阶段 4v4 仍建议在 ISSUE-001 至 ISSUE-003 修复部署后再做最终验收。
- 390×844 使用当前本地工作树和 mock 房间完成视觉/碰撞断言，证明待部署代码有效；它不是当前 Round 55 生产包的证明。

## 代码修改

本子任务没有修改应用代码，只生成黑盒证据和本报告。移动端验证使用现有 `e2e/.round56-room-visual.mjs`，脚本本轮未修改。

