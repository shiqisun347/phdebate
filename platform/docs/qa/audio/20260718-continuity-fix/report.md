# 2026-07-18 实时语音卡顿与提前结束修复报告

## 已确认根因

1. 自由辩论单轮配置为 40 秒，但生成音频实际可达 44–62 秒。比赛引擎先检查轮次超时，再检查正在播放的音频，因此会发送 `free_turn_elapsed`，同时取消 TTS、清空服务端和浏览器队列，造成句中硬切。
2. 独立 Nginx 进程无权写入 `/var/lib/nginx/proxy`。约 520 KB 的 LiveKit 客户端脚本在代理缓冲写临时文件时失败，HTTPS 响应只发送约 64 KB 就关闭，导致浏览器偶发无法加载 RTC 模块。
3. 用户在 RTC 连接完成前点击“开启声音”时，旧实现没有把可信手势传给稍后创建的 `AudioContext`，可能出现 Opus 已到达但上下文仍为 `suspended`。
4. Agent 上游达到 token 上限时，旧实现会把不完整文本直接交给 TTS；系统随后把截断内容当作正常发言完成。

## 已实施修复

- 自由辩论中，已开始发布的 AI 流式语音不再被名义轮次边界撤销；最终音频按真实自然终点播放，完成后再换边。
- 调整比赛引擎检查顺序：权威 `playing` 音频优先完成，不再在同一次 tick 中被标记为 `timed_out`。
- Nginx 禁止代理响应落入不可写临时目录，页面代理改为流式转发，并启用 JavaScript/JSON/CSS/SVG gzip。
- LiveKit 播放上下文在首个网络等待前创建；早期用户点击可立即解锁，连接完成后自动补做 `startAudio()`。
- LiveKit 可选预连接增加 2.5 秒上限及分阶段诊断，不允许静默卡死。
- Agent 对 `length` 截断执行同一发言续写；仍不完整时返回错误，不再把半句保存为最终发言。
- LiveKit 发布器不再在 MOSS 短暂无 PCM 分片时主动插入静音，避免真实语音排在合成静音之后。
- 比赛取消恰逢 MOSS `abort/released` 间隙时，会通过 `/health/ready` 核对 `active=0`、`orphan_count=0` 后自动归还端点信号量；不再依赖重启比赛引擎恢复后续房间。

## 验收结果

- 浏览器正式门禁：`PASS`。
- Agent 首个可读字符 → 浏览器首个非静音样本：`2277.5 ms`，满足 `< 3000 ms`。
- 服务端首个音频采样 → 浏览器首个非静音样本：`222.5 ms`。
- RTC 事件 → 浏览器首个非静音样本：`348.7 ms`。
- 传输：`audio/opus`，测试采样丢包 `0`，浏览器 `AudioContext=running`。
- 大型 LiveKit 脚本：gzip 后网络传输约 `134 KB`，解压后 `520292` 字节完整，未再出现 64 KB 截断。
- 自由辩论长音频：名义轮次 `40 s`，实际音频 `44.32 s`；结果为 `completed`，未产生 `speech.timed_out`，自然结束后换边。
- 自动化测试：前端音频/舞台相关 `92 passed`；后端自由辩论边界回归 `4 passed`；MOSS 取消、丢失释放帧与严格隔离回归 `4 passed`。

## 证据

- `browser-fresh-uncontended-pass.json`：最终首音与 Opus 门禁数据。
- `browser-fresh-uncontended-pass.png`：最终浏览器画面。
- `browser-after-nginx-and-cutoff-fix.json`：Nginx 修复后首次恢复 RTC 的诊断数据；该次因加入了已等待较久的旧生成任务，不作为首音 SLA 结论。
