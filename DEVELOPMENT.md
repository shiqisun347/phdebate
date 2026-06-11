# Development Quickstart

本项目当前已经搭出一个可运行的前后端纵向切片：

- 后端：FastAPI，默认端口 `8000`
- Mock Agent：FastAPI，默认端口 `8100`
- 前端：Vite + React，默认端口 `5174`
- Demo 比赛：`match_001`
- 热更新：Vite 负责前端 HMR，Uvicorn `--reload` 负责后端自动重载

## 一键启动

```bash
npm run dev
```

首次启动会自动：

1. 创建 `.venv`
2. 安装 `apps/backend/requirements.txt`
3. 安装 `apps/frontend/package.json`
4. 启动 Mock Agent、FastAPI 和 Vite

## 页面入口

- 大屏：<http://localhost:5174/screen?match_id=match_001>
- 管理端：<http://localhost:5174/admin?match_id=match_001>
- 辩手端：<http://localhost:5174/console/spk_aff_3?match_id=match_001>
- 学生投票：<http://localhost:5174/vote/match_001>

## 常用命令

```bash
npm run dev:backend
npm run dev:agent
npm run dev:frontend
npm run build:frontend
npm run serve
npm run check:docs
npm run smoke
npm run smoke:browser
```

`npm run smoke` 会对当前 `PHDEBATE_BASE_URL` 或默认 `http://127.0.0.1:8000` 执行一段小型现场彩排，覆盖大屏切换、人类发言 ASR 模拟、学生/评委投票、结果公布和导出下载。生产鉴权开启时可设置 `PHDEBATE_SMOKE_TOKEN=<host_or_admin_token>`。

`npm run smoke:browser` 默认覆盖管理端、大屏、辩手端和投票页。若当前 Chrome/Chromium 环境支持 headless fake microphone，可设置 `PHDEBATE_BROWSER_AUDIO_SMOKE=1` 额外验证辩手端 PCM/L16 录音归档；不支持时保持默认即可，真实麦克风仍在现场彩排中验证。

`npm run smoke:browser` 会用本机 Chrome/Chromium 打开管理端、大屏、辩手端和学生投票页，检查关键 UI 文案，触发“语音链路 / 配置检查”，并提交一次学生投票。默认访问 `http://127.0.0.1:5174`；可用 `PHDEBATE_BROWSER_BASE_URL`、`PHDEBATE_BROWSER_API_URL`、`PHDEBATE_BROWSER_SMOKE_TOKEN` 和 `PHDEBATE_BROWSER_EXECUTABLE` 覆盖。

## 生产预览 / 现场单机入口

```bash
npm run serve
```

该命令会先构建 `apps/frontend/dist`，再由 FastAPI 在 `:8000` 同时托管 API、WebSocket 和静态前端。

- 大屏：<http://localhost:8000/screen?match_id=match_001>
- 管理端：<http://localhost:8000/admin?match_id=match_001>
- 辩手端：<http://localhost:8000/console/spk_aff_3?match_id=match_001>
- 学生投票：<http://localhost:8000/vote/match_001>

如需在本机模拟现场鉴权：

```bash
PHDEBATE_ENV=production \
PHDEBATE_ADMIN_PASSWORD=admin \
PHDEBATE_HOST_PASSWORD=host \
PHDEBATE_SCREEN_TOKEN=screen \
PHDEBATE_SPEAKER_TOKENS='{"spk_aff_3":"speaker"}' \
npm run serve
```

页面也可通过 URL 一次性写入本机 token，例如 `?token=host`、`?token=screen`、`?token=speaker`。

## 当前实现状态

已完成：

- FastAPI API 骨架
- Demo 比赛状态与 SQLite 快照/事件持久化
- WebSocket snapshot 和事件广播
- Agent Gateway HTTP/SSE 客户端与 mock agent
- ASR partial/final 和 TTS 降级联调链路
- 讯飞 WebAPI 适配基础层：HMAC-SHA256 签名 URL、ASR/TTS payload 构造和返回解析单元测试
- 管理端 ASR 自检：后端真实 WebSocket Gateway、短 PCM 连接自检、失败降级状态更新
- 管理端 TTS 试合成：后端真实 WebSocket Gateway、音频落盘、失败降级状态更新
- 管理端归档补识别：PCM/L16 音频归档触发讯飞 ASR、回填 transcript，webm/opus 给出格式降级提示
- 辩手端 PCM/L16 录音归档：Web Audio 采集 16kHz Int16 PCM 分片，浏览器不支持时退回 webm 留档
- PCM/L16 分片实时 ASR 可观测状态：上传即广播 `asr.audio_chunk_received`，完成归档可显式或配置驱动自动补识别
- 讯飞 ASR 实时长连接会话骨架：PCM 分片进入 `open_stream`，回调写入 partial/final，失败降级到录音归档和补识别
- ASR 现场联调保护：连接/关闭/final 超时可配置，结束帧后 final 超时会明确降级为 `asr.failed`
- AI 正式发言 TTS 归档：Agent final 文本调用讯飞 TTS，生成音频写入 `audio_assets(source=agent_tts)`，失败时保留 transcript 并降级纯文字
- 管理端赛前体检报告：后端聚合比赛基础、页面设备、Agent、语音、投票、导出和生产鉴权检查项
- FastAPI 托管生产版前端静态资源
- 生产态轻量 Bearer token 鉴权和页面口令输入
- 管理端现场入口分发：链接复制、本地二维码、打印分发清单、token 生成/导入、环境变量复制和哈希 token 文件生成
- 管理端现场演练清单、`npm run smoke` 核心链路彩排和 `npm run smoke:browser` 浏览器页面彩排
- 管理端赛制配置：环节名称/时长、自由辩论每方总时长和单次上限
- 管理端评委票真实录入表单：立论/过程/结辩、优胜方、最佳辩手
- 管理端 AI 干预闭环：健康检查、重试、中断、人工代输入并写入 transcript
- 管理端语音配置诊断：讯飞 ASR/TTS 环境变量检查、音频目录可写性检查和 mock 降级提示
- 大屏中场真实投票二维码和结果页公布顺序门控
- 大屏、管理端、辩手端、投票页入口
- Vite 热更新开发环境

下一步：

- 用真实讯飞账号和现场麦克风验证 ASR 长连接 partial 延迟，并根据结果调优分片大小
- 为 MediaRecorder webm/opus 保底归档增加后端转码为 PCM/L16 的可选链路
- 将已归档的 AI TTS 音频接入现场扩声/浏览器播放队列，并校准首声计时
