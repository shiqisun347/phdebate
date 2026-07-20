from __future__ import annotations

import re
from pathlib import Path

from app.core.config import settings

VOICE_IDS = tuple(f"debate_voice_{index}" for index in range(1, 9))
SEAT_VOICE_IDS = {
    **{f"aff_{index}": f"debate_voice_{index}" for index in range(1, 5)},
    **{f"neg_{index}": f"debate_voice_{index + 4}" for index in range(1, 5)},
}

# ``debate_voice_5`` is temporarily quarantined. Its production prompt and
# repeated real MOSS generations contain substantially more high-frequency
# discontinuities than the other fixed voices, which listeners perceive as
# intermittent crackle/tearing. Reuse the already validated female voice 8 for
# the negative first speaker until a clean, human-approved replacement prompt
# completes the full audio release gate. Keeping the quarantine here avoids
# changing the MOSS session, LiveKit publisher, Opus settings, or browser
# playback path.
SEAT_VOICE_IDS["neg_1"] = "debate_voice_8"


def voice_for_seat(seat_key: str, *, fallback: str = "debate_voice_1") -> str:
    return SEAT_VOICE_IDS.get(seat_key, fallback)


def validate_voice_id(voice_id: str) -> str:
    normalized = voice_id.strip()
    if normalized not in VOICE_IDS:
        raise ValueError("voice_id must be one of debate_voice_1 ... debate_voice_8")
    return normalized


def prompt_path(voice_id: str) -> Path:
    configured = Path(settings.lighttts_prompt_wav_path)
    safe_voice = voice_id if re.fullmatch(r"[A-Za-z0-9_-]{1,100}", voice_id) else ""
    candidate = configured.parent / f"{safe_voice}.wav" if safe_voice else configured
    return candidate if candidate.exists() else configured


def prompt_text(voice_id: str) -> str:
    return settings.lighttts_prompt_texts.get(voice_id, settings.lighttts_prompt_text)
