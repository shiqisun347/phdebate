from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.models import AdminSession, AdminUser, GatewayKey, utcnow
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db

password_hasher = PasswordHasher()
SESSION_COOKIE = "debate_agent_session"
CSRF_COOKIE = "debate_agent_csrf"


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return password_hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def encrypt_secret(value: str) -> str:
    key = base64.urlsafe_b64encode(hashlib.sha256(settings.encryption_secret.encode("utf-8")).digest())
    return Fernet(key).encrypt(value.encode("utf-8")).decode("ascii") if value else ""


def decrypt_secret(value: str) -> str:
    if not value:
        return ""
    key = base64.urlsafe_b64encode(hashlib.sha256(settings.encryption_secret.encode("utf-8")).digest())
    try:
        return Fernet(key).decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError, ValueError):
        return ""


def create_session(db: Session, user: AdminUser) -> tuple[str, str, AdminSession]:
    token = secrets.token_urlsafe(48)
    csrf = secrets.token_urlsafe(32)
    session = AdminSession(
        user_id=user.id,
        token_hash=token_hash(token),
        csrf_hash=token_hash(csrf),
        expires_at=utcnow() + timedelta(hours=settings.session_hours),
    )
    db.add(session)
    db.flush()
    return token, csrf, session


def current_admin(request: Request, db: Session = Depends(get_db)) -> AdminUser:
    token = request.cookies.get(SESSION_COOKIE, "")
    session = db.scalar(select(AdminSession).where(AdminSession.token_hash == token_hash(token))) if token else None
    if not session or _aware(session.expires_at) <= utcnow():
        raise HTTPException(status_code=401, detail="管理员会话无效或已过期。")
    user = db.get(AdminUser, session.user_id)
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="管理员账号不可用。")
    session.last_seen_at = utcnow()
    return user


def verified_admin(
    request: Request,
    x_csrf_token: str = Header(default="", alias="X-CSRF-Token"),
    db: Session = Depends(get_db),
) -> AdminUser:
    user = current_admin(request, db)
    token = request.cookies.get(SESSION_COOKIE, "")
    session = db.scalar(select(AdminSession).where(AdminSession.token_hash == token_hash(token)))
    if not x_csrf_token or not session or not hmac.compare_digest(session.csrf_hash, token_hash(x_csrf_token)):
        raise HTTPException(status_code=403, detail="CSRF 校验失败。")
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") != settings.public_origin.rstrip("/"):
        raise HTTPException(status_code=403, detail="请求来源不允许。")
    return user


def gateway_key(
    x_debate_agent_key: str = Header(default="", alias="X-Debate-Agent-Key"),
    db: Session = Depends(get_db),
) -> GatewayKey:
    if not x_debate_agent_key:
        raise HTTPException(status_code=401, detail="缺少 Agent Gateway Key。")
    item = db.scalar(select(GatewayKey).where(GatewayKey.key_hash == token_hash(x_debate_agent_key), GatewayKey.is_active.is_(True)))
    if not item or (item.expires_at and _aware(item.expires_at) <= utcnow()):
        raise HTTPException(status_code=401, detail="Agent Gateway Key 无效或已撤销。")
    item.last_used_at = utcnow()
    return item
