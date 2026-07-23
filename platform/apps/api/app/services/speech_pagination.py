from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime

from app.core.config import settings
from app.core.security import as_utc
from app.models.entities import Speech
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session


class SpeechCursorError(ValueError):
    pass


@dataclass(frozen=True)
class SpeechPage:
    rows: list[Speech]
    page: int
    page_size: int
    total: int
    pages: int
    next_cursor: str | None
    has_more: bool


def _encode_part(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _decode_part(value: str) -> bytes:
    try:
        return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise SpeechCursorError("发言分页游标无效。") from exc


def _encode_cursor(
    match_id: str,
    speech: Speech,
    *,
    next_page: int,
    page_size: int,
    snapshot_total: int,
) -> str:
    payload = _encode_part(
        json.dumps(
            {
                "scope": "match_speeches",
                "match_id": match_id,
                "created_at": as_utc(speech.created_at).isoformat(),
                "speech_id": speech.id,
                "page": next_page,
                "page_size": page_size,
                "snapshot_total": snapshot_total,
                "version": 1,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    )
    signature = _encode_part(hmac.new(settings.app_secret.encode(), payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{signature}"


def _decode_cursor(value: str, *, match_id: str, page_size: int) -> tuple[datetime, str, int, int]:
    try:
        payload_part, signature_part = value.split(".", 1)
    except ValueError as exc:
        raise SpeechCursorError("发言分页游标无效。") from exc
    expected = _encode_part(hmac.new(settings.app_secret.encode(), payload_part.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(signature_part, expected):
        raise SpeechCursorError("发言分页游标无效或已过期。")
    try:
        payload = json.loads(_decode_part(payload_part))
        created_at = datetime.fromisoformat(str(payload["created_at"]))
        speech_id = str(payload["speech_id"])
        page = int(payload["page"])
        cursor_page_size = int(payload["page_size"])
        snapshot_total = int(payload["snapshot_total"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SpeechCursorError("发言分页游标无效。") from exc
    if (
        payload.get("scope") != "match_speeches"
        or payload.get("version") != 1
        or payload.get("match_id") != match_id
        or not speech_id
        or page < 2
        or cursor_page_size != page_size
        or snapshot_total < 0
    ):
        raise SpeechCursorError("发言分页游标不属于当前比赛。")
    return as_utc(created_at), speech_id, page, snapshot_total


def paginate_match_speeches(
    db: Session,
    match_id: str,
    *,
    page: int,
    page_size: int,
    cursor: str | None,
) -> SpeechPage:
    completed_scope = (Speech.match_id == match_id, Speech.status == "completed")
    statement = select(Speech).where(*completed_scope)
    current_page = page
    snapshot_total: int | None = None
    if cursor:
        created_at, speech_id, current_page, snapshot_total = _decode_cursor(
            cursor,
            match_id=match_id,
            page_size=page_size,
        )
        statement = statement.where(
            or_(
                Speech.created_at < created_at,
                and_(Speech.created_at == created_at, Speech.id < speech_id),
            )
        )
    else:
        statement = statement.offset((page - 1) * page_size)
    fetched = list(db.scalars(statement.order_by(Speech.created_at.desc(), Speech.id.desc()).limit(page_size + 1)).all())
    if snapshot_total is not None:
        total = snapshot_total
    elif page == 1 and fetched:
        newest = fetched[0]
        total = int(
            db.scalar(
                select(func.count(Speech.id)).where(
                    *completed_scope,
                    or_(
                        Speech.created_at < newest.created_at,
                        and_(Speech.created_at == newest.created_at, Speech.id <= newest.id),
                    ),
                )
            )
            or 0
        )
    elif page == 1:
        total = 0
    else:
        total = int(db.scalar(select(func.count(Speech.id)).where(*completed_scope)) or 0)
    pages = max(1, (total + page_size - 1) // page_size)
    has_more = len(fetched) > page_size
    rows = list(reversed(fetched[:page_size]))
    next_cursor = (
        _encode_cursor(
            match_id,
            rows[0],
            next_page=current_page + 1,
            page_size=page_size,
            snapshot_total=total,
        )
        if has_more and rows
        else None
    )
    return SpeechPage(
        rows=rows,
        page=current_page,
        page_size=page_size,
        total=total,
        pages=pages,
        next_cursor=next_cursor,
        has_more=has_more,
    )
