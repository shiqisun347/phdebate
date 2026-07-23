from __future__ import annotations

import re
from typing import Any

_STRONG_BOUNDARIES = frozenset("。！？!?；;\n")
_WEAK_BOUNDARIES = frozenset("，,、：:")
_WORD_CHARACTER = re.compile(r"[A-Za-z0-9_.+\-/]")
_AGENT_NON_BODY_EVENT_TYPES = frozenset(
    {
        "analysis",
        "analysis_delta",
        "reasoning",
        "reasoning_delta",
        "think",
        "thinking",
        "thinking_delta",
    }
)


def _is_agent_non_body_event(event_type: str) -> bool:
    normalized = event_type.strip().lower().replace("-", "_").replace(".", "_")
    if normalized in _AGENT_NON_BODY_EVENT_TYPES:
        return True
    return any(
        token in {"analysis", "reasoning", "think", "thinking"}
        for token in normalized.split("_")
    )


class SpeakableClauseAssembler:
    """Turn visible Agent deltas into stable, progressively larger clauses."""

    def __init__(
        self,
        *,
        minimum_characters: int = 10,
        maximum_characters: int = 16,
        growth_characters: int = 6,
        maximum_growth_characters: int = 28,
        first_flush_minimum_characters: int = 0,
    ) -> None:
        if minimum_characters < 1 or maximum_characters < minimum_characters:
            raise ValueError("invalid speakable clause bounds")
        if growth_characters < 0 or maximum_growth_characters < maximum_characters:
            raise ValueError("invalid speakable clause growth")
        if first_flush_minimum_characters < 0 or first_flush_minimum_characters > maximum_growth_characters:
            raise ValueError("invalid first flush minimum")
        self.minimum_characters = minimum_characters
        self.maximum_characters = maximum_characters
        self.growth_characters = growth_characters
        self.maximum_growth_characters = maximum_growth_characters
        self.first_flush_minimum_characters = first_flush_minimum_characters
        self._buffer = ""
        self._emitted_chunks = 0

    @property
    def buffered_text(self) -> str:
        return self._buffer

    @property
    def current_minimum_characters(self) -> int:
        return min(
            self.maximum_growth_characters,
            self.minimum_characters + self._emitted_chunks * self.growth_characters,
        )

    @property
    def current_maximum_characters(self) -> int:
        return min(
            self.maximum_growth_characters,
            self.maximum_characters + self._emitted_chunks * self.growth_characters,
        )

    def feed(self, delta: str) -> list[str]:
        if delta:
            self._buffer += delta
        emitted: list[str] = []
        while True:
            boundary = self._next_boundary()
            if boundary is None:
                break
            chunk = self._buffer[:boundary].strip()
            self._buffer = self._buffer[boundary:].lstrip()
            if chunk:
                emitted.append(chunk)
                self._emitted_chunks += 1
        return emitted

    def finish(self) -> list[str]:
        remainder = self._buffer.strip()
        self._buffer = ""
        return [remainder] if remainder else []

    def replace_buffer(self, text: str) -> None:
        """Replace only text that has not yet been handed to synthesis."""

        self._buffer = text.lstrip()

    def flush_soft(self) -> list[str]:
        if len(self._buffer) < self.current_minimum_characters:
            return []
        return self._flush_at_boundary(
            self._soft_boundary(min(self.current_maximum_characters, len(self._buffer)))
        )

    def flush_deadline(self) -> list[str]:
        """Flush the best available phrase at the hard latency deadline."""

        if not self._buffer:
            return []
        # MOSS requires enough text tokens before its first prefill can emit
        # audio. Starting a turn with one or two characters cannot produce
        # PCM yet; timing it out then aborts active CUDA work and may quarantine
        # the only realtime endpoint. Keep only the first undersized phrase
        # buffered. ``finish`` still handles genuinely short complete answers,
        # while later phrases retain the normal low-latency deadline.
        if (
            self._emitted_chunks == 0
            and self.first_flush_minimum_characters
            and len(self._buffer) < self.first_flush_minimum_characters
        ):
            return []
        maximum = min(self.current_maximum_characters, len(self._buffer))
        boundary = self._soft_boundary(maximum)
        if boundary is None:
            # Bound a stalled short Mandarin prefix and the rare long ASCII
            # token for which no clean boundary exists inside the window.
            boundary = maximum
        return self._flush_at_boundary(boundary)

    def _flush_at_boundary(self, boundary: int | None) -> list[str]:
        if boundary is None:
            return []
        chunk = self._buffer[:boundary].strip()
        self._buffer = self._buffer[boundary:].lstrip()
        if not chunk:
            return []
        self._emitted_chunks += 1
        return [chunk]

    def _next_boundary(self) -> int | None:
        if not self._buffer:
            return None
        minimum = self.current_minimum_characters
        maximum = self.current_maximum_characters
        # A complete sentence is stable even when it is shorter than the
        # normal first-clause floor.  Commas still respect the floor so a
        # stream of tiny fragments cannot make the voice sound staccato.
        for index, character in enumerate(self._buffer, start=1):
            if (
                character in _STRONG_BOUNDARIES
                and index <= maximum
                and not (
                    self._emitted_chunks == 0
                    and self.first_flush_minimum_characters
                    and index < self.first_flush_minimum_characters
                )
            ):
                return index
        for index, character in enumerate(self._buffer, start=1):
            if minimum <= index <= maximum and character in _WEAK_BOUNDARIES:
                return index
        if len(self._buffer) < maximum:
            return None
        return self._soft_boundary(min(maximum, len(self._buffer)))

    def _soft_boundary(self, maximum: int) -> int | None:
        minimum = min(self.current_minimum_characters, len(self._buffer))
        for index in range(maximum, minimum - 1, -1):
            if self._buffer[index - 1] in _WEAK_BOUNDARIES:
                return index
        cut = maximum
        while 0 < cut < len(self._buffer) and self._inside_word(cut):
            cut -= 1
        return cut if cut >= minimum else None

    def _inside_word(self, index: int) -> bool:
        if index <= 0 or index >= len(self._buffer):
            return False
        return bool(
            _WORD_CHARACTER.fullmatch(self._buffer[index - 1])
            and _WORD_CHARACTER.fullmatch(self._buffer[index])
        )


def extract_text(data: Any) -> str:
    if isinstance(data, str):
        return data
    if isinstance(data, list):
        return "".join(extract_text(item) for item in data)
    if not isinstance(data, dict):
        return ""
    for key in ("delta", "content", "output", "text", "message", "answer", "response"):
        value = data.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, (dict, list)):
            result = extract_text(value)
            if result:
                return result
    choices = data.get("choices")
    if isinstance(choices, list):
        return "".join(extract_text(choice) for choice in choices)
    return ""


def extract_agent_body_delta(data: Any) -> str:
    """Extract only user-visible answer deltas, never reasoning/thinking."""

    if not isinstance(data, dict):
        return ""
    event_type = str(data.get("type") or "").strip().lower()
    if _is_agent_non_body_event(event_type) or event_type == "final":
        return ""
    delta = data.get("delta")
    if isinstance(delta, str):
        return delta
    if isinstance(delta, dict):
        content = delta.get("content")
        return content if isinstance(content, str) else ""
    choices = data.get("choices")
    if isinstance(choices, list):
        return "".join(extract_agent_body_delta(choice) for choice in choices)
    message = data.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        return content if isinstance(content, str) else ""
    if event_type in {"content", "content_delta", "message", "token"}:
        content = data.get("content") or data.get("text")
        return content if isinstance(content, str) else ""
    return ""


def extract_agent_final(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    content = data.get("content")
    if isinstance(content, str):
        return content
    message = data.get("message")
    if isinstance(message, dict) and isinstance(message.get("content"), str):
        return str(message["content"])
    return extract_text(data)
