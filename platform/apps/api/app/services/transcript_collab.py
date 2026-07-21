from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timedelta, timezone

from app.models.entities import Room, Speech, User
from app.services.speech_correction import speech_owner_id
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

MAX_TOKEN_TTL_SECONDS = 300


def _secret() -> str:
    value = os.getenv("TRANSCRIPT_COLLAB_HMAC_SECRET", "")
    if len(value) < 32:
        raise HTTPException(status_code=503, detail="协同文字稿服务尚未配置。")
    return value


def _ttl_seconds() -> int:
    try:
        requested = int(os.getenv("TRANSCRIPT_COLLAB_TOKEN_TTL_SECONDS", str(MAX_TOKEN_TTL_SECONDS)))
    except ValueError:
        requested = MAX_TOKEN_TTL_SECONDS
    return max(30, min(MAX_TOKEN_TTL_SECONDS, requested))


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode_base64url(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def verify_transcript_collab_token(
    token: str,
    document_name: str,
    *,
    now_seconds: int | None = None,
) -> dict:
    """Compatibility verifier used by tests and diagnostics, not request auth."""

    try:
        payload, supplied_signature = token.split(".", 1)
        expected_signature = hmac.new(_secret().encode("utf-8"), payload.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(_decode_base64url(supplied_signature), expected_signature):
            raise ValueError("invalid signature")
        claims = json.loads(_decode_base64url(payload))
        if document_name != f"room:{claims['room_id']}":
            raise ValueError("document does not match room")
        current = now_seconds if now_seconds is not None else int(datetime.now(timezone.utc).timestamp())
        if int(claims["exp"]) <= current:
            raise ValueError("token expired")
        if int(claims["exp"]) - current > MAX_TOKEN_TTL_SECONDS:
            raise ValueError("token lifetime too long")
        return claims
    except (binascii.Error, KeyError, TypeError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("invalid transcript collaboration token") from exc


def _editable_speech_ids(db: Session, room: Room, user: User) -> list[str]:
    speeches = list(
        db.scalars(
            select(Speech)
            .where(
                Speech.room_id == room.id,
                Speech.speaker_type == "human",
                Speech.status == "completed",
            )
            .order_by(Speech.created_at, Speech.id)
        ).all()
    )
    if user.role == "system_admin":
        return [speech.id for speech in speeches]
    return [speech.id for speech in speeches if speech_owner_id(db, speech) == user.id]


def create_transcript_collab_token(db: Session, room: Room, user: User) -> dict:
    editable_speech_ids = _editable_speech_ids(db, room, user)
    issued_at = datetime.now(timezone.utc)
    expires_at = issued_at + timedelta(seconds=_ttl_seconds())
    role = "admin" if user.role == "system_admin" else "participant" if editable_speech_ids else "viewer"
    claims = {
        "room_id": room.id,
        "user_id": user.id,
        "role": role,
        "editable_speech_ids": editable_speech_ids,
        "exp": int(expires_at.timestamp()),
        "jti": secrets.token_urlsafe(18),
    }
    payload = _base64url(json.dumps(claims, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    signature = _base64url(hmac.new(_secret().encode("utf-8"), payload.encode("ascii"), hashlib.sha256).digest())
    return {
        "document_name": f"room:{room.id}",
        "token": f"{payload}.{signature}",
        "expires_at": expires_at.isoformat(),
        "ws_path": "/collab",
        "role": role,
        "editable_speech_ids": editable_speech_ids,
    }
