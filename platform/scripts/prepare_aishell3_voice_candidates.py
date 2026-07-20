#!/usr/bin/env python3
"""Prepare eight distinct, licensed AISHELL-3 CosyVoice prompt candidates.

The source corpus is the official AISHELL Hugging Face mirror pinned to a
specific commit. Only a small set of selected utterances is retained. Each
candidate is composed exclusively from one real speaker; no pitch shifting,
voice conversion, synthesis, or cross-speaker mixing is performed.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from array import array
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DATASET_ID = "AISHELL/AISHELL-3"
DATASET_COMMIT = "f20d5db4a31fe779ef07bb1af4ea92da5c786622"
BASE_RESOLVE = f"https://huggingface.co/datasets/{DATASET_ID}/resolve/{DATASET_COMMIT}"
BASE_RAW = f"https://huggingface.co/datasets/{DATASET_ID}/raw/{DATASET_COMMIT}"
HF_API = f"https://hf-mirror.com/api/datasets/{DATASET_ID}?blobs=true"
LFS_BATCH_URL = f"https://huggingface.co/datasets/{DATASET_ID}.git/info/lfs/objects/batch"
LICENSE_NAME = "Apache License 2.0"
LICENSE_URL = "https://www.openslr.org/93/"
COSYVOICE_URL = "https://github.com/FunAudioLLM/CosyVoice"
TARGET_PEAK_DBFS = -3.5
TARGET_SAMPLE_RATE = 24_000
GAP_MS = 120
NEUTRALITY_EXCLUDED_TOKENS = (
    "我",
    "你",
    "他",
    "她",
    "嘲笑",
    "恳求",
    "伏特加",
    "牢狱",
    "漂亮",
    "心动",
    "妹砣",
    "杀",
    "死亡",
    "受伤",
    "拘留",
    "爱情",
    "愤怒",
    "哭",
)

SPEAKERS = [
    ("candidate_voice_1", "SSB0273", "male", "north", "B"),
    ("candidate_voice_2", "SSB0241", "male", "north", "B"),
    ("candidate_voice_3", "SSB0073", "male", "north", "B"),
    ("candidate_voice_4", "SSB0629", "male", "north", "B"),
    ("candidate_voice_5", "SSB0016", "female", "north", "B"),
    ("candidate_voice_6", "SSB0534", "female", "north", "C"),
    ("candidate_voice_7", "SSB0380", "female", "north", "B"),
    ("candidate_voice_8", "SSB0200", "female", "north", "B"),
]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, destination: Path) -> None:
    if destination.is_file() and destination.stat().st_size > 0:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "phdebate-qa/1.0"})
    for attempt in range(6):
        try:
            with urllib.request.urlopen(request, timeout=120) as response, destination.open("wb") as output:
                shutil.copyfileobj(response, output)
            return
        except urllib.error.HTTPError as exc:
            if exc.code != 429 or attempt == 5:
                raise
            retry_after = exc.headers.get("Retry-After")
            delay = min(60, float(retry_after)) if retry_after and retry_after.isdigit() else min(60, 5 * 2**attempt)
            time.sleep(delay)


def download_lfs(oid: str, size: int, destination: Path) -> None:
    if destination.is_file() and destination.stat().st_size == size and sha256_file(destination) == oid:
        return
    payload = json.dumps(
        {"operation": "download", "transfers": ["basic"], "objects": [{"oid": oid, "size": size}]}
    ).encode("utf-8")
    request = urllib.request.Request(
        LFS_BATCH_URL,
        data=payload,
        headers={
            "User-Agent": "phdebate-qa/1.0",
            "Accept": "application/vnd.git-lfs+json",
            "Content-Type": "application/vnd.git-lfs+json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        body = json.load(response)
    objects = body.get("objects") if isinstance(body, dict) else None
    if not isinstance(objects, list) or not objects or objects[0].get("error"):
        raise RuntimeError("official Hugging Face LFS batch endpoint did not return a download action")
    action = objects[0].get("actions", {}).get("download", {})
    href = action.get("href")
    if not isinstance(href, str):
        raise RuntimeError("official Hugging Face LFS response is missing a download URL")
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "curl",
            "-fsSL",
            "--retry",
            "3",
            "--connect-timeout",
            "10",
            "--max-time",
            "60",
            "-o",
            str(destination),
            href,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if destination.stat().st_size != size or sha256_file(destination) != oid:
        raise RuntimeError("downloaded LFS object failed size/SHA-256 verification")


def parse_content(text: str, split: str) -> dict[str, dict[str, str]]:
    records: dict[str, dict[str, str]] = {}
    for line in text.splitlines():
        if not line.strip() or "\t" not in line:
            continue
        filename, annotated = line.split("\t", 1)
        tokens = annotated.split()
        transcript = "".join(tokens[0::2])
        if filename.endswith(".wav") and transcript:
            records[filename] = {"transcript": transcript, "split": split}
    return records


def parse_speaker_info(text: str) -> dict[str, dict[str, str]]:
    speakers: dict[str, dict[str, str]] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) == 4:
            speakers[parts[0]] = {"age_group": parts[1], "gender": parts[2], "accent": parts[3]}
    return speakers


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as audio:
        return audio.getnframes() / max(1, audio.getframerate())


def choose_combination(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    options: list[tuple[float, tuple[dict[str, Any], ...]]] = []
    for size in range(1, min(4, len(items)) + 1):
        for group in itertools.combinations(items, size):
            if any(float(item.get("selection_silence_run_max_ms", 0)) > 600 for item in group):
                continue
            total = sum(float(item.get("selection_duration_seconds", item["duration_seconds"])) for item in group)
            total += (size - 1) * GAP_MS / 1000
            if 8 <= total <= 15:
                score = abs(total - 10.5) + 0.15 * (size - 1)
                options.append((score, group))
    if not options:
        raise RuntimeError("could not form an 8–15 second prompt from downloaded utterances")
    return list(min(options, key=lambda item: item[0])[1])


def convert_component(source: Path, destination: Path) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-ar",
            str(TARGET_SAMPLE_RATE),
            "-ac",
            "1",
            "-af",
            (
                "silenceremove=start_periods=1:start_duration=0.05:start_silence=0.05:start_threshold=-50dB,"
                "areverse,"
                "silenceremove=start_periods=1:start_duration=0.05:start_silence=0.05:start_threshold=-50dB,"
                "areverse"
            ),
            "-c:a",
            "pcm_s16le",
            str(destination),
        ],
        check=True,
    )


def compose_prompt(components: list[Path], destination: Path) -> None:
    combined = array("h")
    silence = array("h", [0]) * round(TARGET_SAMPLE_RATE * GAP_MS / 1000)
    for index, path in enumerate(components):
        with wave.open(str(path), "rb") as audio:
            if (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) != (TARGET_SAMPLE_RATE, 1, 2):
                raise RuntimeError("converted component has unexpected WAV format")
            frames = array("h")
            frames.frombytes(audio.readframes(audio.getnframes()))
        if index:
            combined.extend(silence)
        combined.extend(frames)
    peak = max((abs(sample) for sample in combined), default=0)
    target = round(32767 * 10 ** (TARGET_PEAK_DBFS / 20))
    if peak > 0:
        scale = target / peak
        combined = array("h", (round(sample * scale) for sample in combined))
    destination.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(destination), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(TARGET_SAMPLE_RATE)
        audio.writeframes(combined.tobytes())


def dbfs(amplitude: float) -> float:
    if amplitude <= 0:
        return -120.0
    return 20 * math.log10(amplitude / 32768)


def percentile(values: list[float], proportion: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * proportion
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def analyze_wav(path: Path) -> dict[str, Any]:
    with wave.open(str(path), "rb") as audio:
        channels = audio.getnchannels()
        width = audio.getsampwidth()
        rate = audio.getframerate()
        frames = audio.getnframes()
        compression = audio.getcomptype()
        samples = array("h")
        samples.frombytes(audio.readframes(frames))
    peak = max((abs(sample) for sample in samples), default=0)
    rms = math.sqrt(sum(sample * sample for sample in samples) / max(1, len(samples)))
    clipping = sum(abs(sample) >= 32760 for sample in samples)
    window = max(1, round(rate * 0.01))
    levels = []
    for offset in range(0, len(samples), window):
        chunk = samples[offset : offset + window]
        if len(chunk) < window:
            continue
        chunk_rms = math.sqrt(sum(sample * sample for sample in chunk) / len(chunk))
        levels.append(dbfs(chunk_rms))
    runs: list[int] = []
    active_start: int | None = None
    for index, level in enumerate(levels + [0.0]):
        silent = index < len(levels) and level <= -45
        if silent and active_start is None:
            active_start = index
        elif not silent and active_start is not None:
            runs.append((index - active_start) * 10)
            active_start = None
    return {
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
        "codec": "pcm_s16le" if compression == "NONE" and width == 2 else compression,
        "sample_rate_hz": rate,
        "channels": channels,
        "bit_depth": width * 8,
        "duration_seconds": round(frames / max(1, rate), 6),
        "peak_dbfs": round(dbfs(peak), 3),
        "rms_dbfs": round(dbfs(rms), 3),
        "clipping_samples": clipping,
        "silence_run_max_ms": max(runs, default=0),
        "silence_run_p95_ms": round(percentile([float(value) for value in runs], 0.95), 1),
    }


def prompt_static_gate(audio: dict[str, Any], transcript: str) -> dict[str, bool]:
    return {
        "duration_8_to_15_seconds": 8 <= audio["duration_seconds"] <= 15,
        "sample_rate_24000_hz": audio["sample_rate_hz"] == TARGET_SAMPLE_RATE,
        "mono": audio["channels"] == 1,
        "pcm16_wav": audio["codec"] == "pcm_s16le" and audio["bit_depth"] == 16,
        "peak_at_or_below_minus_3_dbfs": audio["peak_dbfs"] <= -3,
        "no_clipping": audio["clipping_samples"] == 0,
        "rms_between_minus_32_and_minus_14_dbfs": -32 <= audio["rms_dbfs"] <= -14,
        "internal_silence_run_at_most_600ms": audio["silence_run_max_ms"] <= 600,
        "transcript_present": bool(transcript),
        "neutrality_lexicon_filter_passed": not any(token in transcript for token in NEUTRALITY_EXCLUDED_TOKENS),
        "cosyvoice_end_marker_present": True,
    }


def markdown(document: dict[str, Any]) -> str:
    lines = [
        "# AISHELL-3 neutral Mandarin CosyVoice prompt candidates",
        "",
        f"- Generated: {document['generated_at']}",
        f"- Dataset commit: `{document['dataset']['commit']}`",
        f"- License: {document['dataset']['license']} ({document['dataset']['license_url']})",
        "- Source: official AISHELL Hugging Face dataset repository; eight distinct real speaker IDs.",
        "- Processing: edge-silence trim, resample/downmix/PCM16 conversion, same-speaker concatenation and peak normalization only.",
        "- Candidate transcripts exclude first/second-person dialogue and a conservative list of emotional/violent/colloquial terms.",
        "- No synthesis, pitch shifting, voice conversion or cross-speaker mixing was used.",
        f"- Static gate: **{'PASS' if document['static_gate_passed'] else 'NO-GO'}**",
        "",
        (
            "| Candidate | Speaker | Sex | Source clips | Duration s | Peak/RMS dBFS | "
            "Max silence ms | Converted SHA-256 | Transcript confidence | Gate |"
        ),
        "|---|---|---|---:|---:|---:|---:|---|---|---|",
    ]
    for item in document["candidates"]:
        audio = item["converted_audio"]
        lines.append(
            f"| {item['candidate_id']} | {item['speaker_id']} | {item['speaker']['gender']} | "
            f"{len(item['source_components'])} | {audio['duration_seconds']} | "
            f"{audio['peak_dbfs']}/{audio['rms_dbfs']} | {audio['silence_run_max_ms']} | "
            f"`{audio['sha256']}` | {item['transcript']['confidence']} | "
            f"{'PASS' if item['static_gate_passed'] else 'NO-GO'} |"
        )
    lines.extend(
        [
            "",
            "## Remaining release blockers",
            "",
            (
                "- Static compliance does not prove naturalness or suitability for debate. Human listening must reject "
                "dialect, role-play tone, noise and unstable delivery."
            ),
            "- Each exact transcript must be listened against the converted composite before it can become a production prompt.",
            "- Run CosyVoice3 TTS→ASR CER, 20-round voice-drift, 8-way blind distinction and MOS gates before assigning debate_voice_1..8.",
            "- Apache-2.0 attribution and the AISHELL-3 notice/source link must be preserved in any redistributed bundle.",
            "",
        ]
    )
    return "\n".join(lines)


def run(output_dir: Path) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    metadata_dir = output_dir / "source-metadata"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    metadata_files = {
        "README.md": f"{BASE_RAW}/README.md",
        "spk-info.txt": f"{BASE_RAW}/spk-info.txt",
        "train-content.txt": f"{BASE_RAW}/train/content.txt",
        "test-content.txt": f"{BASE_RAW}/test/content.txt",
    }
    for name, url in metadata_files.items():
        download(url, metadata_dir / name)
    api_path = metadata_dir / "dataset-api.json"
    if api_path.is_file():
        cached_api = json.loads(api_path.read_text(encoding="utf-8"))
        cached_siblings = cached_api.get("siblings") if isinstance(cached_api, dict) else None
        if not isinstance(cached_siblings, list) or not any(isinstance(item, dict) and item.get("lfs") for item in cached_siblings):
            api_path.unlink()
    download(HF_API, api_path)
    speaker_info = parse_speaker_info((metadata_dir / "spk-info.txt").read_text(encoding="utf-8"))
    content = parse_content((metadata_dir / "train-content.txt").read_text(encoding="utf-8"), "train")
    content.update(parse_content((metadata_dir / "test-content.txt").read_text(encoding="utf-8"), "test"))
    api = json.loads(api_path.read_text(encoding="utf-8"))
    file_entries = {
        Path(item["rfilename"]).name: item
        for item in api.get("siblings", [])
        if isinstance(item, dict) and str(item.get("rfilename", "")).endswith(".wav") and isinstance(item.get("lfs"), dict)
    }
    candidates: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="aishell3-prompts-") as temporary:
        temp_root = Path(temporary)
        for candidate_id, speaker_id, gender, accent, age_group in SPEAKERS:
            official = speaker_info.get(speaker_id)
            expected = {"gender": gender, "accent": accent, "age_group": age_group}
            if official != expected:
                raise RuntimeError(f"speaker metadata mismatch for {speaker_id}")
            available = []
            for filename, record in content.items():
                if not filename.startswith(speaker_id) or filename not in file_entries:
                    continue
                characters = len(record["transcript"])
                neutral = not any(token in record["transcript"] for token in NEUTRALITY_EXCLUDED_TOKENS)
                if 12 <= characters <= 60 and neutral:
                    available.append((characters, filename, record))
            available.sort(key=lambda item: item[0])
            sample_count = min(24, len(available))
            sample_indexes = {
                round(index * (len(available) - 1) / max(1, sample_count - 1))
                for index in range(sample_count)
            }
            downloaded: list[dict[str, Any]] = []
            selected: list[dict[str, Any]] | None = None
            existing_source_dir = output_dir / "selected-source" / candidate_id
            for available_index in sorted(sample_indexes):
                _characters, filename, record = available[available_index]
                entry = file_entries[filename]
                relative = entry["rfilename"]
                source = temp_root / speaker_id / filename
                url = f"{BASE_RESOLVE}/{urllib.parse.quote(relative, safe='/')}"
                retained_previous = existing_source_dir / filename
                expected_oid = str(entry["lfs"]["sha256"])
                expected_size = int(entry["lfs"]["size"])
                if retained_previous.is_file() and retained_previous.stat().st_size == expected_size:
                    if sha256_file(retained_previous) == expected_oid:
                        source.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(retained_previous, source)
                    else:
                        download_lfs(expected_oid, expected_size, source)
                else:
                    download_lfs(expected_oid, expected_size, source)
                preview = temp_root / "preview" / speaker_id / filename
                preview.parent.mkdir(parents=True, exist_ok=True)
                convert_component(source, preview)
                preview_analysis = analyze_wav(preview)
                downloaded.append(
                    {
                        "filename": filename,
                        "relative_path": relative,
                        "source_url": url,
                        "source_sha256": sha256_file(source),
                        "duration_seconds": wav_duration(source),
                        "selection_duration_seconds": wav_duration(preview),
                        "selection_silence_run_max_ms": preview_analysis["silence_run_max_ms"],
                        "transcript": record["transcript"],
                        "split": record["split"],
                        "temporary_path": source,
                        "converted_temporary_path": preview,
                    }
                )
                if len(downloaded) >= 6:
                    try:
                        selected = choose_combination(downloaded)
                    except RuntimeError:
                        selected = None
                    if selected is not None:
                        break
            try:
                selected = selected or choose_combination(downloaded)
            except RuntimeError as exc:
                durations = ",".join(str(round(item["selection_duration_seconds"], 2)) for item in downloaded)
                raise RuntimeError(f"{speaker_id}: {exc}; sampled durations={durations}") from exc
            source_dir = output_dir / "selected-source" / candidate_id
            if source_dir.exists():
                shutil.rmtree(source_dir)
            converted_components = []
            source_components = []
            for index, item in enumerate(selected, start=1):
                retained = source_dir / item["filename"]
                retained.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item["temporary_path"], retained)
                converted = temp_root / "converted" / candidate_id / f"{index}.wav"
                converted.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item["converted_temporary_path"], converted)
                converted_components.append(converted)
                source_components.append(
                    {
                        key: value
                        for key, value in item.items()
                        if key
                        not in {
                            "temporary_path",
                            "converted_temporary_path",
                            "selection_duration_seconds",
                            "selection_silence_run_max_ms",
                        }
                    }
                )
            final = output_dir / "converted" / f"{candidate_id}.wav"
            compose_prompt(converted_components, final)
            transcript = "，".join(item["transcript"] for item in selected)
            prompt_text = f"You are a helpful assistant.<|endofprompt|>{transcript}"
            transcript_dir = output_dir / "transcripts"
            transcript_dir.mkdir(parents=True, exist_ok=True)
            (transcript_dir / f"{candidate_id}.txt").write_text(transcript + "\n", encoding="utf-8")
            (transcript_dir / f"{candidate_id}.prompt.txt").write_text(prompt_text + "\n", encoding="utf-8")
            audio = analyze_wav(final)
            checks = prompt_static_gate(audio, transcript)
            candidates.append(
                {
                    "candidate_id": candidate_id,
                    "speaker_id": speaker_id,
                    "speaker": official,
                    "source_components": source_components,
                    "converted_audio": audio,
                    "transcript": {
                        "text": transcript,
                        "sha256": hashlib.sha256(transcript.encode("utf-8")).hexdigest(),
                        "cosyvoice_prompt_text_sha256": hashlib.sha256(prompt_text.encode("utf-8")).hexdigest(),
                        "confidence": "high: official AISHELL-3 manually transcribed character labels (>98% word/tone accuracy claim)",
                    },
                    "static_checks": checks,
                    "static_gate_passed": all(checks.values()),
                }
            )
    document = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset": {
            "id": DATASET_ID,
            "commit": DATASET_COMMIT,
            "source_url": f"https://huggingface.co/datasets/{DATASET_ID}/tree/{DATASET_COMMIT}",
            "metadata_transport": "hf-mirror API metadata; audio bytes fetched through official Hugging Face Git LFS batch URLs",
            "official_project_url": "https://www.openslr.org/93/",
            "license": LICENSE_NAME,
            "license_url": LICENSE_URL,
            "cosyvoice_reference": COSYVOICE_URL,
            "metadata_sha256": {name: sha256_file(metadata_dir / name) for name in metadata_files},
        },
        "processing": {
            "target_format": "24kHz mono PCM16 WAV",
            "same_speaker_gap_ms": GAP_MS,
            "target_peak_dbfs": TARGET_PEAK_DBFS,
            "pitch_shift": False,
            "voice_conversion": False,
            "synthetic_source": False,
            "cross_speaker_mixing": False,
        },
        "distinct_speaker_ids": [item["speaker_id"] for item in candidates],
        "candidates": candidates,
        "static_gate_passed": len(candidates) == 8
        and len({item["speaker_id"] for item in candidates}) == 8
        and all(item["static_gate_passed"] for item in candidates),
    }
    (output_dir / "candidate-manifest.json").write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output_dir / "README.md").write_text(markdown(document), encoding="utf-8")
    return document


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    document = run(args.output_dir.resolve())
    print(json.dumps({"static_gate_passed": document["static_gate_passed"], "output_dir": str(args.output_dir.resolve())}, indent=2))


if __name__ == "__main__":
    main()
