# Round 18 非语音代码清理、性能与视觉一致性审计

日期：2026-07-20  
范围：`platform` 的公开赛事页、赛事详情、排行榜、管理后台与非语音运维脚本。  
约束：本轮不修改实时 TTS、ASR、LiveKit、浏览器播放或 MOSS 冻结基线。

## 结论

本轮修复了一个会与“单房最多 20 人观战”产品规则冲突的旧压测默认值，降低了管理后台长表格的单页渲染量，清除了仍可能让管理员误认为存在“V2 产品版本”的流程版本写法，并统一了公开页、赛事页、排行、管理后台和参赛弹窗的中文信息标签。移动端首页首屏进一步收紧，赛事详情明确展示观战上限及不占名额的角色。

生产公开页在修改前实测没有基础性能故障：TTFB `20.5 ms`、FCP `120 ms`、LCP `136 ms`、CLS `0.0016`，390 px 视口无横向溢出。修改后本地接入生产只读 API 复核首页和赛事详情，390 px 仍无横向溢出，控制台没有应用错误。

## 已修复问题

### 1. 单房断连风暴脚本仍默认 500 个观众

文件：`scripts/verify_websocket_disconnect_storm.py`

- 旧默认值会在启用 20 人观战上限后产生错误的生产验收预期。
- 默认改为 20，并在真正发起健康检查和 WebSocket 连接前拒绝 `0`、`21`、`500` 等越界输入。
- 新增 `scripts/tests/test_verify_websocket_disconnect_storm.py`，覆盖上限、下限和默认值。
- 多房总容量脚本 `load_watchers.py` 仍可测试 500 个观众，但已经要求至少分布到 25 个房间，单房不超过 20；这是跨房容量测试，不与产品上限冲突。

### 2. 管理后台一次加载和渲染 100 行

文件：`apps/web/app/admin/page.tsx`

- 用户、房间和审计日志默认每页从 100 降到 50。
- 后台仍保留完整分页，不减少可检索数据，只降低低性能终端上的 DOM、布局和重绘成本。
- 搜索与筛选仍由提交动作触发，不在每次输入时请求服务端；请求序列保护仍会丢弃过期响应。

### 3. 用户可见的 `v2` 容易被理解为并行产品版本

文件：`apps/web/components/admin/automation-panel.tsx`

- 自动流程版本从 `v2` 改为“第 2 版”。
- 数据库内部版本号、API 契约和历史流程快照均未改变。

### 4. 页面标签语言与产品语境不统一

文件：

- `apps/web/app/page.tsx`
- `apps/web/app/rankings/page.tsx`
- `apps/web/app/competitions/[slug]/page.tsx`
- `apps/web/app/admin/page.tsx`
- `apps/web/components/participate-dialog.tsx`

将 `Human × AI Debate Arena`、`Competition`、`Leaderboard`、`System Administration`、`Create or join` 改为“人机辩论竞技场”“赛事中心”“赛季排行”“系统管理”“创建或加入比赛”。这不是单纯翻译，而是让学生和赛事运营人员首先看到任务含义，而不是实现或品牌术语。

### 5. 观战人数规则未在进入比赛前说明

文件：`apps/web/app/competitions/[slug]/page.tsx`

- 赛事详情元信息新增“单场最多 20 人观战”。
- 规则页明确：观众计入上限，辩手、房主和管理员不占用观战名额。
- 服务端的 20 人硬限制与第 21 人错误提示保持权威，本轮没有把权限判断移到前端。

### 6. 移动首屏和长列表渲染

文件：`apps/web/app/globals.css`

- 390 px 首页 Hero 从 42 px 标题和较宽垂直留白收紧到 34–38 px 自适应标题、24/42 px 上下间距；关键信息和两个主操作仍在首屏优先区域。
- 公开比赛、排行和历史记录列表启用 `content-visibility: auto`，并保留 64 px 固有尺寸，减少长列表离屏内容的布局与绘制工作。
- 未改变比赛舞台、播放器或语音控制区样式。

### 7. 赛事切换时同步布局效应阻塞首帧

文件：`apps/web/app/competitions/[slug]/page.tsx`

- 单纯滚动到页首不需要在绘制前执行，将 `useLayoutEffect` 改为 `useEffect`。
- 键盘 Tab 导航、加载竞态序列和数据请求路径保持不变。

## 删除与保留决策

| 命中内容 | 决策 | 理由 |
|---|---|---|
| 单房 `500` 个断连观众默认值 | 删除并替换为 20 | 与当前产品规则直接冲突 |
| 自动流程用户界面 `v2` | 删除并替换为“第 2 版” | 流程版本应存在，但不能表现为第二套产品 |
| 教师、课堂、课程入口 | 无需再删 | 浏览器公开页面及当前路由未发现此类入口 |
| E2E 中“退役的教师和课堂路由”负向断言 | 保留 | 防止教学域入口回归，不会进入生产界面 |
| ASR 基准语料里的教师、课堂、课程词汇 | 保留 | 它们是识别压力语料和可能的辩题内容，不是产品域或页面功能 |
| `migrate_legacy.py` 与 `legacy` 数据字段 | 保留 | 用于只读历史比赛迁移、审计和结果回溯；删除会损害历史数据 |
| 旧 `/room/...` Nginx 兼容跳转 | 保留 | 只负责把旧分享链接带回唯一当前路由，不代表双版本运行 |
| `jixia_v2_*` Cookie 兼容读取 | 保留 | 现有会话平滑升级所需；新 Cookie 已使用无版本名称，兼容值不会显示给用户 |
| 冻结目录中的 V2/MOSS 历史术语 | 保留且未改 | 可靠音频基线禁止在本轮修改，历史文档也是可回溯证据 |
| `.next`、`.DS_Store` | 不进入发布或代码备份 | 已由 `.gitignore` 排除；`.next` 是本地构建证据，不是源代码 |

## 性能证据

### 浏览器

生产修改前，`agent-browser vitals https://117.50.192.216/`：

- TTFB：`20.5 ms`
- FCP：`120 ms`
- LCP：`136 ms`
- CLS：`0.0016`
- 390 px 首页和排行榜：`scrollWidth = innerWidth = 390`

修改后本地 Next.js 接入生产只读 API：

- 390 px 首页：`scrollWidth = innerWidth = 390`
- 390 px 赛事详情：`scrollWidth = innerWidth = 390`
- 页面可见“单场最多 20 人观战”
- 控制台只有开发态 HMR/React DevTools 信息，无应用错误

### Bundle 审计

生产构建中，普通页面的 route-specific JavaScript 约 4–50 KB；统计中的约 528 KB 公共首载主要是 Next.js/React 共享运行时。比赛页的 LiveKit 大包仍只出现在比赛相关异步 chunk，没有被引入首页、登录或排行。后台的重模块继续使用 `next/dynamic` 按模块加载。

本轮没有为了追求数字而重写稳定页面为另一套数据架构。公开首页三个互不依赖的请求已经并发启动；管理端 Agent、媒体和竞赛相关子请求也已使用 `Promise.all`。当前更高收益且风险更低的措施是限制长表格渲染量和离屏绘制。

## 验证结果

- `python3 -m pytest -q scripts/tests/test_verify_websocket_disconnect_storm.py scripts/tests/test_load_watchers.py`：`16 passed`
- Web 全量 Vitest：`41 files / 283 tests passed`
- TypeScript：通过
- ESLint：`0 errors`；13 个既有 warning 全部位于冻结的 DebateStage、音频文件或既有 PostCSS 配置，本轮未修改
- Next.js 生产构建：通过，10 个静态页面生成成功
- 可靠音频基线：`82 files`，fingerprint `3219138b22878e766c94b9a1fa0e422211a6f27dd74afbbb93a8e45f7e295285`

## 后续建议

1. 后台未来达到数万用户或房间后，应保持服务端分页，并优先增加查询索引和游标分页；当前没有必要引入复杂虚拟表格库。
2. 若要继续降低公共首载，应先用生产构建的压缩传输大小和真实慢网 LCP/INP 建立预算，不能把 Next.js 共享运行时的未压缩统计误当成实际下载量。
3. 视觉方向继续保持“赛事运营工具”的克制信息层级：一个主行动、明确状态、紧凑数据，不增加无业务含义的渐变、动画和装饰性指标。
