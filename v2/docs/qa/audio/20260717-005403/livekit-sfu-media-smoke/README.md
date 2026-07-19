# LiveKit SFU 外部媒体冒烟

执行时间：2026-07-18 02:03–02:16 CST。证据类型：`newly_run`。

## 结论

生产机已独立启动固定版本 LiveKit `v1.13.3`，信令通过站点现有受信任 IP 证书暴露在
`wss://117.50.218.251`，ICE/TCP `7881` 和 ICE/UDP 起始端口 `7882` 从测试 Mac 可达。

从站外 macOS 客户端使用官方 Python RTC SDK 建立一个发布者和一个订阅者，发布
`48 kHz / mono / PCM16` 的 `agent-tts` 轨并经 SFU 协商为 WebRTC 音频。修正 Nginx 为
`location ^~ /rtc` 后，连续 5/5 个独立房间均收到非静音音频，未再出现信令路径落入 Next.js
404 的情况。

| Run | 发布者连接 | 订阅者连接 | 建轨 | 收到非静音 | 总耗时 |
|---:|---:|---:|---:|---:|---:|
| 1 | 586.0 ms | 1367.7 ms | 是 | 是 | 2067.4 ms |
| 2 | 507.4 ms | 1524.6 ms | 是 | 是 | 2154.5 ms |
| 3 | 1109.8 ms | 1581.2 ms | 是 | 是 | 2803.3 ms |
| 4 | 2336.7 ms | 1847.2 ms | 是 | 是 | 4856.7 ms |
| 5 | 1361.0 ms | 1484.4 ms | 是 | 是 | 2958.2 ms |

接收端五次均报告 `48,000 Hz`、单声道、非零峰值。这里测的是冷建房/建 PeerConnection；
正式方案在进入比赛房间时提前完成这些步骤，并保持一条长驻静音轨，所以这些秒级连接耗时
不进入“LLM 首字 → 浏览器首声”的每轮预算。

## 部署与安全边界

- LiveKit server：官方 release `v1.13.3`，下载包 SHA-256
  `7ac9372a229b3d31a716d63b5a9915ad2ffb7d28a8bacc5ba5fc33a2a475d8a3`。
- 单节点路由；未配置 Redis；RTC TCP `7881`，RTC UDP range `7882–7893`。
- API key/secret 只保存在生产权限 `0600` 的独立配置中，本报告和脚本输出均不包含凭据。
- 本冒烟使用短期、单房间、最小 publish/subscribe grants；结束后发布者和订阅者均断开。
- 本轮只验证 SFU、TLS、ICE 和媒体收发；尚不等于主站 Agent→TTS→浏览器完整验收。

## 发现与修复

最初仅代理精确路径 `/rtc`。官方 SDK 在一次连接回退中访问了 RTC 子路径，落入 Next.js 404。
Nginx 已改为保留 URI 的 `location ^~ /rtc`；随后连续 5 次外部媒体冒烟全部通过。

## 下一门禁

1. 部署主站 subscribe-only token 与 Engine 长驻 publisher。
2. Chrome/Safari 在真实比赛房间进入时预连接，确认只订阅 `agent-tts`。
3. 以真实 TTS PCM 测 `llm_first_text_delta_at → 浏览器首个非静音采样`，P95≤2.5s。
4. 三房间并发、interrupt≤250ms、无串房和无旧 generation 复活。
