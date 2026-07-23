# 辩手舞台实时资源生命周期审计 Round 3

日期：2026-07-22  
范围：辩手页的麦克风、AudioWorklet、ASR WebSocket、实时字幕、重连行为、自由辩论底部举手区和移动端控制栏。  
部署：本轮只修改和验证本地代码，未部署。

## 结论

暂停、终止和阶段切换现在都以同一个浏览器清理边界处理：

1. 停止当前麦克风 `MediaStreamTrack`。
2. 向 ASR AudioWorklet 发送 generation 匹配的 `stop`。
3. 断开 Worklet 和输入 source。
4. 关闭当前 ASR WebSocket，并使旧回调失效。
5. 清空临时字幕、partial、未提交 transcript 和本轮本地状态。
6. 页面显示暂停或比赛结束提示，不显示上一条字幕。

实时连接恢复后只重新开放“开始发言”按钮，不会调用 `getUserMedia`，也不会自动请求 `/speech/start`。用户仍需明确点击开始发言。

## 本轮发现并修复的问题

### 1. 暂停/结束时的本地提示过于笼统

此前房间从 `running` 变为 `paused` 或终态时，会命中通用的“阶段或自由辩论轮次已切换”。资源能够停止，但用户无法判断是比赛暂停、比赛结束还是普通换阶段。

修复后：

- 暂停：`比赛已暂停，麦克风和实时字幕已关闭；本次未提交文字未计入比赛。`
- 完成、待复核、终止或取消：`比赛已经结束，麦克风和实时字幕已关闭；本次未提交文字未计入比赛。`
- 普通阶段/自由辩论换边仍使用原来的阶段切换提示。

### 2. 终态舞台可能继续显示“等待某位辩手开始发言”

如果终态快照保留了 `current_stage`，旧字幕空态会继续从阶段席位推导“等待某某开始发言”，与比赛已经结束矛盾。

终态字幕现在固定为：`比赛已经结束，发言和实时字幕已停止。`

### 3. 单行字幕已经不可滚动，但仍被旧辅助模块加入 Tab 顺序

Round 2 已将字幕改为固定单行并使用省略号，不再是横向滚动区域。旧的 `StageScrollAccessibility` 仍把字幕设置为 `tabindex=0`，会让键盘用户停在一个不能操作、也不需要滚动的文本节点。

本轮删除：

- `components/stage-scroll-accessibility.tsx`
- `components/stage-scroll-accessibility.test.tsx`
- 辩手页和席位无障碍测试中的相关引用

字幕仍保留 `aria-live="polite"`，但不再制造额外键盘停靠点。

### 4. 缺少底部举手面板不遮挡的可执行回归

新增 CSS 合约测试，直接验证：

- `.stage-page` 的层级为 80。
- 举手面板层级高于舞台。
- 桌面端预留 118px 底栏间距。
- 720px 以下移动端预留 176px 底栏间距和安全区。

## 生命周期核验矩阵

| 场景 | 麦克风 | AudioWorklet | ASR WS | 字幕 | 恢复行为 |
| --- | --- | --- | --- | --- | --- |
| 房主暂停/断线超时后的权威暂停快照 | track stop | `stop` + disconnect | close | 清空，显示暂停说明 | 房主继续后仍需辩手点击开始 |
| 比赛完成/待复核/终止/取消 | track stop | `stop` + disconnect | close | 清空，显示比赛结束 | 不再允许继续发言 |
| 普通阶段切换或自由辩论换边 | track stop | `stop` + disconnect | close | 清空 | 新阶段获得权限后手动开始 |
| ASR WS 短暂断开 | 麦克风暂时保持 | 当前 generation 保持 | 有界重连最多 3 次 | 显示重连状态 | 不创建新的 speech |
| ASR 重连耗尽 | 用户仍可结束本次发言 | 不无限积压 PCM | 停止重连 | 明确要求核对文字 | 不自动提交虚假字幕 |
| 房间实时 WS 恢复 | 不调用 `getUserMedia` | 不新建录音 Worklet | 不调用 speech start | 使用权威快照 | 只重新开放按钮 |

说明：房间实时 WS 短暂断开不应立即代替服务端执行“比赛暂停”。系统的产品规则是保留真人席位 60 秒，超时后由服务端权威暂停；浏览器收到暂停快照时执行完整清理。真正的网络中断会同时使 ASR WS 进入有界重连和人工核对路径。

## 新增和更新的回归测试

### DebateStage

- `never starts a microphone or speech merely because realtime reconnects`
  - 断线时按钮禁用。
  - 恢复后按钮可用。
  - `getUserMedia` 调用次数为 0。
  - 不产生 `/speech/start` 请求。
- `cleans microphone, AudioWorklet, ASR socket and subtitle when the room becomes paused`
- `cleans microphone, AudioWorklet, ASR socket and subtitle when the room becomes terminated`
  - 验证 track stop、Worklet stop、disconnect、socket close、字幕清空和结束按钮消失。

### 自由辩论布局

新增 `components/free-turn-queue-layout.test.ts`，将桌面/移动端底部安全间距和 stacking order 固化为测试合约。

### 字幕无障碍

更新 `stage-seat-accessibility.test.tsx`，明确固定单行字幕不加入键盘 Tab 顺序，同时继续运行 axe 检查。

## 验证结果

### 完整 Web 单元/组件回归

```bash
cd platform/apps/web
npm test -- --run
```

结果：

- 51 个测试文件通过。
- 343 个测试执行通过。
- 21 个需要特殊媒体环境的测试按既有配置跳过。
- 0 failed。

覆盖了辩手、观战、房间大厅、结果页、控制台、LiveKit 单音轨、ASR Worklet、字幕、自由辩论、登录和后台页面。

### 生产构建

```bash
npm run build
```

Next.js 编译、TypeScript、页面数据收集和全部静态页面生成通过。

### 静态检查

```bash
npm run lint
```

0 errors。仍有 12 条已有 warning，主要来自 `DebateStage` 旧 hooks dependency 和旧测试未使用变量；本轮没有引入 lint error。

## 约束确认

- 未修改 WebRTC 单一连续音轨实现。
- 未增加 PCM/HTMLAudio 兜底播放器。
- 未增加 AI 自动接管真人席位。
- 未改变真人断线 60 秒后由服务端自动暂停的规则。
- 未部署生产环境。

## 后续仍需真实设备验证

自动测试能证明控制流和资源调用，但不能替代真实浏览器权限、扬声器和移动端视口。发布后仍应使用真实手机完成一次：开始真人发言 → ASR partial → 模拟断线 → 60 秒暂停 → 重连 → 房主继续 → 辩手手动重新开始。重点观察浏览器麦克风占用标识是否在暂停快照到达后立即消失，以及举手面板展开后是否遮住主发言按钮。
