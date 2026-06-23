from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from app.services.sqlite_repo import project_root


_SECTION_RE = re.compile(r"^【([^】]+)】\s*$")
_FREE_TURN_RE = re.compile(r"^【([^】]+)】：\s*(.+?)\s*$")


@dataclass(frozen=True)
class FallbackSection:
    title: str
    text: str


@dataclass(frozen=True)
class FallbackTurn:
    index: int
    speaker_label: str
    text: str


@dataclass(frozen=True)
class FallbackPlan:
    path: str
    sections: Dict[str, FallbackSection]
    free_turns: List[FallbackTurn]


def fallback_history_candidates() -> List[Path]:
    raw = os.getenv("PHDEBATE_FALLBACK_HISTORY_FILE", "").strip()
    candidates: List[Path] = []
    if raw:
        candidates.append(Path(raw).expanduser())
    root = project_root()
    candidates.extend(
        [
            root / "完整兜底历史.md",
            root.parent / "完整兜底历史.md",
            root / "docs" / "完整兜底历史.md",
            Path.cwd() / "完整兜底历史.md",
        ]
    )
    unique: List[Path] = []
    seen = set()
    for path in candidates:
        resolved = str(path)
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(path)
    return unique


def load_fallback_plan() -> FallbackPlan:
    for path in fallback_history_candidates():
        if path.is_file():
            return parse_fallback_history(path.read_text(encoding="utf-8"), path=path)
    expected = ", ".join(str(item) for item in fallback_history_candidates())
    raise FileNotFoundError(f"未找到完整兜底历史.md；已检查：{expected}")


def parse_fallback_history(content: str, *, path: Path | str = "") -> FallbackPlan:
    sections: Dict[str, FallbackSection] = {}
    current_title = ""
    buffer: List[str] = []

    def flush() -> None:
        nonlocal buffer, current_title
        if not current_title:
            buffer = []
            return
        text = "\n".join(buffer).strip()
        sections[current_title] = FallbackSection(title=current_title, text=text)
        buffer = []

    for raw_line in content.splitlines():
        line = raw_line.rstrip()
        match = _SECTION_RE.match(line.strip())
        if match:
            flush()
            current_title = match.group(1).strip()
            continue
        buffer.append(line)
    flush()

    free_text = sections.get("自由辩论", FallbackSection("自由辩论", "")).text
    free_turns: List[FallbackTurn] = []
    for line in free_text.splitlines():
        match = _FREE_TURN_RE.match(line.strip())
        if not match:
            continue
        free_turns.append(
            FallbackTurn(
                index=len(free_turns) + 1,
                speaker_label=match.group(1).strip(),
                text=match.group(2).strip(),
            )
        )
    return FallbackPlan(path=str(path), sections=sections, free_turns=free_turns)


def section_title_for_phase(phase: Dict[str, Any]) -> str:
    name = str(phase.get("name") or "").strip()
    if name in {"反方四辩总结", "反方四辩结辩"}:
        return "反方四辩结辩"
    if name in {"正方四辩总结", "正方四辩结辩"}:
        return "正方四辩结辩"
    return name


def text_for_phase(plan: FallbackPlan, phase: Dict[str, Any]) -> str:
    title = section_title_for_phase(phase)
    section = plan.sections.get(title)
    return section.text if section else ""


def label_for_speaker(speaker: Dict[str, Any]) -> str:
    side = "正方" if speaker.get("side") == "affirmative" else "反方" if speaker.get("side") == "negative" else ""
    seat_map = {1: "一辩", 2: "二辩", 3: "三辩", 4: "四辩"}
    try:
        seat = int(speaker.get("seat") or 0)
    except (TypeError, ValueError):
        seat = 0
    return f"{side}{seat_map.get(seat, '')}".strip()


def speaker_for_phase(phase: Dict[str, Any], speakers: Iterable[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if phase.get("phase_type") == "free_debate":
        return None
    for speaker in speakers:
        if speaker.get("side") == phase.get("side") and speaker.get("seat") == phase.get("speaker_seat"):
            return speaker
    return None


def speaker_for_label(label: str, speakers: Iterable[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    normalized = str(label or "").strip()
    for speaker in speakers:
        if label_for_speaker(speaker) == normalized:
            return speaker
    return None

