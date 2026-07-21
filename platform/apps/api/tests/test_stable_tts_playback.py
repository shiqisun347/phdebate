from __future__ import annotations

import math
import struct
import wave

import pytest
from app.core.config import settings
from app.services.match_engine import _prepare_stable_playback_asset
from app.services.voice_runtime.archive import wav_duration


@pytest.mark.asyncio
async def test_stable_playback_keeps_source_and_creates_pitch_preserving_1_1x_asset(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "media_root", str(tmp_path))
    monkeypatch.setattr(settings, "ffmpeg_binary", "ffmpeg")
    room_code = "123456"
    speech_id = "speech-stable"
    room_dir = tmp_path / room_code
    room_dir.mkdir()
    target = room_dir / f"{speech_id}.wav"
    sample_rate = 24_000
    with wave.open(str(target), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(
            b"".join(
                struct.pack("<h", round(4_000 * math.sin(2 * math.pi * 220 * index / sample_rate)))
                for index in range(sample_rate * 2)
            )
        )

    url = await _prepare_stable_playback_asset(room_code, speech_id, 1.1)

    source = room_dir / f"{speech_id}.source.wav"
    assert url == f"/media/{room_code}/{speech_id}.wav"
    assert source.is_file() and target.is_file()
    assert wav_duration(source) == pytest.approx(2.0, abs=0.01)
    assert wav_duration(target) == pytest.approx(2.0 / 1.1, abs=0.03)
    with wave.open(str(target), "rb") as playback:
        assert playback.getnchannels() == 1
        assert playback.getsampwidth() == 2
        assert playback.getframerate() == sample_rate


@pytest.mark.asyncio
async def test_stable_playback_at_normal_speed_still_keeps_original(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "media_root", str(tmp_path))
    room_code = "654321"
    speech_id = "speech-normal"
    room_dir = tmp_path / room_code
    room_dir.mkdir()
    target = room_dir / f"{speech_id}.wav"
    with wave.open(str(target), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24_000)
        audio.writeframes(b"\0\0" * 2_400)

    await _prepare_stable_playback_asset(room_code, speech_id, 1.0)

    assert target.read_bytes() == (room_dir / f"{speech_id}.source.wav").read_bytes()
