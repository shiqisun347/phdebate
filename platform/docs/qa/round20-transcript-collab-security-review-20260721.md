# Round 20：协同文字稿独立安全复核

日期：2026-07-21
范围：平台 API token、Hocuspocus/Yjs ACL、浏览器 Provider 生命周期、PostgreSQL 持久化、构建产物、Supervisor 与 Nginx。
结论：全链路设计可进入部署前验收；本轮未部署。

## 已验证的关键行为

- API 仅向已登录、通过 CSRF 且有房间查看权的用户签发最长 300 秒 token；响应为 `private, no-store`。
- token 使用 HMAC-SHA256，绑定内部 `room_id`、用户、角色、可编辑 speech id、到期时间与随机 `jti`；Node 服务严格校验文档名与房间绑定。
- 普通用户的可编辑范围按不可变发言归属判定，不随后来换座而转移；管理员可编辑所有已完成的人类发言，但不能冒充辩手提交修正申请。
- Hocuspocus 在应用 Yjs update 前克隆权威文档并验证：只允许 `Y.Map("speeches")`、只允许 token 中列出的 speech id、拒绝未解析依赖、超大消息和超大文档。
- 真实 WebSocket 双客户端测试证明同一 `Y.Text` 的并发修改可合并；viewer 写入和越权 speech 写入会断开连接，且不会进入后续观察者读取到的权威文档。
- 不同房间即使使用相同 speech id 也映射到不同 Y.Doc，数据不会串房。
- 活跃连接到 token 到期时由服务端主动关闭；Hocuspocus Provider 会重新调用平台 API 获取新 token、重连并继续同步。`beforeSync` 还会再次检查到期时间，避免事件循环延迟留下写入窗口。
- 浏览器组件按需加载 Provider/Yjs；关闭抽屉或卸载组件时销毁 Provider、awareness、Y.Doc 和监听器。仅 room、显示身份或实际发言内容变化时重建会话，普通房间 seq 更新不会反复重连。
- Nginx 只精确代理 `/collab` WebSocket，不再使用会把 `/collab-*` 一并暴露的前缀匹配；内部 `/health`、`/ready` 不直接对公网开放。
- 生产模式如果缺少 PostgreSQL URL 会直接启动失败，不再静默退化为内存存储；只接受 `postgres://` 或 `postgresql://`。

## 真实集成测试

`services/transcript-collab/test/provider.integration.test.ts` 使用真实 Hocuspocus Server、`@hocuspocus/provider`、`ws` 和多个独立 Y.Doc，覆盖：

1. 两个授权编辑者并发修改同一 `Y.Text` 并最终收敛。
2. viewer 本地尝试写入后，新的授权观察者看不到越权内容。
3. 仅有 `speech-1` 权限的连接写入 `speech-2` 被拒绝，越权内容不持久化。
4. `room-one` 与 `room-two` 文档隔离。
5. 2 秒短 token 到期后，服务端关闭旧会话，Provider 获取新 token 自动重连，重连后的修改可被另一客户端读取。

测试结果：

- transcript-collab：14 passed（含 3 个真实 Provider/WebSocket 场景）。
- API token + 0030 migration：4 passed。
- deploy 静态约束：2 passed。
- Web 协同 client/component：9 passed。
- TypeScript build：通过。
- build shell syntax：通过。
- production dependencies `npm audit --omit=dev --audit-level=high`：0 vulnerabilities。

## 生产只读检查

对 `117.50.192.216` 仅做了只读检查，没有上传、迁移或重启：

- 受管 Node `v22.23.1`、npm `10.9.8` 路径存在，可运行构建与 Supervisor 命令中指定的启动文件。
- 当前 `DATABASE_URL` 是 SQLAlchemy PostgreSQL URL；Supervisor 中的 Bash 转换会得到 `pg` 可接受的原生 PostgreSQL URL。
- PostgreSQL 正常运行。
- `TRANSCRIPT_COLLAB_HMAC_SECRET` 尚未配置、release 尚未构建、Supervisor 尚未载入该进程，符合“代码未部署”的当前状态。

部署前必须先生成独立的至少 32 字节随机 HMAC secret（API 与 collab 服务使用同一值），执行 0030 migration，再构建 release，最后加载 Supervisor/Nginx；任一步缺失都应视为发布失败。

## 剩余风险与建议

- Hocuspocus 对被拒绝的 update 会在服务端 stderr 打印堆栈。日志不含 token、正文或密钥，但恶意客户端可能制造噪声；上线后应对该日志设置频率告警与轮转，不应为了安静而把拒绝改为静默接受。
- 当前是单个协同服务实例，单进程内每个房间只有一个权威 Y.Doc，PostgreSQL 用于卸载/重启恢复。未来若横向扩容，必须先增加 Hocuspocus Redis 扩展或房间一致性路由，不能直接启动两个无协调实例。
- 协同草稿不是正式逐字稿。只有经过现有修正申请、管理员审核和审计事件后才写回 Speech/Transcript；这一边界应保持不变。
- 上线验收仍需在真实 HTTPS 域名上完成一次浏览器双账号测试，并确认代理的 WebSocket Upgrade、300 秒 token 轮换和服务重启后的 PostgreSQL 恢复。
