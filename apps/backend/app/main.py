from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional

# Load project-root .env (if present) before any service reads os.environ, so local
# dev picks up DASHSCOPE_API_KEY / XFYUN_* keys without exporting them manually.
# Already-set environment variables (e.g. systemd Environment=) take precedence.
# Skipped under pytest so the test environment stays hermetic.
import sys as _sys

if "pytest" not in _sys.modules:
    try:
        from dotenv import load_dotenv

        load_dotenv(Path(__file__).resolve().parents[3] / ".env", override=False)
    except ImportError:
        pass

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.auth import (
    Principal,
    authorize_speaker_or_host,
    authorize_websocket,
    hash_token,
    require_admin,
    require_host,
    require_read_access,
    require_speaker_or_host,
    runtime_auth_status,
    update_runtime_auth_config,
)
from app.services.match_store import MatchStateError, store
from app.services.preflight_report import build_preflight_report
from app.services.speech_diagnostics import build_speech_diagnostics
from app.services.ruleset_store import ruleset_store, generate_flow, FLOW_TEMPLATE
from app.services.xiaoqi_store import xiaoqi_store, COMMANDS as XIAOQI_COMMANDS


_timer_task: Optional[asyncio.Task] = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await start_timer_loop()
    try:
        yield
    finally:
        await stop_timer_loop()


app = FastAPI(title="Phdebate API", version="0.1.0", lifespan=lifespan)
PROJECT_ROOT = Path(__file__).resolve().parents[3]
FRONTEND_DIST = PROJECT_ROOT / "apps" / "frontend" / "dist"
FRONTEND_INDEX = FRONTEND_DIST / "index.html"
FRONTEND_ASSETS = FRONTEND_DIST / "assets"

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(MatchStateError)
async def match_state_error_handler(_request, exc: MatchStateError) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "ok": False,
            "error": {
                "code": exc.code,
                "message": exc.message,
                "details": exc.details,
            },
        },
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail
    if isinstance(detail, dict) and "code" in detail:
        error = {
            "code": detail.get("code", "http_error"),
            "message": detail.get("message", str(exc.detail)),
            "details": detail.get("details", {}),
        }
    else:
        error = {
            "code": "not_found" if exc.status_code == 404 else "http_error",
            "message": str(detail),
            "details": {},
        }
    return JSONResponse(status_code=exc.status_code, content={"ok": False, "error": error})


@app.get("/api/health")
async def health() -> Dict[str, Any]:
    return {"ok": True, "service": "phdebate-api", "version": "0.1.0"}


async def start_timer_loop() -> None:
    global _timer_task
    if _timer_task is None or _timer_task.done():
        _timer_task = asyncio.create_task(_timer_loop())


async def stop_timer_loop() -> None:
    global _timer_task
    if _timer_task is None:
        return
    _timer_task.cancel()
    try:
        await _timer_task
    except asyncio.CancelledError:
        pass
    _timer_task = None


async def _timer_loop() -> None:
    while True:
        try:
            await store.tick_timers()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        await asyncio.sleep(0.35)


@app.post("/api/demo/reset")
async def reset_demo(_principal: Principal = Depends(require_admin)) -> Dict[str, Any]:
    store.reset_demo()
    await store.emit("match.updated", {"reason": "demo_reset"})
    return {"ok": True, "data": await store.get_snapshot()}


@app.post("/api/matches/{match_id}/reset")
async def reset_current_match(match_id: str, body: Dict[str, Any], _principal: Principal = Depends(require_admin)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    data = await store.reset_current_match(str(body.get("confirm_text", "")))
    return {"ok": True, "data": data}


@app.get("/api/matches")
async def list_matches(_principal: Principal = Depends(require_read_access)) -> Dict[str, Any]:
    return {"ok": True, "data": await store.list_matches()}


@app.post("/api/matches")
async def create_match(body: Optional[Dict[str, Any]] = None, _principal: Principal = Depends(require_admin)) -> Dict[str, Any]:
    snapshot = await store.create_match(body or {})
    return {"ok": True, "data": {"match_id": snapshot["match"]["id"], "status": snapshot["match"]["status"]}}


@app.post("/api/matches/{match_id}/switch")
async def switch_match(match_id: str, _principal: Principal = Depends(require_admin)) -> Dict[str, Any]:
    return {"ok": True, "data": await store.switch_match(match_id)}


@app.delete("/api/matches/{match_id}")
async def delete_match(match_id: str, _principal: Principal = Depends(require_admin)) -> Dict[str, Any]:
    return {"ok": True, "data": await store.delete_match(match_id)}


@app.get("/api/matches/{match_id}")
async def get_match(match_id: str, _principal: Principal = Depends(require_read_access)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    snapshot = await store.get_snapshot()
    return {"ok": True, "data": snapshot}


@app.get("/api/current-match")
async def get_current_match(_principal: Principal = Depends(require_read_access)) -> Dict[str, Any]:
    snapshot = await store.get_snapshot()
    return {
        "ok": True,
        "data": {
            "id": snapshot["match"]["id"],
            "title": snapshot["match"]["title"],
            "topic": snapshot["match"]["topic"],
            "status": snapshot["match"]["status"],
            "screen_scene": snapshot["match"].get("screen_scene", "idle"),
            "current_phase_id": snapshot["match"].get("current_phase_id"),
        },
    }


@app.get("/api/public/matches/{match_id}/vote-options")
async def get_public_vote_options(match_id: str) -> Dict[str, Any]:
    await _ensure_match(match_id)
    snapshot = await store.get_snapshot()
    return {
        "ok": True,
        "data": {
            "match": {
                "id": snapshot["match"]["id"],
                "title": snapshot["match"]["title"],
                "topic": snapshot["match"]["topic"],
                "status": snapshot["match"]["status"],
            },
            "teams": [
                {
                    "id": team["id"],
                    "side": team["side"],
                    "name": team["name"],
                    "position": team["position"],
                }
                for team in snapshot["teams"]
                if team["side"] in {"affirmative", "negative"}
            ],
            "speakers": [
                {
                    "id": speaker["id"],
                    "side": speaker["side"],
                    "seat": speaker["seat"],
                    "name": speaker["name"],
                    "speaker_type": speaker["speaker_type"],
                }
                for speaker in snapshot["speakers"]
                if speaker["side"] in {"affirmative", "negative"}
            ],
            "vote_state": {
                "window_status": snapshot["vote_state"]["window_status"],
                "audience_count": snapshot["vote_state"]["audience_count"],
                "judge_published": snapshot["vote_state"]["judge_published"],
                "audience_published": snapshot["vote_state"]["audience_published"],
            },
        },
    }


@app.get("/api/matches/{match_id}/audit-logs")
async def get_audit_logs(match_id: str, limit: int = 30, _principal: Principal = Depends(require_host)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    return {"ok": True, "data": {"items": await store.get_audit_logs(limit)}}


@app.get("/api/matches/{match_id}/data-summary")
async def get_data_summary(match_id: str, _principal: Principal = Depends(require_host)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    return {"ok": True, "data": await store.get_data_summary()}


@app.get("/api/matches/{match_id}/preflight-report")
async def get_preflight_report(match_id: str, _principal: Principal = Depends(require_host)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    snapshot = await store.get_snapshot()
    diagnostics = build_speech_diagnostics(store.audio_root_path())
    return {"ok": True, "data": build_preflight_report(snapshot, diagnostics)}


@app.get("/api/admin/security/auth")
async def get_security_auth(_principal: Principal = Depends(require_admin)) -> Dict[str, Any]:
    return {"ok": True, "data": runtime_auth_status()}


@app.put("/api/admin/security/auth")
async def put_security_auth(body: Dict[str, Any], principal: Principal = Depends(require_admin)) -> Dict[str, Any]:
    if "auth_required" not in body:
        raise HTTPException(status_code=400, detail="auth_required is required")
    token_hashes = _token_hashes_from_security_body(body)
    status = update_runtime_auth_config(
        bool(body.get("auth_required")),
        token_hashes,
        updated_by=principal.actor_id or principal.role,
    )
    await store.emit(
        "security.auth_updated",
        {
            "auth_required": status["auth_required"],
            "runtime_configured": status["runtime_configured"],
            "reason": body.get("reason", "admin_security_update"),
        },
        principal.actor_type,
        principal.actor_id,
    )
    return {"ok": True, "data": status}


# ---------------------------------------------------------------------------
# 赛制规则管理（全局）
# ---------------------------------------------------------------------------
@app.get("/api/admin/rulesets")
async def list_rulesets(_principal: Principal = Depends(require_read_access)) -> Dict[str, Any]:
    return {"ok": True, "data": {"rulesets": ruleset_store.list(), "flow_template": FLOW_TEMPLATE}}


@app.post("/api/admin/rulesets")
async def create_ruleset(body: Dict[str, Any], _principal: Principal = Depends(require_admin)) -> Dict[str, Any]:
    try:
        return {"ok": True, "data": ruleset_store.create(body or {})}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.patch("/api/admin/rulesets/{ruleset_id}")
async def update_ruleset(ruleset_id: str, body: Dict[str, Any], _principal: Principal = Depends(require_admin)) -> Dict[str, Any]:
    try:
        return {"ok": True, "data": ruleset_store.update(ruleset_id, body or {})}
    except KeyError:
        raise HTTPException(status_code=404, detail="ruleset not found")


@app.delete("/api/admin/rulesets/{ruleset_id}")
async def delete_ruleset(ruleset_id: str, _principal: Principal = Depends(require_admin)) -> Dict[str, Any]:
    try:
        ruleset_store.delete(ruleset_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="ruleset not found")
    return {"ok": True, "data": {"rulesets": ruleset_store.list()}}


@app.post("/api/admin/rulesets/generate-flow")
async def generate_ruleset_flow(body: Dict[str, Any], _principal: Principal = Depends(require_admin)) -> Dict[str, Any]:
    template = str((body or {}).get("template") or "")
    use_ai = bool((body or {}).get("use_ai", True))
    return {"ok": True, "data": generate_flow(template, use_ai=use_ai)}


# ---------------------------------------------------------------------------
# 小七管理（全局）
# ---------------------------------------------------------------------------
@app.get("/api/admin/xiaoqi")
async def get_xiaoqi(_principal: Principal = Depends(require_read_access)) -> Dict[str, Any]:
    return {"ok": True, "data": xiaoqi_store.public()}


@app.put("/api/admin/xiaoqi")
async def update_xiaoqi(body: Dict[str, Any], _principal: Principal = Depends(require_admin)) -> Dict[str, Any]:
    return {"ok": True, "data": xiaoqi_store.update(body or {})}


@app.post("/api/admin/xiaoqi/command")
async def send_xiaoqi_command(body: Dict[str, Any], _principal: Principal = Depends(require_host)) -> Dict[str, Any]:
    command = str((body or {}).get("command") or "")
    if command not in XIAOQI_COMMANDS:
        raise HTTPException(status_code=400, detail=f"command must be one of {XIAOQI_COMMANDS}")
    result = await xiaoqi_store.send(
        command,
        question=str((body or {}).get("question") or ""),
        context=(body or {}).get("context") if isinstance((body or {}).get("context"), dict) else None,
    )
    await store.emit("xiaoqi.command_sent", {"command": command, "sent": result.get("sent", False)}, _principal.actor_type, _principal.actor_id)
    return {"ok": True, "data": result}


@app.post("/api/matches/{match_id}/exports")
async def create_export(match_id: str, _principal: Principal = Depends(require_host)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    return {"ok": True, "data": await store.create_export_bundle()}


@app.get("/api/matches/{match_id}/exports/{export_id}/download")
async def download_export(match_id: str, export_id: str, _principal: Principal = Depends(require_host)) -> FileResponse:
    path = await store.export_file_path(export_id, match_id)
    return FileResponse(path, media_type="application/zip", filename=path.name)


@app.patch("/api/matches/{match_id}")
async def update_match(match_id: str, body: Dict[str, Any], _principal: Principal = Depends(require_admin)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    await store.update_match(body)
    return {"ok": True, "data": await store.get_snapshot()}


@app.patch("/api/matches/{match_id}/teams/{team_id}")
async def update_team(match_id: str, team_id: str, body: Dict[str, Any], _principal: Principal = Depends(require_admin)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    await store.update_team(team_id, body)
    return {"ok": True, "data": await store.get_snapshot()}


@app.patch("/api/matches/{match_id}/speakers/{speaker_id}")
async def update_speaker(match_id: str, speaker_id: str, body: Dict[str, Any], _principal: Principal = Depends(require_admin)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    await store.update_speaker(speaker_id, body)
    return {"ok": True, "data": await store.get_snapshot()}


@app.patch("/api/matches/{match_id}/speakers/{speaker_id}/profile")
async def update_speaker_profile(match_id: str, speaker_id: str, body: Dict[str, Any], principal: Principal = Depends(require_speaker_or_host)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    await store.update_speaker_profile(
        speaker_id,
        {"name": body.get("name", "")},
        principal.actor_type,
        principal.actor_id,
    )
    return {"ok": True, "data": await store.get_snapshot()}


@app.patch("/api/matches/{match_id}/phases/{phase_id}")
async def update_phase(match_id: str, phase_id: str, body: Dict[str, Any], _principal: Principal = Depends(require_admin)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    await store.update_phase(phase_id, body)
    return {"ok": True, "data": await store.get_snapshot()}


@app.post("/api/matches/{match_id}/start")
async def start_match(match_id: str, _principal: Principal = Depends(require_host)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    await store.set_match_status("running")
    return {"ok": True, "data": await store.get_snapshot()}


@app.post("/api/matches/{match_id}/begin")
async def begin_match(match_id: str, _principal: Principal = Depends(require_host)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    await store.set_match_status("running")
    return {"ok": True, "data": await store.get_snapshot()}


@app.post("/api/matches/{match_id}/pause")
async def pause_match(match_id: str, _principal: Principal = Depends(require_host)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    await store.set_match_status("paused")
    return {"ok": True, "data": await store.get_snapshot()}


@app.post("/api/matches/{match_id}/resume")
async def resume_match(match_id: str, _principal: Principal = Depends(require_host)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    await store.set_match_status("running")
    return {"ok": True, "data": await store.get_snapshot()}


@app.post("/api/matches/{match_id}/finish")
async def finish_match(match_id: str, _principal: Principal = Depends(require_host)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    await store.set_match_status("finished")
    return {"ok": True, "data": await store.get_snapshot()}


@app.put("/api/matches/{match_id}/audio-output")
async def update_audio_output(match_id: str, body: Dict[str, Any], principal: Principal = Depends(require_host)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    actor_type = principal.actor_type if principal.actor_type in {"host", "admin"} else "host"
    await store.set_audio_output(
        str(body.get("mode", "host")),
        str(body.get("reason", "manual")),
        actor_type,
    )
    return {"ok": True, "data": await store.get_snapshot()}


@app.post("/api/matches/{match_id}/emergency-stop")
async def emergency_stop(match_id: str, body: Optional[Dict[str, Any]] = None, _principal: Principal = Depends(require_admin)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    await store.set_match_status("intervention")
    await store.emit("match.emergency_stopped", {"reason": (body or {}).get("reason", "manual")}, "host")
    return {"ok": True, "data": await store.get_snapshot()}


@app.post("/api/matches/{match_id}/screen/scene")
async def set_screen_scene(match_id: str, body: Dict[str, Any], _principal: Principal = Depends(require_host)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    await store.set_screen_scene(body.get("scene", "live"), body.get("live_mode"))
    return {"ok": True, "data": await store.get_snapshot()}


@app.post("/api/matches/{match_id}/phases/{phase_id}/start")
async def start_phase(match_id: str, phase_id: str, _principal: Principal = Depends(require_host)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    await store.start_phase(phase_id)
    snapshot = await store.get_snapshot()
    phase = next((p for p in snapshot["phases"] if p["id"] == phase_id), None)
    _auto_trigger_agent_speech_for_phase(phase, snapshot["speakers"])
    return {"ok": True, "data": snapshot}


@app.post("/api/matches/{match_id}/phases/next")
async def start_next_phase(match_id: str, _principal: Principal = Depends(require_host)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    snapshot = await store.get_snapshot()
    phases = sorted(snapshot["phases"], key=lambda item: item["display_order"])
    current = next((item for item in phases if item["id"] == snapshot["match"]["current_phase_id"]), None)
    if not current:
        raise HTTPException(status_code=409, detail="current phase not found")
    next_phase = next((item for item in phases if item["display_order"] > current["display_order"]), None)
    if not next_phase:
        await store.set_match_status("finished")
        await store.emit("phase.next_started", {"phase_id": None, "finished": True}, "host")
        return {"ok": True, "data": await store.get_snapshot()}
    await store.start_phase(next_phase["id"])
    await store.emit(
        "phase.next_started",
        {"phase_id": next_phase["id"], "previous_phase_id": current["id"], "name": next_phase["name"]},
        "host",
    )
    snapshot = await store.get_snapshot()
    _auto_trigger_agent_speech_for_phase(next_phase, snapshot["speakers"])
    return {"ok": True, "data": snapshot}


@app.post("/api/matches/{match_id}/phases/{phase_id}/skip")
async def skip_phase(match_id: str, phase_id: str, body: Optional[Dict[str, Any]] = None, _principal: Principal = Depends(require_admin)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    await store.skip_phase(phase_id, (body or {}).get("reason", "manual_skip"))
    return {"ok": True, "data": await store.get_snapshot()}


@app.post("/api/matches/{match_id}/phases/{phase_id}/rollback")
async def rollback_phase(match_id: str, phase_id: str, body: Optional[Dict[str, Any]] = None, _principal: Principal = Depends(require_admin)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    await store.rollback_phase(phase_id, (body or {}).get("reason", "manual_rollback"))
    return {"ok": True, "data": await store.get_snapshot()}


@app.post("/api/matches/{match_id}/clocks/{clock_name}/pause")
async def pause_clock(match_id: str, clock_name: str, body: Optional[Dict[str, Any]] = None, _principal: Principal = Depends(require_host)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    await store.pause_clock(clock_name, (body or {}).get("reason", "manual"))
    return {"ok": True, "data": await store.get_snapshot()}


@app.post("/api/matches/{match_id}/clocks/{clock_name}/resume")
async def resume_clock(match_id: str, clock_name: str, body: Optional[Dict[str, Any]] = None, _principal: Principal = Depends(require_host)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    await store.resume_clock(clock_name, (body or {}).get("reason", "manual"))
    return {"ok": True, "data": await store.get_snapshot()}


@app.post("/api/matches/{match_id}/clocks/{clock_name}/adjust")
async def adjust_clock(match_id: str, clock_name: str, body: Dict[str, Any], _principal: Principal = Depends(require_host)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    await store.adjust_clock(clock_name, int(body.get("remaining_ms", 0)), body.get("reason", "manual"))
    return {"ok": True, "data": await store.get_snapshot()}


@app.post("/api/matches/{match_id}/speakers/{speaker_id}/activate")
async def activate_speaker(match_id: str, speaker_id: str, _principal: Principal = Depends(require_admin)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    await store.activate_speaker(speaker_id)
    snapshot = await store.get_snapshot()
    activated = next((s for s in snapshot["speakers"] if s["id"] == speaker_id), None)
    if activated and activated.get("speaker_type") == "agent":
        try:
            store.ensure_agent_speaker_for_current_phase(speaker_id)
            asyncio.create_task(store.run_agent_speech(speaker_id))
        except Exception:
            pass
    return {"ok": True, "data": snapshot}


@app.post("/api/matches/{match_id}/speakers/{speaker_id}/start-speaking")
async def start_speaking(match_id: str, speaker_id: str, _principal: Principal = Depends(require_speaker_or_host)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    await store.start_speaking(speaker_id)
    return {"ok": True, "data": await store.get_snapshot()}


@app.post("/api/matches/{match_id}/speakers/{speaker_id}/start-agent-speaking")
async def start_agent_speaking(match_id: str, speaker_id: str, _principal: Principal = Depends(require_speaker_or_host)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    store.ensure_agent_speaker_for_current_phase(speaker_id)
    asyncio.create_task(store.run_agent_speech(speaker_id))
    await store.emit("agent.speech.requested", {"speaker_id": speaker_id}, "speaker", speaker_id)
    return {"ok": True, "data": await store.get_snapshot()}


@app.post("/api/matches/{match_id}/speakers/{speaker_id}/free-debate-skip")
async def free_debate_skip(match_id: str, speaker_id: str, _principal: Principal = Depends(require_speaker_or_host)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    return {"ok": True, "data": await store.record_free_debate_skip(speaker_id)}


@app.post("/api/matches/{match_id}/speakers/{speaker_id}/stop-speaking")
async def stop_speaking(match_id: str, speaker_id: str, _principal: Principal = Depends(require_speaker_or_host)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    await store.stop_speaking(speaker_id)
    return {"ok": True, "data": await store.get_snapshot()}


@app.post("/api/matches/{match_id}/speakers/{speaker_id}/pause-speaking")
async def pause_speaking(match_id: str, speaker_id: str, body: Optional[Dict[str, Any]] = None, _principal: Principal = Depends(require_speaker_or_host)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    await store.pause_speaking(speaker_id, (body or {}).get("reason", "manual"))
    return {"ok": True, "data": await store.get_snapshot()}


@app.post("/api/matches/{match_id}/speakers/{speaker_id}/resume-speaking")
async def resume_speaking(match_id: str, speaker_id: str, body: Optional[Dict[str, Any]] = None, _principal: Principal = Depends(require_speaker_or_host)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    await store.resume_speaking(speaker_id, (body or {}).get("reason", "manual"))
    return {"ok": True, "data": await store.get_snapshot()}


@app.post("/api/matches/{match_id}/speeches/current/stop")
async def stop_current_speech(match_id: str, body: Optional[Dict[str, Any]] = None, _principal: Principal = Depends(require_host)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    snapshot = await store.get_snapshot()
    current = snapshot.get("current_speech")
    if not current:
        raise HTTPException(status_code=409, detail="no active speech")
    speaker_id = current["speaker_id"]
    await store.stop_speaking(speaker_id)
    await store.emit(
        "speech.force_stopped",
        {"speaker_id": speaker_id, "reason": (body or {}).get("reason", "host_force_stop")},
        "host",
    )
    return {"ok": True, "data": await store.get_snapshot()}


@app.post("/api/matches/{match_id}/speeches/current/reset")
async def reset_current_speech(match_id: str, body: Optional[Dict[str, Any]] = None, _principal: Principal = Depends(require_host)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    await store.reset_current_speech((body or {}).get("reason", "host_reset_current_speech"))
    return {"ok": True, "data": await store.get_snapshot()}


@app.post("/api/matches/{match_id}/flow/confirm")
async def confirm_flow(match_id: str, body: Optional[Dict[str, Any]] = None, _principal: Principal = Depends(require_host)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    await store.confirm_flow((body or {}).get("reason", "host_confirm"))
    return {"ok": True, "data": await store.get_snapshot()}


@app.post("/api/matches/{match_id}/bell")
async def trigger_bell(match_id: str, body: Optional[Dict[str, Any]] = None, _principal: Principal = Depends(require_host)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    payload = body or {}
    snapshot = await store.get_snapshot()
    audio_output = snapshot.get("audio_output", {})
    await store.emit(
        "clock.bell_triggered",
        {
            "kind": payload.get("kind", "manual"),
            "label": payload.get("label", "手动铃声"),
            "duration_ms": int(payload.get("duration_ms", 800)),
            "audio_output_mode": audio_output.get("mode", "host"),
            "audio_output_label": audio_output.get("label", "主持导播台电脑"),
        },
        "host",
    )
    return {"ok": True, "data": await store.get_snapshot()}


@app.post("/api/matches/{match_id}/speakers/{speaker_id}/asr/partial")
async def asr_partial(match_id: str, speaker_id: str, body: Dict[str, Any], _principal: Principal = Depends(require_speaker_or_host)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    await store.record_asr_partial(speaker_id, body.get("text", ""), body.get("latency_ms"))
    return {"ok": True, "data": await store.get_snapshot()}


@app.post("/api/matches/{match_id}/speakers/{speaker_id}/asr/final")
async def asr_final(match_id: str, speaker_id: str, body: Dict[str, Any], _principal: Principal = Depends(require_speaker_or_host)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    await store.record_asr_final(speaker_id, body.get("text", ""), body.get("latency_ms"))
    return {"ok": True, "data": await store.get_snapshot()}


@app.post("/api/matches/{match_id}/speakers/{speaker_id}/asr/fail")
async def asr_fail(match_id: str, speaker_id: str, body: Optional[Dict[str, Any]] = None, _principal: Principal = Depends(require_speaker_or_host)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    await store.record_asr_failed(speaker_id, (body or {}).get("reason", "asr_failed"))
    return {"ok": True, "data": await store.get_snapshot()}


@app.post("/api/matches/{match_id}/speakers/{speaker_id}/tts/fail")
async def tts_fail(match_id: str, speaker_id: str, body: Optional[Dict[str, Any]] = None, _principal: Principal = Depends(require_speaker_or_host)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    await store.record_tts_failed(
        speaker_id,
        (body or {}).get("reason", "tts_failed"),
        (body or {}).get("text_only", True),
    )
    return {"ok": True, "data": await store.get_snapshot()}


@app.get("/api/matches/{match_id}/integration-config")
async def get_integration_config(match_id: str, _principal: Principal = Depends(require_host)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    return {"ok": True, "data": await store.get_integration_config()}


@app.patch("/api/matches/{match_id}/integration-config")
async def patch_integration_config(match_id: str, body: Optional[Dict[str, Any]] = None, _principal: Principal = Depends(require_admin)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    return {"ok": True, "data": await store.update_integration_config(body or {})}


@app.get("/api/matches/{match_id}/speech/diagnostics")
async def speech_diagnostics(match_id: str, _principal: Principal = Depends(require_host)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    return {"ok": True, "data": build_speech_diagnostics(store.audio_root_path())}


@app.post("/api/matches/{match_id}/speech/tts/probe")
async def probe_tts(match_id: str, body: Optional[Dict[str, Any]] = None, _principal: Principal = Depends(require_host)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    return {"ok": True, "data": await store.probe_tts((body or {}).get("text", ""))}


@app.post("/api/matches/{match_id}/speech/asr/probe")
async def probe_asr(match_id: str, body: Optional[Dict[str, Any]] = None, _principal: Principal = Depends(require_host)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    payload = body or {}
    audio_base64 = str(payload.get("audio_base64") or "")
    audio = base64.b64decode(audio_base64) if audio_base64 else b""
    return {
        "ok": True,
        "data": await store.probe_asr(
            audio,
            str(payload.get("format") or "audio/L16;rate=16000"),
            str(payload.get("encoding") or "raw"),
        ),
    }


@app.post("/api/matches/{match_id}/speeches/{speech_id}/asr/recognize")
async def recognize_speech_audio(match_id: str, speech_id: str, _principal: Principal = Depends(require_host)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    return {"ok": True, "data": await store.recognize_audio_archive(speech_id)}


@app.patch("/api/matches/{match_id}/speeches/{speech_id}")
async def patch_speech(match_id: str, speech_id: str, body: Dict[str, Any], _principal: Principal = Depends(require_host)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    await store.patch_speech(speech_id, body)
    return {"ok": True, "data": await store.get_snapshot()}


@app.post("/api/matches/{match_id}/speeches/{speech_id}/audio-chunks")
async def upload_speech_audio_chunk(
    match_id: str,
    speech_id: str,
    request: Request,
    speaker_id: str = Form(...),
    chunk_index: int = Form(...),
    duration_ms: Optional[int] = Form(None),
    file: UploadFile = File(...),
) -> Dict[str, Any]:
    await _ensure_match(match_id)
    authorize_speaker_or_host(request, speaker_id)
    content = await file.read()
    await store.record_audio_chunk(
        speech_id=speech_id,
        speaker_id=speaker_id,
        chunk_index=chunk_index,
        content=content,
        mime_type=file.content_type or "application/octet-stream",
        duration_ms=duration_ms,
    )
    return {"ok": True, "data": await store.get_snapshot()}


@app.post("/api/matches/{match_id}/speeches/{speech_id}/audio/complete")
async def complete_speech_audio(match_id: str, speech_id: str, request: Request, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    await _ensure_match(match_id)
    payload = body or {}
    speaker_id = payload.get("speaker_id")
    if speaker_id:
        authorize_speaker_or_host(request, speaker_id)
    else:
        require_host(request)
    await store.complete_audio_archive(speech_id, speaker_id)
    if payload.get("auto_recognize") is True:
        result = await store.recognize_audio_archive(speech_id)
        return {"ok": True, "data": result["snapshot"]}
    if await store.should_auto_recognize_audio_archive(speech_id):
        asyncio.create_task(store.auto_recognize_audio_archive(speech_id))
    return {"ok": True, "data": await store.get_snapshot()}


@app.post("/api/matches/{match_id}/speakers/{speaker_id}/request-ai-teammate")
async def request_ai_teammate(match_id: str, speaker_id: str, body: Dict[str, Any], _principal: Principal = Depends(require_speaker_or_host)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    raise MatchStateError(
        "feature_deferred",
        "自由辩论请求 AI 队友发言已暂缓；当前版本只允许赛制授权席位在对应辩手端自行开始发言。",
        {"speaker_id": speaker_id, "agent_speaker_id": body.get("agent_speaker_id")},
    )


@app.post("/api/matches/{match_id}/agent/{speaker_id}/retry")
async def retry_agent(match_id: str, speaker_id: str, _principal: Principal = Depends(require_host)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    store.ensure_agent_speaker_for_current_phase(speaker_id)
    asyncio.create_task(store.run_agent_speech(speaker_id))
    return {"ok": True, "data": await store.get_snapshot()}


@app.post("/api/matches/{match_id}/agent/{speaker_id}/health")
async def check_agent_health(match_id: str, speaker_id: str, _principal: Principal = Depends(require_host)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    result = await store.check_agent_health(speaker_id)
    return {"ok": True, "data": {"result": result, "snapshot": await store.get_snapshot()}}


@app.post("/api/matches/{match_id}/agents/health")
async def check_all_agent_health(match_id: str, _principal: Principal = Depends(require_host)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    results = await store.check_all_agent_health()
    return {"ok": True, "data": {"results": results, "snapshot": await store.get_snapshot()}}


@app.post("/api/matches/{match_id}/agents/configs")
async def create_agent_config(match_id: str, body: Dict[str, Any], _principal: Principal = Depends(require_admin)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    await store.create_agent_config(body)
    return {"ok": True, "data": await store.get_snapshot()}


@app.patch("/api/matches/{match_id}/agents/configs/{agent_config_id}")
async def update_agent_config(match_id: str, agent_config_id: str, body: Dict[str, Any], _principal: Principal = Depends(require_admin)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    await store.update_agent_config(agent_config_id, body)
    return {"ok": True, "data": await store.get_snapshot()}


@app.delete("/api/matches/{match_id}/agents/configs/{agent_config_id}")
async def delete_agent_config(match_id: str, agent_config_id: str, _principal: Principal = Depends(require_admin)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    await store.delete_agent_config(agent_config_id)
    return {"ok": True, "data": await store.get_snapshot()}


@app.post("/api/matches/{match_id}/agents/configs/{agent_config_id}/test")
async def test_agent_config(match_id: str, agent_config_id: str, body: Optional[Dict[str, Any]] = None, _principal: Principal = Depends(require_host)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    custom = (body or {}).get("payload") if isinstance((body or {}).get("payload"), dict) else None
    result = await store.test_agent_config(agent_config_id, custom)
    return {"ok": True, "data": result}


@app.post("/api/matches/{match_id}/agents/configs/test-inline")
async def test_agent_config_inline(match_id: str, body: Dict[str, Any], _principal: Principal = Depends(require_host)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    config = (body or {}).get("config") if isinstance((body or {}).get("config"), dict) else (body or {})
    custom = (body or {}).get("payload") if isinstance((body or {}).get("payload"), dict) else None
    result = await store.test_agent_config_inline(config, custom)
    return {"ok": True, "data": result}


@app.get("/api/matches/{match_id}/logs")
async def get_request_logs(match_id: str, limit: int = 200, _principal: Principal = Depends(require_read_access)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    return {"ok": True, "data": store.get_request_logs(limit)}


@app.delete("/api/matches/{match_id}/logs")
async def clear_request_logs(match_id: str, _principal: Principal = Depends(require_admin)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    store.clear_request_logs()
    return {"ok": True, "data": store.get_request_logs(50)}


@app.post("/api/matches/{match_id}/agent/{speaker_id}/interrupt")
async def interrupt_agent(match_id: str, speaker_id: str, body: Optional[Dict[str, Any]] = None, _principal: Principal = Depends(require_host)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    await store.emit("agent.interrupted", {"speaker_id": speaker_id, "reason": (body or {}).get("reason", "manual")}, "host")
    return {"ok": True, "data": await store.get_snapshot()}


@app.post("/api/matches/{match_id}/agent/{speaker_id}/manual-input")
async def manual_agent_input(match_id: str, speaker_id: str, body: Dict[str, Any], _principal: Principal = Depends(require_host)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    await store.record_manual_agent_input(
        speaker_id,
        body.get("content", ""),
        body.get("reason", "manual_input"),
    )
    return {"ok": True, "data": await store.get_snapshot()}


@app.post("/api/matches/{match_id}/votes")
async def submit_judge_votes(match_id: str, body: Dict[str, Any], _principal: Principal = Depends(require_host)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    await store.submit_vote(body, audience=False)
    return {"ok": True, "data": await store.get_snapshot()}


@app.post("/api/matches/{match_id}/audience-votes/open")
async def open_audience_votes(match_id: str, _principal: Principal = Depends(require_host)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    await store.open_audience_votes()
    vote_url = "/vote" if match_id == "current" else f"/vote/{match_id}"
    return {"ok": True, "data": {"vote_url": vote_url, "window_status": "open"}}


@app.post("/api/matches/{match_id}/audience-votes/close")
async def close_audience_votes(match_id: str, _principal: Principal = Depends(require_host)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    await store.close_audience_votes()
    return {"ok": True, "data": await store.get_snapshot()}


@app.post("/api/matches/{match_id}/votes/publish")
async def publish_votes(match_id: str, body: Dict[str, Any], _principal: Principal = Depends(require_host)) -> Dict[str, Any]:
    await _ensure_match(match_id)
    await store.publish_votes(body.get("scope", "judge"))
    return {"ok": True, "data": await store.get_snapshot()}


@app.post("/api/public/matches/{match_id}/audience-votes")
async def submit_audience_vote(match_id: str, body: Dict[str, Any], request: Request) -> Dict[str, Any]:
    await _ensure_match(match_id)
    vote_body = dict(body)
    vote_body["request_ip"] = _client_ip(request)
    vote_body["request_user_agent"] = request.headers.get("user-agent", "")
    await store.submit_vote(vote_body, audience=True)
    return {"ok": True, "data": {"received": True}}


@app.websocket("/ws/matches/{match_id}")
async def match_ws(
    websocket: WebSocket,
    match_id: str,
    last_seq: int = 0,
    channel: str = "screen",
    speaker_id: Optional[str] = None,
) -> None:
    snapshot = await store.get_snapshot()
    if match_id not in {"current", snapshot["match"]["id"]}:
        await websocket.close(code=1008)
        return
    if authorize_websocket(websocket, channel, speaker_id) is None:
        await websocket.close(code=1008)
        return
    await store.websocket(websocket, last_seq=last_seq, channel=channel, speaker_id=speaker_id)


@app.websocket("/ws/asr-test/{match_id}")
async def asr_test_ws(websocket: WebSocket, match_id: str) -> None:
    """流式 ASR 自检：浏览器持续推送 16k PCM 帧，服务端转发讯飞实时听写并回传 partial/final。"""
    import os as _os
    from app.services.xfyun_rtasr import select_asr_gateway

    await websocket.accept()

    async def emit(kind: str, **kw: Any) -> None:
        try:
            await websocket.send_json({"type": kind, **kw})
        except Exception:
            pass

    url = _os.getenv("XFYUN_ASR_URL", "").strip()
    if not url:
        await emit("error", message="未配置 ASR 地址（XFYUN_ASR_URL），无法进行流式测试。")
        await websocket.close()
        return

    gateway = select_asr_gateway(url)
    try:
        session = await gateway.open_stream(
            on_partial=lambda text: emit("partial", text=text),
            on_final=lambda text: emit("final", text=text),
            on_error=lambda err: emit("error", message=str(err)),
        )
    except Exception as exc:  # noqa: BLE001
        await emit("error", message=f"无法建立 ASR 流：{exc}")
        await websocket.close()
        return

    await emit("ready")
    try:
        while True:
            msg = await websocket.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if msg.get("bytes") is not None:
                await session.send_audio(msg["bytes"])
            elif msg.get("text") == "end":
                break
    except WebSocketDisconnect:
        pass
    finally:
        try:
            result = await session.finish()
            await emit("done", text=result.text, latency_ms=result.latency_ms, chunk_count=result.chunk_count)
        except Exception as exc:  # noqa: BLE001
            await emit("error", message=str(exc))
        try:
            await websocket.close()
        except Exception:
            pass


@app.websocket("/ws/tts-test/{match_id}")
async def tts_test_ws(websocket: WebSocket, match_id: str) -> None:
    """流式 TTS 自检：合成时逐段把音频回传浏览器，便于边收边播。"""
    import os as _os
    from app.services.xfyun_gateway import XfyunTTSGateway

    await websocket.accept()
    try:
        req = await websocket.receive_json()
    except Exception:
        await websocket.close()
        return

    text = str((req or {}).get("text") or "").strip()
    url = _os.getenv("XFYUN_TTS_URL", "").strip()
    if not url:
        await websocket.send_json({"type": "error", "message": "未配置 TTS 地址（XFYUN_TTS_URL）。"})
        await websocket.close()
        return

    gateway = XfyunTTSGateway(url=url)
    try:
        async for ev in gateway.synthesize_stream(text):
            if ev["type"] == "chunk":
                await websocket.send_json(
                    {"type": "chunk", "index": ev["index"], "audio_base64": base64.b64encode(ev["audio"]).decode("ascii")}
                )
            else:
                await websocket.send_json(
                    {"type": "done", "mime_type": ev["mime_type"], "latency_ms": ev["latency_ms"], "chunk_count": ev["chunk_count"]}
                )
    except WebSocketDisconnect:
        return
    except Exception as exc:  # noqa: BLE001
        try:
            await websocket.send_json({"type": "error", "message": str(exc)})
        except Exception:
            pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


def _token_hashes_from_security_body(body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if isinstance(body.get("token_hashes"), dict):
        return body["token_hashes"]
    tokens = body.get("tokens")
    if not isinstance(tokens, dict):
        return None

    def hash_values(value: Any) -> list[str]:
        if isinstance(value, str):
            return [hash_token(value.strip())] if value.strip() else []
        if isinstance(value, list):
            return [hash_token(str(item).strip()) for item in value if str(item).strip()]
        return []

    result: Dict[str, Any] = {}
    for role in ("admin", "host", "screen", "speaker_shared"):
        hashes = hash_values(tokens.get(role))
        if hashes:
            result[f"{role}_hashes"] = hashes
    speaker_tokens = tokens.get("speakers")
    if isinstance(speaker_tokens, dict):
        speakers = {
            str(speaker_id): hash_values(token)
            for speaker_id, token in speaker_tokens.items()
            if hash_values(token)
        }
        if speakers:
            result["speaker_hashes"] = speakers
    return result



def _auto_trigger_agent_speech_for_phase(phase: Dict[str, Any], speakers: list) -> None:
    """Fire-and-forget: if a single-speaker phase designates an AI speaker, auto-start their speech."""
    if not phase or phase.get("phase_type") == "free_debate":
        return
    designated = next(
        (s for s in speakers if s.get("side") == phase.get("side")
         and s.get("seat") == phase.get("speaker_seat")
         and s.get("speaker_type") == "agent"),
        None,
    )
    if not designated:
        return
    try:
        store.ensure_agent_speaker_for_current_phase(designated["id"])
        asyncio.create_task(store.run_agent_speech(designated["id"]))
    except Exception:
        pass


async def _ensure_match(match_id: str) -> None:
    snapshot = await store.get_snapshot()
    if match_id not in {"current", snapshot["match"]["id"]}:
        raise HTTPException(status_code=404, detail="match not found")


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    return request.client.host if request.client else ""


def _frontend_index() -> FileResponse:
    if not FRONTEND_INDEX.exists():
        raise HTTPException(status_code=404, detail="frontend dist not built")
    # index.html must always revalidate so browsers pick up new content-hashed asset
    # bundles after a deploy; the /assets/* files themselves are immutable by hash.
    return FileResponse(FRONTEND_INDEX, headers={"Cache-Control": "no-cache, must-revalidate"})


if FRONTEND_ASSETS.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_ASSETS), name="frontend-assets")


@app.get("/", include_in_schema=False)
async def frontend_root() -> FileResponse:
    return _frontend_index()


@app.get("/screen", include_in_schema=False)
async def frontend_screen() -> FileResponse:
    return _frontend_index()


@app.get("/admin", include_in_schema=False)
async def frontend_admin() -> FileResponse:
    return _frontend_index()


@app.get("/host", include_in_schema=False)
async def frontend_host() -> FileResponse:
    return _frontend_index()


@app.get("/console", include_in_schema=False)
@app.get("/console/{speaker_id}", include_in_schema=False)
async def frontend_console(speaker_id: Optional[str] = None) -> FileResponse:
    return _frontend_index()


@app.get("/vote", include_in_schema=False)
@app.get("/vote/{match_id}", include_in_schema=False)
async def frontend_vote(match_id: Optional[str] = None) -> FileResponse:
    return _frontend_index()
