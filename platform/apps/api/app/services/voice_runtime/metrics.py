from __future__ import annotations

from dataclasses import dataclass, field
from time import monotonic


@dataclass
class VoiceTurnMetrics:
    room_code: str
    speech_id: str
    started_monotonic: float = field(default_factory=monotonic)
    first_text_monotonic: float | None = None
    first_pcm_monotonic: float | None = None
    completed_monotonic: float | None = None
    text_chunks: int = 0
    pcm_bytes: int = 0
    errors: list[str] = field(default_factory=list)

    def mark_text(self) -> None:
        self.text_chunks += 1
        self.first_text_monotonic = self.first_text_monotonic or monotonic()

    def mark_pcm(self, size: int) -> None:
        self.pcm_bytes += max(0, size)
        self.first_pcm_monotonic = self.first_pcm_monotonic or monotonic()

    def finish(self) -> None:
        self.completed_monotonic = monotonic()

    @property
    def first_pcm_seconds(self) -> float | None:
        if self.first_pcm_monotonic is None:
            return None
        return self.first_pcm_monotonic - self.started_monotonic
