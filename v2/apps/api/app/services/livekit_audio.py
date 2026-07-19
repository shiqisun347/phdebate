from __future__ import annotations

import asyncio
import logging
import math
import threading
from array import array
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.config import settings

logger = logging.getLogger(__name__)


def livekit_audio_enabled() -> bool:
    return settings.webrtc_audio_enabled and settings.webrtc_audio_backend == "livekit"


def livekit_room_name(room_code: str) -> str:
    return f"debate:{room_code}"


def create_livekit_token(
    *,
    identity: str,
    room_name: str,
    can_publish: bool,
    can_subscribe: bool,
    ttl_seconds: int,
    hidden: bool = False,
) -> str:
    from livekit import api

    grants = api.VideoGrants(
        room_join=True,
        room=room_name,
        can_publish=can_publish,
        can_subscribe=can_subscribe,
        can_publish_data=False,
        can_publish_sources=["microphone"] if can_publish else [],
        hidden=hidden,
    )
    return (
        api.AccessToken(settings.livekit_api_key, settings.livekit_api_secret)
        .with_identity(identity)
        .with_ttl(timedelta(seconds=ttl_seconds))
        .with_grants(grants)
        .to_jwt()
    )


class StreamingPcm16Resampler:
    """Stateful mono PCM16 linear resampler with chunk-boundary continuity."""

    def __init__(self, source_rate: int, target_rate: int = 48_000) -> None:
        if source_rate < 1 or target_rate < 1:
            raise ValueError("sample rates must be positive")
        self.source_rate = source_rate
        self.target_rate = target_rate
        self._samples = array("h")
        self._base_input_index = 0
        self._next_output_numerator = 0
        self._total_input_samples = 0
        self._total_output_samples = 0
        self._finished = False

    @staticmethod
    def _decode(data: bytes) -> array:
        if len(data) % 2:
            raise ValueError("PCM16 data must contain complete samples")
        samples = array("h")
        samples.frombytes(data)
        if samples.itemsize != 2:
            raise RuntimeError("platform does not expose 16-bit signed short samples")
        return samples

    @staticmethod
    def _encode(samples: array) -> bytes:
        return samples.tobytes()

    def push(self, data: bytes) -> bytes:
        if self._finished:
            raise RuntimeError("resampler is already finished")
        incoming = self._decode(data)
        if not incoming:
            return b""
        self._samples.extend(incoming)
        self._total_input_samples += len(incoming)
        return self._produce(hold_last=False)

    def finish(self) -> bytes:
        if self._finished:
            return b""
        self._finished = True
        if not self._samples:
            return b""
        return self._produce(hold_last=True)

    def _produce(self, *, hold_last: bool) -> bytes:
        output = array("h")
        target_total = round(self._total_input_samples * self.target_rate / self.source_rate)
        available_end = self._base_input_index + len(self._samples)
        while True:
            if hold_last:
                if self._total_output_samples >= target_total:
                    break
            left_global = self._next_output_numerator // self.target_rate
            left_local = left_global - self._base_input_index
            if left_local < 0 or left_local >= len(self._samples):
                break
            right_local = left_local + 1
            if right_local >= len(self._samples) and not hold_last:
                break
            left = int(self._samples[left_local])
            right = int(self._samples[right_local]) if right_local < len(self._samples) else left
            fraction = self._next_output_numerator % self.target_rate
            interpolated = round(
                (left * (self.target_rate - fraction) + right * fraction) / self.target_rate
            )
            output.append(max(-32_768, min(32_767, interpolated)))
            self._next_output_numerator += self.source_rate
            self._total_output_samples += 1
            if not hold_last and self._next_output_numerator // self.target_rate + 1 >= available_end:
                break

        next_left = self._next_output_numerator // self.target_rate
        discard = min(max(0, next_left - self._base_input_index), max(0, len(self._samples) - 1))
        if discard:
            del self._samples[:discard]
            self._base_input_index += discard
        return self._encode(output)


class Pcm20msFramer:
    def __init__(self, sample_rate: int = 48_000, frame_ms: int = 20) -> None:
        if frame_ms <= 0 or sample_rate * frame_ms % 1000:
            raise ValueError("frame duration must contain a whole number of samples")
        self.sample_rate = sample_rate
        self.frame_ms = frame_ms
        self.samples_per_frame = sample_rate * frame_ms // 1000
        self.bytes_per_frame = self.samples_per_frame * 2
        self._buffer = bytearray()

    def feed(self, pcm16: bytes) -> list[bytes]:
        if len(pcm16) % 2:
            raise ValueError("PCM16 data must contain complete samples")
        self._buffer.extend(pcm16)
        frames: list[bytes] = []
        while len(self._buffer) >= self.bytes_per_frame:
            frames.append(bytes(self._buffer[: self.bytes_per_frame]))
            del self._buffer[: self.bytes_per_frame]
        return frames

    def finish(self) -> list[bytes]:
        if not self._buffer:
            return []
        padded = bytes(self._buffer) + b"\x00" * (self.bytes_per_frame - len(self._buffer))
        self._buffer.clear()
        return [padded]


@dataclass
class LiveKitConnection:
    room: Any
    source: Any
    track_sid: str


class LiveKitSDKAdapter:
    async def connect(self, room_code: str) -> LiveKitConnection:
        from livekit import rtc

        room_name = livekit_room_name(room_code)
        identity = f"agent-audio:{room_code}"
        token = create_livekit_token(
            identity=identity,
            room_name=room_name,
            can_publish=True,
            can_subscribe=False,
            ttl_seconds=settings.livekit_publisher_token_ttl_seconds,
            # The browser must receive the publisher participant before its
            # agent-tts track.  A hidden publisher can deliver a track update
            # without the matching participant update, which makes Chromium's
            # subscriber negotiation fail with a missing transceiver/m-line.
            hidden=False,
        )
        room = rtc.Room()
        await room.connect(
            settings.livekit_url,
            token,
            rtc.RoomOptions(auto_subscribe=False, connect_timeout=settings.livekit_connect_timeout_seconds),
        )
        source = rtc.AudioSource(
            settings.livekit_audio_sample_rate,
            1,
            queue_size_ms=settings.livekit_audio_source_queue_ms,
        )
        track = rtc.LocalAudioTrack.create_audio_track("agent-tts", source)
        options = rtc.TrackPublishOptions()
        options.source = rtc.TrackSource.SOURCE_MICROPHONE
        options.audio_encoding.max_bitrate = settings.livekit_audio_max_bitrate_bps
        options.dtx = settings.livekit_audio_dtx_enabled
        options.red = settings.livekit_audio_red_enabled
        publication = await room.local_participant.publish_track(track, options)
        return LiveKitConnection(room=room, source=source, track_sid=publication.sid)

    async def capture_frame(self, connection: LiveKitConnection, pcm16: bytes) -> None:
        from livekit import rtc

        frame = rtc.AudioFrame(
            pcm16,
            sample_rate=settings.livekit_audio_sample_rate,
            num_channels=1,
            samples_per_channel=len(pcm16) // 2,
        )
        await connection.source.capture_frame(frame)

    def clear_queue(self, connection: LiveKitConnection) -> None:
        connection.source.clear_queue()

    async def wait_for_playout(self, connection: LiveKitConnection) -> None:
        await connection.source.wait_for_playout()

    @staticmethod
    def queued_duration(connection: LiveKitConnection) -> float:
        return float(connection.source.queued_duration)

    async def disconnect(self, connection: LiveKitConnection) -> None:
        try:
            if connection.track_sid:
                await connection.room.local_participant.unpublish_track(connection.track_sid)
        finally:
            try:
                await connection.source.aclose()
            finally:
                await connection.room.disconnect()


@dataclass
class _GenerationState:
    speech_id: str
    generation: str
    input_sample_rate: int
    resampler: StreamingPcm16Resampler
    framer: Pcm20msFramer
    revoked: asyncio.Event = field(default_factory=asyncio.Event)
    buffer_ready: asyncio.Event = field(default_factory=asyncio.Event)
    first_capture: asyncio.Future[str] | None = None
    first_frame_enqueued: bool = False
    start_buffer_ms: int = 0
    input_samples_received: int = 0
    audio_frames_enqueued: int = 0
    input_finished: bool = False

    @property
    def start_buffer_ready(self) -> bool:
        if self.start_buffer_ms <= 0 or self.input_finished:
            return True
        return (
            self.audio_frames_enqueued > 0
            and self.input_samples_received * 1_000 >= self.input_sample_rate * self.start_buffer_ms
        )


@dataclass(frozen=True)
class _AudioPacket:
    generation: str
    pcm16: bytes


@dataclass(frozen=True)
class _DrainMarker:
    generation: str
    completed: asyncio.Future[None]


class LiveKitRoomAudioPublisher:
    def __init__(self, room_code: str, *, adapter: LiveKitSDKAdapter | None = None) -> None:
        self.room_code = room_code
        self.adapter = adapter or LiveKitSDKAdapter()
        self.loop = asyncio.get_running_loop()
        queue_frames = math.ceil(settings.livekit_audio_app_queue_ms / settings.livekit_audio_frame_ms)
        self.queue: asyncio.Queue[_AudioPacket | _DrainMarker] = asyncio.Queue(maxsize=queue_frames)
        self.connection: LiveKitConnection | None = None
        self._publisher_task: asyncio.Task[None] | None = None
        self._generation: _GenerationState | None = None
        self._closed = False
        self._state_lock = asyncio.Lock()
        # ``AudioSource.clear_queue`` and ``capture_frame`` must form one
        # ordered media operation.  Without this lock an old generation can be
        # dequeued by ``_publisher_loop``, an interrupt can clear the SDK queue,
        # and the suspended old capture can then complete *after* the clear.
        # That resurrects one stale 20 ms frame after a hard interrupt.
        self._capture_lock = asyncio.Lock()

    @property
    def track_sid(self) -> str:
        return self.connection.track_sid if self.connection else ""

    @property
    def active_generation(self) -> str | None:
        return self._generation.generation if self._generation else None

    async def start(self) -> None:
        if self.connection is not None:
            self._raise_publisher_failure()
            return
        self.connection = await self.adapter.connect(self.room_code)
        # Establish the native playout lead before exposing this publisher to
        # ``start_generation``. If this prefill runs only inside the background
        # task, an immediate generation can clear the queue first and then have
        # stale startup silence inserted ahead of its real PCM.
        silence = b"\x00" * (
            settings.livekit_audio_sample_rate * settings.livekit_audio_frame_ms // 1_000 * 2
        )
        initial_frames = max(1, settings.livekit_audio_source_queue_ms // settings.livekit_audio_frame_ms)
        for _index in range(initial_frames):
            await self.adapter.capture_frame(self.connection, silence)
        self._publisher_task = asyncio.create_task(
            self._publisher_loop(),
            name=f"livekit-agent-audio-{self.room_code}",
        )

    def _raise_publisher_failure(self) -> None:
        if self._publisher_task is not None and self._publisher_task.done():
            self._publisher_task.result()

    async def start_generation(
        self,
        speech_id: str,
        generation: str,
        input_sample_rate: int,
        *,
        start_buffer_ms: int | None = None,
    ) -> str:
        await self.start()
        self._raise_publisher_failure()
        resolved_start_buffer_ms = 0 if start_buffer_ms is None else start_buffer_ms
        if resolved_start_buffer_ms < 0 or resolved_start_buffer_ms % settings.livekit_audio_frame_ms:
            raise ValueError("LiveKit start buffer must be a non-negative whole number of audio frames")
        if resolved_start_buffer_ms > settings.livekit_audio_app_queue_ms:
            raise ValueError("LiveKit start buffer must fit inside the application queue")
        async with self._state_lock:
            if self._closed:
                raise RuntimeError("LiveKit room publisher is closed")
            if self._generation and self._generation.generation != generation:
                raise RuntimeError(f"room {self.room_code} already has an active audio generation")
            if self._generation is None:
                state = _GenerationState(
                    speech_id=speech_id,
                    generation=generation,
                    input_sample_rate=input_sample_rate,
                    resampler=StreamingPcm16Resampler(input_sample_rate, settings.livekit_audio_sample_rate),
                    framer=Pcm20msFramer(settings.livekit_audio_sample_rate, settings.livekit_audio_frame_ms),
                    start_buffer_ms=resolved_start_buffer_ms,
                )
                state.first_capture = self.loop.create_future()
                if state.start_buffer_ready:
                    state.buffer_ready.set()
                self._generation = state
                if self.connection:
                    async with self._capture_lock:
                        self.adapter.clear_queue(self.connection)
            elif self._generation.input_sample_rate != input_sample_rate:
                raise ValueError("sample rate changed inside one TTS generation")
            elif self._generation.start_buffer_ms != resolved_start_buffer_ms:
                raise ValueError("start buffer changed inside one TTS generation")
            return self.track_sid

    async def write_pcm(self, generation: str, pcm16: bytes) -> str | None:
        self._raise_publisher_failure()
        state = self._generation
        if state is None or state.generation != generation or state.revoked.is_set():
            return None
        if len(pcm16) % 2:
            raise ValueError("PCM16 data must contain complete samples")
        start_buffer_was_ready = state.start_buffer_ready
        state.input_samples_received += len(pcm16) // 2
        resampled = state.resampler.push(pcm16)
        frames = state.framer.feed(resampled)
        first_batch = bool(frames and not state.first_frame_enqueued)
        if frames:
            state.first_frame_enqueued = True
        for frame in frames:
            if not await self._put_packet(state, _AudioPacket(generation=generation, pcm16=frame)):
                return None
            state.audio_frames_enqueued += 1
        start_buffer_became_ready = not start_buffer_was_ready and state.start_buffer_ready
        if state.start_buffer_ready:
            state.buffer_ready.set()
        if (first_batch or start_buffer_became_ready) and state.start_buffer_ready and state.first_capture is not None:
            return await asyncio.shield(state.first_capture)
        return state.first_capture.result() if state.first_capture and state.first_capture.done() else None

    async def finish_generation(self, generation: str) -> str | None:
        self._raise_publisher_failure()
        state = self._generation
        if state is None or state.generation != generation or state.revoked.is_set():
            return None
        frames = state.framer.feed(state.resampler.finish())
        frames.extend(state.framer.finish())
        if frames:
            state.first_frame_enqueued = True
        for frame in frames:
            if not await self._put_packet(state, _AudioPacket(generation=generation, pcm16=frame)):
                return None
            state.audio_frames_enqueued += 1
        # Make a short utterance eligible for playout only after all of its
        # tail frames are safely queued.  This avoids consuming a partial
        # prebuffer while the producer is still finalizing the resampler.
        state.input_finished = True
        state.buffer_ready.set()
        drained = self.loop.create_future()
        if not await self._put_packet(state, _DrainMarker(generation=generation, completed=drained)):
            return None
        await asyncio.shield(drained)
        first_capture_at = (
            state.first_capture.result()
            if state.first_capture is not None and state.first_capture.done() and not state.first_capture.cancelled()
            else None
        )
        if self._generation is state:
            self._generation = None
        return first_capture_at

    async def _put_packet(self, state: _GenerationState, packet: _AudioPacket | _DrainMarker) -> bool:
        put = asyncio.create_task(self.queue.put(packet))
        revoked = asyncio.create_task(state.revoked.wait())
        publisher_task = self._publisher_task
        waiters: set[asyncio.Task[Any]] = {put, revoked}
        if publisher_task is not None:
            waiters.add(publisher_task)
        try:
            done, _pending = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            if publisher_task is not None and publisher_task in done:
                if not put.done():
                    put.cancel()
                    await asyncio.gather(put, return_exceptions=True)
                publisher_task.result()
            if revoked in done and state.revoked.is_set():
                if not put.done():
                    put.cancel()
                    await asyncio.gather(put, return_exceptions=True)
                return False
            await put
            return True
        finally:
            if not revoked.done():
                revoked.cancel()
                await asyncio.gather(revoked, return_exceptions=True)

    async def abort_generation(self, generation: str) -> bool:
        state = self._generation
        if state is None or state.generation != generation:
            return False
        state.revoked.set()
        self._generation = None
        self._clear_application_queue(generation)
        if self.connection:
            # Wait for an already-started capture to finish, then clear it.
            # A publisher waiting behind this lock re-checks ``revoked`` before
            # it captures and therefore can only submit silence afterwards.
            async with self._capture_lock:
                self.adapter.clear_queue(self.connection)
        if state.first_capture and not state.first_capture.done():
            state.first_capture.cancel()
        return True

    def abort_generation_nowait(self, generation: str) -> None:
        def schedule() -> None:
            asyncio.create_task(self.abort_generation(generation))

        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if current is self.loop:
            schedule()
        elif not self.loop.is_closed():
            self.loop.call_soon_threadsafe(schedule)

    def _clear_application_queue(self, generation: str) -> None:
        retained: list[_AudioPacket | _DrainMarker] = []
        while True:
            try:
                item = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self.queue.task_done()
            if item.generation != generation:
                retained.append(item)
            elif isinstance(item, _DrainMarker) and not item.completed.done():
                item.completed.cancel()
        for item in retained:
            self.queue.put_nowait(item)

    async def _publisher_loop(self) -> None:
        if self.connection is None:
            raise RuntimeError("LiveKit publisher was started without a connection")
        frame_seconds = settings.livekit_audio_frame_ms / 1000
        silence = b"\x00" * (settings.livekit_audio_sample_rate * settings.livekit_audio_frame_ms // 1000 * 2)
        backlog_started_at: float | None = None
        while True:
            item: _AudioPacket | _DrainMarker | None = None
            state = self._generation
            holding_start_buffer = state is not None and not state.start_buffer_ready
            if holding_start_buffer:
                ready = asyncio.create_task(state.buffer_ready.wait())
                revoked = asyncio.create_task(state.revoked.wait())
                try:
                    await asyncio.wait({ready, revoked}, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for waiter in (ready, revoked):
                        if not waiter.done():
                            waiter.cancel()
                    await asyncio.gather(ready, revoked, return_exceptions=True)
                continue
            else:
                try:
                    item = self.queue.get_nowait()
                except asyncio.QueueEmpty:
                    # Never enqueue synthetic silence into the SDK while a
                    # live generation is merely waiting for its next PCM
                    # burst.  MOSS emits bursty chunks; the old loop filled
                    # the SDK queue with silence during every short gap, so
                    # real PCM arriving milliseconds later had to wait behind
                    # already-buffered silence.  Wait for either the next
                    # packet or an explicit revoke and let the SDK's bounded
                    # queue provide the playout lead.
                    if state is not None and not state.input_finished and not state.revoked.is_set():
                        packet = asyncio.create_task(self.queue.get())
                        revoked = asyncio.create_task(state.revoked.wait())
                        try:
                            done, _pending = await asyncio.wait(
                                {packet, revoked},
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            if revoked in done and state.revoked.is_set():
                                if packet.done() and not packet.cancelled():
                                    discarded = packet.result()
                                    self.queue.task_done()
                                    if isinstance(discarded, _DrainMarker) and not discarded.completed.done():
                                        discarded.completed.cancel()
                                else:
                                    packet.cancel()
                                    await asyncio.gather(packet, return_exceptions=True)
                                continue
                            item = packet.result()
                        finally:
                            if not revoked.done():
                                revoked.cancel()
                            await asyncio.gather(revoked, return_exceptions=True)

            if isinstance(item, _DrainMarker):
                try:
                    state = self._generation
                    if state is not None and state.generation == item.generation and not state.revoked.is_set():
                        playout = asyncio.create_task(self.adapter.wait_for_playout(self.connection))
                        revoked = asyncio.create_task(state.revoked.wait())
                        try:
                            done, _pending = await asyncio.wait(
                                {playout, revoked},
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            if revoked in done and state.revoked.is_set() and not playout.done():
                                playout.cancel()
                                await asyncio.gather(playout, return_exceptions=True)
                            elif not state.revoked.is_set():
                                # A genuine SDK playout failure must fail the
                                # generation instead of being mistaken for a
                                # clean drain.  Only an explicit revoke may
                                # suppress cancellation from the old drain.
                                await playout
                        finally:
                            if not revoked.done():
                                revoked.cancel()
                            await asyncio.gather(revoked, return_exceptions=True)
                    if not item.completed.done():
                        item.completed.set_result(None)
                finally:
                    self.queue.task_done()
                continue

            try:
                async with self._capture_lock:
                    # Re-evaluate the generation inside the same critical
                    # section as SDK capture.  ``abort_generation`` changes the
                    # state before taking this lock, so a packet can never pass
                    # this check after the interrupt's queue clear.
                    pcm16 = silence
                    if isinstance(item, _AudioPacket):
                        state = self._generation
                        if state is not None and state.generation == item.generation and not state.revoked.is_set():
                            pcm16 = item.pcm16
                    await self.adapter.capture_frame(self.connection, pcm16)
                queued_duration = self.adapter.queued_duration(self.connection)
                expected_queue_seconds = settings.livekit_audio_source_queue_ms / 1_000
                if queued_duration > expected_queue_seconds + frame_seconds * 2:
                    backlog_started_at = backlog_started_at or self.loop.time()
                    if self.loop.time() - backlog_started_at >= 2:
                        logger.warning(
                            "LiveKit publisher backlog room=%s queued_duration_ms=%.1f",
                            self.room_code,
                            queued_duration * 1000,
                        )
                        backlog_started_at = self.loop.time()
                else:
                    backlog_started_at = None
                if isinstance(item, _AudioPacket):
                    state = self._generation
                    if state is not None and state.generation == item.generation and state.first_capture is not None:
                        if not state.first_capture.done():
                            state.first_capture.set_result(datetime.now(timezone.utc).isoformat())
            finally:
                if item is not None:
                    self.queue.task_done()

            # ``AudioSource.capture_frame`` already applies backpressure from
            # its native bounded queue. A second wall-clock sleep here kept the
            # queue nearly empty after every generation clear, so one Python
            # scheduling stall became a browser concealment burst. Let active
            # PCM refill the native queue immediately and rely on the SDK for
            # pacing. Test adapters do not model that backpressure, so only
            # pace idle silence when they report an empty native queue.
            if item is None and queued_duration < frame_seconds:
                await asyncio.sleep(frame_seconds - queued_duration)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._generation:
            await self.abort_generation(self._generation.generation)
        if self._publisher_task:
            self._publisher_task.cancel()
            await asyncio.gather(self._publisher_task, return_exceptions=True)
            self._publisher_task = None
        if self.connection:
            self.adapter.clear_queue(self.connection)
            await self.adapter.disconnect(self.connection)
            self.connection = None


class LiveKitAudioRegistry:
    def __init__(self, *, adapter_factory: Any | None = None) -> None:
        self._adapter_factory = adapter_factory or LiveKitSDKAdapter
        self._publishers: dict[str, LiveKitRoomAudioPublisher] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock: asyncio.Lock | None = None
        self._registry_lock = threading.RLock()

    def _ensure_loop(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            if self._publishers:
                raise RuntimeError("LiveKit publishers cannot move between event loops")
            self._loop = loop
            self._lock = asyncio.Lock()
        if self._lock is None:
            raise RuntimeError("LiveKit registry lock initialization failed")
        return self._lock

    async def ensure_room(self, room_code: str) -> LiveKitRoomAudioPublisher:
        lock = self._ensure_loop()
        async with lock:
            with self._registry_lock:
                publisher = self._publishers.get(room_code)
            if publisher is None:
                publisher = LiveKitRoomAudioPublisher(room_code, adapter=self._adapter_factory())
                with self._registry_lock:
                    self._publishers[room_code] = publisher
                try:
                    await publisher.start()
                except BaseException:
                    with self._registry_lock:
                        self._publishers.pop(room_code, None)
                    raise
            return publisher

    async def start_generation(
        self,
        room_code: str,
        speech_id: str,
        generation: str,
        sample_rate: int,
        *,
        start_buffer_ms: int | None = None,
    ) -> str:
        publisher = await self.ensure_room(room_code)
        return await publisher.start_generation(
            speech_id,
            generation,
            sample_rate,
            start_buffer_ms=start_buffer_ms,
        )

    async def write_pcm(self, room_code: str, generation: str, pcm16: bytes) -> str | None:
        publisher = await self.ensure_room(room_code)
        return await publisher.write_pcm(generation, pcm16)

    async def finish_generation(self, room_code: str, generation: str) -> str | None:
        publisher = await self.ensure_room(room_code)
        return await publisher.finish_generation(generation)

    async def abort_generation(self, room_code: str, generation: str) -> bool:
        with self._registry_lock:
            publisher = self._publishers.get(room_code)
        return await publisher.abort_generation(generation) if publisher else False

    def abort_generation_nowait(self, room_code: str, generation: str) -> bool:
        with self._registry_lock:
            publisher = self._publishers.get(room_code)
        if not publisher:
            return False
        publisher.abort_generation_nowait(generation)
        return True

    async def close_room(self, room_code: str) -> None:
        with self._registry_lock:
            publisher = self._publishers.pop(room_code, None)
        if publisher:
            await publisher.close()

    async def close_inactive_rooms(self, active_room_codes: set[str]) -> None:
        with self._registry_lock:
            stale_codes = sorted(set(self._publishers) - active_room_codes)
        for room_code in stale_codes:
            await self.close_room(room_code)

    async def close(self) -> None:
        with self._registry_lock:
            publishers = list(self._publishers.values())
            self._publishers.clear()
        if publishers:
            await asyncio.gather(*(publisher.close() for publisher in publishers), return_exceptions=True)
        self._loop = None
        self._lock = None


livekit_audio_registry = LiveKitAudioRegistry()
