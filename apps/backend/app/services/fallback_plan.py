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
    topic_key: str = "programming"
    topic_label: str = "AI 时代，我们更应该培养编程思维 / 提问思维"


TOPIC_FALLBACK_FILES = {
    "programming": {
        "label": "AI 时代，我们更应该培养编程思维 / 提问思维",
        "filename": "完整兜底历史.md",
        "aliases": ("编程", "提问思维", "学编程", "编程思维"),
    },
    "ai_copyright": {
        "label": "AI生成内容应该不应该享有版权保护?",
        "filename": "完整兜底历史_AI版权保护.md",
        "aliases": ("AI生成内容应该不应该享有版权保护", "AI生成内容", "版权保护", "版权"),
    },
    "ai_persona": {
        "label": "给AI赋予人格设定是好事还是坏事?",
        "filename": "完整兜底历史_AI人格设定.md",
        "aliases": ("给AI赋予人格设定是好事还是坏事", "人格设定", "AI人格", "赋予人格"),
    },
}


def normalize_topic(value: str) -> str:
    return re.sub(r"[\s\W_]+", "", str(value or "").lower(), flags=re.UNICODE)


def fallback_topic_key(topic: str) -> str:
    normalized = normalize_topic(topic)
    if not normalized:
        return "programming"
    for key, meta in TOPIC_FALLBACK_FILES.items():
        for alias in meta["aliases"]:
            if normalize_topic(alias) in normalized:
                return key
    return "programming"


def fallback_topic_label(topic: str) -> str:
    key = fallback_topic_key(topic)
    return str(TOPIC_FALLBACK_FILES.get(key, TOPIC_FALLBACK_FILES["programming"])["label"])


def fallback_history_candidates(topic: str = "") -> List[Path]:
    raw = os.getenv("PHDEBATE_FALLBACK_HISTORY_FILE", "").strip()
    candidates: List[Path] = []
    if raw:
        candidates.append(Path(raw).expanduser())
    root = project_root()
    key = fallback_topic_key(topic)
    filename = str(TOPIC_FALLBACK_FILES.get(key, TOPIC_FALLBACK_FILES["programming"])["filename"])
    topic_paths = [
        root / filename,
        root.parent / filename,
        root / "docs" / filename,
        Path.cwd() / filename,
    ]
    if filename != "完整兜底历史.md":
        candidates.extend(topic_paths)
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


def load_fallback_plan(topic: str = "") -> FallbackPlan:
    key = fallback_topic_key(topic)
    label = fallback_topic_label(topic)
    for path in fallback_history_candidates(topic):
        if path.is_file():
            return parse_fallback_history(path.read_text(encoding="utf-8"), path=path, topic_key=key, topic_label=label)
    expected = ", ".join(str(item) for item in fallback_history_candidates(topic))
    raise FileNotFoundError(f"未找到完整兜底历史.md；已检查：{expected}")


def parse_fallback_history(content: str, *, path: Path | str = "", topic_key: str = "programming", topic_label: str = "") -> FallbackPlan:
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
    return FallbackPlan(
        path=str(path),
        sections=sections,
        free_turns=free_turns,
        topic_key=topic_key,
        topic_label=topic_label or str(TOPIC_FALLBACK_FILES.get(topic_key, TOPIC_FALLBACK_FILES["programming"])["label"]),
    )


def section_title_for_phase(phase: Dict[str, Any]) -> str:
    return section_title_candidates_for_phase(phase)[0]


def section_title_candidates_for_phase(phase: Dict[str, Any]) -> List[str]:
    name = str(phase.get("name") or "").strip()
    candidates = [name] if name else []
    label_match = re.search(r"(正方|反方)[一二三四]辩", name)
    label = label_match.group(0) if label_match else ""
    if label:
        if "开篇立论" in name or "立论" in name:
            candidates.append(f"{label}立论")
        if "驳论" in name or "陈词" in name:
            candidates.append(f"{label}陈词")
        if "总结" in name or "结辩" in name:
            candidates.append(f"{label}结辩")
    candidates.extend(
        [
            name.replace("开篇立论", "立论"),
            name.replace("驳论", "陈词"),
            name.replace("总结陈词", "结辩"),
            name.replace("总结", "结辩"),
        ]
    )
    if name in {"反方四辩总结", "反方四辩总结陈词", "反方四辩结辩"}:
        candidates.append("反方四辩结辩")
    if name in {"正方四辩总结", "正方四辩总结陈词", "正方四辩结辩"}:
        candidates.append("正方四辩结辩")
    unique: List[str] = []
    seen = set()
    for candidate in candidates:
        value = str(candidate or "").strip()
        if value and value not in seen:
            seen.add(value)
            unique.append(value)
    return unique or [name]


def text_for_phase(plan: FallbackPlan, phase: Dict[str, Any]) -> str:
    for title in section_title_candidates_for_phase(phase):
        section = plan.sections.get(title)
        if section:
            return section.text
    return ""


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
