from __future__ import annotations

import base64
import hashlib

from app.core.config import settings
from cryptography.fernet import Fernet, InvalidToken


def _fernet() -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(settings.app_secret.encode("utf-8")).digest())
    return Fernet(key)


def encrypt_secret(value: str) -> str:
    normalized = value.strip()
    return _fernet().encrypt(normalized.encode("utf-8")).decode("ascii") if normalized else ""


def decrypt_secret(value: str) -> str:
    if not value:
        return ""
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError, ValueError):
        return ""
