# Round 20：自由辩论举手队列 UI

日期：2026-07-21
范围：`/rooms/:code/watch`、`/rooms/:code/debate`
状态：已完成，未部署

## 已完成

- 仅在 `current_stage.kind === "free"` 时渲染举手队列，其他比赛阶段完全隐藏。
- 辩手页支持：
  - `can_request=true` 时显示绿色“举手申请下一轮”按钮。
  - 已存在 `my_request` 时切换为“取消举手（第 N 位）”。
  - 申请和取消分别调用 `POST /free-turn-requests` 与 `POST /free-turn-requests/:id/cancel`。
  - 每次新操作生成新的 UUID 幂等键；busy 状态阻止快速双击产生重复请求。
  - REST 成功响应立即交给页面的 `setRoom` 合并，后续 WebSocket snapshot 仍为权威状态。
  - 错误以可访问 alert 显示，busy 释放后按钮可重新操作。
- 观战页只读展示队列，不提供申请或取消能力。
- 队列显示举手图标、服务端顺序、席位姓名与 `requested_at` 时间。
- 通过页面外层 DOM adapter 在冻结舞台的对应席位旁显示“第 N 位”徽标；选中席位显示“已选中”。
- `selected_human_seat` 存在时明确提示“已被选中，其余席位等待下一轮”。
- 三秒窗口依据服务端 `window_deadline_at` 每 100ms 更新；显示到 0.1 秒，并将异常时钟偏差限制在协议规定的 3 秒上限。
- 所有不可申请状态显示服务端 `request_reason`，不使用笼统的“不可用”。

## 契约补充

前端新增 `Room.free_turn_queue` 类型：

```ts
type FreeTurnQueue = {
  items: Array<{
    request_id?: string;
    side: "aff" | "neg";
    seat_key: string;
    order: number;
    requested_at: string;
    is_me: boolean;
  }>;
  window_deadline_at: string | null;
  window_remaining_ms: number | null;
  my_request: FreeTurnQueueItem | null;
  target_side?: "aff" | "neg";
  target_turn_seq?: number;
  can_request: boolean;
  request_reason: string;
};
```

为使取消操作可执行，后端仅在当前登录用户自己的队列项上返回 `request_id`；匿名用户和其他席位看不到该标识。取消接口仍在服务端验证房间和本人身份。

## 可访问性与移动端

- 队列使用具名 aside、二级标题与有序列表语义。
- 倒计时提供完整中文 `aria-label`，视觉值使用 0.1 秒精度。
- 按钮通过 `aria-describedby` 关联具体可用/禁用原因。
- 当前选中状态使用 `role=status`，错误使用 `role=alert`。
- 席位旁徽标不是额外键盘停靠点，并提供包含姓名、顺序或选中状态的无障碍名称。
- axe 自动扫描无违规（测试中仅关闭 jsdom 无法可靠计算的颜色对比规则）。
- 390px 样式将面板收窄为 `viewport - 24px`、限制队列高度并缩小席位徽标，避免横向溢出和遮住底部发言控制区。

## 验证结果

- Web 定向测试：3 个文件、18 项全部通过。
  - 申请、取消、UUID 幂等键、双击防重。
  - REST snapshot 回填、错误后恢复。
  - 三秒倒计时。
  - watch 只读、非自由阶段隐藏。
  - 队列排序、申请时间、选中席位、DOM adapter 清理。
  - axe 可访问性检查。
- API 队列测试：4 项全部通过；另有 1 条 Starlette 测试依赖弃用 warning，与本次功能无关。
- TypeScript：`npx tsc --noEmit` 通过。
- ESLint：0 errors；13 个既有 frozen 文件/PostCSS warning，本轮无新增 warning。
- Next.js 生产构建：通过。

## 边界

- 没有修改冻结的 `components/debate-stage.tsx`、音频模块或 worklet。
- 没有部署，也没有改变线上比赛状态。
