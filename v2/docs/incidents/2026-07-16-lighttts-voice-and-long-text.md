# 2026-07-16 LightTTS 多音色、长文本与自由辩论计时修复

## 影响

- `debate_voice_2`、`debate_voice_3`、`debate_voice_4` 与全局提示文本不匹配，短句可能出现无意义或串入提示音内容的语音。
- 接近正式立论长度的单次请求会被 LightTTS 以 HTTP 417 拒绝；部分较长请求虽返回 200，实际音频仍可能缺失中间内容。
- 自由辩论中的 AI 在 Agent 与 TTS 准备期间继续消耗单轮和阶段计时，长文本可能在播放前即被判定超时。
- LightTTS 实际以 `running_max_req_size=1` 运行，应用层曾允许两路并发，压力下会出现请求挂起或服务端 500。

## 根因

1. 四个提示 WAV 实际包含不同文本，旧实现却始终发送同一个 `LIGHTTTS_PROMPT_TEXT`。
2. 应用没有适配 LightTTS 的单请求长度边界，也没有验证“200 但不完整”的服务行为。
   30 字边界还可能把句末标点拆成纯标点分段，触发 LightTTS 内部 `split_paragraph` 的 `IndexError`。
3. 固定发言阶段会在音频就绪后重置计时，自由辩论阶段却沿用准备前的截止时间。
4. V2 调度并发上限没有与当前 LightTTS 进程的实际容量保持一致。

## 修复

- 新增 `LIGHTTTS_PROMPT_TEXTS` 音色到提示逐字稿映射，并在默认音频回退时同步回退到对应提示文本。
- 将文本按标点保序切成最多 30 个可见字符的分段；HTTP 417 时继续自适应二分。
- 校验所有分段 WAV 参数，原子拼接，并在段间加入 120 ms 静音；取消或失败时清除 `.part` 和分段文件。
- 自由辩论 AI 准备期间冻结服务端总计时和单轮计时；播放开始后恢复权威截止时间。
- 观战与辩手舞台显示“AI 准备中 · 计时暂停”，不再显示即将归零的误导状态。
- 暂停、恢复、跳过、终止和服务失败均会正确保存或清除准备态。
- 将生产 LightTTS 并发校准为 1；对 408、425、429、5xx 和网络超时执行最多 3 次取消感知重试。
- 纯标点尾段会并回前一语音段，整段没有可发音字符时在应用层直接拒绝。
- 启用 LightTTS 官方 `--health_monitor`，真实推理健康检查连续失败时由 Supervisor 自动拉起服务。

## 验证证据

- 四音色两组短句：字符覆盖与序列一致性均为 `1.00`。
- 四音色 89 字立论：序列一致性均为 `1.00`。
- 四音色约 250 字立论：序列一致性 `0.97–1.00`，无 417、无内容段落丢失。
- 浏览器 ASR 桥接：真实 LightTTS → FunASR → WebSocket → 字幕落库通过。
- 自由辩论准备计时：模拟提供商耗尽原截止时间后，阶段仍保留至少 118 秒、单轮至少 43 秒。
- 多场真实语音赛程：2 场 4v4 与 2 场 1v1 同时运行，8 次真实 LightTTS 发言、一次合成中暂停/恢复，容量校准前后的成功运行分别耗时 74.29 秒和 80.25 秒，跨房间内容和音频事件污染为 0。
- 故障注入：LightTTS 或 FunASR 分别停止时 readiness 返回 503 并准确标记故障服务；恢复后自动回到 200。
- 完整质量门禁：后端 146 项、前端 69 项、Next.js 生产构建、Python 与 npm 漏洞扫描全部通过。
- 生产根路径 Playwright：桌面与手机共 6 项通过。

## 部署与回滚

- 当前 Web 发布：`runtime/web-releases/20260716-ai-preparation-clock`。
- 上一个 Web 发布：`runtime/web-releases/20260716-root-cutover`。
- 环境配置修改前备份：服务器根目录下 `.env.before-voice-prompts-<UTC 时间>`。
- LightTTS 容量修改前备份：`.env.before-lighttts-capacity-<UTC 时间>`。
- LightTTS Supervisor 修改前备份：`/etc/supervisor/conf.d/jixia-debate.conf.before-lighttts-health-monitor-<UTC 时间>`。
- 回滚时切换 `.web-current` 到上一发布并恢复对应 `.env` 备份，然后重启 V2 API、engine、worker 和 web。

## 后续运维要求

- 新增或替换提示 WAV 时，必须同时维护该音色的准确逐字稿。
- 上线新音色前运行四音色往返脚本，并要求短句序列一致性不低于 0.90。
- 不应提高 30 字分段上限，除非目标 LightTTS 版本通过长文本完整性复验。
