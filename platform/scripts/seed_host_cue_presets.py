#!/usr/bin/env python3
"""Promote existing verified host WAVs into reusable system presets.

The script never calls TTS. It copies one already-generated WAV for every
fixed template sentence into the global ``_cues`` directory and records it as
an active ``AudioCue``. Run it once before enabling HOST_CUES_PRESET_ONLY.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
from pathlib import Path

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.entities import AudioCue, MatchEvent
from app.services.seed import DAILY_STAGES, TRAINING_STAGES
from sqlalchemy import select


def normalized(value: str) -> str:
    return " ".join(value.split())


def expected_cues() -> dict[str, str]:
    cues: dict[str, str] = {}
    for competition, stages in (("正式赛", DAILY_STAGES), ("训练赛", TRAINING_STAGES)):
        for stage in stages:
            text = normalized(str(stage.get("cue") or ""))
            if text:
                cues.setdefault(text, f"{competition} · {stage['name']}")
    return cues


def source_from_url(audio_url: str) -> Path | None:
    prefix = "/media/"
    if not audio_url.startswith(prefix):
        return None
    relative = Path(audio_url.removeprefix(prefix))
    if relative.is_absolute() or ".." in relative.parts:
        return None
    source = settings.media_path / relative
    return source if source.is_file() and not source.is_symlink() else None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    target_dir = settings.media_path / "_cues"
    created = 0
    reused = 0
    missing: list[str] = []

    with SessionLocal() as db:
        events = db.scalars(
            select(MatchEvent)
            .where(MatchEvent.event_type == "audio.cue.ready")
            .order_by(MatchEvent.created_at.desc())
        ).all()
        sources: dict[str, Path] = {}
        for event in events:
            payload = event.payload or {}
            text = normalized(str(payload.get("text") or ""))
            audio_url = str(payload.get("audio_url") or "")
            if not text or text in sources:
                continue
            source = source_from_url(audio_url)
            if source:
                sources[text] = source

        for text, label in expected_cues().items():
            existing = db.scalar(
                select(AudioCue).where(
                    AudioCue.text == text,
                    AudioCue.is_active.is_(True),
                    AudioCue.audio_url != "",
                )
            )
            if existing and (target_dir / f"{existing.id}.wav").is_file():
                reused += 1
                continue
            source = sources.get(text)
            if not source:
                missing.append(text)
                continue
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
            item = existing or AudioCue(
                key=f"host_{digest}",
                name=f"系统女声主持 · {label}",
                text=text,
                is_active=True,
            )
            if not existing:
                db.add(item)
                db.flush()
            destination = target_dir / f"{item.id}.wav"
            if not args.dry_run:
                target_dir.mkdir(parents=True, exist_ok=True)
                temporary = destination.with_suffix(".wav.part")
                shutil.copyfile(source, temporary)
                temporary.replace(destination)
                item.audio_url = f"/media/_cues/{destination.name}"
            created += 1

        if args.dry_run:
            db.rollback()
        else:
            db.commit()

    print({"created": created, "reused": reused, "missing": missing, "preset_only_ready": not missing})
    if missing:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
