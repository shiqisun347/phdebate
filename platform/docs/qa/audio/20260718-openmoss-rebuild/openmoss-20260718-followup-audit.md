# OpenMOSS 2026-07-18 follow-up audit

## 结论

截至 2026-07-18，OpenMOSS/MOSS-TTS `main` 仍为固定提交
`ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af`，没有比当前网关固定版本更新的
Realtime 源码可直接吸收。生产 RTX 3080 Ti 12GB 的独占 canary 已证明：即使先停掉
LightTTS、启动时空闲 11909MiB，官方 Realtime talker 与完整 MOSS Audio Tokenizer
仍会在 codec 上卡阶段 OOM。因此 12GB 原版 PyTorch 路径维持 NO-GO。

上游当前最有价值的新信息来自 vLLM-Omni：其官方 recipe 已支持
`MOSS-TTS-Realtime` 的在线流式服务，但明确按 A10G 24GB 给出配置，并记录 talker
约 6GB、codec decoder 约 8GB 的峰值显存。这与本次 12GB OOM 一致，不能据此把
生产卡的门槛下调。vLLM-Omni 可以作为独立 24GB+ GPU 的第二实现候选，但它的
OpenAI `/v1/audio/speech` 流式 API 不能直接替代现有“同一 turn 持续接收正文 delta”
的持久双向 WS 合约；接入前必须验证是否能保持一个模型 turn、连续追加正文、完整
abort/release 以及未播放文本剔除语义。

## 已核对的上游事实

1. [OpenMOSS/MOSS-TTS main](https://github.com/OpenMOSS/MOSS-TTS/commit/ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af)
   仍固定在 `ad99ec5`，提交日期为 2026-06-22。
2. [并发 Issue #72](https://github.com/OpenMOSS/MOSS-TTS/issues/72) 中，上游只说明
   将开放 thread-safe sglang 版本；当前公开仓库没有可证明 Realtime 单实例安全并发的
   实现。现有 Gradio/原生推理仍应按单 active endpoint 处理。
3. [尾字截断 Issue #19](https://github.com/OpenMOSS/MOSS-TTS/issues/19) 明确存在概率性
   尾音/尾字问题。上游建议参考音频短于目标文本并补终止标点，但同时承认模型仍有小
   bug；这些建议不能替代末字、吞字率、CER 和人工试听门禁。
4. [vLLM-Omni MOSS-TTS recipe](https://github.com/vllm-project/vllm-omni/blob/main/recipes/OpenMOSS/MOSS-TTS.md)
   把 Realtime 标为 1.7B、TTFB 约 180ms，并以 A10G 24GB 为硬件示例；记录 talker
   约 6GB、codec decoder 约 8GB。
5. [vLLM-Omni Realtime deploy config](https://github.com/vllm-project/vllm-omni/blob/main/vllm_omni/deploy/moss_tts_realtime.yaml)
   使用两阶段同卡配置：talker `max_num_seqs: 8`，codec `max_num_seqs: 1`，codec
   chunk 为 15 frames。它能提供服务端调度，但 codec 仍是单序列瓶颈；2–3 场并发的
   首声、排队和音质必须用真实 GPU 重新测量。
6. [OpenMOSS/sglang](https://github.com/OpenMOSS/sglang) 当前公开支持集中在
   MossTTSDelay/codec 路径；没有找到可直接替换当前 Realtime 原生 streaming session
   的公开实现。

## 对当前方案的影响

- 保留当前持久 WS、严格 seq/ACK、单 recv/demux、全链路 abort、idle 预连接和
  endpoint 隔离；这些协议能力不依赖具体推理 runtime。
- 正式默认仍要求独立 24GB+ GPU。`MOSS_GATEWAY_ALLOW_SUB24GB_DIAGNOSTIC` 只能用于
  受控诊断，不能进入生产 admission。
- 12GB 上只有在“上游明确支持的低精度 codec、跨设备 codec、或完整量化 runtime”
  通过内容完整性、音色、首声、卡顿和恢复门后，才有资格重新评估；仅仅成功加载不算
  通过。
- 独立 24GB+ GPU 上应同时比较两条实现：
  1. 当前固定上游原生 session + 本项目持久 WS gateway；
  2. vLLM-Omni Realtime serving + 适配层。
  两者使用同一套 8 音色、20 轮、1/2/3 房、TTS→ASR、浏览器首声和打断门禁。

## 发布门

本次上游复核没有解除任何生产发布阻塞。仍需：

- 真 GPU 20/20 在 Agent final 前产生非静音 PCM；
- 首 PCM P95 ≤ 800ms，LLM 首正文到浏览器出声 P95 ≤ 2.5s；
- 8 音色 CER、首尾字、吞字、重复、块间静音、音量、漂移与人工 MOS 全通过；
- 2–3 场并发无串房、换声、失真、长队列或不可释放 session；
- interrupt 后 250ms 内静音，旧 generation 不复活。

结论：`UPSTREAM REVIEW COMPLETE / PRODUCTION REALTIME TTS STILL NO-GO`。
