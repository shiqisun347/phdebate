from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse, Response
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload

from app.core.database import get_db
from app.core.deps import current_user, optional_user
from app.models.entities import (
    Competition,
    CompetitionTopic,
    JudgeScorecard,
    Match,
    MatchEvent,
    RatingChange,
    Room,
    RoomSeat,
    Season,
    SeatRestoreRequest,
    User,
)
from app.services.match_archive import MatchArchiveNotFound, read_match_archive
from app.services.participant_archive import serialize_participant_archive
from app.services.room_service import (
    can_control,
    can_view_room,
    leaderboard,
    load_room,
    serialize_competition,
    serialize_room,
    serialize_scorecard,
    serialize_user,
    use_public_projection,
)
from app.services.seasons import serialize_season
from app.services.speech_pagination import SpeechCursorError, paginate_match_speeches
from app.services.system_health import system_readiness

router = APIRouter(prefix="/api", tags=["public"])
API_INSTANCE_ID = os.getenv("API_INSTANCE_ID", "api-local")


def _bounded_int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


PUBLIC_PAUSED_ROOM_LISTING_SECONDS = _bounded_int_env(
    "PUBLIC_PAUSED_ROOM_LISTING_SECONDS",
    3600,
    300,
    86_400,
)
PUBLIC_PAUSE_EVENT_TYPES = ("control.pause", "engine.quarantined", "provider.failed")


def _public_pause_times():
    return (
        select(MatchEvent.room_id.label("room_id"), func.max(MatchEvent.created_at).label("paused_at"))
        .where(MatchEvent.event_type.in_(PUBLIC_PAUSE_EVENT_TYPES))
        .group_by(MatchEvent.room_id)
        .subquery()
    )


def _public_room_is_fresh(pause_times):
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=PUBLIC_PAUSED_ROOM_LISTING_SECONDS)
    return or_(
        Room.status != "paused",
        func.coalesce(pause_times.c.paused_at, Room.updated_at) >= cutoff,
    )


@router.get("/health")
def health(db: Session = Depends(get_db)) -> dict:
    db.scalar(select(func.count(Competition.id)))
    return {
        "ok": True,
        "service": "phdebate-v2",
        "version": "2.0.0",
        "instance": API_INSTANCE_ID,
    }


@router.get("/health/live")
def health_live() -> dict:
    return {
        "ok": True,
        "service": "phdebate-v2",
        "version": "2.0.0",
        "instance": API_INSTANCE_ID,
    }


@router.get("/health/ready")
async def health_ready(db: Session = Depends(get_db)) -> JSONResponse:
    result = await system_readiness(db)
    return JSONResponse(status_code=200 if result["ok"] else 503, content=result)


@router.get("/competitions")
def competitions(db: Session = Depends(get_db)) -> dict:
    pause_times = _public_pause_times()
    live_counts = (
        select(Room.competition_id.label("competition_id"), func.count(Room.id).label("live_count"))
        .outerjoin(pause_times, pause_times.c.room_id == Room.id)
        .where(
            Room.visibility == "public",
            Room.is_test_data.is_(False),
            Room.status.in_(["preparing", "running", "paused", "judging"]),
            _public_room_is_fresh(pause_times),
        )
        .group_by(Room.competition_id)
        .subquery()
    )
    rows = db.execute(
        select(Competition, func.coalesce(live_counts.c.live_count, 0))
        .outerjoin(live_counts, live_counts.c.competition_id == Competition.id)
        .options(joinedload(Competition.season))
        .where(Competition.is_active.is_(True), Competition.is_public.is_(True))
        .order_by(Competition.created_at)
    ).all()
    return {
        "items": [serialize_competition(item, live_count=int(live_count)) for item, live_count in rows]
    }


@router.get("/competitions/{slug}")
def competition_detail(slug: str, db: Session = Depends(get_db)) -> dict:
    competition = db.scalar(
        select(Competition)
        .options(joinedload(Competition.season))
        .where(Competition.slug == slug, Competition.is_public.is_(True))
    )
    if not competition:
        raise HTTPException(status_code=404, detail="赛事不存在。")
    topics = db.scalars(
        select(CompetitionTopic).where(CompetitionTopic.competition_id == competition.id, CompetitionTopic.is_active.is_(True))
    ).all()
    pause_times = _public_pause_times()
    live_filters = (
        Room.competition_id == competition.id,
        Room.visibility == "public",
        Room.is_test_data.is_(False),
        Room.status.in_(["preparing", "running", "paused", "judging"]),
        _public_room_is_fresh(pause_times),
    )
    live_count = db.scalar(
        select(func.count(Room.id))
        .outerjoin(pause_times, pause_times.c.room_id == Room.id)
        .where(*live_filters)
    ) or 0
    live_rooms = db.execute(
        select(Room, pause_times.c.paused_at)
        .outerjoin(pause_times, pause_times.c.room_id == Room.id)
        .where(*live_filters)
        .order_by(Room.updated_at.desc())
        .limit(20)
    ).all()
    return {
        "competition": serialize_competition(competition, topics=list(topics), live_count=int(live_count)),
        "season": serialize_season(competition.season) if competition.season else None,
        "leaderboard": leaderboard(db, competition.id, competition.season_id, limit=20),
        "live_rooms": [
            {
                "code": room.code,
                "topic": room.topic,
                "status": room.status,
                "paused_at": paused_at.isoformat() if paused_at else None,
                "updated_at": room.updated_at.isoformat(),
            }
            for room, paused_at in live_rooms
        ],
    }


@router.get("/seasons")
def seasons(db: Session = Depends(get_db)) -> dict:
    rows = db.scalars(select(Season).order_by(Season.starts_at.desc(), Season.id)).all()
    return {"items": [serialize_season(item) for item in rows]}


@router.get("/rankings")
def rankings(competition_slug: str | None = None, season_slug: str | None = None, db: Session = Depends(get_db)) -> dict:
    competition_id = None
    competition = None
    if competition_slug:
        competition = db.scalar(
            select(Competition).where(Competition.slug == competition_slug, Competition.is_public.is_(True))
        )
        if not competition:
            raise HTTPException(status_code=404, detail="赛事不存在。")
        competition_id = competition.id
    if season_slug:
        season = db.scalar(select(Season).where(Season.slug == season_slug))
        if not season:
            raise HTTPException(status_code=404, detail="赛季不存在。")
    elif competition and competition.season_id:
        season = db.get(Season, competition.season_id)
    else:
        season = db.scalar(select(Season).where(Season.is_active.is_(True)).order_by(Season.starts_at.desc(), Season.id).limit(1))
    return {
        "season": serialize_season(season) if season else None,
        "items": leaderboard(db, competition_id, season.id if season else None),
    }


@router.get("/live-rooms")
def live_rooms(db: Session = Depends(get_db)) -> dict:
    pause_times = _public_pause_times()
    rows = db.execute(
        select(Room, pause_times.c.paused_at)
        .outerjoin(pause_times, pause_times.c.room_id == Room.id)
        .options(joinedload(Room.competition))
        .where(
            Room.visibility == "public",
            Room.is_test_data.is_(False),
            Room.status.in_(["preparing", "running", "paused", "judging"]),
            _public_room_is_fresh(pause_times),
        )
        .order_by(Room.updated_at.desc())
        .limit(30)
    ).all()
    return {
        "items": [
            {
                "code": room.code,
                "topic": room.topic,
                "status": room.status,
                "competition_name": room.competition.name,
                "paused_at": paused_at.isoformat() if paused_at else None,
                "updated_at": room.updated_at.isoformat(),
                "stage": room.template_snapshot[room.current_stage_index]["name"]
                if 0 <= room.current_stage_index < len(room.template_snapshot)
                else "准备中",
            }
            for room, paused_at in rows
        ]
    }


@router.get("/rooms/{code}/public")
def public_room(code: str, db: Session = Depends(get_db), user: User | None = Depends(optional_user)) -> dict:
    room = load_room(db, code)
    if not can_view_room(db, room, user):
        raise HTTPException(status_code=401 if not user else 403, detail="该房间不是公开房间。")
    return {"room": serialize_room(db, room, user, public=True)}


@router.get("/me")
def me(
    page: int = Query(default=1, ge=1, le=100000),
    page_size: int = Query(default=20, ge=1, le=50),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    active_rooms = db.execute(
        select(Room)
        .add_columns(RoomSeat)
        .join(RoomSeat, RoomSeat.room_id == Room.id)
        .where(
            RoomSeat.user_id == user.id,
            Room.status.in_(["lobby", "preparing", "running", "paused", "judging"]),
        )
        .order_by(Room.updated_at.desc(), Room.id.desc())
    ).all()
    history_filter = (
        select(Match.id)
        .join(Room, Match.room_id == Room.id)
        .join(RoomSeat, RoomSeat.room_id == Room.id)
        .where(
            RoomSeat.user_id == user.id,
            Match.status.in_(["completed", "review_required", "terminated"]),
        )
    )
    history_total = db.scalar(select(func.count()).select_from(history_filter.subquery())) or 0
    history = db.execute(
        select(Match, Room)
        .join(Room, Match.room_id == Room.id)
        .join(RoomSeat, RoomSeat.room_id == Room.id)
        .where(
            RoomSeat.user_id == user.id,
            Match.status.in_(["completed", "review_required", "terminated"]),
        )
        .order_by(Match.updated_at.desc(), Match.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    rating_changes = db.scalars(
        select(RatingChange)
        .where(RatingChange.user_id == user.id)
        .order_by(RatingChange.created_at.desc(), RatingChange.id.desc())
        .limit(50)
    ).all()
    total_points = db.scalar(select(func.coalesce(func.sum(RatingChange.points_delta), 0)).where(RatingChange.user_id == user.id)) or 0
    active_seat_ids = [seat.id for _room, seat in active_rooms]
    latest_restore_by_seat: dict[str, SeatRestoreRequest] = {}
    if active_seat_ids:
        for request in db.scalars(
            select(SeatRestoreRequest)
            .where(
                SeatRestoreRequest.seat_id.in_(active_seat_ids),
                SeatRestoreRequest.requester_user_id == user.id,
            )
            .order_by(SeatRestoreRequest.created_at.desc())
        ).all():
            latest_restore_by_seat.setdefault(request.seat_id, request)
    return {
        "user": serialize_user(user),
        "summary": {
            "history_total": history_total,
            "active_total": len(active_rooms),
            "total_points": total_points,
        },
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total": history_total,
            "pages": max(1, (history_total + page_size - 1) // page_size),
        },
        "active_rooms": [
            {
                "code": room.code,
                "topic": room.topic,
                "status": room.status,
                "seat_key": seat.seat_key,
                "occupant_type": seat.occupant_type,
                "can_resume": seat.occupant_type == "human",
                "restore_request": (
                    {
                        "id": latest_restore_by_seat[seat.id].id,
                        "status": latest_restore_by_seat[seat.id].status,
                        "resolution_reason": latest_restore_by_seat[seat.id].resolution_reason,
                    }
                    if seat.id in latest_restore_by_seat
                    else None
                ),
            }
            for room, seat in active_rooms
        ],
        "history": [
            {
                "match_id": match.id,
                "room_code": room.code,
                "topic": room.topic,
                "status": match.status,
                "winner": match.winner,
                "completed_at": room.completed_at.isoformat() if room.completed_at else None,
            }
            for match, room in history
        ],
        "rating_changes": [
            {
                "match_id": item.match_id,
                "points_delta": item.points_delta,
                "score": item.score,
                "reason": item.reason,
                "source": item.source,
                "created_at": item.created_at.isoformat(),
            }
            for item in rating_changes
        ],
    }


@router.get("/matches/{match_id}/history")
def match_history(
    match_id: str,
    speech_page: int = Query(default=1, ge=1, le=100000),
    speech_page_size: int = Query(default=100, ge=1, le=200),
    speech_cursor: str | None = Query(default=None, min_length=10, max_length=1000),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    match = db.get(Match, match_id)
    if not match:
        raise HTTPException(status_code=404, detail="比赛不存在。")
    room = db.get(Room, match.room_id)
    if not can_view_room(db, room, user):
        raise HTTPException(status_code=403, detail="无权查看该比赛。")
    try:
        speech_result = paginate_match_speeches(
            db,
            match.id,
            page=speech_page,
            page_size=speech_page_size,
            cursor=speech_cursor,
        )
    except SpeechCursorError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    scorecard = db.scalar(select(JudgeScorecard).where(JudgeScorecard.match_id == match.id))
    return {
        "match": {"id": match.id, "status": match.status, "winner": match.winner, "topic": room.topic, "room_code": room.code},
        "speeches": [
            {"seat_key": item.seat_key, "stage_key": item.stage_key, "content": item.content, "audio_url": item.audio_url}
            for item in speech_result.rows
        ],
        "speech_pagination": {
            "page": speech_result.page,
            "page_size": speech_result.page_size,
            "total": speech_result.total,
            "pages": speech_result.pages,
            "next_cursor": speech_result.next_cursor,
            "has_more": speech_result.has_more,
        },
        "scorecard": serialize_scorecard(
            scorecard,
            expose_internal_details=user.role == "system_admin",
            allowed_seat_keys={item.seat_key for item in room.seats},
        ),
    }


@router.get("/matches/{match_id}/result")
def match_result(match_id: str, user: User | None = Depends(optional_user), db: Session = Depends(get_db)) -> dict:
    match = db.get(Match, match_id)
    if not match:
        raise HTTPException(status_code=404, detail="比赛不存在。")
    room = db.get(Room, match.room_id)
    if not can_view_room(db, room, user):
        raise HTTPException(status_code=401 if not user else 403, detail="无权查看该比赛。")
    if use_public_projection(db, room, user) and match.status != "completed":
        raise HTTPException(status_code=409, detail="比赛尚未结束，请前往观战页面查看实时内容。")
    scorecard = db.scalar(select(JudgeScorecard).where(JudgeScorecard.match_id == match.id))
    changes = db.scalars(
        select(RatingChange).where(RatingChange.match_id == match.id).order_by(RatingChange.created_at, RatingChange.id)
    ).all()
    rating_names = {
        item.id: item.real_name for item in db.scalars(select(User).where(User.id.in_({change.user_id for change in changes}))).all()
    }
    expose_participant_ids = not use_public_projection(db, room, user)
    return {
        "match": {
            "id": match.id,
            "room_code": room.code,
            "topic": room.topic,
            "status": match.status,
            "winner": match.winner,
            "reason": match.result_reason,
        },
        "scorecard": serialize_scorecard(
            scorecard,
            expose_internal_details=bool(user and user.role == "system_admin"),
            allowed_seat_keys={item.seat_key for item in room.seats},
        ),
        "rating_changes": [
            ({"user_id": item.user_id} if expose_participant_ids else {})
            | {
                "display_name": rating_names.get(item.user_id, "参赛选手"),
                "points_delta": item.points_delta,
                "score": item.score,
                "reason": item.reason,
                "source": item.source,
            }
            for item in changes
        ],
    }


@router.get("/matches/{match_id}/archive")
def download_match_archive(match_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> Response:
    match = db.get(Match, match_id)
    if not match:
        raise HTTPException(status_code=404, detail="比赛不存在。")
    room = db.get(Room, match.room_id)
    if not room:
        raise HTTPException(status_code=404, detail="比赛缺少对应房间。")
    participant = db.scalar(select(RoomSeat.id).where(RoomSeat.room_id == room.id, RoomSeat.user_id == user.id))
    if not participant and not can_control(db, room, user):
        raise HTTPException(status_code=403, detail="仅参赛者、房主或系统管理员可下载比赛归档。")
    try:
        payload = read_match_archive(match.id)
    except MatchArchiveNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    archive = payload.result
    content = payload.content
    archive_sha256 = archive.sha256
    projection = "research"
    if user.role != "system_admin":
        content, archive_sha256 = serialize_participant_archive(json.loads(payload.content))
        projection = "participant"
    return Response(
        content=content,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="debate-{room.code}-{match.id}.json"',
            "Cache-Control": "private, no-store",
            "ETag": f'"{archive_sha256}"',
            "X-Archive-SHA256": archive_sha256,
            "X-Archive-Source-SHA256": archive.source_sha256,
            "X-Archive-Projection": projection,
        },
    )
