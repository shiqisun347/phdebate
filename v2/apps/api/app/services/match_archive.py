from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.entities import (
    AudioAsset,
    Competition,
    JudgeScorecard,
    Match,
    MatchEvent,
    RatingChange,
    Room,
    RoomSeat,
    Season,
    Speech,
    TranscriptSegment,
    User,
)
from sqlalchemy import select, text
from sqlalchemy.orm import Session

ARCHIVE_SCHEMA = "jixia-debate-match-archive"
ARCHIVE_VERSION = 2
logger = logging.getLogger(__name__)


class MatchArchiveNotFound(LookupError):
    pass


@dataclass(frozen=True)
class MatchArchiveResult:
    path: Path
    sha256: str
    source_sha256: str
    size_bytes: int
    reused: bool


@dataclass(frozen=True)
class MatchArchivePayload:
    result: MatchArchiveResult
    content: bytes


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def archive_lock_name(match_id: str) -> str:
    return f"{hashlib.sha256(match_id.encode()).hexdigest()}.lock"


def _atomic_write(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.part")
    try:
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _archive_lock(match_id: str):
    archive_dir = settings.archive_path
    archive_dir.mkdir(parents=True, exist_ok=True)
    lock_dir = archive_dir / ".locks"
    lock_dir.mkdir(mode=0o700, exist_ok=True)
    try:
        os.chmod(lock_dir, 0o700)
    except OSError:
        pass
    lock_path = lock_dir / archive_lock_name(match_id)
    with lock_path.open("a+b") as handle:
        try:
            os.chmod(lock_path, 0o600)
        except OSError:
            pass
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _archive_source(db: Session, match_id: str) -> dict[str, Any]:
    match = db.get(Match, match_id)
    if not match:
        raise MatchArchiveNotFound("比赛不存在。")
    room = db.get(Room, match.room_id)
    if not room:
        raise MatchArchiveNotFound("比赛缺少对应房间。")
    competition = db.get(Competition, match.competition_id)
    season = db.get(Season, match.season_id) if match.season_id else None
    seats = list(db.scalars(select(RoomSeat).where(RoomSeat.room_id == room.id).order_by(RoomSeat.side, RoomSeat.position)).all())
    user_ids = {seat.user_id for seat in seats if seat.user_id}
    users = {item.id: item for item in db.scalars(select(User).where(User.id.in_(user_ids))).all()} if user_ids else {}
    speeches = list(db.scalars(select(Speech).where(Speech.match_id == match.id).order_by(Speech.created_at, Speech.id)).all())
    speech_ids = [item.id for item in speeches]
    segments_by_speech: dict[str, list[TranscriptSegment]] = {item.id: [] for item in speeches}
    if speech_ids:
        segments = db.scalars(
            select(TranscriptSegment)
            .where(TranscriptSegment.speech_id.in_(speech_ids))
            .order_by(TranscriptSegment.speech_id, TranscriptSegment.start_ms, TranscriptSegment.id)
        ).all()
        for segment in segments:
            segments_by_speech.setdefault(segment.speech_id, []).append(segment)
    events = db.scalars(select(MatchEvent).where(MatchEvent.room_id == room.id).order_by(MatchEvent.seq)).all()
    assets = db.scalars(select(AudioAsset).where(AudioAsset.match_id == match.id).order_by(AudioAsset.created_at, AudioAsset.id)).all()
    scorecard = db.scalar(select(JudgeScorecard).where(JudgeScorecard.match_id == match.id))
    rating_changes = list(
        db.scalars(select(RatingChange).where(RatingChange.match_id == match.id).order_by(RatingChange.created_at, RatingChange.id)).all()
    )
    rating_user_ids = {item.user_id for item in rating_changes}
    rating_users = {item.id: item for item in db.scalars(select(User).where(User.id.in_(rating_user_ids))).all()} if rating_user_ids else {}
    service_snapshot = {
        kind: {key: value for key, value in config.items() if key != "secret_ciphertext"}
        for kind, config in (match.service_snapshot or {}).items()
        if isinstance(config, dict)
    }
    return {
        "match": {
            "id": match.id,
            "status": match.status,
            "winner": match.winner,
            "result_reason": match.result_reason,
            "legacy": match.legacy,
            "legacy_source_id": match.legacy_source_id,
            "judge_profile_id": match.judge_profile_id,
            "judge_snapshot": match.judge_snapshot,
            "service_snapshot": service_snapshot,
            "created_at": _iso(match.created_at),
            "updated_at": _iso(match.updated_at),
        },
        "room": {
            "id": room.id,
            "code": room.code,
            "topic": room.topic,
            "status": room.status,
            "visibility": room.visibility,
            "template_snapshot": room.template_snapshot,
            "started_at": _iso(room.started_at),
            "completed_at": _iso(room.completed_at),
            "created_at": _iso(room.created_at),
            "updated_at": _iso(room.updated_at),
        },
        "competition": (
            {
                "id": competition.id,
                "slug": competition.slug,
                "name": competition.name,
                "format": competition.format,
                "ranked": competition.ranked,
            }
            if competition
            else None
        ),
        "season": ({"id": season.id, "slug": season.slug, "name": season.name} if season else None),
        "seats": [
            {
                "seat_key": seat.seat_key,
                "side": seat.side,
                "position": seat.position,
                "occupant_type": seat.occupant_type,
                "user_id": seat.user_id,
                "display_name": users.get(seat.user_id).real_name if seat.user_id in users else seat.display_name,
                "agent_profile_id": seat.agent_profile_id,
            }
            for seat in seats
        ],
        "speeches": [
            {
                "id": speech.id,
                "seat_key": speech.seat_key,
                "stage_key": speech.stage_key,
                "speaker_type": speech.speaker_type,
                "content": speech.content,
                "audio_url": speech.audio_url,
                "duration_seconds": speech.duration_seconds,
                "status": speech.status,
                "created_at": _iso(speech.created_at),
                "transcript_segments": [
                    {
                        "id": segment.id,
                        "start_ms": segment.start_ms,
                        "end_ms": segment.end_ms,
                        "text": segment.text,
                        "is_final": segment.is_final,
                    }
                    for segment in segments_by_speech.get(speech.id, [])
                ],
            }
            for speech in speeches
        ],
        "events": [
            {
                "id": event.id,
                "seq": event.seq,
                "type": event.event_type,
                "actor_user_id": event.actor_user_id,
                "payload": event.payload,
                "created_at": _iso(event.created_at),
            }
            for event in events
        ],
        "audio_assets": [
            {
                "id": asset.id,
                "kind": asset.kind,
                "storage_key": asset.storage_key,
                "mime_type": asset.mime_type,
                "size_bytes": asset.size_bytes,
                "created_at": _iso(asset.created_at),
            }
            for asset in assets
        ],
        "scorecard": (
            {
                "id": scorecard.id,
                "status": scorecard.status,
                "winner": scorecard.winner,
                "affirmative_score": scorecard.affirmative_score,
                "negative_score": scorecard.negative_score,
                "individual_scores": scorecard.individual_scores,
                "reasoning": scorecard.reasoning,
                "reviewed_by": scorecard.reviewed_by,
                "created_at": _iso(scorecard.created_at),
                "updated_at": _iso(scorecard.updated_at),
            }
            if scorecard
            else None
        ),
        "rating_changes": [
            {
                "id": item.id,
                "user_id": item.user_id,
                "display_name": rating_users.get(item.user_id).real_name if item.user_id in rating_users else "参赛选手",
                "points_delta": item.points_delta,
                "score": item.score,
                "reason": item.reason,
                "source": item.source,
                "created_at": _iso(item.created_at),
            }
            for item in rating_changes
        ],
    }


def _build_match_archive_locked(match_id: str) -> MatchArchiveResult:
    with SessionLocal() as db:
        # A match correction updates the scorecard, result, events and rating
        # rows in one transaction. READ COMMITTED could otherwise assemble an
        # archive from different committed moments across these queries.
        if db.get_bind().dialect.name == "postgresql":
            db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
        source = _archive_source(db, match_id)
    source_bytes = _canonical_json(source)
    source_sha256 = _sha256(source_bytes)
    safe_match_id = str(source["match"]["id"])
    archive_dir = settings.archive_path
    archive_path = archive_dir / f"{safe_match_id}.json"
    metadata_path = archive_dir / f"{safe_match_id}.meta.json"
    checksum_path = archive_dir / f"{safe_match_id}.json.sha256"
    try:
        metadata = json.loads(metadata_path.read_text("utf-8"))
        existing = archive_path.read_bytes()
        existing_document = json.loads(existing)
        existing_sha256 = _sha256(existing)
        expected_checksum = f"{existing_sha256}  {archive_path.name}\n"
        if (
            metadata.get("schema") == ARCHIVE_SCHEMA
            and metadata.get("version") == ARCHIVE_VERSION
            and metadata.get("match_id") == safe_match_id
            and metadata.get("source_sha256") == source_sha256
            and metadata.get("sha256") == existing_sha256
            and metadata.get("bytes") == len(existing)
            and existing_document.get("schema") == ARCHIVE_SCHEMA
            and existing_document.get("version") == ARCHIVE_VERSION
            and existing_document.get("source_sha256") == source_sha256
            and _sha256(_canonical_json(existing_document.get("data"))) == source_sha256
        ):
            try:
                checksum_valid = checksum_path.read_text("ascii") == expected_checksum
            except (FileNotFoundError, OSError, UnicodeError):
                checksum_valid = False
            if not checksum_valid:
                _atomic_write(checksum_path, expected_checksum.encode("ascii"))
            return MatchArchiveResult(archive_path, existing_sha256, source_sha256, len(existing), True)
    except (FileNotFoundError, OSError, ValueError, TypeError):
        pass

    generated_at = datetime.now(timezone.utc).isoformat()
    document = {
        "schema": ARCHIVE_SCHEMA,
        "version": ARCHIVE_VERSION,
        "generated_at": generated_at,
        "source_sha256": source_sha256,
        "data": source,
    }
    archive_bytes = (json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    archive_sha256 = _sha256(archive_bytes)
    metadata_bytes = (
        json.dumps(
            {
                "schema": ARCHIVE_SCHEMA,
                "version": ARCHIVE_VERSION,
                "match_id": safe_match_id,
                "generated_at": generated_at,
                "source_sha256": source_sha256,
                "sha256": archive_sha256,
                "bytes": len(archive_bytes),
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")
    _atomic_write(archive_path, archive_bytes)
    _atomic_write(metadata_path, metadata_bytes)
    _atomic_write(checksum_path, f"{archive_sha256}  {archive_path.name}\n".encode("ascii"))
    return MatchArchiveResult(archive_path, archive_sha256, source_sha256, len(archive_bytes), False)


def _ensure_match_exists(match_id: str) -> None:
    # Queue retries can arrive after a disposable verification room has already
    # been removed. Avoid creating a permanent advisory-lock file for work that
    # is known to be obsolete before lock acquisition.
    with SessionLocal() as db:
        if not db.get(Match, match_id):
            raise MatchArchiveNotFound("比赛不存在。")


def build_match_archive(match_id: str) -> MatchArchiveResult:
    _ensure_match_exists(match_id)
    with _archive_lock(match_id):
        return _build_match_archive_locked(match_id)


def read_match_archive(match_id: str) -> MatchArchivePayload:
    """Build and read one immutable response snapshot while holding the per-match lock."""
    _ensure_match_exists(match_id)
    with _archive_lock(match_id):
        result = _build_match_archive_locked(match_id)
        content = result.path.read_bytes()
        if _sha256(content) != result.sha256:
            result = _build_match_archive_locked(match_id)
            content = result.path.read_bytes()
            if _sha256(content) != result.sha256:
                raise OSError("比赛归档在读取期间发生变化。")
        return MatchArchivePayload(result=result, content=content)


def enqueue_match_archive(match_id: str) -> bool:
    """Best-effort queueing; downloads rebuild synchronously if the worker is unavailable."""
    if settings.app_env == "test":
        return False
    try:
        from app.worker_tasks import archive_match

        archive_match.send(match_id)
        return True
    except Exception:
        logger.exception("match_archive_enqueue_failed match_id=%s", match_id)
        return False
