from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.database import Base
from pgvector.sqlalchemy import Vector
from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column


def uid() -> str:
    return str(uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class AdminUser(Base, TimestampMixin):
    __tablename__ = "admin_users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    account: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    real_name: Mapped[str] = mapped_column(String(100))
    password_hash: Mapped[str] = mapped_column(String(500))
    role: Mapped[str] = mapped_column(String(32), default="admin")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class AdminSession(Base):
    __tablename__ = "admin_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(ForeignKey("admin_users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    csrf_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GatewayKey(Base, TimestampMixin):
    __tablename__ = "gateway_keys"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(100))
    key_prefix: Mapped[str] = mapped_column(String(16), index=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LLMProvider(Base, TimestampMixin):
    __tablename__ = "llm_providers"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(100))
    provider: Mapped[str] = mapped_column(String(64), default="openai")
    base_url: Mapped[str] = mapped_column(String(500), default="")
    api_key_ciphertext: Mapped[str] = mapped_column(Text, default="")
    model_id: Mapped[str] = mapped_column(String(160))
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=120)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    max_retries: Mapped[int] = mapped_column(Integer, default=2)
    rpm_limit: Mapped[int] = mapped_column(Integer, default=60)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)


class PromptVersion(Base, TimestampMixin):
    __tablename__ = "prompt_versions"
    __table_args__ = (UniqueConstraint("prompt_key", "version", name="uq_prompt_key_version"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    prompt_key: Mapped[str] = mapped_column(String(100), index=True)
    name: Mapped[str] = mapped_column(String(120))
    task_type: Mapped[str] = mapped_column(String(64), default="debate")
    version: Mapped[int] = mapped_column(Integer, default=1)
    system_template: Mapped[str] = mapped_column(Text)
    user_template: Mapped[str] = mapped_column(Text)
    variables_schema: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(24), default="draft")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MessageTemplate(Base, TimestampMixin):
    __tablename__ = "message_templates"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    key: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    task_type: Mapped[str] = mapped_column(String(64), default="debate")
    stage_pattern: Mapped[str] = mapped_column(String(160), default="*")
    role: Mapped[str] = mapped_column(String(16), default="user")
    template: Mapped[str] = mapped_column(Text)
    position: Mapped[int] = mapped_column(Integer, default=100)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class ModelPreset(Base, TimestampMixin):
    __tablename__ = "model_presets"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    temperature: Mapped[float] = mapped_column(Float, default=0.7)
    top_p: Mapped[float] = mapped_column(Float, default=0.9)
    max_tokens: Mapped[int] = mapped_column(Integer, default=700)
    seed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stop: Mapped[list[str]] = mapped_column(JSON, default=list)
    presence_penalty: Mapped[float] = mapped_column(Float, default=0)
    frequency_penalty: Mapped[float] = mapped_column(Float, default=0)
    reasoning: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class MemoryPolicy(Base, TimestampMixin):
    __tablename__ = "memory_policies"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    mode: Mapped[str] = mapped_column(String(32), default="layered")
    match_window_messages: Mapped[int] = mapped_column(Integer, default=16)
    retrieval_count: Mapped[int] = mapped_column(Integer, default=5)
    max_context_chars: Mapped[int] = mapped_column(Integer, default=12000)
    retention_days: Mapped[int] = mapped_column(Integer, default=180)
    long_term_requires_review: Mapped[bool] = mapped_column(Boolean, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class AgentPersona(Base, TimestampMixin):
    __tablename__ = "agent_personas"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    profile_key: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(Text, default="")
    style_prompt: Mapped[str] = mapped_column(Text, default="")
    provider_id: Mapped[str] = mapped_column(ForeignKey("llm_providers.id"), index=True)
    prompt_version_id: Mapped[str] = mapped_column(ForeignKey("prompt_versions.id"), index=True)
    model_preset_id: Mapped[str] = mapped_column(ForeignKey("model_presets.id"), index=True)
    memory_policy_id: Mapped[str] = mapped_column(ForeignKey("memory_policies.id"), index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class MemoryItem(Base, TimestampMixin):
    __tablename__ = "memory_items"
    __table_args__ = (Index("ix_memory_scope", "profile_key", "match_id", "status"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    profile_key: Mapped[str] = mapped_column(String(100), index=True)
    match_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    scope: Mapped[str] = mapped_column(String(24), default="match")
    status: Mapped[str] = mapped_column(String(24), default="active")
    source_task_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    content: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1536), nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(ForeignKey("admin_users.id"), nullable=True)


class AgentTask(Base, TimestampMixin):
    __tablename__ = "agent_tasks"
    __table_args__ = (UniqueConstraint("task_id", name="uq_agent_task_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    task_id: Mapped[str] = mapped_column(String(100), index=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    match_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    room_code: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    profile_key: Mapped[str] = mapped_column(String(100), index=True)
    status: Mapped[str] = mapped_column(String(24), default="running")
    content: Mapped[str] = mapped_column(Text, default="")
    error_code: Mapped[str] = mapped_column(String(100), default="")
    error_message: Mapped[str] = mapped_column(Text, default="")
    provider_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    prompt_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    model_preset_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    usage: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    latency_ms: Mapped[float] = mapped_column(Float, default=0)
    interrupted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    actor_user_id: Mapped[str | None] = mapped_column(ForeignKey("admin_users.id"), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(120), index=True)
    target_type: Mapped[str] = mapped_column(String(80))
    target_id: Mapped[str] = mapped_column(String(120))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
