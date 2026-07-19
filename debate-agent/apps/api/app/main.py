from __future__ import annotations

import json
import logging
import secrets
import time
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import httpx
import redis.asyncio as redis
from app.agent_engine import AgentEngineError, agent_engine, request_hash
from app.config import settings
from app.database import SessionLocal, get_db
from app.mem0_bridge import mem0_bridge
from app.models import (
    AdminSession,
    AdminUser,
    AgentPersona,
    AgentTask,
    AuditLog,
    GatewayKey,
    LLMProvider,
    MemoryItem,
    MemoryPolicy,
    MessageTemplate,
    ModelPreset,
    PromptVersion,
    utcnow,
)
from app.schemas import (
    AdminCreateInput,
    DebateRequest,
    GatewayKeyInput,
    JudgeRequest,
    LLMProviderInput,
    LoginRequest,
    MemoryPolicyInput,
    MessageTemplateInput,
    ModelPresetInput,
    PersonaInput,
    PromptInput,
    PromptPreviewInput,
)
from app.security import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    create_session,
    current_admin,
    decrypt_secret,
    encrypt_secret,
    gateway_key,
    hash_password,
    token_hash,
    verified_admin,
    verify_password,
)
from app.seed import initialize_database
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    initialize_database()
    try:
        try:
            result = await agent_engine.warmup()
            logger.info("debate model startup warmup result=%s", result)
        except Exception as exc:
            agent_engine.last_warmup_result = {
                "status": "failed",
                "requests": settings.startup_model_warmup_requests,
                "error_type": type(exc).__name__,
            }
            logger.exception("debate model startup warmup failed; service will start without a warm model cache")
        yield
    finally:
        await agent_engine.aclose()


app = FastAPI(
    title="Jixia Debate Agent",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
    if settings.cookie_secure:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


def audit(db: Session, user: AdminUser | None, action: str, target_type: str, target_id: str, payload: dict | None = None) -> None:
    db.add(
        AuditLog(
            actor_user_id=user.id if user else None,
            action=action,
            target_type=target_type,
            target_id=target_id,
            payload=payload or {},
        )
    )


def provider_json(item: LLMProvider) -> dict[str, Any]:
    return {
        "id": item.id,
        "name": item.name,
        "provider": item.provider,
        "base_url": item.base_url,
        "model_id": item.model_id,
        "has_api_key": bool(item.api_key_ciphertext),
        "timeout_seconds": item.timeout_seconds,
        "priority": item.priority,
        "max_retries": item.max_retries,
        "rpm_limit": item.rpm_limit,
        "is_active": item.is_active,
        "updated_at": item.updated_at.isoformat(),
    }


def model_json(item: Any, *, exclude: set[str] | None = None) -> dict[str, Any]:
    exclude = (exclude or set()) | {"api_key_ciphertext", "password_hash", "key_hash", "token_hash", "csrf_hash"}
    return {
        column.name: (
            value.isoformat() if hasattr(value, "isoformat") else value
        )
        for column in item.__table__.columns
        if column.name not in exclude and (value := getattr(item, column.name)) is not None
    }


@app.get("/debate/health")
@app.get("/debate/api/health")
async def health() -> JSONResponse:
    database_ok = False
    redis_ok = False
    active_providers = 0
    try:
        with SessionLocal() as db:
            db.execute(select(1))
            database_ok = True
            active_providers = db.scalar(select(func.count(LLMProvider.id)).where(LLMProvider.is_active.is_(True))) or 0
    except Exception:
        pass
    client = redis.from_url(settings.redis_url, decode_responses=True)
    try:
        redis_ok = bool(await client.ping())
    except Exception:
        pass
    finally:
        await client.aclose()
    warmup = dict(agent_engine.last_warmup_result)
    warmup_ok = warmup.get("status") in {"ok", "disabled", "skipped"}
    ok = database_ok and redis_ok and active_providers > 0 and warmup_ok
    payload = {
        "ok": ok,
        "status": "ready" if ok else "degraded" if database_ok and redis_ok else "unavailable",
        "service": "jixia-debate-agent",
        "version": "1.0.0",
        "checks": {
            "database": {"ok": database_ok},
            "redis": {"ok": redis_ok},
            "llm_gateway": {"ok": active_providers > 0, "active_providers": active_providers},
            "model_warmup": {"ok": warmup_ok, **warmup},
        },
    }
    return JSONResponse(status_code=200 if ok else 503, content=payload)


@app.get("/debate/api/models")
def debate_models(db: Session = Depends(get_db)) -> dict[str, Any]:
    providers = db.scalars(select(LLMProvider).where(LLMProvider.is_active.is_(True)).order_by(LLMProvider.priority)).all()
    seen: set[str] = set()
    models = []
    for provider in providers:
        if provider.model_id in seen:
            continue
        seen.add(provider.model_id)
        models.append({"id": provider.model_id, "name": provider.model_id})
    return {"models": models}


@app.post("/debate/api/admin/login")
def login(payload: LoginRequest, response: Response, db: Session = Depends(get_db)) -> dict[str, Any]:
    user = db.scalar(select(AdminUser).where(AdminUser.account == payload.account.strip().lower()))
    if not user or not user.is_active or not verify_password(user.password_hash, payload.password):
        raise HTTPException(status_code=401, detail="账号或密码错误。")
    token, csrf, _session = create_session(db, user)
    audit(db, user, "auth.login", "admin_user", user.id)
    db.commit()
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        max_age=settings.session_hours * 3600,
        path="/debate",
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf,
        httponly=False,
        secure=settings.cookie_secure,
        samesite="strict",
        max_age=settings.session_hours * 3600,
        path="/debate",
    )
    return {"user": model_json(user, exclude={"created_at", "updated_at"}), "csrf_token": csrf}


@app.post("/debate/api/admin/logout")
def logout(
    request: Request,
    response: Response,
    user: AdminUser = Depends(verified_admin),
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    token = request.cookies.get(SESSION_COOKIE, "")
    db.execute(delete(AdminSession).where(AdminSession.token_hash == token_hash(token)))
    audit(db, user, "auth.logout", "admin_user", user.id)
    db.commit()
    response.delete_cookie(SESSION_COOKIE, path="/debate")
    response.delete_cookie(CSRF_COOKIE, path="/debate")
    return {"ok": True}


@app.get("/debate/api/admin/session")
def session(user: AdminUser = Depends(current_admin)) -> dict[str, Any]:
    return {"user": model_json(user, exclude={"created_at", "updated_at"})}


@app.get("/debate/api/admin/dashboard")
def dashboard(user: AdminUser = Depends(current_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    _ = user
    return {
        "counts": {
            "providers": db.scalar(select(func.count(LLMProvider.id))) or 0,
            "personas": db.scalar(select(func.count(AgentPersona.id))) or 0,
            "published_prompts": db.scalar(select(func.count(PromptVersion.id)).where(PromptVersion.status == "published")) or 0,
            "pending_memories": db.scalar(select(func.count(MemoryItem.id)).where(MemoryItem.status == "pending_review")) or 0,
            "running_tasks": db.scalar(select(func.count(AgentTask.id)).where(AgentTask.status == "running")) or 0,
        },
        "providers": [provider_json(item) for item in db.scalars(select(LLMProvider).order_by(LLMProvider.priority)).all()],
        "prompts": [model_json(item) for item in db.scalars(select(PromptVersion).order_by(PromptVersion.prompt_key, PromptVersion.version.desc())).all()],
        "message_templates": [model_json(item) for item in db.scalars(select(MessageTemplate).order_by(MessageTemplate.position)).all()],
        "presets": [model_json(item) for item in db.scalars(select(ModelPreset).order_by(ModelPreset.name)).all()],
        "memory_policies": [model_json(item) for item in db.scalars(select(MemoryPolicy).order_by(MemoryPolicy.name)).all()],
        "personas": [model_json(item) for item in db.scalars(select(AgentPersona).order_by(AgentPersona.profile_key)).all()],
        "memories": [model_json(item) for item in db.scalars(select(MemoryItem).order_by(MemoryItem.created_at.desc()).limit(200)).all()],
        "tasks": [model_json(item) for item in db.scalars(select(AgentTask).order_by(AgentTask.created_at.desc()).limit(200)).all()],
        "gateway_keys": [model_json(item) for item in db.scalars(select(GatewayKey).order_by(GatewayKey.created_at.desc())).all()],
        "admins": [model_json(item) for item in db.scalars(select(AdminUser).order_by(AdminUser.created_at)).all()],
        "audit": [model_json(item) for item in db.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(200)).all()],
    }


@app.post("/debate/api/admin/llm-providers")
def create_provider(payload: LLMProviderInput, user: AdminUser = Depends(verified_admin), db: Session = Depends(get_db)):
    item = LLMProvider(**payload.model_dump(exclude={"api_key", "clear_api_key"}))
    if payload.api_key:
        item.api_key_ciphertext = encrypt_secret(payload.api_key)
    db.add(item)
    db.flush()
    audit(db, user, "llm_provider.create", "llm_provider", item.id, {"model_id": item.model_id, "has_api_key": bool(payload.api_key)})
    db.commit()
    return {"provider": provider_json(item)}


@app.put("/debate/api/admin/llm-providers/{item_id}")
def update_provider(item_id: str, payload: LLMProviderInput, user: AdminUser = Depends(verified_admin), db: Session = Depends(get_db)):
    item = db.get(LLMProvider, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="LLM Provider 不存在。")
    for key, value in payload.model_dump(exclude={"api_key", "clear_api_key"}).items():
        setattr(item, key, value)
    if payload.clear_api_key:
        item.api_key_ciphertext = ""
    elif payload.api_key:
        item.api_key_ciphertext = encrypt_secret(payload.api_key)
    audit(db, user, "llm_provider.update", "llm_provider", item.id, {"model_id": item.model_id, "secret_changed": bool(payload.api_key or payload.clear_api_key)})
    db.commit()
    return {"provider": provider_json(item)}


@app.post("/debate/api/admin/llm-providers/{item_id}/test")
async def test_provider(
    item_id: str,
    user: AdminUser = Depends(verified_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    item = db.get(LLMProvider, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="LLM Provider 不存在。")
    endpoint = item.base_url.rstrip("/")
    if item.provider == "debate_api" and endpoint.endswith("/api/debate"):
        endpoint = f"{endpoint[:-len('/debate')]}/models"
    elif item.provider != "debate_api":
        endpoint = f"{endpoint}/models"
    if not endpoint:
        raise HTTPException(status_code=422, detail="Provider 地址为空。")
    headers = {}
    secret = decrypt_secret(item.api_key_ciphertext)
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(endpoint, headers=headers)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=f"Provider 连接失败：{type(exc).__name__}") from exc
    raw_models = payload.get("data", payload.get("models", [])) if isinstance(payload, dict) else []
    model_ids = [str(model.get("id") or model.get("name") or "") for model in raw_models if isinstance(model, dict)]
    latency_ms = round((time.perf_counter() - started) * 1000, 1)
    audit(db, user, "llm_provider.test", "llm_provider", item.id, {"ok": True, "latency_ms": latency_ms})
    db.commit()
    return {
        "ok": True,
        "latency_ms": latency_ms,
        "model_available": not model_ids or item.model_id in model_ids,
        "models_count": len(model_ids),
    }


@app.post("/debate/api/admin/prompts")
def create_prompt(payload: PromptInput, user: AdminUser = Depends(verified_admin), db: Session = Depends(get_db)):
    latest = db.scalar(select(func.max(PromptVersion.version)).where(PromptVersion.prompt_key == payload.prompt_key)) or 0
    item = PromptVersion(**payload.model_dump(), version=latest + 1, status="draft")
    db.add(item)
    db.flush()
    audit(db, user, "prompt.create", "prompt_version", item.id, {"prompt_key": item.prompt_key, "version": item.version})
    db.commit()
    return {"prompt": model_json(item)}


@app.post("/debate/api/admin/prompts/preview")
def preview_prompt(payload: PromptPreviewInput, user: AdminUser = Depends(verified_admin)) -> dict[str, Any]:
    _ = user
    sample = {
        "debater_name": "陈思远",
        "holder": "正方",
        "debate_position": "一辩",
        "debate_topic": "人工智能时代，还要不要学编程？",
        "current_stage": "正方一辩立论",
        "next_stage": "反方一辩立论",
        "max_token": 700,
        "persona_style": "保持鲜明立场，并回应对方的关键前提。",
        "debate_history_text": "（暂无历史发言）",
        "match_memory": "（无）",
        "long_term_memory": "（无）",
    } | payload.variables
    environment = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False)
    try:
        messages = [
            {"role": "system", "content": environment.from_string(payload.system_template).render(**sample)},
            {"role": "user", "content": environment.from_string(payload.user_template).render(**sample)},
        ]
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Prompt 预览失败：{type(exc).__name__}") from exc
    return {"messages": messages, "variables": sample}


@app.post("/debate/api/admin/prompts/{item_id}/publish")
def publish_prompt(item_id: str, user: AdminUser = Depends(verified_admin), db: Session = Depends(get_db)):
    item = db.get(PromptVersion, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Prompt 版本不存在。")
    item.status = "published"
    item.published_at = utcnow()
    audit(db, user, "prompt.publish", "prompt_version", item.id, {"prompt_key": item.prompt_key, "version": item.version})
    db.commit()
    return {"prompt": model_json(item)}


def upsert_simple(db: Session, model, payload, item_id: str | None = None):
    item = db.get(model, item_id) if item_id else None
    if item_id and not item:
        raise HTTPException(status_code=404, detail="配置不存在。")
    if not item:
        item = model(**payload.model_dump())
        db.add(item)
    else:
        for key, value in payload.model_dump().items():
            setattr(item, key, value)
    db.flush()
    return item


@app.post("/debate/api/admin/message-templates")
def create_message_template(payload: MessageTemplateInput, user: AdminUser = Depends(verified_admin), db: Session = Depends(get_db)):
    item = upsert_simple(db, MessageTemplate, payload)
    audit(db, user, "message_template.create", "message_template", item.id)
    db.commit()
    return {"item": model_json(item)}


@app.post("/debate/api/admin/model-presets")
def create_preset(payload: ModelPresetInput, user: AdminUser = Depends(verified_admin), db: Session = Depends(get_db)):
    item = upsert_simple(db, ModelPreset, payload)
    audit(db, user, "model_preset.create", "model_preset", item.id)
    db.commit()
    return {"item": model_json(item)}


@app.post("/debate/api/admin/memory-policies")
def create_policy(payload: MemoryPolicyInput, user: AdminUser = Depends(verified_admin), db: Session = Depends(get_db)):
    item = upsert_simple(db, MemoryPolicy, payload)
    audit(db, user, "memory_policy.create", "memory_policy", item.id)
    db.commit()
    return {"item": model_json(item)}


@app.post("/debate/api/admin/personas")
def create_persona(payload: PersonaInput, user: AdminUser = Depends(verified_admin), db: Session = Depends(get_db)):
    for model, item_id, label in (
        (LLMProvider, payload.provider_id, "LLM Provider"),
        (PromptVersion, payload.prompt_version_id, "Prompt"),
        (ModelPreset, payload.model_preset_id, "模型参数"),
        (MemoryPolicy, payload.memory_policy_id, "Memory 策略"),
    ):
        if not db.get(model, item_id):
            raise HTTPException(status_code=422, detail=f"{label} 不存在。")
    item = upsert_simple(db, AgentPersona, payload)
    audit(db, user, "persona.create", "agent_persona", item.id, {"profile_key": item.profile_key})
    db.commit()
    return {"item": model_json(item)}


@app.put("/debate/api/admin/personas/{item_id}")
def update_persona(
    item_id: str,
    payload: PersonaInput,
    user: AdminUser = Depends(verified_admin),
    db: Session = Depends(get_db),
):
    for model, related_id, label in (
        (LLMProvider, payload.provider_id, "LLM Provider"),
        (PromptVersion, payload.prompt_version_id, "Prompt"),
        (ModelPreset, payload.model_preset_id, "模型参数"),
        (MemoryPolicy, payload.memory_policy_id, "Memory 策略"),
    ):
        if not db.get(model, related_id):
            raise HTTPException(status_code=422, detail=f"{label} 不存在。")
    item = upsert_simple(db, AgentPersona, payload, item_id)
    audit(db, user, "persona.update", "agent_persona", item.id, {"profile_key": item.profile_key})
    db.commit()
    return {"item": model_json(item)}


@app.post("/debate/api/admin/gateway-keys")
def create_gateway_key(payload: GatewayKeyInput, user: AdminUser = Depends(verified_admin), db: Session = Depends(get_db)):
    raw = f"dba_{secrets.token_urlsafe(36)}"
    item = GatewayKey(name=payload.name, key_prefix=raw[:12], key_hash=token_hash(raw))
    db.add(item)
    db.flush()
    audit(db, user, "gateway_key.create", "gateway_key", item.id, {"key_prefix": item.key_prefix})
    db.commit()
    return {"item": model_json(item), "secret": raw}


@app.post("/debate/api/admin/gateway-keys/{item_id}/revoke")
def revoke_gateway_key(item_id: str, user: AdminUser = Depends(verified_admin), db: Session = Depends(get_db)):
    item = db.get(GatewayKey, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Gateway Key 不存在。")
    item.is_active = False
    audit(db, user, "gateway_key.revoke", "gateway_key", item.id, {"key_prefix": item.key_prefix})
    db.commit()
    return {"ok": True}


@app.post("/debate/api/admin/admin-users")
def create_admin(payload: AdminCreateInput, user: AdminUser = Depends(verified_admin), db: Session = Depends(get_db)):
    if user.role != "owner":
        raise HTTPException(status_code=403, detail="只有所有者可以创建管理员。")
    item = AdminUser(
        account=payload.account.lower(),
        real_name=payload.real_name.strip(),
        password_hash=hash_password(payload.password),
        role=payload.role,
    )
    db.add(item)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="管理员账号已存在。") from exc
    audit(db, user, "admin_user.create", "admin_user", item.id, {"account": item.account, "role": item.role})
    db.commit()
    return {"item": model_json(item)}


@app.post("/debate/api/admin/memories/{item_id}/{action}")
def review_memory(item_id: str, action: str, user: AdminUser = Depends(verified_admin), db: Session = Depends(get_db)):
    item = db.get(MemoryItem, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Memory 不存在。")
    if action not in {"approve", "reject", "delete"}:
        raise HTTPException(status_code=404, detail="未知 Memory 操作。")
    if action == "delete":
        mem0_bridge.delete(str(item.metadata_json.get("mem0_id") or ""))
        db.delete(item)
    else:
        item.status = "approved" if action == "approve" else "rejected"
        item.scope = "long_term"
        item.reviewed_by = user.id
        metadata = dict(item.metadata_json or {})
        if action == "approve":
            mem0_id = mem0_bridge.add_approved(
                memory_id=item.id,
                profile_key=item.profile_key,
                content=item.content,
                metadata={"source_match_id": metadata.get("source_match_id"), "source_task_id": item.source_task_id},
            )
            metadata["mem0_indexed"] = bool(mem0_id) if settings.mem0_enabled else False
            if mem0_id:
                metadata["mem0_id"] = mem0_id
        else:
            mem0_bridge.delete(str(metadata.get("mem0_id") or ""))
            metadata.pop("mem0_id", None)
            metadata["mem0_indexed"] = False
        item.metadata_json = metadata
    audit(db, user, f"memory.{action}", "memory_item", item_id)
    db.commit()
    return {"ok": True}


@app.get("/debate/api/admin/memories/preview")
def preview_memory(
    profile_key: str = Query(min_length=2, max_length=100),
    match_id: str | None = Query(default=None, max_length=100),
    user: AdminUser = Depends(current_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _ = user
    persona = db.scalar(select(AgentPersona).where(AgentPersona.profile_key == profile_key))
    if not persona:
        raise HTTPException(status_code=404, detail="辩手人设不存在。")
    policy = db.get(MemoryPolicy, persona.memory_policy_id)
    limit = policy.retrieval_count if policy else 5
    match_items: list[MemoryItem] = []
    long_items: list[MemoryItem] = []
    if match_id:
        match_items = list(
            db.scalars(
                select(MemoryItem)
                .where(
                    MemoryItem.profile_key == profile_key,
                    MemoryItem.match_id == match_id,
                    MemoryItem.scope == "match",
                    MemoryItem.status == "active",
                )
                .order_by(MemoryItem.created_at.desc())
                .limit(limit)
            ).all()
        )
        long_items = list(
            db.scalars(
                select(MemoryItem)
                .where(
                    MemoryItem.profile_key == profile_key,
                    MemoryItem.scope == "long_term",
                    MemoryItem.status == "approved",
                )
                .order_by(MemoryItem.updated_at.desc())
                .limit(limit)
            ).all()
        )
    return {
        "profile_key": profile_key,
        "match_id": match_id,
        "stateless": not bool(match_id),
        "match_memory": [model_json(item) for item in match_items],
        "approved_long_term_memory": [model_json(item) for item in long_items],
    }


def _create_or_replay_task(db: Session, payload: DebateRequest) -> tuple[AgentTask, bool]:
    task_id = payload.task_id or str(uuid4())
    payload.task_id = task_id
    digest = request_hash(payload)
    existing = db.scalar(select(AgentTask).where(AgentTask.task_id == task_id))
    if existing:
        if existing.request_hash != digest:
            raise HTTPException(status_code=409, detail="相同 task_id 对应了不同请求内容。")
        if existing.status == "failed" and existing.error_code == "service_restarted":
            existing.status = "running"
            existing.error_code = ""
            existing.error_message = ""
            existing.interrupted_at = None
            db.commit()
            return existing, False
        return existing, True
    profile_key = payload.agent_profile or ""
    if not profile_key:
        persona = db.scalar(select(AgentPersona).where(AgentPersona.name == payload.debater_name, AgentPersona.is_active.is_(True)))
        profile_key = persona.profile_key if persona else "debater-1"
    item = AgentTask(
        task_id=task_id,
        request_hash=digest,
        match_id=payload.match_id,
        room_code=payload.room_code,
        profile_key=profile_key,
        status="running",
    )
    db.add(item)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(select(AgentTask).where(AgentTask.task_id == task_id))
        if not existing or existing.request_hash != digest:
            raise HTTPException(status_code=409, detail="任务发生并发冲突。") from None
        return existing, True
    return item, False


@app.post("/debate/api/debate")
async def debate(payload: DebateRequest, key: GatewayKey = Depends(gateway_key), db: Session = Depends(get_db)):
    key.last_used_at = utcnow()
    db.commit()
    task, replayed = _create_or_replay_task(db, payload)
    if replayed:
        if task.status == "completed":
            async def cached_stream():
                event = {
                    "id": f"debate-{task.task_id[:12]}",
                    "model_name": str(task.usage.get("model") or ""),
                    "current_stage": payload.current_stage,
                    "next_stage": payload.next_stage,
                    "delta": {"content": task.content},
                }
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(cached_stream(), media_type="text/event-stream")
        if task.status == "running":
            return JSONResponse(
                status_code=202,
                content={"task_id": task.task_id, "status": "running", "replayed": True},
            )
        raise HTTPException(status_code=409, detail=f"任务当前状态为 {task.status}。")
    try:
        prepared = await agent_engine.prepare(payload)
    except AgentEngineError as exc:
        agent_engine._fail_task(task.task_id, exc.code, exc.message, 0)
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    with SessionLocal() as config_db:
        primary = config_db.get(LLMProvider, prepared["provider_ids"][0])
        model_name = primary.model_id if primary else ""

    async def event_stream():
        try:
            async for event in agent_engine.stream(task, payload, prepared):
                if event.get("type") != "delta":
                    continue
                compatible = {
                    "id": f"debate-{uuid4().hex[:12]}",
                    "model_name": model_name,
                    "current_stage": payload.current_stage,
                    "next_stage": payload.next_stage,
                    "delta": {"content": str(event.get("delta") or "")},
                }
                yield f"data: {json.dumps(compatible, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        except AgentEngineError as exc:
            agent_engine._fail_task(task.task_id, exc.code, exc.message, 0)
            error = {"error": {"code": exc.code, "message": exc.message}}
            yield f"data: {json.dumps(error, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/debate/api/judge")
async def judge(payload: JudgeRequest) -> dict[str, Any]:
    try:
        return await agent_engine.judge(payload)
    except AgentEngineError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@app.post("/debate/api/tasks/{task_id}/interrupt")
async def interrupt_task(task_id: str, key: GatewayKey = Depends(gateway_key), db: Session = Depends(get_db)):
    key.last_used_at = utcnow()
    db.commit()
    found = await agent_engine.interrupt(task_id)
    return {"ok": True, "task_id": task_id, "status": "interrupted" if found else "not_running"}
