# Round 16 非音频比赛引擎与恢复链路验收

日期：2026-07-20

## 范围与不变量

- 覆盖 API、比赛引擎、人工恢复接口及生产验收脚本。
- 模拟四个并发且相互隔离的房间，包含真人、AI 补位、真人断线、AI 接替、控制权转移、人工暂停、失败重试和自动裁判。
- 保持服务端权威：席位、控制租约、房间状态、恢复窗口与裁判结果均由服务端校验，未通过放宽前端按钮解决问题。
- 未修改 Web、部署配置或可靠音频链路。
- 可靠音频基线复验：82 个文件，指纹
  `3219138b22878e766c94b9a1fa0e422211a6f27dd74afbbb93a8e45f7e295285`。

## 发现与修复

### 1. 生产失败步骤重试验收脚本缺少服务快照

`verify_parallel_control_recovery.py` 手工创建 `Match`，但没有像正式开赛接口一样写入
`service_snapshot`。生产环境正确拒绝了该重试，返回 409，避免旧比赛在无法确认 Agent、ASR、TTS
版本时被错误恢复。

处理：脚本现在通过 `build_service_snapshot()` 构造真实的开赛快照。未削弱生产 API 的安全校验。
新增测试同时证明：空快照在 production 仍返回 409；补齐快照后重试成功；重复请求只产生一个
`control.retry` 事件；同一幂等键使用不同参数返回 409。

### 2. QA 公开房间无法加入第二个测试账号

测试数据隐私规则原来只允许房主或已有席位者查看 QA 房间。第二个测试账号必须先查看房间才能
认领席位，因此形成“尚未入席就必须已经入席”的死锁，生产 `verify_seat_restore_flow.py` 在
`claim-seat` 返回 403。

处理：仅对“公开、lobby、登录账号明确标记为 test account”的组合开放加入。匿名用户、普通学生
账号、私密 QA 房间以及已开赛 QA 房间均没有扩大权限。测试覆盖测试账号成功入席、普通账号仍被
拒绝及席位归属正确。

### 3. 返回比赛不等于恢复在线状态

`verify_return_after_substitution.py` 在 `/api/me` 显示 watch-only 后立即执行管理员恢复。
`GET /api/me` 是只读查询，不代表浏览器已经重新加入房间；管理员接口要求原辩手
`seat.connected=true`，因此 409 是正确行为。

处理：脚本现在使用与网页相同的认证 Cookie 建立并保持 `/ws/rooms/:code` 连接，收到权威初始
快照后再执行管理员恢复，最后关闭连接。没有放宽 `connected`、活动发言或跨房冲突校验。
`verify_seat_restore_flow.py` 同样保持真实 WebSocket presence 后再由房主审批恢复申请。

### 4. 500 匿名观众压测在 lobby 阶段执行

公开 lobby 包含真人姓名和准备状态，匿名 WebSocket 按隐私设计返回 4401。旧压测脚本未开始比赛，
导致 500 个连接全部被正确拒绝，却被误判为系统故障。

处理：压测房间进入一个不调用 Provider 的稳定 `running/announcement` 阶段，再执行匿名连接、慢
消费者和事件突发追赶。保留匿名 lobby 拒绝规则。

### 5. 畸形裁判结果被当作通用引擎崩溃

正常 Judge Provider 会规范化结果，但替代适配器、测试替身或未来 Worker 交接仍可能绕过该层。
缺字段、非法胜方、NaN 或无理由曾会抛出 `KeyError`/`TypeError`，连续三次后将整个房间隔离暂停，
学生只能看到模糊的引擎异常。

处理：比赛引擎在写入权威结果前再次校验胜方、双方分数、个人分数和理由。畸形裁判结果直接进入
`review_required`，保留人工复核路径，不写排行榜；未知房间席位的个人分数被过滤。健康裁判房间
仍独立完成，异常房间不会污染其他房间。

## 四房并发场景

新增 `test_round16_engine_resilience.py` 同时处理：

1. 畸形裁判房间进入人工复核；
2. 正常裁判房间完成且个人分数只保留本房席位；
3. 两真人房间中房主断线 61 秒后由 AI 接替，控制权移交给仍在线真人，旧房主控制请求返回 403；
4. 人工暂停房间保持阶段、剩余时间和状态不变。

另验证跨房终止请求返回 403，健康房间没有收到其他房间的接替或暂停事件。

双用户 WebSocket 回归同时保持房主和另一真人辩手连接，并在 `seat.ready_changed` 增量后分别检查
`my_seat`、`can_control` 和全部 `is_me` 标记。初始快照与增量快照都按连接身份独立生成，没有复用
另一用户的个性化 room projection；RoomHub 继续只广播事件类型、房间号和序号。

## 验证结果

- Round 16 新增测试：4 passed。
- Round 5–16 引擎、生命周期、多房间和实时 Hub 聚焦回归：通过（保留一个既有 xfail）。
- API 全量测试：通过。
- 相关 Python 文件 Ruff：通过。
- 相关验收脚本 `py_compile` 与 `--help` 启动检查：通过。
- 可靠音频基线：82 文件、指纹完全一致。

生产部署后的建议执行顺序：

1. `verify_parallel_control_recovery.py`
2. `verify_seat_restore_flow.py`
3. `verify_return_after_substitution.py`
4. `verify_websocket_backpressure.py --clients 500 --slow-clients 50 --events 64`

所有脚本均包含 finally 清理；若进程被强制杀死，仍应按测试账号前缀和房间创建者执行一次数据库
核查，避免生产 QA 数据残留。
