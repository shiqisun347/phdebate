from __future__ import annotations

import asyncio
import importlib.util
import json
import math
import os
import sys
from array import array
from pathlib import Path

import httpx
import pytest

SCRIPT = Path(__file__).parents[1] / "benchmark_tts_quality_gate.py"
SPEC = importlib.util.spec_from_file_location("benchmark_tts_quality_gate", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_edit_statistics_separates_swallowed_characters() -> None:
    metrics = MODULE.edit_statistics("人工智能时代", "人工智时代")

    assert metrics["deletions"] == 1
    assert metrics["insertions"] == 0
    assert metrics["swallowed_character_rate"] == pytest.approx(1 / 6, abs=1e-6)
    assert metrics["cer"] == pytest.approx(1 / 6, abs=1e-6)


def test_edit_statistics_detects_head_and_tail_swallowing() -> None:
    head = MODULE.edit_statistics("开头中间结尾", "头中间结尾")
    tail = MODULE.edit_statistics("开头中间结尾", "开头中间结")

    assert head["leading_deletions"] == 1
    assert head["leading_text_swallowed"] is True
    assert tail["trailing_deletions"] == 1
    assert tail["trailing_text_swallowed"] is True


def test_edit_statistics_detects_first_and_last_character_substitution() -> None:
    head = MODULE.edit_statistics("开头中间结尾", "改头中间结尾")
    tail = MODULE.edit_statistics("开头中间结尾", "开头中间结束")

    assert head["leading_text_swallowed"] is False
    assert head["first_character_match"] is False
    assert tail["trailing_text_swallowed"] is False
    assert tail["last_character_match"] is False


def test_edit_statistics_records_repeated_or_extra_characters() -> None:
    metrics = MODULE.edit_statistics("人工智能时代", "人工人工智能时代")

    assert metrics["insertions"] == 2
    assert metrics["repeated_or_extra_character_rate"] == pytest.approx(2 / 6, abs=1e-6)


def test_adjacent_repetition_gate_detects_phrase_loops_but_ignores_single_character_forms() -> None:
    repeated = MODULE.adjacent_repetition_statistics("人工智能推动教育", "人工智能人工智能推动教育")
    legitimate = MODULE.adjacent_repetition_statistics("人工智能人工智能推动教育", "人工智能人工智能推动教育")
    single_character = MODULE.adjacent_repetition_statistics("我们常常复核", "我们常常复核")

    assert repeated["adjacent_repetition_count"] == 1
    assert repeated["adjacent_repetition_events"][0]["ngram"] == "人工智能"
    assert legitimate["adjacent_repetition_count"] == 0
    assert single_character["adjacent_repetition_count"] == 0


def test_audio_analysis_detects_boundary_silence_and_network_stall() -> None:
    sample_rate = 16_000
    tone = array("h", [6000] * (sample_rate // 10)).tobytes()
    silence = array("h", [0] * (sample_rate // 20)).tobytes()
    chunks = [tone + silence, silence + tone]
    metrics = MODULE.analyze_audio(
        chunks,
        [0.0, 0.4],
        sample_rate=sample_rate,
        channels=1,
        sample_width=2,
        initial_buffer_ms=100,
    )

    assert metrics["chunk_boundary_silence_ms"]["max"] == pytest.approx(100)
    assert metrics["stutter_count"] == 1
    assert metrics["underrun_ms"] > 0
    assert metrics["network_chunk_gap_ms"]["p99"] > 300


def test_audio_analysis_records_first_non_silent_pcm_separately_from_first_packet() -> None:
    sample_rate = 16_000
    silence = array("h", [0] * (sample_rate // 10)).tobytes()
    tone = array("h", [6000] * (sample_rate // 10)).tobytes()

    metrics = MODULE.analyze_audio(
        [silence, tone],
        [0.0, 0.05],
        sample_rate=sample_rate,
        channels=1,
        sample_width=2,
        initial_buffer_ms=100,
    )

    assert metrics["contains_non_silent_pcm"] is True
    assert metrics["first_non_silent_chunk_index"] == 1
    assert metrics["first_non_silent_stream_offset_ms"] == pytest.approx(100)
    assert metrics["leading_silence_ms"] == 100


def test_twenty_round_drift_proxy_rejects_acoustic_drift() -> None:
    records = []
    for round_index in range(20):
        records.append(
            {
                "voice": "debate_voice_1",
                "cer": 0.0,
                "leading_text_swallowed": False,
                "trailing_text_swallowed": False,
                "duration_per_visible_character_ms": 30.0 if round_index < 10 else 60.0,
                "audio": {
                    "rms_dbfs": -18.0 if round_index < 10 else -10.0,
                    "zero_crossing_rate": 0.02 if round_index < 10 else 0.05,
                    "crest_factor_db": 3.0 if round_index < 10 else 8.0,
                    "timbre": {
                        "spectral_signature": [0.9, 0.1] if round_index < 10 else [0.1, 0.9],
                        "sampled_spectral_centroid_hz": 220 if round_index < 10 else 900,
                        "dominant_low_frequency_hz": 220 if round_index < 10 else 440,
                    },
                },
            }
        )

    summary = MODULE.summarize_voice_drift(records, "debate_voice_1", 20)

    assert summary["rounds_succeeded"] == 20
    assert summary["proxy_passed"] is False
    assert summary["timbre_cosine_distance"]["p95"] > 0.08


def test_twenty_round_timbre_proxy_is_amplitude_invariant_but_rejects_frequency_change() -> None:
    sample_rate = 16_000

    def audio(frequency: int, amplitude: int) -> dict:
        samples = array(
            "h",
            (
                round(amplitude * math.sin(2 * math.pi * frequency * offset / sample_rate))
                for offset in range(sample_rate)
            ),
        )
        return MODULE.analyze_audio(
            [samples.tobytes()],
            [0.0],
            sample_rate=sample_rate,
            channels=1,
            sample_width=2,
            initial_buffer_ms=100,
        )

    stable_records = []
    drift_records = []
    stable_audio = [audio(220, 4000), audio(220, 8000)]
    changed_audio = audio(900, 6000)
    for round_index in range(20):
        common = {
            "voice": "debate_voice_1",
            "cer": 0.0,
            "leading_text_swallowed": False,
            "trailing_text_swallowed": False,
            "duration_per_visible_character_ms": 30.0,
        }
        stable_records.append({**common, "audio": stable_audio[0]})
        drift_records.append({**common, "audio": stable_audio[0] if round_index < 10 else changed_audio})

    assert MODULE.cosine_distance(
        stable_audio[0]["timbre"]["spectral_signature"],
        stable_audio[1]["timbre"]["spectral_signature"],
    ) == pytest.approx(0, abs=1e-6)
    assert MODULE.summarize_voice_drift(stable_records, "debate_voice_1", 20)["proxy_passed"] is True
    assert MODULE.summarize_voice_drift(drift_records, "debate_voice_1", 20)["proxy_passed"] is False


def test_remote_endpoints_require_explicit_opt_in(tmp_path: Path) -> None:
    args = MODULE.parse_args(
        [
            "--tts-mode",
            "openai-pcm",
            "--asr-mode",
            "openai-http",
            "--tts-endpoint",
            "https://tts.example.invalid/v1/audio/speech",
            "--asr-endpoint",
            "https://asr.example.invalid/v1/audio/transcriptions",
            "--output-dir",
            str(tmp_path),
        ]
    )

    with pytest.raises(SystemExit, match="--allow-remote"):
        MODULE.require_endpoint_safety(args)


def test_moss_cli_accepts_multiple_endpoints_in_order(tmp_path: Path) -> None:
    args = MODULE.parse_args(
        [
            "--tts-mode",
            "moss-session",
            "--asr-mode",
            "funasr-ws",
            "--moss-endpoint",
            "http://127.0.0.1:8101",
            "--moss-endpoint",
            "http://127.0.0.1:8102",
            "--asr-endpoint",
            "ws://127.0.0.1:10095",
            "--voice-prompt",
            "debate_voice_1=voice-1.wav",
            "--allow-unversioned-voices",
            "--output-dir",
            str(tmp_path),
        ]
    )

    assert MODULE.configured_tts_endpoints(args) == ["http://127.0.0.1:8101", "http://127.0.0.1:8102"]
    MODULE.require_endpoint_safety(args)


def test_default_concurrency_remains_full_release_gate() -> None:
    args = MODULE.parse_args([])

    assert args.concurrency == [1, 2, 3]
    assert args.single_endpoint_quality_only is False


def test_explicit_concurrency_one_selects_single_endpoint_quality_scope() -> None:
    args = MODULE.parse_args(["--concurrency", "1"])

    assert args.concurrency == [1]
    assert args.single_endpoint_quality_only is True


@pytest.mark.parametrize(
    "values",
    [
        ["--concurrency", "2"],
        ["--concurrency", "3"],
        ["--concurrency", "1", "--concurrency", "2"],
        ["--concurrency", "1", "--concurrency", "4"],
    ],
)
def test_partial_or_invalid_concurrency_does_not_weaken_release_gate(values: list[str]) -> None:
    with pytest.raises(SystemExit, match="single-endpoint quality validation only"):
        MODULE.parse_args(values)


def test_error_sanitization_removes_endpoint_and_secret() -> None:
    error = RuntimeError("POST https://host.invalid/private?token=secret-token Authorization: secret-token")

    rendered = MODULE.sanitize_error(
        error,
        endpoints=["https://host.invalid/private?token=secret-token"],
        secrets=["secret-token"],
    )

    assert "secret-token" not in rendered
    assert "/private" not in rendered
    assert "<redacted>" in rendered


@pytest.mark.asyncio
async def test_fake_selftest_writes_passing_redacted_reports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TTS_QUALITY_TTS_API_KEY", "must-not-appear")
    args = MODULE.parse_args(
        [
            "--output-dir",
            str(tmp_path),
            "--rounds",
            "1",
            "--warmup-requests",
            "0",
            "--cancel-after-first-pcm-ms",
            "0",
            "--drift-rounds",
            "20",
        ]
    )

    exit_code = await MODULE.run(args)

    assert exit_code == 0
    document = json.loads((tmp_path / "tts-quality-gate.json").read_text(encoding="utf-8"))
    markdown = (tmp_path / "tts-quality-gate.md").read_text(encoding="utf-8")
    assert document["gate"]["passed"] is True
    assert document["gate"]["scope"] == "release"
    assert {item["concurrency"] for item in document["scenarios"]} == {1, 2, 3}
    assert document["cancellation"]["cancel_latency_ms"] is not None
    assert document["drift"]["rounds_per_voice"] == 20
    assert document["per_voice"]["selftest"]["mos_auxiliary"]["automatic_proxy_is_not_mos"] is True
    scenarios = {item["concurrency"]: item["summary"] for item in document["scenarios"]}
    assert scenarios[2]["inferred_queue_delay_ms"]["p95"] is not None
    assert scenarios[3]["inferred_queue_delay_ms"]["p95"] is not None
    assert "must-not-appear" not in json.dumps(document)
    assert "must-not-appear" not in markdown
    assert document["endpoints"]["redacted"] is True
    checks = document["gate"]["checks"]
    assert checks["repeated_or_extra_character_rate_p95"]["passed"] is True
    assert checks["unexpected_adjacent_repetition_count"]["passed"] is True
    assert checks["first_char_to_first_pcm_max_ms"]["passed"] is True
    assert checks["first_char_to_first_non_silent_pcm_p95_ms"]["passed"] is True
    assert checks["twenty_round_timbre_cosine_distance_p95_max"]["passed"] is True

    hard_limit_document = json.loads(json.dumps(document))
    hard_limit_document["scenarios"][0]["records"][0]["first_char_to_first_pcm_ms"] = 3000
    assert MODULE.evaluate_gates(hard_limit_document, args)["checks"]["first_char_to_first_pcm_max_ms"]["passed"] is False

    repeated_document = json.loads(json.dumps(document))
    for scenario in repeated_document["scenarios"]:
        for record in scenario["records"]:
            record["repeated_or_extra_character_rate"] = 0.01
            record["adjacent_repetition_count"] = 1
    for record in repeated_document["drift"]["records"]:
        record["repeated_or_extra_character_rate"] = 0.01
        record["adjacent_repetition_count"] = 1
    repeated_checks = MODULE.evaluate_gates(repeated_document, args)["checks"]
    assert repeated_checks["repeated_or_extra_character_rate_p95"]["passed"] is False
    assert repeated_checks["unexpected_adjacent_repetition_count"]["passed"] is False

    boundary_document = json.loads(json.dumps(document))
    boundary_document["scenarios"][0]["records"][0]["last_character_match"] = False
    assert (
        MODULE.evaluate_gates(boundary_document, args)["checks"]["first_or_last_character_mismatch_samples"]["passed"]
        is False
    )

    non_silent_queue_document = json.loads(json.dumps(document))
    non_silent_queue_document["scenarios"][2]["summary"]["inferred_non_silent_queue_delay_ms"]["p95"] = 501
    assert (
        MODULE.evaluate_gates(non_silent_queue_document, args)["checks"]["three_room_non_silent_queue_delay_p95_ms"][
            "passed"
        ]
        is False
    )

    loudness_document = json.loads(json.dumps(document))
    loudness_document["per_voice"]["selftest"]["rms_dbfs_range"] = 3.1
    assert MODULE.evaluate_gates(loudness_document, args)["checks"]["per_voice_rms_dbfs_range_max"]["passed"] is False

    moss_document = json.loads(json.dumps(document))
    moss_document["tts_mode"] = "moss-session"
    moss_checks = MODULE.evaluate_gates(moss_document, args)["checks"]
    assert moss_checks["moss_all_sessions_remotely_released"]["passed"] is True
    assert moss_checks["moss_cancel_remotely_released"]["passed"] is True
    moss_document["cancellation"]["remote_released"] = False
    assert MODULE.evaluate_gates(moss_document, args)["checks"]["moss_cancel_remotely_released"]["passed"] is False


@pytest.mark.asyncio
async def test_explicit_single_endpoint_quality_run_skips_multiroom_release_checks(tmp_path: Path) -> None:
    args = MODULE.parse_args(
        [
            "--output-dir",
            str(tmp_path),
            "--concurrency",
            "1",
            "--rounds",
            "1",
            "--warmup-requests",
            "0",
            "--cancel-after-first-pcm-ms",
            "0",
            "--drift-rounds",
            "20",
        ]
    )

    exit_code = await MODULE.run(args)

    assert exit_code == 0
    document = json.loads((tmp_path / "tts-quality-gate.json").read_text(encoding="utf-8"))
    markdown = (tmp_path / "tts-quality-gate.md").read_text(encoding="utf-8")
    assert {item["concurrency"] for item in document["scenarios"]} == {1}
    assert document["gate"]["scope"] == "single_endpoint_quality"
    assert document["gate"]["passed"] is True
    assert document["gate"]["release_ready"] is False
    assert "two_room_queue_delay_p95_ms" not in document["gate"]["checks"]
    assert "three_room_queue_delay_p95_ms" not in document["gate"]["checks"]
    assert "not a release gate" in markdown


def test_payload_extension_rejects_credential_like_fields(tmp_path: Path) -> None:
    path = tmp_path / "payload.json"
    path.write_text(json.dumps({"api_key": os.urandom(8).hex()}), encoding="utf-8")

    with pytest.raises(SystemExit, match="credential-like"):
        MODULE.load_extra_payload(path)


def test_voice_manifest_requires_fixed_mapping_and_version(tmp_path: Path) -> None:
    manifest = {
        "expected_voice_ids": MODULE.FIXED_VOICE_IDS,
        "seat_mapping": MODULE.FIXED_SEAT_MAPPING,
        "voice_set_version_sha256": "a" * 64,
        "voices": [
            {
                "voice_id": voice,
                "compliant": True,
                "audio": {"sha256": f"{index:x}".rjust(64, "0")},
                "prompt": {"sha256": f"{index + 8:x}".rjust(64, "0")},
            }
            for index, voice in enumerate(MODULE.FIXED_VOICE_IDS)
        ],
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    loaded = MODULE.load_voice_manifest(path)

    assert loaded is not None
    assert loaded["complete_and_compliant"] is True
    assert [item["voice_id"] for item in loaded["voices"]] == MODULE.FIXED_VOICE_IDS


class _MossStream(httpx.AsyncByteStream):
    def __init__(self, completed: asyncio.Event) -> None:
        self.completed = completed

    async def __aiter__(self):
        yield b"\x01\x00" * 8_000
        await self.completed.wait()
        yield b"\x02\x00" * 8_000


@pytest.mark.asyncio
async def test_moss_adapter_shards_endpoints_authenticates_and_confirms_remote_release() -> None:
    active = {"moss-a.test": 0, "moss-b.test": 0}
    completed: dict[str, asyncio.Event] = {}
    start_hosts: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        host = str(request.url.host)
        assert request.headers["X-MOSS-Gateway-Key"] == "gateway-secret"
        if request.url.path == "/health/ready":
            return httpx.Response(200, json={"ok": True, "active": active[host], "orphan_count": 0})
        payload = json.loads(request.content) if request.content else {}
        if request.url.path.endswith("/tts/session/start"):
            session_id = str(payload["session_id"])
            active[host] = 1
            completed[session_id] = asyncio.Event()
            start_hosts.append(host)
            return httpx.Response(200, json={"ok": True})
        if request.url.path.endswith("/audio"):
            session_id = request.url.path.split("/")[-2]
            return httpx.Response(
                200,
                headers={"X-Audio-Codec": "pcm_s16le", "X-Audio-Sample-Rate": "24000"},
                stream=_MossStream(completed[session_id]),
            )
        if request.url.path.endswith("/tts/session/push"):
            if payload.get("is_final"):
                completed[str(payload["session_id"])].set()
            return httpx.Response(200, json={"ok": True})
        if request.url.path.endswith(("/tts/session/close", "/tts/session/abort")):
            active[host] = 0
            return httpx.Response(200, json={"ok": True, "released": True})
        return httpx.Response(404)

    adapter = MODULE.MossSessionAdapter(
        endpoints=["http://moss-a.test", "http://moss-b.test"],
        voice_prompts={"debate_voice_1": "voice-1.wav"},
        chunk_characters=4,
        delta_delay_ms=0,
        api_key="gateway-secret",
        timeout_seconds=2,
        release_timeout_seconds=0.2,
        transport=httpx.MockTransport(handler),
    )
    try:
        results = await asyncio.gather(
            adapter.synthesize("第一场完整内容。", room_id="room-a", voice="debate_voice_1"),
            adapter.synthesize("第二场完整内容。", room_id="room-b", voice="debate_voice_1"),
        )
    finally:
        await adapter.aclose()

    assert set(start_hosts) == {"moss-a.test", "moss-b.test"}
    assert all(result.close_acknowledged is True for result in results)
    assert all(result.remote_released is True for result in results)
    assert all(result.release_active == 0 and result.release_orphan_count == 0 for result in results)


@pytest.mark.asyncio
async def test_moss_cancel_waits_for_released_ack_and_idle_readiness() -> None:
    active = 0
    close_seen = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active
        if request.url.path == "/health/ready":
            return httpx.Response(200, json={"ok": True, "active": active, "orphan_count": 0})
        if request.url.path.endswith("/tts/session/start"):
            active = 1
            return httpx.Response(200, json={"ok": True})
        if request.url.path.endswith("/audio"):
            return httpx.Response(
                200,
                headers={"X-Audio-Codec": "pcm_s16le", "X-Audio-Sample-Rate": "24000"},
                stream=_MossStream(close_seen),
            )
        if request.url.path.endswith("/tts/session/push"):
            return httpx.Response(200, json={"ok": True})
        if request.url.path.endswith(("/tts/session/close", "/tts/session/abort")):
            active = 0
            close_seen.set()
            return httpx.Response(200, json={"ok": True, "released": True})
        return httpx.Response(404)

    adapter = MODULE.MossSessionAdapter(
        endpoints=["http://moss.test"],
        voice_prompts={"debate_voice_1": "voice-1.wav"},
        chunk_characters=20,
        delta_delay_ms=0,
        api_key=None,
        timeout_seconds=2,
        release_timeout_seconds=0.2,
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await adapter.synthesize(
            "取消测试必须等待远端资源释放。",
            room_id="cancel-room",
            voice="debate_voice_1",
            cancel_after_ms=0,
        )
    finally:
        await adapter.aclose()

    assert result.canceled is True
    assert result.close_acknowledged is True
    assert result.remote_released is True
    assert result.release_active == 0 and result.release_orphan_count == 0
    assert result.cancel_latency_ms is not None


@pytest.mark.asyncio
async def test_moss_adapter_rejects_close_without_released_true() -> None:
    active = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active
        if request.url.path == "/health/ready":
            return httpx.Response(200, json={"ok": True, "active": active, "orphan_count": 0})
        if request.url.path.endswith("/tts/session/start"):
            active = 1
            return httpx.Response(200, json={"ok": True})
        if request.url.path.endswith("/audio"):
            return httpx.Response(
                200,
                headers={"X-Audio-Codec": "pcm_s16le", "X-Audio-Sample-Rate": "24000"},
                stream=_MossStream(asyncio.Event()),
            )
        if request.url.path.endswith("/tts/session/push"):
            return httpx.Response(200, json={"ok": True})
        if request.url.path.endswith(("/tts/session/close", "/tts/session/abort")):
            return httpx.Response(200, json={"ok": True, "released": False})
        return httpx.Response(404)

    adapter = MODULE.MossSessionAdapter(
        endpoints=["http://moss.test"],
        voice_prompts={"debate_voice_1": "voice-1.wav"},
        chunk_characters=20,
        delta_delay_ms=0,
        api_key=None,
        timeout_seconds=0.05,
        release_timeout_seconds=0.1,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(RuntimeError, match="released:true"):
            await adapter.synthesize("释放确认必须严格。", room_id="bad-close", voice="debate_voice_1")
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_moss_adapter_rejects_release_that_never_returns_to_idle() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health/ready":
            return httpx.Response(200, json={"ok": True, "active": 1, "orphan_count": 0})
        if request.url.path.endswith(("/tts/session/close", "/tts/session/abort")):
            return httpx.Response(200, json={"ok": True, "released": True})
        return httpx.Response(404)

    adapter = MODULE.MossSessionAdapter(
        endpoints=["http://moss.test"],
        voice_prompts={"debate_voice_1": "voice-1.wav"},
        chunk_characters=20,
        delta_delay_ms=0,
        api_key=None,
        timeout_seconds=1,
        release_timeout_seconds=0.1,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(RuntimeError, match="active=0"):
            await adapter._release_and_verify("http://moss.test", "stuck-session")
    finally:
        await adapter.aclose()
