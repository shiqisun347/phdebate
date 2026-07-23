# LightTTS 4/10/20 房隔离容量与排队体验

- 证据分栏：active-plus-pending-mechanism
- 执行时间（UTC）：2026-07-17T02:52:56.931513+00:00 → 2026-07-17T02:52:59.348326+00:00
- 独立端点：`http://127.0.0.1:18082/inference`
- 端点类型：controlled_mock
- 固定语料：证据充分才能形成可靠判断，公平程序同样重要。
- 生产 gate 活动检测：全程为 0
- 证据边界：可控 mock LightTTS endpoint + 真实 V2 provider/admission/Redis；这里只验证排队机制、取消、超时、隔离与原子 WAV，不代表真实模型吞吐、RTF、音质或 GPU 容量。

| 场景 | jobs | max pending | 接受/拒绝/超时 | 成功 | Queue P50/P95 | E2E P50/P95 | 最大队列 | 最长等待 | FIFO | WAV |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|
| capacity-4 | 4 | 2 | 3/1/0 | 3 | 0.343/0.573s | 0.589/0.818s | 2 | 0.503s | PASS | 3/3 |
| capacity-10 | 10 | 2 | 3/7/0 | 3 | 0.297/0.525s | 0.541/0.77s | 2 | 0.502s | PASS | 3/3 |
| capacity-20 | 20 | 2 | 3/17/0 | 3 | 0.271/0.5s | 0.512/0.743s | 2 | 0.501s | PASS | 3/3 |

## 资源采样

> 这是 mock 端点及宿主机采样，只用于确认测试未失控；不能解释为真实 LightTTS 模型 CPU/GPU/内存需求。

```json
{
  "cpu_percent": {
    "max": null,
    "p50": null,
    "p95": null
  },
  "gpu_process_memory_mib": {
    "max": null,
    "p50": null,
    "p95": null
  },
  "gpu_total_memory_mib": {
    "max": null,
    "p50": null,
    "p95": null
  },
  "gpu_utilization_percent": {
    "max": null,
    "p50": null,
    "p95": null
  },
  "process_count": {
    "max": null,
    "p50": null,
    "p95": null
  },
  "rss_mib": {
    "max": null,
    "p50": null,
    "p95": null
  }
}
```

## 判定说明

- 容量场景的失败率、FIFO 越序、无效 WAV、残留 `.part` 任一非零即失败。
- queued cancellation 应快速移除等待任务；active cancellation 应继续占用 endpoint 处理槽直到 HTTP 返回，再丢弃产物。
- queue timeout 应只失败等待任务，不能打断持槽任务或留下 Redis ghost key。
- 单槽 FIFO 的高并发成功不等于课堂体验达标；尾任务 E2E 与最长等待仍是发布决策依据。
- 本报告不产生真实模型音质、RTF 或 GPU 容量结论。
