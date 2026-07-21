from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import math
import shutil
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.config import settings
from app.core.database import SessionLocal, TransactionLockTimeout
from app.models.entities import (
    AgentProfile,
    AudioAsset,
    AudioCue,
    FreeTurnRequest,
    JudgeScorecard,
    LeaderboardEntry,
    Match,
    RatingChange,
    Room,
    RoomSeat,
    Speech,
    User,
)
from app.services.agent_decision import decide_should_speak, interrupt_agent_task, wait_while_current
from app.services.captions import StreamingCaptionWriter
from app.services.free_turn_queue import (
    begin_intermission,
    expire_room_requests,
    intermission_deadline,
    resolve_intermission,
)
from app.services.match_archive import enqueue_match_archive
from app.services.provider_config import runtime_provider_config
from app.services.providers import (
    ProviderCancelled,
    ProviderError,
    agent_payload,
    debate_agent,
    judge_provider,
    lighttts,
    moss_tts_realtime,
    realtime_tts,
)
from app.services.realtime import audio_stream_aborts, room_hub
from app.services.room_service import (
    append_event,
    free_turn_remaining_seconds,
    load_room,
    now,
    remaining_seconds,
    seat_label,
    stage,
    transfer_room_owner,
)
from app.services.seat_restore import expire_pending_restore_requests
from app.services.speech_quality import usable_transcript
from app.services.voice_runtime.archive import wav_duration
from app.services.voice_runtime.lighttts import lighttts as realtime_voice_runtime
from app.services.voice_runtime.livekit import livekit_audio_enabled, livekit_audio_registry
from app.services.voice_runtime.pipeline import (
    IncrementalVoicePipeline,
    RealtimeVoiceError,
    RealtimeVoiceSynthesisError,
)
from app.services.voice_runtime.voices import voice_for_seat
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentTextPrefetch:
    room_code: str
    stage_index: int
    stage_key: str
    seat_key: str
    fingerprint: str
    task_id: str
    payload: dict[str, Any]
    provider_config: dict[str, Any]
    content: str = ""


@dataclass(frozen=True)
class FreeAgentSpeculation:
    room_code: str
    stage_key: str
    turn_seq: int
    seat_key: str
    decision_payload: dict[str, Any]
    candidate_payload: dict[str, Any]
    provider_config: dict[str, Any]
    task: asyncio.Task | None


def _remove_generated_audio(room_code: str, asset_id: str) -> None:
    if not asset_id:
        return
    target_dir = settings.media_path / room_code
    (target_dir / f"{asset_id}.wav").unlink(missing_ok=True)
    for temporary in target_dir.glob(f".{asset_id}.*.wav.part"):
        if temporary.is_file() and not temporary.is_symlink():
            temporary.unlink(missing_ok=True)


def recover_inflight_engine_tasks() -> dict[str, int]:
    """Interrupt provider attempts whose owning engine process no longer exists."""
    with SessionLocal() as db:
        speech_codes = set(
            db.scalars(
                select(Room.code)
                .join(Speech, Speech.room_id == Room.id)
                .where(
                    Room.status.in_(["running", "judging"]),
                    Speech.speaker_type == "ai",
                    Speech.status.in_(["speaking", "synthesizing"]),
                )
            ).all()
        )
        judge_codes = set(
            db.scalars(
                select(Room.code)
                .join(Match, Match.room_id == Room.id)
                .join(JudgeScorecard, JudgeScorecard.match_id == Match.id)
                .where(Room.status.in_(["running", "judging"]), JudgeScorecard.status == "running")
            ).all()
        )
    recovered_speeches = 0
    recovered_judges = 0
    for code in sorted(speech_codes | judge_codes):
        remove_ids: list[str] = []
        with SessionLocal() as db:
            room = load_room(db, code, lock=True)
            if room.status not in {"running", "judging"}:
                db.rollback()
                continue
            speeches = list(
                db.scalars(
                    select(Speech).where(
                        Speech.room_id == room.id,
                        Speech.speaker_type == "ai",
                        Speech.status.in_(["speaking", "synthesizing"]),
                    )
                ).all()
            )
            for speech in speeches:
                stream_generation = speech.stream_generation
                speech.status = "interrupted"
                speech.stream_generation = ""
                speech.stream_sample_rate = 0
                remove_ids.append(speech.id)
                if stream_generation:
                    append_event(
                        db,
                        room,
                        "audio.stream.aborted",
                        {
                            "speech_id": speech.id,
                            "generation": stream_generation,
                            "reason": "engine_restart",
                        },
                    )
                append_event(
                    db,
                    room,
                    "speech.interrupted",
                    {"speech_id": speech.id, "seat_key": speech.seat_key, "reason": "engine_restart"},
                )
                recovered_speeches += 1
            match = db.scalar(select(Match).where(Match.room_id == room.id))
            scorecard = (
                db.scalar(select(JudgeScorecard).where(JudgeScorecard.match_id == match.id, JudgeScorecard.status == "running"))
                if match
                else None
            )
            if scorecard:
                interrupted_task_id = scorecard.task_id
                scorecard.status = "interrupted"
                scorecard.task_id = ""
                scorecard.reasoning = "比赛引擎重启，旧裁判任务已失效。"
                append_event(
                    db,
                    room,
                    "judge.interrupted",
                    {
                        "scorecard_id": scorecard.id,
                        "task_id": interrupted_task_id,
                        "reason": "engine_restart",
                    },
                )
                recovered_judges += 1
            db.commit()
        for asset_id in remove_ids:
            _remove_generated_audio(code, asset_id)
    return {"speeches": recovered_speeches, "judges": recovered_judges}


class MatchEngine:
    # Paused rooms still need low-cost lifecycle processing.  In particular,
    # presence expiry must be able to replace a human who stays offline past
    # the grace period and transfer repair controls to another participant.
    # Excluding ``paused`` left those rooms permanently dependent on a manual
    # API call even though Redis had already persisted the disconnect.
    _ACTIVE_ROOM_STATUSES = {"lobby", "preparing", "running", "paused", "judging"}
    _UNEXPECTED_FAILURE_LIMIT = 3
    _MAX_RETRY_SECONDS = 30.0
    _LIGHTTTS_ADMISSION_RETRY_DELAYS = (2.0, 5.0, 10.0)
    _LIGHTTTS_ADMISSION_RETRY_CODES = frozenset(
        {"lighttts_queue_full", "lighttts_queue_timeout", "lighttts_admission_unavailable"}
    )
    _LIGHTTTS_RETRY_POLL_SECONDS = 0.5

    @staticmethod
    async def _validated_agent_stream(
        events: AsyncIterator[dict[str, Any]],
    ) -> AsyncIterator[dict[str, str]]:
        """Release only confirmed Agent body text to the realtime pipeline.

        A provider may stream reasoning or punctuation-only placeholders before
        its answer body. Buffer that prefix until it contains a minimally
        substantive transcript, then continue streaming without waiting for
        the full response. This gate intentionally lives before any synthesis
        session receives text.
        """
        buffered = ""
        streamed = ""
        released = False
        async for event in events:
            event_type = str(event.get("type") or "").strip().lower()
            event_channel = str(event.get("channel") or event.get("phase") or "").strip().lower()
            if event_type in {"analysis", "thinking", "reasoning"} or event_channel in {
                "analysis",
                "thinking",
                "reasoning",
            }:
                continue
            if event_type == "delta":
                delta = event.get("delta")
                if not isinstance(delta, str) or not delta:
                    continue
                streamed += delta
                if released:
                    yield {"type": "delta", "delta": delta}
                    continue
                buffered += delta
                if usable_transcript(buffered, require_substantive=True):
                    released = True
                    yield {"type": "delta", "delta": buffered}
                    buffered = ""
                continue
            if event_type != "final":
                continue
            final_value = event.get("content")
            final_text = final_value.strip() if isinstance(final_value, str) else ""
            candidate = final_text or streamed.strip()
            if not usable_transcript(candidate, require_substantive=True):
                raise ProviderError(
                    "辩手 Agent 返回内容无效，请重试当前阶段。",
                    code="agent_invalid_output",
                    retryable=True,
                )
            if not released:
                released = True
                yield {"type": "delta", "delta": candidate}
            yield {"type": "final", "content": candidate}
            return

        candidate = streamed.strip()
        if not usable_transcript(candidate, require_substantive=True):
            raise ProviderError(
                "辩手 Agent 返回内容无效，请重试当前阶段。",
                code="agent_invalid_output",
                retryable=True,
            )
        if not released:
            yield {"type": "delta", "delta": candidate}
        yield {"type": "final", "content": candidate}

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._room_locks: dict[str, asyncio.Lock] = {}
        self._room_tasks: dict[str, asyncio.Task] = {}
        self._room_failures: dict[str, int] = {}
        self._room_transient_failures: dict[str, int] = {}
        self._room_retry_at: dict[str, float] = {}
        self._provider_slots: asyncio.Semaphore | None = None
        self._runtime_loop: asyncio.AbstractEventLoop | None = None
        # Speculative Agent text is deliberately process-local. It is never an
        # authoritative match record and never reserves TTS/LiveKit capacity.
        # A restart simply loses this cache and regenerates the same idempotent
        # upstream task when the room becomes eligible again.
        self._agent_prefetch_tasks: dict[str, asyncio.Task] = {}
        self._agent_prefetch_cache: dict[str, AgentTextPrefetch] = {}
        self._agent_prefetch_attempted: dict[str, str] = {}
        self._free_agent_speculations: dict[str, FreeAgentSpeculation] = {}

    def start(self) -> None:
        if not settings.engine_enabled or self._task:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self.run(), name="jixia-match-engine")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self.drain_room_tasks(cancel=True)
        prefetch_tasks = list(self._agent_prefetch_tasks.values())
        for task in prefetch_tasks:
            task.cancel()
        if prefetch_tasks:
            await asyncio.gather(*prefetch_tasks, return_exceptions=True)
        self._agent_prefetch_tasks.clear()
        self._agent_prefetch_cache.clear()
        self._agent_prefetch_attempted.clear()
        free_tasks = [item.task for item in self._free_agent_speculations.values() if item.task]
        for task in free_tasks:
            task.cancel()
        if free_tasks:
            await asyncio.gather(*free_tasks, return_exceptions=True)
        self._free_agent_speculations.clear()
        await livekit_audio_registry.close()
        self._room_locks.clear()
        self._room_failures.clear()
        self._room_transient_failures.clear()
        self._room_retry_at.clear()
        self._provider_slots = None
        self._runtime_loop = None

    async def drain_room_tasks(self, *, cancel: bool = False) -> None:
        """Wait for the currently registered per-room jobs to leave the engine.

        A room job owns asyncio locks and provider capacity that belong to the
        event loop on which it was created.  Silently replacing the registry
        from another loop would orphan that work: it could still write a cue,
        speech, or judge result while the new scheduler believed the room was
        idle.  Shutdown and test harnesses must therefore drain on the owning
        loop instead of clearing the dictionaries.
        """
        current_loop = asyncio.get_running_loop()
        tasks = list(self._room_tasks.items())
        foreign_pending = [code for code, task in tasks if not task.done() and task.get_loop() is not current_loop]
        if foreign_pending:
            raise RuntimeError(
                "match engine room tasks must be drained on their owning event loop: "
                + ", ".join(sorted(foreign_pending))
            )
        owned_tasks = [task for _code, task in tasks if task.get_loop() is current_loop]
        if cancel:
            for task in owned_tasks:
                if not task.done():
                    task.cancel()
        if owned_tasks:
            await asyncio.gather(*owned_tasks, return_exceptions=True)
        for code, task in tasks:
            if task.done() and self._room_tasks.get(code) is task:
                self._room_tasks.pop(code, None)

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception:
                # 单个 tick 的异常不得终止整个引擎，但必须留下可诊断日志。
                logger.exception("match engine tick failed")
            await asyncio.sleep(settings.engine_poll_seconds)

    async def tick(self) -> None:
        self._ensure_runtime()
        await self._reap_expired_presence()
        with SessionLocal() as db:
            codes = list(db.scalars(select(Room.code).where(Room.status.in_(self._ACTIVE_ROOM_STATUSES))).all())
            # Prewarm before the automated flow and keep one stable publisher
            # throughout an actively running match. Lobby, paused, and judging
            # rooms do not emit TTS; keeping their 20 ms silence clocks alive
            # wastes scheduler time and can starve the real room publisher.
            media_codes = set(
                db.scalars(select(Room.code).where(Room.status.in_(["preparing", "running"]))).all()
            )
        await self._reconcile_livekit_publishers(media_codes)
        active_codes = set(codes)
        for stale_code, stale_lock in list(self._room_locks.items()):
            if stale_code not in active_codes and not stale_lock.locked():
                self._room_locks.pop(stale_code, None)
        for stale_code in (set(self._room_failures) | set(self._room_transient_failures) | set(self._room_retry_at)) - active_codes:
            self._clear_room_failure(stale_code)
        for stale_code in (
            set(self._agent_prefetch_tasks)
            | set(self._agent_prefetch_cache)
            | set(self._agent_prefetch_attempted)
        ) - active_codes:
            self._invalidate_agent_prefetch(stale_code)
        current_time = time.monotonic()
        for code in codes:
            if current_time < self._room_retry_at.get(code, 0.0):
                continue
            existing = self._room_tasks.get(code)
            if existing and not existing.done():
                continue
            task = asyncio.create_task(self._process_room_locked(code), name=f"jixia-room-{code}")
            self._room_tasks[code] = task
            task.add_done_callback(lambda completed, room_code=code: self._room_task_finished(room_code, completed))

    async def _reap_expired_presence(self) -> None:
        """Persist Redis lease expiry exactly once across engine processes.

        Redis atomically assigns each expired identity to one reaper. We then
        recheck the lease while holding the room row lock so a reconnect racing
        the cleanup cannot be overwritten with a stale offline state.
        """
        expired = await room_hub.reap_expired_presence()
        for code, seat_key, user_id in expired:
            try:
                with SessionLocal() as db:
                    room = load_room(db, code, lock=True)
                    if await room_hub.presence_active(code, seat_key, user_id) is not False:
                        db.rollback()
                        continue
                    seat = db.scalar(
                        select(RoomSeat).where(
                            RoomSeat.room_id == room.id,
                            RoomSeat.seat_key == seat_key,
                            RoomSeat.user_id == user_id,
                            RoomSeat.occupant_type == "human",
                        )
                    )
                    if not seat or not seat.connected:
                        db.rollback()
                        continue
                    seat.connected = False
                    seat.disconnected_at = now()
                    active_presence = room.status in {"lobby", "preparing", "running", "paused", "judging"}
                    if active_presence:
                        append_event(
                            db,
                            room,
                            "presence.disconnected",
                            {"seat_key": seat_key, "reason": "lease_expired"},
                            actor_user_id=user_id,
                        )
                    db.commit()
                    if active_presence:
                        await room_hub.publish(
                            code,
                            {"type": "presence.disconnected", "room_code": code, "seq": room.seq},
                        )
            except HTTPException as exc:
                if exc.status_code != 404:
                    logger.warning("presence lease cleanup failed: room=%s status=%s", code, exc.status_code)
            except (OperationalError, TransactionLockTimeout):
                # The identity has already been removed from the expiry index.
                # Reinsert a short tombstone so a later tick can safely retry.
                logger.warning("database busy during presence lease cleanup: room=%s seat=%s", code, seat_key)
                await room_hub.retry_expired_presence(code, seat_key, user_id)
            except Exception:
                logger.exception("presence lease cleanup failed: room=%s seat=%s", code, seat_key)
                await room_hub.retry_expired_presence(code, seat_key, user_id)

    async def _reconcile_livekit_publishers(self, media_room_codes: set[str]) -> None:
        """Keep publishers in the Engine process and retire terminal rooms."""

        if not livekit_audio_enabled():
            await livekit_audio_registry.close_inactive_rooms(set())
            return
        ordered_codes = sorted(media_room_codes)
        results = await asyncio.gather(
            *(livekit_audio_registry.ensure_room(code) for code in ordered_codes),
            return_exceptions=True,
        )
        for room_code, result in zip(ordered_codes, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning("LiveKit room publisher prewarm failed room=%s error=%s", room_code, result)
        await livekit_audio_registry.close_inactive_rooms(media_room_codes)

    def _room_task_finished(self, code: str, task: asyncio.Task) -> None:
        if self._room_tasks.get(code) is task:
            self._room_tasks.pop(code, None)
        if task.cancelled():
            return
        exception = task.exception()
        if exception:
            logger.error("room processing failed: %s", code, exc_info=(type(exception), exception, exception.__traceback__))

    def _ensure_runtime(self) -> None:
        loop = asyncio.get_running_loop()
        if self._runtime_loop is loop:
            return
        pending_codes = [code for code, task in self._room_tasks.items() if not task.done()]
        pending_prefetch_codes = [
            code for code, task in self._agent_prefetch_tasks.items() if not task.done()
        ]
        pending_codes.extend(pending_prefetch_codes)
        if pending_codes:
            raise RuntimeError(
                "match engine event loop changed while room tasks were active; "
                "drain or stop the engine on its owning loop first: "
                + ", ".join(sorted(pending_codes))
            )
        self._runtime_loop = loop
        self._room_locks = {}
        self._room_tasks = {}
        self._room_failures = {}
        self._room_transient_failures = {}
        self._room_retry_at = {}
        self._agent_prefetch_tasks = {}
        self._agent_prefetch_cache = {}
        self._agent_prefetch_attempted = {}
        self._provider_slots = asyncio.Semaphore(settings.engine_max_concurrent_rooms)

    def _provider_semaphore(self) -> asyncio.Semaphore:
        self._ensure_runtime()
        if self._provider_slots is None:
            raise RuntimeError("match engine provider scheduler is not initialized")
        return self._provider_slots

    @staticmethod
    async def _callback_requested(callback: Callable[[], bool | Awaitable[bool]]) -> bool:
        result = callback()
        return bool(await result) if inspect.isawaitable(result) else bool(result)

    async def _wait_for_lighttts_retry(
        self,
        delay_seconds: float,
        should_cancel: Callable[[], bool | Awaitable[bool]],
        *,
        deadline_monotonic: float,
    ) -> None:
        loop = asyncio.get_running_loop()
        retry_deadline = min(loop.time() + max(0.0, delay_seconds), deadline_monotonic)
        while True:
            if await self._callback_requested(should_cancel):
                raise ProviderCancelled("LightTTS 过载重试已取消。")
            current_time = loop.time()
            if current_time >= deadline_monotonic:
                raise ProviderError(
                    "LightTTS 全任务超时，未发布音频。",
                    code="lighttts_job_timeout",
                )
            remaining = retry_deadline - current_time
            if remaining <= 0:
                return
            await asyncio.sleep(min(self._LIGHTTTS_RETRY_POLL_SECONDS, remaining))

    async def _synthesize_with_admission_retry(
        self,
        synthesize: Callable[[], Awaitable[str]],
        *,
        should_cancel: Callable[[], bool | Awaitable[bool]],
        on_retry: Callable[[ProviderError, int, float], Awaitable[None]],
        deadline_monotonic: float,
    ) -> str:
        loop = asyncio.get_running_loop()
        for attempt in range(len(self._LIGHTTTS_ADMISSION_RETRY_DELAYS) + 1):
            if loop.time() >= deadline_monotonic:
                raise ProviderError(
                    "LightTTS 全任务超时，未发布音频。",
                    code="lighttts_job_timeout",
                )
            try:
                return await synthesize()
            except ProviderError as exc:
                exhausted = attempt >= len(self._LIGHTTTS_ADMISSION_RETRY_DELAYS)
                if not exc.retryable or exc.code not in self._LIGHTTTS_ADMISSION_RETRY_CODES or exhausted:
                    raise
                configured_delay = self._LIGHTTTS_ADMISSION_RETRY_DELAYS[attempt]
                remaining_budget = deadline_monotonic - loop.time()
                if remaining_budget <= 0:
                    raise ProviderError(
                        "LightTTS 全任务超时，未发布音频。",
                        code="lighttts_job_timeout",
                    ) from exc
                delay = min(max(configured_delay, exc.retry_after_seconds or 0.0), remaining_budget)
                retry_number = attempt + 1
                await on_retry(exc, retry_number, delay)
                await self._wait_for_lighttts_retry(
                    delay,
                    should_cancel,
                    deadline_monotonic=deadline_monotonic,
                )
        raise RuntimeError("unreachable LightTTS retry state")

    async def _process_room_locked(self, code: str) -> None:
        self._ensure_runtime()
        lock = self._room_locks.setdefault(code, asyncio.Lock())
        if lock.locked():
            return
        room_missing = False
        failure: Exception | None = None
        async with lock:
            try:
                await self.process_room(code)
            except HTTPException as exc:
                if exc.status_code == 404:
                    room_missing = True
                else:
                    failure = exc
            except Exception as exc:
                failure = exc
        if room_missing:
            if self._room_locks.get(code) is lock:
                self._room_locks.pop(code, None)
            self._clear_room_failure(code)
            return
        if failure:
            await self._record_room_failure(code, failure)
            return
        self._clear_room_failure(code)
        try:
            with SessionLocal() as db:
                status = db.scalar(select(Room.status).where(Room.code == code))
        except (OperationalError, TransactionLockTimeout) as exc:
            await self._record_room_failure(code, exc)
            return
        if status is None or status not in self._ACTIVE_ROOM_STATUSES:
            if self._room_locks.get(code) is lock:
                self._room_locks.pop(code, None)

    def _clear_room_failure(self, code: str) -> None:
        self._room_failures.pop(code, None)
        self._room_transient_failures.pop(code, None)
        self._room_retry_at.pop(code, None)

    def _invalidate_agent_prefetch(self, room_code: str) -> None:
        self._agent_prefetch_cache.pop(room_code, None)
        self._agent_prefetch_attempted.pop(room_code, None)
        task = self._agent_prefetch_tasks.pop(room_code, None)
        if task and not task.done():
            task.cancel()

    def _preempt_agent_prefetch_tasks(self) -> None:
        """Formal speech always has priority over speculative fixed-turn work."""
        for room_code, task in list(self._agent_prefetch_tasks.items()):
            self._agent_prefetch_tasks.pop(room_code, None)
            self._agent_prefetch_attempted.pop(room_code, None)
            if not task.done():
                task.cancel()

    def _agent_prefetch_descriptor(
        self, db: Session, room: Room, stage_index: int
    ) -> AgentTextPrefetch | None:
        """Build the complete validity boundary for one speculative response."""
        if room.status != "running" or stage_index < 0 or stage_index >= len(room.template_snapshot):
            return None
        if room.current_stage_index not in {stage_index - 1, stage_index}:
            return None
        target = room.template_snapshot[stage_index]
        if target.get("kind") != "speech":
            return None
        seat = next((item for item in room.seats if item.seat_key == target.get("seat")), None)
        # An AI substitute still belongs to a recoverable human seat. Avoid
        # racing a legitimate restore; only permanent AI seats are prefetched.
        if not seat or seat.occupant_type != "ai":
            return None
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        if not match:
            return None
        profile = db.get(AgentProfile, seat.agent_profile_id) if seat.agent_profile_id else None
        history = self._history_for_agent_prefetch(db, room, stage_index)
        next_name = (
            room.template_snapshot[stage_index + 1]["name"]
            if stage_index + 1 < len(room.template_snapshot)
            else "比赛结束"
        )
        duration = max(1, int(target.get("duration", 120)))
        max_token = min(1_000, max(160, int(duration * 2.1)))
        agent_config = runtime_provider_config(match.service_snapshot, "agent")
        fingerprint_input = {
            "room_id": room.id,
            "match_id": match.id,
            "topic": room.topic,
            "stage_index": stage_index,
            "stage": target,
            "seat": {
                "seat_key": seat.seat_key,
                "occupant_type": seat.occupant_type,
                "display_name": seat.display_name,
                "agent_profile_id": seat.agent_profile_id,
            },
            "profile": {
                "profile_key": profile.profile_key if profile else None,
                "model_name": profile.model_name if profile else None,
                "updated_at": profile.updated_at.isoformat() if profile else None,
            },
            "history": history,
            # Credential material only contributes to this local digest; it is
            # never copied into logs, match events, or browser projections.
            "agent_config": agent_config,
        }
        encoded = json.dumps(fingerprint_input, ensure_ascii=False, sort_keys=True, default=str).encode()
        fingerprint = hashlib.sha256(encoded).hexdigest()
        task_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"phdebate:agent-prefetch:{fingerprint}"))
        payload = agent_payload(
            topic=room.topic,
            debater_name=seat.display_name,
            seat_key=seat.seat_key,
            current_stage=str(target["name"]),
            next_stage=str(next_name),
            history=history,
            max_token=max_token,
            model_name=profile.model_name if profile else None,
            match_id=match.id,
            room_code=room.code,
            task_id=task_id,
            agent_profile=profile.profile_key if profile else None,
        )
        return AgentTextPrefetch(
            room_code=room.code,
            stage_index=stage_index,
            stage_key=str(target["key"]),
            seat_key=seat.seat_key,
            fingerprint=fingerprint,
            task_id=task_id,
            payload=payload,
            provider_config=agent_config,
        )

    def _schedule_next_agent_prefetch(self, room_code: str, source_stage_index: int) -> None:
        """Start text-only work for the immediately following fixed stage."""
        existing = self._agent_prefetch_tasks.get(room_code)
        if existing and not existing.done():
            return
        with SessionLocal() as db:
            room = load_room(db, room_code)
            if room.current_stage_index != source_stage_index:
                self._invalidate_agent_prefetch(room_code)
                return
            descriptor = self._agent_prefetch_descriptor(db, room, source_stage_index + 1)
        if not descriptor:
            self._invalidate_agent_prefetch(room_code)
            return
        cached = self._agent_prefetch_cache.get(room_code)
        if cached and cached.fingerprint == descriptor.fingerprint and cached.content:
            return
        if self._agent_prefetch_attempted.get(room_code) == descriptor.fingerprint:
            return
        if cached and cached.fingerprint != descriptor.fingerprint:
            self._agent_prefetch_cache.pop(room_code, None)
        self._agent_prefetch_attempted[room_code] = descriptor.fingerprint
        task = asyncio.create_task(
            self._run_agent_prefetch(descriptor),
            name=f"jixia-agent-prefetch-{room_code}-{descriptor.stage_key}",
        )
        self._agent_prefetch_tasks[room_code] = task

        def finished(completed: asyncio.Task, code: str = room_code) -> None:
            if self._agent_prefetch_tasks.get(code) is completed:
                self._agent_prefetch_tasks.pop(code, None)
            if completed.cancelled():
                return
            exception = completed.exception()
            if exception:
                logger.warning("Agent text prefetch failed room=%s error_type=%s", code, type(exception).__name__)

        task.add_done_callback(finished)

    async def _run_agent_prefetch(self, descriptor: AgentTextPrefetch) -> None:
        try:
            async with self._provider_semaphore():
                content = await debate_agent.generate(
                    descriptor.payload, provider_config=descriptor.provider_config
                )
            if not isinstance(content, str) or not usable_transcript(content, require_substantive=True):
                return
            with SessionLocal() as db:
                room = load_room(db, descriptor.room_code)
                expected = self._agent_prefetch_descriptor(db, room, descriptor.stage_index)
                if not expected or expected.fingerprint != descriptor.fingerprint:
                    return
            self._agent_prefetch_cache[descriptor.room_code] = AgentTextPrefetch(
                **{**descriptor.__dict__, "content": content.strip()}
            )
        except asyncio.CancelledError:
            raise
        except ProviderError as exc:
            logger.info(
                "Agent text prefetch unavailable room=%s stage=%s code=%s",
                descriptor.room_code,
                descriptor.stage_key,
                exc.code,
            )

    def _consume_agent_prefetch(
        self, db: Session, room: Room, current: dict[str, Any], seat: RoomSeat
    ) -> str:
        cached = self._agent_prefetch_cache.pop(room.code, None)
        self._agent_prefetch_attempted.pop(room.code, None)
        if not cached:
            return ""
        expected = self._agent_prefetch_descriptor(db, room, room.current_stage_index)
        if (
            not expected
            or expected.fingerprint != cached.fingerprint
            or cached.stage_key != current.get("key")
            or cached.seat_key != seat.seat_key
        ):
            return ""
        return cached.content if usable_transcript(cached.content, require_substantive=True) else ""

    def _history_for_agent_prefetch(
        self, db: Session, room: Room, target_stage_index: int
    ) -> list[dict[str, Any]]:
        """Include a just-finalized source transcript before audio drain ends."""
        source_key = ""
        if target_stage_index > 0:
            source_key = str(room.template_snapshot[target_stage_index - 1].get("key") or "")
        grouped: dict[str, list[dict[str, str]]] = {}
        rows = db.scalars(select(Speech).where(Speech.room_id == room.id).order_by(Speech.created_at)).all()
        for item in rows:
            include = item.status == "completed" or (
                item.stage_key == source_key
                and item.status in {"speaking", "synthesizing", "playing"}
            )
            if not include or not usable_transcript(item.content):
                continue
            grouped.setdefault(item.stage_key, []).append(
                {"speaker": seat_label(item.seat_key), "content": item.content}
            )
        return [{"stage": key, "message": value} for key, value in grouped.items()]

    def _retry_later(self, code: str, attempts: int) -> float:
        delay = min(self._MAX_RETRY_SECONDS, float(2 ** max(0, attempts - 1)))
        self._room_retry_at[code] = time.monotonic() + delay
        return delay

    async def _record_room_failure(self, code: str, exc: Exception) -> None:
        if isinstance(exc, (OperationalError, TransactionLockTimeout)):
            self._room_failures.pop(code, None)
            attempts = min(6, self._room_transient_failures.get(code, 0) + 1)
            self._room_transient_failures[code] = attempts
            delay = self._retry_later(code, attempts)
            logger.warning(
                "transient room processing failure; retrying room=%s delay=%.1fs error_type=%s",
                code,
                delay,
                type(exc).__name__,
            )
            return

        self._room_transient_failures.pop(code, None)
        attempts = self._room_failures.get(code, 0) + 1
        self._room_failures[code] = attempts
        logger.error(
            "unexpected room processing failure: room=%s attempt=%s",
            code,
            attempts,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        if attempts < self._UNEXPECTED_FAILURE_LIMIT:
            self._retry_later(code, attempts)
            return

        try:
            quarantined = await self._quarantine_room(code, attempts=attempts, error_type=type(exc).__name__)
        except HTTPException as quarantine_exc:
            if quarantine_exc.status_code == 404:
                self._clear_room_failure(code)
                self._room_locks.pop(code, None)
                return
            self._retry_later(code, attempts)
            logger.error("failed to quarantine room after engine failure: room=%s", code, exc_info=True)
            return
        except (OperationalError, TransactionLockTimeout):
            delay = self._retry_later(code, attempts)
            logger.warning("database busy while quarantining room=%s; retrying in %.1fs", code, delay)
            return
        except Exception:
            self._retry_later(code, attempts)
            logger.exception("failed to quarantine room after engine failure: room=%s", code)
            return
        if quarantined:
            self._clear_room_failure(code)
            self._room_locks.pop(code, None)
        else:
            self._clear_room_failure(code)

    async def _quarantine_room(self, code: str, *, attempts: int, error_type: str) -> bool:
        with SessionLocal() as db:
            room = load_room(db, code, lock=True)
            if room.status not in self._ACTIVE_ROOM_STATUSES:
                db.rollback()
                return False
            if room.status == "lobby":
                room.status = "cancelled"
                room.completed_at = now()
                room.failure_reason = "比赛引擎连续异常，房间已自动取消。"
            else:
                stage_remaining = remaining_seconds(room)
                current = stage(room)
                if current and current.get("kind") == "free":
                    updated_current = dict(current)
                    updated_current["paused_turn_remaining_seconds"] = free_turn_remaining_seconds(room, current) or 0
                    snapshot = list(room.template_snapshot)
                    snapshot[room.current_stage_index] = updated_current
                    room.template_snapshot = snapshot
                self.close_active_speeches(db, room, status="interrupted", reason="engine_quarantined")
                room.status = "paused"
                room.stage_deadline_at = None
                room.paused_remaining_seconds = stage_remaining
                room.failure_reason = f"比赛引擎连续异常，已安全暂停（{error_type}）。"
            append_event(
                db,
                room,
                "engine.quarantined",
                {"attempts": attempts, "error_type": error_type},
            )
            db.commit()
            await self._broadcast(db, room, "engine.quarantined")
        return True

    async def process_room(self, code: str) -> None:
        with SessionLocal() as db:
            room = load_room(db, code, lock=True)
            if await self._expire_presence(db, room):
                db.commit()
                await self._broadcast(db, room, "presence.expired")
            if room.status == "lobby":
                return
            if room.status == "preparing":
                self._invalidate_agent_prefetch(room.code)
                db.commit()
                if not await self._prepare_cues(db, room.code):
                    return
                db.expire_all()
                room = load_room(db, room.code, lock=True)
                if room.status != "preparing":
                    return
                self._enter_stage(db, room, 0)
                db.commit()
                await self._broadcast(db, room, "match.started")
                return
            if room.status not in {"running", "judging"}:
                self._invalidate_agent_prefetch(room.code)
                await self.invalidate_free_agent_speculation(room.code)
                return
            current = stage(room)
            if not current:
                await self._complete_without_judge(db, room)
                return

            if current.get("host_announcement_pending"):
                await self._open_hosted_stage(db, room, current)
                return

            if current.get("kind") == "judging":
                self._invalidate_agent_prefetch(room.code)
                await self.invalidate_free_agent_speculation(room.code)
                await self._judge(db, room)
                return

            if current.get("kind") != "free":
                await self.invalidate_free_agent_speculation(room.code)

            if current.get("kind") == "announcement":
                # Host audio is already prepared and owns no Agent provider
                # slot, making it the safest window to precompute only the
                # next fixed AI speech text.
                self._schedule_next_agent_prefetch(room.code, room.current_stage_index)

            playing = db.scalar(select(Speech).where(Speech.room_id == room.id, Speech.status == "playing"))
            if playing:
                playback_ends_at = playing.playback_ends_at
                if playback_ends_at and playback_ends_at.tzinfo is None:
                    playback_ends_at = playback_ends_at.replace(tzinfo=timezone.utc)
                if playback_ends_at and playback_ends_at > now():
                    if current.get("kind") == "speech":
                        self._schedule_next_agent_prefetch(room.code, room.current_stage_index)
                    return
                self._finish_ai_playback(db, room, playing)
                db.commit()
                if current.get("kind") == "free":
                    self.schedule_free_agent_speculation(room.code)
                await self._broadcast(db, room, "speech.completed")
                return

            if current.get("kind") == "free":
                deadline = intermission_deadline(room, current)
                if deadline:
                    if now() < deadline:
                        self.schedule_free_agent_speculation(room.code)
                        return
                    current, winner = resolve_intermission(db, room, current)
                    db.commit()
                    if winner:
                        await self.invalidate_free_agent_speculation(room.code)
                    await self._broadcast(db, room, "free.intermission_resolved")
                    return
                # MOSS begins audible playback while the final WAV is still
                # being assembled, so the speech remains `synthesizing` for
                # most of the turn. Never revoke that authoritative stream at
                # the nominal turn boundary; provider timeout/cancellation is
                # responsible for a genuinely stuck generation.
                active_stream = db.scalar(
                    select(Speech).where(
                        Speech.room_id == room.id,
                        Speech.status == "synthesizing",
                        Speech.stream_generation.is_not(None),
                        Speech.playback_started_at.is_not(None),
                    )
                )
                if active_stream:
                    return
                if free_turn_remaining_seconds(room, current) == 0:
                    closed = self.close_active_speeches(db, room, status="timed_out", reason="free_turn_elapsed")
                    if not closed:
                        append_event(db, room, "free.turn_timed_out", {"side": current.get("side")})
                    self._advance(db, room, reason="free_turn_completed")
                    db.commit()
                    await self._broadcast(db, room, "speech.timed_out" if closed else "free.turn_timed_out")
                    return

            # Delayed engine ticks and newly substituted AI seats must not
            # start fresh provider work after the authoritative stage deadline.
            if remaining_seconds(room) == 0:
                self.close_active_speeches(db, room, status="timed_out", reason="timer_elapsed")
                self._advance(db, room, reason="timer_elapsed")
                db.commit()
                await self._broadcast(db, room, "stage.advanced")
                return

            if current.get("kind") == "speech":
                seat = next((item for item in room.seats if item.seat_key == current.get("seat")), None)
                if seat and seat.occupant_type in {"ai", "ai_substitute"}:
                    active = db.scalar(
                        select(Speech).where(
                            Speech.room_id == room.id,
                            Speech.stage_key == current["key"],
                            Speech.status.in_(["speaking", "synthesizing", "playing", "completed"]),
                        )
                    )
                    if not active:
                        await self._ai_speech(db, room, seat, current)
                        return

            if current.get("kind") == "free":
                active = db.scalar(
                    select(Speech).where(Speech.room_id == room.id, Speech.status.in_(["speaking", "synthesizing", "playing"]))
                )
                if not active:
                    side = current.get("side", "aff")
                    humans = [item for item in room.seats if item.side == side and item.occupant_type == "human" and item.connected]
                    if current.get("force_ai_fallback") or not humans:
                        ai_seat = self._free_ai_seat(db, room, current, side)
                        if ai_seat:
                            if not current.get("force_ai_fallback") or self._tts_reuse_source(db, room, current):
                                await self._ai_speech(db, room, ai_seat, current, free_turn=True)
                            else:
                                await self._free_ai_speech_with_intent(db, room, ai_seat, current)
                            return

    def _enter_stage(self, db: Session, room: Room, index: int) -> None:
        if index >= len(room.template_snapshot):
            room.status = "judging"
            room.current_stage_index = len(room.template_snapshot)
            room.stage_started_at = now()
            room.stage_deadline_at = None
            room.paused_remaining_seconds = None
            return
        room.current_stage_index = index
        current = dict(room.template_snapshot[index])
        started_at = now()
        hosted = current.get("kind") != "announcement" and bool(str(current.get("cue") or "").strip())
        if current.get("kind") == "free" and not hosted:
            current["turn_seq"] = max(0, int(current.get("turn_seq", 0)))
            current["turn_started_at"] = started_at.isoformat()
            snapshot = list(room.template_snapshot)
            snapshot[index] = current
            room.template_snapshot = snapshot
        duration = max(1, int(current.get("duration", 30)))
        if str(current.get("cue") or "").strip():
            asset_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"phdebate:{room.id}:cue:{current['key']}"))
            audio_duration = self._audio_duration(room.code, asset_id)
            if audio_duration > 0:
                # The cue was prepared before the match began and is already
                # available to every client. Use its real length instead of a
                # guessed template duration, with a small delivery guard so a
                # normal network scheduling delay does not clip the final word.
                cue_duration = max(1, math.ceil(audio_duration + 0.75))
                if current.get("kind") == "announcement":
                    duration = cue_duration
                elif hosted:
                    current["host_announcement_pending"] = True
                    current["host_target_kind"] = current.get("kind")
                    current["kind"] = "announcement"
                    current["host_announcement_deadline_at"] = (started_at + timedelta(seconds=cue_duration)).isoformat()
                    current["host_target_duration_seconds"] = duration
                    snapshot = list(room.template_snapshot)
                    snapshot[index] = current
                    room.template_snapshot = snapshot
                    duration = cue_duration
        room.stage_started_at = started_at
        room.stage_deadline_at = started_at + timedelta(seconds=duration)
        room.paused_remaining_seconds = None
        room.status = "judging" if current.get("kind") == "judging" and not hosted else "running"
        append_event(
            db,
            room,
            "stage.started",
            {"stage": current, "stage_index": index, "deadline_at": room.stage_deadline_at.isoformat()},
            idempotency_key=f"{room.id}:stage:{index}:start",
        )

    async def _open_hosted_stage(self, db: Session, room: Room, current: dict[str, Any]) -> bool:
        """Keep a target stage locked until its prepared host cue has ended."""

        if not current.get("host_announcement_pending"):
            return False
        raw_deadline = current.get("host_announcement_deadline_at")
        try:
            deadline = datetime.fromisoformat(str(raw_deadline))
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            deadline = now()
        if now() < deadline:
            return True
        opened_at = now()
        updated = dict(current)
        duration = max(1, int(updated.pop("host_target_duration_seconds", updated.get("duration", 30))))
        updated["kind"] = str(updated.pop("host_target_kind", updated.get("kind") or "announcement"))
        updated.pop("host_announcement_pending", None)
        updated.pop("host_announcement_deadline_at", None)
        if updated.get("kind") == "free":
            updated["turn_seq"] = max(0, int(updated.get("turn_seq", 0)))
            updated["turn_started_at"] = opened_at.isoformat()
        snapshot = list(room.template_snapshot)
        snapshot[room.current_stage_index] = updated
        room.template_snapshot = snapshot
        room.stage_started_at = opened_at
        room.stage_deadline_at = opened_at + timedelta(seconds=duration)
        room.status = "judging" if updated.get("kind") == "judging" else "running"
        append_event(
            db,
            room,
            "stage.host_announcement_completed",
            {"stage_key": updated.get("key"), "deadline_at": room.stage_deadline_at.isoformat()},
            idempotency_key=f"{room.id}:stage:{room.current_stage_index}:host-opened",
        )
        db.commit()
        await self._broadcast(db, room, "stage.host_announcement_completed")
        return True

    def _advance(self, db: Session, room: Room, *, reason: str) -> None:
        current = stage(room)
        if current and current.get("kind") == "free" and reason == "free_turn_completed" and remaining_seconds(room) not in {0, None}:
            begin_intermission(db, room, current)
            return
        if current and current.get("kind") == "free":
            expire_room_requests(db, room, "stage_ended")
        append_event(db, room, "stage.completed", {"stage": current, "reason": reason})
        self._enter_stage(db, room, room.current_stage_index + 1)

    @staticmethod
    def _mark_ai_preparation(
        room: Room,
        current: dict[str, Any],
        *,
        stage_remaining: int,
        turn_remaining: int | None,
    ) -> dict[str, Any]:
        updated = dict(current)
        updated["ai_preparing"] = True
        updated["preparing_stage_remaining_seconds"] = max(1, stage_remaining)
        if updated.get("kind") == "free" and turn_remaining is not None:
            updated["preparing_turn_remaining_seconds"] = max(1, turn_remaining)
        snapshot = list(room.template_snapshot)
        snapshot[room.current_stage_index] = updated
        room.template_snapshot = snapshot
        # The persisted remaining-time snapshot, rather than a wall-clock
        # deadline, is authoritative while Agent/TTS work is in flight.  This
        # survives API reads and engine restarts without presenting 00:00.
        room.stage_deadline_at = None
        return updated

    @staticmethod
    def _clear_ai_preparation(room: Room) -> dict[str, Any] | None:
        current = stage(room)
        if not current:
            return current
        updated = dict(current)
        changed = False
        for key in ("ai_preparing", "preparing_stage_remaining_seconds", "preparing_turn_remaining_seconds"):
            if key in updated:
                updated.pop(key, None)
                changed = True
        if changed:
            snapshot = list(room.template_snapshot)
            snapshot[room.current_stage_index] = updated
            room.template_snapshot = snapshot
        return updated

    @staticmethod
    def _tts_reuse_source(db: Session, room: Room, current: dict[str, Any]) -> Speech | None:
        """Return only the immediately preceding, never-published TTS attempt.

        Free debate reuses a stage key across many turns.  Looking up the last
        failure per seat would therefore resurrect stale content when that seat
        rotates back several turns later.  Reuse is valid only when the latest
        speech for the entire current stage is itself the failed/interrupted
        attempt.  Creating the replacement speech immediately makes it the
        latest row, which consumes the source once without rewriting audit data.
        """
        previous = db.scalar(
            select(Speech)
            .where(
                Speech.room_id == room.id,
                Speech.stage_key == current["key"],
            )
            .order_by(Speech.created_at.desc(), Speech.id.desc())
            .limit(1)
        )
        if not previous or previous.speaker_type != "ai" or previous.status not in {"failed_retried", "interrupted"}:
            return None
        if not previous.content.strip() or previous.audio_url or previous.playback_started_at is not None:
            return None
        if previous.duration_seconds != 0:
            return None
        return previous

    def _free_agent_payloads(
        self, db: Session, room: Room, seat: RoomSeat, current: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]] | None:
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        if not match:
            return None
        profile = db.get(AgentProfile, seat.agent_profile_id) if seat.agent_profile_id else None
        next_stage = (
            room.template_snapshot[room.current_stage_index + 1]["name"]
            if room.current_stage_index + 1 < len(room.template_snapshot)
            else "比赛结束"
        )
        turn_seq = int(current.get("intermission_turn_seq", current.get("turn_seq", 0)))
        identity = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"phdebate:free-agent:{room.id}:{current['key']}:{turn_seq}:{seat.seat_key}",
        ).hex
        base_payload = agent_payload(
            topic=room.topic,
            debater_name=seat.display_name,
            seat_key=seat.seat_key,
            current_stage=current["name"],
            next_stage=next_stage,
            history=self._history(db, room.id),
            max_token=min(1_000, max(160, int(current.get("turn_duration", 45) * 2.1))),
            model_name=profile.model_name if profile else None,
            match_id=match.id,
            room_code=room.code,
            agent_profile=profile.profile_key if profile else None,
        )
        return (
            {**base_payload, "task_id": f"decision-{identity}", "max_token": 48},
            {**base_payload, "task_id": f"candidate-{identity}", "task_type": "debate_candidate"},
            runtime_provider_config(match.service_snapshot, "agent"),
        )

    def _free_agent_turn_current(self, room_code: str, stage_key: str, turn_seq: int) -> bool:
        with SessionLocal() as check_db:
            check_room = load_room(check_db, room_code)
            check_stage = stage(check_room)
            if (
                check_room.status != "running"
                or not check_stage
                or check_stage.get("kind") != "free"
                or str(check_stage.get("key")) != stage_key
            ):
                return False
            intermission_turn = check_stage.get("intermission_turn_seq")
            if intermission_turn is not None:
                if int(intermission_turn) != turn_seq:
                    return False
            elif int(check_stage.get("turn_seq", -1)) != turn_seq:
                return False
            elif not check_stage.get("force_ai_fallback") and any(
                item.side == check_stage.get("side")
                and item.occupant_type == "human"
                and item.connected
                for item in check_room.seats
            ):
                return False
            active = check_db.scalar(
                select(Speech.id).where(
                    Speech.room_id == check_room.id,
                    Speech.status.in_(["speaking", "synthesizing", "playing"]),
                )
            )
            pending_human = check_db.scalar(
                select(FreeTurnRequest.id).where(
                    FreeTurnRequest.room_id == check_room.id,
                    FreeTurnRequest.stage_key == stage_key,
                    FreeTurnRequest.turn_seq == turn_seq,
                    FreeTurnRequest.status == "pending",
                )
            )
            return active is None and pending_human is None

    async def _run_free_agent_speculation(
        self,
        descriptor: FreeAgentSpeculation,
    ) -> tuple[object, object]:
        endpoint = str(descriptor.provider_config.get("endpoint") or "")
        decision_task = asyncio.create_task(
            decide_should_speak(descriptor.decision_payload, descriptor.provider_config)
        )

        async def generate_candidate() -> str:
            async with self._provider_semaphore():
                return await debate_agent.generate(
                    descriptor.candidate_payload, provider_config=descriptor.provider_config
                )

        candidate_task = asyncio.create_task(generate_candidate())

        async def invalidate() -> None:
            await asyncio.gather(
                interrupt_agent_task(
                    endpoint, descriptor.decision_payload["task_id"], descriptor.provider_config
                ),
                interrupt_agent_task(
                    endpoint, descriptor.candidate_payload["task_id"], descriptor.provider_config
                ),
                return_exceptions=True,
            )

        valid = await wait_while_current(
            {decision_task},
            lambda: self._free_agent_turn_current(
                descriptor.room_code, descriptor.stage_key, descriptor.turn_seq
            ),
            invalidate,
        )
        if not valid:
            candidate_task.cancel()
            await asyncio.gather(candidate_task, return_exceptions=True)
            raise asyncio.CancelledError
        decision = (await asyncio.gather(decision_task, return_exceptions=True))[0]
        if not bool(getattr(decision, "should_speak", True)):
            await invalidate()
            candidate_task.cancel()
            await asyncio.gather(candidate_task, return_exceptions=True)
            return decision, ""
        valid = await wait_while_current(
            {candidate_task},
            lambda: self._free_agent_turn_current(
                descriptor.room_code, descriptor.stage_key, descriptor.turn_seq
            ),
            invalidate,
        )
        if not valid:
            raise asyncio.CancelledError
        return decision, (await asyncio.gather(candidate_task, return_exceptions=True))[0]

    def schedule_free_agent_speculation(self, room_code: str) -> None:
        existing = self._free_agent_speculations.get(room_code)
        # Keep completed negative/positive results until the authoritative
        # three-second window resolves. Re-scheduling here would replay the
        # decision and, after a negative decision, hit an interrupted candidate
        # task instead of reusing the original result.
        if existing:
            return
        with SessionLocal() as db:
            room = load_room(db, room_code)
            current = stage(room)
            if room.status != "running" or not current or not intermission_deadline(room, current):
                return
            turn_seq = int(current.get("intermission_turn_seq", -1))
            side = str(current.get("intermission_side") or "")
            seat = self._free_ai_seat(db, room, current, side)
            if not seat:
                return
            built = self._free_agent_payloads(db, room, seat, current)
            if not built:
                return
            decision_payload, candidate_payload, agent_config = built
        descriptor = FreeAgentSpeculation(
            room_code=room_code,
            stage_key=str(current["key"]),
            turn_seq=turn_seq,
            seat_key=seat.seat_key,
            decision_payload=decision_payload,
            candidate_payload=candidate_payload,
            provider_config=agent_config,
            task=None,
        )
        task = asyncio.create_task(
            self._run_free_agent_speculation(descriptor),
            name=f"free-agent-speculation-{room_code}-{turn_seq}",
        )
        self._free_agent_speculations[room_code] = FreeAgentSpeculation(
            **{**descriptor.__dict__, "task": task}
        )

    async def invalidate_free_agent_speculation(self, room_code: str) -> None:
        descriptor = self._free_agent_speculations.pop(room_code, None)
        if not descriptor:
            return
        await asyncio.gather(
            interrupt_agent_task(
                str(descriptor.provider_config.get("endpoint") or ""),
                descriptor.decision_payload["task_id"],
                descriptor.provider_config,
            ),
            interrupt_agent_task(
                str(descriptor.provider_config.get("endpoint") or ""),
                descriptor.candidate_payload["task_id"],
                descriptor.provider_config,
            ),
            return_exceptions=True,
        )
        if descriptor.task:
            descriptor.task.cancel()
            await asyncio.gather(descriptor.task, return_exceptions=True)

    async def _free_ai_speech_with_intent(
        self,
        db: Session,
        room: Room,
        seat: RoomSeat,
        current: dict[str, Any],
    ) -> None:
        """Run the intent check and hidden candidate generation concurrently."""

        turn_seq = int(current.get("turn_seq", 0))
        room_code = room.code
        stage_key = str(current["key"])
        existing = self._free_agent_speculations.pop(room_code, None)
        if (
            existing
            and existing.stage_key == stage_key
            and existing.turn_seq == turn_seq
            and existing.seat_key == seat.seat_key
            and existing.task
        ):
            descriptor = existing
        else:
            if existing:
                self._free_agent_speculations[room_code] = existing
                await self.invalidate_free_agent_speculation(room_code)
            built = self._free_agent_payloads(db, room, seat, current)
            if not built:
                return
            decision_payload, candidate_payload, agent_config = built
            descriptor = FreeAgentSpeculation(
                room_code=room_code,
                stage_key=stage_key,
                turn_seq=turn_seq,
                seat_key=seat.seat_key,
                decision_payload=decision_payload,
                candidate_payload=candidate_payload,
                provider_config=agent_config,
                task=None,
            )
        decision_payload = descriptor.decision_payload
        candidate_payload = descriptor.candidate_payload
        agent_config = descriptor.provider_config
        db.commit()
        speculation_task = descriptor.task or asyncio.create_task(self._run_free_agent_speculation(descriptor))
        try:
            decision_value, candidate_value = await speculation_task
        except asyncio.CancelledError:
            return
        should_speak = bool(getattr(decision_value, "should_speak", True))
        reason = str(getattr(decision_value, "reason", "判断异常，按既有流程发言"))[:300]
        fallback = bool(getattr(decision_value, "fallback", not hasattr(decision_value, "should_speak")))

        db.expire_all()
        room = load_room(db, room_code, lock=True)
        current = stage(room) or current
        if (
            room.status != "running"
            or not current
            or current.get("kind") != "free"
            or str(current.get("key")) != stage_key
            or int(current.get("turn_seq", -1)) != turn_seq
            or (
                not current.get("force_ai_fallback")
                and any(
                    item.side == current.get("side")
                    and item.occupant_type == "human"
                    and item.connected
                    for item in room.seats
                )
            )
        ):
            db.rollback()
            await asyncio.gather(
                interrupt_agent_task(
                    str(agent_config.get("endpoint") or ""), decision_payload["task_id"], agent_config
                ),
                interrupt_agent_task(
                    str(agent_config.get("endpoint") or ""), candidate_payload["task_id"], agent_config
                ),
                return_exceptions=True,
            )
            return
        append_event(
            db,
            room,
            "free.agent_intent_resolved",
            {
                "seat_key": seat.seat_key,
                "turn_seq": turn_seq,
                "should_speak": should_speak,
                "reason": reason,
                "fallback": fallback,
                "decision_task_id": decision_payload["task_id"],
                "candidate_task_id": candidate_payload["task_id"],
            },
            idempotency_key=f"{room.id}:{stage_key}:{turn_seq}:{seat.seat_key}:agent-intent",
        )
        if not should_speak:
            append_event(
                db,
                room,
                "free.agent_candidate_discarded",
                {"seat_key": seat.seat_key, "turn_seq": turn_seq, "reason": "decision_declined"},
            )
            self._advance(db, room, reason="free_turn_completed")
            db.commit()
            await self._broadcast(db, room, "free.agent_candidate_discarded")
            return
        prefetched_content = (
            candidate_value
            if isinstance(candidate_value, str) and usable_transcript(candidate_value, require_substantive=True)
            else ""
        )
        if not prefetched_content:
            append_event(db, room, "free.agent_candidate_failed", {"seat_key": seat.seat_key, "turn_seq": turn_seq})
        db.commit()
        await self._broadcast(db, room, "free.agent_intent_resolved")
        await self._ai_speech(
            db,
            room,
            seat,
            current,
            free_turn=True,
            speculative_content=prefetched_content,
        )

    async def _ai_speech(
        self,
        db: Session,
        room: Room,
        seat: RoomSeat,
        current: dict[str, Any],
        *,
        free_turn: bool = False,
        speculative_content: str = "",
    ) -> None:
        self._preempt_agent_prefetch_tasks()
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        if not match:
            return
        preparation_started_at = now()
        raw_stage_remaining = remaining_seconds(room)
        preparation_stage_remaining = max(
            1,
            int(raw_stage_remaining if raw_stage_remaining is not None else current.get("duration", 30)),
        )
        raw_turn_remaining = free_turn_remaining_seconds(room, current) if free_turn else None
        preparation_turn_remaining = (
            max(
                1,
                int(raw_turn_remaining if raw_turn_remaining is not None else current.get("turn_duration", 45)),
            )
            if free_turn
            else None
        )
        lighttts_config = runtime_provider_config(match.service_snapshot, "lighttts")
        agent_config = runtime_provider_config(match.service_snapshot, "agent")
        previous = self._tts_reuse_source(db, room, current)
        retry_content = (
            previous.content.strip()
            if (
                previous
                and previous.seat_key == seat.seat_key
            )
            else ""
        )
        prefetched_content = speculative_content or (
            "" if retry_content or free_turn else self._consume_agent_prefetch(db, room, current, seat)
        )
        reusable_content = retry_content or prefetched_content
        speech = Speech(
            match_id=match.id,
            room_id=room.id,
            seat_key=seat.seat_key,
            stage_key=current["key"],
            speaker_type="ai",
            content=reusable_content,
            status="synthesizing" if reusable_content else "speaking",
        )
        db.add(speech)
        db.flush()
        current = self._mark_ai_preparation(
            room,
            current,
            stage_remaining=preparation_stage_remaining,
            turn_remaining=preparation_turn_remaining,
        )
        append_event(db, room, "speech.started", {"speech_id": speech.id, "seat_key": seat.seat_key, "speaker_type": "ai"})
        if reusable_content and previous:
            append_event(
                db,
                room,
                "speech.content.reused",
                {
                    "speech_id": speech.id,
                    "source_speech_id": previous.id,
                    "seat_key": seat.seat_key,
                    "reason": "tts_only_retry",
                },
            )
        elif prefetched_content:
            append_event(
                db,
                room,
                "speech.content.prefetched",
                {
                    "speech_id": speech.id,
                    "seat_key": seat.seat_key,
                    "stage_key": current["key"],
                },
            )
        db.commit()
        await self._broadcast(db, room, "speech.started")

        history = self._history(db, room.id)
        next_stage = (
            room.template_snapshot[room.current_stage_index + 1]["name"]
            if room.current_stage_index + 1 < len(room.template_snapshot)
            else "比赛结束"
        )
        profile = db.get(AgentProfile, seat.agent_profile_id) if seat.agent_profile_id else None
        speech_duration = current.get("turn_duration", 45) if free_turn else current.get("duration", 120)
        # ``max_token`` is an output ceiling, not a target.  The previous
        # 120-token floor repeatedly cut 45-second Chinese turns in the middle
        # of a sentence (the upstream usage ended at exactly 120 tokens).  Give
        # the model enough headroom while the prompt still controls speech
        # length; the Agent gateway separately continues a true length stop.
        max_token = min(1_000, max(160, int(speech_duration * 2.1)))
        payload = agent_payload(
            topic=room.topic,
            debater_name=seat.display_name,
            seat_key=seat.seat_key,
            current_stage=current["name"],
            next_stage=next_stage,
            history=history,
            max_token=max_token,
            model_name=profile.model_name if profile else None,
            match_id=match.id,
            room_code=room.code,
            task_id=speech.id,
            agent_profile=profile.profile_key if profile else None,
        )
        # A 4v4 seat owns one immutable voice for the whole match.  Agent
        # profiles may change prompts/personas between matches, but they must
        # never make a seated speaker drift to another voice mid-tournament.
        voice = voice_for_seat(seat.seat_key)
        # The history/profile reads above start a transaction.  Never wait for a
        # bounded external-provider slot while retaining that connection: once
        # enough rooms queue for Agent generation, a synchronous pool checkout in
        # the next room would otherwise block the event loop and prevent the
        # active provider calls from finishing.  All state needed by the call has
        # been copied, so this safely releases the connection first.
        db.commit()
        audio_url = ""
        room_code = room.code
        speech_id = speech.id
        stage_key = current["key"]
        source_stage_index = room.current_stage_index
        tts_deadline_monotonic = 0.0
        stream_generation: str | None = None
        stream_playback_started_at: datetime | None = None
        agent_first_readable_delta_at: datetime | None = None

        def should_cancel_tts() -> bool:
            return self._tts_job_cancelled(room_code, speech_id)

        async def handle_stream_event(event: dict[str, Any]) -> None:
            nonlocal stream_generation, stream_playback_started_at
            event_type = str(event.get("type") or "")
            generation = str(event.get("generation") or "")
            if event_type == "audio.stream.started":
                db.expire_all()
                stream_room = load_room(db, room_code, lock=True)
                stream_speech = db.get(Speech, speech_id)
                if not self._speech_task_current(stream_room, stream_speech, stage_key, "synthesizing"):
                    db.rollback()
                    raise ProviderCancelled("LightTTS 流式任务已失效。")
                started_at = now()
                stream_generation = generation
                stream_playback_started_at = started_at
                stream_speech.playback_started_at = started_at
                stream_speech.stream_generation = generation
                stream_speech.stream_sample_rate = int(event.get("sample_rate") or settings.lighttts_stream_sample_rate)
                current_stage = self._clear_ai_preparation(stream_room) or current
                if current_stage.get("kind") == "free":
                    updated_current = dict(current_stage)
                    turn_duration = max(1, int(updated_current.get("turn_duration", 45)))
                    preserved_turn_seconds = min(turn_duration, preparation_turn_remaining or turn_duration)
                    elapsed_turn_seconds = max(0, turn_duration - preserved_turn_seconds)
                    updated_current["turn_started_at"] = (
                        started_at - timedelta(seconds=elapsed_turn_seconds)
                    ).isoformat()
                    snapshot = list(stream_room.template_snapshot)
                    snapshot[stream_room.current_stage_index] = updated_current
                    stream_room.template_snapshot = snapshot
                    stream_room.stage_deadline_at = started_at + timedelta(seconds=preparation_stage_remaining)
                else:
                    stream_room.stage_started_at = started_at
                    stream_room.stage_deadline_at = started_at + timedelta(
                        seconds=max(1, int(current_stage.get("duration", 120)))
                    )
                transport = str(event.get("transport") or "websocket_pcm")
                authoritative_event = "audio.rtc.started" if transport == "livekit" else "audio.stream.started"
                append_event(
                    db,
                    stream_room,
                    authoritative_event,
                    {
                        "speech_id": speech_id,
                        "seat_key": seat.seat_key,
                        "generation": generation,
                        "stream_url": event.get("stream_url"),
                        "sample_rate": event.get("sample_rate"),
                        "channels": event.get("channels"),
                        "sample_width": event.get("sample_width"),
                        "transport": transport,
                        "track_sid": event.get("track_sid"),
                        "tts_session_id": event.get("tts_session_id"),
                        "voice_id": event.get("voice_id"),
                        "synthesis_mode": event.get("synthesis_mode"),
                        "agent_first_readable_delta_at": (
                            agent_first_readable_delta_at.isoformat() if agent_first_readable_delta_at else None
                        ),
                        "server_first_capture_at": event.get("server_first_capture_at"),
                        "playback_started_at": started_at.isoformat(),
                    },
                )
                db.commit()
                await self._broadcast(db, stream_room, authoritative_event)
                return
            if event_type == "audio.stream.aborted" and generation:
                audio_stream_aborts.abort(room_code, speech_id, generation)
                db.expire_all()
                stream_room = load_room(db, room_code, lock=True)
                stream_speech = db.get(Speech, speech_id)
                if not stream_speech or stream_speech.stream_generation != generation:
                    db.rollback()
                    return
                stream_speech.stream_generation = ""
                stream_speech.stream_sample_rate = 0
                transport = str(event.get("transport") or "websocket_pcm")
                authoritative_event = "audio.rtc.interrupt" if transport == "livekit" else "audio.stream.aborted"
                append_event(
                    db,
                    stream_room,
                    authoritative_event,
                    {
                        "speech_id": speech_id,
                        "generation": generation,
                        "transport": transport,
                        "track_sid": event.get("track_sid"),
                        "interrupt_id": uuid.uuid4().hex if transport == "livekit" else None,
                        "reason": "provider_failed",
                        "flush_guard_ms": 220 if transport == "livekit" else None,
                    },
                )
                db.commit()
                await self._broadcast(db, stream_room, authoritative_event)

        content = reusable_content
        realtime_pipeline = bool(settings.realtime_voice_pipeline_enabled and realtime_tts.enabled())
        if realtime_pipeline:
            db.expire_all()
            room = load_room(db, room_code, lock=True)
            speech = db.get(Speech, speech_id)
            initial_realtime_status = "synthesizing" if content else "speaking"
            if not self._speech_task_current(room, speech, stage_key, initial_realtime_status):
                if self._interrupt_stale_speech(db, room, speech, reason=room.status):
                    db.commit()
                    await self._broadcast(db, room, "speech.interrupted")
                return
            speech.status = "synthesizing"
            db.commit()
            realtime_tts_timeout = (
                settings.moss_tts_realtime_job_timeout_seconds
                if settings.realtime_voice_backend == "moss_realtime"
                else settings.lighttts_job_timeout_seconds
            )
            tts_deadline_monotonic = asyncio.get_running_loop().time() + realtime_tts_timeout
        try:
            if realtime_pipeline:
                session = await realtime_voice_runtime.open_session(
                    room_code=room_code,
                    speech_id=speech_id,
                    voice_id=voice,
                    provider_config=lighttts_config,
                    should_cancel=should_cancel_tts,
                    deadline_monotonic=tts_deadline_monotonic,
                    on_stream_event=handle_stream_event,
                )

                def mark_first_readable_delta() -> None:
                    nonlocal agent_first_readable_delta_at
                    if agent_first_readable_delta_at is None:
                        agent_first_readable_delta_at = now()

                if content:
                    # A retry may reuse the immutable Agent final text from the
                    # immediately preceding failed attempt.  It still belongs
                    # in one native realtime synthesis session; never fall back
                    # to the retired full-text LightTTS path or send one giant
                    # delta whose ACK would be delayed by the whole utterance.
                    reused_content = content

                    async def reused_agent_events():
                        yield {"type": "delta", "delta": reused_content}
                        yield {"type": "final", "content": reused_content}

                    content, audio_url = await IncrementalVoicePipeline().run(
                        reused_agent_events(),
                        session,
                        should_cancel=should_cancel_tts,
                        on_first_readable_delta=mark_first_readable_delta,
                    )
                else:
                    caption_writer = StreamingCaptionWriter(room_code, speech_id, seat.seat_key)

                    async def agent_events():
                        async with self._provider_semaphore():
                            raw_events = debate_agent.generate_stream(payload, provider_config=agent_config)
                            async for event in self._validated_agent_stream(raw_events):
                                # Yield to the authoritative voice pipeline
                                # first. Caption persistence must never enter
                                # the first-sound latency path.
                                yield event
                                if event.get("type") == "delta" and isinstance(event.get("delta"), str):
                                    await caption_writer.feed(event["delta"])
                                elif event.get("type") == "final":
                                    final_content = str(event.get("content") or "").strip()
                                    await caption_writer.finish(final_content)
                                    # Persist the fixed transcript while native
                                    # audio is still draining, so the next
                                    # Agent sees the exact latest argument. The
                                    # speech remains non-completed until audio
                                    # playback itself is authoritative.
                                    if usable_transcript(final_content, require_substantive=True):
                                        db.expire_all()
                                        prefetch_room = load_room(db, room_code, lock=True)
                                        prefetch_speech = db.get(Speech, speech_id)
                                        if self._speech_task_current(
                                            prefetch_room, prefetch_speech, stage_key, "synthesizing"
                                        ):
                                            prefetch_speech.content = final_content
                                            db.commit()
                                    # The transcript is now fixed, while the
                                    # native realtime session may still be
                                    # draining audio. Start only next-turn
                                    # Agent text; TTS/LiveKit remain untouched.
                                    self._schedule_next_agent_prefetch(room_code, source_stage_index)

                    async def interrupt_agent() -> None:
                        await debate_agent.interrupt(speech_id, provider_config=agent_config)

                    content, audio_url = await IncrementalVoicePipeline().run(
                        agent_events(),
                        session,
                        should_cancel=should_cancel_tts,
                        on_interrupt=interrupt_agent,
                        on_first_readable_delta=mark_first_readable_delta,
                    )
            elif not content:
                async with self._provider_semaphore():
                    content = await debate_agent.generate(payload, provider_config=agent_config)
            if not isinstance(content, str) or not usable_transcript(content, require_substantive=True):
                raise ProviderError(
                    "辩手 Agent 返回内容无效，请重试当前阶段。",
                    code="agent_invalid_output",
                    retryable=True,
                )
        except (ProviderError, RealtimeVoiceError) as exc:
            db.expire_all()
            room = load_room(db, room_code, lock=True)
            speech = db.get(Speech, speech_id)
            expected_status = "synthesizing" if realtime_pipeline else "speaking"
            if not self._speech_task_current(room, speech, stage_key, expected_status):
                if self._interrupt_stale_speech(db, room, speech, reason=room.status):
                    db.commit()
                    await self._broadcast(db, room, "speech.interrupted")
                return
            speech.status = "failed"
            if isinstance(exc, RealtimeVoiceSynthesisError) and exc.final_text:
                speech.content = exc.final_text
            room.status = "paused"
            room.failure_reason = str(exc)
            room.paused_remaining_seconds = preparation_stage_remaining
            updated_current = dict(self._clear_ai_preparation(room) or current)
            if free_turn:
                updated_current["paused_turn_remaining_seconds"] = preparation_turn_remaining or int(
                    updated_current.get("turn_duration", 45)
                )
                snapshot = list(room.template_snapshot)
                snapshot[room.current_stage_index] = updated_current
                room.template_snapshot = snapshot
            append_event(
                db,
                room,
                "provider.failed",
                {
                    "provider": (
                        "agent"
                        if isinstance(exc, ProviderError)
                        else realtime_tts.provider_name()
                        if isinstance(exc, RealtimeVoiceSynthesisError)
                        else "realtime_voice"
                    ),
                    "message": str(exc),
                    "code": exc.code if isinstance(exc, ProviderError) else "realtime_voice_failed",
                    "retryable": exc.retryable if isinstance(exc, ProviderError) else True,
                    "speech_id": speech.id,
                },
            )
            db.commit()
            await self._broadcast(db, room, "provider.failed")
            return

        if not reusable_content:
            db.expire_all()
            room = load_room(db, room_code, lock=True)
            speech = db.get(Speech, speech_id)
            expected_status = "synthesizing" if realtime_pipeline else "speaking"
            if not self._speech_task_current(room, speech, stage_key, expected_status):
                if self._interrupt_stale_speech(db, room, speech, reason=room.status):
                    db.commit()
                    await self._broadcast(db, room, "speech.interrupted")
                return
            speech.content = content
            speech.status = "synthesizing"
            db.commit()
        if tts_deadline_monotonic <= 0:
            tts_deadline_monotonic = asyncio.get_running_loop().time() + settings.lighttts_job_timeout_seconds

        async def synthesize_once() -> str:
            if audio_url:
                return audio_url
            synthesized_url = await lighttts.synthesize(
                content,
                room_code=room_code,
                speech_id=speech_id,
                voice=voice,
                provider_config=lighttts_config,
                should_cancel=should_cancel_tts,
                deadline_monotonic=tts_deadline_monotonic,
                on_stream_event=handle_stream_event if settings.lighttts_streaming_enabled else None,
            )
            if livekit_audio_enabled() and not settings.lighttts_streaming_enabled:
                await lighttts.publish_wav_to_livekit(
                    room_code=room_code,
                    speech_id=speech_id,
                    should_cancel=should_cancel_tts,
                    on_stream_event=handle_stream_event,
                )
            return synthesized_url

        async def record_retry(exc: ProviderError, attempt: int, delay: float) -> None:
            db.expire_all()
            retry_room = load_room(db, room_code, lock=True)
            retry_speech = db.get(Speech, speech_id)
            if not self._speech_task_current(retry_room, retry_speech, stage_key, "synthesizing"):
                db.rollback()
                raise ProviderCancelled("LightTTS 过载重试已取消。")
            append_event(
                db,
                retry_room,
                "provider.retrying",
                {
                    "provider": "lighttts",
                    "code": exc.code,
                    "speech_id": speech_id,
                    "attempt": attempt,
                    "retry_after_seconds": delay,
                },
            )
            db.commit()
            await self._broadcast(db, retry_room, "provider.retrying")

        try:
            audio_url = await self._synthesize_with_admission_retry(
                synthesize_once,
                should_cancel=should_cancel_tts,
                on_retry=record_retry,
                deadline_monotonic=tts_deadline_monotonic,
            )
        except ProviderCancelled:
            db.expire_all()
            room = load_room(db, room_code, lock=True)
            speech = db.get(Speech, speech_id)
            if speech and speech.status in {"speaking", "synthesizing"}:
                speech.status = "interrupted"
                self._clear_ai_preparation(room)
                append_event(
                    db,
                    room,
                    "speech.interrupted",
                    {"speech_id": speech.id, "seat_key": speech.seat_key, "reason": "tts_queue_cancelled"},
                )
                db.commit()
                await self._broadcast(db, room, "speech.interrupted")
            _remove_generated_audio(room.code, speech.id if speech else "")
            return
        except ProviderError as exc:
            db.expire_all()
            room = load_room(db, room_code, lock=True)
            speech = db.get(Speech, speech_id)
            if not self._speech_task_current(room, speech, stage_key, "synthesizing"):
                if self._interrupt_stale_speech(db, room, speech, reason=room.status):
                    db.commit()
                    await self._broadcast(db, room, "speech.interrupted")
                return
            speech.status = "failed"
            room.status = "paused"
            room.failure_reason = str(exc)
            room.paused_remaining_seconds = preparation_stage_remaining
            updated_current = dict(self._clear_ai_preparation(room) or current)
            if free_turn:
                updated_current["paused_turn_remaining_seconds"] = preparation_turn_remaining or int(
                    updated_current.get("turn_duration", 45)
                )
                snapshot = list(room.template_snapshot)
                snapshot[room.current_stage_index] = updated_current
                room.template_snapshot = snapshot
            append_event(
                db,
                room,
                "provider.failed",
                {
                    "provider": "lighttts",
                    "message": str(exc),
                    "code": exc.code,
                    "retryable": exc.retryable,
                    "retry_after_seconds": exc.retry_after_seconds,
                    "speech_id": speech.id,
                },
            )
            db.commit()
            await self._broadcast(db, room, "provider.failed")
            return
        db.expire_all()
        room = load_room(db, room_code, lock=True)
        speech = db.get(Speech, speech_id)
        if not self._speech_task_current(room, speech, stage_key, "synthesizing"):
            _remove_generated_audio(room.code, speech.id if speech else "")
            if self._interrupt_stale_speech(db, room, speech, reason=room.status):
                db.commit()
                await self._broadcast(db, room, "speech.interrupted")
            return
        duration_seconds = self._audio_duration(room.code, speech.id) if audio_url else 0
        if duration_seconds > 0:
            speech.audio_url = audio_url
            playback_started_at = stream_playback_started_at or now()
            speech.duration_seconds = duration_seconds
            speech.playback_started_at = playback_started_at
            playback_ends_at = playback_started_at + timedelta(seconds=duration_seconds)
            current_stage = self._clear_ai_preparation(room)
            if current_stage and current_stage.get("kind") == "free":
                updated_current = dict(current_stage)
                turn_duration = max(1, int(updated_current.get("turn_duration", 45)))
                preserved_turn_seconds = min(turn_duration, preparation_turn_remaining or turn_duration)
                elapsed_turn_seconds = max(0, turn_duration - preserved_turn_seconds)
                updated_current["turn_started_at"] = (
                    playback_started_at - timedelta(seconds=elapsed_turn_seconds)
                ).isoformat()
                snapshot = list(room.template_snapshot)
                snapshot[room.current_stage_index] = updated_current
                room.template_snapshot = snapshot
                current_stage = updated_current
                room.stage_deadline_at = playback_started_at + timedelta(seconds=preparation_stage_remaining)
                # Generated speech must end at a natural audio boundary. The
                # nominal free-turn duration controls how much content we ask
                # the Agent for, but must not truncate an already published
                # sentence or clear queued browser audio mid-word.
            speech.playback_ends_at = playback_ends_at
            speech.status = "playing"
            if not current_stage or current_stage.get("kind") != "free":
                room.stage_started_at = playback_started_at
                room.stage_deadline_at = speech.playback_ends_at
            append_event(
                db,
                room,
                "speech.audio.ready",
                {
                    "speech_id": speech.id,
                    "seat_key": seat.seat_key,
                    "audio_url": speech.audio_url,
                    "duration_seconds": duration_seconds,
                    "preparation_seconds": round(max(0.0, (playback_started_at - preparation_started_at).total_seconds()), 3),
                    "stream_generation": stream_generation,
                    "streamed": stream_generation is not None,
                },
            )
            db.commit()
            await self._broadcast(db, room, "speech.audio.ready")
            return
        _remove_generated_audio(room.code, speech.id)
        self._clear_ai_preparation(room)
        speech.audio_url = ""
        speech.status = "completed"
        append_event(
            db,
            room,
            "speech.completed",
            {"speech_id": speech.id, "seat_key": seat.seat_key, "content": content, "audio_url": speech.audio_url},
        )
        self._advance(db, room, reason="free_turn_completed" if free_turn else "speech_completed")
        db.commit()
        if free_turn:
            self.schedule_free_agent_speculation(room.code)
        await self._broadcast(db, room, "speech.completed")

    def _audio_duration(self, room_code: str, speech_id: str) -> float:
        path = settings.media_path / room_code / f"{speech_id}.wav"
        if not path.is_file() or path.is_symlink():
            return 0
        duration = wav_duration(path)
        return round(max(0.1, min(duration, 600)), 3) if duration > 0 else 0

    def _tts_job_cancelled(self, room_code: str, speech_id: str) -> bool:
        with SessionLocal() as check_db:
            row = check_db.execute(
                select(Room, Speech).join(Speech, Speech.room_id == Room.id).where(Room.code == room_code, Speech.id == speech_id)
            ).first()
            if not row:
                return True
            room, speech = row
            current = stage(room)
            return (
                room.status != "running"
                or not speech
                or speech.status != "synthesizing"
                or not current
                or current.get("key") != speech.stage_key
            )

    @staticmethod
    def _speech_task_current(room: Room, speech: Speech | None, stage_key: str, expected_status: str) -> bool:
        current = stage(room)
        return bool(
            room.status == "running" and speech and speech.status == expected_status and current and current.get("key") == stage_key
        )

    @staticmethod
    def _abort_stream(db: Session, room: Room, speech: Speech, *, reason: str) -> None:
        generation = speech.stream_generation
        if not generation:
            return
        audio_stream_aborts.abort(room.code, speech.id, generation)
        livekit_audio_registry.abort_generation_nowait(room.code, generation)
        speech.stream_generation = ""
        speech.stream_sample_rate = 0
        event_type = "audio.rtc.interrupt" if livekit_audio_enabled() else "audio.stream.aborted"
        append_event(
            db,
            room,
            event_type,
            {
                "speech_id": speech.id,
                "generation": generation,
                "reason": reason,
                "transport": "livekit" if event_type == "audio.rtc.interrupt" else "websocket_pcm",
                "interrupt_id": uuid.uuid4().hex if event_type == "audio.rtc.interrupt" else None,
                "flush_guard_ms": 220 if event_type == "audio.rtc.interrupt" else None,
            },
        )

    @staticmethod
    def _interrupt_stale_speech(db: Session, room: Room, speech: Speech | None, *, reason: str) -> bool:
        if not speech or speech.status not in {"speaking", "synthesizing"}:
            return False
        MatchEngine._abort_stream(db, room, speech, reason="stage_changed" if reason == "running" else reason or "stage_changed")
        speech.status = "interrupted"
        append_event(
            db,
            room,
            "speech.interrupted",
            {
                "speech_id": speech.id,
                "seat_key": speech.seat_key,
                "reason": "stage_changed" if reason == "running" else reason or "stage_changed",
            },
        )
        return True

    def _free_ai_seat(self, db: Session, room: Room, current: dict[str, Any], side: str) -> RoomSeat | None:
        candidates = [item for item in room.seats if item.side == side and item.occupant_type in {"ai", "ai_substitute"}]
        if not candidates:
            return None
        retry_source = self._tts_reuse_source(db, room, current)
        if retry_source:
            retry_seat = next((seat for seat in candidates if seat.seat_key == retry_source.seat_key), None)
            if retry_seat:
                return retry_seat
        counts = dict(
            db.execute(
                select(Speech.seat_key, func.count(Speech.id))
                .where(
                    Speech.room_id == room.id,
                    Speech.stage_key == current["key"],
                    Speech.status == "completed",
                )
                .group_by(Speech.seat_key)
            ).all()
        )
        return min(candidates, key=lambda seat: (counts.get(seat.seat_key, 0), seat.position, seat.seat_key))

    def _finish_ai_playback(self, db: Session, room: Room, speech: Speech) -> None:
        speech.status = "completed"
        speech.playback_ends_at = speech.playback_ends_at or now()
        append_event(
            db,
            room,
            "speech.completed",
            {"speech_id": speech.id, "seat_key": speech.seat_key, "content": speech.content, "audio_url": speech.audio_url},
        )
        current = stage(room)
        self._advance(db, room, reason="free_turn_completed" if current and current.get("kind") == "free" else "speech_completed")

    def close_active_speeches(self, db: Session, room: Room, *, status: str, reason: str) -> list[str]:
        active = list(
            db.scalars(select(Speech).where(Speech.room_id == room.id, Speech.status.in_(["speaking", "synthesizing", "playing"]))).all()
        )
        closed: list[str] = []
        for speech in active:
            self._abort_stream(db, room, speech, reason=reason)
            speech.status = status
            closed.append(speech.id)
            append_event(
                db,
                room,
                "speech.interrupted" if status == "interrupted" else "speech.timed_out",
                {"speech_id": speech.id, "seat_key": speech.seat_key, "reason": reason},
            )
        if closed:
            self._clear_ai_preparation(room)
        return closed

    def interrupt_inflight_ai_speeches(self, db: Session, room: Room, *, reason: str) -> list[str]:
        """Invalidate AI provider work that has not reached authoritative playback yet."""
        inflight = list(
            db.scalars(
                select(Speech).where(
                    Speech.room_id == room.id,
                    Speech.speaker_type == "ai",
                    Speech.status.in_(["speaking", "synthesizing"]),
                )
            ).all()
        )
        interrupted: list[str] = []
        for speech in inflight:
            self._abort_stream(db, room, speech, reason=reason)
            speech.status = "interrupted"
            interrupted.append(speech.id)
            append_event(
                db,
                room,
                "speech.interrupted",
                {"speech_id": speech.id, "seat_key": speech.seat_key, "reason": reason},
            )
        if interrupted:
            self._clear_ai_preparation(room)
        return interrupted

    def interrupt_inflight_judging(self, db: Session, room: Room, *, reason: str) -> bool:
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        if not match:
            return False
        scorecard = db.scalar(select(JudgeScorecard).where(JudgeScorecard.match_id == match.id))
        if not scorecard or scorecard.status != "running":
            return False
        interrupted_task_id = scorecard.task_id
        scorecard.status = "interrupted"
        scorecard.task_id = ""
        scorecard.reasoning = reason
        append_event(
            db,
            room,
            "judge.interrupted",
            {"scorecard_id": scorecard.id, "task_id": interrupted_task_id, "reason": reason},
        )
        return True

    async def _prepare_cues(self, db: Session, room_code: str) -> bool:
        with SessionLocal() as read_db:
            initial = load_room(read_db, room_code)
            template = [dict(item) for item in initial.template_snapshot]
            room_id = initial.id
        for item in template:
            cue = str(item.get("cue") or "").strip()
            if not cue:
                continue
            kind = f"cue:{item['key']}"
            db.expire_all()
            room = load_room(db, room_code, lock=True)
            if room.status != "preparing":
                db.rollback()
                return False
            match = db.scalar(select(Match).where(Match.room_id == room.id))
            if not match:
                db.rollback()
                return False
            asset_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"phdebate:{room_id}:cue:{item['key']}"))
            existing_asset = db.scalar(select(AudioAsset).where(AudioAsset.match_id == match.id, AudioAsset.kind == kind))
            if existing_asset:
                if self._audio_duration(room_code, asset_id) > 0:
                    db.commit()
                    continue
                db.delete(existing_asset)
                append_event(
                    db,
                    room,
                    "audio.cue.invalidated",
                    {"stage_key": item["key"], "reason": "missing_or_invalid_file"},
                )
                db.commit()
            lighttts_config = runtime_provider_config(match.service_snapshot, "lighttts")
            cue_provider = (
                "moss_tts_realtime"
                if settings.realtime_voice_backend == "moss_realtime" and settings.moss_tts_realtime_enabled
                else "lighttts"
            )
            preset = db.scalar(
                select(AudioCue).where(
                    AudioCue.key == item["key"],
                    AudioCue.is_active.is_(True),
                    AudioCue.audio_url != "",
                )
            )
            # A stage key identifies the placement in a versioned flow, but a
            # future host announcement may resolve room-specific variables
            # such as the topic or speaker name. Never copy a stale preset WAV
            # merely because the key matches when its declared text does not.
            if preset and " ".join((preset.text or "").split()) != " ".join(cue.split()):
                preset = None
            preset_source = settings.media_path / "_cues" / f"{preset.id}.wav" if preset else None
            db.commit()
            try:
                source = cue_provider
                audio_url = ""
                if preset_source and preset_source.is_file() and not preset_source.is_symlink():
                    target_dir = settings.media_path / room_code
                    target_dir.mkdir(parents=True, exist_ok=True)
                    target = target_dir / f"{asset_id}.wav"
                    temporary = target.with_suffix(".wav.part")
                    try:
                        shutil.copyfile(preset_source, temporary)
                        temporary.replace(target)
                        audio_url = f"/media/{room_code}/{target.name}"
                        source = "preset"
                    except OSError:
                        logger.warning("unable to copy preset audio cue %s", item["key"], exc_info=True)
                    finally:
                        temporary.unlink(missing_ok=True)
                if not audio_url:
                    cue_deadline_monotonic = asyncio.get_running_loop().time() + settings.lighttts_job_timeout_seconds

                    def should_cancel_cue() -> bool:
                        return self._cue_job_cancelled(room_code)

                    async def synthesize_cue() -> str:
                        if cue_provider == "moss_tts_realtime":
                            return await moss_tts_realtime.synthesize(
                                cue,
                                room_code=room_code,
                                speech_id=asset_id,
                                voice="debate_voice_1",
                                should_cancel=should_cancel_cue,
                                deadline_monotonic=cue_deadline_monotonic,
                                publish_live=False,
                            )
                        return await lighttts.synthesize(
                            cue,
                            room_code=room_code,
                            speech_id=asset_id,
                            provider_config=lighttts_config,
                            should_cancel=should_cancel_cue,
                            background=True,
                            deadline_monotonic=cue_deadline_monotonic,
                        )

                    async def record_cue_retry(exc: ProviderError, attempt: int, delay: float) -> None:
                        db.expire_all()
                        retry_room = load_room(db, room_code, lock=True)
                        if retry_room.status != "preparing":
                            db.rollback()
                            raise ProviderCancelled("LightTTS 提示音过载重试已取消。")
                        append_event(
                            db,
                            retry_room,
                            "provider.retrying",
                            {
                                "provider": cue_provider,
                                "code": exc.code,
                                "stage_key": item["key"],
                                "attempt": attempt,
                                "retry_after_seconds": delay,
                            },
                        )
                        db.commit()
                        await self._broadcast(db, retry_room, "provider.retrying")

                    audio_url = await self._synthesize_with_admission_retry(
                        synthesize_cue,
                        should_cancel=should_cancel_cue,
                        on_retry=record_cue_retry,
                        deadline_monotonic=cue_deadline_monotonic,
                    )
                db.expire_all()
                room = load_room(db, room_code, lock=True)
                if room.status != "preparing":
                    db.rollback()
                    _remove_generated_audio(room_code, asset_id)
                    return False
                match = db.scalar(select(Match).where(Match.room_id == room.id))
                if not match:
                    db.rollback()
                    _remove_generated_audio(room_code, asset_id)
                    return False
                if db.scalar(select(AudioAsset).where(AudioAsset.match_id == match.id, AudioAsset.kind == kind)):
                    db.commit()
                    continue
                storage_key = audio_url
                file_path = settings.media_path / room_code / f"{asset_id}.wav"
                db.add(
                    AudioAsset(
                        id=asset_id,
                        match_id=match.id,
                        kind=kind,
                        storage_key=storage_key,
                        mime_type="audio/wav",
                        size_bytes=file_path.stat().st_size if file_path.exists() else 0,
                    )
                )
                append_event(
                    db,
                    room,
                    "audio.cue.ready",
                    {"stage_key": item["key"], "audio_url": audio_url, "text": cue, "source": source},
                    idempotency_key=f"{room.id}:cue:{item['key']}",
                )
                db.commit()
            except ProviderCancelled:
                db.rollback()
                _remove_generated_audio(room_code, asset_id)
                return False
            except ProviderError as exc:
                db.expire_all()
                room = load_room(db, room_code, lock=True)
                if room.status != "preparing":
                    db.rollback()
                    return False
                room.status = "paused"
                room.failure_reason = str(exc)
                room.paused_remaining_seconds = None
                append_event(
                    db,
                    room,
                    "provider.failed",
                    {
                        "provider": cue_provider,
                        "message": str(exc),
                        "code": exc.code,
                        "retryable": exc.retryable,
                        "retry_after_seconds": exc.retry_after_seconds,
                        "stage_key": item["key"],
                    },
                )
                db.commit()
                await self._broadcast(db, room, "provider.failed")
                return False
        return True

    @staticmethod
    def _cue_job_cancelled(room_code: str) -> bool:
        with SessionLocal() as check_db:
            status = check_db.scalar(select(Room.status).where(Room.code == room_code))
            return status != "preparing"

    async def _judge(self, db: Session, room: Room) -> None:
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        if not match:
            return
        scorecard = db.scalar(select(JudgeScorecard).where(JudgeScorecard.match_id == match.id))
        if scorecard and scorecard.status in {"approved", "review_required"}:
            return
        speeches = self._speech_rows(db, room.id)
        task_id = str(uuid.uuid4())
        if not scorecard:
            scorecard = JudgeScorecard(match_id=match.id, status="running", task_id=task_id)
            db.add(scorecard)
        else:
            scorecard.status = "running"
            scorecard.task_id = task_id
        db.flush()
        append_event(
            db,
            room,
            "judge.started",
            {"scorecard_id": scorecard.id, "task_id": task_id},
        )
        db.commit()
        try:
            async with self._provider_semaphore():
                result = await judge_provider.judge(room.topic, speeches, profile=match.judge_snapshot or None)
            result = self._validated_judge_result(result, room)
            db.expire_all()
            room = load_room(db, room.code, lock=True)
            match = db.scalar(select(Match).where(Match.room_id == room.id))
            scorecard = db.get(JudgeScorecard, scorecard.id)
            if not scorecard or scorecard.status != "running" or scorecard.task_id != task_id:
                db.commit()
                return
            if room.status not in {"judging", "running"}:
                if scorecard.status == "running":
                    scorecard.status = "interrupted"
                db.commit()
                return
            scorecard.status = "approved"
            scorecard.winner = result["winner"]
            scorecard.affirmative_score = result["affirmative_score"]
            scorecard.negative_score = result["negative_score"]
            scorecard.individual_scores = result["individual_scores"]
            scorecard.reasoning = result["reasoning"]
            match.winner = result["winner"]
            match.result_reason = result["reasoning"]
            match.status = "completed"
            room.status = "completed"
            room.completed_at = now()
            room.stage_deadline_at = None
            room.paused_remaining_seconds = None
            expire_pending_restore_requests(db, room, "比赛已完成")
            self._apply_ranking(db, room, match, scorecard)
            append_event(db, room, "match.completed", {"winner": result["winner"], "scorecard_id": scorecard.id})
        except ProviderError as exc:
            db.expire_all()
            room = load_room(db, room.code, lock=True)
            match = db.scalar(select(Match).where(Match.room_id == room.id))
            scorecard = db.get(JudgeScorecard, scorecard.id)
            if not scorecard or scorecard.status != "running" or scorecard.task_id != task_id:
                db.commit()
                return
            if room.status not in {"judging", "running"}:
                if scorecard.status == "running":
                    scorecard.status = "interrupted"
                db.commit()
                return
            scorecard.status = "review_required"
            scorecard.reasoning = str(exc)
            match.status = "review_required"
            room.status = "review_required"
            room.completed_at = now()
            room.stage_deadline_at = None
            room.paused_remaining_seconds = None
            room.failure_reason = str(exc)
            expire_pending_restore_requests(db, room, "比赛已结束并进入人工复核")
            append_event(db, room, "judge.review_required", {"message": str(exc), "scorecard_id": scorecard.id})
        db.commit()
        enqueue_match_archive(match.id)
        await self._broadcast(db, room, "match.result")

    @staticmethod
    def _validated_judge_result(result: Any, room: Room) -> dict[str, Any]:
        """Defend the authoritative result boundary from malformed adapters.

        ``JudgeProvider`` normally normalizes external JSON, but tests,
        alternate adapters and future worker hand-offs can still return a
        structurally invalid object.  Letting a ``KeyError`` escape causes the
        generic engine quarantine to retry the same bad result several times
        and leaves students with an opaque engine failure.  A bad judgement is
        a reviewable match outcome, not a room-runtime corruption.
        """

        if not isinstance(result, dict):
            raise ProviderError(
                "AI 裁判返回格式无效，比赛已转入人工复核。",
                code="judge_invalid_output",
            )
        winner = result.get("winner")
        if winner not in {"aff", "neg", "draw"}:
            raise ProviderError(
                "AI 裁判返回了无法识别的胜方，比赛已转入人工复核。",
                code="judge_invalid_output",
            )

        scores: dict[str, float] = {}
        for key in ("affirmative_score", "negative_score"):
            value = result.get(key)
            if isinstance(value, bool):
                value = None
            try:
                score = float(value)
            except (TypeError, ValueError) as exc:
                raise ProviderError(
                    "AI 裁判返回了无效分数，比赛已转入人工复核。",
                    code="judge_invalid_output",
                ) from exc
            if not math.isfinite(score) or not 0 <= score <= 100:
                raise ProviderError(
                    "AI 裁判分数必须在 0–100 之间，比赛已转入人工复核。",
                    code="judge_invalid_output",
                )
            scores[key] = round(score, 2)

        individual = result.get("individual_scores", {})
        if not isinstance(individual, dict) or len(individual) > 100:
            raise ProviderError(
                "AI 裁判个人评分格式无效，比赛已转入人工复核。",
                code="judge_invalid_output",
            )
        allowed_seats = {seat.seat_key for seat in room.seats}
        normalized_individual: dict[str, float] = {}
        for seat_key, raw_score in individual.items():
            if seat_key not in allowed_seats or isinstance(raw_score, bool):
                continue
            try:
                score = float(raw_score)
            except (TypeError, ValueError):
                continue
            if math.isfinite(score) and 0 <= score <= 100:
                normalized_individual[seat_key] = round(score, 2)

        reasoning = result.get("reasoning")
        if not isinstance(reasoning, str) or not reasoning.strip():
            raise ProviderError(
                "AI 裁判未返回有效判定理由，比赛已转入人工复核。",
                code="judge_invalid_output",
            )
        if len(reasoning) > 10_000:
            raise ProviderError(
                "AI 裁判判定理由过长，比赛已转入人工复核。",
                code="judge_invalid_output",
            )
        return {
            "winner": winner,
            **scores,
            "individual_scores": normalized_individual,
            "reasoning": reasoning.strip(),
        }

    async def _complete_without_judge(self, db: Session, room: Room) -> None:
        match = db.scalar(select(Match).where(Match.room_id == room.id))
        if match:
            match.status = "review_required"
            scorecard = db.scalar(select(JudgeScorecard).where(JudgeScorecard.match_id == match.id))
            if not scorecard:
                db.add(JudgeScorecard(match_id=match.id, status="review_required", reasoning="流程结束但未产生裁判结果。"))
            elif scorecard.status != "approved":
                scorecard.status = "review_required"
                scorecard.reasoning = "流程结束但未产生裁判结果。"
        room.status = "review_required"
        room.completed_at = now()
        room.stage_deadline_at = None
        room.paused_remaining_seconds = None
        expire_pending_restore_requests(db, room, "比赛已结束并进入人工复核")
        append_event(db, room, "judge.review_required", {"message": "流程结束但未产生裁判结果。"})
        db.commit()
        if match:
            enqueue_match_archive(match.id)
        await self._broadcast(db, room, "judge.review_required")

    async def _expire_presence(self, db: Session, room: Room) -> bool:
        changed = False
        profiles = list(db.scalars(select(AgentProfile).where(AgentProfile.is_active.is_(True)).order_by(AgentProfile.created_at)).all())
        active_human_speeches = {
            speech.seat_key: speech
            for speech in db.scalars(
                select(Speech).where(
                    Speech.room_id == room.id,
                    Speech.speaker_type == "human",
                    Speech.status == "speaking",
                )
            ).all()
        }
        for index, seat in enumerate(room.seats):
            if seat.occupant_type != "human" or not seat.user_id:
                continue
            participant = db.get(User, seat.user_id)
            account_disabled = participant is None or not participant.is_active
            if seat.connected and not account_disabled:
                continue
            if account_disabled and seat.connected:
                seat.connected = False
                seat.disconnected_at = now()
                append_event(
                    db,
                    room,
                    "presence.disconnected",
                    {"seat_key": seat.seat_key, "reason": "account_disabled"},
                    actor_user_id=seat.user_id,
                )
                changed = True
            if not seat.disconnected_at:
                if not account_disabled:
                    continue
                seat.disconnected_at = now()
            disconnected_at = seat.disconnected_at
            if disconnected_at.tzinfo is None:
                disconnected_at = disconnected_at.replace(tzinfo=timezone.utc)
            elapsed = (now() - disconnected_at).total_seconds()
            expiry_reason = "account_disabled" if account_disabled else "presence_expired"
            if (
                room.status == "lobby"
                and not account_disabled
                and seat.user_id == room.owner_id
                and elapsed >= 120
            ):
                # A lobby owner is the only participant who cannot voluntarily
                # release their seat.  Presence expiry must therefore preserve
                # the room and control identity so a browser/network interruption
                # does not silently hand the room to another student.  Owners can
                # explicitly transfer control; disabled accounts still use the
                # repair path below.
                continue
            if room.status == "lobby" and (account_disabled or elapsed >= 120):
                self._transfer_disconnected_owner(
                    db,
                    room,
                    seat,
                    reason="owner_account_disabled" if account_disabled else "owner_presence_expired",
                )
                old_name = seat.display_name
                seat.occupant_type = "open"
                seat.user_id = None
                seat.display_name = "待加入"
                seat.is_ready = False
                seat.disconnected_at = None
                seat.control_lease = ""
                seat.control_session_id = None
                append_event(
                    db,
                    room,
                    "seat.expired",
                    {"seat_key": seat.seat_key, "real_name": old_name, "reason": expiry_reason},
                )
                changed = True
            elif room.status in {"preparing", "running", "paused", "judging"} and (account_disabled or elapsed >= 60):
                self._transfer_disconnected_owner(
                    db,
                    room,
                    seat,
                    reason="owner_account_disabled" if account_disabled else "owner_presence_expired",
                )
                profile = profiles[index % len(profiles)] if profiles else None
                seat.occupant_type = "ai_substitute"
                seat.agent_profile_id = profile.id if profile else None
                seat.display_name = f"AI 接替·{seat.display_name}"
                seat.control_lease = ""
                seat.control_session_id = None
                append_event(
                    db,
                    room,
                    "seat.ai_substituted",
                    {"seat_key": seat.seat_key, "user_id": seat.user_id, "reason": expiry_reason},
                )
                abandoned_speech = active_human_speeches.get(seat.seat_key)
                if abandoned_speech:
                    abandoned_speech.status = "interrupted"
                    append_event(
                        db,
                        room,
                        "speech.interrupted",
                        {
                            "speech_id": abandoned_speech.id,
                            "seat_key": seat.seat_key,
                            "reason": expiry_reason,
                        },
                    )
                changed = True
        owner_present = any(seat.occupant_type == "human" and seat.user_id == room.owner_id for seat in room.seats)
        if room.status == "lobby" and not owner_present:
            room.status = "cancelled"
            room.completed_at = now()
            append_event(db, room, "room.cancelled", {"reason": "owner_left_lobby"})
            changed = True
        return changed

    @staticmethod
    def _transfer_disconnected_owner(
        db: Session,
        room: Room,
        expiring_seat: RoomSeat,
        *,
        reason: str,
    ) -> bool:
        """Keep repair controls with a connected human when the owner leaves."""

        if expiring_seat.user_id != room.owner_id:
            return False
        candidates = sorted(
            (
                seat
                for seat in room.seats
                if seat.id != expiring_seat.id
                and seat.occupant_type == "human"
                and seat.user_id
            ),
            # An online participant can repair the room immediately.  If all
            # remaining humans are temporarily offline, still bind ownership
            # to an active account so the first returning participant is not
            # stranded behind a disabled owner forever.
            key=lambda seat: (not seat.connected, seat.position, seat.seat_key),
        )
        for successor in candidates:
            participant = db.get(User, successor.user_id)
            if participant and participant.is_active:
                return transfer_room_owner(
                    db,
                    room,
                    successor,
                    reason=reason,
                    idempotency_key=f"{room.id}:owner-transfer:{expiring_seat.user_id}",
                )
        return False

    def _history(self, db: Session, room_id: str) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, str]]] = {}
        for item in db.scalars(
            select(Speech).where(Speech.room_id == room_id, Speech.status == "completed").order_by(Speech.created_at)
        ).all():
            if not usable_transcript(item.content):
                continue
            grouped.setdefault(item.stage_key, []).append({"speaker": seat_label(item.seat_key), "content": item.content})
        return [{"stage": key, "message": value} for key, value in grouped.items()]

    def _speech_rows(self, db: Session, room_id: str) -> list[dict[str, Any]]:
        return [
            {"seat_key": item.seat_key, "stage": item.stage_key, "content": item.content}
            for item in db.scalars(
                select(Speech).where(Speech.room_id == room_id, Speech.status == "completed").order_by(Speech.created_at)
            ).all()
            if usable_transcript(item.content)
        ]

    def _apply_ranking(self, db: Session, room: Room, match: Match, scorecard: JudgeScorecard) -> None:
        if not room.competition.ranked or room.is_test_data:
            return
        db.flush()
        participant_user_ids = sorted(
            {seat.user_id for seat in room.seats if seat.occupant_type in {"human", "ai_substitute"} and seat.user_id}
        )
        if participant_user_ids:
            db.execute(select(User.id).where(User.id.in_(participant_user_ids)).order_by(User.id).with_for_update())
        for seat in room.seats:
            if seat.occupant_type not in {"human", "ai_substitute"} or not seat.user_id:
                continue
            existing_change = db.scalar(
                select(RatingChange.id).where(
                    RatingChange.match_id == match.id,
                    RatingChange.user_id == seat.user_id,
                    RatingChange.source == "initial",
                )
            )
            if existing_change:
                continue
            outcome = "draw" if match.winner == "draw" else "win" if match.winner == seat.side else "loss"
            delta = 3 if outcome == "win" else 1 if outcome == "draw" else 0
            score = scorecard.affirmative_score if seat.side == "aff" else scorecard.negative_score
            entry = db.scalar(
                select(LeaderboardEntry).where(
                    LeaderboardEntry.competition_id == room.competition_id,
                    LeaderboardEntry.season_id == match.season_id,
                    LeaderboardEntry.user_id == seat.user_id,
                )
            )
            if not entry:
                entry = LeaderboardEntry(
                    competition_id=room.competition_id,
                    season_id=match.season_id,
                    user_id=seat.user_id,
                    points=0,
                    wins=0,
                    draws=0,
                    losses=0,
                    matches=0,
                    average_score=0,
                    last_match_at=None,
                )
                db.add(entry)
            old_total = entry.average_score * entry.matches
            entry.matches += 1
            entry.points += delta
            entry.wins += int(outcome == "win")
            entry.draws += int(outcome == "draw")
            entry.losses += int(outcome == "loss")
            entry.average_score = (old_total + score) / entry.matches
            completed_at = room.completed_at or now()
            previous_match_at = entry.last_match_at
            if previous_match_at and previous_match_at.tzinfo is None:
                previous_match_at = previous_match_at.replace(tzinfo=timezone.utc)
            if previous_match_at is None or completed_at > previous_match_at:
                entry.last_match_at = completed_at
            db.add(
                RatingChange(
                    match_id=match.id,
                    user_id=seat.user_id,
                    points_delta=delta,
                    score=score,
                    reason=outcome,
                    source="initial",
                    idempotency_key=f"{match.id}:{seat.user_id}:initial",
                )
            )

    def _correct_ranking(
        self,
        db: Session,
        room: Room,
        match: Match,
        *,
        correction_id: str,
        old_winner: str,
        new_winner: str,
        old_affirmative_score: float,
        old_negative_score: float,
        new_affirmative_score: float,
        new_negative_score: float,
    ) -> None:
        """Apply a correction as compensating rows without rewriting settlement history."""
        if not room.competition.ranked or room.is_test_data:
            return

        seen_users: set[str] = set()
        for seat in room.seats:
            if seat.occupant_type not in {"human", "ai_substitute"} or not seat.user_id or seat.user_id in seen_users:
                continue
            seen_users.add(seat.user_id)
            old_outcome = "draw" if old_winner == "draw" else "win" if old_winner == seat.side else "loss"
            new_outcome = "draw" if new_winner == "draw" else "win" if new_winner == seat.side else "loss"
            old_delta = 3 if old_outcome == "win" else 1 if old_outcome == "draw" else 0
            new_delta = 3 if new_outcome == "win" else 1 if new_outcome == "draw" else 0
            old_score = old_affirmative_score if seat.side == "aff" else old_negative_score
            new_score = new_affirmative_score if seat.side == "aff" else new_negative_score

            initial_change = db.scalar(
                select(RatingChange).where(
                    RatingChange.match_id == match.id,
                    RatingChange.user_id == seat.user_id,
                    RatingChange.source == "initial",
                )
            )
            entry = db.scalar(
                select(LeaderboardEntry)
                .where(
                    LeaderboardEntry.competition_id == room.competition_id,
                    LeaderboardEntry.season_id == match.season_id,
                    LeaderboardEntry.user_id == seat.user_id,
                )
                .with_for_update()
            )
            if not initial_change or not entry or entry.matches < 1:
                raise RuntimeError(f"missing initial ranking settlement for user {seat.user_id}")

            entry.points += new_delta - old_delta
            entry.wins = max(0, entry.wins - int(old_outcome == "win") + int(new_outcome == "win"))
            entry.draws = max(0, entry.draws - int(old_outcome == "draw") + int(new_outcome == "draw"))
            entry.losses = max(0, entry.losses - int(old_outcome == "loss") + int(new_outcome == "loss"))
            entry.average_score = max(0.0, min(100.0, (entry.average_score * entry.matches - old_score + new_score) / entry.matches))
            db.add(
                RatingChange(
                    match_id=match.id,
                    user_id=seat.user_id,
                    points_delta=new_delta - old_delta,
                    score=new_score,
                    reason=f"correction:{old_outcome}->{new_outcome}",
                    source="correction",
                    idempotency_key=f"{correction_id}:{seat.user_id}",
                )
            )

    async def _broadcast(self, db: Session, room: Room, event_type: str) -> None:
        # All callers persist the room/event before broadcasting.  Refreshing here
        # opens a new transaction and keeps its database connection checked out
        # while Redis I/O is awaited.  With many rooms that can exhaust the SQL
        # pool and stall every room, including human timers.  ``expire_on_commit``
        # is disabled, so the already-persisted projection values are authoritative.
        await room_hub.publish(room.code, {"type": event_type, "room_code": room.code, "seq": room.seq})


match_engine = MatchEngine()
