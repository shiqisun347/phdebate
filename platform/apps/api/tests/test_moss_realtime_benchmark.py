from __future__ import annotations

import importlib.util
import json
import wave
from pathlib import Path

import pytest
import websockets

MODULE_PATH = Path(__file__).parents[3] / "scripts" / "benchmark_moss_realtime_sessions.py"
SPEC = importlib.util.spec_from_file_location("benchmark_moss_realtime_sessions", MODULE_PATH)
assert SPEC and SPEC.loader
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)


def test_text_deltas_preserve_the_complete_input() -> None:
    text = "第一段。第二段包含更多文字。"
    chunks = benchmark.text_deltas(text, 4)
    assert "".join(chunks) == text
    assert all(1 <= len(chunk) <= 4 for chunk in chunks)
    production_chunks = benchmark.production_text_deltas(text, 2)
    assert "".join(production_chunks) == text


def test_non_silent_pcm_requires_sustained_signal() -> None:
    assert benchmark.pcm_chunk_is_non_silent(b"\x00\x00" * 480) is False
    assert benchmark.pcm_chunk_is_non_silent(b"\x00\x01" * 480) is True


def test_write_wav_preserves_native_pcm_format(tmp_path: Path) -> None:
    target = tmp_path / "moss.wav"
    benchmark.write_wav(target, b"\x01\x00" * 24_000, 24_000)
    with wave.open(str(target), "rb") as audio:
        assert audio.getframerate() == 24_000
        assert audio.getnchannels() == 1
        assert audio.getsampwidth() == 2
        assert audio.getnframes() == 24_000


def test_continuous_playback_distinguishes_network_gaps_from_audible_underruns() -> None:
    buffered = benchmark.simulate_continuous_playback(
        [0.0, 0.35, 0.7],
        [0.4, 0.4, 0.4],
        initial_buffer_ms=100,
    )
    assert buffered["underrun_count"] == 0
    assert buffered["startup_wait_ms"] == pytest.approx(0.0)
    assert buffered["minimum_queue_lead_ms"] == pytest.approx(50.0)

    starved = benchmark.simulate_continuous_playback(
        [0.0, 0.65, 1.3],
        [0.08, 0.4, 0.4],
        initial_buffer_ms=100,
    )
    assert starved["startup_wait_ms"] == pytest.approx(650.0)
    assert starved["underrun_count"] == 1
    assert starved["max_underrun_ms"] == pytest.approx(170.0)


def test_websocket_endpoint_preserves_security_and_uses_formal_path() -> None:
    assert benchmark.websocket_endpoint("http://gateway.test/") == "ws://gateway.test/tts/session/ws"
    assert benchmark.websocket_endpoint("https://gateway.test/base") == "wss://gateway.test/base/tts/session/ws"
    assert benchmark.websocket_endpoint("wss://gateway.test/tts/session/ws") == "wss://gateway.test/tts/session/ws"
    assert benchmark.health_endpoint("wss://gateway.test/base") == "https://gateway.test/base/health/ready"
    assert benchmark.health_endpoint("wss://gateway.test/tts/session/ws") == "https://gateway.test/health/ready"
    with pytest.raises(ValueError, match="endpoint must use"):
        benchmark.websocket_endpoint("gateway.test")


def test_cli_defaults_to_websocket_and_http_requires_explicit_selection(tmp_path: Path) -> None:
    base = [
        "--endpoint",
        "http://gateway.test",
        "--voice-prompt",
        "debate_voice_1=debate_voice_1.wav",
        "--output-dir",
        str(tmp_path),
    ]
    assert benchmark.argument_parser().parse_args(base).transport == "websocket"
    assert benchmark.argument_parser().parse_args(base).canary_stage_observability is False
    assert benchmark.argument_parser().parse_args([*base, "--canary-stage-observability"]).canary_stage_observability is True
    assert benchmark.argument_parser().parse_args([*base, "--transport", "http"]).transport == "http"


@pytest.mark.asyncio
async def test_websocket_benchmark_uses_one_incremental_session_and_demuxes_pcm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: list[dict] = []

    async def mock_gateway(socket) -> None:
        assert socket.request.headers["X-MOSS-Gateway-Key"] == "benchmark-secret"
        start = json.loads(await socket.recv())
        received.append(start)
        assert start["type"] == "start" and start["seq"] == 0
        session_id = start["session_id"]
        await socket.send(
            json.dumps(
                {
                    "type": "ready",
                    "session_id": session_id,
                    "voice": "debate_voice_1",
                    "next_seq": 1,
                    "audio": {"codec": "pcm_s16le", "sample_rate": 24_000, "channels": 1},
                }
            )
        )
        delta = json.loads(await socket.recv())
        received.append(delta)
        await socket.send(b"\x00\x01" * 480)
        await socket.send(json.dumps({"type": "ack", "seq": delta["seq"]}))
        final = json.loads(await socket.recv())
        received.append(final)
        await socket.send(json.dumps({"type": "ack", "seq": final["seq"]}))
        await socket.send(b"\x00\x01" * 480)
        await socket.send(json.dumps({"type": "audio_end", "session_id": session_id}))
        await socket.send(
            json.dumps(
                {"type": "released", "session_id": session_id, "released": True, "status": "closed"}
            )
        )
        await socket.close()

    async def release_confirmed(**_kwargs) -> bool:
        return True

    monkeypatch.setattr(benchmark, "confirm_release", release_confirmed)
    server = await websockets.serve(mock_gateway, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        result = await benchmark.run_request(
            endpoint=f"http://127.0.0.1:{port}",
            voice="debate_voice_1",
            prompt_audio="debate_voice_1.wav",
            text="这是正文。",
            chunk_characters=12,
            delta_delay_seconds=0,
            finish_delay_seconds=0,
            output_path=tmp_path / "formal-ws.wav",
            request_index=1,
            timeout_seconds=2,
            api_key="benchmark-secret",
        )
    finally:
        server.close()
        await server.wait_closed()

    assert [message["type"] for message in received] == ["start", "text_delta", "final"]
    assert [message["seq"] for message in received] == [0, 1, 2]
    assert result["transport"] == "websocket"
    assert result["close_ack"] is True and result["release_confirmed"] is True
    assert result["chunks"] == 2 and result["pcm_bytes"] == 1_920
    assert result["continuous_playback"]["100"]["underrun_count"] == 0
    assert result["first_pcm_ms"] >= 0
    assert result["first_pcm_from_request_start_ms"] >= result["first_pcm_ms"]
    assert result["handshake_ms"] >= result["transport_connect_ms"]
    assert (tmp_path / "formal-ws.wav").exists()


@pytest.mark.asyncio
async def test_websocket_benchmark_records_canary_stage_and_per_pcm_mapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def mock_gateway(socket) -> None:
        start = json.loads(await socket.recv())
        assert start["canary_observe"] is True
        session_id = start["session_id"]
        await socket.send(
            json.dumps(
                {
                    "type": "ready",
                    "session_id": session_id,
                    "voice": "debate_voice_1",
                    "next_seq": 1,
                    "audio": {"codec": "pcm_s16le", "sample_rate": 24_000, "channels": 1},
                    "canary_observe": True,
                }
            )
        )
        delta = json.loads(await socket.recv())
        await socket.send(
            json.dumps(
                {
                    "type": "canary.stage",
                    "session_id": session_id,
                    "stage": "prefill",
                    "stage_index": 0,
                    "duration_ms": 100.0,
                    "turn_elapsed_ms": 100.0,
                    "source_stage": None,
                    "source_stage_index": None,
                }
            )
        )
        await socket.send(
            json.dumps(
                {
                    "type": "canary.pcm",
                    "session_id": session_id,
                    "chunk_index": 0,
                    "source_stage": "prefill",
                    "source_stage_index": 0,
                    "source_stage_duration_ms": 100.0,
                    "decoder_stage_index": 1,
                    "decoder_yield_ms": 20.0,
                    "turn_elapsed_ms": 120.0,
                }
            )
        )
        await socket.send(b"\x00\x01" * 480)
        await socket.send(json.dumps({"type": "ack", "seq": delta["seq"]}))
        final = json.loads(await socket.recv())
        await socket.send(json.dumps({"type": "ack", "seq": final["seq"]}))
        await socket.send(json.dumps({"type": "audio_end", "session_id": session_id}))
        await socket.send(
            json.dumps(
                {"type": "released", "session_id": session_id, "released": True, "status": "closed"}
            )
        )

    async def release_confirmed(**_kwargs) -> bool:
        return True

    monkeypatch.setattr(benchmark, "confirm_release", release_confirmed)
    server = await websockets.serve(mock_gateway, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        result = await benchmark.run_request(
            endpoint=f"http://127.0.0.1:{port}",
            voice="debate_voice_1",
            prompt_audio="debate_voice_1.wav",
            text="正文不会进入观测元数据。",
            chunk_characters=20,
            delta_delay_seconds=0,
            finish_delay_seconds=0,
            output_path=tmp_path / "canary-ws.wav",
            request_index=1,
            timeout_seconds=2,
            api_key="benchmark-secret",
            canary_stage_observability=True,
        )
    finally:
        server.close()
        await server.wait_closed()

    assert result["canary_stage_observability"] is True
    assert result["native_stage_summary_ms"]["prefill"]["p50"] == 100.0
    assert result["chunk_timeline"][0]["native_stage"]["decoder_yield_ms"] == 20.0
    serialized = json.dumps(result, ensure_ascii=False)
    assert "正文不会进入观测元数据" not in serialized
    assert "benchmark-secret" not in serialized


def test_markdown_names_formal_transport_and_first_pcm_origin() -> None:
    document = {
        "generated_at": "2026-07-18T00:00:00+00:00",
        "endpoint_count": 3,
        "transport": "websocket",
        "scenarios": [],
    }
    rendered = benchmark.markdown(document)
    assert "Transport: websocket" in rendered
    assert "persistent WS" in rendered
    assert "first body text_delta/push send time" in rendered


def test_three_way_gate_requires_cleanup_and_all_latency_thresholds() -> None:
    records = [
        {
            "first_pcm_ms": 500,
            "first_non_silent_pcm_ms": 510,
            "first_pcm_before_final": True,
            "rtf": 0.4,
            "active_rtf": 0.35,
            "chunk_gap_p99_ms": 100,
            "continuous_playback": {"100": {"underrun_count": 0, "max_underrun_ms": 0, "minimum_queue_lead_ms": 100}},
            "close_ack": True,
            "release_confirmed": True,
        },
        {
            "first_pcm_ms": 600,
            "first_non_silent_pcm_ms": 610,
            "first_pcm_before_final": True,
            "rtf": 0.5,
            "active_rtf": 0.45,
            "chunk_gap_p99_ms": 120,
            "continuous_playback": {"100": {"underrun_count": 0, "max_underrun_ms": 0, "minimum_queue_lead_ms": 80}},
            "close_ack": True,
            "release_confirmed": True,
        },
        {
            "first_pcm_ms": 700,
            "first_non_silent_pcm_ms": 710,
            "first_pcm_before_final": True,
            "rtf": 0.6,
            "active_rtf": 0.55,
            "chunk_gap_p99_ms": 150,
            "continuous_playback": {"100": {"underrun_count": 0, "max_underrun_ms": 0, "minimum_queue_lead_ms": 60}},
            "close_ack": True,
            "release_confirmed": True,
        },
    ]
    assert benchmark.summarize(records, 3)["lifecycle_gate_passed"] is True
    assert benchmark.summarize(records, 3)["transport_gate_passed"] is True
    assert benchmark.summarize(records, 3)["continuous_playback_gate_passed"] is True
    records[-1]["close_ack"] = False
    assert benchmark.summarize(records, 3)["transport_gate_passed"] is False
