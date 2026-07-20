from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "prepare_aishell3_voice_candidates.py"
SPEC = importlib.util.spec_from_file_location("prepare_aishell3_voice_candidates", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_parse_content_reconstructs_hanzi_transcript() -> None:
    records = MODULE.parse_content("SSB00010001.wav\t你 ni3 好 hao3 世 shi4 界 jie4\n", "train")

    assert records["SSB00010001.wav"] == {"transcript": "你好世界", "split": "train"}


def test_choose_combination_stays_in_prompt_duration_window() -> None:
    items = [
        {"duration_seconds": 3.0, "filename": "a.wav"},
        {"duration_seconds": 3.5, "filename": "b.wav"},
        {"duration_seconds": 4.0, "filename": "c.wav"},
    ]

    chosen = MODULE.choose_combination(items)
    duration = sum(item["duration_seconds"] for item in chosen) + (len(chosen) - 1) * MODULE.GAP_MS / 1000

    assert 8 <= duration <= 15


def test_fixed_selection_has_eight_distinct_real_speakers_and_balanced_sexes() -> None:
    speaker_ids = [item[1] for item in MODULE.SPEAKERS]
    genders = [item[2] for item in MODULE.SPEAKERS]

    assert len(speaker_ids) == len(set(speaker_ids)) == 8
    assert genders.count("male") == 4
    assert genders.count("female") == 4
