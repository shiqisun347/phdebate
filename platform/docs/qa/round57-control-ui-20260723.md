# Round 57：房主、辩手控制与浅色舞台专项

日期：2026-07-23  
范围：仅 `platform/apps/web` 的 lobby、control、debate、watch、自由辩论举手及其前端测试；未修改后端或 API 契约，未部署生产。

## 结论

赛前大厅、房主控制台、辩手舞台和公开观战已经形成一致的浅色比赛操作体系。开赛等待、自动流程、真人断线 60 秒后暂停、房主恢复、非房主只读权限均有明确文案。字幕在舞台只显示一行；公开观众没有文字记录入口，也看不到实时字幕。

本轮发现并修复三个真实交互问题：

1. 普通辩手点击“更多”后，弹窗仍叫“比赛操作”，容易误认为自己拥有房主权限。现在按身份统一为“比赛控制 / 更多操作 / 观看设置”。
2. 390×844 下自由辩论举手卡片遮挡最后一排席位。现在未展开状态压缩为单行主操作，并在三秒可申请窗口临时占用固定底栏上排，不遮挡席位或主发言按钮；队列详情仍可主动展开。
3. 举手倒计时到 0 后，客户端可能短暂保留可点击状态。现在以权威截止时间即时禁用，并显示“本轮举手窗口已结束，请等待下一次申请”。

同时修复了视觉测试脚本自身的问题：使用 `localhost` 避免 Next.js 16 开发资源跨域阻断；模拟房间 WebSocket 快照和关闭 RTC，使截图反映正常连接状态，而不是测试环境的重连噪音。

## 交互检查

- 开局等待：赛前大厅显示四步流程、尚未准备的具体真人、空席 AI 补齐规则和开赛后的短暂语音准备说明。
- 房主控制：控制台强调正常流程自动推进；暂停、恢复、重试、跳过、移交房主、提前结束按风险分层。
- 真人断线：提示席位保留 60 秒，超时自动暂停，系统不会安排 AI 接管；全部真人回来后才允许房主继续。
- 非房主：舞台仅提供“更多操作”，不出现暂停、恢复和结束比赛能力。
- 固定底栏：辩手身份、文字记录、声音、全屏、控制/更多与主发言按钮保持固定；自由辩论的举手主操作进入同一底栏体系。
- 字幕：辩手舞台保持电视剧式单行省略；公开观战只显示赛况，不展示文字稿或实时字幕。
- 移动端：四排席位、单行字幕、主操作和自由辩论举手在 390×844 下无横向溢出，举手按钮不遮席位和主发言按钮。

## 验证

- Vitest 全量：`51` 个文件、`360` 项测试全部通过。
- ESLint：`0` error，`12` 个既有 warning。
- Next.js 生产构建：通过，TypeScript、静态页面生成均成功。
- Playwright 视觉 harness：1440×900 与 390×844，lobby/control/debate/debate-free/watch 共 10 张截图全部通过。
- 几何断言：无横向溢出；固定 dock 在视口内；文字记录按钮在底栏内且不遮席位；举手按钮在底栏内、不遮席位、不遮主发言按钮。
- 权限断言：公开 watch 不存在“文字记录”按钮，也无法找到模拟的实时字幕内容。

## 视觉证据

- `assets/round56-room-control-ux/lobby-desktop.png`
- `assets/round56-room-control-ux/lobby-mobile.png`
- `assets/round56-room-control-ux/control-desktop.png`
- `assets/round56-room-control-ux/control-mobile.png`
- `assets/round56-room-control-ux/debate-desktop.png`
- `assets/round56-room-control-ux/debate-mobile.png`
- `assets/round56-room-control-ux/debate-free-desktop.png`
- `assets/round56-room-control-ux/debate-free-mobile.png`
- `assets/round56-room-control-ux/watch-desktop.png`
- `assets/round56-room-control-ux/watch-mobile.png`

## 变更文件

- `apps/web/components/debate-stage.tsx`
- `apps/web/components/debate-stage.test.tsx`
- `apps/web/components/free-turn-queue.tsx`
- `apps/web/components/free-turn-queue.test.tsx`
- `apps/web/components/free-turn-queue.module.css`
- `apps/web/components/free-turn-queue-layout.test.ts`
- `apps/web/app/globals.css`
- `apps/web/e2e/.round56-room-visual.mjs`

生产当前存在运行中房间 `462372`，因此本专项未执行部署或服务重启。
