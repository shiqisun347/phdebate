from __future__ import annotations

import json
from datetime import timedelta

from app.core.database import SessionLocal
from app.models.entities import (
    AdminAuditLog,
    JudgeScorecard,
    Match,
    MatchEvent,
    Speech,
    SpeechCorrectionRequest,
    TranscriptSegment,
)
from app.services.room_service import append_event, load_room, now
from conftest import csrf
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from test_platform import create_training_room


def _completed_human_match(owner: TestClient, topic: str) -> tuple[str, str, str]:
    owner_id = owner.get("/api/auth/session").json()["user"]["id"]
    code = create_training_room(owner, topic)["code"]
    assert owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True}).status_code == 200
    assert owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={}).status_code == 200
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        speech = Speech(
            match_id=match.id,
            room_id=room.id,
            seat_key="aff_1",
            stage_key="aff_case",
            speaker_type="human",
            status="completed",
            content="原始误提交文字包含一个明显错误。",
            duration_seconds=6.4,
        )
        db.add(speech)
        db.flush()
        db.add(
            TranscriptSegment(
                speech_id=speech.id,
                start_ms=120,
                end_ms=6400,
                text=speech.content,
                is_final=True,
            )
        )
        scorecard = JudgeScorecard(
            match_id=match.id,
            status="approved",
            winner="aff",
            affirmative_score=88,
            negative_score=82,
            reasoning="原裁判结果保持不变。",
        )
        db.add(scorecard)
        room.status = "completed"
        room.completed_at = now()
        match.status = "completed"
        match.winner = "aff"
        match.result_reason = scorecard.reasoning
        append_event(
            db,
            room,
            "speech.completed",
            {"speech_id": speech.id, "seat_key": speech.seat_key, "content": speech.content},
            actor_user_id=owner_id,
        )
        append_event(db, room, "match.completed", {"winner": "aff", "scorecard_id": scorecard.id})
        db.commit()
        return code, match.id, speech.id


def _admin(client: TestClient) -> TestClient:
    admin = TestClient(client.app)
    admin.__enter__()
    login = admin.post("/api/auth/login", json={"account": "admin_test", "password": "Admin-test-1234"})
    assert login.status_code == 200, login.text
    return admin


def test_human_correction_is_idempotent_admin_reviewed_and_archive_auditable(client, register_user) -> None:
    owner = register_user("round19_correction_owner")
    outsider = register_user("round19_correction_outsider")
    code, match_id, speech_id = _completed_human_match(owner, "Round19 真人误提交纠正闭环")
    outsider_id = outsider.get("/api/auth/session").json()["user"]["id"]
    # Current seat state is mutable repair state and must not redefine who
    # authored an already-committed speech.  Simulate a later seat transfer.
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        seat = next(item for item in room.seats if item.seat_key == "aff_1")
        seat.user_id = outsider_id
        seat.display_name = "后续接替者"
        later_speech = Speech(
            match_id=match_id,
            room_id=room.id,
            seat_key="aff_1",
            stage_key="later_free_turn",
            speaker_type="human",
            status="completed",
            content="同一席位后续实际发言者提交的文字。",
        )
        db.add(later_speech)
        db.flush()
        append_event(
            db,
            room,
            "speech.completed",
            {"speech_id": later_speech.id, "seat_key": "aff_1", "content": later_speech.content},
            actor_user_id=outsider_id,
        )
        db.commit()
        later_speech_id = later_speech.id
    later_request = outsider.post(
        f"/api/rooms/{code}/speeches/{later_speech_id}/correction-requests",
        headers=csrf(outsider),
        json={"proposed_content": "同一席位后续实际发言者修正后的文字。", "reason": "后续发言者纠错"},
    )
    assert later_request.status_code == 200, later_request.text
    assert owner.post(
        f"/api/rooms/{code}/speeches/{later_speech_id}/correction-requests",
        headers=csrf(owner),
        json={"proposed_content": "原开赛者不能修改后来发言者的文字。", "reason": "越权测试"},
    ).status_code == 403
    assert outsider.post(
        f"/api/rooms/{code}/speech-correction-requests/{later_request.json()['request']['id']}/cancel",
        headers=csrf(outsider),
        json={},
    ).status_code == 200
    request_headers = csrf(owner) | {"X-Idempotency-Key": "round19-correction-once"}
    payload = {
        "proposed_content": "修正后的最终发言文字准确表达了选手观点。",
        "reason": "语音识别将关键结论转写错误",
    }
    created = owner.post(
        f"/api/rooms/{code}/speeches/{speech_id}/correction-requests",
        headers=request_headers,
        json=payload,
    )
    replayed = owner.post(
        f"/api/rooms/{code}/speeches/{speech_id}/correction-requests",
        headers=request_headers,
        json=payload,
    )
    conflict = owner.post(
        f"/api/rooms/{code}/speeches/{speech_id}/correction-requests",
        headers=request_headers,
        json=payload | {"proposed_content": "另一个不同的修正版本。"},
    )
    assert created.status_code == 200 and created.json()["replayed"] is False
    assert replayed.status_code == 200 and replayed.json()["replayed"] is True
    assert conflict.status_code == 409
    request_id = created.json()["request"]["id"]

    assert client.get(f"/api/rooms/{code}/speech-correction-requests").status_code == 401
    outsider_items = outsider.get(f"/api/rooms/{code}/speech-correction-requests").json()["items"]
    assert request_id not in {item["id"] for item in outsider_items}
    assert {item["status"] for item in outsider_items} == {"cancelled"}
    assert owner.get(f"/api/rooms/{code}/speech-correction-requests").json()["items"][0]["id"] == request_id
    assert outsider.post(
        f"/api/rooms/{code}/speeches/{speech_id}/correction-requests",
        headers=csrf(outsider),
        json=payload,
    ).status_code == 403

    admin = _admin(client)
    try:
        listing = admin.get("/api/admin/speech-corrections")
        pending = next(item for item in listing.json()["items"] if item["id"] == request_id)
        assert pending["requester_name"].startswith("测试选手")
        stale = admin.post(
            f"/api/admin/speech-corrections/{request_id}/approve",
            headers=csrf(admin),
            json={
                "expected_updated_at": (now() - timedelta(days=1)).isoformat(),
                "reason": "错误的旧页面审批",
            },
        )
        assert stale.status_code == 409
        approved = admin.post(
            f"/api/admin/speech-corrections/{request_id}/approve",
            headers=csrf(admin),
            json={
                "expected_updated_at": pending["updated_at"],
                "reason": "已对照参赛者录音和上下文确认",
            },
        )
        repeated = admin.post(
            f"/api/admin/speech-corrections/{request_id}/approve",
            headers=csrf(admin),
            json={
                "expected_updated_at": pending["updated_at"],
                "reason": "重复审批",
            },
        )
        assert approved.status_code == 200, approved.text
        assert repeated.status_code == 409
        research_archive = admin.get(f"/api/matches/{match_id}/archive")
        assert research_archive.status_code == 200
        research_correction = next(
            item for item in research_archive.json()["data"]["speech_corrections"] if item["id"] == request_id
        )
        assert research_correction["requester_user_id"]
        assert research_correction["original_segments"][0]["start_ms"] == 120
        assert research_correction["after_judging"] is True
        provenance = research_archive.json()["data"]["scorecard"]["transcript_provenance"]
        assert provenance["basis"] == "pre_correction"
        assert provenance["correction_after_judging_count"] == 1
        assert provenance["judged_transcript_sha256"] != provenance["current_transcript_sha256"]
    finally:
        admin.__exit__(None, None, None)

    with SessionLocal() as db:
        speech = db.get(Speech, speech_id)
        request = db.get(SpeechCorrectionRequest, request_id)
        segments = list(db.scalars(select(TranscriptSegment).where(TranscriptSegment.speech_id == speech_id)).all())
        match = db.get(Match, match_id)
        scorecard = db.scalar(select(JudgeScorecard).where(JudgeScorecard.match_id == match_id))
        assert speech.content == payload["proposed_content"]
        assert len(segments) == 1 and segments[0].text == speech.content and segments[0].end_ms == 6400
        assert request.status == "approved"
        assert request.original_content == "原始误提交文字包含一个明显错误。"
        assert request.original_segments[0]["text"] == request.original_content
        assert match.winner == "aff" and scorecard.reasoning == "原裁判结果保持不变。"
        assert db.scalar(
                select(func.count(MatchEvent.id)).where(
                    MatchEvent.room_id == request.room_id,
                    MatchEvent.event_type == "speech.correction_requested",
                    MatchEvent.payload["request_id"].as_string() == request_id,
                )
        ) == 1
        assert db.scalar(
            select(func.count(MatchEvent.id)).where(
                MatchEvent.room_id == request.room_id,
                MatchEvent.event_type == "speech.corrected",
            )
        ) == 1
        audit = db.scalar(
            select(AdminAuditLog).where(
                AdminAuditLog.action == "speech_correction.approve",
                AdminAuditLog.target_id == request_id,
            )
        )
        assert audit and "proposed_content" not in json.dumps(audit.payload, ensure_ascii=False)

    result = owner.get(f"/api/rooms/{code}/result").json()
    owner_speech = next(item for item in result["speeches"] if item["id"] == speech_id)
    assert owner_speech["content"] == payload["proposed_content"]
    assert owner_speech["can_request_correction"] is True
    outsider_result = outsider.get(f"/api/rooms/{code}/result").json()
    assert next(item for item in outsider_result["speeches"] if item["id"] == speech_id)["can_request_correction"] is False
    history = owner.get(f"/api/matches/{match_id}/history").json()
    original_history = next(item for item in history["speeches"] if item["content"] == payload["proposed_content"])
    assert original_history["can_request_correction"] is True
    personal = owner.get("/api/me").json()
    assert any(item["match_id"] == match_id for item in personal["history"])
    participant_archive = owner.get(f"/api/matches/{match_id}/archive").json()
    assert len(participant_archive["data"]["speech_corrections"]) == 1
    participant_correction = next(
        item
        for item in participant_archive["data"]["speech_corrections"]
        if item["speech_id"] == speech_id
    )
    assert participant_correction["original_content"] == "原始误提交文字包含一个明显错误。"
    assert "requester_user_id" not in participant_correction and "reviewed_by_user_id" not in participant_correction

    post_approval_replay = owner.post(
        f"/api/rooms/{code}/speeches/{speech_id}/correction-requests",
        headers=request_headers,
        json=payload,
    )
    assert post_approval_replay.status_code == 200 and post_approval_replay.json()["replayed"] is True

    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        another = Speech(
            match_id=match_id,
            room_id=room.id,
            seat_key="aff_1",
            stage_key="owner_later_turn",
            speaker_type="human",
            status="completed",
            content="原作者在另一轮提交的独立发言。",
        )
        db.add(another)
        db.flush()
        owner_id = owner.get("/api/auth/session").json()["user"]["id"]
        append_event(
            db,
            room,
            "speech.completed",
            {"speech_id": another.id, "seat_key": "aff_1", "content": another.content},
            actor_user_id=owner_id,
        )
        db.commit()
        another_id = another.id
    same_header_other_speech = owner.post(
        f"/api/rooms/{code}/speeches/{another_id}/correction-requests",
        headers=request_headers,
        json={"proposed_content": "原作者另一轮发言的修正版本。", "reason": "另一轮独立纠错"},
    )
    assert same_header_other_speech.status_code == 200
    assert same_header_other_speech.json()["request"]["id"] != request_id


def test_correction_cancel_reject_and_terminal_boundary_are_recoverable(client, register_user) -> None:
    owner = register_user("round19_cancel_owner")
    code, _match_id, speech_id = _completed_human_match(owner, "Round19 撤销拒绝与终止边界")
    first = owner.post(
        f"/api/rooms/{code}/speeches/{speech_id}/correction-requests",
        headers=csrf(owner) | {"X-Idempotency-Key": "round19-cancel"},
        json={"proposed_content": "参赛者准备撤销的修正版本。", "reason": "先提交后再次核对"},
    )
    request_id = first.json()["request"]["id"]
    cancelled = owner.post(f"/api/rooms/{code}/speech-correction-requests/{request_id}/cancel", headers=csrf(owner), json={})
    replayed = owner.post(f"/api/rooms/{code}/speech-correction-requests/{request_id}/cancel", headers=csrf(owner), json={})
    assert cancelled.status_code == 200 and cancelled.json()["replayed"] is False
    assert replayed.status_code == 200 and replayed.json()["replayed"] is True

    second = owner.post(
        f"/api/rooms/{code}/speeches/{speech_id}/correction-requests",
        headers=csrf(owner) | {"X-Idempotency-Key": "round19-reject"},
        json={"proposed_content": "管理员最终未采纳的修正版本。", "reason": "参赛者认为转写不准确"},
    )
    second_id = second.json()["request"]["id"]
    admin = _admin(client)
    try:
        pending = next(item for item in admin.get("/api/admin/speech-corrections").json()["items"] if item["id"] == second_id)
        rejected = admin.post(
            f"/api/admin/speech-corrections/{second_id}/reject",
            headers=csrf(admin),
            json={"expected_updated_at": pending["updated_at"], "reason": "录音证据不支持该修正"},
        )
        assert rejected.status_code == 200
    finally:
        admin.__exit__(None, None, None)

    pending_before_termination = owner.post(
        f"/api/rooms/{code}/speeches/{speech_id}/correction-requests",
        headers=csrf(owner) | {"X-Idempotency-Key": "round19-pending-before-terminate"},
        json={"proposed_content": "终止前已经提交但尚未审批的版本。", "reason": "等待管理员确认"},
    )
    assert pending_before_termination.status_code == 200
    terminal_pending = pending_before_termination.json()["request"]
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.status = "terminated"
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        match.status = "terminated"
        db.commit()
    immutable = owner.post(
        f"/api/rooms/{code}/speeches/{speech_id}/correction-requests",
        headers=csrf(owner),
        json={"proposed_content": "终止后不应直接改写的版本。", "reason": "终止边界测试"},
    )
    assert immutable.status_code == 409 and "保持只读" in immutable.json()["detail"]
    admin = _admin(client)
    try:
        blocked_approval = admin.post(
            f"/api/admin/speech-corrections/{terminal_pending['id']}/approve",
            headers=csrf(admin),
            json={"expected_updated_at": terminal_pending["updated_at"], "reason": "终止后不应批准"},
        )
        assert blocked_approval.status_code == 409 and "保持只读" in blocked_approval.json()["detail"]
        closed = admin.post(
            f"/api/admin/speech-corrections/{terminal_pending['id']}/reject",
            headers=csrf(admin),
            json={"expected_updated_at": terminal_pending["updated_at"], "reason": "比赛已终止，关闭申请"},
        )
        assert closed.status_code == 200
    finally:
        admin.__exit__(None, None, None)
    with SessionLocal() as db:
        speech = db.get(Speech, speech_id)
        requests = list(db.scalars(select(SpeechCorrectionRequest).where(SpeechCorrectionRequest.speech_id == speech_id)).all())
        assert speech.content == "原始误提交文字包含一个明显错误。"
        assert [item.status for item in requests] == ["cancelled", "rejected", "rejected"]


def test_original_non_owner_participant_keeps_archive_access_after_seat_transfer(register_user) -> None:
    owner = register_user("round19_archive_owner")
    participant = register_user("round19_archive_participant")
    replacement = register_user("round19_archive_replacement")
    participant_id = participant.get("/api/auth/session").json()["user"]["id"]
    replacement_id = replacement.get("/api/auth/session").json()["user"]["id"]
    code = create_training_room(owner, "Round19 不可变参赛身份归档权限")["code"]
    assert participant.post(
        f"/api/rooms/{code}/claim-seat",
        headers=csrf(participant),
        json={"seat_key": "neg_1"},
    ).status_code == 200
    for browser in (owner, participant):
        assert browser.post(f"/api/rooms/{code}/ready", headers=csrf(browser), json={"ready": True}).status_code == 200
    assert owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={}).status_code == 200
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        speech = Speech(
            match_id=match.id,
            room_id=room.id,
            seat_key="neg_1",
            stage_key="neg_case",
            speaker_type="human",
            status="completed",
            content="原反方参赛者提交的有效发言。",
        )
        db.add(speech)
        db.flush()
        append_event(
            db,
            room,
            "speech.completed",
            {"speech_id": speech.id, "seat_key": "neg_1", "content": speech.content},
            actor_user_id=participant_id,
        )
        seat = next(item for item in room.seats if item.seat_key == "neg_1")
        seat.user_id = replacement_id
        seat.display_name = "后续席位接替者"
        room.status = "completed"
        room.completed_at = now()
        match.status = "completed"
        match.winner = "aff"
        append_event(db, room, "match.completed", {"winner": "aff"})
        db.commit()
        match_id = match.id
        speech_id = speech.id

    correction = participant.post(
        f"/api/rooms/{code}/speeches/{speech_id}/correction-requests",
        headers=csrf(participant),
        json={"proposed_content": "原反方参赛者修正后的有效发言。", "reason": "本人纠错隐私验证"},
    )
    assert correction.status_code == 200

    archive = participant.get(f"/api/matches/{match_id}/archive")
    assert archive.status_code == 200
    assert archive.headers["x-archive-projection"] == "participant"
    assert len(archive.json()["data"]["speech_corrections"]) == 1
    owner_archive = owner.get(f"/api/matches/{match_id}/archive")
    assert owner_archive.status_code == 200
    assert owner_archive.json()["data"]["speech_corrections"] == []
    assert any(item["match_id"] == match_id for item in participant.get("/api/me").json()["history"])
