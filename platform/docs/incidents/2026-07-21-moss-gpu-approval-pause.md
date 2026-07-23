# 2026-07-21 MOSS GPU 审批不匹配导致比赛安全暂停

## 影响

- 生产房间 `296152` 在开场语音准备阶段自动安全暂停。
- 用户看到“比赛遇到临时服务异常，已自动安全暂停”。
- 比赛进度、席位、服务配置快照和事件日志均完整保留，没有跳阶段或丢失发言。
- 同一原因也影响此前暂停的房间 `117519`；本次仅对仍有用户在线的 `296152` 执行受控恢复。

## 根因

生产 MOSS-TTS-Realtime 启动脚本仍审批旧 RTX 3090 VBIOS
`94.02.26.88.08`，实际服务器显卡为：

- 型号：NVIDIA GeForce RTX 3090
- 显存：24576 MiB
- VBIOS：`94.02.42.00.02`

硬件准入因此拒绝启动，主 API 正确地把 `moss_tts_not_ready` 识别为可重试服务异常，暂停比赛而不是继续推进。

## 修复

1. 核对 GPU UUID、型号、显存、VBIOS 和独占状态。
2. 只更新生产启动器及其部署测试中的已审批 VBIOS，不放宽 GPU 准入规则。
3. 运行部署测试与 shell 语法检查。
4. 重启单个 `jixia-moss-realtime` 进程，并等待当前 GPU 指纹下首次 TorchInductor 编译完成。
5. MOSS 冷启动约 21 分钟后，主 API 健康检查确认：模型已暖机、一个 endpoint 就绪、无等待任务和孤儿任务。
6. 管理员以唯一幂等键对房间 `296152` 执行“重试当前异常步骤”。

对应代码提交：`9564519ec786cbcda9b0f04bc00fc2db112ed818`。

## 恢复验证

- 房间状态：`paused -> preparing -> running`。
- 故障原因已清空，服务配置快照仍存在。
- 12 条开场/阶段提示音全部生成，随后正常进入比赛阶段。
- 第一位 AI 辩手产生 `audio.rtc.started`，使用 24 kHz 实时音轨。
- 第一段 AI 发言持续 106.56 秒，产生 `speech.audio.ready`、`speech.completed` 和 `stage.completed`，没有中途断流或再次暂停。
- 下一阶段自动开始，第二位 AI 辩手也成功产生新的 `audio.rtc.started`。
- 数据库、Redis、引擎、Worker、FunASR、MOSS、存储和备份健康检查全部通过；Worker 队列与死信均为 0。

## 音频基线保护

本次没有修改冻结的 TTS、实时音频传输或浏览器播放实现。可靠音频基线保持：

```text
audio_baseline_verified files=82 fingerprint=3219138b22878e766c94b9a1fa0e422211a6f27dd74afbbb93a8e45f7e295285
```

## 后续运维要求

- 更换显卡、更新 VBIOS 或迁移服务器后，必须在开放比赛前执行 MOSS GPU 指纹准入与完整暖机。
- 首次 TorchInductor 编译应作为部署阶段任务完成，不能把 20 分钟级冷启动留到真实比赛开始后。
- 只有聚合健康检查显示 `model_warmed=true` 且 `ready_endpoints>=1` 时，生产入口才允许创建需要 AI 语音的比赛。
- 准入失败时保持当前安全暂停策略；不得自动跳过语音或推进比赛。
