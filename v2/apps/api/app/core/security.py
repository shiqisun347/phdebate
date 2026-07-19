from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

password_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)
ACCOUNT_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{3,64}$")


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return password_hasher.verify(password_hash, password)
    except (InvalidHashError, VerificationError, VerifyMismatchError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    try:
        return password_hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


def validate_account(account: str) -> str:
    normalized = account.strip().lower()
    if not ACCOUNT_PATTERN.fullmatch(normalized):
        raise ValueError("账号须为 3–64 位字母、数字、点、下划线或短横线。")
    return normalized


def validate_real_name(real_name: str) -> str:
    normalized = " ".join(real_name.strip().split())
    if not 2 <= len(normalized) <= 64:
        raise ValueError("真实姓名长度须为 2–64 个字符。")
    return normalized


def validate_password(password: str) -> None:
    if len(password) < 8 or len(password) > 128:
        raise ValueError("密码长度须为 8–128 个字符。")


def new_token(size: int = 32) -> str:
    return secrets.token_urlsafe(size)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def expires_in_days(days: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=days)


def as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
