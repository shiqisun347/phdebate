from __future__ import annotations

import asyncio
import json
import logging
import re
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
    ShouldSpeakRequest,
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


def _probe_error(response: httpx.Response) -> str:
    """Extract a short, secret-free error from an upstream response."""

    detail = ""
    try:
        body = response.json()
        if isinstance(body, dict):
            value = body.get("error", body)
            if isinstance(value, dict):
                code = str(value.get("code") or value.get("type") or "").strip()
                message = str(value.get("message") or value.get("detail") or "").strip()
                detail = ": ".join(item for item in (code, message) if item)
            else:
                detail = str(value).strip()
    except (ValueError, TypeError):
        detail = response.text.strip()
    detail = " ".join(detail.split())
    # Provider error bodies should never echo an API key or a large arbitrary
    # payload into the admin UI/audit trail.
    detail = re.sub(r"(?i)(?:bearer\s+|api[_-]?key[=:]\s*)[^\s,;]+", "<redacted>", detail)
    return detail[:240]


def _provider_models_url(item: LLMProvider) -> str:
    endpoint = item.base_url.rstrip("/")
    if not endpoint:
        raise ValueError("Provider 地址为空。")
    if item.provider == "debate_api" and endpoint.endswith("/api/debate"):
        return f"{endpoint[:-len('/debate')]}/models"
    if endpoint.endswith("/models"):
        return endpoint
    return f"{endpoint}/models"


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
    try:
        models_endpoint = _provider_models_url(item)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    headers = {}
    secret = decrypt_secret(item.api_key_ciphertext)
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    started = time.perf_counter()
    try:
        timeout = httpx.Timeout(connect=5, read=min(item.timeout_seconds, 15), write=5, pool=5)
        async with httpx.AsyncClient(timeout=timeout) as client:
            models_response = await client.get(models_endpoint, headers=headers)
            if models_response.status_code >= 400:
                detail = _probe_error(models_response)
                suffix = f"：{detail}" if detail else "。"
                raise RuntimeError(f"模型列表检查失败（HTTP {models_response.status_code}）{suffix}")
            try:
                models_payload = models_response.json()
            except ValueError as exc:
                raise RuntimeError("模型列表响应不是有效 JSON。") from exc

            raw_models = (
                models_payload.get("data", models_payload.get("models", []))
                if isinstance(models_payload, dict)
                else []
            )
            # A models endpoint alone cannot prove that the configured model
            # is authenticated or can actually generate.  Probe the same
            # generation contract used in production, consuming only the
            # first streamed delta for Debate REST providers.
            if item.provider == "debate_api":
                probe_body = {
                    "model_name": item.model_id,
                    "debater_name": "连接测试",
                    "debate_position": "一辩",
                    "debate_topic": "连接测试：请只回复“测试通过”。",
                    "current_stage": "自我介绍",
                    "next_stage": "比赛结束",
                    "holder": "正方",
                    "debate_history": [],
                    "task_type": "self_intro",
                    "max_token": 16,
                    "output": {"stream": True, "language": "zh-CN"},
                }
                generated = False
                async with client.stream("POST", item.base_url, json=probe_body, headers={**headers, "Content-Type": "application/json"}) as response:
                    if response.status_code >= 400:
                        detail = _probe_error(response)
                        suffix = f"：{detail}" if detail else "。"
                        raise RuntimeError(f"生成接口检查失败（HTTP {response.status_code}）{suffix}")
                    async for line in response.aiter_lines():
                        line = line.strip()
                        if not line or line.startswith(":"):
                            continue
                        if line.startswith("data:"):
                            line = line[5:].strip()
                        if line == "[DONE]":
                            break
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        delta = event.get("delta") if isinstance(event, dict) else None
                        if isinstance(delta, dict) and str(delta.get("content") or ""):
                            generated = True
                            break
                        if isinstance(event, dict) and isinstance(event.get("error"), dict):
                            message = str(event["error"].get("message") or "上游返回错误")
                            raise RuntimeError(f"生成接口返回错误：{message[:200]}")
                if not generated:
                    raise RuntimeError("生成接口未返回有效首段文本。")
            else:
                endpoint = item.base_url.rstrip("/")
                if not endpoint.endswith("/chat/completions"):
                    endpoint = f"{endpoint}/chat/completions"
                probe_body = {
                    "model": item.model_id,
                    "messages": [{"role": "user", "content": "只回复：测试通过"}],
                    "max_tokens": 8,
                    "temperature": 0,
                    "stream": False,
                    "extra_body": {"enable_thinking": False},
                }
                response = await client.post(endpoint, json=probe_body, headers={**headers, "Content-Type": "application/json"})
                if response.status_code >= 400:
                    detail = _probe_error(response)
                    suffix = f"：{detail}" if detail else "。"
                    raise RuntimeError(f"生成接口检查失败（HTTP {response.status_code}）{suffix}")
                try:
                    generated_payload = response.json()
                except ValueError as exc:
                    raise RuntimeError("生成接口响应不是有效 JSON。") from exc
                choices = generated_payload.get("choices") if isinstance(generated_payload, dict) else None
                if not isinstance(choices, list) or not choices:
                    raise RuntimeError("生成接口未返回 choices。")
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        message = f"Provider 连接失败：{exc}"
        audit(db, user, "llm_provider.test", "llm_provider", item.id, {"ok": False, "error": message[:300]})
        db.commit()
        raise HTTPException(status_code=502, detail=message[:400]) from exc
    model_ids = [str(model.get("id") or model.get("name") or "") for model in raw_models if isinstance(model, dict)]
    latency_ms = round((time.perf_counter() - started) * 1000, 1)
    audit(db, user, "llm_provider.test", "llm_provider", item.id, {"ok": True, "latency_ms": latency_ms, "generation_probe": True})
    db.commit()
    return {
        "ok": True,
        "latency_ms": latency_ms,
        "model_available": not model_ids or item.model_id in model_ids,
        "models_count": len(model_ids),
        "generation_probe": True,
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
                    # ``prepared`` may try several providers.  The previous
                    # implementation always advertised the first provider,
                    # even when it failed and a fallback produced the text.
                    # Forward the authoritative provider attached to each
                    # delta so the main platform can diagnose a real run.
                    "model_name": str(event.get("model_name") or model_name),
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


def _parse_should_speak(content: str) -> tuple[bool, str]:
    """Parse the intentionally tiny decision output without accepting prose.

    A malformed response is an upstream/configuration failure, not an implicit
    negative decision.  The match platform can then fail open and preserve the
    established automatic-debate flow.
    """

    raw = content.strip()
    if raw.startswith("```"):
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AgentEngineError("decision_invalid_output", "是否发言判断未返回有效 JSON。") from exc
    if not isinstance(value, dict) or type(value.get("should_speak")) is not bool:
        raise AgentEngineError("decision_invalid_output", "是否发言判断缺少布尔字段 should_speak。")
    reason = str(value.get("reason") or "").strip()[:300]
    return value["should_speak"], reason


@app.post("/debate/api/should-speak")
async def should_speak(
    payload: ShouldSpeakRequest,
    key: GatewayKey = Depends(gateway_key),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Return one idempotent free-debate intent decision.

    The endpoint deliberately does not generate or return the candidate speech.
    Callers start ``/api/debate`` concurrently under a separate task id, which
    makes negative decisions cheap to cancel and impossible to leak as output.
    """

    key.last_used_at = utcnow()
    db.commit()
    task, replayed = _create_or_replay_task(db, payload)
    if replayed:
        if task.status == "completed":
            decision, reason = _parse_should_speak(task.content)
            return {
                "task_id": task.task_id,
                "should_speak": decision,
                "reason": reason,
                "replayed": True,
            }
        if task.status == "running":
            return JSONResponse(
                status_code=202,
                content={"task_id": task.task_id, "status": "running", "replayed": True},
            )
        raise HTTPException(status_code=409, detail=f"任务当前状态为 {task.status}。")
    try:
        prepared = await agent_engine.prepare(payload)
        prepared["messages"] = [
            *prepared["messages"],
            {
                "role": "user",
                "content": (
                    "这是自由辩论抢答判断。结合当前完整历史，判断此刻该辩手是否有必要主动发言。"
                    "只有在能提出新论点、直接反驳关键漏洞或澄清重大误解时才选择发言；避免重复和无效占时。"
                    "只输出一个 JSON 对象，格式严格为 "
                    '{"should_speak":true,"reason":"不超过60字"}，不得输出 Markdown 或发言正文。'
                ),
            },
        ]
        content = ""
        # Intent must never occupy the hot path for a full speech duration.
        async with asyncio.timeout(5.0):
            async for event in agent_engine.stream(task, payload, prepared):
                if event.get("type") == "delta":
                    content += str(event.get("delta") or "")
                elif event.get("type") == "final":
                    content = str(event.get("content") or content)
        decision, reason = _parse_should_speak(content)
    except TimeoutError as exc:
        await agent_engine.interrupt(task.task_id)
        raise HTTPException(status_code=504, detail="是否发言判断超时。") from exc
    except AgentEngineError as exc:
        with SessionLocal() as failure_db:
            failed = failure_db.scalar(select(AgentTask).where(AgentTask.task_id == task.task_id))
            if failed and failed.status != "interrupted":
                failed.status = "failed"
                failed.error_code = exc.code
                failed.error_message = exc.message
                failure_db.commit()
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    return {
        "task_id": task.task_id,
        "should_speak": decision,
        "reason": reason,
        "replayed": False,
    }


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
