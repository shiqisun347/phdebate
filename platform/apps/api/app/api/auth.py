from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from threading import Lock
from time import monotonic

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.deps import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    current_user,
    optional_user,
    request_session_token,
    verify_csrf,
)
from app.core.security import (
    expires_in_days,
    hash_password,
    new_token,
    password_needs_rehash,
    token_hash,
    validate_account,
    validate_password,
    validate_real_name,
    verify_password,
)
from app.models.entities import Room, RoomSeat, User, UserSession
from app.schemas.requests import ChangePasswordRequest, LoginRequest, RegisterRequest
from app.services.room_service import serialize_user

router = APIRouter(prefix="/api/auth", tags=["auth"])
DUMMY_PASSWORD_HASH = hash_password(new_token())
attempts: dict[str, deque[float]] = {}
attempts_lock = Lock()
last_attempt_cleanup = 0.0
MAX_ATTEMPT_KEYS = 10_000
LOGIN_IP_LIMIT = 120


def _serialized_user(db: Session, user: User) -> dict:
    del db
    return serialize_user(user)


def _active_room(db: Session, user: User) -> dict | None:
    row = db.execute(
        select(Room, RoomSeat)
        .join(RoomSeat, RoomSeat.room_id == Room.id)
        .where(
            RoomSeat.user_id == user.id,
            Room.status.in_(["lobby", "preparing", "running", "paused", "judging"]),
        )
        .order_by(Room.updated_at.desc(), Room.id.desc())
        .limit(1)
    ).first()
    if not row:
        return None
    room, seat = row
    return {
        "code": room.code,
        "topic": room.topic,
        "status": room.status,
        "seat_key": seat.seat_key,
    }


def _guard(key: str, *, limit: int = 12) -> None:
    global last_attempt_cleanup
    current = monotonic()
    cutoff = current - 300
    with attempts_lock:
        if current - last_attempt_cleanup >= 60:
            for stale_key, stale_bucket in list(attempts.items()):
                while stale_bucket and stale_bucket[0] < cutoff:
                    stale_bucket.popleft()
                if not stale_bucket:
                    attempts.pop(stale_key, None)
            last_attempt_cleanup = current
        bucket = attempts.get(key)
        if bucket is None:
            if len(attempts) >= MAX_ATTEMPT_KEYS:
                raise HTTPException(status_code=429, detail="登录保护记录已满，请稍后再试。")
            bucket = deque()
            attempts[key] = bucket
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= limit:
            raise HTTPException(status_code=429, detail="尝试次数过多，请稍后再试。")
        bucket.append(current)


def _clear_attempts(key: str) -> None:
    with attempts_lock:
        attempts.pop(key, None)


def _limit_user_sessions(db: Session, user_id: str, keep: int = 9) -> None:
    expired_ids = db.scalars(select(UserSession.id).where(UserSession.expires_at <= datetime.now(timezone.utc))).all()
    if expired_ids:
        db.execute(delete(UserSession).where(UserSession.id.in_(expired_ids)))
    overflow_ids = db.scalars(
        select(UserSession.id).where(UserSession.user_id == user_id).order_by(UserSession.created_at.desc()).offset(keep)
    ).all()
    if overflow_ids:
        db.execute(delete(UserSession).where(UserSession.id.in_(overflow_ids)))


def _create_session(db: Session, request: Request, response: Response, user: User) -> str:
    _limit_user_sessions(db, user.id)
    token = new_token()
    csrf = new_token(24)
    db.add(
        UserSession(
            user_id=user.id,
            token_hash=token_hash(token),
            csrf_hash=token_hash(csrf),
            user_agent=request.headers.get("user-agent", "")[:255],
            ip_address=(request.client.host if request.client else "")[:64],
            expires_at=expires_in_days(settings.session_days),
        )
    )
    user.last_login_at = datetime.now(timezone.utc)
    db.commit()
    cookie_common = dict(max_age=settings.session_days * 86400, secure=settings.cookie_secure, samesite="lax", path="/")
    response.set_cookie(SESSION_COOKIE, token, httponly=True, **cookie_common)
    response.set_cookie(CSRF_COOKIE, csrf, httponly=False, **cookie_common)
    return csrf


@router.post("/register")
def register(
    payload: RegisterRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    authenticated_user: User | None = Depends(optional_user),
) -> dict:
    if authenticated_user is not None:
        raise HTTPException(status_code=409, detail="你已登录，如需注册其他账号，请先退出当前账号。")
    _guard(f"register:{request.client.host if request.client else 'unknown'}", limit=60)
    try:
        account = validate_account(payload.account)
        real_name = validate_real_name(payload.real_name)
        validate_password(payload.password)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if db.scalar(select(User).where(User.account == account)):
        raise HTTPException(status_code=409, detail="该登录账号已被使用。")
    user = User(account=account, real_name=real_name, password_hash=hash_password(payload.password))
    db.add(user)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="该登录账号已被使用。") from exc
    csrf = _create_session(db, request, response, user)
    return {"user": _serialized_user(db, user), "csrf_token": csrf}


@router.post("/login")
def login(payload: LoginRequest, request: Request, response: Response, db: Session = Depends(get_db)) -> dict:
    normalized_account = payload.account.strip().lower()
    source = request.client.host if request.client else "unknown"
    _guard(f"login-ip:{source}", limit=LOGIN_IP_LIMIT)
    key = f"login:{source}:{normalized_account}"
    _guard(key)
    user = db.scalar(select(User).where(User.account == normalized_account))
    password_valid = verify_password(user.password_hash if user else DUMMY_PASSWORD_HASH, payload.password)
    if not user or not password_valid:
        raise HTTPException(status_code=401, detail="账号或密码错误。")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="该账号已停用。")
    if password_needs_rehash(user.password_hash):
        user.password_hash = hash_password(payload.password)
    _clear_attempts(key)
    csrf = _create_session(db, request, response, user)
    return {"user": _serialized_user(db, user), "csrf_token": csrf}


@router.post("/logout")
def logout(
    response: Response,
    request: Request,
    _user: User = Depends(verify_csrf),
    db: Session = Depends(get_db),
) -> dict:
    token = request_session_token(request)
    if token:
        db.execute(delete(UserSession).where(UserSession.token_hash == token_hash(token)))
        db.commit()
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    return {"ok": True}


@router.get("/session")
def session(
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    return {"user": _serialized_user(db, user)}


@router.get("/session-state")
def session_state(
    user: User | None = Depends(optional_user),
    db: Session = Depends(get_db),
) -> dict:
    """Return the optional navigation identity without logging a routine 401."""
    return {
        "user": _serialized_user(db, user) if user else None,
        "active_room": _active_room(db, user) if user else None,
    }


@router.post("/password")
def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    response: Response,
    user: User = Depends(verify_csrf),
    db: Session = Depends(get_db),
) -> dict:
    key = f"password:{user.id}"
    _guard(key)
    if not verify_password(user.password_hash, payload.current_password):
        raise HTTPException(status_code=400, detail="当前密码不正确。")
    try:
        validate_password(payload.new_password)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if verify_password(user.password_hash, payload.new_password):
        raise HTTPException(status_code=409, detail="新密码不能与当前密码相同。")

    user.password_hash = hash_password(payload.new_password)
    db.execute(delete(UserSession).where(UserSession.user_id == user.id))
    csrf = _create_session(db, request, response, user)
    _clear_attempts(key)
    return {"ok": True, "user": _serialized_user(db, user), "csrf_token": csrf, "other_sessions_revoked": True}


@router.post("/sessions/revoke-others")
def revoke_other_sessions(
    request: Request,
    user: User = Depends(verify_csrf),
    db: Session = Depends(get_db),
) -> dict:
    current_token = request_session_token(request)
    current_hash = token_hash(current_token) if current_token else ""
    result = db.execute(delete(UserSession).where(UserSession.user_id == user.id, UserSession.token_hash != current_hash))
    db.commit()
    return {"ok": True, "revoked": result.rowcount or 0}
