# 14 · ASR / TTS 交互协议（讯飞实时转写 + 超拟人合成）

本章定义后端与讯飞语音服务的输入输出约定，对应 `需求 2.md` 的「TTS/ASR 的时机和流程是固定的，需要提供一个 md 文件协商好交互的输入输出」。真实密钥只通过环境变量注入，不写入仓库与前端。

时机（固定流程）：

1. 人类辩手发言：后端把浏览器上传的 16k/16bit PCM 分片实时转发给 ASR，`asr.partial` / `asr.final` 事件驱动大屏字幕。
2. AI 辩手发言：Agent 流式文本（`agent.speech.delta`）实时上大屏，同时把 final 文本送 TTS 合成音频播放/归档。

## 1. ASR：讯飞实时语音转写（RTASR 极速版）

- Endpoint：`wss://office-api-ast-dx.iflyaisol.com/ast/communicate/v1`
- 鉴权（查询参数）：`appId`、`accessKeyId`、`utc`、`signature`、`uuid`，以及 `audio_encode=pcm_s16le`、`lang`、`samplerate=16000`。
  - `utc`：东八区 ISO8601，如 `2025-09-04T15:38:07+0800`（URL 编码后入参与签名）。
  - `signature`：除 `signature` 外所有参数按参数名升序、键值各自 URL-encode 后以 `&` 连接，得到 baseString；`signature = base64(HMAC-SHA1(accessKeySecret, baseString))`。
- 音频帧：原始 PCM `16000Hz / 16bit / 单声道`，建议 `1280 字节 / 40ms` 推送；15s 无数据会被服务端断开。
- 结束：发送 JSON `{"end": true}`。
- 返回（JSON）：

```json
{
  "msg_type": "result",
  "data": {
    "cn": { "st": { "type": "0", "rt": [ { "ws": [ { "cw": [ { "w": "人机辩论赛" } ] } ] } ] } },
    "ls": true
  }
}
```

- 文本：拼接 `data.cn.st.rt[].ws[].cw[].w`。
- `data.cn.st.type`：`"1"` 中间结果（→ `asr.partial`），`"0"` 最终结果（→ `asr.final`）。
- 握手首帧 `data.action == "started"`；错误帧 `action == "error"` 或 `msg_type == "error"`。

实现：`app/services/xfyun_rtasr.py`（`XfyunRTASRGateway.recognize` 一次性识别、`open_stream` 流式会话）。`XFYUN_ASR_URL` 指向 iflyaisol 端点时由 `select_asr_gateway` 自动切到 RTASR，否则回退老版 IAT。

状态：握手 / 鉴权 / 分帧 / 结束 / 结果解析已对官方端点真实联调通过（`started → result → 正常关闭`）。识别文本质量需现场用真实麦克风音频核验。

## 2. TTS：讯飞超拟人合成（super smart-tts）

- Endpoint：`wss://cbm01.cn-huabei-1.xf-yun.com/v1/private/mcd9m97e6`
- 鉴权：标准讯飞 WebAPI 签名（`host` / `date` / `authorization` 的 HMAC-SHA256，放入查询参数），见 `app/services/xfyun_adapter.py:xfyun_signed_url`。
- 请求（`header` / `parameter` / `payload` 三段式，与老版 TTS 不同）：

```json
{
  "header": { "app_id": "<appid>", "status": 2 },
  "parameter": { "tts": { "vcn": "x7_xinchang_pro", "speed": 50, "volume": 50, "pitch": 50, "audio": { "encoding": "lame", "sample_rate": 24000, "channels": 1, "bit_depth": 16, "frame_size": 0 } } },
  "payload": { "text": { "encoding": "utf8", "compress": "raw", "format": "plain", "status": 2, "seq": 0, "text": "<base64>" } }
}
```

- 返回：音频在 `payload.audio.audio`（base64，按 `seq` 累加）；`header.status == 2` 或 `payload.audio.status == 2` 表示结束；`header.code != 0` 为错误。
- 发音人 `vcn`：该试用 app（`b16a5121`）**免费发音人**为 `x6_lingfeiyi_pro` / `x6_lingxiaoxuan_pro` / `x6_lingfeibo_pro` / `x6_lingxiaoyue_pro`，默认 `x6_lingfeiyi_pro`，可用 `XFYUN_TTS_VOICE` 覆盖。使用未授权发音人会返回 `LiccCheck failed ... licc limit`。

实现：`app/services/xfyun_gateway.py:XfyunTTSGateway.synthesize`；URL 含 `/v1/private/` 时自动用超拟人 schema（`build_super_tts_frame` / `extract_super_tts_audio`）。

状态：schema、鉴权、合成已对官方端点真实联调通过（免费 x6 发音人返回 MP3 音频，24kHz/单声道）。

## 3. 环境变量

```text
XFYUN_APP_ID=
XFYUN_API_KEY=          # = accessKeyId（RTASR）
XFYUN_API_SECRET=       # = accessKeySecret（RTASR）
XFYUN_ASR_URL=wss://office-api-ast-dx.iflyaisol.com/
XFYUN_TTS_URL=wss://cbm01.cn-huabei-1.xf-yun.com/v1/private/mcd9m97e6
XFYUN_ASR_LANG=autodialect
XFYUN_TTS_VOICE=x7_xinchang_pro
XFYUN_ASR_SCHEMA=       # 置 rtasr 可强制走极速版协议
XFYUN_TTS_SCHEMA=       # 置 super 可强制走超拟人 schema
```

密钥只存环境变量（本地走 gitignored 的 `.env`，生产走 systemd / 环境注入），不在前端、文档或仓库出现明文。
