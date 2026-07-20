# vLLM-Omni WebSocket `SpeechSynthesisSession` 适配审计

审计时间：2026-07-17（Asia/Shanghai）  
范围：只读源码审计与测试设计；未修改 V2 业务代码、未部署、未进行 GPU 实测。

## 1. 结论

**固定版 vLLM-Omni 的 `/v1/audio/speech/stream` 可以实现一个协议正确、可取消、可落盘的 `SpeechSynthesisSession` 适配器，但不能实现当前 V2 所要求的“Agent delta 到达后、`finish()` 前即开始出声”。因此：**

- 作为“整段文本结束后再流式返回 PCM”的兼容适配：**可做**。
- 作为 V2 核心的实时 `push_text()` 会话：**NO-GO**。
- 简单修改客户端状态机无法补齐这一差距；固定版服务端把所有 `input.text` 仅追加到列表，直到收到 `input.done` 才创建唯一一次推理请求。
- OpenMOSS 原生 `MossTTSRealtimeStreamingSession` 确实具备 V2 想要的语义：`push_text()` 达到预填充阈值后立即生成音频帧，随后 `end_text()`、`drain()` 收尾。vLLM-Omni 当前 WebSocket 并没有暴露这套原生会话状态。
- 更严重的是，固定版 vLLM-Omni 的在线 MOSS-TTS-Realtime builder 自己注明当前仍走兼容性的 `prompt_audio_array` 路径，**“full Realtime support needs a separate processor ... which we don't wire here”**；而同仓库离线实现已经使用正确的 Realtime processor/prompt grid。不能把“模型被列为 supported”直接等同于“在线实时会话已完整接通”。

建议决策：

1. 不要把此 WebSocket 适配器作为 V2 默认实时语音后端。
2. 若需要快速做集成 canary，可实现本文的“严格兼容适配”，但必须明确标记 `audio_before_finish=false`，只验证音色、PCM、取消和单请求稳定性。
3. 核心路线应是：给 OpenMOSS 原生 session 做一个有 admission gate 的常驻服务，或在 vLLM-Omni 中真正接入原生 Realtime processor、增量 token feed、KV/session 生命周期；仅给现有 handler 增加 `input.commit` 不够，因为现有 engine request 在 `input.done` 后才创建。

## 2. 固定源码与证据等级

### 2.1 vLLM-Omni

- 仓库：<https://github.com/vllm-project/vllm-omni>
- 固定 commit：[`7aa5c9a0901b7b9254052c4d342d0c3fa447eb95`](https://github.com/vllm-project/vllm-omni/commit/7aa5c9a0901b7b9254052c4d342d0c3fa447eb95)
- 协议 handler：[`serving_speech_stream.py`](https://github.com/vllm-project/vllm-omni/blob/7aa5c9a0901b7b9254052c4d342d0c3fa447eb95/vllm_omni/entrypoints/openai/serving_speech_stream.py)
- MOSS 在线 serving：[`serving_speech.py`](https://github.com/vllm-project/vllm-omni/blob/7aa5c9a0901b7b9254052c4d342d0c3fa447eb95/vllm_omni/entrypoints/openai/serving_speech.py)
- MOSS deploy：[`moss_tts_realtime.yaml`](https://github.com/vllm-project/vllm-omni/blob/7aa5c9a0901b7b9254052c4d342d0c3fa447eb95/vllm_omni/deploy/moss_tts_realtime.yaml)

### 2.2 OpenMOSS

- 仓库：<https://github.com/OpenMOSS/MOSS-TTS>
- 固定 commit：[`ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af`](https://github.com/OpenMOSS/MOSS-TTS/commit/ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af)
- 原生 streaming session：[`streaming_mossttsrealtime.py`](https://github.com/OpenMOSS/MOSS-TTS/blob/ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af/moss_tts_realtime/mossttsrealtime/streaming_mossttsrealtime.py)
- 官方 session HTTP 示例：[`fast_api.py`](https://github.com/OpenMOSS/MOSS-TTS/blob/ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af/moss_tts_realtime/fast_api.py)

证据优先级：固定 commit 源码 > 同 commit 测试 > 同 commit 文档。路由 docstring 说“按句切分”，但 handler 和测试都明确是“全部缓冲后一次请求”，本文以源码和测试为准。

## 3. 核心语义差异

| 能力 | V2 `SpeechSynthesisSession` 预期 | vLLM-Omni WS 固定版 | OpenMOSS 原生 session |
|---|---|---|---|
| `push_text(delta)` | final 前持续喂入，尽早出音频 | 只 `text_parts.append(text)`，不推理 | 达到 prefill 阈值后生成音频帧 |
| `finish()` | 结束文本并排空剩余音频 | 发送 `input.done` 后才首次创建推理 | `end_text()` 后 `drain()` |
| 连续模型状态 | 同一 speech 内保留上下文 | 整段只有一次 request；没有增量 feed | 同 turn 保留 KV/生成状态 |
| 多轮上下文 | 可选 | WS 每连接只处理一个 finalized utterance | `reset_turn(... reset_cache=False)` 可复用 KV |
| 取消 | `abort()` | 关闭 WS，服务端 best-effort `engine.abort` | 原生示例没有标准 cancel ack，需要服务包装 |

直接证据：

- handler 文件开头明确写着“buffers it until `input.done`, and generates audio once”：[`serving_speech_stream.py#L1-L4`](https://github.com/vllm-project/vllm-omni/blob/7aa5c9a0901b7b9254052c4d342d0c3fa447eb95/vllm_omni/entrypoints/openai/serving_speech_stream.py#L1-L4)。
- `input.text` 只追加，`input.done` 才 join 并调用一次 `_generate_and_send(..., sentence_index=0)`：[`serving_speech_stream.py#L100-L149`](https://github.com/vllm-project/vllm-omni/blob/7aa5c9a0901b7b9254052c4d342d0c3fa447eb95/vllm_omni/entrypoints/openai/serving_speech_stream.py#L100-L149)。
- 官方测试专门断言多句被合成一个请求：[`test_serving_speech_stream.py#L348-L368`](https://github.com/vllm-project/vllm-omni/blob/7aa5c9a0901b7b9254052c4d342d0c3fa447eb95/tests/entrypoints/openai_api/test_serving_speech_stream.py#L348-L368)。
- 路由 docstring 的“splits at sentence boundaries”与实现矛盾：[`api_server.py#L1557-L1563`](https://github.com/vllm-project/vllm-omni/blob/7aa5c9a0901b7b9254052c4d342d0c3fa447eb95/vllm_omni/entrypoints/openai/api_server.py#L1557-L1563)。
- OpenMOSS 原生 `push_text()` 会分段、tokenize 并 drain pending tokens：[`streaming_mossttsrealtime.py#L618-L729`](https://github.com/OpenMOSS/MOSS-TTS/blob/ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af/moss_tts_realtime/mossttsrealtime/streaming_mossttsrealtime.py#L618-L729)。
- 官方示例的真实顺序是 `push_text → end_text → drain → decoder.flush`：[`example_llm_stream_to_tts.py#L138-L184`](https://github.com/OpenMOSS/MOSS-TTS/blob/ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af/moss_tts_realtime/example_llm_stream_to_tts.py#L138-L184)。

## 4. vLLM-Omni WebSocket 精确协议

端点：`ws(s)://<host>/v1/audio/speech/stream`

### 4.1 客户端消息

第一条消息必须是：

```json
{
  "type": "session.config",
  "model": "OpenMOSS-Team/MOSS-TTS-Realtime",
  "response_format": "pcm",
  "stream_audio": true,
  "word_timestamps": false,
  "speed": 1.0,
  "ref_audio": "data:audio/wav;base64,..."
}
```

随后可发送零到多条：

```json
{"type":"input.text","text":"第一段"}
{"type":"input.text","text":"，第二段。"}
```

最后恰好一次：

```json
{"type":"input.done"}
```

限制来自 [`serving_speech_stream.py#L49-L54`](https://github.com/vllm-project/vllm-omni/blob/7aa5c9a0901b7b9254052c4d342d0c3fa447eb95/vllm_omni/entrypoints/openai/serving_speech_stream.py#L49-L54)：

- 等待 config：10 秒。
- input idle：30 秒。
- config 文本上限：4 MiB。
- 单条 `input.text` 上限：128 KiB。
- 没有 `session.configured`/ack；客户端发送 config 后只能通过“未收到 error/未断开”推断继续。

配置字段与校验见 [`audio.py#L499-L560`](https://github.com/vllm-project/vllm-omni/blob/7aa5c9a0901b7b9254052c4d342d0c3fa447eb95/vllm_omni/entrypoints/openai/protocol/audio.py#L499-L560)。对本项目的 MOSS Realtime 适配应固定：

- `stream_audio=true`
- `response_format="pcm"`
- `speed=1.0`
- `word_timestamps=false`
- `ref_audio` 必填
- `speaker_embedding` 不用
- `task_type`、`instructions`、`non_streaming_mode` 不传
- `language`、`ref_text` 在固定版 MOSS Realtime builder 中没有被使用，不应误以为它们会控制合成
- `initial_codec_chunk_frames` 虽然是通用协议字段，但固定版 MOSS Realtime builder 没有把它写入该模型的 `additional_information`；首块大小应通过 deploy YAML 验证和调整，不能依赖此 session 字段

### 4.2 服务端成功序列

```text
JSON   audio.start
BINARY pcm chunk 0
BINARY pcm chunk 1
...
JSON   audio.done(error=false,total_bytes=N)
JSON   session.done(total_sentences=1)
```

生成事件源码：[`serving_speech_stream.py#L234-L315`](https://github.com/vllm-project/vllm-omni/blob/7aa5c9a0901b7b9254052c4d342d0c3fa447eb95/vllm_omni/entrypoints/openai/serving_speech_stream.py#L234-L315)。官方测试也要求 binary 总长度严格等于 `audio.done.total_bytes`：[`test_qwen3_tts_websocket.py#L115-L133`](https://github.com/vllm-project/vllm-omni/blob/7aa5c9a0901b7b9254052c4d342d0c3fa447eb95/tests/entrypoints/openai_api/test_qwen3_tts_websocket.py#L115-L133)。

空白输入只有：

```json
{"type":"session.done","total_sentences":0}
```

见 [`test_serving_speech_stream.py#L335-L346`](https://github.com/vllm-project/vllm-omni/blob/7aa5c9a0901b7b9254052c4d342d0c3fa447eb95/tests/entrypoints/openai_api/test_serving_speech_stream.py#L335-L346)。V2 已经拒绝空 final text，因此适配器遇到 `total_sentences=0` 应按失败处理。

### 4.3 错误序列

错误只有：

```json
{"type":"error","message":"..."}
```

没有稳定的 machine-readable code，见 [`serving_speech_stream.py#L425-L436`](https://github.com/vllm-project/vllm-omni/blob/7aa5c9a0901b7b9254052c4d342d0c3fa447eb95/vllm_omni/entrypoints/openai/serving_speech_stream.py#L425-L436)。

生成中失败的实际序列可能是：

```text
audio.start
0..N 个 binary chunk
error
audio.done(error=true,total_bytes=已发送字节数)
session.done(total_sentences=1)
```

官方测试明确覆盖此顺序：[`test_serving_speech_stream.py#L424-L465`](https://github.com/vllm-project/vllm-omni/blob/7aa5c9a0901b7b9254052c4d342d0c3fa447eb95/tests/entrypoints/openai_api/test_serving_speech_stream.py#L424-L465)。因此：

- 收到任意 `error` 必须设置不可逆 failure latch。
- `audio.done.error=true` 必须失败。
- 后续即使收到 `session.done` 也不能把任务翻回成功。
- `audio.start` 不是“模型验证已通过”的确认：server 在 `_prepare_speech_generation()` 前先发 start。缺失/非法 `ref_audio` 也可能先 start 再 error。

未知输入消息、非法 JSON、错误类型和超大 `input.text` 通常只发 error 并继续会话；客户端实现不应继续容错，而应 fail closed、关闭连接，避免文本与音频状态失配。服务端“未知类型后仍可继续”的测试见 [`test_serving_speech_stream.py#L370-L392`](https://github.com/vllm-project/vllm-omni/blob/7aa5c9a0901b7b9254052c4d342d0c3fa447eb95/tests/entrypoints/openai_api/test_serving_speech_stream.py#L370-L392)。

## 5. 适配器状态机

建议内部状态：

```text
NEW
  -> CONNECTING
  -> INPUT_OPEN          (WS 已连，config 已发送；没有服务端 ack)
  -> INPUT_FINISHED      (input.done 已发送)
  -> AUDIO_STREAMING     (收到唯一 audio.start)
  -> AUDIO_DONE          (收到 audio.done，且 error=false)
  -> COMPLETED           (收到 session.done=1，校验全部通过并原子发布 WAV)

任意非终态 -> ABORTING -> ABORTED
任意协议/网络/服务错误 -> FAILED
```

### 5.1 每个方法的精确语义

`push_text(text)`：

1. 只允许 `INPUT_OPEN`。
2. 空字符串直接 no-op；非字符串由 Python 类型约束拒绝。
3. UTF-8/JSON 编码后的单条消息须有本地上限，建议远低于服务端 128 KiB。
4. 使用单一 send lock 发送 `input.text`。
5. 返回只代表“消息已写入 socket”，**不代表已合成或已产生音频**。
6. receiver task 从连接建立后就常驻，以便 config/input 阶段及时捕获 `error` 或连接关闭。

`finish()`：

1. 仅第一次调用从 `INPUT_OPEN` 转 `INPUT_FINISHED` 并发送一次 `input.done`；重复调用应复用同一个 completion future，不能重复发 done。
2. 等待 receiver 走完 `audio.start → binary* → audio.done → session.done`。
3. 校验成功后修正 WAV header、`fsync`/关闭文件、原子 rename `.part` 为最终 `.wav`，再返回 `/media/...` URL。
4. 任何失败都删除 `.part`，不得发布最终 URL。

`abort()`：

1. 幂等；本地状态立即变成 `ABORTED`。
2. 取消 receiver/finish wait，关闭 WS，不发送不存在的 `input.cancel`/`session.cancel`。
3. 删除 partial 文件并发 V2 `audio.stream.aborted`。
4. 不等待服务端 cancel ack，因为协议没有 ack。

### 5.2 接收端严格校验

- `audio.start`：必须恰好一次；`sentence_index==0`、`format=="pcm"`、`sample_rate==24000`、`sentence_text` 等于所有 push 文本拼接后 `strip()` 的结果。
- binary：只能在 `audio.start` 后、`audio.done` 前接收；每帧长度必须为正且为 2 的倍数。
- `audio.done`：必须恰好一次；index 为 0；`error is false`；`total_bytes` 等于累计 binary 字节；非空文本时必须大于 0。
- `session.done`：只能在成功的 `audio.done` 后；必须恰好一次且 `total_sentences==1`。
- 未知 JSON event、重复 start/done、binary 越界、提前 close、超时，全部失败。
- 设置单任务最大 PCM 字节/时长，防止异常服务无限写盘。

## 6. 音频格式与 V2 发布

固定协议常量是 24 kHz、16-bit、mono：[`serving_speech_stream.py#L49-L54`](https://github.com/vllm-project/vllm-omni/blob/7aa5c9a0901b7b9254052c4d342d0c3fa447eb95/vllm_omni/entrypoints/openai/serving_speech_stream.py#L49-L54)。MOSS pipeline 同样声明输出 24 kHz mono waveform：[`pipeline.py#L15-L21`](https://github.com/vllm-project/vllm-omni/blob/7aa5c9a0901b7b9254052c4d342d0c3fa447eb95/vllm_omni/model_executor/models/moss_tts/pipeline.py#L15-L21)。

`word_timestamps=false` 时 binary frame 是无 header 的 PCM S16LE。适配器应：

1. 创建 `.<speech_id>.<generation>.wav.part`。
2. 先写 44-byte WAV header 占位：1 channel、24000 Hz、16 bit。
3. 每个 binary frame 同时：
   - 追加到 WAV part；
   - 通过现有 `on_stream_event`/房间音频 WS 发布同一份 PCM，不能二次编码；
   - 更新 byte count、chunk seq、PTS。
4. 成功时把 RIFF/data 长度回填为 `total_bytes`，最终文件大小应为 `44 + total_bytes`。
5. 原子发布最终 WAV。

不要启用 `word_timestamps`：该模式会把音频改成 JSON `audio.chunk` + base64，并需要 forced aligner；源码见 [`serving_speech_stream.py#L316-L397`](https://github.com/vllm-project/vllm-omni/blob/7aa5c9a0901b7b9254052c4d342d0c3fa447eb95/vllm_omni/entrypoints/openai/serving_speech_stream.py#L316-L397)。它增加 base64 和 alignment 开销，不符合当前核心路径。

注意：普通 binary 模式没有每块 authoritative sample rate，只有 `audio.start.sample_rate` 的 nominal 24000。因此部署必须固定 MOSS Realtime 24 kHz，适配器应拒绝其他值。

## 7. Voice reference 语义

### 7.1 固定版 MOSS Realtime 的真实要求

- `MOSS-TTS-Realtime` 必须有 `ref_audio`：[`serving_speech.py#L1779-L1809`](https://github.com/vllm-project/vllm-omni/blob/7aa5c9a0901b7b9254052c4d342d0c3fa447eb95/vllm_omni/entrypoints/openai/serving_speech.py#L1779-L1809)。
- 接受 `http://`、`https://`、`data:`、`file://`；裸路径无效：[`serving_speech.py#L1590-L1600`](https://github.com/vllm-project/vllm-omni/blob/7aa5c9a0901b7b9254052c4d342d0c3fa447eb95/vllm_omni/entrypoints/openai/serving_speech.py#L1590-L1600)。
- reference 时长必须 1–30 秒；服务端会转单声道并缓存 resolve 结果：[`serving_speech.py#L2442-L2502`](https://github.com/vllm-project/vllm-omni/blob/7aa5c9a0901b7b9254052c4d342d0c3fa447eb95/vllm_omni/entrypoints/openai/serving_speech.py#L2442-L2502)。
- Realtime branch 只把 `text/mode/prompt_audio_array` 交给模型，`ref_text` 与 `language` 未进入该 branch：[`serving_speech.py#L1891-L1908`](https://github.com/vllm-project/vllm-omni/blob/7aa5c9a0901b7b9254052c4d342d0c3fa447eb95/vllm_omni/entrypoints/openai/serving_speech.py#L1891-L1908)。

### 7.2 V2 的 8 个辩位

建议保持固定映射：

```text
voice_id -> 不可变 reference 资产版本 -> sha256 -> 预期采样率/时长
```

首选部署方式是服务启动时预注册/预热 8 个 named voice，V2 每场只发送稳定的 `voice_id`；不需要增加任何前台“政策管理”或教学模块。若该部署没有完成 named voice 支持，则使用预生成、进程内缓存的 `data:audio/wav;base64,...`：

- 不依赖 vLLM 容器能看到 V2 本地路径。
- 不引入每次请求的外部 URL DNS/下载延迟。
- 必须保证整条 config 小于 4 MiB。

`file://` 只适合两个服务共享同一只读挂载且 vLLM 启动时配置了 `--allowed-local-media-path` 的场景。公开 HTTP URL 不应作为正式赛热路径。

固定版 uploaded voice 会把 name 解析成存储的 audio data，再写回 `request.ref_audio`：[`serving_speech.py#L1135-L1188`](https://github.com/vllm-project/vllm-omni/blob/7aa5c9a0901b7b9254052c4d342d0c3fa447eb95/vllm_omni/entrypoints/openai/serving_speech.py#L1135-L1188)。

### 7.3 在线 Realtime 支持风险

固定版 serving branch 注释明确说明：Realtime 的 `AutoProcessor` 未自动发现，当前只保留旧 `prompt_audio_array` 路径，“full Realtime support” 尚未接入：[`serving_speech.py#L1891-L1908`](https://github.com/vllm-project/vllm-omni/blob/7aa5c9a0901b7b9254052c4d342d0c3fa447eb95/vllm_omni/entrypoints/openai/serving_speech.py#L1891-L1908)。

同 commit 的离线实现已经直接加载 `processing_mossttsrealtime.py`，构造 17 列 Realtime prompt grid：[`end2end.py#L107-L171`](https://github.com/vllm-project/vllm-omni/blob/7aa5c9a0901b7b9254052c4d342d0c3fa447eb95/examples/offline_inference/text_to_speech/moss_tts/end2end.py#L107-L171)。这证明在线与离线 prompt 路径并不等价。

此外，唯一覆盖 MOSS Realtime 在线 serving 的 E2E 文件在固定版仍整体 skip：[`test_moss_tts_expansion.py#L33-L40`](https://github.com/vllm-project/vllm-omni/blob/7aa5c9a0901b7b9254052c4d342d0c3fa447eb95/tests/e2e/online_serving/test_moss_tts_expansion.py#L33-L40)。所以在真实 GPU canary 通过前，不得把 recipe 中的延迟数字当成本项目验收结果。

## 8. 取消语义

协议没有客户端 cancel message。唯一可用动作是关闭 WebSocket。

- `input.done` 前关闭：尚未创建 engine request，server 外层捕获 disconnect 并结束。
- 推理中关闭：发送 binary/JSON 时触发 `WebSocketDisconnect` 后，如果已有 request id，server 调用 `engine_client.abort(request_id)`：[`serving_speech_stream.py#L292-L298`](https://github.com/vllm-project/vllm-omni/blob/7aa5c9a0901b7b9254052c4d342d0c3fa447eb95/vllm_omni/entrypoints/openai/serving_speech_stream.py#L292-L298)。官方单测验证 abort：[`test_serving_speech_stream.py#L517-L555`](https://github.com/vllm-project/vllm-omni/blob/7aa5c9a0901b7b9254052c4d342d0c3fa447eb95/tests/entrypoints/openai_api/test_serving_speech_stream.py#L517-L555)。

这是 best-effort，不是有确认的强取消。V2 必须以本地 generation 为权威：一旦暂停、结束比赛、退出、换 speech 或 ASR 修订触发 abort，旧 generation 的任何迟到 chunk 都丢弃，partial 文件删除，最终 URL 不发布。

建议超时：

- connect 10 秒以内；
- 从 `input.done` 到 `audio.start` 单独 TTFA deadline；
- 两个音频 chunk 之间 idle deadline；
- 全任务 deadline；
- abort 后关闭 socket 的短 grace deadline，超时直接取消本地任务。

### 8.1 OpenMOSS 官方 `fast_api.py` 的取消/清理缺口

原生 streaming session 类没有 `abort()`/`close()`；官方 HTTP 包装的 `/tts/session/close` 只是把 `shutdown` 放进该 session 的 command queue，立刻从 manager 删除 session 并返回成功：[`fast_api.py#L981-L990`](https://github.com/OpenMOSS/MOSS-TTS/blob/ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af/moss_tts_realtime/fast_api.py#L981-L990)。

这不是运行中生成的强取消：

- worker 单线程同步处理 `start_turn`、`push_text`、`finish_turn`，只有当前 handler 返回后才会读取下一条 `shutdown`：[`fast_api.py#L552-L583`](https://github.com/OpenMOSS/MOSS-TTS/blob/ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af/moss_tts_realtime/fast_api.py#L552-L583)。
- `finish_turn` 会同步执行完整 `end_text()`、循环 `drain(max_steps=1)`、decoder flush，期间没有 cancel flag 检查：[`fast_api.py#L813-L847`](https://github.com/OpenMOSS/MOSS-TTS/blob/ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af/moss_tts_realtime/fast_api.py#L813-L847)。
- close 不 join worker、不等待 codec context 退出、没有 cancel ack，也没有把 worker exception 通过结构化状态返回给 client。
- `/push` 只表示命令已入队；如果 worker 稍后报错，HTTP 调用仍已返回 `ok=true`，音频流只能依赖 sentinel 结束，无法区分正常完成与失败。

若采用原生服务路线，必须在包装层增加每 turn cancel event，并在 push/drain/emit 循环检查；close 要等待有界清理、确保 `codec.streaming` context 退出、audio queue 发带原因的 terminal event、worker 生命周期可观测。不能原样把官方 demo server 当生产并发服务。

## 9. 并发语义

源码称每个 WebSocket 是独立 session：[`serving_speech_stream.py#L57-L69`](https://github.com/vllm-project/vllm-omni/blob/7aa5c9a0901b7b9254052c4d342d0c3fa447eb95/vllm_omni/entrypoints/openai/serving_speech_stream.py#L57-L69)。这只表示协议对象独立，不表示 GPU 资源或延迟隔离。

官方 Realtime deploy：

- Stage 0 `max_num_seqs: 8`
- Stage 1 codec `max_num_seqs: 1`
- 两 stage 都在 device 0
- codec streaming 开启，`codec_chunk_frames: 15`

见 [`moss_tts_realtime.yaml#L10-L58`](https://github.com/vllm-project/vllm-omni/blob/7aa5c9a0901b7b9254052c4d342d0c3fa447eb95/vllm_omni/deploy/moss_tts_realtime.yaml#L10-L58)。因此 2–3 个连接可以同时存在，但 codec stage 明确是单序列，不能从配置推导出 3 场同时平滑播放。

OpenMOSS 官方 model card/示例也注明当前只支持 batch size 1：[`moss_tts_realtime_model_card.md#L169-L180`](https://github.com/OpenMOSS/MOSS-TTS/blob/ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af/docs/moss_tts_realtime_model_card.md#L169-L180)。官方 FastAPI 虽然给每个 session 建 worker thread，但所有 session 共享同一 backend model/codec，且每个 turn 都进入 `codec.streaming(batch_size=1)`；代码没有全局 GPU semaphore：[`fast_api.py#L482-L550`](https://github.com/OpenMOSS/MOSS-TTS/blob/ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af/moss_tts_realtime/fast_api.py#L482-L550)、[`fast_api.py#L667-L721`](https://github.com/OpenMOSS/MOSS-TTS/blob/ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af/moss_tts_realtime/fast_api.py#L667-L721)。线程数不等于已验证并发能力。

V2 当前配置允许 `moss_tts_realtime_max_active` 到 3，但上线默认应从 1 开始，只有以下 2/3 并发 canary 全部通过才提高：

- 每场 TTFA、chunk gap、RTF、GPU memory、错误率均达标；
- 三条音频无串音、无 voice reference 泄漏、无 chunk 交叉；
- 任意一路 abort 不影响另外两路；
- 长文本与短文本混跑不饿死；
- codec stage 无全局状态污染或 CUDA 并发异常。

若单实例达不到 3 active，应使用 2–3 个独立 worker/GPU 实例做固定容量池，不应仅把 application semaphore 改成 3。

## 10. Fake WebSocket 单测矩阵

建议在 `apps/api/tests/` 新增纯 async fake WS 单测；fake connection 提供 `send`、`recv`、`close`、可编程 incoming queue、send barrier、disconnect injection。以下矩阵不需要 GPU。

| ID | 场景 / fake server 序列 | 断言 |
|---|---|---|
| F01 | 正常 start + 3 binary + done + session.done | 生成 WAV；PCM 相同；URL 正确 |
| F02 | 两次 `push_text` 后检查 client sends | config 第一条；两个 input.text 保序；尚无 input.done |
| F03 | `finish()` | `input.done` 恰好一次 |
| F04 | 两个并发 `finish()` | 共享同一 future；只发一次 done；返回同一 URL |
| F05 | `push_text()` 在 finish 后 | 本地状态错误，不发消息 |
| F06 | `abort()` 在 config 后 | socket close；无 final；无 part |
| F07 | `abort()` 在第一块 PCM 后 | 发布 aborted；删除 part；迟到 chunk 丢弃 |
| F08 | 重复 `abort()` | 幂等，无异常、无重复发布 |
| F09 | config 后立即 `error` | push/finish 失败；错误 message 被包装但不依赖字符串分类 |
| F10 | binary 在 `audio.start` 前 | 协议失败 |
| F11 | 重复 `audio.start` | 协议失败 |
| F12 | `audio.start.format != pcm` | 协议失败 |
| F13 | `audio.start.sample_rate != 24000` | 协议失败 |
| F14 | `sentence_index != 0` | 协议失败 |
| F15 | `sentence_text` 与拼接文本不一致 | 协议失败，不发布音频 |
| F16 | 奇数字节 binary frame | PCM 对齐失败 |
| F17 | `audio.done.total_bytes` 少/多 | 失败；part 删除 |
| F18 | `audio.done.error=true` 后 `session.done` | failure latch 保持失败 |
| F19 | `error` 后 `audio.done(error=false)` | 仍失败 |
| F20 | `session.done` 早于 `audio.done` | 协议失败 |
| F21 | `session.done.total_sentences=0` 且文本非空 | 失败 |
| F22 | 未知 JSON event | fail closed |
| F23 | WS 在 start 前关闭 | provider transport error |
| F24 | WS 在部分 PCM 后关闭 | aborted/failed；无 final |
| F25 | connect timeout | 明确 timeout code；无残留任务 |
| F26 | TTFA timeout | 关闭 socket；part 删除 |
| F27 | chunk idle timeout | 关闭 socket；part 删除 |
| F28 | 全任务 timeout | admission/资源释放一次 |
| F29 | 超过本地 max audio bytes | 主动 abort；不写爆磁盘 |
| F30 | 非 ASCII 中文、多次 delta | JSON 正确；server echo 文本校验通过 |
| F31 | config 接近 4 MiB | 本地阈值行为可预测；不无限复制 |
| F32 | fake 在收到 `input.text` 后、`input.done` 前发 PCM | 适配器可接收，但测试明确记录这不是官方 fixed-commit 行为 |
| F33 | `word_timestamps` 异常出现 `audio.chunk` | 默认适配器拒绝，避免双协议 |
| F34 | atomic rename 前故障 | final 不存在；part 清理 |
| F35 | rename 成功后回调故障 | final 文件仍一致；completion 只结算一次 |

并发 fake 测试：

| ID | 场景 | 断言 |
|---|---|---|
| C01 | 3 个 session 各自交错返回 chunk | 文件、generation、事件完全隔离 |
| C02 | session A abort，B/C 正常 | B/C 不关闭、不丢 chunk |
| C03 | admission max=1，3 个请求 | FIFO/定义好的公平策略；无超卖 |
| C04 | 排队请求取消 | 从队列移除；不占 active lease |
| C05 | active request 超时 | lease 精确释放一次；下一请求进入 |
| C06 | 三个相同 voice reference | config/缓存可复用，无可变对象串写 |
| C07 | 三个不同 voice reference | 每场发正确 voice，不串音色配置 |
| C08 | 一长两短 | 记录 head-of-line 行为；不把 WS 独立误判成 GPU 并行 |

## 11. 必须增加的真实 GPU 验收

Fake WS 只能验证客户端协议，不能证明 MOSS 路径可用。上线前至少做：

1. 固定 vLLM-Omni commit、模型 revision、deploy YAML、GPU 型号。
2. 8 个辩位 reference 各跑中文短句、长句、数字/英文混读。
3. 验证 `audio.start` 后确实多块 PCM，而不是一个最终大块。
4. 对比 final WAV 与实时发布 PCM bit-identical。
5. 记录：connect、`input.done`、`audio.start`、首 binary、末 binary、done 的单调时间戳。
6. 1/2/3 active，各持续至少 30 分钟；报告 p50/p95/p99 TTFA、最大 chunk gap、RTF、失败率、GPU 峰值。
7. 每档并发执行 abort storm，确认 engine request 被回收且后续请求不被毒化。
8. 对比 vLLM 在线输出与 OpenMOSS 原生 session 的音色、文本完整性、首音频延迟和跨 chunk 连续性。
9. 若仍使用固定版兼容 builder，必须特别检查短 prompt 之外的输出；源码已经声明该路径不是完整 Realtime processor 支持。

## 12. 对当前 V2 接口的最终映射

当前接口位于 `apps/api/app/services/realtime_voice.py:24-29`：

```python
class SpeechSynthesisSession(Protocol):
    async def push_text(self, text: str) -> None: ...
    async def finish(self) -> str: ...
    async def abort(self) -> None: ...
```

严格适配可保持签名不变，但必须把语义写进 provider capability：

```text
supports_incremental_text_input = true
supports_audio_before_finish = false
supports_cancel_ack = false
audio_format = pcm_s16le/24000/mono
max_sentences_per_ws = 1
```

V2 的 `IncrementalVoicePipeline` 会在 Agent final 前调用多次 `push_text()`；在此 fixed commit 上这些调用只能提前传输文本，不能提前生成音频。若产品 gate 要求“首个稳定 clause 到达后开始 TTS”，则 provider selection 必须拒绝该能力组合，而不是静默退化。

禁止用“每个 10–24 字 clause 新建一个 vLLM WS/REST request”伪装成增量 session：这会为每段重建生成状态，增加排队和 prompt/reference 开销，并容易造成语气、音色边界和间隙问题。OpenMOSS 原生设计本来就是一个 turn 内持续 `push_text`，应保留这一核心结构。

## 13. 最终 GO/NO-GO Gate

### 兼容适配 GO 条件

- 本文 F01–F35、C01–C07 全过。
- 单请求真实 GPU canary 证明 MOSS 在线 serving 输出正确。
- `audio.done.total_bytes`、最终 WAV、浏览器收到 PCM 三者一致。
- abort 后无 final 文件、无 orphan engine request。

### 核心实时后端 GO 条件

除上述条件外，还必须满足：

- `audio_before_finish=true`，且首块 PCM 在首批稳定文本送入后出现；
- 在线路径使用正确的 MOSS Realtime processor/session 语义，而非当前兼容性 `prompt_audio_array`；
- 2–3 场并发真实 GPU 数据达到项目阈值；
- 单场暂停/退出/结束不影响其他场。

在固定 commit `7aa5c9a...` 上，前两条不成立，因此核心实时后端结论是 **NO-GO**。
