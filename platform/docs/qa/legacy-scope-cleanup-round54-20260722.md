# Round 54：旧机制与旧 UI 安全清理

日期：2026-07-22  
范围：当前辩论平台源码审计与安全清理，不部署。

## 结论

当前运行规则已经是唯一规则：真人断线保留席位 60 秒，超时自动暂停比赛，不生成 AI 接替席位，也不允许 AI 代替真人继续发言。

本轮删除了仍暴露在个人中心、观战页和比赛控制台中的“席位恢复申请/审批/管理员直接恢复”旧 UI。它们依赖已退役的 AI 接管流程，迁移后的正常运行房间不可能合法进入该状态，继续展示只会误导用户。

历史终局比赛中的 `ai_substitute`、旧事件和恢复申请记录仍保持只读可解析，避免破坏历史审计和旧比赛读取。

## 已完成清理

- 删除 `SeatRestorePanel` 组件及其测试。
- 观战页不再显示“申请恢复真人席位”。
- 单场控制台不再显示：
  - 真人席位恢复申请审批；
  - 管理员直接恢复真人；
  - 旧版 AI 接替席位的可操作入口。
- 个人中心对遗留记录只显示“旧版席位记录 · 仅可查看历史比赛状态”，不提供恢复按钮。
- 前端 `Room` 类型与 WebSocket/REST 投影合并不再携带当前页面不使用的 `seat_restore_requests` 和 `can_admin_restore`。
- 删除上述组件对应的无用 CSS。
- 用户可见的应用名称由 `Jixia Debate V2` 改为 `Jixia Debate`。
- 当前比赛页面不包含课堂、教师、课程或教学活动文案。
- `StageScrollAccessibility` 已在前一轮彻底删除，当前生产源码无引用。

## 新增与调整测试

- 新增 `lib/legacy-runtime-scope.test.ts`：
  - 防止个人中心、观战页、控制台重新引入席位恢复接口或按钮；
  - 防止当前赛事页面重新出现课堂/教师/课程文案。
- 控制台测试改为验证即使收到历史 `ai_substitute` 席位，也不会暴露恢复操作。
- 个人中心测试改为验证历史席位只能进入只读观战，没有恢复按钮。

## 必须保留的历史兼容范围

以下内容本轮没有删除，因为删除会破坏数据库迁移链、历史终局比赛读取或已有审计记录：

- `room_seats.occupant_type = ai_substitute` 的读取兼容。
- `seat_restore_requests` 数据表、ORM 模型和旧记录序列化。
- `seat.ai_substituted`、`seat.restore_*` 等历史事件名称与展示标签。
- Alembic 迁移 `0024`、`0033` 及更早的课堂模块迁移文件。迁移文件属于不可缺失的数据库版本链，不代表当前功能仍启用。
- 后台审计接口中的课堂/活动等退役类型过滤器。它们用于阻止旧审计记录重新出现在当前后台。
- `jixia_v2_session`、`jixia_v2_csrf` 旧 Cookie 兼容读取，以及 `v2_admin_*`、`phdebate-v2.db` 等部署兼容标识。直接改名会使已有登录或默认数据库路径失效；它们不在用户界面展示。
- 比赛引擎对遗留 `ai_substitute` 的阻断分支。该分支只会暂停异常旧房间并阻止产生新发言，是安全闸门，不是接管机制。

## 仍保留的服务端恢复接口

`seat-restore-requests` 和管理员 `seats/{seat_key}/restore` 接口本轮暂留。原因是旧 API 测试和历史数据修复工具仍依赖这些接口；迁移 `0033_remove_live_ai_substitution` 已将所有非终局房间中的遗留席位恢复为 `human` 并使待处理申请失效，因此正常运行数据无法触发这些接口的成功路径。

后续如要完全删除接口，应先完成一次生产数据库核验：

1. 活跃状态房间中 `ai_substitute` 数量必须为 0；
2. `pending` 的 `seat_restore_requests` 数量必须为 0；
3. 管理员最近 30 天无调用恢复接口记录；
4. 将旧 API 场景测试改为 404/410 退役合同；
5. 保留历史表和读取逻辑，只删除写接口与服务函数。

## 音频链路判定

- 正式 AI 发言：保留 LiveKit/WebRTC 单一连续音轨。现有测试明确验证恢复耗尽后不会切换到 PCM/WAV 播放器。
- 主持提示音：保留 `HTMLAudioElement`。它只播放比赛前已生成、全场复用的固定女声提示音，不承担实时 AI 发言。
- `/ws/rooms/{code}/audio`：本轮暂留。服务端 provider 在 RTC 未启用配置下仍引用该地址，当前不能证明删除后不会影响诊断或旧部署配置；正式浏览器路径没有使用它作为临时播放器兜底。

## 验证结果

- 前端定向回归：35 项通过。
- 前端全量测试：51 个测试文件，341 项通过，21 项按环境跳过。
- 前端生产构建：通过。
- ESLint：0 error，12 个既有 warning。
- 后端无 AI 接管与断线边界：18 项通过。
- 静态扫描：当前赛事页面无 `SeatRestorePanel`、恢复写接口、课堂/教师/课程或 `StageScroll` 运行时引用。

执行命令：

```bash
cd platform/apps/web
npm test -- --run lib/legacy-runtime-scope.test.ts app/me/page.test.tsx 'app/rooms/[code]/control/page.test.tsx' 'app/rooms/[code]/watch/page.test.tsx'
npm run lint
npm test -- --run
npm run build

cd ../api
../../.venv/bin/pytest -q tests/test_round45_no_ai_takeover_policy.py tests/test_round46_disconnect_flow_boundaries.py
```

## 未执行

- 未部署到服务器。
- 未修改 `providers.py`。
- 未修改比赛引擎核心状态转换。
- 未删除历史数据库表、迁移或历史事件。
