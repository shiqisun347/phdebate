# MOSS Realtime Gateway 只读审查

审查日期：2026-07-18  
审查对象：`v2/services/moss-realtime-gateway/`  
对照上游：OpenMOSS/MOSS-TTS `ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af`  
初始结论：**当前可做导入/启动 smoke，但不应进入正式 GPU 音频验收；先修复下面两个 P0。**

## 复审后的修复状态

主线程已在本次审查后修复并增加回归：

- 正常 PCM 改为 lossless per-turn backlog，EOF 使用独立事件，不再用“清空正常音频”腾出终止标记；慢消费者和 final ACK 不再互相制造假 orphan。
- final/close/abort 使用单次原子 terminal 注册；并发终止请求加入同一 worker 退出结果，abort 可升级正在进行的 graceful terminal，不再入队第二个永远无法确认的命令。
- `/start` 只有在携带的 initial text 真正处理成功后才确认；V2 正式调用改为先用空文本 start、建立 audio GET，再 push 第一段。
- OpenMOSS 模式强制 API key、固定 8 音色、固定 upstream/model/codec revision，并校验实际导入模块位于固定 checkout 且 Git HEAD 匹配。
- startup 对 8 个音色逐个执行真实短句 warmup；shutdown 清模型/codec 引用并释放 CUDA cache。
- 已完成 session 有界保留，拒绝活动 session_id 以不同参数复用，并增加单 session 文本安全上限。

新增定向测试覆盖尾音不丢、无早期消费者、close/abort 竞态、initial 生成失败、配置 fail-closed；网关测试更新为 `15 passed`。另用 3 个独立 fake endpoint 完成 1/2/3 路各 20 批，共 120/120 成功、120/120 final 前首包、120/120 release 确认，协议生命周期门通过。真实 GPU 音质、RTF、CER 和浏览器首声仍未验收，因此生产开关继续关闭。

## 阻断项

### [P0] 正常音频结束会因有界队列而阻塞 ACK，或直接丢弃尚未播放的音频

位置：

- [`runtime.py:467`](/Users/sunshiqi/code/phdebate/v2/services/moss-realtime-gateway/moss_realtime_gateway/runtime.py:467) 的 `_emit` 会在 audio queue 满时持续等待消费者；
- [`runtime.py:447`](/Users/sunshiqi/code/phdebate/v2/services/moss-realtime-gateway/moss_realtime_gateway/runtime.py:447) 正常结束写 `_AUDIO_END` 使用 `put_nowait`，队列满时调用 `clear_audio(terminal=True)`，会把全部未播放 PCM 清空。

影响：

- 如果客户端按现有测试中的顺序“final/close 后再 GET audio”，较长真实音频会填满队列，final/close 永远无法确认，最终被误判 orphan；
- 如果生成音频刚好填满队列，worker 能正常退出，但结束标记写入失败后会清空整个 backlog，客户端只读到 EOF，音频静默丢失；
- 慢速网络或暂时落后的 WebRTC 转发也可能在最后一刻触发整段尾音丢失。

已用当前 fake backend 定向复现：

- `audio_queue_chunks=1`，不先连接 audio stream，`close` 得到 `SessionOrphaned(close_ack_timeout)`；
- `audio_queue_chunks=2`，initial + final 正好填满队列，final 返回 `closed/released=true`，但随后 audio 第一项直接为 EOF。

建议：audio stream 必须在 start 后、任何正文 push 前建立；同时终止标记不能通过“清空正常音频”腾位置。应使用不会丢数据的关闭状态/独立 event，或允许消费者在队列排空后观察 EOF。控制 ACK 与客户端消费速度也不应绑定。

### [P0] final、close、abort 和音频断连之间存在双终止竞态，可把健康进程误标为 orphan

位置：

- [`runtime.py:242`](/Users/sunshiqi/code/phdebate/v2/services/moss-realtime-gateway/moss_realtime_gateway/runtime.py:242)、[`runtime.py:260`](/Users/sunshiqi/code/phdebate/v2/services/moss-realtime-gateway/moss_realtime_gateway/runtime.py:260)、[`runtime.py:276`](/Users/sunshiqi/code/phdebate/v2/services/moss-realtime-gateway/moss_realtime_gateway/runtime.py:276) 都可在没有原子 terminal 状态的情况下各自入队；
- worker 只处理第一个 terminal command 后退出，[`runtime.py:408`](/Users/sunshiqi/code/phdebate/v2/services/moss-realtime-gateway/moss_realtime_gateway/runtime.py:408)；后续 command 永远不会 acknowledged；
- audio HTTP 断连还会自动发起 abort，[`app.py:126`](/Users/sunshiqi/code/phdebate/v2/services/moss-realtime-gateway/moss_realtime_gateway/app.py:126)。

已复现：并发 `close` 与 `abort` 时，close 正常返回 `aborted/released=true`，abort 等待超时并把 readiness 永久改为 503、`orphan_count=1`，尽管 worker 已干净退出。

影响：浏览器主动打断、音频连接同时关闭、上层重复重试等正常场景即可触发整台 endpoint fail-fast。

建议：在 session lock 下做一次性 terminal CAS，记录唯一 terminal future；后续 final/close/abort 应合并到同一个结果，或按优先级把 graceful final 升级为 abort，而不能再创建第二个待确认 command。协议还需要 `generation_id + seq`，用于去重、排序和拒绝旧轮请求。

## 高优先级问题

### [P1] `/start` 在 initial text 真正进入模型前就确认成功

worker 在 [`runtime.py:398`](/Users/sunshiqi/code/phdebate/v2/services/moss-realtime-gateway/moss_realtime_gateway/runtime.py:398) 设置 `status=active` 和 `started`，随后才在下一行生成 initial audio。若首段 prefill/decode 失败，HTTP start 可能已经返回 200。

已复现：让 `initial_audio()` 延迟 200 ms 后抛错，`start_session` 在 0 ms 左右返回 `active`；稍后 session 变为 `failed`。

建议：区分 `turn_opened` 与 `initial_processed`。如果 start 携带 `assistant_text`，ACK 至少应等该文本被 session 接受；首段生成错误必须反映在 start/push 响应中。更简单的协议是 start 只建 session，首段统一通过带 seq 的 push 提交。

### [P1] “固定上游 commit”目前只是配置字符串，未验证实际导入源码

[`config.py:114`](/Users/sunshiqi/code/phdebate/v2/services/moss-realtime-gateway/moss_realtime_gateway/config.py:114) 只检查环境变量等于目标 SHA；[`backends.py:236`](/Users/sunshiqi/code/phdebate/v2/services/moss-realtime-gateway/moss_realtime_gateway/backends.py:236) 随后直接导入当前 `PYTHONPATH` 上的 `mossttsrealtime`，没有检查模块文件、Git HEAD 或源码 hash。health 因而可能报告 `ad99ec5...`，实际运行另一版本。

另外 README 所写“upstream checkout 放到 `PYTHONPATH`”不够准确：把 MOSS-TTS 仓库根目录放入 `PYTHONPATH` 时，`import mossttsrealtime` 会失败；当前 import 需要把 `MOSS-TTS/moss_tts_realtime` 放入路径，或改用正确的安装包路径。

建议在 GPU smoke 前增加启动 fail-fast：输出并验证 `mossttsrealtime.__file__`，校验固定 checkout 的 Git HEAD/manifest hash；health 同时报告实际 model、codec revision。提供精确安装命令或镜像 digest。

### [P1] OpenMOSS 模式未强制鉴权和全部固定版本/音色约束

- API key 为空时鉴权完全关闭，[`app.py:65`](/Users/sunshiqi/code/phdebate/v2/services/moss-realtime-gateway/moss_realtime_gateway/app.py:65)；`openmoss` 配置校验未要求非空 key；
- `MOSS_GATEWAY_REQUIRE_EIGHT_PROMPTS=false` 可以绕过固定 8 音色要求；
- model/codec revision 可任意覆盖，只有 upstream revision 被固定，[`config.py:105`](/Users/sunshiqi/code/phdebate/v2/services/moss-realtime-gateway/moss_realtime_gateway/config.py:105)。

README 把这些称为 required production configuration，但代码没有 fail closed。建议在 `backend=openmoss` 时强制 API key、8 个 voice ID 和三个固定 revision，除非显式启用仅限测试的 insecure 标志。

## 其他问题

### [P2] warmup 没有逐个验证 8 个音色的真实合成

[`backends.py:303`](/Users/sunshiqi/code/phdebate/v2/services/moss-realtime-gateway/moss_realtime_gateway/backends.py:303) 会编码全部 prompt，但只用第一个 prompt 做一次真实 synthesis。这样可以预热模型/compile，却不能发现其他音色在 `make_ensemble`、上下文长度或实际听感上的问题。

GPU 验收前至少应对 8 个 voice 各做短句 smoke，并保存每个 voice 的首包、RTF、音频非静音和 speaker embedding 结果。

### [P2] 完成 session 和 GPU backend 资源没有显式回收

`_sessions` 中的已完成 Session 从不删除；长时间运行会保留所有历史 session、queue 和 command 对象。[`runtime.py:209`](/Users/sunshiqi/code/phdebate/v2/services/moss-realtime-gateway/moss_realtime_gateway/runtime.py:209)

backend shutdown 只清 prompt token cache，没有释放 model/codec 引用或 CUDA cache。[`backends.py:382`](/Users/sunshiqi/code/phdebate/v2/services/moss-realtime-gateway/moss_realtime_gateway/backends.py:382) 单进程 supervisor 重启时影响较小，但不适合应用内重载或长期复用。

同一 `session_id` 再次 start 时，如果旧 worker 仍活跃，会直接返回旧 session 状态，新的 voice/user/assistant 参数被静默忽略。[`runtime.py:209`](/Users/sunshiqi/code/phdebate/v2/services/moss-realtime-gateway/moss_realtime_gateway/runtime.py:209)

## 与 OpenMOSS 固定 commit 的兼容性判断

已确认的正确点：

- 固定 commit 中确实存在 `MossTTSRealtimeTextStreamBridge`；
- `push_text_delta()` 会调用原生 `session.push_text()`，保留标点/短语聚合，属于真实模型级增量，不是每块独立 TTS；
- 每 turn 只进入一次 `codec.streaming(batch_size=1)`，worker finally 后退出 context；
- 当前新增 cancel event 将 `inferencer.is_finished` 与 abort 关联，可在当前一次 GPU prefill/step 返回后停止后续 drain；
- 单活设计避免了多个线程同时驱动共享 model/codec。

仍需明确的边界：

- abort 不能抢占正在执行的单次 CUDA kernel/prefill，只能在当前步骤返回后停止；15 秒 grace 是否足够必须用 GPU 实测；
- 协议是多次 HTTP push，不是持久 WS；正文/thinking 过滤仍完全依赖上游调用者；
- 请求没有 seq、generation ID、幂等键和文本长度限制，不能抵抗重试、乱序或超大 delta。

## 验证结果

- 现有测试：`11 passed`；Ruff、`py_compile` 通过；
- 测试全部使用 fake backend，未覆盖真实 CUDA/import/revision；
- 本审查额外复现了：队列满导致 close orphan、正常结束清空音频、start 假成功、双 terminal 误 orphan。

修复 P0 后再执行 GPU gate：固定源码校验 -> 8 音色 warmup -> audio 先连接 -> 单轮增量 -> final 排空 -> abort 残音为 0 -> 慢消费者 -> close/abort/断连竞态。
