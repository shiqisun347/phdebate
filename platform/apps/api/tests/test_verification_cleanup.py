from __future__ import annotations

from app.core.database import SessionLocal
from app.models.entities import Room, RoomCodeReservation
from app.services.verification_cleanup import release_verification_room_codes


def test_release_verification_room_codes_only_removes_requested_reservations(register_user) -> None:
    first = register_user("verification_code_first")
    second = register_user("verification_code_second")
    first_room = first.post(
        "/api/rooms",
        headers={"X-CSRF-Token": first.cookies["jixia_csrf"]},
        json={
            "competition_slug": "training-1v1",
            "custom_topic": "验证脚本能否清理自己的房间号？",
            "seat_key": "aff_1",
            "visibility": "private",
        },
    ).json()["room"]
    second_room = second.post(
        "/api/rooms",
        headers={"X-CSRF-Token": second.cookies["jixia_csrf"]},
        json={
            "competition_slug": "training-1v1",
            "custom_topic": "未指定的房间号是否保持永久保留？",
            "seat_key": "aff_1",
            "visibility": "private",
        },
    ).json()["room"]

    with SessionLocal() as db:
        assert db.get(RoomCodeReservation, first_room["code"])
        assert db.get(RoomCodeReservation, second_room["code"])
        released = release_verification_room_codes(db, [first_room["id"]])
        db.commit()
        assert released == [first_room["code"]]
        assert db.get(Room, first_room["id"])
        assert db.get(RoomCodeReservation, first_room["code"]) is None
        assert db.get(RoomCodeReservation, second_room["code"])
