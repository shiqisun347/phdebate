# MOSS-TTS-Nano CPU canary 取消与安全收尾

日期：2026-07-18（Asia/Shanghai）  
状态：**CANCELLED_SUPERSEDED**

## 结论

用户已将方向改为 GPU 部署与推理加速，因此 CPU-only canary 已停止，不再评估。本轮未执行任何 TTS 推理、并发压测、ASR 回转或模型质量判断；此状态不是模型质量的 GO/NO-GO 结论。

## 执行边界

- 启动前安全预检通过：`active_match_processing=false`，数据库活动房列表为空。
- 在收到取消指令前，已在独立目录 `/home/ubuntu/sunsq/moss-nano-cpu-canary` 固定检出上游提交 `11619374849c649486584e3b10ed55b176a924ee`，创建隔离 CPU-only 环境并下载约 729 MiB ONNX 模型资产；尚未开始模型加载后的推理或监听端口。
- 安装与下载阶段由 2 秒一次的 fail-closed watchdog 监控；任务限制在 CPU 8–11、低调度优先级，使用 CPU-only PyTorch 与 ONNX Runtime CPU provider，`CUDA_VISIBLE_DEVICES` 为空。
- 未修改或重启 V2、Agent、LightTTS、FunASR、Nginx、PostgreSQL、Redis 或 Supervisor 配置；未写业务数据库，未占用生产 GPU。

## 收尾复核

- 已终止 canary watchdog，并删除生产机上的整个隔离目录。
- 隔离目录不存在；相关进程数为 0；候选端口 `18083`–`18085` 监听数为 0。
- 数据库活动房仍为空，`active_match_processing=false`。
- 全部 Supervisor 服务仍为 `RUNNING`；核心服务 PID 与预检一致，未发生重启。
- GPU 收尾值为 5000 MiB used / 6914 MiB free / 0% utilization，与预检一致。
- LightTTS admission gate 保持 active 0 / queue 0，生产 readiness 为 `ok=true`。

后续如继续 MOSS-TTS-Nano，应另立 GPU 加速 canary，重新定义显存隔离、推理后端、单路与三路并发及内容完整性验收门。
