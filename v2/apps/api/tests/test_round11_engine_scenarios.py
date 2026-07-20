from __future__ import annotations

import asyncio
import gc
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from app.api import realtime as realtime_api
from app.api import rooms as rooms_api
from app.core.database import SessionLocal
from app.models.entities import Match, MatchEvent, Speech
from app.services.match_archive import build_match_archive
from app.services.match_engine import match_engine
from app.services.providers import ProviderError, debate_agent, lighttts
from app.services.public_snapshot import public_snapshot_cache
from app.services.room_service import append_event, load_room, now
from app.services.voice_runtime.pipeline import IncrementalVoicePipeline
from conftest import csrf
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from test_platform import create_training_room


def _start_room(owner: TestClient, topic: str) -> str:
    code = create_training_room(owner, topic)["code"]
    assert owner.post(f"/api/rooms/{code}/ready", headers=csrf(owner), json={"ready": True}).status_code == 200
    assert owner.post(f"/api/rooms/{code}/start", headers=csrf(owner), json={}).status_code == 200
    return code


@pytest.mark.parametrize("terminal_status", ["completed", "review_required"])
def test_result_window_accepts_late_transcript_and_refreshes_archive(
    register_user,
    monkeypatch,
    terminal_status: str,
) -> None:
    """Completion/review retains a weak-network recovery path.

    A browser can finish uploading ASR text after the engine has already
    settled or sent the match to review. The locally retained transcript must
    not be discarded, but accepting it also has to refresh the archive that
    may already exist.
    """

    owner = register_user(f"round11_terminal_{terminal_status}")
    code = _start_room(owner, f"Round11 {terminal_status} 终局写保护")
    lease = f"round11-{terminal_status}-lease"

    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        seat = next(item for item in room.seats if item.seat_key == "aff_1")
        seat.control_lease = lease
        speech = Speech(
            match_id=match.id,
            room_id=room.id,
            seat_key=seat.seat_key,
            stage_key="late_human_turn",
            speaker_type="human",
            status="timed_out",
        )
        db.add(speech)
        db.flush()
        room.status = terminal_status
        room.completed_at = now()
        room.stage_deadline_at = None
        match.status = terminal_status
        append_event(db, room, f"match.{terminal_status}", {"round": 11})
        db.commit()
        match_id = match.id
        speech_id = speech.id

    first_archive = build_match_archive(match_id)
    enqueued: list[str] = []
    monkeypatch.setattr(rooms_api, "enqueue_match_archive", lambda target: enqueued.append(target) or True)
    finalized = owner.post(
        f"/api/rooms/{code}/speech/finish",
        headers=csrf(owner) | {"X-Control-Lease": lease},
        json={
            "speech_id": speech_id,
            "content": "这是一段在比赛结果产生以后才恢复的本地文字，需要保留并同步到正式历史。",
        },
    )
    assert finalized.status_code == 200
    assert finalized.json()["timed_out"] is True
    assert enqueued == [match_id]

    second_archive = build_match_archive(match_id)
    assert second_archive.reused is False
    assert second_archive.source_sha256 != first_archive.source_sha256
    with SessionLocal() as db:
        room = load_room(db, code)
        stored = db.get(Speech, speech_id)
        assert room.status == terminal_status
        assert stored and stored.status == "completed" and "比赛结果产生以后" in stored.content


def test_human_finish_racing_termination_has_one_terminal_order_and_no_active_speech(register_user) -> None:
    """A stale finish and emergency termination may arrive on different workers."""

    owner = register_user("round11_finish_terminate_race")
    code = _start_room(owner, "Round11 真人提交与紧急终止竞态")
    lease = "round11-racing-device"
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        room.status = "running"
        room.template_snapshot = [
            {"key": "human_case", "name": "正方真人发言", "kind": "speech", "seat": "aff_1", "duration": 90}
        ]
        room.current_stage_index = 0
        room.stage_started_at = now()
        room.stage_deadline_at = now() + timedelta(seconds=90)
        db.commit()

    lease_headers = csrf(owner) | {"X-Control-Lease": lease}
    assert owner.post(f"/api/rooms/{code}/control-lease", headers=lease_headers, json={}).status_code == 200
    started = owner.post(f"/api/rooms/{code}/speech/start", headers=lease_headers, json={})
    assert started.status_code == 200
    speech_id = started.json()["speech_id"]

    second_device = TestClient(owner.app)
    with second_device:
        second_device.cookies.update(owner.cookies)
        barrier = Barrier(2)

        def finish():
            barrier.wait()
            return owner.post(
                f"/api/rooms/{code}/speech/finish",
                headers=lease_headers,
                json={"speech_id": speech_id, "content": "真人在竞态发生前完成了一段有效且完整的比赛发言。"},
            )

        def terminate():
            barrier.wait()
            return second_device.post(
                f"/api/rooms/{code}/control/terminate",
                headers=csrf(second_device),
                json={"reason": "网络异常，房主紧急终止"},
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            finished, terminated = list(pool.map(lambda operation: operation(), [finish, terminate]))

    assert terminated.status_code == 200
    assert finished.status_code in {200, 409}
    with SessionLocal() as db:
        room = load_room(db, code)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        speech = db.get(Speech, speech_id)
        events = list(
            db.scalars(select(MatchEvent).where(MatchEvent.room_id == room.id).order_by(MatchEvent.seq)).all()
        )
        terminate_event = next(item for item in events if item.event_type == "control.terminate")
        later_mutations = [
            item
            for item in events
            if item.seq > terminate_event.seq and item.event_type in {"speech.completed", "speech.late_finalized", "stage.started"}
        ]
        assert room.status == match.status == "terminated"
        assert speech and speech.status in {"completed", "interrupted"}
        assert later_mutations == []
        assert (
            db.scalar(
                select(func.count(Speech.id)).where(
                    Speech.room_id == room.id,
                    Speech.status.in_(["speaking", "synthesizing", "playing"]),
                )
            )
            == 0
        )


def test_late_human_audio_refreshes_already_final_match_archive(register_user, monkeypatch) -> None:
    """The transcript and its recording are separate browser requests."""

    owner = register_user("round11_final_audio_archive")
    code = _start_room(owner, "Round11 终局录音与归档一致性")
    with SessionLocal() as db:
        room = load_room(db, code, lock=True)
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        speech = Speech(
            match_id=match.id,
            room_id=room.id,
            seat_key="aff_1",
            stage_key="human_final",
            speaker_type="human",
            content="这段真人发言已经提交，浏览器仍在异步整理录音文件。",
            status="completed",
        )
        db.add(speech)
        db.flush()
        room.status = "completed"
        room.completed_at = now()
        room.stage_deadline_at = None
        match.status = "completed"
        match.winner = "aff"
        append_event(db, room, "match.completed", {"winner": "aff"})
        db.commit()
        match_id = match.id
        speech_id = speech.id

    first_archive = build_match_archive(match_id)
    enqueued: list[str] = []

    async def accepted_audio(_path, _suffix: str, _size: int) -> float:
        return 2.5

    monkeypatch.setattr(rooms_api, "_audio_suffix", lambda _header: ".wav")
    monkeypatch.setattr(rooms_api, "_validate_uploaded_audio_off_loop", accepted_audio)
    monkeypatch.setattr(rooms_api, "enqueue_match_archive", lambda target: enqueued.append(target) or True)
    uploaded = owner.post(
        f"/api/rooms/{code}/speech/{speech_id}/audio",
        headers=csrf(owner),
        files={"audio": ("late.wav", b"RIFF-late-human-audio", "audio/wav")},
    )
    assert uploaded.status_code == 200, uploaded.text
    assert enqueued == [match_id]

    second_archive = build_match_archive(match_id)
    assert second_archive.reused is False
    assert second_archive.source_sha256 != first_archive.source_sha256
    with SessionLocal() as db:
        speech = db.get(Speech, speech_id)
        assert speech and speech.audio_url.endswith(f"/{speech_id}.wav")
        assert speech.duration_seconds == 2.5


@pytest.mark.asyncio
async def test_late_agent_result_after_manual_skip_cannot_overwrite_next_stage_or_other_room(
    register_user,
    monkeypatch,
) -> None:
    """A skipped Agent call must be stale even if its HTTP result arrives later."""

    skipped_owner = register_user("round11_late_agent_skipped")
    healthy_owner = register_user("round11_late_agent_healthy")
    skipped_code = _start_room(skipped_owner, "Round11 迟到 Agent 跳过房间")
    healthy_code = _start_room(healthy_owner, "Round11 并发正常 Agent 房间")
    for code in (skipped_code, healthy_code):
        with SessionLocal() as db:
            room = load_room(db, code, lock=True)
            room.status = "running"
            room.template_snapshot = [
                {"key": "ai_case", "name": "反方 AI 立论", "kind": "speech", "seat": "neg_1", "duration": 90},
                {"key": "human_next", "name": "正方真人回应", "kind": "speech", "seat": "aff_1", "duration": 90},
            ]
            room.current_stage_index = 0
            room.stage_started_at = now()
            room.stage_deadline_at = now() + timedelta(seconds=90)
            db.commit()

    skipped_started = asyncio.Event()
    release_skipped = asyncio.Event()

    async def generated(payload, **_kwargs):
        if payload["room_code"] == skipped_code:
            skipped_started.set()
            await release_skipped.wait()
            return "这段迟到的 Agent 内容属于已经被跳过的旧阶段，不得写入下一阶段。"
        return "并发正常房间的 Agent 内容必须只写入自己的比赛记录。"

    async def no_audio(*_args, room_code: str, speech_id: str, **_kwargs):
        return f"/media/mock/{room_code}/{speech_id}.wav"

    monkeypatch.setattr(debate_agent, "generate", generated)
    monkeypatch.setattr(lighttts, "synthesize", no_audio)

    skipped_task = asyncio.create_task(match_engine.process_room(skipped_code))
    healthy_task = asyncio.create_task(match_engine.process_room(healthy_code))
    await asyncio.wait_for(skipped_started.wait(), timeout=1)
    skipped = skipped_owner.post(
        f"/api/rooms/{skipped_code}/control/skip",
        headers=csrf(skipped_owner),
        json={"reason": "Agent 长时间无响应，跳过旧阶段"},
    )
    assert skipped.status_code == 200
    release_skipped.set()
    await asyncio.wait_for(asyncio.gather(skipped_task, healthy_task), timeout=2)

    with SessionLocal() as db:
        skipped_room = load_room(db, skipped_code)
        healthy_room = load_room(db, healthy_code)
        skipped_speeches = list(db.scalars(select(Speech).where(Speech.room_id == skipped_room.id)).all())
        healthy_speeches = list(db.scalars(select(Speech).where(Speech.room_id == healthy_room.id)).all())
        assert skipped_room.status == "running" and skipped_room.current_stage_index == 1
        assert len(skipped_speeches) == 1
        assert skipped_speeches[0].status == "interrupted" and skipped_speeches[0].content == ""
        assert healthy_room.status == "running" and healthy_room.current_stage_index == 1
        assert len(healthy_speeches) == 1
        assert healthy_speeches[0].status == "completed"
        assert "并发正常房间" in healthy_speeches[0].content
        assert all(skipped_code not in speech.content for speech in healthy_speeches)


@pytest.mark.asyncio
@pytest.mark.xfail(
    reason=(
        "冻结的 realtime voice pipeline 在 anext 已失败且父任务同拍取消时没有回收已完成 task 的异常；"
        "保留为生产旧日志的可复现风险，需在独立音频变更窗口修复"
    ),
    strict=False,
)
async def test_frozen_realtime_pipeline_retrieves_agent_error_when_cancel_races_completed_anext() -> None:
    """Reproduce the historical async_generator_asend warning without gating release."""

    class Session:
        async def push_text(self, _text: str) -> None:
            return None

        async def finish(self) -> str:
            return ""

        async def abort(self, reason: str = "") -> None:
            del reason

    loop = asyncio.get_running_loop()
    contexts: list[dict] = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: contexts.append(context))
    holder: dict[str, asyncio.Task] = {}

    async def failed_agent_events():
        current = asyncio.current_task()
        assert current is not None
        # asyncio.wait has already registered the pipeline wakeup. Queueing a
        # parent cancellation behind it makes anext complete with ProviderError
        # just before the parent receives CancelledError.
        current.add_done_callback(lambda _completed: holder["pipeline"].cancel())
        raise ProviderError("辩手 Agent 调用失败", code="agent_error", retryable=True)
        yield  # pragma: no cover - keeps this function an async generator

    try:
        holder["pipeline"] = asyncio.create_task(
            IncrementalVoicePipeline().run(failed_agent_events(), Session())
        )
        with pytest.raises(asyncio.CancelledError):
            await holder["pipeline"]
        await asyncio.sleep(0)
        gc.collect()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_handler)

    assert not [context for context in contexts if context.get("message") == "Task exception was never retrieved"]


@pytest.mark.asyncio
async def test_hidden_lobby_websocket_closes_after_accept_without_raising_http_response(register_user) -> None:
    """Exercise the exact accepted-socket privacy denial path from old API logs."""

    public_snapshot_cache.clear()
    owner = register_user("round11_hidden_websocket")
    code = create_training_room(owner, "Round11 隐藏大厅 WebSocket 拒绝路径")["code"]

    class AcceptedThenDeniedSocket:
        headers = {"origin": "http://localhost:3200"}
        cookies: dict[str, str] = {}

        def __init__(self) -> None:
            self.accepted = False
            self.close_codes: list[int] = []

        async def accept(self) -> None:
            self.accepted = True

        async def close(self, *, code: int = 1000) -> None:
            assert self.accepted, "the room websocket currently validates hidden public state after accepting"
            self.close_codes.append(code)

    socket = AcceptedThenDeniedSocket()
    await realtime_api.room_websocket(socket, code)
    assert socket.accepted is True
    assert socket.close_codes == [4401]
