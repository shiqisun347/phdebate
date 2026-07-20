#!/usr/bin/env python3
"""Verify participant-requested seat restoration against a live deployment."""

from __future__ import annotations

import argparse
import shutil
import ssl
import time
from secrets import token_hex
from urllib.parse import urlsplit, urlunsplit

import httpx
from app.core.config import settings
from app.core.database import SessionLocal
from app.models.entities import (
    AudioAsset,
    JudgeScorecard,
    Match,
    MatchEvent,
    RatingChange,
    Room,
    RoomSeat,
    SeatRestoreRequest,
    Speech,
    TranscriptSegment,
    User,
    UserSession,
)
from app.services.room_service import append_event, load_room
from app.services.verification_cleanup import release_verification_room_codes
from sqlalchemy import delete, select
from websockets.sync.client import ClientConnection, connect

PASSWORD = "Seat-restore-verify-1234"


def csrf(client: httpx.Client) -> dict[str, str]:
    value = client.cookies.get("jixia_csrf")
    return {"X-CSRF-Token": value} if value else {}


def register(client: httpx.Client, account: str, real_name: str) -> str:
    response = client.post(
        "/api/auth/register",
        json={"account": account, "real_name": real_name, "password": PASSWORD, "confirm_password": PASSWORD},
    )
    response.raise_for_status()
    return response.json()["user"]["id"]


def reconnect_room_presence(client: httpx.Client, base_url: str, code: str) -> ClientConnection:
    parsed = urlsplit(base_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    websocket_url = urlunsplit((scheme, parsed.netloc, f"/ws/rooms/{code}", "", ""))
    cookie_header = "; ".join(f"{key}={value}" for key, value in client.cookies.items())
    ssl_context = None
    if scheme == "wss":
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
    connection = connect(
        websocket_url,
        additional_headers={"Cookie": cookie_header},
        ssl=ssl_context,
        open_timeout=20,
        close_timeout=5,
    )
    connection.recv(timeout=20)
    return connection


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://117.50.192.216")
    args = parser.parse_args()
    suffix = f"{int(time.time())}_{token_hex(3)}"
    owner = httpx.Client(base_url=args.base_url, verify=False, timeout=30, follow_redirects=True)
    participant = httpx.Client(base_url=args.base_url, verify=False, timeout=30, follow_redirects=True)
    user_ids: list[str] = []
    room_codes: list[str] = []
    participant_presence: ClientConnection | None = None
    try:
        owner_id = register(owner, f"qa_restore_owner_{suffix}", "恢复验收房主")
        participant_id = register(participant, f"qa_restore_student_{suffix}", "恢复验收辩手")
        user_ids.extend([owner_id, participant_id])
        with SessionLocal() as db:
            for user_id in user_ids:
                user = db.get(User, user_id)
                assert user
                user.is_test_account = True
            db.commit()

        created = owner.post(
            "/api/rooms",
            headers=csrf(owner) | {"X-Idempotency-Key": token_hex(16)},
            json={
                "competition_slug": "training-1v1",
                "custom_topic": "AI 接替后原辩手是否应由房主审批恢复？",
                "seat_key": "aff_1",
                "visibility": "public",
            },
        )
        created.raise_for_status()
        code = created.json()["room"]["code"]
        room_codes.append(code)
        participant.post(f"/api/rooms/{code}/claim-seat", headers=csrf(participant), json={"seat_key": "neg_1"}).raise_for_status()
        owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True}).raise_for_status()
        participant.post(f"/api/rooms/{code}/ready", headers=csrf(participant), json={"ready": True}).raise_for_status()
        owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={}).raise_for_status()

        with SessionLocal() as db:
            room = load_room(db, code, lock=True)
            seat = next(item for item in room.seats if item.user_id == participant_id)
            seat.occupant_type = "ai_substitute"
            seat.display_name = f"AI 接替·{seat.display_name}"
            append_event(
                db,
                room,
                "seat.ai_substituted",
                {"seat_key": seat.seat_key, "reason": "live restore verification"},
                actor_user_id=participant_id,
            )
            db.commit()

        # A restore request can be created while watch-only, but approval must
        # prove that the original participant has actually returned to the
        # room.  Keep the same WebSocket presence path used by the browser open
        # for the remainder of this verification.
        participant_presence = reconnect_room_presence(participant, args.base_url, code)
        headers = csrf(participant) | {"X-Idempotency-Key": "live-seat-restore"}
        requested = participant.post(f"/api/rooms/{code}/seat-restore-requests", headers=headers, json={})
        replayed = participant.post(f"/api/rooms/{code}/seat-restore-requests", headers=headers, json={})
        requested.raise_for_status()
        replayed.raise_for_status()
        request_id = requested.json()["request"]["id"]
        assert replayed.json()["replayed"] is True and replayed.json()["request"]["id"] == request_id

        owner_view = owner.get(f"/api/rooms/{code}")
        owner_view.raise_for_status()
        pending = owner_view.json()["room"]["seat_restore_requests"]
        assert len(pending) == 1 and pending[0]["can_review"] is True
        approved = owner.post(
            f"/api/rooms/{code}/seat-restore-requests/{request_id}/approve",
            headers=csrf(owner),
            json={"reason": "生产恢复闭环验收"},
        )
        approved.raise_for_status()
        restored_room = approved.json()["room"]
        restored_seat = next(item for item in restored_room["seats"] if item["seat_key"] == "neg_1")
        restored_request = next(item for item in restored_room["seat_restore_requests"] if item["id"] == request_id)
        assert restored_seat["occupant_type"] == "human"
        assert restored_request["status"] == "approved"
        owner.post(
            f"/api/rooms/{code}/control/terminate",
            headers=csrf(owner),
            json={"reason": "seat restore verification complete"},
        ).raise_for_status()
        print(
            "seat_restore_flow_verified request=1 replay=1 owner_approved=1 restored=1 "
            f"room={code} qa_isolated=true"
        )
    finally:
        if participant_presence is not None:
            participant_presence.close()
        owner.close()
        participant.close()
        if user_ids:
            with SessionLocal() as db:
                room_ids = list(db.scalars(select(Room.id).where(Room.owner_id.in_(user_ids))).all())
                match_ids = list(db.scalars(select(Match.id).where(Match.room_id.in_(room_ids))).all())
                speech_ids = list(db.scalars(select(Speech.id).where(Speech.room_id.in_(room_ids))).all())
                if speech_ids:
                    db.execute(delete(TranscriptSegment).where(TranscriptSegment.speech_id.in_(speech_ids)))
                if match_ids:
                    db.execute(delete(RatingChange).where(RatingChange.match_id.in_(match_ids)))
                    db.execute(delete(JudgeScorecard).where(JudgeScorecard.match_id.in_(match_ids)))
                    db.execute(delete(AudioAsset).where(AudioAsset.match_id.in_(match_ids)))
                if room_ids:
                    db.execute(delete(SeatRestoreRequest).where(SeatRestoreRequest.room_id.in_(room_ids)))
                    db.execute(delete(Speech).where(Speech.room_id.in_(room_ids)))
                    db.execute(delete(MatchEvent).where(MatchEvent.room_id.in_(room_ids)))
                    db.execute(delete(Match).where(Match.id.in_(match_ids)))
                    db.execute(delete(RoomSeat).where(RoomSeat.room_id.in_(room_ids)))
                    release_verification_room_codes(db, room_ids)
                    db.execute(delete(Room).where(Room.id.in_(room_ids)))
                db.execute(delete(UserSession).where(UserSession.user_id.in_(user_ids)))
                db.execute(delete(User).where(User.id.in_(user_ids)))
                db.commit()
        for code in room_codes:
            shutil.rmtree(settings.media_path / code, ignore_errors=True)


if __name__ == "__main__":
    main()
