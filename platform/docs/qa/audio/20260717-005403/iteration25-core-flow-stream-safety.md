# Iteration 25：核心流程、流式边界安全与生产发布回归

更新时间：2026-07-17 21:24 CST。生产 release：`20260717T2115-core-flow-stream-safety`。

## 结论

- 4v4 学生主链的认证回跳、创建入口、八席信息、观战只读、参赛者主动放弃、管理员恢复真人席位约束和历史比赛危险重试已完成修复、完整门禁、部署与生产回归。
- LightTTS 流式候选的两个具体 P0 已在代码层关闭：future/live-edge 游标由服务端按真实 growing WAV 可用量回退最多 400ms；低于 400ms 的最终尾音在 Worklet `end` 时会 prime 并 drain。
- 生产 `LIGHTTTS_STREAMING_ENABLED` 仍保持关闭/default false，LightTTS 仍为 `1 active + 2 waiting`。因此本轮不宣称生产已达到超低首声，也不宣称支持 2–3 场实时并发。
- 生产同配置 Debate Agent 不写业务数据库的直连流式实测确认默认 reasoning 是主要瓶颈。默认样本首可见正文 23,515ms、完整 24,966ms，303 个流块中 281 个为 reasoning；单请求关闭 thinking 后首正文 2,163ms、完整 3,754ms，分别改善约 91% 和 85%。该 fast path 已在后续 [Iteration 26](iteration26-agent-fastpath-deployment.md) 发布，生产 SSE canary 首正文 3,375.9ms、完整 4,673.6ms。

## 实现范围

- API 音频 WebSocket 按实际 WAV/part 文件长度计算 available samples/frame count；future/equal-live-edge 请求 clamp 到真实 live edge 前最多 10 个 40ms frame，并在 `audio.start` 返回权威 `start_seq/start_pts_samples`、requested seq、available samples 和 clamped。
- Web 流播放器以服务端 `audio.start` 的权威 seq/PTS 重设期望游标；AudioWorklet 在短尾 `end` 时启动并耗尽缓存。
- 自由辩论流式音频完成只约束单轮 `playback_ends_at`，不覆盖整段自由辩论 deadline；新增直接回归。
- `/watch` 严格只读；房主使用显式 control 页面；retry 增加确认。
- started match 的非房主可执行“放弃本场并由 AI 接替”；本人仍有活跃发言时拒绝，房主不能用该动作逃离整场责任。
- 管理员恢复 AI 接替的真人席位时取得 participant lock，并复验跨房真人唯一性。
- 登录/注册互转保留安全 `next`；匿名房号加入保留精确 lobby 回跳；匿名创建返回后重开原赛事创建入口；AI 接替后的旧参赛者进入只读 watch。
- 生产历史 Match 若 `service_snapshot={}`，retry 返回 409，禁止静默回退到陈旧环境 Provider。

## 自动化质量门

- Web 定向核心回归：112/112 passed。
- Web 全量：26 files / 190 tests passed。
- Next.js production build：通过。
- API `test_platform + test_multi_room_simulation`：160/160 passed。
- API 全量：295 passed，22 warnings，89.70s。
- 首次未设置 `PYTHONPATH=.:apps/api` 的命令发生 `scripts` collection error；修正运行环境后全量通过，不属于产品测试失败。

## 部署与回滚

第一次尝试 `20260717T2110-core-flow-stream-safety`：API shadow 与 schema 0019 实际通过，但部署 harness 错把 Uvicorn 正常的 `Session terminated…` 尾部日志当成失败，并在生产切换前触发回滚。已降级回 0018、恢复源码与旧 Web、移除 migration、重启服务；连续 3 次 ready、schema0018、旧 Web 指针和服务状态均正常。这是部署脚本误判，不是产品故障。

第二次 `20260717T2115-core-flow-stream-safety` 成功：Alembic 0018→0019、远端 Linux Next build、API 12350 shadow、Web 12349 shadow、原子 Web symlink 切换、API/Engine/Worker/Web 重启。生产 ready 5/5，gate 0/0，`active_match_processing=false`，API/Engine/Web stderr 增量均为 0。

- 发布包：`/tmp/phdebate-20260717T2115-core-flow-stream-safety-source.tar.gz`
- 发布包 SHA-256：`3aa57c365b62d39bb4b11917a109a4e7e275c29c88eff951ba27f1ab704a7019`
- 源码回滚包：`/home/ubuntu/sunsq/phdebate/runtime/deploy-backups/20260717T2115-core-flow-stream-safety-before-source.tar.gz`
- 回滚包 SHA-256：`94f99cc103a3afe676e88c9d85acc0b4d4015129f51fdd6cbdb4f77cec59cbad`
- 前一 Web 指针记录：`/home/ubuntu/sunsq/phdebate/runtime/deploy-backups/20260717T2115-core-flow-stream-safety-before-web.txt`
- 前一 release：`20260717T1640-audio-boundary-playback`
- DB 备份：`auto-20260717T130406Z.dump`，178093 bytes，SHA-256 `fdce1912f1c6d032bb9013300c59bcc06ef62345c14478670b6bf60e55052178`

## 生产浏览器回归

- Chrome 首页：登录态、4v4 人机辩论正式赛、1v1 训练赛和 0 场进行中正常。
- Chrome 4v4 创建弹窗：8 席、辩题和 AI 补位说明正常；未提交，未创建比赛。
- `/login?next=/rooms/381526/lobby`：登录→注册→返回登录均保留精确安全 next。
- 房 764886 结果：正方胜 82:76，11 条发言、93 个事件、8 席个人分、逐字稿和录音完整；Chrome warning/error 为 `[]`。
- Computer Use 使用 Chrome AX 树独立复核赛果和赛事大厅；未使用 AdsPower/SunBrowser。

截图：

- [TC-1021 首页](../../screenshots/20260717-005403/TC-1021-Iteration25-Chrome-发布后4v4首页.png)
- [TC-1022 4v4 创建弹窗](../../screenshots/20260717-005403/TC-1022-Iteration25-Chrome-4v4创建弹窗未提交.png)
- [TC-1023 登录注册回跳](../../screenshots/20260717-005403/TC-1023-Iteration25-Chrome-登录注册保留房间回跳.png)
- [TC-1024 Chrome 4v4 赛果](../../screenshots/20260717-005403/TC-1024-Iteration25-Chrome-发布后4v4赛果完整.png)
- [TC-1025 Computer Use 4v4 赛果](../../screenshots/20260717-005403/TC-1025-Iteration25-ComputerUse-Chrome-4v4赛果.png)
- [TC-1026 Computer Use 赛事大厅](../../screenshots/20260717-005403/TC-1026-Iteration25-ComputerUse-Chrome-发布后赛事大厅.png)

## INCIDENT-001：生产观战页误操作

坐标型浏览器测试 `/rooms/278571/watch` 时误触房主“重试异常步骤”，产生非预期生产写操作；该场随后被终止，最终为 `terminated / seq 115`。retry 返回 400，根因是历史 Match `service_snapshot={}`，旧代码回退到陈旧环境 Agent endpoint 且缺 gateway secret。

纠正措施已经部署：watch 严格只读，只提供显式 control 链接；retry 增加确认；空 snapshot 的生产 retry 返回 409；不再操作该房。报告中更早的“paused / seq108 / 未操作”只代表当时截点，不代表最终状态。

## 仍开放的核心门

1. 历史 31 个生产 AgentTask 完整响应为 min 2.198s、P50 19.371s、P90 23.342s、P95 25.230s、max 27.245s、mean 18.757s；默认 thinking 明显不适合实时辩手。Qwen3 服务端 fast path 已通过生产 canary，仍需连续 P50/P95、赛内质量和 2/3 路并发验证。
2. 真实 LightTTS 既有 1/2/3 并发证据为：单路首块 P95 1.259s、RTF P95 0.878；2 路 6.333s/1.795；3 路 10.983s/2.651，近似串行，2–3 场实时 NO-GO。
3. 流式 feature flag 关闭；仍需真实 Chrome/Safari 的首声、欠载、短尾、断线追赶、自动播放手势和活动舞台弱网验收。
4. 服务端/MessagePort pacing、高水位与 played ACK、Engine SIGTERM lease drain、上游取消确认仍未关闭。

## Agent fast path 本地门禁

- 复用现有 `ModelPreset.reasoning`，无数据库迁移；Qwen3 旧 preset `{}` 缺省 `enable_thinking=false`，显式 bool 可覆盖。
- 只影响直连 LiteLLM 的 Qwen3 辩手；非 Qwen、Judge 和 `debate_api` fallback 不注入该参数。
- 客户端传入的 model、thinking 或 extra body 不能覆盖服务端 preset。
- 定向 Agent API：10 passed；Ruff 与 py_compile 通过；独立只读复审结论 GO。
