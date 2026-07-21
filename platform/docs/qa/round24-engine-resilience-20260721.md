# Round 24 比赛引擎韧性与观战隐私审计

日期：2026-07-21  
范围：多人类与多 Agent、多房间并发、异常暂停与恢复、真人断线接替、跨房间隔离、20 人观战上限、观战文字隐私。  
约束：未修改冻结的 TTS、MOSS、LiveKit 音频和浏览器播放路径。

## 结论

- 4v4 多真人/多 Agent、1v1 人人、1v1 人机可并发创建、抢座、准备和启动，房间状态与控制权限保持隔离。
- 引擎对单房间异常采用有界退避和单房间隔离；连续异常不会阻塞健康房间。
- 真人断线 60 秒保护、AI 接替、重新连接、房主审批/管理员恢复路径均有自动化覆盖。
- 观战容量由 Redis Lua 原子占位实现，房间级上限为 20；生产 Redis 不可用时新观众连接失败关闭，不会在双 API Worker 间放宽上限。
- 发现并修复一项高优先级观战隐私问题：观众虽然收到 `can_view_transcript=false`，此前仍可从 `caption_segments` 和实时 `asr`/`caption.segment` 事件取得逐句文字。修复后匿名观众和登录但未参赛的观众均只获得音频播放身份、时序和回退音频元数据，所有正文与字幕字段为空或被移除。
- 发现并协同修复开赛/异常重试前缺少 MOSS 已暖机门禁：同一 readiness 门禁现已覆盖 `start` 与服务异常 `retry`；未就绪返回 503 + `Retry-After: 5`，并保持原房间、席位、Match 和失败发言完全不变。

## 自动化测试证据

### 核心并发与恢复回归

```bash
cd platform/apps/api
../../.venv/bin/python -m pytest -q \
  tests/test_round18_engine_scenarios.py \
  tests/test_round16_engine_resilience.py \
  tests/test_round15_engine_soak.py \
  tests/test_multi_room_simulation.py
```

结果：`36 passed`。

覆盖内容包括：

- 4v4 日常赛四名真人与四名 AI、1v1 人人、1v1 人机同时运行。
- 同席位并发抢座仅一个请求成功，重复开始幂等。
- 20 房间、100+ 发言、并行 Agent 有界调度、暂停/恢复、裁判重试和引擎重启恢复。
- 单房间 Provider/数据库/状态机异常退避与隔离。
- 跨房控制返回 403，事件与服务快照不串房。

### WebSocket、Presence 和 20 人观战

```bash
cd platform/apps/api
../../.venv/bin/python -m pytest -q \
  tests/test_realtime_hub.py \
  tests/test_round18_engine_scenarios.py::test_twenty_spectators_are_room_scoped_and_all_repair_roles_remain_exempt \
  tests/test_round18_engine_scenarios.py::test_disconnect_grace_ai_substitution_and_real_websocket_restore \
  tests/test_round18_engine_scenarios.py::test_4v4_human_ai_1v1_human_human_and_1v1_human_ai_run_in_parallel
```

结果：`12 passed`。

验证了：

- 20 个观众席位在多 API Worker 间原子共享，第 21 个连接关闭码为 `4429`。
- 观众断开或租约到期后容量可以回收。
- 参赛者、房主和系统管理员不占观众额度，其他房间容量互不影响。
- Presence 多设备引用计数与 Redis 租约只在最后一个设备断开时标记离线。

### 修复后的综合回归

```bash
cd platform/apps/api
../../.venv/bin/python -m pytest -q \
  tests/test_platform.py \
  tests/test_round20_captions.py \
  tests/test_round18_engine_scenarios.py \
  tests/test_round16_engine_resilience.py \
  tests/test_round15_engine_soak.py \
  tests/test_multi_room_simulation.py \
  tests/test_realtime_hub.py
```

结果：`224 passed`，仅有 Starlette TestClient/httpx2 迁移提示，不影响行为。

### MOSS 冷启动门禁与观众隐私联合回归

```bash
cd platform/apps/api
../../.venv/bin/python -m pytest -q \
  tests/test_round24_start_readiness.py \
  tests/test_round20_captions.py
```

结果：`8 passed`。

- MOSS 0 个 endpoint 暖机时，start 返回 503，房间仍在 lobby，未创建 Match、未填充/锁定空席。
- 至少 1 个 endpoint 暖机时可以开赛；已成功提交的 start 幂等重放不受后续瞬时 readiness 变化破坏。
- 异常 retry 在 MOSS 未暖机时保持 paused、failure_reason、failed speech 和事件日志不变；暖机后同一请求正常进入 preparing，并把失败发言标记为 `failed_retried`。

新增/收紧的断言：

- 匿名 REST 观战、匿名 WebSocket、登录非参赛观众 REST/WebSocket 均不得收到字幕正文。
- `caption_segments == []`。
- `active_speech.content == ""`，历史 `speeches[*].content == ""`。
- `speech.completed` 公共事件不含 `content`；`asr` 和 `caption.segment` 公共实时事件不含正文、语音 ID、分段 ID、起止时间或 timing basis。
- 参赛者和控制者的非公开投影保持原有字幕/文本能力。

## 生产只读证据

```bash
curl -k -fsS https://117.50.192.216/api/health/ready
curl -k -fsS https://117.50.192.216/api/rooms/296152/public
```

检查时：

- database、schema、Redis、engine、worker、MOSS、FunASR、storage、backup 全部健康。
- MOSS：1/1 endpoint ready，`model_warmed=true`，无 pending/orphan。
- 房间 `296152` 已恢复为 `running`，由自由辩论进入反方四辩总结；多段既有发言均为 completed。
- 该响应直接暴露了修复前的 `caption_segments` 正文，构成此次隐私修复的生产复现证据。

生产观众容量只读压力：

```bash
VERIFY_BASE_URL=https://117.50.192.216 \
VERIFY_ROOM_CODE=296152 \
VERIFY_CONNECTIONS=20 \
VERIFY_HOLD_SECONDS=2 \
platform/.venv/bin/python platform/deploy/verify_websocket_load.py
```

当时已有 2 名观众，测试新建 18 个连接后总数达到 20，另外 2 个连接均以 `4429` 被拒绝。测试连接随后全部关闭。这证明生产上限按“房间内所有现存观众”计算，而不是允许每个压测批次额外进入 20 人。

## 冻结音频基线

```bash
platform/.venv/bin/python platform/scripts/create_reliable_audio_manifest.py \
  --verify platform/backups/reliable-audio-20260719-declick/reliable-audio-baseline.json
```

结果：

```text
audio_baseline_verified files=82 fingerprint=3219138b22878e766c94b9a1fa0e422211a6f27dd74afbbb93a8e45f7e295285
```

## 修改文件

- `apps/api/app/services/room_service.py`
  - 公共投影不再查询或序列化 Caption/TranscriptSegment。
  - 匿名事件白名单移除 `content`、`text`。
  - 公共实时 ASR/字幕事件仅保留无正文的事件序号外壳。
- `apps/api/tests/test_round20_captions.py`
  - 增加匿名和登录观众的严格无文字断言。
- `apps/api/tests/test_platform.py`
  - 更新匿名与登录观众 WebSocket 隐私契约。

## 尚未覆盖或需持续观察的风险

1. 生产 MOSS 冷启动首次 TorchInductor 编译可能持续约 20 分钟。新的 start/retry readiness 门禁能防止比赛进入失败流程，但仍需在部署后用真实未暖机场景验证提示文案和按钮恢复。
2. Redis 是全局观战容量和跨 Worker Presence 的权威协调层。生产当前采用 fail-closed；Redis 长时不可用时新观众会暂时无法进入，这是安全优先的预期降级，需要 UI 显示明确重试提示。
3. 现有压力测试覆盖 20 房间与 20 人单房间观战，但未在本轮重新执行 500 个长连接、持续数小时的公网弱网测试；应保留为后续非高峰期 soak 项目。
4. 本轮没有触碰 TTS 或浏览器播放器，未重新评价音质、撕裂音和首音延迟；这些由冻结音频基线和既有专项验收继续约束。
5. 观众无文字修复必须随 API 新版本部署后重新请求生产 `/api/rooms/:code/public`，确认 `caption_segments=[]`，再用匿名与登录观众 WebSocket 各采样一次。
6. 当前观战页仍无条件挂载 `StageCaptionProjection`；后端不再给字幕后，它会显示“当前发言暂时没有逐句字幕”。该提示不泄露正文，但仍暗示观战具有字幕功能，应从 watch 页面移除该组件，参赛 debate 页面继续保留。
