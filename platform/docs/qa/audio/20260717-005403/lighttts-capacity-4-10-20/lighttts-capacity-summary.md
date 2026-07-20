# LightTTS 4/10/20 房容量专项汇总

## 证据边界

本轮使用远端独立端口 `127.0.0.1:18081` 的可控 mock LightTTS endpoint，并运行真实 V2 `LightTTSProvider`、Redis admission gate、文本切分、HTTP、WAV 校验/合并、原子落盘、取消与超时逻辑。所有场景使用独立 Redis key prefix；开始前 `processing_rooms=0`、生产 gate `active=0/queue=0`，结束后生产 gate 仍为 `0/0`。未启动第二个 GPU 模型、未修改生产配置、未部署。

因此，本轮只证明排队机制和当前配置的接纳边界，不产生真实 LightTTS 模型吞吐、RTF、音质、自然度或 GPU 容量结论。真实模型单路耗时仍引用既有隔离基准，而不能用这里约 0.25 秒的 mock service time 替代。

## 机制容量：pending 上限随突发量放开

| 同步突发 | 接受/拒绝/超时 | Queue P50/P95 | E2E P50/P95 | 最大队列/最长等待 | FIFO | 有效 WAV |
|---:|---:|---:|---:|---:|---|---:|
| 4 | 4/0/0 | 0.650/1.111s | 0.944/1.404s | 3/0.966s | 0 次越序 | 4/4 |
| 10 | 10/0/0 | 1.700/3.141s | 1.946/3.386s | 9/3.153s | 0 次越序 | 10/10 |
| 20 | 20/0/0 | 3.455/6.465s | 3.699/6.713s | 19/6.463s | 0 次越序 | 20/20 |

机制场景合计 34/34 容量任务成功，`.part` 残留为 0，最终 gate `active=0/queue=0`。另行验证：queued cancellation 1 条按预期移除；active cancellation 1 条等待 endpoint 返回后丢弃产物；queue timeout 1 条只清理等待者。三者均无 ghost key。

## 当前生产参数：active=1、max pending=2、queue timeout=20s

| 同步突发 | 接受 | 队满拒绝 | 超时 | 拒绝率 | 已接受任务有效 WAV |
|---:|---:|---:|---:|---:|---:|
| 4 | 2 | 2 | 0 | 50% | 2/2 |
| 10 | 2 | 8 | 0 | 80% | 2/2 |
| 20 | 2 | 18 | 0 | 90% | 2/2 |

这是同步突发的实测接纳结果。配置的稳态理论上限是 1 active + 2 pending；但在首个请求尚未从 Redis queue 转为 active 的同一突发窗口，queue 本身先达到 2，因此本轮每档只有 2 个请求被接受，其余立即 `queue_full`。mock 很快，已接受请求没有触发 20 秒 timeout；真实模型下等待只会更长，不能据此推断真实 timeout 为 0。

## 产品判定

- FIFO、公平性、取消、超时清理、房间目录隔离和原子 WAV 机制通过。
- 当前 `max_pending=2` 对课堂同步开赛是明确的 **P1 容量/体验缺口**：4/10/20 个房间同时需要 TTS 时，本轮机制实测立即拒绝率为 50%/80%/90%。
- 现有产品只有全局 `queue_depth` 和 `oldest_wait_seconds`，没有每个房间/任务的 queue position 或经过真实模型校准的 ETA。
- 本轮新增纯 admission FIFO 等待估算：可报告下一任务位置；只有提供环境实测 service P50/P95 时才返回 ETA，未校准时显式返回 `None`，避免虚假承诺。
- 在扩大 pending 上限前，需要定义课堂降级策略、逐房公平配额、取消入口和教师可见容量闸门；单纯把 timeout 加长会把即时拒绝变成长时间无反馈。

## 权威产物

- 机制容量：[JSON](mechanism/lighttts-capacity-4-10-20.json)、[Markdown](mechanism/lighttts-capacity-4-10-20.md)、`mechanism/audio/`
- 当前生产配置：[JSON](production-config/lighttts-capacity-4-10-20.json)、[Markdown](production-config/lighttts-capacity-4-10-20.md)、`production-config/audio/`
- 原始归档 SHA-256：`db29ff8b217f2d719dfe17852d094e067c60271d9ac3aa2082958c74e26073aa`
- Admission 定向回归：12 passed；Ruff 与 py_compile 通过。
