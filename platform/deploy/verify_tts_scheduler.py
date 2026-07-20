"""Production-safe LightTTS scheduler verifier using only an in-process mock transport."""

from __future__ import annotations

import asyncio
import io
import shutil
import tempfile
import wave
from pathlib import Path

import httpx
from app.core.config import settings
from app.services.providers import LightTTSProvider, ProviderCancelled


def wav_bytes(seconds: float = 1.0) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(b"\x00\x00" * int(24000 * seconds))
    return buffer.getvalue()


def run() -> dict:
    with tempfile.TemporaryDirectory(prefix="jixia-tts-verify-") as temporary_root:
        root = Path(temporary_root)
        prompt = root / "debate_voice_1.wav"
        prompt.write_bytes(b"production-safe-mock-prompt")
        settings.lighttts_prompt_wav_path = str(prompt)
        settings.media_root = str(root / "media")

        async def scenario() -> dict:
            first_started = asyncio.Event()
            release_first = asyncio.Event()
            request_calls = 0

            async def blocking_handler(_request: httpx.Request) -> httpx.Response:
                nonlocal request_calls
                request_calls += 1
                first_started.set()
                await release_first.wait()
                return httpx.Response(200, content=wav_bytes())

            provider = LightTTSProvider(httpx.MockTransport(blocking_handler))
            provider._semaphore = asyncio.Semaphore(1)
            active = asyncio.create_task(provider.synthesize("占用语音资源。", room_code="active", speech_id="active"))
            await asyncio.wait_for(first_started.wait(), timeout=1)
            cancelled = False
            waiting = asyncio.create_task(
                provider.synthesize(
                    "排队后取消。",
                    room_code="cancelled",
                    speech_id="cancelled",
                    should_cancel=lambda: cancelled,
                )
            )
            await asyncio.sleep(0.1)
            cancelled = True
            try:
                await asyncio.wait_for(waiting, timeout=0.8)
                raise AssertionError("cancelled waiter unexpectedly completed")
            except ProviderCancelled:
                pass
            assert request_calls == 1
            release_first.set()
            await active

            cached = settings.media_path / "cached" / "speech.wav"
            cached.parent.mkdir(parents=True)
            cached.write_bytes(wav_bytes())
            provider._semaphore = asyncio.Semaphore(0)
            cached_result = await asyncio.wait_for(
                provider.synthesize("复用已完成音频。", room_code="cached", speech_id="speech"),
                timeout=0.2,
            )
            assert cached_result == "/media/cached/speech.wav"

            release = asyncio.Event()
            first_background_started = asyncio.Event()
            two_started = asyncio.Event()
            calls: list[str] = []

            async def priority_handler(request: httpx.Request) -> httpx.Response:
                body = await request.aread()
                label = "live" if "实时辩手".encode() in body else "background"
                calls.append(label)
                first_background_started.set()
                if len(calls) >= 2:
                    two_started.set()
                await release.wait()
                return httpx.Response(200, content=wav_bytes())

            priority = LightTTSProvider(httpx.MockTransport(priority_handler))
            priority._semaphore = asyncio.Semaphore(2)
            priority._background_semaphore = asyncio.Semaphore(1)
            first_background = asyncio.create_task(
                priority.synthesize("后台预生成甲。", room_code="cue-a", speech_id="cue-a", background=True)
            )
            await asyncio.wait_for(first_background_started.wait(), timeout=1)
            second_background = asyncio.create_task(
                priority.synthesize("后台预生成乙。", room_code="cue-b", speech_id="cue-b", background=True)
            )
            live = asyncio.create_task(priority.synthesize("实时辩手发言。", room_code="live", speech_id="live"))
            try:
                await asyncio.wait_for(two_started.wait(), timeout=1)
                assert calls == ["background", "live"]
            finally:
                release.set()
                await asyncio.gather(first_background, second_background, live, return_exceptions=True)
            assert calls == ["background", "live", "background"]

            directory_started = asyncio.Event()
            release_directory = asyncio.Event()

            async def directory_handler(_request: httpx.Request) -> httpx.Response:
                directory_started.set()
                await release_directory.wait()
                return httpx.Response(200, content=wav_bytes())

            directory_provider = LightTTSProvider(httpx.MockTransport(directory_handler))
            directory_task = asyncio.create_task(directory_provider.synthesize("目录删除后重建。", room_code="removed", speech_id="speech"))
            await asyncio.wait_for(directory_started.wait(), timeout=1)
            shutil.rmtree(settings.media_path / "removed")
            release_directory.set()
            await asyncio.wait_for(directory_task, timeout=1)
            assert (settings.media_path / "removed" / "speech.wav").is_file()
            return {
                "queued_cancellation": True,
                "cache_bypass": True,
                "background_order": calls,
                "directory_recovery": True,
            }

        result = asyncio.run(scenario())

        async def cross_loop(label: str) -> str:
            async def handler(_request: httpx.Request) -> httpx.Response:
                return httpx.Response(200, content=wav_bytes())

            shared.transport = httpx.MockTransport(handler)
            return await shared.synthesize("跨循环调度。", room_code=label, speech_id=label)

        shared = LightTTSProvider()
        first_loop = asyncio.run(cross_loop("loop-one"))
        second_loop = asyncio.run(cross_loop("loop-two"))
        assert first_loop.endswith("/loop-one.wav") and second_loop.endswith("/loop-two.wav")
        result["cross_loop_reinitialization"] = True
        result["external_lighttts_called"] = False
        result["ok"] = True
        return result


if __name__ == "__main__":
    print(run())
