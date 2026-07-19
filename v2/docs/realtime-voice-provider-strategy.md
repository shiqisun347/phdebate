# 实时语音 Provider 策略：生产只使用 MOSS-Realtime

更新时间：2026-07-19  
生产服务器：`117.50.192.216`

## 当前唯一生产路径

```text
Debate Agent SSE 正文 delta
        ↓
稳定短语提交器
        ↓
同一轮 MOSS-Realtime 双向 WebSocket
        ↓
24 kHz PCM16（仅服务器内部）
        ↓
LiveKit 48 kHz / 20 ms / Opus
        ↓
浏览器连续 AudioWorklet 播放
```

- 生产 TTS Provider 固定为 `moss_realtime`。
- 每轮只建立一个 TTS session、voice、generation，不按句重连。
- 首个正文短语立即进入 MOSS，不等待 Agent 完整回复。
- 暂停、跳过和终止必须同时取消 Agent、MOSS、服务端 PCM、LiveKit 发布队列和浏览器
  未播放样本。
- 服务器内部保留 PCM/WAV 作为审计原件；公网实时音频只使用 WebRTC Opus。

详细协议、延迟和恢复门禁见
[`realtime-voice-rebuild.md`](realtime-voice-rebuild.md)。

## 已淘汰方案

以下方案不参与新比赛，也不能作为静默回退：

- LightTTS / CosyVoice；
- 火山引擎双向流式 TTS；
- 分段 MP3、完整文本后合成和逐句重建连接；
- 浏览器直接接收公网 PCM；
- 在一轮已经播放后切换 Provider 或拼接另一音色。

历史实现、基准和事故材料只保留在 `docs/qa`、`docs/incidents` 或归档脚本中用于追溯，
不得被 README、管理页面或部署说明描述为可选生产能力。

## 冻结边界

当前 MOSS、LiveKit 与浏览器播放链路已经形成可靠基线。非音频功能改造不得修改：

- MOSS Gateway、模型参数、Prompt 和固定音色；
- Agent delta 到 MOSS 的增量会话；
- PCM、重采样、LiveKit 发布和中断队列；
- 浏览器 AudioWorklet、播放缓冲和 generation 隔离。

受保护文件与 SHA-256 由
[`operations/PROTECTED_REALTIME_AUDIO.md`](operations/PROTECTED_REALTIME_AUDIO.md)
管理。只有用户明确要求修改语音链路，并重新通过首声、长发言、连续性、中断、音色和
多人并发门禁后，才能生成新的可靠基线。

## 发布检查

生产发布后执行：

```bash
./deploy/verify_gpu_voice_runtime.py
.venv/bin/python scripts/verify_moss_only_production.py \
  --env-file .env \
  --supervisor-dir /etc/supervisor/conf.d
```

第二个检查只输出非敏感开关和服务名，不输出 API Key、数据库地址或 LiveKit 凭据。
它会拒绝重新启用 LightTTS 流式合成、CosyVoice/火山 TTS Supervisor 服务或非 LiveKit
浏览器传输；仍存在但不可达的旧配置字段只作为清理债务报告。

## 当前限制

- 单张 RTX 3090 当前只允许一个活跃 MOSS 合成任务；多房间可同时在线，但 AI 发言按
  有界队列排队。
- 需要真正并行的多场 AI 语音时，应增加独立 GPU endpoint，不在同一模型进程内放宽
  未验证的并发。
- 现存 LightTTS 兼容代码仍位于受保护音频基线中。它保持不可达，待下一次完整音频发布
  门同时完成通用命名迁移和删除，不能为了“代码看起来干净”而绕过可靠性验证。
