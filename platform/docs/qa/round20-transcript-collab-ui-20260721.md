# Round 20：协同文字稿前端闭环

日期：2026-07-21
范围：观战/辩手页面的文字记录抽屉、Hocuspocus/Yjs 协同客户端、修正申请闭环
状态：已完成，未部署

## 实现结果

- Web 固定依赖：
  - `@hocuspocus/provider@4.4.0`
  - `yjs@13.6.31`
- 文字记录抽屉默认仍是轻量只读模式；只有用户点击“协同编辑”后才动态导入协同客户端并连接 `/collab`。
- 使用 token API 返回的 `document_name`、`token`、`ws_path`、`role` 和 `editable_speech_ids`；前端不解码、不推断 token 内部 claims。
- token API 返回结构会先做 fail-closed 校验，缺失文档名、token、角色、WebSocket 路径或可编辑 ID 列表时不会建立连接。
- Provider 重连时重新请求短期 token；文档名必须继续匹配原房间，否则拒绝重连。
- 使用 `Y.Map("speeches")`，每个 `speech_id` 对应独立 `Y.Text`。首次同步完成后，仅对当前 token 可编辑且尚不存在的 speech，在同一个 Yjs transaction 中用权威 `Speech.content` 初始化。
- 已存在的协同草稿不会被后续权威初始化覆盖；不同 speech key 的更新相互隔离。

## 权限与保存闭环

- 普通参赛者只有 `editable_speech_ids` 中本人、真人、`completed` 的发言显示编辑器；其他发言只读。
- Viewer token 只读，无法向 Hocuspocus 服务提交更新。
- 匿名用户获取 token 失败时，协同区显示可恢复错误；上方正式文字记录仍保持只读可用。
- 系统管理员可以协同查看和编辑 token 授权的草稿，但页面不显示“提交修正申请”，不能冒充作者提交。
- 参赛者保存草稿时：
  - 必须填写至少 2 个字符的修正原因。
  - 必须经过浏览器二次确认。
  - 调用既有 `POST /api/rooms/:code/speeches/:speechId/correction-requests`。
  - 使用每个 speech 独立、可重试的 UUID 幂等键。
  - 不直接覆盖权威 `Speech`。
  - 返回 pending 后显示“修正申请等待管理员审核”，并移除重复提交入口。

## 连接、awareness 与生命周期

- 显示 connecting、connected、synced、disconnected、error 状态。
- 显示 awareness 中其他在线编辑者姓名和人数。
- 断线由 Hocuspocus Provider 自动恢复，并在重连时刷新 token。
- 抽屉关闭或关闭协同模式时，取消订阅并销毁 Provider、Awareness 与 `Y.Doc`。
- Room WebSocket snapshot 即使创建了新的 `speeches` 数组对象，只要 speech id 与权威 content 未变化，就不会反复销毁和重连协同 Provider；只有房间、身份显示名、speech 集合/权威内容实际变化或用户主动重试时才重建连接。

## 懒加载与包体

- `TranscriptCollaboration` 不静态导入 Hocuspocus 或 Yjs，只使用：

```ts
import("@/lib/transcript-collab-client")
```

- 生产构建中 Hocuspocus/Yjs 位于独立约 130 KB 的 JS chunk。
- 浏览器在打开普通观战页及只读文字记录时不请求该 chunk；点击“协同编辑”后才请求对应脚本。

## 多实例与安全验证

- 新增真实 Hocuspocus Server + Provider 集成测试：
  - 两个真实 Provider 连接同一 document。
  - 两个客户端的 Y.Text 更新最终合并一致。
  - Viewer token 本地尝试写入时，服务端 `beforeSync` 拒绝并关闭连接。
  - 第三个观察客户端确认越权内容没有进入服务端文档。
- 协同服务 `npm run check`：TypeScript 构建通过，10 项测试全部通过。
- 协同服务新增的测试 WebSocket polyfill 固定为无已知 npm audit 漏洞的 `ws@8.21.1`；当前 audit 为 0 vulnerabilities。

## Web 验证

- Vitest：50 个测试文件、315 项测试全部通过。
- 覆盖：
  - 双客户端 Yjs 合并、跨 speech 隔离、原子初始化和已有草稿保护。
  - Participant、viewer、anonymous、admin 权限。
  - awareness、同步/断线恢复状态、销毁。
  - 纠错理由、二次确认、幂等提交与 pending 防重复。
  - Room snapshot 数组身份变化不触发重连。
  - 懒加载源码与固定依赖版本。
  - axe 自动可访问性检查。
- ESLint：0 errors；13 个既有冻结舞台/音频或 PostCSS warning，本轮无新增 warning。
- Next.js 生产构建：通过。
- 生产浏览器视觉检查：
  - 1920×1080：抽屉 430px，`scrollWidth === viewport width`。
  - 390×844：抽屉 390px，`scrollWidth === viewport width`。
  - 匿名/无效 token 场景保留正式记录和清晰的重新连接入口。

视觉证据：

- [桌面协同抽屉](./round20-transcript-collab-desktop.png)
- [390px 协同抽屉](./round20-transcript-collab-mobile-390.png)

## 边界

- 协同内容只是草稿；只有既有 correction request 审核流程可以改变权威文字。
- 没有修改冻结的 `components/debate-stage.tsx`、音频模块或 worklet。
- 没有部署，也没有改变线上比赛或协同文档。
