from __future__ import annotations

import fcntl
import hashlib
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from app.core.config import settings
from fastapi.responses import JSONResponse

ASGIApp = Callable[
    [dict[str, Any], Callable[[], Awaitable[dict[str, Any]]], Callable[[dict[str, Any]], Awaitable[None]]],
    Awaitable[None],
]

_AUDIO_UPLOAD_PATH = re.compile(r"^/api/rooms/[^/]+/speech/(?P<speech_id>[^/]+)/audio/?$")


class SpeechAudioUploadLeaseMiddleware:
    """Reject duplicate speech uploads before Starlette parses multipart data."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if scope.get("type") != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return
        match = _AUDIO_UPLOAD_PATH.fullmatch(str(scope.get("path", "")))
        if not match:
            await self.app(scope, receive, send)
            return

        lock_dir = settings.media_path.parent / ".audio-upload-locks"
        lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_name = hashlib.sha256(match.group("speech_id").encode("utf-8")).hexdigest()
        lock_path = Path(lock_dir) / f"{lock_name}.lock"
        with lock_path.open("a+b") as lock_file:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                response = JSONResponse(status_code=409, content={"detail": "该发言音频正在上传，请稍后重试。"})
                await response(scope, receive, send)
                return
            try:
                # Await the complete downstream ASGI exchange so the lease is
                # held through multipart parsing, handler work, and response
                # body delivery.
                await self.app(scope, receive, send)
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
