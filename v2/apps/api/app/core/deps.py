from __future__ import annotations

from datetime import datetime, timezone

from app.core.database import get_db
from app.core.security import as_utc, token_hash
from app.models.entities import User, UserSession
from fastapi import Cookie, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

SESSION_COOKIE = "jixia_v2_session"
CSRF_COOKIE = "jixia_v2_csrf"


def optional_user(
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    db: Session = Depends(get_db),
) -> User | None:
    if not session_token:
        return None
    session = db.scalar(select(UserSession).where(UserSession.token_hash == token_hash(session_token)))
    if not session or as_utc(session.expires_at) <= datetime.now(timezone.utc):
        db.rollback()
        return None
    user = db.get(User, session.user_id)
    result = user if user and user.is_active else None
    # Authentication is resolved before FastAPI invokes an async endpoint and,
    # for multipart requests, may precede further body processing.  Leaving this
    # read transaction open pins one SQL connection per in-flight request.
    db.commit()
    return result


def current_user(user: User | None = Depends(optional_user)) -> User:
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录。")
    return user


def authenticated_session(
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    db: Session = Depends(get_db),
) -> UserSession:
    if not session_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录。")
    session = db.scalar(select(UserSession).where(UserSession.token_hash == token_hash(session_token)))
    if not session or as_utc(session.expires_at) <= datetime.now(timezone.utc):
        db.rollback()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录状态已失效，请重新登录。")
    return session


def system_admin(user: User = Depends(current_user)) -> User:
    if user.role != "system_admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="仅系统管理员可访问。")
    return user


def verify_csrf(
    request: Request,
    user: User = Depends(current_user),
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    csrf_cookie: str | None = Cookie(default=None, alias=CSRF_COOKIE),
    csrf_header: str | None = Header(default=None, alias="X-CSRF-Token"),
    db: Session = Depends(get_db),
) -> User:
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return user
    if not session_token or not csrf_cookie or not csrf_header or csrf_cookie != csrf_header:
        raise HTTPException(status_code=403, detail="CSRF 校验失败。")
    session = db.scalar(select(UserSession).where(UserSession.token_hash == token_hash(session_token)))
    if not session or session.csrf_hash != token_hash(csrf_header):
        db.rollback()
        raise HTTPException(status_code=403, detail="CSRF 校验失败。")
    db.commit()
    return user
