# Round 11 集成与生产验收报告

日期：2026-07-20

## 本轮成果

- 用四个隔离浏览器身份测试匿名访客、房主、第二名学生和同账号第二设备，覆盖 4v4、1v1、弱网、重复操作、权限和响应式。
- 弱网写请求不再把“服务器是否收到操作”和“当前 WebSocket 是否在线”混为一谈：恢复连接后明确提示结果未确认，相同操作沿用幂等号重试，权威快照或成功响应确认后清除。
- 4v4 八席按正反方分组，直接显示各方真人数和总席位数，长姓名和移动端信息更易读取。
- 管理后台模块写入 URL 查询参数，刷新、分享和故障协作可返回原模块；保留动态拆包、分页和请求去重。
- skip link 激活后真正把焦点移到主内容，同时保留原生 hash、历史和滚动。
- 真人文字或录音在比赛结果/复核窗口迟到恢复时，重新排队本场归档，避免归档遗漏学生最终证据；终止和取消仍保持不可写。
- 真人提交与终止同拍、迟到 Agent 与人工跳过、多房间隔离、隐藏大厅 WebSocket 拒绝路径均新增失败注入测试。
- 生产数据不变量审计全部通过；两个旧 QA 账号遗留的物化排行榜行已从权威结果重新构建并清除，追加式积分审计保留。

## 验证门禁

```text
API: 409 passed, 1 expected xfail, 2 third-party deprecation warnings
Web: 37 files, 260 passed
Deploy tests: 30 passed
Ruff: passed
ESLint/TypeScript/Knip/Next production build: passed
npm audit high: 0 vulnerabilities
Reliable audio baseline: 82 files verified
Reliable audio fingerprint: 3219138b22878e766c94b9a1fa0e422211a6f27dd74afbbb93a8e45f7e295285
```

唯一 xfail 是冻结 `IncrementalVoicePipeline` 在极窄取消同拍中可能不取走已经完成的 async-generator 异常。它解释了历史日志中的 `Task exception was never retrieved`，但修复点属于明确冻结的实时语音路径。本轮没有为消除测试标记而冒险修改 TTS/播放链；最小复现已经保留，等待独立音频变更窗口。

## 生产黑盒与性能

生产黑盒没有发现 Critical/High，发现的一个 Medium 弱网提示和一个 Low skip-link 焦点问题均已修复并在部署后复测。

agent-browser 抽样：

| 页面 | TTFB | LCP | CLS |
| --- | ---: | ---: | ---: |
| 首页 | 29.7ms | 348ms | 0.02 |
| 排行榜 | 35.5ms | 128ms | 0.06 |
| 登录 | 20.1ms | 76ms | 0.05 |

公开接口 50 并发持续复测中，排行榜连续五轮平均延迟中位数约 253ms，最大 p95 约 561ms。数据全量重建结束后的第一次观测曾出现一次 5 秒峰值，因此运维上禁止在大量学生同时访问时执行全榜重建；没有为一次瞬时争用引入会造成赛果陈旧的 TTL 缓存。

## 开源实现对照

本轮通过 GitHub 对照 Colyseus 的恢复席位清理、boardgame.io 的每次状态变化后终局检查和 Lichess 的服务端断线宽限。当前服务端权威席位、追加式事件、恢复申请、终局写保护和引擎重启恢复与这些原则一致；只吸收风险模型和测试思路，没有复制第三方代码。

## 发布

- GitHub 分支：`backup/production-20260720-round11`
- 应用提交：`cd3494cc53038fcb540ede41346aa36153375648`
- API/Web release：`round11-weaknet-recovery-20260720`
- 数据库 schema：`0025_speech_result_pagination`，本轮无结构迁移。
- API primary/secondary、Web、Engine、Worker 已滚动切换。
- 发布前 `active_match_processing=false`；没有中断真实比赛。

部署后使用新的 4v4 QA 房间完成：八席正反方分组、skip-link 主内容焦点、离线认领、连接恢复、未确认提示、相同幂等号重试和单次认领验证。房间随后关闭并标记 QA，账号停用并撤销会话；没有开赛、授权麦克风或触发 Agent/TTS/ASR。

## 音频保护

没有修改 DebateStage、MOSS/TTS、LiveKit、AudioWorklet、PCM、浏览器播放队列、模型、音色、Prompt 或音频参数。同步、构建和部署后均验证冻结清单，82 文件指纹保持一致。

## 服务器恢复集

- V2 DB：`auto-20260720T101139Z.dump`，临时恢复 24 张表并逐表比对。
- Debate Agent DB：`agent-20260720T101201Z.dump`，临时恢复 13 张表并逐表比对。
- 数据卷：逐文件重新计算 380 个 SHA-256，与已隔离恢复验证的 `20260720T0930Z-round10-data-volumes.tar.gz` 完全一致，因此复用该不可变归档，没有再复制约 394MB 的相同数据。
- 私密配置、可靠语音运行包和 MOSS 离线包未变化，继续复用各自已校验 SHA-256。
- 绑定清单：`recovery-set-20260720T1013Z-round11.manifest`，六个文件均重新计算 SHA-256。

Mac 上没有数据库 dump、数据卷 tar、恢复 manifest、浏览器截图、视频、Cookie、HAR 或 trace；源码恢复点只保存在 GitHub 快照分支。
