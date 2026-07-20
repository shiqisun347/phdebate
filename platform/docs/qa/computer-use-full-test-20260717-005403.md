# 生产网站 Computer Use 全站测试报告

## 执行摘要

- 测试状态：Iteration 38 已按实时语音重建方案完成独立48GB GPU的固定上游原生session改进与复测。稳定inferencer和逐step低延迟bridge解决了重复编译与“先算完再解码”问题；GC-off单endpoint 20/20成功、20/20 final前首PCM、20/20 release，真实WS interrupt 5/5在102.249–196.276ms内完成reset/release。800ms连续时钟复算把最长卡顿从1359.964ms降至152.524ms，但首PCM P95仍1248.667ms、active RTF P95 0.977、gap P99最大1124.817ms，严格发布门继续NO-GO。最新门禁为API全量387/387、Gateway40/40、benchmark9/9、本轮实时链路定向115/115、部署静态检查和Ruff PASS；既有Web194/194与build PASS。
- 测试目标：`https://117.50.218.251`
- 执行方式：Codex Computer Use 操作浏览器，真实端到端测试
- 边界：按用户后续授权完成核心代码修复、自动化、备份、部署和生产回归；未删除真实数据、未使用 AdsPower/SunBrowser、未提高 LightTTS 并发、未启用流式 flag、未泄露凭据
- 最终结论：学生自助正式赛/训练赛主链和 WebRTC 完整WAV安全下行可继续小规模受控试用；管理模块按用户指令移出当前范围。OpenMOSS在独立48GB GPU上已证明真实正文增量→持久WS→逐step PCM、资源释放和gateway级250ms打断可运行，MOSS→LiveKit 800ms generation预缓冲也完成本地回归；但真实浏览器2.5秒、30分钟无卡顿、8音色质量和2–3个真GPU endpoint仍未验证，单路RTF/gap也不达严格门。核心实时语音继续NO-GO，所有实时生产开关保持关闭，未部署本轮候选。

## 测试环境和账号代号

- 测试开始时间：2026-07-17 00:54:03 CST（Asia/Shanghai）
- 测试结束时间：持续测试中；最近更新 2026-07-18 10:51 CST（Asia/Shanghai）
- 操作系统：macOS 15.7.2（Build 24G325，arm64）
- 浏览器：Google Chrome、Safari
- 浏览器版本：Chrome 150.0.7871.115；Safari 26.1
- 桌面窗口截图尺寸：1357×768（当前显示环境；未达到目标 1440×900）
- 窄屏测试尺寸：Chrome override `390×844 CSS px`（DPR 1）；无滚动舞台布局宽 390，长结果页因 15px 垂直滚动条布局宽 375，截图内容图为 375×812（TC-999–1015）。Safari 既有最小窗口 574×798
- User A：QA 新账号 `qa_cu_20260717_01`（Chrome 无痕；不记录密码）
- User B：QA 新账号 `qa_cu_0717_b4`（历史使用 SunBrowser 独立环境；用户 10:24 CST 指令后停止使用；不记录密码）
- System Admin：待识别（不记录凭据）
- Agent Admin：已有独立会话（不记录凭据）
- 多用户模拟方式：历史使用 Chrome 无痕 User A + SunBrowser User B；用户后续要求仅 Safari/Chrome。最终权威回归使用 Chrome 既有测试标签；Safari 无痕窗口被系统密码锁定，未尝试解锁。

## 覆盖范围与未覆盖项

- 当前阶段：生产仍为 Python runtime `20260717T1845Z-webrtc-safe`、Web release `20260717T1920Z-livekit-dualpc`、schema `0019_audio_streaming`；Debate Agent 当前主模型 `qwen-plus`。WebRTC/LiveKit 正式下行已开启并通过 Chrome/Safari 连接与声音解锁；`LIGHTTTS_STREAMING_ENABLED=false`、`LIGHTTTS_BISTREAM_ENABLED=false`、`REALTIME_VOICE_PIPELINE_ENABLED=false`、`MOSS_TTS_REALTIME_ENABLED=false` 继续保持。本地 MOSS Realtime 候选未部署；Nano real-GPU canary 已明确 NO-GO，并在测试后关闭、显存恢复。
- 最近进度保存：2026-07-18 10:51 CST
- 已创建：User A、User B、1v1 房间 `566139`、4v4 房间 `764886`、首轮部署后语音房间 `586109`、二次部署回归房间 `750374`
- 未覆盖项：System Admin 凭据实登、真人录音 CER、人工 MOS、独立灰度 LightTTS active=2/3 GPU 容量、浏览器 AudioWorklet 真实首声/欠载、Safari 精确移动视口、真实活动舞台弱网中断与 200% 缩放；管理模块已移出当前产品范围
- DevTools：Safari `Option+Command+I` 未打开 Web Inspector；Safari Console/Network 现象统一记录为“未获取”。Chrome 未手动打开 DevTools；Iteration 22 通过浏览器自动化日志接口读取 warning/error，结果为 0。

## 总体统计

| 指标 | 数量 |
|---|---:|
| PASS（219 条显式 TC 中） | 177 |
| FAIL（含历史失败证据、当前语音子门与容量门） | 35 |
| BLOCKED | 7 |
| NOT-RUN（已编号 TC） | 0 |
| P0（代码缺陷） | 0 |
| P1（未解决） | 8 |
| P2（未解决） | 24 |
| P3（未解决） | 2 |
| 明确可复现 TC 记录 | 219 |

> PASS/FAIL/BLOCKED 合计为 219 条明确 TC；FAIL 保留历史失败证据并包含当前语音子门和容量门，不等同于开放缺陷数。Iteration 38 的48GB原生session已经关闭重复编译、延迟解码和gateway打断子问题，但单路真实RTF/gap、浏览器实播、8音色质量和真c2/c3仍未关闭，因此生产真双流启用门仍为NO-GO/P1。

## 累计部署与生产回归状态（更新至 2026-07-17 17:10 CST）

| 项目 | 当前状态 | 生产回归证据 |
|---|---|---|
| 结构化登录错误显示 | 已修复 / PASS | Safari 错误登录显示“账号或密码错误”；`TC-901-部署后Safari登录错误-可读.png` |
| 匿名直接访问辩论页 | 已修复 / PASS | `/rooms/278571/debate` 自动转到 `/watch`；`TC-902-部署后匿名辩论页-重定向观战.png` |
| 4v4 个人评分与缺席评分 | 已修复 / PASS | 8 席个人分完整，User B 显示“暂无评分”；`TC-903`、`TC-905` |
| 结果时间线兼容性 | 已修复 / PASS | Chrome 与 SunBrowser 均从 50/93 加载至 93/93；`TC-904`、`TC-906` |
| 结果页音频 | 已修复 / PASS | Chrome/SunBrowser 真人与 AI 音频均可播放暂停；部署后连续切换只保留一条播放，离页停止且返回不复活；见 `TC-916-结果页音频互斥-修复后.jpg` 与 DEF-010 |
| 房间创建 | 部署中发现并已热修 / PASS | 新增精确 `/api/rooms`、`/v2/api/rooms` Nginx location，消除 301/307 循环；`TC-907` 为失败证据，`TC-908` 为修复后成功进入房间 586109 |
| 麦克风启动反馈 | 已修复 / PASS | 点击后立即显示“正在启动麦克风”；`TC-909-部署后麦克风启动-即时反馈.png` |
| 跨环节麦克风释放 | 已修复 / PASS | 人类回合结束后 Chrome 录音指示消失，随后 LightTTS 正常播放；`TC-910-部署后跨环节麦克风释放-LightTTS播放.png` |
| 静音/底噪 ASR 门控 | 二次修复 / PASS | 房间 750374 静音录音不再自动提交，进入人工核对；服务端 speech 保持空内容、无音频；`TC-913` |
| 本机录音状态文案 | 二次修复 / PASS | 房间 750374 录音时显示“当前设备正在发言”；`TC-912` |
| 非 WAV 音频时长 | 二次修复 / PASS | TC-911 WebM 实际解码 34.62 秒，不再依赖缺失的容器 duration；API 全量测试覆盖 WebM 解码/VAD |
| 语音过载恢复 | 已修复 / PASS | 可恢复 LightTTS 排队错误按 2/5/10 秒有界重试，AI 准备期冻结计时，TTS-only 重试不重生成论点，房主舞台/观战可幂等重试；TC-957/958 |
| 辩手页必要控制与 ASR 修正 | 已修复 / PASS | 房主在 debate/watch 均可通过 REST 暂停、继续或提前结束；辩手可显式结束并修改本轮 ASR；退出页面保留席位归属并阻止未提交录音丢失；文字完成而录音失败时只重试权威音频上传；TC-970–977 |

### 已部署版本与回滚点

- 首轮 Web release：`/home/ubuntu/sunsq/phdebate/runtime/web-releases/20260716T191929Z-qa-core`
- 源码回滚包：`/home/ubuntu/sunsq/phdebate/runtime/deploy-backups/20260716T191929Z-before-qa-core-source.tar.gz`
- 首轮 Nginx 回滚：`/home/ubuntu/sunsq/phdebate/runtime/deploy-backups/20260716T191929Z-before-jixia-nginx.conf`
- 房间精确路由热修前 Nginx 回滚：`/home/ubuntu/sunsq/phdebate/runtime/deploy-backups/20260716T194300Z-before-room-exact.conf`
- 当前 Nginx 候选归档：`/home/ubuntu/sunsq/phdebate/runtime/jixia-nginx-candidate-20260716T194300Z-room-exact.conf`
- 数据库部署前备份及恢复验证：`auto-20260716T184954Z.dump`，23 张表，Alembic `0012_agent_gateway_secret`

二次语音修复部署后：

- 当前 Web release：`/home/ubuntu/sunsq/phdebate/runtime/web-releases/20260716T200200Z-qa-speech2`
- 二次源码回滚包：`/home/ubuntu/sunsq/phdebate/runtime/deploy-backups/20260716T200200Z-before-qa-speech2-source.tar.gz`
- 二次切换前 Web 路径记录：`/home/ubuntu/sunsq/phdebate/runtime/deploy-backups/20260716T200200Z-previous-web.txt`
- 发布包 SHA-256：Web `f4a6d4bd7f5c9bff56d1c722b04e6eaaa56e0818b540a50da9fb06a6d16c4669`；Code `7f74184710e4eec0565185d199c18054776e3ce3ea20ac29fb0d503128ed8144`

结果页音频互斥修复部署后：

- 当前 Web release：`/home/ubuntu/sunsq/phdebate/runtime/web-releases/20260716T205546Z-qa-audio-exclusive`
- 源码回滚包：`/home/ubuntu/sunsq/phdebate/runtime/deploy-backups/20260716T205546Z-before-audio-exclusive-source.tar.gz`，SHA-256 `34fb5c298293184e35402ce6c89a9aa420768492df3312c2d8e6a04ff5a00211`
- 切换前 Web 路径记录：`/home/ubuntu/sunsq/phdebate/runtime/deploy-backups/20260716T205546Z-previous-web.txt`
- 质量门：结果页定向 `12 passed`，Web 全量 `97 passed`，TypeScript 与 Next.js production build 通过；12343 shadow 首页/结果页均 HTTP 200

SunBrowser 结果音频按需加载修复部署后：

- 当前 Web release：`/home/ubuntu/sunsq/phdebate/runtime/web-releases/20260716T211321Z-qa-audio-lazy`
- 源码回滚包：`/home/ubuntu/sunsq/phdebate/runtime/deploy-backups/20260716T211321Z-before-audio-lazy-source.tar.gz`，SHA-256 `43f1bc5fba5c0887e7b27f4313b8b610927913bc933f6ad4fcbc09c3054c754c`
- 切换前 Web 路径记录：`/home/ubuntu/sunsq/phdebate/runtime/deploy-backups/20260716T211321Z-previous-web.txt`
- 质量门：结果页 `13 passed`，Web 全量 `98 passed`，TypeScript 与 production build 通过；12343 shadow 通过；主站、结果页、API health 200，Web stderr 空

LightTTS Redis 全局门禁 canary 部署后：

- 部署时间：2026-07-17 06:05–06:14 CST；仅覆盖 TTS config/admission/provider/health/lifespan 与 verifier，不包含本地 0013/0014、Activity/Teacher API，生产 Alembic 仍为 `0012_agent_gateway_secret`。
- 回滚包：`/home/ubuntu/sunsq/phdebate/runtime/deploy-backups/20260716T220527Z-before-lighttts-gate-canary.tar.gz`，SHA-256 `b92e2e818e00da95ab2a2ceb2652892f0f54de244618f44d301ff0992c9e917b`；含部署前 `.env` 和 4 个被覆盖源文件。
- 发布包：`/tmp/phdebate-lighttts-gate-canary-0605.tar.gz`，SHA-256 `298d6b343e8075366ce9c46eb172bc050d36c0606253f33e38416b094f915250`；`main.py` 由生产版本仅合入 shutdown drain，未带入 teacher router。
- 启用前：无 `preparing/running/judging` 房间，无生产 admission key；真实 Redis shadow 三进程 `maximum_active=1`，response-loss 可恢复且临时键清零。
- 真实 LightTTS canary：启用 flag 后同时提交 2 个短句，4.233 秒完成；`maximum_active=1`、`maximum_queue_depth=1`，两个 RIFF WAV 分别 2.44/2.60 秒，结束后 active/queue 均为 0；证据：[AUDIO-TTS-GATE-PROD-CANARY.json](audio/20260717-005403/AUDIO-TTS-GATE-PROD-CANARY.json)。
- 启用过程事件：首次原子改写 `.env` 时临时文件属主成为 `root:root`，API 因 `PermissionError` 出现一次 spawn error；Engine 当时尚未重启且无活动比赛。按备份确认原属主后恢复为 `ubuntu:ubuntu 0600`，API/Engine 随即正常启动。此事件已记录，不隐藏为“无异常部署”。
- 最终状态：API/Engine/Worker/Web RUNNING，health/ready 200；admission `enabled=true / ok=true / fail_closed=true / active=0 / queue=0`；Computer Use 刷新主页/结果页正常，结果页两条音频连续切换仍严格互斥，见 TC-919/920。

课堂、Consent 与历史授权灰度部署后（2026-07-17 08:16–08:20 CST）：

- 当前 Web release：`/home/ubuntu/sunsq/phdebate/runtime/web-releases/20260717T0816Z-qa-classroom-consent`；切换前 Web 为 `20260716T211321Z-qa-audio-lazy`。
- 发布包：`/tmp/phdebate-20260717T0816Z-qa-classroom-consent.tar.gz`，SHA-256 `d0e635415676c8c9c822f0c0f46c80a69ea1ebd66d4cb192b40bd565d01ffe58`。
- 源码/Web 回滚包：`/home/ubuntu/sunsq/phdebate/runtime/deploy-backups/20260717T0014Z-before-qa-classroom-consent.tar.gz`，SHA-256 `51e0e641e01f99812b0ea4f33567e9bc20f8464c2d3b7cb6d4ffcbb52a9e8d6a`。
- 数据库备份：`/home/ubuntu/sunsq/phdebate/runtime/backups/auto-20260717T000030Z.dump`，SHA-256 `6058b08f72fc004f4e5aaf674895ffed03a2f2ace6e5493ab6da33e974edfcb2`；恢复演练通过，23 张表，备份时 Alembic 为 `0012_agent_gateway_secret`。
- Alembic 已从 `0012_agent_gateway_secret` 顺序升级到 `0017_retain_activity_room_scope`；ready 中 current/expected 均为 `0017_retain_activity_room_scope`。
- shadow PostgreSQL 首轮暴露 `0016_recording_consent_provenance` 超过生产 `alembic_version.version_num VARCHAR(32)`，已把 revision ID 缩短为 `0016_consent_provenance` 并新增长度回归测试；第二轮因 shadow 的相对备份状态路径导致 ready 503，改用绝对 `BACKUP_STATUS_FILE` 后 schema 与所有依赖检查通过。两次均为部署前隔离验证，不影响生产。
- 生产部署自动回滚 trap 覆盖迁移降级、源码恢复、Web symlink 恢复和四服务重启；本次未触发回滚。Web shadow 启动探测首个连接出现一次 `connection refused`，随后页面探测、切换和最终健康检查全部通过。
- 08:20 CST 只读复核：API/Engine/Worker/Web、PostgreSQL、Redis、FunASR、LightTTS 均 RUNNING；ready `ok=true`，Worker 1 个、dead letters 0；无 `preparing/running/judging` 房间。`/`、`/teacher`、`/teacher/consents` 分别约 14.9/11.1/10.6 ms 且均 HTTP 200。Web/Engine stderr 空；API 只有本轮部署前的历史 `.env PermissionError`，Worker 只有部署重启时一次连接 reset，之后无持续错误。
- QA provisioning 变更前另做数据库备份 `/home/ubuntu/sunsq/phdebate/runtime/backups/auto-20260717T002131Z.dump`，153775 bytes，SHA-256 `4dd3aed62564aedb4d3b2266011337185b624c5f280fa30ed0fad72a2a59ddee`。dry-run 无冲突；首次 apply 精确创建 1 org、1 classroom、2 org memberships、2 classroom memberships；同 manifest replay 返回 `already_applied=true / replayed=true / changed=false`，审计恰 1 条且不含 password/secret。
- Computer Use：User A 在 provisioning 前访问 `/teacher` 与 `/teacher/consents` 得到安全权限说明；apply 后同一会话无需重登即可看到 scoped 教学/政策导航、唯一 QA 组织与 QA 课堂。User B 作为同课堂 student 仍不能访问两页且无管理导航。政策发布弹窗默认未确认、提交禁用、时区说明清楚，Escape 关闭并恢复焦点；证据 TC-923–930。

研究导出与学生课前 Consent 入口部署后（2026-07-17 09:00–09:04 CST）：

- 当前 Web release：`/home/ubuntu/sunsq/phdebate/runtime/web-releases/20260717T0900-research-consent`；前一版本为 `20260717T0816Z-qa-classroom-consent`。
- 发布包：`/tmp/phdebate-20260717T0900-research-consent.tar.gz`，SHA-256 `fab831aa4518186490b8fefe96dd95aac554b2eb95239b90e915ce06f80a695b`。
- 源码/Web 回滚包：`/home/ubuntu/sunsq/phdebate/runtime/deploy-backups/20260717T0900-before-research-consent.tar.gz`，192831289 bytes，SHA-256 `e39ccd188d2880f574860c324b606d159af3361422f72a51e0f7ef6ec6635e16`；前一 Web 指针另存 `20260717T0900-before-research-consent-web.txt`。
- 数据库备份：`/home/ubuntu/sunsq/phdebate/runtime/backups/auto-20260717T010027Z.dump`，154977 bytes，SHA-256 `668e62febb14d498446fa7a617c591d20384999e390a647048aa0fa66d3e31bc`。
- Alembic 已从 0017 升级到 `0018_research_exports`；新增稳定组织伪名映射、异步导出 job 和 `researcher` scoped 角色。导出 ZIP 只含 manifest/JSONL/CSV/media checksum，不打包媒体；request/download、break-glass、同意撤回拒绝和 artifact 异常均审计。
- PostgreSQL shadow 第一次因 `pg_restore --no-owner` 把对象留给 postgres，生产应用角色无权读取 `alembic_version`；第二次 schema/API 实际已通过，但 harness 把 Uvicorn 正常 startup 日志所在 stderr 误判为失败。修正对象 role 与错误模式后第三次完整通过：schema 0018、两张表、researcher constraint、ready 200、research/consent 未登录均 401；临时数据库每次均由 trap 清理，未影响生产。
- 完整质量门：API `249 passed, 20 warnings`；Web `22 files / 127 tests passed`；Ruff、py_compile、TypeScript、Next production build 通过。20 条 warning 均为 Alembic `path_separator` deprecation。
- 部署后 ready `ok=true`，schema current/expected 0018；API/Engine/Worker/Web RUNNING，Worker queue/dead letter 0；LightTTS active/queue 0。现有真实房 278571 保持 paused，未被操作。
- Computer Use：User B 对 `/me` 强制刷新后出现“我的课堂与待处理同意”，只显示 QA 灰度班/QA 组织；无 policy 时 badge 为“等待政策”，明确建房与新录音保持关闭。证据 TC-932。

## 历史审计：课堂规模化与研究数据治理（已移出当前产品范围）

> 本节保留 Iteration 1–11 的历史测试证据与当时建议。用户在 Iteration 12 明确要求移除教学、同意政策、研究导出和容量编排等管理型模块；因此下列 GAP 不再计入当前开放 P1/P2/P3，也不作为当前产品路线要求。

核心比赛状态机、事件序列、服务配置快照和单场归档已有良好基础，但下列缺口会阻止平台安全扩展到多班级和大量学生。

| ID | 严重度 | 缺口与证据 | 建议 MVP 与验收重点 |
|---|---|---|---|
| GAP-CLASS-001 | CLOSED（原 P1） SECURITY | 0013–0017 已生产迁移；历史 RoomSeat 不再永久授权，Activity 物理删除被 RESTRICT；QA owner_teacher/student 双会话验证 scoped 导航与学生拒绝，global role 均保持 `user` | 保持为回归项；后续补 removed student 与第二组织教师的真实生产会话，不重新开放已关闭 P1 |
| GAP-PRIVACY-001 | P1 PRIVACY | 录音/研究/公开展示 policy、timezone-aware 换版、effective private、Consent provenance、管理员入口、QA provisioning 与学生建房前 `/me` 自助入口均已生产；但尚未最终发布 QA policy，也未完成学生真实 grant/revoke、换版失效和录音门端到端 | 仅在 QA 组织发布明确测试政策；由学生本人完成 grant/revoke；用旧/新版本、active/removed student 做生产 Computer Use 后再向真实课堂开放 |
| GAP-CLASS-002 | P1 GAP | 生产已具备 Activity、确定性分组、幂等批量私密建房、教师看板和 QA org/classroom；本地 120 人/8 并发/30 房通过。仍缺生产活动/名单/房间完整 E2E，以及 CSV/邀请/到场、名单变更恢复和分页 | 用 QA 课堂完成活动→名单→分组→私密建房→学生进入→教师总览；随后补 CSV/邀请/到场和异常恢复 |
| GAP-EXPORT-001 | P1 DATA | 0018 已生产提供 scoped async ResearchExport API、稳定伪名、manifest/JSONL/CSV/media checksum、幂等/审计/下载时重验；但尚无教师/研究人员 UI，也未用真实 QA activity + research_use grant 完成首个生产 artifact E2E | 增加 `/teacher/research-exports` 筛选/job/download UI；QA 活动完成后用 research policy 与学生本人 grant 生成、下载、撤回失效并核验 ZIP |
| GAP-CAPACITY-001 | P1 PERF | LightTTS 生产仍为全局 active=1/pending=2。Iteration 13 已把空闲同步接纳从 2 修正为严格 3（1 active + 2 waiting）；真实三短句 canary 5.798 秒完成，最大 active=1/queue=2。但 4/10/20 机制拒绝率仍为 25%/70%/85%，真实 4/10/20 GPU 与 FunASR 竞争容量未证明；既有争用 synth P95 21.527 秒 | 建立真实独立 GPU 容量环境；按开赛批次做预留/调度，向每房显示 queue position、校准 ETA、延后与恢复；容量不足时在开赛前而非发言后拒绝 |
| GAP-RETENTION-001 | P2 PRIVACY | 已引用的录音、逐字稿和归档没有保留期、撤回、删除请求或 legal hold | RetentionPolicy + deletion/tombstone 队列；DB、媒体、归档和备份处理必须一致且可重试审计 |
| GAP-AUDIT-001 | P2 SECURITY | 审计主要覆盖管理员写操作，不覆盖登录、敏感读取、归档/导出下载和真人音频访问 | 增加 DataAccessAudit，记录不可变 actor、scope、purpose、filters、result_count、IP 哈希和下载事件 |
| GAP-LINEAGE-001 | P2 DATA | 录制时 policy/record 快照已随 0016 生产上线并进入 Match、Speech、archive v2；仍缺 audio hash/codec/rate、ASR source/confidence/provider version、人工修订历史及完整 Agent/voice 质量血缘 | 继续固化语音、模型、角色与质量血缘；离线 bundle 校验所有音频 checksum，并验证旧 archive v1 可重建为 v2 |
| GAP-JUDGE-001 | P2 DATA | 人工复核只能修正胜负、团队分和理由，不能修正 8 席个人分 | JudgeReviewRequest 支持逐席 0–100 分与前后事件；archive/rating/audit 保留旧值和新值 |
| GAP-QA-001 | P3 QA | 已登记 48 个长格式 TC + 47 个带最小复现/证据的原子 TC，共 95 个；早期长用例与原子 TC 仍未全部统一为手册规定的 20 字段格式 | 后续逐步统一为完整长格式，并继续补真实移动端、弱网、真人 CER 与 MOS 缺证项 |
| CONTENT-TEACHER-001 | P3 CONTENT | `/teacher` 的 `CLASSES / 课堂与活动` 标题旁显示“0 个”，同时下方已有 1 个课堂；实际含义似为“0 个活动”，首次使用可能误认为课堂加载失败 | 改成“1 个课堂 · 0 个活动”或分别计数；有课堂无活动时不得显示含义不明的单一 `0 个` |

历史阶段的结论是：核心比赛链路可受控试用，而课堂/研究治理目标未达到发布条件。该历史结论已被 Iteration 12 的学生自助赛事范围收缩取代；当前发布判断只按学生比赛主链、语音质量、真实 LightTTS 容量和关键终端体验计算。

### 教师课堂 MVP 推荐第一纵向切片

推荐先做“已有账号名单的单班活动”，不要把教师提升为全局 `User.role=teacher`，也不要继续扩张系统管理员单体后台。

1. 数据基础：`Organization`、`OrganizationMembership(owner/admin/teacher/researcher/student)`、`Classroom`、`ClassroomMembership(teacher/student)`；membership 每次请求查库，停用后下一请求立即失权。
2. 活动与分组：`Activity`、`ActivityParticipant(group_no, seat_key, attendance_status)`；房间增加 nullable `activity_id/activity_group_no`，课堂房默认 private。
3. 权限不变量：Teacher A 对 Teacher B 的 classroom/activity/room/history/media/archive/export 全部 403；teacher 可控制本活动房但不能发言，除非另有真人席位；不能访问 Provider/Judge/Prompt 系统配置。
4. 批量建房：`roster:preview → roster:commit → group-plan:preview → assignments(expected_revision) → rooms:provision(idempotency key)`；120 人/30 个 4v4 房并发重试后必须仍恰好 30 房、120 assignments，任何学生冲突则整批 0 新房。
5. 前端独立 `/teacher`：课堂列表、活动五步 stepper、30 房进度/异常总览；学生 `/me` 直接显示“我的课堂活动/进入我的房间”，不要求记房间号。
6. 上线约束：新表和 nullable 列显式 Alembic 迁移，增加真实 0012→新 head 测试；旧房 `activity_id=NULL` 保持原行为；功能旗标默认关闭，只对 QA 机构开放。Consent/Privacy 未完成前不得给真实未成年人启用。

## 路由覆盖矩阵

| 路由 | 身份 | 桌面 | 手机 | 状态 | 证据/备注 |
|---|---|---|---|---|---|
| `/` | 匿名/登录用户 | 已测 | 精确 390×844 | PASS | 首页无横向溢出，正式赛/训练赛和学生创建入口可达；TC-995 |
| `/login` | 匿名 | 已测 | 窄屏 | PASS | Safari 部署后错误登录显示可读中文错误 |
| `/register` | 匿名 | 已测 | 窄屏 | PASS/BLOCKED | Chrome、SunBrowser 注册成功；Safari 表单与结构化错误修复已部署，最终再次创建账号受确认规则阻塞 |
| `/rankings` | 匿名/登录用户 | 已测 | 待测 | PASS | 第一赛季页面正常，当前暂无排名数据 |
| `/admin` | 匿名/普通用户/System Admin | 部分 | 待测 | PASS/BLOCKED | 匿名与普通用户均被重定向；System Admin 待凭据 |
| `/admin/agent-access` | 匿名/普通用户/System Admin | 部分 | 待测 | PASS/BLOCKED | 匿名与普通用户均被重定向；System Admin 待凭据 |
| `/debate` | 匿名/Agent Admin | 已测 | 待测 | PASS | 独立登录页正常；已有 Agent Admin 会话可用 |
| `/api/health` | 匿名 | 已测 | 不适用 | PASS | `ok=true`, service=`phdebate`, version=`2.0.0` |
| `/debate/health` | 匿名 | 已测 | 不适用 | PASS | status=`ready`; database/redis/llm_gateway 均正常 |
| `/debate/api/models` | 匿名/Agent Admin | 已测 | 不适用 | PASS | 返回模型列表；未发现密钥明文 |
| `/teacher` | QA owner_teacher / QA student | 已测 | 待测 | PASS | User A 仅见 QA 灰度班；User B 同课堂 student 仍被拒绝且无管理导航，TC-924/927/928 |
| `/teacher/consents` | QA org owner / QA student | 已测 | 待测 | PASS | User A 仅见 scoped QA 组织与三类政策；User B 被拒绝；发布弹窗安全门通过，TC-925/926/929/930 |
| `/rooms/566139/result` | User A/User B | 已测 | 390×844 + Fast/Slow 3G | PASS/P2 | 赛果、长文本、9 条录音、60/60 时间线、互斥和离页清理通过；audio 约 30px、Slow 3G pending 切换延迟为 P2，TC-1002–1008/1012–1014 |
| `/rooms/764886/result` | User A/User B | 已测 | 390×844 | PASS/P2 | 8 席评分、11 条录音、93/93 时间线完整；评分标题与说明窄屏挤压为 P2，TC-1009–1011 |
| `/rooms/764886/debate` | User A/User B | 已测 | 待测 | PASS | 历史启动竞态已修复；生产真实麦克风与自动化竞态回归通过 |
| `/rooms/764886/control` | 房主/User B | 已测 | 待测 | PASS | 房主暂停、刷新保持、恢复正常；User B 无控制权限并转观战 |
| `/me` | 登录用户 | 已测 | 精确窄屏 | PASS | 进行中比赛卡和继续比赛入口可达；TC-998 |
| `/rooms/278571/debate` | 真人参赛者/其他设备只读 | 已测 | 精确 390×844 | PASS/P2 | 舞台、比赛操作面板和退出确认可达且无横向溢出；全局导航与少数恢复控件不足 44px，TC-996/999–1001 |

## 1v1 阶段覆盖矩阵

| 阶段/能力 | 状态 | 可见耗时 | 证据/备注 |
|---|---|---:|---|
| 创建与认领席位 | PASS | <10 s | 房间 566139，User A 正方一辩，AI 反方一辩；辩题 HTML 标签按文本展示，无 XSS |
| 未准备开始拦截 | PASS | 即时 | 未准备时开始按钮禁用 |
| 准备与 AI 补位 | PASS | <10 s | 准备后成功开始并补齐 AI |
| 人类回合/麦克风/字幕 | PASS | 权限弹窗即时 | 启动即时反馈、本机状态、静音拦截、人工核对和麦克风释放均通过 |
| AI 回合/字幕/TTS | PASS | 约 5–30 s/轮 | AI 文本、字幕、LightTTS 播放正常 |
| 自由辩论 | PASS | 正常推进 | 无字幕/静音时进入人工核对，不再自动提交 partial |
| 总结阶段 | PASS | 正常推进 | 状态机完成 |
| AI Judge | PASS | 约数十秒 | 生成胜负、团队分、理由和时间线 |
| 结果页与 `/me` 历史 | PASS | 即时 | 反方胜；9 次发言/60 事件；历史存在；非 WAV 时长与结果音频已修复 |

## 4v4 阶段覆盖矩阵

| 阶段/能力 | 状态 | 可见耗时 | 证据/备注 |
|---|---|---:|---|
| 创建、双用户加入与准备 | PASS | <1 min | 房间 764886；User A 正方1、User B 正方2，双方会话实时同步 |
| AI 补位与预设语音 | PASS | <15 s | 其余 6 席由 6 个独立 Agent 补齐 |
| 正方一辩立论 | PASS | 正常推进 | 录音、本机状态和人工补文已通过二次生产回归 |
| 反方一辩立论 | PASS | 正常推进 | AI 字幕与 LightTTS 正常 |
| 正方二辩驳论 | PASS（修复验证） | 轮次超时历史已关闭 | 启动竞态根因修复并由真实麦克风与自动化用例回归 |
| 反方二辩驳论 | PASS | 正常推进 | AI 正常 |
| 正方三辩质询 | PASS | 正常推进 | AI 乾元正常 |
| 反方三辩质询 | PASS | 正常推进 | AI 明川正常 |
| 自由辩论（双方至少两轮） | PASS（修复验证） | 状态机正常 | 旧 ASR/MediaRecorder 回调与阶段变化隔离，人工回合可恢复 |
| 反方四辩总结 | PASS | 正常推进 | AI 若谷完整生成并播放 |
| 正方四辩总结 | PASS | 正常推进 | AI 知微完整生成并播放 |
| AI 裁判评议 | PASS | 数秒内跳转结果 | 正方胜，82:76；Judge 给出结构化中文理由 |
| 结果、个人分、积分与排行 | PASS | 即时 | 11 次发言、93 事件；8 席个人分、User B 暂无评分、双用户积分、音频实播均通过 |

## 权限矩阵

| 资源/操作 | 匿名 | 普通用户 | 房主 | System Admin | Agent Admin |
|---|---|---|---|---|---|
| 公开页面/排行榜 | PASS | PASS | PASS | BLOCKED（无会话） | PASS（主站按匿名/普通身份） |
| 房间观战 | PASS | PASS | PASS | BLOCKED（无会话） | 不适用 |
| 辩论页 | PASS：自动转观战 | PASS：仅本人已认领席位可操作 | PASS | BLOCKED（无会话） | 不适用 |
| 房间控制台 | PASS：拒绝 | PASS：非房主拒绝并转观战 | PASS | BLOCKED（无会话） | 不适用 |
| 主系统后台 | PASS：拒绝 | PASS：拒绝 | PASS：普通房主仍拒绝 | BLOCKED（无会话） | 不适用 |
| Debate Agent 后台 | PASS：仅显示独立登录 | 不适用 | 不适用 | 不适用 | PASS：独立会话、只读检查与连接测试通过 |

课堂作用域补充矩阵：

| 资源/操作 | QA owner_teacher（User A） | QA classroom student（User B） | 无 membership 的普通用户 |
|---|---|---|---|
| 教师工作台发现 | PASS：仅 QA 灰度班 | PASS：拒绝且无导航 | PASS：拒绝且无数据泄露 |
| 政策管理组织发现 | PASS：仅 QA 自动化学校 | PASS：拒绝且无导航 | PASS：拒绝且无组织信息 |
| 政策发布弹窗 | PASS：可打开；默认未确认，提交禁用 | 不可访问 | 不可访问 |
| 全局 `User.role` | 保持 `user` | 保持 `user` | 保持原角色 |

## 浏览器和响应式覆盖矩阵

| 页面 | 桌面当前尺寸 | 1440×900 | 390×844 | 键盘/可访问性 | 状态 |
|---|---|---|---|---|---|
| 首页 | 已测 1357×768 | 未达到 | Chrome override 390×844 | 核心 CTA 可达；全局导航部分目标 <44px | PASS/P2 |
| 赛事详情 | 已测 1357×768 | 未达到 | 已测 574×798 | Tab 名称清晰 | PASS |
| 房间大厅 | 待测 | 待测 | 待测 | 待测 | NOT-RUN |
| 辩论页 | 已测匿名/真人视图 | 未达到 | Chrome override 390×844 | 核心舞台控件 44px；少数恢复控件偏小 | PASS/P2 |
| 观战页 | 已测 | 未达到 | 已测 574×798 | 声音/全屏/设置有名称 | PASS |
| 结果页 | 已测 1357×768 | 未达到 | Chrome override 390×844 + Fast/Slow 3G | 1v1/4v4、音频互斥、离页清理、60/60与93/93通过；30px audio、评分标题、pending切换为P2 | PASS/P2（活动舞台弱网仍阻塞） |
| 主后台 | 待测 | 待测 | 待测 | 待测 | NOT-RUN |
| Agent 后台 | 已测 1357×768 | 未达到 | 已测 574×798 | 表单 Label 与按钮名称可获取 | PASS |
| 教师工作台 | 已测 1357×768 | 未达到 | 待测 | scoped 空状态与管理表单可获取名称 | PASS（移动端待测） |
| 政策管理 | 已测 1357×768 | 未达到 | 待测 | 确认默认未勾选；Escape 关闭并恢复焦点 | PASS（移动端待测） |

## 用户角色与关键任务地图

| 角色 | 首要任务 | 当前可完成度 | 最大阻力 | 规模化结论 |
|---|---|---|---|---|
| 第一次使用的学生 | 理解赛制、注册、加入、试音、准备、完成比赛 | 中 | 缺少新手引导、赛前设备检查、隐私/录音说明；需要理解房间号和席位 | 不适合无培训大规模使用 |
| 有经验的学生辩手 | 查看规则、准确计时与发言、赛后复盘 | 较高 | ASR 真人质量证据不足；结果音频会重叠；逐字稿不可纠错 | 小规模受控可用 |
| 房主/队长 | 邀请、确认到场、处理未准备/掉线并开赛 | 中 | 依赖六位房号；无到场清单、提醒、批量异常处理 | 多房间组织成本高 |
| 匿名观众 | 快速理解赛况、听发言、查看结果 | 较高 | 默认公开暴露真实姓名、真人音频与逐字稿；隐私边界不适合课堂 | 公开演示可用，学生场景高风险 |
| 教师/赛事组织者 | 导入名单、分组、批量建房、看板、干预、导出 | 中 | scoped 课堂、Activity/roster/dashboard 和 policy 管理已生产；仍缺 QA 生产完整活动链、CSV/到场/异常恢复与批量导出 | P1 仍阻塞规模化，但不再是功能全缺失 |
| 系统管理员 | 服务健康、审计、异常恢复、权限治理 | 中/阻塞 | 本轮无 System Admin 会话；现有权限以全局管理员为主，缺教师作用域 | 证据不足且权限模型不完整 |
| 研究/数据运营 | 按活动导出、匿名化、质量筛选、复现实验 | 低 | 只有单场归档；缺批量导出、血缘、保留删除和访问审计 | P1/P2 阻塞长期研究 |
| 移动端/弱网用户 | 小屏参赛、重连、连续播放 | 低/阻塞 | 未达到 390×844；弱网未测；WAV 体积大、无排队/降级提示 | 不可作为课堂主路径 |

建议的新学生主流程：`赛事/课堂邀请链接 → 账号与同意 → 60 秒设备检查/试音 → 规则与角色示例 → 自动进入指定席位 → 准备检查表 → 比赛 → 可纠错逐字稿与复盘`。建议教师主流程：`班级/名单 → 活动与赛制 → dry-run 自动分组 → 幂等批量建房 → 到场与异常看板 → 批量开赛/暂停 → 待复核队列 → 受控数据导出`。

## 页面级产品评分（1–5）

| 页面 | 功能 | 易用 | 可靠 | 性能 | 视觉 | A11Y | 内容 | 评分依据 |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| 首页/赛事大厅 | 4 | 3 | 4 | 4 | 4 | 3 | 3 | 赛事入口清楚，但学生/教师目标未分流，缺课堂入口与帮助 |
| 登录/注册 | 4 | 4 | 4 | 4 | 4 | 3 | 4 | 错误已可读；原生校验可用；缺隐私、录音与研究同意 |
| 赛事详情 | 4 | 3 | 4 | 4 | 4 | 3 | 4 | 规则可读，加入/创建仍依赖用户理解房号和赛制 |
| 房间大厅 | 4 | 3 | 4 | 4 | 4 | 3 | 3 | 席位与准备链路可用；缺设备预检、到场提醒和故障自助 |
| 辩论页 | 4 | 3 | 4 | 3 | 4 | 3 | 4 | 发言状态和修复后的麦克风反馈较好；Agent/TTS 等待仍偏长 |
| 观战页 | 4 | 4 | 4 | 4 | 4 | 3 | 4 | 赛况清晰且无控制权限；默认公开隐私设计不适合学生 |
| 控制台 | 4 | 3 | 4 | 4 | 3 | 3 | 3 | 单房间应急可用；缺教师多房总览、批量操作与原因化异常 |
| 结果页 | 4 | 3 | 4 | 4 | 4 | 4 | 4 | 个人分/时间线/归档完整；按需音频、时长、ARIA、互斥与离页回归通过 |
| 主系统后台 | 3 | 3 | 3 | 4 | 3 | 3 | 3 | 普通用户隔离通过；管理员实登和教师 scoped 权限未验证/缺失 |
| Agent 后台 | 4 | 3 | 4 | 4 | 3 | 3 | 3 | 简易设置与连接测试可用；高级配置仍偏技术化 |

## 全局产品质量总分

| 维度 | 权重 | 得分 | 依据 |
|---|---:|---:|---|
| 功能正确 | 20 | 16 | 1v1/4v4、Agent、Judge、结果与恢复主链路已跑通；课堂能力缺失 |
| 易用性 | 15 | 9 | 单场流程可理解，但新手、房主和教师仍需要开发者/管理员知识 |
| 可靠性 | 15 | 12 | 多项竞态、静音 ASR、音频互斥和 SunBrowser 长列表加载已闭环；语音容量和真人 CER 未关闭 |
| 性能 | 10 | 6 | 页面较快；LightTTS 单路隔离达标，但争用 P95 21.527 秒 |
| 视觉与一致性 | 10 | 7 | 学生端整体统一；后台与长文本密度仍偏工程化 |
| 无障碍 | 10 | 6 | 主要控件有可访问名称；Chrome 首页 200% 缩放与折叠菜单通过，390×844、其余关键页缩放和完整键盘流仍未验证 |
| 数据完整性 | 10 | 6 | 单场事件、语音、Judge、积分可追溯；缺导出血缘、质量规则和保留治理 |
| 运营管理能力 | 10 | 2 | 无班级、活动、批量分组/建房、教师看板和批量导出 |
| **总分** | **100** | **64** | **核心比赛可受控试用，距离成熟课堂与研究产品仍有结构性缺口** |

## 详细测试用例

### TC-001：首页匿名访问与首屏基线

- 状态：PASS
- 严重程度：无
- 页面/路由：`/`
- 测试身份：匿名
- 前置条件：Safari 独立测试窗口，无主站登录会话
- 操作步骤：
  1. 使用 Computer Use 打开 `https://117.50.218.251/`。
  2. 等待页面加载完成并检查导航、赛事卡片、公开观战和排行榜摘要。
- 预期结果：首页可正常加载，无证书警告、白屏、无限 Loading 或乱码。
- 实际结果：首页正常渲染；显示 4v4、1v1 赛事卡片和一个公开观战房间入口；未观察到证书警告、白屏或乱码。
- 证据：`screenshots/20260717-005403/TC-001-首页-正常.png`
- 控制台/网络现象：未获取
- 复现稳定性：必现
- 建议修复方向：无

### TC-101：登录空字段校验

- 状态：PASS
- 严重程度：无
- 页面/路由：`/login`
- 测试身份：匿名
- 前置条件：登录账号与密码均为空
- 操作步骤：
  1. 打开登录页。
  2. 不填写账号与密码，点击“登录”。
- 预期结果：阻止提交并明确提示必填字段。
- 实际结果：浏览器原生校验阻止提交，账号输入框显示“填写此栏”。
- 证据：`screenshots/20260717-005403/TC-101-登录-空字段校验.png`
- 控制台/网络现象：未获取
- 复现稳定性：必现
- 建议修复方向：可保留原生校验；如需统一视觉，可增加与站点设计一致的字段级提示。

### TC-102：错误账号密码提示

- 状态：PASS（历史失败已修复）
- 严重程度：历史 P2，当前已关闭
- 页面/路由：`/login`
- 测试身份：匿名
- 前置条件：使用明显不存在的 QA 测试账号和非敏感测试密码
- 操作步骤：
  1. 填写不存在的测试账号。
  2. 填写非敏感测试密码。
  3. 点击“登录”。
- 预期结果：登录失败，并显示用户可理解且不泄露内部信息的错误文案，例如“账号或密码错误”。
- 实际结果：首轮显示 `[object Object]`；部署后 Safari 回归显示“账号或密码错误。”，未泄露内部结构。
- 证据：历史失败 `TC-102-登录-错误账号密码.png`；修复验证 `TC-901-部署后Safari登录错误-可读.png`
- 控制台/网络现象：未获取
- 复现稳定性：必现
- 建议修复方向：统一解析 API 错误响应，确保渲染字符串消息；对未知对象提供安全的通用降级文案。

### TC-003：注册页与空字段校验

- 状态：PASS
- 严重程度：无
- 页面/路由：`/register`
- 测试身份：匿名
- 前置条件：独立 Safari 窗口
- 操作步骤：
  1. 打开注册页。
  2. 不填写任何字段，点击“注册并进入赛场”。
- 预期结果：页面正常显示；空字段阻止提交。
- 实际结果：页面正常渲染；浏览器原生必填校验显示“填写此栏”，未创建账号。
- 证据：`screenshots/20260717-005403/TC-003-注册页-正常.png`、`screenshots/20260717-005403/TC-103-注册-空字段校验.png`
- 控制台/网络现象：未获取
- 复现稳定性：必现
- 建议修复方向：无

### TC-004：公开排行榜

- 状态：PASS
- 严重程度：无
- 页面/路由：`/rankings`
- 测试身份：匿名
- 前置条件：无
- 操作步骤：打开排行榜并检查赛事、赛季选择器和空状态。
- 预期结果：页面正常，无白屏或敏感信息。
- 实际结果：页面正常；默认显示 4v4 日常赛、第一赛季进行中及“暂无排名数据”。
- 证据：`screenshots/20260717-005403/TC-004-排行榜-正常.png`
- 控制台/网络现象：未获取
- 复现稳定性：必现
- 建议修复方向：无

### TC-005：4v4 赛事详情与 Tab

- 状态：PASS
- 严重程度：无
- 页面/路由：`/competitions/daily-4v4`
- 测试身份：匿名
- 前置条件：无
- 操作步骤：
  1. 检查赛事介绍、可选辩题和参赛入口。
  2. 依次切换排行榜、观战列表、规则说明。
  3. 在排行榜 Tab 刷新页面。
- 预期结果：各 Tab 内容可访问，无敏感后台信息。
- 实际结果：各 Tab 正常；观战列表仅显示房间号、辩题和状态；刷新后 Tab 回到“赛事介绍”，URL 始终不包含 Tab 状态，作为可用性观察保留。
- 证据：`screenshots/20260717-005403/TC-005-4v4赛事详情-正常.png`
- 控制台/网络现象：未获取
- 复现稳定性：必现
- 建议修复方向：若产品需要可分享/可恢复的 Tab，建议将 Tab 写入 URL 或持久化选中状态。

### TC-006：房间号输入约束与匿名登录引导

- 状态：PASS
- 严重程度：无
- 页面/路由：`/competitions/daily-4v4`
- 测试身份：匿名
- 前置条件：打开“立即参赛”弹窗并切换到“搜索房间”
- 操作步骤：输入空值、3 位数字、字母、特殊字符、7 位数字和不存在的 6 位数字。
- 预期结果：仅允许六位数字；非法输入不导致崩溃；匿名进入时引导登录。
- 实际结果：空值/短号按钮禁用；字母和特殊字符被过滤；7 位数字截断为 6 位；匿名提交 6 位数字后跳转 `/login?next=%2Fcompetitions%2Fdaily-4v4`。
- 证据：`screenshots/20260717-005403/TC-006-房间号-不存在提示.png`（实际为匿名登录引导结果）
- 控制台/网络现象：未获取
- 复现稳定性：必现
- 建议修复方向：可增加显式的“请输入六位数字”字段级提示，避免用户只看到按钮禁用。

### TC-007：健康接口基线

- 状态：PASS
- 严重程度：无
- 页面/路由：`/api/health`、`/debate/health`、`/debate/api/models`
- 测试身份：匿名
- 前置条件：无
- 操作步骤：使用浏览器逐一打开三个接口。
- 预期结果：主站与 Agent 健康正常；模型接口无密钥明文。
- 实际结果：主站 `ok=true`；Agent `status=ready`，database/redis/llm_gateway 均 `ok=true`；模型接口返回模型列表，未显示 Key。
- 证据：浏览器可访问响应；接口未单独截图
- 控制台/网络现象：未获取
- 复现稳定性：必现
- 建议修复方向：无

### TC-008：匿名与普通用户后台权限

- 状态：PASS
- 严重程度：无
- 页面/路由：`/admin`、`/admin/agent-access`
- 测试身份：匿名、User A
- 前置条件：Safari 无主站会话；Chrome 已有普通用户会话
- 操作步骤：分别直接访问后台路由。
- 预期结果：不能进入后台。
- 实际结果：匿名和 User A 均被重定向至首页，未显示后台内容。
- 证据：Computer Use 可访问性树与最终 URL
- 控制台/网络现象：未获取
- 复现稳定性：必现
- 建议修复方向：无

### TC-009：独立 Debate Agent 后台只读检查

- 状态：PASS
- 严重程度：无
- 页面/路由：`/debate`
- 测试身份：Agent Admin
- 前置条件：Chrome 已存在独立 Agent 管理登录会话
- 操作步骤：
  1. 检查简易模式和高级模式导航。
  2. 只读检查 LLM API、Prompt Studio、消息模板、8 个辩手人设、模型参数、Memory、请求日志、安全与管理员。
  3. 不点击任何保存、创建、删除或密钥操作。
- 预期结果：简易设置可用；Key 不显示明文；8 个辩手启用；API 调用方式为 RESTful POST；Judge 启用。
- 实际结果：简易设置显示模型服务 2、AI 辩手 8、运行任务 0、待审核记忆 3；API Key 显示为已配置/掩码；API 调用地址为 `POST /debate/api/debate`；AI 裁判显示已启用且接口为 `POST /debate/api/judge`；高级模块均可只读进入，未发现 Key 明文。
- 证据：`screenshots/20260717-005403/TC-008-Agent后台-高级模式只读.png`、`screenshots/20260717-005403/TC-009-Agent后台-简易模式.png`
- 控制台/网络现象：未获取
- 复现稳定性：必现
- 建议修复方向：无

### TC-010：Agent 模型测试连接

- 状态：PASS
- 严重程度：无
- 页面/路由：`/debate`
- 测试身份：Agent Admin
- 前置条件：简易模式已有模型配置；不修改字段
- 操作步骤：点击“测试连接”，不点击保存。
- 预期结果：连接成功并返回可见延迟。
- 实际结果：连接成功，约 60.6 ms；发现 23 个模型，当前模型可用。
- 证据：页面成功状态文案
- 控制台/网络现象：未获取
- 复现稳定性：仅一次
- 建议修复方向：无

### TC-201：User A 会话保持与个人中心

- 状态：PASS
- 严重程度：无
- 页面/路由：`/me`
- 测试身份：User A
- 前置条件：Chrome 已有普通用户登录会话
- 操作步骤：打开 `/me`，检查比赛、历史和积分区域；刷新页面。
- 预期结果：个人中心可访问；刷新后会话有效。
- 实际结果：页面显示进行中/历史/积分相关区域；刷新后仍位于 `/me`，仍为登录状态。
- 证据：`screenshots/20260717-005403/TC-201-个人中心-UserA.png`
- 控制台/网络现象：未获取
- 复现稳定性：必现
- 建议修复方向：无

### TC-202：User A 不能进入后台

- 状态：PASS
- 严重程度：无
- 页面/路由：`/admin/agent-access`
- 测试身份：User A
- 前置条件：User A 已登录
- 操作步骤：直接输入后台 URL。
- 预期结果：拒绝或跳转。
- 实际结果：跳转首页，未显示 Agent 接入后台内容。
- 证据：Computer Use 最终 URL 与页面内容
- 控制台/网络现象：未获取
- 复现稳定性：必现
- 建议修复方向：无

### TC-301：匿名公开观战

- 状态：PASS
- 严重程度：无
- 页面/路由：`/rooms/278571/watch`
- 测试身份：匿名（主站）
- 前置条件：公开房间存在
- 操作步骤：直接打开观战 URL，检查按钮和公开内容。
- 预期结果：可观战；无麦克风、开始、暂停、跳过等操作按钮。
- 实际结果：可见当前阶段、计时、双方席位和声音开关；没有麦克风、开始、暂停或跳过按钮。
- 证据：`screenshots/20260717-005403/TC-301-匿名观战页.png`
- 控制台/网络现象：未获取
- 复现稳定性：必现
- 建议修复方向：无

### TC-302：匿名直接访问辩论页

- 状态：PASS（历史失败已修复）
- 严重程度：历史 P2，当前已关闭
- 页面/路由：`/rooms/278571/debate`
- 测试身份：匿名（主站）
- 前置条件：公开房间存在
- 操作步骤：在地址栏直接访问辩论页 URL。
- 预期结果：拒绝访问或重定向到登录/观战页。
- 实际结果：首轮可进入辩论舞台；部署后 Safari 匿名访问自动重定向至 `/rooms/278571/watch`。
- 证据：历史失败 `TC-302-匿名直达辩论页-未拒绝.png`；修复验证 `TC-902-部署后匿名辩论页-重定向观战.png`
- 控制台/网络现象：未获取
- 复现稳定性：必现
- 建议修复方向：在路由守卫和服务端房间权限校验中将匿名用户重定向到 `/watch` 或登录页，不只依赖按钮禁用。

### TC-303：匿名直接访问控制台

- 状态：PASS
- 严重程度：无
- 页面/路由：`/rooms/278571/control`
- 测试身份：匿名（主站）
- 前置条件：公开房间存在
- 操作步骤：直接访问控制台 URL。
- 预期结果：拒绝访问。
- 实际结果：被重定向到 `/rooms/278571/watch`，未显示控制按钮。
- 证据：Computer Use 最终 URL 与页面按钮列表
- 控制台/网络现象：未获取
- 复现稳定性：必现
- 建议修复方向：无

### TC-401：首页窄屏响应式

- 状态：PASS
- 严重程度：无
- 页面/路由：`/`
- 测试身份：匿名
- 前置条件：Safari 独立窗口缩窄至 574×798
- 操作步骤：检查导航、首屏 CTA、赛事卡片与导航菜单。
- 预期结果：无横向滚动、截断或按钮遮挡；移动导航可展开。
- 实际结果：首屏正常重排；CTA 与赛事卡片可见；导航菜单可展开并显示赛事大厅、排行榜；未发现横向滚动。
- 证据：`screenshots/20260717-005403/TC-401-首页-窄屏窗口.png`
- 控制台/网络现象：未获取
- 复现稳定性：必现
- 建议修复方向：无

### TC-402：4v4 赛事详情窄屏响应式

- 状态：PASS
- 严重程度：无
- 页面/路由：`/competitions/daily-4v4`
- 测试身份：匿名
- 前置条件：574×798 窄屏窗口
- 操作步骤：检查标题、说明、状态标签、参赛按钮、Tab 与正文卡片。
- 预期结果：内容可读且无横向滚动。
- 实际结果：标题、标签、按钮和四个 Tab 均正常显示；正文单列重排；未发现横向滚动。
- 证据：`screenshots/20260717-005403/TC-402-4v4赛事详情-窄屏.png`
- 控制台/网络现象：未获取
- 复现稳定性：必现
- 建议修复方向：无

### TC-403：观战页窄屏响应式

- 状态：PASS
- 严重程度：无
- 页面/路由：`/rooms/278571/watch`
- 测试身份：匿名
- 前置条件：574×798 窄屏窗口，公开比赛暂停
- 操作步骤：检查上方舞台、计时、双方席位和底部观战控制区。
- 预期结果：舞台与固定控制区不互相遮挡；无参赛控制。
- 实际结果：舞台、字幕区、8 个席位和固定底部“观战模式”区域均清晰；无麦克风/开始/暂停/跳过按钮；未发现横向滚动。
- 证据：`screenshots/20260717-005403/TC-403-观战页-窄屏.png`
- 控制台/网络现象：未获取
- 复现稳定性：必现
- 建议修复方向：无

### TC-404：辩论页窄屏响应式

- 状态：PASS
- 严重程度：无
- 页面/路由：`/rooms/278571/debate`
- 测试身份：匿名
- 前置条件：574×798 窄屏窗口
- 操作步骤：检查舞台、计时、席位与固定底部发言控制区。
- 预期结果：上方舞台、下方固定控制面板；禁用原因清晰。
- 实际结果：布局符合“上方舞台、下方固定控制面板”；底部显示“未绑定席位 / 登录后才能参赛”和禁用发言按钮；未发现遮挡或横向滚动。
- 证据：`screenshots/20260717-005403/TC-404-匿名辩论页-窄屏.png`
- 控制台/网络现象：未获取
- 复现稳定性：必现
- 建议修复方向：路由权限问题见 DEF-002；布局本身正常。

### TC-405：Agent 后台窄屏响应式

- 状态：PASS
- 严重程度：无
- 页面/路由：`/debate`
- 测试身份：Agent Admin
- 前置条件：574×798 窄屏窗口，简易模式
- 操作步骤：检查顶部操作、统计卡片和模型服务表单。
- 预期结果：信息可读、表单不溢出、不显示 Key 明文。
- 实际结果：统计卡片双列重排，表单单列可读；API Key 仍为已配置/掩码；未发现横向滚动。
- 证据：`screenshots/20260717-005403/TC-405-Agent后台-窄屏.png`
- 控制台/网络现象：未获取
- 复现稳定性：必现
- 建议修复方向：无

### TC-406：未认证 POST 访问 Judge

- 状态：PASS
- 严重程度：无
- 页面/路由：`POST /debate/api/judge`
- 测试身份：未携带认证的本地 `data:` 表单
- 前置条件：空请求体，不包含辩论内容或敏感数据
- 操作步骤：从浏览器本地 `data:` 页面提交空 POST 表单至 Judge 接口。
- 预期结果：返回 403，不能绕过限制。
- 实际结果：页面返回 `403 Forbidden`。
- 证据：`screenshots/20260717-005403/TC-406-Judge未认证POST-403.png`
- 控制台/网络现象：未获取
- 复现稳定性：仅一次
- 建议修复方向：无

### TC-407：键盘 Tab 焦点顺序

- 状态：BLOCKED
- 严重程度：无
- 页面/路由：`/`
- 测试身份：匿名
- 前置条件：Safari 574×798
- 操作步骤：连续按 Tab 并通过辅助功能树读取焦点。
- 预期结果：焦点依次进入导航、链接和按钮，且有可见焦点状态。
- 实际结果：辅助功能树持续报告焦点停留在页面 HTML 内容；Safari 当前键盘导航设置可能未启用“Tab 高亮网页中的每一项”，无法可靠区分站点缺陷与浏览器设置。
- 证据：Computer Use 焦点序列
- 控制台/网络现象：未获取
- 复现稳定性：必现（当前环境）
- 建议修复方向：在启用完整键盘访问的浏览器环境回归；同时确保所有交互控件保留原生可聚焦语义和清晰的 `:focus-visible` 样式。

### TC-408：不存在房间的直接 URL

- 状态：PASS
- 严重程度：无
- 页面/路由：`/rooms/999999/watch`、`/rooms/999999/debate`、`/rooms/999999/control`
- 测试身份：匿名（主站）
- 前置条件：使用不存在的六位房间号
- 操作步骤：分别直接访问观战、辩论和控制 URL。
- 预期结果：显示准确错误，不出现其他房间数据或控制能力。
- 实际结果：三个 URL 均显示“页面暂时无法打开 / 比赛房间不存在或已被删除”；未出现已知房间数据或控制按钮。
- 证据：`screenshots/20260717-005403/TC-408-不存在房间-错误提示.png`
- 控制台/网络现象：未获取
- 复现稳定性：必现
- 建议修复方向：无

### TC-501：1v1 创建、准备、AI 补位与完整比赛

- 状态：PASS（语音质量问题另见 TC-506/DEF-003）
- 页面/路由：`/rooms/566139`、`/rooms/566139/debate`、`/rooms/566139/result`
- 测试身份：User A；AI 反方一辩“乾元”
- 实际结果：未准备时开始被拦截；准备后 AI 补位；立论、自由辩论、总结和 Judge 均完成。结果为反方胜，团队分 5:90，记录 9 次发言和 60 个时间线事件。辩题中的 `<b>` 标签按普通文本显示，未执行 HTML。
- 证据：`screenshots/20260717-005403/TC-501-1v1房间大厅-成功.png`、`TC-503-1v1未准备-开始禁用.png`、`TC-504-1v1准备完成.png`、`TC-507-1v1-AI立论-字幕.png`、`TC-510-1v1结果-Judge.png`

### TC-506：1v1 真人静音录音、ASR 与结果音频

- 状态：PASS（历史失败经二次部署修复）
- 严重程度：历史 P2，当前已关闭
- 操作：在真人轮次允许麦克风但不刻意说话，结束后查看字幕、完整记录和音频。
- 预期：静音/底噪不应生成有效演讲；不应进入 Judge；无有效音频时应明确标为无录音。
- 实际：首轮存在幻觉和 0:00；二次部署后房间 750374 静音录音进入“提交前核对发言文字”，提示“未检测到清晰语音，不会保存静音录音”，服务端 speech 内容为空、`audio_url` 为空，未进入 Judge。录音时文案为“当前设备正在发言”。
- 证据：历史失败 `TC-506-1v1麦克风-录音中.png`、`TC-511-1v1完整记录-下半页.png`；修复验证 `TC-912-二次部署-当前设备正在发言.png`、`TC-913-二次部署-静音拦截与人工核对.png`
- 关联验证：真实 TC-911 WebM 解码时长为 34.62 秒；API 171、Web 95、TypeScript、Ruff、compileall 和 Next.js production build 通过。

### TC-508：1v1 字幕缺失后的人工恢复

- 状态：PASS
- 实际结果：自由辩论无字幕时出现人工补文入口，提交 `QA-CU 测试发言：应明确标注人工智能生成内容。` 后比赛继续。
- 证据：`screenshots/20260717-005403/TC-508-1v1字幕缺失-文字恢复.png`

### TC-512：1v1 结果写入个人中心

- 状态：PASS
- 实际结果：`/me` 显示 1 条历史比赛、无进行中比赛；本场净积分为 0，QA 房间与赛果可打开。
- 证据：`screenshots/20260717-005403/TC-512-1v1个人中心-历史.png`

### TC-601：4v4 双用户并发加入、准备与 Agent 隔离

- 状态：PASS
- 页面/路由：`/rooms/764886`
- 测试身份：User A（Chrome 无痕，正方1辩）、User B（SunBrowser，正方2辩）
- 实际结果：两名用户实时看到各自席位和准备状态；房主开始后补齐 6 个 AI 席位；未观察到与已完成 1v1 房间 566139 的辩题、房号、字幕或控制状态串线。
- 证据：`screenshots/20260717-005403/TC-602-4v4双用户大厅-两席.png`、`TC-603-4v4双用户准备-开始可用.png`、`TC-604-4v4开场-AI补齐.png`

### TC-606：4v4 真人发言按钮跨浏览器无响应

- 状态：PASS（历史失败已修复）
- 严重程度：历史 P1，当前已关闭
- 实际结果：User B 在专属正方2辩和自由辩论轮次中看到启用的“开始发言”，AX 点击、坐标点击和 Enter 均无响应；将 SunBrowser 麦克风权限设为允许并刷新后仍复现。User A 在 Chrome 自由辩论轮次也复现相同行为；两名真人轮次均因超时丢失，但状态机继续由 AI 推进。
- 证据：`screenshots/20260717-005403/TC-606-UserB正方2辩-开始发言可用.png`、`TC-606-UserB自由辩论-开始发言无响应.png`、`TC-608-4v4Chrome自由辩论-开始发言无响应.png`
- 复现稳定性：两个独立浏览器、多个人类轮次稳定复现。
- 修复验证：发言启动使用可取消 generation/turn token，迟到权限、旧 ASR socket 和旧 MediaRecorder 尾块被隔离；房间 750374 生产回归成功进入录音态，显示本机状态，结束后安全进入人工核对；全量 Web 95 项含相关竞态用例。

### TC-801：4v4 房主控制台暂停、刷新恢复与权限

- 状态：PASS（有短暂错误体验观察）
- 实际结果：房主可暂停，剩余 3 秒冻结；刷新控制台后仍保持暂停和剩余时间；恢复后从 3 秒继续。User B 直接访问控制台不能获得控制权，最终重定向观战页；过程中短暂显示通用错误/重连。
- 证据：`screenshots/20260717-005403/TC-801-4v4控制台-运行状态.png`、`TC-802-4v4暂停刷新-状态保持.png`、`TC-107-UserB控制台-重定向观战.png`

### TC-610：4v4 Judge、结果、个人中心和排行榜结算

- 状态：PASS
- 实际结果：比赛自动进入结果页；正方胜 82:76，Judge 给出中文理由；记录 11 次发言、93 个时间线事件；User A 和 User B 均首次结算 +3。User A `/me` 显示累计积分 3、历史比赛 2、进行中 0；排行榜同时显示两名 QA 用户各 3 分、平均评分 82、有效场次 1。
- 音频回放：部署后 Chrome 元数据加载正常；SunBrowser 中真人首段与 AI 音频均实际播放并可暂停。
- 证据：`TC-610-4v4结果-Judge积分音频回放.png`、`TC-612-4v4排行榜-双用户积分.png`、`TC-903-部署后4v4结果-个人评分与音频元数据.png`、`TC-905-4v4部署回归-音频可播放.png`

### TC-615：4v4 完整时间线分页与归档下载

- 状态：PASS（历史 SunBrowser 失败已修复）
- 实际结果：首轮 User B 出现 `Failed to fetch`；部署后 SunBrowser 初始 50/93，点击加载后稳定达到 93/93，按钮消失；Chrome 同样达到 93/93。完整归档成功下载并通过结构校验。
- 归档证据：`docs/qa/archives/20260717-005403/debate-764886-b748a0dd-9ff2-48c6-8a04-b7896d9f7a3e.json`
- 失败/修复截图：`TC-615-4v4时间线加载更早事件-失败.png`、`TC-616-4v4时间线-UserA加载93成功.png`、`TC-904-部署后4v4时间线-93事件.png`、`TC-906-4v4部署回归-时间线93-93.png`
- 产品观察：所谓“完整归档”当前是 JSON 清单而非包含全部音频文件的 ZIP；11 条 speech 均有音频 URL，但仅 1 条上传音频作为 `audio_assets` 元数据，不足以独立离线复现全部音频，需定义研究归档的可携带性标准。

### TC-916：结果页音频互斥、离页停止与旧音频不复活

- 状态：PASS（历史 P2 已修复）
- 问题类型：BUG / UX
- 严重程度：历史 P2，当前已关闭
- 用户影响：学生、教师、观众
- 发生概率：修复前必现；修复后连续两条回归未复现
- 页面/路由：`/rooms/764886/result`
- 测试身份：User A，Chrome 无痕
- 前置条件：结果页至少有两条可播放 AI 发言。
- 操作步骤：
  1. 播放“见山”发言并确认其控件显示暂停。
  2. 在第一条仍播放时点击下一条“清和”发言。
  3. 检查所有媒体控件的播放/暂停状态。
  4. 在第二条播放时进入 `/me`，等待媒体状态收敛，再使用浏览器后退返回结果页。
- 预期结果：任意时刻最多一条发言播放；离页后全部停止；返回后没有旧音频自动复活。
- 实际结果：切换后“见山”恢复为播放，仅“清和”显示暂停；离页约 2.5 秒后 Chrome 标题不再显示正在播放音频；返回结果页没有暂停控件或自动播放。
- 证据：修复前 `screenshots/20260717-005403/cu-chrome-result-audio-switch-20260717.jpg`；修复后 `screenshots/20260717-005403/TC-916-结果页音频互斥-修复后.jpg`。
- 控制台/网络现象：未获取；服务器 Web stderr 为空。
- 复现稳定性：修复前稳定复现，修复后生产回归通过。
- 操作成本：2 次播放点击、1 次离页、1 次后退；无无反馈等待。
- 根因：每条原生 `<audio>` 独立控制，结果页未维护唯一 active media，也未在卸载时清理。
- 验收标准：连续切换任意两条音频时只有最新一条处于播放；路由变化 300ms 内调用 pause/reset；返回不自动播放。
- 修复状态：VERIFIED
- 关联改动/提交说明：结果页音频 registry/onPlay 互斥与卸载清理；Web `97/97`，release `20260716T205546Z-qa-audio-exclusive`。

### TC-917：SunBrowser 长结果页多音频元数据加载失败

- 状态：FAIL / CLOSED（修复前证据）
- 问题类型：BUG / PERF / UX
- 严重程度：P2
- 用户影响：学生、教师、观众、低性能/隔离浏览器用户
- 发生概率：当前 SunBrowser 刷新后稳定复现
- 页面/路由：`/rooms/764886/result`
- 测试身份：User B，SunBrowser 独立会话
- 前置条件：结果页包含 11 条带音频的发言，页面为修复后 release。
- 操作步骤：刷新结果页，读取全部原生媒体控件状态，尝试播放第一条和后续条目。
- 预期结果：所有存在且服务器可访问的音频均可按需播放；未播放条目不应因预加载资源限制被禁用。
- 实际结果：刷新后 10 条出现 `Unable to play media`，后续多条 play/mute 控件禁用；第一条已缓存音频仍可播放。Chrome 同页没有该失败。
- 证据：`screenshots/20260717-005403/TC-917-SunBrowser-结果音频控件失败.jpg`；Computer Use 可访问性树计数；11 个媒体 URL 的 32-byte Range 请求全部返回 HTTP 206，Content-Type 正确。
- 控制台/网络现象：DevTools 未获取；终端逐条 Range 健康，倾向为浏览器并发 `preload=metadata`/解码器资源问题，仍标记为推测。
- 复现稳定性：刷新后稳定复现，失败数在 9–11 条间变化。
- 操作成本：1 次刷新、1 次播放；页面无解释性错误或恢复入口。
- 建议方案：避免同时初始化所有原生媒体；使用按需加载或单一共享播放器，列表中用服务端 duration 展示时长；保持互斥、离页停止和 Range 支持。
- 验收标准：SunBrowser/Chrome 对 11 条音频刷新后均无 `Unable to play media`；任意前中后条目首次点击可播放；仅一条播放；离页停止。
- 修复状态：VERIFIED BY TC-918
- 关联改动/提交说明：所有结果音频改为 `preload="none"`，使用服务端时长常驻展示，保留互斥/离页清理；release `20260716T211321Z-qa-audio-lazy`。

### TC-918：SunBrowser 11 条音频按需加载与首中末实际播放

- 状态：PASS
- 问题类型：BUG / PERF / UX 修复回归
- 严重程度：无（历史 P2 已关闭）
- 用户影响：学生、教师、观众、隔离浏览器用户
- 发生概率：修复后首/中/末三条均通过
- 页面/路由：`/rooms/764886/result`
- 测试身份：User B，SunBrowser 独立会话
- 前置条件：无缓存刷新至 release `20260716T211321Z-qa-audio-lazy`，结果页含 11 条音频。
- 操作步骤：
  1. 无缓存刷新，确认 11 个 play 按钮均未禁用，列表显示 11 个服务端录音时长。
  2. 播放第 1 条，确认产生播放进度。
  3. 播放第 6 条，确认第 1 条恢复为 play，只有第 6 条为 pause。
  4. 播放第 11 条，确认第 6 条恢复为 play，只有末条为 pause。
- 预期结果：未点击时不并发预载媒体；任意条目首次点击可加载；切换互斥；时长无需预载即可可见。
- 实际结果：11 条 play 均可用；第 1/6/11 条实际播放成功并有进度；切换后始终只有一个 pause；服务端时长分别正常显示。
- 证据：`screenshots/20260717-005403/TC-918-SunBrowser-按需音频首中末可播放.jpg`；Computer Use 控件/进度状态。
- 控制台/网络现象：服务器所有媒体 Range 206；主站/结果/API health 200；Web stderr 为空。
- 复现稳定性：三条不同位置连续通过。
- 操作成本：3 次播放点击，无额外等待提示缺失。
- 验收标准：长列表所有 play 可用，首/中/末可播放且互斥；Chrome 邻近回归不退化。
- 修复状态：VERIFIED
- 关联改动/提交说明：`preload="none"`、服务端时长、ARIA label、移动端换行；Web `98/98`。

### TC-919：生产 LightTTS Redis 全局门禁真实双任务 canary

- 状态：PASS
- 问题类型：PERF / RELIABILITY 修复回归
- 严重程度：无（课堂容量 P1 仍未关闭）
- 用户影响：所有 AI 发言参赛者、观众、教师
- 发生概率：启用后双任务稳定通过
- 页面/路由：生产 API/Engine/Redis/LightTTS；浏览器主页与结果页邻近回归
- 测试身份：QA 运维 canary；未使用真实用户房间
- 前置条件：生产无 `preparing/running/judging` 房间；Redis 无 admission key；完整源文件与 `.env` 已备份；门禁默认关闭代码先启动健康。
- 操作步骤：
  1. 运行真实 Redis 三进程 shadow，验证排队、取消、deadline、lease lost、response-loss 与 key 清理，不调用 LightTTS HTTP。
  2. 使用进程级 flag 对真实 LightTTS 并发提交两个短句，确认一个 active、一个 queued。
  3. 写入生产 flag，重启 API/Engine，再次提交两个真实短句。
  4. 检查 WAV、active/queue、ready、Supervisor 和浏览器主页/结果页。
- 预期结果：真实 GPU 同时最多一个任务；第二任务公平排队；两个音频有效；完成后无 active/queued/ghost key；Redis 故障 fail closed；生产页面不退化。
- 实际结果：2 个任务 4.233 秒完成，`maximum_active=1`、`maximum_queue_depth=1`；两个 RIFF WAV 为 2.44/2.60 秒；最终 active/queue 均 0；ready 200 且 admission enabled/ok/fail-closed。
- 证据：`audio/20260717-005403/AUDIO-TTS-GATE-PROD-CANARY.json`、`AUDIO-TTS-REDIS-GATE-P1-FINAL.json`；远端 `runtime/lighttts-gate-enabled-canary-20260716T2210Z.json`。
- 控制台/网络现象：首次 `.env` 原子更新因属主变为 root 导致 API 一次 spawn error；恢复 `ubuntu:ubuntu 0600` 后通过。没有活动比赛受到影响；Engine 在修复前尚未重启。
- 复现稳定性：启用前进程级 canary 与启用后生产 canary 各通过一次；Redis shadow 多进程通过两轮。
- 操作成本：两轮实际 canary，各约 4–5 秒；一次受控 API/Engine 重启。
- 根因推测：无；门禁以 Redis token lease + FIFO ZSET 实现，响应丢失按同 token 恢复。
- 建议方案：保留当前 fail-closed 与 whole-job deadline；后续增加 queue position/ETA、跨进程累计指标和 4/10/20 房隔离容量试验。
- 验收标准：本用例指标持续满足；任一真实任务不得观察到 active>1；完成/取消/重启后无 ghost key；ready 反映 Redis 故障。
- 修复状态：VERIFIED（容量 P1 部分缓解，未关闭）
- 关联改动/提交说明：LightTTS admission gate、whole-job deadline、cancel drain/abandon、response-loss recovery、readiness；回滚包与 SHA 见部署章节。

### TC-920：门禁启用后 Computer Use 结果页刷新与音频互斥

- 状态：PASS
- 问题类型：功能 / 浏览器邻近回归
- 严重程度：无
- 用户影响：学生、教师、观众
- 发生概率：连续切换通过
- 页面/路由：`/`、`/rooms/764886/result`
- 测试身份：User A，Chrome 无痕
- 前置条件：生产 API/Engine 已以 `LIGHTTTS_GLOBAL_GATE_ENABLED=true` 重启，health/ready 200。
- 操作步骤：
  1. Computer Use 打开主页，确认赛事、公开观战和登录态正常。
  2. 打开结果页，确认 11 条发言、裁判结果、积分与音频控件渲染。
  3. 播放第一条音频，确认按钮变为 pause 且进度增长。
  4. 播放第二条，确认第一条恢复 play、第二条为 pause；再暂停第二条。
- 预期结果：API/Engine 重启与门禁启用不影响页面、结果数据或媒体；同页只能播放一条音频。
- 实际结果：首页与结果页正常；第一条进度可见；切换第二条后第一条立即恢复 play，始终只有一个 pause；最后手动停止。
- 证据：`screenshots/20260717-005403/TC-919-LightTTS门禁启用后-结果页刷新正常.jpg`、`TC-920-LightTTS门禁启用后-结果音频互斥.jpg`；Computer Use 可访问性树记录按钮/进度变化。
- 控制台/网络现象：DevTools 未获取；服务器 health/ready 200，admission active/queue 均回到 0。
- 复现稳定性：第一→第二连续切换通过。
- 操作成本：2 次导航、3 次音频点击、无异常等待。
- 根因推测：无。
- 建议方案：继续保留单播放器互斥与离页清理；后续在真实新比赛的 AI 阶段补测队列位置/等待文案。
- 验收标准：页面可用；任一切换后只有最新音频播放；离页不复活；服务健康无退化。
- 修复状态：VERIFIED
- 关联改动/提交说明：本轮无 Web 改动；验证 API/Engine TTS gate 部署未破坏既有 Web 音频行为。

### TC-921：Engine task drain 加固部署后结果页刷新

- 状态：PASS
- 问题类型：RELIABILITY / 部署邻近回归
- 严重程度：无
- 用户影响：学生、教师、观众
- 页面/路由：`/rooms/764886/result`
- 测试身份：User A，Chrome 无痕
- 前置条件：生产无 `preparing/running/judging` 房；仅替换 `match_engine.py` 并重启 Engine，API/Web/Worker 未重启；ready 200。
- 操作步骤：Computer Use 刷新结果页，检查胜负、比分、裁判评议、11 条发言、93 个事件、个人分与归档入口。
- 预期结果：Engine 生命周期加固不影响既有结果数据和页面加载；旧音频不自动复活。
- 实际结果：页面完整刷新，正方 82:76、11 条发言和 93 个事件一致；音频进度归零且没有自动播放。
- 证据：[TC-921 截图](screenshots/20260717-005403/TC-921-Engine任务收尾部署后-结果页刷新正常.jpg)；Computer Use 可访问性树。
- 复现稳定性：部署后刷新一次通过。
- 验收标准：结果页可用、数据不变、无旧音频复活、生产 ready 持续 200。
- 修复状态：VERIFIED

### TC-922：Engine task drain 加固部署后首页与公开观战入口

- 状态：PASS
- 问题类型：RELIABILITY / 浏览器邻近回归
- 严重程度：无
- 用户影响：匿名访客、学生、教师
- 页面/路由：`/`
- 测试身份：User A，Chrome 无痕
- 前置条件：与 TC-921 相同；Engine 已重新建立 heartbeat。
- 操作步骤：Computer Use 从结果页导航首页，检查赛事卡、进行中计数、公开观战房、排行榜与登录态。
- 预期结果：首页正常渲染；既有暂停房 278571 仍只作为公开观战入口，不被 Engine 重启改变。
- 实际结果：4v4 显示 1 场进行中、1v1 为 0；公开房 278571、排行榜和 User A 登录态正常。
- 证据：[TC-922 截图](screenshots/20260717-005403/TC-922-Engine任务收尾部署后-首页正常.jpg)；Computer Use 可访问性树。
- 复现稳定性：部署后导航一次通过。
- 验收标准：首页无白屏/错误，公开房与用户状态不退化，Engine/Worker/LightTTS health 正常。
- 修复状态：VERIFIED

### TC-923：课堂/Consent 发布后首页与既有登录会话不退化

- 状态：PASS
- 问题类型：RELIABILITY
- 严重程度：无
- 用户影响：全体用户
- 发生概率：高
- 页面/路由：`/`
- 测试身份：User A，Chrome 无痕
- 前置条件：生产已从 Alembic 0012 升级到 0017，并切换 Web release `20260717T0816Z-qa-classroom-consent`。
- 操作步骤：
  1. 使用 Computer Use 刷新生产首页。
  2. 检查赛事卡、公开观战、排行榜摘要和 User A 登录态。
- 预期结果：发布不影响既有公开页面和登录会话。
- 实际结果：首页完整渲染；User A 会话、4v4/1v1 卡片、公开房 278571 和排行榜摘要正常。
- 证据：[TC-923 截图](screenshots/20260717-005403/TC-923-生产部署后-首页与登录会话.png)
- 控制台/网络现象：DevTools 未获取；同时间服务器 `/` HTTP 200，约 14.9ms。
- 复现稳定性：部署后一次刷新通过，邻近 TC-921/922 已有两次相同页面回归。
- 操作成本：1 次刷新；明显等待 1 次；最长无反馈约 1 秒；不需猜测下一步。
- 根因推测：无。
- 建议方案：继续把首页作为每次 schema/Web 发布后的固定 smoke route。
- 验收标准：首页无白屏/错误、登录态不丢失、公开数据不被课堂迁移污染。
- 修复状态：VERIFIED
- 关联改动/提交说明：课堂/Consent/Teacher/0013–0017 灰度发布的邻近回归。

### TC-924：QA 赋权前教师工作台安全拒绝

- 状态：PASS
- 问题类型：SECURITY / CONTENT
- 严重程度：无
- 用户影响：普通用户、教师
- 发生概率：高
- 页面/路由：`/teacher`
- 测试身份：User A，Chrome 无痕；此时尚无组织/课堂 membership
- 前置条件：教师 API/Web 已发布，QA provisioning 尚未 apply。
- 操作步骤：直接访问 `/teacher`。
- 预期结果：无 scoped teacher 权限时不显示课堂数据，不出现白屏或伪授权。
- 实际结果：显示“教学活动功能未开放”和可恢复说明；没有课堂、名单、房间或创建表单数据。
- 证据：[TC-924 截图](screenshots/20260717-005403/TC-924-QA租户配置前-教师工作台权限提示.png)
- 控制台/网络现象：DevTools 未获取；路由 HTTP 200，授权由后端 scoped discovery 决定。
- 复现稳定性：必现，随后 provisioning 后同会话即时变为授权状态。
- 操作成本：1 次导航；1 次等待；最长无反馈约 1 秒；无需猜测恢复路径。
- 根因推测：无。
- 建议方案：把标题进一步改为“你没有课堂教师权限”，避免与服务器功能未启用混为一谈。
- 验收标准：无 membership 的普通用户不能读取任何课堂实体，页面给出安全、可理解的下一步。
- 修复状态：VERIFIED
- 关联改动/提交说明：Teacher scoped discovery 与权限空状态。

### TC-925：QA 赋权前政策管理安全拒绝

- 状态：PASS
- 问题类型：SECURITY
- 严重程度：无
- 用户影响：普通用户、组织管理员
- 发生概率：高
- 页面/路由：`/teacher/consents`
- 测试身份：User A，Chrome 无痕；此时尚无 owner/admin membership
- 前置条件：政策管理 API/Web 已发布，QA provisioning 尚未 apply。
- 操作步骤：直接访问 `/teacher/consents`。
- 预期结果：无 owner/admin scope 时不能发现组织或读取政策正文。
- 实际结果：显示“无政策管理权限”；导航不出现教学活动/政策管理；未泄露组织和政策信息。
- 证据：[TC-925 截图](screenshots/20260717-005403/TC-925-QA租户配置前-政策管理权限提示.png)
- 控制台/网络现象：DevTools 未获取；服务端 scoped organization discovery 返回空授权集合。
- 复现稳定性：必现，随后 owner_teacher provisioning 后同会话即时生效。
- 操作成本：1 次导航；1 次等待；最长无反馈约 1 秒。
- 根因推测：无。
- 建议方案：保留当前不泄露组织存在性的统一空状态。
- 验收标准：普通用户/teacher/student/member/outsider 均不能读取政策正文或发布版本。
- 修复状态：VERIFIED
- 关联改动/提交说明：Consent policy admin scoped API 与前端访问拒绝态。

### TC-926：QA owner_teacher 权限无需重新登录即时生效

- 状态：PASS
- 问题类型：SECURITY / UX
- 严重程度：无
- 用户影响：组织 owner、教师
- 发生概率：高
- 页面/路由：`/teacher/consents`
- 测试身份：User A，Chrome 无痕
- 前置条件：同一登录会话中完成 QA provisioning；User A 仅新增 org owner + classroom teacher，global role 仍为 `user`。
- 操作步骤：
  1. 在原权限拒绝页面刷新。
  2. 检查全局导航、组织选择和三类政策卡。
- 预期结果：后端每次请求查 membership；不要求退出重登；只出现被授权 QA 组织。
- 实际结果：导航即时出现“教学活动/政策管理”；仅发现 `QA 自动化学校 20260717 · Owner`；recording/research_use/public_display 三类均显示尚未发布及门禁说明。
- 证据：[TC-926 截图](screenshots/20260717-005403/TC-926-QA组织赋权后-政策管理即时生效.png)
- 控制台/网络现象：DevTools 未获取；生产 apply/replay 后 ready 200、核心服务 RUNNING。
- 复现稳定性：一次 scope 变更与一次刷新通过。
- 操作成本：1 次刷新；明显等待 1 次；最长无反馈约 1 秒。
- 根因推测：无。
- 建议方案：长期保留 request-time membership 判定，避免把组织角色写入长期 session claim。
- 验收标准：新增/撤销 membership 在下一请求生效；global role 不扩权；跨组织数据不可见。
- 修复状态：VERIFIED
- 关联改动/提交说明：QA provisioning `owner_teacher` 与 Consent policy organization discovery。

### TC-927：QA 教师工作台展示 scoped 课堂与隐私门状态

- 状态：PASS
- 问题类型：UX / PRIVACY
- 严重程度：无
- 用户影响：教师、组织者
- 发生概率：高
- 页面/路由：`/teacher`
- 测试身份：User A，Chrome 无痕
- 前置条件：QA organization/classroom 已 active；User A 为 org owner + classroom teacher；尚未发布 recording policy。
- 操作步骤：从政策管理进入教学活动，检查课堂列表、活动创建表单和隐私提示。
- 预期结果：只显示 scoped QA 课堂；明确提示录音政策未就绪和批量房默认私密。
- 实际结果：显示 `QA 灰度班 / QA 自动化学校 20260717`；创建表单可选 4v4；明确写明“录音政策未就绪”和“所有房间默认私密、仅活动名单学生预分配席位”。
- 证据：[TC-927 截图](screenshots/20260717-005403/TC-927-QA组织赋权后-教师工作台与私密建房提示.png)
- 控制台/网络现象：DevTools 未获取；服务器 `/teacher` HTTP 200，约 11.1ms。
- 复现稳定性：一次导航通过。
- 操作成本：1 次点击；1 次等待；最长无反馈约 1 秒；下一步清晰。
- 根因推测：无。观察项：列表标题旁“0 个”语义可能被理解为 0 个课堂，实际表示暂无活动。
- 建议方案：把计数文案改为“0 个活动”或分别显示课堂数/活动数，减少首次使用歧义。
- 验收标准：教师只能看到本课堂；隐私门和默认 private 明示；计数含义无歧义。
- 修复状态：VERIFIED（计数文案作为 P3 CONTENT 观察项开放）
- 关联改动/提交说明：Teacher workspace 与 scoped classroom discovery。

### TC-928：学生账号不能访问教师工作台

- 状态：PASS
- 问题类型：SECURITY
- 严重程度：无
- 用户影响：学生、教师
- 发生概率：高
- 页面/路由：`/teacher`
- 测试身份：User B，SunBrowser；org member + classroom student，global role=`user`
- 前置条件：User B 已在 QA 课堂，但没有 teacher membership。
- 操作步骤：直接访问 `/teacher`。
- 预期结果：课堂学生不能看到教师导航、活动列表、名单或批量建房能力。
- 实际结果：主导航只有赛事大厅/排行榜；页面显示无课堂教师权限；未泄露 QA 课堂数据。
- 证据：[TC-928 截图](screenshots/20260717-005403/TC-928-学生账号-教师工作台拒绝且无管理导航.png)
- 控制台/网络现象：DevTools 未获取。
- 复现稳定性：必现。
- 操作成本：1 次导航；1 次等待；最长无反馈约 1 秒。
- 根因推测：无。
- 建议方案：保持课堂 student 与 teacher 权限完全分离。
- 验收标准：student 即使同组织同课堂也不能读取 Teacher API 或看到管理入口。
- 修复状态：VERIFIED
- 关联改动/提交说明：ClassroomMembership role enforcement 与条件导航。

### TC-929：学生账号不能访问政策正文管理

- 状态：PASS
- 问题类型：SECURITY / PRIVACY
- 严重程度：无
- 用户影响：学生、组织管理员
- 发生概率：高
- 页面/路由：`/teacher/consents`
- 测试身份：User B，SunBrowser；org member + classroom student
- 前置条件：与 TC-928 相同。
- 操作步骤：直接访问 `/teacher/consents`。
- 预期结果：学生不能发现 policy-admin 组织集合、读取政策管理正文或发布版本。
- 实际结果：显示“无政策管理权限”；无管理导航、组织选择、正文和发布按钮。
- 证据：[TC-929 截图](screenshots/20260717-005403/TC-929-学生账号-政策管理拒绝且无管理导航.png)
- 控制台/网络现象：DevTools 未获取。
- 复现稳定性：必现。
- 操作成本：1 次导航；1 次等待；最长无反馈约 1 秒。
- 根因推测：无。
- 建议方案：学生侧只在具体 consent 决策控件展示当前政策正文，不复用管理员页面。
- 验收标准：student/member/teacher 访问 policy admin API 为 403 或空授权；页面不泄露组织政策。
- 修复状态：VERIFIED
- 关联改动/提交说明：Consent policy admin role matrix与条件导航。

### TC-930：不可覆盖政策发布弹窗的确认、时区与键盘安全门

- 状态：PASS
- 问题类型：UX / A11Y / SECURITY
- 严重程度：无
- 用户影响：组织 owner/admin、辅助技术用户
- 发生概率：高
- 页面/路由：`/teacher/consents`
- 测试身份：User A，Chrome 无痕
- 前置条件：QA 组织没有 recording policy；User A 具有 owner 权限。
- 操作步骤：
  1. 点击课堂录音“发布首个版本”。
  2. 检查标题、完整正文、生效时间和确认控件默认状态。
  3. 不勾选、不提交，按 Escape 关闭。
  4. 检查焦点是否回到原“发布首个版本”按钮。
- 预期结果：确认默认未勾选且提交禁用；明确 Asia/Shanghai→UTC；Escape 关闭并恢复焦点；不产生政策版本。
- 实际结果：正文和带时区生效时间可编辑；确认值为 0，“确认发布新版本”禁用；Escape 关闭，焦点恢复到原触发按钮；未提交任何版本。
- 证据：[TC-930 截图](screenshots/20260717-005403/TC-930-录音政策发布弹窗-完整正文时区与默认未确认.png)；Computer Use 可访问性树记录 disabled 按钮与焦点恢复。
- 控制台/网络现象：DevTools 未获取；没有发出发布请求。
- 复现稳定性：一次完整打开/关闭通过；Web 自动测试覆盖 focus trap/Escape/焦点恢复。
- 操作成本：1 次打开、1 次 Escape；无明显等待；无需猜测不可逆后果。
- 根因推测：无。
- 建议方案：发布后继续在卡片上显示 version、UTC/本地双时间和旧授权失效范围。
- 验收标准：未显式确认无法提交；服务端仍要求 `confirmation=publish_new_version` 与 timezone-aware `effective_at`；键盘焦点不逃逸或丢失。
- 修复状态：VERIFIED
- 关联改动/提交说明：Consent policy immutable version modal、服务端确认字段与时间校验。

### TC-931：学生在课堂批量建房前没有可达的录音同意入口

- 状态：FAIL
- 问题类型：BUG / UX / PRIVACY
- 严重程度：P1
- 用户影响：课堂学生、教师、组织者
- 发生概率：高
- 页面/路由：`/me`、`/teacher`、Activity `rooms:provision`
- 测试身份：User B（classroom student，SunBrowser）与 User A（owner_teacher）
- 前置条件：QA 组织/课堂与 scoped membership 已生产；尚无 QA Activity 房间；批量建房要求所有名单学生先有当前 recording policy grant。
- 操作步骤：
  1. 以 User B 打开 `/me`，检查课堂、待处理同意和政策入口。
  2. 检查现有 Consent 控件的可达页面。
  3. 对照 Teacher provision 门：建房前要求 current recording grant。
- 预期结果：学生在房间创建前即可从 `/me` 或课堂预检页看到组织/课堂、完整政策正文，自主 grant/revoke；教师随后才能批量建房。
- 实际结果：`/me` 只有比赛档案、账号安全和历史比赛，没有课堂或录音同意入口；现有控件仅在房间 lobby/debate，而房间又必须在 grant 后才能创建，形成闭环阻塞。
- 证据：[TC-931 截图](screenshots/20260717-005403/TC-931-学生个人中心-缺少课前录音同意入口.png)；服务端 `require_activity_recording_consents()` 在 `rooms:provision` 前置执行；组件仅被 lobby/debate 引用。
- 控制台/网络现象：DevTools 未获取；生产数据库只读核验 policy/activity/room 均为 0，未通过绕过 UI 的方式替学生授权。
- 复现稳定性：必现。
- 操作成本：学生无可完成路径；无限等待/必须找开发者或管理员绕过。
- 根因推测：Consent UI 最初以房间为入口实现，未覆盖课堂批量建房的 pre-room 业务顺序。
- 建议方案：新增 self-only active classroom consent discovery，并在 `/me` 增加“我的课堂与待处理同意”；复用全文、默认未勾选、版本、grant/revoke、换版和键盘安全门。
- 验收标准：学生无需房间号即可在建房前自行决定；停用 membership 立即消失；teacher/outsider 看不到学生条目；grant 后 dashboard ready=true，revoke 后新的建房/录音立即阻止。
- 修复状态：VERIFIED（入口循环关闭；完整政策/同意/建房链仍在 GAP-PRIVACY/CLASS）
- 关联改动/提交说明：Iteration 11 正在实现 API discovery 与 `/me` 入口。

### TC-932：学生个人中心提供建房前录音同意入口

- 状态：PASS
- 问题类型：PRIVACY / UX / SECURITY
- 严重程度：无（关闭 TC-931 的入口循环）
- 用户影响：课堂学生、教师
- 发生概率：高
- 页面/路由：`/me`、`GET /api/consents/me/classrooms`
- 测试身份：User B，SunBrowser，active classroom student
- 前置条件：0018/新 Web 已生产发布；QA org/class active；尚未发布 recording policy。
- 操作步骤：强制刷新 `/me`，检查课堂、组织、政策状态和历史表邻近布局。
- 预期结果：建房前可发现本人有效课堂；无 policy 时明确等待，不泄露其他课堂；不自动弹空 dialog。
- 实际结果：出现“我的课堂与待处理同意”，仅显示 QA 灰度班/QA 自动化学校；badge“等待政策”，明确课堂建房与新录音保持关闭；历史表“操作”表头具名。
- 证据：[TC-932 截图](screenshots/20260717-005403/TC-932-学生个人中心-课前录音同意入口已上线.png)
- 控制台/网络现象：DevTools 未获取；生产 ready 200、schema 0018。
- 复现稳定性：强制刷新后必现；普通刷新第一次命中旧静态缓存，硬刷新后加载新 release。
- 操作成本：1 次硬刷新；最长无反馈约 2 秒；下一步明确。
- 根因推测：无。
- 建议方案：后续加入课堂邀请/活动时间和待处理数量摘要。
- 验收标准：active student scope 才显示；停用 membership 下一请求消失；policy/grant/revoke/换版均可在此完成。
- 修复状态：VERIFIED
- 关联改动/提交说明：student consent discovery + `/me` pre-room Consent 区；release `20260717T0900-research-consent`。

### TC-933：LightTTS 单槽 FIFO 机制的 4/10/20 隔离容量

- 状态：PASS
- 问题类型：PERF / RELIABILITY
- 严重程度：无（仅机制层）
- 用户影响：多房间参赛者
- 发生概率：高
- 页面/路由：隔离 Redis prefix + 可控 mock endpoint + 真实 平台 provider/admission
- 测试身份：非浏览器容量脚本
- 前置条件：生产 processing room=false，production gate active/queue=0；未启动真实 GPU 模型。
- 操作步骤：独立运行 4、10、20 jobs，并覆盖 queued cancel、active cancel、queue timeout。
- 预期结果：FIFO 无越序、产物原子有效、取消/超时无 ghost key。
- 实际结果：4/10/20 全部接受，34/34 WAV valid，overtake=0、`.part`=0；queue P95 1.111/3.141/6.465s，E2E P95 1.404/3.386/6.713s；三类异常均按预期清理。
- 证据：[容量汇总](audio/20260717-005403/lighttts-capacity-4-10-20/lighttts-capacity-summary.md)
- 控制台/网络现象：生产 gate 全程 0/0；隔离 prefix 最终 0/0。
- 复现稳定性：一次完整固定脚本通过。
- 操作成本：约 15 秒；无生产用户影响。
- 根因推测：无。
- 建议方案：保留 FIFO/取消/原子发布；不要把 mock 延迟外推为真实模型吞吐。
- 验收标准：0 越序、0 无效 WAV、0 ghost key、0 production gate activity。
- 修复状态：VERIFIED
- 关联改动/提交说明：`benchmark_lighttts_capacity.py` 与 admission position/ETA estimator。

### TC-934：当前生产 LightTTS pending=2 无法承受课堂同步突发

- 状态：FAIL
- 问题类型：PERF / GAP
- 严重程度：P1
- 用户影响：多班级、大量学生
- 发生概率：高
- 页面/路由：生产参数模拟 `max_active=1,max_pending=2,queue_timeout=20`
- 测试身份：非浏览器隔离容量脚本
- 前置条件：与 TC-933 相同；只复用生产参数，不调用生产 gate/GPU。
- 操作步骤：对 4/10/20 同步 jobs 执行 admission。
- 预期结果：课堂容量应在开赛前可预测；已允许开赛的房间不应在发言时大比例立即失败。
- 实际结果：三档都只接受 2 个；queue_full 分别 2/8/18，拒绝率 50%/80%/90%。mock 很快所以 timeout=0，不能外推真实模型。
- 证据：[生产参数容量证据](audio/20260717-005403/lighttts-capacity-4-10-20/lighttts-capacity-summary.md)
- 控制台/网络现象：production gate 未被触碰。
- 复现稳定性：固定参数必现。
- 操作成本：用户侧当前没有排队位置/校准 ETA，失败发生在核心发言链。
- 根因推测：生产 `max_pending=2` 是过载保护，不是课堂容量调度方案。
- 建议方案：开赛批次容量闸门、房间级公平调度、可见 queue position/校准 ETA、延后/恢复路径；不要简单放大无界队列。
- 验收标准：隔离真实模型 4/10/20 的核心房间失败率与 P95 达标，容量不足在开赛前明示。
- 修复状态：OPEN
- 关联改动/提交说明：新增全局 next position 与显式未校准 ETA；每房可见性和真实容量仍未实现。

### TC-935：研究批量导出 0018 权限、匿名化与 artifact 安全

- 状态：PASS
- 问题类型：DATA / SECURITY / PRIVACY
- 严重程度：无（后端 MVP）
- 用户影响：教师、研究人员、系统管理员
- 发生概率：中
- 页面/路由：`/api/research/exports*`
- 测试身份：scoped teacher、org researcher、system_admin、outsider
- 前置条件：本地 0017 数据库与测试媒体；research_use policy/grants 按场景设置。
- 操作步骤：创建/重放 job，构建并下载 ZIP；检查 scoped filters、伪名、文件清单、撤回、break-glass、traversal/symlink/tamper/stale-running。
- 预期结果：只导出授权范围与当前同意数据；不打包媒体；结构化身份稳定伪名；artifact 和下载重验安全。
- 实际结果：专项及邻近回归 41 passed；完整 API 249 passed。ZIP 仅 manifest/matches/speeches/media checksums；撤回/换版后普通下载 410；异常信息去敏，request/download 审计完整。
- 证据：`apps/api/tests/test_research_exports.py`；Alembic 0017→0018 shadow。
- 控制台/网络现象：无生产数据导出；生产未登录路由 401。
- 复现稳定性：自动化必现。
- 操作成本：异步 job，不阻塞 HTTP 大包；尚无用户 UI。
- 根因推测：无。
- 建议方案：补 `/teacher/research-exports` UI 与 artifact retention/expiry。
- 验收标准：真实 QA activity 生成/下载/撤回失效通过，且 ZIP 逐文件 checksum 匹配。
- 修复状态：FIXED / PRODUCTION API DEPLOYED
- 关联改动/提交说明：0018、ResearchExportJob/ResearchSubjectIdentity、router/service/worker。

### TC-936：0018 与学生 Consent 入口的生产发布和 shadow

- 状态：PASS
- 问题类型：RELIABILITY / DEPLOYMENT
- 严重程度：无
- 用户影响：全体用户
- 发生概率：高
- 页面/路由：ready、`/me`、`/teacher*`、research/consent API
- 测试身份：服务探针、User B
- 前置条件：完整质量门、数据库/source backup、无 active processing room；保留 paused 房 278571。
- 操作步骤：三轮 PostgreSQL shadow 校正测试装置后通过；生产 0017→0018，重启 API/Engine/Worker，Web shadow 后切换 release，最终健康与 Computer Use 回归。
- 预期结果：迁移/发布可回滚，既有比赛和服务不退化。
- 实际结果：schema 0018、服务 RUNNING、ready 200、LightTTS 0/0、Worker dead letter 0；User B `/me` 新入口正常；paused 房未操作。
- 证据：部署/备份章节、TC-932；package/backup SHA 均已记录。
- 控制台/网络现象：Web shadow 首个连接有一次预期 connection refused 后成功；无持续 stderr 错误。
- 复现稳定性：生产发布一次通过，shadow 最终完整通过。
- 操作成本：服务重启约 1 分钟；页面无异常长等待。
- 根因推测：前两次 shadow 失败均为 harness（恢复对象 owner、正常 stderr 判定），非迁移/产品故障。
- 建议方案：固化 shadow restore role 与 stderr error-pattern verifier。
- 验收标准：ready/schema/routes/服务/浏览器均通过，回滚点完整。
- 修复状态：VERIFIED
- 关联改动/提交说明：release `20260717T0900-research-consent`，Alembic 0018。

### TC-937：未获确认时政策发布保持无生产副作用

- 状态：PASS
- 问题类型：SECURITY / PRIVACY
- 严重程度：无
- 用户影响：学生、教师、管理员
- 发生概率：高
- 页面/路由：`/teacher/consents`
- 测试身份：User A（QA 组织 owner + classroom teacher）
- 前置条件：生产 QA 组织尚无 policy；政策编辑器曾进入最终提交前但未获得 action-time 明确确认。
- 操作步骤：停止提交；09:24 CST 重新读取 Chrome 当前可访问性树，检查正文、确认框和提交按钮；核对页面未出现已发布版本。
- 预期结果：未确认时不得发布不可覆盖版本，也不得留下 policy/grant 副作用。
- 实际结果：编辑器回到默认标题、空正文、未勾选状态，提交按钮禁用；未发送发布请求。随后组织者确认线上政策不是必要流程，本轮不再发布。
- 证据：`screenshots/20260717-005403/TC-937-政策发布弹窗-未提交且草稿已重置.jpg`
- 控制台/网络现象：DevTools 未获取；页面无成功通知或版本卡片。
- 复现稳定性：本次安全门一次通过。
- 操作成本：0 次提交，0 次生产写入。
- 根因推测：草稿重置来自页面刷新/重新渲染；不影响“未提交”结论。
- 建议方案：保持所有真实 policy/evidence 创建的 action-time 明确确认。
- 验收标准：无明确确认时数据库 policy/grant 数量不变，提交按钮不能被误触发。
- 修复状态：VERIFIED
- 关联改动/提交说明：无生产改动。

### TC-938：管理型课堂/Consent 流程与实际学生自助赛事目标不匹配

- 状态：FAIL
- 问题类型：GAP / UX / PRIVACY
- 严重程度：P1
- 用户影响：学生、教师、产品运营
- 发生概率：高
- 页面/路由：`/teacher`
- 测试身份：User A（QA 组织 owner + classroom teacher）
- 前置条件：组织者已在线下完成学生签署；生产 QA 组织未发布线上 recording policy。
- 操作步骤：打开教师工作台；检查 QA 课堂状态和创建活动的课堂选项；评估能否登记或引用线下签署证明。
- 预期结果：产品范围应与实际使用方式一致；学生从赛事大厅直接创建正式赛或训练赛，不被课堂、政策、研究或容量编排入口干扰。
- 实际结果：课堂固定显示“尚未发布录音政策/录音政策未就绪”，页面没有线下证明入口，后续批量私密建房仍会被线上 grant 规则阻断。
- 证据：`screenshots/20260717-005403/TC-938-教师活动-仅支持线上政策导致线下签署仍显示未就绪.jpg`
- 控制台/网络现象：DevTools 未获取；页面本身明确暴露单一线上路径。
- 复现稳定性：必现。
- 操作成本：教师没有可恢复路径，只能改用不符合实际流程的逐人在线签署或找开发者绕过。
- 根因推测：当前 Consent 数据模型与 readiness 只识别 versioned online policy/grant，没有通用 evidence source。
- 建议方案：移除管理导航和管理页面；保留已有数据和后端兼容，不做破坏性删除；首页明确正式赛与训练赛均由学生自助创建。
- 验收标准：导航无教学/政策/研究入口；`/teacher*` 不展示管理 UI；`/me` 无课堂 Consent；赛事创建主链仍可用。
- 修复状态：VERIFIED
- 关联改动/提交说明：Web release `20260717T1000-student-self-service`。

### TC-939：排行榜移除管理入口并使用正式赛名称

- 状态：PASS
- 问题类型：UX / CONTENT
- 严重程度：无
- 用户影响：学生
- 发生概率：高
- 页面/路由：`/rankings`
- 测试身份：User B
- 前置条件：部署 Web 简化版并保留现有登录会话。
- 操作步骤：硬刷新排行榜；若切换瞬间首次数据请求失败，点击页面内“重新尝试”；检查导航和赛事选择器。
- 预期结果：只显示赛事大厅、排行榜、个人中心；无教学/政策/研究入口；4v4 正式赛名称统一。
- 实际结果：导航符合预期，选择器显示“4v4 人机辩论正式赛”，榜单数据正常。
- 证据：`screenshots/20260717-005403/TC-939-排行榜-管理入口移除且正式赛命名生效.jpg`
- 控制台/网络现象：首次硬刷新出现一次 `Failed to fetch`，页面重试立即恢复；服务器无 5xx、Web stderr 为空，后续未复现。
- 复现稳定性：功能必现；首次请求失败仅部署切换后一次。
- 操作成本：1 次刷新；异常时额外 1 次重试。
- 根因推测：部署切换后的浏览器旧连接/缓存瞬态，服务器日志不支持持续后端故障。
- 建议方案：保留可见重试；后续发布观察是否仍发生首请求失败。
- 验收标准：稳定时首次打开成功，管理入口不可见，正式赛名称一致。
- 修复状态：VERIFIED
- 关联改动/提交说明：GlobalNav、rankings、primary competition display。

### TC-940：首页同时保留正式赛与训练赛，学生均可自助创建

- 状态：PASS
- 问题类型：UX / CONTENT
- 严重程度：无
- 用户影响：全体学生
- 发生概率：高
- 页面/路由：`/`
- 测试身份：User B
- 前置条件：第一赛季开放；4v4 和 1v1 赛事 active。
- 操作步骤：打开赛事大厅；检查 Hero、两张赛事卡及创建按钮。
- 预期结果：突出 4v4 正式赛，但训练赛仍保留；两个赛事均允许学生自行创建。
- 实际结果：Hero 提供“创建 4v4 比赛”；4v4 正式赛和 1v1 训练赛卡片同时显示，各有“创建比赛”。
- 证据：`screenshots/20260717-005403/TC-940-首页-正式赛与训练赛均可学生自助创建.jpg`
- 控制台/网络现象：页面重试后 API 数据正常；终端 `/` 与 `/api/competitions` 连续 3 次均 200。
- 复现稳定性：必现。
- 操作成本：首页 0 次额外导航，创建入口 1 次点击。
- 根因推测：无。
- 建议方案：正式赛作为首要 CTA，训练赛作为并列赛事卡，不再引入管理工作台。
- 验收标准：两类赛事同时可见且无管理入口。
- 修复状态：VERIFIED
- 关联改动/提交说明：HomePage、primary competition display。

### TC-941：4v4 正式赛学生创建窗口提供辩题与八席

- 状态：PASS
- 问题类型：无
- 严重程度：无
- 用户影响：参赛学生
- 发生概率：高
- 页面/路由：首页参赛弹窗
- 测试身份：User B
- 前置条件：4v4 正式赛赛季开放且题库可用。
- 操作步骤：点击“创建 4v4 比赛”；检查创建/搜索模式、题目、八个席位和最终按钮；关闭窗口，不提交建房。
- 预期结果：学生可理解并完成建房准备，不依赖教师或政策管理。
- 实际结果：显示当前题目、正反各四席、邀请说明和“创建比赛”按钮；未产生新房间。
- 证据：`screenshots/20260717-005403/TC-941-4v4正式赛-学生自助创建窗口八席可用.jpg`
- 控制台/网络现象：赛事详情加载正常。
- 复现稳定性：必现。
- 操作成本：1 次点击打开，0 次提交。
- 根因推测：无。
- 建议方案：保持当前单窗口创建/搜索结构。
- 验收标准：8 席完整、题目可选、创建与搜索清楚区分。
- 修复状态：VERIFIED
- 关联改动/提交说明：ParticipateDialog。

### TC-942：个人中心仅保留比赛、积分与账号安全

- 状态：PASS
- 问题类型：UX / INFORMATION ARCHITECTURE
- 严重程度：无
- 用户影响：学生
- 发生概率：高
- 页面/路由：`/me`
- 测试身份：User B
- 前置条件：User B 有历史 4v4 比赛和积分。
- 操作步骤：从账号入口打开个人中心；检查第一屏到账号安全区域。
- 预期结果：不加载或展示课堂 Consent；只呈现学生核心记录和安全操作。
- 实际结果：显示积分、历史、继续比赛、最近积分、历史比赛和账号安全；无课堂/政策/同意内容。
- 证据：`screenshots/20260717-005403/TC-942-个人中心-仅保留比赛积分与账号安全.jpg`
- 控制台/网络现象：`/api/me` 正常；不再请求 `/api/consents/me/classrooms`。
- 复现稳定性：必现。
- 操作成本：1 次点击。
- 根因推测：无。
- 建议方案：保持学生任务优先级。
- 验收标准：页面 DOM 和网络均无课堂 Consent 模块。
- 修复状态：VERIFIED
- 关联改动/提交说明：MePage。

### TC-943：直达教学管理路由返回赛事大厅

- 状态：PASS
- 问题类型：UX / SCOPE
- 严重程度：无
- 用户影响：旧书签用户
- 发生概率：中
- 页面/路由：`/teacher`
- 测试身份：User B
- 前置条件：已登录。
- 操作步骤：地址栏直接访问 `/teacher`，等待服务端重定向。
- 预期结果：不显示教学管理 UI，安全返回赛事大厅。
- 实际结果：URL 最终为 `/`，首页正式赛与训练赛均正常。
- 证据：`screenshots/20260717-005403/TC-943-教学管理直达-重定向赛事大厅.jpg`
- 控制台/网络现象：HTTP 307 `Location: /`，最终 200。
- 复现稳定性：必现。
- 操作成本：0 次恢复操作。
- 根因推测：无。
- 建议方案：保留服务端 redirect，兼容旧链接。
- 验收标准：不渲染管理 JS/数据请求，最终首页 200。
- 修复状态：VERIFIED
- 关联改动/提交说明：`app/teacher/page.tsx`。

### TC-944：直达政策管理路由返回赛事大厅

- 状态：PASS
- 问题类型：UX / SCOPE
- 严重程度：无
- 用户影响：旧书签用户
- 发生概率：中
- 页面/路由：`/teacher/consents`
- 测试身份：User B
- 前置条件：已登录。
- 操作步骤：地址栏直接访问 `/teacher/consents`，等待服务端重定向。
- 预期结果：不显示政策管理 UI，安全返回赛事大厅。
- 实际结果：URL 最终为 `/`，无政策或同意管理内容。
- 证据：`screenshots/20260717-005403/TC-944-政策管理直达-重定向赛事大厅.jpg`
- 控制台/网络现象：HTTP 307 `Location: /`，最终 200。
- 复现稳定性：必现。
- 操作成本：0 次恢复操作。
- 根因推测：无。
- 建议方案：保留服务端 redirect，兼容旧链接。
- 验收标准：不渲染政策管理 UI，最终首页 200。
- 修复状态：VERIFIED
- 关联改动/提交说明：`app/teacher/consents/page.tsx`。

### TC-945：Chrome 保留训练赛且不显示管理入口

- 状态：PASS
- 问题类型：UX / SCOPE
- 严重程度：无
- 用户影响：训练赛学生
- 发生概率：高
- 页面/路由：`/competitions/training-1v1`
- 测试身份：Chrome 现有登录用户
- 前置条件：不操作该账号正在参与的真实房 278571。
- 操作步骤：切换到 Chrome 既有稷下辩论标签；关闭未提交弹窗；刷新训练赛详情；检查导航和赛事内容。
- 预期结果：训练赛继续存在，导航无教学/政策/研究入口，不影响真实房间。
- 实际结果：1v1 训练赛介绍、题库和参赛入口正常；导航仅赛事大厅、排行榜、个人中心；未操作 278571。
- 证据：`screenshots/20260717-005403/TC-945-Chrome-训练赛保留且管理入口移除.jpg`
- 控制台/网络现象：页面正常加载，无错误态。
- 复现稳定性：必现。
- 操作成本：切换标签、关闭弹窗、刷新。
- 根因推测：无。
- 建议方案：正式赛与训练赛并列保留。
- 验收标准：训练赛可访问，管理入口不可见。
- 修复状态：VERIFIED
- 关联改动/提交说明：GlobalNav、赛事范围回调修正。

### TC-946：Chrome 最终首页正式赛、训练赛与直播名称一致

- 状态：PASS
- 问题类型：CONTENT / UX
- 严重程度：无
- 用户影响：全体学生
- 发生概率：高
- 页面/路由：`/`
- 测试身份：Chrome 现有登录用户
- 前置条件：最新 Web release `20260717T1021-student-self-service-labels`。
- 操作步骤：从训练赛详情返回赛事大厅；检查 Hero、两张赛事卡和正在进行列表。
- 预期结果：4v4 正式赛、1v1 训练赛均可创建；旧进行中房间名称按当前正式赛显示。
- 实际结果：三处均显示“4v4 人机辩论正式赛”，训练赛仍在；直播房 278571 名称已一致。
- 证据：`screenshots/20260717-005403/TC-946-Chrome-首页正式赛训练赛与直播名称一致.jpg`
- 控制台/网络现象：页面与数据一次加载成功。
- 复现稳定性：必现。
- 操作成本：1 次点击返回首页。
- 根因推测：无。
- 建议方案：保持显示层兼容旧存储名称。
- 验收标准：首页、房间、排行榜对正式赛名称一致。
- 修复状态：VERIFIED
- 关联改动/提交说明：primary competition stored-name display mapping。

### TC-947：Chrome 首页 200% 缩放保持核心内容与操作可达

- 状态：PASS
- 问题类型：A11Y / RESPONSIVE
- 严重程度：无
- 用户影响：使用系统缩放或低视力的学生
- 发生概率：中
- 页面/路由：`/`
- 测试身份：Chrome 现有登录用户
- 前置条件：保持首页，不操作真实房 278571；Chrome 初始缩放 100%。
- 操作步骤：使用浏览器快捷键逐级放大到 200%；检查品牌、账号、赛事 Hero、正式赛/训练赛卡片和直播入口。
- 预期结果：无横向内容丢失，核心标题、创建入口和赛事信息仍可理解、可操作。
- 实际结果：页面自动切换到折叠导航；Hero、两类赛事和直播信息均保留，未发现被遮挡或无法访问的核心操作。
- 证据：`screenshots/20260717-005403/TC-947-Chrome-首页200缩放导航与赛事可用.png`
- 控制台/网络现象：未触发新网络请求错误。
- 复现稳定性：一次操作通过。
- 操作成本：5 次放大快捷键，测试后已恢复 100%。
- 根因推测：无。
- 建议方案：保持当前响应式断点，并把关键页面 200% 缩放纳入发布矩阵。
- 验收标准：200% 下核心内容不丢失，交互不依赖水平滚动。
- 修复状态：VERIFIED
- 关联改动/提交说明：无代码变更，生产审计证据。

### TC-948：Chrome 首页 200% 缩放折叠导航可打开并暴露全部入口

- 状态：PASS
- 问题类型：A11Y / UX
- 严重程度：无
- 用户影响：使用 200% 缩放的键鼠或触控板用户
- 发生概率：中
- 页面/路由：`/`
- 测试身份：Chrome 现有登录用户
- 前置条件：Chrome 200% 缩放，导航显示“打开导航菜单”。
- 操作步骤：点击打开导航；检查赛事大厅、排行榜、账号和退出入口；关闭菜单并恢复 100%。
- 预期结果：菜单可打开/关闭，所有入口具有可访问名称，不覆盖或永久锁住页面。
- 实际结果：菜单展开后“赛事大厅”“排行榜”均可访问，按钮名称切换为“关闭导航菜单”；关闭和缩放复原成功。
- 证据：`screenshots/20260717-005403/TC-948-Chrome-首页200缩放导航菜单可操作.png`
- 控制台/网络现象：无页面跳转或副作用。
- 复现稳定性：一次操作通过。
- 操作成本：1 次打开、1 次关闭、1 次缩放复原。
- 根因推测：无。
- 建议方案：后续在赛事详情、大厅、舞台和结果页重复同一矩阵。
- 验收标准：菜单打开/关闭状态、焦点和入口名称在 200% 下均正确。
- 修复状态：VERIFIED
- 关联改动/提交说明：无代码变更，生产审计证据。

### TC-949：核心可靠性发布后 Chrome 首页无回归

- 状态：PASS
- 问题类型：DEPLOY / REGRESSION
- 严重程度：无
- 用户影响：全体学生
- 发生概率：高
- 页面/路由：`/`
- 测试身份：Chrome 现有登录用户
- 前置条件：Web release `20260717T1120-core-reliability` 已切换，API/Engine/Worker/Web RUNNING。
- 操作步骤：在 Chrome 首页强制刷新；检查导航、正式赛、训练赛、直播和排行榜摘要。
- 预期结果：新发布不影响学生赛事入口，历史直播名称和登录态保持。
- 实际结果：页面一次加载成功；正式赛/训练赛和房 278571 只读入口均正常，管理入口仍不可见。
- 证据：`screenshots/20260717-005403/TC-949-Chrome-核心可靠性发布后首页正常.png`
- 控制台/网络现象：无页面错误态。
- 复现稳定性：部署后一次刷新通过；终端首页另连续 3 次 HTTP 200。
- 操作成本：1 次刷新。
- 根因推测：无。
- 建议方案：保持首页为每次核心服务发布的最小生产回归入口。
- 验收标准：登录态、两类赛事、直播入口和导航均正常。
- 修复状态：VERIFIED
- 关联改动/提交说明：release `20260717T1120-core-reliability`。

### TC-950：核心可靠性发布后 Chrome 只读观战建立实时连接

- 状态：PASS
- 问题类型：REALTIME / REGRESSION
- 严重程度：无
- 用户影响：观众与参赛学生
- 发生概率：高
- 页面/路由：`/rooms/278571/watch`
- 测试身份：Chrome 现有登录用户，观战模式
- 前置条件：只允许读取真实房 278571，不进入 debate/control，不触发任何房间操作。
- 操作步骤：从首页点击公开观战；等待初始“重连中”切换为“实时连接”；检查暂停状态、阶段、计时和声音开关。
- 预期结果：只读页面连接成功，房间状态不改变。
- 实际结果：约 1 秒内显示“实时连接”；房间仍为“比赛已暂停 / 正方一辩立论 / 03:00”，观战模式与开启声音按钮正常。
- 证据：`screenshots/20260717-005403/TC-950-Chrome-核心可靠性发布后只读观战实时连接.png`
- 控制台/网络现象：WebSocket 连接成功，无持续重连警告。
- 复现稳定性：一次连接通过；终端 watch 路由另连续 3 次 HTTP 200。
- 操作成本：1 次点击，约 1 秒等待。
- 根因推测：无。
- 建议方案：继续把只读观战作为部署后 WebSocket 安全回归，不使用真实参赛控制页。
- 验收标准：实时连接成功且不产生房间写操作。
- 修复状态：VERIFIED
- 关联改动/提交说明：release `20260717T1120-core-reliability`。

### TC-951：部署后真实 LightTTS 三任务使用一活动槽与两等待槽

- 状态：PASS
- 问题类型：PERF / RELIABILITY
- 严重程度：无（缩小但不关闭 TC-934/P1）
- 用户影响：同时进入 AI 发言的多个学生房间
- 发生概率：课堂同步开赛时高
- 页面/路由：生产 LightTTS provider + Redis admission gate
- 测试身份：终端低峰 canary；不绑定真实房间
- 前置条件：ready 200，`active_match_processing=false`，gate active/queue=0；音频仅写入 `/tmp` QA 目录。
- 操作步骤：同时提交三个不同短句；每 100ms 记录生产 gate；持续检查真实比赛活动，若出现则取消。
- 预期结果：三任务全部完成，maximum active=1、queue=2，最终 0/0，无真实比赛活动。
- 实际结果：墙钟 5.798 秒；任务总耗时 2.074/3.915/5.696 秒，WAV 2.24/2.12/2.20 秒，三个 RIFF WAV 均有效；最大 active=1、queue=2，最终 0/0，未检测到 active match。
- 证据：[结果 JSON](audio/20260717-005403/lighttts-real-active-plus-pending-canary/result.json)；[WAV 1](audio/20260717-005403/lighttts-real-active-plus-pending-canary/audio/qa-gate-1/canary-1.wav)、[WAV 2](audio/20260717-005403/lighttts-real-active-plus-pending-canary/audio/qa-gate-2/canary-2.wav)、[WAV 3](audio/20260717-005403/lighttts-real-active-plus-pending-canary/audio/qa-gate-3/canary-3.wav)。
- 控制台/网络现象：无 queue_full、timeout、lease lost 或残留 key。
- 复现稳定性：真实 canary 一次通过；真实 Redis 无模型 shadow 连续 3 轮通过。
- 操作成本：约 6 秒。
- 根因推测：旧 enqueue 在 active 尚未取得时把首任务计入 pending；修复后 active 空闲可临时接纳 `max_pending+1`。
- 建议方案：保持 active=1/pending=2；未完成真实 4/10/20 前不提高容量承诺。
- 验收标准：三任务全成功、max active=1、max queue=2、final 0/0。
- 修复状态：VERIFIED
- 关联改动/提交说明：`lighttts_admission.py` enqueue v2、`providers.py` background admission order。

### TC-952：断线禁止新发言且发言启动网络超时可安全重试

- 状态：PASS（自动化与发布验证；真实弱网房间仍待专项）
- 问题类型：BUG / UX / RELIABILITY
- 严重程度：P2（已修复）
- 用户影响：弱网或 WebSocket 暂断时轮到发言的学生
- 发生概率：中
- 页面/路由：`DebateStage`、`POST /api/rooms/{code}/speech/start`
- 测试身份：Web 组件测试；生产发布后只读邻近回归
- 前置条件：真人席位拥有设备控制；分别模拟 `connected=false` 与 `/speech/start` 12 秒无响应。
- 操作步骤：断开实时连接时检查发言按钮；麦克风成功打开后让 start 请求挂起至超时；恢复后再次点击并比较幂等键。
- 预期结果：断线时不打开麦克风；start 超时停止 recorder/track、恢复按钮并提示检查网络；重试沿用同一 `X-Idempotency-Key`。
- 实际结果：断线按钮显示“等待实时连接”且禁用；已经录音/待提交状态仍可结束或提交。start 超时后媒体资源全部释放，重试键保持一致。
- 证据：`apps/web/components/debate-stage.test.tsx`；定向 32 passed，完整 Web 118 passed，TypeScript/build 通过。
- 控制台/网络现象：模拟 AbortSignal 被触发，无重复 speech-start。
- 复现稳定性：自动化稳定通过。
- 操作成本：无生产写操作。
- 根因推测：旧实现只给 `getUserMedia` 设置超时，未限制后续 start API，且 `canStartSpeaking` 未要求实时连接。
- 建议方案：后续用隔离 QA 房补真实弱网、响应丢失和在线恢复回归。
- 验收标准：断线不新开录音；12 秒超时释放媒体；幂等键重试不重复创建发言。
- 修复状态：VERIFIED（真实弱网专项待补）
- 关联改动/提交说明：`debate-stage.tsx` 与 3 个新增回归用例。

### TC-953：Chrome 只读观战 200% 缩放可滚动查看全部 4v4 席位与底栏控制

- 状态：PASS
- 问题类型：A11Y / RESPONSIVE / REGRESSION
- 严重程度：无
- 用户影响：使用浏览器 200% 缩放的观众与低视力用户
- 发生概率：中
- 页面/路由：`/rooms/278571/watch`
- 测试身份：Chrome 现有登录用户，公开只读观战
- 前置条件：只读访问真实房 278571；不进入 debate/control，不触发房间状态写入。
- 操作步骤：把 Chrome 缩放到 200%；检查辩题、实时连接、暂停状态、当前环节、计时、字幕和底栏；点击舞台区域后按 Page Down，检查内部滚动与下方席位；测试后恢复 100% 并返回首页。
- 预期结果：核心信息和声音、全屏、设置控制可达；舞台可在窄有效视口内滚动查看全部 4v4 席位，不要求水平滚动。
- 实际结果：初始画面可见辩题、双方上层席位、阶段、03:00 计时、字幕和三项底栏控制；Page Down 后舞台内部出现垂直滚动并完整露出正反双方第三、第四席。页面底栏保持固定可操作，房间仍为暂停且实时连接正常。
- 证据：`screenshots/20260717-005403/TC-953-Chrome-只读观战200缩放核心控制可达.png`、`screenshots/20260717-005403/TC-953b-Chrome-只读观战200缩放PageDown后.png`
- 控制台/网络现象：无写操作、无错误态；仅改变本地缩放和舞台滚动位置。
- 复现稳定性：一次完整操作通过；两张截图对比可见滚动条和下方席位位置变化。
- 操作成本：200% 缩放、1 次舞台聚焦、1 次 Page Down、恢复 100%。
- 根因推测：无；移动断点下 `.stage-arena` 的内部 `overflow:auto` 按预期工作。
- 建议方案：保持舞台内部滚动；后续在真实 390×844 设备补触控滚动和焦点可见性。
- 验收标准：200% 下全部 8 席与三项底栏控制可达，无水平滚动且不改变比赛状态。
- 修复状态：VERIFIED
- 关联改动/提交说明：无代码变更，生产 Chrome 只读审计。

### TC-954：设备恢复发布后 Chrome 首页保持学生自助正式赛与训练赛入口

- 状态：PASS
- 问题类型：DEPLOY / REGRESSION
- 严重程度：无
- 用户影响：全体学生
- 发生概率：高
- 页面/路由：`/`
- 测试身份：Chrome 现有登录用户
- 前置条件：Web release `20260717T1137-student-device-recovery` 已切换；四主服务 RUNNING。
- 操作步骤：Chrome 回到首页并读取完整可访问性树；检查 4v4 正式赛、1v1 训练赛、学生自助说明、直播和排行榜。
- 预期结果：设备/联网改动不回归赛事入口，管理入口仍不可见。
- 实际结果：首页一次加载成功；正式赛与训练赛创建按钮、规则入口、公开观战房 278571 和榜单均正常，导航仅保留赛事大厅、排行榜与账号。
- 证据：`screenshots/20260717-005403/TC-954-Chrome-设备恢复发布后首页正常.jpg`
- 控制台/网络现象：无页面错误态；终端同路由连续 3 次 HTTP 200。
- 复现稳定性：发布后一次 Computer Use 与三次 HTTP 探测通过。
- 操作成本：一次页面状态读取。
- 根因推测：无。
- 建议方案：保持首页作为每次 Web release 的首要回归入口。
- 验收标准：两类赛事、学生自助说明、直播和导航全部可用。
- 修复状态：VERIFIED
- 关联改动/提交说明：release `20260717T1137-student-device-recovery`。

### TC-955：设备恢复发布后 Chrome 只读观战实时连接无回归

- 状态：PASS
- 问题类型：REALTIME / DEPLOY / REGRESSION
- 严重程度：无
- 用户影响：观众与参赛学生
- 发生概率：高
- 页面/路由：`/rooms/278571/watch`
- 测试身份：Chrome 现有登录用户，公开只读观战
- 前置条件：不得进入真实房 debate/control，不改变比赛状态。
- 操作步骤：从首页点击公开观战；等待页面加载；检查实时连接、暂停状态、阶段、计时、字幕及声音/全屏/设置。
- 预期结果：新 `useRoom` 联网恢复逻辑不破坏正常 WebSocket，房间保持只读和暂停。
- 实际结果：显示“实时连接 / 比赛已暂停 / 正方一辩立论 / 03:00”，字幕占位和三项底栏控制正常；未产生任何写操作。
- 证据：`screenshots/20260717-005403/TC-955-Chrome-设备恢复发布后只读观战实时连接.jpg`
- 控制台/网络现象：无持续重连或错误态；终端 watch 路由连续 3 次 HTTP 200。
- 复现稳定性：发布后一次 Computer Use 与三次 HTTP 探测通过。
- 操作成本：一次点击和约 2 秒等待。
- 根因推测：无。
- 建议方案：保持公开只读观战作为 `useRoom` 每次修改后的生产 WebSocket 回归。
- 验收标准：实时连接成功，房间状态不变，底栏控制可达。
- 修复状态：VERIFIED
- 关联改动/提交说明：`use-room.ts` offline/online 恢复和 terminal guard；release `20260717T1137-student-device-recovery`。

### TC-956：LightTTS 周期健康检查不再绕过全局门禁执行真实合成

- 状态：PASS
- 问题类型：PERF / RELIABILITY / OPERATIONS
- 严重程度：P1（旁路负载已关闭；整体容量 P1 未关闭）
- 用户影响：所有同时等待 AI 语音的房间
- 发生概率：原实现固定每约 89 秒一次
- 页面/路由：生产 LightTTS Supervisor、`/health`、平台 Redis gate
- 测试身份：低峰生产运维；不绑定真实房间
- 前置条件：ready 200、`active_match_processing=false`、平台 gate 0/0；生产只有一张 RTX 3080 Ti。
- 操作步骤：确认 `--health_monitor` 每约 88 秒调用一次真实合成 `/health`；备份 Supervisor 配置；只移除该 flag，保留 Supervisor autorestart 与 平台 TCP/readiness；重启 LightTTS；等待超过旧周期；再通过 平台 provider/gate 合成一条短句并校验 WAV、时长、active/queue 清理。
- 预期结果：不再自动请求 `/health`；真实 TTS 仍可用；平台 gate 最大 active=1、结束后 0/0。
- 实际结果：旧日志 `GET /health` 计数为 921；新进程运行超过 92 秒后仍为 921，且后续真实 canary 完成后仍未增加。canary 3.479 秒完成，WAV 4.32 秒、207404 bytes、RIFF 有效；最大 active=1、queue=0，最终 0/0。API/Web/Engine/Worker/LightTTS RUNNING，ready 200。
- 证据：[结果 JSON](audio/20260717-005403/lighttts-no-synthetic-health-canary/result.json)、[canary WAV](audio/20260717-005403/lighttts-no-synthetic-health-canary/canary.wav)；Supervisor 回滚包 `/home/ubuntu/sunsq/phdebate/runtime/deploy-backups/20260717T1200-before-lighttts-no-synthetic-health.conf`，SHA-256 `8d6a1b26a902a914e0af291c60cbf001ca9dc1a1e4ac0cd841b7f5bf0d54a00f`。
- 控制台/网络现象：模型重载期间 8080 短暂未监听，约 92 秒后 liveness/ready 恢复；没有活动比赛、队列或死信。
- 复现稳定性：等待一个完整旧健康周期并完成一次真实 canary 后通过。
- 操作成本：一次可回滚 LightTTS 重启、一次短句合成。
- 根因推测：LightTTS 内置 health monitor 直接在模型内部调用 `health_check()` 合成“你好”，绕过 平台 admission 和指标。
- 建议方案：保留轻量 liveness/readiness；将未来低频真实 canary 作为显式 QA 任务并通过统一 gate，禁止模型内部旁路合成。
- 验收标准：旧周期内 `/health` 计数不增长；真实 provider canary 有效；服务与 gate 清理正常。
- 修复状态：VERIFIED
- 关联改动/提交说明：生产 Supervisor 仅移除 `--health_monitor`，未改模型、并发、Nginx、API 或数据库。

### TC-957：过载恢复发布后 Chrome 首页与学生自助赛事入口无回归

- 状态：PASS
- 问题类型：DEPLOY / REGRESSION
- 严重程度：无
- 用户影响：全体学生
- 发生概率：高
- 页面/路由：`/`
- 测试身份：Chrome 现有登录用户
- 前置条件：release `20260717T1255-overload-recovery` 已切换；API/Engine/Worker/Web RUNNING，ready 200。
- 操作步骤：使用 Computer Use 读取 Chrome 完整可访问性树，检查导航、4v4 正式赛、1v1 训练赛、公开观战和排行榜。
- 预期结果：过载恢复与舞台修改不影响首页，两类赛事仍由学生自行创建。
- 实际结果：首页显示“4v4 人机辩论正式赛”和“1v1 辩论训练赛”的创建/规则入口，学生自助说明、房 278571 公开观战和榜单正常；未触发创建或其他写操作。
- 证据：`screenshots/20260717-005403/TC-957-Chrome-过载恢复发布后首页正常.jpg`，SHA-256 `3ae23cfad4699547d6a08ed2ca2b960a592070bdadbb5dc5a90379e144258ca7`。
- 控制台/网络现象：Computer Use 无页面错误态；12349 shadow 与 12341 生产首页/训练赛/观战均 HTTP 200。
- 复现稳定性：一次 Chrome Computer Use、一次 Web shadow 和一次生产探测通过。
- 操作成本：一次页面状态读取。
- 根因推测：无。
- 建议方案：保持首页为每个 Web release 的固定首项生产回归。
- 验收标准：两类赛事、学生自助文案、直播和导航均可达。
- 修复状态：VERIFIED
- 关联改动/提交说明：release `20260717T1255-overload-recovery`。

### TC-958：过载恢复发布后 Chrome 观战实时连接与房主恢复入口可达

- 状态：PASS
- 问题类型：REALTIME / RECOVERY / DEPLOY
- 严重程度：无（未实际点击真实房恢复操作）
- 用户影响：房主、参赛学生与观众
- 发生概率：过载/临时 Provider 失败时
- 页面/路由：`/rooms/278571/watch`
- 测试身份：Chrome 现有登录用户；该账号是真实房房主，因此页面合法显示恢复按钮。
- 前置条件：仅访问公开 `/watch`；禁止进入 lobby/debate/control，禁止点击“重试异常步骤”或任何改变真实房状态的控件。
- 操作步骤：从首页点击房 278571 公开观战；等待实时连接；只读检查房间状态、阶段、计时、脱敏错误文案、房主身份文案和恢复入口；然后返回首页。
- 预期结果：WebSocket 正常；错误信息不泄露 Redis/端点/provider 内部细节；房主可看到恢复入口，普通观众仍无控制权。
- 实际结果：连续两次完整状态读取均显示“实时连接 / 比赛已暂停 / 正方一辩立论 / 03:00”；房主底栏显示“房主观战 / 可在比赛异常时重试当前步骤”，错误横幅为安全中文描述，“重试异常步骤”按钮可达。未点击该按钮，未进入任何写路由，真实房仍保持 paused。
- 证据：`screenshots/20260717-005403/TC-958-Chrome-过载恢复发布后观战实时连接.jpg`，SHA-256 `a25fb321b315d4de7302b3d0b30740fc16a2b6081999c9c05da85bd0705e492c`。
- 控制台/网络现象：无持续重连或页面错误态；shadow 与 production watch 均 HTTP 200。
- 复现稳定性：两次 Computer Use 状态读取一致，发布后服务端状态仍 `active_match_processing=false`。
- 操作成本：一次点击、约 3 秒等待和一次返回首页。
- 根因推测：无。
- 建议方案：未来使用隔离 QA 房实测按钮的幂等、断线 REST 恢复与多用户权限；继续禁止在真实房点击。
- 验收标准：实时连接成功；脱敏文案准确；房主恢复入口可达；页面检查不改变真实房。
- 修复状态：VERIFIED（入口/权限文案/实时连接）；真实点击恢复仍需隔离 QA 房。
- 关联改动/提交说明：`DebateStage` 脱敏故障横幅、房主幂等 REST retry、watch 房主文案；release `20260717T1255-overload-recovery`。

### TC-959：舞台仅播放权威 AI 音频，真人录音与旧 cue 不复活

- 状态：PASS
- 问题类型：AUDIO / STATE MACHINE / ASR ECHO
- 严重程度：P1（已修复）
- 页面/路由：`/rooms/:code/debate`、`/rooms/:code/watch`
- 操作步骤：构造 completed 真人 Speech + `speech.audio.ready`、active `playing` AI Speech、server completed、历史 cue、fresh announcement cue 与阶段切换；记录 Audio 实例、play/pause 和 seek。
- 预期结果：真人上传录音不自动播放；只播放 `active_speech.id` 对齐的 `status=playing` AI；权威结束/切阶段立即停止；历史 cue 不复活。
- 实际结果：全部符合。真人 `speech.audio.ready` 未创建 Audio；AI 按 `playback_started_at` seek；completed 后 pause 且不创建新 Audio；fresh cue 仅在开场窗口播放并在下一阶段停止。
- 证据：`apps/web/components/debate-stage.test.tsx`，DebateStage 44 passed；Web 全量 144 passed；release `20260717T1348-authoritative-audio-home-resilience`。
- 生产回归：Web 12349 shadow 与 12341 production 首页/训练赛/watch 均 200，无新 Web stderr。本 release 视觉 Computer Use 因用户正在使用 Chrome 而延后，未抢占标签。
- 修复状态：VERIFIED（自动状态机、shadow、生产健康）；弱网 WAV 下载/服务器计时完整性仍 BLOCKED。

### TC-960：直播和排行榜失败/永久 pending 不再阻断核心参赛入口

- 状态：PASS
- 问题类型：RELIABILITY / FAILURE DOMAIN / UX
- 严重程度：P1（已修复）
- 页面/路由：`/`
- 操作步骤：让 `/api/competitions` 立即成功，分别让 `/api/live-rooms` 和 `/api/rankings` 同时 reject，以及永久不 resolve；检查首页 loading/错误与创建按钮。
- 预期结果：首页只以 competitions 作为核心 gate；次要面板失败或卡住不影响 4v4/1v1 创建/加入。
- 实际结果：两次要接口同时 reject 时显示独立中文说明和重试按钮，4v4 创建仍 enabled；两请求永久 pending 时核心赛事仍立即可用，不停留在 loading screen。
- 证据：`apps/web/app/public-pages.test.tsx`，public pages 5 passed；Web 全量 144 passed；release `20260717T1348-authoritative-audio-home-resilience`。
- 生产回归：首页 12349 shadow/12341 production 均 200，Next Linux build 通过，无新 Web stderr。
- 修复状态：VERIFIED。

### TC-961：FunASR readiness 使用合法 WebSocket 握手且不制造 ERROR

- 状态：PASS
- 问题类型：OPERATIONS / LOG QUALITY / HEALTH CHECK
- 严重程度：P2（已修复）
- 页面/路由：`/api/health/ready`、FunASR `ws://127.0.0.1:10095`
- 操作步骤：用本地真实 WebSocket server 验证 `_endpoint_check`；生产 API 12350 shadow 调用 ready 并量取 FunASR stderr delta；切换后连续调用 production ready 5 次再量取 delta。
- 预期结果：`ws/wss` 做合法 HTTP Upgrade，立即正常关闭；ready 仍 ok；不产生 `opening handshake failed`。
- 实际结果：本地合法握手测试通过；API shadow ready 200/FunASR ok 且 stderr delta=0；生产 ready 连续 5/5=200，FunASR stderr delta=0。修复前每次 ready 稳定新增约 1930 bytes traceback，累计 199 次/约 1.49MB。
- 证据：`apps/api/tests/test_providers.py`；API 全量 265 passed；release `20260717T1348-authoritative-audio-home-resilience`。
- 部署事件：首次 harness 误把旧 API 预检的最后一条错误计入新 shadow，在生产切换前自动回滚；调整日志基线后同一修复精确 delta=0 并成功发布。
- 修复状态：VERIFIED。

### TC-962：ASR ready 前 PCM 按序缓存，tail block 先于唯一 finish

- 状态：PASS
- 问题类型：ASR / ORDERING / SENTENCE BOUNDARY
- 严重程度：P1（已修复）
- 操作步骤：ready 前依次输入 A/B 两块 PCM，ready 后输入 C；点击停止后再输入 tail D；检查 wire payload 顺序。
- 预期结果：ready 前不丢帧；顺序严格 A→B→C→D→finish；processor 为 1024 samples；finish 只出现一次且始终最后。
- 实际结果：四块 PCM 首样本与顺序精确匹配，`finish` 为最后一条消息；认证声明 v1/PCM16/mono/16k。
- 证据：`apps/web/components/debate-stage.test.tsx`；DebateStage 53 passed；release `20260717T1435-asr-boundary-tts-integrity`。
- 修复状态：VERIFIED。

### TC-963：stop-before-ready、双击与 ready 超时安全降级

- 状态：PASS
- 问题类型：ASR / CONCURRENCY / RECOVERY
- 严重程度：P1（已修复）
- 操作步骤：ready 前停止并重复触发停止；随后分别让 ready 迟到与永不 ready。
- 预期结果：迟到 ready 先 flush 再发唯一 finish；MediaRecorder/track 只停止一次；永不 ready 最多等待 2 秒，不发 finish、不等待 30.5 秒 final，转人工核对。
- 实际结果：全部符合，未出现重复 HTTP finish 或上传。
- 证据：DebateStage `flushes buffered PCM...` 与 `falls back...` 回归；53 passed。
- 修复状态：VERIFIED。

### TC-964：旧 ASR generation 晚到 ready/resume 不污染新录音

- 状态：PASS
- 问题类型：ASR / GENERATION ISOLATION / RACE
- 严重程度：P1（已修复）
- 操作步骤：gen1 在 suspended `AudioContext.resume()` 中挂起；阶段切换 abort；启动 gen2 至 recording；再让 gen1 resume/ready 晚到。
- 预期结果：gen1 不 flush、不恢复 capturing、不停止/清空 gen2 recorder/stream/socket，也不把旧错误写到新 UI。
- 实际结果：gen2 仍为 recording，第二音轨未 stop，新 socket 保留，旧 buffer 零发送且无错误污染。
- 证据：DebateStage suspended-resume 和 old-generation 回归；独立只读复审最终 GO。
- 修复状态：VERIFIED。

### TC-965：44.1k/48k 输入跨块有状态重采样为真实 16k wire PCM

- 状态：PASS
- 问题类型：ASR / SAMPLE RATE / AUDIO
- 严重程度：P1（已修复）
- 操作步骤：分别用 44,100Hz 与 48,000Hz AudioContext，两块合计 100ms 输入，检查跨块输出样本数。
- 预期结果：wire 输出为 1,599–1,600 samples，VAD 也按 canonical 16k 样本统计，无块边界重复或三倍时长错误。
- 实际结果：两种采样率均在目标范围；认证 sample_rate 与实际 wire 数据一致。
- 证据：DebateStage 参数化重采样回归；TypeScript/Next build 通过。
- 修复状态：VERIFIED。

### TC-966：恢复发言 final 前断线不得用旧文字静默提交新录音

- 状态：PASS
- 问题类型：ASR / DATA INTEGRITY / RECOVERY
- 严重程度：P1（已修复）
- 操作步骤：以已有 server baseline 恢复发言，发送新 PCM 与 finish，但在 final 前正常关闭 socket。
- 预期结果：进入人工核对，保留旧 baseline 供编辑，不自动调用 `/speech/finish`，避免新音频与旧文字不一致。
- 实际结果：显示“最终字幕返回前中断”，textarea 保留 baseline，fetch 仅 control/start 两次。
- 证据：DebateStage close-before-final 回归；独立复审 GO。
- 修复状态：VERIFIED。

### TC-967：ASR wire v1 严格验证且兼容旧客户端

- 状态：PASS
- 问题类型：ASR / PROTOCOL / SECURITY
- 严重程度：P2（已修复）
- 操作步骤：验证 legacy `{type,lease}`、完整 v1、错误版本/编码/声道/采样率、部分字段、bool 类型与未知字段。
- 预期结果：legacy 和完整 v1 成功；其他在 claim/upstream connect 前返回结构化错误并以 4400 关闭；4409 仍只表示 lease/控制权失败。
- 实际结果：定向 10 passed；拒绝后可重新 claim，无 upstream connect。
- 证据：`apps/api/tests/test_platform.py`；API 全量 277 passed。
- 修复状态：VERIFIED。

### TC-968：LightTTS HTTP 200/缓存明显截断音频被拒绝并重生

- 状态：PASS
- 问题类型：TTS / CONTENT INTEGRITY / CACHE
- 严重程度：P1（已修复）
- 操作步骤：30 字请求分别返回 0.8/1.0/2.4 秒 WAV，并放入 1.0 秒截断缓存；检查 segment、合并和 cache 路径。
- 预期结果：明显过短的 200 响应不能成为可复用成功产物；2.4 秒保守边界通过；截断 cache 被替换。
- 实际结果：阈值为 `max(0.25s, visible_chars×0.08s)`，0.8/1.0 秒拒绝，2.4 秒通过，cache 重生且无 `.part` 残留。
- 证据：provider 48 passed；17 条生产 AI 发言、5 条 cue、四音色 1/5 字样本只读审计均无误拒；独立复审 GO（speed=1.0）。
- 修复状态：VERIFIED；speed=2 与 markup/URL/emoji 仍为 P2 测试缺口。

### TC-969：Iteration 17 shadow、发布、回滚点与生产健康

- 状态：PASS
- 问题类型：DEPLOY / OPERATIONS / REGRESSION
- 严重程度：无
- 操作步骤：备份 6 个源码文件与旧 Web 指针；API 12350 shadow（禁用 presence startup reset）；远端 Linux build；Web 12349 shadow；原子切换并重启四服务；连续 ready/HTTP/日志/哈希检查。
- 预期结果：失败自动恢复旧源码/指针；成功后 schema/服务/queue/gate/页面健康，真实房只读状态不变。
- 实际结果：API shadow ready 200，Web shadow 首页/训练赛/watch 均 200；生产 ready 5/5、四服务与 FunASR/LightTTS RUNNING、公开三路 200、6/6 SHA 一致。API/Engine/Web stderr delta=0，Worker 879 bytes 仅正常重启日志。真实房 278571 仍为 `paused / seq 91`，未进入写路由。
- 证据：release `20260717T1435-asr-boundary-tts-integrity`；包 SHA-256 `5098e5852f5b7f9d0a0032097c61bd27a0c6ad8396797c75b5a69b059b26bcf0`；源码备份 SHA-256 `06eaeefd2c65a9eecf312606f8c783581766cc1ee7e33c8416a23f7ae460d295`。
- 回滚点：`/home/ubuntu/sunsq/phdebate/runtime/deploy-backups/20260717T1435-asr-boundary-tts-integrity-before-source.tar.gz` 与 `...-before-web.txt`；前一 release `20260717T1348-authoritative-audio-home-resilience`。
- Computer Use：发布后 Chrome 当前聚焦用户的百度智能云页面；只读取窗口标题/可访问性树确认边界，未切换到后台稷下标签，未保存用户页面截图。Safari/SunBrowser 未使用。
- 修复状态：VERIFIED。

### TC-970：房主在辩论与观战舞台拥有最小必要控制

- 状态：PASS
- 问题类型：UI / AUTHORIZATION / SELF-SERVICE
- 严重程度：P1（已修复）
- 操作步骤：分别以房主 debate、房主 watch、普通辩手和普通观众 projection 打开“比赛操作”；检查暂停/继续/提前结束显示与后端权限兜底。
- 预期结果：只有 `room.can_control=true` 显示全场控制；普通辩手仍可发言/退出页面但不能终止全场；房主被 AI 接替而进入 watch 后不丢失应急控制。
- 实际结果：debate/watch 房主均显示控制，非房主不显示；后端现有 owner-only `can_control`、房间锁与 403/409 状态校验保持不变。
- 证据：`apps/web/components/debate-stage.test.tsx` 的 owner-only 与 watch-mode 回归；独立可靠性复审最终 GO。
- 修复状态：VERIFIED。

### TC-971：实时通道重连时 REST 控制、双击与响应丢失保持幂等

- 状态：PASS
- 问题类型：CONCURRENCY / IDEMPOTENCY / RECOVERY
- 严重程度：P1（已修复）
- 操作步骤：在 `connected=false` 但已有权威 room snapshot 时点击暂停；快速重复点击；首个响应模拟丢失后重试同一动作并记录 `X-Idempotency-Key`。
- 预期结果：WebSocket 波动不阻断 REST 应急控制；同步 single-flight 只发一次并阻止跨动作并发；响应丢失重试沿用同 key，权威 snapshot 对账后下一周期生成新 key。
- 实际结果：断线 REST pause 成功应用响应 room；快速双击单请求；两次 lost-response attempt 的 key 非空且完全相同。
- 证据：DebateStage 60 passed；独立可靠性复审最终 GO。
- 修复状态：VERIFIED。

### TC-972：干净 ASR 也可由辩手主动结束并修正后提交

- 状态：PASS
- 问题类型：ASR / USER CONTROL / DATA INTEGRITY
- 严重程度：P1（已修复）
- 操作步骤：产生有效语音与完整 ASR final，在录音中打开“比赛操作”，选择“结束发言并修改文字”，修改 textarea 后确认。
- 预期结果：stop 仍遵守 tail drain、ready/final 和 generation 规则；即使 ASR 完整也不得自动 `/speech/finish`，必须先展示当前识别稿，人工确认后只提交修订稿。
- 实际结果：确认前 fetch 仅 control lease + speech start；编辑框含完整 ASR；确认后 body 精确为人工修订文字。
- 证据：DebateStage `lets a speaker explicitly stop and correct even a clean ASR result before submission`。
- 修复状态：VERIFIED。

### TC-973：文字已完成而录音上传失败时只重试权威音频

- 状态：PASS
- 问题类型：AUDIO / TWO-PHASE SUBMISSION / IDEMPOTENCY
- 严重程度：P1（已修复）
- 操作步骤：让 `/speech/finish` 返回不同的权威 speech id，首次 audio 上传返回 503；尝试修改已提交文字并重试。
- 预期结果：finish 200 后立即锁定 canonical 文字和权威 id；textarea readonly；重试不能再次调用 finish 或改变文字，只向权威 id 重传音频；快速双击共享同一 submit Promise。
- 实际结果：finish 总计一次，第一次与重试 audio URL 均使用权威 id；DOM change 不改变文字；第二次 audio 成功后 pending 清空。
- 证据：DebateStage `locks finalized text and retries only the authoritative audio upload after a failure`；独立语音数据复审最终 GO。
- 修复状态：VERIFIED。

### TC-974：退出比赛页面不会静默丢失本地录音

- 状态：PASS
- 问题类型：NAVIGATION / DATA LOSS / RECOVERY
- 严重程度：P1（已修复）
- 操作步骤：在 starting/capturing/finishing/pending 状态尝试顶部品牌、操作面板、刷新/关页与浏览器后退/侧滑；无本地工作时执行退出确认。
- 预期结果：有本地工作时禁用/拦截退出，`beforeunload` 覆盖刷新关页，同 URL duplicate-history guard 覆盖 `popstate`；无本地工作时明确说明比赛继续、席位归属保留、离开较久 AI 可能接替，确认后转“我的”。
- 实际结果：beforeunload 被取消，popstate 立即恢复 debate URL 并显示处理提示；确认框文案不把“离开页面”伪装成永久释放席位。
- 证据：DebateStage navigation guard 回归；DebatePage `onLeave -> /me`；独立 UI 复审最终 GO。
- 修复状态：VERIFIED。

### TC-975：比赛操作面板、危险确认与移动触控可访问

- 状态：PASS
- 问题类型：ACCESSIBILITY / MOBILE / DIALOG
- 严重程度：P2（已修复）
- 操作步骤：键盘打开操作面板，检查首焦点/Tab trap/Escape；分别从设置和顶部品牌打开确认，检查同一时刻 modal 数量、取消焦点；检查移动触控最小尺寸和 safe-area。
- 预期结果：任一时刻最多一个 `aria-modal=true`；设置来源取消后重开面板并聚焦相关动作，品牌来源回到品牌；tool、品牌、action、确认按钮至少 44px；移动 sheet 可滚动且不遮主发言按钮。
- 实际结果：全部符合；paused 状态先决定“继续比赛/异常暂停”文案，不因断线或本地工作错误显示“暂停比赛”。
- 证据：DebateStage/页面定向 69 passed；Axe 既有回归继续通过；独立 UI 复审最终 GO。
- 修复状态：VERIFIED。

### TC-976：Iteration 18 完整质量门与三路独立复审

- 状态：PASS
- 问题类型：QUALITY GATE / REVIEW
- 严重程度：无
- 操作步骤：并行执行 Web 全量测试与 TypeScript+Next production build；分别由 UI、控制可靠性、ASR/文字音频一致性审查者做只读复核，首轮 NO-GO 问题修复后再复核。
- 预期结果：全量门禁通过；WebSocket/观战控制、popstate/双模态、两阶段提交/single-flight 三组阻断均关闭，最终三路 GO。
- 实际结果：Web `23 files / 160 tests passed`；TypeScript 与 Next 16.2.10 production build 通过；三路最终均 GO。房主观战说明文案最后改为“可暂停、继续或提前结束比赛”后，首轮全量仅暴露对应旧文本断言，更新测试期望后最终 160 项全绿，非运行时失败。`npm run lint` 仍因项目既有脚本使用 Next 16 已移除的 `next lint` 不可用，与本次改动无关。
- 证据：本地全量输出；三名并行只读审查结论。
- 修复状态：VERIFIED。

### TC-977：Iteration 18 Web shadow、发布、回滚点与生产健康

- 状态：PASS
- 问题类型：DEPLOY / OPERATIONS / REGRESSION
- 严重程度：无
- 操作步骤：确认 `active_match_processing=false`；备份四个源文件与旧 Web 指针；远端 Linux build；12349 shadow 探测首页/训练赛/公开 watch；原子切换并仅重启 Web；连续 ready、服务、HTTP、哈希与日志检查。
- 预期结果：失败自动恢复源码和旧指针；成功后不修改数据库/API/Nginx/.env/模型/容量，真实房状态不变。
- 实际结果：shadow 与 production 三路均 HTTP 200；生产 Web RUNNING、ready 5/5、API/Engine/Worker/FunASR/LightTTS 均持续 RUNNING，queue/dead letter 0、gate 0/0、`active_match_processing=false`；发布时四文件 SHA 与包内本地文件一致，Web stderr delta=0；真实房 278571 仍为 `paused / seq 91`。最终仅更新一条文案断言的 test source 后另做无重启同步，远端 test SHA `bfc6580f...` 与本地一致。
- 证据：release `20260717T1520-participant-controls`；发布包 SHA-256 `5eed0cac30b7043fb3c7b04dc63a888d116e6fb87eca6d60836c6bfc2ff1f31f`；源码备份 SHA-256 `98589f26a2965967ea7013f3db0c9454a8d7e0db4522bbe87692affe1829966b`。
- 回滚点：`/home/ubuntu/sunsq/phdebate/runtime/deploy-backups/20260717T1520-participant-controls-before-source.tar.gz` 与 `...-before-web.txt`；前一 release `20260717T1435-asr-boundary-tts-integrity`。
- Computer Use：初次检查时 Chrome 聚焦用户的“CHI 2026 论文列表”；稍后确认前台长时间无变化后，只切换既有稷下标签。该标签原 URL 为真实房 `/debate`，未点击任何房间内容，立即改为允许的 `/watch`；随后只打开/关闭“比赛操作”面板，不点击 retry、暂停、继续、提前结束或其他写操作。保存 TC-978/979 后恢复原 CHI 标签。Safari 系统密码锁定窗口未解锁；AdsPower/SunBrowser 未使用。
- 修复状态：VERIFIED。

### TC-978：生产房主观战页显示必要控制入口

- 状态：PASS
- 问题类型：COMPUTER USE / PRODUCTION UI / AUTHORIZATION
- 严重程度：P1（已修复）
- 页面/路由：`/rooms/278571/watch`
- 操作步骤：Computer Use 在 Chrome 现有登录会话打开真实房只读 watch；不点击任何写操作，检查房主身份说明、实时状态和操作入口。
- 预期结果：房主即使在 watch 仍看到“比赛操作”，说明文案明确可暂停、继续或提前结束；真实房保持 paused/seq 不变。
- 实际结果：页面显示“房主观战”“可暂停、继续或提前结束比赛”和“比赛操作”；实时连接正常，房状态仍为暂停。
- 证据：[房主观战操作入口截图](screenshots/20260717-005403/TC-978-Iteration18-房主观战-比赛操作入口.png)；生产 snapshot `paused / seq 91`。
- 修复状态：VERIFIED。

### TC-979：生产比赛操作面板在异常暂停时不提供错误恢复动作

- 状态：PASS
- 问题类型：COMPUTER USE / ACCESSIBILITY / STATE
- 严重程度：P1（已修复）
- 页面/路由：`/rooms/278571/watch`
- 操作步骤：只点击“比赛操作”打开面板，读取可访问性树与截图，然后 Escape 关闭；不点击面板内任何动作。
- 预期结果：异常暂停不显示可执行“继续比赛”，而显示禁用“异常暂停，请先重试”；危险操作名称为“提前结束比赛”；面板单 modal、首焦点落在关闭按钮或首个可用动作。
- 实际结果：面板精确显示禁用“异常暂停，请先重试”和“提前结束比赛”，AI 语音/麦克风/权威计时说明完整；关闭后恢复入口焦点。
- 证据：[房主观战比赛操作面板截图](screenshots/20260717-005403/TC-979-Iteration18-房主观战-比赛操作面板.png)。
- 修复状态：VERIFIED。

### TC-980：Iteration 19 发布后房主观战音频与比赛操作入口回归

- 状态：PASS
- 问题类型：COMPUTER USE / PRODUCTION REGRESSION
- 严重程度：无
- 用户影响：房主、观众
- 发生概率：高
- 页面/路由：`/rooms/278571/watch`
- 测试身份：现有 Chrome 房主会话
- 前置条件：真实房仅允许 `/watch` 只读检查；房间为异常暂停状态。
- 操作步骤：
  1. 从用户原“CHI 2026 论文列表”标签导航到公开 watch。
  2. 读取房间状态、实时连接、声音开关、全屏和比赛操作入口。
  3. 不点击 retry、暂停、继续、提前结束或任何比赛写操作。
- 预期结果：Iteration 19 发布不回归房主观战 UI；真实房状态不被改变。
- 实际结果：页面显示实时连接、比赛已暂停、开启声音、全屏和比赛操作；真实房仍为暂停。房间 seq 从 91 到 95 的增量仅来自既有 presence connected/disconnected 事件，不是本轮控制操作。
- 证据：[发布后房主观战截图](screenshots/20260717-005403/TC-980-Iteration19-生产发布后-房主观战-音频控制与比赛操作.png)。
- 控制台/网络现象：未获取；页面实时连接正常。
- 复现稳定性：本次生产回归稳定。
- 操作成本：1 次地址导航，1 次等待，最长约 5 秒。
- 根因推测：不适用。
- 建议方案：保持声音与比赛控制分离，避免本地播放设置触发比赛状态写入。
- 验收标准：watch 页面上述入口可达，真实房 snapshot 无控制事件。
- 修复状态：VERIFIED。

### TC-981：生产观战声音开关只改变本地播放状态并可还原

- 状态：PASS
- 问题类型：COMPUTER USE / AUDIO UX / STATE ISOLATION
- 严重程度：无
- 用户影响：观众、房主
- 发生概率：高
- 页面/路由：`/rooms/278571/watch`
- 测试身份：现有 Chrome 房主会话
- 前置条件：房间暂停，无当前权威 speech/cue 正在播放。
- 操作步骤：读取 fresh accessibility tree；点击一次“开启声音”；确认切换按钮为 `Value: 1 / 关闭比赛声音`；截图；再次读取 fresh tree 并点击还原。
- 预期结果：按钮可在用户手势中解锁播放；无活动音频时不报错、不触发比赛控制；还原后为静音。
- 实际结果：开启后可访问名称变为“关闭比赛声音”，页面无错误；再次点击后恢复“开启比赛声音 / Value: 0”。真实房状态未改变。
- 证据：[声音开启状态截图](screenshots/20260717-005403/TC-981-Iteration19-生产观战-声音开启状态.png)。
- 控制台/网络现象：未获取；无可见播放错误。
- 复现稳定性：开启/关闭各一次通过。
- 操作成本：2 次点击，无明显等待。
- 根因推测：不适用。
- 建议方案：后续 AudioWorklet 版本继续把用户手势解锁与权威音频 identity 分离。
- 验收标准：按钮状态正确，未生成 room control event，离开页面前恢复初始状态。
- 修复状态：VERIFIED。

### TC-982：生产 LightTTS 原生 PCM 流式 1/2/3 并发实时性

- 状态：FAIL
- 问题类型：PERF / CAPACITY / AUDIO STREAMING
- 严重程度：P1
- 用户影响：同时进行正式赛或训练赛的学生
- 发生概率：高（两个以上 AI 回合时间重叠时）
- 页面/路由：生产机本地 LightTTS `/inference_zero_shot`，`stream=true`
- 测试身份：只读容量探针
- 前置条件：readiness 正常、`active_match_processing=false`；不经过比赛房间、数据库或应用 admission gate。
- 操作步骤：使用三段固定辩论语料和三个既有音色，1/2/3 并发各执行 3 轮；记录首 PCM chunk、完成时间、RTF、chunk 数和最大块间空洞。
- 预期结果：至少两路同时具备实时输出；目标首块 P95 ≤0.9 秒、RTF P95 ≤0.8，第三路可在短时有界队列中公平等待。
- 实际结果：18/18 请求协议成功，但单路首块 P95 1.259 秒；两路首块 P95 6.333 秒、RTF P95 1.795；三路首块 P95 10.983 秒、RTF P95 2.651。每轮第二、第三请求呈明显串行等待，当前配置不能满足 2–3 场实时同时发声。
- 证据：[流式并发分析](audio/20260717-005403/iteration19-lighttts-stream-1-2-3.md)；[原始 JSON](audio/20260717-005403/iteration19-lighttts-stream-1-2-3.json)。
- 控制台/网络现象：HTTP 请求全部成功；PCM 为 24 kHz mono s16le；块间最大空洞 P95 约 0.86–0.91 秒。
- 复现稳定性：1/2/3 并发各 3 轮一致。
- 操作成本：18 次直接合成，约 30 秒；开始、每轮与结束健康检查均通过。
- 根因推测：生产 `running_max_req_size=1`、decode 单路处理和应用 active=1 共同形成串行瓶颈；这是基于启动参数、上游实现和时间序列的推测，需在独立 active=2 灰度实例验证。
- 建议方案：先实现 PCM streaming + AudioWorklet；在独立端口灰度 `active=2`，第三场短时公平等待。只有 TTFT/RTF/GPU/欠载通过门槛后才提升生产并发。
- 验收标准：2 active 下首块 P95 ≤0.9 秒、浏览器首声 P95 ≤1.5 秒、RTF P95 ≤0.8、无跨房/跨 generation 音频；第三路等待可取消且不饿死。
- 修复状态：OPEN。

### TC-983：LightTTS bi-stream PCM、最终 WAV 与取消语义

- 状态：PASS（未部署候选的只读/自动化证据）
- 问题类型：TTS / DATA INTEGRITY / CANCELLATION
- 严重程度：无
- 操作步骤：审查官方 bi-stream 发送顺序与候选 provider；执行 prompt、分段 text、finish、PCM 接收、最终 WAV 和取消定向测试。
- 预期结果：实际播放 PCM 与最终归档 WAV 来自同一字节序列；取消不发布 final，part 被删除并发出 generation abort。
- 实际结果：定向用例通过；官方 LightTTS `test_bistream.py` 也使用 init JSON→prompt WAV→并行 text/receive→finish 的相同主协议。
- 证据：[Iteration 20 审计](audio/20260717-005403/iteration20-lighttts-ws-audioworklet.md)。
- 修复状态：VERIFIED，但不足以支持启用完整链路。

### TC-984：独立音频 WebSocket 权限复验与慢客户端隔离

- 状态：PASS（未部署候选的只读/自动化证据）
- 问题类型：WEBSOCKET / AUTHORIZATION / ISOLATION
- 严重程度：无
- 操作步骤：执行公开匿名连接、跨站 Origin 拒绝、stale generation、公开转私密后的现有连接撤销、固定 PCM frame seq/PTS 测试；审查独立文件描述符和 2 秒 send timeout。
- 预期结果：音频连接不能绕过房间查看权限；单个慢观众不能反压 engine 或其他观众。
- 实际结果：相应用例通过；每秒复验 session/can_view/generation，慢 socket 独立关闭。
- 证据：[Iteration 20 审计](audio/20260717-005403/iteration20-lighttts-ws-audioworklet.md)。
- 修复状态：VERIFIED，但 future seek、真实 growing part 和 malformed subscribe 仍未通过。

### TC-985：late join future seek 与不足 400ms final tail

- 状态：FAIL
- 问题类型：AUDIO STREAMING / PROTOCOL / BROWSER PLAYBACK
- 严重程度：P0
- 操作步骤：并行审查前端 `after_seq`、服务端 file seek 和 Worklet prime/end 状态机；构造合成慢于墙上时间的 late join，以及只有 4800 samples 尾音后收到 end 的状态。
- 预期结果：重连游标不超过当前 live edge；不足 prime 水位的 final tail 仍应播放并 drained。
- 实际结果：服务端不 clamp future offset，可能零 PCM final 并伪造时间线；Worklet 稳定停在 `available>0 / primed=false / ended=true`，尾音永久静音。Node 仿真可重复。
- 用户影响：晚加入、静音恢复、网络重连或 near-final 页面可完全听不到当前 AI 发言，且 UI 可能没有明确 fallback。
- 证据：[Iteration 20 审计](audio/20260717-005403/iteration20-lighttts-ws-audioworklet.md)。
- 验收标准：server authoritative live-edge clamp + gap/reset；`end && available>0` 必须 tail-prime；新增 slow synthesis/future seek/sub-400ms 自动化。
- 修复状态：OPEN；阻断任何流式灰度。

### TC-986：AudioWorklet 流控、Safari 手势、比赛计时与 2–3 场容量门

- 状态：FAIL
- 问题类型：PERF / BROWSER / MATCH STATE / OPERATIONS
- 严重程度：P1
- 操作步骤：三路独立只读复审 server pacing、ring high-water、重试计数、AudioContext 手势、自由辩论 deadline、进程 SIGTERM lease；与生产 1/2/3 并发和主机 GPU/内存证据交叉验证。
- 预期结果：网络 burst 不导致 overflow/restart storm；只有 context running 才报告已发声；final 不改变自由辩论剩余时间；重启不遗留调度租约；至少 2 场实时。
- 实际结果：API/MessagePort 无有界 pacing，retry 在 started 后立即清零；Safari 可在 suspended context 下被误报 started；final 可把自由辩论 deadline 改成单条音频结束；SIGTERM 不主动 drain lease；当前 2/3 并发仍近似串行，同机第二实例又无安全资源余量。
- 证据：[Iteration 20 审计](audio/20260717-005403/iteration20-lighttts-ws-audioworklet.md)、[生产 1/2/3 并发基准](audio/20260717-005403/iteration19-lighttts-stream-1-2-3.md)。
- 验收标准：关闭上述状态机与生命周期问题；独立 GPU active=2/3 达到浏览器首声 P95≤1.5s、RTF P95≤0.8、无欠载和跨房串音。
- 修复状态：OPEN；生产继续 `streaming=false / active=1`。

### TC-987：Chrome 现网观战声音解锁状态与房间写操作隔离

- 状态：PASS
- 问题类型：COMPUTER USE / CHROME / AUDIO UX
- 严重程度：无
- 页面/路由：`/rooms/278571/watch`
- 操作步骤：使用用户已有 Chrome 登录会话打开公开 watch；读取权威暂停状态；点击唯一“开启比赛声音”，确认按钮变为 pressed 的“关闭比赛声音”；随后点击还原。全程不点击重试、继续、暂停、提前结束。
- 预期结果：声音开关只改变本地播放许可，不触发房间控制；无活动音频时不报错、不创建旧媒体播放。
- 实际结果：页面保持“实时连接 / 比赛已暂停 / 正方一辩立论 / 02:52”；按钮状态正确切换并还原；页面 audio 元素为 0，无比赛控制写操作。
- 证据：[Chrome 现网声音解锁截图](screenshots/20260717-005403/TC-987-Iteration20-Chrome-现网完整WAV声音解锁.png)、[Chrome 音频回归记录](audio/20260717-005403/iteration20-chrome-production-audio-regression.md)。
- 修复状态：VERIFIED（生产完整 WAV 回退路径）。

### TC-988：Chrome 结果页两条音频连续切换保持全局互斥

- 状态：PASS
- 问题类型：COMPUTER USE / HTMLAUDIO / EXCLUSIVITY
- 严重程度：无
- 页面/路由：`/rooms/566139/result`
- 操作步骤：播放 40.84 秒“反方立论录音”，确认其单独进入 playing；随后点击下一条 25.72 秒“自由辩论录音”，读取全部 9 个 audio 的 paused/currentTime/duration。
- 预期结果：开始第二条时第一条立即暂停，同一时刻最多一条发声。
- 实际结果：第一条先为 `paused=false / currentTime≈0.39s`；切换后第一条 `paused=true / currentTime≈10.67s`，第二条 `paused=false / currentTime≈0.53s`，其余 7 条 paused。
- 证据：[Chrome 结果页音频互斥截图](screenshots/20260717-005403/TC-988-Iteration20-Chrome-结果页音频互斥.png)、[Chrome 音频回归记录](audio/20260717-005403/iteration20-chrome-production-audio-regression.md)。
- 修复状态：VERIFIED。

### TC-989：Chrome 播放中离页并返回不会复活旧音频

- 状态：PASS
- 问题类型：COMPUTER USE / PAGE LIFECYCLE / OLD AUDIO
- 严重程度：无
- 页面/路由：`/rooms/566139/result` → `/` → 浏览器 Back
- 操作步骤：在第二条结果音频仍播放时直接导航赛事大厅；确认首页无 audio；浏览器返回结果页并等待 hydration，读取全部 audio 状态。
- 预期结果：离页立即停止；返回后旧音频不得自动恢复或从缓存继续。
- 实际结果：首页 `audioCount=0`；返回后 9 条音频全部 `paused=true / currentTime=0`。Chrome 本轮 console warning/error 为 0。
- 证据：[Chrome 离页返回不复活截图](screenshots/20260717-005403/TC-989-Iteration20-Chrome-离页返回音频不复活.png)、[Chrome 音频回归记录](audio/20260717-005403/iteration20-chrome-production-audio-regression.md)。
- 修复状态：VERIFIED。

### TC-990：Chrome 退出比赛页面确认明确保留席位且可取消

- 状态：PASS
- 问题类型：COMPUTER USE / PARTICIPANT EXIT / SAFETY
- 严重程度：无
- 页面/路由：`/rooms/278571/debate`
- 操作步骤：从创建冲突提示返回当前比赛；临时标签因不是控制设备显示“其他设备已接管”。点击顶部“退出比赛页面”，读取 alertdialog 后点击取消。
- 预期结果：退出页面与放弃席位严格分离；确认前不导航、不释放席位；文案说明比赛继续和席位保留。
- 实际结果：dialog 标题为“确认退出比赛页面？”，明确“比赛会继续进行，席位归属会保留；离开较久时 AI 可能接替，稍后可从‘我的’返回”；取消后关闭。未接管设备、未执行 control/retry。
- 证据：[退出页面确认截图](screenshots/20260717-005403/TC-990-Iteration21-Chrome-退出比赛页面确认与席位保留.png)、[Iteration 21 审计](audio/20260717-005403/iteration21-participant-exit-asr-production-audit.md)。
- 修复状态：VERIFIED。

### TC-991：跨房真人席位唯一性原子阻止创建且不留半成品房

- 状态：PASS
- 问题类型：COMPUTER USE / CONCURRENCY / DATA INTEGRITY
- 严重程度：无
- 页面/路由：首页 1v1 创建窗口、`/me`
- 操作步骤：当前账号仍占房 278571 真人席位时，填写唯一 QA 辩题、选择正方1辩并提交创建；随后检查个人中心和公开 live rooms。
- 预期结果：同一真人不能同时进入第二活动房；失败不得创建半成品房或席位。
- 实际结果：返回 409 可读提示“你已在房间 #278571 参赛”；个人中心仍只有房 278571，marker 不存在；公开 live rooms 也无 marker。并发 API 回归中同用户两个设备抢两个房间只有一方成功。
- 证据：[跨房创建阻止截图](screenshots/20260717-005403/TC-991-Iteration21-Chrome-跨房真人席位唯一性阻止建房.png)、[Iteration 21 审计](audio/20260717-005403/iteration21-participant-exit-asr-production-audit.md)。
- 修复状态：VERIFIED（正常创建/认领入口）。

### TC-992：started+paused 房间缺少个人永久退赛导致账号无限锁死

- 状态：FAIL
- 问题类型：PARTICIPANT EXIT / PRODUCT DEADLOCK / RECOVERY
- 严重程度：P1
- 复现：真人进入并开始比赛后，房间因人工或服务异常进入 paused；非房主退出页面并离线；尝试创建或加入另一房。
- 预期结果：学生应能使用独立、高风险确认的“退出本场并由 AI 接替”，在不破坏历史归属的前提下解除跨房真人占用。
- 实际结果：paused 被跨房检查视为活动状态；release-seat 只允许 lobby；普通参赛者无 terminate；退出页面只导航 `/me`；离线 AI 接替不处理 paused。若房主不再返回，非房主账号可无限期无法参加新比赛。真实房 278571 长时间 paused 后 aff_3 仍为 `human / connected=false`。
- 用户影响：常见异常安全暂停或房主离场即可让学生无法继续使用平台；创建错误还误导其“释放席位”，但开赛后该操作不存在。
- 证据：[Iteration 21 审计](audio/20260717-005403/iteration21-participant-exit-asr-production-audit.md)。
- 验收标准：非房主在 preparing/running/paused/judging 可幂等、两步确认地转为 `ai_substitute`；当前真人发言或待提交内容时拒绝；保留 user_id 与审计，成功后可进入新房。
- 修复状态：OPEN。

### TC-993：管理员恢复 AI 接替席位未复验跨房唯一性

- 状态：FAIL
- 问题类型：AUTHORIZATION / MULTI-ROOM ISOLATION / DATA INTEGRITY
- 严重程度：P1
- 复现：用户在旧房离线被转为 `ai_substitute` 后进入新活动房；system admin 再把旧房席位恢复为 human。
- 预期结果：恢复必须锁定 participant，并在用户已有另一活动 human assignment 时返回 409。
- 实际结果：正常跨房检查只统计 `occupant_type=human`，因此 AI 接替后允许进入新房；管理员恢复路径没有调用同一冲突检查/participant lock，数据库也无跨房 user 唯一约束，可能让同一用户同时在两个活动房成为真人。
- 用户影响：两个房间可同时授权同一账号发言、设备控制和积分归属，破坏房间隔离不变量。
- 证据：[Iteration 21 审计](audio/20260717-005403/iteration21-participant-exit-asr-production-audit.md)。
- 验收标准：恢复前 participant lock + `_active_human_assignment(exclude_room)`；冲突 409，且并发恢复/认领只有一个成功。
- 修复状态：OPEN。

### TC-994：真人麦克风 ASR 人工修正与同一 speech 音频归档生产 E2E

- 状态：BLOCKED
- 问题类型：ASR / MEDIARECORDER / TRANSCRIPT EDIT / DATA CONSISTENCY
- 严重程度：P1 发布门证据
- 阻塞原因：当前 Chrome 账号仍占真实暂停房 278571；系统正确禁止创建隔离训练房。没有其他允许的 Chrome QA 学生会话；创建新账号属于必须在动作前获得用户确认的账户操作。本轮未释放/终止真实房，也未代替用户创建账号。
- 已有证据：DebateStage 74 passed；ParticipateDialog+Lobby+DebateStage 93 passed；API 并发唯一性、finish/audio 幂等、人工 transcript 替换并保留同一 speech 音频等 4 tests passed。
- 缺失证据：真实 MediaRecorder、ASR ready/binary/final、只修改一个词、finish→audio upload 顺序、麦克风释放、最终数据库/音频内容差异和完整首尾试听。
- 固定语料与完整验收步骤：[Iteration 21 审计](audio/20260717-005403/iteration21-participant-exit-asr-production-audit.md)。
- 修复状态：BLOCKED；不能由自动化测试替代生产真人证据。

### TC-995：Chrome 移动首页与学生创建入口无横向溢出

- 状态：PASS
- 问题类型：COMPUTER USE / MOBILE / RESPONSIVE
- 严重程度：无
- 页面/路由：`/`
- 操作步骤：将 Chrome 页面视口切换为移动窄屏，检查首页标题、正式赛/训练赛卡片、学生创建入口和文档宽度。
- 预期结果：主内容完整可读，无横向滚动；核心创建 CTA 可达。
- 实际结果：页面无横向溢出，正式赛/训练赛入口均可见，主创建按钮高 46px。该截图保存于视口切换稳定前的 375×812 内容区；随后 TC-999–1001 已补精确 390×844 证据。
- 证据：[移动首页截图](screenshots/20260717-005403/TC-995-Iteration22-Chrome-390x844-首页与学生创建入口.png)、[Iteration 22 移动回归](audio/20260717-005403/iteration22-chrome-390x844-mobile-regression.md)。

### TC-996：移动全局导航和少数恢复控件未达到 44×44 触控目标

- 状态：FAIL
- 问题类型：MOBILE / ACCESSIBILITY / TOUCH TARGET
- 严重程度：P2
- 页面/路由：全局导航、创建窗口、`/rooms/278571/debate`
- 复现：在 Chrome 窄屏展开导航，并测量菜单、账号、退出、导航链接、创建窗关闭、异常重试、面板关闭和设备接管按钮。
- 预期结果：项目移动端交互目标至少为 44×44 CSS px，且相邻目标不易误触。
- 实际结果：菜单 36×36、账号 90×37、退出 35×35、展开导航 317×39；创建窗关闭约 42×42；异常重试 328×40；比赛操作面板关闭约 27.7×42、设备接管 147×38。核心创建、继续比赛、舞台和主要操作仍可达，未复现流程阻断。
- 用户影响：增加单手操作和精细动作困难用户的误触风险；当前不升级为 P1。
- 证据：[移动导航截图](screenshots/20260717-005403/TC-996-Iteration22-Chrome-390x844-移动导航与触控目标.png)、[操作面板截图](screenshots/20260717-005403/TC-1000-Iteration22-Chrome-390x844-比赛操作面板.png)、[Iteration 22 移动回归](audio/20260717-005403/iteration22-chrome-390x844-mobile-regression.md)。
- 修复状态：OPEN。

### TC-997：Chrome 移动 1v1 创建窗口完整可达

- 状态：PASS
- 问题类型：COMPUTER USE / MOBILE / CREATE DIALOG
- 严重程度：无
- 页面/路由：首页 1v1 创建窗口
- 操作步骤：在窄屏打开 1v1 创建窗口，检查创建/搜索模式、正反方席位、提交和关闭控件。
- 预期结果：窗口不越界，选择和提交路径完整可达。
- 实际结果：窗口无横向溢出；模式卡约 285×82、席位按钮 285×48、提交 285×46。关闭约 42×42，作为 TC-996 的次要实例登记。
- 证据：[1v1 创建窗口截图](screenshots/20260717-005403/TC-997-Iteration22-Chrome-390x844-1v1创建窗口.png)。

### TC-998：Chrome 移动个人中心可继续进行中比赛

- 状态：PASS
- 问题类型：COMPUTER USE / MOBILE / MY ROOMS
- 严重程度：无
- 页面/路由：`/me`
- 操作步骤：打开个人中心，等待数据加载完成，检查进行中房间卡片和继续比赛入口。
- 预期结果：无横向溢出，当前比赛与返回入口不被遮挡。
- 实际结果：页面布局稳定，房 278571 卡片和继续比赛入口可见、可达；只有复用的顶部导航触控尺寸计入 TC-996。
- 证据：[移动个人中心截图](screenshots/20260717-005403/TC-998-Iteration22-Chrome-390x844-个人中心与继续比赛.png)。

### TC-999：Chrome 精确 390×844 辩论舞台无横向溢出

- 状态：PASS
- 问题类型：COMPUTER USE / MOBILE / DEBATE STAGE
- 严重程度：无
- 页面/路由：`/rooms/278571/debate`
- 操作步骤：在 `window.innerWidth=390 / innerHeight=844 / DPR=1` 下读取双方席位、阶段、计时、暂停告警、多设备只读状态和底部控制。
- 预期结果：核心舞台信息和控制均在视口内，不出现横向滚动或遮挡。
- 实际结果：文档 `clientWidth=scrollWidth=390`；两队、暂停状态、剩余 02:52、多设备只读和底部控制均可见。退出、声音、全屏和比赛操作按钮均 44×44。
- 证据：[精确 390×844 舞台截图](screenshots/20260717-005403/TC-999-Iteration22-Chrome-390x844-辩论舞台与多设备只读.png)。

### TC-1000：Chrome 移动比赛操作面板在异常与多设备状态下可用

- 状态：PASS
- 问题类型：COMPUTER USE / MOBILE / MATCH CONTROLS
- 严重程度：无（偏小触控目标计入 TC-996 P2）
- 页面/路由：`/rooms/278571/debate`
- 操作步骤：只打开“比赛操作”面板，检查异常暂停、提前结束、退出页面、设备状态和接管入口；不点击任何写操作。
- 预期结果：面板内容完整，危险操作清晰分离且不会被底部区域遮挡。
- 实际结果：提前结束和退出页面均为 320×44；状态说明和设备接管均可见。关闭与接管按钮偏小，计入 TC-996。未点击重试、结束、退出确认或接管。
- 证据：[比赛操作面板截图](screenshots/20260717-005403/TC-1000-Iteration22-Chrome-390x844-比赛操作面板.png)。

### TC-1001：Chrome 移动退出比赛页面二次确认可取消

- 状态：PASS
- 问题类型：COMPUTER USE / MOBILE / PARTICIPANT EXIT
- 严重程度：无
- 页面/路由：`/rooms/278571/debate`
- 操作步骤：点击顶部退出页面入口，读取 alertdialog，并只点击取消。
- 预期结果：确认前不退出；文案说明比赛继续、席位保留和 AI 可能接替；取消与确认按钮适合触控。
- 实际结果：确认文案完整；取消与“确认退出页面”均高 44px。取消后对话框关闭，未退出、未释放席位，也未改变比赛状态。
- 证据：[移动退出确认截图](screenshots/20260717-005403/TC-1001-Iteration22-Chrome-390x844-退出比赛页面确认.png)。

### TC-1002：Chrome 移动 1v1 赛果顶部、长评议与返回路径完整

- 状态：PASS
- 问题类型：COMPUTER USE / MOBILE / RESULT
- 严重程度：无
- 页面/路由：`/rooms/566139/result`
- 操作步骤：在 Chrome override `390×844` 下打开已完成 1v1 房，检查比分、长裁判理由、摘要、评分和两个返回入口。
- 预期结果：内容正常换行，无横向溢出，返回个人中心和赛事大厅均可达。
- 实际结果：`scrollWidth=clientWidth=375`；两条返回按钮均 168×46，比分与长理由完整，HTML 测试文本按普通文本显示。
- 证据：[移动 1v1 赛果截图](screenshots/20260717-005403/TC-1002-Iteration23-Chrome-390x844-1v1赛果顶部与评分.png)、[Iteration 23 记录](audio/20260717-005403/iteration23-mobile-result-weak-network-audio.md)。

### TC-1003：移动结果音频控件高度仅约 30px

- 状态：FAIL
- 问题类型：MOBILE / ACCESSIBILITY / AUDIO CONTROL
- 严重程度：P2
- 复现：滚动到 1v1/4v4 完整辩论记录，测量每条原生 `<audio>`。
- 预期结果：高频重复播放控件达到项目 44×44 CSS px 触控目标。
- 实际结果：控件约 240×30px；1v1 有 9 个、4v4 有 11 个重复实例。控件可以播放且互不重叠，但增加移动触控误操作风险。
- 证据：[录音与长文本截图](screenshots/20260717-005403/TC-1003-Iteration23-Chrome-390x844-赛果录音与长文本卡片.png)、[Iteration 23 记录](audio/20260717-005403/iteration23-mobile-result-weak-network-audio.md)。
- 修复状态：OPEN。

### TC-1004：Chrome 移动结果 AI WAV 可播放

- 状态：PASS
- 问题类型：COMPUTER USE / HTMLAUDIO / MOBILE
- 严重程度：无
- 页面/路由：`/rooms/566139/result`
- 操作步骤：点击 40.84 秒反方立论 WAV，读取全部 audio 状态。
- 预期结果：点击后成功播放，只有所选录音处于播放状态。
- 实际结果：目标音频 `paused=false / currentTime>0 / duration=40.84 / readyState=4`，其余全部暂停。
- 证据：[移动音频播放截图](screenshots/20260717-005403/TC-1004-Iteration23-Chrome-390x844-结果音频播放与互斥.png)。

### TC-1005：Chrome 移动快速切换音频保持全局互斥

- 状态：PASS
- 问题类型：COMPUTER USE / AUDIO EXCLUSIVITY / MOBILE
- 严重程度：无
- 页面/路由：`/rooms/566139/result`
- 操作步骤：第一条 40.84 秒 WAV 正在播放时滚动并点击下一条 25.72 秒 WAV。
- 预期结果：前一条立即暂停，后一条开始，任意时刻只保留最终选择。
- 实际结果：A `paused=true / currentTime≈38.58`；B `paused=false / currentTime≈1.23`；其余暂停。
- 证据：[移动快速切换截图](screenshots/20260717-005403/TC-1005-Iteration23-Chrome-390x844-快速切换音频保持互斥.png)。

### TC-1006：Chrome 移动播放中离页并返回不复活旧音频

- 状态：PASS
- 问题类型：COMPUTER USE / PAGE LIFECYCLE / AUDIO
- 严重程度：无
- 页面/路由：`/rooms/566139/result` → `/me` → Back
- 操作步骤：在 WAV `currentTime=0.706s` 时离页到个人中心，再返回结果页。
- 预期结果：离页停止且释放媒体；返回后不得自动续播或保留旧进度。
- 实际结果：`/me` audio=0；返回后 9 条全部 `paused=true / currentTime=0 / readyState=0`。
- 证据：[移动离页返回截图](screenshots/20260717-005403/TC-1006-Iteration23-Chrome-390x844-播放中离页返回不复活.png)。

### TC-1007：Chrome 移动 1v1 时间线内外滚动均可达

- 状态：PASS
- 问题类型：COMPUTER USE / MOBILE / NESTED SCROLL
- 严重程度：无
- 页面/路由：`/rooms/566139/result`
- 操作步骤：页面滚至 600px 时间线容器，触控滚动内层 2400px，再从容器外继续滚动页面。
- 预期结果：时间线可到末尾，外层仍能到加载按钮和 footer，不形成滚动陷阱。
- 实际结果：内层从 `scrollTop=0` 到 2400；外层从 5009 到页面底部 5388，加载按钮高 46px 可达。
- 证据：[时间线滚动截图](screenshots/20260717-005403/TC-1007-Iteration23-Chrome-390x844-时间线与加载更早事件.png)。

### TC-1008：Chrome 移动 1v1 时间线加载至 60/60 并保持位置

- 状态：PASS
- 问题类型：COMPUTER USE / PAGINATION / SCROLL POSITION
- 严重程度：无
- 页面/路由：`/rooms/566139/result`
- 操作步骤：在内层时间线底部点击“加载更早事件”。
- 预期结果：从 50/60 加载到 60/60，按钮消失且阅读位置不跳回顶部。
- 实际结果：显示 `已加载 60 / 60`，按钮消失，内层保持底部 `scrollTop=3000 / scrollHeight=3600`。
- 证据：[60/60 时间线截图](screenshots/20260717-005403/TC-1008-Iteration23-Chrome-390x844-时间线加载60项与滚动保持.png)。

### TC-1009：Chrome 移动 4v4 赛果与八席数据完整

- 状态：PASS
- 问题类型：COMPUTER USE / MOBILE / 4V4 RESULT
- 严重程度：无
- 页面/路由：`/rooms/764886/result`
- 操作步骤：打开 4v4 完成房，检查比分、长裁判理由、赛事名称、积分轨迹、八席评分、11 条录音和时间线计数。
- 预期结果：所有数据完整，无横向溢出或卡片截断。
- 实际结果：`scrollWidth=clientWidth=375`；摘要显示正式赛、11 发言、93 事件、净变动 6；八席均有评分或明确“暂无评分”。
- 证据：[4v4 移动赛果截图](screenshots/20260717-005403/TC-1009-Iteration23-Chrome-390x844-4v4赛果与个人评分.png)。

### TC-1010：4v4 个人评分标题与说明在移动端挤压错行

- 状态：FAIL
- 问题类型：MOBILE / RESPONSIVE / CONTENT HIERARCHY
- 严重程度：P2
- 复现：在 375px 实际布局宽度滚到“个人评分”章节。
- 预期结果：章节标题与辅助说明保持清晰层级，不互相挤压或视觉交错。
- 实际结果：“个人评分”被拆成两行，右侧“未产生有效发言评分的席位显示暂无评分”也多行折叠并与标题交错；八席评分数据仍完整可达。
- 证据：[八席评分截图](screenshots/20260717-005403/TC-1010-Iteration23-Chrome-390x844-4v4八席评分与结算轨迹.png)。
- 修复状态：OPEN。

### TC-1011：Chrome 移动 4v4 时间线加载至 93/93 并保持位置

- 状态：PASS
- 问题类型：COMPUTER USE / PAGINATION / 4V4 TIMELINE
- 严重程度：无
- 页面/路由：`/rooms/764886/result`
- 操作步骤：内层滚至第 50 项后，外层到加载按钮并加载剩余 43 项。
- 预期结果：最终 93/93、按钮消失、内层停留末尾、footer 可达。
- 实际结果：`已加载 93 / 93`；按钮消失；`scrollTop=4980 / clientHeight=600 / scrollHeight=5580`。
- 证据：[93/93 时间线截图](screenshots/20260717-005403/TC-1011-Iteration23-Chrome-390x844-4v4时间线加载93项与位置保持.png)。

### TC-1012：Fast 3G 下完整 WAV 约 1.9 秒开始推进

- 状态：PASS
- 问题类型：COMPUTER USE / NETWORK THROTTLING / RANGE AUDIO
- 严重程度：无
- 页面/路由：`/rooms/566139/result`
- 条件：150ms latency、200000 B/s 下行、禁用缓存。
- 操作步骤：页面数据加载后点击 1.96MB、40.84 秒 WAV，每约 100ms 读取媒体状态并捕获 CDP Network 事件。
- 预期结果：建议门槛 3 秒内开始，使用可续传 Range，无媒体错误。
- 实际结果：1.926 秒首次 `currentTime>0`，`readyState=4`；先请求 `Range: bytes=0-`，随后 `bytes=130347-`，均 206。
- 证据：[Fast 3G 播放截图](screenshots/20260717-005403/TC-1012-Iteration23-Chrome-390x844-Fast3G结果音频首播.png)、[Iteration 23 记录](audio/20260717-005403/iteration23-mobile-result-weak-network-audio.md)。

### TC-1013：Slow 3G 下完整 WAV 约 5.8 秒开始推进

- 状态：PASS
- 问题类型：COMPUTER USE / SLOW NETWORK / RANGE AUDIO
- 严重程度：无
- 页面/路由：`/rooms/566139/result`
- 条件：400ms latency、50000 B/s 下行、禁用缓存。
- 操作步骤：页面数据完成后点击同一 WAV并轮询媒体状态。
- 预期结果：建议门槛 8 秒内开始，不出现媒体错误。
- 实际结果：5.787 秒首次 `currentTime>0`，`duration=40.84 / readyState=4`；首段与续段均返回正确 206。
- 证据：[Slow 3G 播放截图](screenshots/20260717-005403/TC-1013-Iteration23-Chrome-390x844-Slow3G结果音频首播.png)。

### TC-1014：Slow 3G pending A→B 切换拖慢最终音频

- 状态：FAIL
- 问题类型：PERFORMANCE / AUDIO CANCELLATION / SLOW NETWORK
- 严重程度：P2
- 复现：禁用缓存的 Slow 3G 下，点击 A 后约 250ms 点击 B，持续读取两条媒体状态。
- 预期结果：旧 A 立即终止下载；最终 B 的首播延迟不应显著高于单条 B，旧 A 不得抢回。
- 实际结果：A 从未推进且最终 paused，B 保持最终选择，没有双播或抢回；但 B 到首次 `currentTime>0` 延长到约 10.447 秒，而单条 Slow 3G 为 5.787 秒。A 在约 9.8 秒已到 `readyState=3`，表明旧 pending 请求仍消耗有限带宽。
- 证据：[Slow 3G A→B 截图](screenshots/20260717-005403/TC-1014-Iteration23-Chrome-390x844-Slow3G快速AB切换.png)、[Iteration 23 记录](audio/20260717-005403/iteration23-mobile-result-weak-network-audio.md)。
- 修复状态：OPEN。

### TC-1015：新鲜生产标签页正式赛命名与训练赛范围一致

- 状态：PASS
- 问题类型：COMPUTER USE / CONTENT / PRODUCT SCOPE
- 严重程度：无
- 页面/路由：`/`
- 操作步骤：使用新建 Chrome 标签重新请求首页，读取 hero、赛事卡和直播行。
- 预期结果：4v4 显示“正式赛”，1v1 训练赛继续保留，学生可自行创建。
- 实际结果：hero、4v4 卡片和房 278571 直播行均为“4v4 人机辩论正式赛”；同时显示“1v1 辩论训练赛”和学生创建文案。先前已打开很久的用户标签仍有旧“日常赛”DOM，确认属于旧页面未刷新而非生产回归。
- 证据：[首页命名截图](screenshots/20260717-005403/TC-1015-Iteration23-Chrome-390x844-首页正式赛名称回归.png)。

## 补充原子用例登记（47 条）

以下用例均来自现存截图、音频、JSON 或生产回归记录，不把代码审计和覆盖矩阵当作执行证据。与上方 133 个长格式用例合计 180 个明确、可复现用例记录。

### UI、多用户与部署回归（20 条）

| ID | 状态 | 最小复现与实际结果 | 证据 |
|---|---|---|---|
| TC-002 | PASS | 匿名打开 `/login`；表单、导航正常，无白屏乱码 | `TC-002-登录页-正常.png`、`TC-002-登录页-渲染检查.png` |
| TC-011 | PASS | User A 登录后打开 1v1 赛事详情，赛事规则与入口可见 | `TC-011-1v1赛事详情-UserA.png` |
| TC-104 | BLOCKED（历史 FAIL） | Safari 普通窗口提交注册；历史结构化错误显示对象数组，修复后最终创建账号受确认规则限制 | `TC-104-UserB注册-失败.png` |
| TC-105 | BLOCKED（历史 FAIL） | Safari 私密窗口重复注册；历史失败同上 | `TC-105-UserB私密注册-失败.png` |
| TC-106A | PASS | SunBrowser 注册 User B 并返回登录态首页 | `TC-106-UserB注册-成功.png` |
| TC-106B | PASS | User B 刷新，身份和页面权限保持 | `TC-106-UserB刷新保持-成功.png` |
| TC-107 | PASS | User B 直接访问 User A 控制台，被拒绝并转观战 | `TC-107-UserB控制台-重定向观战.png` |
| TC-108A | PASS | User B 打开已完成房间 `/watch`，转到结果页 | `TC-108-UserB观战页-已结束重定向赛果.png` |
| TC-108B | PASS | User B 打开已完成房间 `/debate`，转到结果页 | `TC-108-UserB辩论页-已结束重定向赛果.png` |
| TC-503 | PASS | 1v1 未准备时开始按钮禁用 | `TC-503-1v1未准备-开始禁用.png` |
| TC-504 | PASS | 点击准备后席位和开赛条件实时更新 | `TC-504-1v1准备完成.png` |
| TC-505 | PASS | 本人真人轮次发言按钮启用，非本人轮次不启用 | `TC-505-1v1人类回合-可发言.png` |
| TC-507 | PASS | 人类回合结束后 AI 字幕、当前席位与 LightTTS 阶段一致 | `TC-507-1v1-AI立论-字幕.png` |
| TC-602A | PASS | User B 输入房号并认领 4v4 空闲真人席位 | `TC-602-UserB加入4v4-加入前.png`、`TC-602-UserB加入4v4-成功.png` |
| TC-602B | PASS | Chrome/SunBrowser 同时查看大厅，两个真人席位实时一致 | `TC-602-4v4双用户大厅-两席.png` |
| TC-603 | PASS | User B 准备后，房主端同步并启用开始 | `TC-603-UserB准备-成功.png`、`TC-603-4v4双用户准备-开始可用.png` |
| TC-907 | FAIL / CLOSED | 首次部署后提交 1v1 建房，301/307 循环导致 `Failed to fetch` | `TC-907-部署后创建1v1房间-Failed-to-fetch.png` |
| TC-908 | PASS | 精确 Nginx 路由热修后同操作成功进入房间 586109 | `TC-908-部署后房间创建修复-成功进入586109.png` |
| TC-909 | PASS | 真人轮次点击开始发言，立即显示“正在启动麦克风” | `TC-909-部署后麦克风启动-即时反馈.png` |
| TC-910 | PASS | 真人回合结束后录音指示消失，后续 LightTTS 正常播放 | `TC-910-部署后跨环节麦克风释放-LightTTS播放.png` |

### FunASR 固定语料（10 条）

共同复现：`scripts/benchmark_funasr.py` + 固定 manifest，生产 FunASR，4 并发、100ms 分片、`pace=1.0`。权威结果：`audio/20260717-005403/funasr-benchmark-v1-final-20260717.json`。

| ID | 状态 | 样本与实际结果 |
|---|---|---|
| TC-ASR-001 | PASS | `opening_education.wav`，CER 0，首尾锚点通过 |
| TC-ASR-002 | FAIL | `numbers_and_units.wav`，“三点五秒”→“三点”，CER 6.25% |
| TC-ASR-003 | FAIL | `names_and_terms.wav`，“竺可桢”→“竹可珍”，句首锚点失败 |
| TC-ASR-004 | FAIL | `mixed_chinese_english.wav`，`ChatGPT` 英文片段不稳定 |
| TC-ASR-005 | PASS（严格文本有差异） | `polyphonic_characters.wav`，CER 2.78%，仅“作出”→“做出” |
| TC-ASR-006 | FAIL | `head_tail_anchors.wav`，首尾锚点均失败，CER 11.90% |
| TC-ASR-007 | PASS | `controlled_repetition.wav`，CER 0，无额外重复二元组 |
| TC-ASR-008 | PASS（有中段删除） | `long_argument.wav`，CER 2.83%，STOP final 1345ms |
| TC-ASR-009 | PASS | `negative_silence.wav`，无 partial、无实质文本 |
| TC-ASR-010 | PASS | `negative_low_noise.wav`，无误识别 |

这些是 LightTTS→FunASR 回转流，不是真人麦克风 CER。

### LightTTS 音色、短句与争用（17 条）

共同证据：`audio/20260717-005403/lighttts-benchmark-isolated/`、`lighttts-benchmark-short/`、`lighttts-benchmark-smoke/` 的 JSON、Markdown 和 WAV。

| ID | 状态 | 实际结果 |
|---|---|---|
| TC-TTS-I01 | PASS（自动指标） | voice1 标准中文，CER 0、RTF 0.754、最大内部静音 440ms |
| TC-TTS-I02 | FAIL（自动内容门） | voice1 人名标点，raw CER 10.81% |
| TC-TTS-I03 | FAIL（拼接静音） | voice2 标准中文 CER 0、RTF 0.722，但内部静音 830ms |
| TC-TTS-I04 | FAIL | voice2 人名标点 CER 24.32%、内部静音 560ms |
| TC-TTS-I05 | PASS（自动指标） | voice3 标准中文 CER 0、RTF 0.688、内部静音 360ms |
| TC-TTS-I06 | FAIL | voice3 人名标点 CER 21.62%、内部静音 800ms |
| TC-TTS-I07 | PASS（自动指标） | voice4 标准中文 CER 0、RTF 0.720、内部静音 320ms |
| TC-TTS-I08 | FAIL | voice4 人名标点 CER 16.22% |
| TC-TTS-S01 | PASS | voice1 一字“好”回转一致，合成 0.992s |
| TC-TTS-S02 | PASS | voice1 五字“证据最重要”一致，1.398s |
| TC-TTS-S03 | PASS | voice2 一字一致，1.024s |
| TC-TTS-S04 | PASS（前导静音观察） | voice2 五字一致，1.627s；前导静音 720ms |
| TC-TTS-S05 | PASS | voice3 一字一致，0.788s |
| TC-TTS-S06 | PASS | voice3 五字一致，1.237s |
| TC-TTS-S07 | PASS | voice4 一字一致，0.836s |
| TC-TTS-S08 | PASS | voice4 五字一致，1.937s |
| TC-TTS-P01 | FAIL | 与 FunASR 基准争用时 synth P95 21.527s，实时性分 40 |

上述 PASS 只代表自动内容/波形/耗时子项，不能替代自然度 MOS；专名 raw CER 可能包含 FunASR 同音正字差异，仍需人工听辨。

## 去重后的缺陷清单

### DEF-001（P2）：登录错误响应被渲染为 `[object Object]`

- 修复状态：VERIFIED
- 影响用例：TC-102
- 最小复现：打开 `/login`，输入不存在的账号和任意非敏感测试密码，点击登录。
- 预期：显示明确、安全的账号或密码错误提示。
- 实际：显示 `[object Object]`。
- 截图：`screenshots/20260717-005403/TC-102-登录-错误账号密码.png`
- 建议：规范前端错误对象到展示字符串的映射，并为无法识别的响应设置通用错误文案。

### DEF-002（P2）：匿名用户可直接进入 `/rooms/:code/debate`

- 修复状态：VERIFIED
- 影响用例：TC-302
- 最小复现：退出主站登录，直接访问任一公开房间的 `/rooms/:code/debate`。
- 预期：拒绝、跳转登录页或跳转观战页。
- 实际：进入完整辩论舞台；仅发言按钮被禁用。
- 截图：`screenshots/20260717-005403/TC-302-匿名直达辩论页-未拒绝.png`
- 建议：增加前端路由守卫和服务端权限校验，并为匿名访问统一重定向到观战页。

### DEF-003（P2）：静音/底噪被 ASR 幻觉识别并进入 Judge，空音频归档为 0:00

- 影响用例：TC-506
- 实际：无刻意语音仍产生长段乱码；服务端持久化 Speech/TranscriptSegment，Judge 将其作为真人论证；静音音频被归档且结果页显示 0:00/0:00 或不可播放。
- 根因：采集端和服务端均缺少 VAD/置信度/文本质量门；音频上传仅按 Blob/容器检查；真人音频未回填时长；Judge/Agent 历史无质量过滤。
- 修复状态：VERIFIED。二次部署后静音进入人工核对，不持久化内容或音频；非 WAV 使用 ffmpeg 解码帧计算时长并做 VAD。

### DEF-004（P2）：本机正在录音时错误提示“该席位正在另一设备发言”

- 修复状态：VERIFIED。权威 active speech 与本地 speech id 匹配时显示“当前设备正在发言/整理发言”。
- 影响用例：TC-506、4v4 正方一辩
- 实际：录音和“结束发言”控件有效，但状态文案同时声称另一设备占用席位，易诱导用户刷新或放弃发言。
- 建议：将本连接的 ownership/session token 与服务器 speaking 状态合并判断；本机持有录音租约时显示“正在录音”。

### DEF-005（P1）：4v4 真人“开始发言”按钮启用但跨浏览器无响应

- 影响用例：TC-606
- 实际：Chrome User A 与 SunBrowser User B 均复现，麦克风权限已允许；点击和键盘激活后无录音态、无错误提示、无人工恢复入口，最终直接超时。
- 历史用户影响：课堂比赛中真人会无声丢失整轮发言，且没有可恢复动作；该阻断项现已关闭。
- 修复状态：VERIFIED。生产 1v1 真实麦克风回归和 Web 竞态测试均通过；启动、停止、跨阶段清理与旧回调隔离已闭环。

### DEF-006（P2）：Safari 注册失败与结构化错误不可读

- 影响用例：User B 初始注册
- 实际：Safari 普通与私密窗口多次提交均显示 `[object Object],[object Object]`；相同信息在 SunBrowser 可注册。代码审计发现 Safari/自动填充改变 DOM 值但未触发 React 状态同步，同时 API 错误数组直接传给 `Error`。
- 修复状态：FIXED / PARTIALLY VERIFIED。Safari 错误登录的结构化错误已验证可读；注册最终提交因 Computer Use 账号创建确认规则未再次创建账号，保留为专项补测。

### DEF-007（P1）：发言启动竞态导致跨阶段隐形麦克风持续占用

- 影响用例：TC-606；截图 `screenshots/20260717-005403/TC-609-4v4跨阶段-麦克风仍录音.png`
- 实际：Chrome 自由辩论点击“开始发言”后 UI 未进入录音态；轮次超时并进入反方四辩、正方四辩 AI 总结后，Chrome 窗口仍持续显示“麦克风正在录音”。页面没有结束按钮或占用提示，用户无法从产品界面停止隐形采集。
- 风险：隐私、设备资源和回声污染 ASR；若流继续发送，可能将后续 AI TTS/环境声错误写入真人记录。
- 建议：对 `getUserMedia`/MediaRecorder 启动使用可取消的 turn token；阶段、发言人、连接或组件变化时统一 stop recorder、stop tracks、关闭 ASR socket；迟到的权限 Promise 返回时先校验 token，再决定是否启动；所有失败和晚启动必须显示明确反馈。
- 修复状态：VERIFIED。房间 586109 人类回合结束后 Chrome 录音指示消失，随后 LightTTS 正常播放；二次回归离开辩论页后无录音指示。

### DEF-008（P2）：结果页“加载更早事件”请求失败，页面无法查看完整时间线

- 影响用例：TC-615
- 实际：4v4 共有 93 个事件，首屏仅 50 个；点击加载更早事件后显示 `Failed to fetch`，计数仍为 50/93。
- 用户影响：教师/研究者无法在 UI 中复核早期准备、控制、超时和发言事件，只能下载 JSON 后离线检查。
- 修复状态：VERIFIED。Chrome 与 SunBrowser 均达到 93/93。

### DEF-009（P3）：AI 裁判阶段发言按钮禁用理由误写“比赛尚未开始”

- 实际：比赛已完成所有发言并正在 Judge 时，真人控制区使用等待赛前的原因文案。
- 建议：为 `judging`、`completed`、`paused`、非本人轮次分别提供权威状态文案，避免状态机已推进但 UI 语义倒退。
- 修复状态：VERIFIED。裁判阶段改为“裁判正在评议”。

### DEF-010（P2）：结果页多条发言音频可同时播放并发生重叠

- 实际：Chrome 结果页先播放“见山”发言，再点击下一条“清和”发言后，两条媒体控件同时显示“暂停”，系统级媒体状态仍为正在播放；页面没有自动暂停上一条音频。
- 用户影响：课堂投影复盘或个人复盘时会出现两段语音叠加，听感不可辨，且用户难以定位需要停止的上一条媒体。
- 证据：[Chrome 多音频切换截图](screenshots/20260717-005403/cu-chrome-result-audio-switch-20260717.jpg)；Computer Use 可访问性树同时出现两个“暂停”控件。
- 建议：结果页维护唯一 active audio；任一 `play` 事件触发时暂停并复位其他 `<audio>`，切页/卸载时统一停止；补充连续切换、快速连点、真人/AI 交叉切换和移动端用例。
- 修复：结果页维护音频元素集合，任一 `play` 暂停其他媒体；路由 code 变化或卸载时暂停并归零全部媒体，并清理已从赛果数据中移除的引用。
- 自动化验证：结果页 `12/12`、Web 全量 `97/97`、TypeScript、Next.js production build 全部通过。
- 生产回归：房间 764886 连续播放“见山→清和”后仅“清和”显示暂停，前一条恢复为播放；离开到 `/me` 后媒体状态消失，返回结果页无旧音频复活。证据：[修复后截图](screenshots/20260717-005403/TC-916-结果页音频互斥-修复后.jpg)。
- 修复状态：VERIFIED。release `20260716T205546Z-qa-audio-exclusive`，回滚点见部署章节。

### DEF-011（P2）：SunBrowser 长结果页批量预载音频导致多数媒体不可播放

- 影响用例：TC-917。
- 实际：11 条音频的结果页刷新后出现 9–11 条 `Unable to play media`，后续控件禁用；同一页面 Chrome 正常，服务器逐 URL Range 全部 206。
- 用户影响：教师在独立浏览器/课堂终端复盘时，列表后半部分音频无法启动，且页面没有重试或失败原因。
- 根因推测：每条 `<audio preload="metadata">` 同时初始化，在 SunBrowser/AdsPower Chromium 环境触发媒体解码器或并发资源上限；需通过按需加载修复后复测确认。
- 建议：只保留一个实际媒体元素，或将列表音频改为 `preload="none"` 并用数据库 duration 展示时长；用户点击后才装载 src，切换时卸载上一资源。
- 修复：全部音频改为 `preload="none"`，常驻展示服务端 `duration_seconds`，新增 ARIA label 和移动端换行；保留唯一播放与离页清理。
- 验证：结果页 `13/13`、Web `98/98`、TypeScript/build 通过；SunBrowser 无缓存刷新后 11 个 play 全部可用，第 1/6/11 条实际播放并互斥。
- 修复状态：VERIFIED。release `20260716T211321Z-qa-audio-lazy`，回滚点见部署章节。

### DEF-012（P2）：完整测试中的前序 preparing 房被后续全局 Engine tick 扫描

- 影响质量门：完整 API 套件连续两次失败于 `test_twenty_four_mixed_rooms_advance_only_their_authoritative_state`。
- 实际：24 房用例期望只看到本用例 4 个 preparing room 的 `background_cues`，实际额外出现 1 个前序用例房号；两次房号不同，但均来自前序 Engine 工作。
- 隔离证据：该用例单独复跑 `1 passed in 2.22s`；完整套件第二轮为 `218 passed, 1 failed, 9 warnings in 44.29s`，因此不能把 API 全量标为绿色。
- 根因：并非 20 房 task 或内存集合泄漏；更早的 Consent 用例启动了真实课堂房却没有终止，后续全局 `match_engine.tick()` 按产品语义正确扫描了该仍为 `preparing` 的房间。
- 修复：Consent 测试通过真实 terminate API 做领域级收尾；Engine `stop()` 统一 cancel+drain room tasks；新增 owning-loop drain，并在事件循环变化但仍有活跃 room task 时明确拒绝，避免静默清 registry 形成 orphan。
- 验证：精确 `consent→20房→24房 + lifecycle` 为 `5/5`；multi-room `19/19`；完整 API `225/225`。生产仅部署独立 `match_engine.py` 加固，Engine 重启后 ready 200、stderr 空、Computer Use 首页与结果页正常。
- 修复状态：VERIFIED。回滚文件与 SHA 见 Iteration 9。

### DEF-013（历史 P1，当前 REMOVED-SCOPE）：课堂批量建房与学生录音同意形成不可完成循环

- 修复状态：VERIFIED（入口循环关闭）
- 影响用例：TC-931
- 用户影响：所有通过课堂 Activity 批量参赛的学生与教师；发生概率高。
- 最小复现：学生加入 active QA classroom；教师把学生加入 Activity；学生访问 `/me`；教师尝试 provision。
- 预期：学生可在房间创建前自主阅读并同意当前录音政策，教师仅在全部 current grant 后建房。
- 实际：学生同意控件只存在于已创建房间的 lobby/debate；provision 又在创建房间前要求 grant，因此合法 UI 路径永远无法完成。
- 截图：[学生个人中心缺口](screenshots/20260717-005403/TC-931-学生个人中心-缺少课前录音同意入口.png)
- 历史修复：曾新增 self-only `/api/consents/me/classrooms` 与 `/me` 待处理同意区，生产 TC-932 验证入口循环关闭。Iteration 12 按用户指令移除该用户入口并停止课堂/Consent 流程；未继续执行 grant/revoke，当前不计作开放缺陷。

### PRODUCT-GAP-001：4v4 结果缺少八个席位的个人评分明细

- 当前只显示团队比分 82:76、裁判理由和两名真人的积分结算；没有八席个人维度评分、评分依据或无发言席位标记。
- 对教学复盘，建议最小 MVP 展示每席“是否实际发言、内容/表达/回应维度分、总分、扣分原因”，AI 与真人明确区分；没有发言的 User B 不应被误解为已完成个人表现评分。
- 修复状态：VERIFIED。部署后显示 8 席个人分；User B 无有效发言显示“暂无评分”。

## 性能观察

- Agent 模型连接测试：约 60.6 ms，成功。
- 页面加载：公开页面大多在 1–5 秒内完成可见渲染（Computer Use 观察，未使用 DevTools 精确计时）。
- LightTTS：1v1 与 4v4 多轮均能播放，AI 字幕与音频阶段一致；固定语料的隔离 RTF P95 为 0.743，1/5 字短文本完整 WAV P95 为 1.828 秒；并发争用与听感风险见下节。
- 比赛 Agent：1v1 与 4v4 多 Agent 内容正常生成，未观察到跨房间串题；4v4 AI 准备阶段通常为数秒到数十秒。
- Judge：1v1 与 4v4 均成功生成结构化胜负、团队分、个人分与理由；二次生产回归证明静音不再自动进入 Judge 输入。

## 固定语料语音质量、实时性与并发专项

### 五项评分与发布门槛

| 项目 | 分数 / 状态 | 主要证据 | 结论 |
|---|---|---|---|
| FunASR 内容质量 | 89.85 / 100；真人门槛 BLOCKED | 8 条 LightTTS 回转正样本 + 静音/低噪 2 条负样本：micro CER 4.27%、macro CER 4.40%、删除率 1.42%、插入率 0.28%、误触发 0、跨流污染 0 | 工程链路得分达标，但这不是人类麦克风 CER；不能单独通过真人 ASR 发布门槛 |
| LightTTS 内容完整性 | 65 / 100，FAIL | 4 声线、标准中文句均逐字一致；全语料 CER 中位数 5.41%、P95 23.37%，错误集中在“乾元/知微/景行/见山”等专名同音转写；3 条存在 >500ms 内部静音 | 标准句良好，但按固定评分规则仍低于 80；需人工听辨与拼音/专名感知复核 |
| LightTTS 自然度 MOS | BLOCKED | Computer Use 能验证播放状态但不能听觉评分；尚无两个场景、多人盲听 MOS | 未达到发布证据要求 |
| LightTTS 实时性 | 隔离 80 / 100；争用 40 / 100 | 隔离 synth P50/P95 6.947/8.609 秒，RTF P50/P95 0.696/0.743；1/5 字完整 WAV P95 1.828 秒；与 FunASR 基准竞争时 synth P95 21.527 秒 | 单路隔离达标，课堂并发容量不达标 |
| 浏览器播放稳定性 | 结果页 88 / 100；舞台标准状态机 PASS；活动舞台弱网 BLOCKED | 结果页已补 390×844、Fast 3G 1.926s、Slow 3G 5.787s、互斥、离页清理和 Range 206；Slow 3G pending A→B 最终音频约10.447s。Iteration 16 的舞台回归证明标准状态机隔离，但未覆盖真实缓冲恢复 | 结果页单条弱网播放可用，但旧 pending 下载仍拖慢切换；服务器从 WAV 就绪即计时，活动舞台缓冲后不追赶仍可能截断尾部，整体不能宣称 PASS |

五项中合成回转 ASR 工程分和隔离 TTS 实时性达到 80；结果页正常与标签页级 Fast/Slow 3G 单条播放、舞台权威播放状态机已有通过证据，但活动舞台弱网内容完整性仍 BLOCKED。TTS 内容完整性、人工 MOS、真实并发争用和活动舞台弱网仍未达到门槛。因此语音发布总门槛仍为 **FAIL / BLOCKED**，不能用于支持“成熟产品可发布”的结论。

### FunASR 固定语料结果

- 固定语料：开场教育语句、数字单位、专名术语、多音字、中英混合、头尾锚点、重复短语、长论证，以及静音和低噪负样本。
- 生产现状（CPU、3 秒增量触发）：首个 partial P50/最大值 3279/4105ms；STOP 后 final P50/最大值 672/1345ms。
- 主要错误：“五秒”删除、竺可桢转为同音“竹可珍”、`ChatGPT` 不稳定、长句中部少量删除；头锚点通过率 75%，尾锚点 87.5%。
- 详细证据：[FunASR 分析](audio/20260717-005403/funasr-benchmark-v1-analysis.md)、[生产基线 JSON](audio/20260717-005403/funasr-benchmark-v1-final-20260717.json)、`audio/20260717-005403/funasr-benchmark-v1-audio/`。

为降低首个 partial 延迟，在独立端口使用同一模型、同一 WAV、4 路并发和实时发送节奏比较了三个候选；生产端口在比较期间未改动。

| 候选 | micro CER | partial P50 / max | STOP final P50 / max | 负样本误触发 / 串流污染 | 决策 |
|---|---:|---:|---:|---:|---|
| 现网 CPU / 3000ms | 4.27% | 3279 / 4105ms | 672 / 1345ms | 0 / 0 | 保留基线 |
| CPU / 1000ms | 4.27% | 1676 / 2456ms | 2318 / 3611ms | 0 / 0 | 拒绝：最终确认严重退化 |
| CPU / 2000ms | 4.27% | 2527 / 3409ms | 766 / 2508ms | 0 / 0 | 拒绝：尾延迟扩大，收益有限 |
| GPU / 1000ms | 4.27% | 2848 / 4891ms | 459 / 656ms | 0 / 0 | 拒绝：partial 未改善且额外占用约 2356MiB GPU，会与 LightTTS 争用 |

候选证据保存在 `audio/20260717-005403/funasr-benchmark-candidate-*.json` 及对应日志。三种候选都没有同时改善 partial、final 和共享资源风险，因此未部署；现网 FunASR/LightTTS 与全站服务在专项结束后均保持 RUNNING，主站 HTTPS 返回 200。

### LightTTS 固定语料结果

- 固定 corpus 已定义 1、5、30、100、300 字、特殊符号和 3780 字极端文本；本轮现存结果实际执行的是 1、5、40、46 字四类样本，不能把未产生 WAV/JSON 的 full/extreme 条目计为已测。已执行输出均为 24kHz、16-bit、单声道 PCM WAV，约 384kbps，无削波。
- 隔离 8/8 成功；标准中文样本 4 声线转写均完全一致。专名样本的错误大多为同音异字，因此 raw CER 只能作为保守内容指标，不能替代人工听辨。
- 短文本 8/8 转写一致，完整 WAV 合成 P50 1.131 秒、P95 1.828 秒；短音频固定开销使 RTF >1，不适合用 RTF 单独评价。
- 隔离报告：[完整语料](audio/20260717-005403/lighttts-benchmark-isolated/lighttts-benchmark.md)、[短文本](audio/20260717-005403/lighttts-benchmark-short/lighttts-benchmark.md)。并发争用报告：[竞争场景](audio/20260717-005403/lighttts-benchmark-smoke/lighttts-benchmark.md)。

### LightTTS 容量根因与安全演进路径

- `LIGHTTTS_MAX_ACTIVE=1` 既写在 平台 配置上限，也写在生产 LightTTS 的 `running_max_req_size/decode_max_batch_size=1`。但 平台 的 `asyncio.Semaphore(1)` 只在单个 Python 进程/事件循环内生效；引擎、基准或维护进程可以各自持有一个“并发 1”，同时把请求压到 8080。
- LightTTS HTTP 服务可接收更多协程，等待内部唯一请求槽；这个等待没有面向产品的队列上限、room identity、queue deadline 或公平策略。这与两个独立基准竞争时 synth P95 从 8.609 秒恶化到 21.527 秒一致。
- 当前整条发言在应用层持槽跨所有 30 字分段，最长可到 128 chunks；每 chunk read timeout 可到 180 秒并重试，缺少 whole-job deadline，超长文本会产生队头阻塞。
- 健康检查只报告 TCP 可达和 configured max active；LightTTS `/metrics` 的成功/失败与 latency 样本不完整，不能用来做容量闸门。生产日志没有错误不等于没有排队。
- 当前传输为 `stream=false` 的完整 24kHz/16-bit/mono PCM WAV；Range/ETag/缓存正常，但浏览器必须等完整文件可用，约 384kbps 会在移动网络进一步放大首播等待。
- 容量估算：按隔离 P50 6.947 秒和目标利用率 65%，单槽安全持续速率约 5.6 jobs/min；按 P95 8.609 秒约 4.5 jobs/min。4 个任务同时到达时，尾任务串行等待约 25.8 秒、完成约 34.4 秒，无法满足多班课堂。
- 最安全第一步不是把 GPU 并发直接调到 2，而是建立主机级唯一 admission gate/dispatcher：全局 active=1、每房最多 1 个 pending/active、live pending 初始上限 2、排队超时 20 秒、queued cancel P95 <500ms、token/fencing 防双活，并暴露 queue depth、oldest wait、queue/E2E P95、reject/cancel/timeout 指标。
- 只有在隔离环境完成 1/2/4/8/16 burst、四声线、40/100/300 字、取消/超时/dispatcher 重启后，才评估第二实例或真正流式/连续批处理。不能通过加长 timeout 或直接提高 `running_max_req_size` 宣称容量通过。

## 数据采集审计与建议数据字典

当前单场数据已能关联 match/room/seat/speech/event/Judge/rating，且积分修正采用追加式记录；不足之处是课堂作用域、同意、批量导出、访问审计、保留删除和模型/语音血缘。建议以不可变内部 ID 关联，姓名只作展示字段，导出默认使用活动内稳定伪名。

| 实体 | 关键字段 | 用途 | 敏感级别 | 保留建议 | 质量规则 |
|---|---|---|---|---|---|
| Organization | id, name, status | 学校/机构租户边界 | 内部 | 账户生命周期 + 审计期 | 所有课堂资源必须有 org_id |
| Classroom | id, org_id, name, term, owner_teacher_id | 班级作用域 | 内部 | 学期结束后按政策归档 | teacher 必须具备 scoped membership |
| Membership | classroom_id, user_id, role, status, joined_at | 教师/学生/助教权限 | 敏感 | 账号期 + 审计期 | 唯一(classroom,user)，角色变更追加审计 |
| Activity | id, classroom_id, event_id, schedule, ruleset_snapshot | 一次课堂/赛事批次 | 内部 | 教研项目周期 | 赛制、题目、配置必须快照化 |
| ConsentRecord | user_id, version, purposes, guardian_ref, accepted_at, revoked_at | 录音/研究/公开展示同意 | 高敏感 | 法规/机构政策决定 | 未同意当前版本不得进入需录音活动 |
| Room/Match | id, code, activity_id, privacy, status, created_by | 单场比赛 | 内部 | 按课程/研究政策 | code 唯一；状态只按合法状态机迁移 |
| SeatAssignment | match_id, seat_key, user_id/agent_id, claimed_at | 身份、立场、角色 | 敏感 | 随比赛 | 每席唯一；重名不影响内部关联 |
| Speech | id, match_id, seat_key, source, text_raw, text_final, status, timing | 发言主记录 | 高敏感 | 原始/研究副本分层保留 | 不允许空文本伪装成功；保留修订历史 |
| TranscriptSegment | speech_id, seq, text, is_final, start/end, provider_version, confidence | ASR 过程与质量研究 | 高敏感 | 可比原始音频更短 | seq 单调；final 唯一；缺片/重片显式标记 |
| AudioAsset | id, speech_id, uri, codec, rate, channels, bytes, duration, sha256 | 回放、复核、研究 | 高敏感 | 默认最短必要周期 | checksum/时长/格式必填；访问必须审计 |
| AgentSnapshot | match_id, seat_key, provider/model/prompt/persona/voice versions | AI 可复现性 | 内部机密 | 至少随研究数据集 | 不保存密钥；比赛后配置变化不改历史 |
| MatchEvent | match_id, seq, type, actor, payload, created_at | 状态、异常和干预轨迹 | 内部 | 长期审计 | seq 唯一连续；跳过/重试/接替不可静默 |
| JudgeResult | match_id, raw, normalized, model_snapshot, reviewed_by, review_event | 胜负、评分、理由 | 敏感 | 随比赛/成绩政策 | 原始结果不可覆盖；复核追加前后值 |
| RatingLedger | user_id, match_id, delta, reason, supersedes_id | 积分可追溯 | 敏感 | 长期 | 追加式；总分可由 ledger 重建 |
| DatasetExport | id, scope, filters, pseudonym_salt_ref, manifest_uri, expires_at | 教师/研究批量导出 | 高敏感 | 短期下载，长期保留 manifest | 行数、字段、媒体 checksum 与查询条件可复现 |
| DataAccessAudit | actor, scope, purpose, filters, result_count, ip_hash, created_at | 敏感读取与下载审计 | 高敏感 | 至少 1–3 年/按政策 | 不可变；覆盖真人音频、归档和导出 |

每场比赛建议计算 `data_completeness_score`：必需阶段事件 30%、席位身份 10%、发言文本 20%、音频与 metadata 15%、Judge/复核 10%、积分 ledger 5%、配置快照 5%、异常/干预事件 5%。任一真人发言“有音频无文本”或“有文本无来源/状态”、事件序列重复、Judge 缺模型快照时，比赛必须进入 `review_required`，不得静默算作完整研究样本。

## 信息架构、Nielsen 可用性与视觉设计审计

- 信息架构当前围绕“赛事—房间—比赛”组织，适合单场体验，但没有“机构—班级—活动—批次—房间—复核—导出”教师层级。建议学生导航保留“赛事大厅/我的比赛”，教师新增独立“教学活动”，系统运维继续隔离到后台。
- 系统状态可见性：比赛阶段、计时和连接总体清楚；Agent/TTS 长等待、容量排队、Judge 评议进度仍缺 ETA 与阶段化反馈。
- 现实世界匹配：席位、立论、质询、自由辩论符合辩论心智模型；“Agent、ASR、Profile”等词应只出现在管理员或诊断模式。
- 用户控制与自由：单房间暂停/继续可用；学生缺试音、撤回错误席位、逐字稿纠错；教师缺批量暂停与异常恢复。
- 一致性与错误预防：禁用原因已改善；结果页媒体控件未互斥，连续点击可制造不可理解的重叠声音。
- 识别优于回忆：仍要求记六位房号和席位；应优先使用活动邀请链接/二维码并自动定位班级、比赛和可选席位。
- 错误恢复：登录、麦克风、静音 ASR 已有明确恢复；服务排队、音频失败、ASR 断线仍缺面向学生的重试/保留原音频路径。
- 帮助与文档：首屏缺“第一次参赛”引导、设备检查、隐私说明和故障自助，规模化课堂会把支持成本转移给教师。
- 视觉：学生端深色赛事风格统一，题目、阶段、计时层级较成熟；长评议和长逐字稿行宽/密度高，结果页需要章节导航、当前播放高亮和时间线联动。
- 后台：卡片、表格和表单基本一致，但导航以系统模块为中心，教师不应进入包含 Provider/Prompt/服务技术状态的系统后台。
- 包容性：AI/真人、正/反方除颜色外已有文字；Chrome 首页 200% 缩放与折叠菜单已通过，但 390×844、其余关键页 200%、44px 触控目标、完整键盘流、减少动态和长姓名仍缺验收证据。

## Top 10 产品改进机会

| 排名 | 机会 | 用户价值 | 风险降低 | 工作量 | 依赖 |
|---:|---|---|---|---|---|
| 1 | 学生自助赛事大厅与建房主路径（VERIFIED） | 无需教师即可选择正式赛/训练赛并创建 | 消除管理模块认知负担 | S | 赛事 API、建房弹窗 |
| 2 | LightTTS 公平有界队列、可见等待与容量闸门 | 多房间语音可预期 | 防止全局单路长队列和比赛死寂 | M/L | 指标、队列、容量环境 |
| 3 | 真人 ASR 固定语料、设备预检和流式识别 | 学生发言更完整、反馈更及时 | 降低 partial 慢、截断和误提交 | L | 流式 ASR、浏览器设备 UI |
| 4 | 第一次参赛引导与麦克风试音 | 新学生可独立完成首场比赛 | 降低开赛后才发现设备问题 | M | 引导、设备权限 |
| 5 | 房间邀请链接、二维码和复制反馈 | 同学更快加入同一房间 | 降低房号抄错和口头传递成本 | S | 分享组件 |
| 6 | 断线、刷新和跨设备接管的明确恢复 | 比赛中断后可自助恢复 | 降低重复发言和控制权冲突 | M | WebSocket、设备令牌 |
| 7 | 结果页逐字稿、唯一播放器与时间线联动 | 学生复盘更清楚 | 降低音频重叠和定位成本 | M | 结果页组件 |
| 8 | 学生提交前逐字稿纠错与原始记录保留 | 修正 ASR 错字而不丢失证据 | 防止错误文本进入裁判 | M | 修订历史 |
| 9 | 移动端、弱网与 200% 缩放质量门 | 手机和普通网络可用 | 降低页面遮挡、断线和音频卡顿 | M | 设备矩阵、网络实验室 |
| 10 | 系统管理员健康页与异常房间只读定位 | 运维可快速判断服务故障 | 缩短恢复时间且不增加学生复杂度 | M | 健康指标、审计 |

## 可直接开发的实施 Backlog

| ID | 优先级 | 工作量 | 模块 | 验收标准 | 回归用例 |
|---|---|---|---|---|---|
| BL-001（VERIFIED） | P2 | S | Web 结果页 | 任一音频播放时其余音频 300ms 内暂停；切页后全部停止；快速连点不重叠 | 真人→AI、AI→AI、三条快速切换、浏览器后退 |
| BL-002（REMOVED-SCOPE） | — | — | Organization/Classroom | 用户明确移除教学管理；不再作为发布门 | 确认导航/页面无入口 |
| BL-003（REMOVED-SCOPE） | — | — | Consent policy UI | 用户明确移除政策管理；不再作为发布门 | `/teacher/consents` 重定向首页 |
| BL-004（REMOVED-SCOPE） | — | — | Activity/roster | 用户明确采用学生自助建房；不再作为发布门 | `/teacher` 重定向首页 |
| BL-005（REMOVED-SCOPE） | — | — | 教师看板 | 用户明确移除教学编排 | 导航无教学入口 |
| BL-006（REMOVED-SCOPE） | — | — | ResearchExport UI | 用户明确移除研究运营入口；既有后端仅 dormant 兼容 | 导航/路由无 UI |
| BL-007 | P1 | M | LightTTS 调度 | 有界公平队列、每房最多一个活动任务、取消令牌、queue position/ETA；满载明确拒绝/延后 | 4/10/20 房、取消、超时、崩溃恢复 |
| BL-008 | P2 | L | FunASR/浏览器 | 首个 partial P95 ≤1.5s、STOP final P95 ≤3s、安静真人 CER P95 ≤15%，静音零实质文本 | 真人固定语料、噪声、断线、回声 |
| BL-009 | P2 | M | Speech/Review | 保留 raw/final/修订历史；学生提交前可改，教师复核可追加纠错，不覆盖原文 | 权限、并发修订、导出血缘 |
| BL-010 | P2 | M | QA/响应式 | 真实 390×844、200% 缩放、键盘与弱网矩阵进入 CI/发布清单 | 关键八页、触控、焦点、断网恢复 |
| BL-011 | P2 | M | 新手引导/设备 | 首次参赛前完成麦克风权限、输入音量、扬声器和示例发言检查 | 拒绝权限、静音、无设备、移动端 |
| BL-012 | P2 | S | 房间分享 | 一键复制链接并显示成功；二维码可扫描；错误房号可恢复 | 复制失败、6 位校验、过期房 |
| BL-013 | P2 | M | 断线恢复 | 刷新/掉线后 60 秒内回到正确阶段；旧设备只读且无重复录音 | AI 生成、TTS 播放、人类录音中断 |

## 迭代日志

### Iteration 1：核心链路与认证/路由修复

- 目标：完成 1v1/4v4、双用户、Judge、结果、权限和归档基线。
- 修改：结构化认证错误、匿名辩论路由、结果时间线、8 席个人分、房间精确 Nginx 路由。
- 验证：API/Web 全量测试、构建、备份与生产 Computer Use 回归；回滚点见部署章节。
- 结果：核心比赛能够完成；发现真人语音竞态、静音幻觉和课堂产品缺口。

### Iteration 2：真人语音可靠性

- 目标：关闭麦克风启动竞态、跨阶段占用、静音 ASR 幻觉和非 WAV 时长错误。
- 修改：可取消 turn token、旧 ASR/MediaRecorder 隔离、人工核对、VAD/低信息门控、真实音频 metadata。
- 验证：房间 586109/750374、API 171 项、Web 95 项、生产服务/日志健康。
- 结果：静音不再进入 Judge，麦克风按阶段释放；真人固定语料 CER 仍缺。

### Iteration 3：固定语料、并发与成熟产品审计

- 目标：量化 FunASR/LightTTS、验证优化候选、审计多班级和研究治理。
- 修改：新增可重复语音基准与测试；未部署三个会退化的 FunASR 候选。
- 验证：ASR micro CER 4.27%、零负样本误触发/串流污染；TTS 隔离 RTF P95 0.743，争用 synth P95 21.527 秒。
- 新发现：5 个课堂规模化 P1、结果页多音频重叠 P2；当时全局产品质量 63/100。
- 下一轮：关闭音频互斥，确定 Classroom/Activity 和 LightTTS 调度的第一实现切片，并把已有证据扩展为 ≥80 个明确用例。

### Iteration 4：结果页音频互斥闭环与课堂/容量第一切片

- 时间：2026-07-17 04:51–04:59 CST。
- 本轮目标：关闭稳定复现的结果页音频重叠，并把教师课堂和 LightTTS 容量 P1 拆成可实现的纵向切片。
- 修改文件：`apps/web/app/rooms/[code]/result/page.tsx`、`page.test.tsx`。
- 本地验证：结果页 `12/12`、Web 全量 `97/97`、TypeScript、Next.js production build 通过。
- 部署与备份：release `20260716T205546Z-qa-audio-exclusive`；源码备份及 previous web 记录见部署章节；12343 shadow 通过后原子切换。
- 生产回归：连续两条音频只保留最新播放；离页停止；返回无旧音频；主站/结果/API health 200，Web stderr 空。
- 页面评分变化：结果页可靠性 3→4；浏览器播放稳定性 75→85；全局产品质量 63→64。
- 追加发现与闭环：SunBrowser 批量 `preload="metadata"` 触发 9–11 条媒体不可用；改为 `preload="none"` + 服务端时长后，第 1/6/11 条实际播放通过，浏览器播放稳定性最终为 88。
- 方案进展：教师第一切片确定为“已有账号名单→单班活动→分组预览→幂等批量私密房→教师总览/控制→学生 `/me` 一键进入”；LightTTS 第一切片确定为跨进程唯一门禁、有界队列、deadline 与指标，保持 LightTTS 并发 1，不直接调高 GPU 服务并发。
- 未解决风险：课堂/隐私/导出/容量 P1、真人 CER、人工 MOS、TTS 内容完整性；83 个用例的数量门已达到，但部分仍是原子表格格式且移动/弱网缺证。
- 下一轮计划：审查并灰度 Classroom API 第一纵切或 LightTTS gate 的 whole-job deadline/指标；补真人语音和移动弱网证据。

### Iteration 5：课堂权限与 TTS 全局门禁本地基础

- 本轮目标：开始削减课堂作用域和语音容量 P1，而非只保留方案文档。
- Classroom 本地实现：新增 Organization、OrganizationMembership、Classroom、ClassroomMembership；教师只存在于 scoped membership，不修改 `User.role`；新增统一 403 权限 helper 和显式 Alembic `0013_classroom_foundation`。
- Classroom 验证：专项 5 passed；真实 Alembic head→0012→0013 验证表/约束/head；跨组织、停用 membership、system_admin 旁路通过。
- TTS 本地实现：新增默认关闭的 Redis 全局 FIFO admission gate，跨进程 active=1、有界 pending、queue deadline、queued cancellation、token 比较释放与续租；生产 Redis 故障 fail closed。
- TTS 验证：多 gate 并发、队列满、deadline、取消、租约续期/stale token、默认关闭与 Provider 统一入口均覆盖。
- 统一本地质量门：Ruff 通过；`PYTHONPATH=.:apps/api .venv/bin/pytest -q apps/api/tests` 为 `188 passed`。
- 部署决策：两项基础均未部署。Classroom 尚无 Activity/API/UI/Consent；TTS gate 尚缺 whole-job deadline、产品队列状态与真实 Redis/LightTTS shadow 压测。提前启用不能宣称 P1 关闭。
- QA：从现有截图/音频/JSON 拆分为 36 个长格式 + 47 个原子记录，共 83 个明确 TC；同时纠正 LightTTS 实际只跑 1/5/40/46 字，不把仅定义的 30/100/300/3780 字算作已测。

### Iteration 6：Activity API 与 TTS 门禁可启用性（本地闭环完成，未部署）

- 时间：2026-07-17 05:27 CST 起。
- 本轮目标：把 Classroom schema 推进到 Activity/名单/分组/幂等批量建房/dashboard API；为 TTS gate 增加 whole-job deadline、低基数状态和真实 Redis shadow。
- 生产基线：Web release `20260716T211321Z-qa-audio-lazy`；API/Web/Engine/Worker/FunASR/LightTTS RUNNING；主页/API health 200；API/Web stderr 0；生产 Alembic 仍为 `0012_agent_gateway_secret`。
- Activity 本地进展：新增活动、名单 preview/commit、确定性分组、乐观 revision、幂等批量私密建房和教师 dashboard；120 人/30 房由 8 个并发请求验证只创建 30 房，学生所有权、预分配席位和 `disconnected_at=NULL` 正确；跨组织统一 403、旧房间不变、冲突时全事务回滚。审查后补充同组席位显式去重、参与者 active classroom/org membership 复核及 `(activity_id, activity_group_no)` 唯一约束。
- Activity 验证：第一轮专项 `12 passed`；真实 Alembic `0013→0014` 验证新表、房间新列/唯一约束、旧房间保持和 head。独立审查后关闭两个候选部署 P1：Activity 创建教师在仍具有 active classroom teacher + active organization membership 时，可访问/控制自己批量创建的 private 房间、结果、媒体、普通房间 WebSocket 与归档；建房后名单冻结，新幂等键返回 409，既有成功键仍可 replay，provision/roster 并发只产生两种可恢复一致结果。最终教师专项 `15 passed`。
- TTS 本地进展：whole-job deadline 已覆盖 Redis 排队、本地 semaphore、分段、fallback 和重试；超时/租约丢失不发布目标文件并清理 `.part`；健康检查暴露 global active、queue depth、oldest wait 和低基数事件。外部 task cancel 现在只取消调用方，内部推理继续持有 semaphore/Redis lease 排空后丢弃；关停超过 10 秒则 abandon lease、不主动 `DEL`，由 330 秒 TTL 防止新旧 GPU 推理重叠。Redis `ACQUIRE` 提交后响应丢失时，用新连接按同 token 原子恢复并续租；未提交或恢复不可判定时 fail closed。
- 真实 Redis 第二轮 shadow：未调用 LightTTS HTTP，3 个独立进程 `maximum_active=1`，queued cancel、whole-job deadline、lease lost、生产 Redis 故障关闭和 post-commit response-loss 恢复均通过，释放后无 ghost active lock，临时键清零；最终证据：[AUDIO-TTS-REDIS-GATE-P1-FINAL.json](audio/20260717-005403/AUDIO-TTS-REDIS-GATE-P1-FINAL.json)。TTS 专项 `47 passed`。
- 最终串行质量门：相关专项合并 `56 passed`；完整 API `207 passed`（6 个既有 Alembic `path_separator` deprecation warning）；Ruff、py_compile 通过。主线程早先与代理同时启动 pytest 导致共享测试数据库竞争，已作为测试基础设施竞争排除，不计入产品缺陷。
- 独立只读审查结论：4 个候选部署 P1 均已修复并回归；P0 为 0。保留限制：`max_pending=2` 只是过载保护而非 30 房容量方案；事件 counter 是进程内数据，只有 Redis global gauges 可视为跨进程事实；同班非创建教师尚不能协作管理；极端强杀可能使 TTS 安全 fail-closed 最长 330 秒。
- 生产复核：API/Web/Engine/Worker RUNNING，主页和 `/api/health` 为 200，Web 仍指向 `20260716T211321Z-qa-audio-lazy`，Alembic 仍为 `0012_agent_gateway_secret`，Supervisor API/Web stderr tail 为空。
- 部署决策：两个候选均仅在本地实现；未迁移生产、未启用 TTS gate、未对真实用户开放教师能力。Activity 后端虽关闭本轮候选阻塞，但仍缺 UI、Consent、协作教师、名单变更恢复和研究导出；TTS 已具备低峰单实例 canary 的技术前置条件，但尚未与真实 GPU 调用共同启用观察，且队列容量仍不支持 30 房突发，因此本轮不部署、不宣称课堂容量 P1 关闭。

### Iteration 7：LightTTS 全局门禁低峰生产 canary

- 时间：2026-07-17 06:01–06:16 CST。
- 本轮目标：在不迁移 0013/0014、不开放教师 API、没有真实进行中比赛的前提下，单独部署 TTS gate，并用真实 LightTTS 与 Computer Use 验证。
- 新发现：部署配置 updater 使用原子临时文件替换 `.env` 时没有保留属主，导致 `ubuntu` 服务用户一次读取失败；备份显示原属主为 `ubuntu:ubuntu`。
- 选择修复的问题：只发布 TTS gate 文件；`main.py` 从生产版本仅合入 shutdown drain；启用前后各做双任务实际 canary；配置更新后恢复并校验 `.env` 的 `ubuntu:ubuntu 0600`。
- 修改/部署文件：`app/core/config.py`、`app/services/lighttts_admission.py`、`app/services/providers.py`、`app/services/system_health.py`、`app/main.py`、`deploy/verify_lighttts_redis_gate.py`、生产 `.env` 的 6 个非敏感门禁参数。没有部署 Activity/Teacher/migration 文件。
- 本地验证：完整 API `207 passed`；相关专项 `56 passed`；Ruff、py_compile 通过。
- 部署与备份：回滚包 `/home/ubuntu/sunsq/phdebate/runtime/deploy-backups/20260716T220527Z-before-lighttts-gate-canary.tar.gz`，SHA-256 `b92e2e818e00da95ab2a2ceb2652892f0f54de244618f44d301ff0992c9e917b`；发布 tar SHA-256 `298d6b343e8075366ce9c46eb172bc050d36c0606253f33e38416b094f915250`。
- 生产回归：启用前 shadow 和两次真实双任务 canary 均通过；启用后 2 个任务 4.233 秒，active 最大 1、queue 最大 1、两个 WAV 有效，最终 active/queue 0；API/Engine/Worker/Web RUNNING，health/ready 200。10 分钟后复查仍无 processing room、active/queue/oldest wait 均为 0；Engine stderr 为 0，API stderr 最后修改时间停留在属主错误发生的 22:10:03 UTC，修复后没有新增 stderr。首次 API spawn error 已由属主修复，未影响任何活动比赛。
- Computer Use：Chrome 主页与结果页刷新正常；第一条音频有进度，切换第二条后第一条恢复 play，只有第二条 pause；最后手动停止。证据 TC-919/920。
- 页面评分变化：页面评分不变；TTS 调度可靠性提升，但由于 30 房突发仍会大量明确拒绝、无 queue position/ETA，课堂并发实时性评分仍维持 40，不关闭容量 P1。
- 未解决风险：单实例 canary 不能替代 4/10/20 房隔离压测；进程内事件 counter 不是跨进程累计；极端强杀可安全 fail-closed 最长 330 秒；用户端尚无排队位置、ETA 或延后说明。
- 下一轮计划：建立隔离 GPU 容量环境并做 4/10/20 房；并行推进 Consent/default-private 与教师 `/teacher` UI，但生产仍不得迁移本地 0013/0014，直至隐私门和 UI 完整。

### Iteration 8：Consent/Privacy 与教师活动 UI（本地完成，部署阻塞）

- 时间：2026-07-17 06:17–07:17 CST。
- 本轮目标：并行补齐录音同意的真实业务门和教师 Activity 的可用界面；只在本地实现，不迁移生产 0013/0014/0015。
- Consent 完成项：组织级版本化 `recording/research_use/public_display` policy、政策正文和生效时间、append-only grant/revoke、复合外键与 RESTRICT 审计保全；录音门接入 Activity claim-seat、ready、room start、每次新 speech/start；撤回不破坏正在完成/上传的既有发言，只阻止下一次新录音。Activity 完整归档作为 research export，要求全员当前 `research_use` grant；system_admin break-glass 明确审计。旧 `activity_id=NULL` 房兼容。
- Teacher/Student UI 完成项：新增 scoped `/teacher`、课堂发现、Activity/名单/确定性分组/批量私密建房/dashboard、逐房控制权限、409 恢复、冻结 revision 二次确认、异步旧响应丢弃、空状态和移动布局；学生 recording consent 控件展示政策全文，默认未勾选，支持 grant/revoke、换版刷新、focus trap/Escape，并接入 lobby/debate。
- 锁序与并发：统一 `activity → existing rooms → policy → sorted subjects/users`；仅 provision 使用 30 秒事务锁等待，其他端点保持 5 秒。最终根因不是首事务慢，而是 async fast replay 持同步 SQLite 锁直接 return，下一请求阻塞同一 event loop、导致前一请求 dependency cleanup 无法释放；现先构造纯值响应并显式 `db.rollback()` 释放锁再返回。
- 规模验证：120 学生、8 个并发 provision 请求全部 200，只创建 30 个 private 房、120 个真人席位，独占用例 `1 passed in 0.52s`；Consent 并发/锁序 `11 passed in 2.95s`；Teacher+Consent API `21/21 passed`。
- Web 质量门：全量 `21 files / 118 tests passed`；TypeScript、Ruff、Next.js production build 通过。Consent RTL/axe 覆盖默认未勾选、换版、grant/revoke、409 恢复、Escape/focus；Teacher RTL 覆盖旧异步响应丢弃、切换清空、冻结 revision、无 policy 禁止建房。
- 数据库质量门：真实 `0014→0015` 与 ConsentRecord 复合 FK/RESTRICT 约束 `2 passed`；Alembic 单一 head 为 `0015_consent_privacy_mvp`；Ruff 与 py_compile 通过。
- 完整 API 结论：第二轮 `218 passed, 1 failed, 9 warnings in 44.29s`，不能标记全绿。唯一失败是 24 房混合模拟看到前序测试遗留的一个 Engine background cue；单独复跑通过，但完整套件连续两次在同一断言失败，记录为 DEF-012 / GAP-ENGINE-001。9 条 warning 均为既有 Alembic `path_separator` deprecation。
- 独立只读审查：部署阻塞 P1 为退班/停用学生仍可凭历史 RoomSeat 访问私密 room/result/media/archive。保留 P2：录制时 policy_id/version/record_id 未快照进 Match/Speech/Archive；`effective_at` 未强制 timezone-aware；dirty activity `visibility=public` 虽被权限层强制私密，序列化/归档仍可能误导消费者。
- 生产复核：07:12 CST，`/api/health/ready` 200，schema current/expected 均为 `0012_agent_gateway_secret`；Engine heartbeat 4.15 秒，Worker 1 个、queue/dead letter 为 0。LightTTS admission `enabled/ok/fail_closed=true`、active/queue/oldest wait 为 0；API/Engine/Worker/Web/LightTTS/FunASR 全部 RUNNING。Computer Use 的 Chrome 结果页仍正常，焦点为“播放”，未观察到音频复活或页面异常；证据：[最终复核截图](screenshots/20260717-005403/TC-920-LightTTS门禁启用后-最终复核.jpg)，SHA-256 `779b271bd9c491d6e893c0d198efbbb99f455a20de8807341e5450d3d7d6459f`。
- 部署决策：本地 0013/0014/0015、Teacher/Consent Web 均不部署。必须先关闭退班历史读取 P1、补政策发布运营入口与 QA 机构/账号 provisioning，再做生产 Computer Use；完整 API 的 Engine task 隔离 P2 也需单独关闭。目标产品结论保持“不可发布”。

### Iteration 9：历史访问、Consent provenance 与 Engine task lifecycle

- 时间：2026-07-17 07:18–07:40 CST。
- 本轮目标：关闭 Iteration 8 的退班历史读取 P1、Consent 时间/可见性/血缘 P2，以及完整 API 唯一失败 DEF-012；只部署可独立于 0013–0016 的 Engine 加固。
- 历史访问 P1：新增 activity 当前授权判定，历史 RoomSeat 不再是永久授权。必须同时存在匹配 activity/group/seat 的 ActivityParticipant，并保持 User、Classroom、Organization、ClassroomMembership、OrganizationMembership 全部 active；同班 active teacher/system_admin 保留，legacy `activity_id=NULL` 保持旧语义。
- 入口覆盖：room/result/public、match history/result/archive、media、`/me`、普通房间 WS 与 ASR 初连/持续连接均复用统一权限；成员撤销后返回 403/4403。停用 tenant 不再被误判为 legacy；system_admin research export 仍走 audited break-glass。
- Consent hardening：`effective_at` 拒绝 naive datetime 并统一 UTC；activity 房即使数据库脏写 `visibility=public`，API 和 archive 均输出 effective `private`。新增 `0016_consent_provenance`，在 Match 快照全体参与者当前录音政策/record，在每段 human Speech 快照该发言者 policy_id/version/content/effective_at/record_id/decided_at/actor，并进入 archive v2；旧 Match/Speech 升级后默认 `{}`，降级保留旧行。
- DEF-012 根因与修复：额外 background cue 来自更早 Consent 用例留下的真实 preparing 房，而非 20 房任务泄漏。测试改用真实 terminate API 收尾；Engine `stop()` 统一 cancel+drain，新增 owning-loop drain，禁止 loop 变化时静默丢弃活跃 room task registry。
- 本地验证：Consent `14 passed`；0015→0016 实迁移 `1 passed`；archive 邻接 `1 passed`；multi-room `19 passed`；完整 API `225 passed, 13 warnings in 49.32s`；Web `118/118`；TypeScript、Next production build、Ruff、py_compile 全部通过。13 条 warning 均为既有 Alembic `path_separator` deprecation。
- 测试基础设施说明：一次从仓库根目录直接跑 pytest 因未显式加入 `scripts`/`apps/api` 导入路径，在 collection 阶段报 `ModuleNotFoundError`；权威命令为 `PYTHONPATH=.:apps/api .venv/bin/pytest -q apps/api/tests`。代理曾同时使用共享 `pytest-v2.db` 产生 disk I/O/503，均作无效运行排除，最终结果来自主线程独占串行质量门。
- Engine 生产部署：部署前确认无 `preparing/running/judging` 房。回滚文件 `/home/ubuntu/sunsq/phdebate/runtime/deploy-backups/20260716T233719Z-before-engine-task-drain-match_engine.py`，SHA-256 `6099cd2b9a6dde25ccc0c52cabb799402fb5b86c35fad5239a167db801fb3c7d`；部署文件 SHA-256 `35cb3cf9d1199a34305d7f10d94374be192d62227e7fac2699d4c8e64f4402d5`。仅重启 `jixia-engine`，API/Web/Worker 未动。
- 生产回归：Engine/API/Web/Worker RUNNING；ready 200，Engine heartbeat 3.55 秒，Worker 1 个、queue/dead letter 为 0；Engine stderr 空。LightTTS admission enabled/ok/fail-closed，active/queue/oldest wait 为 0。TC-921/922 证明结果页和首页正常，房 278571 状态未被改变。
- 部署决策：Engine task lifecycle 已生产 VERIFIED。0013–0016、Teacher/Consent Web 仍不部署，因为尚缺组织管理员政策发布/换版运营入口、QA 机构和真实教师/学生 provisioning；未来若物理删除 Activity，必须软删除或保留不可变课堂 scope，不能依赖当前 `rooms.activity_id ON DELETE SET NULL`。
- 当前结论：退班历史读取、Consent 时区/effective visibility/provenance 与 DEF-012 均已在本地关闭，Engine 加固已生产闭环；目标产品仍受课堂功能未安全上线、研究批量导出、真实语音容量和真人语音/MOS 证据阻塞。

### Iteration 10：课堂/Consent 生产灰度、数据保全与动态 presence 竞态

- 时间：2026-07-17 07:41–08:32 CST。
- 本轮目标：完成组织政策运营入口、QA 租户 provisioning、Consent 录制血缘、Activity scope 保全和动态 presence 竞态修复；通过 PostgreSQL shadow、完整质量门、备份与生产 Computer Use 灰度验证。
- 新发现：PostgreSQL 的 `alembic_version.version_num` 为 32 字符，原 0016 revision ID 过长；shadow ready 使用相对备份状态路径时会误报 503；完整 API 偶发暴露“先构造 projection、后同步 presence”导致旧 WebSocket 连接认领席位后 `connected=false`。
- 选择修复的问题：缩短 0016 revision ID 并增加所有 revision 长度门；新增 0017，把 Activity 历史房间外键从 `ON DELETE SET NULL` 改为 `RESTRICT`；sender 在同步 presence 后重新加载 projection；新增 owner/admin 的政策查询与明确换版确认 UI/API；新增只引用既有用户、默认 dry-run、幂等且全事务的 QA provisioning。
- 数据/隐私变化：Match 与 human Speech 保存录制时 `recording_consent_snapshot`，archive 升级为 v2；政策 `effective_at` 强制 timezone-aware 并归一 UTC；Activity 房即使脏数据写为 public 也对 API/归档序列化为 effective private；历史访问继续要求 active user/tenant/classroom/org membership。
- 本地验证：完整 API `238 passed, 17 warnings`；Web `22 files / 123 tests passed`；TypeScript、Next production build、Ruff、py_compile 通过。Presence 竞态定向重复 `10/10`；QA provisioning `7 passed`；0015→0016 与 0016→0017 PostgreSQL/约束迁移测试通过。17 条 warning 均为 Alembic `path_separator` deprecation。
- 部署与备份：数据库 dump、源码/Web 回滚包和发布包及 SHA 见“已部署版本与回滚点”；生产从 0012 顺序迁移到 0017，新 Web release `20260717T0816Z-qa-classroom-consent`，自动回滚 trap 未触发。
- 生产回归：ready/schema、四主服务、LightTTS/FunASR、备份状态与三个 Web 路由均通过；无 processing room。QA provisioning 完成 dry-run→apply→同请求 replay，组织/课堂及两类 membership 数量与审计均精确；Computer Use 的 owner_teacher/student 双角色、条件导航、scoped 组织/课堂、权限拒绝和政策弹窗安全门均通过，见 TC-923–930。
- 页面评分变化：Teacher/Consent 的 scoped 权限、状态可见性和键盘安全门达到生产灰度标准；仍因缺真实 policy grant/revoke、活动/名单/建房全链和移动端而不提升目标产品发布结论。
- 未解决风险：教师批量研究导出、真实课堂 4/10/20 房语音容量、真人 CER/MOS、精确 390×844 和 System Admin 实登仍未关闭；政策生效/撤回的真实多用户浏览器链路还需继续扩展。
- 下一轮计划：仅在 QA 组织发布明确测试政策并由学生本人完成 grant/revoke，继续活动→名单→分组→私密建房生产 E2E；随后推进批量导出和隔离语音容量专项。

### Iteration 11：建房前学生 Consent 闭环、研究批量导出与语音容量

- 时间：2026-07-17 08:33–09:08 CST。
- 本轮目标：完成 QA 课堂真实 activity 全链；并行推进研究批量导出 MVP 和 4/10/20 房 LightTTS 隔离容量证据。
- 新发现：`rooms:provision` 正确要求名单学生在建房前具有当前 recording grant，但现有学生 Consent 控件只存在于房间 lobby/debate；`/me` 没有课堂或待处理同意入口，导致“未同意不能建房、未建房无处同意”的 P1 循环（DEF-013 / TC-931）。
- 生产基线：ready 200，起始 schema 0017；LightTTS global gate active=0/queue=0；QA org/class active，2 条 org membership 与 2 条 classroom membership；policy/activity/activity-room 均为 0。未替学生写入 consent，未创建房间。
- 选择修复的问题：新增 self-only active classroom consent discovery，并在 `/me` 提供建房前全文阅读、默认未勾选、grant/revoke、换版刷新与停用 membership 即时撤出；复用现有键盘/焦点安全门。
- 本地验证：完整 API `249 passed, 20 warnings`；Web `127/127`；Ruff、py_compile、TypeScript、Next build 通过。Consent discovery/政策/隐私 `22 passed`，相关 Web `19 passed`；研究导出专项与邻近 `41 passed`；LightTTS admission `12 passed`。
- 研究导出：0018 scoped async job、stable pseudonym、manifest/JSONL/CSV/media checksum、幂等/审计/下载时 scope+consent 重验、artifact traversal/symlink/tamper/stale-running 防护已生产部署；尚缺用户 UI 和首个真实 QA artifact E2E。
- 语音容量：可控 mock endpoint + 真实 平台 provider/admission/Redis 的 4/10/20 FIFO 机制全通过；按生产 pending=2，同步突发拒绝率为 50%/80%/90%，未启动真实 GPU、未修改生产 gate。证据见容量汇总。
- shadow/部署：三次 shadow 中前两次暴露恢复对象 owner 与 stderr 判定 harness 问题，第三次完整通过；数据库备份、源码回滚和 package SHA 见部署章节。生产已迁移 0018 并切换 Web `20260717T0900-research-consent`，服务/ready/LightTTS 均健康。
- Computer Use：TC-932 验证 User B `/me` 只显示本人 QA 课堂，无 policy 时“等待政策”，入口循环已关闭。User A 的 QA recording policy 正文已在浏览器草稿中完成核对，尚未点击最终发布。
- 未解决风险：政策发布与学生 grant/revoke 都是不可忽略的真实权限变更，生产 Computer Use 将在最终点击前按规则请求 action-time 确认；Activity/房间仅使用 QA 标记并保留审计，不删除真实数据。
- 下一步：获得 action-time 确认后，User A 发布不可覆盖的 QA recording v1；User B 在 `/me` 自主 grant；随后用 Teacher UI 完成 Activity→roster→deterministic group→private room，并验证 revoke 阻止新的录音。之后补 ResearchExport UI/真实 artifact 与独立 GPU 容量。

### Iteration 12：管理模块扩展停止与学生自助赛事范围收缩

- 时间：2026-07-17 09:09–10:23 CST。
- 本轮目标：最初并行补 ResearchExport/政策影响/教师容量；用户随后明确认为这些属于过度设计，本轮转为完整撤销尚未部署的管理扩展，移除用户可见的政策、教学、研究和容量编排入口，聚焦学生自行创建赛事。
- 安全门：政策版本发布与 User B grant/revoke 都属于真实权限/隐私状态变化，必须分别获得 action-time 明确确认。09:24 CST 重新读取当前 Chrome 可访问性树时，弹窗仍在但此前 QA 草稿已因页面刷新/重新渲染回到默认空白正文、默认标题、未勾选状态，“确认发布新版本”禁用；未点击提交，未产生 policy、Consent、Activity 或房间数据。后续如获确认需重新填写并再次核对完整正文。证据：[TC-937](screenshots/20260717-005403/TC-937-政策发布弹窗-未提交且草稿已重置.jpg)。
- 独立移动端/产品审计新发现：政策换版弹窗缺当前 grant、受影响课堂/活动/学生和版本失效范围预览（P1）；研究导出必须明确稳定伪名可跨导出关联、自由文本可能自我披露、包中仍含 audio reference/媒体校验和、下载到本地的副本无法由平台撤回，并为 system_admin break-glass 使用独立高风险确认（P1）；`/me` 多课堂 Consent 信息优先级、教师创建页、活动名单同意计数语义、390×844 触控目标、批量建房弹窗和移动导航焦点闭环仍有 P2。
- 并行回退结果：ResearchExport UI/discovery/风险确认契约、教师容量 API/UI/文档、政策 impact preview/offline evidence 草案均已撤销，未部署；0018 表、既有 dormant ResearchExport 后端、teacher/activity 与 Consent 历史数据暂留作兼容和回滚，不做破坏性删库。GlobalNav 已移除教学活动、政策管理、研究导出及其权限探测；`/me` 已移除课堂 Consent；`/teacher` 与 `/teacher/consents` 均已重定向首页。
- 中间产品方向（09:26 CST，已被 09:28 CST 范围收缩取代）：组织者确认学生已完成线下签署，因此停止生产在线 grant 链。此时曾短暂评估“线下签署证明”模型；随后用户明确要求直接去除同意政策、教学和研究等管理模块，该模型未实现、未部署，也不再列入当前路线。TC-938 仅保留为范围调整前的失败证据：[截图](screenshots/20260717-005403/TC-938-教师活动-仅支持线上政策导致线下签署仍显示未就绪.jpg)。
- 产品范围再次收缩（用户 09:28 CST 明确指令）：去除同意政策管理、教学管理、ResearchExport/课堂编排/容量编排等管理型产品模块，不再扩展线上或线下 Consent evidence；学生可自行创建赛事房间。09:46 CST 用户进一步澄清：训练赛继续保留，`4v4 人机辩论正式赛` 只是赛事目录中的一个重点赛事，不是唯一赛制。因此首页保留正式赛与训练赛卡片，突出正式赛快速创建，但不隐藏 1v1 训练。已生产的 0013–0018 表和历史数据暂不做破坏性删除，底层身份认证、房间权限、录音安全和数据完整性校验仍保留；前端主流程不再依赖课堂/政策/研究模块。
- 本地质量门：完整 API `248 passed, 22 warnings in 230.31s`；完整 Web `22 files / 115 tests passed`；TypeScript、Ruff、py_compile、Next.js production build 全部通过。首次同时跑 5 个重型 Web 文件出现 7 个 5 秒 timeout 与 1 个重复标题断言；修正唯一真实断言后，主线程单 worker 完整 Web 全绿，超时均作为测试资源竞争排除。
- 生产发布：Web-only release `20260717T1000-student-self-service`；发布包 `/tmp/phdebate-20260717T1000-student-self-service-web.tar.gz`，SHA-256 `b8691e60d7e806043fa3b13f87a4d38d6f66be0732afeef4393e83f36b52163b`。源码回滚包 `/home/ubuntu/sunsq/phdebate/runtime/deploy-backups/20260717T1000-before-student-self-service-web-source.tar.gz`，SHA-256 `455e662f55e659a8b26585d5ea1c72f2c40858d4c508c0bbfa95ecdc40078fb1`；前一 release 指针记录在 `/home/ubuntu/sunsq/phdebate/runtime/deploy-backups/20260717T1000-before-student-self-service-web.txt`。Linux 原生 build、12349 shadow 首页/teacher redirects/rankings、原子 symlink 切换和 Web restart 均通过，自动回滚未触发；没有 API、数据库、Nginx 或配置变化。
- 标签一致性补丁：旧进行中房间 API 仍返回历史名称“4v4 人机辩论日常赛”，首页直播行与新正式赛名称不一致；新增只读显示映射并发布 Web release `20260717T1021-student-self-service-labels`。补丁包 `/tmp/phdebate-20260717T1021-student-self-service-labels-web.tar.gz`，SHA-256 `d3d6ecc79f8df77704248252159954e797a56c7a3c3b60a44f2757ee5736934f`；源码回滚包 `/home/ubuntu/sunsq/phdebate/runtime/deploy-backups/20260717T1021-before-student-self-service-labels-source.tar.gz`，SHA-256 `be42dccb15b614b8fb6eca659748a6f1e220c19a0969e50e801eebf8472310db`。定向 Web 3 passed、tsc/build 与 remote shadow/健康均通过。
- 生产 Computer Use：早期在 SunBrowser User B 验证管理导航、正式赛命名、首页两类赛事、4v4 创建窗口、`/me` 简化和 `/teacher*` 重定向（TC-939–944）；用户随后明确要求不再使用 AdsPower/SunBrowser，本轮立即停止使用。Safari 当前仅有锁定的无痕窗口，需要系统用户密码，未尝试解锁；最终权威浏览器回归改用 Chrome 既有稷下测试标签，验证 1v1 训练赛保留、管理入口移除、首页正式赛/训练赛与直播名称一致（TC-945/946）。未点击最终“创建比赛”，未新增房间，也未操作 Chrome 账号正在参与的真实房 278571。发布切换后的 SunBrowser 排行榜和首页第一次数据加载各出现一次 `Failed to fetch`，重试即恢复；Nginx/Web/API 无对应新 5xx 或 stderr，Chrome 最终回归一次加载成功。
- 容量证据边界：此前 4/10/20 结果只证明可控 mock LightTTS endpoint + 真实 平台 provider/admission/Redis 的 FIFO/隔离机制，不代表真实 LightTTS GPU 吞吐；生产配置 `max_active=1/max_pending=2` 的同步突发拒绝率仍为 50%/80%/90%，不得用 pending 数或猜测 ETA 向教师承诺容量。
- 下一步：继续聚焦学生核心比赛链，优先补真人 ASR/MOS、真实 LightTTS 容量、390×844/弱网和首次设备检查；管理模块不再进入当前发布范围。

### Iteration 13：LightTTS 接纳语义与学生端弱网发言加固

- 时间：2026-07-17 10:36–11:14 CST。
- 本轮目标：在不提高 LightTTS GPU 并发、不扩大 pending/timeout、不触碰真实房 278571 的前提下，减少同步突发的无谓拒绝；同时补首页 200% 缩放证据并加固学生弱网开始发言路径。
- 并行审计：LightTTS 现有 Redis gate 已覆盖 FIFO、whole-job deadline、取消、租约续期/丢失和 response-loss 恢复，但 `max_pending=2` 在 active 尚未取得时把首任务也计入 pending，导致空闲同步突发只能接纳 2 个；后台 cue 任务在取得本地 background semaphore 前先进入全局队列，可能挤占真人发言。前端已有麦克风权限/无设备/占用/12 秒设备启动错误，但 `/speech/start` 无网络超时，`connected=false` 仍可能基于陈旧快照打开麦克风。
- 源码对账：现网 `lighttts_admission.py` 比本地多 queue position/ETA estimator，production health 已返回 `next_queue_position=1 / ETA=null`。本轮先把该现网能力同步回本地，再叠加接纳语义修复，避免 API 部署覆盖现有能力；`providers.py` 本地与现网 SHA 原本一致。
- 当前实现：`ENQUEUE_SCRIPT` 增加 active key；active 空闲时队列临时允许 `max_pending+1`，取得租约后稳定为 `1 active + max_pending waiting`，最大 active 仍为 1。后台任务先取得进程内 background semaphore，再进入 Redis admission，避免同一 API 进程用多个 cue 填满全局 pending。
- 代码审查闭环：首次复审判 NO-GO，发现 background semaphore 前移后本地等待未受 whole-job deadline 约束，且 admission 前取消漏计指标。修复为 deadline-aware 0.1 秒轮询，并用 `global_admission_attempted` 区分本地取消与 Redis queued cancel；新增 background deadline/cancel 和 public acquire barrier/FIFO/cleanup 测试。二次只读复审判 GO，未发现双记、双活或 semaphore 泄漏。
- 本地质量门：LightTTS admission/provider 定向 `39 passed`；完整 API `253 passed, 22 warnings in 81.68s`；DebateStage 定向 `32 passed`，完整 Web `22 files / 118 tests passed`；TypeScript、Ruff、py_compile、macOS 与远端 Linux Next production build 全部通过。
- 真实 Redis shadow：唯一临时 prefix、4 个进程、不调用 LightTTS HTTP。4-job idle burst 连续 3 轮均精确 accepted=3/rejected=1、maximum active=1、maximum queue=2、final 0/0；queued cancel、whole-job deadline、lease lost、post-commit response-loss recovery 和生产 Redis fail-closed 全通过，临时 key 清零。证据：[AUDIO-TTS-REDIS-GATE-ACTIVE-PLUS-PENDING.json](audio/20260717-005403/AUDIO-TTS-REDIS-GATE-ACTIVE-PLUS-PENDING.json)，SHA-256 `875630a349a3ffc2243e255edfedf8c70b476f16e6c428da6d0e66f8dcafd598`。
- 可控 mock 4/10/20：首次仅因 `/tmp` 目录为 root 属主而未启动，无 HTTP 调用；修正临时目录属主后完整通过。生产参数 `max_pending=2` 下 4/10/20 均接纳 3 个，拒绝率 25%/70%/85%，FIFO overtakes=0，9/9 WAV 有效、`.part`=0、mock endpoint maximum active=1。证据：[容量 JSON](audio/20260717-005403/lighttts-capacity-active-plus-pending/lighttts-capacity-4-10-20.json) 与 [Markdown](audio/20260717-005403/lighttts-capacity-active-plus-pending/lighttts-capacity-4-10-20.md)。该结果只证明接纳/隔离机制改善，不代表真实 GPU 吞吐，P1 保持 OPEN。
- 部署与回滚：发布包 `/tmp/phdebate-20260717T1120-core-reliability-source.tar.gz`，SHA-256 `8494d83e3eda72e27eb538151009bb0b59375b0c5ad4cb867dd5f668a2832212`。首次 API shadow 因临时包缺 `alembic.ini/alembic/` 且相对 backup status 指向临时目录而持续 ready 503；应用本身已启动，自动 rollback 在切换前恢复源文件并重启原 release，现网保持健康。补齐 shadow 元数据并固定绝对 backup status 后 ready 200。最终源码备份 `/home/ubuntu/sunsq/phdebate/runtime/deploy-backups/20260717T1120-core-reliability-before-core-reliability-source.tar.gz`，SHA-256 `5c5c10eee9259ce93c9f24cb31a242ada8d86cbebd9ec6d7c931bc71131bfe4b`；前一 Web 为 `20260717T1021-student-self-service-labels`。
- 生产发布：远端 Linux build、API port 12350 shadow、Web port 12349 首页/训练赛/只读观战 shadow 全通过；原子切换 Web release `20260717T1120-core-reliability` 并重启 API/Engine/Worker/Web。没有数据库、Nginx、`.env`、LightTTS 模型、GPU 并发、pending 或 timeout 配置变化；最终 ready 200、schema 0018、四服务 RUNNING、Worker queue/dead letter 0，部署后新增日志区间无 Traceback/ERROR。
- 真实 LightTTS canary：部署后同时提交三个短句，音频仅写入 `/tmp` QA 目录并持续监控真实比赛。墙钟 5.798 秒，三任务总耗时 2.074/3.915/5.696 秒，最大 active=1、queue=2，三个有效 WAV，最终 0/0，未检测到 active match。证据见 TC-951 与 [结果 JSON](audio/20260717-005403/lighttts-real-active-plus-pending-canary/result.json)。
- Computer Use：仅使用 Chrome；首页 200% 缩放与折叠菜单通过（TC-947/948）；发布后首页和只读观战实时连接通过（TC-949/950），随后返回首页。未使用 Safari 的锁定窗口，未使用 SunBrowser，未创建比赛、未进入 debate/control、未操作 278571 状态。
- 生产基线：10:41 CST ready 200，schema 0018，API/Web/Engine/Worker、FunASR、LightTTS 均健康，Worker queue/dead letter 0，LightTTS active/queue 0，`active_match_processing=false`。
- 本轮结论：安全改进已部署并生产验证；同步接纳从 2 提升到 3，但 4/10/20 仍会拒绝 25%/70%/85%，真实 GPU 大容量、FunASR 争用、MOS/CER 和真实弱网房间仍未关闭，因此 LightTTS 容量 P1 与整体“不可发布”结论保持。

### Iteration 14：首次设备检查、联网恢复与大厅范围收敛

- 时间：2026-07-17 11:21–11:44 CST。
- 生产基线：Web release `20260717T1120-core-reliability`；本地与生产 Iteration 13 三个核心文件哈希一致；ready 200、四主服务 RUNNING、LightTTS active/queue=0、`active_match_processing=false`。
- 最终实现：真人席位大厅新增 4 秒默认麦克风预检，160ms 更新三级音量，提供可访问 `meter` 和成功、静音、权限拒绝、无设备、设备占用、浏览器不支持等中文提示；所有路径停止轨道、断开节点并关闭 AudioContext，且不作为准备硬门禁。`useRoom` 在浏览器 offline 时停止退避/心跳并关闭 socket，online 后立即刷新 REST 权威快照并重建 WebSocket；terminal 4401/4403/4404 后自动 online 不再发 REST 或覆盖精确错误，手动重连才重置。大厅同时移除录音 Consent 面板、认领/准备前端硬门禁和相关文案，继续符合学生自助正式赛/训练赛范围。
- 审查回退：LightTTS “强标点→中标点→顿号”候选会产生句首/纯标点 chunk，并可能把长文本分段数放大至 `MAX_CHUNKS`；独立审查判 P2 风险后完整撤回，`providers.py` SHA 恢复为生产 `1a0a41c8af5f34e6fb76a2c04e66b904450ff5b30a18b183cd5567f32072b133`。未调用真实 LightTTS，未改变语音配置。
- 后端范围审计：学生自行创建的训练赛/正式赛 `activity_id=None`，现有 API 已不受录音 Consent 阻断；旧课堂活动房仍保留兼容门禁与 0015–0018 历史数据。当前不做破坏性删库或大规模授权重写；用户可见大厅已不展示或依赖 Consent。
- 本地质量门：完整 API `253 passed, 22 warnings in 66.34s`；完整 Web 连续两轮 `23 files / 130 tests passed`；`useRoom` 定向 11 passed；大厅+麦克风 16 passed；TypeScript、Ruff、py_compile、macOS 与远端 Linux Next production build全部通过。22 条 warning 均为既有 Starlette/audioop/Alembic deprecation。
- 部署与回滚：Web 包 `/tmp/phdebate-20260717T1137-student-device-recovery-web.tar.gz`，SHA-256 `8f2521705b33e242316e644dd982d99c455f5285d3ac1e95fb28c6bf9378968a`；源码回滚包 `/home/ubuntu/sunsq/phdebate/runtime/deploy-backups/20260717T1137-before-student-device-recovery-web-source.tar.gz`，SHA-256 `9f59f0c75ebfcd8978041f3a1ac66e03974ca4c83a63cf18347f9a4363f589d3`；前一指针保存在 `/home/ubuntu/sunsq/phdebate/runtime/deploy-backups/20260717T1137-before-student-device-recovery-web.txt`，前一 release 为 `20260717T1120-core-reliability`。
- 部署事件：远端 Linux build 与 12349 影子首页/训练赛/只读观战通过。第一次原子切换后 harness 错用历史端口 12343，30 秒探测失败并自动恢复旧 Web 指针和旧源码；旧 release 随即在真实端口 12341 RUNNING，主页/ready 200、stderr 空。确认 Supervisor 实际端口后，第二次对同一已验证 release 使用 12341 探测，切换成功。该事件未造成应用功能失败或数据变更。
- 生产结果：当前 Web release `20260717T1137-student-device-recovery`；六个变更文件本地/远端 SHA 全部一致。API/Web/Engine/Worker RUNNING，ready 200、schema 0018、Worker queue/dead letter 0、LightTTS gate 0/0、`active_match_processing=false`；首页、训练赛、只读观战连续 3 轮 HTTP 200，Web stderr 空。
- Computer Use：仅使用 Chrome。200% 只读观战内部滚动通过（TC-953）；发布后首页与只读观战实时连接通过（TC-954/955）。尝试打开训练赛创建窗核验大厅入口时，系统提示当前账号仍在真实房 278571 参赛并正确阻止第二房创建；未提交创建、未进入 278571 lobby/debate/control、未触发麦克风权限，也未改变真实房状态。Safari 锁定窗口未解锁，AdsPower/SunBrowser 未使用；最终 Chrome 已关闭弹窗并返回首页。
- 本轮结论：学生首次设备检查与网络恢复改进已安全上线；大厅用户可见 Consent 依赖已移除。生产麦克风权限实测因唯一安全 Chrome 会话占用真实房而未执行，仍需未来隔离 QA 会话补证；真实 LightTTS 4/10/20、真人 CER、人工 MOS 和真实移动/弱网仍未关闭，因此整体结论保持“不可发布”。

### Iteration 15：真实语音容量与过载恢复

- 时间：2026-07-17 11:47–12:56 CST。
- 生产基线：Web release `20260717T1137-student-device-recovery`；ready 200、schema 0018、四主服务 RUNNING、Worker queue/dead letter 0、平台 LightTTS gate 0/0、`active_match_processing=false`。
- 资源事实：生产只有 1 张 RTX 3080 Ti 12GiB；LightTTS 约占 4998MiB GPU，全部 GPU 计算进程均属于 LightTTS。FunASR 当前明确以 `--device cpu` 且空 `CUDA_VISIBLE_DEVICES` 启动，不与 TTS 争 GPU；但 FunASR、LightTTS、API/Engine/Worker 共用 12 logical CPU、31GiB RAM、无 swap，FunASR 还有全局 `MODEL_LOCK`，partial/final 推理串行。
- 新发现 P1：LightTTS Supervisor 使用 `--health_monitor`，内部每约 88–89 秒请求 `/health`；该端点真实合成“你好”，最近 26 次 P95 约 1.420 秒。它不进入 平台 Redis active/queue，稳定抢占 GPU、污染容量指标，饱和时连续三次失败还会自杀重启。旧 `jixia-debate` 与 `jixia-voice-agent` 也仍直接指向同一 8080/10095 端点，不受 平台 gate 完整统计；因此 `gate=0/0` 不能证明模型端点绝对空闲。
- 容量边界：LightTTS 服务全链 `http workers=1 / running_max_req_size=1 / encode,gpt,decode parallel=1 / decode_max_batch_size=1`；decoder 源码硬编码 batch size 1。单机第二实例虽可用端口命名空间隔离，但主机约 12GiB MemAvailable、无 swap，现有 LightTTS 进程组内存很高，不能安全把第二个完整模型塞入同一生产机。FunASR 4 并发首个 partial 派生 P95 约 4.004 秒，10/20 缺证；LightTTS 完整 WAV 隔离 P95 8.609 秒，与 FunASR 基准并行时 21.527 秒。
- 新发现 P1：`queue_full`、queue timeout 和 admission unavailable 被压平为普通 `ProviderError`；Engine 立即把发言设为 failed、房间设为 paused，且 paused 不会自动处理。当前容量为 `1 active + 2 waiting` 时，第 4 个并发房间不是“稍后继续”，而是整场永久等待人工重试。学生舞台不展示 `failure_reason`，也没有现有 `/control/retry` 的入口；固定 AI 发言在 Agent/TTS 准备最长 300 秒时仍倒计时，可能长期显示 00:00 与“正在生成”并存。TTS-only 人工重试还会重新调用 Agent，造成观点漂移和额外负载。
- 已关闭旁路自检：备份 `/home/ubuntu/sunsq/phdebate/runtime/deploy-backups/20260717T1200-before-lighttts-no-synthetic-health.conf` 后，从生产 LightTTS Supervisor 命令移除 `--health_monitor`；保留 Supervisor autorestart、平台 TCP/readiness 和显式真实 canary。模型重载后 liveness/ready 恢复，旧 `GET /health` 计数 921 在超过一个 88 秒周期后不增长。通过 平台 gate 的短句 canary 3.479 秒完成，WAV 4.32 秒，最大 active=1、最终 0/0；证据见 TC-956。
- 本地实现：`ProviderError` 新增稳定 `code/retryable/retry_after_seconds`，queue full/timeout/admission unavailable 映射为可恢复错误；AI speech 与 cue 按 2/5/10 秒最多三次取消感知重试，重试期间只写 `provider.retrying`、不暂停房间，耗尽才进入既有人工暂停。固定/自由 AI Agent+TTS 准备统一持久化 `ai_preparing` 并冻结总计时/轮次计时，音频可播放后才按实际开始时间恢复。TTS-only 人工重试或 Engine restart 仅复用同阶段、同席位、已有文本且从未发布/播放音频的最近失败内容，跳过 Agent 并记录 `speech.content.reused`。
- 学生舞台实现：所有 `failure_reason` 统一显示脱敏安全横幅；`can_control=true` 的房主可直接调用现有幂等 `/control/retry`，断线时也能应用 REST 权威房间响应；非房主明确等待房主。失败后复用同一 idempotency key，不显示 Redis、内部端点或原始 provider 文本。`ai_preparing` 期间浏览器本地总计时和自由轮次计时也冻结，标记清除后恢复。
- 首轮审查 NO-GO：API 复审发现自由辩论席位轮换会让 failed/interrupted 文本留到若干轮后再次复用，且 2/5/10 重试每次重置 300 秒 provider deadline，最坏可能接近 20 分钟。修复为 `_tts_reuse_source` 只接受当前 stage 全体发言中最新、未发布/未播放的可复用尝试，`_free_ai_seat` 立即选择其原席位，正常轮换只统计 completed；新 attempt 创建后自然一次性消费 source。AI speech/cue 各只计算一个共享 monotonic deadline，所有 attempt 和等待使用同一预算，不采用会遗留后台 GPU 工作的外层强制取消。
- Web 复审闭环：重试幂等键绑定 `room.seq + failure_reason`，同一故障复用、新故障换键；`completed/review_required + matching timed_out speech` 保留迟到补交能力，提交中收到终局 WebSocket 不中断 finish/音频上传；仅 `terminated`、匹配的 `interrupted` 明确允许放弃未提交内容。可恢复 409（如其他设备接管）不再被误判为永久拒绝，录音/文字仍保留且可编辑重试。房主观战文案与权限一致；失败/断线横幅改为单一自然堆叠容器，移动端不再依赖固定 top 猜测高度。
- 当前最终质量门：完整 API `264 passed, 22 warnings in 41.75s`；完整 Web `23 files / 140 tests passed`；DebateStage 定向 `42 passed`；TypeScript、Ruff、py_compile、Next production build 全部通过。Web 依赖在早先 pnpm 事故后已用仓库 `npm ci` 重建，`npm audit` 0 vulnerability；本轮绿灯来自重建后的环境。新增全路径覆盖自由辩论立即同席 TTS-only 复用一次、共享 deadline 不倍增、迟到终稿补交、上传中终局竞态和可恢复 409 不丢录音。
- 部署装置事件：前两次尝试均在生产切换前自动回滚。原因是 harness 使用 `install -o ubuntu -m 640 /dev/null /tmp/<shadow>.log` 后，root shell 在 sticky `/tmp` 受 `fs.protected_regular` 拒绝重定向；第一次因 SSH stderr 被抑制仅看到空日志，第二次明确捕获 `Permission denied`。两次都恢复旧源码/旧 Web 指针并重启原服务，ready 200、真实房未变。第三次改为每轮 `rm + root-owned 0600` 日志后完整通过。
- 生产发布：发布包 `/tmp/phdebate-20260717T1255-overload-recovery-source.tar.gz`，SHA-256 `22c6265831e93569829aebac983aca2af0c21f1c08339954615da83449725328`；源码回滚包 `/home/ubuntu/sunsq/phdebate/runtime/deploy-backups/20260717T1255-before-overload-recovery-source.tar.gz`，SHA-256 `726557f6e8bdf025b1415fce93acb05a11908dd836e34c0af713996918a97081`；旧 Web 指针记录 `/home/ubuntu/sunsq/phdebate/runtime/deploy-backups/20260717T1255-before-overload-recovery-web.txt`，前一 release 为 `20260717T1137-student-device-recovery`。API port 12350 shadow ready/schema/gate 通过；远端 Linux Next build 通过；Web port 12349 首页/训练赛/公开 watch 均 200。切换 release `20260717T1255-overload-recovery` 后重启 API/Engine/Worker/Web，11/11 源码哈希与本地一致。未改数据库、Nginx、`.env`、LightTTS 模型、GPU 并发、pending 或 timeout 配置。
- 发布后健康：12:56 CST ready 200，schema current/expected `0018_research_exports`，四主服务 RUNNING，Worker queue/dead letter 0，LightTTS gate 0/0，`active_match_processing=false`；首页、训练赛和 278571 `/watch` 均 200。新 API/Engine/Web stderr 增量为 0，Worker 879 bytes 仅为无 `ERROR/Traceback/CRITICAL` 的正常重启日志。
- Computer Use：仅使用 Chrome，未使用 Safari、AdsPower 或 SunBrowser。发布后首页通过 TC-957；只访问真实房 278571 的公开 `/watch`，连续显示实时连接与脱敏故障恢复横幅。当前 Chrome 账号是该房房主，因此可见“重试异常步骤”，但未点击；未进入 lobby/debate/control，最终返回首页。证据见 TC-958。
- 测试环境事件：只读 Web 审计线程误用 pnpm，仅重排本地 `apps/web/node_modules`；未改源码、未部署。主线程用仓库 lock 执行 `npm ci` 完整恢复，`npm audit` 0 vulnerability，并在恢复环境上重跑 140 项全量 Web 与 production build。
- 下一切片：真实 4/10/20 模型吞吐只在独立 GPU/隔离实例做，不把同机第二实例或生产 gate=0 当作安全容量证据；使用隔离 QA 房补做房主实际 retry、断线 REST retry 和非房主多用户权限。

### Iteration 16：权威音频播放与回声污染

- 时间：2026-07-17 13:00–13:44 CST。
- 浏览器边界：仅使用 Chrome。开始读取正式赛规则页时，用户将 Chrome 切换到另一个正在使用的管理页；Computer Use 立即停止动作，未抢占标签、未读写该页。Safari、AdsPower 与 SunBrowser 未使用。
- 新发现 P1：`DebateStage` 遇到任意 `speech.audio.ready` 时，会选择“最近一个有 `audio_url` 的 Speech”自动播放。但真人完成发言后上传录音也广播同一事件，且 Speech 仍为 `completed`；因此所有在房客户端可能立即回放刚提交的真人录音，与下一阶段/AI 音频重叠并污染下一位真人 ASR。WS `liveEvent` 不含 speech_id/speaker_type/audio_url，前端无法从事件本身安全区分。
- 同根因 P1：当权威 snapshot 已无 `status=playing` 音频时，原 effect 直接 return，不 pause/clear 已创建的 `HTMLAudioElement`；旧 AI/cue 音频可跨阶段继续。且持久 `audio.cue.ready` 被当作当前播放事件，可在历史入场或 AI 播放结束后复活。
- 本地修复：自动 Speech 播放严格限定为 `room.active_speech.id` 对齐且 snapshot `status=playing` 的权威 AI 发言，完全移除 `liveEvent speech.audio.ready` fallback。播放 identity 绑定 `speech_id + audio_url + playback_started_at`，暂停后新 epoch 按服务端 elapsed seek。无权威音频、静音、终局/评判时主动 pause + clear。Cue 只在 `announcement` 阶段且匹配 `stage.started` 的 10 秒窗口内启动，离开阶段立即停止。
- 定向质量门：DebateStage `44 passed`，TypeScript `tsc --noEmit` 通过。新回归覆盖：真人 completed 录音 + `speech.audio.ready` 不创建 Audio；权威 AI playing 播放并按 elapsed seek；server completed 后立即 pause且不创建新 Audio；历史 cue 不复活；新开场 cue 只播放一次并在下一阶段停止。
- 新发现 P1（首页故障域）：首页把核心 `/api/competitions` 与次要 `/api/live-rooms`、`/api/rankings` 绑在同一 `Promise.all`；任一直播/榜单服务失败或长时间卡顿，整个赛事大厅都进入 LoadError，学生无法创建或加入比赛。本地修复将 competitions 作为唯一核心 gate，直播/榜单分区捕获错误、显示“不影响参赛”与独立重试。新测试确认两个次要接口同时 reject 时，4v4 创建按钮仍 enabled，未出现全页失败。
- 新发现 P2（readiness 污染 FunASR 日志）：`system_health._endpoint_check()` 对 `ws://127.0.0.1:10095` 只做 raw TCP open/close；每次 `/api/health/ready` 稳定让 FunASR 产生一条 `opening handshake failed` ERROR traceback，生产已累计 199 次/约 1.49MB。本地修复按 scheme 分流，`ws/wss` 使用合法 WebSocket handshake 后立即关闭，其他端点保留 TCP 检查；新增真实本地 WebSocket server 回归，验证一次合法连接且 readiness 返回 ok。
- 生产只读审计其他结论：ready 10/10=200，四主服务、queue/dead letter、gate、房/Match/Speech/Event 跨房引用、27/27 媒体、`.part` 清理和备份 catalog 均正常。新 P2 包括：单 API 重启窗口产生 4 次 WS 502，进程内 presence/启动全局 reset 阻碍双 API 蓝绿；3 个 legacy completed 房缺 winner/scorecard/用户关联且 UI 会误显示“比赛正在进行”。本轮不猜测回填旧赛果。
- 学生端只读审计其他 P2：刷新/离开会丢失未提交录音；AI 超时接替后原辩手/房主无自助恢复；未登录加入的房号/`next`/创建草稿丢失；空席快速双点并发 claim；房主创建后无法换席；席位选择、动态阶段播报和 38×38 移动触控目标存在可访问性缺口；ASR ready 前 PCM 直接丢弃存在句首截断窗口。
- 最终质量门：完整 API `265 passed, 22 warnings in 111.06s`，Ruff 与全部 Python py_compile 通过；完整 Web `23 files / 144 tests passed`，TypeScript 和 Next production build 通过。DebateStage 定向 44 passed，public pages 5 passed。权威音频、首页故障域和 WebSocket readiness 三项均获得独立只读 GO，未发现新 P0/P1。
- 部署装置事件：首次 release `20260717T1340-authoritative-audio-home-resilience` 在生产切换前自动回滚。API shadow 实际 ready/FunASR ok，但 harness 在运行旧 API 预检之前记录 FunASR stderr 基线，因而把旧实现的最后一条 raw-TCP `opening handshake failed` 误算为新 shadow 增量。回滚恢复旧源码/Web 指针和四服务，ready 200。第二次把基线移到旧预检之后，shadow 与 production readiness 的 FunASR stderr 增量均精确为 0。
- 生产发布：发布包 `/tmp/phdebate-20260717T1348-authoritative-audio-home-resilience-source.tar.gz`，SHA-256 `1fe86f8121b891c3be05ee2ab8bbf07b62db1470e7f48f4f5052d1cac345a33d`；源码回滚包 `/home/ubuntu/sunsq/phdebate/runtime/deploy-backups/20260717T1348-before-authoritative-audio-home-resilience-source.tar.gz`，SHA-256 `4ea3836b80140e1cb68f9c875a8504330c0d49fbaeeaf715577fe483b19be321`；旧 Web 指针 `/home/ubuntu/sunsq/phdebate/runtime/deploy-backups/20260717T1348-before-authoritative-audio-home-resilience-web.txt`，前一 release `20260717T1255-overload-recovery`。API port 12350 shadow ready/schema/FunASR 通过，FunASR stderr delta=0；远端 Linux build 通过；Web port 12349 首页/训练赛/公开 watch 均 200。切换 `20260717T1348-authoritative-audio-home-resilience` 后重启四服务，6/6 源码哈希与本地一致。未改数据库、Nginx、`.env`、LightTTS/FunASR 模型或容量配置。
- 发布后状态：ready 连续 5/5=200，schema 0018，四服务 RUNNING，queue/dead letter 0，gate 0/0，`active_match_processing=false`；连续 5 次 readiness 后 FunASR stderr delta=0。API/Engine/Web 新 stderr 为 0，Worker 991 bytes 仅正常重启日志，无 ERROR/Traceback。首页、训练赛、公开 watch 均 200。
- Computer Use 回归边界：发布后 Chrome 当前焦点仍是用户正在使用的其他页面，本线程只读取窗口标题确认边界，未切换标签或抢占 Chrome。因此本 release 的发布后 Computer Use 视觉截图待下一个安全窗口；当前以全量测试、API/Web shadow、production HTTP/日志为生产回归证据。未使用 Safari、AdsPower 或 SunBrowser。
- 语音后续 P2（只读证据）：8 个现存双分段 LightTTS 样本的实际拼接点总静音中位约 340ms，2 条 >500ms；样本首部静音最高 1000ms，1/5 字短句跨样本 RMS 极差 10.61dB。原因是原始 segment 不裁边界静音/不交叉淡化/不做响度处理，另固定插入 120ms 零段。`_split_text` 也会在第 30 个可见字符硬切英文/URL/长数字 token，现有正式产物未覆盖 NUMBERS_ENGLISH/SPECIAL_MARKUP/30–300 字边界。该两项不在本次 P1 修复包中，后续先做离线 PCM16 保守裁静音/峰值安全响度统一和 token-aware split，再使用隔离真实 TTS 复核 MOS/CER。

### Iteration 17：ASR 句首/句尾可靠性与 LightTTS 截断响应校验

- 时间：2026-07-17 13:45–14:38 CST。
- 浏览器边界：用户正在使用 Chrome 的百度智能云等其他页面，本线程只读取当前窗口标题/可访问性树确认边界，未切换后台稷下标签、未抢焦点、未保存用户页面截图；Safari 系统密码锁定窗口未解锁；AdsPower/SunBrowser 未使用。发布后浏览器视觉回归因此以 HTTP/shadow/自动化替代，待安全窗口补截图。
- ASR 根因：旧前端只有 WebSocket 收到 `ready` 后才发送 PCM，因此连接握手期间的句首音频被静默丢弃；停止时先关闭 AudioContext、随后立即发 `finish`，无法保证最后一个 processor block 已送达；双击结束与服务端超时也没有共享 single-flight，存在重复 stop/finish/提交风险。API/FunASR 固定把 wire PCM 解释为 16kHz mono PCM16，但旧前端只请求 `AudioContext({sampleRate:16000})`，没有对实际 rate 做有状态重采样或声明协议。
- 最终实现：为每次 capture 创建 generation-scoped ASR session；`ready` 前按顺序缓存最多 32,000 samples（2 秒/64KiB），溢出时有界丢弃旧块并强制人工核对；所有输入先依据 `context.sampleRate` 有状态线性重采样至 16k，再以 1024-sample processor 执行 VAD/PCM16 编码。认证声明 `protocol_version=1 / pcm_s16le / mono / 16000`；新 API 对完全无协议字段的 legacy 客户端继续兼容，显式 v1 四字段严格验证，错误/部分/未知字段在 claim 和 FunASR connect 前结构化拒绝并以 4400 关闭。
- 停止顺序：`stopSpeaking` 已改为共享 Promise；先标记 stopping，等待下一 PCM block 或最多 150ms，再断开/关闭采集并停止 MediaRecorder；若 ASR 尚未 ready，最多等待 2 秒，ready 回调必须先顺序 flush 缓存再 resolve，随后 `finish` 作为最后一条 WebSocket 消息；永不 ready 时直接保留录音并进入人工核对，不再额外等待 30.5 秒 final。旧 generation、abort 和 unmount 回调不得 flush 或提交。
- LightTTS 当前本地实现：过短校验改为 `max(0.25 秒, 可见字符数 × 0.08 秒)`，取消旧 4 秒上限，同一公式覆盖 segment、合并结果与缓存命中。30 字 0.8/1.0 秒拒绝、2.4 秒通过；定向 `test_providers.py` 48 passed，Ruff 与 py_compile 通过。独立只读复核判 GO（限生产 speed=1.0）：17 条生产 AI 发言、5 条 cue 和四音色 1/5 字样本均不会误拒；未来 speed=2.0 与 markup/URL/emoji 密集文本仍记 P2。
- 新增复审闭环：独立只读审查发现并关闭两个高风险竞态。其一，`AudioContext.resume()` 挂起期间阶段切换后，旧 generation 恢复不得重新 `setCapturing(true)`；新增 suspended-resume + stage switch 回归。其二，恢复发言在 `finish` 后、final 前 socket 正常关闭，不得把 waiter resolve 误当最终字幕成功并自动提交旧 baseline；现在强制人工核对，新增 resumed baseline + close-before-final 回归。另修复 finish send 抛错仍等待 30.5 秒，以及旧 recorder 异步 onstop 清新 generation ref。
- 最终质量门：DebateStage `53 passed`，新增覆盖 A/B/C ready 缓存顺序、tail block→finish、stop-before-ready、never-ready 2 秒降级、重复点击单 stop/finish、旧 generation 零 flush、44.1k/48k 跨块重采样、suspended gen1→gen2 交错、close-before-final 和 finish send throw。完整 Web `23 files / 153 tests passed`；完整 API `277 passed, 22 warnings in 62.52s`；ASR 协议定向 10 passed；provider 48 passed；TypeScript、Next production build、Ruff、compileall 全部通过。首次全量 API 与 provider 定向并行运行导致共享 SQLite 测试库被竞争删除/重建，出现只读/503；停止并行 API 后单独全量重跑 277 项全绿，前次结果不作为产品失败。
- 复审结果：ASR 前端最终只读复审 GO，确认旧 generation catch 的 recorder/stream identity guard、当前 generation 错误写入和 gen2 存活回归闭环；ASR 协议/LightTTS 后端复审 GO。残余 P2 为 TTS speed=2、markup/URL/emoji、长文本旧 4 秒 cache 与同 speech 短 cache 并发单请求等扩展测试。
- 生产发布：包 `/tmp/phdebate-20260717T1435-asr-boundary-tts-integrity-source.tar.gz`，SHA-256 `5098e5852f5b7f9d0a0032097c61bd27a0c6ad8396797c75b5a69b059b26bcf0`；源码备份 `/home/ubuntu/sunsq/phdebate/runtime/deploy-backups/20260717T1435-asr-boundary-tts-integrity-before-source.tar.gz`，SHA-256 `06eaeefd2c65a9eecf312606f8c783581766cc1ee7e33c8416a23f7ae460d295`；旧 Web 指针记录在同目录 `...-before-web.txt`。API 12350 shadow 使用 `PRESENCE_RESET_ON_STARTUP=false`，ready 200；Linux build 和 Web 12349 首页/训练赛/watch 均 200；切换 release 后四服务重启成功，6/6 源码哈希一致。未修改数据库、Nginx、`.env`、模型、GPU 并发、pending 或 timeout。
- 发布后状态：ready 连续 5/5=200，schema 0018，API/Engine/Worker/Web、FunASR、LightTTS RUNNING，queue/dead letter 0，gate 0/0，`active_match_processing=false`；公开首页/训练赛/watch 均 200。API/Engine/Web stderr delta=0，Worker 879 bytes 仅正常重启日志。真实房 278571 的公开 snapshot 仍为 `paused / seq 91`；未进入 lobby/debate/control。部署前约 06:33:37 该房后台 WebSocket 曾出现既有 row-lock timeout/ASGI 错误，发生在本轮 stderr 基线前；新服务启动后无新增错误，房状态未改变。
- 用户新增下一轮需求：辩手辩论过程中，在合适权限和状态下提供暂停/继续、修正 ASR 文字稿、结束比赛、退出比赛。下一轮先审计既有 control API、角色权限、断线/重复操作和“退出是否 AI 接替/释放席位”的产品语义，再做最小学生端控件；不影响本轮发布，也不引入管理模块。

### Iteration 18：辩手页轻量控制、提交前 ASR 修正与退出防丢

- 时间：2026-07-17 14:35–15:25 CST。
- 产品边界：不新增教学、政策、研究、赛事编排或赛后稿件版本管理。暂停/继续/提前结束复用既有 owner-only control API；“退出比赛”明确实现为离开比赛页面、保留席位归属，离开较久时既有 presence 规则可能由 AI 接替，不虚构为释放席位。ASR 修改限定在当前真人发言提交前，避免已启动 Agent/Judge 读取到变化中的历史稿。
- 舞台控制：现有“设置”改为“比赛操作”面板，主发言按钮仍是唯一中央主操作。房主在 debate 和 watch 均可暂停、继续或提前结束；真人发言进行中暂停按后端规则禁用，异常暂停只能 retry；提前结束使用不可恢复的可访问确认框。控制走 REST，不依赖 WebSocket connected；陈旧状态由后端房间锁和 403/409 权威裁决。
- 幂等与并发：每个 control action 持有稳定 `X-Idempotency-Key`，响应丢失后复用；权威 room snapshot 对账到目标状态后清旧 key。同步 `roomActionInFlight` 在 React 重渲染前拦截快速双击和跨动作并发；REST/WS 响应继续按 `seq >= current.seq` 合并。
- ASR 人工修正：录音中可选择“结束发言并修改文字”，复用 generation-scoped stop single-flight、tail drain、ready/final 等待和 pending editor；即使 ASR 完整也不会自动 finish，必须先确认文字。普通“结束发言”仍保留自动提交，不强迫每轮额外操作。
- 两阶段提交修复：独立语音复审发现旧实现若文字 finish 成功但 audio 上传失败，仍允许改文字并以同一幂等键重提，必然 409。现在 `PendingFinish` 保存 `finalizedSpeechId/finalizedContent`；finish 200 后文字立即锁定，audio 失败只重试权威 speech id 的音频。`finishPromise` 提供 submit single-flight，陈旧 controller 不得提前清 busy。
- 退出与导航保护：无本地工作时，顶部品牌或面板退出均先说明比赛继续、席位归属保留、AI 可能接替，确认后进入 `/me`。starting/capturing/finishing/pending 时退出禁用并显示原因；`beforeunload` 覆盖刷新/关页，同 URL duplicate-history guard 覆盖后退、Alt+Left 和移动侧滑 `popstate`，本地工作结束后回收 guard。
- 可访问性：操作面板和危险确认各自有焦点 trap/Escape；打开确认前先关闭面板，任一时刻只有一个 modal；取消按真实来源回到顶部品牌或重开操作面板。移动 tool、品牌、action 和确认按钮均至少 44px，sheet 保留 safe-area 与滚动。
- 质量门与复审：DebateStage 60 passed；DebateStage+Watch+DebatePage 定向 69 passed；Web 最终全量 `23 files / 160 tests passed`；TypeScript 与 Next production build 通过。UI、控制可靠性、语音数据一致性三路首轮分别发现 popstate/双模态、WS/watch 控制、finish/audio 两阶段问题，修复后二轮全部 GO。房主观战说明文案最后调整后首轮全量仅有一条旧文本断言失败，更新断言并重新全量后 160 项通过。
- 生产发布：Web-only release `20260717T1520-participant-controls`；包 `/tmp/phdebate-20260717T1520-participant-controls-web.tar.gz`，SHA-256 `5eed0cac30b7043fb3c7b04dc63a888d116e6fb87eca6d60836c6bfc2ff1f31f`。源码备份 `/home/ubuntu/sunsq/phdebate/runtime/deploy-backups/20260717T1520-participant-controls-before-source.tar.gz`，SHA-256 `98589f26a2965967ea7013f3db0c9454a8d7e0db4522bbe87692affe1829966b`；旧 Web 指针记录在同目录 `...-before-web.txt`。发布后 test-only 文案断言同步另有回滚包 `20260717T1520-participant-controls-postrelease-test-before.tar.gz`，SHA-256 `bdd58cd6da002659bc38b55f9d8a871c0adbedc8050ea760ca077418d4f3166a`，未重启服务。
- 发布后状态：12349 shadow 与 12341 production 首页/训练赛/公开 watch 均 200；只重启 Web，API/Engine/Worker 未重启。ready 连续 5/5、所有主服务/FunASR/LightTTS RUNNING、queue/dead letter 0、gate 0/0、`active_match_processing=false`；四文件哈希一致，Web stderr delta=0。真实房 278571 保持 `paused / seq 91`，未进入 lobby/debate/control 或触发写操作。
- Computer Use 边界：初次 fresh app state 显示 Chrome 聚焦用户的“CHI 2026 论文列表”。稍后确认前台长时间无变化后，切换既有稷下标签；发现其停留在真实房 `/debate` 后未点击页面内容并立即改为允许的 `/watch`，只读检查“比赛操作”入口与面板，保存 TC-978/979，再恢复原 CHI 标签。全程未点击 retry、暂停、继续、提前结束或其他写操作。Safari 密码锁未解锁；AdsPower/SunBrowser 未使用。仍待补录真人 debate 的 ASR 修正/退出保护和精确移动端视觉。

### Iteration 19：LightTTS 分段边界与浏览器音频生命周期

- 时间：2026-07-17 15:26–17:10 CST。
- 波形根因已复核：8 个现存双分段生产样本的拼接点均存在约 120–131.8ms 连续数字零；按严格 `-60dBFS / 10ms` 窗口计算，包含拼接点的总低能量间隔为 120–750ms，中位约 255ms。2/8 边界在前一段最后 1–10ms 仍有约 `-15dBFS` 至 `-8.2dBFS RMS` 的明显语音能量时直接切为数字零，存在硬切 click 风险。相邻分段整体 RMS 差仅 0.08–2.20dB、峰值最高 `-0.09dBFS` 且无削波，因此本轮不做自动响度归一化。可重复分析与逐样本结果见 [Iteration 19 拼接边界审计](audio/20260717-005403/iteration19-lighttts-splice-audit.md) 和同目录 JSON。
- 安全设计：只处理多段 WAV 的内部边界；单段输出和整句全局首尾保持不变。仅对 uncompressed PCM16 mono 使用严格强静音检测；只裁已确认的内部边缘静音并保留安全垫，动态补足到约 120ms 总间隔。若没有可裁静音，不删除语音帧，仅对边界 5ms 做淡出/淡入以消除阶跃；不做语音重叠 crossfade，不处理内部自然停顿。
- 生产可靠性审计同时发现两个条件性 P1：同一 `speech_id` 并发 writer 会被通配 stale-part 清理互删临时文件；final `os.replace` 后再次观察到 caller cancel 时会删除已被并发 cache reader 返回的文件。本轮实现要求 stale 清理只碰足够旧的临时文件，最终取消检查必须在 commit 前完成，发布后 final 视为不可变。
- 浏览器专项发现并关闭三个 P1：旧 Audio A 的延迟 `play()` reject 可在 B 已开始后把全局设为 muted；Safari 的 `NotAllowedError` 被混同网络/解码错误且“开启声音”未在用户手势处理器中直接 `play()`；阶段切换只 `pause()`、未清空 `src`/`load()`，旧长 WAV 可能继续下载并由旧回调影响新实例。最终实现以 generation/identity、attempt token 和 single-flight 隔离异步结果；dispose 会移除 listener、pause、清 src 并 `load()`；静音恢复追赶权威时间轴，`elapsed>=duration` 不播放，cue 超过阶段开始 10 秒必然失效。
- GitHub/生产源码交叉调研：CosyVoice/LightTTS 官方实现已支持 HTTP PCM streaming、WebSocket bi-stream、prompt embedding 缓存和 continuous batching；生产 LightTTS 也确实暴露 `stream=true` 与 `/inference_zero_shot_bistream`。当前平台却显式发送 `stream=false` 并读取完整 `response.content`，浏览器再请求完整 WAV；生产 `running_max_req_size/decode_max_batch_size` 与应用 global active 均为 1。即“整段完成后才播放、一次只合成一条”主要是现有平台链路/部署选择，不是模型协议硬限制。完整一手源码映射、最小协议、AudioWorklet ring、2–3 场并发门槛见 [实时音频参考研究](audio/20260717-005403/iteration19-github-realtime-audio-research.md)。
- 实现与质量门：Provider `56 passed`，完整 API `285 passed, 22 warnings in 58.95s`；DebateStage `73 passed`，完整 Web `23 files / 173 tests passed`；Ruff、py_compile、TypeScript 和 Next 16.2.10 production build 通过。独立 provider 最终复审 GO；Web 三轮复审关闭同 session 多 play、静音恢复不追赶、ended 重播、`elapsed>=duration` 仍 play、过期 cue 复活和 expired/ended media error 污染，最终 GO。
- 发布后真实质量复测：8/8 固定样本成功，CER 中位 0.0541；四音色标准中文均为 0。双分段边界 exact zero 从 120–131.8ms 降为 85–119ms，join low-energy 中位/最大从 255/750ms 降为 130/130ms，立即硬阶跃从 2 降为 0，无削波。voice2 专名三次复测 CER 为 0.2432/0.1892/0.1892，属于既有音色弱项而非边界回归。证据见 [发布后质量基准](audio/20260717-005403/lighttts-benchmark-iteration19-postdeploy/lighttts-benchmark.md)、[拼接复测](audio/20260717-005403/lighttts-benchmark-iteration19-postdeploy/splice-audit.md) 与 `lighttts-iteration19-voice2-repeats/`。
- 生产发布：release `20260717T1640-audio-boundary-playback`；包 `/tmp/phdebate-20260717T1640-audio-boundary-playback-source.tar.gz`，SHA-256 `0cff4f3c1b9e9d4a1a19ad4842380e416ab989a706f2f86e2a708f6e73c97e98`；源码备份 `/home/ubuntu/sunsq/phdebate/runtime/deploy-backups/20260717T1640-audio-boundary-playback-before-source.tar.gz`，SHA-256 `f14e5962350f10d66dcfa51b9af7acae13d564bf21c55f8b574b3d47490fa0e8`，旧 Web 指针保存在同前缀 `...-before-web.txt`。12350 API shadow ready 与 12349 Web shadow 首页/训练赛/watch 均通过；切换后 API/Engine/Worker/Web 正常，生产三路 HTTP 200，readiness 5/5、queue/dead 0、gate 0/0、stderr delta 0，FunASR/LightTTS RUNNING。未修改数据库、Nginx、`.env`、模型或并发配置。
- Computer Use：TC-980/981 在 Chrome 真实 watch 验证发布后声音开关、全屏和比赛操作入口。只切换本地声音状态并还原，未点击 retry、暂停、继续或结束；截图已保存。真实房保持暂停，seq 95 仅对应 presence 事件。随后恢复用户原“CHI 2026 论文列表”标签；Safari 未解锁，AdsPower/SunBrowser 未使用。
- 原生流式容量结论：直接生产本地 `stream=true` 的 18 个请求全部成功，单路首块 P95 1.259 秒；但 2/3 并发首块 P95 为 6.333/10.983 秒，RTF P95 为 1.795/2.651，呈串行处理。原生流式能把约 4 秒完整 WAV 等待缩短到单路约 1.2 秒首 PCM，但当前不能满足 2–3 场同时实时发声。详见 [1/2/3 并发基准](audio/20260717-005403/iteration19-lighttts-stream-1-2-3.md)。
- 当前决策：Iteration 19 的边界与旧音频生命周期缺陷已关闭；不再继续堆叠完整 WAV 优化。下一轮实现 PCM streaming + WebSocket room audio protocol + AudioWorklet ring，并先在独立 active=2 灰度实例验证 TTFT、RTF、GPU 水位、欠载和第三路公平等待。正式赛切换前保留完整 WAV 一键回退；整体发布结论仍为不可发布。

### Iteration 20：流式候选并行 NO-GO 审计

- 时间：2026-07-17 17:10–18:27 CST。
- 用户边界：只测试、研究和保存证据；不修改代码、不部署。三路子任务分别审查生产可靠性、学生浏览器体验和语音协议；主任务交叉检索 LightTTS、CosyVoice、GoogleChromeLabs Web Audio 与 WebKit/MDN 一手实现。
- 结论：候选 `LightTTS bi-stream → growing WAV part → 独立 audio WS → AudioWorklet ring` 的方向合理，但当前实现 **NO-GO**。两个 P0 分别是 future late-join seek 越过当前 live edge，以及不足 400ms 的 final tail 永久不 prime/不播放；已有自动化全绿却没有覆盖这两个状态。
- 比赛状态 P1：首 PCM 已恢复自由辩论剩余时间，但 final WAV 会再次清 preparation 并把 deadline 改为本条音频结束时间，可能把 300 秒自由辩论压缩为十几秒。
- 浏览器 P1：服务端和 MessagePort 缺少有界 pacing/high-water；重试计数在每次 started 清零，可能形成 overflow 重连风暴。Worklet 缓冲 prime 不等于 Safari/Chrome AudioContext 已 running，未用户手势时仍可能收满 3 秒 ring 并错误清除“开启声音”提示。
- 运维 P1：standalone engine 没有 SIGTERM stop/drain，活动任务期间重启可能留下最长约 330 秒 admission lease；恢复 abort 只落库不主动 publish 普通 room WS；上游断连是否真正停止 GPU 仍缺确认探针。
- 容量：生产 1/2/3 并发仍为 1.259/6.333/10.983 秒首块 P95。同机第二 LightTTS 实例因 GPU/主机内存无余量为 NO-GO；生产 active=2 冷切也没有安全证据。LightTTS 官方 CLI 虽支持 request concurrency，但 `decode_max_batch_size` 明确“currently only support 1”，必须在独立 GPU/主机验证真实 token-to-wave 并行。
- 正向证据：bi-stream PCM 与 final WAV 一致，取消不发布 final；音频 WS 的权限复验、独立 FD、发送超时和 generation snapshot 设计成立；feature flag 默认关闭，生产未受本轮候选影响。
- 研究对照：LightTTS 官方提供 HTTP streaming、bi-stream 和 TTFT/RTF benchmark；公开 4090D stream benchmark 在 2/4 workers 下 TTFT P90 约 1.53/4.37 秒，说明 worker 数增加不自动等于实时。GoogleChromeLabs 用有界 ring 解耦 WebAudio 128-frame render quantum；WebKit/MDN 明确要求在真实用户手势中 create/resume/play 并检查实际 running/rejection。
- 完整证据：[Iteration 20 LightTTS/WS/AudioWorklet 审计](audio/20260717-005403/iteration20-lighttts-ws-audioworklet.md)、[GitHub/官方实现证据矩阵](audio/20260717-005403/iteration20-github-realtime-audio-evidence-matrix.md)与[Chrome 生产音频回归](audio/20260717-005403/iteration20-chrome-production-audio-regression.md)。外部成熟链路共同区分 sent/buffered/played，支持增加 played PTS/buffered samples ACK，并由服务端决定 fresh join live edge。
- Chrome 现网：TC-987–989 重新验证生产完整 WAV 回退路径。watch 声音开关只改变本地许可并可还原；结果页两条 AI WAV 连续切换时恰好一条 playing；播放中离页到首页后 audio=0，返回结果页 9 条 audio 全部 paused/currentTime=0。Range GET 返回 206、Accept-Ranges 与正确 Content-Range；console warning/error 0。18:26 CST ready `ok=true`、LightTTS active/queue=0/0、max active=1、`active_match_processing=false`。房 278571 保持 `paused / remaining 172s / seq 102`，新增 101/102 仅为 watch presence connected/disconnected，无控制事件。这些 PASS 不覆盖未部署 AudioWorklet 候选的两个 P0。
- Safari 仍被系统密码锁定，未绕过；AdsPower/SunBrowser 未使用。Chrome 测试后所用用户标签已恢复原赛事大厅首页。
- 当前决策：生产继续 `LIGHTTTS_STREAMING_ENABLED=false`、`LIGHTTTS_MAX_ACTIVE=1`，完整 WAV/HTMLAudio 回退保持不变。只有关闭两个 P0、修复计时/流控/手势/重启问题，并在独立 GPU 完成 active=2/3 与 Chrome/Safari 真实播放后，才允许 QA 房灰度。

### Iteration 21：参赛者退出、跨房占用与 ASR 修正前置链路

- 时间：2026-07-17 18:27–18:44 CST。
- Computer Use：Chrome 新建隔离临时标签，从首页 1v1 创建窗口填写唯一 QA 辩题并选择正方1辩。生产返回“你已在房间 #278571 参赛”，没有创建房间；`/me` 仍只有 278571，公开 live rooms marker 为 0。测试标签随后关闭，用户原 Chrome 页面未被改写；SunBrowser 未使用。
- 退出保护：从冲突提示返回 `/rooms/278571/debate`，临时标签正确识别“其他设备已接管”。顶部“退出比赛页面”打开两步确认，明确比赛继续/席位保留/AI 可能接替；只点击取消，没有 takeover、retry 或比赛控制写入。证据 TC-990/991。
- 新 P1（账号锁死）：跨房检查包含 paused 且拦截 human；release-seat 只允许 lobby；非房主无 terminate；退出页面不改变席位；离线 AI 接替又排除 paused。非房主一旦被遗留在暂停/异常房，房主不再返回时可无限期无法创建或加入新比赛。房 278571 在长期 paused、connected=false 后仍为 human，生产直接复现。
- 新 P1（恢复冲突）：AI 接替保留 user_id 并解除 human 占用，用户可进入新房；但 system admin 恢复旧席位没有 participant lock 或跨房 assignment 复验，可能把同一用户恢复成两个活动房的 human。需在 restore 路径用相同不变量与并发锁。
- 最小产品语义：保留可逆的“退出页面”；新增独立不可逆“退出本场并由 AI 接替”。非房主可在 preparing/running/paused/judging 使用，两步确认；活跃发言/待提交录音时 409；原子 human→ai_substitute，保留 user_id/历史/积分归属，清 lease 并写幂等审计事件。房主仍通过终止整场或保留控制权，不能把两种动作混合。
- ASR E2E：未执行生产真人语音写入。当前账号被真实房占用且无其他 Chrome QA 学生会话；没有释放/终止真实房，也没有创建需用户确认的新账号。本地 DebateStage 74 passed、三组件 93 passed、API 生命周期 4 passed，确认 clean ASR 可强制人工核对、finish/audio 幂等、人工文本替换唯一 final segment 并保留同一 speech 音频，但这些不能代替生产麦克风证据。
- QA P2：`authenticated-lobby.spec.ts` 仍使用旧文案“立即参赛/创建房间”，默认被 `E2E_MUTATING` 跳过，且未覆盖 start、真人发言、两种结束路径、退出/返回。相关单测全绿不能证明双用户生产链。
- 完整证据：[Iteration 21 参赛者退出与 ASR 审计](audio/20260717-005403/iteration21-participant-exit-asr-production-audit.md)。

### Iteration 22：Chrome 精确 390×844 学生核心页面回归

- 时间：2026-07-17 18:44–18:58 CST；仅使用 Chrome，不修改代码、不部署、不创建房间、不使用 AdsPower/SunBrowser，也未绕过 Safari 系统密码锁。
- 精确视口：最终取证为 `window.innerWidth/innerHeight=390×844`、DPR 1、`visualViewport=390×844`、文档 `clientWidth=scrollWidth=390`。TC-995–998 是视口切换稳定前的 375×812 内容截图；TC-999–1001 为精确 390×844。
- PASS：首页和学生创建入口、1v1 创建窗口、个人中心、辩论舞台、比赛操作面板、退出二次确认均可达且未见横向溢出。舞台核心退出/声音/全屏/操作为 44×44，面板提前结束与退出页面为 320×44，退出确认两个按钮均高 44。
- 新 P2：移动全局导航和少数恢复控件未达到项目 44×44 CSS px 目标。实测菜单 36×36、账号 90×37、退出 35×35、导航 317×39、创建窗关闭约 42×42、异常重试 328×40、操作面板关闭约 27.7×42、设备接管 147×38。当前没有目标重叠或流程阻断，统一作为一个系统性 MOBILE/ACCESSIBILITY P2，不升级为 P1。
- 安全对账：只打开设置和退出确认，并取消退出；未点击重试、接管、暂停、恢复、提前结束或确认退出。Chrome warning/error console 为 0。
- 生产只读复核：18:58:24 CST 房 278571 仍为 `paused / remaining 172 / seq 108 / active_speech=false`；本轮仅新增 seq 107 `presence.connected` 与 seq 108 `presence.disconnected`，没有 control、retry、takeover、terminate、speech 或 provider 事件。测试结束后 aff_3 `connected=false`，比赛状态未变化。
- 完整证据：[Iteration 22 Chrome 移动端回归](audio/20260717-005403/iteration22-chrome-390x844-mobile-regression.md)；截图 TC-995–1001。

### Iteration 23：移动结果页与弱网完整 WAV 回归

- 时间：2026-07-17 19:02–19:22 CST；仅使用 Chrome，不修改代码、不部署、不创建房间、不调用真实 TTS。
- 视口边界：Chrome override 与页面 `innerWidth/innerHeight` 为 390×844；长页面因 15px 垂直滚动条，实际布局/visual viewport 宽 375px。截图接口保存 375×812 内容图，文件名保留测试目标 390×844，但报告不把 PNG 像素误写为精确视口。
- 移动赛果 PASS：1v1/4v4 顶部、长裁判理由、积分轨迹、八席评分数据、9/11 条录音和长逐字稿均无横向溢出；返回个人中心/大厅按钮高 46px。1v1 时间线从 50/60 到 60/60，4v4 从 50/93 到 93/93；内层滚动、外层页面、加载按钮和 footer 均可达，加载后保持末尾位置。
- 音频生命周期 PASS：40.84s WAV 正常播放；切换至 25.72s WAV 后前一条暂停；在 `currentTime=0.706s` 时离页，返回后 9 条全部 `paused=true/currentTime=0/readyState=0`。console warning/error 为 0。
- 弱网 PASS：标签页级 Fast 3G（150ms/200000Bps）禁缓存时，1.96MB WAV 1.926s 首次 `currentTime>0`；Slow 3G（400ms/50000Bps）为 5.787s。媒体使用 `Range: bytes=0-` 和续段 `bytes=130347-`，均 206、Content-Range/Accept-Ranges 正确。测试后限速、禁缓存与视口覆盖均已恢复。
- 新 P2 1：结果页原生 audio 固定约 240×30px，1v1/4v4 重复 9/11 次，低于项目 44px 触控目标。
- 新 P2 2：4v4“个人评分”标题与右侧长说明在 375px 布局宽度互相挤压，标题拆行并视觉交错；评分数据本身完整。
- 新 P2 3：Slow 3G 下 A→B 间隔约 250ms，A 没有播放且 B 最终保持选择，但 B 首次推进延长到约 10.447s；A 在 9.8s 时已 `readyState=3`，旧 pending 下载与最终选择竞争有限带宽。
- 既有舞台弱网 P1 保持：服务端完整 WAV 计时不等待浏览器实际出声，前端在 waiting/stalled 恢复后不重新追赶权威时间轴，活动舞台发生缓冲时仍可能尾部截断。本轮只测试 completed 结果页，不能关闭该 P1。
- 新鲜首页标签确认 hero、4v4 卡片和直播行均为“4v4 人机辩论正式赛”，1v1 训练赛保留；旧用户标签中的“日常赛”是未刷新旧 DOM，不是当前生产响应。
- 生产对账：19:21:55 CST 房 278571 仍为 `paused / seq 108 / remaining 172 / aff_3 connected=false`，相对基线无新增事件；566139/764886 保持 completed seq60/93。API stderr 64537 bytes、Web stderr 0，均零增长，无任何 control 写入。
- 完整证据：[Iteration 23 移动结果页与弱网音频](audio/20260717-005403/iteration23-mobile-result-weak-network-audio.md)；截图 TC-1002–1015。

### Iteration 24：跨标签音频、键盘 Seek 与极窄响应式

### TC-1016：不同结果页标签可同时播放音频

- 状态：FAIL
- 问题类型：UX
- 严重程度：P2
- 用户影响：观众、移动端
- 发生概率：中
- 页面/路由：`/rooms/566139/result`、`/rooms/764886/result`
- 测试身份：已登录普通用户
- 前置条件：两个结果页分别位于 Chrome 标签页。
- 操作步骤：1. 在第一个标签播放结果音频；2. 切换第二个标签并播放另一条音频；3. 返回核对两个媒体状态。
- 预期结果：全浏览器会话最多一条比赛音频播放，或新标签明确提示并接管。
- 实际结果：两个标签可同时播放；现有互斥只在单页 React 生命周期内生效。
- 证据：[房 566139](screenshots/20260717-005403/TC-1016A-Iteration24-Chrome-房566139跨标签播放中.png)、[房 764886](screenshots/20260717-005403/TC-1016B-Iteration24-Chrome-房764886跨标签播放中.png)
- 控制台/网络现象：无页面崩溃。
- 复现稳定性：必现
- 操作成本：4 次点击、1 次标签切换，无明显等待。
- 根因推测：推测互斥状态仅保存在单个页面实例，没有 BroadcastChannel/Media Session 级协调。
- 建议方案：以 BroadcastChannel 发布播放 ownership，新播放开始时让其他标签 pause、clear ownership。
- 验收标准：同一浏览器 3 个结果/舞台标签连续播放时始终只有 1 个 `paused=false`。
- 修复状态：OPEN
- 关联改动/提交说明：无

### TC-1017：结果音频支持键盘 Seek 至末尾

- 状态：PASS
- 问题类型：无
- 严重程度：无
- 用户影响：辅助技术用户、观众
- 发生概率：高
- 页面/路由：`/rooms/764886/result`
- 测试身份：已登录普通用户
- 前置条件：结果 WAV 已加载。
- 操作步骤：1. 聚焦原生音频控件；2. 使用键盘推进；3. 核对当前时间与总时长。
- 预期结果：键盘可操作时间轴并到达末尾。
- 实际结果：可 Seek 至 `0:55 / 0:55`。
- 证据：[TC-1017](screenshots/20260717-005403/TC-1017-Iteration24-Chrome-结果音频键盘Seek至末尾.png)
- 控制台/网络现象：未见错误。
- 复现稳定性：必现
- 操作成本：1 次聚焦、多次按键，无无反馈等待。
- 根因推测：不适用
- 建议方案：保留原生键盘语义；后续自定义控件不得退化。
- 验收标准：Tab 可聚焦，方向键/快捷键可改变 currentTime，并有可读时间反馈。
- 修复状态：VERIFIED
- 关联改动/提交说明：无

### TC-1018：约 195 CSS px 等效极窄宽度出现横向溢出

- 状态：FAIL
- 问题类型：VIS / A11Y
- 严重程度：P2
- 用户影响：移动端、低视力用户
- 发生概率：低
- 页面/路由：`/rooms/764886/result`
- 测试身份：已登录普通用户
- 前置条件：390 物理宽页面应用等效 200% 缩放，约 195 CSS px。
- 操作步骤：1. 应用等效极窄视口；2. 查看赛果顶部与主卡；3. 检查横向溢出和可读性。
- 预期结果：主要信息重排，页面不依赖横向滚动才能读取核心结论。
- 实际结果：顶栏和主卡明显横向溢出，内容被压成不可用窄列。
- 证据：[TC-1018](screenshots/20260717-005403/TC-1018-Iteration24-Chrome-195px等效200缩放赛果顶部.png)
- 控制台/网络现象：未见脚本错误。
- 复现稳定性：必现
- 操作成本：一次视口覆盖，无等待。
- 根因推测：推测多个固定最小宽度、不可换行操作和卡片 padding 的总和超过极窄容器。
- 建议方案：在 `max-width:240px` 下隐藏次要装饰、让动作纵向堆叠、允许比分和标题独立换行。
- 验收标准：约 200 CSS px 下 `scrollWidth <= clientWidth`，胜负、比分、返回入口可读可操作。
- 修复状态：OPEN
- 关联改动/提交说明：无

### TC-1019：320×844 结果页核心内容正常重排

- 状态：PASS
- 问题类型：无
- 严重程度：无
- 用户影响：移动端
- 发生概率：中
- 页面/路由：`/rooms/764886/result`
- 测试身份：已登录普通用户
- 前置条件：Chrome 320×844。
- 操作步骤：1. 打开结果页顶部；2. 检查胜负、比分、理由和导航；3. 纵向滚动。
- 预期结果：核心内容无横向溢出，按钮可达。
- 实际结果：顶部正确重排，核心按钮和内容均可达。
- 证据：[TC-1019](screenshots/20260717-005403/TC-1019-Iteration24-Chrome-320x844赛果响应式.png)
- 控制台/网络现象：未见错误。
- 复现稳定性：必现
- 操作成本：1 次打开、1 次滚动，无明显等待。
- 根因推测：不适用
- 建议方案：保持 320px 基线回归。
- 验收标准：320×844 无横向滚动，主要按钮不少于项目触控目标。
- 修复状态：VERIFIED
- 关联改动/提交说明：无

### TC-1020：320px 下个人评分标题与长说明仍挤压

- 状态：FAIL
- 问题类型：VIS
- 严重程度：P2
- 用户影响：移动端
- 发生概率：高
- 页面/路由：`/rooms/764886/result`
- 测试身份：已登录普通用户
- 前置条件：Chrome 320×844，滚动到个人评分区。
- 操作步骤：1. 定位个人评分标题；2. 检查标题与右侧说明；3. 核对评分数据。
- 预期结果：标题与说明建立清晰层级，不互相挤压。
- 实际结果：标题与长说明错行挤压；评分数据仍完整。该项复现 TC-1010，不重复计算开放缺陷。
- 证据：[TC-1020](screenshots/20260717-005403/TC-1020-Iteration24-Chrome-320x844-个人评分标题挤压.png)
- 控制台/网络现象：未见错误。
- 复现稳定性：必现
- 操作成本：1 次滚动，无等待。
- 根因推测：推测标题行仍使用横向 flex 且说明缺少窄屏独占行规则。
- 建议方案：小于 480px 时改为纵向 header，说明置于标题下方并限制行宽。
- 验收标准：320/375/390px 标题不拆成孤字，不与说明交叠。
- 修复状态：OPEN
- 关联改动/提交说明：关联 TC-1010

### Iteration 25：核心流修复、生产发布与双浏览器控制回归

### TC-1021：发布后赛事大厅正式赛与训练赛入口正常

- 状态：PASS
- 问题类型：无
- 严重程度：无
- 用户影响：全体用户
- 发生概率：高
- 页面/路由：`/`
- 测试身份：Chrome 既有登录用户
- 前置条件：release `20260717T2115-core-flow-stream-safety` 已上线。
- 操作步骤：1. 打开首页；2. 核对登录态、赛事类型和进行中数量；3. 检查主入口。
- 预期结果：4v4 正式赛与 1v1 训练赛清晰可见，页面可用。
- 实际结果：4v4 人机辩论正式赛、1v1 训练赛正常，0 场进行中，无白屏。
- 证据：[TC-1021](screenshots/20260717-005403/TC-1021-Iteration25-Chrome-发布后4v4首页.png)
- 控制台/网络现象：无 warning/error。
- 复现稳定性：必现
- 操作成本：1 次打开、1 次明显加载。
- 根因推测：不适用
- 建议方案：保持主链优先，次要榜单/直播故障不得阻塞参赛。
- 验收标准：首页核心赛事接口成功时，创建/加入入口始终可用。
- 修复状态：VERIFIED
- 关联改动/提交说明：Iteration 25 release

### TC-1022：4v4 创建弹窗八席与 AI 补位信息完整

- 状态：PASS
- 问题类型：无
- 严重程度：无
- 用户影响：学生、房主
- 发生概率：高
- 页面/路由：`/`
- 测试身份：Chrome 既有登录用户
- 前置条件：首页正常。
- 操作步骤：1. 点击创建 4v4；2. 查看辩题、8 席和补位说明；3. 关闭而不提交。
- 预期结果：用户提交前理解正式赛结构；取消不创建数据。
- 实际结果：8 席、辩题和 AI 补位说明完整；未提交、未创建房间。
- 证据：[TC-1022](screenshots/20260717-005403/TC-1022-Iteration25-Chrome-4v4创建弹窗未提交.png)
- 控制台/网络现象：无错误请求。
- 复现稳定性：必现
- 操作成本：1 次点击、0 次输入、无等待。
- 根因推测：不适用
- 建议方案：后续继续以学生可理解文案表达 AI 补位，不暴露编排术语。
- 验收标准：未提交关闭后数据库无新 Room/Match。
- 修复状态：VERIFIED
- 关联改动/提交说明：无生产写入

### TC-1023：登录与注册互转保留房间精确回跳

- 状态：PASS
- 问题类型：无
- 严重程度：无
- 用户影响：新用户、参赛者
- 发生概率：高
- 页面/路由：`/login?next=/rooms/381526/lobby`、`/register`
- 测试身份：未登录状态页
- 前置条件：安全相对路径 `next=/rooms/381526/lobby`。
- 操作步骤：1. 打开带 next 的登录页；2. 点击立即注册；3. 检查返回登录链接。
- 预期结果：登录↔注册互转不丢邀请房间意图。
- 实际结果：注册链接和返回登录链接均保留精确安全 next。
- 证据：[TC-1023](screenshots/20260717-005403/TC-1023-Iteration25-Chrome-登录注册保留房间回跳.png)
- 控制台/网络现象：DOM href 精确核对通过。
- 复现稳定性：必现
- 操作成本：2 次点击、0 次重复输入。
- 根因推测：不适用
- 建议方案：持续对 next 做站内相对路径白名单校验。
- 验收标准：登录、注册、互转和成功认证后最终落到原 lobby；外部 URL 被拒绝。
- 修复状态：VERIFIED
- 关联改动/提交说明：`auth-form.tsx`、匿名参与回跳链

### TC-1024：发布后 4v4 赛果、八席评分和时间线完整

- 状态：PASS
- 问题类型：无
- 严重程度：无
- 用户影响：参赛者、观众
- 发生概率：高
- 页面/路由：`/rooms/764886/result`
- 测试身份：Chrome 既有登录用户
- 前置条件：房 764886 已完成。
- 操作步骤：1. 打开结果；2. 核对比分和发言/事件数；3. 检查八席评分、逐字稿与录音；4. 读取控制台日志。
- 预期结果：完整呈现 4v4 结果与可追溯证据。
- 实际结果：正方胜 82:76，11 条发言、93 个事件、8 席个人分；User B 为“暂无评分”；逐字稿和录音完整。
- 证据：[TC-1024](screenshots/20260717-005403/TC-1024-Iteration25-Chrome-发布后4v4赛果完整.png)
- 控制台/网络现象：warning/error `[]`。
- 复现稳定性：必现
- 操作成本：1 次打开、数次滚动，1 次明显加载。
- 根因推测：不适用
- 建议方案：保留八席完整性和缺席评分显式状态。
- 验收标准：比分、winner、11 speeches、93 events、8 scores 与 API 数据一致。
- 修复状态：VERIFIED
- 关联改动/提交说明：Iteration 25 production regression

### TC-1025：Computer Use 独立复核 4v4 完整赛果

- 状态：PASS
- 问题类型：无
- 严重程度：无
- 用户影响：参赛者、观众
- 发生概率：高
- 页面/路由：`/rooms/764886/result`
- 测试身份：Chrome 既有登录用户
- 前置条件：Computer Use 已连接本地 Chrome，未使用 AdsPower/SunBrowser。
- 操作步骤：1. 读取 AX 树；2. 核对胜负、比分、发言、事件和个人分；3. 保存截图。
- 预期结果：通过真实桌面 UI 仍能读取完整赛果。
- 实际结果：AX 树确认 82:76、11 发言、93 事件和八席评分。
- 证据：[TC-1025](screenshots/20260717-005403/TC-1025-Iteration25-ComputerUse-Chrome-4v4赛果.png)
- 控制台/网络现象：Computer Use 不提供 DevTools；Chrome 前项为 0 错误。
- 复现稳定性：必现
- 操作成本：1 次标签切换、1 次 AX 读取。
- 根因推测：不适用
- 建议方案：保留语义化标题、列表和按钮，便于辅助技术读取。
- 验收标准：AX 树包含胜负、比分、完整计数和主要导航。
- 修复状态：VERIFIED
- 关联改动/提交说明：Computer Use production regression

### TC-1026：Computer Use 从赛果返回赛事大厅

- 状态：PASS
- 问题类型：无
- 严重程度：无
- 用户影响：全体用户
- 发生概率：高
- 页面/路由：`/`
- 测试身份：Chrome 既有登录用户
- 前置条件：位于房 764886 赛果。
- 操作步骤：1. 用 Computer Use 点击赛事大厅；2. 读取首页 AX 树；3. 核对赛事类型和活动数量。
- 预期结果：返回路径清晰，首页核心入口正常。
- 实际结果：4v4 正式赛、1v1 训练赛正常，0 场活动赛。
- 证据：[TC-1026](screenshots/20260717-005403/TC-1026-Iteration25-ComputerUse-Chrome-发布后赛事大厅.png)
- 控制台/网络现象：未获取；Chrome 自动化回归无 warning/error。
- 复现稳定性：必现
- 操作成本：1 次点击、1 次页面加载。
- 根因推测：不适用
- 建议方案：保持赛果到大厅的一步返回路径。
- 验收标准：点击后 URL 为首页且两类赛事入口可操作。
- 修复状态：VERIFIED
- 关联改动/提交说明：Computer Use production regression

### Iteration 25 实现、部署和性能摘要

- 流式音频：服务端按 growing WAV 真实可用样本把 future/equal live-edge 请求回退最多 400ms；`audio.start` 返回权威 seq/PTS，客户端据此重置连续性；Worklet `end` 会 prime 并 drain 低于阈值的最终尾音。
- 比赛流程：watch 严格只读，房主转显式 control，retry 二次确认；非房主可“放弃本场并由 AI 接替”；管理员恢复需 participant lock 与跨房真人复验；历史空 `service_snapshot` retry 返回 409。
- 认证流程：登录/注册保留安全 next；匿名邀请、房号加入和创建赛事意图可回跳；被 AI 接替的原参赛者进入只读 watch。
- 质量门：Web 全量 26 files / 190 tests；API 全量 295 passed、22 warnings；平台+并发模拟 160/160；Next production build 通过。
- 部署：第一次 `20260717T2110` 因 harness 把正常 Uvicorn 终止日志误判而在切换前安全回滚；第二次 `20260717T2115-core-flow-stream-safety` 成功，schema 0019，ready 5/5，四服务健康，API/Engine/Web stderr delta 0。详细回滚点见[Iteration 25 证据](audio/20260717-005403/iteration25-core-flow-stream-safety.md)。
- Agent 实测：Prompt 准备 44.5ms；流对象打开 1,975.5ms；首个非空内容 21,173.2ms；完整 22,446.7ms。上游首内容是当前最核心性能瓶颈，仍为 P1。
- LightTTS 既有真实 1/2/3 路结果：单路首块 P95 1.259s、RTF 0.878；2 路 6.333s/1.795；3 路 10.983s/2.651，2–3 场实时仍 NO-GO。生产流式 flag 保持关闭。

### INCIDENT-001：房 278571 观战页误触重试

- 事实：坐标型浏览器测试 `/rooms/278571/watch` 时误触房主“重试异常步骤”，产生非预期生产写操作；随后该场被终止，最终 `terminated / seq 115`。
- 故障链：retry 后 Agent 返回 400；历史 Match 的 `service_snapshot={}`，旧代码回退到陈旧环境 Agent endpoint 且缺 gateway secret。
- 纠正：watch 已严格只读，只提供显式 control 链接；retry 增加确认；空 snapshot 的生产 retry 返回 409；本任务不再操作该房。
- 说明：报告早期“paused / seq108 / 未操作”是当时截点，不代表最终状态。该事故单列，不另增 TC 统计。

### Iteration 26：Debate Agent 首内容快速路径

### TC-1027：Qwen3 辩手关闭默认深度思考后首内容显著加速

- 状态：PASS
- 问题类型：PERF
- 严重程度：无
- 用户影响：全体参赛者、观众
- 发生概率：高
- 页面/路由：`POST /debate/api/debate` SSE
- 测试身份：专用 QA Gateway canary
- 前置条件：Agent release `20260717T2140-agent-fastpath` 已完成备份、shadow 和单 API 切换；主 平台 无活动比赛，Agent running task=0。
- 操作步骤：1. 用唯一 QA task id 提交真实辩手请求；2. 记录 HTTP stream open、首个非空 delta、完整耗时、chunk/字符数；3. 检查 `[DONE]` 与 think 泄露；4. 核对数据库 task、服务健康和活动比赛。
- 预期结果：Qwen3 辩手使用服务端 preset 的 `enable_thinking=false`，首内容显著低于 21 秒基线，SSE 合约和发言内容正常。
- 实际结果：open 160.4ms，首 delta 3375.9ms，总耗时 4673.6ms，30 chunks、129 字，`DONE=true`、`think_leak=false`。首内容较 21173.2ms 基线下降 84.1%，约快 6.27 倍；总耗时下降 79.2%，约快 4.8 倍。
- 证据：[Iteration 26 发布证据](audio/20260717-005403/iteration26-agent-fastpath-deployment.md)、[canary JSON](audio/20260717-005403/iteration26-agent-fastpath-canary.json)
- 控制台/网络现象：Agent 本机/公网 health 5/5；stderr 仅新增 Alembic INFO，无 ERROR/Traceback/CRITICAL。
- 复现稳定性：本轮生产 canary 1/1；发布前无状态 A/B 方向一致。
- 操作成本：1 个 QA SSE 请求，首可见等待 3.38 秒，完整等待 4.67 秒。
- 根因推测：已证实默认模型把 281/303 流块用于 reasoning，主可见正文被推迟；不是本地 Prompt 准备瓶颈。
- 建议方案：保持辩手 fast path；裁判继续独立策略。下一步把 Agent 正文 delta 安全切分后直接喂入 LightTTS bi-stream，并补 2/3 路真实并发。
- 验收标准：连续样本首正文 P95 明显低于旧 20+ 秒基线，无 think 泄露、无观点质量显著退化；Judge/fallback 不受影响。
- 修复状态：VERIFIED
- 关联改动/提交说明：`debate-agent/apps/api/app/agent_engine.py`、`seed.py`；release `20260717T2140-agent-fastpath`

Iteration 26 部署只替换 Agent API 的 `agent_engine.py` 与 `seed.py`，只重启 `jixia-agent-api`；Agent Web、主 平台、Nginx、环境变量和 schema 未修改。源码回滚包：`/home/ubuntu/sunsq/debate-agent/runtime/deploy-backups/20260717T2140-agent-fastpath-before-source.tar.gz`，SHA-256 `881ccff47d6ddcbaf03977c15bf0b7d20d43d5f4094add6fa392a9d1bf841f43`。Agent DB 备份：`/home/ubuntu/sunsq/debate-agent/backups/agent-20260717T134123Z.dump`，SHA-256 `2594622e395481d24a40cfe8f613a4621b722dd1cfe218425bfd5f94e5ddcf15`。

### Iteration 27：OpenMOSS/CosyVoice 实时语音架构

- 只换 TTS 无法达到 3 秒：当前 Engine 等 Agent 完整输出 4.674s 后才启动 TTS；加 LightTTS 首 PCM P95 1.259s 和浏览器 400ms 缓冲，理论首声约 6.333s。
- OpenMOSS 首选为 MOSS-TTS-Realtime：官方暖机 TTFB 180ms、RTF 0.51、中文 CER 1.07%，原生支持 Agent text delta 与多轮音色一致；但官方配方约需 talker 6GB + codec 8GB，当前 12GB GPU 无法安全运行。
- 当前服务器主 canary 候选改为 CosyVoice3 Base 0.5B + Triton/TensorRT：官方 L20 四并发首块 P95 977.55ms，中文 CER 1.21%、SIM 78.0；必须在 3080 Ti 上实测，不能照搬官方数字。
- MOSS-TTS-Nano 不进入生产主线：官方 issue #58/#60/#81/#87 仍报告吞句、短句重复、语速不均和标点丢句，直接违反硬门。生产预检又发现真实房 433825 正在运行，因此安全停止了隔离安装，没有下载/启动模型或改变服务。
- 本地已新增 Agent delta 流、可朗读片段组装和通用 raw-PCM 三路 benchmark。Provider 全量 59 passed；stream/assembler/benchmark 定向组合 10 passed；Ruff、py_compile 通过。尚未接入 MatchEngine，生产行为不变。
- 完整证据：[Iteration 27 OpenMOSS 实时架构](audio/20260717-005403/iteration27-openmoss-realtime-tts-architecture.md)、[MOSS Nano 安全预检](audio/20260717-005403/moss-nano-canary/README.md)。

### Iteration 28：Agent 尾延迟与连续 TTS session

- 7 次无业务写入的生产同配置 Agent 增量基准：首 delta P50/P95/max = 1.460/4.935/6.186s；12 字 = 1.666/5.112/6.438s；首强标点可朗读子句 = 1.820/5.112/6.438s；full = 3.123/6.982/8.248s。Agent 文本 P95 单独已超 5s，3 秒端到端目标仍为 NO-GO。证据：[Agent delta latency](audio/20260717-005403/agent-delta-latency/README.md)。
- A/B/C 每组 6 样本进一步排除建连主瓶颈：新 client/持久 keep-alive/3 路并发首可播 P95 分别 2.519/3.992/3.397s；TCP connect P95 仅 16.6/13.2ms，多路复用连接时 HTTP open 仍同时约 2.420s。连接池/预热不能把 P95 压到 1.25s，P0 是上游响应/推理排队。证据：[transport root cause](audio/20260717-005403/agent-delta-latency/agent-transport-root-cause/README.md)。
- 本地已将 `DebateAgentProvider` 改为进程级持久 keep-alive 连接池，避免每个 AI 回合新建 `AsyncClient`；应用关闭时显式释放。A/B/C 已证明它只是低成本降噪，不是 P95 主修复。
- 本地已新增真正动态喂入的 `LightTTSProvider.open_incremental_session()`：Agent 子句在 final 前进入同一条 websocket，共用一个 prompt、admission lease、GPU slot、codec/音色上下文和最终 WAV；禁止退化为每句独立 HTTP 合成。
- Provider 全量 61 passed；新测试确认首子句在 `finish` 前发送、两子句只用一条 bi-stream 连接并原子生成同一 WAV；Ruff、py_compile 通过。MatchEngine 已接入新分支，并有端到端单测证明 Agent final 未到达时已建立音频 generation；独立 kill switch 默认关闭，未部署，生产行为不变。
- 新流水线增加 200ms 首可播软超时，不等无标点 Agent 无限继续输出；不取消正在等待的 SSE `anext()`，不切断英文/数字 token。TTS 中途失败时仍会排空 Agent final 并存储完整逐字稿，供 TTS-only retry 复用。
- 本地音频 WebSocket/AudioWorklet 已增加 buffered/played 反压：首批限 800ms，反馈后每批限 320ms，高水位 1.2s，避免快于实时的 TTS 爆发写满 3s ring buffer。API 流控定向 2 passed；Web player/worklet 2 files / 7 tests passed。未部署。
- 本地统一回归：API `313 passed, 22 warnings`；Web `26 files / 190 tests passed`；Next.js production build 通过。
- 生产 TTS 已确认是 `Fun-CosyVoice3-0.5B-2512 + LightTTS/TRT`。同卡官方 PyTorch/gRPC 副本在 CPU tokenizer 模式下 warmup 仍 OOM，安全门阻止了单路/三路/CER 强跑；878 次 watchdog 无真实比赛，收尾后 GPU/端口/进程完全恢复。证据：[CosyVoice3 canary](audio/20260717-005403/cosyvoice3-canary/README.md)。
- 生产 bi-stream 首个 10 字两段请求 80s 仍 0 PCM，停在 LLM prefill 后且断开无法 abort，形成占满唯一 slot 的 orphan，ready 仍假绿。无活动比赛下只重启 `jixia-lighttts` 恢复；恢复后 ready 5/5、8080/OpenAPI 正常、GPU 回基线、req246=0、其他服务未重启。本地增加独立 `lighttts_bistream_enabled=false` 安全门，未修复 orphan/abort 前不可启用。证据：[bi-stream short clause](audio/20260717-005403/production-bistream-short-clause/README.md)。

### Iteration 29：MOSS 原生 session 候选与安全分片

- 固定源码审计确认 vLLM-Omni `/v1/audio/speech/stream` 会缓存全部 `input.text`，直到 `input.done` 才创建 engine request；它不是 Agent delta 直连，核心路径判定 NO-GO。证据：[vLLM-Omni session audit](audio/20260717-005403/vllm-omni-session-adapter-audit.md)。
- OpenMOSS 原生 session 才是真增量：`push_text` 达 prefill 阈值即可出帧。本地新增默认关闭的 `moss_realtime` Provider，使用同一 turn 的 start/push/持续 PCM/close，首 PCM 进入现有 growing-WAV 浏览器链，完成后校验并原子发布最终 WAV。
- 取消/暂停/deadline 路径会删除半成品并保证尝试 close；三路 benchmark 把 close ACK 100% 纳入硬门。音色使用服务端固定 prompt 文件，同一发言不重建 voice context。
- 官方示例共享同一 model/codec，`codec.streaming(batch_size=1)` 且无全局调度；每 session 一个线程不能证明单实例 2–3 路安全。因此本地默认每 endpoint 仅 1 active，通过 `MOSS_TTS_REALTIME_URLS` 把并发分片到独立 endpoint。fake 双 endpoint 已验证两场同时启动时分别落到不同实例。
- 新增 `scripts/benchmark_moss_realtime_sessions.py`：真实独立 GPU 到位后直接测原生协议 1/2/3 路首 PCM、RTF、块间隔、失败率、close ACK、WAV 与 endpoint 分片；三路门为首 PCM P95≤800ms、RTF P95≤0.65、chunk-gap P99≤200ms、零失败、close ACK 100%。
- LightTTS lifecycle 只读审计确认：API 可补 deadline、disconnect watcher、guaranteed abort 与内部 readiness；若 stuck model RPC 在 abort grace 内不释放，只能 fail-fast 重启完整 LightTTS 进程组。证据：[lifecycle fix audit](audio/20260717-005403/lighttts-bistream-lifecycle-fix-audit.md)。
- 本轮定向 `79 passed`；API 全量 `322 passed, 22 warnings`；Ruff/py_compile 通过。MOSS endpoint 未部署、实时 flag 未开启、生产行为未改变；没有独立 GPU 就不宣称 TTFB/CER/音色/三路容量通过。

### Iteration 30：Agent 原始 SSE 低延迟主路径发布

- 平台 数据库的权威 Agent Provider 已确认指向本机 `https://117.50.218.251/debate/api/debate` 并带加密 gateway secret；`.env` 中的 `47.93` 只是未被使用的环境 fallback。
- 同生产 Prompt、同 gateway、同 `qwen-plus` 三路对照：原始 OpenAI SSE 首 10 字 P95 `1.675s`；LiteLLM `acompletion(stream=True)` 为 `3.374s`；旧 Agent API 为 `3.451s`。Redis 新连接 8 次总计仅 `7.2ms`，不能解释秒级差值。
- 已发布辩手原始 SSE parser；`reasoning_content` 不转发，`enable_thinking=false` 仍由服务端强制。Judge/非流式继续使用 LiteLLM。HTTP/Redis 改为进程级连接；interrupt 100ms 有界轮询；已输出部分正文后禁止切换 Provider 拼接第二段文本。
- Iteration 30 发布时曾灰度 `qwen3.6-flash`。当时三路两波 6 请求的首正文 P95 `2.352s`、首 10 字 P50/P95/max `1.876/2.398/2.466s`、完整 P95 `3.985s`；6/6 completed、无 error、无 think。该组现仅作为历史 canary，不代表当前生产模型。
- 当前主模型为 `qwen-plus`。2026-07-18 02:01 CST 的 10 波×3 并发 post-warm 复测：首正文 P50/P95/max `1.067/2.779/2.962s`，首 10 字 `1.404/3.271/3.272s`，完整响应 `2.585/4.233/4.347s`；30/30 completed、thinking 泄露为 0。第 1 波三路共同出现空闲冷态；波 2–10 的 27 个热态样本首正文 P95/max 为 `1.485/2.076s`。证据：[Agent post-warm 30](audio/20260717-005403/agent-postwarm-30/README.md)。
- 该复测从 LLM 请求开始计时，并非“LLM 首字→Chrome/Safari 实际出声”；不能据此宣称浏览器 2.5 秒首声通过，核心语音仍不能标记 PASS。
- Agent 自动化 `12 passed, 1 warning`；Ruff/py_compile 通过。只重启 `jixia-agent-api`，平台 API/Engine/Worker/Web 未重启；Agent/平台 health 均 ready，`active_match_processing=false`。
- 证据与回滚：[Iteration 30 Agent direct SSE](audio/20260717-005403/iteration30-agent-direct-sse-fastpath.md)。DB 备份 `agent-20260717T161955Z.dump`，源码包 `20260717T163004Z-before-direct-sse.tar.gz`，旧模型 `qwen3.7-plus` 可立即恢复。

### Iteration 31：LiveKit WebRTC 正式下行发布与浏览器闭环

- 已部署 LiveKit Server 1.13.3、Engine 每房间长驻 `agent-tts` publisher、subscribe-only token、48kHz/20ms 连续时钟和浏览器 LiveKit 订阅；实时 ASR 使用 AudioWorklet，MediaRecorder 只保留赛后归档。
- Chrome 首轮发现 `LIVEKIT_PUBLIC_URL` 多带 `/rtc`，客户端再次追加 RTC path 后持续 `v1 RTC path not found`；生产已修正为根 `wss://117.50.218.251`。
- 进入真实协商后 Chrome 稳定复现 `Transceiver not found based on m-line index`。证据指向 LiveKit Server 1.13.3 缺少 2026-07-16 合并的 single-PC answer 路由修复；前端显式 `singlePeerConnection:false`，未冒险部署未 release 的 main 二进制。
- publisher 取消 hidden grant，确保浏览器先看到 participant 再接收 `agent-tts` track；新增专项测试。
- Safari 在 Web release 切换后复现旧动态 chunk 404。构建脚本现把上一 release 的 immutable chunks 以 `--ignore-existing` 合入新 release；旧 `3wwitj-97ja54.js` 恢复 HTTP 200，页面自动恢复，不再显示兼容回退错误。
- Computer Use：Chrome 和 Safari 均显示“实时连接”，声音开关从“开启比赛声音”成功切到“关闭比赛声音”；LiveKit room service 同时看到 active `agent-audio:214317` publisher 与 active 浏览器 participant。双 PC 修复后无新增 conflicting ICE、DTLS timeout 或 participant restart 错误。
- 截图：[Chrome 已连接并解锁](screenshots/20260717-005403/TC-RTC-04-Chrome-WebRTC已连接并解锁.png)、[Safari 已连接并解锁](screenshots/20260717-005403/TC-RTC-05-Safari-WebRTC已连接并解锁.png)。
- Python release：`20260717T1845Z-webrtc-safe`；Web release：`20260717T1920Z-livekit-dualpc`；部署前备份：`/home/ubuntu/sunsq/phdebate/runtime/deploy-backups/20260717-183325`。
- 当前安全过渡在完整 LightTTS WAV 生成后才向 LiveKit 发布，WebRTC transport 已通过，但低延迟真双流仍不通过。按用户指令已完整读取并启动 `docs/realtime-voice-rebuild.md`，所有危险增量开关继续关闭。
- 完整证据：[Iteration 31 LiveKit WebRTC 发布](audio/20260717-005403/iteration31-livekit-webrtc-production-rollout.md)。

### TC-1028：Chrome LiveKit signaling 公开 URL 路径

- 状态：PASS
- 问题类型：BUG / PERF
- 严重程度：P1（已修复）
- 用户影响：所有辩手、观众
- 发生概率：必现
- 页面/路由：`/rooms/560402/watch`、`/rtc/v1`
- 测试身份：Chrome 既有登录用户
- 前置条件：WebRTC 前后端开关均启用。
- 操作步骤：1. 进入观战页；2. 读取浏览器 warning/error；3. 核对 LiveKit public URL；4. 修正后重新加载。
- 预期结果：客户端连接站点根并由 SDK 追加 RTC path。
- 实际结果：初始配置多带 `/rtc`，持续 `v1 RTC path not found`；修正为 `wss://117.50.218.251` 后进入真实 SDP/ICE 协商。
- 证据：[Iteration 31](audio/20260717-005403/iteration31-livekit-webrtc-production-rollout.md)
- 控制台/网络现象：修复前重复 websocket close；修复后该错误不再新增。
- 复现稳定性：修复前必现，修复后 0/多次页面重载。
- 操作成本：1 次进入、1 次配置修正、1 次 API 重启。
- 根因推测：已证实 SDK 自动追加 RTC path。
- 建议方案：public URL 永远配置 origin，不配置 SDK 内部 path。
- 验收标准：token 返回根 WSS URL，浏览器无 `v1 RTC path not found`。
- 修复状态：VERIFIED
- 关联改动/提交说明：生产 `.env`、Nginx `/rtc` prefix 保持。

### TC-1029：Chrome LiveKit 双 PeerConnection 兼容

- 状态：PASS
- 问题类型：BUG / PERF
- 严重程度：P1（已修复）
- 用户影响：Chrome 辩手、观众
- 发生概率：必现
- 页面/路由：`/rooms/214317/watch`
- 测试身份：Chrome 150 既有登录用户
- 前置条件：LiveKit Server 1.13.3、client 2.20.1。
- 操作步骤：1. 以默认 single-PC 进入；2. 记录 transceiver/ICE 错误；3. 部署 `singlePeerConnection:false`；4. 刷新并核对 SFU participant。
- 预期结果：subscriber 稳定连接并订阅 `agent-tts`。
- 实际结果：默认模式稳定失败；双 PC 后 browser participant 与 `agent-audio:214317` 均 active，声音开关可解锁。
- 证据：[Chrome 双 PC 诊断](audio/20260717-005403/livekit-chrome-transceiver-diagnosis.md)、[TC-RTC-04](screenshots/20260717-005403/TC-RTC-04-Chrome-WebRTC已连接并解锁.png)
- 控制台/网络现象：修复后无新增 transceiver/conflicting ICE/DTLS/restart 错误。
- 复现稳定性：修复前必现；修复后 Chrome Computer Use 1/1 与 LiveKit room service 1/1。
- 操作成本：1 次页面刷新、1 次声音解锁。
- 根因推测：已由 Server 版本时间线、官方 PR 与生产日志交叉证实。
- 建议方案：保持双 PC，Server 正式 release 包含 #4680 后再单独评估 single-PC。
- 验收标准：浏览器 participant active，声音开关可用，无 SDP/transceiver 错误。
- 修复状态：VERIFIED
- 关联改动/提交说明：`livekit-room-audio.ts`、client 精确锁 2.20.1。

### TC-1030：LiveKit publisher participant 与 track 顺序

- 状态：PASS
- 问题类型：BUG
- 严重程度：P1（已修复）
- 用户影响：所有 WebRTC 订阅者
- 发生概率：中
- 页面/路由：LiveKit room `debate:214317`
- 测试身份：Chrome/Safari 订阅者
- 前置条件：Engine 创建 `agent-audio:{room}` publisher。
- 操作步骤：1. 观察浏览器 track/participant 错误；2. 取消 publisher hidden grant；3. 重启 Engine；4. 列出 room participants/tracks。
- 预期结果：participant 更新先于或与 track 更新一致可用。
- 实际结果：publisher active 且发布未静音 `agent-tts`；浏览器 participant active，旧 `participant not present` 不再新增。
- 证据：[Iteration 31](audio/20260717-005403/iteration31-livekit-webrtc-production-rollout.md)
- 控制台/网络现象：修复前见 participant missing；修复后无新增。
- 复现稳定性：专项单测 1/1；生产 room service 1/1。
- 操作成本：1 次 Engine 安全重启。
- 根因推测：LiveKit hidden publisher 不适合面向普通订阅者发布正式媒体。
- 建议方案：保留普通可见 publisher，UI 不展示 LiveKit participant 清单。
- 验收标准：publisher/track 均可查询，客户端无 participant missing。
- 修复状态：VERIFIED
- 关联改动/提交说明：`livekit_audio.py`、`test_livekit_audio.py`。

### TC-1031：Safari 发布中旧动态 chunk 兼容

- 状态：PASS
- 问题类型：BUG / RELIABILITY
- 严重程度：P1（已修复）
- 用户影响：比赛中保持旧标签的用户
- 发生概率：高（每次含新 chunk 的发布）
- 页面/路由：`/_next/static/chunks/3wwitj-97ja54.js`
- 测试身份：Safari 26.1 匿名观众
- 前置条件：旧页面在 Web release 原子切换前已打开。
- 操作步骤：1. 切换 Web release；2. Safari 加载 LiveKit 动态模块；3. 观察 ChunkLoadError；4. 合并上一 release 静态文件；5. 重试。
- 预期结果：旧标签在新 release 后仍能加载其 immutable chunk。
- 实际结果：初始 404 并回退兼容播放；合并旧 chunks 后目标 URL 200，错误文案自动消失。
- 证据：[TC-RTC-05](screenshots/20260717-005403/TC-RTC-05-Safari-WebRTC已连接并解锁.png)
- 控制台/网络现象：Safari Web Inspector 未获取；页面错误文本与 HTTP 200 为权威证据。
- 复现稳定性：修复前必现，修复后 1/1。
- 操作成本：无需用户清缓存；页面自动恢复。
- 根因推测：已证实 `.web-current` 的单 release 静态根移除了旧哈希资源。
- 建议方案：所有 release 保留上一版 immutable static；定期按 release 保留策略清理。
- 验收标准：切换后上一 release 关键 chunk 仍为 200，旧标签不显示 ChunkLoadError。
- 修复状态：VERIFIED
- 关联改动/提交说明：`deploy/build-web-release.sh`。

### TC-1032：Chrome 与 Safari WebRTC 声音解锁

- 状态：PASS
- 问题类型：无
- 严重程度：无
- 用户影响：辩手、观众
- 发生概率：高
- 页面/路由：`/rooms/214317/watch`
- 测试身份：Chrome 既有登录用户、Safari 匿名观众
- 前置条件：房间 publisher 已预热，浏览器已收到实时页面。
- 操作步骤：1. 分别进入页面；2. 确认“实时连接”；3. 点击“开启比赛声音”；4. 读取开关状态并保存截图。
- 预期结果：用户手势后开关变为 on/“关闭比赛声音”，无错误提示。
- 实际结果：Chrome 与 Safari 均成功切换；SFU 中 publisher 和浏览器 participant active。
- 证据：[Chrome](screenshots/20260717-005403/TC-RTC-04-Chrome-WebRTC已连接并解锁.png)、[Safari](screenshots/20260717-005403/TC-RTC-05-Safari-WebRTC已连接并解锁.png)
- 控制台/网络现象：Chrome 修复后无新增协商错误；Safari 无可用 Web Inspector。
- 复现稳定性：Chrome 1/1、Safari 1/1。
- 操作成本：每浏览器 1 次页面进入、1 次点击。
- 根因推测：不适用
- 建议方案：保持显式声音解锁和静音状态，不依赖浏览器自动播放。
- 验收标准：两浏览器开关状态变为 on，LiveKit participant active。
- 修复状态：VERIFIED
- 关联改动/提交说明：Iteration 31 production release。

### Iteration 32：实时语音重建控制面、真实 GPU 淘汰门与浏览器门禁

- 平台 已把实时语音拆为 `services/voice_runtime/`：正文过滤与短语聚合、单 session TTS、音色映射、归档、指标、admission 和 LiveKit 各自独立；旧模块仅兼容 re-export。
- 正式短语参数改为第一块 10–16 字、后续增长到 16–28 字，强标点可立即提交；Agent final 只能修改尚未送入 TTS 的尾部，已经提交的前缀不可重写。thinking/reasoning 不进入 TTS，也不触发首正文计时。
- `agent_first_readable_delta_at` 已在本地加入 `audio.rtc.started` / `audio.stream.started`，Chrome 探针可严格计算“首正文 delta→实际非静音样本”；定向 API 21/21、最终 API 353/353 通过。
- LightTTS 隔离候选修复 first-PCM/whole timeout、append/finish 锁、abort/release、orphan/readiness、组件 heartbeat 和完整进程组恢复；全新 clone 应用补丁后 19/19 通过。
- 真实 RTX 3080 Ti canary：模型和 startup 非流式健康合成可用，但 13 字首块进入 bi-stream 后 3 秒仍为 0 PCM；watchdog 完整重启并回收 GPU。结论严格分层为 recovery PASS / synthesis NO-GO，未部署。
- 8 个 AISHELL-3 固定候选音色完成来源、授权、逐字稿、24k mono PCM16、峰值和 SHA 门；仍需 CosyVoice3 CER、20 轮漂移、盲听区分与 MOS，未替换生产音色。
- Chrome 合成连续 PCM 最终门禁：首正文→首声 446.9ms；打断 pause 0.7ms、数字静音 13.2ms、750ms 无旧 generation 复活。该结果证明浏览器媒体图能力，不代表当前 GPU TTS 已通过。
- 最终自动化：API `353 passed`；Web 单 worker `192/192`、production build PASS。默认并发 Vitest 在多任务机器上出现分散 5 秒 timeout，单文件与单 worker 均全绿，保留为测试基础设施稳定性风险。
- 04:55 CST 最终生产只读确认：平台 `/api/health/ready` 与 Agent `/debate/health` 均 `ok/ready`；schema `0019_audio_streaming`，Engine/Worker 心跳正常，dead letters 0，LightTTS admission active/queue `0/0`，`active_match_processing=false`。
- 证据：[GPU canary](audio/20260718-realtime-voice-rebuild/lighttts-gpu-canary/README.md)、[lifecycle patch](audio/20260717-005403/lighttts-lifecycle-isolated-20260718/README.md)、[浏览器门禁](audio/20260718-realtime-voice-rebuild/browser-first-sound-interrupt-gate.md)、[API 回归](audio/20260718-realtime-voice-rebuild/api-full-regression.md)、[8 音色候选](audio/20260718-realtime-voice-rebuild/aishell3-candidates/README.md)。

### TC-1033：平台 realtime voice 模块拆分与单 session 接口

- 状态：PASS
- 问题类型：GAP / RELIABILITY
- 严重程度：P1（本地修复）
- 用户影响：所有 AI 辩手、观众
- 发生概率：高
- 页面/路由：API Match Engine / `services/voice_runtime/`
- 测试身份：本地自动化
- 前置条件：实时开关关闭，禁止部署候选。
- 操作步骤：1. 检查模块边界；2. 通过 legacy re-export 与新 runtime 打开同一增量 session；3. 跑定向和全量 API。
- 预期结果：一轮只创建一个 TTS session，模块职责独立，旧调用兼容。
- 实际结果：拆分完成；旧 import 指向新实现；API 353/353 通过。
- 证据：[API 回归](audio/20260718-realtime-voice-rebuild/api-full-regression.md)
- 控制台/网络现象：无失败请求；22 条 warning 均为既有 deprecation。
- 复现稳定性：定向与全量均通过
- 操作成本：自动化，无用户点击
- 根因推测：原 `providers.py` 过大使生命周期、音频 transport 与业务流程难以独立验收。
- 建议方案：保持统一 `open_session/push_text/finish/abort` 合约，后端替换不侵入 Match Engine。
- 验收标准：同一 speech 仅一个 session；全量 API 通过；默认开关关闭。
- 修复状态：FIXED（本地，未部署）
- 关联改动/提交说明：`apps/api/app/services/voice_runtime/`、兼容 `realtime_voice.py`。

### TC-1034：正文-only 短语聚合与 Agent final 前缀保护

- 状态：PASS
- 问题类型：BUG / PERF
- 严重程度：P1（本地修复）
- 用户影响：AI 辩手、听众
- 发生概率：高
- 页面/路由：Agent SSE → TTS
- 测试身份：本地自动化
- 前置条件：模拟 thinking、正文 delta、延迟 final 和尾部修订。
- 操作步骤：1. 输入 thinking 事件；2. 输入 10–16 字稳定首块；3. 延迟 final；4. 分别修改未提交尾部和已提交前缀。
- 预期结果：thinking 永不送入 TTS；首块按正式参数提交；未提交尾部可替换；已提交前缀修改必须失败。
- 实际结果：全部规则通过；短语随轮次增长到最多 28 字，强标点可立即提交。
- 证据：[API 回归](audio/20260718-realtime-voice-rebuild/api-full-regression.md)
- 控制台/网络现象：无
- 复现稳定性：定向 21/21；全量 353/353
- 操作成本：自动化
- 根因推测：旧 6–14 字参数容易语调破碎，final 覆盖缓冲尾部时可能重复或读出旧文本。
- 建议方案：保持 10–16/16–28 灰度参数，真实音质门通过后再校准。
- 验收标准：thinking 泄露 0；已提交前缀不可变；合成文本与最终 transcript 一致。
- 修复状态：FIXED（本地，未部署）
- 关联改动/提交说明：`voice_runtime/text.py`、`voice_runtime/pipeline.py`。

### TC-1035：LightTTS lifecycle/orphan/readiness 故障注入

- 状态：PASS
- 问题类型：BUG / RELIABILITY
- 严重程度：P1（隔离候选）
- 用户影响：多房间 AI 发言
- 发生概率：中
- 页面/路由：隔离 LightTTS 服务
- 测试身份：隔离 clone
- 前置条件：生产同 commit 基线；不改生产。
- 操作步骤：注入首 PCM 卡死、整轮卡死、断连、锁卡死、prefill/decode/token2wav 卡死、abort 不释放和 orphan。
- 预期结果：readiness 503，资源真实释放或完整进程组退出，不得伪造 green。
- 实际结果：全新 clone 应用补丁成功，19/19 通过；patch SHA 已固化。
- 证据：[lifecycle 报告](audio/20260717-005403/lighttts-lifecycle-isolated-20260718/README.md)
- 控制台/网络现象：模拟 orphan 时按预期触发 fail-fast。
- 复现稳定性：规范顺序 19/19
- 操作成本：自动化
- 根因推测：原服务只看 HTTP/端口，断连后的 GPU 请求可能继续占槽并保持假绿。
- 建议方案：只有完整真实 GPU 重复门禁通过后才部署该补丁。
- 验收标准：orphan→503→完整进程组恢复；无遗留子进程。
- 修复状态：VERIFIED（隔离候选）
- 关联改动/提交说明：候选 HEAD `c9615423...`；未部署。

### TC-1036：真实 GPU 卡死后的完整恢复

- 状态：PASS
- 问题类型：RELIABILITY
- 严重程度：P1（恢复子门）
- 用户影响：共享 TTS 容量
- 发生概率：高（当前 bi-stream）
- 页面/路由：隔离端口 `8083`
- 测试身份：GPU canary
- 前置条件：无活动比赛；生产服务安全停止后启动隔离候选，结束后恢复。
- 操作步骤：1. 加载真实模型；2. 提交延迟-finish bi-stream；3. 等待 first-PCM timeout；4. 观察 orphan watchdog 与进程组退出；5. 恢复生产服务。
- 预期结果：失败请求不能永久占槽，完整 GPU 进程组必须退出。
- 实际结果：首 PCM 超时后约 2.08 秒触发 SIGTERM；重型子进程终止、端口关闭、GPU 回收、生产恢复 ready。
- 证据：[GPU canary](audio/20260718-realtime-voice-rebuild/lighttts-gpu-canary/README.md)
- 控制台/网络现象：关闭阶段仍有 shared-memory/semaphore warning。
- 复现稳定性：真实 canary 1/1
- 操作成本：一次隔离服务切换；未操作比赛数据
- 根因推测：manager request refcount 卡在 4，普通 abort 无法释放。
- 建议方案：保留完整进程组恢复作为最后防线，并继续消除共享资源泄漏。
- 验收标准：端口、进程和显存均回收；生产 ready；无假绿。
- 修复状态：VERIFIED（恢复子门，未部署）
- 关联改动/提交说明：manager watchdog + process-group fail-fast。

### TC-1037：真实 CosyVoice3/LightTTS finish 前首 PCM

- 状态：FAIL
- 问题类型：BUG / PERF
- 严重程度：P1
- 用户影响：所有 AI 辩手和听众
- 发生概率：高
- 页面/路由：`WS /inference_zero_shot_bistream`
- 测试身份：GPU canary
- 前置条件：真实生产同款模型和提示音；第一块 13 字，2 秒后第二块和 finish。
- 操作步骤：提交首块后持续接收二进制 PCM，记录 finish 前后首包和连接状态。
- 预期结果：20/20 均在 finish 前有 PCM；首 PCM P95≤800ms。
- 实际结果：本次 0 PCM、0 字节；3 秒触发 `FirstPcmTimeout`，5204.2ms 后连接异常关闭。
- 证据：[result.json](audio/20260718-realtime-voice-rebuild/lighttts-gpu-canary/result.json)、[报告](audio/20260718-realtime-voice-rebuild/lighttts-gpu-canary/README.md)
- 控制台/网络现象：encode 与 LLM prefill 已发生，但 decode 没有输出块。
- 复现稳定性：真实门第一次即失败；此前生产双流也有同类卡死证据
- 操作成本：一次 GPU canary，无浏览器点击
- 根因推测：推测为当前 LightTTS/CosyVoice3 bi-stream 模型路径在累计上下文/decoder 协作处卡死；尚未缩小到单一函数。
- 建议方案：不继续包装当前后端上线；转独立 24GB+ GPU 的原生 OpenMOSS Realtime，并复用同一 平台 session 合约。
- 验收标准：20/20 finish 前 PCM，P95≤800ms、P99≤1.2s，重复取消无残留。
- 修复状态：OPEN / NO-GO
- 关联改动/提交说明：所有生产 bi-stream/realtime flag 保持 false。

### TC-1038：8 个固定普通话候选音色静态门

- 状态：PASS
- 问题类型：GAP / AUDIO QUALITY
- 严重程度：P2
- 用户影响：4v4 八个辩位
- 发生概率：高
- 页面/路由：音色资产目录
- 测试身份：离线质量工具
- 前置条件：AISHELL-3 Apache-2.0 公开语料。
- 操作步骤：选 4 男 4 女不同 speaker；转换 24kHz mono PCM16；核对 8–15 秒、峰值、逐字稿、来源和 SHA。
- 预期结果：8 个来源可追踪、格式合规、彼此独立的中性普通话候选。
- 实际结果：8/8 静态门通过，时长约 9.7–11.3 秒，峰值约 -3.5dBFS；未替换生产。
- 证据：[候选报告](audio/20260718-realtime-voice-rebuild/aishell3-candidates/README.md)
- 控制台/网络现象：无
- 复现稳定性：15 个专项测试通过，哈希复核通过
- 操作成本：自动化准备；未进行人工试听发布
- 根因推测：现有生产 voice 1–4 资产不完整，无法满足 8 席稳定区分。
- 建议方案：后端通过后再跑 TTS→ASR CER、20 轮漂移、盲听区分和 MOS。
- 验收标准：CER、末字/重复、盲听≥80%、自然中性≥4/5、20 轮无漂移全部通过。
- 修复状态：VERIFIED（静态候选）；发布门仍未通过
- 关联改动/提交说明：仅新增 QA 候选资产与可复现脚本。

### TC-1039：Chrome 连续 PCM 首声与全链路打断探针

- 状态：PASS
- 问题类型：PERF / RELIABILITY
- 严重程度：P1（浏览器子门）
- 用户影响：Chrome 辩手、观众
- 发生概率：高
- 页面/路由：本地浏览器诊断 fixture
- 测试身份：Chrome
- 前置条件：用户手势解锁 AudioContext；连续 PCM MediaStream。
- 操作步骤：1. 注入探针；2. 生成连续 PCM；3. 记录首个非静音样本；4. 触发 interrupt；5. 观察 750ms 旧音频复活。
- 预期结果：首声≤2500ms；打断≤250ms；旧 generation 不复活。
- 实际结果：首声 446.9ms；pause 0.7ms、数字静音 13.2ms；旧音频复活 0 次。
- 证据：[门禁报告](audio/20260718-realtime-voice-rebuild/browser-first-sound-interrupt-gate.md)、[首声截图](audio/20260718-realtime-voice-rebuild/browser-first-sound-final-20260718.png)、[打断截图](audio/20260718-realtime-voice-rebuild/browser-interrupt-final-20260718.png)
- 控制台/网络现象：AudioContext 为 running，无播放错误。
- 复现稳定性：最终 smoke 与两项 gate 均 PASS
- 操作成本：浏览器自动化，无生产状态修改
- 根因推测：不适用；该测试只隔离浏览器媒体图。
- 建议方案：保持 LiveKit/WebRTC 正式路径和 100ms 左右自适应初始缓冲。
- 验收标准：真实生产轮次使用同一探针也达到阈值。
- 修复状态：VERIFIED（合成媒体子门）
- 关联改动/提交说明：诊断脚本与 fixture；未部署业务逻辑。

### TC-1040：生产首正文 delta→浏览器实际首声严格门

- 状态：BLOCKED
- 问题类型：PERF
- 严重程度：P1
- 用户影响：所有 AI 发言听众
- 发生概率：高
- 页面/路由：生产活跃 AI 轮次
- 测试身份：Chrome/Safari 观众
- 前置条件：受控 QA 房、可工作的真增量 TTS、部署 `agent_first_readable_delta_at`。
- 操作步骤：被动观察生产房并等待 `audio.rtc.started` 与非静音样本。
- 预期结果：严格起点和浏览器首声均存在，总延迟≤2.5 秒。
- 实际结果：观察房没有活动 RTC 轮次；本地时间戳已实现但未部署，当前 TTS 又 0 PCM，无法形成真实端到端样本。
- 证据：[浏览器门禁报告](audio/20260718-realtime-voice-rebuild/browser-first-sound-interrupt-gate.md)
- 控制台/网络现象：结果 `BLOCKED_NO_ACTIVE_RTC_TURN`；无伪造性能结论。
- 复现稳定性：观察窗内稳定无活动轮次
- 操作成本：只读观察；未恢复或推进他人比赛
- 根因推测：阻塞条件是上游 TTS NO-GO 与诊断字段尚未发布，不是浏览器播放失败。
- 建议方案：新 TTS 后端通过 GPU 门后，在 QA 房发布诊断字段并执行 20 次 Chrome/Safari canary。
- 验收标准：LLM 首正文→浏览器首声 P95≤2.5 秒，完整分段时间均可审计。
- 修复状态：BLOCKED
- 关联改动/提交说明：本地计时字段已完成，未部署。

### TC-1041：Safari 精确 audible latency 与打断波形

- 状态：BLOCKED
- 问题类型：PERF / TESTABILITY
- 严重程度：P2
- 用户影响：Safari 辩手、观众
- 发生概率：高
- 页面/路由：Safari 生产观战/辩论页
- 测试身份：Safari
- 前置条件：Safari Web Inspector 或测试构建可在加载前注入 WebAudio 探针；受控活动轮次。
- 操作步骤：用 Computer Use 验证连接、声音解锁与暂停 UI，并尝试取得解码后 RMS/非静音时间。
- 预期结果：保存严格 audible 时间和打断后持续静音证据。
- 实际结果：连接与声音解锁已通过，但 Computer Use 无法读取 Safari 内部 WebAudio RMS/系统扬声器波形；不得用按钮状态冒充人耳首声。
- 证据：[Safari 边界说明](audio/20260718-realtime-voice-rebuild/browser-first-sound-interrupt-gate.md)
- 控制台/网络现象：Safari Web Inspector 未获取。
- 复现稳定性：工具能力限制稳定存在
- 操作成本：页面进入与一次声音解锁
- 根因推测：测试工具缺少 Safari 页面内部数字音频遥测，不是已证实的产品缺陷。
- 建议方案：受控测试构建注入同一探针，或使用音频回环采集并配合人工听感。
- 验收标准：Safari 保存 JSON/波形，首声与打断阈值均通过且无爆音/吞字。
- 修复状态：BLOCKED
- 关联改动/提交说明：无生产改动。

### TC-1042：MOSS 独立网关、平台 适配和 3 endpoint 生命周期压力门

- 状态：PASS（协议/生命周期）；BLOCKED（真实 GPU 音质与性能）
- 问题类型：PERF / RELIABILITY / AUDIO QUALITY
- 严重程度：P1
- 用户影响：2–3 场并发的全部 AI 辩手与听众
- 发生概率：高
- 页面/路由：`services/moss-realtime-gateway`、平台 MOSS provider、质量门脚本
- 测试身份：本地隔离网关 + fake backend；未使用生产用户数据
- 前置条件：3 个独立单活 endpoint、8 个固定 prompt basename、start→audio→push→final→close 协议。
- 操作步骤：1. 先以空 initial text 建 session；2. 建立连续 PCM GET；3. 推送正文短语；4. final 前检测非静音 PCM；5. close 后复核 `released=true`、`active=0`、`orphan_count=0`；6. 以并发 1/2/3 各跑 20 批。
- 预期结果：120/120 成功、final 前首包、无尾音丢失、无双 terminal 假 orphan、全部真实释放。
- 实际结果：120/120 请求成功；120/120 final 前首个非静音 PCM；120/120 close ACK 与释放复核；c1/c2/c3 生命周期门全部 PASS。fake RTF P95 为 0.724/0.758/0.773，因合成音频仅 200ms 且人为保留 100ms final 延迟，严格 GPU release gate 按预期 NO-GO，不能冒充模型性能。
- 证据：[生命周期基准](audio/20260718-openmoss-rebuild/fake-3-endpoint-20-rounds/moss-realtime-session-benchmark.md)、[原始 JSON](audio/20260718-openmoss-rebuild/fake-3-endpoint-20-rounds/moss-realtime-session-benchmark.json)、[网关复审](audio/20260718-openmoss-rebuild/moss-gateway-review.md)、[上游审计](audio/20260718-openmoss-rebuild/openmoss-current-upstream-audit.md)
- 控制台/网络现象：所有 start/audio/push/close/readiness 为 200；没有 409、503、orphan 或资源未释放。
- 复现稳定性：网关 `15 passed`；MOSS/平台/质量门组合 `96 passed`；API 全量 `357 passed`；Web `192 passed`、build PASS。
- 操作成本：本地 fake backend；没有 GPU 费用、生产部署或外部写操作。
- 根因推测：原 P0 来自有界音频队列把终止 ACK 绑定到消费者速度，以及多个 terminal command 各自排队；均已修成 lossless backlog + 原子单 terminal join。
- 建议方案：按验收计划在 24GB+ GPU 固定 OpenMOSS commit，运行相同 20 批门和 TTS→ASR/人工听感门。
- 验收标准：真 GPU c1/c2/c3 release gate 全 PASS；CER≤2%、重复/吞字/漂移为0；浏览器正文首字→首声≤2.5秒；打断≤250ms。
- 修复状态：VERIFIED（协议/生命周期）；GPU BLOCKED
- 关联改动/提交说明：默认关闭、未部署；新增隔离 MOSS 网关、平台 健康分片/释放复核和质量门。

### TC-1043：MOSS-TTS-Nano ONNX-CUDA 真实 GPU 延迟、CER、打断和资源门

- 状态：FAIL / NO-GO
- 问题类型：PERF / AUDIO QUALITY / RELIABILITY / CAPACITY
- 严重程度：P1
- 用户影响：全部 AI 辩手和同卡生产语音任务
- 发生概率：高
- 页面/路由：隔离 loopback Nano 服务；未接入生产路由
- 测试身份：RTX 3080 Ti real GPU canary
- 前置条件：生产无 active match、LightTTS gate 0/0；Nano repo 与两组 ONNX 权重固定 SHA；CUDAExecutionProvider。
- 操作步骤：1. 固定 commit 启动 ONNX-CUDA；2. 8 个普通话 prompt 顺序生成同一句；3. 记录首 PCM、RTF、块间隔和显存；4. 用本机 FunASR 回转 CER；5. 长文本首包后 close，检测残音与资源释放；6. 停止 canary 并复核生产。
- 预期结果：8/8 CER≤2%、吞字/重复为0；首 PCM≤800ms；RTF≤0.65；打断≤250ms；不挤压生产；可证明2–3场并发。
- 实际结果：首 PCM 161.6–278.5ms、RTF 0.423–0.506；7/8 CER=0、重复 bigram=0；打断 close 5.1ms、音频流 5.7ms 内结束且旧块0。`candidate_voice_2` 只读出开头，删除19/28字，CER 67.86%。单 runtime 使用全程执行锁，不能提供2–3场并发。显存从5259MiB升至10744MiB，长文本取消后11256MiB、仅余657MiB；停止进程后恢复5259MiB。随后清理一套无监听端口、无Supervisor归属的旧8083孤儿canary，生产8080保持健康，基线进一步恢复为5000MiB used / 6914MiB free。
- 证据：[Nano CUDA 报告](audio/20260718-openmoss-rebuild/nano-onnx-cuda-canary/README.md)、[原始指标](audio/20260718-openmoss-rebuild/nano-onnx-cuda-canary/results.json)、同目录10个真实 WAV。
- 控制台/网络现象：ONNX Runtime CUDA provider 对图插入多组 Memcpy node 警告；额外创建第二 CUDA runtime 时因显存不足出现 CUDNN_STATUS_INTERNAL_ERROR；隔离服务停止后端口消失、显存恢复、平台 ready 和 LightTTS 正常。
- 复现稳定性：真实 GPU 单轮 8 音色；不足以证明20轮，但已经出现明确发布阻断反例。
- 操作成本：未停止或修改生产服务；canary 进程测试后已关闭。
- 根因推测：Nano 完整文本分块/自回归早停对 prompt 敏感；ONNX CUDA session 与 codec 占用显存显著，且当前 serving 用 execution lock 串行请求。
- 建议方案：淘汰 Nano 正式主链，保留诊断价值；正式使用独立24GB+ GPU上的 MOSS-TTS-Realtime，每 endpoint 单活并部署2–3个独立 endpoint。
- 验收标准：Realtime c1/c2/c3各20批 release gate、8音色20轮 CER/漂移/MOS和Chrome/Safari真首声全部通过。
- 修复状态：VERIFIED NO-GO
- 关联改动/提交说明：正式 MOSS 网关新增24GB总显存、20GB启动空闲显存、CUDA与固定SDPA fail-closed；未部署。

### TC-1044：正式持久 WS、预连接、独立 Uvicorn 和 3 endpoint 压力门

- 状态：PASS（协议/生命周期/部署依赖）；BLOCKED（真实 OpenMOSS GPU 模型）
- 问题类型：PERF / RELIABILITY / DEPLOYABILITY
- 严重程度：P1
- 用户影响：所有 AI 辩手与 2–3 场并发听众
- 发生概率：高
- 页面/路由：`WS /tts/session/ws`、平台 MOSS provider、正式 benchmark、Chrome 生产观战/健康页
- 测试身份：本地 3 个独立 Uvicorn fake endpoint + Chrome 生产只读
- 前置条件：MOSS 总开关默认关闭；8 个固定 voice ID；3 个单活 endpoint；API key；正式 `websockets.connect` 客户端。
- 操作步骤：1. 对齐 `start seq0/voice`、嵌套 `ready.audio`、`text_delta/ack`、`final/ack/audio_end/released`；2. 慢 push 期间并发 ping/abort；3. 填满有界 WS egress 后 abort；4. 预建已鉴权未 start idle WS，首轮领用、TTL/断线丢弃、释放后补齐；5. 用独立 Uvicorn 进程运行 3 endpoint，执行 c1/c2/c3 各20批；6. Chrome 只读检查生产观战页和 `/api/health/ready`。
- 预期结果：单一 recv/demux；thinking/乱序拒绝；abort 不死锁且清旧 PCM；120/120 final 前首包、释放确认、无 orphan；独立安装可处理 WebSocket upgrade；生产不受本地候选影响。
- 实际结果：Gateway 全量26/26；provider全量86/86；API371/371；Web192/192及build通过。真实进程首轮暴露 gateway 运行依赖缺少 WebSocket server backend，Uvicorn upgrade 返回404；补入固定 `websockets>=15,<16` 后重跑 120/120 成功，120/120 `close_ack=true`、`release_confirmed=true`、final前非静音PCM。c1/c2/c3生命周期均PASS；fake严格模型门 c2/c3 因200ms人造音频与100ms finish延迟导致RTF P95 0.653/0.687而按设计NO-GO，不能冒充真模型性能。Chrome 生产观战页仍为安全暂停，ready `ok=true`、LightTTS active/queue 0。
- 证据：[正式 WS 压力报告](audio/20260718-openmoss-rebuild/fake-ws-3-endpoint-20-rounds/moss-realtime-session-benchmark.md)、[原始 JSON](audio/20260718-openmoss-rebuild/fake-ws-3-endpoint-20-rounds/moss-realtime-session-benchmark.json)、[Chrome 观战截图](screenshots/20260718-realtime-voice/production-watch-paused-chrome.png)、[Chrome 健康截图](screenshots/20260718-realtime-voice/production-health-ready-chrome.png)
- 控制台/网络现象：修复前独立 Uvicorn 明确输出“Unsupported upgrade request / No supported WebSocket library”；修复后所有连接完成正式握手，endpoint 停止后18990–18992端口均关闭。
- 复现稳定性：正式 WS 120 请求；slow push、满 egress、final/abort/断连竞态均有定向测试；跨 API/gateway 真实 loopback 通过。
- 操作成本：本地 fake 进程与 Chrome 只读；未部署、未创建比赛、未修改生产配置。
- 根因推测：此前 HTTP 兼容协议和 ASGI TestClient 掩盖了真实独立进程的 WebSocket server 运行依赖；abort 旧顺序也可能在满 egress 时先等音频泵而死锁。
- 建议方案：以当前持久 WS/idle preconnect 为正式控制层，在独立24GB+ GPU 部署2–3个固定 Realtime endpoint，复用同一 benchmark 与质量门。
- 验收标准：真 GPU c1/c2/c3各20批全部 release gate PASS；8音色20轮CER/漂移/MOS通过；LLM首正文→浏览器首声P95≤2.5秒；打断≤250ms。
- 修复状态：VERIFIED（协议/生命周期/可部署性）；真实模型 BLOCKED
- 关联改动/提交说明：正式 WS、provider demux/隔离、idle预连接、benchmark、Uvicorn WebSocket依赖；全部默认关闭且未部署。

### TC-1045：Agent 首正文 delta 的 200ms 硬截止与正文过滤

- 状态：PASS
- 问题类型：PERF / RELIABILITY
- 严重程度：P1（核心延迟子门）
- 用户影响：所有 AI 辩手与听众
- 发生概率：高
- 页面/路由：Debate Agent SSE → `voice_runtime/text.py` → MOSS/LightTTS session
- 测试身份：本地自动化；未调用生产比赛
- 前置条件：正文 SSE 可能只到达 1–9 个汉字且暂时无标点；thinking/reasoning 与正文事件可能交错。
- 操作步骤：1. 首个非空正文 delta 启动 200ms deadline；2. 测量 4 字停顿和已有16字两类输入各20轮；3. 注入复合 thinking/reasoning/analysis SSE 类型；4. 在未提交尾部存在时触发 interrupt。
- 预期结果：短正文不能无限等待；首块优先10–16字或完整短句；后续16/22/28字且最大28；thinking不送TTS；中断后未提交尾部不进入TTS或历史。
- 实际结果：4字停顿中位200.129ms、最大200.223ms；16字就绪中位0.067ms、最大0.195ms。取消轮询不再每50ms误触发分块；复合 thinking 类型全部过滤；未提交尾部在 interrupt 后未进入 TTS/历史。
- 证据：[Agent delta 延迟审计](audio/20260718-openmoss-rebuild/agent-delta-latency-audit.md)
- 控制台/网络现象：无；为纯服务层自动化。
- 复现稳定性：两类各20轮；相关定向34项通过；合并 API 378/378。
- 操作成本：无浏览器点击；无网络写入；未部署。
- 根因推测：旧逻辑只有达到字符阈值或遇到标点才提交，短 delta 停顿时没有独立最大等待截止。
- 建议方案：保留首正文 200ms deadline，并在真模型 QA 房逐轮记录 Agent delta→TTS ACK→首PCM→浏览器非静音。
- 验收标准：20轮短 delta 最大等待≤250ms；thinking 0条进入TTS；interrupt后未提交尾部0条进入历史。
- 修复状态：FIXED / VERIFIED（本地）
- 关联改动/提交说明：`voice_runtime/text.py`、`pipeline.py` 及相关测试；未部署。

### TC-1046：LiveKit 服务端队列与浏览器 AudioWorklet 的完整打断

- 状态：PASS
- 问题类型：BUG / RELIABILITY / PERF
- 严重程度：P1（核心打断子门）
- 用户影响：暂停、跳过、终止或退出时的全部辩手与观众
- 发生概率：中
- 页面/路由：LiveKit `AudioSource` → WebRTC jitter buffer → 浏览器 AudioWorklet
- 测试身份：本地自动化 + Chromium 合成媒体 fixture
- 前置条件：旧 generation 的20ms帧可能已出队但 `capture_frame` 尚未完成；浏览器仍可能保留约220ms WebRTC尾帧。
- 操作步骤：1. 在 capture 未完成时并发 abort；2. 清空服务端队列并启动新 generation；3. 浏览器触发 generation-scoped flush；4. 连续消费远端音轨但输出数字静音；5. 观察750ms旧音复活；6. 重复 synthetic gate 5轮。
- 预期结果：旧帧不能在 clear 后复活；旧 finish 不能等待新 generation；浏览器250ms内静音；stale abort不伤新 generation。
- 实际结果：服务端新增 capture/clear 串行临界区并在锁内复核 generation；浏览器 gate 丢弃完整220ms尾帧，只有新 generation 能激活。5轮首声416.3–435.2ms，interrupt→flush 0–0.2ms、持续静音15.0–17.1ms，旧音复活0次。smoke 已新增首声≤2500ms、flush/静音≤250ms硬断言。
- 证据：`apps/api/app/services/livekit_audio.py`、`apps/web/public/worklets/livekit-interrupt-gate.js`、`scripts/tests/run_realtime_audio_probe_smoke.mjs` 及对应自动化测试。
- 控制台/网络现象：第一次并行测试期间单次 synthetic fixture 曾出现3376.8ms首声；该旧 smoke 当时没有延迟阈值。随后在同一代码上连续5轮均为416.3–435.2ms，并把2.5秒阈值写入测试，避免以后误报PASS。该 fixture仍不能替代生产真模型首声。
- 复现稳定性：API 378/378；Web 194/194；Next build PASS；synthetic gate 5/5 PASS。
- 操作成本：本地浏览器自动化；无生产状态修改。
- 根因推测：旧实现只 detach/pause `<audio>`，无法保证 WebRTC jitter-buffer尾帧被消费丢弃；服务端清队列与未完成 capture 也缺少同一临界区。
- 建议方案：在独立真模型 QA 房继续用相同 probe 执行20轮 Chrome/Safari，报告真实 Agent首正文→浏览器非静音和 interrupt→持续静音。
- 验收标准：真回合20/20打断≤250ms、旧generation复活0；首声最大≤2.5秒。
- 修复状态：FIXED / VERIFIED（本地 synthetic与单测）
- 关联改动/提交说明：LiveKit服务端capture锁、revoked drain、Web AudioWorklet gate、DebateStage generation接线及测试；未部署。

### TC-1047：TTS 自动质量门 schema v4

- 状态：PASS（测试能力）；真实模型发布门仍 BLOCKED
- 问题类型：AUDIO QUALITY / TESTABILITY
- 严重程度：P1（发布门基础设施）
- 用户影响：8个AI辩位和2–3场并发听众
- 发生概率：高
- 页面/路由：`scripts/benchmark_tts_quality_gate.py`
- 测试身份：离线自动质量门
- 前置条件：原质量门可能把首包静音、首尾字替换、句内音量漂移和多房首非静音排队遗漏掉。
- 操作步骤：1. 增加首个含非静音PCM包和前导静音；2. 检查首字/末字替换；3. 统计句内与跨样本响度；4. 记录c2/c3首非静音排队；5. 对同音色20轮做幅度归一化频谱指纹和余弦漂移；6. 跑1/2/3路离线自检。
- 预期结果：质量门对吞头、吞尾、静音首包、音量跳变、音色漂移代理和并发排队 fail-closed，且不冒充人工MOS或说话人验证。
- 实际结果：schema升级v4；目标19项、scripts全量25项通过；离线自检25/25自动门通过。`release_ready=false`，因为没有真人MOS和已验证说话人嵌入，符合fail-closed。
- 证据：[质量门审计](audio/20260718-openmoss-rebuild/tts-quality-gate-audit-v4.md)、[自检报告](audio/20260718-openmoss-rebuild/tts-quality-gate-selftest-v4/tts-quality-gate.md)、[JSON](audio/20260718-openmoss-rebuild/tts-quality-gate-selftest-v4/tts-quality-gate.json)
- 控制台/网络现象：无；`audioop` 仅有 Python 3.13 移除预告，不影响本轮结果。
- 复现稳定性：25/25 scripts tests；fake自检稳定通过。
- 操作成本：离线生成；未调用生产模型。
- 根因推测：旧指标以包到达和整体CER为主，无法完整覆盖“有首包但全静音”或首尾替换等听感失败。
- 建议方案：任何真模型canary必须产出同一schema v4 JSON，并另附至少两场景人工盲听/MOS。
- 验收标准：自动指标全部通过且 `release_ready=true` 只能在人工MOS/说话人稳定性证据齐全后出现。
- 修复状态：FIXED / VERIFIED（测试工具）
- 关联改动/提交说明：质量门脚本、测试和自检资产；未部署。

### TC-1048：原版 OpenMOSS Realtime 在 RTX 3080 Ti 12GB 独占加载

- 状态：FAIL / NO-GO
- 问题类型：CAPACITY / DEPLOYABILITY
- 严重程度：P1
- 用户影响：全部实时AI语音；决定现有生产GPU能否替换LightTTS
- 发生概率：必现（本轮固定版本与配置）
- 页面/路由：隔离维护窗、本机loopback；未接入平台生产路由
- 测试身份：真实RTX 3080 Ti GPU canary
- 前置条件：`active_match_processing=false`、LightTTS gate 0/0；固定OpenMOSS commit/model/codec revision；先停止LightTTS并确认GPU total/free 12288/11909MiB。
- 操作步骤：1. 加载403个Realtime talker权重；2. 加载1600个MOSS Audio Tokenizer权重；3. 将codec移到CUDA；4. 计划编码voice1并生成中文短句；5. 无论成功失败均trap恢复LightTTS并复核主站。
- 预期结果：模型与codec成功加载，至少产生一个非静音PCM，再进入首包/RTF/CER门。
- 实际结果：codec `.to(cuda)` 阶段 `torch.OutOfMemoryError`；GPU总11.63GiB、已用11.62GiB、仅余5.44MiB，再申请20MiB失败。未生成PCM，因此延迟、CER、音色与并发门均NOT-RUN。trap后LightTTS RUNNING、8080 health `Ok`、GPU used/free 5008/6906MiB；主站ready `ok=true`、schema0019、active_match=false、gate0/0。
- 证据：[12GB独占canary](audio/20260718-openmoss-rebuild/openmoss-realtime-12gb-exclusive-canary.md)、[上游跟进审计](audio/20260718-openmoss-rebuild/openmoss-20260718-followup-audit.md)
- 控制台/网络现象：模型和codec权重物化完成，最终上卡失败；没有启动MOSS端口或更改生产flag。
- 复现稳定性：固定版本真实独占canary 1/1即出现明确硬阻断；与vLLM-Omni官方约6GB talker+8GB codec/A10G24GB说明一致。
- 操作成本：一次受控维护窗；未操作比赛数据；现有服务已自动恢复并独立复核。
- 根因推测：完整MOSS Audio Tokenizer本身约8GB，加1.7B talker及CUDA工作区超过12GB物理容量；不是LightTTS共存或显存碎片导致的主因。
- 建议方案：默认转独立24GB+ GPU；仅在上游明确支持的低精度codec、跨设备codec或完整量化runtime通过全部质量/延迟门时，才保留sub-24GB诊断分支。
- 验收标准：24GB+环境成功加载并通过1/2/3 endpoint各20批、8音色20轮、浏览器首声≤2.5秒和打断≤250ms。
- 修复状态：VERIFIED NO-GO（12GB原版路径）
- 关联改动/提交说明：仅新增显式sub-24GB diagnostic配置和证据；默认24GB/20GB门槛不变，未部署。

### TC-1049：12GB GPU talker + CPU FP32 codec 诊断路径

- 状态：FAIL / NO-GO
- 问题类型：PERF / DEPLOYABILITY
- 严重程度：P1
- 用户影响：尝试复用现有12GB生产GPU的全部实时AI语音
- 发生概率：必现（当前固定硬件/权重）
- 页面/路由：隔离维护窗；OpenMOSS gateway diagnostic placement
- 测试身份：真实RTX 3080 Ti + 12核Xeon canary
- 前置条件：权重核算为Realtime BF16 4.343GiB、codec FP32 6.611GiB；显式启用sub-24GB diagnostic并把完整codec放CPU，所有生产flag保持false。
- 操作步骤：1. 停LightTTS并清空GPU；2. 模型加载cuda:0、codec加载CPU；3. 编码约10秒voice1 prompt；4. 计划生成正式中文短句；5. 首轮真实接口错误修复后重复相同canary；6. 5分钟上限主动终止并恢复生产。
- 预期结果：prompt预热可在可接受时间内完成，生成阶段首PCM≤800ms、浏览器预算≤2.5秒。
- 实际结果：首轮26.212秒完成后端加载，GPU used/free 4774/7140MiB，随后暴露网关错误传list给固定codec `encode()`；修复为3D Tensor并正确读取`audio_codes`后，Gateway 29/29。第二轮不再OOM或接口报错，但单个约10秒prompt在321秒时仍未编码完成，GPU利用率0%、Python CPU约85–94%、RSS约5.4→7.6GiB，未进入首PCM或stream decoder。
- 证据：[CPU codec canary](audio/20260718-openmoss-rebuild/openmoss-realtime-12gb-cpu-codec-canary.md)、[内存路径审计](audio/20260718-openmoss-rebuild/rtx3080ti-memory-path-audit.md)
- 控制台/网络现象：第一轮`AttributeError: list has no attribute dim`已修复并有回归；第二轮在有界窗口内持续CPU工作、无PCM，主动终止。恢复后LightTTS RUNNING/health Ok、GPU5000/6914MiB、主站ready ok、schema0019、active=false、gate0/0。
- 复现稳定性：真实接口错误1/1；修复后CPU编码性能反例1/1，已远超硬预算两个数量级。
- 操作成本：两次受控维护窗；未进入比赛、未部署、未改生产flag。
- 根因推测：1.6B FP32 Audio Tokenizer的完整CPU encode/decode计算量不适合当前12核Xeon实时执行；离线预计算prompt只能隐藏encode，不能解决每轮streaming decode。
- 建议方案：停止12GB CPU codec方向；正式候选只在24GB+ GPU或第二块独立GPU上运行完整加速codec。
- 验收标准：该12GB路径不再进入发布候选；24GB+路径必须重新跑真实20轮、8音色、2–3场并发和浏览器门。
- 修复状态：VERIFIED NO-GO
- 关联改动/提交说明：保留默认关闭、health显式暴露的diagnostic配置用于审计；修复固定codec Tensor接口并新增单测；未部署。

### TC-1050：12GB codec encoder CPU / decoder CUDA 拆分热态真实 canary

- 状态：FAIL / NO-GO
- 问题类型：PERF / CAPACITY / DEPLOYABILITY
- 严重程度：P1
- 用户影响：尝试复用现有12GB生产GPU的全部实时AI语音
- 发生概率：必现（本轮固定版本、硬件与两轮样本）
- 页面/路由：隔离维护窗；OpenMOSS gateway diagnostic encoder-offload placement
- 测试身份：真实RTX 3080 Ti、固定OpenMOSS revisions、同进程冷轮+热轮
- 前置条件：生产`active_match_processing=false`、LightTTS gate 0/0；完整codec先在CUDA预编码8个固定prompt，再把encoder offload到CPU，只保留quantizer/decoder与Realtime talker在CUDA；所有生产实时开关保持false。
- 操作步骤：1. 停LightTTS并确认约11909MiB空闲；2. 加载完整codec并编码8个prompt；3. encoder移CPU并清cache；4. 加载BF16 talker；5. 同一backend、同一音色连续运行冷编译轮和第二轮热态；6. 记录首PCM、RTF、块间隔与显存；7. trap恢复生产并独立检查公共ready。
- 预期结果：第二轮热态首PCM≤800ms、RTF≤0.65、块间隔≤200ms；浏览器端到端预算仍有余量。
- 实际结果：placement成功，准备后GPU used/free约8437/3475MiB，证明12GB可驻留。冷轮首PCM113.732秒、墙钟130.328秒、5.44秒音频、RTF23.957、最大块间隔10.752秒；同进程热轮首PCM反而175.936秒、墙钟196.531秒、6.72秒音频、RTF29.246、最大块间隔13.769秒。三项热态门全部失败，性能差异不是一次性编译可解释。
- 证据：[完整报告](audio/20260718-openmoss-rebuild/codec-split-real-canary/README.md)、[结果JSON](audio/20260718-openmoss-rebuild/codec-split-real-canary/codec-split-warm-turns-result.json)、[冷轮WAV](audio/20260718-openmoss-rebuild/codec-split-real-canary/codec-split-cold-compile.wav)、[热轮WAV](audio/20260718-openmoss-rebuild/codec-split-real-canary/codec-split-warm-measured.wav)、[完整日志](audio/20260718-openmoss-rebuild/codec-split-real-canary/codec-split-warm-turns.log)。
- 控制台/网络现象：两次模型加载前的环境/门槛配置错误均按设计fail-closed并自动恢复，不计性能样本；正式两轮运行无OOM，说明失败点是生成吞吐而非装载。最终LightTTS RUNNING、8080 health Ok、GPU5000/6914MiB、公共ready ok、active_match=false、gate0/0。
- 复现稳定性：前一轮单次首PCM40.693秒/RTF19.565；本轮同进程冷/热两轮均远超门槛，且热轮更差，已有三个独立性能反例。
- 操作成本：三次有自动退出保护的受控维护尝试；前两次均在推理前fail-closed；未进入比赛、未部署、未改生产flag。
- 根因推测：decoder-only codec与Realtime talker在当前PyTorch/SDPA/RTX3080Ti组合上的逐步生成吞吐极低；encoder offload只释放静态权重，不会加速每轮talker+decoder计算。
- 建议方案：停止全部12GB OpenMOSS Realtime正式候选；按`docs/realtime-voice-rebuild.md`转到独立24GB+ GPU，先跑固定上游原生session单endpoint20轮，再比较vLLM-Omni runtime。
- 验收标准：24GB+单路20/20首PCM/RTF/gap/release通过后，才允许8音色质量、2–3独立endpoint并发和浏览器实际首声。
- 修复状态：VERIFIED NO-GO（12GB拆分路径）
- 关联改动/提交说明：新增默认关闭的diagnostic encoder-offload与decoder-only streaming context；Gateway34/34；未部署。

### Iteration 38：48GB 原生 session 改进、连续播放与真实打断

本轮没有操作生产比赛、数据库或生产开关。所有真模型运行均位于独立48GB GPU canary；生产 `MOSS_TTS_REALTIME_ENABLED`、`REALTIME_VOICE_PIPELINE_ENABLED`、LightTTS bi-stream开关继续为false。本轮按重建方案打通正文delta、持久WS、逐step PCM、可取消推理、连续时钟预缓冲和资源释放，并对不达标项保留NO-GO。

### TC-1051：vLLM-Omni Realtime 真双向增量合约审计

- 状态：FAIL / NO-GO
- 问题类型：ARCHITECTURE / RELIABILITY
- 严重程度：P1（候选runtime淘汰门）
- 用户影响：全部AI辩手的实时语音和打断
- 发生概率：必现（当前审计版本）
- 页面/路由：vLLM-Omni Realtime WebSocket runtime
- 测试身份：固定上游源码只读审计
- 前置条件：要求同一session持续接收正文delta、生成期处理cancel、返回`audio_reset/released`并保持generation隔离。
- 操作步骤：检查WS输入循环、`input.done`、生成任务、cancel读取、session关闭、PCM async chunk、codec并发与E2E测试状态。
- 预期结果：首段正文后即可持续推理；生成期间仍读取cancel；session不因单次生成自动关闭；完整释放语义可映射正式gateway协议。
- 实际结果：当前runtime累积`input.text`至`input.done`后才一次性生成，生成期不继续读取cancel，结束后关闭session；没有项目要求的`audio_reset/released`与generation隔离。PCM异步chunk基础存在，但Realtime E2E仍skip、codec stage默认`max_num_seqs=1`。不接入伪双流路径。
- 证据：[vLLM-Omni runtime audit](audio/20260718-openmoss-rebuild/vllm-omni-openmoss-runtime-audit.md)
- 控制台/网络现象：只读源码审计，无线上请求。
- 复现稳定性：固定审计版本完整检查1次；关键合约均有源码位置。
- 操作成本：无部署、无GPU写入。
- 根因推测：现有Realtime示例面向单段prompt生成，不是持续append/cancel的房间级会话。
- 建议方案：继续使用固定上游原生session与现有gateway；仅在vLLM提供真append session或完成等价runtime级适配后重评。
- 验收标准：同WS持续delta、生成期cancel、release ACK和generation隔离均有非skip E2E。
- 修复状态：VERIFIED NO-GO（当前vLLM候选）
- 关联改动/提交说明：无接入改动；避免为追进度引入伪双流。

### TC-1052：固定版本 OpenMOSS endpoint 部署与重启工具

- 状态：PASS
- 问题类型：DEPLOYABILITY / RELIABILITY
- 严重程度：P1（真GPU门基础设施）
- 用户影响：canary运维和后续2–3端点隔离
- 发生概率：高
- 页面/路由：`deploy/openmoss/`
- 测试身份：本地静态测试 + 独立48GB endpoint runner
- 前置条件：固定源码/model/codec revisions、本地snapshot、offline、GPU UUID/显存 admission、每endpoint独立key/lock/port。
- 操作步骤：验证manifest哈希、GPU门、启动参数、ready文件、端口重启preflight、trap清理、DCF12/I6默认值和离线依赖路径。
- 预期结果：错误GPU或显存不足fail-closed；干净重启不被TIME_WAIT误拦；真实listener冲突仍拒绝；退出后不留gateway进程。
- 实际结果：runner成功启动并复用48GB真机canary；端口检查改为连接探测+`SO_REUSEADDR`，允许干净重启且仍拒绝活listener；部署静态测试PASS。所有配置默认离线、固定revision，未接触生产。
- 证据：`deploy/openmoss/deployment-manifest.json`、`run-endpoint.sh`、`preflight.py`、`test-static.sh`，以及[48GB证据索引](audio/20260718-openmoss-rebuild/native-48gb-real-canary/README.md)
- 控制台/网络现象：endpoint ready暴露固定placement、active/orphan与GC状态；密钥未写入报告。
- 复现稳定性：静态检查重复PASS；真实endpoint完成多组benchmark。
- 操作成本：独立canary GPU；未部署生产。
- 根因推测：旧端口检查把TIME_WAIT当活listener，且runner缺少固定PYTHONPATH/参数闭环。
- 建议方案：后续每张GPU只运行一个endpoint进程，并以相同manifest派生c2/c3。
- 验收标准：三端点各自ready、单活、独立释放，停止后端口与GPU均清洁。
- 修复状态：FIXED / VERIFIED（canary工具）
- 关联改动/提交说明：固定manifest、runner、GPU/端口preflight与静态测试；未部署。

### TC-1053：48GB 稳定 inferencer 与逐 step 低延迟 bridge

- 状态：FAIL / NO-GO（控制流已修复，性能发布门失败）
- 问题类型：PERF / RELIABILITY
- 严重程度：P1
- 用户影响：所有AI辩手的首声与连续播放
- 发生概率：高
- 页面/路由：OpenMOSS native session → gateway persistent WS
- 测试身份：独立48GB CUDA GPU、单endpoint真实模型
- 前置条件：固定OpenMOSS/model/codec revisions；完整codec与8个prompt在CUDA；SDPA；生产开关关闭。
- 操作步骤：1. 复现每轮动态class的Dynamo重编译；2. 改为backend生命周期稳定inferencer；3. prefill与每个step立即sanitize/decode/yield；4. 比较DCF3/6/12；5. DCF12/I6执行GC-off c1×20。
- 预期结果：20/20成功和release；首PCM P95≤800ms、RTF P95≤0.65、gap P99≤200ms；无长尾重编译或多秒GC卡顿。
- 实际结果：动态class的`RecompileLimitExceeded`已消失，逐step bridge不再等待完整pending token。GC-off c1×20为20/20成功、20/20 final前首PCM、20/20 release；首PCM P95 `1248.667ms`、active RTF P95 `0.977`、gap P99最大 `1124.817ms`，发布门仍全部失败。GC-off把上一轮极端gap `3282.160ms`降至`1124.817ms`，但中位吞吐未改善。
- 证据：[48GB真机证据索引](audio/20260718-openmoss-rebuild/native-48gb-real-canary/README.md)、[GC-off 20轮报告](audio/20260718-openmoss-rebuild/native-48gb-ws-low-latency-dcf12-i6-gc-off-c1-20/moss-realtime-session-benchmark.md)
- 控制台/网络现象：最新20轮无失败、无orphan；严格release gate按预期为false。
- 复现稳定性：c1×20完整样本；另有stable direct、DCF3/6、voice5重复10轮和低延迟bridge20轮对照。
- 操作成本：独立canary GPU；未进入比赛、未部署。
- 根因推测：控制流阻塞已解除，剩余瓶颈是talker+codec逐步生成吞吐与块到达节奏。
- 建议方案：不降低门槛；先做8音色质量与真实浏览器单场，2–3场容量必须由独立endpoint横向扩展，不在单实例并发session。
- 验收标准：c1/c2/c3各20批全部首PCM/RTF/gap/release通过，且30分钟连续播放0 underrun。
- 修复状态：PARTIAL / RELEASE NO-GO
- 关联改动/提交说明：stable inferencer、low-latency bridge、DCF12/I6、warmup后关闭自动GC；均未部署。

### TC-1054：800ms 连续时钟预缓冲与真实 gateway interrupt

- 状态：FAIL / NO-GO（打断子门PASS，浏览器与连续播放发布门未通过）
- 问题类型：PERF / RELIABILITY / AUDIO PLAYBACK
- 严重程度：P1
- 用户影响：AI发言听感以及暂停、跳过、结束、退出
- 发生概率：高
- 页面/路由：MOSS WS → MOSS Provider → LiveKit AudioSource → WebRTC/AudioWorklet
- 测试身份：真实gateway WS + 本地LiveKit自动化 + chunk timeline复算
- 前置条件：MOSS起播预缓冲默认800ms、应用队列1200ms、20ms对齐；非MOSS额外缓冲0；同generation abort清应用与SDK队列。
- 操作步骤：1. 用20轮真实chunk timeline模拟连续时钟；2. 对比GC-off前后underrun；3. 验证短句finish、阈值前不dequeue、abort generation隔离、旧drain不复活；4. 首PCM后真实abort重复5次。
- 预期结果：首个可朗读字符→浏览器实际首声≤2.5秒；30分钟0 underrun；打断≤250ms且旧PCM为0；短句不被永久缓冲。
- 实际结果：GC-off的800ms复算首正文→服务端起播P50/P95/max为`1914.243/2163.240/2230.155ms`；加200ms正文聚合后推导P95/max约`2363.240/2430.155ms`，但未包含真实LiveKit/浏览器。19/20无underrun，唯一一次152.524ms，仍不满足0卡顿门。真实gateway abort 5/5在`102.249–196.276ms`返回reset/release，post-abort PCM为0、active/orphan为0。LiveKit/Provider本地106项和配置边界通过，非MOSS路径不变。
- 证据：[800ms复算](audio/20260718-openmoss-rebuild/native-48gb-ws-low-latency-dcf12-i6-gc-off-c1-20/playback-800ms-derived.md)、[真实interrupt目录](audio/20260718-openmoss-rebuild/native-48gb-real-interrupt/)、[48GB证据索引](audio/20260718-openmoss-rebuild/native-48gb-real-canary/README.md)
- 控制台/网络现象：5次abort后均无残留active/orphan；没有真实Chrome/Safari OpenMOSS音轨，故不记录伪造浏览器首声。
- 复现稳定性：timeline 20轮；真实interrupt 5轮；LiveKit/Provider定向106项、本轮合并定向115项PASS。
- 操作成本：独立canary与本地自动化；未部署生产。
- 根因推测：800ms缓冲能吸收大多数模型抖动，但不能消除全部约1秒级chunk间隔；真实WebRTC还会增加不可忽略的传输/解码预算。
- 建议方案：保留generation预缓冲和完整abort语义；下一轮必须在真实QA房测Chrome/Safari，并在多endpoint真GPU容量通过后做30分钟连续播放。
- 验收标准：真实浏览器20/20首声≤2.5秒、30分钟0 underrun、完整Agent→AudioWorklet打断≤250ms、未播放文本0条进入历史。
- 修复状态：PARTIAL / RELEASE NO-GO
- 关联改动/提交说明：MOSS 800ms generation预缓冲、短句finish flush、应用/SDK双队列abort、真实interrupt benchmark；未部署。

## 测试创建的数据清单

- 测试账号：`qa_cu_20260717_01`（User A）、`qa_cu_0717_b4`（User B）；密码不写入报告。
- 1v1 房间：`566139`，辩题/标记 `QA-CU-1V1-005403 <b>QA-CU</b>`，已完成。
- 4v4 房间：`764886`，辩题“人工智能时代，还要不要学编程？”，已完成并结算。
- 首轮部署语音回归房间：`586109`，已由测试控制台终止。
- 二次部署语音回归房间：`750374`，带 `QA-CU-1V1-20260717` 标记，已由测试控制台终止。
- Iteration 21 计划辩题 `QA-ITER21-ASR-编辑与退出保护-20260717` 未创建成功；跨房真人席位唯一性在写入前返回 409，生产不存在该 marker 房间。
- 房 `278571` 不是本轮新建 QA 房；坐标型观战测试误触 retry 后该房已终止，最终 `terminated / seq 115`。事故与纠正措施见 INCIDENT-001，不再操作该房。
- Agent canary task：`qa-fastpath-20260717t2140-agent-fastpath`，completed，latency_ms 4504.5；发布后 Agent running task=0。
- Iteration 30 Agent canary：`qa-qwen-plus-*`、`qa-direct-sse-*`、`qa-direct-sse-wave2-*`、`qa-flash-direct-*`，均无 match_id、不会生成比赛记忆；其中最终 6 个 flash 三路任务全部 completed，usage provider=`openai_direct`，是历史发布证据。最新 `qwen-plus` 30 次 post-warm 复测不调用 Agent 控制路由，AgentTask `118→118`、running `0→0`、MemoryItem `84→84`。
- Iteration 31 QA 房间：`560402`，辩题 `QA-CU-RTC-20260718 WebRTC 安全过渡回归`，由 System Admin API 会话创建；未开赛。房 `214317` 不是本轮创建数据，仅用于公开只读观战和 WebRTC 连接验证；该房因 Debate Agent 500 自动安全暂停，本轮未替房主重试、跳过或终止。
- QA 组织：`QA 自动化学校 20260717`（slug `qa-automation-20260717`），active；QA 课堂：`QA 灰度班`（slug `qa-canary-class`），active。
- QA scoped 角色：User A 为 org owner + classroom teacher；User B 为 org member + classroom student；两者 global role 均仍为 `user`。未创建新账号、未发布政策、未代表学生授予 consent。
- Safari 失败注册尝试可能在服务端仅留下失败请求，不应存在可登录账号；不在测试中删除任何生产数据，清理留待管理员按账号前缀和房号复核。

## 后续专项测试建议

1. 保留当前48GB单endpoint固定版本，不降低RTF/gap门；先补8音色TTS→ASR CER、姓名/术语、20轮漂移、盲听与人工MOS，淘汰不合格prompt。
2. 部署2–3个相互隔离的同版本endpoint，按正式4v4文本做30分钟并发：记录首PCM、RTF、排队、GPU、取消释放、串房、吞字、重复、块间静音和音色漂移。
3. 将native gateway接入隔离LiveKit QA房，用Chrome/Safari各至少20个AI轮次严格测`agent_first_readable_delta_at`→实际首声、underrun、完整打断和旧generation复活。
4. 只有浏览器、质量和c2/c3全部通过后，才依次灰度QA房→训练赛→正式4v4；任一门失败继续保持生产开关false。
5. 用两个 Chrome/Safari 测试账号完成学生注册→创建正式赛/训练赛→双人加入→4v4 AI 补位→暂停/改 ASR/结束/退出→结果的最终生产闭环；不扩展教学、政策或复杂编排模块。
6. 补 390×844 真实设备 safe-area、弱网与断线恢复，但只围绕学生建房、参赛、观战和比赛控制主链。

## 本地修复、部署准备与审查状态

- 已部署修复覆盖：Safari/结构化认证错误、匿名辩论路由、静音 ASR、单字/低可信文本过滤、WebM/MP3/Ogg/M4A 解码与时长、真人音频 metadata、人工核对、Judge/Agent 历史质量门、发言启动竞态、旧 socket/MediaRecorder 隔离、跨阶段麦克风清理、结果时间线兼容性、8 席个人分、结果音频互斥与 SunBrowser 长列表按需加载。
- 当前本地证据：Iteration 19 已发布版本的最近统一门禁为 API `285 passed, 22 warnings`、Web `23 files / 173 tests passed`。未部署 Iteration 20 候选另有 API 290、Web 182、定向 5/14/78/82 passed 等记录，但并行复审仍发现 2 个未被测试覆盖的 P0，因此不能把候选自动化全绿解释为可启用。
- Iteration 21 定向证据：DebateStage `74 passed`；ParticipateDialog+Lobby+DebateStage `93 passed`；API 跨房并发、finish/audio 幂等和人工 transcript 同 speech 归档 `4 passed`。现有 authenticated lobby E2E 使用过期文案且默认 skip，未覆盖 start/真人发言/退出，生产 ASR E2E 仍 BLOCKED。
- 新增语音基准工具自检：ASR 基准新增测试 `4 passed`，既有 ASR 专项 `19 passed`；LightTTS/ASR 基准脚本均通过 Ruff、py_compile 与 JSON 语料校验。FunASR 候选仅在远端独立端口运行，未写入生产服务。
- Iteration 32 本地重建：API `353 passed, 22 warnings`；Web 单 worker `28 files / 192 tests passed`；Next production build PASS。Chrome 合成连续 PCM 首声 446.9ms，打断到 pause 0.7ms、数字静音 13.2ms；Safari 精确 audible 仍 BLOCKED。
- Iteration 33 OpenMOSS 候选：网关定向 `15 passed`，MOSS provider/基准/质量门组合 `96 passed`，API 全量 `357 passed, 22 warnings`，Web `192 passed`、build PASS。3 个 fake 独立 endpoint 共 120/120 生命周期通过；严格模型 release gate 保持 NO-GO，等待真实 24GB+ GPU。
- Iteration 34 Nano real GPU：RTX 3080 Ti ONNX-CUDA 首 PCM/RTF/数字打断达到预算，但 1/8 音色 CER 67.86%、serving 串行且显存只余657MiB，正式链路 NO-GO；canary 已关闭，生产显存与健康恢复。Realtime 网关增加24GB/20GB GPU fail-closed，定向测试仍为15 passed。
- Iteration 35 正式持久 WS：Gateway 使用独立控制接收、单 writer、有界 egress 和可抢占 abort；平台 使用单 recv/demux、严格 seq/ACK、endpoint 隔离及每 endpoint 1 条 idle 预连接。独立 Uvicorn 首轮发现并修复缺失 WebSocket server 依赖；3 endpoint c1/c2/c3 共120/120生命周期通过。最新 API `371 passed, 22 warnings`、Gateway `26 passed`、Web `192 passed`、build PASS；开关仍为 false，未部署。
- Iteration 36 核心延迟/打断/容量：首正文增加200ms硬截止并扩展thinking过滤；LiveKit服务端修复capture/clear竞态，浏览器增加generation级AudioWorklet 220ms丢尾；质量门升级schema v4。合并验证API `378 passed`、scripts `25 passed`、Gateway `29 passed`、Web `194 passed`、build PASS。Synthetic browser gate新增首声≤2.5秒、打断≤250ms硬断言，连续5轮首声416.3–435.2ms、持续静音15.0–17.1ms。真实12GB同GPU加载在codec上卡时OOM；CPU codec诊断修复真实Tensor接口后，10秒prompt超过321秒仍未编码完成。两次维护窗后LightTTS与主站均恢复。所有Realtime生产开关仍为false，未部署。
- Iteration 38 48GB原生session：stable inferencer与逐step bridge关闭重复编译和延迟解码；GC-off c1×20全部成功/release/final前PCM，首PCM P95 `1248.667ms`、active RTF P95 `0.977`、gap P99最大`1124.817ms`。800ms连续时钟复算19/20无underrun，最长152.524ms；真实gateway interrupt 5/5在102.249–196.276ms释放。API全量387、Gateway40、benchmark9、本轮定向115、部署静态检查和Ruff通过；真实浏览器、8音色和c2/c3仍未测，未部署。
- LightTTS lifecycle consolidated patch 已在全新 clone 19/19 通过；真实 GPU recovery PASS，但 bi-stream 3 秒内 0 PCM，synthesis NO-GO。8 个固定音色仅完成静态候选门。所有相关本地代码和资产均未部署。
- 生产状态：Iteration 25 发布后主 平台 ready 连续 5/5，schema 0019，API/Engine/Worker/Web RUNNING，gate 0/0，`active_match_processing=false`，API/Engine/Web stderr delta 0。房 278571 在后续 INCIDENT-001 后为 `terminated / seq 115`；566139/764886 保持 completed。
- 数据库：生产已顺序迁移到 `0018_research_exports`；部署前 dump、QA provisioning 前 dump 与 0018 部署前 dump 均已记录。Activity 历史 scope、Consent provenance、学生 pre-room discovery、ResearchExport job/伪名已上线。
- Nginx：为 `/api/rooms` 与 `/v2/api/rooms` 增加精确 location，修复新增流式上传 location 导致的 301/307 循环；候选配置 `nginx -t` 成功后热加载。回滚点见“已部署版本与回滚点”。
- 最新部署：主 平台 release `20260717T2115-core-flow-stream-safety`，schema `0019_audio_streaming`；流式 flag 关闭/default false，LightTTS 容量仍为 `1 active + 2 waiting`。发布前 DB dump、源码回滚包、前一 Web 指针和 SHA 均已记录；第一次部署 harness 误判在生产切换前安全回滚，第二次成功。
- 生产真实回归：TC-1021–1026 使用 Chrome 与 Computer Use 验证首页、4v4 创建弹窗、登录注册回跳、4v4 完整赛果和返回大厅；未提交创建、未创建新房、console 无 warning/error。

## 最终结论

结论分层如下：

- **学生比赛主链：可小规模受控使用。** 正式赛和训练赛可由学生创建；登录/注册保留房间意图，观战严格只读，退出页面与“放弃本场并由 AI 接替”语义分离，管理员恢复会复验跨房真人唯一性。仍需用隔离双用户生产房完成新接口 E2E。
- **核心语音发布结论：WebRTC transport、浏览器媒体图、正式MOSS持久WS控制层和48GB单endpoint真实双向增量/释放/打断子门已PASS，连续播放与完整发布门仍NO-GO。** Chrome与Safari已真实连接生产LiveKit并完成声音解锁，但仍播放完整WAV；独立48GB真机的stable inferencer、逐step bridge、GC-off和真实interrupt已经证明20/20生命周期与5/5 gateway打断，800ms服务端连续时钟推导接近2.5秒预算。然而单路active RTF P95 `0.977`、gap P99最大`1124.817ms`且20轮仍有1次152.524ms underrun，真实OpenMOSS→LiveKit→Chrome/Safari未测，8音色与真c2/c3未测。因此所有增量开关继续关闭，本轮候选不部署。

下一核心发布门改为：保留固定上游原生session+当前持久WS gateway，先完成8音色质量，再部署2–3个独立真GPU endpoint，并在隔离LiveKit QA房完成Chrome/Safari实际首声≤2.5秒、30分钟0 underrun和完整Agent→AudioWorklet打断≤250ms。vLLM-Omni当前合约已NO-GO，不再作为本轮并行候选。全部通过后只按QA房→训练赛→正式4v4学生主链灰度。

最终结论：`学生主链可受控试用；核心实时语音 NO-GO`
