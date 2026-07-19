# Iteration 26：Debate Agent 首内容快速路径生产发布

发布时间：2026-07-17 21:40–21:42 CST。Release：`20260717T2140-agent-fastpath`。

## 结果

- Qwen3 直连发言路径由服务端按模型预设控制 `enable_thinking`；预设未填写时默认 `false`，客户端请求不能覆盖。Judge 与 `debate_api` 回退路径不受影响。
- 生产真实 SSE canary：连接打开 160.4ms，首个非空 delta 3375.9ms，总耗时 4673.6ms，30 个内容块、129 字，正常收到 `[DONE]`，未发现 think/reasoning 泄露。
- 相比发布前同配置首内容 21173.2ms，首内容下降 84.1%，约快 6.27 倍；总耗时由 22446.7ms 降至 4673.6ms，下降 79.2%，约快 4.8 倍。
- canary task 已完成，Agent running task=0；主 V2 无 preparing/running/paused/judging 房间，`active_match_processing=false`。

## 门禁与部署

- 发布前连续两次确认 Agent running task=0、主 V2 活动房=0。
- 本地 Agent API 定向测试 10 passed，Ruff 与 py_compile 通过。
- 远端使用生产同版本依赖完成 staging import/compile；18401 shadow health ready，DB/Redis 和 2 个 active provider 正常。
- 只原子替换 `apps/api/app/agent_engine.py` 与 `apps/api/app/seed.py`，只重启 `jixia-agent-api`；Agent Web、backup、主 V2、Nginx、环境变量和数据库 schema 均未修改。
- 发布后 Agent 本机/公网 health 连续 5/5；Alembic 保持 `0001_initial (head)`；API stderr 仅新增 Alembic 正常 informational 日志，无 ERROR/Traceback/CRITICAL。
- Agent/V2 七个 Supervisor 服务均 RUNNING；主 V2 ready 为 schema `0019_audio_streaming`，Worker queue/dead letter 0，LightTTS gate 0/0。

## 发布包、备份与回滚

- 发布包：`/tmp/debate-agent-20260717T2140-agent-fastpath-source.tar.gz`
- 发布包 SHA-256：`4aee1c3d0f57f738ba7015ec702cd18a96e6b2cd74859e32e13084882d075212`
- Agent DB 备份：`/home/ubuntu/sunsq/debate-agent/backups/agent-20260717T134123Z.dump`
- DB 备份 SHA-256：`2594622e395481d24a40cfe8f613a4621b722dd1cfe218425bfd5f94e5ddcf15`
- 源码回滚包：`/home/ubuntu/sunsq/debate-agent/runtime/deploy-backups/20260717T2140-agent-fastpath-before-source.tar.gz`
- 源码回滚包 SHA-256：`881ccff47d6ddcbaf03977c15bf0b7d20d43d5f4094add6fa392a9d1bf841f43`
- 回滚包包含原生产 `agent_engine.py` 与 `seed.py`；恢复后只需重启 `jixia-agent-api` 并复核 health、schema、日志和 running task。

## 证据

- [真实 SSE canary JSON](iteration26-agent-fastpath-canary.json)

第一次备份尝试在生产切换前停止：`backup.same-host.sh` 以 `ubuntu` 身份从 `/root` 启动，脚本清理阶段无法恢复工作目录。生产源码与服务没有变化。修正为先进入 Agent 根目录后，第二次完整通过；未触发生产源码回滚。
