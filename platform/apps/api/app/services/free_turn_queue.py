from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from app.core.database import acquire_transaction_locks
from app.models.entities import FreeTurnRequest, Match, Room, RoomSeat, Speech, User
from app.services.room_service import append_event, free_turn_duration, now, remaining_seconds, stage
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

INTERMISSION_SECONDS = 3
ACTIVE_SPEECH_STATUSES = {"speaking", "synthesizing", "playing"}


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _parse_time(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return _aware(datetime.fromisoformat(str(value)))
    except ValueError:
        return None


def intermission_deadline(room: Room, current: dict | None = None) -> datetime | None:
    current = current or stage(room)
    if not current or current.get("kind") != "free":
        return None
    return _parse_time(current.get("intermission_deadline_at"))


def intermission_remaining_ms(room: Room, current: dict | None = None) -> int | None:
    deadline = intermission_deadline(room, current)
    if not deadline:
        return None
    if room.status == "paused":
        paused = (current or stage(room) or {}).get("paused_intermission_remaining_ms")
        return max(0, int(paused)) if paused is not None else 0
    return max(0, round((deadline - now()).total_seconds() * 1000))


def _target_context(db: Session, room: Room, seat: RoomSeat) -> tuple[dict, int, str]:
    current = stage(room)
    if room.status != "running" or not current or current.get("kind") != "free":
        raise HTTPException(status_code=409, detail="当前不在可申请的自由辩论发言窗口。")
    stage_deadline = room.stage_deadline_at
    if stage_deadline and _aware(stage_deadline) <= now():
        raise HTTPException(status_code=409, detail="自由辩论环节已经结束，不能再申请下一轮发言。")
    if seat.occupant_type != "human" or not seat.user_id:
        raise HTTPException(status_code=409, detail="只有当前真人辩手可以申请自由辩论发言。")
    if not seat.connected:
        raise HTTPException(status_code=409, detail="请先返回比赛并恢复在线状态后再申请发言。")
    current_turn = max(0, int(current.get("turn_seq", 0)))
    deadline = intermission_deadline(room, current)
    if deadline:
        if now() >= deadline:
            raise HTTPException(status_code=409, detail="本轮三秒申请窗口已经结束。")
        eligible_side = str(current.get("intermission_side") or "")
        target_turn = max(current_turn + 1, int(current.get("intermission_turn_seq", current_turn + 1)))
    else:
        active = db.scalar(
            select(Speech).where(
                Speech.room_id == room.id,
                Speech.stage_key == str(current.get("key")),
                Speech.status.in_(ACTIVE_SPEECH_STATUSES),
            )
        )
        if not active:
            raise HTTPException(status_code=409, detail="请在对方发言开始后申请下一轮发言。")
        active_seat = next((item for item in room.seats if item.seat_key == active.seat_key), None)
        if not active_seat:
            raise HTTPException(status_code=409, detail="当前发言席位状态异常，请刷新后重试。")
        if active.speaker_type == "ai" and not active.playback_started_at:
            raise HTTPException(status_code=409, detail="请等待对方 AI 发言实际开始播放后再申请。")
        eligible_side = "neg" if active_seat.side == "aff" else "aff"
        target_turn = current_turn + 1
    if seat.side != eligible_side:
        raise HTTPException(status_code=403, detail="只有下一发言阵营的真人辩手可以申请。")
    return current, target_turn, eligible_side


def _idempotency_key(room: Room, current: dict, target_turn: int, seat: RoomSeat, user: User, supplied: str | None) -> str | None:
    if not supplied:
        return None
    digest = hashlib.sha256(
        f"free-turn:{room.id}:{current.get('key')}:{target_turn}:{seat.seat_key}:{user.id}:{supplied[:128]}".encode()
    ).hexdigest()
    return f"free-turn:{digest}"


def request_free_turn(
    db: Session,
    room: Room,
    user: User,
    *,
    provided_idempotency_key: str | None,
) -> tuple[FreeTurnRequest, bool]:
    acquire_transaction_locks(db, f"1:user:{user.id}", f"2:room:{room.id}")
    seat = next((item for item in room.seats if item.user_id == user.id), None)
    if not seat:
        raise HTTPException(status_code=403, detail="你不是本房间辩手。")
    current, target_turn, eligible_side = _target_context(db, room, seat)
    match = db.scalar(select(Match).where(Match.room_id == room.id))
    if not match:
        raise HTTPException(status_code=409, detail="比赛记录不存在。")
    operation_key = _idempotency_key(room, current, target_turn, seat, user, provided_idempotency_key)
    fingerprint = hashlib.sha256(f"{room.id}:{current.get('key')}:{target_turn}:{seat.seat_key}:{user.id}".encode()).hexdigest()
    if operation_key:
        replay = db.scalar(select(FreeTurnRequest).where(FreeTurnRequest.idempotency_key == operation_key))
        if replay:
            if replay.request_fingerprint != fingerprint:
                raise HTTPException(status_code=409, detail="同一个幂等键不能用于不同的自由辩论轮次。")
            return replay, True
    pending = db.scalar(
        select(FreeTurnRequest).where(
            FreeTurnRequest.room_id == room.id,
            FreeTurnRequest.stage_key == str(current.get("key")),
            FreeTurnRequest.turn_seq == target_turn,
            FreeTurnRequest.seat_key == seat.seat_key,
            FreeTurnRequest.status == "pending",
        )
    )
    if pending:
        return pending, True
    item = FreeTurnRequest(
        room_id=room.id,
        match_id=match.id,
        stage_key=str(current.get("key")),
        turn_seq=target_turn,
        side=eligible_side,
        seat_key=seat.seat_key,
        user_id=user.id,
        idempotency_key=operation_key,
        request_fingerprint=fingerprint,
    )
    db.add(item)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="该席位本轮已有发言申请，请刷新后查看。") from exc
    append_event(
        db,
        room,
        "free.turn_requested",
        {"request_id": item.id, "seat_key": seat.seat_key, "side": seat.side, "turn_seq": target_turn},
        actor_user_id=user.id,
    )
    return item, False


def cancel_free_turn(db: Session, room: Room, request: FreeTurnRequest, user: User) -> bool:
    acquire_transaction_locks(db, f"1:user:{user.id}", f"2:room:{room.id}")
    if request.room_id != room.id or request.user_id != user.id:
        raise HTTPException(status_code=403, detail="只能取消自己的自由辩论申请。")
    if request.status == "cancelled":
        return True
    if request.status != "pending":
        raise HTTPException(status_code=409, detail="该申请已经选中或失效，不能取消。")
    request.status = "cancelled"
    request.resolved_at = now()
    request.resolution_reason = "participant_cancelled"
    append_event(
        db,
        room,
        "free.turn_request_cancelled",
        {"request_id": request.id, "seat_key": request.seat_key, "turn_seq": request.turn_seq},
        actor_user_id=user.id,
    )
    return False


def begin_intermission(db: Session, room: Room, current: dict) -> dict:
    # Free-debate time is speaking time, not wall-clock time. Freeze the
    # authoritative total before the three-second application window.
    stage_remaining = remaining_seconds(room)
    target_turn = max(0, int(current.get("turn_seq", 0))) + 1
    next_side = "neg" if current.get("side") == "aff" else "aff"
    deadline = now() + timedelta(seconds=INTERMISSION_SECONDS)
    updated = dict(current)
    updated["free_stage_remaining_seconds"] = max(0, int(stage_remaining or 0))
    updated["intermission_side"] = next_side
    updated["intermission_turn_seq"] = target_turn
    updated["intermission_started_at"] = now().isoformat()
    updated["intermission_deadline_at"] = deadline.isoformat()
    updated.pop("selected_human_seat", None)
    updated.pop("force_ai_fallback", None)
    updated.pop("ai_preparing", None)
    updated.pop("preparing_stage_remaining_seconds", None)
    updated.pop("preparing_turn_remaining_seconds", None)
    updated.pop("turn_started_at", None)
    updated.pop("awaiting_human_start", None)
    room.stage_started_at = None
    room.stage_deadline_at = None
    snapshot = list(room.template_snapshot)
    snapshot[room.current_stage_index] = updated
    room.template_snapshot = snapshot
    append_event(
        db,
        room,
        "free.intermission_started",
        {"side": next_side, "turn_seq": target_turn, "deadline_at": deadline.isoformat()},
    )
    return updated


def resolve_intermission(db: Session, room: Room, current: dict) -> tuple[dict, FreeTurnRequest | None]:
    deadline = intermission_deadline(room, current)
    if not deadline or now() < deadline:
        return current, None
    stage_key = str(current.get("key"))
    target_turn = int(current.get("intermission_turn_seq", int(current.get("turn_seq", 0)) + 1))
    side = str(current.get("intermission_side") or ("neg" if current.get("side") == "aff" else "aff"))
    pending = list(
        db.scalars(
            select(FreeTurnRequest)
            .where(
                FreeTurnRequest.room_id == room.id,
                FreeTurnRequest.stage_key == stage_key,
                FreeTurnRequest.turn_seq == target_turn,
                FreeTurnRequest.side == side,
                FreeTurnRequest.status == "pending",
            )
            .order_by(FreeTurnRequest.requested_at, FreeTurnRequest.id)
        ).all()
    )
    seat_by_key = {item.seat_key: item for item in room.seats}
    winner = next(
        (
            item
            for item in pending
            if (seat := seat_by_key.get(item.seat_key))
            and seat.user_id == item.user_id
            and seat.side == side
            and seat.occupant_type == "human"
            and seat.connected
        ),
        None,
    )
    frozen_stage_remaining = current.get("free_stage_remaining_seconds")
    if frozen_stage_remaining is None:
        frozen_stage_remaining = remaining_seconds(room)
    stage_remaining_seconds = max(0, int(frozen_stage_remaining or 0))
    # The application window can close on the exact tick that exhausts the
    # whole free-debate budget.  No queued human and no permanent Agent may be
    # granted a fresh turn after that boundary; the next engine tick must end
    # the stage.  Keeping ``winner`` here previously let a 0-second stage be
    # restarted for one second, while the AI branch re-armed a full fallback
    # turn and produced an extra transcript entry.
    if stage_remaining_seconds == 0:
        winner = None
    resolved_at = now()
    for item in pending:
        item.resolved_at = resolved_at
        if item is winner:
            item.status = "selected"
            item.resolution_reason = "earliest_valid"
        else:
            item.status = "expired"
            item.resolution_reason = (
                "stage_time_elapsed"
                if stage_remaining_seconds == 0
                else "another_request_selected"
                if winner
                else "window_closed"
            )
    updated = dict(current)
    updated["free_stage_remaining_seconds"] = stage_remaining_seconds
    for key in (
        "intermission_side",
        "intermission_turn_seq",
        "intermission_started_at",
        "intermission_deadline_at",
        "paused_intermission_remaining_ms",
    ):
        updated.pop(key, None)
    updated["side"] = side
    updated["turn_seq"] = target_turn
    room.stage_started_at = None
    room.stage_deadline_at = None
    if winner:
        updated["selected_human_seat"] = winner.seat_key
        updated["awaiting_human_start"] = True
        updated.pop("force_ai_fallback", None)
        updated.pop("ai_preparing", None)
        updated.pop("preparing_stage_remaining_seconds", None)
        updated.pop("preparing_turn_remaining_seconds", None)
    else:
        updated.pop("selected_human_seat", None)
        # A missed three-second request window must never turn into an AI
        # takeover for an all-human match.  When the target side has a
        # permanent AI seat, the engine may start that AI turn; otherwise the
        # newly selected side gets a normal human turn window and may press
        # the speaking button immediately after the intermission.  The old
        # implementation set ``force_ai_fallback`` even when no AI existed,
        # which disabled the human button and made a no-AI room appear stuck
        # until the full turn timer elapsed.
        has_ai = any(item.side == side and item.occupant_type == "ai" for item in room.seats)
        if stage_remaining_seconds == 0:
            updated.pop("force_ai_fallback", None)
            updated.pop("ai_preparing", None)
            updated.pop("preparing_stage_remaining_seconds", None)
            updated.pop("preparing_turn_remaining_seconds", None)
            updated.pop("awaiting_human_start", None)
        elif has_ai:
            updated["force_ai_fallback"] = True
            updated.pop("awaiting_human_start", None)
            # Resolving the three-second application window is the
            # authoritative hand-off to an AI turn.  Persist that phase in the
            # same transaction instead of leaving the room looking expired
            # until the next engine pass has finished its Agent intent/text
            # speculation.  Provider work may legitimately take a few
            # seconds, but it must be visible as preparation with both clocks
            # frozen, never as a hidden 00:00 transition.
            updated["ai_preparing"] = True
            updated["preparing_stage_remaining_seconds"] = max(1, int(frozen_stage_remaining or 0))
            updated["preparing_turn_remaining_seconds"] = free_turn_duration(updated)
        else:
            updated.pop("force_ai_fallback", None)
            updated.pop("ai_preparing", None)
            updated.pop("preparing_stage_remaining_seconds", None)
            updated.pop("preparing_turn_remaining_seconds", None)
            updated["awaiting_human_start"] = True
    snapshot = list(room.template_snapshot)
    snapshot[room.current_stage_index] = updated
    room.template_snapshot = snapshot
    append_event(
        db,
        room,
        "free.intermission_resolved",
        {
            "side": side,
            "turn_seq": target_turn,
            "seat_key": winner.seat_key if winner else None,
            "fallback": winner is None and stage_remaining_seconds > 0,
            "stage_exhausted": stage_remaining_seconds == 0,
        },
    )
    append_event(db, room, "free.side_changed", {"side": side, "turn_seq": target_turn})
    return updated, winner


def expire_room_requests(db: Session, room: Room, reason: str) -> int:
    rows = list(
        db.scalars(
            select(FreeTurnRequest).where(
                FreeTurnRequest.room_id == room.id,
                FreeTurnRequest.status == "pending",
            )
        ).all()
    )
    for item in rows:
        item.status = "expired"
        item.resolved_at = now()
        item.resolution_reason = reason
    return len(rows)


def queue_projection(db: Session, room: Room, user: User | None) -> dict:
    current = stage(room)
    if not current or current.get("kind") != "free":
        return {
            "items": [],
            "window_deadline_at": None,
            "window_remaining_ms": None,
            "my_request": None,
            "can_request": False,
            "request_reason": "当前不是自由辩论环节",
        }
    target_turn = int(current.get("intermission_turn_seq", int(current.get("turn_seq", 0)) + 1))
    items = list(
        db.scalars(
            select(FreeTurnRequest)
            .where(
                FreeTurnRequest.room_id == room.id,
                FreeTurnRequest.stage_key == str(current.get("key")),
                FreeTurnRequest.turn_seq == target_turn,
                FreeTurnRequest.status == "pending",
            )
            .order_by(FreeTurnRequest.requested_at, FreeTurnRequest.id)
        ).all()
    )
    projected = [
        {
            "side": item.side,
            "seat_key": item.seat_key,
            "order": index + 1,
            "requested_at": _aware(item.requested_at).isoformat(),
            "is_me": bool(user and item.user_id == user.id),
            **({"request_id": item.id} if user and item.user_id == user.id else {}),
        }
        for index, item in enumerate(items)
    ]
    mine = next((item for item in projected if item["is_me"]), None)
    deadline = intermission_deadline(room, current)
    can_request = False
    request_reason = "登录并作为下一方真人辩手后可申请"
    if user:
        seat = next((item for item in room.seats if item.user_id == user.id), None)
        if seat:
            try:
                _target_context(db, room, seat)
                can_request = mine is None
                request_reason = "已申请" if mine else "可申请下一轮发言"
            except HTTPException as exc:
                request_reason = str(exc.detail)
    return {
        "items": projected,
        "window_deadline_at": deadline.isoformat() if deadline else None,
        "window_remaining_ms": intermission_remaining_ms(room, current),
        "my_request": mine,
        "target_side": current.get("intermission_side") or ("neg" if current.get("side") == "aff" else "aff"),
        "target_turn_seq": target_turn,
        "can_request": can_request,
        "request_reason": request_reason,
    }
