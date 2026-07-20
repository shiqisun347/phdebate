from __future__ import annotations

import csv
import io
import json
import re
import time
import wave
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile
from sqlalchemy import case, delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

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
    ProviderConfig,
    Room,
    RoomSeat,
    Season,
    Speech,
    User,
    UserSession,
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
    SeasonCreate,
    SeasonPatch,
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
from app.services.seat_restore import expire_pending_restore_requests
from app.services.system_health import system_readiness

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
def patch_user(user_id: str, payload: AdminUserPatch, admin: User = Depends(verify_csrf), db: Session = Depends(get_db)) -> dict:
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
    total = db.scalar(select(func.count(Room.id)).where(*filters)) or 0
    pagination = _pagination(page, page_size, total)
    effective_page = pagination["page"]
    rows = db.scalars(
        select(Room)
        .where(*filters)
        .order_by(Room.updated_at.desc(), Room.id)
        .offset((effective_page - 1) * page_size)
        .limit(page_size)
    ).all()
    return {
        "items": [serialize_room(db, load_room(db, room.code), admin) for room in rows],
        "pagination": pagination,
    }


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
    expire_pending_restore_requests(db, room, "比赛已完成")
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


@router.post("/rooms/{code}/seats/{seat_key}/restore")
async def restore_human_seat(
    code: str,
    seat_key: str,
    admin: User = Depends(verify_csrf),
    db: Session = Depends(get_db),
) -> dict:
    if admin.role != "system_admin":
        raise HTTPException(status_code=403, detail="仅系统管理员可恢复被 AI 接替的席位。")
    room = load_room(db, code, lock=True)
    seat = next((item for item in room.seats if item.seat_key == seat_key), None)
    if not seat:
        raise HTTPException(status_code=404, detail="席位不存在。")
    if seat.occupant_type != "ai_substitute" or not seat.user_id:
        raise HTTPException(status_code=409, detail="该席位当前不是 AI 接替状态。")
    if room.status not in {"preparing", "running", "paused", "judging"}:
        raise HTTPException(status_code=409, detail="比赛已经结束，不能再恢复真人席位。")
    if not seat.connected:
        raise HTTPException(status_code=409, detail="原辩手尚未重新连接，返回比赛后才能恢复真人控制。")
    active = db.scalar(
        select(Speech.id).where(
            Speech.room_id == room.id,
            Speech.seat_key == seat.seat_key,
            Speech.status.in_(["speaking", "synthesizing", "playing"]),
        )
    )
    if active:
        raise HTTPException(status_code=409, detail="该席位正在发言，结束后才能恢复真人控制。")
    participant = db.get(User, seat.user_id)
    if not participant or not participant.is_active:
        raise HTTPException(status_code=409, detail="原辩手账号不可用，无法恢复席位。")
    acquire_transaction_locks(db, f"1:user:{participant.id}")
    db.scalar(select(User.id).where(User.id == participant.id).with_for_update())
    conflicting_room = db.execute(
        select(Room.code)
        .join(RoomSeat, RoomSeat.room_id == Room.id)
        .where(
            Room.id != room.id,
            RoomSeat.user_id == participant.id,
            RoomSeat.occupant_type == "human",
            Room.status.in_(["lobby", "preparing", "running", "paused", "judging"]),
        )
        .order_by(Room.updated_at.desc())
        .limit(1)
    ).first()
    if conflicting_room:
        raise HTTPException(
            status_code=409,
            detail=f"原辩手已在房间 #{conflicting_room.code} 参赛，不能同时恢复旧席位。",
        )
    seat.occupant_type = "human"
    seat.display_name = participant.real_name
    seat.agent_profile_id = None
    seat.is_ready = True
    seat.disconnected_at = None
    expire_pending_restore_requests(db, room, "管理员已直接恢复席位", seat_id=seat.id)
    from app.services.room_service import append_event

    append_event(
        db,
        room,
        "seat.human_restored",
        {"seat_key": seat.seat_key, "user_id": participant.id, "connected": seat.connected},
        actor_user_id=admin.id,
    )
    audit(db, admin, "seat.restore_human", "room_seat", seat.id, {"room_code": code, "seat_key": seat.seat_key})
    db.commit()
    await room_hub.publish(room.code, {"type": "seat.human_restored", "room_code": room.code, "seq": room.seq})
    return {"room": serialize_room(db, load_room(db, code), admin)}


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
