# MOSS-TTS-Nano 隔离 Canary 安全结论

日期：2026-07-17（Asia/Shanghai）  
状态：**未执行推理，因生产安全前置条件不成立而主动终止**

## 结论

本轮没有在生产服务器安装、下载或启动 MOSS-TTS-Nano。只读预检发现生产正在处理真实比赛：房间 `433825` 为 `running`，阶段索引 7，`active_match_processing=true`。这与“无活动比赛时才允许启动隔离 canary”的硬前提冲突。

生产复核显示：所有 Supervisor 服务仍为 `RUNNING`，API ready 正常，LightTTS health 正常，接纳门为 active 0 / queue 0；服务器上不存在 `/home/ubuntu/sunsq/moss-tts-nano-canary`，也没有创建环境、下载模型、启动临时端口、修改 V2/Agent/数据库/Nginx/Supervisor/`.env` 或重启任何服务。

即使排除当前生产占用，MOSS-TTS-Nano ONNX 也暂不适合作为“不中断、不吞词、无音色漂移”的主生产替代。官方仓库当前仍有吞字吞句、短句重复、语速不均和标点处丢句的开放问题；这些问题直接违反本项目的核心验收标准。

## 生产资源预检

| 项目 | 只读实测 |
|---|---:|
| CPU | 12 logical cores；load 0.68 / 0.82 / 0.71 |
| RAM | 31 GiB 总量；17 GiB 已用；13 GiB available；无 swap |
| 磁盘 | 38 GiB 可用 |
| GPU | RTX 3080 Ti 12 GiB；5128 MiB 已用；6786 MiB 空闲 |
| LightTTS | RUNNING；health OK；gate 0 active / 0 queued |
| 活动比赛 | 1 场，房间 433825，running |

无 swap 且生产已有 17 GiB 常驻内存。虽然 ONNX 权重本身约 727.85 MiB，官方当前 Python ONNX 入口仍直接导入 `torch` 与 `torchaudio`，完整运行时内存不能按权重体积简单估算。在真实比赛期间做首次下载、环境解析、模型加载和三并发压测没有安全余量。

## 官方实现核对

- 官方源码：[OpenMOSS/MOSS-TTS-Nano](https://github.com/OpenMOSS/MOSS-TTS-Nano)，核对提交 `11619374849c649486584e3b10ed55b176a924ee`，Apache-2.0。
- 官方推荐 ONNX Runtime CPU 路径，0.1B TTS 模型加 Nano codec 的模型资产合计 `763,206,064` bytes（727.85 MiB）。
- ONNX manifest 内置 6 个中文参考音色：Junhao、Zhiming、Weiguo、Xiaoyu、Yuewen、Lingyu。
- 其中 Zhiming 明确标记“京味胡同闲聊”，Yuewen 的官方参考文本为台湾口语，Lingyu 为“深夜电台”情感风格。按本项目“正常普通话、无方言、无奇怪感情”的要求，官方现成中文音色池不能直接视为 4 个合格固定音色，仍需独立听感筛选或自建普通话参考音频。
- vLLM-Omni 可为 MOSS-TTS-Nano 提供 OpenAI-compatible `/v1/audio/speech` 与 PCM streaming，但要求每次携带 `ref_audio`，没有内置 speaker preset；它改变服务与流式输出方式，不会自动消除上游模型的吞句、重复或语速问题。

## 与核心目标冲突的开放问题

- [#58：ONNX 吞字吞句严重](https://github.com/OpenMOSS/MOSS-TTS-Nano/issues/58)：报告 CPU/CUDA 均随机出现，且语速忽快忽慢。维护者说明部分长文本尾部丢失与 `Max New Frames` 上限有关，但 issue 仍为开放状态。
- [#60：ONNX 短文本容易生成重复语音](https://github.com/OpenMOSS/MOSS-TTS-Nano/issues/60)：例如“你好”可能生成十几秒；建议仅是增加标点、调参或重试。
- [#81：短句重复及语速不均](https://github.com/OpenMOSS/MOSS-TTS-Nano/issues/81)：例如“其实”重复到 17 秒。
- [#87：标点处丢句](https://github.com/OpenMOSS/MOSS-TTS-Nano/issues/87)：引号、破折号等位置尤其明显。

这些不是边缘体验问题，而是辩论系统的 P0：会导致论点被删改、时间轴与音频内容不一致，并破坏“不卡顿、不吞词”的承诺。因此本轮没有为了得到速度数字而冒险下载或启动。

## 后续隔离机验收门

如果后续在独立 CPU 主机或独立 GPU 主机继续评估，必须同时满足以下条件，不能只看首 PCM：

1. 四个固定普通话参考音色，逐音色至少 100 条；覆盖 2–6 字短句、标点、数字、英文缩写、30–90 秒辩论陈述。
2. 文本完整率 100%：ASR 回译与原文对齐，0 吞句、0 重复句、0 非预期插入。
3. 单路和 3 并发均测：首 PCM、首可播放音频、完成时间、RTF、最大块间隔、p50/p95/p99、成功率、CPU/RAM/GPU。
4. 首可播放音频小于 3 秒；稳态最大块间隔需低于播放器安全缓冲，且 3 并发下无 underrun。
5. 同一席位跨 100 轮说话人 embedding/音色相似度不得持续漂移；人工盲听不得出现方言或异常情绪。
6. 超时或质量门失败时必须 fail closed 到已验证 TTS，不能靠自动重试掩盖短句重复。

结构化预检证据见 [`preflight.json`](./preflight.json)。本目录没有 WAV 或性能结果，因为推理服务从未启动。
