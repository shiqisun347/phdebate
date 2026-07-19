# LightTTS 4/10/20 房隔离容量与排队体验

- 证据分栏：current-production-config-max-active-1-max-pending-2-timeout-20
- 执行时间（UTC）：2026-07-17T00:51:02.547825+00:00 → 2026-07-17T00:51:04.861690+00:00
- 独立端点：`http://127.0.0.1:18081/inference_zero_shot`
- 端点类型：controlled_mock
- 固定语料：证据充分才能形成可靠判断，公平程序同样重要。
- 生产 gate 活动检测：全程为 0
- 证据边界：可控 mock LightTTS endpoint + 真实 V2 provider/admission/Redis；这里只验证排队机制、取消、超时、隔离与原子 WAV，不代表真实模型吞吐、RTF、音质或 GPU 容量。

| 场景 | jobs | max pending | 接受/拒绝/超时 | 成功 | Queue P50/P95 | E2E P50/P95 | 最大队列 | 最长等待 | FIFO | WAV |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|
| capacity-4 | 4 | 2 | 2/2/0 | 2 | 0.334/0.566s | 0.659/0.819s | 1 | 0.451s | PASS | 2/2 |
| capacity-10 | 10 | 2 | 2/8/0 | 2 | 0.219/0.347s | 0.515/0.688s | 1 | 0.169s | PASS | 2/2 |
| capacity-20 | 20 | 2 | 2/18/0 | 2 | 0.273/0.398s | 0.571/0.742s | 1 | 0.162s | PASS | 2/2 |

## 资源采样

> 这是 mock 端点及宿主机采样，只用于确认测试未失控；不能解释为真实 LightTTS 模型 CPU/GPU/内存需求。

```json
{
  "cpu_percent": {
    "max": 0.1,
    "p50": 0.1,
    "p95": 0.1
  },
  "gpu_process_memory_mib": {
    "max": 0.0,
    "p50": 0.0,
    "p95": 0.0
  },
  "gpu_total_memory_mib": {
    "max": 4998.0,
    "p50": 4998.0,
    "p95": 4998.0
  },
  "gpu_utilization_percent": {
    "max": 0.0,
    "p50": 0.0,
    "p95": 0.0
  },
  "process_count": {
    "max": 1.0,
    "p50": 1.0,
    "p95": 1.0
  },
  "rss_mib": {
    "max": 18.1,
    "p50": 18.1,
    "p95": 18.1
  }
}
```

## 判定说明

- 容量场景的失败率、FIFO 越序、无效 WAV、残留 `.part` 任一非零即失败。
- queued cancellation 应快速移除等待任务；active cancellation 应继续占用 endpoint 处理槽直到 HTTP 返回，再丢弃产物。
- queue timeout 应只失败等待任务，不能打断持槽任务或留下 Redis ghost key。
- 单槽 FIFO 的高并发成功不等于课堂体验达标；尾任务 E2E 与最长等待仍是发布决策依据。
- 本报告不产生真实模型音质、RTF 或 GPU 容量结论。
