from __future__ import annotations

import wave
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WavMetadata:
    sample_rate: int
    channels: int
    sample_width: int
    frames: int

    @property
    def duration_seconds(self) -> float:
        return self.frames / max(1, self.sample_rate)


def inspect_wav(path: Path) -> WavMetadata:
    if not path.is_file() or path.is_symlink():
        raise FileNotFoundError(path)
    with wave.open(str(path), "rb") as audio:
        return WavMetadata(
            sample_rate=audio.getframerate(),
            channels=audio.getnchannels(),
            sample_width=audio.getsampwidth(),
            frames=audio.getnframes(),
        )


def wav_duration(path: Path) -> float:
    try:
        return inspect_wav(path).duration_seconds
    except (FileNotFoundError, OSError, wave.Error):
        return 0.0


def remove_partial_archives(directory: Path, speech_id: str) -> int:
    removed = 0
    for candidate in directory.glob(f".{speech_id}.*.wav.part"):
        if candidate.is_file() and not candidate.is_symlink():
            candidate.unlink(missing_ok=True)
            removed += 1
    return removed
