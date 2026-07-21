# Round20 协同文字稿独立服务与平台接入

日期：2026-07-21
状态：本地实现并通过定向测试；未部署，未修改冻结语音文件。

## 实现结果

新增 `services/transcript-collab`，使用固定版本：

- `@hocuspocus/server 4.4.0`
- `yjs 13.6.31`
- `pg 8.22.0`
- Node.js 22 以上，当前验证环境 Node.js 24

Hocuspocus 和 Yjs 均为 MIT License；完整依赖与许可证清单见服务目录的 `THIRD_PARTY_LICENSES.md`。版本固定在 `package.json` 和 `package-lock.json`，没有复制或自写 CRDT。

参考的官方实现：

- Hocuspocus 仓库与 MIT 许可证：https://github.com/ueberdosis/hocuspocus
- Hocuspocus v4.4.0 `beforeSync`/`beforeHandleMessage` 类型与服务器实现：https://github.com/ueberdosis/hocuspocus/tree/v4.4.0/packages/server
- Hocuspocus Database extension 的完整 Y.Doc 状态持久化方式：https://github.com/ueberdosis/hocuspocus/tree/v4.4.0/packages/extension-database
- Yjs 仓库与 MIT 许可证：https://github.com/yjs/yjs

## 认证与权限

主平台通过 `POST /api/rooms/{code}/transcript-collab-token` 签发最长 300 秒的 HMAC-SHA256 token。Token 为：

```text
base64url(JSON claims).base64url(HMAC-SHA256(payload))
```

Claims 包含 `room_id`、`user_id`、`role`、`editable_speech_ids`、`exp` 和 `jti`。Node 与 Python 使用相同 UTF-8、无 padding base64url 和 payload 签名规则。

- `document_name` 必须严格等于 `room:<room.id>`。
- system admin 可编辑该房间全部 `completed + human` Speech。
- 普通用户只能编辑 `speech_owner_id()` 判定为本人提交的 `completed + human` Speech。
- 当前席位转移不会改变历史发言所有权；测试覆盖原参赛者在席位转移后仍获得原发言编辑权。
- 登录但没有可编辑发言的用户获得 viewer token；viewer/anonymous/read-only 连接不能提交 Yjs 更新。
- API 使用 CSRF，响应 `private, no-store`，不记录 token 或 secret。
- 响应同时返回非敏感 `role` 和 `editable_speech_ids`，Web 无需自行解码 token；这两个字段仅用于界面呈现，不能替代 Hocuspocus 服务端 ACL。
- API secret 只直接读取 `TRANSCRIPT_COLLAB_HMAC_SECRET`，长度不足 32 时返回 503；没有加入平台 `config.py`。

Y.Doc 首次加载时允许为空。拥有编辑权的客户端对尚不存在的 speech 使用一次原子操作 `speeches.set(speechId, new Y.Text(initialText))` 完成初始化，随后协作者编辑该 Y.Text。客户端只能根据响应决定是否显示编辑器；Hocuspocus `beforeSync` 仍会对首次 set 和后续每个更新执行权威 speech-id 范围校验。

## 字段级 Yjs 权限

Hocuspocus `beforeHandleMessage` 收到的是完整协议帧，不能直接当作 Yjs update。实现没有误用该参数，而是：

1. `beforeHandleMessage` 只执行原始消息大小限制。
2. 使用 Hocuspocus v4.4.0 官方 `beforeSync` hook 获取解码后的 y-sync `payload`。
3. SyncStep1 是只读 state-vector 请求，允许通过。
4. SyncStep2/YjsUpdate 在应用到权威文档前先应用到克隆 Y.Doc。
5. `observeDeep` 记录本事务实际触及的 speech map key；任何超出 token scope 的 key 均拒绝。
6. 任何其他 root type 变化、未解析依赖、非法 update 或超过文档上限均 fail closed。
7. Hocuspocus 自身 `connectionConfig.readOnly` 作为第二层只读保护。

这避免了仅检查更新后的 JSON 差异而漏掉“修改后恢复原值”或嵌套 `Y.Text` 结构变化。测试同时覆盖普通 map value 和嵌套 `Y.Text`。

## 持久化与资源限制

新增 Alembic `0030_collab_documents`：

- `document_name` 主键
- PostgreSQL `BYTEA` / SQLite `LargeBinary` 的 `state`
- `version BIGINT`
- `updated_at`

Node PostgreSQL repository 全部使用 `$1/$2` 参数化 SQL，并以 upsert 保存完整 Y.Doc 状态。开发与测试使用可注入的内存 repository。Readiness 会同时验证数据库连接和 `collab_documents` 表可查询。

资源门限包括：

- WebSocket 单消息大小
- 编码后文档总大小
- 单连接未认证队列字节数和消息数
- 单连接待认证 document 数
- 固定认证超时
- 持久化 debounce/max debounce
- Hocuspocus 优雅关闭时 flush pending stores，再关闭 repository

结构化日志对 token、Authorization、Cookie、secret、update/state/content 字段脱敏。

## 健康接口

- `GET /health`：进程存活
- `GET /ready`：repository 和预期持久化表可用

平台 token 响应返回 `ws_path=/collab`；本轮没有修改 Nginx 或部署配置。

## 验证

Node：

```text
npm run check
9 tests passed
TypeScript build passed
npm audit: 0 vulnerabilities
```

覆盖：有效认证、过期、跨房间、viewer/anonymous 只读拒写、speech map 字段权限、嵌套 Y.Text 权限、持久化恢复、并发 Yjs 合并、health/readiness。

API/Alembic：

```text
4 passed
```

覆盖：0030 SQLite upgrade/downgrade/幂等、PostgreSQL/SQLite binary 类型、CSRF、私密房间越权、最长 300 秒、跨 document、过期、管理员权限、普通用户权限和席位转移。

Ruff、Python compile、Alembic 单 head 和 `git diff --check` 均通过。

## 上线前限制

- 当前 repository 设计面向单个 Hocuspocus 服务实例。多个实例直接对同一行写完整 Y.Doc 状态会出现最后写入覆盖；横向扩容前必须加入 Hocuspocus Redis/统一文档所有权方案。
- 主平台尚未提供协同编辑 UI，也没有配置 `/collab` 反向代理和进程守护。
- `0030` 迁移尚未应用到生产数据库。
- Token 的 `jti` 用于审计与未来撤销；本轮没有建立撤销表。最长 300 秒过期是当前撤销窗口上界。
