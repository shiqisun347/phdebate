from __future__ import annotations

import importlib.util
import json
import sys
import wave
from array import array
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "inventory_tts_voice_assets.py"
SPEC = importlib.util.spec_from_file_location("inventory_tts_voice_assets", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def write_wav(path: Path, *, sample_rate: int = 24_000, seconds: int = 8, amplitude: int = 8000) -> None:
    samples = array("h", [amplitude, -amplitude] * (sample_rate * seconds // 2))
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(samples.tobytes())


def test_inventory_never_records_prompt_text_or_source_paths(tmp_path: Path) -> None:
    voice_dir = tmp_path / "private-voices"
    voice_dir.mkdir()
    write_wav(voice_dir / "debate_voice_1.wav")
    secret_prompt = "You are a helpful assistant.<|endofprompt|>这是完全匹配的逐字稿。"
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"APP_SECRET=never-read\nLIGHTTTS_PROMPT_TEXT='{secret_prompt}'\n", encoding="utf-8")

    document = MODULE.inventory(voice_dir, dotenv, "test-host")
    serialized = json.dumps(document, ensure_ascii=False)

    assert secret_prompt not in serialized
    assert str(voice_dir) not in serialized
    assert "never-read" not in serialized
    assert document["summary"]["audio_present"] == 1
    assert document["summary"]["missing_voice_ids"] == [f"debate_voice_{index}" for index in range(2, 9)]
    assert document["voices"][0]["prompt"]["has_endofprompt"] is True


def test_placeholder_transcript_is_flagged(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "LIGHTTTS_PROMPT_TEXTS='{" + '"debate_voice_2":"<|endofprompt|>第二音色提示音的准确逐字稿"' + "}'\n",
        encoding="utf-8",
    )

    prompts = MODULE.prompt_texts_from_dotenv(dotenv)
    metadata = MODULE.prompt_metadata(prompts["debate_voice_2"])

    assert metadata["configured"] is True
    assert metadata["placeholder_suspected"] is True


def test_manifest_version_changes_when_asset_changes(tmp_path: Path) -> None:
    voice_dir = tmp_path / "voices"
    voice_dir.mkdir()
    path = voice_dir / "debate_voice_1.wav"
    write_wav(path, amplitude=4000)
    first = MODULE.inventory(voice_dir, None, "test")["voice_set_version_sha256"]
    write_wav(path, amplitude=6000)
    second = MODULE.inventory(voice_dir, None, "test")["voice_set_version_sha256"]

    assert first != second
