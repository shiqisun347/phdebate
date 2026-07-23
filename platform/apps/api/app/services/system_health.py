from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import redis.asyncio as redis
import websockets
from alembic.config import Config
from alembic.script import ScriptDirectory
from app.core.config import settings
from app.models.entities import Room
from app.services.lighttts_admission import lighttts_admission_gate
from app.services.providers import moss_tts_realtime
from app.services.release_provenance import release_provenance
from app.services.voice_runtime.lighttts import probe_readiness as probe_lighttts_readiness
from sqlalchemy import select, text
from sqlalchemy.orm import Session


@lru_cache
def expected_schema_revision() -> str:
    api_root = Path(__file__).resolve().parents[2]
    config = Config(str(api_root / "alembic.ini"))
    config.set_main_option("script_location", str(api_root / "alembic"))
    return ScriptDirectory.from_config(config).get_current_head()


def _database_checks(db: Session, *, production: bool) -> dict[str, dict[str, Any]]:
    started = time.perf_counter()
    try:
        db.scalar(select(1))
        database = {"ok": True, "latency_ms": round((time.perf_counter() - started) * 1000, 2)}
    except Exception as exc:
        return {
            "database": {"ok": False, "message": type(exc).__name__},
            "schema": {"ok": False, "message": "database unavailable"},
        }
    if not production:
        return {"database": database, "schema": {"ok": True, "skipped": True}}
    try:
        current = db.scalar(text("select version_num from alembic_version"))
        expected = expected_schema_revision()
        schema = {"ok": current == expected, "current": current, "expected": expected}
    except Exception as exc:
        schema = {"ok": False, "message": type(exc).__name__}
    return {"database": database, "schema": schema}


def _float_env(name: str, default: float, *, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


def _storage_check(*, production: bool) -> dict[str, Any]:
    usage = shutil.disk_usage(settings.media_path)
    free_percent = usage.free / max(1, usage.total) * 100
    failure_percent = _float_env("HEALTH_DISK_FAILURE_FREE_PERCENT", 10.0, minimum=2.0, maximum=50.0)
    warning_percent = _float_env("HEALTH_DISK_WARNING_FREE_PERCENT", 20.0, minimum=failure_percent, maximum=70.0)
    status = "critical" if free_percent < failure_percent else "warning" if free_percent < warning_percent else "healthy"
    return {
        "ok": not production or status != "critical",
        "warning": production and status == "warning",
        "status": status,
        "skipped": not production,
        "free_percent": round(free_percent, 2),
        "free_bytes": usage.free,
        "warning_percent": warning_percent,
        "failure_percent": failure_percent,
    }


def _backup_check(path: Path, *, production: bool) -> dict[str, Any]:
    if not production:
        return {"ok": True, "skipped": True}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        raw = str(payload["last_success_at"])
        completed_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        age_hours = max(0.0, (datetime.now(timezone.utc) - completed_at).total_seconds() / 3600)
        state = str(payload.get("state") or "succeeded")
        if state not in {"succeeded", "failed", "running"}:
            raise ValueError("unsupported backup state")
        attempt_at = str(payload.get("last_attempt_at") or raw)
        warning = state in {"failed", "running"}
        return {
            "ok": age_hours <= settings.backup_max_age_hours,
            "warning": warning,
            "status": state,
            "last_success_at": completed_at.isoformat(),
            "last_attempt_at": attempt_at,
            "age_hours": round(age_hours, 2),
            "maximum_age_hours": settings.backup_max_age_hours,
            "bytes": payload.get("bytes", 0),
            "message": str(payload.get("error_code") or "") if state == "failed" else "",
        }
    except Exception as exc:
        return {"ok": False, "message": type(exc).__name__, "maximum_age_hours": settings.backup_max_age_hours}


def _agent_backup_status_path() -> Path:
    return Path(
        os.getenv(
            "AGENT_BACKUP_STATUS_FILE",
            "/home/ubuntu/sunsq/debate-agent/runtime/backup-status.json",
        )
    ).expanduser().resolve()


async def _redis_checks(*, production: bool) -> dict[str, dict[str, Any]]:
    if not production:
        return {
            "redis": {"ok": True, "skipped": True},
            "engine": {"ok": True, "skipped": True},
            "worker": {"ok": True, "skipped": True},
        }
    client: redis.Redis | None = None
    started = time.perf_counter()
    try:
        client = redis.from_url(settings.redis_url, decode_responses=True)
        await asyncio.wait_for(client.ping(), timeout=2)
        redis_check = {"ok": True, "latency_ms": round((time.perf_counter() - started) * 1000, 2)}
        heartbeat = await asyncio.wait_for(client.get("jixia:heartbeat:engine"), timeout=2)
        if heartbeat is None:
            engine = {"ok": False, "message": "heartbeat missing"}
        else:
            age_seconds = max(0.0, time.time() - float(heartbeat))
            engine = {"ok": age_seconds <= 20, "age_seconds": round(age_seconds, 2), "maximum_age_seconds": 20}
        worker_heartbeats = await asyncio.wait_for(client.zrange("dramatiq:__heartbeats__", 0, -1, withscores=True), timeout=2)
        latest_worker_score = max((float(score) for _member, score in worker_heartbeats), default=0.0)
        worker_age_seconds = max(0.0, time.time() - latest_worker_score / 1000) if latest_worker_score else None
        dead_queue, dead_messages, queued_messages = await asyncio.gather(
            asyncio.wait_for(client.zcard("dramatiq:default.XQ"), timeout=2),
            asyncio.wait_for(client.hlen("dramatiq:default.XQ.msgs"), timeout=2),
            asyncio.wait_for(client.llen("dramatiq:default"), timeout=2),
        )
        dead_letters = max(int(dead_queue), int(dead_messages))
        worker_ok = worker_age_seconds is not None and worker_age_seconds <= 20 and dead_letters == 0
        worker = {
            "ok": worker_ok,
            "age_seconds": round(worker_age_seconds, 2) if worker_age_seconds is not None else None,
            "maximum_age_seconds": 20,
            "workers": sum(1 for _member, score in worker_heartbeats if time.time() - float(score) / 1000 <= 20),
            "queued_messages": int(queued_messages),
            "dead_letters": dead_letters,
        }
        if worker_age_seconds is None:
            worker["message"] = "Worker 心跳缺失"
        elif dead_letters:
            worker["message"] = f"死信任务 {dead_letters} 条"
    except Exception as exc:
        redis_check = {"ok": False, "message": type(exc).__name__}
        engine = {"ok": False, "message": "redis unavailable"}
        worker = {"ok": False, "message": "redis unavailable"}
    finally:
        if client:
            await client.aclose()
    return {"redis": redis_check, "engine": engine, "worker": worker}


async def _endpoint_check(url: str, *, name: str, production: bool) -> dict[str, Any]:
    if not production:
        return {"ok": True, "skipped": True}
    parsed = urlparse(url)
    host = parsed.hostname
    default_port = 443 if parsed.scheme in {"https", "wss"} else 80
    port = parsed.port or default_port
    if not host:
        return {"ok": False, "message": f"{name} endpoint invalid"}
    started = time.perf_counter()
    writer: asyncio.StreamWriter | None = None
    try:
        if parsed.scheme in {"ws", "wss"}:
            async with websockets.connect(
                url,
                open_timeout=2,
                close_timeout=1,
                ping_interval=None,
            ):
                pass
            return {
                "ok": True,
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                "service": name,
            }
        _reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=2)
        return {
            "ok": True,
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "service": name,
        }
    except Exception as exc:
        return {"ok": False, "message": type(exc).__name__, "service": name}
    finally:
        if writer:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass


async def _provider_checks(*, production: bool) -> dict[str, dict[str, Any]]:
    if settings.realtime_voice_backend == "moss_realtime" and settings.moss_tts_realtime_enabled:
        moss, funasr = await asyncio.gather(
            moss_tts_realtime.readiness_snapshot() if production else asyncio.sleep(
                0,
                result={"ok": True, "skipped": True, "service": "MOSS-TTS-Realtime"},
            ),
            _endpoint_check(settings.funasr_ws_url, name="FunASR", production=production),
        )
        return {"moss_tts_realtime": moss, "funasr": funasr}

    lighttts, funasr, admission = await asyncio.gather(
        probe_lighttts_readiness(settings.lighttts_url) if production else asyncio.sleep(
            0,
            result={"ok": True, "skipped": True, "service": "LightTTS"},
        ),
        _endpoint_check(settings.funasr_ws_url, name="FunASR", production=production),
        lighttts_admission_gate.status_snapshot(),
    )
    lighttts["admission_gate"] = admission
    if production:
        lighttts["configured_max_active"] = settings.lighttts_max_active
        if settings.lighttts_global_gate_enabled and not admission["ok"]:
            lighttts["ok"] = False
    return {"lighttts": lighttts, "funasr": funasr}


async def system_readiness(db: Session) -> dict[str, Any]:
    production = settings.app_env == "production"
    checks = _database_checks(db, production=production)
    # Readiness performs network checks next.  Release the synchronous database
    # connection first so concurrent load-balancer probes cannot consume the
    # whole SQL pool while waiting on Redis.
    db.rollback()
    checks.update(await _redis_checks(production=production))
    checks.update(await _provider_checks(production=production))
    checks["storage"] = _storage_check(production=production)
    checks["backup"] = _backup_check(settings.backup_status_path, production=production)
    checks["agent_backup"] = _backup_check(_agent_backup_status_path(), production=production)
    checks["release"] = release_provenance() if production else {"ok": True, "skipped": True}
    try:
        active_rooms = db.scalar(select(Room.id).where(Room.status.in_(["preparing", "running", "judging"])).limit(1))
    except Exception:
        active_rooms = None
    return {
        "ok": all(bool(item.get("ok")) for item in checks.values()),
        "checks": checks,
        "active_match_processing": active_rooms is not None,
        "match_audio_archive_enabled": settings.match_audio_archive_enabled,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
