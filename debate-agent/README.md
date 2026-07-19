# Jixia Debate Agent

独立于主辩论系统的 Agent 控制平面，当前正式入口为 `https://117.50.192.216/debate`。
主平台继续使用 `/admin`，独立 Agent 控制台使用 `/debate`，两者不会互相覆盖。

## 能力

- RESTful `POST /debate/api/debate`，请求字段兼容既有 Debate API，响应固定使用 SSE `delta.content` 与 `[DONE]` 格式。
- 辩手 LLM 固定使用上游流式生成，并由服务端强制传递 `enable_thinking=false`；客户端不能重新开启 Thinking，也不会收到推理过程。
- 延迟门通过的 OpenAI-compatible 辩手模型使用原始 SSE 快速路径，避免 LiteLLM 在首内容前的额外等待；裁判与非流式请求仍使用 LiteLLM。`DIRECT_OPENAI_STREAM_MODELS` 明确列出允许走快速路径的模型。
- 服务启动时默认发送 3 个不创建任务/记忆的极短原始 SSE warm-up，在 readiness 前吸收外部模型冷波；可用 `STARTUP_MODEL_WARMUP_*` 调整或关闭。
- 内部 `POST /debate/api/judge` 为主辩论系统提供 AI 裁判，复用主 LLM，但强制使用独立裁判规则和结构化 JSON 输出。
- LiteLLM 多 Provider、优先级、重试与 Fallback。
- `debate_api` 上游适配器可直接调用既有 RESTful Debate Agent，并把其 SSE 输出转换为平台统一的 JSON/SSE、幂等任务和日志。
- LangGraph 配置加载、Memory 检索、Prompt 渲染与持久化流程。
- 版本化 Prompt、消息模板、人设、模型参数和分层 Memory；PostgreSQL/pgvector 保存权威记录，启用 `MEM0_ENABLED` 后只把管理员批准的长期记忆同步到 Mem0，并按 `profile_key` 隔离检索。
- `task_id` 幂等、任务中断、请求日志、Gateway Key、多管理员和审计。
- LLM API Key 加密保存；Gateway Key 只保存哈希。

## 日常管理

登录 `/debate` 后默认进入简易设置，只需要维护接入方式、API 地址、模型名称和 API Key，并可直接执行连接测试。辩手列表用于查看人设与当前绑定模型。Prompt、Memory、消息模板、采样参数和审计记录保留在“进入高级设置”中，普通维护无需修改。

主模型使用仓库根目录 `readme.md` 末尾配置的 OpenAI-compatible LLM API。配置导入生产数据库时 API Key 会加密保存，管理接口和浏览器都不会返回明文。原有 RESTful Debate API 作为低优先级故障回退，客户端传入的 `model_name` 仅用于协议兼容，不能覆盖服务端配置。

## 本地启动

```bash
cp .env.example .env
docker compose up -d postgres redis
python -m venv .venv
.venv/bin/pip install -r apps/api/requirements.txt
PYTHONPATH=apps/api .venv/bin/uvicorn app.main:app --port 8400
cd apps/web && npm install && npm run dev
```

API 管理页由 Web 的 `/debate` 提供，开发时需使用反向代理保持同源。

## 生产部署

当前 `117.50.192.216` 与主辩论平台同机，生产环境采用独立 Supervisor 进程和本机端口部署，复用现有 PostgreSQL、Redis 与 HTTPS Nginx 基础设施，但使用独立数据库、数据库账号、Redis DB 和运行目录：

- API：`127.0.0.1:18400`
- Web：`127.0.0.1:13300`
- 数据库：`debate_agent`
- 入口：`/debate`
- Supervisor：`jixia-agent-api`、`jixia-agent-web`、`jixia-agent-backup`

同机发布 Next.js standalone 构建时，必须将 `apps/web/.next/static/` 复制到发布目录根部的 `.next/static/`。当前 standalone 入口是发布目录根部的 `server.js`，不能把静态文件放到 `apps/web/.next/static/`，否则页面 HTML 虽然返回 200，浏览器仍会因 CSS/JavaScript 404 无法加载。

对应配置位于 `deploy/supervisor.same-host.conf` 与 `deploy/nginx.same-host.locations.conf`。以下 Docker Compose 流程保留给未来迁移到独立服务器时使用。

1. 将目录同步到 `/opt/debate-agent`，执行 `deploy/generate-env.sh` 创建权限为 `0600` 的 `.env`。管理员密码和 Gateway Key 只会在创建时输出一次，应立即保存到密码管理器。
2. 执行 `deploy/bootstrap.sh`。脚本先启动 Postgres、Redis、API 和 Web，再使用 Certbot standalone 在 80 端口申请 `shortlived` IP 证书，最后启动正式 Nginx；不会因首启时证书文件不存在而循环失败。
3. 如需接收 ACME 通知，可在执行前设置 `ACME_EMAIL`。
4. 后续版本执行 `docker compose up -d --build`。
5. 每 12 小时运行 `deploy/renew-ip-certificate.sh`，每天运行 `deploy/backup.sh`。

主平台配置：

- Endpoint：`https://117.50.192.216/debate/api/debate`
- Health：`https://117.50.192.216/debate/api/health`
- Header：`X-Debate-Agent-Key`

裁判配置：

- Endpoint：`https://117.50.192.216/debate/api/judge`
- 主平台通过启用的 `JudgeProfile` 调用。
- Nginx 只允许同机比赛引擎访问裁判 Endpoint，公网客户端直接请求返回 403。
- 裁判请求中的 `model_name` 不能覆盖独立平台发布的主模型；管理员裁判说明会追加到服务端固定的安全与 JSON 格式规则之后。

不支持 Qwen3 8B，不使用 6016。

当前生产 Provider 顺序：

- 主 Provider：根目录 `readme.md` 末尾的 OpenAI-compatible LLM API。
- 回退 Provider：既有 `debate_api` RESTful 服务。
- 所有辩手人设绑定主 Provider；仅在主 Provider 调用失败并符合重试条件时进入回退。

`MEM0_CONFIG_JSON` 接受 Mem0 `Memory.from_config` 的 JSON 配置。默认关闭 Mem0 外部索引，不影响数据库内的比赛记忆、审核和检索；配置好 embedding/LLM 后再开启。
