#!/usr/bin/env python3
"""Synthesize debate voices with LightTTS and verify their content through FunASR."""

from __future__ import annotations

import argparse
import asyncio
import audioop
import difflib
import json
import re
import shutil
import wave

import websockets
from app.core.config import settings
from app.services.providers import lighttts


def pcm16k(path) -> bytes:
    with wave.open(str(path), "rb") as audio:
        channels = audio.getnchannels()
        width = audio.getsampwidth()
        rate = audio.getframerate()
        frames = audio.readframes(audio.getnframes())
    if channels != 1 or width != 2:
        raise RuntimeError(f"unsupported generated WAV format: channels={channels} width={width}")
    if rate != 16000:
        frames, _state = audioop.ratecv(frames, width, channels, rate, 16000, None)
    return frames


async def transcribe(path, *, name: str) -> tuple[str, int]:
    frames = pcm16k(path)
    async with websockets.connect(settings.funasr_ws_url, max_size=8 * 1024 * 1024) as socket:
        await socket.send("START")
        await socket.send("LANGUAGE:zh-CN")
        await asyncio.sleep(0.1)
        for offset in range(0, len(frames), 3200):
            await socket.send(frames[offset : offset + 3200])
            await asyncio.sleep(0.005)
        await socket.send("STOP")
        partial_text = ""
        final_text = ""
        latency_ms = 0
        deadline = asyncio.get_running_loop().time() + 15
        while asyncio.get_running_loop().time() < deadline:
            try:
                raw = await asyncio.wait_for(socket.recv(), timeout=2)
            except asyncio.TimeoutError:
                continue
            payload = json.loads(raw)
            sentences = payload.get("sentences") if isinstance(payload.get("sentences"), list) else []
            sentence_text = "".join(str(item.get("text") or "") for item in sentences if isinstance(item, dict))
            text = str(payload.get("text") or payload.get("partial") or sentence_text).strip()
            is_final = bool(payload.get("is_final")) or payload.get("mode") in {"2pass-offline", "offline"}
            if text and is_final:
                final_text = text
            elif text:
                partial_text = text
            if is_final:
                latency_ms = int(payload.get("latency_ms") or 0)
                break
        return final_text or partial_text, latency_ms


def overlap_ratio(expected: str, actual: str) -> float:
    expected_chars = set(re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", expected))
    actual_chars = set(re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", actual))
    return len(expected_chars & actual_chars) / max(1, len(expected_chars))


def sequence_ratio(expected: str, actual: str) -> float:
    def normalize(value: str) -> str:
        return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", value)

    return difflib.SequenceMatcher(None, normalize(expected), normalize(actual), autojunk=False).ratio()


async def main_async(voices: list[str], text: str) -> None:
    room_code = "tts-asr-roundtrip"
    try:

        async def verify(voice: str) -> tuple[str, str, float, int]:
            await lighttts.synthesize(text, room_code=room_code, speech_id=voice, voice=voice)
            path = settings.media_path / room_code / f"{voice}.wav"
            transcript, latency_ms = await transcribe(path, name=f"roundtrip-{voice}")
            ratio = overlap_ratio(text, transcript)
            sequence = sequence_ratio(text, transcript)
            if len(transcript) < 4 or ratio < 0.35 or sequence < 0.9:
                raise RuntimeError(f"{voice} round-trip mismatch: transcript={transcript!r} overlap={ratio:.2f} sequence={sequence:.2f}")
            return voice, transcript, ratio, sequence, latency_ms

        results = await asyncio.gather(*(verify(voice) for voice in voices))
        compact = [
            {
                "voice": voice,
                "transcript": transcript,
                "overlap": round(ratio, 2),
                "sequence": round(sequence, 2),
                "asr_latency_ms": latency_ms,
            }
            for voice, transcript, ratio, sequence, latency_ms in results
        ]
        print("tts_asr_roundtrip_verified " + json.dumps(compact, ensure_ascii=False))
    finally:
        shutil.rmtree(settings.media_path / room_code, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--voices", default="debate_voice_1,debate_voice_2,debate_voice_3,debate_voice_4")
    parser.add_argument("--text", default="人工智能时代仍然需要学习编程。")
    args = parser.parse_args()
    voices = [item.strip() for item in args.voices.split(",") if item.strip()]
    if not voices:
        raise SystemExit("--voices must contain at least one voice")
    asyncio.run(main_async(voices, args.text))


if __name__ == "__main__":
    main()
