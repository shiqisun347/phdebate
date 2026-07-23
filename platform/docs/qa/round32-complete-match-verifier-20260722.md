# Round 32：完整比赛验收脚本审计与重建

日期：2026-07-22

## 结论

旧 `verify_complete_match.py` 不能证明一场比赛真的完成。它只创建一名真人、覆盖少量 1v1 路径，并且在自由辩论中等待一个服务端不会自动创建的“真人 active_speech”，随后又调用 `speech/start`。脚本本身就可能卡死，不能据此判断产品正常或异常。

本轮将脚本改为通过真实 REST + 房间 WebSocket 驱动以下场景：

- `1v1-human-ai`：一名真人与一名 AI。
- `1v1-two-human`：两个独立真人浏览器会话。
- `4v4-mixed`：四个独立真人会话占据 `aff_1 / neg_1 / aff_2 / neg_2`，其余四席由 AI 自动补齐。

默认覆盖三轮自由辩论，确保至少出现“真人发言 → 对方举手 → 三秒窗口选中 → 对方发言”，再通过房主的正式 `skip` 控制跳过剩余自由辩论时长。随后仍必须经过全部总结阶段和真实 AI 裁判，最终状态必须为 `completed`。如需验证完整 240/300 秒自由辩论计时，使用 `--full-free-duration`。

## 已修复的验收脚本问题

1. 旧脚本没有维持参赛者 WebSocket。真人席位在服务端仍是 `connected=false`，自由辩论举手必然被拒绝。新脚本为每名真人维持独立房间 WebSocket，并在发言前复核在线状态。
2. 旧脚本错误地等待“系统自动创建真人发言”。正确协议是：举手被选中后房间返回 `can_speak=true`，真人浏览器再调用 `speech/start`。新脚本按该协议执行。
3. 旧脚本只有一名真人，无法覆盖抢座、多人准备、跨设备控制权和对方举手。新脚本支持两个真人及 4v4 四真人混合阵容。
4. 旧脚本没有把临时账号标成测试数据。若直接扩展到正式赛，会污染赛事列表与排行榜。新脚本在创建房间前通过管理员接口把所有临时账号标记为 `is_test_account=true`；远程环境禁止绕过此步骤。
5. 旧脚本在自由辩论第一次真人发言后很快跳过，没有证明双边轮换和举手队列。新脚本默认至少覆盖三轮。
6. 旧脚本没有验证 AI 是否真的开始播放，只验证阶段最终变化。新脚本要求实时房间投影至少出现一次 AI `playback_started_at`。
7. 旧脚本只检查结果页 HTTP 200 和发言数量。新脚本核对全部阶段、固定发言、自由辩论、事件序号、异常事件、未完成发言、裁判状态、胜方和 `match.completed`。
8. 旧脚本没有验证房主暂停/恢复后的倒计时冻结，也没有验证真人 60 秒窗口内断线返回。新脚本默认各执行一次。
9. 旧脚本遇到异常暂停立即失败，却没有证明用户是否能恢复。新脚本对每个阶段最多执行一次正式 `retry` 或 `resume`；同一阶段再次暂停即失败，避免把反复故障伪装成成功。
10. 新脚本始终在 `finally` 中关闭参与者 WebSocket，并取消或终止未完成的测试房间，避免占用最多五个活动房间的产品容量。

## 成功门槛

一次通过必须同时满足：

- `/api/health/ready` 返回 `ok=true`。
- 开赛后在默认 15 秒内进入 `opening`；超过即判定“主持提示音未做到全局预生成/直接复用”。
- 预期模板的每个 `stage.started` 都存在。
- 所有固定 speech 阶段都有一条 `completed` 发言。
- 自由辩论至少保存 `--free-turns` 条发言，默认 3 条。
- 真人+AI 场景同时包含真人和 AI 发言，且实时观察到 AI 实际开始播放。
- 比赛和结果页均为 `completed`，裁判卡为 `approved`，winner 为 `aff / neg / draw`。
- 事件日志不存在 `provider.failed`、`engine.quarantined`、`judge.review_required`。
- 事件 `seq` 连续，不存在遗留的 `speaking / synthesizing / playing / failed` 发言。
- 验收房间被标记为测试数据，不进入公开赛事统计和排行榜。

## 生产验证前置条件

1. 生产 `/api/health/ready` 必须全绿。2026-07-22 本轮只读检查结果为 `ok=true`、无失败检查项、当前无比赛处理任务。
2. 本机或执行容器必须设置 `PHDEBATE_ADMIN_ACCOUNT` 和 `PHDEBATE_ADMIN_PASSWORD`。值不得写入命令、日志或本文件。
3. 至少有一个活动房间名额。系统上限为 5；脚本不会关闭或修改其他人的房间。
4. Agent、实时语音、裁判、Redis、PostgreSQL、对象存储均应处于 readiness 通过状态。
5. 生产执行必须显式添加 `--allow-production`。远程环境禁止 `--allow-unclassified-test-data`。
6. 先执行 1v1，再执行 4v4；不要并行运行两个完整语音验收，以免把单路 MOSS/TTS 资源排队误判为产品故障。

## 推荐命令

在 `platform/` 目录执行：

```bash
PYTHONPATH=apps/api:. .venv/bin/python scripts/verify_complete_match.py \
  --base-url https://117.50.192.216 \
  --allow-production \
  --scenario 1v1-human-ai \
  --output docs/qa/results/complete-1v1-human-ai.json
```

再验证两名真人：

```bash
PYTHONPATH=apps/api:. .venv/bin/python scripts/verify_complete_match.py \
  --base-url https://117.50.192.216 \
  --allow-production \
  --scenario 1v1-two-human \
  --output docs/qa/results/complete-1v1-two-human.json
```

最后验证 4v4 混合阵容：

```bash
PYTHONPATH=apps/api:. .venv/bin/python scripts/verify_complete_match.py \
  --base-url https://117.50.192.216 \
  --allow-production \
  --scenario 4v4-mixed \
  --timeout-seconds 420 \
  --output docs/qa/results/complete-4v4-mixed.json
```

完整自由辩论时长验收应单独执行：

```bash
PYTHONPATH=apps/api:. .venv/bin/python scripts/verify_complete_match.py \
  --base-url https://117.50.192.216 \
  --allow-production \
  --scenario 1v1-human-ai \
  --full-free-duration \
  --timeout-seconds 420
```

## 本地证据

- `scripts/tests/test_verify_complete_match.py`：9 项通过。
- 覆盖远程写入保护、测试数据保护、三种阵容、诊断脱敏、完整结果门槛、异常事件、缺失阶段、未完成发言、裁判待复核和音频样本合法性。
- `python -m py_compile scripts/verify_complete_match.py`：通过。

## 仍需在部署后取得的证据

本轮没有把尚未部署的脚本直接复制到生产服务器，也没有使用未分类账号运行正式赛。因此以下项目仍需在核心修复部署后依次执行并保存 JSON：

- 真实 1v1 人机完整生命周期。
- 真实 1v1 双真人完整生命周期。
- 真实 4v4 四真人 + 四 AI 完整生命周期。
- 至少一次不跳过 240 秒自由辩论的计时验收。
- 浏览器侧声音连续性和字幕视觉验收不由本脚本代替，仍需 agent-browser/真实浏览器音频探针完成。
