# Round 10 集成、部署与恢复报告

日期：2026-07-20

## 本轮结果

- 完成真实生产黑盒测试：注册、登录、4v4 双真人大厅、抢座、准备、断网返回、个人历史、排行榜、移动端和权限边界。
- 等待大厅不再向匿名用户公开真人姓名、席位和准备状态；登录用户仍可凭六位房间号加入公开大厅，正式开赛后继续支持匿名观战。
- 停用账号会立即撤销会话并释放大厅席位；比赛中的停用辩手由 AI 接替，必要时自动把房主权限交给有效真人。
- 终止或取消比赛后，迟到的发言请求不能继续修改逐字稿和归档。
- 建房必须明确选择本人席位；控制台越权、错误房间、移动导航、排行榜移动布局、弱网重复请求和后台筛选反馈均已修复。
- 管理后台新增正式比赛 CSV 索引，默认排除 QA 数据；用户可控字段经过公式注入防护。完整逐字稿和音频引用仍保存在逐场 JSON 归档中。
- 比赛归档现在保存 `room.is_test_data`。管理员重新分类账号或房间后会异步重建归档，排行榜、导出索引和逐场归档保持一致。
- 37 场终局比赛已全部重建并通过正文、元数据和 SHA-256 校验。

## 验证门禁

```text
API full regression: 403 passed, 2 third-party deprecation warnings
Web unit/component: 36 files, 253 passed
Production Playwright: 8 passed, 2 skipped (no active public match)
Deploy tests: 30 passed
Ruff: passed
ESLint/TypeScript/Knip/Next production build: passed
Reliable audio baseline: 82 files verified
Reliable audio fingerprint: 3219138b22878e766c94b9a1fa0e422211a6f27dd74afbbb93a8e45f7e295285
```

第一次生产 E2E 运行发现旧断言把“赛事标签不得换行”错误等同于“高度必须不超过 48px”。页面实际为 50px、`nowrap`，人工黑盒确认没有换行且更符合触控尺寸。断言改为 52px 上限后，桌面和 390px 两组均通过；没有缩小产品按钮。

## 音频保护

本轮没有修改 DebateStage、LiveKit、AudioWorklet、PCM 播放、MOSS/TTS 模型、Prompt、音色或浏览器播放链路。

服务器源码同步后，冻结门禁发现一个未纳入 Git 的 `egg-info/SOURCES.txt` 曾被包工具重新生成，多出两条源码索引且多一个末尾换行。发布因此被主动阻止。恢复该生成元数据至冻结清单记录后，82 文件指纹重新一致才继续部署；没有绕过或更新音频基线。

## 生产部署

- GitHub 分支：`backup/production-20260720-round10`
- 应用构建提交：`1f9c47703bdebab765a747436c3caa64abc1e791`
- E2E 门禁修正提交：`1625546e670a589ad219b6ce2dfeddb4a0ef8159`
- API/Web release：`round10-scale-privacy-20260720`
- 数据库迁移：`0025_speech_result_pagination`，本轮不新增结构迁移。
- API primary/secondary、Web、Engine、Worker 已完成切换。
- 部署前后 `active_match_processing=false`；最终 readiness 中数据库、Redis、Engine、Worker、MOSS、FunASR、存储和备份均为绿色。

部署后专门创建一个公开等待房间，确认匿名 REST 读取返回 401、登录用户搜索加入正常，随后关闭房间。所有 Round 10 黑盒和部署后账号已标记为测试、停用并撤销会话；房间已取消并标记 QA。

## 新服务器恢复材料

- V2 DB：`auto-20260720T092837Z.dump`，临时恢复 24 张表并逐表比对。
- Debate Agent DB：`agent-20260720T092858Z.dump`，临时恢复 13 张表并逐表比对。
- 数据卷：`20260720T0930Z-round10-data-volumes.tar.gz`，隔离恢复并逐文件校验 380 个文件。
- 绑定清单：`recovery-set-20260720T0932Z-round10.manifest`，六个恢复文件重新计算 SHA-256 后全部通过。

数据库、数据卷、私密配置、模型和清单只保存在新服务器受限目录。Mac 上没有 dump、数据卷 tar、恢复 manifest、Playwright 截图或 trace；源码备份只使用 GitHub 分支。
