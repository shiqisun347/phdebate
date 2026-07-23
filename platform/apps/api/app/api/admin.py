from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import time
import wave
from datetime import timedelta
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, Response, UploadFile
from sqlalchemy import case, delete, exists, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.core.config import settings
from app.core.database import acquire_transaction_locks, get_db
from app.core.deps import system_admin, verify_csrf
from app.core.secret_crypto import decrypt_secret, encrypt_secret
from app.core.security import as_utc, hash_password, validate_password
from app.models.entities import (
    AdminAuditLog,
    AgentProfile,
    AudioCue,
    AutomationTemplate,
    Competition,
    CompetitionTopic,
    JudgeProfile,
    JudgeScorecard,
    Match,
    MatchEvent,
    ProviderConfig,
    Room,
    RoomSeat,
    Season,
    Speech,
    SpeechCorrectionRequest,
    SpeechDataIssueDisposition,
    TranscriptSegment,
    User,
    UserSession,
    VoiceTelemetry,
)
from app.schemas.requests import (
    AdminPasswordResetRequest,
    AdminRoomDataScopePatch,
    AdminUserPatch,
    AgentProfilePatch,
    AudioCuePatch,
    AutomationTemplateVersionCreate,
    CompetitionPatch,
    JudgeProfileCreate,
    JudgeProfilePatch,
    JudgeRetryRequest,
    JudgeReviewRequest,
    MediaCleanupRequest,
    ProviderConfigUpsert,
    RoomMatchConsistencyRepairRequest,
    SeasonCreate,
    SeasonPatch,
    SpeechCorrectionReview,
    SpeechDataIssueDispositionPatch,
    TopicCreate,
    TopicPatch,
)
from app.services.archive_storage import FINAL_MATCH_STATUSES, inspect_archives, inspect_match_archive
from app.services.health import provider_health
from app.services.match_archive import build_match_archive, enqueue_match_archive
from app.services.match_engine import match_engine
from app.services.media_storage import inspect_media
from app.services.provider_config import SUPPORTED_PROVIDER_KINDS, build_service_snapshot, normalize_provider_config
from app.services.realtime import room_hub
from app.services.room_capacity import ACTIVE_PARTICIPANT_STATUSES, MAX_ACTIVE_ROOMS
from app.services.room_match_consistency import (
    EXPECTED_MATCH_STATUS_BY_TERMINAL_ROOM,
    scan_room_match_consistency,
)
from app.services.room_service import (
    append_event,
    leaderboard,
    load_room,
    now,
    rebuild_leaderboard_entries,
    serialize_competition,
    serialize_room,
    serialize_user,
)
from app.services.seasons import serialize_season
from app.services.speech_correction import (
    enqueue_correction_archive,
    review_correction_request,
    serialize_correction_request,
)
from app.services.system_health import system_readiness
from app.services.voice_telemetry import safe_voice_summary

router = APIRouter(prefix="/api/admin", tags=["admin"])
MAX_AUDIO_CUE_BYTES = 20 * 1024 * 1024
AUDIO_CUE_KEY = re.compile(r"^[a-z][a-z0-9_]{1,79}$")
RETIRED_AUDIT_ACTION_PREFIXES = ("classroom.", "activity.", "consent.", "organization.", "research.")
RETIRED_AUDIT_TARGET_TYPES = {
    "classroom",
    "classroom_membership",
    "teaching_activity",
    "organization",
    "organization_membership",
    "consent_policy",
    "consent_record",
    "research_export",
}
SPEECH_DATA_ISSUE_CODES = {"missing_transcript", "missing_audio", "missing_segments"}


def ensure_mutable_competition(item: Competition) -> None:
    """Legacy imports are historical evidence, not reusable competitions."""
    if item.format == "legacy" or item.slug.startswith("legacy"):
        raise HTTPException(status_code=409, detail="旧系统历史比赛是只读归档，不能修改赛事、赛季或题库。")


def audit(db: Session, actor: User, action: str, target_type: str, target_id: str, payload: dict | None = None) -> None:
    db.add(AdminAuditLog(actor_user_id=actor.id, action=action, target_type=target_type, target_id=target_id, payload=payload or {}))


def serialize_audio_cue(item: AudioCue) -> dict:
    return {
        "id": item.id,
        "key": item.key,
        "name": item.name,
        "text": item.text,
        "audio_url": item.audio_url,
        "is_active": item.is_active,
        "updated_at": item.updated_at.isoformat(),
    }


def serialize_speech_data_issue_disposition(item: SpeechDataIssueDisposition) -> dict:
    return {
        "status": item.status,
        "note": item.note,
        "reviewed_at": item.reviewed_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
    }


def serialize_admin_room_summary(room: Room, *, connected_humans: int, paused_at) -> dict:
    current_stage = (
        dict(room.template_snapshot[room.current_stage_index])
        if 0 <= room.current_stage_index < len(room.template_snapshot or [])
        else None
    )
    attention_reason = ""
    if room.status == "review_required":
        attention_reason = "review_required"
    stale_paused = bool(
        room.status == "paused"
        and connected_humans == 0
        and paused_at is not None
        and as_utc(paused_at)
        <= now() - timedelta(minutes=settings.stale_paused_after_minutes)
    )
    if attention_reason != "review_required":
        if stale_paused:
            attention_reason = "stale_paused"
        elif room.failure_reason:
            attention_reason = "service_failure"
    paused_duration_seconds = (
        max(0, int((now() - as_utc(paused_at)).total_seconds())) if room.status == "paused" and paused_at else None
    )
    return {
        "id": room.id,
        "code": room.code,
        "topic": room.topic,
        "status": room.status,
        "is_test_data": room.is_test_data,
        "competition": {
            "id": room.competition.id,
            "slug": room.competition.slug,
            "name": room.competition.name,
        },
        "current_stage": current_stage,
        "connected_humans": connected_humans,
        "failure_reason": room.failure_reason,
        "paused_at": paused_at.isoformat() if paused_at else None,
        "paused_duration_seconds": paused_duration_seconds,
        "is_stale_paused": stale_paused,
        "capacity_consuming": room.status in {"lobby", "preparing", "running", "paused", "judging"},
        "available_actions": (
            ["retry", "terminate"] if stale_paused and room.failure_reason else ["resume", "terminate"] if stale_paused else []
        ),
        "attention_reason": attention_reason,
        "updated_at": room.updated_at.isoformat(),
    }


def serialize_judge_profile(item: JudgeProfile) -> dict:
    return {
        "id": item.id,
        "name": item.name,
        "endpoint": item.endpoint,
        "model_name": item.model_name,
        "system_prompt": item.system_prompt,
        "timeout_seconds": item.timeout_seconds,
        "is_active": item.is_active,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
    }


def safe_csv_text(value: str | None) -> str:
    """Prevent user-controlled cells from becoming spreadsheet formulas."""
    text = value or ""
    return f"'{text}" if text.startswith(("=", "+", "-", "@", "\t", "\r")) else text


def verify_review_revision(scorecard: JudgeScorecard, expected_updated_at) -> None:
    if as_utc(scorecard.updated_at) != as_utc(expected_updated_at):
        raise HTTPException(status_code=409, detail="该裁判结果已被其他管理员更新，请刷新后重试。")


def serialize_provider_config(item: ProviderConfig) -> dict:
    return {
        "id": item.id,
        "kind": item.kind,
        "endpoint": item.endpoint,
        "settings": item.settings,
        "has_secret": bool(item.secret_ciphertext),
        "is_active": item.is_active,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
    }


async def read_audio_cue_upload(audio: UploadFile) -> bytes:
    data = await audio.read(MAX_AUDIO_CUE_BYTES + 1)
    if len(data) > MAX_AUDIO_CUE_BYTES:
        raise HTTPException(status_code=413, detail="预设语音文件不能超过 20 MiB。")
    try:
        with wave.open(io.BytesIO(data), "rb") as wav:
            if wav.getnchannels() not in {1, 2} or wav.getsampwidth() != 2 or not 8000 <= wav.getframerate() <= 48000:
                raise ValueError
            duration = wav.getnframes() / max(1, wav.getframerate())
            if not 0.1 <= duration <= 600:
                raise ValueError
    except (EOFError, ValueError, wave.Error) as exc:
        raise HTTPException(status_code=415, detail="预设语音必须是 0.1–600 秒的 PCM WAV 文件。") from exc
    return data


@router.get("/dashboard")
async def dashboard(admin: User = Depends(system_admin), db: Session = Depends(get_db)) -> dict:
    counts = {
        "users": db.scalar(select(func.count(User.id))) or 0,
        "competitions": db.scalar(select(func.count(Competition.id))) or 0,
        "live_rooms": db.scalar(select(func.count(Room.id)).where(Room.status.in_(["preparing", "running", "paused", "judging"]))) or 0,
        "review_required": db.scalar(select(func.count(Match.id)).where(Match.status == "review_required")) or 0,
    }
    rooms = db.scalars(select(Room).order_by(Room.updated_at.desc()).limit(20)).all()
    room_rows = [
        {"code": item.code, "topic": item.topic, "status": item.status, "updated_at": item.updated_at.isoformat()} for item in rooms
    ]
    active_judge = db.scalar(
        select(JudgeProfile).where(JudgeProfile.is_active.is_(True)).order_by(JudgeProfile.created_at, JudgeProfile.id)
    )
    judge_health_profile = serialize_judge_profile(active_judge) if active_judge else None
    service_health_configs = build_service_snapshot(db)
    db.rollback()
    providers = await provider_health(judge_profile=judge_health_profile, service_configs=service_health_configs)
    readiness = await system_readiness(db)
    return {
        "counts": counts,
        "rooms": room_rows,
        "providers": providers,
        "system_health": readiness,
        "leaderboard": leaderboard(db, limit=10, include_test_accounts=True),
    }


@router.get("/users")
def users(
    page: int = Query(default=1, ge=1, le=100000),
    page_size: int = Query(default=100, ge=1, le=200),
    q: str = Query(default="", max_length=64),
    admin: User = Depends(system_admin),
    db: Session = Depends(get_db),
) -> dict:
    filters = []
    query = q.strip().lower()
    if query:
        filters.append(or_(func.lower(User.account).contains(query), func.lower(User.real_name).contains(query)))
    total = db.scalar(select(func.count(User.id)).where(*filters)) or 0
    pagination = _pagination(page, page_size, total)
    effective_page = pagination["page"]
    rows = db.scalars(
        select(User)
        .where(*filters)
        .order_by(User.created_at.desc(), User.id)
        .offset((effective_page - 1) * page_size)
        .limit(page_size)
    ).all()
    return {
        "items": [serialize_user(item) | {"created_at": item.created_at.isoformat()} for item in rows],
        "pagination": pagination,
    }


@router.patch("/users/{user_id}")
async def patch_user(user_id: str, payload: AdminUserPatch, admin: User = Depends(verify_csrf), db: Session = Depends(get_db)) -> dict:
    if admin.role != "system_admin":
        raise HTTPException(status_code=403, detail="仅系统管理员可操作。")
    active_admins = list(
        db.scalars(select(User).where(User.role == "system_admin", User.is_active.is_(True)).order_by(User.id).with_for_update()).all()
    )
    user = db.scalar(select(User).where(User.id == user_id).with_for_update())
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在。")
    if user.id == admin.id and payload.is_active is False:
        raise HTTPException(status_code=409, detail="不能停用当前管理员账号。")
    if user.id == admin.id and payload.role == "user":
        raise HTTPException(status_code=409, detail="不能取消当前账号的管理员权限。")
    removes_active_admin = user.role == "system_admin" and user.is_active and (payload.is_active is False or payload.role == "user")
    if removes_active_admin and not any(item.id != user.id for item in active_admins):
        raise HTTPException(status_code=409, detail="系统必须保留至少一个有效管理员账号。")
    old_is_active = user.is_active
    old_role = user.role
    old_is_test_account = user.is_test_account
    archive_match_ids: set[str] = set()
    classified_rooms: list[tuple[str, int]] = []
    if payload.is_active is not None:
        user.is_active = payload.is_active
    if payload.role is not None:
        user.role = payload.role
    if payload.is_test_account is not None:
        user.is_test_account = payload.is_test_account
        if payload.is_test_account:
            test_room_ids = select(RoomSeat.room_id).where(RoomSeat.user_id == user.id)
            test_rooms = list(
                db.scalars(
                    select(Room)
                    .where(
                        or_(
                            Room.owner_id == user.id,
                            Room.created_by_user_id == user.id,
                            Room.id.in_(test_room_ids),
                        )
                    )
                    .order_by(Room.id)
                    .with_for_update()
                ).all()
            )
            rebuild_scopes: dict[tuple[str, str | None], set[str]] = {}
            for test_room in test_rooms:
                if test_room.is_test_data:
                    continue
                test_room.is_test_data = True
                classified_rooms.append((test_room.code, test_room.seq))
                match_id = db.scalar(select(Match.id).where(Match.room_id == test_room.id))
                if match_id:
                    archive_match_ids.add(match_id)
                affected = {seat.user_id for seat in test_room.seats if seat.user_id}
                rebuild_scopes.setdefault((test_room.competition_id, test_room.season_id), set()).update(affected)
            for (competition_id, season_id), affected_user_ids in rebuild_scopes.items():
                rebuild_leaderboard_entries(
                    db,
                    competition_id=competition_id,
                    season_id=season_id,
                    user_ids=affected_user_ids,
                )
            audit_payload_room_count = len(test_rooms)
    deactivated = old_is_active and payload.is_active is False
    role_changed = payload.role is not None and payload.role != old_role
    revoked_sessions = 0
    if deactivated or role_changed:
        result = db.execute(delete(UserSession).where(UserSession.user_id == user.id))
        revoked_sessions = result.rowcount or 0
    audit_payload = payload.model_dump(exclude_none=True)
    if payload.is_test_account:
        audit_payload["rooms_classified_as_test"] = audit_payload_room_count
        audit_payload["archives_refresh_queued"] = len(archive_match_ids)
    if payload.is_test_account is not None and payload.is_test_account != old_is_test_account:
        audit_payload["ranking_visibility"] = "excluded" if payload.is_test_account else "included_for_future_matches"
    if revoked_sessions:
        audit_payload["revoked_sessions"] = revoked_sessions
    audit(db, admin, "user.patch", "user", user.id, audit_payload)
    db.commit()
    for match_id in archive_match_ids:
        enqueue_match_archive(match_id)
    for room_code, room_seq in classified_rooms:
        await room_hub.publish(
            room_code,
            {"type": "room.data_scope.updated", "room_code": room_code, "seq": room_seq},
        )
    return {"user": serialize_user(user)}


@router.patch("/rooms/{code}/data-scope")
async def patch_room_data_scope(
    code: str,
    payload: AdminRoomDataScopePatch,
    admin: User = Depends(verify_csrf),
    db: Session = Depends(get_db),
) -> dict:
    if admin.role != "system_admin":
        raise HTTPException(status_code=403, detail="仅系统管理员可操作。")
    room = load_room(db, code, lock=True)
    if room.is_test_data == payload.is_test_data:
        return {"room": serialize_room(db, room, admin), "replayed": True}
    if not payload.is_test_data and room.status not in {"lobby", "cancelled"}:
        raise HTTPException(status_code=409, detail="比赛开始后不能恢复为正式数据，避免改写历史统计。")
    affected_user_ids = {seat.user_id for seat in room.seats if seat.user_id}
    archive_match_id = db.scalar(select(Match.id).where(Match.room_id == room.id))
    room.is_test_data = payload.is_test_data
    if payload.is_test_data:
        rebuild_leaderboard_entries(
            db,
            competition_id=room.competition_id,
            season_id=room.season_id,
            user_ids=affected_user_ids,
        )
    audit(
        db,
        admin,
        "room.data_scope.patch",
        "room",
        room.id,
        {"room_code": room.code, "is_test_data": room.is_test_data, "affected_users": len(affected_user_ids)},
    )
    db.commit()
    if archive_match_id:
        enqueue_match_archive(archive_match_id)
    await room_hub.publish(room.code, {"type": "room.data_scope.updated", "room_code": room.code, "seq": room.seq})
    return {"room": serialize_room(db, load_room(db, code), admin), "replayed": False}


@router.post("/users/{user_id}/reset-password")
def reset_user_password(
    user_id: str,
    payload: AdminPasswordResetRequest,
    admin: User = Depends(verify_csrf),
    db: Session = Depends(get_db),
) -> dict:
    if admin.role != "system_admin":
        raise HTTPException(status_code=403, detail="仅系统管理员可操作。")
    if user_id == admin.id:
        raise HTTPException(status_code=409, detail="当前管理员请在个人中心使用密码修改功能。")
    user = db.scalar(select(User).where(User.id == user_id).with_for_update())
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在。")
    try:
        validate_password(payload.new_password)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    user.password_hash = hash_password(payload.new_password)
    result = db.execute(delete(UserSession).where(UserSession.user_id == user.id))
    audit(
        db,
        admin,
        "user.password_reset",
        "user",
        user.id,
        {"revoked_sessions": result.rowcount or 0},
    )
    db.commit()
    return {"ok": True, "revoked_sessions": result.rowcount or 0}


@router.get("/seasons")
def admin_seasons(admin: User = Depends(system_admin), db: Session = Depends(get_db)) -> dict:
    rows = db.scalars(select(Season).order_by(Season.starts_at.desc(), Season.id)).all()
    return {
        "items": [
            serialize_season(item)
            | {
                "competition_count": db.scalar(select(func.count(Competition.id)).where(Competition.season_id == item.id)) or 0,
                "match_count": db.scalar(select(func.count(Match.id)).where(Match.season_id == item.id)) or 0,
            }
            for item in rows
        ]
    }


@router.post("/seasons")
def create_season(
    payload: SeasonCreate,
    admin: User = Depends(verify_csrf),
    db: Session = Depends(get_db),
) -> dict:
    if admin.role != "system_admin":
        raise HTTPException(status_code=403, detail="仅系统管理员可管理赛季。")
    if db.scalar(select(Season.id).where(Season.slug == payload.slug)):
        raise HTTPException(status_code=409, detail="赛季标识已存在。")
    item = Season(**payload.model_dump())
    db.add(item)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="赛季创建冲突，请刷新后重试。") from exc
    audit(db, admin, "season.create", "season", item.id, payload.model_dump(mode="json"))
    db.commit()
    return {"season": serialize_season(item)}


@router.patch("/seasons/{season_id}")
def patch_season(
    season_id: str,
    payload: SeasonPatch,
    admin: User = Depends(verify_csrf),
    db: Session = Depends(get_db),
) -> dict:
    if admin.role != "system_admin":
        raise HTTPException(status_code=403, detail="仅系统管理员可管理赛季。")
    item = db.scalar(select(Season).where(Season.id == season_id).with_for_update())
    if not item:
        raise HTTPException(status_code=404, detail="赛季不存在。")
    changes = payload.model_dump(exclude_unset=True)
    starts_at = changes.get("starts_at", item.starts_at)
    ends_at = changes.get("ends_at", item.ends_at)
    if ends_at is not None and as_utc(ends_at) <= as_utc(starts_at):
        raise HTTPException(status_code=422, detail="赛季结束时间必须晚于开始时间。")
    if {"starts_at", "ends_at"}.intersection(changes) and db.scalar(select(Match.id).where(Match.season_id == item.id).limit(1)):
        raise HTTPException(status_code=409, detail="已有比赛记录的赛季不能修改起止时间。")
    if changes.get("is_active") is False:
        bound = db.scalar(
            select(Competition.name)
            .where(Competition.season_id == item.id, Competition.is_active.is_(True), Competition.ranked.is_(True))
            .limit(1)
        )
        if bound:
            raise HTTPException(status_code=409, detail=f"积分赛事“{bound}”仍绑定该赛季，请先切换赛事赛季。")
    for key, value in changes.items():
        setattr(item, key, value)
    audit(db, admin, "season.patch", "season", item.id, payload.model_dump(mode="json", exclude_unset=True))
    db.commit()
    return {"season": serialize_season(item)}


@router.get("/competitions")
def admin_competitions(admin: User = Depends(system_admin), db: Session = Depends(get_db)) -> dict:
    rows = db.scalars(select(Competition).order_by(Competition.created_at)).all()
    return {
        "items": [
            serialize_competition(
                item,
                topics=list(
                    db.scalars(
                        select(CompetitionTopic)
                        .where(CompetitionTopic.competition_id == item.id)
                        .order_by(CompetitionTopic.created_at, CompetitionTopic.id)
                    ).all()
                ),
            )
            for item in rows
        ]
    }


def serialize_automation_template(item: AutomationTemplate, competitions: list[Competition]) -> dict:
    return {
        "id": item.id,
        "slug": item.slug,
        "name": item.name,
        "version": item.version,
        "stages": item.stages,
        "is_active": item.is_active,
        "competitions": [{"id": competition.id, "name": competition.name} for competition in competitions],
        "created_at": item.created_at.isoformat(),
    }


def ensure_competition_can_accept_new_rooms(db: Session, competition: Competition) -> None:
    if not competition.allow_custom_topic:
        active_topics = db.scalar(
            select(func.count(CompetitionTopic.id)).where(
                CompetitionTopic.competition_id == competition.id,
                CompetitionTopic.is_active.is_(True),
            )
        ) or 0
        if active_topics == 0:
            raise HTTPException(status_code=409, detail="赛事至少需要一个启用中的辩题才能开放参赛。")
    template = db.get(AutomationTemplate, competition.automation_template_id) if competition.automation_template_id else None
    if not template or not template.is_active or not template.stages:
        raise HTTPException(status_code=409, detail="赛事缺少可用的自动流程模板，不能开放参赛。")
    if competition.ranked:
        season = db.get(Season, competition.season_id) if competition.season_id else None
        if not season or not season.is_active:
            raise HTTPException(status_code=409, detail="积分赛事必须绑定一个启用中的赛季才能开放参赛。")


@router.get("/automation-templates")
def automation_templates(admin: User = Depends(system_admin), db: Session = Depends(get_db)) -> dict:
    rows = db.scalars(select(AutomationTemplate).order_by(AutomationTemplate.slug, AutomationTemplate.version.desc())).all()
    competitions = list(db.scalars(select(Competition).order_by(Competition.created_at)).all())
    return {
        "items": [
            serialize_automation_template(
                item,
                [competition for competition in competitions if competition.automation_template_id == item.id],
            )
            for item in rows
        ]
    }


@router.post("/automation-templates/{template_id}/versions")
def create_automation_template_version(
    template_id: str,
    payload: AutomationTemplateVersionCreate,
    admin: User = Depends(verify_csrf),
    db: Session = Depends(get_db),
) -> dict:
    if admin.role != "system_admin":
        raise HTTPException(status_code=403, detail="仅系统管理员可操作。")
    source = db.scalar(select(AutomationTemplate).where(AutomationTemplate.id == template_id).with_for_update())
    if not source:
        raise HTTPException(status_code=404, detail="自动流程模板不存在。")
    if source.slug.startswith("legacy"):
        raise HTTPException(status_code=409, detail="旧系统自动流程是只读归档，不能创建新版本。")
    competitions = list(db.scalars(select(Competition).where(Competition.id.in_(payload.competition_ids)).order_by(Competition.id)).all())
    if len(competitions) != len(payload.competition_ids):
        raise HTTPException(status_code=404, detail="绑定赛事中包含不存在的记录。")
    for competition in competitions:
        ensure_mutable_competition(competition)
    stage_rows = [stage.model_dump(exclude_none=True) for stage in payload.stages]
    for competition in competitions:
        maximum_position = max(1, competition.seat_count // 2)
        for stage in payload.stages:
            if stage.seat and int(stage.seat.split("_", 1)[1]) > maximum_position:
                raise HTTPException(
                    status_code=422,
                    detail=f"阶段 {stage.name} 的席位 {stage.seat} 超出赛事 {competition.name} 的席位范围。",
                )
    latest = db.scalar(
        select(AutomationTemplate).where(AutomationTemplate.slug == source.slug).order_by(AutomationTemplate.version.desc()).limit(1)
    )
    if latest and latest.name == (payload.name or source.name) and latest.stages == stage_rows:
        latest_competition_ids = set(db.scalars(select(Competition.id).where(Competition.automation_template_id == latest.id)).all())
        if latest_competition_ids == set(payload.competition_ids):
            return {"template": serialize_automation_template(latest, competitions), "replayed": True}
    latest_version = db.scalar(select(func.max(AutomationTemplate.version)).where(AutomationTemplate.slug == source.slug)) or 0
    created = AutomationTemplate(
        slug=source.slug,
        name=payload.name or source.name,
        version=latest_version + 1,
        stages=stage_rows,
        is_active=True,
    )
    db.add(created)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="自动流程版本创建冲突，请重试。") from exc
    for competition in competitions:
        competition.automation_template_id = created.id
    audit(
        db,
        admin,
        "automation_template.version_create",
        "automation_template",
        created.id,
        {
            "source_template_id": source.id,
            "slug": created.slug,
            "version": created.version,
            "stage_count": len(stage_rows),
            "competition_ids": payload.competition_ids,
        },
    )
    db.commit()
    return {"template": serialize_automation_template(created, competitions)}


@router.patch("/competitions/{competition_id}")
def patch_competition(
    competition_id: str, payload: CompetitionPatch, admin: User = Depends(verify_csrf), db: Session = Depends(get_db)
) -> dict:
    if admin.role != "system_admin":
        raise HTTPException(status_code=403, detail="仅系统管理员可操作。")
    item = db.scalar(select(Competition).where(Competition.id == competition_id).with_for_update())
    if not item:
        raise HTTPException(status_code=404, detail="赛事不存在。")
    ensure_mutable_competition(item)
    changes = payload.model_dump(exclude_none=True)
    if "season_id" in changes:
        season = db.get(Season, changes["season_id"])
        if not season:
            raise HTTPException(status_code=404, detail="赛季不存在。")
        if not season.is_active:
            raise HTTPException(status_code=409, detail="不能把赛事绑定到已停用赛季。")
    for key, value in changes.items():
        setattr(item, key, value)
    if changes.get("is_active") is True:
        ensure_competition_can_accept_new_rooms(db, item)
    audit(db, admin, "competition.patch", "competition", item.id, changes)
    db.commit()
    return {"competition": serialize_competition(item)}


@router.post("/competitions/{competition_id}/topics")
def create_topic(competition_id: str, payload: TopicCreate, admin: User = Depends(verify_csrf), db: Session = Depends(get_db)) -> dict:
    if admin.role != "system_admin":
        raise HTTPException(status_code=403, detail="仅系统管理员可操作。")
    competition = db.scalar(select(Competition).where(Competition.id == competition_id).with_for_update())
    if not competition:
        raise HTTPException(status_code=404, detail="赛事不存在。")
    ensure_mutable_competition(competition)
    topic = CompetitionTopic(competition_id=competition_id, title=payload.title.strip())
    db.add(topic)
    db.flush()
    audit(db, admin, "topic.create", "topic", topic.id, {"title": topic.title})
    db.commit()
    return {"topic": {"id": topic.id, "title": topic.title}}


@router.patch("/competitions/{competition_id}/topics/{topic_id}")
def patch_topic(
    competition_id: str,
    topic_id: str,
    payload: TopicPatch,
    admin: User = Depends(verify_csrf),
    db: Session = Depends(get_db),
) -> dict:
    if admin.role != "system_admin":
        raise HTTPException(status_code=403, detail="仅系统管理员可操作。")
    competition = db.scalar(select(Competition).where(Competition.id == competition_id).with_for_update())
    if not competition:
        raise HTTPException(status_code=404, detail="赛事不存在。")
    ensure_mutable_competition(competition)
    topic = db.scalar(
        select(CompetitionTopic).where(
            CompetitionTopic.id == topic_id,
            CompetitionTopic.competition_id == competition_id,
        )
    )
    if not topic:
        raise HTTPException(status_code=404, detail="赛事题目不存在。")
    changes = payload.model_dump(exclude_none=True)
    if changes.get("is_active") is False and competition.is_active and not competition.allow_custom_topic:
        remaining_active_topics = db.scalar(
            select(func.count(CompetitionTopic.id)).where(
                CompetitionTopic.competition_id == competition.id,
                CompetitionTopic.is_active.is_(True),
                CompetitionTopic.id != topic.id,
            )
        ) or 0
        if remaining_active_topics == 0:
            raise HTTPException(
                status_code=409,
                detail="开放中的赛事必须保留至少一个启用辩题；请先停用赛事，再停用最后一个辩题。",
            )
    for key, value in changes.items():
        setattr(topic, key, value)
    audit(db, admin, "topic.patch", "topic", topic.id, changes)
    db.commit()
    return {"topic": {"id": topic.id, "title": topic.title, "is_active": topic.is_active}}


@router.get("/rooms")
def admin_rooms(
    page: int = Query(default=1, ge=1, le=100000),
    page_size: int = Query(default=100, ge=1, le=200),
    q: str = Query(default="", max_length=100),
    status: str = Query(
        default="",
        pattern="^(|lobby|preparing|running|paused|judging|review_required|completed|terminated|cancelled)$",
    ),
    data_scope: str = Query(default="", pattern="^(|production|qa)$"),
    admin: User = Depends(system_admin),
    db: Session = Depends(get_db),
) -> dict:
    filters = []
    query = q.strip().lower()
    if query:
        filters.append(or_(func.lower(Room.code).contains(query), func.lower(Room.topic).contains(query)))
    if status.strip():
        filters.append(Room.status == status.strip())
    if data_scope == "qa":
        filters.append(Room.is_test_data.is_(True))
    elif data_scope == "production":
        filters.append(Room.is_test_data.is_(False))
    connected_human_exists = exists(
        select(RoomSeat.id).where(
            RoomSeat.room_id == Room.id,
            RoomSeat.occupant_type == "human",
            RoomSeat.connected.is_(True),
        )
    )
    stale_cutoff = now() - timedelta(minutes=settings.stale_paused_after_minutes)
    metrics = db.execute(
        select(
            select(func.count(Room.id)).where(*filters).scalar_subquery().label("filtered_total"),
            select(func.count(Room.id))
            .where(Room.status.in_(ACTIVE_PARTICIPANT_STATUSES))
            .scalar_subquery()
            .label("open_rooms"),
            select(func.count(Room.id))
            .where(Room.status == "paused")
            .scalar_subquery()
            .label("paused_rooms"),
            select(func.count(Room.id))
            .where(
                Room.status == "paused",
                ~connected_human_exists,
                # Use the room's last mutation for the global aggregate.  It is
                # deliberately conservative (any later recovery/presence write
                # postpones staleness) and avoids scanning the full event log.
                Room.updated_at <= stale_cutoff,
            )
            .scalar_subquery()
            .label("stale_paused_rooms"),
        )
    ).one()
    total = int(metrics.filtered_total or 0)
    pagination = _pagination(page, page_size, total)
    effective_page = pagination["page"]
    rooms = list(
        db.scalars(
            select(Room)
            .options(joinedload(Room.competition))
            .where(*filters)
            .order_by(Room.updated_at.desc(), Room.id)
            .offset((effective_page - 1) * page_size)
            .limit(page_size)
        )
        .unique()
        .all()
    )
    room_ids = [room.id for room in rooms]
    connected_counts = (
        dict(
            db.execute(
                select(RoomSeat.room_id, func.count(RoomSeat.id))
                .where(
                    RoomSeat.room_id.in_(room_ids),
                    RoomSeat.occupant_type == "human",
                    RoomSeat.connected.is_(True),
                )
                .group_by(RoomSeat.room_id)
            ).all()
        )
        if room_ids
        else {}
    )
    pause_times = (
        dict(
            db.execute(
                select(MatchEvent.room_id, func.max(MatchEvent.created_at))
                .where(
                    MatchEvent.room_id.in_(room_ids),
                    MatchEvent.event_type.in_(("control.pause", "engine.quarantined", "provider.failed")),
                )
                .group_by(MatchEvent.room_id)
            ).all()
        )
        if room_ids
        else {}
    )
    return {
        "items": [
            serialize_admin_room_summary(
                room,
                connected_humans=int(connected_counts.get(room.id, 0)),
                paused_at=pause_times.get(room.id) or (room.updated_at if room.status == "paused" else None),
            )
            for room in rooms
        ],
        "pagination": pagination,
        "capacity": {
            "limit": MAX_ACTIVE_ROOMS,
            "open_room_count": int(metrics.open_rooms or 0),
            "available_room_slots": max(0, MAX_ACTIVE_ROOMS - int(metrics.open_rooms or 0)),
            "paused_room_count": int(metrics.paused_rooms or 0),
            "stale_paused_room_count": int(metrics.stale_paused_rooms or 0),
        },
    }


@router.get("/consistency/room-matches")
def room_match_consistency(
    room_code: str = Query(default="", max_length=6),
    limit: int = Query(default=200, ge=1, le=1000),
    admin: User = Depends(system_admin),
    db: Session = Depends(get_db),
) -> dict:
    del admin
    issues = scan_room_match_consistency(db, room_code=room_code.strip(), limit=limit)
    return {
        "items": [item.serialize() for item in issues],
        "summary": {
            "issue_count": len(issues),
            "repairable_count": sum(1 for item in issues if item.repairable),
            "non_repairable_count": sum(1 for item in issues if not item.repairable),
        },
        "generated_at": now().isoformat(),
    }


@router.post("/consistency/room-matches/{code}/repair")
async def repair_room_match_consistency(
    code: str,
    payload: RoomMatchConsistencyRepairRequest,
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    admin: User = Depends(verify_csrf),
    db: Session = Depends(get_db),
) -> dict:
    if admin.role != "system_admin":
        raise HTTPException(status_code=403, detail="仅系统管理员可操作。")
    room = load_room(db, code, lock=True)
    match = db.scalar(select(Match).where(Match.room_id == room.id).with_for_update())
    if not match:
        raise HTTPException(status_code=404, detail="该房间没有比赛记录。")
    expected_match_status = EXPECTED_MATCH_STATUS_BY_TERMINAL_ROOM.get(payload.expected_room_status)
    if expected_match_status != payload.expected_match_status:
        raise HTTPException(status_code=422, detail="房间终态与目标比赛状态不匹配。")

    operation_key = None
    if idempotency_key:
        operation_key = "consistency-repair:" + hashlib.sha256(
            f"{room.id}:{admin.id}:{idempotency_key[:128]}".encode()
        ).hexdigest()
        previous = db.scalar(select(MatchEvent).where(MatchEvent.idempotency_key == operation_key))
        if previous:
            if previous.payload.get("reason") != payload.reason:
                raise HTTPException(status_code=409, detail="同一个幂等键不能用于不同的补偿原因。")
            return {"room": serialize_room(db, room, admin), "replayed": True, "issue": previous.payload}

    if room.status != payload.expected_room_status:
        raise HTTPException(status_code=409, detail="房间状态已变化，请重新扫描后再修复。")
    if match.status == payload.expected_match_status:
        return {"room": serialize_room(db, room, admin), "replayed": True, "already_consistent": True}
    issue = scan_room_match_consistency(db, room_code=room.code, limit=1)
    if not issue or not issue[0].repairable or issue[0].expected_match_status != payload.expected_match_status:
        raise HTTPException(status_code=409, detail="当前状态不再符合可安全补偿的异常，请重新扫描。")

    old_status = match.status
    match.status = payload.expected_match_status
    compensation = {
        "room_code": room.code,
        "room_status": room.status,
        "match_id": match.id,
        "old_match_status": old_status,
        "new_match_status": match.status,
        "reason": payload.reason,
    }
    append_event(
        db,
        room,
        "consistency.match_status_compensated",
        compensation,
        actor_user_id=admin.id,
        idempotency_key=operation_key,
    )
    audit(db, admin, "consistency.match_status_compensated", "room", room.id, compensation)
    db.commit()
    enqueue_match_archive(match.id)
    await room_hub.publish(room.code, {"type": "consistency.match_status_compensated", "room_code": room.code, "seq": room.seq})
    return {"room": serialize_room(db, load_room(db, code), admin), "replayed": False, "issue": compensation}


@router.get("/rooms/{code}/voice-telemetry")
def room_voice_telemetry(
    code: str,
    limit: int = Query(default=20, ge=1, le=100),
    admin: User = Depends(system_admin),
    db: Session = Depends(get_db),
) -> dict:
    room = load_room(db, code)
    rows = db.execute(
        select(VoiceTelemetry, Speech)
        .join(Speech, Speech.id == VoiceTelemetry.speech_id)
        .where(VoiceTelemetry.room_id == room.id)
        .order_by(VoiceTelemetry.created_at.desc())
        .limit(limit)
    ).all()
    # Deliberately no Speech.content, audio URL, provider key or raw identity.
    return {"items": [safe_voice_summary(item, speech) for item, speech in rows]}


@router.get("/agents")
def agents(admin: User = Depends(system_admin), db: Session = Depends(get_db)) -> dict:
    rows = db.scalars(select(AgentProfile).order_by(AgentProfile.created_at)).all()
    return {
        "items": [
            {
                "id": item.id,
                "name": item.name,
                "profile_key": item.profile_key,
                "provider": item.provider,
                "voice_id": item.voice_id,
                "is_active": item.is_active,
            }
            for item in rows
        ]
    }


@router.patch("/agents/{agent_id}")
def patch_agent(
    agent_id: str,
    payload: AgentProfilePatch,
    admin: User = Depends(verify_csrf),
    db: Session = Depends(get_db),
) -> dict:
    if admin.role != "system_admin":
        raise HTTPException(status_code=403, detail="仅系统管理员可操作。")
    item = db.get(AgentProfile, agent_id)
    if not item:
        raise HTTPException(status_code=404, detail="AI 辩手配置不存在。")
    changes = payload.model_dump(exclude_none=True)
    active_profiles = list(
        db.scalars(select(AgentProfile).where(AgentProfile.is_active.is_(True)).order_by(AgentProfile.id).with_for_update()).all()
    )
    if changes.get("is_active") is False and item.is_active and len(active_profiles) <= 1:
        raise HTTPException(status_code=409, detail="系统必须至少保留一个启用的 AI 辩手配置。")
    mutable_runtime_fields = {"name", "profile_key", "voice_id"}
    if mutable_runtime_fields.intersection(changes):
        active_room = db.scalar(
            select(Room.code)
            .join(RoomSeat, RoomSeat.room_id == Room.id)
            .where(
                RoomSeat.agent_profile_id == item.id,
                Room.status.in_(["preparing", "running", "paused", "judging"]),
            )
            .limit(1)
        )
        if active_room:
            raise HTTPException(status_code=409, detail=f"AI 配置正被活跃房间 #{active_room} 使用，请在比赛结束后修改。")
    for key, value in changes.items():
        setattr(item, key, value)
    audit(db, admin, "agent.patch", "agent_profile", item.id, changes)
    db.commit()
    return {
        "agent": {
            "id": item.id,
            "name": item.name,
            "profile_key": item.profile_key,
            "provider": item.provider,
            "voice_id": item.voice_id,
            "is_active": item.is_active,
        }
    }


@router.get("/judges")
def judges(admin: User = Depends(system_admin), db: Session = Depends(get_db)) -> dict:
    rows = db.scalars(select(JudgeProfile).order_by(JudgeProfile.created_at, JudgeProfile.id)).all()
    return {"items": [serialize_judge_profile(item) for item in rows]}


@router.post("/judges")
def create_judge(
    payload: JudgeProfileCreate,
    admin: User = Depends(verify_csrf),
    db: Session = Depends(get_db),
) -> dict:
    if admin.role != "system_admin":
        raise HTTPException(status_code=403, detail="仅系统管理员可管理裁判配置。")
    profiles = list(db.scalars(select(JudgeProfile).order_by(JudgeProfile.id).with_for_update()).all())
    if any(item.name.casefold() == payload.name.casefold() for item in profiles):
        raise HTTPException(status_code=409, detail="裁判配置名称已存在。")
    activate = payload.is_active or not any(profile.is_active for profile in profiles)
    if activate:
        for profile in profiles:
            profile.is_active = False
        db.flush()
    item = JudgeProfile(**(payload.model_dump() | {"is_active": activate}))
    try:
        db.add(item)
        db.flush()
        audit(
            db,
            admin,
            "judge_profile.create",
            "judge_profile",
            item.id,
            {"name": item.name, "endpoint": item.endpoint, "model_name": item.model_name, "is_active": item.is_active},
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="裁判配置发生并发冲突，请刷新后重试。") from exc
    return {"judge": serialize_judge_profile(item)}


@router.patch("/judges/{judge_id}")
def patch_judge(
    judge_id: str,
    payload: JudgeProfilePatch,
    admin: User = Depends(verify_csrf),
    db: Session = Depends(get_db),
) -> dict:
    if admin.role != "system_admin":
        raise HTTPException(status_code=403, detail="仅系统管理员可管理裁判配置。")
    profiles = list(db.scalars(select(JudgeProfile).order_by(JudgeProfile.id).with_for_update()).all())
    item = next((profile for profile in profiles if profile.id == judge_id), None)
    if not item:
        raise HTTPException(status_code=404, detail="裁判配置不存在。")
    changes = payload.model_dump(exclude_none=True)
    if "name" in changes and any(profile.id != item.id and profile.name.casefold() == changes["name"].casefold() for profile in profiles):
        raise HTTPException(status_code=409, detail="裁判配置名称已存在。")
    runtime_fields = {"endpoint", "model_name", "system_prompt", "timeout_seconds"}
    if runtime_fields.intersection(changes):
        active_room = db.scalar(
            select(Room.code)
            .join(Match, Match.room_id == Room.id)
            .where(
                Match.judge_profile_id == item.id,
                Room.status.in_(["preparing", "running", "paused", "judging"]),
            )
            .limit(1)
        )
        if active_room:
            raise HTTPException(status_code=409, detail=f"裁判配置正被活跃房间 #{active_room} 使用，请在比赛结束后修改。")
    if changes.get("is_active") is True:
        for profile in profiles:
            if profile.id != item.id:
                profile.is_active = False
        db.flush()
    elif (
        changes.get("is_active") is False
        and item.is_active
        and not any(profile.id != item.id and profile.is_active for profile in profiles)
    ):
        raise HTTPException(status_code=409, detail="系统必须至少保留一个启用的裁判配置。")
    for key, value in changes.items():
        setattr(item, key, value)
    audit(db, admin, "judge_profile.patch", "judge_profile", item.id, changes)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="裁判配置发生并发冲突，请刷新后重试。") from exc
    return {"judge": serialize_judge_profile(item)}


@router.get("/providers")
def provider_configs(admin: User = Depends(system_admin), db: Session = Depends(get_db)) -> dict:
    rows = db.scalars(select(ProviderConfig).where(ProviderConfig.kind.in_(SUPPORTED_PROVIDER_KINDS)).order_by(ProviderConfig.kind)).all()
    return {"items": [serialize_provider_config(item) for item in rows]}


@router.put("/providers/{kind}")
def upsert_provider_config(
    kind: str,
    payload: ProviderConfigUpsert,
    admin: User = Depends(verify_csrf),
    db: Session = Depends(get_db),
) -> dict:
    if admin.role != "system_admin":
        raise HTTPException(status_code=403, detail="仅系统管理员可管理服务配置。")
    normalized_kind = kind.strip().lower()
    try:
        endpoint, normalized_settings = normalize_provider_config(normalized_kind, payload.endpoint, payload.settings)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    item = db.scalar(select(ProviderConfig).where(ProviderConfig.kind == normalized_kind).with_for_update())
    created = item is None
    if not item:
        item = ProviderConfig(
            kind=normalized_kind,
            endpoint=endpoint,
            settings=normalized_settings,
            is_active=payload.is_active,
        )
        db.add(item)
    item.endpoint = endpoint
    item.settings = normalized_settings
    item.is_active = payload.is_active
    secret_changed = False
    if normalized_kind == "agent":
        if payload.clear_secret:
            item.secret_ciphertext = ""
            secret_changed = True
        elif payload.secret is not None and payload.secret.strip():
            item.secret_ciphertext = encrypt_secret(payload.secret)
            secret_changed = True
    try:
        db.flush()
        audit(
            db,
            admin,
            "provider_config.create" if created else "provider_config.patch",
            "provider_config",
            item.id,
            {
                "kind": item.kind,
                "endpoint": endpoint,
                "settings": normalized_settings,
                "is_active": item.is_active,
                "secret_changed": secret_changed,
            },
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="服务配置发生并发冲突，请刷新后重试。") from exc
    return {"provider": serialize_provider_config(item)}


@router.post("/providers/agent/test")
async def test_agent_provider(
    admin: User = Depends(verify_csrf),
    db: Session = Depends(get_db),
) -> dict:
    if admin.role != "system_admin":
        raise HTTPException(status_code=403, detail="仅系统管理员可测试 Agent 接入。")
    item = db.scalar(select(ProviderConfig).where(ProviderConfig.kind == "agent"))
    if not item or not item.is_active:
        raise HTTPException(status_code=409, detail="RESTful Agent 接入尚未启用。")
    endpoint, normalized_settings = normalize_provider_config("agent", item.endpoint, item.settings)
    health_endpoint = str(normalized_settings["health_endpoint"])
    headers = {}
    secret = decrypt_secret(item.secret_ciphertext)
    if secret:
        headers["X-Debate-Agent-Key"] = secret
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(health_endpoint, headers=headers)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=f"Agent 健康检查失败：{type(exc).__name__}") from exc
    latency_ms = round((time.perf_counter() - started) * 1000, 1)
    audit(db, admin, "provider_config.test", "provider_config", item.id, {"kind": "agent", "ok": True, "latency_ms": latency_ms})
    db.commit()
    return {"ok": True, "endpoint": endpoint, "health": payload, "latency_ms": latency_ms}


@router.get("/audio-cues")
def audio_cues(admin: User = Depends(system_admin), db: Session = Depends(get_db)) -> dict:
    rows = db.scalars(select(AudioCue).order_by(AudioCue.key)).all()
    return {"items": [serialize_audio_cue(item) for item in rows]}


@router.post("/audio-cues")
async def create_audio_cue(
    key: str = Form(...),
    name: str = Form(...),
    text: str = Form(default=""),
    audio: UploadFile = File(...),
    admin: User = Depends(verify_csrf),
    db: Session = Depends(get_db),
) -> dict:
    if admin.role != "system_admin":
        raise HTTPException(status_code=403, detail="仅系统管理员可上传预设语音。")
    normalized_key = key.strip().lower()
    normalized_name = name.strip()
    normalized_text = text.strip()
    if not AUDIO_CUE_KEY.fullmatch(normalized_key):
        raise HTTPException(status_code=422, detail="阶段 key 须为 2–80 位小写字母、数字或下划线，并以字母开头。")
    if not 1 <= len(normalized_name) <= 120 or len(normalized_text) > 2000:
        raise HTTPException(status_code=422, detail="预设语音名称或文本长度不符合要求。")
    data = await read_audio_cue_upload(audio)
    if db.scalar(select(AudioCue.id).where(AudioCue.key == normalized_key)):
        raise HTTPException(status_code=409, detail="该阶段 key 已有预设语音，可直接替换音频。")
    item = AudioCue(key=normalized_key, name=normalized_name, text=normalized_text, is_active=True)
    db.add(item)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="该阶段 key 已有预设语音。") from exc
    target_dir = settings.media_path / "_cues"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{item.id}.wav"
    temporary = target.with_suffix(".wav.part")
    try:
        temporary.write_bytes(data)
        temporary.replace(target)
        item.audio_url = f"/media/_cues/{target.name}"
        audit(
            db,
            admin,
            "audio_cue.create",
            "audio_cue",
            item.id,
            {"key": item.key, "name": item.name, "bytes": len(data)},
        )
        db.commit()
    except Exception:
        db.rollback()
        target.unlink(missing_ok=True)
        raise
    finally:
        temporary.unlink(missing_ok=True)
    return {"audio_cue": serialize_audio_cue(item)}


@router.patch("/audio-cues/{cue_id}")
def patch_audio_cue(
    cue_id: str,
    payload: AudioCuePatch,
    admin: User = Depends(verify_csrf),
    db: Session = Depends(get_db),
) -> dict:
    if admin.role != "system_admin":
        raise HTTPException(status_code=403, detail="仅系统管理员可管理预设语音。")
    item = db.get(AudioCue, cue_id)
    if not item:
        raise HTTPException(status_code=404, detail="预设语音不存在。")
    changes = payload.model_dump(exclude_none=True)
    for field, value in changes.items():
        setattr(item, field, value)
    audit(db, admin, "audio_cue.patch", "audio_cue", item.id, changes)
    db.commit()
    return {"audio_cue": serialize_audio_cue(item)}


@router.post("/audio-cues/{cue_id}/audio")
async def replace_audio_cue(
    cue_id: str,
    audio: UploadFile = File(...),
    admin: User = Depends(verify_csrf),
    db: Session = Depends(get_db),
) -> dict:
    if admin.role != "system_admin":
        raise HTTPException(status_code=403, detail="仅系统管理员可替换预设语音。")
    data = await read_audio_cue_upload(audio)
    item = db.get(AudioCue, cue_id)
    if not item:
        raise HTTPException(status_code=404, detail="预设语音不存在。")
    target_dir = settings.media_path / "_cues"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{item.id}.wav"
    temporary = target.with_suffix(".wav.part")
    try:
        temporary.write_bytes(data)
        temporary.replace(target)
        item.audio_url = f"/media/_cues/{target.name}"
        audit(db, admin, "audio_cue.replace", "audio_cue", item.id, {"key": item.key, "bytes": len(data)})
        db.commit()
    finally:
        temporary.unlink(missing_ok=True)
    return {"audio_cue": serialize_audio_cue(item)}


@router.get("/media")
def media_status(admin: User = Depends(system_admin), db: Session = Depends(get_db)) -> dict:
    return {"media": inspect_media(db)}


@router.post("/media/cleanup")
def cleanup_media(
    payload: MediaCleanupRequest,
    admin: User = Depends(verify_csrf),
    db: Session = Depends(get_db),
) -> dict:
    if admin.role != "system_admin":
        raise HTTPException(status_code=403, detail="仅系统管理员可清理媒体文件。")
    result = inspect_media(
        db,
        min_age_hours=payload.min_age_hours,
        include_stale_parts=payload.include_stale_parts,
        delete=not payload.dry_run,
    )
    if not payload.dry_run and result["truncated"]:
        raise HTTPException(status_code=409, detail="媒体文件数量超过安全扫描上限，本次未执行清理。")
    if not payload.dry_run:
        audit(
            db,
            admin,
            "media.cleanup",
            "media_storage",
            "global",
            {
                "min_age_hours": payload.min_age_hours,
                "deleted_files": result["deleted_files"],
                "deleted_bytes": result["deleted_bytes"],
                "truncated": result["truncated"],
            },
        )
        db.commit()
    return {"media": result}


@router.get("/archives")
def archive_status(admin: User = Depends(system_admin), db: Session = Depends(get_db)) -> dict:
    return {"archives": inspect_archives(db)}


@router.get("/data-quality")
def data_quality(admin: User = Depends(system_admin), db: Session = Depends(get_db)) -> dict:
    """Summarize whether production debate records are usable for review and analysis."""
    audio_archiving_enabled = settings.match_audio_archive_enabled
    production_match = Room.is_test_data.is_(False)
    match_row = db.execute(
        select(
            func.count(Match.id),
            func.coalesce(func.sum(case((Match.status.in_(("lobby", "preparing", "running", "paused", "judging")), 1), else_=0)), 0),
            func.coalesce(func.sum(case((Match.status == "completed", 1), else_=0)), 0),
            func.coalesce(func.sum(case((Match.status == "review_required", 1), else_=0)), 0),
            func.coalesce(func.sum(case((Match.status == "terminated", 1), else_=0)), 0),
        )
        .join(Room, Room.id == Match.room_id)
        .where(production_match)
    ).one()
    completed_human = (Speech.speaker_type == "human") & (Speech.status == "completed")
    speech_row = db.execute(
        select(
            func.coalesce(func.sum(case((completed_human, 1), else_=0)), 0),
            func.coalesce(
                func.sum(case((completed_human & (func.length(func.trim(Speech.content)) > 0), 1), else_=0)),
                0,
            ),
            func.coalesce(
                func.sum(case((completed_human & (func.length(func.trim(Speech.audio_url)) > 0), 1), else_=0)),
                0,
            ),
            func.coalesce(
                func.sum(case(((Speech.speaker_type == "ai") & (Speech.status == "completed"), 1), else_=0)),
                0,
            ),
        )
        .join(Room, Room.id == Speech.room_id)
        .where(Room.is_test_data.is_(False))
    ).one()
    human_completed, human_with_transcript, human_with_audio, ai_completed = (int(value or 0) for value in speech_row)

    published_without_scorecard = db.scalar(
        select(func.count(Match.id))
        .join(Room, Room.id == Match.room_id)
        .outerjoin(JudgeScorecard, JudgeScorecard.match_id == Match.id)
        .where(
            Room.is_test_data.is_(False),
            Match.status == "completed",
            Match.legacy.is_(False),
            or_(JudgeScorecard.id.is_(None), JudgeScorecard.status != "approved"),
        )
    ) or 0
    published_without_speeches = db.scalar(
        select(func.count(Match.id))
        .join(Room, Room.id == Match.room_id)
        .where(
            Room.is_test_data.is_(False),
            Match.status == "completed",
            Match.legacy.is_(False),
            ~select(Speech.id).where(Speech.match_id == Match.id).exists(),
        )
    ) or 0
    human_missing_segments = db.scalar(
        select(func.count(Speech.id))
        .join(Room, Room.id == Speech.room_id)
        .where(
            Room.is_test_data.is_(False),
            completed_human,
            ~select(TranscriptSegment.id).where(TranscriptSegment.speech_id == Speech.id).exists(),
        )
    ) or 0
    human_missing_transcript = max(0, human_completed - human_with_transcript)
    human_missing_audio = max(0, human_completed - human_with_audio) if audio_archiving_enabled else 0

    segment_exists = select(TranscriptSegment.id).where(TranscriptSegment.speech_id == Speech.id).exists()
    sample_issue_conditions = [
        func.length(func.trim(Speech.content)) == 0,
        ~segment_exists,
    ]
    if audio_archiving_enabled:
        sample_issue_conditions.append(func.length(func.trim(Speech.audio_url)) == 0)
    sample_rows = db.execute(
        select(
            Room.code,
            Match.id,
            Speech.id,
            Speech.seat_key,
            Speech.stage_key,
            Speech.content,
            Speech.audio_url,
            segment_exists.label("has_segment"),
        )
        .join(Match, Match.room_id == Room.id)
        .join(Speech, Speech.match_id == Match.id)
        .where(
            Room.is_test_data.is_(False),
            completed_human,
            or_(*sample_issue_conditions),
        )
        .order_by(Speech.created_at.desc(), Speech.id)
        .limit(20)
    ).all()
    sampled_speech_ids = [speech_id for _, _, speech_id, *_ in sample_rows]
    disposition_rows = (
        db.scalars(
            select(SpeechDataIssueDisposition).where(
                SpeechDataIssueDisposition.speech_id.in_(sampled_speech_ids)
            )
        ).all()
        if sampled_speech_ids
        else []
    )
    dispositions = {
        (item.speech_id, item.issue_code): serialize_speech_data_issue_disposition(item)
        for item in disposition_rows
    }
    samples = []
    unreviewed_sample_issues = 0
    for room_code, match_id, speech_id, seat_key, stage_key, content, audio_url, has_segment in sample_rows:
        issues = []
        if not (content or "").strip():
            issues.append("missing_transcript")
        if audio_archiving_enabled and not (audio_url or "").strip():
            issues.append("missing_audio")
        if not has_segment:
            issues.append("missing_segments")
        issue_dispositions = {
            issue: dispositions[(speech_id, issue)]
            for issue in issues
            if (speech_id, issue) in dispositions
        }
        unreviewed_sample_issues += len(issues) - len(issue_dispositions)
        samples.append(
            {
                "room_code": room_code,
                "match_id": match_id,
                "speech_id": speech_id,
                "seat_key": seat_key,
                "stage_key": stage_key,
                "issues": issues,
                "dispositions": issue_dispositions,
            }
        )

    def coverage(numerator: int, denominator: int) -> float:
        return round(100 * numerator / denominator, 1) if denominator else 100.0

    return {
        "scope": "production",
        "audio_archiving_enabled": audio_archiving_enabled,
        "matches": {
            "total": int(match_row[0] or 0),
            "active": int(match_row[1] or 0),
            "completed": int(match_row[2] or 0),
            "review_required": int(match_row[3] or 0),
            "terminated": int(match_row[4] or 0),
        },
        "speeches": {
            "human_completed": human_completed,
            "human_with_transcript": human_with_transcript,
            "human_with_audio": human_with_audio,
            "ai_completed": ai_completed,
            "transcript_coverage_percent": coverage(human_with_transcript, human_completed),
            "audio_coverage_percent": (
                coverage(human_with_audio, human_completed)
                if audio_archiving_enabled
                else None
            ),
        },
        "attention": {
            "published_without_scorecard": int(published_without_scorecard),
            "published_without_speeches": int(published_without_speeches),
            "human_missing_transcript": human_missing_transcript,
            "human_missing_audio": human_missing_audio,
            "human_missing_segments": int(human_missing_segments),
            "unreviewed_sample_issues": unreviewed_sample_issues,
            "samples": samples,
        },
    }


@router.patch("/data-quality/speeches/{speech_id}/issues/{issue_code}")
def patch_speech_data_issue(
    speech_id: str,
    issue_code: str,
    payload: SpeechDataIssueDispositionPatch,
    admin: User = Depends(verify_csrf),
    db: Session = Depends(get_db),
) -> dict:
    if admin.role != "system_admin":
        raise HTTPException(status_code=403, detail="仅系统管理员可操作。")
    if issue_code not in SPEECH_DATA_ISSUE_CODES:
        raise HTTPException(status_code=422, detail="不支持的数据问题类型。")
    if issue_code == "missing_audio" and not settings.match_audio_archive_enabled:
        raise HTTPException(status_code=409, detail="比赛音频归档已关闭，不再将缺少录音视为数据问题。")

    acquire_transaction_locks(db, f"speech-data-issue:{speech_id}")
    row = db.execute(
        select(Speech, Room)
        .join(Room, Room.id == Speech.room_id)
        .where(Speech.id == speech_id)
        .with_for_update()
    ).one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="发言记录不存在。")
    speech, room = row
    if room.is_test_data:
        raise HTTPException(status_code=409, detail="测试比赛不进入正式数据处置流程。")
    if speech.speaker_type != "human" or speech.status != "completed":
        raise HTTPException(status_code=409, detail="只有已完成的真人发言可以进行数据处置。")

    current_issues = set()
    if not speech.content.strip():
        current_issues.add("missing_transcript")
    if not speech.audio_url.strip():
        current_issues.add("missing_audio")
    has_segment = db.scalar(
        select(TranscriptSegment.id).where(TranscriptSegment.speech_id == speech.id).limit(1)
    )
    if not has_segment:
        current_issues.add("missing_segments")
    if issue_code not in current_issues:
        raise HTTPException(status_code=409, detail="该数据问题已经不存在，请重新检查数据质量。")

    item = db.scalar(
        select(SpeechDataIssueDisposition)
        .where(
            SpeechDataIssueDisposition.speech_id == speech.id,
            SpeechDataIssueDisposition.issue_code == issue_code,
        )
        .with_for_update()
    )
    if item is None:
        item = SpeechDataIssueDisposition(
            speech_id=speech.id,
            issue_code=issue_code,
            status=payload.status,
            note=payload.note,
            reviewed_by_user_id=admin.id,
            reviewed_at=now(),
        )
        db.add(item)
    else:
        item.status = payload.status
        item.note = payload.note
        item.reviewed_by_user_id = admin.id
        item.reviewed_at = now()
    audit(
        db,
        admin,
        "data_quality.issue.disposition",
        "speech",
        speech.id,
        {"issue_code": issue_code, "status": payload.status},
    )
    db.commit()
    db.refresh(item)
    return {"disposition": serialize_speech_data_issue_disposition(item)}


@router.get("/archive-index.csv")
def download_archive_index(
    competition_id: str | None = Query(default=None),
    season_id: str | None = Query(default=None),
    include_test_data: bool = Query(default=False),
    limit: int = Query(default=5000, ge=1, le=5000),
    admin: User = Depends(system_admin),
    db: Session = Depends(get_db),
) -> Response:
    """Export a compact research index; full transcripts remain in per-match archives."""
    filters = [Match.status.in_(FINAL_MATCH_STATUSES)]
    if competition_id:
        filters.append(Match.competition_id == competition_id)
    if season_id:
        filters.append(Match.season_id == season_id)
    if not include_test_data:
        filters.append(Room.is_test_data.is_(False))

    total = db.scalar(select(func.count(Match.id)).join(Room, Room.id == Match.room_id).where(*filters)) or 0
    if total > limit:
        raise HTTPException(
            status_code=409,
            detail=f"符合条件的比赛有 {total} 场，超过单次导出上限 {limit}；请按赛事或赛季分批导出。",
        )

    records = db.execute(
        select(Match, Room, Competition, Season, JudgeScorecard)
        .join(Room, Room.id == Match.room_id)
        .join(Competition, Competition.id == Match.competition_id)
        .outerjoin(Season, Season.id == Match.season_id)
        .outerjoin(JudgeScorecard, JudgeScorecard.match_id == Match.id)
        .where(*filters)
        .order_by(Room.completed_at.desc(), Match.created_at.desc())
    ).all()
    match_ids = [match.id for match, _room, _competition, _season, _scorecard in records]
    room_ids = [room.id for _match, room, _competition, _season, _scorecard in records]

    speech_stats: dict[str, tuple[int, int, int]] = {}
    if match_ids:
        for match_id, speech_count, transcript_chars, audio_speech_count in db.execute(
            select(
                Speech.match_id,
                func.count(Speech.id),
                func.coalesce(func.sum(func.length(Speech.content)), 0),
                func.coalesce(func.sum(case((Speech.audio_url != "", 1), else_=0)), 0),
            )
            .where(Speech.match_id.in_(match_ids))
            .group_by(Speech.match_id)
        ):
            speech_stats[match_id] = (int(speech_count), int(transcript_chars), int(audio_speech_count))

    participants: dict[str, list[dict[str, str | int]]] = {room_id: [] for room_id in room_ids}
    if room_ids:
        seat_rows = db.execute(
            select(RoomSeat, User)
            .outerjoin(User, User.id == RoomSeat.user_id)
            .where(RoomSeat.room_id.in_(room_ids), RoomSeat.occupant_type.in_(("human", "ai")))
            .order_by(RoomSeat.room_id, RoomSeat.side, RoomSeat.position)
        ).all()
        for seat, user in seat_rows:
            participants[seat.room_id].append(
                {
                    "seat_key": seat.seat_key,
                    "side": seat.side,
                    "position": seat.position,
                    "type": seat.occupant_type,
                    "name": user.real_name if user else seat.display_name,
                }
            )

    output = io.StringIO(newline="")
    fieldnames = [
        "match_id", "room_code", "competition", "competition_id", "season", "season_id", "topic",
        "status", "winner", "data_scope", "visibility", "started_at", "completed_at", "participants",
        "speech_count", "transcript_chars", "audio_speech_count", "scorecard_status", "archive_ready",
        "archive_sha256", "archive_source_sha256", "archive_url",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for match, room, competition, season, scorecard in records:
        speech_count, transcript_chars, audio_speech_count = speech_stats.get(match.id, (0, 0, 0))
        archive = inspect_match_archive(match.id)
        writer.writerow(
            {
                "match_id": match.id,
                "room_code": room.code,
                "competition": safe_csv_text(competition.name),
                "competition_id": competition.id,
                "season": safe_csv_text(season.name if season else ""),
                "season_id": season.id if season else "",
                "topic": safe_csv_text(room.topic),
                "status": match.status,
                "winner": match.winner or "",
                "data_scope": "test" if room.is_test_data else "production",
                "visibility": room.visibility,
                "started_at": room.started_at.isoformat() if room.started_at else "",
                "completed_at": room.completed_at.isoformat() if room.completed_at else "",
                "participants": json.dumps(participants.get(room.id, []), ensure_ascii=False, separators=(",", ":")),
                "speech_count": speech_count,
                "transcript_chars": transcript_chars,
                "audio_speech_count": audio_speech_count,
                "scorecard_status": scorecard.status if scorecard else "",
                "archive_ready": "yes" if archive["ready"] else "no",
                "archive_sha256": archive["sha256"],
                "archive_source_sha256": archive["source_sha256"],
                "archive_url": f"/api/matches/{match.id}/archive",
            }
        )
    filename = f"debate-archive-index-{int(time.time())}.csv"
    return Response(
        content="\ufeff" + output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "private, no-store",
            "X-Exported-Matches": str(total),
        },
    )


@router.post("/archives/cleanup")
def cleanup_archives(
    payload: MediaCleanupRequest,
    admin: User = Depends(verify_csrf),
    db: Session = Depends(get_db),
) -> dict:
    if admin.role != "system_admin":
        raise HTTPException(status_code=403, detail="仅系统管理员可清理比赛归档。")
    result = inspect_archives(db, min_age_hours=payload.min_age_hours, delete=not payload.dry_run)
    if not payload.dry_run and result["truncated"]:
        raise HTTPException(status_code=409, detail="归档文件数量超过安全扫描上限，本次未执行清理。")
    if not payload.dry_run:
        audit(
            db,
            admin,
            "archives.cleanup",
            "archive_storage",
            "global",
            {
                "min_age_hours": payload.min_age_hours,
                "deleted_files": result["deleted_files"],
                "deleted_bytes": result["deleted_bytes"],
                "truncated": result["truncated"],
            },
        )
        db.commit()
    return {"archives": result}


@router.post("/archives/repair")
def repair_archives(
    admin: User = Depends(verify_csrf),
    db: Session = Depends(get_db),
) -> dict:
    if admin.role != "system_admin":
        raise HTTPException(status_code=403, detail="仅系统管理员可修复比赛归档。")
    before = inspect_archives(db)
    if before["truncated"]:
        raise HTTPException(status_code=409, detail="归档文件数量超过安全扫描上限，本次未执行修复。")
    repaired: list[str] = []
    failed: list[str] = []
    for item in before["invalid_archives"]:
        try:
            build_match_archive(item["match_id"])
            repaired.append(item["match_id"])
        except Exception:
            failed.append(item["match_id"])
    after = inspect_archives(db)
    audit(
        db,
        admin,
        "archives.repair",
        "archive_storage",
        "global",
        {"requested": len(before["invalid_archives"]), "repaired": len(repaired), "failed": len(failed)},
    )
    db.commit()
    return {"archives": after, "repaired": repaired, "failed": failed}


@router.get("/reviews")
def reviews(admin: User = Depends(system_admin), db: Session = Depends(get_db)) -> dict:
    pending_rows = db.execute(
        select(JudgeScorecard, Match, Room)
        .join(Match, JudgeScorecard.match_id == Match.id)
        .join(Room, Match.room_id == Room.id)
        .where(JudgeScorecard.status == "review_required")
        .order_by(JudgeScorecard.updated_at.desc())
    ).all()
    recent_rows = db.execute(
        select(JudgeScorecard, Match, Room)
        .join(Match, JudgeScorecard.match_id == Match.id)
        .join(Room, Match.room_id == Room.id)
        .where(JudgeScorecard.status == "approved", Match.status == "completed")
        .order_by(JudgeScorecard.updated_at.desc())
        .limit(30)
    ).all()

    def serialize(score: JudgeScorecard, match: Match, room: Room) -> dict:
        return {
            "scorecard_id": score.id,
            "match_id": match.id,
            "room_code": room.code,
            "topic": room.topic,
            "status": score.status,
            "winner": score.winner,
            "affirmative_score": score.affirmative_score,
            "negative_score": score.negative_score,
            "reason": score.reasoning,
            "updated_at": score.updated_at.isoformat(),
        }

    return {
        "items": [serialize(score, match, room) for score, match, room in pending_rows],
        "recent": [serialize(score, match, room) for score, match, room in recent_rows],
    }


@router.get("/speech-corrections")
def speech_corrections(admin: User = Depends(system_admin), db: Session = Depends(get_db)) -> dict:
    del admin
    base = (
        select(SpeechCorrectionRequest, Speech, Room, User)
        .join(Speech, SpeechCorrectionRequest.speech_id == Speech.id)
        .join(Room, SpeechCorrectionRequest.room_id == Room.id)
        .join(User, SpeechCorrectionRequest.requester_user_id == User.id)
    )
    pending_rows = db.execute(
        base.where(SpeechCorrectionRequest.status == "pending")
        .order_by(SpeechCorrectionRequest.created_at)
        .limit(500)
    ).all()
    recent_rows = db.execute(
        base.where(SpeechCorrectionRequest.status != "pending")
        .order_by(SpeechCorrectionRequest.updated_at.desc())
        .limit(50)
    ).all()

    def serialize(item: SpeechCorrectionRequest, speech: Speech, room: Room, requester: User) -> dict:
        return serialize_correction_request(item, expose_internal=True) | {
            "room_code": room.code,
            "topic": room.topic,
            "seat_key": speech.seat_key,
            "requester_name": requester.real_name,
        }

    return {
        "items": [serialize(item, speech, room, requester) for item, speech, room, requester in pending_rows],
        "recent": [serialize(item, speech, room, requester) for item, speech, room, requester in recent_rows],
    }


async def _review_speech_correction(
    request_id: str,
    payload: SpeechCorrectionReview,
    admin: User,
    db: Session,
    *,
    approve: bool,
) -> dict:
    request = db.get(SpeechCorrectionRequest, request_id)
    if not request:
        raise HTTPException(status_code=404, detail="修正申请不存在。")
    room_row = db.get(Room, request.room_id)
    if not room_row:
        raise HTTPException(status_code=409, detail="修正申请缺少房间记录。")
    room = load_room(db, room_row.code, lock=True)
    db.refresh(request)
    replayed, archive_match_id = review_correction_request(
        db,
        room,
        request,
        admin,
        approve=approve,
        reason=payload.reason,
        expected_updated_at=payload.expected_updated_at,
    )
    action = "speech_correction.approve" if approve else "speech_correction.reject"
    audit(
        db,
        admin,
        action,
        "speech_correction_request",
        request.id,
        {"speech_id": request.speech_id, "room_code": room.code, "reason": payload.reason},
    )
    db.commit()
    enqueue_correction_archive(archive_match_id)
    event_type = "speech.corrected" if approve else "speech.correction_rejected"
    await room_hub.publish(room.code, {"type": event_type, "room_code": room.code, "seq": room.seq})
    return {"ok": True, "replayed": replayed, "request": serialize_correction_request(request, expose_internal=True)}


@router.post("/speech-corrections/{request_id}/approve")
async def approve_speech_correction(
    request_id: str,
    payload: SpeechCorrectionReview,
    admin: User = Depends(verify_csrf),
    db: Session = Depends(get_db),
) -> dict:
    if admin.role != "system_admin":
        raise HTTPException(status_code=403, detail="仅系统管理员可操作。")
    return await _review_speech_correction(request_id, payload, admin, db, approve=True)


@router.post("/speech-corrections/{request_id}/reject")
async def reject_speech_correction(
    request_id: str,
    payload: SpeechCorrectionReview,
    admin: User = Depends(verify_csrf),
    db: Session = Depends(get_db),
) -> dict:
    if admin.role != "system_admin":
        raise HTTPException(status_code=403, detail="仅系统管理员可操作。")
    return await _review_speech_correction(request_id, payload, admin, db, approve=False)


@router.post("/reviews/{scorecard_id}/retry", status_code=202)
async def retry_review_with_current_judge(
    scorecard_id: str,
    payload: JudgeRetryRequest,
    admin: User = Depends(verify_csrf),
    db: Session = Depends(get_db),
) -> dict:
    if admin.role != "system_admin":
        raise HTTPException(status_code=403, detail="仅系统管理员可操作。")
    scorecard = db.get(JudgeScorecard, scorecard_id)
    if not scorecard:
        raise HTTPException(status_code=404, detail="裁判记录不存在。")
    match = db.get(Match, scorecard.match_id)
    if not match:
        raise HTTPException(status_code=409, detail="裁判记录缺少对应比赛。")
    room_row = db.get(Room, match.room_id)
    if not room_row:
        raise HTTPException(status_code=409, detail="比赛缺少对应房间。")
    room = load_room(db, room_row.code, lock=True)
    db.refresh(scorecard)
    db.refresh(match)
    verify_review_revision(scorecard, payload.expected_updated_at)
    if scorecard.status != "review_required" or match.status != "review_required" or room.status != "review_required":
        raise HTTPException(status_code=409, detail="只有等待人工复核的比赛才能重试 AI 裁判。")
    judge_profile = db.scalar(
        select(JudgeProfile)
        .where(JudgeProfile.is_active.is_(True))
        .order_by(JudgeProfile.created_at, JudgeProfile.id)
        .with_for_update()
    )
    if not judge_profile:
        raise HTTPException(status_code=409, detail="当前没有启用的 AI 裁判配置。")
    previous_reason = scorecard.reasoning
    match.judge_profile_id = judge_profile.id
    match.judge_snapshot = {
        "id": judge_profile.id,
        "name": judge_profile.name,
        "endpoint": judge_profile.endpoint,
        "model_name": judge_profile.model_name,
        "system_prompt": judge_profile.system_prompt,
        "timeout_seconds": judge_profile.timeout_seconds,
    }
    match.status = "judging"
    match.winner = None
    match.result_reason = ""
    scorecard.status = "running"
    scorecard.winner = None
    scorecard.affirmative_score = 0
    scorecard.negative_score = 0
    scorecard.individual_scores = {}
    scorecard.reasoning = "AI 裁判已使用当前配置重新排队。"
    scorecard.reviewed_by = None
    room.status = "judging"
    room.completed_at = None
    room.failure_reason = ""
    room.stage_deadline_at = None
    room.paused_remaining_seconds = None
    append_event(
        db,
        room,
        "judge.retry_requested",
        {
            "scorecard_id": scorecard.id,
            "judge_profile_id": judge_profile.id,
            "judge_profile_name": judge_profile.name,
            "previous_reason": previous_reason,
        },
        actor_user_id=admin.id,
    )
    audit(
        db,
        admin,
        "judge.retry",
        "scorecard",
        scorecard.id,
        {"judge_profile_id": judge_profile.id, "judge_profile_name": judge_profile.name},
    )
    db.commit()
    await room_hub.publish(room.code, {"type": "judge.retry_requested", "room_code": room.code, "seq": room.seq})
    return {"ok": True, "room_code": room.code, "judge_profile": judge_profile.name}


@router.post("/reviews/{scorecard_id}/approve")
async def approve_review(
    scorecard_id: str,
    payload: JudgeReviewRequest,
    admin: User = Depends(verify_csrf),
    db: Session = Depends(get_db),
) -> dict:
    if admin.role != "system_admin":
        raise HTTPException(status_code=403, detail="仅系统管理员可操作。")
    scorecard = db.get(JudgeScorecard, scorecard_id)
    if not scorecard:
        raise HTTPException(status_code=404, detail="裁判记录不存在。")
    match = db.get(Match, scorecard.match_id)
    if not match:
        raise HTTPException(status_code=409, detail="裁判记录缺少对应比赛。")
    room_row = db.get(Room, match.room_id)
    if not room_row:
        raise HTTPException(status_code=409, detail="比赛缺少对应房间。")
    room = load_room(db, room_row.code, lock=True)
    db.refresh(scorecard)
    db.refresh(match)
    verify_review_revision(scorecard, payload.expected_updated_at)
    if scorecard.status == "approved":
        raise HTTPException(status_code=409, detail="该结果已经确认。")
    if scorecard.status != "review_required":
        raise HTTPException(status_code=409, detail="自动裁判尚未进入待复核状态。")
    scorecard.status = "approved"
    scorecard.winner = payload.winner
    scorecard.affirmative_score = payload.affirmative_score
    scorecard.negative_score = payload.negative_score
    scorecard.reasoning = payload.reasoning
    scorecard.reviewed_by = admin.id
    match.status = "completed"
    match.winner = payload.winner
    match.result_reason = payload.reasoning
    room.status = "completed"
    room.completed_at = now()
    room.stage_deadline_at = None
    room.paused_remaining_seconds = None
    match_engine._apply_ranking(db, room, match, scorecard)
    append_event(db, room, "judge.reviewed", {"winner": payload.winner, "scorecard_id": scorecard.id}, actor_user_id=admin.id)
    audit(db, admin, "judge.approve", "scorecard", scorecard.id, payload.model_dump(exclude={"expected_updated_at"}))
    db.commit()
    enqueue_match_archive(match.id)
    await room_hub.publish(room.code, {"type": "match.result", "room_code": room.code, "seq": room.seq})
    return {"ok": True}


@router.post("/reviews/{scorecard_id}/correct")
async def correct_review(
    scorecard_id: str,
    payload: JudgeReviewRequest,
    admin: User = Depends(verify_csrf),
    db: Session = Depends(get_db),
) -> dict:
    if admin.role != "system_admin":
        raise HTTPException(status_code=403, detail="仅系统管理员可操作。")
    scorecard = db.get(JudgeScorecard, scorecard_id)
    if not scorecard:
        raise HTTPException(status_code=404, detail="裁判记录不存在。")
    match = db.get(Match, scorecard.match_id)
    if not match:
        raise HTTPException(status_code=409, detail="裁判记录缺少对应比赛。")
    room_row = db.get(Room, match.room_id)
    if not room_row:
        raise HTTPException(status_code=409, detail="比赛缺少对应房间。")

    room = load_room(db, room_row.code, lock=True)
    db.refresh(scorecard)
    db.refresh(match)
    verify_review_revision(scorecard, payload.expected_updated_at)
    if scorecard.status != "approved" or match.status != "completed" or room.status != "completed":
        raise HTTPException(status_code=409, detail="只有已确认且已完成的比赛才能修正赛果。")

    old_result = {
        "winner": scorecard.winner or match.winner,
        "affirmative_score": scorecard.affirmative_score,
        "negative_score": scorecard.negative_score,
        "reasoning": scorecard.reasoning,
    }
    new_result = payload.model_dump(exclude={"expected_updated_at"})
    if old_result == new_result:
        raise HTTPException(status_code=409, detail="提交内容与当前赛果完全相同，无需修正。")
    if old_result["winner"] not in {"aff", "neg", "draw"}:
        raise HTTPException(status_code=409, detail="原始赛果不完整，无法自动计算积分补偿。")

    correction_id = str(uuid4())
    try:
        match_engine._correct_ranking(
            db,
            room,
            match,
            correction_id=correction_id,
            old_winner=old_result["winner"],
            new_winner=payload.winner,
            old_affirmative_score=old_result["affirmative_score"],
            old_negative_score=old_result["negative_score"],
            new_affirmative_score=payload.affirmative_score,
            new_negative_score=payload.negative_score,
        )
    except RuntimeError:
        db.rollback()
        raise HTTPException(status_code=409, detail="排行榜原始结算记录不完整，已取消修正，请管理员检查数据。") from None

    scorecard.winner = payload.winner
    scorecard.affirmative_score = payload.affirmative_score
    scorecard.negative_score = payload.negative_score
    scorecard.reasoning = payload.reasoning
    scorecard.reviewed_by = admin.id
    match.winner = payload.winner
    match.result_reason = payload.reasoning

    from app.services.room_service import append_event

    event_payload = {
        "correction_id": correction_id,
        "scorecard_id": scorecard.id,
        "old": old_result,
        "new": new_result,
    }
    append_event(db, room, "judge.corrected", event_payload, actor_user_id=admin.id)
    audit(db, admin, "judge.correct", "scorecard", scorecard.id, event_payload)
    db.commit()
    enqueue_match_archive(match.id)
    await room_hub.publish(room.code, {"type": "match.result", "room_code": room.code, "seq": room.seq})
    return {"ok": True, "correction_id": correction_id}


@router.get("/audit")
def audit_logs(
    page: int = Query(default=1, ge=1, le=100000),
    page_size: int = Query(default=100, ge=1, le=200),
    q: str = Query(default="", max_length=120),
    admin: User = Depends(system_admin),
    db: Session = Depends(get_db),
) -> dict:
    filters = [
        *(
            ~func.lower(AdminAuditLog.action).startswith(prefix)
            for prefix in RETIRED_AUDIT_ACTION_PREFIXES
        ),
        ~AdminAuditLog.target_type.in_(RETIRED_AUDIT_TARGET_TYPES),
    ]
    query = q.strip().lower()
    if query:
        filters.append(
            or_(
                func.lower(AdminAuditLog.action).contains(query),
                func.lower(AdminAuditLog.target_type).contains(query),
                func.lower(AdminAuditLog.target_id).contains(query),
            )
        )
    total = db.scalar(select(func.count(AdminAuditLog.id)).where(*filters)) or 0
    pagination = _pagination(page, page_size, total)
    effective_page = pagination["page"]
    rows = db.execute(
        select(AdminAuditLog, User.real_name)
        .join(User, User.id == AdminAuditLog.actor_user_id)
        .where(*filters)
        .order_by(AdminAuditLog.created_at.desc(), AdminAuditLog.id)
        .offset((effective_page - 1) * page_size)
        .limit(page_size)
    ).all()
    return {
        "items": [
            {
                "id": item.id,
                "actor_name": actor_name,
                "action": item.action,
                "target_type": item.target_type,
                "target_id": item.target_id,
                "payload": item.payload,
                "created_at": item.created_at.isoformat(),
            }
            for item, actor_name in rows
        ],
        "pagination": pagination,
    }


def _pagination(page: int, page_size: int, total: int) -> dict:
    pages = max(1, (total + page_size - 1) // page_size)
    return {
        "page": min(page, pages),
        "page_size": page_size,
        "total": total,
        "pages": pages,
    }
