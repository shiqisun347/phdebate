from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from app.core.database import acquire_transaction_locks
from app.models.entities import Room, RoomSeat, SeatRestoreRequest, Speech, User
from app.services.room_service import append_event, can_control, seat_label
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

ACTIVE_ROOM_STATUSES = {"preparing", "running", "paused", "judging"}
ACTIVE_SPEECH_STATUSES = {"speaking", "synthesizing", "playing"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _active_speech_exists(db: Session, room: Room) -> bool:
    return db.scalar(
        select(Speech.id).where(
            Speech.room_id == room.id,
            Speech.status.in_(ACTIVE_SPEECH_STATUSES),
        ).limit(1)
    ) is not None


def _conflicting_human_room(db: Session, room: Room, user_id: str) -> str | None:
    return db.scalar(
        select(Room.code)
        .join(RoomSeat, RoomSeat.room_id == Room.id)
        .where(
            Room.id != room.id,
            RoomSeat.user_id == user_id,
            RoomSeat.occupant_type == "human",
            Room.status.in_(["lobby", *ACTIVE_ROOM_STATUSES]),
        )
        .order_by(Room.updated_at.desc())
        .limit(1)
    )


def effective_status(request: SeatRestoreRequest, room: Room, seat: RoomSeat | None) -> tuple[str, str]:
    if request.status != "pending":
        return request.status, request.resolution_reason
    if room.status not in ACTIVE_ROOM_STATUSES:
        return "expired", "比赛已经结束或取消"
    if not seat or seat.occupant_type != "ai_substitute" or seat.user_id != request.requester_user_id:
        return "expired", "席位状态已经改变"
    return "pending", ""


def serialize_restore_request(
    request: SeatRestoreRequest,
    room: Room,
    seat: RoomSeat | None,
    viewer: User,
) -> dict:
    status, reason = effective_status(request, room, seat)
    controller = can_control(None, room, viewer)
    is_requester = request.requester_user_id == viewer.id
    return {
        "id": request.id,
        "seat_key": seat.seat_key if seat else "",
        "seat_label": seat_label(seat.seat_key) if seat else "已移除席位",
        "requester": {"id": request.requester.id, "real_name": request.requester.real_name},
        "requester_connected": bool(seat and seat.connected),
        "status": status,
        "resolution_reason": reason,
        "created_at": request.created_at.isoformat(),
        "resolved_at": request.resolved_at.isoformat() if request.resolved_at else None,
        "can_cancel": status == "pending" and is_requester,
        "can_review": status == "pending" and controller,
    }


def visible_restore_requests(db: Session, room: Room, viewer: User | None) -> list[dict]:
    if not viewer:
        return []
    controller = can_control(db, room, viewer)
    statement = (
        select(SeatRestoreRequest)
        .where(SeatRestoreRequest.room_id == room.id)
        .order_by(SeatRestoreRequest.created_at.desc())
        .limit(30)
    )
    if not controller:
        statement = statement.where(SeatRestoreRequest.requester_user_id == viewer.id)
    requests = db.scalars(statement).all()
    seats = {seat.id: seat for seat in room.seats}
    return [serialize_restore_request(item, room, seats.get(item.seat_id), viewer) for item in requests]


def expire_pending_restore_requests(db: Session, room: Room, reason: str, *, seat_id: str | None = None) -> int:
    """Persist lifecycle invalidation when a room reaches a terminal state."""
    statement = select(SeatRestoreRequest).where(
        SeatRestoreRequest.room_id == room.id,
        SeatRestoreRequest.status == "pending",
    )
    if seat_id:
        statement = statement.where(SeatRestoreRequest.seat_id == seat_id)
    pending = db.scalars(statement).all()
    resolved_at = _now()
    for request in pending:
        request.status = "expired"
        request.resolution_reason = reason
        request.resolved_at = resolved_at
        append_event(
            db,
            room,
            "seat.restore_expired",
            {"request_id": request.id, "seat_key": request.seat.seat_key, "reason": reason},
        )
    return len(pending)


def create_restore_request(
    db: Session,
    room: Room,
    requester: User,
    provided_idempotency_key: str | None,
) -> tuple[SeatRestoreRequest, bool]:
    acquire_transaction_locks(db, f"1:user:{requester.id}", f"2:room:{room.id}")
    db.scalar(select(User.id).where(User.id == requester.id).with_for_update())
    if room.status not in ACTIVE_ROOM_STATUSES:
        raise HTTPException(status_code=409, detail="比赛已经结束或取消，不能申请恢复席位。")
    seat = next((item for item in room.seats if item.user_id == requester.id), None)
    if not seat or seat.occupant_type != "ai_substitute":
        raise HTTPException(status_code=409, detail="你当前没有由 AI 接替的真人席位。")
    if not requester.is_active:
        raise HTTPException(status_code=409, detail="账号当前不可用，不能申请恢复席位。")
    if _active_speech_exists(db, room):
        raise HTTPException(status_code=409, detail="当前仍有发言正在进行，请在发言结束后申请恢复。")
    conflict = _conflicting_human_room(db, room, requester.id)
    if conflict:
        raise HTTPException(status_code=409, detail=f"你已在房间 #{conflict} 参赛，不能同时恢复本场席位。")

    operation_key = None
    if provided_idempotency_key:
        digest = hashlib.sha256(provided_idempotency_key.strip().encode()).hexdigest()
        operation_key = f"seat-restore-request:{room.id}:{requester.id}:{digest}"
        replay = db.scalar(select(SeatRestoreRequest).where(SeatRestoreRequest.idempotency_key == operation_key))
        if replay:
            return replay, True

    pending = db.scalar(
        select(SeatRestoreRequest)
        .where(
            SeatRestoreRequest.room_id == room.id,
            SeatRestoreRequest.seat_id == seat.id,
            SeatRestoreRequest.requester_user_id == requester.id,
            SeatRestoreRequest.status == "pending",
        )
        .order_by(SeatRestoreRequest.created_at.desc())
        .limit(1)
    )
    if pending:
        return pending, True

    request = SeatRestoreRequest(
        room_id=room.id,
        seat_id=seat.id,
        requester_user_id=requester.id,
        status="pending",
        idempotency_key=operation_key,
    )
    db.add(request)
    db.flush()
    append_event(
        db,
        room,
        "seat.restore_requested",
        {"request_id": request.id, "seat_key": seat.seat_key, "user_id": requester.id},
        actor_user_id=requester.id,
    )
    return request, False


def cancel_restore_request(db: Session, room: Room, request: SeatRestoreRequest, actor: User) -> bool:
    acquire_transaction_locks(db, f"1:user:{actor.id}", f"2:room:{room.id}")
    if request.room_id != room.id or request.requester_user_id != actor.id:
        raise HTTPException(status_code=403, detail="只能撤销自己的席位恢复申请。")
    if request.status == "cancelled":
        return True
    status, reason = effective_status(request, room, request.seat)
    if status != "pending":
        if status == "expired" and request.status == "pending":
            request.status = "expired"
            request.resolution_reason = reason
            request.resolved_at = _now()
        raise HTTPException(status_code=409, detail="该恢复申请已经处理或失效。")
    request.status = "cancelled"
    request.resolution_reason = "参赛者主动撤销"
    request.resolved_by_user_id = actor.id
    request.resolved_at = _now()
    append_event(
        db,
        room,
        "seat.restore_cancelled",
        {"request_id": request.id, "seat_key": request.seat.seat_key},
        actor_user_id=actor.id,
    )
    return False


def review_restore_request(
    db: Session,
    room: Room,
    request: SeatRestoreRequest,
    reviewer: User,
    *,
    approve: bool,
    reason: str = "",
) -> bool:
    if not can_control(db, room, reviewer):
        raise HTTPException(status_code=403, detail="仅房主或系统管理员可以审批恢复申请。")
    acquire_transaction_locks(db, f"1:user:{request.requester_user_id}", f"2:room:{room.id}")
    if request.room_id != room.id:
        raise HTTPException(status_code=404, detail="恢复申请不存在。")
    if request.status == ("approved" if approve else "rejected"):
        return True
    seat = request.seat
    status, expired_reason = effective_status(request, room, seat)
    if status != "pending":
        if status == "expired" and request.status == "pending":
            request.status = "expired"
            request.resolution_reason = expired_reason
            request.resolved_at = _now()
        raise HTTPException(status_code=409, detail="该恢复申请已经处理或失效。")
    if not approve:
        request.status = "rejected"
        request.resolution_reason = reason or "房间管理者暂未批准"
        request.resolved_by_user_id = reviewer.id
        request.resolved_at = _now()
        append_event(
            db,
            room,
            "seat.restore_rejected",
            {"request_id": request.id, "seat_key": seat.seat_key},
            actor_user_id=reviewer.id,
        )
        return False

    if not seat.connected:
        raise HTTPException(status_code=409, detail="原辩手尚未重新连接，返回比赛后才能恢复真人控制。")
    if _active_speech_exists(db, room):
        raise HTTPException(status_code=409, detail="当前仍有发言正在进行，结束后才能恢复真人控制。")
    participant = db.get(User, request.requester_user_id)
    if not participant or not participant.is_active:
        raise HTTPException(status_code=409, detail="原辩手账号不可用，无法恢复席位。")
    conflict = _conflicting_human_room(db, room, participant.id)
    if conflict:
        raise HTTPException(status_code=409, detail=f"原辩手已在房间 #{conflict} 参赛，不能同时恢复旧席位。")

    seat.occupant_type = "human"
    seat.display_name = participant.real_name
    seat.agent_profile_id = None
    seat.is_ready = True
    seat.disconnected_at = None if seat.connected else seat.disconnected_at
    request.status = "approved"
    request.resolution_reason = reason or "恢复真人控制"
    request.resolved_by_user_id = reviewer.id
    request.resolved_at = _now()
    other_pending = db.scalars(
        select(SeatRestoreRequest).where(
            SeatRestoreRequest.room_id == room.id,
            SeatRestoreRequest.seat_id == seat.id,
            SeatRestoreRequest.id != request.id,
            SeatRestoreRequest.status == "pending",
        )
    ).all()
    for item in other_pending:
        item.status = "expired"
        item.resolution_reason = "席位已通过另一申请恢复"
        item.resolved_at = _now()
    append_event(
        db,
        room,
        "seat.human_restored",
        {"request_id": request.id, "seat_key": seat.seat_key, "user_id": participant.id, "connected": seat.connected},
        actor_user_id=reviewer.id,
    )
    return False
