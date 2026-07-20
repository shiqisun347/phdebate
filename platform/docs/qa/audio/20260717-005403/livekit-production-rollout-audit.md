# LiveKit / WebRTC 上线前最小安全审计

- 审计时间：2026-07-18（Asia/Shanghai）
- 范围：生产环境变量、Supervisor 进程边界、Python/Web 依赖与构建、启用顺序、回滚
- 操作：只读审计；未部署、未重启、未修改生产配置

## 结论

当前可以部署代码和依赖，但**尚不能打开用户可见的 WebRTC 音频**。

主要原因：

1. 生产 Python 环境尚未安装 `livekit==1.1.13`、`livekit-api==1.2.0`。
2. 生产 Web 源目录尚未安装 `livekit-client`，必须使用 lockfile 重新安装并生成新的 standalone release。
3. 生产 `.env` 尚无 LiveKit URL、API key/secret 和 WebRTC 开关。
4. 当前没有已通过生产门禁的 MOSS-Realtime 增量 TTS endpoint。已知 LightTTS bi-stream 会卡死并遗留占用槽，不能作为正式增量链路。
5. LiveKit 当前配置为 UDP 端口范围 `7882–7893`，不能只验证或放通 `7882/udp`。

安全上线应分两步：先在前端关闭的情况下启用后端 LiveKit 静音发布者做影子连接；真实 MOSS 增量 TTS 和 Chrome/Safari 门禁通过后，才构建并切换 `NEXT_PUBLIC_WEBRTC_AUDIO_ENABLED=true` 的 Web release。

## 生产只读核对结果

| 项目 | 当前状态 | 判断 |
|---|---|---|
| `jixia-api` | RUNNING | 正常 |
| `jixia-engine` | RUNNING | 正常 |
| `jixia-worker` | RUNNING | 正常 |
| `jixia-web` | RUNNING | 正常 |
| `jixia-livekit` | RUNNING，LiveKit Server `1.13.3` | SFU 已独立运行 |
| `.env` 权限 | `600 ubuntu:ubuntu` | 合格 |
| LiveKit config 权限 | `600 ubuntu:ubuntu` | 合格 |
| `APP_ENV` | `production` | 合格 |
| `ENGINE_ENABLED` | `false` | 必须保持；避免 API 再启动内嵌 Engine |
| `LIGHTTTS_MAX_ACTIVE` | `1` | 必须保持 |
| `LIGHTTTS_GLOBAL_GATE_ENABLED` | `true` | 当前既有配置；本次 WebRTC 上线不要顺带修改 |
| WebRTC/MOSS/Realtime 开关 | 未配置，等效默认关闭 | 当前安全 |
| Python `livekit` / `livekit-api` | missing | 必须先升级版本化虚拟环境 |
| Web `livekit-client` | missing | 必须先按 lockfile 安装并重建 Web release |
| LiveKit signaling | `7880/tcp`，Nginx `/rtc` TLS 代理 | 已具备基本路径 |
| LiveKit ICE/TCP | `7881/tcp` | 已配置 |
| LiveKit ICE/UDP | `port_range_start: 7882`、`port_range_end: 7893` | 必须放通完整 UDP 范围，或改为单一 `udp_port` |

`/api/health/ready` 当前不会检查 LiveKit signaling、publisher 是否成功发布轨道，也不会检查浏览器 ICE/Opus。因此普通 readiness 为 200 不能替代 WebRTC 上线门禁。

## 必须保持的进程边界

```text
jixia-api
  - 登录、房间权限检查
  - 签发 subscribe-only 短 TTL token
  - 不创建 agent-audio publisher

jixia-engine
  - 唯一的 Agent/TTS 执行者
  - 唯一的 room publisher 所有者
  - PCM -> 48k/20ms -> LiveKit AudioSource

jixia-web
  - 只持有 subscribe-only token
  - 订阅 agent-tts，不持有 API secret

jixia-livekit
  - 独立 SFU 进程
  - 不和 API/Engine 共用 Python event loop
```

生产 `.env` 中必须保留：

```dotenv
ENGINE_ENABLED=false
```

API 的 lifespan 会调用 `match_engine.start()`；若该值缺失，会按默认值 `true` 在 API 进程再启动一个比赛引擎。独立 `apps/engine/main.py` 直接运行 `match_engine.run()`，因此 `.env` 中设为 `false` 不会关闭 Supervisor 管理的独立 Engine。

Supervisor 中 Engine 的 `environment=ENGINE_ENABLED=true` 会在 command 内再次 `source .env`，但独立 Engine 入口本身不依赖此设置。不要依赖 Supervisor 的这一覆盖来阻止 API 启动内嵌 Engine。

## 第一阶段：代码和依赖上线，所有新功能保持关闭

此阶段允许升级代码、Python 依赖和 Web release，但不得改变用户音频路径。

必须明确写入生产 `.env`：

```dotenv
ENGINE_ENABLED=false

WEBRTC_AUDIO_ENABLED=false
WEBRTC_AUDIO_BACKEND=websocket_pcm
NEXT_PUBLIC_WEBRTC_AUDIO_ENABLED=false

REALTIME_VOICE_PIPELINE_ENABLED=false
REALTIME_VOICE_BACKEND=lighttts

LIGHTTTS_STREAMING_ENABLED=false
LIGHTTTS_BISTREAM_ENABLED=false
LIGHTTTS_MAX_ACTIVE=1

MOSS_TTS_REALTIME_ENABLED=false
MOSS_TTS_REALTIME_URL=
MOSS_TTS_REALTIME_URLS=[]
```

以下现有值不要作为 WebRTC 发布的一部分调整：

```dotenv
LIGHTTTS_GLOBAL_GATE_ENABLED=true
LIGHTTTS_MAX_ACTIVE=1
```

LightTTS Redis gate 与 WebRTC 无直接依赖。当前 gate 已在生产启用；除非单独完成 admission gate 回归，不应在同一发布窗口关闭或重新调参。

### Python 依赖步骤

候选虚拟环境必须包含：

```text
livekit==1.1.13
livekit-api==1.2.0
```

先只构建和验证候选环境：

```bash
sudo PHDEBATE_SKIP_SWITCH=true ./deploy/upgrade-python-runtime.sh
```

至少确认：

```bash
.python-venvs/<release>/bin/pip check
.python-venvs/<release>/bin/python -c \
  'from importlib.metadata import version; print(version("livekit"), version("livekit-api"))'
```

候选通过后再使用版本化运行时脚本切换。该脚本会停止并重新启动 API、Engine、Worker，并在失败时恢复上一虚拟环境链接。切换前必须记录当前 `.venv` 链接目标。

### Web 依赖与构建步骤

生产源目录当前缺少 `livekit-client`。必须依据 `apps/web/package-lock.json` 安装，不得只执行一个会漂移版本的临时 `npm install`：

```bash
export PATH=/home/ubuntu/sunsq/phdebate/runtime/node/bin:$PATH
npm --prefix apps/web ci
node -p 'require("./apps/web/node_modules/livekit-client/package.json").version'
```

当前 lockfile 固定解析到 `livekit-client 2.20.1`。第一阶段 Web 构建必须保持：

```dotenv
NEXT_PUBLIC_WEBRTC_AUDIO_ENABLED=false
```

然后生成独立 release：

```bash
PHDEBATE_WEB_DEPLOYMENT_MODE=root ./deploy/build-web-release.sh
```

`NEXT_PUBLIC_WEBRTC_AUDIO_ENABLED` 是 Next.js 构建期常量，不是仅重启 Web 就能改变的运行时开关。每次切换 true/false 都必须切换到对应的已构建 release。

## 第二阶段：只启用后端 LiveKit 影子连接

在 Python SDK 已安装、API key/secret 与 SFU config 完全一致后，可在无活动比赛的窗口启用后端 publisher，但仍不让浏览器切换音频路径：

```dotenv
WEBRTC_AUDIO_ENABLED=true
WEBRTC_AUDIO_BACKEND=livekit

LIVEKIT_URL=ws://127.0.0.1:7880
LIVEKIT_PUBLIC_URL=wss://117.50.218.251/rtc
LIVEKIT_API_KEY=<与 LiveKit config 一致>
LIVEKIT_API_SECRET=<与 LiveKit config 一致>
LIVEKIT_AUDIO_SAMPLE_RATE=48000
LIVEKIT_AUDIO_FRAME_MS=20
LIVEKIT_AUDIO_SOURCE_QUEUE_MS=100
LIVEKIT_AUDIO_APP_QUEUE_MS=120
LIVEKIT_CONNECT_TIMEOUT_SECONDS=5
LIVEKIT_TOKEN_TTL_SECONDS=300
LIVEKIT_PUBLISHER_TOKEN_TTL_SECONDS=600

NEXT_PUBLIC_WEBRTC_AUDIO_ENABLED=false
REALTIME_VOICE_PIPELINE_ENABLED=false
LIGHTTTS_STREAMING_ENABLED=false
LIGHTTTS_BISTREAM_ENABLED=false
MOSS_TTS_REALTIME_ENABLED=false
```

这一组合的作用：

- Engine 为活跃/暂停房间建立长驻静音 `agent-tts` 音轨。
- API 可以签发短 TTL、subscribe-only token。
- 当前 Web release 不连接 LiveKit，用户继续使用既有 WS/WAV 回退播放。
- 不会触发已知会卡死的 LightTTS bi-stream。

影子阶段必须检查 Engine 日志中没有重复 identity、publisher 重连循环、持续 backlog 或 RSS 单调增长。API 进程日志中不应出现发布轨建立记录，因为 API 只能签 token。

## 用户可见 WebRTC 的启用条件

不得只打开：

```dotenv
NEXT_PUBLIC_WEBRTC_AUDIO_ENABLED=true
```

前端一旦成功连接 LiveKit，会停止现有 PCM WS 播放。如果后端没有真实增量 PCM tee，用户将连接到一条只有静音的音轨。

当前 LightTTS 不能承担正式双向增量链路，以下值必须保持 false：

```dotenv
LIGHTTTS_STREAMING_ENABLED=false
LIGHTTTS_BISTREAM_ENABLED=false
```

只有 OpenMOSS-Realtime-TTS GPU endpoint 已完成单场质量、2–3 场并发、取消释放、CER 和首声门禁后，才允许使用以下完整组合：

```dotenv
WEBRTC_AUDIO_ENABLED=true
WEBRTC_AUDIO_BACKEND=livekit

REALTIME_VOICE_PIPELINE_ENABLED=true
REALTIME_VOICE_BACKEND=moss_realtime
MOSS_TTS_REALTIME_ENABLED=true
MOSS_TTS_REALTIME_URLS=["http://<endpoint-1>","http://<endpoint-2>","http://<endpoint-3>"]
MOSS_TTS_REALTIME_MAX_ACTIVE=3
MOSS_TTS_REALTIME_MAX_ACTIVE_PER_ENDPOINT=1

LIGHTTTS_STREAMING_ENABLED=false
LIGHTTTS_BISTREAM_ENABLED=false
LIGHTTTS_MAX_ACTIVE=1

NEXT_PUBLIC_WEBRTC_AUDIO_ENABLED=true
```

`MOSS_TTS_REALTIME_URLS` 中每个地址必须是独立模型/codec 进程；不能把同一个 endpoint 重复三次伪装成三路容量。音色所需的 `MOSS_TTS_PROMPT_FILES` 必须在每个 endpoint 上均可解析。

启用顺序必须是：

1. 先启用并重启 API/Engine 的 LiveKit + MOSS 后端。
2. 用不可见测试订阅者确认 `agent-tts` 有非静音 PCM、无串房且 abort 生效。
3. 构建 `NEXT_PUBLIC_WEBRTC_AUDIO_ENABLED=true` 的独立 Web release。
4. 最后切换 `.web-current` 并只重启 Web。

不能先切前端再启后端。

## 网络与 TLS 门禁

当前 LiveKit config 使用：

```yaml
port: 7880
rtc:
  tcp_port: 7881
  port_range_start: 7882
  port_range_end: 7893
  use_external_ip: false
  node_ip: 117.50.218.251
```

因此必须二选一：

1. 云安全组和主机防火墙放通 `7882–7893/udp` 全范围；或
2. 修改 LiveKit 为明确的单一 UDP mux 端口，再重启并重新做 ICE 验证。

只探测 `7882/udp` 不足以证明当前端口范围配置可支持多个浏览器。还需确认：

- `443/tcp` 的 `/rtc` 使用受信任证书并支持 WebSocket upgrade。
- `7881/tcp` 公网可达，作为 ICE/TCP fallback。
- `7880/tcp` 不应直接向公网开放；浏览器应只使用 `wss://117.50.218.251/rtc`。
- Chrome 和 Safari 的实际 selected candidate pair 不是仅 localhost/host 内部候选。

## 最小验收门禁

启用用户可见 WebRTC 前至少满足：

- `/api/health/ready` 为 200，但同时单独验证 LiveKit；readiness 本身不覆盖 LiveKit。
- 登录用户和公开观众取得的 token 均为 `canSubscribe=true`、`canPublish=false`、`canPublishData=false`。
- API 进程没有 `agent-audio:<room>` participant；每个房间只有 Engine 的一个 publisher。
- Chrome 和 Safari 都能订阅 `agent-tts`，Safari 手势解锁后后续发言不再重复弹出。
- `tts_first_pcm -> 浏览器首个非静音` P95 不超过 0.6 秒。
- `llm_first_text_delta -> 浏览器首个非静音` P95 不超过 2.5 秒。
- pause/terminate 后 P95 不超过 250ms 停止可闻音频，且旧 generation 不恢复。
- 3 场并发无串房、无持续 backlog、无 Engine/SFU RSS 单调增长。
- 旧 WS PCM/WAV 路径仍可由显式回滚开关恢复。

## 回滚步骤

### 最快用户音频回滚

1. 把 `.web-current` 切回发布前、编译时 `NEXT_PUBLIC_WEBRTC_AUDIO_ENABLED=false` 的 Web release。
2. 重启 `jixia-web`。
3. 确认新打开页面不再请求 RTC token，并恢复 WS/WAV 兼容播放。

前端回滚优先于停止 LiveKit，可最快恢复用户声音。

### 后端回滚

在生产 `.env` 恢复：

```dotenv
WEBRTC_AUDIO_ENABLED=false
WEBRTC_AUDIO_BACKEND=websocket_pcm
REALTIME_VOICE_PIPELINE_ENABLED=false
REALTIME_VOICE_BACKEND=lighttts
MOSS_TTS_REALTIME_ENABLED=false
LIGHTTTS_STREAMING_ENABLED=false
LIGHTTTS_BISTREAM_ENABLED=false
ENGINE_ENABLED=false
```

保留当前既有值：

```dotenv
LIGHTTTS_GLOBAL_GATE_ENABLED=true
LIGHTTTS_MAX_ACTIVE=1
```

然后只重启 API 和 Engine。正在运行的 Agent/TTS generation 会被安全中断，因此应优先选择无活动比赛窗口；紧急故障时以停止错误音频为先。

LiveKit SFU 可以继续运行，不会影响回滚后的 WS/WAV 音频。确认无订阅者和 publisher 后再单独停止 SFU，避免把 SFU 生命周期和应用回滚绑在一起。

### Python 运行时回滚

部署前记录 `.venv` 原链接。若新 SDK 导致 API/Engine 启动失败，恢复上一版本化虚拟环境链接并重启 API、Engine、Worker。不要删除新环境，保留日志和依赖快照供复盘。

## NO-GO 条件

出现任一项，不得打开用户可见 WebRTC：

- `LIGHTTTS_BISTREAM_ENABLED=true`。
- Python LiveKit SDK 或 Web `livekit-client` 缺失。
- API 与 Engine 同时出现 `agent-audio:<room>` publisher。
- 仅放通 `7882/udp`，但 SFU 仍配置 `7882–7893` 端口范围。
- 没有可用的 MOSS-Realtime 增量 endpoint，却把前端 WebRTC flag 编译为 true。
- Token 可发布音频或数据。
- Safari/Chrome 首声、打断、3 场并发门禁未通过。
- 没有已经构建好的 WebRTC-off Web release 和旧 Python 虚拟环境可供回滚。
