from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def uuid4() -> str:
    return str(uuid.uuid4())


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4)
    account: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    real_name: Mapped[str] = mapped_column(String(64), index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(24), default="user", index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_test_account: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class UserSession(Base):
    __tablename__ = "user_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    csrf_hash: Mapped[str] = mapped_column(String(64))
    user_agent: Mapped[str] = mapped_column(String(255), default="")
    ip_address: Mapped[str] = mapped_column(String(64), default="")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    user: Mapped[User] = relationship()


class Season(Base, TimestampMixin):
    __tablename__ = "seasons"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(100))
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ends_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class AutomationTemplate(Base, TimestampMixin):
    __tablename__ = "automation_templates"
    __table_args__ = (UniqueConstraint("slug", "version", name="uq_template_slug_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4)
    slug: Mapped[str] = mapped_column(String(80), index=True)
    name: Mapped[str] = mapped_column(String(120))
    version: Mapped[int] = mapped_column(Integer, default=1)
    stages: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Competition(Base, TimestampMixin):
    __tablename__ = "competitions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4)
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    tagline: Mapped[str] = mapped_column(String(240), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    rules: Mapped[str] = mapped_column(Text, default="")
    format: Mapped[str] = mapped_column(String(32))
    seat_count: Mapped[int] = mapped_column(Integer)
    ranked: Mapped[bool] = mapped_column(Boolean, default=False)
    allow_custom_topic: Mapped[bool] = mapped_column(Boolean, default=False)
    is_public: Mapped[bool] = mapped_column(Boolean, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    accent: Mapped[str] = mapped_column(String(32), default="blue")
    automation_template_id: Mapped[str] = mapped_column(ForeignKey("automation_templates.id"))
    season_id: Mapped[Optional[str]] = mapped_column(ForeignKey("seasons.id"), nullable=True)
    automation_template: Mapped[AutomationTemplate] = relationship()
    season: Mapped[Optional[Season]] = relationship()


class CompetitionTopic(Base, TimestampMixin):
    __tablename__ = "competition_topics"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4)
    competition_id: Mapped[str] = mapped_column(ForeignKey("competitions.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(300))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class RoomCodeReservation(Base):
    __tablename__ = "room_code_reservations"

    code: Mapped[str] = mapped_column(String(6), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class Room(Base, TimestampMixin):
    __tablename__ = "rooms"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4)
    code: Mapped[str] = mapped_column(String(6), unique=True, index=True)
    creation_key: Mapped[Optional[str]] = mapped_column(String(96), nullable=True, unique=True)
    creation_fingerprint: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_by_user_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("users.id", name="fk_rooms_created_by_user_id"), nullable=True, index=True
    )
    competition_id: Mapped[str] = mapped_column(ForeignKey("competitions.id"), index=True)
    season_id: Mapped[Optional[str]] = mapped_column(ForeignKey("seasons.id"), nullable=True, index=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    topic: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(32), default="lobby", index=True)
    visibility: Mapped[str] = mapped_column(String(16), default="public")
    is_test_data: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    template_snapshot: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    current_stage_index: Mapped[int] = mapped_column(Integer, default=-1)
    stage_started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    stage_deadline_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    paused_remaining_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    seq: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_reason: Mapped[str] = mapped_column(Text, default="")
    competition: Mapped[Competition] = relationship()
    season: Mapped[Optional[Season]] = relationship()
    owner: Mapped[User] = relationship(foreign_keys=[owner_id])
    created_by_user: Mapped[Optional[User]] = relationship(foreign_keys=[created_by_user_id])
    seats: Mapped[list[RoomSeat]] = relationship(back_populates="room", cascade="all, delete-orphan")


class RoomSeat(Base, TimestampMixin):
    __tablename__ = "room_seats"
    __table_args__ = (UniqueConstraint("room_id", "seat_key", name="uq_room_seat"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4)
    room_id: Mapped[str] = mapped_column(ForeignKey("rooms.id", ondelete="CASCADE"), index=True)
    seat_key: Mapped[str] = mapped_column(String(24))
    side: Mapped[str] = mapped_column(String(8))
    position: Mapped[int] = mapped_column(Integer)
    occupant_type: Mapped[str] = mapped_column(String(16), default="open")
    user_id: Mapped[Optional[str]] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    display_name: Mapped[str] = mapped_column(String(64), default="待加入")
    is_ready: Mapped[bool] = mapped_column(Boolean, default=False)
    connected: Mapped[bool] = mapped_column(Boolean, default=False)
    disconnected_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    control_lease: Mapped[str] = mapped_column(String(64), default="")
    control_session_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("user_sessions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    agent_profile_id: Mapped[Optional[str]] = mapped_column(ForeignKey("agent_profiles.id"), nullable=True)
    room: Mapped[Room] = relationship(back_populates="seats")
    user: Mapped[Optional[User]] = relationship()


class SeatRestoreRequest(Base, TimestampMixin):
    """A participant's explicit request to take control back from an AI substitute."""

    __tablename__ = "seat_restore_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4)
    room_id: Mapped[str] = mapped_column(ForeignKey("rooms.id", ondelete="CASCADE"), index=True)
    seat_id: Mapped[str] = mapped_column(ForeignKey("room_seats.id", ondelete="CASCADE"), index=True)
    requester_user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(160), nullable=True, unique=True)
    resolution_reason: Mapped[str] = mapped_column(String(300), default="")
    resolved_by_user_id: Mapped[Optional[str]] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    room: Mapped[Room] = relationship()
    seat: Mapped[RoomSeat] = relationship()
    requester: Mapped[User] = relationship(foreign_keys=[requester_user_id])
    resolved_by: Mapped[Optional[User]] = relationship(foreign_keys=[resolved_by_user_id])


class Match(Base, TimestampMixin):
    __tablename__ = "matches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4)
    room_id: Mapped[str] = mapped_column(ForeignKey("rooms.id"), unique=True, index=True)
    competition_id: Mapped[str] = mapped_column(ForeignKey("competitions.id"), index=True)
    season_id: Mapped[Optional[str]] = mapped_column(ForeignKey("seasons.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="running")
    winner: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    result_reason: Mapped[str] = mapped_column(Text, default="")
    legacy: Mapped[bool] = mapped_column(Boolean, default=False)
    legacy_source_id: Mapped[Optional[str]] = mapped_column(String(120), nullable=True, unique=True)
    judge_profile_id: Mapped[Optional[str]] = mapped_column(ForeignKey("judge_profiles.id"), nullable=True, index=True)
    judge_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    service_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    room: Mapped[Room] = relationship()


class MatchEvent(Base):
    __tablename__ = "match_events"
    __table_args__ = (UniqueConstraint("room_id", "seq", name="uq_room_event_seq"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4)
    room_id: Mapped[str] = mapped_column(ForeignKey("rooms.id", ondelete="CASCADE"), index=True)
    match_id: Mapped[Optional[str]] = mapped_column(ForeignKey("matches.id"), nullable=True, index=True)
    seq: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(80), index=True)
    actor_user_id: Mapped[Optional[str]] = mapped_column(ForeignKey("users.id"), nullable=True)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(120), nullable=True, unique=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class Speech(Base):
    __tablename__ = "speeches"
    __table_args__ = (
        Index(
            "uq_speeches_room_active",
            "room_id",
            unique=True,
            postgresql_where=text("status IN ('speaking', 'synthesizing', 'playing')"),
            sqlite_where=text("status IN ('speaking', 'synthesizing', 'playing')"),
        ),
        Index("ix_speeches_match_created_id", "match_id", "created_at", "id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4)
    match_id: Mapped[str] = mapped_column(ForeignKey("matches.id", ondelete="CASCADE"), index=True)
    room_id: Mapped[str] = mapped_column(ForeignKey("rooms.id", ondelete="CASCADE"), index=True)
    seat_key: Mapped[str] = mapped_column(String(24))
    stage_key: Mapped[str] = mapped_column(String(80))
    speaker_type: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text, default="")
    audio_url: Mapped[str] = mapped_column(String(500), default="")
    duration_seconds: Mapped[float] = mapped_column(Float, default=0)
    playback_started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    playback_ends_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    stream_generation: Mapped[str] = mapped_column(String(64), default="")
    stream_sample_rate: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(24), default="completed")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TranscriptSegment(Base):
    __tablename__ = "transcript_segments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4)
    speech_id: Mapped[str] = mapped_column(ForeignKey("speeches.id", ondelete="CASCADE"), index=True)
    start_ms: Mapped[int] = mapped_column(Integer, default=0)
    end_ms: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str] = mapped_column(Text)
    is_final: Mapped[bool] = mapped_column(Boolean, default=True)


class SpeechDataIssueDisposition(Base, TimestampMixin):
    """An administrator's durable disposition for a missing speech artifact.

    The underlying transcript/audio facts remain untouched.  This record only
    captures the operational follow-up so an issue cannot disappear from the
    data-quality workflow merely because somebody acknowledged it verbally.
    """

    __tablename__ = "speech_data_issue_dispositions"
    __table_args__ = (UniqueConstraint("speech_id", "issue_code", name="uq_speech_data_issue"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4)
    speech_id: Mapped[str] = mapped_column(ForeignKey("speeches.id", ondelete="CASCADE"), index=True)
    issue_code: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(24), index=True)
    note: Mapped[str] = mapped_column(Text, default="")
    reviewed_by_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    reviewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AudioAsset(Base, TimestampMixin):
    __tablename__ = "audio_assets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4)
    match_id: Mapped[Optional[str]] = mapped_column(ForeignKey("matches.id"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(32))
    storage_key: Mapped[str] = mapped_column(String(500))
    mime_type: Mapped[str] = mapped_column(String(80), default="audio/wav")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)


class JudgeScorecard(Base, TimestampMixin):
    __tablename__ = "judge_scorecards"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4)
    match_id: Mapped[str] = mapped_column(ForeignKey("matches.id"), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    task_id: Mapped[str] = mapped_column(String(36), default="")
    winner: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    affirmative_score: Mapped[float] = mapped_column(Float, default=0)
    negative_score: Mapped[float] = mapped_column(Float, default=0)
    individual_scores: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    reasoning: Mapped[str] = mapped_column(Text, default="")
    reviewed_by: Mapped[Optional[str]] = mapped_column(ForeignKey("users.id"), nullable=True)


class LeaderboardEntry(Base, TimestampMixin):
    __tablename__ = "leaderboard_entries"
    __table_args__ = (UniqueConstraint("competition_id", "season_id", "user_id", name="uq_leaderboard_user"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4)
    competition_id: Mapped[str] = mapped_column(ForeignKey("competitions.id"), index=True)
    season_id: Mapped[Optional[str]] = mapped_column(ForeignKey("seasons.id"), nullable=True, index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    points: Mapped[int] = mapped_column(Integer, default=0)
    wins: Mapped[int] = mapped_column(Integer, default=0)
    draws: Mapped[int] = mapped_column(Integer, default=0)
    losses: Mapped[int] = mapped_column(Integer, default=0)
    matches: Mapped[int] = mapped_column(Integer, default=0)
    average_score: Mapped[float] = mapped_column(Float, default=0)
    last_match_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    user: Mapped[User] = relationship()


class RatingChange(Base):
    __tablename__ = "rating_changes"
    __table_args__ = (
        Index(
            "uq_rating_change_initial",
            "match_id",
            "user_id",
            unique=True,
            postgresql_where=text("source = 'initial'"),
            sqlite_where=text("source = 'initial'"),
        ),
        Index(
            "uq_rating_change_idempotency",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
            sqlite_where=text("idempotency_key IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4)
    match_id: Mapped[str] = mapped_column(ForeignKey("matches.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    points_delta: Mapped[int] = mapped_column(Integer)
    score: Mapped[float] = mapped_column(Float, default=0)
    reason: Mapped[str] = mapped_column(String(240))
    source: Mapped[str] = mapped_column(String(24), default="initial")
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AgentProfile(Base, TimestampMixin):
    __tablename__ = "agent_profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(100))
    profile_key: Mapped[str] = mapped_column(String(100), unique=True, index=True, default=uuid4)
    provider: Mapped[str] = mapped_column(String(32), default="debate_agent")
    model_name: Mapped[str] = mapped_column(String(120), default="qwen3.6-27b")
    endpoint: Mapped[str] = mapped_column(String(500), default="http://47.93.206.109:8000/api/debate")
    voice_id: Mapped[str] = mapped_column(String(100), default="debate_voice_1")
    system_prompt: Mapped[str] = mapped_column(Text, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class JudgeProfile(Base, TimestampMixin):
    __tablename__ = "judge_profiles"
    __table_args__ = (
        Index(
            "uq_judge_profiles_one_active",
            "is_active",
            unique=True,
            postgresql_where=text("is_active"),
            sqlite_where=text("is_active = 1"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(100))
    endpoint: Mapped[str] = mapped_column(String(500), default="")
    model_name: Mapped[str] = mapped_column(String(120), default="")
    system_prompt: Mapped[str] = mapped_column(Text, default="")
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=120)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class ProviderConfig(Base, TimestampMixin):
    __tablename__ = "provider_configs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4)
    kind: Mapped[str] = mapped_column(String(32), unique=True)
    endpoint: Mapped[str] = mapped_column(String(500))
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    secret_ciphertext: Mapped[str] = mapped_column(Text, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class AudioCue(Base, TimestampMixin):
    __tablename__ = "audio_cues"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4)
    key: Mapped[str] = mapped_column(String(80), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    text: Mapped[str] = mapped_column(Text, default="")
    audio_url: Mapped[str] = mapped_column(String(500), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class AdminAuditLog(Base):
    __tablename__ = "admin_audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4)
    actor_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    action: Mapped[str] = mapped_column(String(120), index=True)
    target_type: Mapped[str] = mapped_column(String(80))
    target_id: Mapped[str] = mapped_column(String(120))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
