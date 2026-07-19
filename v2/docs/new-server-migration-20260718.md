# 新服务器迁移与部署验收记录

日期：2026-07-18  
目标服务器：`117.50.192.216`  
部署目录：`/home/ubuntu/sunsq/phdebate-v2`

## 结论

辩论平台 V2、独立 Debate Agent、PostgreSQL、Redis、比赛引擎、异步 Worker、LiveKit、FunASR、Nginx、备份和证书续期已经迁移并在新服务器运行。公开入口为：

- 辩论平台：`https://117.50.192.216/`
- Debate Agent 管理平台：`https://117.50.192.216/debate`
- 主平台健康检查：`https://117.50.192.216/api/health`
- Agent 健康检查：`https://117.50.192.216/debate/health`

MOSS-Realtime 的代码、固定模型、8 个音色、Gateway、LiveKit 接入和测试工具已经部署，但真实 3090 性能未达到生产门槛，因此未接入正式比赛。这是有意的安全门，而不是部署遗漏。

## 已完成迁移

- 旧服务器 PostgreSQL 数据分别以自定义 dump 备份并恢复到新服务器独立数据库。
- V2 和 Debate Agent 的 Alembic 迁移均已通过。
- pgvector 0.8.1 已安装，Agent 的 Prompt、Memory、请求日志和配置数据可正常访问。
- 前端使用 Node.js 22 生产构建并通过 release 目录切换运行。
- Debate Agent 内部地址已改为新服务器本机服务；密钥保持加密/脱敏，没有写入本文档。
- FunASR 已部署并通过真实 WAV 识别；LiveKit 已启动并由 V2 使用房间级配置访问。
- HTTPS 使用包含 `117.50.192.216` IP SAN 的可信短期 Let’s Encrypt 证书，并配置高频自动续期。
- 旧服务器未删除，保留为数据与运行时回滚来源。

## 数据校验

浏览器验收产生的临时管理员、学生和房间已删除。清理后新服务器与迁移源核心计数一致：

| 数据 | 数量 |
|---|---:|
| 用户 | 6 |
| 房间 | 12 |
| 房间席位 | 72 |
| 比赛事件 | 679 |

旧比赛、事件和席位没有被测试数据覆盖或重写。

## 功能验证

- 主平台首页、注册、登录、个人中心、赛事详情、创建房间、认领席位、准备、观战和移动端布局均完成浏览器冒烟测试。
- `/admin` 和 `/admin/agent-access` 完成管理员权限与 RESTful-only 配置检查；主平台到本机 Debate Agent 的连接测试返回 200。
- Debate Agent 的自我介绍、正反方立论、质询、反驳、自由辩论、正反方总结共 8 个阶段均完成 SSE 流式测试，均收到正文和 `[DONE]`，没有输出 thinking 标签。
- Judge 接口完成真实调用，返回胜方、理由和结构化结果。
- MOSS 单条生成音频经 FunASR 回识与 44 字测试原文一致。
- 本地实时语音测试 141 项通过；MOSS Gateway 测试 54 项通过，Ruff 检查通过。

## MOSS-Realtime 验收

固定版本：

- OpenMOSS 源码：`ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af`
- MOSS-TTS-Realtime 模型：`6acbc7f161a0db71c291f2d0aaa9eee59334cab2`
- MOSS Audio Tokenizer：`3cd226ba2947efa357ef453bcad111b6eafba782`

RTX 3090 单路真实结果：

| 指标 | 首块 6 帧 | 首块 12 帧 | 发布要求 |
|---|---:|---:|---:|
| 首个非静音 PCM | 535 ms | 1063 ms | P95 ≤ 800 ms |
| RTF | 1.049 | 0.991 | P95 ≤ 0.65 |
| 块间间隔 P99 | 1156 ms | 1407 ms | ≤ 200 ms |
| 100 ms 缓冲 underrun | 6 次 | 1 次 | 0 |
| 最大 underrun | 624 ms | 513 ms | 0 |
| 打断到 `released` | — | 71.8 ms | P95 ≤ 250 ms |

另行测试首块 6 帧的异步 Decoder：首 PCM 948 ms、RTF 1.230、块间隔 P99 1215 ms、100 ms 缓冲发生 12 次 underrun。它可以稳定启动和释放会话，但在单张 3090 上性能更差，已回退为关闭。

打断测试通过，且打断后未收到旧 PCM、健康检查显示 0 个活跃会话和 0 个孤儿会话。连续播放与 RTF 门失败，因此总体结论为 NO-GO。

当前安全配置：

```text
REALTIME_VOICE_PIPELINE_ENABLED=false
MOSS_TTS_REALTIME_ENABLED=false
WEBRTC_AUDIO_ENABLED=false
```

MOSS Supervisor 项保持 `autostart=false` 且当前为停止状态，不占用 GPU。禁止在未重新通过门槛前仅修改开关启用。

## 后续发布条件

正式启用 MOSS 前至少需要：

1. 优化或替换 Talker/Decoder 实时调度，使 RTF P95 ≤ 0.65、块间间隔 P99 ≤ 200 ms、连续播放无 underrun。
2. 同配置完成单路 20 轮、8 音色 CER/吞字/重复/漂移与人工听感测试。
3. 完成 30 分钟稳定性、浏览器 WebRTC 首声和完整比赛打断恢复测试。
4. 先灰度 1v1 QA 房，再启用 4v4；任何阶段失败立即关闭开关并保留旧服务器回滚入口。
