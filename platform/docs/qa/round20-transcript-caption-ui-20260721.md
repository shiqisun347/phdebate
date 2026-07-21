# Round 20：文字记录与逐句字幕前端第一阶段

日期：2026-07-21
范围：`/rooms/:code/watch`、`/rooms/:code/debate`
状态：已完成，未部署

## 结果

- 观战页和辩手页均增加右侧“文字记录”入口及可访问的只读抽屉。
- 正式文字记录只展示当前阶段且 `status === "completed"` 的发言；`interrupted`、`failed`、`timed_out` 等状态不会冒充已完成记录。
- 当前 active speech 的最新字幕在抽屉中作为独立“正在发言”卡片展示，不计入已完成发言数量；同一份字幕同时投影到现有舞台字幕区域。
- 字幕选择按 `speech_id` 隔离，按 `segment_id` 去重，并以服务端时间戳排序；同时间戳时 final 覆盖 interim。支持 `caption.segment` 与兼容的 `asr` 实时事件，AI/ASR 使用同一投影路径。
- 没有字幕分段时明确显示“当前发言暂时没有逐句字幕”，不伪造字幕、segment id 或时间戳。
- 本人纠错能力仍在结果页按已有权限使用；抽屉不提供编辑器。无权限用户无法编辑。
- 协同编辑没有提前实现，按钮保持禁用并明确显示“协同编辑将在房间权限确认后启用”。

## 后端字段契约

`Room` 新增可选字段：

```ts
caption_segments?: Array<{
  segment_id: string;
  speech_id: string;
  seat_key: string;
  text: string;
  is_final: boolean;
  start_ms: number | null;
  end_ms: number | null;
  updated_at: string;
  source?: "asr" | "ai" | string;
}>;
```

`Room.speeches[]` 使用 `status` 判断正式记录，并可选返回 `can_request_correction`。实时事件支持：

```ts
{
  type: "caption.segment" | "asr";
  segment_id?: string;
  speech_id?: string;
  seat_key?: string;
  text: string;
  is_final?: boolean;
  timestamp_ms?: number;
  updated_at?: string;
}
```

可靠关联应优先由后端提供 `speech_id`、`segment_id` 与时间戳；前端不会为缺失字段生成伪权威值。

## 可访问性与响应式验证

- 抽屉使用具名非模态 dialog，按钮提供 `aria-controls`/`aria-expanded`。
- 打开后焦点进入关闭按钮；Escape 关闭并把焦点还给入口按钮。
- 当前实时字幕和记录数量使用礼貌播报；记录列表使用正确的 list/listitem 语义。
- axe 自动扫描无违规（关闭仅与测试环境颜色计算有关的 color-contrast 规则）。
- 1920×1080：抽屉宽 430px，页面 `scrollWidth === viewport width`，无横向溢出。
- 390×844：抽屉宽 390px，页面 `scrollWidth === viewport width`，无横向溢出；禁用协同按钮保持可见。

视觉证据：

- [舞台逐句字幕（桌面）](./round20-transcript-caption-desktop.png)
- [文字记录抽屉（桌面）](./round20-transcript-drawer-desktop.png)
- [文字记录抽屉（390px）](./round20-transcript-drawer-mobile-390.png)

## 自动化验证

- Vitest：47 个测试文件、301 个测试全部通过。
- 新增/更新覆盖：阶段过滤、snapshot 更新、completed 状态门槛、active caption 独立展示、segment 去重、final/interim、跨 speech 隔离、空状态、Escape 焦点恢复、路由集成。
- TypeScript：`npx tsc --noEmit` 通过。
- ESLint：0 errors；13 个既有 warning，均位于冻结的 `DebateStage`/音频文件或既有测试/PostCSS 配置，本轮没有新增 warning。
- Next.js 生产构建：通过，watch/debate 动态路由正常生成。

## 边界

- 本阶段没有实现 CRDT、多用户协同写入或冲突合并。
- 没有修改冻结的 `components/debate-stage.tsx`、音频核心、worklet 或后端语音运行时。
- 没有部署、没有修改线上状态。
