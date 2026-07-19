#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import wave

from app.core.config import settings
from app.services.providers import lighttts


async def run(voice: str, text: str, keep: bool) -> None:
    room_code = "voicecheck"
    speech_id = voice
    prompt = lighttts.prompt_path(voice)
    url = await lighttts.synthesize(text, room_code=room_code, speech_id=speech_id, voice=voice)
    path = settings.media_path / room_code / f"{speech_id}.wav"
    with wave.open(str(path), "rb") as audio:
        duration = audio.getnframes() / max(1, audio.getframerate())
    print(
        json.dumps(
            {
                "voice": voice,
                "prompt": prompt.name,
                "url": url,
                "bytes": path.stat().st_size,
                "duration_seconds": round(duration, 3),
                "riff": path.read_bytes()[:4] == b"RIFF",
            },
            ensure_ascii=False,
        )
    )
    if not keep:
        path.unlink(missing_ok=True)
        try:
            path.parent.rmdir()
        except OSError:
            pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--voice", default="debate_voice_2")
    parser.add_argument("--text", default="这是 LightTTS 多音色系统测试。")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.voice, args.text, args.keep))


if __name__ == "__main__":
    main()
