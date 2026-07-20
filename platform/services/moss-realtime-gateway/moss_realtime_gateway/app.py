from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from moss_realtime_gateway.backends import SynthesisBackend, build_backend
from moss_realtime_gateway.config import GatewaySettings
from moss_realtime_gateway.runtime import (
    GatewayError,
    GatewayNotReady,
    GatewayRuntime,
    SessionConflict,
    SessionMissing,
    SessionOrphaned,
)
from moss_realtime_gateway.websocket_protocol import add_websocket_protocol


class SessionStartRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)
    assistant_text: str = Field(default="", max_length=4_096)
    user_text: str | None = Field(default=None, max_length=2_000)
    prompt_audio: str = Field(min_length=1, max_length=256)
    user_audio: str | None = Field(default=None, max_length=256)
    new_turn: bool = True


class SessionPushRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)
    text: str = Field(default="", max_length=4_096)
    is_final: bool = False


class SessionControlRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)
    reason: str = ""


def create_app(
    settings: GatewaySettings | None = None,
    *,
    backend: SynthesisBackend | None = None,
    runtime_factory: Callable[[GatewaySettings, SynthesisBackend], GatewayRuntime] | None = None,
) -> FastAPI:
    configured = settings or GatewaySettings.from_env()
    selected_backend = backend or build_backend(configured)
    factory = runtime_factory or (lambda value, engine: GatewayRuntime(value, engine))
    runtime = factory(configured, selected_backend)

    @asynccontextmanager
    async def lifespan(_application: FastAPI) -> AsyncIterator[None]:
        await runtime.startup()
        yield
        await runtime.shutdown()

    application = FastAPI(title="PhDebate MOSS-TTS-Realtime Gateway", version="0.1.0", lifespan=lifespan)
    application.state.runtime = runtime
    add_websocket_protocol(application, runtime, configured)

    async def require_key(
        x_moss_gateway_key: Annotated[str | None, Header(alias="X-MOSS-Gateway-Key")] = None,
    ) -> None:
        if configured.api_key and not (
            x_moss_gateway_key and secrets.compare_digest(x_moss_gateway_key, configured.api_key)
        ):
            raise HTTPException(status_code=401, detail="gateway authentication failed")

    auth = Depends(require_key)

    @application.get("/health/live")
    async def health_live() -> dict[str, str]:
        return {"status": "live"}

    @application.get("/health/ready", dependencies=[auth])
    async def health_ready() -> JSONResponse:
        payload = runtime.health()
        return JSONResponse(payload, status_code=200 if runtime.ready else 503)

    @application.post("/tts/session/start", dependencies=[auth])
    async def session_start(request: SessionStartRequest) -> dict[str, Any]:
        if not request.new_turn:
            raise HTTPException(status_code=400, detail="new_turn must be true")
        if request.user_audio:
            raise HTTPException(status_code=400, detail="user_audio is not accepted by this fixed-prompt gateway")
        try:
            return await runtime.start_session(
                session_id=request.session_id,
                prompt_audio=request.prompt_audio,
                user_text=request.user_text or "请使用普通话进行自然、清晰、稳定的辩论发言。",
                assistant_text=request.assistant_text,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except SessionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (GatewayNotReady, SessionOrphaned) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except GatewayError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @application.post("/tts/session/push", dependencies=[auth])
    async def session_push(request: SessionPushRequest) -> dict[str, Any]:
        try:
            return await runtime.push_text(request.session_id, request.text, is_final=request.is_final)
        except SessionMissing as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except SessionOrphaned as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except GatewayError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @application.get("/tts/session/{session_id}/audio", dependencies=[auth])
    async def session_audio(session_id: str) -> StreamingResponse:
        try:
            session = runtime.claim_audio(session_id)
        except SessionMissing as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except SessionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        async def stream() -> AsyncIterator[bytes]:
            completed = False
            try:
                while True:
                    item = await runtime.audio_item(session)
                    if item is None:
                        completed = True
                        break
                    yield item
            finally:
                if not completed:
                    cleanup = asyncio.create_task(runtime.audio_disconnected(session))
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        pass

        return StreamingResponse(
            stream(),
            media_type="application/octet-stream",
            headers={
                "X-Audio-Sample-Rate": str(configured.sample_rate),
                "X-Audio-Channels": "1",
                "X-Audio-Codec": "pcm_s16le",
                "Cache-Control": "no-store",
            },
        )

    @application.post("/tts/session/close", dependencies=[auth])
    async def session_close(request: SessionControlRequest) -> dict[str, Any]:
        try:
            return await runtime.close_session(request.session_id)
        except SessionMissing as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except SessionOrphaned as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except GatewayError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @application.post("/tts/session/abort", dependencies=[auth])
    async def session_abort(request: SessionControlRequest) -> dict[str, Any]:
        try:
            return await runtime.abort_session(request.session_id, reason=request.reason or "client_abort")
        except SessionMissing as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except SessionOrphaned as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except GatewayError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return application


app = create_app()
