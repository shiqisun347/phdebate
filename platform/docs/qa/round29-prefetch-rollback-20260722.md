# Round 29：预加载争用回归与安全回滚

## 发现

把下一位完整 TTS 在当前正式 MOSS 生成开始前排入队列，会让单路 MOSS endpoint 发生争用。真实生产测试房间 `461630` 出现 `push_ack_timeout`、orphan session，随后 MOSS readiness 暂时降为不可用。

## 处置

1. 立即停止测试房间并安全暂停比赛。
2. 回滚 API 到上一份稳定 release，重启 Engine 与 MOSS。
3. 等待 MOSS 完成模型加载、编译和 readiness 检查。
4. 部署修正版 `round29-safe-prefetch-20260722`。

## 当前规则

- 单路 MOSS 端点只服务当前正式发言。
- 当前完整 WAV 生成并释放端点后，才开始下一位 Agent 的完整语音预取；下一语音在当前 WAV 播放期间生成。
- 同房间正式阶段开始时，若预取仍在运行则等待它，不取消并重算。
- 任何暂停、跳过、接管或历史变化都会使预取失效并清理文件。

## 验证

- API 全量：498 passed，1 xfailed。
- 预取/稳定播放定向：6 passed。
- 生产 readiness：数据库、Redis、Engine、Worker、MOSS、FunASR 全部 healthy。

> 这次回归说明“预加载”不能只追求更早提交；必须服从真实 TTS 端点容量。若要同时满足 Agent 首字后 3 秒出声和不争用本机 MOSS，下一阶段应接入独立的云端真双向 TTS，而不是提高本机并发。
