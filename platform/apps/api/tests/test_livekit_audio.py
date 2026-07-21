from __future__ import annotations

import asyncio
import uuid
from array import array
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import jwt
import pytest
from app.core.config import Settings, settings
from app.core.database import SessionLocal
from app.services import livekit_audio as livekit_audio_module
from app.services import match_engine as match_engine_module
from app.services.livekit_audio import (
    LiveKitAudioRegistry,
    LiveKitConnection,
    LiveKitSDKAdapter,
    Pcm20msFramer,
    StreamingPcm16Resampler,
    livekit_audio_registry,
)
from app.services.room_service import load_room
from conftest import csrf
from fastapi.testclient import TestClient


class FakeLiveKitAdapter:
    instances: list[FakeLiveKitAdapter] = []

    def __init__(self) -> None:
        self.room_code = ""
        self.frames: list[bytes] = []
        self.clear_calls = 0
        self.wait_calls = 0
        self.disconnected = False
        self.__class__.instances.append(self)

    async def connect(self, room_code: str) -> LiveKitConnection:
        self.room_code = room_code
        return LiveKitConnection(room=SimpleNamespace(), source=SimpleNamespace(), track_sid=f"TR_{room_code}")

    async def capture_frame(self, _connection: LiveKitConnection, pcm16: bytes) -> None:
        self.frames.append(pcm16)
        await asyncio.sleep(0)

    def clear_queue(self, _connection: LiveKitConnection) -> None:
        self.clear_calls += 1

    async def wait_for_playout(self, _connection: LiveKitConnection) -> None:
        self.wait_calls += 1
        await asyncio.sleep(0)

    @staticmethod
    def queued_duration(_connection: LiveKitConnection) -> float:
        return 0.0

    async def disconnect(self, _connection: LiveKitConnection) -> None:
        self.disconnected = True


class BlockingCaptureAdapter(FakeLiveKitAdapter):
    """Models an SDK capture that yields after validation but before enqueue."""

    def __init__(self) -> None:
        super().__init__()
        self.capture_started = asyncio.Event()
        self.release_capture = asyncio.Event()
        self.source_queue: list[bytes] = []

    async def capture_frame(self, _connection: LiveKitConnection, pcm16: bytes) -> None:
        if any(pcm16):
            self.capture_started.set()
            await self.release_capture.wait()
        self.frames.append(pcm16)
        self.source_queue.append(pcm16)

    def clear_queue(self, _connection: LiveKitConnection) -> None:
        self.clear_calls += 1
        self.source_queue.clear()


class BlockingPlayoutAdapter(FakeLiveKitAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.playout_started = asyncio.Event()
        self.playout_cancelled = asyncio.Event()

    async def wait_for_playout(self, _connection: LiveKitConnection) -> None:
        self.wait_calls += 1
        self.playout_started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.playout_cancelled.set()
            raise


class NativeQueuePacingAdapter(FakeLiveKitAdapter):
    """Models AudioSource's queue: fast fill, then one frame of backpressure."""

    def __init__(self) -> None:
        super().__init__()
        self.source_queue: list[bytes] = []
        self.capacity = settings.livekit_audio_source_queue_ms // settings.livekit_audio_frame_ms

    async def capture_frame(self, _connection: LiveKitConnection, pcm16: bytes) -> None:
        if len(self.source_queue) >= self.capacity:
            await asyncio.sleep(settings.livekit_audio_frame_ms / 1_000)
            self.source_queue.pop(0)
        self.frames.append(pcm16)
        self.source_queue.append(pcm16)

    def clear_queue(self, _connection: LiveKitConnection) -> None:
        self.clear_calls += 1
        self.source_queue.clear()

    def queued_duration(self, _connection: LiveKitConnection) -> float:
        return len(self.source_queue) * settings.livekit_audio_frame_ms / 1_000


def pcm(samples: list[int]) -> bytes:
    return array("h", samples).tobytes()


def test_streaming_resampler_is_chunk_boundary_invariant() -> None:
    source = [round(12_000 * ((index % 97) / 96 - 0.5)) for index in range(3_007)]
    one_shot = StreamingPcm16Resampler(24_000)
    expected = one_shot.push(pcm(source)) + one_shot.finish()

    chunked = StreamingPcm16Resampler(24_000)
    chunks = [source[:1], source[1:19], source[19:777], source[777:778], source[778:2_503], source[2_503:]]
    actual = b"".join(chunked.push(pcm(chunk)) for chunk in chunks) + chunked.finish()

    assert actual == expected
    assert abs(len(actual) // 2 - round(len(source) * 48_000 / 24_000)) <= 1


def test_pcm_framer_emits_exact_20ms_frames_and_pads_only_the_tail() -> None:
    framer = Pcm20msFramer()
    frames = framer.feed(b"\x01\x00" * 1_921)
    tail = framer.finish()
    assert [len(item) for item in frames] == [1_920, 1_920]
    assert len(tail) == 1 and len(tail[0]) == 1_920
    assert tail[0][:2] == b"\x01\x00" and set(tail[0][2:]) == {0}
    with pytest.raises(ValueError, match="complete samples"):
        Pcm20msFramer().feed(b"\x00")


@pytest.mark.asyncio
async def test_sdk_publisher_is_visible_so_subscribers_receive_participant_before_track(monkeypatch) -> None:
    from livekit import rtc

    token_arguments: dict[str, object] = {}

    def create_token(**kwargs) -> str:
        token_arguments.update(kwargs)
        return "publisher-token"

    published_options: dict[str, object] = {}

    class FakeParticipant:
        async def publish_track(self, _track, options):
            published_options.update({
                "max_bitrate": options.audio_encoding.max_bitrate,
                "dtx": options.dtx,
                "red": options.red,
            })
            return SimpleNamespace(sid="TR_visible")

    class FakeRoom:
        def __init__(self) -> None:
            self.local_participant = FakeParticipant()

        async def connect(self, _url, _token, _options) -> None:
            return None

    monkeypatch.setattr(livekit_audio_module, "create_livekit_token", create_token)
    monkeypatch.setattr(rtc, "Room", FakeRoom)
    monkeypatch.setattr(rtc, "RoomOptions", lambda **kwargs: kwargs)
    monkeypatch.setattr(rtc, "AudioSource", lambda *_args, **_kwargs: SimpleNamespace())
    monkeypatch.setattr(
        rtc,
        "LocalAudioTrack",
        SimpleNamespace(create_audio_track=lambda *_args: SimpleNamespace()),
    )
    monkeypatch.setattr(
        rtc,
        "TrackPublishOptions",
        lambda: SimpleNamespace(source=None, audio_encoding=SimpleNamespace(max_bitrate=0), dtx=False, red=False),
    )
    monkeypatch.setattr(rtc, "TrackSource", SimpleNamespace(SOURCE_MICROPHONE="microphone"))

    connection = await LiveKitSDKAdapter().connect("visible-room")

    assert connection.track_sid == "TR_visible"
    assert token_arguments["hidden"] is False
    assert published_options == {"max_bitrate": 64_000, "dtx": False, "red": True}


@pytest.mark.asyncio
async def test_room_publisher_uses_bounded_queues_drains_and_rejects_revoked_generation(monkeypatch) -> None:
    FakeLiveKitAdapter.instances.clear()
    monkeypatch.setattr(settings, "livekit_audio_source_queue_ms", 100)
    monkeypatch.setattr(settings, "livekit_audio_app_queue_ms", 120)
    registry = LiveKitAudioRegistry(adapter_factory=FakeLiveKitAdapter)
    generation = uuid.uuid4().hex

    publisher = await registry.ensure_room("123456")
    assert publisher.queue.maxsize == 6
    track_sid = await registry.start_generation("123456", "speech-1", generation, 24_000)
    first_capture_at = await registry.write_pcm("123456", generation, pcm([8_000] * 960))
    await registry.finish_generation("123456", generation)

    adapter = FakeLiveKitAdapter.instances[0]
    assert track_sid == "TR_123456"
    assert first_capture_at
    assert adapter.wait_calls == 1
    assert all(len(frame) == 1_920 for frame in adapter.frames)
    assert any(any(frame) for frame in adapter.frames)

    revoked = uuid.uuid4().hex
    await registry.start_generation("123456", "speech-2", revoked, 24_000)
    assert await registry.abort_generation("123456", revoked) is True
    assert await registry.write_pcm("123456", revoked, pcm([9_000] * 960)) is None
    assert adapter.clear_calls >= 3
    await registry.close()
    assert adapter.disconnected is True


@pytest.mark.asyncio
async def test_moss_start_buffer_holds_1200ms_then_plays_without_early_silence(monkeypatch) -> None:
    FakeLiveKitAdapter.instances.clear()
    monkeypatch.setattr(settings, "livekit_audio_source_queue_ms", 40)
    monkeypatch.setattr(settings, "livekit_audio_app_queue_ms", 2_400)
    registry = LiveKitAudioRegistry(adapter_factory=FakeLiveKitAdapter)
    generation = uuid.uuid4().hex
    sample_rate = 24_000

    await registry.start_generation(
        "buffer-room",
        "buffer-speech",
        generation,
        sample_rate,
        start_buffer_ms=1_200,
    )
    adapter = FakeLiveKitAdapter.instances[0]
    for duration_ms in (240, 320, 400):
        result = await registry.write_pcm(
            "buffer-room",
            generation,
            pcm([6_000] * (sample_rate * duration_ms // 1_000)),
        )
        assert result is None
    await asyncio.sleep(settings.livekit_audio_frame_ms / 1_000 * 2)
    assert adapter.frames and all(not any(frame) for frame in adapter.frames)

    first_capture_at = await asyncio.wait_for(
        registry.write_pcm(
            "buffer-room",
            generation,
            pcm([6_000] * (sample_rate * 240 // 1_000)),
        ),
        timeout=0.5,
    )
    assert first_capture_at
    await registry.finish_generation("buffer-room", generation)

    first_voice = next(index for index, frame in enumerate(adapter.frames) if any(frame))
    last_voice = max(index for index, frame in enumerate(adapter.frames) if any(frame))
    assert all(any(frame) for frame in adapter.frames[first_voice : last_voice + 1])
    await registry.close()


@pytest.mark.asyncio
async def test_active_moss_generation_does_not_queue_silence_between_slow_pcm_bursts(monkeypatch) -> None:
    FakeLiveKitAdapter.instances.clear()
    monkeypatch.setattr(settings, "livekit_audio_source_queue_ms", 40)
    monkeypatch.setattr(settings, "livekit_audio_app_queue_ms", 400)
    registry = LiveKitAudioRegistry(adapter_factory=FakeLiveKitAdapter)
    generation = uuid.uuid4().hex

    await registry.start_generation(
        "bursty-room",
        "bursty-speech",
        generation,
        24_000,
        start_buffer_ms=40,
    )
    assert await asyncio.wait_for(
        registry.write_pcm("bursty-room", generation, pcm([6_000] * 960)),
        timeout=0.5,
    )
    # This is longer than one frame and used to make the publisher enqueue
    # several audible silence frames ahead of the next real PCM burst.
    await asyncio.sleep(0.08)
    await registry.write_pcm("bursty-room", generation, pcm([7_000] * 960))
    await registry.finish_generation("bursty-room", generation)

    frames = FakeLiveKitAdapter.instances[0].frames
    first_voice = next(index for index, frame in enumerate(frames) if any(frame))
    last_voice = max(index for index, frame in enumerate(frames) if any(frame))
    assert all(any(frame) for frame in frames[first_voice : last_voice + 1])
    await registry.close()


@pytest.mark.asyncio
async def test_active_pcm_quickly_refills_native_source_queue_without_second_clock(monkeypatch) -> None:
    NativeQueuePacingAdapter.instances.clear()
    monkeypatch.setattr(settings, "livekit_audio_source_queue_ms", 100)
    monkeypatch.setattr(settings, "livekit_audio_app_queue_ms", 400)
    registry = LiveKitAudioRegistry(adapter_factory=NativeQueuePacingAdapter)
    generation = uuid.uuid4().hex

    await registry.start_generation(
        "native-queue-room",
        "native-queue-speech",
        generation,
        24_000,
        start_buffer_ms=40,
    )
    assert await registry.write_pcm(
        "native-queue-room",
        generation,
        pcm([6_000] * (24_000 * 240 // 1_000)),
    )
    await asyncio.sleep(0.005)

    adapter = NativeQueuePacingAdapter.instances[0]
    assert len(adapter.source_queue) == adapter.capacity
    assert all(any(frame) for frame in adapter.source_queue)
    await asyncio.wait_for(registry.abort_generation("native-queue-room", generation), timeout=0.2)
    await registry.close()


@pytest.mark.asyncio
async def test_moss_short_generation_flushes_before_start_buffer_threshold(monkeypatch) -> None:
    FakeLiveKitAdapter.instances.clear()
    monkeypatch.setattr(settings, "livekit_audio_source_queue_ms", 40)
    monkeypatch.setattr(settings, "livekit_audio_app_queue_ms", 1_200)
    registry = LiveKitAudioRegistry(adapter_factory=FakeLiveKitAdapter)
    generation = uuid.uuid4().hex

    await registry.start_generation(
        "short-room",
        "short-speech",
        generation,
        24_000,
        start_buffer_ms=800,
    )
    assert await registry.write_pcm("short-room", generation, pcm([7_000] * 7_200)) is None
    adapter = FakeLiveKitAdapter.instances[0]
    await asyncio.sleep(settings.livekit_audio_frame_ms / 1_000 * 2)
    assert adapter.frames and all(not any(frame) for frame in adapter.frames)

    first_capture_at = await asyncio.wait_for(
        registry.finish_generation("short-room", generation),
        timeout=0.5,
    )
    assert first_capture_at
    assert any(any(frame) for frame in adapter.frames)
    await registry.close()


@pytest.mark.asyncio
async def test_abort_discards_moss_pcm_held_inside_start_buffer(monkeypatch) -> None:
    FakeLiveKitAdapter.instances.clear()
    monkeypatch.setattr(settings, "livekit_audio_source_queue_ms", 40)
    monkeypatch.setattr(settings, "livekit_audio_app_queue_ms", 1_200)
    registry = LiveKitAudioRegistry(adapter_factory=FakeLiveKitAdapter)
    generation = uuid.uuid4().hex

    await registry.start_generation(
        "buffer-abort-room",
        "buffer-abort-speech",
        generation,
        24_000,
        start_buffer_ms=800,
    )
    assert await registry.write_pcm("buffer-abort-room", generation, pcm([9_000] * 9_600)) is None
    assert await registry.abort_generation("buffer-abort-room", generation) is True
    await asyncio.sleep(settings.livekit_audio_frame_ms / 1_000 * 3)

    adapter = FakeLiveKitAdapter.instances[0]
    assert adapter.frames and all(not any(frame) for frame in adapter.frames)
    assert await registry.write_pcm("buffer-abort-room", generation, pcm([9_000] * 960)) is None
    await registry.close()


@pytest.mark.asyncio
async def test_non_moss_generation_keeps_immediate_pcm_after_existing_sdk_lead(monkeypatch) -> None:
    FakeLiveKitAdapter.instances.clear()
    monkeypatch.setattr(settings, "livekit_audio_source_queue_ms", 100)
    registry = LiveKitAudioRegistry(adapter_factory=FakeLiveKitAdapter)
    generation = uuid.uuid4().hex

    await registry.start_generation("legacy-room", "legacy-speech", generation, 24_000)
    first_capture_at = await asyncio.wait_for(
        registry.write_pcm("legacy-room", generation, pcm([5_000] * 960)),
        timeout=0.5,
    )
    assert first_capture_at
    adapter = FakeLiveKitAdapter.instances[0]
    first_voice = next(index for index, frame in enumerate(adapter.frames) if any(frame))
    assert first_voice == settings.livekit_audio_source_queue_ms // settings.livekit_audio_frame_ms
    await registry.abort_generation("legacy-room", generation)
    await registry.close()


@pytest.mark.asyncio
async def test_abort_cannot_let_an_inflight_old_generation_frame_reappear_after_sdk_clear(monkeypatch) -> None:
    BlockingCaptureAdapter.instances.clear()
    monkeypatch.setattr(settings, "livekit_audio_source_queue_ms", 40)
    registry = LiveKitAudioRegistry(adapter_factory=BlockingCaptureAdapter)
    generation = uuid.uuid4().hex

    await registry.start_generation("race-room", "speech-old", generation, 24_000)
    write = asyncio.create_task(
        registry.write_pcm("race-room", generation, pcm([12_000] * 960))
    )
    adapter = BlockingCaptureAdapter.instances[0]
    await asyncio.wait_for(adapter.capture_started.wait(), timeout=0.5)

    abort = asyncio.create_task(registry.abort_generation("race-room", generation))
    await asyncio.sleep(0)
    assert not abort.done(), "abort must serialize with the in-flight SDK capture"

    adapter.release_capture.set()
    assert await asyncio.wait_for(abort, timeout=0.5) is True
    await asyncio.gather(write, return_exceptions=True)
    await asyncio.sleep(settings.livekit_audio_frame_ms / 1000 * 2)

    # The blocked old frame may complete before abort, but abort's ordered SDK
    # clear must remove it.  Only the publisher's continuous-clock silence may
    # be queued after the interrupt.
    assert adapter.clear_calls >= 2
    assert adapter.source_queue
    assert all(not any(frame) for frame in adapter.source_queue)
    assert await registry.write_pcm("race-room", generation, pcm([9_000] * 960)) is None
    await registry.close()


@pytest.mark.asyncio
async def test_abort_cancels_old_drain_so_finish_cannot_wait_on_the_next_generation(monkeypatch) -> None:
    BlockingPlayoutAdapter.instances.clear()
    monkeypatch.setattr(settings, "livekit_audio_source_queue_ms", 40)
    registry = LiveKitAudioRegistry(adapter_factory=BlockingPlayoutAdapter)
    old_generation = uuid.uuid4().hex

    await registry.start_generation("drain-room", "speech-old", old_generation, 24_000)
    assert await registry.write_pcm("drain-room", old_generation, pcm([7_000] * 960))
    finishing = asyncio.create_task(registry.finish_generation("drain-room", old_generation))
    adapter = BlockingPlayoutAdapter.instances[0]
    await asyncio.wait_for(adapter.playout_started.wait(), timeout=0.5)

    assert await registry.abort_generation("drain-room", old_generation) is True
    next_generation = uuid.uuid4().hex
    await registry.start_generation("drain-room", "speech-next", next_generation, 24_000)

    await asyncio.wait_for(finishing, timeout=0.5)
    await asyncio.wait_for(adapter.playout_cancelled.wait(), timeout=0.5)
    assert registry._publishers["drain-room"].active_generation == next_generation
    await registry.abort_generation("drain-room", next_generation)
    await registry.close()


@pytest.mark.asyncio
async def test_registry_isolates_concurrent_rooms(monkeypatch) -> None:
    FakeLiveKitAdapter.instances.clear()
    monkeypatch.setattr(settings, "livekit_audio_source_queue_ms", 100)
    registry = LiveKitAudioRegistry(adapter_factory=FakeLiveKitAdapter)
    generations = {"111111": uuid.uuid4().hex, "222222": uuid.uuid4().hex}

    async def publish(room_code: str, value: int) -> None:
        generation = generations[room_code]
        await registry.start_generation(room_code, f"speech-{room_code}", generation, 24_000)
        await registry.write_pcm(room_code, generation, pcm([value] * 960))
        await registry.finish_generation(room_code, generation)

    await asyncio.gather(publish("111111", 3_000), publish("222222", 12_000))
    by_room = {adapter.room_code: adapter for adapter in FakeLiveKitAdapter.instances}
    first_voice = {
        room_code: next(frame for frame in adapter.frames if any(frame))
        for room_code, adapter in by_room.items()
    }
    assert first_voice["111111"] != first_voice["222222"]
    await registry.close_inactive_rooms({"111111"})
    assert by_room["222222"].disconnected is True
    assert by_room["111111"].disconnected is False
    await registry.close()


def create_public_room(client: TestClient) -> dict:
    response = client.post(
        "/api/rooms",
        headers=csrf(client),
        json={
            "competition_slug": "training-1v1",
            "custom_topic": "LiveKit token 权限测试",
            "seat_key": "aff_1",
            "visibility": "public",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["room"]


def test_rtc_token_is_short_lived_subscribe_only_and_checks_room_visibility(client, register_user, monkeypatch) -> None:
    owner = register_user("rtc_token")
    room = create_public_room(owner)
    with SessionLocal() as db:
        stored = load_room(db, room["code"], lock=True)
        stored.status = "running"
        db.commit()
    ensure_room = AsyncMock(side_effect=AssertionError("API process must not create the Agent publisher"))
    monkeypatch.setattr(livekit_audio_registry, "ensure_room", ensure_room)
    monkeypatch.setattr(settings, "webrtc_audio_enabled", True)
    monkeypatch.setattr(settings, "webrtc_audio_backend", "livekit")
    monkeypatch.setattr(settings, "livekit_url", "ws://127.0.0.1:7880")
    monkeypatch.setattr(settings, "livekit_public_url", "wss://rtc.test.invalid")
    monkeypatch.setattr(settings, "livekit_api_key", "test-key")
    monkeypatch.setattr(settings, "livekit_api_secret", "test-secret-with-enough-entropy-32")
    monkeypatch.setattr(settings, "livekit_token_ttl_seconds", 300)

    response = owner.post(f"/api/rooms/{room['code']}/rtc-token", headers={"X-Device-ID": "chrome-qa"})
    assert response.status_code == 200, response.text
    body = response.json()
    claims = jwt.decode(body["token"], "test-secret-with-enough-entropy-32", algorithms=["HS256"], audience=None)
    grants = claims["video"]
    assert body["enabled"] is True and body["backend"] == "livekit"
    assert body["url"] == "wss://rtc.test.invalid"
    assert body["room_name"] == f"debate:{room['code']}"
    assert body["expires_in"] == 300
    assert grants["roomJoin"] is True and grants["room"] == body["room_name"]
    assert grants["canSubscribe"] is True
    assert grants["canPublish"] is False
    assert grants["canPublishData"] is False
    assert claims["exp"] - claims["nbf"] == 300
    assert claims["sub"].endswith(":device:chrome-qa")
    ensure_room.assert_not_awaited()

    anonymous = client.post(f"/api/rooms/{room['code']}/rtc-token")
    assert anonymous.status_code == 403
    assert anonymous.json()["detail"] == "请先进入观战页面并建立实时连接。"

    with pytest.raises(ValueError, match="URL、API key"):
        Settings(
            webrtc_audio_enabled=True,
            webrtc_audio_backend="livekit",
            livekit_url="",
            livekit_api_key="",
            livekit_api_secret="",
        )


def test_rtc_token_stays_unavailable_while_feature_flag_is_off(register_user, monkeypatch) -> None:
    monkeypatch.setattr(settings, "webrtc_audio_enabled", False)
    monkeypatch.setattr(settings, "webrtc_audio_backend", "websocket_pcm")
    owner = register_user("rtc_token_off")
    room = create_public_room(owner)
    response = owner.post(f"/api/rooms/{room['code']}/rtc-token")
    assert response.status_code == 200
    assert response.json() == {
        "enabled": False,
        "backend": "websocket_pcm",
        "url": None,
        "token": None,
        "room_name": None,
        "expires_in": 0,
    }


def test_livekit_public_url_falls_back_to_engine_url() -> None:
    configured = Settings(livekit_url="ws://127.0.0.1:7880", livekit_public_url="")
    assert configured.livekit_browser_url == "ws://127.0.0.1:7880"


def test_moss_livekit_start_buffer_configuration_is_frame_aligned_and_fits_app_queue() -> None:
    configured = Settings()
    assert configured.moss_tts_livekit_start_buffer_ms == 1_200
    assert configured.livekit_audio_app_queue_ms == 2_400
    assert configured.livekit_audio_source_queue_ms == 200
    assert configured.livekit_audio_max_bitrate_bps == 64_000
    assert configured.livekit_audio_dtx_enabled is False
    with pytest.raises(ValueError, match="整数倍"):
        Settings(moss_tts_livekit_start_buffer_ms=810)


@pytest.mark.asyncio
async def test_generation_rejects_start_buffer_larger_than_bounded_app_queue(monkeypatch) -> None:
    FakeLiveKitAdapter.instances.clear()
    monkeypatch.setattr(settings, "livekit_audio_app_queue_ms", 780)
    registry = LiveKitAudioRegistry(adapter_factory=FakeLiveKitAdapter)
    with pytest.raises(ValueError, match="fit inside"):
        await registry.start_generation(
            "small-queue-room",
            "small-queue-speech",
            uuid.uuid4().hex,
            24_000,
            start_buffer_ms=800,
        )
    await registry.close()


def test_match_interrupt_revokes_livekit_generation_and_emits_flush_guard(monkeypatch) -> None:
    abort_generation = Mock(return_value=True)
    append_event = Mock()
    monkeypatch.setattr(settings, "webrtc_audio_enabled", True)
    monkeypatch.setattr(settings, "webrtc_audio_backend", "livekit")
    monkeypatch.setattr(match_engine_module.livekit_audio_registry, "abort_generation_nowait", abort_generation)
    monkeypatch.setattr(match_engine_module.audio_stream_aborts, "abort", Mock())
    monkeypatch.setattr(match_engine_module, "append_event", append_event)
    room = SimpleNamespace(code="654321")
    speech = SimpleNamespace(id="speech-rtc", stream_generation="generation-rtc", stream_sample_rate=24_000)

    match_engine_module.MatchEngine._abort_stream(Mock(), room, speech, reason="manual_pause")

    abort_generation.assert_called_once_with("654321", "generation-rtc")
    assert speech.stream_generation == "" and speech.stream_sample_rate == 0
    event_args = append_event.call_args.args
    assert event_args[2] == "audio.rtc.interrupt"
    assert event_args[3]["generation"] == "generation-rtc"
    assert event_args[3]["reason"] == "manual_pause"
    assert event_args[3]["flush_guard_ms"] == 220


@pytest.mark.asyncio
async def test_engine_owns_prewarm_and_terminal_room_reconciliation(monkeypatch) -> None:
    ensure_room = AsyncMock()
    close_inactive = AsyncMock()
    monkeypatch.setattr(settings, "webrtc_audio_enabled", True)
    monkeypatch.setattr(settings, "webrtc_audio_backend", "livekit")
    monkeypatch.setattr(match_engine_module.livekit_audio_registry, "ensure_room", ensure_room)
    monkeypatch.setattr(match_engine_module.livekit_audio_registry, "close_inactive_rooms", close_inactive)

    await match_engine_module.MatchEngine()._reconcile_livekit_publishers({"active-room", "paused-room"})

    assert {call.args[0] for call in ensure_room.await_args_list} == {"active-room", "paused-room"}
    close_inactive.assert_awaited_once_with({"active-room", "paused-room"})
