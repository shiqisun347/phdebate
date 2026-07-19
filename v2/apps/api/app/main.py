from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError

from app.api import admin, auth, media, public, realtime, rooms
from app.core.config import settings
from app.core.database import SessionLocal, TransactionLockTimeout, create_schema
from app.services.audio_upload_lease import SpeechAudioUploadLeaseMiddleware
from app.services.match_engine import match_engine
from app.services.providers import debate_agent, lighttts, moss_tts_realtime
from app.services.realtime import room_hub
from app.services.room_service import reset_connected_presence
from app.services.seed import seed_database

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_schema()
    with SessionLocal() as db:
        seed_database(db)
        if settings.presence_reset_on_startup:
            reset_connected_presence(db)
            db.commit()
    match_engine.start()
    if settings.moss_tts_realtime_enabled:
        moss_snapshot = await moss_tts_realtime.prewarm()
        if not moss_snapshot.get("ok"):
            logger.warning("MOSS-TTS-Realtime prewarm/readiness failed snapshot=%s", moss_snapshot)
    try:
        yield
    finally:
        await match_engine.stop()
        remaining_tts = await lighttts.drain_cancelled_jobs(settings.lighttts_shutdown_drain_seconds)
        if remaining_tts:
            logger.warning(
                "LightTTS shutdown drain reached limit pending=%s lease_ttl_seconds=%s",
                remaining_tts,
                settings.lighttts_global_gate_lease_seconds,
            )
        await debate_agent.aclose()
        await moss_tts_realtime.aclose()
        await room_hub.close()


app = FastAPI(title=settings.app_name, version="2.0.0", lifespan=lifespan)
app.add_middleware(SpeechAudioUploadLeaseMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(TransactionLockTimeout)
async def transaction_lock_timeout(_request: Request, _exc: TransactionLockTimeout) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": "房间正在处理其他操作，请稍后重试。"})


@app.exception_handler(OperationalError)
async def database_operational_error(_request: Request, exc: OperationalError) -> JSONResponse:
    sqlstate = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
    if sqlstate == "55P03":
        return JSONResponse(status_code=409, content={"detail": "房间操作等待超时，请刷新状态后重试。"})
    return JSONResponse(status_code=503, content={"detail": "数据库暂时不可用，请稍后重试。"})


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), geolocation=(), microphone=(self), payment=(), usb=()")
    response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    response.headers.setdefault("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'; base-uri 'none'")
    forwarded_proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    if settings.app_env == "production" and forwarded_proto == "https":
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


app.include_router(auth.router)
app.include_router(public.router)
app.include_router(rooms.router)
app.include_router(admin.router)
app.include_router(realtime.router)
app.include_router(media.router)
