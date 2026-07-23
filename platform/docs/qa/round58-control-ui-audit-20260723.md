# Round 58 房主控制台与比赛舞台断线体验审计

审计日期：2026-07-24  
范围：`platform/apps/web` 的房主控制台、辩手比赛页与公开观战投影  
结论：人类辩手断线后，页面现在依据服务端房间投影显示实时倒计时；60 秒到期时进入“正在自动暂停”，真人席位与身份始终保留，不提供任何 AI 接管、替补或代打入口。

## 本轮修复

1. 新增统一的断线宽限期解析逻辑，兼容参赛者可见的逐席位投影和观众可见的脱敏汇总投影。
2. 房主控制台不再显示固定“60 秒”，而是显示服务端剩余秒数并持续倒计时。
3. 倒计时归零后，主要操作显示“正在自动暂停”，同时禁用手动暂停和跳过，避免与服务端自动暂停发生操作竞态。
4. 比赛舞台的人类席位明确显示“真人 · 在线”或“真人 · 已断线”，避免仅依靠颜色圆点表达状态。
5. 断线提示统一为正向规则说明：真人席位和身份保持不变，60 秒后整场比赛自动暂停。
6. 设置、离场确认、赛事规则和暂停提示中移除了“AI 接管、AI 接替、AI 代打”类用户文案。

## 产品行为约束

- 断线不会改变席位的 `occupant_type`，页面也没有任何把真人席位改为 AI 的请求或操作。
- 60 秒倒计时以服务端权威投影为准；多个真人同时断线时使用最早到期时间。
- 到期后暂停的是整场比赛，不是单个辩手的回合。
- 观众只能看到脱敏后的断线人数和最短剩余时间，不依赖或暴露断线辩手身份。
- 比赛恢复仍由服务端校验；前端不把“辩手重新连接”直接等同于自动继续。

## 验证结果

### 自动化测试

- 定向测试：3 个文件、97 项测试全部通过。
  - `lib/disconnect-grace.test.ts`
  - `app/rooms/[code]/control/page.test.tsx`
  - `components/debate-stage.test.tsx`
- 定向 ESLint：0 个错误；保留 `DebateStage` 既有的 9 个警告，本轮未扩大范围处理。
- TypeScript 全量检查被仓库已有问题阻断：`free-turn-queue-layout.test.ts` 的 ES target 正则问题，以及 `use-room.test.tsx` 的可空字段问题；本轮新增文件未出现类型错误。
- Next.js 生产构建成功，全部页面完成类型检查、静态生成和路由构建。
- Web 全量测试：365/366 通过。唯一失败位于未修改的 `app/admin/page.test.tsx`，动态管理模块停留在“正在载入管理模块…”，测试未找到“停用”按钮；与本轮房间断线 UI 无关。

### 浏览器验证

使用真实浏览器分别检查 1440×900 桌面端和 390×844 手机端：

- 控制台能够从权威剩余时间倒数到“正在自动暂停”。
- 倒计时归零后暂停和跳过按钮均不可操作。
- 比赛舞台显示断线辩手为“真人 · 已断线”。
- 手机端页面宽度与滚动宽度均为 390px，无横向溢出。
- 实时字幕保持单行省略策略，不挤压固定底部控制区。
- 页面中未发现 AI 接管、替补或代打文案。
- 浏览器控制台无运行时错误；本地 API 未启动产生的代理拒绝仅属于测试环境，不是页面脚本错误。

## 截图证据

- [房主控制台桌面端](assets/round58-control-ui-audit/control-disconnect-1440x900.png)
- [房主控制台手机端](assets/round58-control-ui-audit/control-disconnect-390x844.png)
- [比赛舞台桌面端](assets/round58-control-ui-audit/debate-disconnect-1440x900.png)
- [比赛舞台手机端](assets/round58-control-ui-audit/debate-disconnect-390x844.png)

## 变更文件

- `platform/apps/web/lib/disconnect-grace.ts`
- `platform/apps/web/lib/disconnect-grace.test.ts`
- `platform/apps/web/app/rooms/[code]/control/page.tsx`
- `platform/apps/web/app/rooms/[code]/control/page.test.tsx`
- `platform/apps/web/components/debate-stage.tsx`
- `platform/apps/web/components/debate-stage.test.tsx`
- `platform/apps/web/app/competitions/[slug]/page.tsx`

本轮未部署、未修改 API，也未扩大到房间大厅或后台管理模块。
