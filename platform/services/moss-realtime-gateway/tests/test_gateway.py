from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

from moss_realtime_gateway.app import create_app
from moss_realtime_gateway.backends import (
    CanaryPcmPayload,
    FakeBackend,
    FakeTurn,
    OpenMossCudaBackend,
    OpenMossTurn,
    gpu_total_memory_meets_floor,
)
from moss_realtime_gateway.config import OPENMOSS_UPSTREAM_REVISION, GatewaySettings, PromptRegistry
from moss_realtime_gateway.low_latency_bridge import CanaryAudioChunk, CanaryStageTiming
from moss_realtime_gateway.runtime import GatewayError, GatewayRuntime, SessionOrphaned
from moss_realtime_gateway.websocket_protocol import WebSocketSender, _audio_pump, _cancel_audio


def settings(tmp_path: Path, **overrides) -> GatewaySettings:
    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir(exist_ok=True)
    for index in range(1, 9):
        (prompt_dir / f"voice-{index}.wav").write_bytes(b"RIFFfake")
    values = {
        "backend": "fake",
        "prompt_dir": prompt_dir,
        "voice_prompts": {f"debate_voice_{index}": f"voice-{index}.wav" for index in range(1, 9)},
        "require_eight_prompts": True,
        "control_ack_timeout_seconds": 1.0,
        "terminal_grace_seconds": 1.0,
        "startup_timeout_seconds": 2.0,
    }
    values.update(overrides)
    return GatewaySettings(**values)


def auth(value: str = "secret") -> dict[str, str]:
    return {"X-MOSS-Gateway-Key": value}


def receive_ws_frame(websocket) -> dict | bytes:
    message = websocket.receive()
    if message["type"] == "websocket.close":
        raise WebSocketDisconnect(message.get("code", 1000), message.get("reason", ""))
    if message.get("bytes") is not None:
        return message["bytes"]
    return json.loads(message["text"])


def receive_until_json(websocket, message_type: str) -> tuple[dict, list[dict | bytes]]:
    frames: list[dict | bytes] = []
    while True:
        frame = receive_ws_frame(websocket)
        frames.append(frame)
        if isinstance(frame, dict) and frame.get("type") == message_type:
            return frame, frames


def start_websocket_turn(websocket, session_id: str) -> dict:
    websocket.send_json(
        {
            "type": "start",
            "seq": 0,
            "session_id": session_id,
            "voice": "debate_voice_1",
            "user_text": "普通话辩论",
        }
    )
    ready = websocket.receive_json()
    assert ready["type"] == "ready" and ready["session_id"] == session_id
    assert ready["next_seq"] == 1
    assert ready["audio"] == {"codec": "pcm_s16le", "sample_rate": 24_000, "channels": 1}
    return ready


def test_websocket_auth_ping_health_and_abort_release(tmp_path: Path) -> None:
    configured = settings(tmp_path, api_key="secret")
    application = create_app(configured, backend=FakeBackend())
    with TestClient(application) as client:
        with pytest.raises(WebSocketDenialResponse) as missing:
            with client.websocket_connect("/tts/session/ws"):
                pass
        assert missing.value.status_code == 401
        with pytest.raises(WebSocketDenialResponse) as wrong:
            with client.websocket_connect("/tts/session/ws", headers=auth("wrong")):
                pass
        assert wrong.value.status_code == 401

        with client.websocket_connect("/tts/session/ws", headers=auth()) as websocket:
            websocket.send_json({"type": "ping", "id": "probe-1"})
            assert websocket.receive_json() == {"type": "pong", "id": "probe-1"}
            websocket.send_json({"type": "health"})
            health = websocket.receive_json()
            assert health["type"] == "health" and health["ok"] is True and health["active"] == 0
            assert "secret" not in str(health)
            start_websocket_turn(websocket, "ws-auth")
            websocket.send_json({"type": "abort", "reason": "test_complete"})
            assert websocket.receive_json() == {"type": "audio_reset", "session_id": "ws-auth"}
            released = websocket.receive_json()
            assert released["type"] == "released" and released["released"] is True
            assert released["status"] == "aborted"


def test_websocket_pcm_arrives_before_final_and_delta_sequence_is_preserved(tmp_path: Path) -> None:
    application = create_app(settings(tmp_path), backend=FakeBackend())
    with TestClient(application) as client:
        with client.websocket_connect("/tts/session/ws") as websocket:
            start_websocket_turn(websocket, "ws-stream")
            accepted_sequences: list[int] = []
            pcm_frames: list[bytes] = []
            for seq, text in ((1, "第一段正文"), (2, "第二段正文")):
                websocket.send_json({"type": "text_delta", "seq": seq, "text": text})
                accepted, frames = receive_until_json(websocket, "ack")
                accepted_sequences.append(accepted["seq"])
                pcm_frames.extend(frame for frame in frames if isinstance(frame, bytes))
                if not any(isinstance(frame, bytes) for frame in frames):
                    pcm_frames.append(websocket.receive_bytes())
            assert accepted_sequences == [1, 2]
            assert pcm_frames and all(frame and len(frame) % 2 == 0 for frame in pcm_frames)

            websocket.send_json({"type": "final", "seq": 3})
            released, terminal_frames = receive_until_json(websocket, "released")
            terminal_types = [frame.get("type") for frame in terminal_frames if isinstance(frame, dict)]
            assert "ack" in terminal_types and "audio_end" in terminal_types
            assert any(isinstance(frame, dict) and frame == {"type": "ack", "seq": 3} for frame in terminal_frames)
            assert terminal_types.index("ack") < terminal_types.index("audio_end")
            assert terminal_types.index("audio_end") < terminal_types.index("released")
            audio_end_index = next(
                index
                for index, frame in enumerate(terminal_frames)
                if isinstance(frame, dict) and frame.get("type") == "audio_end"
            )
            assert any(isinstance(frame, bytes) for frame in terminal_frames[:audio_end_index])
            assert not any(isinstance(frame, bytes) for frame in terminal_frames[audio_end_index + 1 :])
            assert released["released"] is True and released["status"] == "closed"


def test_websocket_canary_stage_observation_is_opt_in_numeric_and_pcm_ordered(tmp_path: Path) -> None:
    class ObservedTurn(FakeTurn):
        def push_text(self, text: str):
            for payload in super().push_text(text):
                timing = CanaryStageTiming("talker_step", 0, 12.5, 13.0)
                observation = CanaryAudioChunk(
                    audio=None,
                    source_stage="talker_step",
                    source_stage_index=0,
                    source_stage_duration_ms=12.5,
                    decoder_stage_index=1,
                    decoder_yield_ms=3.25,
                    turn_elapsed_ms=16.25,
                )
                yield timing
                yield CanaryStageTiming("decoder_yield", 1, 3.25, 16.25, "talker_step", 0)
                yield CanaryPcmPayload(payload, observation)

    class ObservedBackend(FakeBackend):
        def open_turn(self, *, prompt_path: Path, user_text: str, initial_text: str):
            del user_text
            return ObservedTurn(self, initial_text)

    application = create_app(
        settings(tmp_path, api_key="canary-secret", canary_stage_observability=True),
        backend=ObservedBackend(),
    )
    with TestClient(application) as client:
        with client.websocket_connect("/tts/session/ws", headers=auth("canary-secret")) as websocket:
            websocket.send_json(
                {
                    "type": "start",
                    "seq": 0,
                    "session_id": "ws-canary-observe",
                    "voice": "debate_voice_1",
                    "user_text": None,
                    "canary_observe": True,
                }
            )
            ready = websocket.receive_json()
            assert ready["canary_observe"] is True
            websocket.send_json({"type": "text_delta", "seq": 1, "text": "不得出现在观测中"})
            ack, frames = receive_until_json(websocket, "ack")
            assert ack["seq"] == 1
            while not any(isinstance(frame, bytes) for frame in frames):
                frames.append(receive_ws_frame(websocket))
            stage = next(frame for frame in frames if isinstance(frame, dict) and frame.get("type") == "canary.stage")
            pcm_meta = next(frame for frame in frames if isinstance(frame, dict) and frame.get("type") == "canary.pcm")
            pcm_index = frames.index(pcm_meta) + 1
            assert isinstance(frames[pcm_index], bytes)
            assert pcm_meta["chunk_index"] == 0
            assert pcm_meta["source_stage"] == "talker_step"
            assert stage["duration_ms"] == 12.5
            serialized = json.dumps([stage, pcm_meta], ensure_ascii=False)
            assert "不得出现在观测中" not in serialized
            assert "canary-secret" not in serialized
            websocket.send_json({"type": "abort", "reason": "done"})
            receive_until_json(websocket, "released")


def test_websocket_canary_metadata_stays_off_without_session_opt_in(tmp_path: Path) -> None:
    class ObservedTurn(FakeTurn):
        def push_text(self, text: str):
            for payload in super().push_text(text):
                yield CanaryStageTiming("prefill", 0, 1.0, 1.0)
                yield CanaryPcmPayload(
                    payload,
                    CanaryAudioChunk(None, "prefill", 0, 1.0, 1, 2.0, 3.0),
                )

    class ObservedBackend(FakeBackend):
        def open_turn(self, *, prompt_path: Path, user_text: str, initial_text: str):
            del user_text
            return ObservedTurn(self, initial_text)

    application = create_app(
        settings(tmp_path, canary_stage_observability=True),
        backend=ObservedBackend(),
    )
    with TestClient(application) as client:
        with client.websocket_connect("/tts/session/ws") as websocket:
            ready = start_websocket_turn(websocket, "ws-canary-default-off")
            assert ready["canary_observe"] is False
            websocket.send_json({"type": "text_delta", "seq": 1, "text": "正文"})
            _ack, frames = receive_until_json(websocket, "ack")
            while not any(isinstance(frame, bytes) for frame in frames):
                frames.append(receive_ws_frame(websocket))
            assert any(isinstance(frame, bytes) for frame in frames)
            assert not any(
                isinstance(frame, dict) and str(frame.get("type", "")).startswith("canary.")
                for frame in frames
            )
            websocket.send_json({"type": "abort", "reason": "done"})
            receive_until_json(websocket, "released")


def test_websocket_ping_and_abort_are_not_blocked_by_slow_delta(tmp_path: Path) -> None:
    backend = FakeBackend(push_delay_seconds=0.15)
    application = create_app(settings(tmp_path), backend=backend)
    with TestClient(application) as client:
        with client.websocket_connect("/tts/session/ws") as websocket:
            start_websocket_turn(websocket, "ws-preempt")
            websocket.send_json({"type": "text_delta", "seq": 1, "text": "一段较慢的正文"})
            websocket.send_json({"type": "ping", "id": "during-push"})
            pong, frames = receive_until_json(websocket, "pong")
            assert pong["id"] == "during-push"
            assert not any(isinstance(frame, dict) and frame.get("type") == "ack" for frame in frames)
            websocket.send_json({"type": "abort", "reason": "interrupt_slow_push"})
            released, release_frames = receive_until_json(websocket, "released")
            assert any(
                isinstance(frame, dict) and frame.get("type") == "audio_reset" for frame in release_frames
            )
            reset_index = next(
                index
                for index, frame in enumerate(release_frames)
                if isinstance(frame, dict) and frame.get("type") == "audio_reset"
            )
            assert not any(isinstance(frame, bytes) for frame in release_frames[reset_index + 1 :])
            assert released["released"] is True and released["status"] == "aborted"
            session = application.state.runtime._require_session("ws-preempt")
            assert session.audio_queue.empty() and session.audio_ended.is_set()
            assert backend.abort_called.is_set() and backend.context_exited.is_set()


@pytest.mark.parametrize(
    ("payload", "error_code"),
    [
        ({"type": "text_delta", "seq": 2, "text": "跳号"}, "invalid_seq"),
        ({"type": "text_delta", "seq": 1, "text": "正文", "thinking": "内部推理"}, "thinking_not_allowed"),
        ({"type": "text_delta", "seq": 1, "text": "超" * 4_097}, "text_too_long"),
    ],
)
def test_websocket_rejects_invalid_seq_thinking_and_long_text(
    tmp_path: Path,
    payload: dict,
    error_code: str,
) -> None:
    application = create_app(settings(tmp_path), backend=FakeBackend())
    with TestClient(application) as client:
        with client.websocket_connect("/tts/session/ws") as websocket:
            start_websocket_turn(websocket, f"ws-invalid-{error_code}")
            websocket.send_json(payload)
            error, frames = receive_until_json(websocket, "error")
            assert error["code"] == error_code
            released, release_frames = receive_until_json(websocket, "released")
            all_frames = frames + release_frames
            assert any(isinstance(frame, dict) and frame.get("type") == "audio_reset" for frame in all_frames)
            assert released["released"] is True and released["status"] == "aborted"


def test_websocket_rejects_duplicate_delta_sequence(tmp_path: Path) -> None:
    application = create_app(settings(tmp_path), backend=FakeBackend())
    with TestClient(application) as client:
        with client.websocket_connect("/tts/session/ws") as websocket:
            start_websocket_turn(websocket, "ws-duplicate")
            websocket.send_json({"type": "text_delta", "seq": 1, "text": "只应合成一次"})
            _accepted, first_frames = receive_until_json(websocket, "ack")
            if not any(isinstance(frame, bytes) for frame in first_frames):
                websocket.receive_bytes()
            websocket.send_json({"type": "text_delta", "seq": 1, "text": "重复序号"})
            error, _frames = receive_until_json(websocket, "error")
            assert error["code"] == "invalid_seq"
            released, _release_frames = receive_until_json(websocket, "released")
            assert released["released"] is True and released["status"] == "aborted"


def test_websocket_requires_start_sequence_zero(tmp_path: Path) -> None:
    application = create_app(settings(tmp_path), backend=FakeBackend())
    with TestClient(application) as client:
        with client.websocket_connect("/tts/session/ws") as websocket:
            websocket.send_json(
                {
                    "type": "start",
                    "session_id": "missing-start-seq",
                    "voice": "debate_voice_1",
                }
            )
            error = websocket.receive_json()
            assert error["type"] == "error" and error["code"] == "invalid_message"


def test_websocket_final_abort_race_has_one_released_ack_and_no_orphan(tmp_path: Path) -> None:
    finish_release = threading.Event()
    backend = FakeBackend(finish_block=finish_release)
    application = create_app(settings(tmp_path, terminal_grace_seconds=1.0), backend=backend)
    with TestClient(application) as client:
        with client.websocket_connect("/tts/session/ws") as websocket:
            start_websocket_turn(websocket, "ws-terminal-race")
            websocket.send_json({"type": "final", "seq": 1})
            websocket.send_json({"type": "abort", "reason": "race_abort"})
            threading.Timer(0.05, finish_release.set).start()
            released, frames = receive_until_json(websocket, "released")
            assert sum(
                isinstance(frame, dict) and frame.get("type") == "released" for frame in frames
            ) == 1
            assert released["released"] is True and released["status"] == "aborted"
            assert application.state.runtime.health()["orphan_count"] == 0


def test_websocket_disconnect_aborts_worker_and_restores_capacity(tmp_path: Path) -> None:
    backend = FakeBackend(push_delay_seconds=0.1)
    application = create_app(settings(tmp_path), backend=backend)
    with TestClient(application) as client:
        with client.websocket_connect("/tts/session/ws") as websocket:
            start_websocket_turn(websocket, "ws-disconnect")
            websocket.send_json({"type": "text_delta", "seq": 1, "text": "断连时正在合成"})
        assert backend.context_exited.wait(1.0)
        health = application.state.runtime.health()
        assert health["active"] == 0 and health["orphan_count"] == 0
        session = application.state.runtime._require_session("ws-disconnect")
        assert session.worker_exited.is_set() and session.status == "aborted"


@pytest.mark.asyncio
async def test_abort_cancels_audio_pump_before_clearing_a_full_slow_egress_queue() -> None:
    writer_release = asyncio.Event()
    writer_started = asyncio.Event()

    class SlowWebSocket:
        def __init__(self) -> None:
            self.controls: list[dict] = []

        async def send_bytes(self, _payload: bytes) -> None:
            writer_started.set()
            await writer_release.wait()

        async def send_json(self, payload: dict) -> None:
            self.controls.append(payload)

        async def close(self, *, code: int, reason: str) -> None:
            del code, reason

    class BackloggedRuntime:
        def __init__(self) -> None:
            self.items = [b"\x00\x00"] * 80 + [None]

        async def audio_item(self, _session) -> bytes | None:
            return self.items.pop(0)

    websocket = SlowWebSocket()
    sender = WebSocketSender(websocket)
    runtime = BackloggedRuntime()
    audio_task = asyncio.create_task(_audio_pump(runtime, object(), sender))
    await asyncio.wait_for(writer_started.wait(), timeout=0.5)

    async def wait_until_full() -> None:
        while sender.queue.qsize() < sender.queue.maxsize:
            await asyncio.sleep(0)

    await asyncio.wait_for(wait_until_full(), timeout=0.5)
    await asyncio.wait_for(_cancel_audio(audio_task), timeout=0.5)
    await asyncio.wait_for(sender.drop_pending_pcm(), timeout=0.5)
    assert sender.queue.empty()
    writer_release.set()
    await asyncio.wait_for(sender.json({"type": "audio_reset"}), timeout=0.5)
    await asyncio.wait_for(sender.json({"type": "released", "released": True}), timeout=0.5)
    assert websocket.controls == [{"type": "audio_reset"}, {"type": "released", "released": True}]
    await sender.shutdown()


def test_openmoss_configuration_fails_closed_without_fixed_security_and_revisions(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    base = {
        **configured.__dict__,
        "backend": "openmoss",
        "api_key": "secret",
    }
    GatewaySettings(**base).validate()
    with pytest.raises(ValueError, match="API_KEY"):
        GatewaySettings(**{**base, "api_key": ""}).validate()
    with pytest.raises(ValueError, match="cannot disable"):
        GatewaySettings(**{**base, "require_eight_prompts": False}).validate()
    with pytest.raises(ValueError, match="model revision"):
        GatewaySettings(**{**base, "model_revision": "moving-main"}).validate()
    with pytest.raises(ValueError, match="codec revision"):
        GatewaySettings(**{**base, "codec_revision": "moving-main"}).validate()
    with pytest.raises(ValueError, match="CUDA device"):
        GatewaySettings(**{**base, "device": "cpu"}).validate()
    with pytest.raises(ValueError, match="SDPA"):
        GatewaySettings(**{**base, "attention_implementation": "eager"}).validate()
    with pytest.raises(ValueError, match="at least 24GB"):
        GatewaySettings(**{**base, "minimum_gpu_total_memory_gb": 12}).validate()
    GatewaySettings(
        **{
            **base,
            "minimum_gpu_total_memory_gb": 12,
            "minimum_gpu_free_memory_gb": 10,
            "allow_sub24gb_diagnostic": True,
            "diagnostic_codec_device": "cpu",
        }
    ).validate()
    GatewaySettings(
        **{
            **base,
            "minimum_gpu_total_memory_gb": 12,
            "minimum_gpu_free_memory_gb": 10,
            "allow_sub24gb_diagnostic": True,
            "diagnostic_codec_encoder_offload": True,
        }
    ).validate()
    with pytest.raises(ValueError, match="diagnostic codec placement"):
        GatewaySettings(**{**base, "diagnostic_codec_device": "cpu"}).validate()
    with pytest.raises(ValueError, match="supports only cpu"):
        GatewaySettings(
            **{
                **base,
                "allow_sub24gb_diagnostic": True,
                "diagnostic_codec_device": "cuda:1",
            }
        ).validate()
    with pytest.raises(ValueError, match="encoder offload requires"):
        GatewaySettings(
            **{
                **base,
                "diagnostic_codec_encoder_offload": True,
            }
        ).validate()
    with pytest.raises(ValueError, match="cannot be combined"):
        GatewaySettings(
            **{
                **base,
                "allow_sub24gb_diagnostic": True,
                "diagnostic_codec_device": "cpu",
                "diagnostic_codec_encoder_offload": True,
            }
        ).validate()
    with pytest.raises(ValueError, match="at least 10GB"):
        GatewaySettings(
            **{
                **base,
                "minimum_gpu_total_memory_gb": 8,
                "minimum_gpu_free_memory_gb": 7,
                "allow_sub24gb_diagnostic": True,
            }
        ).validate()
    with pytest.raises(ValueError, match="free-memory threshold"):
        GatewaySettings(**{**base, "minimum_gpu_free_memory_gb": 25}).validate()


def test_nominal_24gib_gpu_allows_small_cuda_driver_reservation() -> None:
    nominal = 24 * 1024**3
    assert gpu_total_memory_meets_floor(nominal - 480 * 1024**2, 24)
    assert not gpu_total_memory_meets_floor(23 * 1024**3, 24)


def test_openmoss_diagnostic_places_model_on_cuda_and_codec_on_cpu(
    tmp_path: Path,
    monkeypatch,
) -> None:
    checkout = tmp_path / "MOSS-TTS"
    package = checkout / "moss_tts_realtime" / "mossttsrealtime"
    package.mkdir(parents=True)
    module_path = package / "__init__.py"
    module_path.write_text("", encoding="utf-8")

    placements: dict[str, str] = {}

    class Loaded:
        def __init__(self, name: str) -> None:
            self.name = name

        def eval(self):
            return self

        def to(self, device):
            placements[self.name] = str(device)
            return self

    class RealtimeModel:
        @classmethod
        def from_pretrained(cls, *_args, **_kwargs):
            return Loaded("model")

    class Processor:
        def __init__(self, tokenizer) -> None:
            self.tokenizer = tokenizer

    class AutoTokenizer:
        @classmethod
        def from_pretrained(cls, *_args, **_kwargs):
            return object()

    class AutoModel:
        @classmethod
        def from_pretrained(cls, *_args, **_kwargs):
            return Loaded("codec")

    openmoss_module = ModuleType("mossttsrealtime")
    openmoss_module.__file__ = str(module_path)
    openmoss_module.MossTTSRealtime = RealtimeModel
    openmoss_module.MossTTSRealtimeProcessor = Processor
    transformers_module = ModuleType("transformers")
    transformers_module.AutoModel = AutoModel
    transformers_module.AutoTokenizer = AutoTokenizer

    gib = 1024**3
    fake_torch = ModuleType("torch")
    fake_torch.bfloat16 = "bfloat16"
    fake_torch.float16 = "float16"
    fake_torch.device = lambda value: value
    fake_torch.set_float32_matmul_precision = lambda _value: None
    fake_torch._dynamo = SimpleNamespace(config=SimpleNamespace(cache_size_limit=8))
    fake_torch.cuda = SimpleNamespace(
        is_available=lambda: True,
        is_bf16_supported=lambda: True,
        mem_get_info=lambda: (11 * gib, 12 * gib),
        device=lambda _value: SimpleNamespace(
            __enter__=lambda: None,
            __exit__=lambda *_args: None,
        ),
    )
    # Special methods are resolved on the type, not an instance attribute.
    class DeviceContext:
        def __enter__(self):
            return None

        def __exit__(self, *_args):
            return None

    fake_torch.cuda.device = lambda _value: DeviceContext()
    fake_torch.backends = SimpleNamespace(
        cuda=SimpleNamespace(matmul=SimpleNamespace(allow_tf32=False)),
        cudnn=SimpleNamespace(allow_tf32=False),
    )

    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setitem(sys.modules, "transformers", transformers_module)
    monkeypatch.setitem(sys.modules, "mossttsrealtime", openmoss_module)
    monkeypatch.setattr(
        "moss_realtime_gateway.backends.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=f"{OPENMOSS_UPSTREAM_REVISION}\n"),
    )
    configured = settings(
        tmp_path,
        backend="openmoss",
        api_key="secret",
        upstream_checkout=checkout,
        minimum_gpu_total_memory_gb=12,
        minimum_gpu_free_memory_gb=10,
        allow_sub24gb_diagnostic=True,
        diagnostic_codec_device="cpu",
    )
    configured.validate()
    backend = OpenMossCudaBackend(configured)
    backend.startup()

    assert placements == {"model": "cuda:0", "codec": "cpu"}
    assert str(backend.device) == "cuda:0"
    assert str(backend.codec_device) == "cpu"
    assert fake_torch._dynamo.config.cache_size_limit == 64
    assert backend.placement["dynamo_cache_size_limit"] == 64

    placements.clear()
    split = settings(
        tmp_path,
        backend="openmoss",
        api_key="secret",
        upstream_checkout=checkout,
        minimum_gpu_total_memory_gb=12,
        minimum_gpu_free_memory_gb=10,
        allow_sub24gb_diagnostic=True,
        diagnostic_codec_encoder_offload=True,
    )
    split.validate()
    split_backend = OpenMossCudaBackend(split)
    split_backend.startup()

    assert placements == {"codec": "cuda:0"}
    assert split_backend.model is None
    assert str(split_backend.codec_device) == "cuda:0"


def test_diagnostic_codec_device_reads_only_from_explicit_environment(monkeypatch) -> None:
    monkeypatch.delenv("MOSS_GATEWAY_DIAGNOSTIC_CODEC_DEVICE", raising=False)
    assert GatewaySettings.from_env().diagnostic_codec_device == ""
    monkeypatch.setenv("MOSS_GATEWAY_DIAGNOSTIC_CODEC_DEVICE", " CPU ")
    assert GatewaySettings.from_env().diagnostic_codec_device == "cpu"


def test_dynamo_cache_size_limit_reads_environment_and_rejects_too_small(monkeypatch) -> None:
    monkeypatch.setenv("MOSS_GATEWAY_DYNAMO_CACHE_SIZE_LIMIT", "96")
    assert GatewaySettings.from_env().dynamo_cache_size_limit == 96
    with pytest.raises(ValueError, match="must be at least 8"):
        GatewaySettings(dynamo_cache_size_limit=7).validate()


def test_async_decoder_requires_explicit_environment_and_bounded_runtime(monkeypatch) -> None:
    monkeypatch.delenv("MOSS_GATEWAY_ASYNC_DECODER_ENABLED", raising=False)
    assert GatewaySettings.from_env().async_decoder_enabled is False
    monkeypatch.setenv("MOSS_GATEWAY_ASYNC_DECODER_ENABLED", "true")
    monkeypatch.setenv("MOSS_GATEWAY_ASYNC_DECODER_QUEUE_SIZE", "3")
    monkeypatch.setenv("MOSS_GATEWAY_ASYNC_DECODER_JOIN_TIMEOUT_SECONDS", "7.5")
    configured = GatewaySettings.from_env()
    assert configured.async_decoder_enabled is True
    assert configured.async_decoder_queue_size == 3
    assert configured.async_decoder_join_timeout_seconds == 7.5
    with pytest.raises(ValueError, match="QUEUE_SIZE"):
        GatewaySettings(async_decoder_queue_size=0).validate()
    with pytest.raises(ValueError, match="JOIN_TIMEOUT"):
        GatewaySettings(async_decoder_join_timeout_seconds=0).validate()


def test_diagnostic_codec_encoder_offload_reads_only_from_explicit_environment(monkeypatch) -> None:
    monkeypatch.delenv("MOSS_GATEWAY_DIAGNOSTIC_CODEC_ENCODER_OFFLOAD", raising=False)
    assert GatewaySettings.from_env().diagnostic_codec_encoder_offload is False
    monkeypatch.setenv("MOSS_GATEWAY_DIAGNOSTIC_CODEC_ENCODER_OFFLOAD", "true")
    assert GatewaySettings.from_env().diagnostic_codec_encoder_offload is True


def test_diagnostic_codec_encoder_offload_keeps_decode_graph_on_cuda(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class Parameter:
        def __init__(self, device: str) -> None:
            self.device = device

    class Module:
        def __init__(self, count: int, device: str = "cuda:0") -> None:
            self.values = [Parameter(device) for _ in range(count)]

        def parameters(self):
            return iter(self.values)

        def to(self, device):
            for parameter in self.values:
                parameter.device = str(device)
            return self

    class ModuleList(list):
        def parameters(self):
            return iter(parameter for module in self for parameter in module.parameters())

    calls: list[str] = []

    class DeviceContext:
        def __enter__(self):
            calls.append("cuda_context_enter")

        def __exit__(self, *_args):
            calls.append("cuda_context_exit")

    fake_torch = ModuleType("torch")
    fake_torch.cuda = SimpleNamespace(
        device=lambda _device: DeviceContext(),
        synchronize=lambda: calls.append("synchronize"),
        empty_cache=lambda: calls.append("empty_cache"),
    )
    monkeypatch.setitem(sys.modules, "torch", fake_torch)

    configured = settings(
        tmp_path,
        allow_sub24gb_diagnostic=True,
        diagnostic_codec_encoder_offload=True,
    )
    backend = OpenMossCudaBackend(configured)
    backend.device = "cuda:0"
    backend.codec_device = "cuda:0"
    encoder = Module(3)
    quantizer = Module(2)
    decoder = ModuleList([Module(2), Module(1)])
    backend.codec = SimpleNamespace(encoder=encoder, quantizer=quantizer, decoder=decoder)
    backend._prompt_tokens = {
        tmp_path / f"voice-{index}.wav": object() for index in range(1, 9)
    }

    backend._activate_diagnostic_codec_encoder_offload()

    assert {parameter.device for parameter in encoder.parameters()} == {"cpu"}
    assert {parameter.device for parameter in quantizer.parameters()} == {"cuda:0"}
    assert {parameter.device for parameter in decoder.parameters()} == {"cuda:0"}
    assert calls == [
        "cuda_context_enter",
        "synchronize",
        "cuda_context_exit",
        "cuda_context_enter",
        "empty_cache",
        "cuda_context_exit",
    ]
    assert backend.placement == {
        "mode": "diagnostic_encoder_cpu_decoder_cuda",
        "realtime_model": None,
        "codec_encoder": "cpu",
        "codec_quantizer": "cuda:0",
        "codec_decoder": "cuda:0",
        "codec_streaming_context": "decoder_only",
        "prompt_tokens_cached": 8,
        "prompt_token_frames": {f"voice-{index}.wav": 0 for index in range(1, 9)},
        "warmup_text_tokens": 0,
            "dynamo_cache_size_limit": None,
            "async_decoder_enabled": False,
            "automatic_gc_disabled": False,
    }


def test_diagnostic_codec_encoder_offload_rejects_shared_decode_parameters(tmp_path: Path) -> None:
    class Parameter:
        device = "cuda:0"

    shared = Parameter()

    class Module:
        def __init__(self, parameters) -> None:
            self.values = list(parameters)

        def parameters(self):
            return iter(self.values)

    configured = settings(
        tmp_path,
        allow_sub24gb_diagnostic=True,
        diagnostic_codec_encoder_offload=True,
    )
    backend = OpenMossCudaBackend(configured)
    backend.device = "cuda:0"
    backend.codec_device = "cuda:0"
    backend.codec = SimpleNamespace(
        encoder=Module([shared]),
        quantizer=Module([shared]),
        decoder=Module([Parameter()]),
    )
    backend._prompt_tokens = {
        tmp_path / f"voice-{index}.wav": object() for index in range(1, 9)
    }

    with pytest.raises(RuntimeError, match="shares parameters"):
        backend._activate_diagnostic_codec_encoder_offload()


def test_diagnostic_codec_encoder_offload_orders_prompt_encode_before_model_load(
    tmp_path: Path,
    monkeypatch,
) -> None:
    configured = settings(
        tmp_path,
        allow_sub24gb_diagnostic=True,
        diagnostic_codec_encoder_offload=True,
    )
    backend = OpenMossCudaBackend(configured)
    events: list[str] = []
    prompts = {
        f"debate_voice_{index}": tmp_path / f"voice-{index}.wav" for index in range(1, 9)
    }

    def encode(path: Path):
        events.append(f"encode:{path.name}")
        backend._prompt_tokens[path] = object()

    monkeypatch.setattr(backend, "_encode_prompt", encode)
    monkeypatch.setattr(
        backend,
        "_activate_diagnostic_codec_encoder_offload",
        lambda: events.append("offload_encoder"),
    )
    monkeypatch.setattr(backend, "_load_realtime_model", lambda: events.append("load_realtime"))

    backend._prepare_for_warmup(prompts)

    assert events == [
        *(f"encode:voice-{index}.wav" for index in range(1, 9)),
        "offload_encoder",
        "load_realtime",
    ]


def test_openmoss_warmup_primes_full_repetition_window_for_every_voice(
    tmp_path: Path,
    monkeypatch,
) -> None:
    configured = settings(tmp_path, repetition_window=50)
    backend = OpenMossCudaBackend(configured)

    class Tokenizer:
        @staticmethod
        def encode(text: str, *, add_special_tokens: bool) -> list[int]:
            assert add_special_tokens is False
            return list(range(len(text.replace(" ", ""))))

    class Turn:
        @staticmethod
        def initial_audio():
            return [b"initial"]

        @staticmethod
        def finish():
            return [b"final"]

        @staticmethod
        def close() -> None:
            return None

    observed: list[str] = []
    backend.tokenizer = Tokenizer()
    backend.processor = SimpleNamespace(delay_tokens_len=12)
    monkeypatch.setattr(backend, "_prepare_for_warmup", lambda _prompts: None)

    def open_turn(*, prompt_path: Path, user_text: str, initial_text: str) -> Turn:
        del prompt_path, user_text
        observed.append(initial_text)
        return Turn()

    monkeypatch.setattr(backend, "open_turn", open_turn)
    monkeypatch.setattr("moss_realtime_gateway.backends.gc.isenabled", lambda: False)
    prompts = {
        f"debate_voice_{index}": tmp_path / f"voice-{index}.wav" for index in range(1, 9)
    }

    backend.warmup(prompts, "普通话预热。")

    assert len(observed) == 8
    assert len(backend.tokenizer.encode(observed[0], add_special_tokens=False)) >= 63
    assert observed == [observed[0]] * 8
    assert backend._warmup_text_tokens >= 63


def test_diagnostic_codec_encoder_offload_uses_decoder_only_streaming_context(tmp_path: Path) -> None:
    events: list[str] = []

    class StreamingContext:
        def __init__(self, name: str) -> None:
            self.name = name

        def __enter__(self):
            events.append(f"enter:{self.name}")

        def __exit__(self, *_args):
            events.append(f"exit:{self.name}")

    class DecoderModule:
        def __init__(self, name: str) -> None:
            self.name = name

        def streaming(self, *, batch_size: int):
            assert batch_size == 1
            return StreamingContext(self.name)

    class Codec:
        decoder = [DecoderModule("decoder-1"), DecoderModule("decoder-2")]

        def streaming(self, *, batch_size: int):
            del batch_size
            raise AssertionError("root codec.streaming traverses encoder and must not be used")

    configured = settings(
        tmp_path,
        allow_sub24gb_diagnostic=True,
        diagnostic_codec_encoder_offload=True,
    )
    backend = OpenMossCudaBackend(configured)
    backend.codec = Codec()

    with backend._codec_streaming_context():
        events.append("decode")

    assert events == [
        "enter:decoder-1",
        "enter:decoder-2",
        "decode",
        "exit:decoder-2",
        "exit:decoder-1",
    ]


def test_prompt_encode_uses_pinned_codec_tensor_contract(tmp_path: Path, monkeypatch) -> None:
    import numpy as np

    class Waveform:
        shape = (1, 24_000)

        def unsqueeze(self, dimension: int):
            assert dimension == 0
            self.shape = (1, 1, 24_000)
            return self

        def to(self, device):
            assert str(device) == "cpu"
            return self

    class Codes:
        def detach(self):
            return self

        def cpu(self):
            return self

        def numpy(self):
            return np.arange(16 * 5, dtype=np.int64).reshape(16, 1, 5)

    class EncodeResult:
        audio_codes = Codes()

    class Codec:
        def encode(self, input_values, *, chunk_duration: float):
            assert input_values.shape == (1, 1, 24_000)
            assert chunk_duration == pytest.approx(0.24)
            return EncodeResult()

    class InferenceMode:
        def __enter__(self):
            return None

        def __exit__(self, *_args):
            return None

    torchaudio = ModuleType("torchaudio")
    torchaudio.load = lambda _path: (Waveform(), 24_000)
    torch = ModuleType("torch")
    torch.inference_mode = lambda: InferenceMode()
    monkeypatch.setitem(sys.modules, "torchaudio", torchaudio)
    monkeypatch.setitem(sys.modules, "torch", torch)

    backend = OpenMossCudaBackend(settings(tmp_path))
    backend.codec = Codec()
    backend.codec_device = "cpu"
    prompt = tmp_path / "prompt.wav"
    prompt.write_bytes(b"RIFF")

    codes = backend._encode_prompt(prompt)

    assert codes.shape == (16, 5)
    assert backend._prompt_tokens[prompt] is codes


def test_openmoss_turn_keeps_audio_generation_lazy_and_one_context_per_turn() -> None:
    import numpy as np

    class Bridge:
        def __init__(self) -> None:
            self.generated: list[str] = []

        def push_text_delta(self, text: str):
            self.generated.append(text)
            yield np.asarray([0.25, -0.25], dtype=np.float32)

        def finish(self, *, drain_step: int):
            assert drain_step == 1
            self.generated.append("finish")
            yield np.asarray([0.1], dtype=np.float32)

    class CodecContext:
        def __init__(self) -> None:
            self.exits = 0

        def __exit__(self, exc_type, exc, traceback) -> None:
            del exc_type, exc, traceback
            self.exits += 1

    bridge = Bridge()
    context = CodecContext()
    turn = OpenMossTurn(bridge=bridge, codec_context=context, initial_text="首段")
    assert bridge.generated == []
    initial = turn.initial_audio()
    assert bridge.generated == []
    assert len(next(iter(initial))) == 4
    pushed = turn.push_text("增量")
    assert bridge.generated == ["首段"]
    assert len(next(iter(pushed))) == 4
    assert list(turn.finish()) and bridge.generated == ["首段", "增量", "finish"]
    turn.close()
    turn.close()
    assert context.exits == 1


def test_openmoss_turn_abort_sets_the_inference_cancel_signal() -> None:
    class CodecContext:
        def __exit__(self, exc_type, exc, traceback) -> None:
            del exc_type, exc, traceback

    cancelled = threading.Event()
    turn = OpenMossTurn(
        bridge=object(),
        codec_context=CodecContext(),
        initial_text="",
        cancel_event=cancelled,
    )
    turn.abort()
    assert turn.aborted is True and cancelled.is_set()
    turn.close()


def _install_fake_tensor_torch(monkeypatch):
    import numpy as np

    class FakeTensor:
        def __init__(self, values, *, device: str = "cuda:0") -> None:
            self.values = np.asarray(values)
            self.device = device

        @property
        def shape(self):
            return self.values.shape

        @property
        def dtype(self):
            return self.values.dtype

        def __getitem__(self, index):
            return FakeTensor(self.values[index], device=self.device)

        def __invert__(self):
            return FakeTensor(~self.values, device=self.device)

        def __ior__(self, other):
            self.values |= other.values
            return self

        def __eq__(self, other):
            return FakeTensor(self.values == _values(other), device=self.device)

        def __ne__(self, other):
            return FakeTensor(self.values != _values(other), device=self.device)

        def __gt__(self, other):
            return FakeTensor(self.values > _values(other), device=self.device)

        def __lt__(self, other):
            return FakeTensor(self.values < _values(other), device=self.device)

        def __mul__(self, other):
            return FakeTensor(self.values * _values(other), device=self.device)

        def __truediv__(self, other):
            return FakeTensor(self.values / _values(other), device=self.device)

        def unsqueeze(self, dim: int):
            return FakeTensor(np.expand_dims(self.values, dim), device=self.device)

        def detach(self):
            return self

        def cpu(self):
            return self

        def tolist(self):
            return self.values.tolist()

        def to(self, *, dtype):
            return FakeTensor(self.values.astype(dtype), device=self.device)

        def scatter_add_(self, dim: int, index, source):
            assert dim == 1
            for row in range(self.values.shape[0]):
                np.add.at(self.values[row], index.values[row], source.values[row])
            return self

        def copy_(self, other):
            self.values[...] = other.values
            return self

    def _values(value):
        return value.values if isinstance(value, FakeTensor) else value

    class InferenceMode:
        def __enter__(self):
            return None

        def __exit__(self, exc_type, exc, traceback) -> None:
            del exc_type, exc, traceback

    fake_torch = ModuleType("torch")
    fake_torch.Tensor = FakeTensor
    fake_torch.long = np.int64
    fake_torch.int32 = np.int32
    fake_torch.inference_mode = InferenceMode
    fake_torch.tensor = lambda values, *, device, dtype: FakeTensor(
        np.asarray(values, dtype=dtype),
        device=device,
    )
    fake_torch.cat = lambda tensors, dim: FakeTensor(
        np.concatenate([tensor.values for tensor in tensors], axis=dim),
        device=tensors[0].device,
    )
    fake_torch.stack = lambda tensors, dim: FakeTensor(
        np.stack([tensor.values for tensor in tensors], axis=dim),
        device=tensors[0].device,
    )
    fake_torch.full = lambda shape, value, *, dtype, device: FakeTensor(
        np.full(shape, value, dtype=dtype),
        device=device,
    )
    fake_torch.zeros_like = lambda tensor, dtype=None: FakeTensor(
        np.zeros_like(tensor.values, dtype=dtype),
        device=tensor.device,
    )
    fake_torch.where = lambda condition, left, right: FakeTensor(
        np.where(condition.values, _values(left), _values(right)),
        device=condition.device,
    )
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    return FakeTensor


def test_cancellable_inference_uses_fixed_repetition_history_and_trims_ring(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import numpy as np

    Tensor = _install_fake_tensor_torch(monkeypatch)
    captures: list[dict] = []

    class Model:
        def __call__(self, **_kwargs):
            return SimpleNamespace(
                past_key_values=object(),
                last_hidden_state=Tensor(np.zeros((1, 1, 4), dtype=np.float32)),
            )

    class Inference:
        def __init__(self, model, tokenizer, *, max_length: int) -> None:
            del tokenizer, max_length
            self.model = model
            self.text_pad_id = 99
            self.audio_eos_token = 1000
            self._last_audio_tokens = Tensor([[7, 8]])
            self.attention_mask = Tensor([[True]])
            self._is_stopping = Tensor([False])
            self.past_key_values = object()
            self._generated_tokens = [self._last_audio_tokens]
            self._step_idx = 1
            self.base_finished = False

        @property
        def is_finished(self) -> bool:
            return self.base_finished

        def generate_local_transformer(self, **kwargs):
            captures.append(kwargs)
            call_index = len(captures)
            return Tensor([[call_index, call_index + 100]])

    backend = OpenMossCudaBackend(settings(tmp_path))
    inference_class = backend._get_cancellable_inference_class(Inference)
    inferencer = inference_class(
        Model(),
        object(),
        max_length=200,
        cancel_event=threading.Event(),
    )

    for _ in range(52):
        inferencer.step(
            42,
            temperature=0.7,
            top_p=0.55,
            top_k=17,
            do_sample=False,
            repetition_penalty=1.2,
            repetition_window=50,
        )

    first_history = captures[0]["generated_tokens"].values
    last_history = captures[-1]["generated_tokens"].values
    assert first_history.shape == (1, 50, 2)
    assert np.all(first_history[:, :49, :] == 1024)
    assert first_history[0, -1, :].tolist() == [7, 8]
    assert all(capture["generated_tokens"].shape == (1, 50, 2) for capture in captures)
    assert all(capture["gen_step"] == 50 for capture in captures)
    assert all(capture["repetition_window"] == 50 for capture in captures)
    assert captures[0]["temperature"] == 0.7
    assert captures[0]["top_p"] == 0.55
    assert captures[0]["top_k"] == 17
    assert captures[0]["do_sample"] is False
    assert captures[0]["repetition_penalty"] == 1.2
    assert last_history[0, :, 0].tolist() == list(range(2, 52))
    assert [token.values[0, 0] for token in inferencer._generated_tokens] == list(range(3, 53))
    assert inferencer._step_idx == 53

    with pytest.raises(ValueError, match="repetition_window=50"):
        inferencer.step(42, repetition_window=49)


def test_cancellable_inference_penalty_ignores_pad_and_penalizes_unique_valid_tokens(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import numpy as np

    Tensor = _install_fake_tensor_torch(monkeypatch)

    class Inference:
        def __init__(self, *_args, **_kwargs) -> None:
            return None

        @property
        def is_finished(self) -> bool:
            return False

    backend = OpenMossCudaBackend(settings(tmp_path))
    inference_class = backend._get_cancellable_inference_class(Inference)
    inferencer = inference_class(cancel_event=threading.Event())
    raw_scores = np.arange(1024, dtype=np.float32)
    raw_scores[0] = 10.0
    raw_scores[3] = -4.0
    raw_scores[5] = 6.0
    scores = Tensor(raw_scores.reshape(1, 1, -1))
    history = Tensor(
        np.asarray([[*[1024] * 45, 0, 3, 3, 5, 1024]], dtype=np.int64)
    )

    adjusted = inferencer.apply_repetition_penalty(
        scores,
        history,
        penalty=2.0,
        repetition_window=50,
    )

    assert adjusted.values[0, 0] == 5.0
    assert adjusted.values[0, 3] == -8.0
    assert adjusted.values[0, 5] == 3.0
    assert adjusted.values[0, 4] == raw_scores[4]
    assert adjusted.shape == (1, 1024)


def test_openmoss_backend_reuses_one_cancellable_inferencer_across_turns(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import numpy as np

    class Inference:
        def __init__(self, model, tokenizer, *, max_length: int) -> None:
            del model, tokenizer
            self.max_length = max_length
            self.base_finished = False
            self.reset_count = 0

        @property
        def is_finished(self) -> bool:
            return self.base_finished

        def reset_generation_state(self, *, keep_cache: bool) -> None:
            assert keep_cache is False
            self.base_finished = False
            self.reset_count += 1

    class Session:
        def __init__(self, inferencer, processor, **_kwargs) -> None:
            self.inferencer = inferencer
            self.processor = processor
            self._pending_tokens = []
            self._prefilled = False
            self._text_cache = ""
            self._text_ended = False

        def _extract_text_segments(self, *, force: bool):
            del force
            return []

        def _prefill_if_needed(self):
            return []

        def _tokenize(self, _text: str):
            return []

        def set_voice_prompt_tokens(self, tokens) -> None:
            self.voice_prompt_tokens = tokens

        def reset_turn(self, **_kwargs) -> None:
            return None

    class Decoder:
        def __init__(self, *_args, **_kwargs) -> None:
            return None

    class Bridge:
        def __init__(self, session, decoder, *, batch_size: int) -> None:
            assert batch_size == 1
            self.session = session
            self.decoder = decoder

    class CodecContext:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

    class Tokenizer:
        def __call__(self, _text: str) -> dict[str, list[int]]:
            return {"input_ids": [1, 2]}

    class Processor:
        channels = 16
        audio_channel_pad = 0
        delay_tokens_len = 1

        def __init__(self) -> None:
            self.tokenizer = Tokenizer()

        def make_ensemble(self, _tokens):
            return np.zeros((1, self.channels + 1), dtype=np.int64)

    streaming_module = ModuleType("mossttsrealtime.streaming_mossttsrealtime")
    streaming_module.AudioStreamDecoder = Decoder
    streaming_module.MossTTSRealtimeInference = Inference
    streaming_module.MossTTSRealtimeStreamingSession = Session
    streaming_module.MossTTSRealtimeTextStreamBridge = Bridge
    streaming_module._sanitize_audio_tokens = lambda tokens, **_kwargs: (tokens, False)
    openmoss_package = ModuleType("mossttsrealtime")
    openmoss_package.__path__ = []
    monkeypatch.setitem(sys.modules, "mossttsrealtime", openmoss_package)
    monkeypatch.setitem(
        sys.modules,
        "mossttsrealtime.streaming_mossttsrealtime",
        streaming_module,
    )

    backend = OpenMossCudaBackend(settings(tmp_path))
    backend.model = object()
    backend.tokenizer = object()
    backend.processor = Processor()
    backend.codec = object()
    backend.codec_device = "cpu"
    monkeypatch.setattr(
        backend,
        "_encode_prompt",
        lambda _path: np.zeros((16, 3), dtype=np.int64),
    )
    monkeypatch.setattr(backend, "_codec_streaming_context", lambda: CodecContext())
    prompt = tmp_path / "prompt.wav"

    first = backend.open_turn(prompt_path=prompt, user_text="第一轮", initial_text="")
    first_inferencer = first.bridge.session.inferencer
    first_cancel_event = first.cancel_event
    first.close()
    second = backend.open_turn(prompt_path=prompt, user_text="第二轮", initial_text="")
    second_inferencer = second.bridge.session.inferencer

    assert first_inferencer is second_inferencer
    assert type(first_inferencer) is type(second_inferencer)
    assert type(first_inferencer) is backend._cancellable_inference_class
    assert second_inferencer.cancel_event is second.cancel_event
    assert first_cancel_event is not second.cancel_event
    assert second_inferencer.reset_count == 2
    assert first_inferencer.is_finished is False
    assert second_inferencer.is_finished is False

    first.abort()
    assert first_cancel_event.is_set() is True
    assert first_inferencer.is_finished is False
    assert second_inferencer.is_finished is False
    second_inferencer.base_finished = True
    assert second_inferencer.is_finished is True
    second_inferencer.base_finished = False
    second.abort()
    assert second_inferencer.is_finished is True

    second.close()
    fake_torch = ModuleType("torch")
    fake_torch.cuda = SimpleNamespace(is_available=lambda: False)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    backend.shutdown()
    assert backend._cancellable_inference_class is None
    assert backend._realtime_inferencer is None


def test_prompt_registry_rejects_traversal_unknown_and_symlink(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    registry = PromptRegistry(configured)
    assert registry.resolve("debate_voice_1").name == "voice-1.wav"
    assert registry.resolve("voice-1.wav").name == "voice-1.wav"
    with pytest.raises(ValueError, match="configured voice"):
        registry.resolve("../voice-1.wav")
    with pytest.raises(ValueError, match="allowlist"):
        registry.resolve("other.wav")
    outside = tmp_path / "outside.wav"
    outside.write_bytes(b"RIFF")
    link = configured.prompt_dir / "voice-1.wav"
    link.unlink()
    link.symlink_to(outside)
    with pytest.raises(ValueError, match="missing or unsafe"):
        registry.resolve("debate_voice_1")


def test_api_key_protects_ready_control_and_audio_but_not_liveness(tmp_path: Path) -> None:
    configured = settings(tmp_path, api_key="secret")
    backend = FakeBackend()
    with TestClient(create_app(configured, backend=backend)) as client:
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").status_code == 401
        assert client.get("/health/ready", headers=auth()).status_code == 200
        body = {
            "session_id": "auth-session",
            "assistant_text": "开始",
            "prompt_audio": "debate_voice_1",
            "new_turn": True,
        }
        assert client.post("/tts/session/start", json=body).status_code == 401
        assert client.post("/tts/session/start", json=body, headers=auth("wrong")).status_code == 401
        assert client.post("/tts/session/start", json=body, headers=auth()).status_code == 200
        assert client.get("/tts/session/auth-session/audio").status_code == 401
        ready = client.get("/health/ready", headers=auth()).json()
        assert "secret" not in str(ready)
        assert ready["backend"] == "fake" and ready["upstream_revision"].startswith("ad99ec5")
        assert ready["ok"] is True and ready["model_warmed"] is True
        assert ready["active"] == 1 and ready["orphan_count"] == 0
        assert ready["diagnostic_codec_encoder_offload"] is False
        assert ready["placement"] is None
        assert client.post(
            "/tts/session/abort",
            json={"session_id": "auth-session", "reason": "test"},
            headers=auth(),
        ).status_code == 200


def test_full_protocol_is_single_active_and_streams_mono_pcm16(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    backend = FakeBackend()
    with TestClient(create_app(configured, backend=backend)) as client:
        start = client.post(
            "/tts/session/start",
            json={
                "session_id": "session-one",
                "assistant_text": "第一段",
                "user_text": "普通话辩论",
                "prompt_audio": "debate_voice_1",
                "new_turn": True,
            },
        )
        assert start.status_code == 200 and start.json()["status"] == "active"
        conflict = client.post(
            "/tts/session/start",
            json={
                "session_id": "session-two",
                "assistant_text": "冲突",
                "prompt_audio": "debate_voice_2",
                "new_turn": True,
            },
        )
        assert conflict.status_code == 409
        assert client.post(
            "/tts/session/push",
            json={"session_id": "session-one", "text": "第二段", "is_final": False},
        ).status_code == 200
        final = client.post(
            "/tts/session/push",
            json={"session_id": "session-one", "text": "最后一段", "is_final": True},
        )
        assert final.status_code == 200 and final.json()["worker_exited"] is True
        assert final.json()["released"] is True
        audio = client.get("/tts/session/session-one/audio")
        assert audio.status_code == 200 and audio.content and len(audio.content) % 2 == 0
        assert audio.headers["x-audio-sample-rate"] == "24000"
        assert audio.headers["x-audio-channels"] == "1"
        assert audio.headers["x-audio-codec"] == "pcm_s16le"
        closed = client.post("/tts/session/close", json={"session_id": "session-one"})
        assert closed.status_code == 200 and closed.json()["worker_exited"] is True
        assert closed.json()["released"] is True


@pytest.mark.asyncio
async def test_close_waits_for_backend_finish_and_codec_context_exit(tmp_path: Path) -> None:
    configured = settings(tmp_path, terminal_grace_seconds=2.0)
    backend = FakeBackend(finish_delay_seconds=0.15)
    runtime = GatewayRuntime(configured, backend)
    await runtime.startup()
    await runtime.start_session(
        session_id="close-ack",
        prompt_audio="debate_voice_1",
        user_text="普通话",
        assistant_text="开始",
    )
    started = time.perf_counter()
    result = await runtime.close_session("close-ack")
    elapsed = time.perf_counter() - started
    assert elapsed >= 0.14
    assert result["worker_exited"] is True
    assert backend.context_exited.is_set()
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_final_generation_has_a_longer_deadline_than_abort_cleanup(tmp_path: Path) -> None:
    configured = settings(
        tmp_path,
        terminal_grace_seconds=0.03,
        final_generation_timeout_seconds=0.5,
    )
    release = threading.Event()
    backend = FakeBackend(finish_block=release)
    runtime = GatewayRuntime(configured, backend)
    await runtime.startup()
    await runtime.start_session(
        session_id="long-final",
        prompt_audio="debate_voice_1",
        user_text="普通话",
        assistant_text="开始",
    )
    finishing = asyncio.create_task(runtime.push_text("long-final", "", is_final=True))
    await asyncio.sleep(0.08)
    assert not finishing.done()
    release.set()
    result = await finishing
    assert result["released"] is True and runtime.health()["orphan_count"] == 0
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_terminal_audio_is_lossless_without_an_early_audio_consumer(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    backend = FakeBackend()
    runtime = GatewayRuntime(configured, backend)
    await runtime.startup()
    await runtime.start_session(
        session_id="lossless-tail",
        prompt_audio="debate_voice_1",
        user_text="普通话",
        assistant_text="第一段会先进入队列",
    )
    result = await runtime.close_session("lossless-tail")
    assert result["released"] is True and runtime.ready is True
    session = runtime.claim_audio("lossless-tail")
    chunks: list[bytes] = []
    while True:
        item = await runtime.audio_item(session)
        if item is None:
            break
        chunks.append(item)
    assert len(chunks) >= 2 and all(chunks)
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_concurrent_close_and_abort_join_one_terminal_operation(tmp_path: Path) -> None:
    configured = settings(tmp_path, terminal_grace_seconds=1.0)
    release = threading.Event()
    backend = FakeBackend(finish_block=release)
    runtime = GatewayRuntime(configured, backend)
    await runtime.startup()
    await runtime.start_session(
        session_id="terminal-race",
        prompt_audio="debate_voice_1",
        user_text="普通话",
        assistant_text="开始",
    )
    closing = asyncio.create_task(runtime.close_session("terminal-race"))
    await asyncio.sleep(0.02)
    aborting = asyncio.create_task(runtime.abort_session("terminal-race", reason="audio_disconnect"))
    await asyncio.sleep(0.02)
    release.set()
    close_result, abort_result = await asyncio.gather(closing, aborting)
    assert close_result["released"] is True and abort_result["released"] is True
    assert runtime.ready is True and runtime.health()["orphan_count"] == 0
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_start_reports_initial_generation_failure(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    backend = FakeBackend()
    original_open_turn = backend.open_turn

    def open_turn_with_initial_failure(**kwargs):
        turn = original_open_turn(**kwargs)

        def failing_initial_audio():
            raise RuntimeError("initial synthesis failed")
            yield b""  # pragma: no cover

        turn.initial_audio = failing_initial_audio
        return turn

    runtime = GatewayRuntime(configured, backend)
    await runtime.startup()
    backend.open_turn = open_turn_with_initial_failure
    with pytest.raises(GatewayError, match="session start failed"):
        await runtime.start_session(
            session_id="initial-failure",
            prompt_audio="debate_voice_1",
            user_text="普通话",
            assistant_text="必须失败",
        )
    assert runtime._require_session("initial-failure").worker_exited.is_set()
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_close_failure_still_waits_for_worker_and_context_exit(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    backend = FakeBackend()
    runtime = GatewayRuntime(configured, backend)
    await runtime.startup()
    original_open_turn = backend.open_turn

    def open_turn_with_close_failure(**kwargs):
        turn = original_open_turn(**kwargs)
        original_close = turn.close

        def failing_close() -> None:
            original_close()
            raise RuntimeError("codec close failed")

        turn.close = failing_close
        return turn

    backend.open_turn = open_turn_with_close_failure
    await runtime.start_session(
        session_id="close-failure",
        prompt_audio="debate_voice_1",
        user_text="普通话",
        assistant_text="开始",
    )
    with pytest.raises(GatewayError, match="close failed"):
        await runtime.close_session("close-failure")
    session = runtime._require_session("close-failure")
    assert session.worker_exited.is_set() and session.worker is not None and not session.worker.is_alive()
    assert backend.context_exited.is_set()
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_push_response_waits_for_actual_worker_ack(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    backend = FakeBackend(push_delay_seconds=0.12)
    runtime = GatewayRuntime(configured, backend)
    await runtime.startup()
    await runtime.start_session(
        session_id="push-ack",
        prompt_audio="debate_voice_1",
        user_text="普通话",
        assistant_text="开始",
    )
    started = time.perf_counter()
    await runtime.push_text("push-ack", "第二段", is_final=False)
    assert time.perf_counter() - started >= 0.11
    await runtime.abort_session("push-ack", reason="test_complete")
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_abort_clears_audio_calls_backend_and_exits_worker(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    backend = FakeBackend()
    runtime = GatewayRuntime(configured, backend)
    await runtime.startup()
    await runtime.start_session(
        session_id="abort-ack",
        prompt_audio="debate_voice_1",
        user_text="普通话",
        assistant_text="有待清空的音频",
    )
    result = await runtime.abort_session("abort-ack", reason="manual_pause")
    session = runtime.claim_audio("abort-ack")
    assert await runtime.audio_item(session) is None
    assert result["status"] == "aborted" and result["worker_exited"] is True
    assert result["released"] is True
    assert backend.abort_called.is_set() and backend.context_exited.is_set()
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_orphan_marks_readiness_503_and_invokes_fail_fast_callback(tmp_path: Path) -> None:
    configured = settings(tmp_path, terminal_grace_seconds=0.05)
    release = threading.Event()
    backend = FakeBackend(finish_block=release)
    callbacks: list[dict] = []

    async def callback(payload: dict) -> None:
        callbacks.append(payload)

    runtime = GatewayRuntime(configured, backend, fail_fast_callback=callback)
    await runtime.startup()
    await runtime.start_session(
        session_id="orphaned",
        prompt_audio="debate_voice_1",
        user_text="普通话",
        assistant_text="开始",
    )
    with pytest.raises(SessionOrphaned):
        await runtime.close_session("orphaned")
    assert runtime.ready is False
    assert runtime.health()["orphaned_sessions"] == 1
    assert callbacks and callbacks[0]["reason"] == "close_ack_timeout"
    release.set()
    await asyncio.to_thread(runtime._require_session("orphaned").worker_exited.wait, 1.0)
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_audio_disconnect_aborts_the_active_session(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    backend = FakeBackend()
    runtime = GatewayRuntime(configured, backend)
    await runtime.startup()
    await runtime.start_session(
        session_id="disconnect",
        prompt_audio="debate_voice_1",
        user_text="普通话",
        assistant_text="开始",
    )
    session = runtime.claim_audio("disconnect")
    await runtime.audio_disconnected(session)
    assert session.status == "aborted" and backend.abort_called.is_set()
    assert await runtime.audio_item(session) is None
    await runtime.shutdown()
