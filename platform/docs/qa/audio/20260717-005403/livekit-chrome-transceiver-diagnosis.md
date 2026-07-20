# Chrome LiveKit transceiver 连接失败诊断

- 日期：2026-07-18（Asia/Shanghai）
- 生产组合：`livekit-client 2.20.1` + LiveKit Server `1.13.3`
- 浏览器错误：`Failed to execute createOffer/setLocalDescription: Transceiver not found based on m-line index`
- 范围：只诊断和修改本地候选代码；未部署

## 结论

问题不是 `prepareConnection()` 重复建立 PeerConnection，也不是 `prepareConnection()` 与 `connect()` 被错误串联。

最可能的根因是：

1. `livekit-client 2.20.1` 默认启用了 `singlePeerConnection: true`。
2. LiveKit Server `1.13.3` 发布于 2026-07-03，不包含 2026-07-16 才合并的 single-PC subscriber answer 路由修复。
3. 生产 Server 日志已经出现 single-PC publisher transport 的 SDP/ICE 协商冲突，包括 `multiple conflicting ice-ufrag values`、DTLS timeout 和 participant restart 失败。
4. 浏览器是 subscribe-only participant，但 single-PC 模式仍把数据通道和下行订阅放在 publisher transport 上。服务端推送新订阅轨道时会发起 renegotiation；该路径正是官方最新 PR 修复的路径。

最小安全修复是显式设置：

```ts
singlePeerConnection: false
```

这会让浏览器恢复使用成熟的双 PeerConnection 模式，将 subscriber 下行与 publisher/data transport 分离。当前浏览器只订阅一条 `agent-tts` 音轨，因此双 PC 的额外成本很小，稳定性优先级更高。

## `prepareConnection()` 不是根因

`livekit-client 2.20.1` 的 `Room.prepareConnection()`：

- 仅检查 room 仍处于 disconnected 状态。
- 自托管 LiveKit 路径只执行一次 HTTP `HEAD`，用于 DNS/TLS 预热。
- 不创建 `RTCPeerConnection`、不调用 `createOffer()`、不添加 transceiver。

因此当前的：

```ts
await room.prepareConnection(url, token);
await room.connect(url, token, { autoSubscribe: true });
```

是官方支持的组合，不会产生两个 PeerConnection。保留它可以继续预热 TLS；此次修复没有删除该调用。

官方源码：

- [`Room.prepareConnection()`，v2.20.1](https://github.com/livekit/client-sdk-js/blob/v2.20.1/src/room/Room.ts#L779-L810)
- [LiveKit JS README 的 prepareConnection 用法](https://github.com/livekit/client-sdk-js/blob/v2.20.1/README.md#connect-to-a-room)

## 公开 URL 必须使用根地址

`livekit-client` 会在传入 URL 后自动追加 `rtc/v1`：

```text
wss://117.50.218.251
  -> wss://117.50.218.251/rtc/v1
```

因此生产 `LIVEKIT_PUBLIC_URL` 应为：

```dotenv
LIVEKIT_PUBLIC_URL=wss://117.50.218.251
```

不能配置成 `wss://117.50.218.251/rtc`，否则客户端会生成 `/rtc/rtc/v1`。生产在修正为根 URL 后已经进入真实 WebRTC 协商阶段，说明 signaling 路径已不再是当前错误的首要原因。

官方源码：[`createRtcUrl()`](https://github.com/livekit/client-sdk-js/blob/v2.20.1/src/api/utils.ts#L4-L18)

## Server 1.13.3 缺失的官方修复

LiveKit 官方 PR #4680 描述的合法 single-PC 流程与当前场景一致：

```text
服务器为新增订阅轨发 offer
  -> 浏览器返回 SDP answer
  -> answer 必须交回承载 single-PC 的 publisher transport
```

修复前 `HandleAnswer` 仍无条件把 answer 交给 subscriber transport；single-PC 模式没有独立 subscriber transport。官方修复将 answer 路由回 publisher transport。

- [LiveKit PR #4680：route answer to publisher in single-PC / one-shot mode](https://github.com/livekit/livekit/pull/4680)
- [修复 merge commit `5407ee0`](https://github.com/livekit/livekit/commit/5407ee03aeb3a1779db1575cbe6bd373a074aee1)
- [Server v1.13.3 release](https://github.com/livekit/livekit/releases/tag/v1.13.3)

PR 于 2026-07-16 合并，而 v1.13.3 于 2026-07-03 发布，因此生产二进制不含该修复。不要为了紧急修复直接在生产使用未经 release 的 main 分支二进制；先采用客户端双 PC workaround，后续升级到明确包含 #4680 的正式 Server release。

LiveKit 官方维护者也在订阅故障排查中建议用 `singlePeerConnection: false` 作为隔离 single-PC 问题的方式，并确认 false 会恢复上/下行分离的双 PC 模式：[LiveKit issue #4379](https://github.com/livekit/livekit/issues/4379)。

## 本地最小改动

文件：`apps/web/lib/audio/livekit-room-audio.ts`

```ts
const room = new LiveKitRoom({
  adaptiveStream: false,
  dynacast: false,
  stopLocalTrackOnUnpublish: true,
  singlePeerConnection: false,
});
```

同时将 `livekit-client` 从 semver caret 改为精确版本 `2.20.1`，避免重新安装依赖时静默漂移到未经本项目验证的后续版本。

## 测试

新增/加强的断言：

- Room 使用 `singlePeerConnection: false`。
- `adaptiveStream`、`dynacast` 继续关闭。
- `prepareConnection()` 仍在 `connect()` 前完成。
- 仍只订阅名为 `agent-tts` 的音轨。
- unlock、attach、interrupt flush 行为不变。

结果：

- `livekit-room-audio.test.ts`：1 passed。
- 两个 DebateStage 超时用例单独复跑均通过；合并运行时的超时属于测试进程负载，不涉及本次 LiveKit 改动。
- Next.js production build：成功，包括 TypeScript 和静态页面生成。
- `npm ci --dry-run --ignore-scripts`：通过。

## 上线后的验证重点

该改动上线后，Chrome 控制台和 Server 日志中应不再出现：

- `Transceiver not found based on m-line index`
- `multiple conflicting ice-ufrag values`
- 同一 identity 连续 `could not restart participant`

必须检查 `getStats()` 中实际存在一条 subscriber `inbound-rtp` audio，并确认：

- `bytesReceived`、`packetsReceived` 持续增长。
- selected candidate pair 成功。
- `agent-tts` 能触发 `TrackSubscribed`。
- 页面退出后两个 PC 均被关闭。

如果双 PC 模式仍失败，下一步应采集 Chrome `chrome://webrtc-internals` 和 Server debug room logs；不应重新打开 single-PC，也不应通过反复 reconnect 掩盖 SDP 状态错误。
