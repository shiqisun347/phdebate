from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import struct
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles


APP_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = Path(os.getenv("QWEN_TTS_WEBUI_OUTPUT_DIR", str(APP_DIR / "outputs")))
TTS_BASE_URL = os.getenv("QWEN_TTS_BASE_URL", "http://127.0.0.1:12302").rstrip("/")
WEBUI_TOKEN = os.getenv("QWEN_TTS_WEBUI_TOKEN", "").strip()
DEFAULT_MODEL = os.getenv("QWEN_TTS_WEBUI_MODEL", "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice")

RECOMMENDED_VOICES = {"aiden", "ryan", "dylan", "sohee"}
PROBLEM_VOICES = {"serena", "eric", "uncle_fu", "vivian", "ono_anna"}

app = FastAPI(title="Qwen3-TTS Web UI", version="0.1.0")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/outputs", StaticFiles(directory=str(OUTPUT_DIR)), name="outputs")


def _check_token(token: str = "", x_debug_token: str = "") -> None:
    if WEBUI_TOKEN and token != WEBUI_TOKEN and x_debug_token != WEBUI_TOKEN:
        raise HTTPException(status_code=401, detail="invalid_token")


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def _bool_value(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off", ""}


def _int_value(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        number = default
    return max(low, min(high, number))


def _split_long_sentence(sentence: str, max_chars: int) -> list[str]:
    chunks: list[str] = []
    remaining = sentence.strip()
    soft_breaks = "，,、：: "
    while len(remaining) > max_chars:
        window = remaining[:max_chars]
        split_at = max(window.rfind(ch) for ch in soft_breaks)
        if split_at < max(18, int(max_chars * 0.45)):
            split_at = max_chars
        else:
            split_at += 1
        chunks.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


def _split_tts_text(text: str, max_chars: int) -> list[str]:
    normalized = re.sub(r"\s+", " ", text).strip()
    if not normalized:
        return []
    sentences: list[str] = []
    buf = ""
    for char in normalized:
        buf += char
        if char in "。！？!?；;":
            sentences.append(buf.strip())
            buf = ""
    if buf.strip():
        sentences.append(buf.strip())

    packed: list[str] = []
    current = ""
    for sentence in sentences:
        pieces = _split_long_sentence(sentence, max_chars) if len(sentence) > max_chars else [sentence]
        for piece in pieces:
            if not piece:
                continue
            if current and len(current) + len(piece) <= max_chars:
                current += piece
            else:
                if current:
                    packed.append(current)
                current = piece
    if current:
        packed.append(current)
    return packed or [normalized]


def _normalize_spoken_text(text: str) -> str:
    value = str(text or "").strip()
    value = re.sub(r"^感谢主席(?=[，,。！!？?；;\s]|$)", "感谢主持人", value)
    value = re.sub(r"(?<=[。！？；;])感谢主席(?=[，,。！!？?；;\s]|$)", "感谢主持人", value)
    return value


def _concat_audio_parts(audio_parts: list[bytes], suffix: str) -> tuple[bytes, str]:
    if len(audio_parts) <= 1:
        return (audio_parts[0] if audio_parts else b"", "single")
    if not shutil.which("ffmpeg"):
        return (b"".join(audio_parts), "byte_join_no_ffmpeg")
    with tempfile.TemporaryDirectory(prefix="qwen_tts_join_") as tmp:
        tmp_path = Path(tmp)
        list_path = tmp_path / "concat.txt"
        out_path = tmp_path / f"joined.{suffix}"
        lines = []
        for idx, audio in enumerate(audio_parts):
            part_path = tmp_path / f"{idx:04d}.{suffix}"
            part_path.write_bytes(audio)
            escaped = str(part_path).replace("'", "'\\''")
            lines.append(f"file '{escaped}'")
        list_path.write_text("\n".join(lines), encoding="utf-8")
        command = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(list_path)]
        if suffix == "wav":
            command += ["-c:a", "pcm_s16le", str(out_path)]
        else:
            command += ["-c", "copy", str(out_path)]
        try:
            subprocess.run(command, check=True, timeout=90)
            if out_path.exists() and out_path.stat().st_size > 0:
                return (out_path.read_bytes(), "ffmpeg_concat")
        except (OSError, subprocess.SubprocessError):
            pass
    return (b"".join(audio_parts), "byte_join_fallback")


def _wav_header(sample_rate: int, data_len: int) -> bytes:
    channels = 1
    bits = 16
    byte_rate = sample_rate * channels * bits // 8
    block_align = channels * bits // 8
    riff_size = 36 + data_len
    return (
        b"RIFF"
        + struct.pack("<I", riff_size)
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, channels, sample_rate, byte_rate, block_align, bits)
        + b"data"
        + struct.pack("<I", data_len)
    )


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * p))))
    return ordered[idx]


def _tts_payload_from_body(body: dict[str, Any], text: str, *, response_format: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": str(body.get("model") or DEFAULT_MODEL),
        "input": text,
        "voice": str(body.get("voice") or "dylan").strip().lower(),
        "response_format": response_format or str(body.get("response_format") or "mp3"),
        "speed": float(body.get("speed") or 1.0),
        "stream": bool(body.get("stream", True)),
    }
    optional_fields = (
        "language",
        "language_type",
        "instructions",
        "instruct",
        "chunk_size",
        "max_new_tokens",
        "temperature",
        "top_k",
        "top_p",
        "repetition_penalty",
        "volume",
        "pitch_rate",
    )
    for key in optional_fields:
        value = body.get(key)
        if value not in (None, ""):
            payload[key] = value
    return payload


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return INDEX_HTML


@app.get("/api/health")
async def health(token: str = Query(default=""), x_debug_token: str = Header(default="")) -> JSONResponse:
    _check_token(token, x_debug_token)
    async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=3.0)) as client:
        response = await client.get(f"{TTS_BASE_URL}/health")
    response.raise_for_status()
    data = response.json()
    voices = []
    for voice in data.get("supported_speakers") or []:
        normalized = str(voice).strip().lower()
        voices.append(
            {
                "id": normalized,
                "label": str(voice),
                "recommended": normalized in RECOMMENDED_VOICES,
                "problem": normalized in PROBLEM_VOICES,
            }
        )
    voices.sort(key=lambda item: (not item["recommended"], item["problem"], item["label"]))
    return JSONResponse(
        {
            "ok": True,
            "tts_base_url": TTS_BASE_URL,
            "model": data.get("model_id") or DEFAULT_MODEL,
            "default_voice": data.get("default_voice"),
            "voices": voices,
            "raw": data,
        }
    )


@app.get("/api/history")
async def history(token: str = Query(default=""), x_debug_token: str = Header(default="")) -> JSONResponse:
    _check_token(token, x_debug_token)
    items = []
    for meta_path in sorted(OUTPUT_DIR.glob("*.json"), reverse=True)[:50]:
        try:
            item = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        items.append(item)
    return JSONResponse({"ok": True, "items": items})


@app.post("/api/synthesize")
async def synthesize(request: Request, token: str = Query(default=""), x_debug_token: str = Header(default="")) -> JSONResponse:
    _check_token(token, x_debug_token)
    body = await request.json()
    text = str(body.get("input") or body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="input_required")
    spoken_text = _normalize_spoken_text(text)
    payload = _tts_payload_from_body(body, spoken_text)

    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=8.0)) as client:
            response = await client.post(f"{TTS_BASE_URL}/v1/audio/speech", json=payload)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"tts_request_failed: {exc}") from exc
    latency_ms = int((time.perf_counter() - started) * 1000)
    if response.status_code >= 400:
        detail = response.text[:1000]
        raise HTTPException(status_code=502, detail=f"tts_error_{response.status_code}: {detail}")

    audio = response.content
    if not audio:
        raise HTTPException(status_code=502, detail="empty_audio")
    suffix = "wav" if payload["response_format"] == "wav" else "mp3"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe_voice = "".join(ch for ch in payload["voice"] if ch.isalnum() or ch in {"_", "-"}) or "voice"
    file_name = f"{stamp}-{safe_voice}-{latency_ms}ms.{suffix}"
    audio_path = OUTPUT_DIR / file_name
    audio_path.write_bytes(audio)
    meta = {
        "created_at": stamp,
        "url": f"/outputs/{file_name}",
        "file_name": file_name,
        "voice": payload["voice"],
        "speed": payload["speed"],
        "temperature": payload.get("temperature"),
        "top_k": payload.get("top_k"),
        "top_p": payload.get("top_p"),
        "repetition_penalty": payload.get("repetition_penalty"),
        "size_bytes": len(audio),
        "latency_ms": latency_ms,
        "text": text[:300],
        "spoken_text": spoken_text[:300],
        "payload": {key: _jsonable(value) for key, value in payload.items() if key != "input"},
    }
    (OUTPUT_DIR / f"{file_name}.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return JSONResponse(
        {
            "ok": True,
            **meta,
            "mime_type": response.headers.get("content-type") or ("audio/wav" if suffix == "wav" else "audio/mpeg"),
            "audio_base64": base64.b64encode(audio).decode("ascii"),
        }
    )


@app.post("/api/stream-test")
async def stream_test(request: Request, token: str = Query(default=""), x_debug_token: str = Header(default="")) -> JSONResponse:
    _check_token(token, x_debug_token)
    body = await request.json()
    text = str(body.get("input") or body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="input_required")
    spoken_text = _normalize_spoken_text(text)
    payload = _tts_payload_from_body(body, spoken_text, response_format="pcm")
    payload["stream"] = True
    started = time.perf_counter()
    first_chunk_ms = 0
    last_chunk_at = started
    chunk_gaps_ms: list[float] = []
    chunks: list[bytes] = []
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=8.0)) as client:
            async with client.stream("POST", f"{TTS_BASE_URL}/v1/audio/speech", json=payload) as response:
                if response.status_code >= 400:
                    detail = (await response.aread())[:1000].decode("utf-8", "ignore")
                    raise HTTPException(status_code=502, detail=f"tts_error_{response.status_code}: {detail}")
                async for chunk in response.aiter_bytes():
                    if not chunk:
                        continue
                    now = time.perf_counter()
                    if not chunks:
                        first_chunk_ms = int((now - started) * 1000)
                    else:
                        chunk_gaps_ms.append((now - last_chunk_at) * 1000)
                    last_chunk_at = now
                    chunks.append(chunk)
    except HTTPException:
        raise
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"stream_request_failed: {exc}") from exc
    total_latency_ms = int((time.perf_counter() - started) * 1000)
    pcm = b"".join(chunks)
    if not pcm:
        raise HTTPException(status_code=502, detail="empty_stream")
    sample_rate = 24000
    wav_audio = _wav_header(sample_rate, len(pcm)) + pcm
    audio_duration_ms = int(len(pcm) / (sample_rate * 2) * 1000)
    avg_gap_ms = sum(chunk_gaps_ms) / len(chunk_gaps_ms) if chunk_gaps_ms else 0.0
    max_gap_ms = max(chunk_gaps_ms) if chunk_gaps_ms else 0.0
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe_voice = "".join(ch for ch in payload["voice"] if ch.isalnum() or ch in {"_", "-"}) or "voice"
    file_name = f"{stamp}-{safe_voice}-stream-{first_chunk_ms}ms.wav"
    audio_path = OUTPUT_DIR / file_name
    audio_path.write_bytes(wav_audio)
    meta = {
        "created_at": stamp,
        "kind": "stream_test",
        "url": f"/outputs/{file_name}",
        "file_name": file_name,
        "voice": payload["voice"],
        "speed": payload["speed"],
        "size_bytes": len(wav_audio),
        "pcm_bytes": len(pcm),
        "chunk_count": len(chunks),
        "first_chunk_ms": first_chunk_ms,
        "total_latency_ms": total_latency_ms,
        "audio_duration_ms": audio_duration_ms,
        "rtf": round(total_latency_ms / audio_duration_ms, 3) if audio_duration_ms else None,
        "avg_chunk_gap_ms": round(avg_gap_ms, 1),
        "p95_chunk_gap_ms": round(_percentile(chunk_gaps_ms, 0.95), 1),
        "max_chunk_gap_ms": round(max_gap_ms, 1),
        "text": text[:300],
        "spoken_text": spoken_text[:300],
        "payload": {key: _jsonable(value) for key, value in payload.items() if key != "input"},
    }
    (OUTPUT_DIR / f"{file_name}.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return JSONResponse({"ok": True, **meta, "mime_type": "audio/wav", "audio_base64": base64.b64encode(wav_audio).decode("ascii")})


@app.post("/api/pcm-stream")
async def pcm_stream(request: Request, token: str = Query(default=""), x_debug_token: str = Header(default="")) -> StreamingResponse:
    _check_token(token, x_debug_token)
    body = await request.json()
    text = str(body.get("input") or body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="input_required")
    spoken_text = _normalize_spoken_text(text)
    payload = _tts_payload_from_body(body, spoken_text, response_format="pcm")
    payload["stream"] = True

    async def body_iter():
        async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=8.0)) as client:
            async with client.stream("POST", f"{TTS_BASE_URL}/v1/audio/speech", json=payload) as response:
                if response.status_code >= 400:
                    detail = (await response.aread())[:1000]
                    raise RuntimeError(f"tts_error_{response.status_code}: {detail.decode('utf-8', 'ignore')}")
                async for chunk in response.aiter_bytes():
                    if chunk:
                        yield chunk

    return StreamingResponse(
        body_iter(),
        media_type="audio/L16",
        headers={
            "X-Sample-Rate": "24000",
            "X-Channels": "1",
            "X-Sample-Format": "s16le",
            "X-Server-Tempo-Speed": str(float(body.get("speed") or 1.0)),
            "X-Client-Playback-Speed": "1.0",
        },
    )


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Qwen3-TTS 调试台</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #fff;
      --line: #d9dee7;
      --text: #172033;
      --muted: #657083;
      --accent: #0f766e;
      --blue: #1d4ed8;
      --warn: #b45309;
      --bad: #b91c1c;
    }
    * { box-sizing: border-box; }
    body { margin: 0; font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--bg); color: var(--text); }
    header { display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 18px 22px; border-bottom: 1px solid var(--line); background: var(--panel); position: sticky; top: 0; z-index: 5; }
    h1 { margin: 0; font-size: 20px; letter-spacing: 0; }
    main { display: grid; grid-template-columns: minmax(360px, 520px) 1fr; gap: 16px; padding: 16px; }
    section { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 14px; }
    .grid { display: grid; gap: 12px; }
    .cols { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
    .cols3 { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
    label { display: grid; gap: 5px; color: var(--muted); font-size: 12px; }
    input, select, textarea, button { font: inherit; border-radius: 6px; border: 1px solid var(--line); }
    input, select, textarea { width: 100%; padding: 9px 10px; background: #fff; color: var(--text); }
    textarea { min-height: 168px; resize: vertical; line-height: 1.55; }
    button { display: inline-flex; align-items: center; justify-content: center; gap: 8px; padding: 9px 12px; background: var(--accent); color: white; border-color: var(--accent); cursor: pointer; }
    button.secondary { background: #fff; color: var(--text); border-color: var(--line); }
    button:disabled { opacity: .55; cursor: not-allowed; }
    audio { width: 100%; margin-top: 10px; }
    pre { margin: 0; padding: 12px; background: #111827; color: #e5e7eb; border-radius: 8px; overflow: auto; max-height: 360px; font-size: 12px; line-height: 1.5; }
    .row { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; }
    .status { color: var(--muted); font-size: 13px; }
    .pill { display: inline-flex; align-items: center; border-radius: 999px; padding: 3px 8px; font-size: 12px; border: 1px solid var(--line); color: var(--muted); }
    .pill.good { color: var(--accent); border-color: #99d6cc; background: #ecfdf5; }
    .pill.bad { color: var(--bad); border-color: #fecaca; background: #fef2f2; }
    .metric { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; }
    .metric div { border: 1px solid var(--line); border-radius: 8px; padding: 10px; background: #fbfcfe; }
    .metric strong { display: block; font-size: 18px; }
    .history { display: grid; gap: 8px; max-height: 340px; overflow: auto; }
    .history button { justify-content: flex-start; color: var(--text); background: #fff; border-color: var(--line); text-align: left; }
    @media (max-width: 900px) {
      main { grid-template-columns: 1fr; }
      .cols, .cols3, .metric { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>Qwen3-TTS 调试台</h1>
      <div class="status" id="health">连接中…</div>
    </div>
    <div class="row">
      <span class="pill good">试听不写入 phdebate</span>
      <button class="secondary" id="refresh">刷新状态</button>
    </div>
  </header>
  <main>
    <section class="grid">
      <div class="cols">
        <label>音色
          <select id="voice"></select>
        </label>
        <label>格式
          <select id="response_format">
            <option value="mp3">mp3</option>
            <option value="wav">wav</option>
          </select>
        </label>
      </div>
      <label>模型
        <input id="model" value="Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice" />
      </label>
      <label>文本
        <textarea id="input">感谢主席，各位评委同学大家好。我方将用正式、清晰、平直的语气完成本轮发言。</textarea>
      </label>
      <div class="cols3">
        <label>语速 speed
          <input id="speed" type="number" min="0.5" max="2.2" step="0.05" value="1.95" />
        </label>
        <label>温度 temperature
          <input id="temperature" type="number" min="0" max="1" step="0.01" value="0.05" />
        </label>
        <label>top_k
          <input id="top_k" type="number" min="1" max="100" step="1" value="20" />
        </label>
      </div>
      <div class="cols3">
        <label>repetition_penalty
          <input id="repetition_penalty" type="number" min="0.8" max="2" step="0.05" value="1.1" />
        </label>
        <label>chunk_size
          <input id="chunk_size" type="number" min="1" max="64" step="1" value="8" />
        </label>
        <label>max_new_tokens
          <input id="max_new_tokens" type="number" min="128" max="4096" step="64" value="2048" />
        </label>
      </div>
      <div class="cols3">
        <label>top_p 兼容字段
          <input id="top_p" type="number" min="0" max="1" step="0.01" value="0.5" />
        </label>
        <label>音量 volume
          <input id="volume" type="number" min="0" max="100" step="1" value="70" />
        </label>
        <label>音调 pitch_rate
          <input id="pitch_rate" type="number" min="0.5" max="2" step="0.05" value="1" />
        </label>
      </div>
      <label>Instructions
        <textarea id="instructions" style="min-height:92px">正式、平直、清晰、克制，接近现场辩论正常发言；不要戏剧化，不要抑扬顿挫，不要夸张情绪，不要拖腔，不要口音化，不要故意拉长字音，保持稳定音量和自然停顿。</textarea>
      </label>
      <div class="row">
        <button id="synth">合成并播放</button>
        <button id="livePlay" class="secondary">边收边播</button>
        <button id="streamTest" class="secondary">流式性能测试</button>
        <button class="secondary" id="copy">复制 Payload</button>
        <button class="secondary" id="reset">恢复推荐参数</button>
        <span class="status" id="status"></span>
      </div>
    </section>
    <div class="grid">
      <section class="grid">
        <div class="metric">
          <div><span class="status">延迟</span><strong id="latency">-</strong></div>
          <div><span class="status">大小</span><strong id="bytes">-</strong></div>
          <div><span class="status">时长</span><strong id="duration">-</strong></div>
          <div><span class="status">voice</span><strong id="voiceMetric">-</strong></div>
        </div>
        <audio id="audio" controls></audio>
        <a id="download" href="#" download style="display:none">下载本次音频</a>
      </section>
      <section class="grid">
        <div class="row"><strong>请求 Payload</strong><span class="status">用于复现实验</span></div>
        <pre id="payload">{}</pre>
      </section>
      <section class="grid">
        <div class="row"><strong>最近生成</strong><button class="secondary" id="historyRefresh">刷新</button></div>
        <div class="history" id="history"></div>
      </section>
    </div>
  </main>
  <script>
    const qs = new URLSearchParams(location.search);
    const token = qs.get("token") || localStorage.getItem("qwen_tts_webui_token") || "";
    if (qs.get("token")) localStorage.setItem("qwen_tts_webui_token", qs.get("token"));
    const $ = (id) => document.getElementById(id);
    const numeric = (id) => {
      const value = $(id).value;
      return value === "" ? undefined : Number(value);
    };
    function api(path, options = {}) {
      const url = new URL(path, location.origin);
      if (token) url.searchParams.set("token", token);
      return fetch(url, { ...options, headers: { "Content-Type": "application/json", "X-Debug-Token": token, ...(options.headers || {}) } });
    }
    function payload() {
      const body = {
        model: $("model").value.trim(),
        input: $("input").value.trim(),
        voice: $("voice").value,
        response_format: $("response_format").value,
        speed: numeric("speed"),
        stream: true,
        language_type: "Chinese",
        instructions: $("instructions").value.trim(),
        chunk_size: numeric("chunk_size"),
        max_new_tokens: numeric("max_new_tokens"),
        temperature: numeric("temperature"),
        top_k: numeric("top_k"),
        top_p: numeric("top_p"),
        repetition_penalty: numeric("repetition_penalty"),
        volume: numeric("volume"),
        pitch_rate: numeric("pitch_rate"),
      };
      for (const key of Object.keys(body)) {
        if (body[key] === undefined || body[key] === "") delete body[key];
      }
      $("payload").textContent = JSON.stringify(body, null, 2);
      return body;
    }
    async function loadHealth() {
      try {
        const res = await api("/api/health");
        if (!res.ok) throw new Error(await res.text());
        const data = await res.json();
        $("health").textContent = `${data.raw.model_id || data.model} · ${data.raw.device || ""} · ${data.raw.dtype || ""}`;
        $("model").value = data.model || $("model").value;
        $("voice").innerHTML = "";
        for (const voice of data.voices) {
          const opt = document.createElement("option");
          opt.value = voice.id;
          opt.textContent = `${voice.label}${voice.recommended ? " · 推荐" : ""}${voice.problem ? " · 问题候选" : ""}`;
          $("voice").appendChild(opt);
        }
        if ([...$("voice").options].some((item) => item.value === "dylan")) $("voice").value = "dylan";
        payload();
      } catch (err) {
        $("health").textContent = `连接失败：${err.message || err}`;
      }
    }
    async function synth() {
      const body = payload();
      if (!body.input) return;
      $("synth").disabled = true;
      $("status").textContent = "合成中…";
      try {
        const res = await api("/api/synthesize", { method: "POST", body: JSON.stringify(body) });
        if (!res.ok) throw new Error(await res.text());
        const data = await res.json();
        const bytes = Uint8Array.from(atob(data.audio_base64), c => c.charCodeAt(0));
        const blob = new Blob([bytes], { type: data.mime_type || "audio/mpeg" });
        const objectUrl = URL.createObjectURL(blob);
        $("audio").src = objectUrl;
        $("audio").play().catch(() => {});
        $("download").href = data.url;
        $("download").download = data.file_name;
        $("download").style.display = "inline";
        $("latency").textContent = `${data.latency_ms}ms`;
        $("bytes").textContent = `${Math.round(data.size_bytes / 1024)}KB`;
        $("voiceMetric").textContent = data.voice;
        $("status").textContent = "完成";
        $("audio").onloadedmetadata = () => {
          $("duration").textContent = Number.isFinite($("audio").duration) ? `${$("audio").duration.toFixed(1)}s` : "-";
        };
        await loadHistory();
      } catch (err) {
        $("status").textContent = `失败：${err.message || err}`;
      } finally {
        $("synth").disabled = false;
      }
    }
    async function streamTest() {
      const body = payload();
      if (!body.input) return;
      $("streamTest").disabled = true;
      $("status").textContent = "流式测试中…";
      try {
        const res = await api("/api/stream-test", { method: "POST", body: JSON.stringify(body) });
        if (!res.ok) throw new Error(await res.text());
        const data = await res.json();
        const bytes = Uint8Array.from(atob(data.audio_base64), c => c.charCodeAt(0));
        const blob = new Blob([bytes], { type: "audio/wav" });
        const objectUrl = URL.createObjectURL(blob);
        $("audio").src = objectUrl;
        $("audio").play().catch(() => {});
        $("download").href = data.url;
        $("download").download = data.file_name;
        $("download").style.display = "inline";
        $("latency").textContent = `${data.first_chunk_ms}ms`;
        $("bytes").textContent = `${Math.round(data.size_bytes / 1024)}KB`;
        $("duration").textContent = `${(data.audio_duration_ms / 1000).toFixed(1)}s`;
        $("voiceMetric").textContent = data.voice;
        $("status").textContent = `流式完成 · 总 ${data.total_latency_ms}ms · ${data.chunk_count} 片 · 平均间隔 ${data.avg_chunk_gap_ms}ms · 最大间隔 ${data.max_chunk_gap_ms}ms · RTF ${data.rtf}`;
        $("payload").textContent = JSON.stringify({ request: body, stream_result: data }, null, 2);
        await loadHistory();
      } catch (err) {
        $("status").textContent = `流式失败：${err.message || err}`;
      } finally {
        $("streamTest").disabled = false;
      }
    }
    function pcmBytesToFloat32(bytes, state) {
      let data = bytes;
      if (state.carry) {
        data = new Uint8Array(state.carry.length + bytes.length);
        data.set(state.carry, 0);
        data.set(bytes, state.carry.length);
        state.carry = null;
      }
      if (data.length % 2) {
        state.carry = data.slice(data.length - 1);
        data = data.slice(0, data.length - 1);
      }
      const view = new DataView(data.buffer, data.byteOffset, data.byteLength);
      const out = new Float32Array(data.byteLength / 2);
      for (let i = 0; i < out.length; i += 1) {
        out[i] = Math.max(-1, Math.min(1, view.getInt16(i * 2, true) / 32768));
      }
      return out;
    }
    function wavBlobFromPcm(chunks, sampleRate) {
      const dataBytes = chunks.reduce((sum, item) => sum + item.byteLength, 0);
      const buffer = new ArrayBuffer(44 + dataBytes);
      const view = new DataView(buffer);
      const write = (offset, text) => {
        for (let i = 0; i < text.length; i += 1) view.setUint8(offset + i, text.charCodeAt(i));
      };
      write(0, "RIFF");
      view.setUint32(4, 36 + dataBytes, true);
      write(8, "WAVEfmt ");
      view.setUint32(16, 16, true);
      view.setUint16(20, 1, true);
      view.setUint16(22, 1, true);
      view.setUint32(24, sampleRate, true);
      view.setUint32(28, sampleRate * 2, true);
      view.setUint16(32, 2, true);
      view.setUint16(34, 16, true);
      write(36, "data");
      view.setUint32(40, dataBytes, true);
      let offset = 44;
      const target = new Uint8Array(buffer);
      for (const chunk of chunks) {
        target.set(chunk, offset);
        offset += chunk.byteLength;
      }
      return new Blob([buffer], { type: "audio/wav" });
    }
    async function livePlay() {
      const body = payload();
      if (!body.input) return;
      $("livePlay").disabled = true;
      $("status").textContent = "边收边播连接中…";
      const sampleRate = 24000;
      const startedAt = performance.now();
      let audioContext = null;
      let nextTime = 0;
      let firstByteAt = 0;
      let firstPlayAt = 0;
      let lastChunkAt = 0;
      let chunkCount = 0;
      let byteCount = 0;
      let underruns = 0;
      let maxGap = 0;
      const pcmChunks = [];
      const pcmState = { carry: null };
      try {
        audioContext = new AudioContext();
        await audioContext.resume();
        nextTime = audioContext.currentTime + 0.16;
        const res = await api("/api/pcm-stream", { method: "POST", body: JSON.stringify(body) });
        if (!res.ok || !res.body) throw new Error(await res.text());
        const reader = res.body.getReader();
        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          if (!value || !value.byteLength) continue;
          const now = performance.now();
          if (!firstByteAt) firstByteAt = now;
          if (lastChunkAt) maxGap = Math.max(maxGap, now - lastChunkAt);
          lastChunkAt = now;
          chunkCount += 1;
          byteCount += value.byteLength;
          pcmChunks.push(value.slice());
          const floats = pcmBytesToFloat32(value, pcmState);
          if (!floats.length) continue;
          const buffer = audioContext.createBuffer(1, floats.length, sampleRate);
          buffer.copyToChannel(floats, 0);
          const source = audioContext.createBufferSource();
          source.buffer = buffer;
          source.playbackRate.value = 1;
          source.connect(audioContext.destination);
          if (nextTime < audioContext.currentTime + 0.02) {
            if (chunkCount > 1) underruns += 1;
            nextTime = audioContext.currentTime + 0.04;
          }
          source.start(nextTime);
          if (!firstPlayAt) firstPlayAt = performance.now();
          nextTime += buffer.duration;
          const queued = Math.max(0, nextTime - audioContext.currentTime);
          $("latency").textContent = `${Math.round(firstByteAt - startedAt)}ms`;
          $("bytes").textContent = `${Math.round(byteCount / 1024)}KB`;
          $("duration").textContent = `${queued.toFixed(2)}s 队列`;
          $("voiceMetric").textContent = body.voice;
          $("status").textContent = `边播中 · 服务端保音调变速 ${body.speed || 1}x · 首包 ${Math.round(firstByteAt - startedAt)}ms · 首播 ${Math.round(firstPlayAt - startedAt)}ms · ${chunkCount} 片 · 最大间隔 ${Math.round(maxGap)}ms · 断流 ${underruns}`;
        }
        const wav = wavBlobFromPcm(pcmChunks, sampleRate);
        const objectUrl = URL.createObjectURL(wav);
        $("download").href = objectUrl;
        $("download").download = `live-${Date.now()}-${body.voice || "voice"}.wav`;
        $("download").style.display = "inline";
        $("status").textContent = `边播完成 · 服务端保音调变速 ${body.speed || 1}x · 首包 ${Math.round(firstByteAt - startedAt)}ms · 首播 ${Math.round(firstPlayAt - startedAt)}ms · ${chunkCount} 片 · ${Math.round(byteCount / 1024)}KB · 最大间隔 ${Math.round(maxGap)}ms · 断流 ${underruns}`;
      } catch (err) {
        $("status").textContent = `边播失败：${err.message || err}`;
      } finally {
        $("livePlay").disabled = false;
      }
    }
    async function loadHistory() {
      const res = await api("/api/history");
      if (!res.ok) return;
      const data = await res.json();
      $("history").innerHTML = "";
      for (const item of data.items) {
        const btn = document.createElement("button");
        btn.className = "secondary";
        btn.textContent = `${item.created_at} · ${item.voice} · ${item.latency_ms}ms · ${item.text}`;
        btn.onclick = () => {
          $("audio").src = item.url;
          $("download").href = item.url;
          $("download").download = item.file_name;
          $("download").style.display = "inline";
          $("audio").play().catch(() => {});
        };
        $("history").appendChild(btn);
      }
    }
    $("synth").onclick = synth;
    $("livePlay").onclick = livePlay;
    $("streamTest").onclick = streamTest;
    $("refresh").onclick = loadHealth;
    $("historyRefresh").onclick = loadHistory;
    $("copy").onclick = () => navigator.clipboard?.writeText(JSON.stringify(payload(), null, 2));
    $("reset").onclick = () => {
      $("speed").value = "1.95";
      $("temperature").value = "0.05";
      $("top_k").value = "20";
      $("top_p").value = "0.5";
      $("repetition_penalty").value = "1.1";
      $("chunk_size").value = "8";
      $("max_new_tokens").value = "2048";
      $("volume").value = "70";
      $("pitch_rate").value = "1";
      $("instructions").value = "正式、平直、清晰、克制，接近现场辩论正常发言；不要戏剧化，不要抑扬顿挫，不要夸张情绪，不要拖腔，不要口音化，不要故意拉长字音，保持稳定音量和自然停顿。";
      payload();
    };
    for (const id of ["model","input","voice","response_format","speed","temperature","top_k","top_p","repetition_penalty","chunk_size","max_new_tokens","volume","pitch_rate","instructions"]) {
      $(id).addEventListener("input", payload);
      $(id).addEventListener("change", payload);
    }
    loadHealth();
    loadHistory();
  </script>
</body>
</html>"""
