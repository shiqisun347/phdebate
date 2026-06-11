# 10 · Security And Deploy

本章定义 MVP 的权限、安全、紧急停止和现场部署要求。

## 1. 权限模型

MVP 使用轻量 Bearer token/口令，不建设复杂账号系统。开发态默认不强制鉴权；`PHDEBATE_ENV=production` 或 `PHDEBATE_AUTH_REQUIRED=1` 时启用。

| 身份 | 获取方式 | 权限 |
| --- | --- | --- |
| `admin` | 管理员口令/token | 配置、控制、投票、干预 |
| `host` | 主持人口令/token | 控制、投票、干预，不改敏感配置 |
| `screen` | 只读 token 或局域网白名单 | 只读大屏状态 |
| `speaker` | 每位辩手独立临时 token | 只控制本人 |
| `audience` | 投票 token 或弱指纹 | 只提交学生票 |

所有 token 只存哈希，不在数据库保存明文。

## 2. 敏感信息

不得下发到前端：

- 讯飞 `APP_ID`、`API_KEY`、`API_SECRET`
- Agent 共享密钥
- 外部模型 API Key
- 管理员/主持人口令哈希

管理端可以显示 Agent URL 和模型名称，但不显示密钥。

## 3. 审计日志

以下操作必须写 `audit_logs`：

- 比赛开始、暂停、继续、结束、紧急停止。
- 环节开始、跳过、回退。
- 指定发言人、强制结束发言、锁定麦克风。
- Agent 中断、重试、人工代输入。
- 时钟校准。
- 修正 transcript、标记发言无效。
- 录入、修改、公布投票结果。
- 修改比赛配置。

审计字段至少包含 actor、action、target、request、result、error。

## 4. 紧急停止

`POST /api/matches/{match_id}/emergency-stop` 必须是最高优先级控制。

执行顺序：

1. 标记比赛进入 `intervention` 或直接保持当前状态但设置 emergency flag。
2. 停止所有 running clocks。
3. 关闭所有 Agent SSE。
4. 调用 Agent interrupt。
5. 停止 TTS 播放队列。
6. 锁定所有辩手麦克风按钮。
7. 广播 `match.emergency_stopped`。
8. 写审计日志。

紧急停止后，主持人可以选择：

- 恢复比赛。
- 跳过当前环节。
- 人工代输入。
- 结束比赛。

## 5. 部署拓扑

### 5.1 开发

```text
frontend dev server :5174
backend FastAPI     :8000
mock agents         :8100-8103
SQLite              :apps/backend/storage/phdebate.sqlite3
```

Vite 代理：

- `/api` -> `http://localhost:8000`
- `/ws` -> `ws://localhost:8000`

### 5.2 现场

```text
host machine
  FastAPI :8000
  static frontend
  SQLite database
  audio archive
  optional mock/real agents
```

所有设备访问：

```text
http://<host-ip>:8000
```

推荐启动命令：

```bash
npm run serve
```

该命令先构建 `apps/frontend/dist`，再启动 FastAPI。现场模式下同一个 `:8000` 服务同时提供：

- REST API：`/api/...`
- WebSocket：`/ws/matches/{match_id}`
- 前端静态资源：`/assets/...`
- SPA 页面：`/screen`、`/admin`、`/console/{speaker_id}`、`/vote/{match_id}`

开发调试仍使用 `npm run dev`，页面访问 `:5174`，API 和 WebSocket 由 Vite 代理到 `:8000`。

建议现场固定 host IP，关闭系统休眠，接入稳定电源和有线网络。

## 6. 环境变量

```text
PHDEBATE_ENV=production
PHDEBATE_AUTH_REQUIRED=1
PHDEBATE_BASE_URL=http://<host-ip>:8000
PHDEBATE_DATABASE_URL=sqlite:///storage/phdebate.sqlite3
PHDEBATE_AUDIO_DIR=storage/audio
PHDEBATE_TOKEN_FILE=storage/tokens.json
PHDEBATE_ADMIN_PASSWORD=
PHDEBATE_HOST_PASSWORD=
PHDEBATE_SCREEN_TOKEN=
PHDEBATE_SPEAKER_TOKEN=
PHDEBATE_SPEAKER_TOKENS={"spk_aff_3":"...","spk_neg_2":"..."}
PHDEBATE_AGENT_SHARED_TOKEN=
XFYUN_APP_ID=
XFYUN_API_KEY=
XFYUN_API_SECRET=
XFYUN_ASR_URL=
XFYUN_TTS_URL=
XFYUN_ASR_OPEN_TIMEOUT_S=8
XFYUN_ASR_CLOSE_TIMEOUT_S=3
XFYUN_ASR_FINAL_TIMEOUT_S=12
PHDEBATE_ASR_REALTIME=
PHDEBATE_ASR_AUTO_RECOGNIZE=
PHDEBATE_TTS_FORMAL=
```

访问方式：

- REST：`Authorization: Bearer <token>`。
- WebSocket：浏览器无法自定义握手 header，使用查询参数 `?token=<token>`。
- 页面：管理端、大屏、辩手端在收到 401 后显示口令输入；也支持首次打开时带 `?token=...` 写入本机。
- 学生投票页不要求后台访问 token，只通过投票 token/浏览器弱指纹防重复。
- 管理端设置页提供“现场入口分发”，可在主持人浏览器本地填写现场主机地址和各类 token，生成入口链接、二维码与打印分发清单。页面也可生成/轮换本地 token 套件、批量导入 JSON 或 `PHDEBATE_*` 环境变量片段，并复制生产启动所需的环境变量或下载只含 SHA-256 哈希的 token 文件。token 不从后端明文下发，页面生成的新 token 只有在使用同一组环境变量或 `PHDEBATE_TOKEN_FILE` 重启生产服务后才会被后端接受。

生产鉴权支持两种方式：

- 快速环境变量：`PHDEBATE_ADMIN_PASSWORD`、`PHDEBATE_HOST_PASSWORD`、`PHDEBATE_SCREEN_TOKEN`、`PHDEBATE_SPEAKER_TOKEN`、`PHDEBATE_SPEAKER_TOKENS` 可直接写明文 token，适合临时彩排。
- 推荐哈希文件：将管理端下载的 token 文件保存为 `storage/tokens.json`，设置 `PHDEBATE_TOKEN_FILE=storage/tokens.json`。文件只包含 `sha256:<hex>`，字段包括 `admin_hashes`、`host_hashes`、`screen_hashes`、`speaker_shared_hashes`、`speaker_hashes`。

现场分发顺序建议：

1. 在主机上启动 `npm run serve`。
2. 管理端打开 `/admin?match_id=match_001` 并输入管理员或主持人口令。
3. 在“比赛设置 / 现场入口分发”中填写 `http://<host-ip>:8000`。
4. 填入现有 token，或点击“生成/轮换 token”生成新清单。
5. 推荐点击“下载 token 文件”，保存为 `storage/tokens.json`，并用 `PHDEBATE_TOKEN_FILE=storage/tokens.json PHDEBATE_AUTH_REQUIRED=1 npm run serve` 重启服务；临时彩排也可复制明文环境变量片段启动。
6. 重新打开管理端并导入同一组明文 token 配置，确认链接、二维码和打印清单一致。哈希 token 文件不能反推出明文链接，需保管好生成时的分发清单。
7. 点击“打印清单”生成只包含现场入口的 A4 分发页；纸质清单包含完整 token，仅交给对应设备负责人和辩手本人。
8. 复制、扫码或裁切纸质清单分发大屏、辩手端、学生投票入口。

## 7. 备份与导出

现场演练和正式比赛前后都要备份：

- SQLite 数据库文件。
- `storage/audio/`。
- 导出的 transcript JSON/CSV。
- 事件日志导出。

推荐导出结构：

```text
exports/{match_id}/
  match.json
  transcript.json
  transcript.csv
  events.jsonl
  audit_logs.jsonl
  votes.json
  audio_manifest.json
  audio/
```

## 8. 网络与浏览器要求

- 大屏电脑使用 Chromium/Chrome 全屏播放。
- 辩手电脑使用 Chrome 或 Edge，提前授权麦克风。
- 现场网络必须允许访问讯飞云 API；如不可达，提前切换 ASR/TTS 降级方案。
- 后端与 Agent 机器之间延迟应稳定，建议同一局域网。

## 9. 最低硬件建议

| 角色 | 建议 |
| --- | --- |
| 后端/主持人主机 | 16GB RAM，稳定 CPU，足够磁盘写音频 |
| 大屏设备 | 可全屏浏览器，HDMI/投屏稳定 |
| 辩手设备 | 每位人类辩手一台，麦克风可用 |
| 网络 | 独立路由器或有线交换机 |

## 10. 现场风险清单

- 讯飞账号额度或并发不足。
- 浏览器麦克风权限未授权。
- 投屏分辨率不是 16:9 导致文字裁切。
- TTS 自动播放被浏览器策略阻止。
- Agent 首字延迟过高导致自由辩论冷场。
- 主持人误触回退或紧急停止。

以上风险必须在 `11-testing-acceptance.md` 的现场演练中覆盖。
