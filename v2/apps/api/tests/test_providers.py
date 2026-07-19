from __future__ import annotations

import asyncio
import io
import json
import os
import re
import shutil
import socket
import sys
import threading
import time
import wave
from array import array
from pathlib import Path

import app.services.providers as providers_service
import app.services.system_health as system_health_service
import httpx
import pytest
import uvicorn
import websockets
from app.core.config import Settings, settings
from app.core.secret_crypto import encrypt_secret
from app.schemas.requests import AgentProfilePatch
from app.services.lighttts_admission import (
    LightTTSAdmissionQueueFull,
    LightTTSAdmissionQueueTimeout,
    LightTTSAdmissionUnavailable,
)
from app.services.provider_config import normalize_provider_config, runtime_provider_config
from app.services.providers import (
    DebateAgentProvider,
    JudgeProvider,
    LightTTSProvider,
    MossTTSRealtimeProvider,
    ProviderCancelled,
    ProviderError,
    RealtimeTTSProviderRouter,
    agent_payload,
)


def wav_bytes(seconds: float) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(b"\x00\x00" * int(24000 * seconds))
    return buffer.getvalue()


def pcm16_wav_bytes(samples: list[int], *, sample_rate: int = 24_000) -> bytes:
    buffer = io.BytesIO()
    frames = array("h", samples)
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(frames.tobytes())
    return buffer.getvalue()


def read_pcm16_samples(path: Path) -> tuple[int, list[int]]:
    with wave.open(str(path), "rb") as audio:
        assert audio.getnchannels() == 1
        assert audio.getsampwidth() == 2
        sample_rate = audio.getframerate()
        samples = array("h")
        samples.frombytes(audio.readframes(audio.getnframes()))
    return sample_rate, list(samples)


def multipart_field(body: bytes, name: str) -> str:
    marker = f'name="{name}"'.encode()
    header = body.index(marker)
    start = body.index(b"\r\n\r\n", header) + 4
    end = body.index(b"\r\n", start)
    return body[start:end].decode()


def test_agent_payload_uses_profile_model_and_config_rejects_qwen3_8b() -> None:
    payload = agent_payload(
        topic="模型配置测试",
        debater_name="乾元",
        seat_key="neg_2",
        current_stage="自由辩论",
        next_stage="总结",
        history=[],
        max_token=300,
        model_name="qwen3.6-27b-specialized",
    )
    assert payload["model_name"] == "qwen3.6-27b-specialized"
    with pytest.raises(ValueError, match="Qwen3 8B"):
        Settings(agent_model_name="Qwen3-8B-Instruct")


def test_lighttts_prompt_text_map_is_normalized_and_validated() -> None:
    configured = Settings(lighttts_prompt_texts={"debate_voice_2": "  第二音色提示文本  "})
    assert configured.lighttts_prompt_texts == {"debate_voice_2": "第二音色提示文本"}
    assert configured.lighttts_global_gate_enabled is False
    assert configured.lighttts_global_gate_max_pending == 2
    assert configured.lighttts_global_gate_queue_timeout_seconds == 20
    assert configured.lighttts_job_timeout_seconds == 300
    assert configured.lighttts_shutdown_drain_seconds == 10
    assert configured.lighttts_streaming_enabled is False
    assert configured.lighttts_bistream_enabled is False
    assert configured.realtime_voice_pipeline_enabled is False
    assert configured.realtime_voice_backend == "lighttts"
    assert configured.moss_tts_realtime_enabled is False
    assert configured.moss_tts_realtime_transport == "websocket"
    assert configured.moss_tts_realtime_idle_ws_ttl_seconds == 60
    assert configured.moss_tts_realtime_url == ""
    assert configured.moss_tts_realtime_max_active == 3
    assert configured.moss_tts_realtime_max_active_per_endpoint == 1
    assert configured.moss_tts_realtime_readiness_timeout_seconds == 2
    assert configured.moss_tts_realtime_first_pcm_timeout_seconds == 1.2
    assert configured.moss_tts_realtime_close_timeout_seconds == 15
    assert configured.lighttts_stream_sample_rate == 24_000
    with pytest.raises(ValueError, match="音色 ID"):
        Settings(lighttts_prompt_texts={"../unsafe": "提示文本"})
    with pytest.raises(ValueError, match="提示文本"):
        Settings(lighttts_prompt_texts={"debate_voice_2": ""})
    assert Settings(lighttts_max_active=2).lighttts_max_active == 2
    with pytest.raises(ValueError, match="less than or equal to 3"):
        Settings(lighttts_max_active=4)
    with pytest.raises(ValueError, match="greater than or equal to 330"):
        Settings(lighttts_global_gate_lease_seconds=329)
    with pytest.raises(ValueError, match="不得短于"):
        Settings(lighttts_job_timeout_seconds=331, lighttts_global_gate_lease_seconds=330)
    assert Settings(realtime_voice_backend="moss-realtime").realtime_voice_backend == "moss_realtime"
    with pytest.raises(ValueError, match="实时语音后端"):
        Settings(realtime_voice_backend="buffered_fake_stream")
    assert Settings(moss_tts_realtime_transport="HTTP").moss_tts_realtime_transport == "http"
    with pytest.raises(ValueError, match="transport"):
        Settings(moss_tts_realtime_transport="rest")
    assert Settings(moss_tts_prompt_files={"debate_voice_1": " voice-1.wav "}).moss_tts_prompt_files == {
        "debate_voice_1": "voice-1.wav"
    }
    with pytest.raises(ValueError, match="音色 ID"):
        Settings(moss_tts_prompt_files={"../unsafe": "voice.wav"})
    eight_prompts = {f"debate_voice_{index}": f"debate_voice_{index}.wav" for index in range(1, 9)}
    enabled = Settings(
        moss_tts_realtime_enabled=True,
        moss_tts_realtime_urls=["http://moss-a:8083", "http://moss-b:8083", "http://moss-c:8083"],
        moss_tts_prompt_files=eight_prompts,
    )
    assert enabled.moss_tts_realtime_max_active == 3
    with pytest.raises(ValueError, match="只允许 1 个活动会话"):
        Settings(
            moss_tts_realtime_enabled=True,
            moss_tts_realtime_urls=["http://moss-a:8083", "http://moss-b:8083"],
            moss_tts_realtime_max_active=2,
            moss_tts_realtime_max_active_per_endpoint=2,
            moss_tts_prompt_files={
                f"debate_voice_{index}": f"debate_voice_{index}.wav" for index in range(1, 9)
            },
        )
    with pytest.raises(ValueError, match="WAV basename"):
        Settings(
            moss_tts_realtime_enabled=True,
            moss_tts_realtime_urls=[
                "http://moss-a:8083",
                "http://moss-b:8083",
                "http://moss-c:8083",
            ],
            moss_tts_prompt_files={
                **{f"debate_voice_{index}": f"debate_voice_{index}.wav" for index in range(1, 9)},
                "debate_voice_8": "/tmp/debate_voice_8.wav",
            },
        )
    with pytest.raises(ValueError, match="完整配置"):
        Settings(
            moss_tts_realtime_enabled=True,
            moss_tts_realtime_url="http://moss-a:8083",
            moss_tts_realtime_max_active=1,
            moss_tts_prompt_files={"debate_voice_1": "voice.wav"},
        )
    with pytest.raises(ValueError, match="安全容量"):
        Settings(
            moss_tts_realtime_enabled=True,
            moss_tts_realtime_url="http://moss-a:8083",
            moss_tts_realtime_max_active=3,
            moss_tts_prompt_files=eight_prompts,
        )


def test_provider_error_keeps_message_compatibility_and_exposes_recovery_metadata() -> None:
    error = ProviderError(
        "服务繁忙，请稍后重试。",
        code="stable_capacity_code",
        retryable=True,
        retry_after_seconds=2,
    )
    assert str(error) == "服务繁忙，请稍后重试。"
    assert error.args == ("服务繁忙，请稍后重试。",)
    assert error.code == "stable_capacity_code"
    assert error.retryable is True
    assert error.retry_after_seconds == 2


def test_agent_profile_patch_accepts_only_fixed_debate_voices() -> None:
    assert AgentProfilePatch(voice_id="debate_voice_8").voice_id == "debate_voice_8"
    with pytest.raises(ValueError, match="debate_voice_1"):
        AgentProfilePatch(voice_id="random_character_voice")


@pytest.mark.parametrize(
    ("admission_error", "expected_message", "expected_code", "expected_retry_after"),
    [
        (LightTTSAdmissionQueueFull("full"), "全局队列已满", "lighttts_queue_full", 2),
        (LightTTSAdmissionQueueTimeout("timeout"), "全局排队超时", "lighttts_queue_timeout", 5),
        (
            LightTTSAdmissionUnavailable("unavailable"),
            "全局调度服务不可用",
            "lighttts_admission_unavailable",
            5,
        ),
    ],
)
async def test_lighttts_admission_failures_are_structured_and_retryable(
    tmp_path: Path,
    monkeypatch,
    admission_error: Exception,
    expected_message: str,
    expected_code: str,
    expected_retry_after: int,
) -> None:
    prompt = tmp_path / "debate_voice_1.wav"
    prompt.write_bytes(b"prompt")
    monkeypatch.setattr(settings, "lighttts_prompt_wav_path", str(prompt))
    monkeypatch.setattr(settings, "media_root", str(tmp_path / "media"))
    monkeypatch.setattr(settings, "lighttts_global_gate_enabled", True)

    class RejectedGate:
        async def acquire(self, _should_cancel=None, **_kwargs):
            raise admission_error

    async def forbidden_request(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("admission rejection must stop before the GPU request")

    monkeypatch.setattr(providers_service, "lighttts_admission_gate", RejectedGate())
    provider = LightTTSProvider(httpx.MockTransport(forbidden_request))
    with pytest.raises(ProviderError, match=expected_message) as captured:
        await provider.synthesize("结构化过载错误。", room_code="gate-structured", speech_id=expected_code)
    assert captured.value.code == expected_code
    assert captured.value.retryable is True
    assert captured.value.retry_after_seconds == expected_retry_after


async def test_lighttts_honors_an_expired_caller_shared_deadline_before_admission_or_gpu() -> None:
    provider = LightTTSProvider()
    with pytest.raises(ProviderError) as captured:
        await provider.synthesize(
            "共享预算已耗尽时不得启动新尝试。",
            room_code="expired-budget",
            speech_id="expired-budget",
            deadline_monotonic=asyncio.get_running_loop().time() - 0.001,
        )
    assert captured.value.code == "lighttts_job_timeout"
    assert captured.value.retryable is False


async def test_lighttts_feature_flag_routes_every_provider_instance_through_shared_gate(tmp_path: Path, monkeypatch) -> None:
    prompt = tmp_path / "debate_voice_1.wav"
    prompt.write_bytes(b"prompt")
    monkeypatch.setattr(settings, "lighttts_prompt_wav_path", str(prompt))
    monkeypatch.setattr(settings, "media_root", str(tmp_path / "media"))
    monkeypatch.setattr(settings, "lighttts_global_gate_enabled", True)
    acquired = 0
    released = 0

    class FakeLease:
        lost = False

        async def cancellation_requested(self, callback) -> bool:
            if callback is None:
                return False
            result = callback()
            return bool(await result) if asyncio.iscoroutine(result) else bool(result)

        async def release(self) -> None:
            nonlocal released
            released += 1

    class FakeGate:
        async def acquire(self, _should_cancel=None, **_kwargs) -> FakeLease:
            nonlocal acquired
            acquired += 1
            return FakeLease()

    monkeypatch.setattr(providers_service, "lighttts_admission_gate", FakeGate())

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=wav_bytes(1.0))

    first = LightTTSProvider(httpx.MockTransport(handler))
    second = LightTTSProvider(httpx.MockTransport(handler))
    await first.synthesize("第一个 Provider。", room_code="gate-a", speech_id="speech-a")
    await second.synthesize("第二个 Provider。", room_code="gate-b", speech_id="speech-b")
    assert acquired == 2 and released == 2


async def test_lighttts_production_gate_failure_stops_before_gpu_request(tmp_path: Path, monkeypatch) -> None:
    prompt = tmp_path / "debate_voice_1.wav"
    prompt.write_bytes(b"prompt")
    monkeypatch.setattr(settings, "lighttts_prompt_wav_path", str(prompt))
    monkeypatch.setattr(settings, "media_root", str(tmp_path / "media"))
    monkeypatch.setattr(settings, "lighttts_global_gate_enabled", True)
    calls = 0

    class UnavailableGate:
        async def acquire(self, _should_cancel=None, **_kwargs):
            raise LightTTSAdmissionUnavailable("Redis unavailable")

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=wav_bytes(0.8))

    monkeypatch.setattr(providers_service, "lighttts_admission_gate", UnavailableGate())
    provider = LightTTSProvider(httpx.MockTransport(handler))
    with pytest.raises(ProviderError, match="全局调度服务不可用"):
        await provider.synthesize("不得绕过全局 gate。", room_code="gate-fail", speech_id="speech")
    assert calls == 0


async def test_lighttts_production_rejects_lifecycle_fake_green_before_gpu_request(
    tmp_path: Path,
    monkeypatch,
) -> None:
    prompt = tmp_path / "debate_voice_1.wav"
    prompt.write_bytes(b"prompt")
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "lighttts_prompt_wav_path", str(prompt))
    monkeypatch.setattr(settings, "media_root", str(tmp_path / "media"))
    calls = 0

    async def not_ready(_endpoint: str) -> None:
        raise ProviderError(
            "LightTTS 未真正就绪，已拒绝新的语音任务。",
            code="lighttts_not_ready",
            retryable=True,
        )

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=wav_bytes(0.8))

    monkeypatch.setattr(providers_service, "require_lighttts_readiness", not_ready)
    provider = LightTTSProvider(httpx.MockTransport(handler))
    with pytest.raises(ProviderError) as raised:
        await provider.synthesize("不得向假绿服务分配任务。", room_code="not-ready", speech_id="speech")
    assert raised.value.code == "lighttts_not_ready"
    assert calls == 0


async def test_lighttts_health_exposes_gate_snapshot_and_fails_ready_when_enabled_redis_is_down(monkeypatch) -> None:
    async def readiness_check(_url: str) -> dict[str, object]:
        return {"ok": True, "service": "LightTTS", "orphan_count": 0}

    async def endpoint_check(_url: str, *, name: str, production: bool) -> dict[str, object]:
        assert production is True and name == "FunASR"
        return {"ok": True, "service": name}

    class UnhealthyGate:
        async def status_snapshot(self) -> dict[str, object]:
            return {
                "ok": False,
                "enabled": True,
                "fail_closed": True,
                "active": None,
                "queue_depth": None,
                "oldest_wait_seconds": None,
                "events": {
                    "enqueue": 0,
                    "acquire": 0,
                    "reject": 1,
                    "cancel": 0,
                    "timeout": 0,
                    "lease_lost": 0,
                },
            }

    monkeypatch.setattr(settings, "lighttts_global_gate_enabled", True)
    monkeypatch.setattr(system_health_service, "probe_lighttts_readiness", readiness_check)
    monkeypatch.setattr(system_health_service, "_endpoint_check", endpoint_check)
    monkeypatch.setattr(system_health_service, "lighttts_admission_gate", UnhealthyGate())
    checks = await system_health_service._provider_checks(production=True)
    assert checks["lighttts"]["ok"] is False
    assert checks["lighttts"]["admission_gate"]["fail_closed"] is True


async def test_websocket_endpoint_health_uses_a_valid_handshake() -> None:
    connections = 0

    async def handler(websocket) -> None:
        nonlocal connections
        connections += 1
        await websocket.wait_closed()

    server = await websockets.serve(handler, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        result = await system_health_service._endpoint_check(
            f"ws://127.0.0.1:{port}",
            name="FunASR",
            production=True,
        )
    finally:
        server.close()
        await server.wait_closed()

    assert result["ok"] is True
    assert result["service"] == "FunASR"
    assert connections == 1


def test_lighttts_splitter_attaches_trailing_punctuation_and_rejects_punctuation_only() -> None:
    text = f"{'甲' * 30}。"
    assert LightTTSProvider._split_text(text) == [text]
    assert LightTTSProvider._split_text(f"{'甲' * 30}……") == [f"{'甲' * 30}……"]
    with pytest.raises(ProviderError, match="可发音内容"):
        LightTTSProvider._split_text("……？！")


async def test_agent_provider_parses_json_response(monkeypatch) -> None:
    monkeypatch.setattr(settings, "agent_mock", False)

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/debate"
        return httpx.Response(200, json={"content": "正方完整发言"})

    provider = DebateAgentProvider(httpx.MockTransport(handler))
    result = await provider.generate(
        {"output": {"stream": False}},
        "http://agent.test/api/debate",
    )
    assert result == "正方完整发言"


async def test_agent_provider_reuses_one_keepalive_pool_and_closes_it(monkeypatch) -> None:
    monkeypatch.setattr(settings, "agent_mock", False)
    requests = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json={"content": f"第{requests}轮"})

    provider = DebateAgentProvider(httpx.MockTransport(handler))
    assert await provider.generate({"output": {"stream": False}}, "http://agent.test/api/debate") == "第1轮"
    pooled = provider._client
    assert pooled is not None and pooled.is_closed is False
    assert await provider.generate({"output": {"stream": False}}, "http://agent.test/api/debate") == "第2轮"
    assert provider._client is pooled
    await provider.aclose()
    assert pooled.is_closed is True
    assert provider._client is None


async def test_agent_provider_parses_sse_response(monkeypatch) -> None:
    monkeypatch.setattr(settings, "agent_mock", False)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content='data: {"delta":"第一句"}\n\ndata: {"delta":"第二句"}\n\ndata: [DONE]\n\n'.encode("utf-8"),
        )

    provider = DebateAgentProvider(httpx.MockTransport(handler))
    result = await provider.generate(
        {"output": {"stream": True}},
        "http://agent.test/api/debate",
    )
    assert result == "第一句第二句"


async def test_agent_provider_rejects_partial_sse_error_instead_of_marking_prefix_final(monkeypatch) -> None:
    monkeypatch.setattr(settings, "agent_mock", False)

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(
                'data: {"delta":{"content":"已经朗读但尚未完成——"}}\n\n'
                'data: {"error":{"code":"output_truncated","message":"达到输出上限"}}\n\n'
                "data: [DONE]\n\n"
            ).encode(),
        )

    provider = DebateAgentProvider(httpx.MockTransport(handler))
    with pytest.raises(ProviderError) as captured:
        _events = [
            event
            async for event in provider.generate_stream(
                {"output": {"stream": True}},
                "http://agent.test/api/debate",
            )
        ]
    assert captured.value.code == "output_truncated"
    assert captured.value.retryable is False


async def test_agent_provider_exposes_sse_deltas_before_authoritative_final(monkeypatch) -> None:
    monkeypatch.setattr(settings, "agent_mock", False)

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(
                'data: {"type":"delta","delta":"第一段，"}\n\n'
                'data: {"type":"delta","delta":"第二段。"}\n\n'
                'data: {"type":"final","content":"第一段，第二段。"}\n\n'
                "data: [DONE]\n\n"
            ).encode(),
        )

    provider = DebateAgentProvider(httpx.MockTransport(handler))
    events = [
        event
        async for event in provider.generate_stream(
            {"output": {"stream": True}},
            "http://agent.test/api/debate",
        )
    ]
    assert events == [
        {"type": "delta", "delta": "第一段，"},
        {"type": "delta", "delta": "第二段。"},
        {"type": "final", "content": "第一段，第二段。"},
    ]


async def test_agent_provider_never_exposes_thinking_or_reasoning_as_body_delta(monkeypatch) -> None:
    monkeypatch.setattr(settings, "agent_mock", False)

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(
                'data: {"type":"thinking","delta":"内部思考不得朗读"}\n\n'
                'data: {"choices":[{"delta":{"reasoning_content":"也不得朗读","content":"正文第一句，"}}]}\n\n'
                'data: {"type":"reasoning_delta","delta":"隐藏推理"}\n\n'
                'data: {"type":"delta","delta":"正文第二句。"}\n\n'
                'data: {"type":"final","content":"正文第一句，正文第二句。"}\n\n'
                "data: [DONE]\n\n"
            ).encode(),
        )

    provider = DebateAgentProvider(httpx.MockTransport(handler))
    events = [
        event
        async for event in provider.generate_stream(
            {"output": {"stream": True}},
            "http://agent.test/debate/api/debate",
        )
    ]
    assert events == [
        {"type": "delta", "delta": "正文第一句，"},
        {"type": "delta", "delta": "正文第二句。"},
        {"type": "final", "content": "正文第一句，正文第二句。"},
    ]


async def test_agent_provider_interrupt_uses_task_endpoint_and_gateway_secret(monkeypatch) -> None:
    monkeypatch.setattr(settings, "agent_mock", False)

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.raw_path == b"/debate/api/tasks/speech%2F1/interrupt"
        assert request.headers["X-Debate-Agent-Key"] == "interrupt-secret"
        return httpx.Response(200, json={"ok": True, "status": "interrupted"})

    provider = DebateAgentProvider(httpx.MockTransport(handler))
    assert await provider.interrupt(
        "speech/1",
        provider_config={
            "endpoint": "https://agent.test/debate/api/debate",
            "enabled": True,
            "secret_ciphertext": encrypt_secret("interrupt-secret"),
            "settings": {},
        },
    ) is True


async def test_agent_provider_uses_restful_gateway_snapshot_and_final_event(monkeypatch) -> None:
    monkeypatch.setattr(settings, "agent_mock", False)

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.headers["X-Debate-Agent-Key"] == "gateway-secret"
        body = json.loads(await request.aread())
        assert body["output"]["stream"] is True
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(
                'data: {"type":"delta","delta":"临时"}\n\n'
                'data: {"type":"final","content":"最终完整发言"}\n\n'
                "data: [DONE]\n\n"
            ).encode(),
        )

    provider = DebateAgentProvider(httpx.MockTransport(handler))
    result = await provider.generate(
        {"output": {"stream": False}},
        provider_config={
            "endpoint": "https://agent.test/debate/api/debate",
            "enabled": True,
            "secret_ciphertext": encrypt_secret("gateway-secret"),
            "settings": {
                "method": "POST",
                "protocol": "restful",
                "health_endpoint": "https://agent.test/debate/api/health",
                "timeout_seconds": 45,
                "stream": True,
            },
        },
    )
    assert result == "最终完整发言"


async def test_judge_provider_uses_frozen_profile_and_validates_result(monkeypatch) -> None:
    monkeypatch.setattr(settings, "agent_mock", False)

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and request.url == httpx.URL("http://judge.test/api/judge")
        payload = json.loads(await request.aread())
        assert payload["model_name"] == "judge-27b"
        assert payload["system_prompt"] == "依据论证质量公平裁决"
        assert payload["debate_topic"] == "测试辩题"
        return httpx.Response(
            200,
            json={
                "output": {
                    "winner": "正方",
                    "affirmative_score": 88.123,
                    "negative_score": 84,
                    "individual_scores": {"aff_1": 89},
                    "reasoning": "正方论证更完整。",
                }
            },
        )

    provider = JudgeProvider(httpx.MockTransport(handler))
    result = await provider.judge(
        "测试辩题",
        [{"seat_key": "aff_1", "content": "正方发言"}],
        profile={
            "endpoint": "http://judge.test/api/judge",
            "model_name": "judge-27b",
            "system_prompt": "依据论证质量公平裁决",
            "timeout_seconds": 30,
        },
    )
    assert result == {
        "winner": "aff",
        "affirmative_score": 88.12,
        "negative_score": 84.0,
        "individual_scores": {"aff_1": 89},
        "reasoning": "正方论证更完整。",
    }


@pytest.mark.parametrize(
    "raw, message",
    [
        ({"winner": "unknown", "affirmative_score": 80, "negative_score": 80, "reasoning": "理由"}, "胜方"),
        ({"winner": "aff", "affirmative_score": float("nan"), "negative_score": 80, "reasoning": "理由"}, "0–100"),
        ({"winner": "aff", "affirmative_score": 101, "negative_score": 80, "reasoning": "理由"}, "0–100"),
        ({"winner": "aff", "affirmative_score": 80, "negative_score": 79, "reasoning": ""}, "判定理由"),
        (["invalid"], "格式"),
    ],
)
def test_judge_provider_rejects_unsafe_results(raw, message: str) -> None:
    with pytest.raises(ProviderError, match=message):
        JudgeProvider()._normalize_score(raw)


async def test_lighttts_wraps_pcm_as_playable_wav(tmp_path: Path, monkeypatch) -> None:
    prompt = tmp_path / "prompt.wav"
    prompt.write_bytes(b"RIFF-test-prompt")
    monkeypatch.setattr(settings, "lighttts_prompt_wav_path", str(prompt))
    monkeypatch.setattr(settings, "lighttts_prompt_text", "测试提示音")
    monkeypatch.setattr(settings, "media_root", str(tmp_path / "media"))

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/inference_zero_shot"
        return httpx.Response(200, content=b"\x01\x00\x02\x00" * 6000)

    provider = LightTTSProvider(httpx.MockTransport(handler))
    url = await provider.synthesize("测试语音", room_code="123456", speech_id="speech-1")
    target = tmp_path / "media" / "123456" / "speech-1.wav"
    assert url == "/media/123456/speech-1.wav"
    assert target.read_bytes().startswith(b"RIFF")


class FakeBiStreamWebSocket:
    def __init__(self, incoming: list[bytes | str | None]) -> None:
        self.incoming = list(incoming)
        self.sent: list[bytes | str] = []

    async def send(self, value: bytes | str) -> None:
        self.sent.append(value)

    async def recv(self) -> bytes | str | None:
        await asyncio.sleep(0)
        return self.incoming.pop(0)


class FakeBiStreamConnection:
    def __init__(self, websocket: FakeBiStreamWebSocket) -> None:
        self.websocket = websocket

    async def __aenter__(self) -> FakeBiStreamWebSocket:
        return self.websocket

    async def __aexit__(self, *_args) -> None:
        return None


class IncrementalBiStreamWebSocket:
    def __init__(self, audio: bytes) -> None:
        self.audio = audio
        self.sent: list[bytes | str] = []
        self.finish_seen = asyncio.Event()
        self.audio_sent = False

    async def send(self, value: bytes | str) -> None:
        self.sent.append(value)
        if isinstance(value, str) and json.loads(value).get("finish") is True:
            self.finish_seen.set()

    async def recv(self) -> bytes | str | None:
        if not self.audio_sent:
            while not any(
                isinstance(item, str) and "tts_text" in json.loads(item)
                for item in self.sent
            ):
                await asyncio.sleep(0)
            self.audio_sent = True
            return self.audio
        await self.finish_seen.wait()
        return None


async def test_lighttts_bistream_publishes_the_same_pcm_as_the_final_wav(tmp_path: Path, monkeypatch) -> None:
    prompt = tmp_path / "debate_voice_1.wav"
    prompt.write_bytes(b"prompt-wav")
    media = tmp_path / "media"
    first = b"\x01\x00" * 12_000
    second = b"\x02\x00" * 12_000
    websocket = FakeBiStreamWebSocket([first, second, None])
    calls: list[tuple[str, dict[str, object]]] = []

    def connect(url: str, **_kwargs) -> FakeBiStreamConnection:
        calls.append((url, {}))
        return FakeBiStreamConnection(websocket)

    async def on_stream_event(event: dict[str, object]) -> None:
        calls.append((str(event["type"]), event))
        if event["type"] == "audio.stream.started":
            part = media / "stream-room" / f".stream-speech.{event['generation']}.wav.part"
            assert part.is_file()
            assert part.stat().st_size > 44

    monkeypatch.setattr(settings, "lighttts_prompt_wav_path", str(prompt))
    monkeypatch.setattr(settings, "lighttts_prompt_text", "提示文本")
    monkeypatch.setattr(settings, "media_root", str(media))
    monkeypatch.setattr(settings, "lighttts_streaming_enabled", True)
    monkeypatch.setattr(settings, "lighttts_bistream_enabled", True)
    provider = LightTTSProvider(stream_connect=connect)
    url = await provider.synthesize(
        "实时流式语音测试。",
        room_code="stream-room",
        speech_id="stream-speech",
        on_stream_event=on_stream_event,
    )

    target = media / "stream-room" / "stream-speech.wav"
    assert url == "/media/stream-room/stream-speech.wav"
    _sample_rate, samples = read_pcm16_samples(target)
    assert samples == ([1] * 12_000) + ([2] * 12_000)
    assert calls[0][0].endswith("/inference_zero_shot_bistream")
    started = next(payload for name, payload in calls if name == "audio.stream.started")
    assert started["stream_url"] == "/ws/rooms/stream-room/audio"
    sent_json = [json.loads(item) for item in websocket.sent if isinstance(item, str)]
    assert sent_json[0] == {"prompt_text": "提示文本", "tts_model_name": "default"}
    assert sent_json[-1] == {"finish": True}
    assert not list((media / "stream-room").glob("*.part"))


async def test_lighttts_incremental_session_sends_agent_clauses_before_finish(tmp_path: Path, monkeypatch) -> None:
    prompt = tmp_path / "debate_voice_1.wav"
    prompt.write_bytes(b"prompt-wav")
    media = tmp_path / "media"
    websocket = IncrementalBiStreamWebSocket(b"\x01\x00" * 24_000)
    events: list[str] = []

    def connect(_url: str, **_kwargs) -> FakeBiStreamConnection:
        return FakeBiStreamConnection(websocket)

    async def on_stream_event(event: dict[str, object]) -> None:
        events.append(str(event["type"]))

    monkeypatch.setattr(settings, "lighttts_prompt_wav_path", str(prompt))
    monkeypatch.setattr(settings, "lighttts_prompt_text", "提示文本")
    monkeypatch.setattr(settings, "media_root", str(media))
    monkeypatch.setattr(settings, "lighttts_streaming_enabled", True)
    monkeypatch.setattr(settings, "lighttts_bistream_enabled", True)
    monkeypatch.setattr(settings, "lighttts_global_gate_enabled", False)
    provider = LightTTSProvider(stream_connect=connect)
    session = provider.open_incremental_session(
        room_code="delta-room",
        speech_id="delta-speech",
        on_stream_event=on_stream_event,
    )

    await session.push_text("第一段。")
    for _attempt in range(100):
        controls = [json.loads(item) for item in websocket.sent if isinstance(item, str)]
        if any(item.get("tts_text") == "第一段。" for item in controls):
            break
        await asyncio.sleep(0)
    else:
        pytest.fail("首个 Agent 子句未在 finish 前发送到 TTS")
    assert not websocket.finish_seen.is_set()

    await session.push_text("第二段。")
    url = await session.finish()

    controls = [json.loads(item) for item in websocket.sent if isinstance(item, str)]
    assert [item["tts_text"] for item in controls if "tts_text" in item] == ["第一段。", "第二段。"]
    assert controls[-1] == {"finish": True}
    assert events == ["audio.stream.started"]
    assert url == "/media/delta-room/delta-speech.wav"
    assert (media / "delta-room" / "delta-speech.wav").is_file()


async def test_lighttts_completed_wav_publishes_through_livekit_without_bistream(tmp_path: Path, monkeypatch) -> None:
    media = tmp_path / "media"
    target = media / "wav-room" / "wav-speech.wav"
    target.parent.mkdir(parents=True)
    target.write_bytes(wav_bytes(0.3))
    calls: list[tuple[str, object]] = []
    events: list[dict[str, object]] = []

    async def start_generation(room_code: str, speech_id: str, generation: str, sample_rate: int) -> str:
        calls.append(("start", (room_code, speech_id, generation, sample_rate)))
        return "TR_wav-room"

    async def write_pcm(room_code: str, generation: str, pcm16: bytes) -> str:
        calls.append(("write", (room_code, generation, len(pcm16))))
        return "2026-07-18T00:00:00+00:00"

    async def finish_generation(room_code: str, generation: str) -> None:
        calls.append(("finish", (room_code, generation)))

    async def abort_generation(room_code: str, generation: str) -> bool:
        calls.append(("abort", (room_code, generation)))
        return True

    async def on_stream_event(event: dict[str, object]) -> None:
        events.append(event)

    monkeypatch.setattr(settings, "media_root", str(media))
    monkeypatch.setattr(settings, "webrtc_audio_enabled", True)
    monkeypatch.setattr(settings, "webrtc_audio_backend", "livekit")
    monkeypatch.setattr(settings, "lighttts_streaming_enabled", False)
    monkeypatch.setattr(settings, "lighttts_bistream_enabled", False)
    monkeypatch.setattr(providers_service.livekit_audio_registry, "start_generation", start_generation)
    monkeypatch.setattr(providers_service.livekit_audio_registry, "write_pcm", write_pcm)
    monkeypatch.setattr(providers_service.livekit_audio_registry, "finish_generation", finish_generation)
    monkeypatch.setattr(providers_service.livekit_audio_registry, "abort_generation", abort_generation)

    generation = await LightTTSProvider().publish_wav_to_livekit(
        room_code="wav-room",
        speech_id="wav-speech",
        should_cancel=lambda: False,
        on_stream_event=on_stream_event,
    )

    assert generation
    assert [name for name, _payload in calls].count("start") == 1
    assert [name for name, _payload in calls].count("write") == 3
    assert [name for name, _payload in calls].count("finish") == 1
    assert not any(name == "abort" for name, _payload in calls)
    assert events == [
        {
            "type": "audio.stream.started",
            "room_code": "wav-room",
            "speech_id": "wav-speech",
            "generation": generation,
            "stream_url": None,
            "sample_rate": 24_000,
            "channels": 1,
            "sample_width": 2,
            "transport": "livekit",
            "track_sid": "TR_wav-room",
            "server_first_capture_at": "2026-07-18T00:00:00+00:00",
        }
    ]


async def test_lighttts_completed_wav_livekit_publish_aborts_and_clears_generation(tmp_path: Path, monkeypatch) -> None:
    media = tmp_path / "media"
    target = media / "cancel-wav-room" / "cancel-wav-speech.wav"
    target.parent.mkdir(parents=True)
    target.write_bytes(wav_bytes(0.3))
    cancelled = False
    aborted: list[tuple[str, str]] = []
    events: list[str] = []

    async def start_generation(_room_code: str, _speech_id: str, _generation: str, _sample_rate: int) -> str:
        return "TR_cancel-wav-room"

    async def write_pcm(_room_code: str, _generation: str, _pcm16: bytes) -> str:
        nonlocal cancelled
        cancelled = True
        return "2026-07-18T00:00:00+00:00"

    async def abort_generation(room_code: str, generation: str) -> bool:
        aborted.append((room_code, generation))
        return True

    async def on_stream_event(event: dict[str, object]) -> None:
        events.append(str(event["type"]))

    monkeypatch.setattr(settings, "media_root", str(media))
    monkeypatch.setattr(settings, "webrtc_audio_enabled", True)
    monkeypatch.setattr(settings, "webrtc_audio_backend", "livekit")
    monkeypatch.setattr(providers_service.livekit_audio_registry, "start_generation", start_generation)
    monkeypatch.setattr(providers_service.livekit_audio_registry, "write_pcm", write_pcm)
    monkeypatch.setattr(providers_service.livekit_audio_registry, "abort_generation", abort_generation)

    with pytest.raises(ProviderCancelled):
        await LightTTSProvider().publish_wav_to_livekit(
            room_code="cancel-wav-room",
            speech_id="cancel-wav-speech",
            should_cancel=lambda: cancelled,
            on_stream_event=on_stream_event,
        )

    assert len(aborted) == 1
    assert events == ["audio.stream.started", "audio.stream.aborted"]


def test_lighttts_incremental_session_requires_separate_verified_bistream_gate(monkeypatch) -> None:
    async def on_stream_event(_event) -> None:
        return None

    monkeypatch.setattr(settings, "lighttts_streaming_enabled", True)
    monkeypatch.setattr(settings, "lighttts_bistream_enabled", False)
    with pytest.raises(ProviderError, match="bi-stream"):
        LightTTSProvider().open_incremental_session(
            room_code="gate-room",
            speech_id="gate-speech",
            on_stream_event=on_stream_event,
        )


class FakeMossAudioStream(httpx.AsyncByteStream):
    def __init__(self, final_seen: asyncio.Event, *, wait_for_final: bool = True) -> None:
        self.final_seen = final_seen
        self.wait_for_final = wait_for_final

    async def __aiter__(self):
        yield b"\x01\x00" * 24_000
        if self.wait_for_final:
            await self.final_seen.wait()
            yield b"\x02\x00" * 24_000


class StalledMossAudioStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        await asyncio.Event().wait()
        yield b""  # pragma: no cover - keeps this an async generator


class ShortMossAudioStream(httpx.AsyncByteStream):
    def __init__(self, final_seen: asyncio.Event) -> None:
        self.final_seen = final_seen

    async def __aiter__(self):
        yield b"\x03\x00" * 7_200
        await self.final_seen.wait()


def moss_ready_response(*, active: int = 0) -> httpx.Response:
    return httpx.Response(
        200,
        json={"ok": True, "model_warmed": True, "active": active, "pending": 0, "orphan_count": 0},
    )


def use_http_moss(monkeypatch) -> None:
    monkeypatch.setattr(settings, "moss_tts_realtime_transport", "http")


class FakePersistentMossWebSocket:
    def __init__(self, on_send) -> None:
        class Transport:
            def __init__(self) -> None:
                self.aborted = False

            def abort(self) -> None:
                self.aborted = True

        self.on_send = on_send
        self.incoming: asyncio.Queue[bytes | str | BaseException] = asyncio.Queue()
        self.sent: list[dict[str, object]] = []
        self.recv_active = 0
        self.max_recv_active = 0
        self.session_id = ""
        self.transport = Transport()

    async def send(self, raw: str) -> None:
        payload = json.loads(raw)
        self.sent.append(payload)
        await self.on_send(self, payload)

    async def recv(self) -> bytes | str:
        self.recv_active += 1
        self.max_recv_active = max(self.max_recv_active, self.recv_active)
        try:
            item = await self.incoming.get()
        finally:
            self.recv_active -= 1
        if isinstance(item, BaseException):
            raise item
        return item

    def feed_json(self, payload: dict[str, object]) -> None:
        self.incoming.put_nowait(json.dumps(payload, ensure_ascii=False))

    def feed_pcm(self, samples: int = 24_000) -> None:
        self.incoming.put_nowait(b"\x01\x00" * samples)

    def feed_error(self, error: BaseException) -> None:
        self.incoming.put_nowait(error)


class FakePersistentMossConnection:
    def __init__(self, websocket: FakePersistentMossWebSocket) -> None:
        self.websocket = websocket
        self.exit_count = 0

    async def __aenter__(self) -> FakePersistentMossWebSocket:
        return self.websocket

    async def __aexit__(self, _exc_type, _exc, _traceback) -> None:
        self.exit_count += 1
        return None


def configure_ws_moss(monkeypatch, tmp_path: Path, *, endpoints: list[str]) -> None:
    monkeypatch.setattr(settings, "media_root", str(tmp_path / "media"))
    monkeypatch.setattr(settings, "moss_tts_realtime_enabled", True)
    monkeypatch.setattr(settings, "moss_tts_realtime_transport", "websocket")
    monkeypatch.setattr(settings, "moss_tts_realtime_url", "")
    monkeypatch.setattr(settings, "moss_tts_realtime_urls", endpoints)
    monkeypatch.setattr(settings, "moss_tts_realtime_max_active", len(endpoints))
    monkeypatch.setattr(settings, "moss_tts_realtime_max_active_per_endpoint", 1)
    monkeypatch.setattr(settings, "moss_tts_prompt_files", {"debate_voice_1": "voice-1.wav"})


async def moss_ready_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/health/ready"):
        return moss_ready_response()
    return httpx.Response(404)


async def normal_moss_ws_script(websocket: FakePersistentMossWebSocket, payload: dict[str, object]) -> None:
    event_type = payload["type"]
    if event_type == "ping":
        websocket.feed_json({"type": "pong", "id": payload["id"]})
    elif event_type == "health":
        websocket.feed_json({"type": "health", "ok": True, "active": 0})
    elif event_type == "start":
        websocket.session_id = str(payload["session_id"])
        websocket.feed_json(
            {
                "type": "ready",
                "session_id": websocket.session_id,
                "voice": payload["voice"],
                "next_seq": 1,
                "audio": {"sample_rate": 24_000, "channels": 1, "codec": "pcm_s16le"},
            }
        )
    elif event_type == "text_delta":
        if payload["seq"] == 1:
            websocket.feed_pcm()
        websocket.feed_json({"type": "ack", "seq": payload["seq"]})
    elif event_type == "final":
        websocket.feed_json({"type": "ack", "seq": payload["seq"]})
        websocket.feed_json({"type": "audio_end", "session_id": websocket.session_id})
        websocket.feed_json(
            {
                "type": "released",
                "session_id": websocket.session_id,
                "status": "closed",
                "released": True,
            }
        )
    elif event_type == "abort":
        websocket.feed_json({"type": "audio_reset", "session_id": websocket.session_id})
        websocket.feed_json(
            {
                "type": "released",
                "session_id": websocket.session_id,
                "status": "aborted",
                "released": True,
            }
        )


async def test_moss_ws_uses_one_persistent_connection_for_start_deltas_pcm_and_release(
    tmp_path: Path,
    monkeypatch,
) -> None:
    configure_ws_moss(monkeypatch, tmp_path, endpoints=["https://moss.internal:8443/api"])
    monkeypatch.setattr(settings, "moss_tts_realtime_api_key", "gateway-key")
    connections: list[tuple[str, dict[str, object], FakePersistentMossWebSocket]] = []

    def connect(url: str, **kwargs):
        websocket = FakePersistentMossWebSocket(normal_moss_ws_script)
        connections.append((url, kwargs, websocket))
        return FakePersistentMossConnection(websocket)

    events: list[str] = []

    async def on_stream_event(event: dict[str, object]) -> None:
        events.append(str(event["type"]))

    provider = MossTTSRealtimeProvider(httpx.MockTransport(moss_ready_handler), ws_connect=connect)
    session = provider.open_incremental_session(
        room_code="moss-ws-room",
        speech_id="moss-ws-speech",
        voice="debate_voice_1",
        on_stream_event=on_stream_event,
    )

    await session.push_text("第一段。")
    await session.push_text("第二段。")
    url = await session.finish()

    assert url == "/media/moss-ws-room/moss-ws-speech.wav"
    turn_connections = [
        item for item in connections if any(payload["type"] == "start" for payload in item[2].sent)
    ]
    assert len(turn_connections) == 1
    ws_url, kwargs, websocket = turn_connections[0]
    assert ws_url == "wss://moss.internal:8443/api/tts/session/ws"
    assert kwargs["additional_headers"] == {"X-MOSS-Gateway-Key": "gateway-key"}
    assert [payload["type"] for payload in websocket.sent] == ["start", "text_delta", "text_delta", "final"]
    assert [payload["seq"] for payload in websocket.sent if "seq" in payload] == [0, 1, 2, 3]
    assert [payload["text"] for payload in websocket.sent if payload["type"] == "text_delta"] == [
        "第一段。",
        "第二段。",
    ]
    assert websocket.sent[0] == {
        "type": "start",
        "seq": 0,
        "session_id": websocket.session_id,
        "voice": "debate_voice_1",
        "user_text": settings.moss_tts_realtime_speech_instruction,
    }
    assert websocket.sent[-1] == {"type": "final", "seq": 3}
    assert websocket.max_recv_active == 1
    assert events == ["audio.stream.started"]
    assert all(
        not any(payload["type"] == "start" for payload in idle_websocket.sent)
        for _url, _kwargs, idle_websocket in connections
        if idle_websocket is not websocket
    )
    target = tmp_path / "media" / "moss-ws-room" / "moss-ws-speech.wav"
    assert target.is_file() and read_pcm16_samples(target)[0] == 24_000


async def test_moss_ws_final_ack_uses_job_deadline_not_short_delta_ack_timeout(
    tmp_path: Path,
    monkeypatch,
) -> None:
    configure_ws_moss(monkeypatch, tmp_path, endpoints=["https://moss-final.internal:8443/api"])
    monkeypatch.setattr(settings, "moss_tts_realtime_ack_timeout_seconds", 0.02)
    monkeypatch.setattr(settings, "moss_tts_realtime_job_timeout_seconds", 0.5)
    monkeypatch.setattr(settings, "moss_tts_realtime_close_timeout_seconds", 0.2)
    monkeypatch.setattr(LightTTSProvider, "MIN_AUDIO_DURATION_SECONDS", 0.01)
    monkeypatch.setattr(LightTTSProvider, "MIN_SECONDS_PER_VISIBLE_CHARACTER", 0.0001)

    async def script(websocket: FakePersistentMossWebSocket, payload: dict[str, object]) -> None:
        event_type = payload["type"]
        if event_type == "start":
            websocket.session_id = str(payload["session_id"])
            websocket.feed_json(
                {
                    "type": "ready",
                    "session_id": websocket.session_id,
                    "voice": payload["voice"],
                    "next_seq": 1,
                    "audio": {"sample_rate": 24_000, "channels": 1, "codec": "pcm_s16le"},
                }
            )
        elif event_type == "text_delta":
            websocket.feed_pcm(2_400)
            websocket.feed_json({"type": "ack", "seq": payload["seq"]})
        elif event_type == "final":
            async def complete_after_model_finishes() -> None:
                await asyncio.sleep(0.08)
                websocket.feed_json({"type": "ack", "seq": payload["seq"]})
                websocket.feed_json({"type": "audio_end", "session_id": websocket.session_id})
                websocket.feed_json(
                    {
                        "type": "released",
                        "session_id": websocket.session_id,
                        "status": "closed",
                        "released": True,
                    }
                )

            asyncio.create_task(complete_after_model_finishes())

    def connect(_url: str, **_kwargs):
        return FakePersistentMossConnection(FakePersistentMossWebSocket(script))

    provider = MossTTSRealtimeProvider(httpx.MockTransport(moss_ready_handler), ws_connect=connect)
    session = provider.open_incremental_session(
        room_code="moss-final-room",
        speech_id="moss-final-speech",
        voice="debate_voice_1",
        on_stream_event=lambda _event: asyncio.sleep(0),
    )
    await session.push_text("完整长发言的最终确认不应使用短增量超时。")
    assert await session.finish() == "/media/moss-final-room/moss-final-speech.wav"


async def test_moss_background_synthesis_writes_wav_without_publishing_livekit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    configure_ws_moss(monkeypatch, tmp_path, endpoints=["https://moss.internal:8443/api"])
    monkeypatch.setattr(settings, "webrtc_audio_enabled", True)
    monkeypatch.setattr(settings, "webrtc_audio_backend", "livekit")
    connections: list[FakePersistentMossWebSocket] = []

    def connect(_url: str, **_kwargs):
        websocket = FakePersistentMossWebSocket(normal_moss_ws_script)
        connections.append(websocket)
        return FakePersistentMossConnection(websocket)

    async def forbidden_livekit_start(*_args, **_kwargs):
        raise AssertionError("background cue synthesis must not publish the room track")

    monkeypatch.setattr(providers_service.livekit_audio_registry, "start_generation", forbidden_livekit_start)
    provider = MossTTSRealtimeProvider(httpx.MockTransport(moss_ready_handler), ws_connect=connect)

    url = await provider.synthesize(
        "比赛即将开始。",
        room_code="moss-cue-room",
        speech_id="moss-cue-speech",
        publish_live=False,
    )

    assert url == "/media/moss-cue-room/moss-cue-speech.wav"
    assert (tmp_path / "media" / "moss-cue-room" / "moss-cue-speech.wav").is_file()
    assert any(any(payload["type"] == "start" for payload in websocket.sent) for websocket in connections)


async def test_moss_livekit_merges_small_upstream_chunks_before_first_write(
    tmp_path: Path,
    monkeypatch,
) -> None:
    configure_ws_moss(monkeypatch, tmp_path, endpoints=["http://moss-buffer.internal:8083"])
    monkeypatch.setattr(settings, "webrtc_audio_enabled", True)
    monkeypatch.setattr(settings, "webrtc_audio_backend", "livekit")
    monkeypatch.setattr(settings, "moss_tts_livekit_start_buffer_ms", 800)
    monkeypatch.setattr(settings, "moss_tts_realtime_first_pcm_timeout_seconds", 0.1)
    monkeypatch.setattr(LightTTSProvider, "MIN_AUDIO_DURATION_SECONDS", 0.01)
    monkeypatch.setattr(LightTTSProvider, "MIN_SECONDS_PER_VISIBLE_CHARACTER", 0.0001)
    writes: list[int] = []
    starts: list[int | None] = []
    events: list[dict[str, object]] = []

    async def script(websocket: FakePersistentMossWebSocket, payload: dict[str, object]) -> None:
        event_type = payload["type"]
        if event_type == "start":
            websocket.session_id = str(payload["session_id"])
            websocket.feed_json(
                {
                    "type": "ready",
                    "session_id": websocket.session_id,
                    "voice": payload["voice"],
                    "next_seq": 1,
                    "audio": {"sample_rate": 24_000, "channels": 1, "codec": "pcm_s16le"},
                }
            )
        elif event_type == "text_delta":
            websocket.feed_pcm(24_000 * 240 // 1_000)
            # Upstream PCM is healthy even though the LiveKit start buffer is
            # intentionally not ready before the first-PCM deadline.
            await asyncio.sleep(0.15)
            for duration_ms in (80, 160, 320):
                websocket.feed_pcm(24_000 * duration_ms // 1_000)
            websocket.feed_json({"type": "ack", "seq": payload["seq"]})
        elif event_type == "final":
            websocket.feed_json({"type": "ack", "seq": payload["seq"]})
            websocket.feed_json({"type": "audio_end", "session_id": websocket.session_id})
            websocket.feed_json(
                {
                    "type": "released",
                    "session_id": websocket.session_id,
                    "status": "closed",
                    "released": True,
                }
            )
        elif event_type == "abort":
            websocket.feed_json({"type": "audio_reset", "session_id": websocket.session_id})
            websocket.feed_json(
                {
                    "type": "released",
                    "session_id": websocket.session_id,
                    "status": "aborted",
                    "released": True,
                }
            )

    def connect(_url: str, **_kwargs):
        return FakePersistentMossConnection(FakePersistentMossWebSocket(script))

    async def ensure_room(_room_code: str) -> None:
        return None

    async def start_generation(
        _room_code: str,
        _speech_id: str,
        _generation: str,
        _sample_rate: int,
        *,
        start_buffer_ms: int | None = None,
    ) -> str:
        starts.append(start_buffer_ms)
        return "TR_moss-buffer"

    async def write_pcm(_room_code: str, _generation: str, audio: bytes) -> str:
        writes.append(len(audio))
        return "2026-07-18T00:00:00+00:00"

    async def finish_generation(_room_code: str, _generation: str) -> str | None:
        return None

    async def on_stream_event(event: dict[str, object]) -> None:
        events.append(event)

    monkeypatch.setattr(providers_service.livekit_audio_registry, "ensure_room", ensure_room)
    monkeypatch.setattr(providers_service.livekit_audio_registry, "start_generation", start_generation)
    monkeypatch.setattr(providers_service.livekit_audio_registry, "write_pcm", write_pcm)
    monkeypatch.setattr(providers_service.livekit_audio_registry, "finish_generation", finish_generation)

    provider = MossTTSRealtimeProvider(httpx.MockTransport(moss_ready_handler), ws_connect=connect)
    session = provider.open_incremental_session(
        room_code="moss-buffer-room",
        speech_id="moss-buffer-speech",
        voice="debate_voice_1",
        on_stream_event=on_stream_event,
    )
    await session.push_text("预缓冲测试。")
    await session.finish()

    assert starts == [800]
    assert writes == [24_000 * 800 // 1_000 * 2]
    started = next(event for event in events if event["type"] == "audio.stream.started")
    assert started["server_first_capture_at"] == "2026-07-18T00:00:00+00:00"


async def test_moss_prewarm_is_atomic_reused_by_first_turn_and_refilled(
    tmp_path: Path,
    monkeypatch,
) -> None:
    configure_ws_moss(monkeypatch, tmp_path, endpoints=["http://moss-prewarm.internal:8083"])
    monkeypatch.setattr(LightTTSProvider, "MIN_AUDIO_DURATION_SECONDS", 0.01)
    monkeypatch.setattr(LightTTSProvider, "MIN_SECONDS_PER_VISIBLE_CHARACTER", 0.0001)
    connections: list[FakePersistentMossConnection] = []

    def connect(_url: str, **_kwargs):
        connection = FakePersistentMossConnection(FakePersistentMossWebSocket(normal_moss_ws_script))
        connections.append(connection)
        return connection

    provider = MossTTSRealtimeProvider(httpx.MockTransport(moss_ready_handler), ws_connect=connect)
    snapshots = await asyncio.gather(provider.prewarm(), provider.prewarm())
    assert all(snapshot["ok"] is True for snapshot in snapshots)
    assert all(snapshot["idle_ws_ready_endpoints"] == 1 for snapshot in snapshots)
    assert len(connections) == 1
    assert [payload["type"] for payload in connections[0].websocket.sent] == ["ping", "health"]

    async def on_stream_event(_event: dict[str, object]) -> None:
        return None

    session = provider.open_incremental_session(
        room_code="moss-prewarm-room",
        speech_id="moss-prewarm-speech",
        voice="debate_voice_1",
        on_stream_event=on_stream_event,
    )
    await session.push_text("首轮必须原子领用已经认证的空闲连接。")
    await session.finish()
    refill_deadline = asyncio.get_running_loop().time() + 1
    while len(connections) < 2 and asyncio.get_running_loop().time() < refill_deadline:
        await asyncio.sleep(0.01)

    assert len(connections) == 2
    assert [payload["type"] for payload in connections[0].websocket.sent] == [
        "ping",
        "health",
        "ping",
        "start",
        "text_delta",
        "final",
    ]
    assert connections[0].exit_count == 1
    assert [payload["type"] for payload in connections[1].websocket.sent] == ["ping", "health"]
    assert not any(payload["type"] == "start" for payload in connections[1].websocket.sent)
    await provider.aclose()
    assert connections[1].exit_count == 1


@pytest.mark.parametrize("stale_mode", ["expired", "disconnected"])
async def test_moss_prewarm_discards_stale_idle_connection_before_start(
    stale_mode: str,
    tmp_path: Path,
    monkeypatch,
) -> None:
    configure_ws_moss(monkeypatch, tmp_path, endpoints=["http://moss-stale.internal:8083"])
    monkeypatch.setattr(LightTTSProvider, "MIN_AUDIO_DURATION_SECONDS", 0.01)
    monkeypatch.setattr(LightTTSProvider, "MIN_SECONDS_PER_VISIBLE_CHARACTER", 0.0001)
    connections: list[FakePersistentMossConnection] = []

    def connect(_url: str, **_kwargs):
        connection = FakePersistentMossConnection(FakePersistentMossWebSocket(normal_moss_ws_script))
        connections.append(connection)
        return connection

    provider = MossTTSRealtimeProvider(httpx.MockTransport(moss_ready_handler), ws_connect=connect)
    assert (await provider.prewarm())["idle_ws_ready_endpoints"] == 1
    idle = provider._idle_websockets["http://moss-stale.internal:8083"]
    if stale_mode == "expired":
        idle.created_at -= settings.moss_tts_realtime_idle_ws_ttl_seconds + 1
    else:
        idle.websocket.close_code = 1006

    async def on_stream_event(_event: dict[str, object]) -> None:
        return None

    session = provider.open_incremental_session(
        room_code=f"moss-stale-{stale_mode}",
        speech_id="speech",
        voice="debate_voice_1",
        on_stream_event=on_stream_event,
    )
    await session.push_text("失效连接不得承载 start。")
    await session.finish()
    assert len(connections) >= 2
    assert connections[0].exit_count == 1
    assert not any(payload["type"] == "start" for payload in connections[0].websocket.sent)
    assert any(payload["type"] == "start" for payload in connections[1].websocket.sent)
    await provider.aclose()


def test_moss_prewarm_isolates_idle_connections_between_event_loops(tmp_path: Path, monkeypatch) -> None:
    configure_ws_moss(monkeypatch, tmp_path, endpoints=["http://moss-loop.internal:8083"])
    connections: list[FakePersistentMossConnection] = []

    def connect(_url: str, **_kwargs):
        connection = FakePersistentMossConnection(FakePersistentMossWebSocket(normal_moss_ws_script))
        connections.append(connection)
        return connection

    provider = MossTTSRealtimeProvider(httpx.MockTransport(moss_ready_handler), ws_connect=connect)
    assert asyncio.run(provider.prewarm())["idle_ws_ready_endpoints"] == 1
    first = connections[0].websocket
    assert first.transport.aborted is False

    assert asyncio.run(provider.prewarm())["idle_ws_ready_endpoints"] == 1
    assert len(connections) == 2
    assert first.transport.aborted is True
    second = connections[1].websocket
    assert second.transport.aborted is False

    asyncio.run(provider.aclose())
    assert second.transport.aborted is True


async def test_moss_ws_integrates_with_real_gateway_asgi_and_websockets(
    tmp_path: Path,
    monkeypatch,
) -> None:
    gateway_root = Path(__file__).resolve().parents[3] / "services" / "moss-realtime-gateway"
    sys.path.insert(0, str(gateway_root))
    try:
        from moss_realtime_gateway.app import create_app as create_gateway_app
        from moss_realtime_gateway.backends import FakeBackend
        from moss_realtime_gateway.config import GatewaySettings
    finally:
        if sys.path[0] == str(gateway_root):
            sys.path.pop(0)

    prompt_dir = tmp_path / "gateway-prompts"
    prompt_dir.mkdir()
    voice_prompts = {}
    for index in range(1, 9):
        filename = f"voice-{index}.wav"
        (prompt_dir / filename).write_bytes(b"RIFFfake")
        voice_prompts[f"debate_voice_{index}"] = filename
    gateway_settings = GatewaySettings(
        backend="fake",
        prompt_dir=prompt_dir,
        voice_prompts=voice_prompts,
        require_eight_prompts=True,
        api_key="integration-key",
        control_ack_timeout_seconds=1,
        terminal_grace_seconds=1,
        startup_timeout_seconds=2,
    )
    application = create_gateway_app(gateway_settings, backend=FakeBackend())

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = int(listener.getsockname()[1])
    server = uvicorn.Server(
        uvicorn.Config(application, host="127.0.0.1", port=port, log_level="error", lifespan="on")
    )
    server.install_signal_handlers = lambda: None
    server_thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    server_thread.start()
    startup_deadline = time.monotonic() + 5
    while not server.started and server_thread.is_alive() and time.monotonic() < startup_deadline:
        time.sleep(0.01)
    assert server.started and server_thread.is_alive()

    configure_ws_moss(monkeypatch, tmp_path, endpoints=[f"http://127.0.0.1:{port}"])
    monkeypatch.setattr(settings, "moss_tts_realtime_api_key", "integration-key")
    # FakeBackend intentionally emits tiny deterministic PCM frames. The
    # production truncation heuristic is orthogonal to this wire-contract test.
    monkeypatch.setattr(LightTTSProvider, "MIN_AUDIO_DURATION_SECONDS", 0.01)
    monkeypatch.setattr(LightTTSProvider, "MIN_SECONDS_PER_VISIBLE_CHARACTER", 0.0001)
    events: list[str] = []

    async def on_stream_event(event: dict[str, object]) -> None:
        events.append(str(event["type"]))

    provider = MossTTSRealtimeProvider()
    try:
        prewarm = await provider.prewarm()
        assert prewarm["ok"] is True and prewarm["idle_ws_ready_endpoints"] == 1
        session = provider.open_incremental_session(
            room_code="moss-real-gateway",
            speech_id="real-wire-contract",
            voice="debate_voice_1",
            on_stream_event=on_stream_event,
        )
        await session.push_text("第一段真实 WebSocket 正文。")
        await session.push_text("第二段继续复用同一条连接。")
        media_url = await session.finish()
    finally:
        await provider.aclose()
        server.should_exit = True
        server_thread.join(timeout=5)
        listener.close()

    assert not server_thread.is_alive()
    assert media_url == "/media/moss-real-gateway/real-wire-contract.wav"
    target = tmp_path / "media" / "moss-real-gateway" / "real-wire-contract.wav"
    sample_rate, samples = read_pcm16_samples(target)
    assert sample_rate == 24_000 and samples
    assert events == ["audio.stream.started"]


async def test_moss_ws_shards_two_concurrent_turns_across_two_endpoints(tmp_path: Path, monkeypatch) -> None:
    configure_ws_moss(
        monkeypatch,
        tmp_path,
        endpoints=["http://moss-a.internal:8083", "http://moss-b.internal:8083"],
    )
    connection_hosts: list[str] = []
    both_connected = asyncio.Event()

    def connect(url: str, **_kwargs):
        host = str(httpx.URL(url).host)
        connection_hosts.append(host)
        if len(connection_hosts) == 2:
            both_connected.set()
        return FakePersistentMossConnection(FakePersistentMossWebSocket(normal_moss_ws_script))

    async def on_stream_event(_event: dict[str, object]) -> None:
        return None

    provider = MossTTSRealtimeProvider(httpx.MockTransport(moss_ready_handler), ws_connect=connect)
    first = provider.open_incremental_session(
        room_code="moss-ws-a",
        speech_id="speech-a",
        voice="debate_voice_1",
        on_stream_event=on_stream_event,
    )
    second = provider.open_incremental_session(
        room_code="moss-ws-b",
        speech_id="speech-b",
        voice="debate_voice_1",
        on_stream_event=on_stream_event,
    )

    await asyncio.gather(first.push_text("第一场内容。"), second.push_text("第二场内容。"))
    await asyncio.wait_for(both_connected.wait(), timeout=1)
    urls = await asyncio.gather(first.finish(), second.finish())

    assert set(connection_hosts) == {"moss-a.internal", "moss-b.internal"}
    assert urls == ["/media/moss-ws-a/speech-a.wav", "/media/moss-ws-b/speech-b.wav"]


async def test_moss_ws_first_pcm_timeout_aborts_same_connection_and_isolates_endpoint(
    tmp_path: Path,
    monkeypatch,
) -> None:
    configure_ws_moss(monkeypatch, tmp_path, endpoints=["http://moss-timeout.internal:8083"])
    monkeypatch.setattr(settings, "moss_tts_realtime_first_pcm_timeout_seconds", 0.05)
    monkeypatch.setattr(settings, "moss_tts_realtime_close_timeout_seconds", 0.2)
    sockets: list[FakePersistentMossWebSocket] = []

    async def script(websocket: FakePersistentMossWebSocket, payload: dict[str, object]) -> None:
        if payload["type"] == "start":
            websocket.session_id = str(payload["session_id"])
            websocket.feed_json(
                {
                    "type": "ready",
                    "session_id": websocket.session_id,
                    "voice": payload["voice"],
                    "next_seq": 1,
                    "audio": {"codec": "pcm_s16le", "sample_rate": 24_000, "channels": 1},
                }
            )
        elif payload["type"] == "text_delta":
            websocket.feed_json({"type": "ack", "seq": payload["seq"]})
        elif payload["type"] == "abort":
            websocket.feed_json({"type": "audio_reset", "session_id": websocket.session_id})
            websocket.feed_json(
                {
                    "type": "released",
                    "session_id": websocket.session_id,
                    "status": "aborted",
                    "released": True,
                }
            )

    def connect(_url: str, **_kwargs):
        websocket = FakePersistentMossWebSocket(script)
        sockets.append(websocket)
        return FakePersistentMossConnection(websocket)

    async def on_stream_event(_event: dict[str, object]) -> None:
        return None

    provider = MossTTSRealtimeProvider(httpx.MockTransport(moss_ready_handler), ws_connect=connect)
    session = provider.open_incremental_session(
        room_code="moss-ws-timeout",
        speech_id="speech-timeout",
        voice="debate_voice_1",
        on_stream_event=on_stream_event,
    )
    await session.push_text("首个音频必须及时返回。")
    with pytest.raises(ProviderError) as raised:
        await session.finish()

    assert raised.value.code == "moss_tts_first_pcm_timeout"
    assert [payload["type"] for payload in sockets[0].sent] == ["start", "text_delta", "abort"]
    assert sockets[0].max_recv_active == 1
    assert provider._endpoint_semaphores[0][1].locked() is True


async def test_moss_ws_rejects_out_of_order_ack_and_isolates_endpoint(tmp_path: Path, monkeypatch) -> None:
    configure_ws_moss(monkeypatch, tmp_path, endpoints=["http://moss-protocol.internal:8083"])
    sockets: list[FakePersistentMossWebSocket] = []

    async def script(websocket: FakePersistentMossWebSocket, payload: dict[str, object]) -> None:
        if payload["type"] == "start":
            websocket.session_id = str(payload["session_id"])
            websocket.feed_json(
                {
                    "type": "ready",
                    "session_id": websocket.session_id,
                    "voice": payload["voice"],
                    "next_seq": 1,
                    "audio": {"codec": "pcm_s16le", "sample_rate": 24_000, "channels": 1},
                }
            )
        elif payload["type"] == "text_delta":
            websocket.feed_json({"type": "ack", "seq": int(payload["seq"]) + 1})
        elif payload["type"] == "abort":
            websocket.feed_json({"type": "audio_reset", "session_id": websocket.session_id})
            websocket.feed_json(
                {
                    "type": "released",
                    "session_id": websocket.session_id,
                    "status": "aborted",
                    "released": True,
                }
            )

    def connect(_url: str, **_kwargs):
        websocket = FakePersistentMossWebSocket(script)
        sockets.append(websocket)
        return FakePersistentMossConnection(websocket)

    async def on_stream_event(_event: dict[str, object]) -> None:
        return None

    provider = MossTTSRealtimeProvider(httpx.MockTransport(moss_ready_handler), ws_connect=connect)
    session = provider.open_incremental_session(
        room_code="moss-ws-protocol",
        speech_id="speech-protocol",
        voice="debate_voice_1",
        on_stream_event=on_stream_event,
    )
    await session.push_text("乱序确认必须失败。")
    with pytest.raises(ProviderError) as raised:
        await session.finish()

    assert raised.value.code == "moss_tts_ws_protocol_error"
    assert [payload["type"] for payload in sockets[0].sent] == ["start", "text_delta", "abort"]
    assert sockets[0].max_recv_active == 1
    assert provider._endpoint_semaphores[0][1].locked() is True


async def test_moss_ws_abort_waits_for_released_on_same_connection_and_returns_endpoint(
    tmp_path: Path,
    monkeypatch,
) -> None:
    configure_ws_moss(monkeypatch, tmp_path, endpoints=["http://moss-cancel.internal:8083"])
    sockets: list[FakePersistentMossWebSocket] = []
    first_pcm = asyncio.Event()
    events: list[str] = []

    def connect(_url: str, **_kwargs):
        websocket = FakePersistentMossWebSocket(normal_moss_ws_script)
        sockets.append(websocket)
        return FakePersistentMossConnection(websocket)

    async def on_stream_event(event: dict[str, object]) -> None:
        events.append(str(event["type"]))
        if event["type"] == "audio.stream.started":
            first_pcm.set()

    provider = MossTTSRealtimeProvider(httpx.MockTransport(moss_ready_handler), ws_connect=connect)
    session = provider.open_incremental_session(
        room_code="moss-ws-cancel",
        speech_id="speech-cancel",
        voice="debate_voice_1",
        on_stream_event=on_stream_event,
    )
    await session.push_text("这段正文已经进入合成。")
    await asyncio.wait_for(first_pcm.wait(), timeout=1)
    await asyncio.wait_for(session.abort(), timeout=1)

    assert [payload["type"] for payload in sockets[0].sent] == ["start", "text_delta", "abort"]
    assert sockets[0].max_recv_active == 1
    assert events == ["audio.stream.started", "audio.stream.aborted"]
    assert provider._endpoint_semaphores[0][1].locked() is False
    assert not list((tmp_path / "media" / "moss-ws-cancel").glob("*.part"))


async def test_moss_ws_cancel_recovers_endpoint_when_release_frame_is_lost_but_health_is_idle(
    tmp_path: Path,
    monkeypatch,
) -> None:
    configure_ws_moss(monkeypatch, tmp_path, endpoints=["http://moss-cancel-recover.internal:8083"])
    monkeypatch.setattr(settings, "moss_tts_realtime_close_timeout_seconds", 0.05)
    first_pcm = asyncio.Event()

    async def script(websocket: FakePersistentMossWebSocket, payload: dict[str, object]) -> None:
        if payload["type"] == "start":
            websocket.session_id = str(payload["session_id"])
            websocket.feed_json(
                {
                    "type": "ready",
                    "session_id": websocket.session_id,
                    "voice": payload["voice"],
                    "next_seq": 1,
                    "audio": {"sample_rate": 24_000, "channels": 1, "codec": "pcm_s16le"},
                }
            )
        elif payload["type"] == "text_delta":
            websocket.feed_pcm()
            websocket.feed_json({"type": "ack", "seq": payload["seq"]})
        elif payload["type"] == "abort":
            # Simulate a lost release frame. The HTTP readiness endpoint still
            # proves that GPU work has stopped and no orphan remains.
            return

    def connect(_url: str, **_kwargs):
        return FakePersistentMossConnection(FakePersistentMossWebSocket(script))

    async def on_stream_event(event: dict[str, object]) -> None:
        if event["type"] == "audio.stream.started":
            first_pcm.set()

    provider = MossTTSRealtimeProvider(httpx.MockTransport(moss_ready_handler), ws_connect=connect)
    session = provider.open_incremental_session(
        room_code="moss-ws-cancel-recover",
        speech_id="speech-cancel-recover",
        voice="debate_voice_1",
        on_stream_event=on_stream_event,
    )
    await session.push_text("终止比赛后端点必须自动归还。")
    await asyncio.wait_for(first_pcm.wait(), timeout=1)
    await asyncio.wait_for(session.abort(), timeout=1)

    assert provider._endpoint_semaphores[0][1].locked() is False


async def test_moss_realtime_incremental_session_streams_one_continuous_turn(
    tmp_path: Path,
    monkeypatch,
) -> None:
    use_http_moss(monkeypatch)
    media = tmp_path / "media"
    final_seen = asyncio.Event()
    audio_connected = asyncio.Event()
    first_pcm = asyncio.Event()
    requests: list[tuple[str, dict[str, object]]] = []
    events: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        payload = json.loads(request.content) if request.content else {}
        requests.append((path, payload))
        if path.endswith("/health/ready"):
            return moss_ready_response()
        if path.endswith("/tts/session/start"):
            return httpx.Response(200, json={"ok": True})
        if path.endswith("/audio"):
            audio_connected.set()
            return httpx.Response(
                200,
                headers={
                    "X-Audio-Codec": "pcm_s16le",
                    "X-Audio-Sample-Rate": "24000",
                    "X-Audio-Channels": "1",
                },
                stream=FakeMossAudioStream(final_seen),
            )
        if path.endswith("/tts/session/push"):
            if payload.get("is_final"):
                final_seen.set()
            return httpx.Response(200, json={"ok": True})
        if path.endswith("/tts/session/close"):
            return httpx.Response(200, json={"ok": True, "released": True})
        return httpx.Response(404)

    async def on_stream_event(event: dict[str, object]) -> None:
        events.append(str(event["type"]))
        if event["type"] == "audio.stream.started":
            first_pcm.set()
            part = media / "moss-room" / f".moss-speech.{event['generation']}.wav.part"
            assert part.is_file() and part.stat().st_size > 44

    monkeypatch.setattr(settings, "media_root", str(media))
    monkeypatch.setattr(settings, "moss_tts_realtime_enabled", True)
    monkeypatch.setattr(settings, "moss_tts_realtime_url", "http://moss.internal:8083")
    monkeypatch.setattr(settings, "moss_tts_realtime_urls", [])
    monkeypatch.setattr(settings, "moss_tts_prompt_files", {"debate_voice_1": "voice-1.wav"})
    provider = MossTTSRealtimeProvider(httpx.MockTransport(handler))
    session = provider.open_incremental_session(
        room_code="moss-room",
        speech_id="moss-speech",
        voice="debate_voice_1",
        on_stream_event=on_stream_event,
    )

    await session.push_text("第一段。")
    await asyncio.wait_for(audio_connected.wait(), timeout=1)
    await asyncio.wait_for(first_pcm.wait(), timeout=1)
    assert not final_seen.is_set(), "first PCM must arrive before the Agent/TTS turn is finalized"

    await session.push_text("第二段。")
    url = await session.finish()

    assert url == "/media/moss-room/moss-speech.wav"
    assert events == ["audio.stream.started"]
    target = media / "moss-room" / "moss-speech.wav"
    sample_rate, samples = read_pcm16_samples(target)
    assert sample_rate == 24_000
    assert samples == ([1] * 24_000) + ([2] * 24_000)
    start_payload = next(payload for path, payload in requests if path.endswith("/tts/session/start"))
    assert start_payload["assistant_text"] == ""
    assert start_payload["prompt_audio"] == "voice-1.wav"
    pushed = [payload for path, payload in requests if path.endswith("/tts/session/push")]
    assert pushed == [
        {"session_id": start_payload["session_id"], "text": "第一段。", "is_final": False},
        {"session_id": start_payload["session_id"], "text": "第二段。", "is_final": False},
        {"session_id": start_payload["session_id"], "text": "", "is_final": True},
    ]
    assert sum(path.endswith("/tts/session/close") for path, _payload in requests) == 1
    assert not list((media / "moss-room").glob("*.part"))


async def test_moss_http_livekit_flushes_short_pcm_on_finish(tmp_path: Path, monkeypatch) -> None:
    use_http_moss(monkeypatch)
    media = tmp_path / "media"
    final_seen = asyncio.Event()
    writes: list[int] = []
    events: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        payload = json.loads(request.content) if request.content else {}
        if path.endswith("/health/ready"):
            return moss_ready_response()
        if path.endswith("/tts/session/start"):
            return httpx.Response(200, json={"ok": True})
        if path.endswith("/audio"):
            return httpx.Response(
                200,
                headers={
                    "X-Audio-Codec": "pcm_s16le",
                    "X-Audio-Sample-Rate": "24000",
                    "X-Audio-Channels": "1",
                },
                stream=ShortMossAudioStream(final_seen),
            )
        if path.endswith("/tts/session/push"):
            if payload.get("is_final"):
                final_seen.set()
            return httpx.Response(200, json={"ok": True})
        if path.endswith("/tts/session/close"):
            return httpx.Response(200, json={"ok": True, "released": True})
        return httpx.Response(404)

    async def ensure_room(_room_code: str) -> None:
        return None

    async def start_generation(
        _room_code: str,
        _speech_id: str,
        _generation: str,
        _sample_rate: int,
        *,
        start_buffer_ms: int | None = None,
    ) -> str:
        assert start_buffer_ms == 1_200
        return "TR_short"

    async def write_pcm(_room_code: str, _generation: str, audio: bytes) -> str | None:
        writes.append(len(audio))
        return None

    async def finish_generation(_room_code: str, _generation: str) -> str:
        return "2026-07-18T00:00:00+00:00"

    async def on_stream_event(event: dict[str, object]) -> None:
        events.append(event)

    monkeypatch.setattr(settings, "media_root", str(media))
    monkeypatch.setattr(settings, "moss_tts_realtime_enabled", True)
    monkeypatch.setattr(settings, "moss_tts_realtime_url", "http://moss.internal:8083")
    monkeypatch.setattr(settings, "moss_tts_realtime_urls", [])
    monkeypatch.setattr(settings, "moss_tts_prompt_files", {"debate_voice_1": "voice-1.wav"})
    monkeypatch.setattr(settings, "webrtc_audio_enabled", True)
    monkeypatch.setattr(settings, "webrtc_audio_backend", "livekit")
    monkeypatch.setattr(LightTTSProvider, "MIN_AUDIO_DURATION_SECONDS", 0.01)
    monkeypatch.setattr(LightTTSProvider, "MIN_SECONDS_PER_VISIBLE_CHARACTER", 0.0001)
    monkeypatch.setattr(providers_service.livekit_audio_registry, "ensure_room", ensure_room)
    monkeypatch.setattr(providers_service.livekit_audio_registry, "start_generation", start_generation)
    monkeypatch.setattr(providers_service.livekit_audio_registry, "write_pcm", write_pcm)
    monkeypatch.setattr(providers_service.livekit_audio_registry, "finish_generation", finish_generation)

    provider = MossTTSRealtimeProvider(httpx.MockTransport(handler))
    session = provider.open_incremental_session(
        room_code="moss-short-room",
        speech_id="moss-short-speech",
        voice="debate_voice_1",
        on_stream_event=on_stream_event,
    )
    await session.push_text("短句。")
    await session.finish()

    assert writes == [7_200 * 2]
    assert [event["type"] for event in events] == ["audio.stream.started"]
    assert events[0]["server_first_capture_at"] == "2026-07-18T00:00:00+00:00"


async def test_moss_realtime_abort_closes_upstream_and_removes_partial_audio(
    tmp_path: Path,
    monkeypatch,
) -> None:
    use_http_moss(monkeypatch)
    media = tmp_path / "media"
    never_final = asyncio.Event()
    first_pcm = asyncio.Event()
    paths: list[str] = []
    events: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        paths.append(path)
        if path.endswith("/health/ready"):
            return moss_ready_response()
        if path.endswith("/tts/session/start"):
            return httpx.Response(200, json={"ok": True})
        if path.endswith("/tts/session/abort") or path.endswith("/tts/session/close"):
            return httpx.Response(200, json={"ok": True, "released": True})
        if path.endswith("/audio"):
            return httpx.Response(
                200,
                headers={
                    "X-Audio-Codec": "pcm_s16le",
                    "X-Audio-Sample-Rate": "24000",
                    "X-Audio-Channels": "1",
                },
                stream=FakeMossAudioStream(never_final),
            )
        return httpx.Response(200, json={"ok": True})

    async def on_stream_event(event: dict[str, object]) -> None:
        events.append(str(event["type"]))
        if event["type"] == "audio.stream.started":
            first_pcm.set()

    monkeypatch.setattr(settings, "media_root", str(media))
    monkeypatch.setattr(settings, "moss_tts_realtime_enabled", True)
    monkeypatch.setattr(settings, "moss_tts_realtime_url", "http://moss.internal:8083")
    monkeypatch.setattr(settings, "moss_tts_realtime_urls", [])
    monkeypatch.setattr(settings, "moss_tts_prompt_files", {"debate_voice_1": "voice-1.wav"})
    provider = MossTTSRealtimeProvider(httpx.MockTransport(handler))
    session = provider.open_incremental_session(
        room_code="moss-cancel-room",
        speech_id="moss-cancel-speech",
        on_stream_event=on_stream_event,
    )

    await session.push_text("这段音频会被取消。")
    await asyncio.wait_for(first_pcm.wait(), timeout=1)
    await asyncio.wait_for(session.abort(), timeout=2)

    assert events == ["audio.stream.started", "audio.stream.aborted"]
    assert sum(path.endswith("/tts/session/abort") for path in paths) == 1
    target_dir = media / "moss-cancel-room"
    assert not (target_dir / "moss-cancel-speech.wav").exists()
    assert not list(target_dir.glob("*.part"))


async def test_moss_realtime_pool_shards_concurrent_turns_across_independent_endpoints(
    tmp_path: Path,
    monkeypatch,
) -> None:
    use_http_moss(monkeypatch)
    media = tmp_path / "media"
    final_events: dict[str, asyncio.Event] = {}
    start_hosts: dict[str, str] = {}
    two_started = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content) if request.content else {}
        path = request.url.path
        if path.endswith("/health/ready"):
            return moss_ready_response()
        if path.endswith("/tts/session/start"):
            session_id = str(payload["session_id"])
            final_events[session_id] = asyncio.Event()
            start_hosts[session_id] = request.url.host
            if len(start_hosts) == 2:
                two_started.set()
            return httpx.Response(200, json={"ok": True})
        if path.endswith("/audio"):
            session_id = path.split("/")[-2]
            return httpx.Response(
                200,
                headers={
                    "X-Audio-Codec": "pcm_s16le",
                    "X-Audio-Sample-Rate": "24000",
                    "X-Audio-Channels": "1",
                },
                stream=FakeMossAudioStream(final_events[session_id]),
            )
        if path.endswith("/tts/session/push"):
            if payload.get("is_final"):
                final_events[str(payload["session_id"])].set()
            return httpx.Response(200, json={"ok": True})
        if path.endswith("/tts/session/close"):
            return httpx.Response(200, json={"ok": True, "released": True})
        return httpx.Response(404)

    async def on_stream_event(_event: dict[str, object]) -> None:
        return None

    monkeypatch.setattr(settings, "media_root", str(media))
    monkeypatch.setattr(settings, "moss_tts_realtime_enabled", True)
    monkeypatch.setattr(settings, "moss_tts_realtime_url", "")
    monkeypatch.setattr(
        settings,
        "moss_tts_realtime_urls",
        ["http://moss-a.internal:8083", "http://moss-b.internal:8083"],
    )
    monkeypatch.setattr(settings, "moss_tts_realtime_max_active", 3)
    monkeypatch.setattr(settings, "moss_tts_realtime_max_active_per_endpoint", 1)
    monkeypatch.setattr(settings, "moss_tts_prompt_files", {"debate_voice_1": "voice-1.wav"})
    provider = MossTTSRealtimeProvider(httpx.MockTransport(handler))
    first = provider.open_incremental_session(
        room_code="pool-room-a",
        speech_id="pool-speech-a",
        on_stream_event=on_stream_event,
    )
    second = provider.open_incremental_session(
        room_code="pool-room-b",
        speech_id="pool-speech-b",
        on_stream_event=on_stream_event,
    )

    await asyncio.gather(first.push_text("并发第一场。"), second.push_text("并发第二场。"))
    await asyncio.wait_for(two_started.wait(), timeout=1)
    assert set(start_hosts.values()) == {"moss-a.internal", "moss-b.internal"}
    urls = await asyncio.gather(first.finish(), second.finish())
    assert urls == [
        "/media/pool-room-a/pool-speech-a.wav",
        "/media/pool-room-b/pool-speech-b.wav",
    ]


async def test_moss_realtime_requires_close_ack_before_publishing_final_wav(
    tmp_path: Path,
    monkeypatch,
) -> None:
    use_http_moss(monkeypatch)
    media = tmp_path / "media"
    final_seen = asyncio.Event()
    events: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        payload = json.loads(request.content) if request.content else {}
        if path.endswith("/health/ready"):
            return moss_ready_response()
        if path.endswith("/tts/session/start"):
            return httpx.Response(200, json={"ok": True})
        if path.endswith("/audio"):
            return httpx.Response(
                200,
                headers={
                    "X-Audio-Codec": "pcm_s16le",
                    "X-Audio-Sample-Rate": "24000",
                    "X-Audio-Channels": "1",
                },
                stream=FakeMossAudioStream(final_seen),
            )
        if path.endswith("/tts/session/push"):
            if payload.get("is_final"):
                final_seen.set()
            return httpx.Response(200, json={"ok": True})
        if path.endswith("/tts/session/close"):
            return httpx.Response(503, json={"ok": False})
        return httpx.Response(404)

    async def on_stream_event(event: dict[str, object]) -> None:
        events.append(str(event["type"]))

    monkeypatch.setattr(settings, "media_root", str(media))
    monkeypatch.setattr(settings, "moss_tts_realtime_enabled", True)
    monkeypatch.setattr(settings, "moss_tts_realtime_url", "http://moss.internal:8083")
    monkeypatch.setattr(settings, "moss_tts_realtime_urls", [])
    monkeypatch.setattr(settings, "moss_tts_prompt_files", {"debate_voice_1": "voice-1.wav"})
    provider = MossTTSRealtimeProvider(httpx.MockTransport(handler))
    session = provider.open_incremental_session(
        room_code="moss-close-room",
        speech_id="moss-close-speech",
        on_stream_event=on_stream_event,
    )

    await session.push_text("释放确认失败时不得发布最终音频。")
    with pytest.raises(ProviderError, match="未确认会话释放"):
        await session.finish()

    assert events == ["audio.stream.started", "audio.stream.aborted"]
    target_dir = media / "moss-close-room"
    assert not (target_dir / "moss-close-speech.wav").exists()
    assert not list(target_dir.glob("*.part"))


async def test_moss_realtime_first_pcm_timeout_fails_fast_and_releases_endpoint(
    tmp_path: Path,
    monkeypatch,
) -> None:
    use_http_moss(monkeypatch)
    paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        paths.append(path)
        if path.endswith("/health/ready"):
            return moss_ready_response()
        if path.endswith("/tts/session/start"):
            return httpx.Response(200, json={"ok": True})
        if path.endswith("/audio"):
            return httpx.Response(
                200,
                headers={
                    "X-Audio-Codec": "pcm_s16le",
                    "X-Audio-Sample-Rate": "24000",
                    "X-Audio-Channels": "1",
                },
                stream=StalledMossAudioStream(),
            )
        if path.endswith("/tts/session/push"):
            return httpx.Response(200, json={"ok": True})
        if path.endswith("/tts/session/close"):
            return httpx.Response(200, json={"ok": True, "released": True})
        return httpx.Response(404)

    async def on_stream_event(_event: dict[str, object]) -> None:
        return None

    monkeypatch.setattr(settings, "media_root", str(tmp_path / "media"))
    monkeypatch.setattr(settings, "moss_tts_realtime_enabled", True)
    monkeypatch.setattr(settings, "moss_tts_realtime_url", "http://moss.internal:8083")
    monkeypatch.setattr(settings, "moss_tts_realtime_urls", [])
    monkeypatch.setattr(settings, "moss_tts_prompt_files", {"debate_voice_1": "voice-1.wav"})
    monkeypatch.setattr(settings, "moss_tts_realtime_first_pcm_timeout_seconds", 0.05)
    provider = MossTTSRealtimeProvider(httpx.MockTransport(handler))
    session = provider.open_incremental_session(
        room_code="moss-timeout-room",
        speech_id="moss-timeout-speech",
        on_stream_event=on_stream_event,
    )

    await session.push_text("这段稳定文字必须快速产生首个音频。")
    with pytest.raises(ProviderError) as raised:
        await session.finish()
    assert raised.value.code == "moss_tts_first_pcm_timeout"
    assert sum(path.endswith("/tts/session/close") for path in paths) == 1


async def test_moss_realtime_skips_unready_endpoint_and_keeps_authenticated_pool(
    tmp_path: Path,
    monkeypatch,
) -> None:
    use_http_moss(monkeypatch)
    start_hosts: list[str] = []
    final_seen = asyncio.Event()
    header_values: list[str | None] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        header_values.append(request.headers.get("X-MOSS-Gateway-Key"))
        path = request.url.path
        if path.endswith("/health/ready"):
            if request.url.host == "moss-a.internal":
                return httpx.Response(503, json={"ok": False, "model_warmed": False, "orphan_count": 1})
            return moss_ready_response()
        if path.endswith("/tts/session/start"):
            start_hosts.append(str(request.url.host))
            return httpx.Response(200, json={"ok": True})
        if path.endswith("/audio"):
            return httpx.Response(
                200,
                headers={
                    "X-Audio-Codec": "pcm_s16le",
                    "X-Audio-Sample-Rate": "24000",
                    "X-Audio-Channels": "1",
                },
                stream=FakeMossAudioStream(final_seen),
            )
        if path.endswith("/tts/session/push"):
            final_seen.set()
            return httpx.Response(200, json={"ok": True})
        if path.endswith("/tts/session/close"):
            return httpx.Response(200, json={"ok": True, "released": True})
        return httpx.Response(404)

    async def on_stream_event(_event: dict[str, object]) -> None:
        return None

    monkeypatch.setattr(settings, "media_root", str(tmp_path / "media"))
    monkeypatch.setattr(settings, "moss_tts_realtime_enabled", True)
    monkeypatch.setattr(settings, "moss_tts_realtime_url", "")
    monkeypatch.setattr(
        settings,
        "moss_tts_realtime_urls",
        ["http://moss-a.internal:8083", "http://moss-b.internal:8083"],
    )
    monkeypatch.setattr(settings, "moss_tts_realtime_max_active", 2)
    monkeypatch.setattr(settings, "moss_tts_realtime_max_active_per_endpoint", 1)
    monkeypatch.setattr(settings, "moss_tts_realtime_api_key", "internal-test-key")
    monkeypatch.setattr(settings, "moss_tts_prompt_files", {"debate_voice_1": "voice-1.wav"})
    provider = MossTTSRealtimeProvider(httpx.MockTransport(handler))
    session = provider.open_incremental_session(
        room_code="moss-ready-room",
        speech_id="moss-ready-speech",
        on_stream_event=on_stream_event,
    )

    await session.push_text("只允许已暖机且没有孤儿任务的端点。")
    await session.finish()
    assert start_hosts == ["moss-b.internal"]
    assert header_values and set(header_values) == {"internal-test-key"}
    assert provider._client is not None and not provider._client.is_closed
    await provider.aclose()


def test_moss_realtime_session_is_default_off(monkeypatch) -> None:
    async def on_stream_event(_event) -> None:
        return None

    monkeypatch.setattr(settings, "moss_tts_realtime_enabled", False)
    monkeypatch.setattr(settings, "moss_tts_realtime_url", "http://moss.internal:8083")
    monkeypatch.setattr(settings, "moss_tts_realtime_urls", [])
    with pytest.raises(ProviderError, match="尚未启用"):
        MossTTSRealtimeProvider().open_incremental_session(
            room_code="disabled-room",
            speech_id="disabled-speech",
            on_stream_event=on_stream_event,
        )


def test_realtime_tts_router_selects_only_the_explicit_enabled_backend(monkeypatch) -> None:
    class FakeProvider:
        def __init__(self, result: str) -> None:
            self.result = result
            self.calls = 0

        def open_incremental_session(self, **_kwargs):
            self.calls += 1
            return self.result

    light = FakeProvider("light-session")
    moss = FakeProvider("moss-session")
    router = RealtimeTTSProviderRouter(light, moss)  # type: ignore[arg-type]

    monkeypatch.setattr(settings, "realtime_voice_backend", "moss_realtime")
    monkeypatch.setattr(settings, "moss_tts_realtime_enabled", True)
    monkeypatch.setattr(settings, "moss_tts_realtime_url", "http://moss.internal:8083")
    monkeypatch.setattr(settings, "moss_tts_realtime_urls", [])
    monkeypatch.setattr(settings, "lighttts_streaming_enabled", False)
    monkeypatch.setattr(settings, "lighttts_bistream_enabled", False)
    assert router.enabled() is True
    assert router.provider_name() == "moss_tts_realtime"
    assert router.open_incremental_session() == "moss-session"
    assert moss.calls == 1 and light.calls == 0

    monkeypatch.setattr(settings, "moss_tts_realtime_enabled", False)
    assert router.enabled() is False


async def test_lighttts_bistream_cancellation_aborts_generation_and_never_publishes_final(
    tmp_path: Path,
    monkeypatch,
) -> None:
    prompt = tmp_path / "debate_voice_1.wav"
    prompt.write_bytes(b"prompt-wav")
    media = tmp_path / "media"
    websocket = FakeBiStreamWebSocket([b"\x01\x00" * 12_000, None])
    cancelled = False
    events: list[str] = []

    def connect(_url: str, **_kwargs) -> FakeBiStreamConnection:
        return FakeBiStreamConnection(websocket)

    async def on_stream_event(event: dict[str, object]) -> None:
        nonlocal cancelled
        events.append(str(event["type"]))
        if event["type"] == "audio.stream.started":
            cancelled = True

    monkeypatch.setattr(settings, "lighttts_prompt_wav_path", str(prompt))
    monkeypatch.setattr(settings, "lighttts_prompt_text", "提示文本")
    monkeypatch.setattr(settings, "media_root", str(media))
    monkeypatch.setattr(settings, "lighttts_streaming_enabled", True)
    monkeypatch.setattr(settings, "lighttts_bistream_enabled", True)
    provider = LightTTSProvider(stream_connect=connect)
    with pytest.raises(ProviderCancelled):
        await provider.synthesize(
            "取消流式语音。",
            room_code="cancel-room",
            speech_id="cancel-speech",
            should_cancel=lambda: cancelled,
            on_stream_event=on_stream_event,
        )
    assert events == ["audio.stream.started", "audio.stream.aborted"]
    target_dir = media / "cancel-room"
    assert not (target_dir / "cancel-speech.wav").exists()
    assert not list(target_dir.glob("*.part"))


@pytest.mark.parametrize("seconds", [0.8, 1.0])
async def test_lighttts_rejects_grossly_truncated_http_200_audio(
    tmp_path: Path,
    monkeypatch,
    seconds: float,
) -> None:
    prompt = tmp_path / "debate_voice_1.wav"
    prompt.write_bytes(b"prompt")
    monkeypatch.setattr(settings, "lighttts_prompt_wav_path", str(prompt))
    monkeypatch.setattr(settings, "media_root", str(tmp_path / "media"))
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=wav_bytes(seconds))

    provider = LightTTSProvider(httpx.MockTransport(handler))
    with pytest.raises(ProviderError, match="音频无效或过短"):
        await provider.synthesize("甲" * 30, room_code="truncated-room", speech_id=f"truncated-{seconds}")
    assert calls == 1
    target_dir = tmp_path / "media" / "truncated-room"
    assert not (target_dir / f"truncated-{seconds}.wav").exists()
    assert list(target_dir.glob("*.part")) == []


async def test_lighttts_accepts_a_conservative_minimum_duration_for_thirty_characters(
    tmp_path: Path,
    monkeypatch,
) -> None:
    prompt = tmp_path / "debate_voice_1.wav"
    prompt.write_bytes(b"prompt")
    monkeypatch.setattr(settings, "lighttts_prompt_wav_path", str(prompt))
    monkeypatch.setattr(settings, "media_root", str(tmp_path / "media"))

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=wav_bytes(2.4))

    provider = LightTTSProvider(httpx.MockTransport(handler))
    await provider.synthesize("甲" * 30, room_code="minimum-room", speech_id="minimum-speech")
    target = tmp_path / "media" / "minimum-room" / "minimum-speech.wav"
    assert provider._minimum_duration("甲" * 30) == pytest.approx(2.4)
    assert provider._wav_duration(target) == pytest.approx(2.4, abs=0.01)


async def test_lighttts_replaces_a_grossly_truncated_cached_output_before_reusing_it(
    tmp_path: Path,
    monkeypatch,
) -> None:
    prompt = tmp_path / "debate_voice_1.wav"
    prompt.write_bytes(b"prompt")
    media = tmp_path / "media"
    target = media / "cache-validation-room" / "cache-validation-speech.wav"
    target.parent.mkdir(parents=True)
    target.write_bytes(wav_bytes(1.0))
    monkeypatch.setattr(settings, "lighttts_prompt_wav_path", str(prompt))
    monkeypatch.setattr(settings, "media_root", str(media))
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=wav_bytes(2.4))

    provider = LightTTSProvider(httpx.MockTransport(handler))
    expected_url = "/media/cache-validation-room/cache-validation-speech.wav"
    assert await provider.synthesize("甲" * 30, room_code="cache-validation-room", speech_id="cache-validation-speech") == expected_url
    assert calls == 1
    assert provider._wav_duration(target) == pytest.approx(2.4, abs=0.01)
    assert await provider.synthesize("甲" * 30, room_code="cache-validation-room", speech_id="cache-validation-speech") == expected_url
    assert calls == 1


def test_lighttts_single_segment_merge_is_bit_identical(tmp_path: Path) -> None:
    source = tmp_path / "single.wav"
    target = tmp_path / "merged.wav"
    source.write_bytes(pcm16_wav_bytes([0, 1, -1, 10_000, -10_000, 0] * 100, sample_rate=1_000))

    LightTTSProvider._merge_wav_segments([source], target)

    assert target.read_bytes() == source.read_bytes()


def test_lighttts_merge_collapses_only_strong_internal_edge_silence(tmp_path: Path) -> None:
    first = tmp_path / "first.wav"
    second = tmp_path / "second.wav"
    target = tmp_path / "merged.wav"
    first.write_bytes(pcm16_wav_bytes(([0] * 50) + ([10_000] * 100) + ([0] * 500), sample_rate=1_000))
    second.write_bytes(pcm16_wav_bytes(([0] * 700) + ([-10_000] * 100) + ([0] * 70), sample_rate=1_000))

    LightTTSProvider._merge_wav_segments([first, second], target)

    sample_rate, samples = read_pcm16_samples(target)
    assert sample_rate == 1_000
    assert samples[:50] == [0] * 50
    assert samples[50:150] == [10_000] * 100
    assert samples[150:270] == [0] * 120
    assert samples[270:370] == [-10_000] * 100
    assert samples[370:] == [0] * 70
    assert len(samples) == 440


def test_lighttts_merge_fades_a_hard_voiced_boundary_without_deleting_frames(tmp_path: Path) -> None:
    first = tmp_path / "first.wav"
    second = tmp_path / "second.wav"
    target = tmp_path / "merged.wav"
    first.write_bytes(pcm16_wav_bytes([20_000] * 100, sample_rate=1_000))
    second.write_bytes(pcm16_wav_bytes([-20_000] * 100, sample_rate=1_000))

    LightTTSProvider._merge_wav_segments([first, second], target)

    _sample_rate, samples = read_pcm16_samples(target)
    assert len(samples) == 320
    assert samples[:95] == [20_000] * 95
    assert samples[99] == 0
    assert samples[100:220] == [0] * 120
    assert samples[220] == 0
    assert samples[225:] == [-20_000] * 95
    assert max(abs(value) for value in samples) == 20_000


def test_lighttts_merge_does_not_trim_quiet_but_audible_edge_frames(tmp_path: Path) -> None:
    first = tmp_path / "first.wav"
    second = tmp_path / "second.wav"
    target = tmp_path / "merged.wav"
    quiet_edge = [64, -64] * 50
    first.write_bytes(pcm16_wav_bytes(([10_000] * 100) + quiet_edge, sample_rate=1_000))
    second.write_bytes(pcm16_wav_bytes([-10_000] * 100, sample_rate=1_000))

    LightTTSProvider._merge_wav_segments([first, second], target)

    _sample_rate, samples = read_pcm16_samples(target)
    assert len(samples) == 420
    assert samples[100:195] == quiet_edge[:95]
    assert samples[200:320] == [0] * 120
    assert samples[325:] == [-10_000] * 95


def test_lighttts_merge_processes_multiple_internal_boundaries_independently(tmp_path: Path) -> None:
    segments: list[Path] = []
    for index, value in enumerate((8_000, -9_000, 10_000)):
        segment = tmp_path / f"segment-{index}.wav"
        segment.write_bytes(pcm16_wav_bytes([value] * 50, sample_rate=1_000))
        segments.append(segment)
    target = tmp_path / "merged.wav"

    LightTTSProvider._merge_wav_segments(segments, target)

    _sample_rate, samples = read_pcm16_samples(target)
    assert len(samples) == 390
    assert samples[50:170] == [0] * 120
    assert samples[220:340] == [0] * 120
    assert samples[:45] == [8_000] * 45
    assert samples[175:215] == [-9_000] * 40
    assert samples[345:] == [10_000] * 45


async def test_lighttts_splits_and_merges_long_speech(tmp_path: Path, monkeypatch) -> None:
    prompt = tmp_path / "debate_voice_1.wav"
    prompt.write_bytes(b"prompt")
    monkeypatch.setattr(settings, "lighttts_prompt_wav_path", str(prompt))
    monkeypatch.setattr(settings, "lighttts_prompt_text", "提示文本")
    monkeypatch.setattr(settings, "media_root", str(tmp_path / "media"))
    requested_chunks: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        requested_chunks.append(multipart_field(body, "tts_text"))
        return httpx.Response(200, content=wav_bytes(2.5))

    text = f"{'甲' * 55}。{'乙' * 55}。{'丙' * 30}。"
    provider = LightTTSProvider(httpx.MockTransport(handler))
    await provider.synthesize(text, room_code="long-room", speech_id="long-speech")
    target = tmp_path / "media" / "long-room" / "long-speech.wav"
    assert "".join(requested_chunks) == text
    assert len(requested_chunks) >= 5
    assert all(any(character.isalnum() or "\u4e00" <= character <= "\u9fff" for character in item) for item in requested_chunks)
    assert max(len(re.sub(r"[^\w\u4e00-\u9fff]", "", item)) for item in requested_chunks) <= provider.MAX_CHUNK_CHARACTERS
    expected_duration = 2.5 * len(requested_chunks) + provider.SEGMENT_PAUSE_SECONDS * (len(requested_chunks) - 1)
    assert provider._wav_duration(target) == pytest.approx(expected_duration, abs=0.02)
    assert list(target.parent.glob("*.part")) == []


async def test_lighttts_adaptively_splits_http_417_chunks(tmp_path: Path, monkeypatch) -> None:
    prompt = tmp_path / "debate_voice_1.wav"
    prompt.write_bytes(b"prompt")
    monkeypatch.setattr(settings, "lighttts_prompt_wav_path", str(prompt))
    monkeypatch.setattr(settings, "lighttts_prompt_text", "提示文本")
    monkeypatch.setattr(settings, "media_root", str(tmp_path / "media"))
    attempts: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        chunk = multipart_field(body, "tts_text")
        attempts.append(chunk)
        if len(chunk) > 18:
            return httpx.Response(417, json={"message": "input too long"})
        return httpx.Response(200, content=wav_bytes(1.5))

    provider = LightTTSProvider(httpx.MockTransport(handler))
    await provider.synthesize("甲" * 45, room_code="adaptive-room", speech_id="adaptive-speech")
    successful = [item for item in attempts if len(item) <= 18]
    assert "".join(successful) == "甲" * 45
    assert len(successful) >= 3
    assert any(len(item) > 18 for item in attempts)
    target = tmp_path / "media" / "adaptive-room" / "adaptive-speech.wav"
    expected_duration = 1.5 * len(successful) + provider.SEGMENT_PAUSE_SECONDS * (len(successful) - 1)
    assert provider._wav_duration(target) == pytest.approx(expected_duration, abs=0.02)


async def test_lighttts_rechecks_minimum_duration_after_internal_silence_is_trimmed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    prompt = tmp_path / "debate_voice_1.wav"
    prompt.write_bytes(b"prompt")
    monkeypatch.setattr(settings, "lighttts_prompt_wav_path", str(prompt))
    monkeypatch.setattr(settings, "media_root", str(tmp_path / "media"))
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            samples = ([10_000] * 100) + ([0] * 2_300)
        else:
            samples = ([0] * 2_300) + ([-10_000] * 100)
        return httpx.Response(200, content=pcm16_wav_bytes(samples, sample_rate=1_000))

    provider = LightTTSProvider(httpx.MockTransport(handler))
    with pytest.raises(ProviderError, match="音频无效或过短"):
        await provider.synthesize("甲" * 60, room_code="trimmed-short-room", speech_id="speech")

    assert calls == 2
    target_dir = tmp_path / "media" / "trimmed-short-room"
    assert not (target_dir / "speech.wav").exists()
    assert list(target_dir.glob("*.part")) == []


async def test_lighttts_retries_transient_server_failures(tmp_path: Path, monkeypatch) -> None:
    prompt = tmp_path / "debate_voice_1.wav"
    prompt.write_bytes(b"prompt")
    monkeypatch.setattr(settings, "lighttts_prompt_wav_path", str(prompt))
    monkeypatch.setattr(settings, "lighttts_prompt_text", "提示文本")
    monkeypatch.setattr(settings, "media_root", str(tmp_path / "media"))
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(500, json={"message": "temporary GPU failure"})
        return httpx.Response(200, content=wav_bytes(0.8))

    provider = LightTTSProvider(httpx.MockTransport(handler))
    provider.RETRY_BASE_SECONDS = 0
    await provider.synthesize("瞬时故障重试", room_code="retry-room", speech_id="retry-speech")
    assert calls == 3
    assert (tmp_path / "media" / "retry-room" / "retry-speech.wav").is_file()


async def test_lighttts_retries_transport_errors_but_not_permanent_4xx(tmp_path: Path, monkeypatch) -> None:
    prompt = tmp_path / "debate_voice_1.wav"
    prompt.write_bytes(b"prompt")
    monkeypatch.setattr(settings, "lighttts_prompt_wav_path", str(prompt))
    monkeypatch.setattr(settings, "lighttts_prompt_text", "提示文本")
    monkeypatch.setattr(settings, "media_root", str(tmp_path / "media"))
    transport_calls = 0

    async def transport_handler(request: httpx.Request) -> httpx.Response:
        nonlocal transport_calls
        transport_calls += 1
        if transport_calls == 1:
            raise httpx.ReadTimeout("temporary timeout", request=request)
        return httpx.Response(200, content=wav_bytes(0.8))

    provider = LightTTSProvider(httpx.MockTransport(transport_handler))
    provider.RETRY_BASE_SECONDS = 0
    await provider.synthesize("网络故障重试", room_code="transport-room", speech_id="transport-speech")
    assert transport_calls == 2

    permanent_calls = 0

    async def permanent_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal permanent_calls
        permanent_calls += 1
        return httpx.Response(400, json={"message": "invalid request"})

    permanent = LightTTSProvider(httpx.MockTransport(permanent_handler))
    permanent.RETRY_BASE_SECONDS = 0
    with pytest.raises(ProviderError, match="400 Bad Request"):
        await permanent.synthesize("永久错误", room_code="permanent-room", speech_id="permanent-speech")
    assert permanent_calls == 1


async def test_lighttts_uses_frozen_service_endpoint_and_settings(tmp_path: Path, monkeypatch) -> None:
    prompt = tmp_path / "debate_voice_1.wav"
    prompt.write_bytes(b"prompt")
    monkeypatch.setattr(settings, "lighttts_prompt_wav_path", str(prompt))
    monkeypatch.setattr(settings, "lighttts_prompt_text", "测试提示文本")
    monkeypatch.setattr(settings, "media_root", str(tmp_path / "media"))

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("http://tts-snapshot.test/inference")
        body = await request.aread()
        assert b"1.25" in body
        return httpx.Response(200, content=wav_bytes(1.2))

    provider = LightTTSProvider(httpx.MockTransport(handler))
    result = await provider.synthesize(
        "使用比赛快照中的语音服务。",
        room_code="snapshot-room",
        speech_id="snapshot-speech",
        provider_config={
            "endpoint": "http://tts-snapshot.test/inference",
            "enabled": True,
            "settings": {"read_timeout_seconds": 45, "speed": 1.25},
        },
    )
    assert result == "/media/snapshot-room/snapshot-speech.wav"
    with pytest.raises(ProviderError, match="已停用"):
        await provider.synthesize(
            "不会发送",
            room_code="disabled-room",
            speech_id="disabled-speech",
            provider_config={"endpoint": "http://tts.test/inference", "enabled": False, "settings": {}},
        )


def test_provider_config_validation_and_legacy_fallback() -> None:
    endpoint, values = normalize_provider_config("funasr", "wss://asr.test/ws", {"final_wait_seconds": 25.5})
    assert endpoint == "wss://asr.test/ws" and values == {"final_wait_seconds": 25.5}
    endpoint, values = normalize_provider_config(
        "lighttts",
        "https://tts.test/inference",
        {"read_timeout_seconds": 90, "speed": 1.1},
    )
    assert endpoint == "https://tts.test/inference" and values == {"read_timeout_seconds": 90, "speed": 1.1}
    with pytest.raises(ValueError, match="ws://"):
        normalize_provider_config("funasr", "http://asr.test", {})
    with pytest.raises(ValueError, match="不支持参数"):
        normalize_provider_config("lighttts", "http://tts.test", {"max_active": 99})
    fallback = runtime_provider_config({}, "funasr")
    assert fallback["source"] == "environment" and fallback["endpoint"] == settings.funasr_ws_url

    endpoint, values = normalize_provider_config(
        "agent",
        "https://117.50.218.251/debate/api/debate",
        {
            "method": "POST",
            "protocol": "restful",
            "health_endpoint": "https://117.50.218.251/debate/api/health",
            "timeout_seconds": 120,
            "stream": True,
        },
    )
    assert endpoint.endswith("/debate/api/debate")
    assert values["method"] == "POST" and values["protocol"] == "restful"
    with pytest.raises(ValueError, match="RESTful POST"):
        normalize_provider_config("agent", endpoint, {"method": "GET"})


async def test_lighttts_uses_requested_voice_prompt(tmp_path: Path, monkeypatch) -> None:
    default_prompt = tmp_path / "debate_voice_1.wav"
    requested_prompt = tmp_path / "debate_voice_2.wav"
    default_prompt.write_bytes(b"voice-one")
    requested_prompt.write_bytes(b"voice-two")
    monkeypatch.setattr(settings, "lighttts_prompt_wav_path", str(default_prompt))
    monkeypatch.setattr(settings, "lighttts_prompt_text", "所有音色使用同一段提示文本")
    monkeypatch.setattr(settings, "lighttts_prompt_texts", {"debate_voice_2": "第二音色专属提示文本"})
    monkeypatch.setattr(settings, "media_root", str(tmp_path / "media"))

    async def handler(request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        assert b"debate_voice_2.wav" in body
        assert b"voice-two" in body
        assert "第二音色专属提示文本".encode() in body
        return httpx.Response(200, content=wav_bytes(0.5))

    provider = LightTTSProvider(httpx.MockTransport(handler))
    assert provider.prompt_path("debate_voice_2") == requested_prompt
    await provider.synthesize("第二音色", room_code="123456", speech_id="speech-2", voice="debate_voice_2")


async def test_lighttts_missing_voice_uses_default_prompt_and_matching_text(tmp_path: Path, monkeypatch) -> None:
    default_prompt = tmp_path / "debate_voice_1.wav"
    default_prompt.write_bytes(b"voice-one")
    monkeypatch.setattr(settings, "lighttts_prompt_wav_path", str(default_prompt))
    monkeypatch.setattr(settings, "lighttts_prompt_text", "全局回退提示文本")
    monkeypatch.setattr(
        settings,
        "lighttts_prompt_texts",
        {"debate_voice_1": "第一音色提示文本", "missing_voice": "不得与默认音频混用"},
    )
    monkeypatch.setattr(settings, "media_root", str(tmp_path / "media"))

    async def handler(request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        assert b"debate_voice_1.wav" in body
        assert "第一音色提示文本".encode() in body
        assert "不得与默认音频混用".encode() not in body
        return httpx.Response(200, content=wav_bytes(0.5))

    provider = LightTTSProvider(httpx.MockTransport(handler))
    await provider.synthesize("未知音色回退", room_code="123456", speech_id="missing", voice="missing_voice")


async def test_lighttts_cancels_waiting_job_before_gpu_request(tmp_path: Path, monkeypatch) -> None:
    prompt = tmp_path / "debate_voice_1.wav"
    prompt.write_bytes(b"prompt")
    monkeypatch.setattr(settings, "lighttts_prompt_wav_path", str(prompt))
    monkeypatch.setattr(settings, "media_root", str(tmp_path / "media"))
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        first_started.set()
        await release_first.wait()
        return httpx.Response(200, content=b"\x00\x00" * 6000)

    provider = LightTTSProvider(httpx.MockTransport(handler))
    provider._semaphore = asyncio.Semaphore(1)
    first = asyncio.create_task(provider.synthesize("第一条", room_code="1", speech_id="first"))
    await first_started.wait()
    second = asyncio.create_task(provider.synthesize("第二条", room_code="2", speech_id="second", should_cancel=lambda: True))
    with pytest.raises(ProviderCancelled):
        await asyncio.wait_for(second, timeout=0.8)
    assert calls == 1, "cancelled waiter must leave the queue before a GPU slot becomes available"
    release_first.set()
    await first
    assert calls == 1


async def test_lighttts_cached_output_bypasses_a_busy_gpu_queue(tmp_path: Path, monkeypatch) -> None:
    prompt = tmp_path / "debate_voice_1.wav"
    prompt.write_bytes(b"prompt")
    media = tmp_path / "media"
    cached = media / "cached-room" / "cached-speech.wav"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(wav_bytes(1))
    monkeypatch.setattr(settings, "lighttts_prompt_wav_path", str(prompt))
    monkeypatch.setattr(settings, "media_root", str(media))
    request_started = asyncio.Event()
    release_request = asyncio.Event()

    async def handler(_request: httpx.Request) -> httpx.Response:
        request_started.set()
        await release_request.wait()
        return httpx.Response(200, content=wav_bytes(1.5))

    provider = LightTTSProvider(httpx.MockTransport(handler))
    provider._semaphore = asyncio.Semaphore(1)
    active = asyncio.create_task(provider.synthesize("占用语音资源。", room_code="active-room", speech_id="active-speech"))
    await asyncio.wait_for(request_started.wait(), timeout=1)
    result = await asyncio.wait_for(
        provider.synthesize("重启后复用完整输出。", room_code="cached-room", speech_id="cached-speech"),
        timeout=0.2,
    )
    assert result == "/media/cached-room/cached-speech.wav"
    release_request.set()
    await active


async def test_lighttts_recreates_room_directory_before_writing_response(tmp_path: Path, monkeypatch) -> None:
    prompt = tmp_path / "debate_voice_1.wav"
    prompt.write_bytes(b"prompt")
    media = tmp_path / "media"
    monkeypatch.setattr(settings, "lighttts_prompt_wav_path", str(prompt))
    monkeypatch.setattr(settings, "media_root", str(media))
    request_started = asyncio.Event()
    release_request = asyncio.Event()

    async def handler(_request: httpx.Request) -> httpx.Response:
        request_started.set()
        await release_request.wait()
        return httpx.Response(200, content=wav_bytes(1.5))

    provider = LightTTSProvider(httpx.MockTransport(handler))
    task = asyncio.create_task(provider.synthesize("目录被维护任务移除后仍应安全落盘。", room_code="removed-room", speech_id="speech"))
    await asyncio.wait_for(request_started.wait(), timeout=1)
    shutil.rmtree(media / "removed-room")
    release_request.set()
    assert await asyncio.wait_for(task, timeout=1) == "/media/removed-room/speech.wav"
    assert (media / "removed-room" / "speech.wav").is_file()


async def test_lighttts_background_prefetch_does_not_fill_all_live_slots(tmp_path: Path, monkeypatch) -> None:
    prompt = tmp_path / "debate_voice_1.wav"
    prompt.write_bytes(b"prompt")
    monkeypatch.setattr(settings, "lighttts_prompt_wav_path", str(prompt))
    monkeypatch.setattr(settings, "media_root", str(tmp_path / "media"))
    monkeypatch.setattr(settings, "lighttts_global_gate_enabled", True)
    release = asyncio.Event()
    first_started = asyncio.Event()
    two_started = asyncio.Event()
    calls: list[str] = []
    gate_calls: list[str] = []

    class Lease:
        lost = False

        async def cancellation_requested(self, callback) -> bool:
            return await LightTTSProvider._cancel_requested(callback)

        async def release(self) -> None:
            return None

        async def abandon(self) -> None:
            return None

    class Gate:
        async def acquire(self, _should_cancel=None, **_kwargs):
            gate_calls.append(asyncio.current_task().get_name())
            return Lease()

        def record_cancel(self) -> None:
            return None

        def record_timeout(self) -> None:
            return None

    monkeypatch.setattr(providers_service, "lighttts_admission_gate", Gate())

    async def handler(request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        label = "live" if "实时辩手".encode() in body else "background"
        calls.append(label)
        first_started.set()
        if len(calls) >= 2:
            two_started.set()
        await release.wait()
        return httpx.Response(200, content=wav_bytes(1))

    provider = LightTTSProvider(httpx.MockTransport(handler))
    provider._semaphore = asyncio.Semaphore(2)
    provider._background_semaphore = asyncio.Semaphore(1)
    first_background = asyncio.create_task(
        provider.synthesize("后台预生成甲。", room_code="cue-a", speech_id="cue-a", background=True),
        name="background-a",
    )
    await asyncio.wait_for(first_started.wait(), timeout=1)
    second_background = asyncio.create_task(
        provider.synthesize("后台预生成乙。", room_code="cue-b", speech_id="cue-b", background=True),
        name="background-b",
    )
    live = asyncio.create_task(
        provider.synthesize("实时辩手发言。", room_code="live", speech_id="live"),
        name="live",
    )
    try:
        await asyncio.wait_for(two_started.wait(), timeout=1)
        assert calls == ["background", "live"]
        assert len(gate_calls) == 2, "the second background job must wait locally before Redis admission"
    finally:
        release.set()
        await asyncio.gather(first_background, second_background, live, return_exceptions=True)
    assert calls == ["background", "live", "background"]
    assert len(gate_calls) == 3


async def test_lighttts_background_local_wait_respects_whole_job_deadline(tmp_path: Path, monkeypatch) -> None:
    prompt = tmp_path / "debate_voice_1.wav"
    prompt.write_bytes(b"prompt")
    monkeypatch.setattr(settings, "lighttts_prompt_wav_path", str(prompt))
    monkeypatch.setattr(settings, "media_root", str(tmp_path / "media"))
    monkeypatch.setattr(settings, "lighttts_global_gate_enabled", True)
    monkeypatch.setattr(settings, "lighttts_job_timeout_seconds", 0.08)
    gate_acquires = 0
    timeouts = 0
    cancels = 0

    class Gate:
        async def acquire(self, _should_cancel=None, **_kwargs):
            nonlocal gate_acquires
            gate_acquires += 1
            raise AssertionError("expired background work must not enter Redis admission")

        def record_timeout(self) -> None:
            nonlocal timeouts
            timeouts += 1

        def record_cancel(self) -> None:
            nonlocal cancels
            cancels += 1

    async def forbidden(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("expired background work must not call LightTTS")

    monkeypatch.setattr(providers_service, "lighttts_admission_gate", Gate())
    provider = LightTTSProvider(httpx.MockTransport(forbidden))
    provider._background_semaphore = asyncio.Semaphore(0)
    started = asyncio.get_running_loop().time()
    with pytest.raises(ProviderError, match="全任务超时"):
        await provider.synthesize("后台等待也受总时限约束。", room_code="deadline-bg", speech_id="speech", background=True)
    assert asyncio.get_running_loop().time() - started < 0.3
    assert gate_acquires == 0
    assert timeouts == 1
    assert cancels == 0
    assert not (tmp_path / "media" / "deadline-bg" / "speech.wav").exists()


async def test_lighttts_background_local_wait_records_caller_cancel_before_admission(tmp_path: Path, monkeypatch) -> None:
    prompt = tmp_path / "debate_voice_1.wav"
    prompt.write_bytes(b"prompt")
    monkeypatch.setattr(settings, "lighttts_prompt_wav_path", str(prompt))
    monkeypatch.setattr(settings, "media_root", str(tmp_path / "media"))
    monkeypatch.setattr(settings, "lighttts_global_gate_enabled", True)
    monkeypatch.setattr(settings, "lighttts_job_timeout_seconds", 1)
    gate_acquires = 0
    timeouts = 0
    cancels = 0
    cancelled = False

    class Gate:
        async def acquire(self, _should_cancel=None, **_kwargs):
            nonlocal gate_acquires
            gate_acquires += 1
            raise AssertionError("cancelled background work must not enter Redis admission")

        def record_timeout(self) -> None:
            nonlocal timeouts
            timeouts += 1

        def record_cancel(self) -> None:
            nonlocal cancels
            cancels += 1

    async def forbidden(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("cancelled background work must not call LightTTS")

    monkeypatch.setattr(providers_service, "lighttts_admission_gate", Gate())
    provider = LightTTSProvider(httpx.MockTransport(forbidden))
    provider._background_semaphore = asyncio.Semaphore(0)
    task = asyncio.create_task(
        provider.synthesize(
            "后台等待可以由房间状态取消。",
            room_code="cancel-bg",
            speech_id="speech",
            background=True,
            should_cancel=lambda: cancelled,
        )
    )
    await asyncio.sleep(0.02)
    cancelled = True
    with pytest.raises(ProviderCancelled, match="排队任务已取消"):
        await asyncio.wait_for(task, timeout=0.3)
    assert gate_acquires == 0
    assert timeouts == 0
    assert cancels == 1
    assert not (tmp_path / "media" / "cancel-bg" / "speech.wav").exists()


async def test_lighttts_cancels_active_request_without_leaving_partial_audio(tmp_path: Path, monkeypatch) -> None:
    prompt = tmp_path / "debate_voice_1.wav"
    prompt.write_bytes(b"prompt")
    monkeypatch.setattr(settings, "lighttts_prompt_wav_path", str(prompt))
    monkeypatch.setattr(settings, "media_root", str(tmp_path / "media"))
    request_started = asyncio.Event()
    release_request = asyncio.Event()
    cancelled = False
    cancel_count_before = providers_service.lighttts_admission_gate.metrics.snapshot()["events"]["cancel"]

    async def handler(_request: httpx.Request) -> httpx.Response:
        request_started.set()
        await release_request.wait()
        return httpx.Response(200, content=wav_bytes(1))

    provider = LightTTSProvider(httpx.MockTransport(handler))
    provider._semaphore = asyncio.Semaphore(1)
    task = asyncio.create_task(
        provider.synthesize(
            "正在运行的任务也必须能够取消。",
            room_code="active-cancel",
            speech_id="speech",
            should_cancel=lambda: cancelled,
        )
    )
    await asyncio.wait_for(request_started.wait(), timeout=1)
    cancelled = True
    await asyncio.sleep(0.3)
    assert not task.done(), "cancelled inference must retain its GPU slot until the server finishes"
    release_request.set()
    with pytest.raises(ProviderCancelled, match="运行任务"):
        await asyncio.wait_for(task, timeout=1)
    target_dir = tmp_path / "media" / "active-cancel"
    assert not (target_dir / "speech.wav").exists()
    assert list(target_dir.glob("*.part")) == []
    cancel_count_after = providers_service.lighttts_admission_gate.metrics.snapshot()["events"]["cancel"]
    assert cancel_count_after == cancel_count_before + 1


async def test_lighttts_late_cancel_after_atomic_publish_does_not_delete_final(
    tmp_path: Path,
    monkeypatch,
) -> None:
    prompt = tmp_path / "debate_voice_1.wav"
    prompt.write_bytes(b"prompt")
    monkeypatch.setattr(settings, "lighttts_prompt_wav_path", str(prompt))
    monkeypatch.setattr(settings, "media_root", str(tmp_path / "media"))
    cancelled = False
    original_replace = providers_service.os.replace

    def publish_then_cancel(source: Path, target: Path) -> None:
        nonlocal cancelled
        original_replace(source, target)
        cancelled = True

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=wav_bytes(1))

    monkeypatch.setattr(providers_service.os, "replace", publish_then_cancel)
    provider = LightTTSProvider(httpx.MockTransport(handler))
    result = await provider.synthesize(
        "发布后取消。",
        room_code="publish-cancel-room",
        speech_id="speech",
        should_cancel=lambda: cancelled,
    )

    target = tmp_path / "media" / "publish-cancel-room" / "speech.wav"
    assert result == "/media/publish-cancel-room/speech.wav"
    assert target.is_file()
    assert provider._wav_duration(target) == pytest.approx(1, abs=0.01)


async def test_lighttts_whole_job_deadline_includes_retry_budget_and_removes_stale_audio(
    tmp_path: Path, monkeypatch
) -> None:
    prompt = tmp_path / "debate_voice_1.wav"
    prompt.write_bytes(b"prompt")
    media = tmp_path / "media"
    target_dir = media / "deadline-room"
    target_dir.mkdir(parents=True)
    (target_dir / "speech.wav").write_bytes(wav_bytes(0.01))
    stale_part = target_dir / ".speech.abandoned.segment.wav.part"
    stale_part.write_bytes(b"stale")
    os.utime(stale_part, (0, 0))
    monkeypatch.setattr(settings, "lighttts_prompt_wav_path", str(prompt))
    monkeypatch.setattr(settings, "media_root", str(media))
    monkeypatch.setattr(settings, "lighttts_job_timeout_seconds", 0.08)
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.04)
        return httpx.Response(500, json={"message": "retry after the remaining budget"})

    provider = LightTTSProvider(httpx.MockTransport(handler))
    provider.RETRY_BASE_SECONDS = 0.1
    with pytest.raises(ProviderError, match="全任务超时"):
        await provider.synthesize("超时后不得保留旧音频。", room_code="deadline-room", speech_id="speech")
    assert calls == 1, "the whole-job deadline must prevent a retry outside the remaining budget"
    assert not (target_dir / "speech.wav").exists()
    assert list(target_dir.glob("*.part")) == []


async def test_lighttts_stale_cleanup_preserves_a_fresh_part_from_another_writer(
    tmp_path: Path,
    monkeypatch,
) -> None:
    prompt = tmp_path / "debate_voice_1.wav"
    prompt.write_bytes(b"prompt")
    media = tmp_path / "media"
    target_dir = media / "fresh-writer-room"
    target_dir.mkdir(parents=True)
    fresh_part = target_dir / ".speech.other-writer.segment.wav.part"
    fresh_part.write_bytes(b"active writer")
    monkeypatch.setattr(settings, "lighttts_prompt_wav_path", str(prompt))
    monkeypatch.setattr(settings, "media_root", str(media))

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=wav_bytes(1))

    provider = LightTTSProvider(httpx.MockTransport(handler))
    result = await provider.synthesize("活跃写入文件不能互删。", room_code="fresh-writer-room", speech_id="speech")

    assert result == "/media/fresh-writer-room/speech.wav"
    assert fresh_part.read_bytes() == b"active writer"
    assert (target_dir / "speech.wav").is_file()


async def test_lighttts_active_lease_loss_discards_response_and_partial_files(tmp_path: Path, monkeypatch) -> None:
    prompt = tmp_path / "debate_voice_1.wav"
    prompt.write_bytes(b"prompt")
    media = tmp_path / "media"
    monkeypatch.setattr(settings, "lighttts_prompt_wav_path", str(prompt))
    monkeypatch.setattr(settings, "media_root", str(media))
    monkeypatch.setattr(settings, "lighttts_global_gate_enabled", True)
    request_started = asyncio.Event()
    release_request = asyncio.Event()

    class LostLease:
        lost = False

        async def cancellation_requested(self, callback) -> bool:
            return self.lost or await LightTTSProvider._cancel_requested(callback)

        async def release(self) -> None:
            return None

    lease = LostLease()

    class Gate:
        async def acquire(self, _should_cancel=None, **_kwargs):
            return lease

    async def handler(_request: httpx.Request) -> httpx.Response:
        request_started.set()
        await release_request.wait()
        return httpx.Response(200, content=wav_bytes(1))

    monkeypatch.setattr(providers_service, "lighttts_admission_gate", Gate())
    provider = LightTTSProvider(httpx.MockTransport(handler))
    task = asyncio.create_task(provider.synthesize("租约丢失必须安全失败。", room_code="lease-lost", speech_id="speech"))
    await asyncio.wait_for(request_started.wait(), timeout=1)
    lease.lost = True
    await asyncio.sleep(0.3)
    assert not task.done(), "the local slot remains held until the in-flight GPU response drains"
    release_request.set()
    with pytest.raises(ProviderError, match="租约丢失"):
        await asyncio.wait_for(task, timeout=1)
    target_dir = media / "lease-lost"
    assert not (target_dir / "speech.wav").exists()
    assert list(target_dir.glob("*.part")) == []


async def test_external_task_cancel_drains_gpu_before_releasing_global_lease(tmp_path: Path, monkeypatch) -> None:
    prompt = tmp_path / "debate_voice_1.wav"
    prompt.write_bytes(b"prompt")
    media = tmp_path / "media"
    monkeypatch.setattr(settings, "lighttts_prompt_wav_path", str(prompt))
    monkeypatch.setattr(settings, "media_root", str(media))
    monkeypatch.setattr(settings, "lighttts_global_gate_enabled", True)
    request_started = asyncio.Event()
    release_request = asyncio.Event()
    released = 0
    abandoned = 0

    class Lease:
        lost = False

        async def cancellation_requested(self, callback) -> bool:
            return await LightTTSProvider._cancel_requested(callback)

        async def release(self) -> None:
            nonlocal released
            released += 1

        async def abandon(self) -> None:
            nonlocal abandoned
            abandoned += 1

    class Gate:
        async def acquire(self, _should_cancel=None, **_kwargs):
            return Lease()

        def record_cancel(self) -> None:
            return None

    async def handler(_request: httpx.Request) -> httpx.Response:
        request_started.set()
        await release_request.wait()
        return httpx.Response(200, content=wav_bytes(1))

    monkeypatch.setattr(providers_service, "lighttts_admission_gate", Gate())
    provider = LightTTSProvider(httpx.MockTransport(handler))
    waiter = asyncio.create_task(
        provider.synthesize("外部取消必须先安全排空。", room_code="external-cancel", speech_id="speech")
    )
    await asyncio.wait_for(request_started.wait(), timeout=1)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    await asyncio.sleep(0.3)
    assert released == 0 and abandoned == 0
    assert await provider.drain_cancelled_jobs(0.01) == 1
    release_request.set()
    assert await provider.drain_cancelled_jobs(1) == 0
    assert released == 1 and abandoned == 0
    target_dir = media / "external-cancel"
    assert not (target_dir / "speech.wav").exists()
    assert list(target_dir.glob("*.part")) == []


async def test_shutdown_limit_abandons_inflight_lease_instead_of_releasing_it(tmp_path: Path, monkeypatch) -> None:
    prompt = tmp_path / "debate_voice_1.wav"
    prompt.write_bytes(b"prompt")
    monkeypatch.setattr(settings, "lighttts_prompt_wav_path", str(prompt))
    monkeypatch.setattr(settings, "media_root", str(tmp_path / "media"))
    monkeypatch.setattr(settings, "lighttts_global_gate_enabled", True)
    request_started = asyncio.Event()
    released = 0
    abandoned = 0

    class Lease:
        lost = False

        async def cancellation_requested(self, callback) -> bool:
            return await LightTTSProvider._cancel_requested(callback)

        async def release(self) -> None:
            nonlocal released
            released += 1

        async def abandon(self) -> None:
            nonlocal abandoned
            abandoned += 1

    class Gate:
        async def acquire(self, _should_cancel=None, **_kwargs):
            return Lease()

        def record_cancel(self) -> None:
            return None

    async def handler(_request: httpx.Request) -> httpx.Response:
        request_started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(providers_service, "lighttts_admission_gate", Gate())
    provider = LightTTSProvider(httpx.MockTransport(handler))
    waiter = asyncio.create_task(provider.synthesize("关停上限后保留租约。", room_code="shutdown", speech_id="speech"))
    await asyncio.wait_for(request_started.wait(), timeout=1)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    internal = next(iter(provider._draining_jobs))
    internal.cancel()
    await asyncio.gather(internal, return_exceptions=True)
    assert released == 0 and abandoned == 1
    assert not (tmp_path / "media" / "shutdown" / "speech.wav").exists()


async def test_lighttts_reuses_valid_atomic_output_after_process_restart(tmp_path: Path, monkeypatch) -> None:
    prompt = tmp_path / "debate_voice_1.wav"
    prompt.write_bytes(b"prompt")
    media = tmp_path / "media"
    target = media / "restart-room" / "restart-speech.wav"
    target.parent.mkdir(parents=True)
    target.write_bytes(wav_bytes(1))
    monkeypatch.setattr(settings, "lighttts_prompt_wav_path", str(prompt))
    monkeypatch.setattr(settings, "media_root", str(media))

    async def forbidden(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("valid existing output must be reused")

    provider = LightTTSProvider(httpx.MockTransport(forbidden))
    result = await provider.synthesize("重启后复用完整输出。", room_code="restart-room", speech_id="restart-speech")
    assert result == "/media/restart-room/restart-speech.wav"
    assert provider._wav_duration(target) == pytest.approx(1, abs=0.01)


async def test_lighttts_keeps_global_bound_and_skips_cancelled_waiter(tmp_path: Path, monkeypatch) -> None:
    prompt = tmp_path / "debate_voice_1.wav"
    prompt.write_bytes(b"prompt")
    monkeypatch.setattr(settings, "lighttts_prompt_wav_path", str(prompt))
    monkeypatch.setattr(settings, "media_root", str(tmp_path / "media"))
    release = asyncio.Event()
    two_started = asyncio.Event()
    active = 0
    max_active = 0
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal active, max_active, calls
        active += 1
        calls += 1
        max_active = max(max_active, active)
        if calls >= 2:
            two_started.set()
        await release.wait()
        active -= 1
        return httpx.Response(200, content=wav_bytes(1))

    provider = LightTTSProvider(httpx.MockTransport(handler))
    provider._semaphore = asyncio.Semaphore(2)
    cancelled = [False] * 6
    tasks = [
        asyncio.create_task(
            provider.synthesize(
                f"第 {index + 1} 个并发语音任务。",
                room_code=f"room-{index}",
                speech_id=f"speech-{index}",
                should_cancel=lambda index=index: cancelled[index],
            )
        )
        for index in range(6)
    ]
    await asyncio.wait_for(two_started.wait(), timeout=1)
    cancelled[0] = True
    cancelled[4] = True
    await asyncio.sleep(0.3)
    assert calls == 2 and max_active == 2
    release.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert isinstance(results[0], ProviderCancelled)
    assert isinstance(results[4], ProviderCancelled)
    assert all(isinstance(results[index], str) for index in (1, 2, 3, 5))
    assert calls == 5 and max_active == 2
    for index in range(6):
        exists = (tmp_path / "media" / f"room-{index}" / f"speech-{index}.wav").exists()
        assert exists is (index in {1, 2, 3, 5})


async def test_lighttts_retries_an_unusable_voice_with_the_default_prompt(tmp_path: Path, monkeypatch) -> None:
    default_prompt = tmp_path / "debate_voice_1.wav"
    broken_prompt = tmp_path / "debate_voice_2.wav"
    default_prompt.write_bytes(b"voice-one")
    broken_prompt.write_bytes(b"voice-two")
    monkeypatch.setattr(settings, "lighttts_prompt_wav_path", str(default_prompt))
    monkeypatch.setattr(
        settings,
        "lighttts_prompt_texts",
        {"debate_voice_1": "第一音色提示文本", "debate_voice_2": "第二音色提示文本"},
    )
    monkeypatch.setattr(settings, "media_root", str(tmp_path / "media"))
    prompts: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        prompt = "debate_voice_2.wav" if b"debate_voice_2.wav" in body else "debate_voice_1.wav"
        expected_prompt_text = "第二音色提示文本" if prompt.endswith("2.wav") else "第一音色提示文本"
        assert expected_prompt_text.encode() in body
        prompts.append(prompt)
        return httpx.Response(200, content=wav_bytes(0.05 if prompt.endswith("2.wav") else 2.4))

    provider = LightTTSProvider(httpx.MockTransport(handler))
    await provider.synthesize("第二音色输出过短时必须自动回退。", room_code="123456", speech_id="fallback", voice="debate_voice_2")
    target = tmp_path / "media" / "123456" / "fallback.wav"
    assert prompts == ["debate_voice_2.wav", "debate_voice_1.wav"]
    assert provider._wav_duration(target) == pytest.approx(2.4, abs=0.01)
