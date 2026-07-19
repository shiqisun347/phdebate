#!/usr/bin/env python3
"""Verify deployed LightTTS queue bounds, cancellation, and atomic output files."""

from __future__ import annotations

import argparse
import asyncio
import io
import os
import shutil
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from secrets import token_hex

from app.core.config import settings
from app.services.providers import LightTTSProvider, ProviderCancelled


def wav_bytes(seconds: float = 1) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(b"\x00\x00" * int(24000 * seconds))
    return buffer.getvalue()


class SchedulerHandler(BaseHTTPRequestHandler):
    lock = threading.Lock()
    release = threading.Event()
    capacity_started = threading.Event()
    expected_capacity = 1
    active = 0
    max_active = 0
    calls = 0

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length", "0"))
        self.rfile.read(length)
        with self.lock:
            type(self).active += 1
            type(self).calls += 1
            type(self).max_active = max(type(self).max_active, type(self).active)
            if type(self).calls >= type(self).expected_capacity:
                type(self).capacity_started.set()
        type(self).release.wait(timeout=15)
        content = wav_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)
        with self.lock:
            type(self).active -= 1

    def log_message(self, _format: str, *_args) -> None:
        return None


async def main_async() -> None:
    suffix = f"tts-scheduler-{int(time.time())}-{token_hex(3)}"
    prompt = settings.media_path / f".{suffix}-prompt.wav"
    prompt.write_bytes(wav_bytes(0.5))
    original_prompt = settings.lighttts_prompt_wav_path
    settings.lighttts_prompt_wav_path = str(prompt)
    server = ThreadingHTTPServer(("127.0.0.1", 0), SchedulerHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    room_codes = [f"{suffix}-{index}" for index in range(6)]
    try:
        configured_capacity = settings.lighttts_max_active
        assert configured_capacity == 1, "deployed LightTTS process only supports one active request"
        with SchedulerHandler.lock:
            SchedulerHandler.active = 0
            SchedulerHandler.max_active = 0
            SchedulerHandler.calls = 0
            SchedulerHandler.expected_capacity = configured_capacity
        SchedulerHandler.release.clear()
        SchedulerHandler.capacity_started.clear()
        provider = LightTTSProvider()
        cancelled = [False] * 6
        endpoint = f"http://127.0.0.1:{server.server_port}/inference"
        tasks = [
            asyncio.create_task(
                provider.synthesize(
                    f"第 {index + 1} 个房间的语音调度验收文本。",
                    room_code=room_codes[index],
                    speech_id=f"speech-{index}",
                    provider_config={
                        "endpoint": endpoint,
                        "enabled": True,
                        "settings": {"read_timeout_seconds": 30, "speed": 1},
                    },
                    should_cancel=lambda index=index: cancelled[index],
                )
            )
            for index in range(6)
        ]
        assert await asyncio.to_thread(SchedulerHandler.capacity_started.wait, 5)
        cancelled[0] = True
        cancelled[4] = True
        await asyncio.sleep(0.4)
        with SchedulerHandler.lock:
            assert SchedulerHandler.calls == configured_capacity
            assert SchedulerHandler.max_active == configured_capacity
        assert not tasks[0].done()
        SchedulerHandler.release.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=15)
        assert isinstance(results[0], ProviderCancelled) and isinstance(results[4], ProviderCancelled)
        assert all(isinstance(results[index], str) for index in (1, 2, 3, 5))
        with SchedulerHandler.lock:
            calls = SchedulerHandler.calls
            max_active = SchedulerHandler.max_active
        assert calls == 5 and max_active == configured_capacity
        for index, room_code in enumerate(room_codes):
            target_dir = settings.media_path / room_code
            assert (target_dir / f"speech-{index}.wav").exists() is (index in {1, 2, 3, 5})
            assert list(target_dir.glob("*.part")) == []
        print(
            "lighttts_scheduler_verified "
            f"rooms=6 requests=5 completed=4 active_cancelled=1 waiting_cancelled=1 "
            f"max_active={configured_capacity} partial_files=0"
        )
    finally:
        SchedulerHandler.release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        settings.lighttts_prompt_wav_path = original_prompt
        prompt.unlink(missing_ok=True)
        for room_code in room_codes:
            shutil.rmtree(settings.media_path / room_code, ignore_errors=True)


def main() -> None:
    if os.geteuid() == 0:
        raise SystemExit("请使用 V2 服务账号运行 LightTTS 调度验收。")
    parser = argparse.ArgumentParser()
    parser.parse_args()
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
