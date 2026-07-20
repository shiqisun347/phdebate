#!/usr/bin/env python3
"""Build a privacy-preserving manifest for the eight fixed debate voices.

Only exact LightTTS prompt-related dotenv keys are read. Prompt text is never
printed or persisted; reports contain its SHA-256, character count and boolean
validation flags. Audio is not copied or modified.
"""

from __future__ import annotations

import argparse
import ast
import audioop
import hashlib
import json
import math
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VOICE_IDS = [f"debate_voice_{index}" for index in range(1, 9)]
SEAT_MAPPING = {
    "aff_1": "debate_voice_1",
    "aff_2": "debate_voice_2",
    "aff_3": "debate_voice_3",
    "aff_4": "debate_voice_4",
    "neg_1": "debate_voice_5",
    "neg_2": "debate_voice_6",
    "neg_3": "debate_voice_7",
    "neg_4": "debate_voice_8",
}
PROMPT_KEYS = {"LIGHTTTS_PROMPT_TEXT", "LIGHTTTS_PROMPT_TEXTS"}


def _dotenv_value(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        try:
            parsed = ast.literal_eval(value)
        except (SyntaxError, ValueError):
            return value[1:-1]
        return str(parsed)
    return value


def prompt_texts_from_dotenv(path: Path | None) -> dict[str, str]:
    if path is None or not path.is_file():
        return {}
    selected: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        if key.strip() in PROMPT_KEYS:
            selected[key.strip()] = _dotenv_value(value)
    prompts: dict[str, str] = {}
    default = selected.get("LIGHTTTS_PROMPT_TEXT", "")
    if default:
        prompts["debate_voice_1"] = default
    encoded = selected.get("LIGHTTTS_PROMPT_TEXTS", "")
    if encoded:
        try:
            mapping = json.loads(encoded)
        except json.JSONDecodeError:
            mapping = {}
        if isinstance(mapping, dict):
            for voice_id, text in mapping.items():
                if voice_id in VOICE_IDS and isinstance(text, str):
                    prompts[voice_id] = text
    return prompts


def prompt_metadata(text: str | None) -> dict[str, Any]:
    if not text:
        return {
            "configured": False,
            "sha256": None,
            "characters": 0,
            "has_endofprompt": False,
            "placeholder_suspected": False,
        }
    normalized = text.strip()
    placeholder_tokens = ("准确逐字稿", "提示音的", "placeholder", "replace me")
    return {
        "configured": True,
        "sha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        "characters": len(normalized),
        "has_endofprompt": "<|endofprompt|>" in normalized,
        "placeholder_suspected": any(token.lower() in normalized.lower() for token in placeholder_tokens),
    }


def _dbfs(amplitude: int) -> float:
    if amplitude <= 0:
        return -120.0
    return 20 * math.log10(amplitude / 32768)


def wav_metadata(path: Path) -> dict[str, Any]:
    with wave.open(str(path), "rb") as audio:
        channels = audio.getnchannels()
        sample_width = audio.getsampwidth()
        sample_rate = audio.getframerate()
        frames = audio.getnframes()
        compression = audio.getcomptype()
        pcm = audio.readframes(frames)
    peak_dbfs = _dbfs(audioop.max(pcm, sample_width)) if pcm and sample_width in {1, 2, 3, 4} else None
    return {
        "basename": path.name,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "bytes": path.stat().st_size,
        "codec": "pcm_s16le" if compression == "NONE" and sample_width == 2 else compression,
        "sample_rate_hz": sample_rate,
        "channels": channels,
        "bit_depth": sample_width * 8,
        "duration_seconds": round(frames / max(1, sample_rate), 6),
        "peak_dbfs": round(peak_dbfs, 3) if peak_dbfs is not None else None,
    }


def inventory(voice_dir: Path, dotenv: Path | None, source_label: str) -> dict[str, Any]:
    prompts = prompt_texts_from_dotenv(dotenv)
    voices: list[dict[str, Any]] = []
    for voice_id in VOICE_IDS:
        path = voice_dir / f"{voice_id}.wav"
        prompt = prompt_metadata(prompts.get(voice_id))
        if path.is_file():
            try:
                audio = wav_metadata(path)
                error = None
            except (EOFError, wave.Error, OSError) as exc:
                audio = None
                error = f"{type(exc).__name__}: invalid or unreadable WAV"
        else:
            audio = None
            error = None
        checks = {
            "audio_present": audio is not None,
            "duration_8_to_15_seconds": bool(audio and 8 <= audio["duration_seconds"] <= 15),
            "sample_rate_24000_hz": bool(audio and audio["sample_rate_hz"] == 24_000),
            "mono": bool(audio and audio["channels"] == 1),
            "pcm16_wav": bool(audio and audio["codec"] == "pcm_s16le" and audio["bit_depth"] == 16),
            "peak_at_or_below_minus_3_dbfs": bool(audio and audio["peak_dbfs"] is not None and audio["peak_dbfs"] <= -3),
            "prompt_configured": prompt["configured"],
            "prompt_has_endofprompt": prompt["has_endofprompt"],
            "prompt_not_placeholder": prompt["configured"] and not prompt["placeholder_suspected"],
        }
        voices.append(
            {
                "voice_id": voice_id,
                "seat": next(seat for seat, mapped_voice in SEAT_MAPPING.items() if mapped_voice == voice_id),
                "audio": audio,
                "prompt": prompt,
                "checks": checks,
                "compliant": all(checks.values()),
                "error": error,
            }
        )
    version_material = [
        {
            "voice_id": voice["voice_id"],
            "audio_sha256": (voice["audio"] or {}).get("sha256"),
            "prompt_sha256": voice["prompt"]["sha256"],
        }
        for voice in voices
    ]
    version_sha256 = hashlib.sha256(
        json.dumps(version_material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_label": source_label,
        "source_paths_recorded": False,
        "prompt_text_recorded": False,
        "expected_voice_ids": VOICE_IDS,
        "seat_mapping": SEAT_MAPPING,
        "voice_set_version_sha256": version_sha256,
        "voices": voices,
        "summary": {
            "expected": len(VOICE_IDS),
            "audio_present": sum(voice["checks"]["audio_present"] for voice in voices),
            "prompt_configured": sum(voice["checks"]["prompt_configured"] for voice in voices),
            "compliant": sum(voice["compliant"] for voice in voices),
            "missing_voice_ids": [voice["voice_id"] for voice in voices if not voice["checks"]["audio_present"]],
            "noncompliant_voice_ids": [voice["voice_id"] for voice in voices if not voice["compliant"]],
        },
    }


def markdown(document: dict[str, Any]) -> str:
    summary = document["summary"]
    lines = [
        "# Fixed debate voice asset inventory",
        "",
        f"- Generated: {document['generated_at']}",
        f"- Source: `{document['source_label']}` (paths and prompt text omitted)",
        f"- Voice-set version SHA-256: `{document['voice_set_version_sha256']}`",
        f"- Audio present: {summary['audio_present']}/{summary['expected']}",
        f"- Fully compliant: {summary['compliant']}/{summary['expected']}",
        "",
        "| Seat | Voice | Audio | SHA-256 | Format | Duration s | Peak dBFS | Prompt | End marker | Placeholder | Compliant |",
        "|---|---|---|---|---|---:|---:|---|---|---|---|",
    ]
    for voice in document["voices"]:
        audio = voice["audio"]
        rendered_format = (
            f"{audio['codec']}/{audio['sample_rate_hz']}Hz/{audio['channels']}ch/{audio['bit_depth']}bit" if audio else "-"
        )
        lines.append(
            f"| {voice['seat']} | {voice['voice_id']} | {'yes' if audio else 'missing'} | "
            f"{audio['sha256'] if audio else '-'} | {rendered_format} | "
            f"{audio['duration_seconds'] if audio else '-'} | {audio['peak_dbfs'] if audio else '-'} | "
            f"{'yes' if voice['prompt']['configured'] else 'missing'} | "
            f"{'yes' if voice['prompt']['has_endofprompt'] else 'no'} | "
            f"{'suspected' if voice['prompt']['placeholder_suspected'] else 'no'} | "
            f"{'PASS' if voice['compliant'] else 'NO-GO'} |"
        )
    lines.extend(
        [
            "",
            "Compliance requires: 8–15 seconds, 24kHz mono PCM16 WAV, peak ≤−3dBFS, configured matching transcript, "
            "`<|endofprompt|>` marker, and no obvious placeholder transcript.",
            "",
            f"Missing voices: {', '.join(summary['missing_voice_ids']) or 'none'}.",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--voice-dir", type=Path, required=True)
    parser.add_argument("--dotenv", type=Path)
    parser.add_argument("--source-label", default="configured-host")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--stdout-json", action="store_true")
    args = parser.parse_args()
    document = inventory(args.voice_dir, args.dotenv, args.source_label)
    if args.output_dir:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / "voice-assets-manifest.json").write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (args.output_dir / "voice-assets-inventory.md").write_text(markdown(document), encoding="utf-8")
    if args.stdout_json or not args.output_dir:
        print(json.dumps(document, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
