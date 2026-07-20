from __future__ import annotations

import argparse
import json
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.core.database import SessionLocal, create_schema
from app.core.security import hash_password
from app.models.entities import (
    AutomationTemplate,
    Competition,
    Match,
    MatchEvent,
    Room,
    RoomSeat,
    Speech,
    TranscriptSegment,
    User,
)
from sqlalchemy import select


def rows(connection: sqlite3.Connection, query: str, params: tuple = ()) -> list[dict]:
    cursor = connection.execute(query, params)
    columns = [item[0] for item in cursor.description]
    return [dict(zip(columns, item)) for item in cursor.fetchall()]


def parse_json(raw: str | None, fallback):
    try:
        return json.loads(raw or "")
    except (TypeError, json.JSONDecodeError):
        return fallback


def generate_code(db) -> str:
    while True:
        code = f"{secrets.randbelow(900000) + 100000:06d}"
        if not db.scalar(select(Room.id).where(Room.code == code)):
            return code


def migrate(source: Path, dry_run: bool = False) -> dict[str, int]:
    if not source.exists():
        raise SystemExit(f"Legacy SQLite not found: {source}")
    create_schema()
    legacy = sqlite3.connect(source)
    stats = {"matches": 0, "rooms": 0, "events": 0, "speeches": 0, "transcripts": 0}
    with SessionLocal() as db:
        guest = db.scalar(select(User).where(User.account == "legacy_guest"))
        if not guest:
            guest = User(
                account="legacy_guest",
                real_name="历史访客",
                password_hash=hash_password(secrets.token_urlsafe(32)),
                role="user",
                is_active=False,
            )
            db.add(guest)
            db.flush()
        template = db.scalar(select(AutomationTemplate).where(AutomationTemplate.slug == "legacy-import", AutomationTemplate.version == 1))
        if not template:
            template = AutomationTemplate(slug="legacy-import", name="旧系统只读流程", version=1, stages=[], is_active=False)
            db.add(template)
            db.flush()
        competition = db.scalar(select(Competition).where(Competition.slug == "legacy-archive"))
        if not competition:
            competition = Competition(
                slug="legacy-archive",
                name="旧系统历史比赛",
                tagline="V1 迁移的只读比赛记录",
                description="从旧 SQLite 导入，仅用于历史查询和审计。",
                rules="旧系统原始规则",
                format="legacy",
                seat_count=8,
                ranked=False,
                allow_custom_topic=False,
                is_public=False,
                is_active=False,
                automation_template_id=template.id,
            )
            db.add(competition)
            db.flush()

        room_by_match = {item["match_id"]: item for item in rows(legacy, "select * from rooms")}
        for old_match in rows(legacy, "select * from structured_matches order by updated_at"):
            old_id = old_match["id"]
            if db.scalar(select(Match).where(Match.legacy_source_id == old_id)):
                continue
            old_room = room_by_match.get(old_id)
            code = (
                old_room["room_code"]
                if old_room and str(old_room["room_code"]).isdigit() and len(str(old_room["room_code"])) == 6
                else generate_code(db)
            )
            if db.scalar(select(Room.id).where(Room.code == code)):
                code = generate_code(db)
            room = Room(
                code=code,
                competition_id=competition.id,
                owner_id=guest.id,
                topic=old_match.get("topic") or old_match.get("title") or "旧比赛",
                status="completed",
                visibility="private",
                template_snapshot=[],
                completed_at=datetime.now(timezone.utc),
            )
            db.add(room)
            db.flush()
            stats["rooms"] += 1
            slot_rows = rows(legacy, "select * from structured_slots where match_id=? order by side, seat", (old_id,))
            speaker_to_seat = {}
            for slot in slot_rows:
                side_raw = str(slot.get("side") or "").lower()
                side = "aff" if side_raw in {"aff", "affirmative", "pro", "正方"} else "neg"
                position = int(slot.get("seat") or 1)
                key = f"{side}_{position}"
                speaker_to_seat[slot["speaker_id"]] = key
                db.add(
                    RoomSeat(
                        room_id=room.id,
                        seat_key=key,
                        side=side,
                        position=position,
                        occupant_type="ai" if slot.get("speaker_type") == "ai" else "human",
                        display_name=slot.get("name") or key,
                        is_ready=True,
                        connected=False,
                    )
                )
            match = Match(
                room_id=room.id,
                competition_id=competition.id,
                status="completed",
                legacy=True,
                legacy_source_id=old_id,
            )
            db.add(match)
            db.flush()
            stats["matches"] += 1
            speech_map = {}
            for old_speech in rows(legacy, "select * from structured_speeches where match_id=? order by started_at", (old_id,)):
                speech = Speech(
                    match_id=match.id,
                    room_id=room.id,
                    seat_key=speaker_to_seat.get(old_speech.get("speaker_id"), old_speech.get("speaker_id") or "legacy_1"),
                    stage_key=old_speech.get("phase_id") or "legacy",
                    speaker_type="legacy",
                    content=old_speech.get("content_final") or old_speech.get("content_partial") or "",
                    status="completed",
                )
                db.add(speech)
                db.flush()
                speech_map[old_speech["speech_id"]] = speech.id
                stats["speeches"] += 1
            for segment in rows(legacy, "select * from structured_transcript_segments where match_id=? order by created_at", (old_id,)):
                speech_id = speech_map.get(segment.get("speech_id"))
                if speech_id:
                    db.add(TranscriptSegment(speech_id=speech_id, text=segment.get("text") or "", is_final=bool(segment.get("is_final"))))
                    stats["transcripts"] += 1
            for event in rows(legacy, "select * from events where match_id=? order by seq", (old_id,)):
                db.add(
                    MatchEvent(
                        room_id=room.id,
                        match_id=match.id,
                        seq=int(event["seq"]),
                        event_type=f"legacy.{event['type']}",
                        payload=parse_json(event.get("payload_json"), {}),
                        idempotency_key=f"legacy:{old_id}:{event['seq']}",
                    )
                )
                room.seq = max(room.seq, int(event["seq"]))
                stats["events"] += 1
        if dry_run:
            db.rollback()
        else:
            db.commit()
    legacy.close()
    return stats


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    print(json.dumps(migrate(args.source, args.dry_run), ensure_ascii=False, indent=2))
