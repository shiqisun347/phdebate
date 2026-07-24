# Round 66 浏览器与比赛控制专项 QA

日期：2026-07-24  
范围：辩手比赛页、房主控制台、移动端关键操作；本轮未修改 API、部署或服务器配置。

## 本轮结论

- 开场准备阶段现在允许房主主动暂停，主操作明确显示“暂停准备流程”；恢复后从已保存状态继续。
- 修正准备阶段的矛盾提示：不再显示“等待下一位辩手开始发言”，改为说明正在连接比赛声音和加载开场提示。
- `participant_start_timeout` 被归类为可恢复的人员准备暂停，而不是服务故障：不显示服务重试，不允许跳过当前真人阶段，只提供“继续比赛”。
- 真人尚未点击开始时，正式发言计时保持冻结；页面单独显示安全开始倒计时，避免用户误以为发言时间正在被消耗。
- 控制台的信息层级已收敛：仅顶部警告完整解释原因，流程卡片说明已保留状态，底部控制条只给出下一步行动，避免同一长句重复铺满页面。

## 关键状态验收

| 场景 | 期望行为 | 结果 |
| --- | --- | --- |
| 开场准备 | 可暂停，计时未开始，恢复后继续自动流程 | 通过 |
| 等待真人开始 | 仅当前真人可开始；正式发言钟冻结；独立显示安全倒计时 | 通过 |
| 真人开始超时 | 自动安全暂停；无服务重试；不可跳过；房主确认后继续 | 通过 |
| 真人正在发言 | 正常结束优先；紧急暂停仅用于设备或识别卡死 | 通过（既有覆盖） |
| AI 生成/播放 | 显示生成和播放状态；异常恢复操作与真人暂停区分 | 通过（既有覆盖） |
| 真人断线 | 断线等待与自动暂停文案保持独立，不与开始超时混淆 | 通过（既有覆盖） |
| 自由辩论 | 保持原有轮次队列和发言权限逻辑 | 未改动，回归测试由主线统一执行 |
| 观众隐私 | 观战页不新增文字稿入口或控制能力 | 未改动 |

## 自动化验证

```text
npm test -- --run 'components/debate-stage.test.tsx' 'app/rooms/[code]/control/page.test.tsx'
2 files passed, 122 tests passed

npm run lint
0 errors, 14 existing warnings
```

新增测试覆盖：准备阶段暂停/恢复、真人开始安全倒计时与正式计时冻结、开始超时后的恢复路径、跳过与服务重试禁用。

## 浏览器与视觉检查

- 使用 1672×941、1440×900、390×844 三类视口检查辩手页、观战页和控制台。
- 移动端控制栏完整位于视口内，页面宽度无横向溢出；“暂停准备流程”按钮可见且可操作。
- 浏览器控制台未发现业务错误；仅有 React 开发环境提示。
- 视觉方向遵循 `image.png` 的清晰浅色产品结构，并保留辩论舞台自身的沉浸式表现，没有进行无关的全站换肤。

截图证据：

- [移动端准备状态](./round66-browser-controls/screenshots/preparing-agent-browser-mobile.png)
- [桌面端辩论舞台](./round66-browser-controls/screenshots/preparing-debate-desktop.png)
- [移动端辩论舞台](./round66-browser-controls/screenshots/preparing-debate-mobile.png)
- [移动端控制台](./round66-browser-controls/screenshots/preparing-control-mobile.png)
- [参考尺寸辩论页](./round66-browser-controls/screenshots/debate-reference.png)
- [参考尺寸观战页](./round66-browser-controls/screenshots/watch-reference.png)

## 后续联调关注

- 后端应在真人安全开始期限到达时写入 `pause_health.reason_code=participant_start_timeout`，并保留当前阶段和完整发言时长。
- 恢复接口应回到同一真人阶段，重新建立合理的开始期限；不得跳到下一阶段或改为 AI 接管。
- 真实多人浏览器联调时应再次核对：超时瞬间的 WebSocket 投影、控制台和辩手页状态是否同一帧收敛。
