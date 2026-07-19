from __future__ import annotations

import math
import re
import unicodedata
from array import array
from dataclasses import dataclass

MIN_ASR_CONFIDENCE = 0.35
MIN_ASR_SPEECH_CHARACTERS = 2
MIN_VOICED_SAMPLES = 4_000
MIN_ASR_VOICED_SAMPLES = 8_000
VOICE_RMS_THRESHOLD = 0.01
VOICE_PEAK_THRESHOLD = 0.03

_VISIBLE_CONTENT = re.compile(r"[A-Za-z0-9\u3400-\u9fff]")
_WHITESPACE = re.compile(r"\s+")


def normalize_transcript(text: str) -> str:
    return _WHITESPACE.sub(" ", unicodedata.normalize("NFKC", text)).strip()


def transcript_rejection_reason(
    text: str,
    *,
    confidence: float | None = None,
    require_substantive: bool = False,
) -> str | None:
    normalized = normalize_transcript(text)
    if not normalized:
        return "empty"
    if confidence is not None and confidence < MIN_ASR_CONFIDENCE:
        return "low_confidence"
    if not _VISIBLE_CONTENT.search(normalized):
        return "no_speech_characters"
    if any(unicodedata.category(character) in {"Cc", "Cs"} for character in normalized):
        return "control_characters"
    visible = [character for character in normalized if not character.isspace()]
    speech_characters = sum(bool(_VISIBLE_CONTENT.fullmatch(character)) for character in visible)
    if require_substantive and speech_characters < MIN_ASR_SPEECH_CHARACTERS:
        return "too_short"
    if visible and speech_characters / len(visible) < 0.5:
        return "excessive_symbols"
    if len(visible) >= 8 and len(set(visible)) <= 2:
        return "repeated_characters"
    return None


def usable_transcript(
    text: str,
    *,
    confidence: float | None = None,
    require_substantive: bool = False,
) -> bool:
    return transcript_rejection_reason(
        text,
        confidence=confidence,
        require_substantive=require_substantive,
    ) is None


def payload_confidence(payload: dict) -> float | None:
    values: list[float] = []

    def add(value: object) -> None:
        if isinstance(value, bool):
            return
        try:
            number = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return
        if 1 < number <= 100:
            number /= 100
        if math.isfinite(number) and 0 <= number <= 1:
            values.append(number)

    for key in ("confidence", "conf"):
        add(payload.get(key))
    sentences = payload.get("sentences")
    if isinstance(sentences, list):
        for sentence in sentences:
            if isinstance(sentence, dict):
                for key in ("confidence", "conf"):
                    add(sentence.get(key))
    return sum(values) / len(values) if values else None


@dataclass
class PcmVoiceActivity:
    minimum_voiced_samples: int = MIN_VOICED_SAMPLES
    voiced_samples: int = 0
    total_samples: int = 0
    peak: float = 0

    def observe(self, pcm: bytes) -> None:
        usable_length = len(pcm) - (len(pcm) % 2)
        if usable_length <= 0:
            return
        samples = array("h")
        samples.frombytes(pcm[:usable_length])
        if not samples:
            return
        self.total_samples += len(samples)
        mean = sum(samples) / len(samples)
        centered = [value - mean for value in samples]
        peak_sample = max(abs(value) for value in centered)
        rms = math.sqrt(sum(value * value for value in centered) / len(centered)) / 32768
        peak = peak_sample / 32768
        self.peak = max(self.peak, peak)
        if rms >= VOICE_RMS_THRESHOLD and peak >= VOICE_PEAK_THRESHOLD:
            self.voiced_samples += len(samples)

    @property
    def has_voice(self) -> bool:
        return self.voiced_samples >= self.minimum_voiced_samples
