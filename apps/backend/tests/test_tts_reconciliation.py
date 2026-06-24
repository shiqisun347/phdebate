"""TTS 可靠性不变量测试。

核心保证：一段 AI 发言合成结束后，[0, expected) 中每个分段序号都必然 ready 或 skipped
（expected == |ready| + |skipped| 且不相交）——这样大屏的纯函数对账仅凭快照就能推进到
结束、绝不会等一个永远不来的分段而永久卡死。另外覆盖 live 流式默认关、手动救援控制。
"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.main import app
from app.services import match_store as match_store_module
from app.services.match_store import store
from app.services.tts_live import tts_live_manager
from app.services.xfyun_gateway import TTSResult, XfyunGatewayError

from fastapi.testclient import TestClient

client = TestClient(app)


@pytest.fixture(autouse=True)
def reset_demo_state(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("PHDEBATE_RUNTIME_AUTH_FILE", str(tmp_path / "runtime_auth.json"))
    monkeypatch.setenv("PHDEBATE_AUDIO_DIR", str(tmp_path / "audio"))
    from app.services.integration_config import integration_config

    integration_config.config = integration_config._seed_from_env()
    integration_config._normalize()
    integration_config._apply_to_env()
    client.post("/api/demo/reset")


def _patch_tts(monkeypatch, gateway, provider: str = "test") -> None:
    monkeypatch.setattr(
        "app.services.match_store.select_tts_gateway",
        lambda **_kwargs: SimpleNamespace(gateway=gateway, provider=provider, options={}, preset=None),
    )


def _set_tts_settings(**settings) -> None:
    from app.services.integration_config import integration_config

    integration_config.config.setdefault("tts", {}).setdefault("settings", {}).update(settings)


def _use_embedded_mock_agent(speaker_id: str = "spk_aff_2") -> None:
    resp = client.patch(
        f"/api/matches/match_001/agents/configs/agent_{speaker_id}",
        json={"provider_type": "rest_api", "endpoint": ""},
    )
    assert resp.status_code == 200, resp.text


def _seed_running_speech(speech_id: str = "sp_test", speaker_id: str = "spk_aff_2", **extra) -> dict:
    """Put a minimal in-progress AI speech into the live snapshot for direct method tests."""
    store.snapshot["match"]["status"] = "running"
    speech = {
        "id": speech_id,
        "speaker_id": speaker_id,
        "side": "affirmative",
        "source": "agent_text",
        "state": "speaking",
        "tts_task_id": "task_seed",
        "tts_skipped_sentences": [],
        "content_final": "",
        "content_partial": "",
    }
    speech.update(extra)
    store.snapshot["current_speech"] = speech
    return speech


def test_stable_tts_segments_do_not_create_tiny_first_chunk(monkeypatch) -> None:
    _set_tts_settings(stability_mode="stable", min_segment_chars=80, max_segment_chars=120)
    text = (
        "第一，我们讨论人工智能时代的学习方式，不能只看工具替代了多少操作，"
        "更要看人是否还能理解问题如何被拆解、验证和复盘。"
        "第二，编程思维不是要求每个人都成为工程师，而是训练一种稳定、清晰、可执行的表达方式。"
        "第三，提问当然重要，但如果没有结构化执行，问题很容易停留在口号层面。"
        "第四，现场辩论的语音应该保持平直克制，不应该突然出现奇怪的拖腔、口音和夸张语气。"
    )

    segments = store._stable_tts_segments(text)

    assert len(segments) >= 2
    assert len(segments[0]) >= 80
    assert all(len(segment) >= 40 for segment in segments[:-1])


def test_stable_tts_text_synthesis_runs_segments_sequentially(monkeypatch) -> None:
    _set_tts_settings(stability_mode="stable", min_segment_chars=40, max_segment_chars=55)
    _seed_running_speech()
    speaker = store._find_speaker("spk_aff_2")
    order: list[tuple[str, int]] = []

    monkeypatch.setattr(
        store,
        "_select_tts_for_speech",
        lambda *_args, **_kwargs: SimpleNamespace(gateway=object(), provider="local_qwen", options={}, preset=None),
    )

    async def fake_synthesize(text, sentence_idx, task_id, speech_id, speaker, selection=None):
        order.append(("start", sentence_idx))
        await asyncio.sleep(0)
        order.append(("end", sentence_idx))
        return True

    monkeypatch.setattr(store, "_synthesize_sentence_tts_with_timeout", fake_synthesize)
    text = (
        "第一，稳定模式会等待较大的自然语义块，避免八个字就切成一段。"
        "第二，它会顺序请求本机语音模型，减少同一次发言里的音色漂移。"
        "第三，合成完成后再按顺序播放，虽然首音略慢，但整体听感更正常。"
    )

    expected = asyncio.run(store._synthesize_text_tts("sp_test", "task_seed", speaker, text))

    assert expected >= 2
    assert order == [item for idx in range(expected) for item in (("start", idx), ("end", idx))]


def test_tts_loudness_normalization_degrades_to_original_when_ffmpeg_missing(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("PHDEBATE_TTS_LOUDNESS_NORMALIZE", "1")
    monkeypatch.setenv("PHDEBATE_FFMPEG_BIN", str(tmp_path / "missing-ffmpeg"))
    original = b"fake-mp3-bytes"

    audio, meta = asyncio.run(store._normalize_tts_audio_bytes(original, "audio/mpeg", tmp_path, "task_seed", 0))

    assert audio == original
    assert meta["status"] == "missing_ffmpeg"


# --- 完成不变量 ----------------------------------------------------------------

_MULTI_SENTENCE = (
    "我方认为编程思维比提问思维在现代社会中更加重要而且不可替代。"
    "因为结构化的拆解能力是解决复杂问题的根本前提条件。"
    "同时它还能显著提升团队协作与沟通的整体效率水平。"
    "综上所述我方坚定地认为编程思维更重要。"
)


class _FlakyGateway:
    """偶数次调用成功、奇数次失败，制造 ready / skipped 混合的缺口。"""

    def __init__(self) -> None:
        self.calls = 0

    async def synthesize(self, text: str, **_opts) -> TTSResult:
        i = self.calls
        self.calls += 1
        if i % 2 == 1:
            raise XfyunGatewayError("flaky tts", code=500)
        return TTSResult(audio=b"mp3", mime_type="audio/mpeg", latency_ms=1, chunk_count=1)


class _FirstOkThenFailGateway:
    """首段成功、后续失败，用于稳定覆盖尾部分段 skipped 的完成路径。"""

    def __init__(self) -> None:
        self.calls = 0

    async def synthesize(self, text: str, **_opts) -> TTSResult:
        self.calls += 1
        if self.calls > 1:
            raise XfyunGatewayError("tail tts failed", code=500)
        return TTSResult(audio=b"mp3", mime_type="audio/mpeg", latency_ms=1, chunk_count=1)


class _AlwaysOkGateway:
    """每句都成功合成——用于"所有句子都有音频"的场景。"""

    async def synthesize(self, text: str, **_opts) -> TTSResult:
        return TTSResult(audio=b"mp3-bytes", mime_type="audio/mpeg", latency_ms=1, chunk_count=1)


class _BoomGateway:
    """synthesize 抛出非 SpeechProviderError 的意外异常（复刻线上那次 AttributeError 事故）。"""

    async def synthesize(self, text: str, **_opts) -> TTSResult:
        raise RuntimeError("unexpected boom")


def test_unexpected_synthesize_error_skips_sentence_not_stall(monkeypatch) -> None:
    """根因加固（线上事故复刻）：合成抛出非 SpeechProviderError 的意外异常（如曾经调用未定义方法的
    AttributeError）时，每个分段也必须降级为「跳句」事件，绝不能让任务静默死亡、让大屏空等看门狗
    12s。否则任意一个 bug 就会让整段 AI 发言全程没声音。"""
    _set_tts_settings(stability_mode="legacy")
    monkeypatch.setenv("PHDEBATE_TTS_SENTENCE_CONCURRENCY", "1")
    monkeypatch.setenv("PHDEBATE_TTS_RETRY_ATTEMPTS", "0")
    _patch_tts(monkeypatch, _BoomGateway(), provider="xfyun")
    speech = _seed_running_speech(content_final=_MULTI_SENTENCE)

    snap = asyncio.run(store.resynthesize_speech_tts(speech["id"]))

    cs = snap["current_speech"]
    expected = int(cs["tts_expected_sentences"])
    assert expected >= 4
    ready = store._tts_ready_indices(speech["id"])
    skipped = set(int(i) for i in cs["tts_skipped_sentences"])
    assert ready | skipped == set(range(expected))   # 全覆盖，无空洞（大屏据此可推进到结束）
    assert skipped == set(range(expected))            # 全部失败 → 全部跳过，而非卡住
    assert not ready


class _RetryThenOkStreamGateway:
    """流式合成：前 fail_times 次在首块之前抛可重试错误(429)，之后正常出块。"""

    def __init__(self, fail_times: int) -> None:
        self.calls = 0
        self.fail_times = fail_times

    async def synthesize_stream(self, text: str, **_opts):
        i = self.calls
        self.calls += 1
        if i < self.fail_times:
            raise XfyunGatewayError("rate limited", code=429)
        yield {"type": "chunk", "audio": b"mp3", "index": 1}
        yield {"type": "done", "mime_type": "audio/mpeg", "latency_ms": 1, "chunk_count": 1}


class _MidStreamFailGateway:
    """先成功推出一块音频，再抛错——此时重试会让大屏收到重复音频，故必须不重试。"""

    def __init__(self) -> None:
        self.calls = 0

    async def synthesize_stream(self, text: str, **_opts):
        self.calls += 1
        yield {"type": "chunk", "audio": b"mp3", "index": 1}
        raise XfyunGatewayError("stream dropped", code=500)


def test_live_stream_retries_before_first_chunk(monkeypatch) -> None:
    """live 流式 TTS：首块之前遇到 429 等瞬时错误应退避重试并最终成功出声（而非每句直接跳过）。"""
    monkeypatch.setenv("PHDEBATE_TTS_RETRY_ATTEMPTS", "2")
    monkeypatch.setenv("PHDEBATE_TTS_MIN_REQUEST_INTERVAL_MS", "0")
    monkeypatch.setenv("PHDEBATE_TTS_RETRY_BASE_MS", "0")
    gateway = _RetryThenOkStreamGateway(fail_times=1)
    selection = SimpleNamespace(gateway=gateway, provider="alicloud", options={}, preset=None)
    live_key = ("match_001", "sp_test", "task_seed", 0)

    async def scenario():
        await tts_live_manager.start(live_key, {"mime_type": "audio/mpeg"})
        try:
            return await store._stream_tts_live_with_retry(selection, "你好世界", live_key, "audio/mpeg")
        finally:
            await tts_live_manager.fail(live_key, "cleanup")

    result = asyncio.run(scenario())
    assert result.chunk_count == 1            # 成功出块
    assert gateway.calls == 2                 # 第 1 次失败 + 第 2 次成功


def test_live_stream_does_not_retry_after_publishing(monkeypatch) -> None:
    """live 流式 TTS：已推出音频块后再断流，绝不能重试（否则大屏收到重复音频），直接抛错交由上层跳句。"""
    monkeypatch.setenv("PHDEBATE_TTS_RETRY_ATTEMPTS", "2")
    monkeypatch.setenv("PHDEBATE_TTS_MIN_REQUEST_INTERVAL_MS", "0")
    gateway = _MidStreamFailGateway()
    selection = SimpleNamespace(gateway=gateway, provider="alicloud", options={}, preset=None)
    live_key = ("match_001", "sp_test", "task_seed", 1)

    async def scenario():
        await tts_live_manager.start(live_key, {"mime_type": "audio/mpeg"})
        try:
            await store._stream_tts_live_with_retry(selection, "你好世界", live_key, "audio/mpeg")
        finally:
            await tts_live_manager.fail(live_key, "cleanup")

    with pytest.raises(XfyunGatewayError):
        asyncio.run(scenario())
    assert gateway.calls == 1                 # 中途失败不重试


class _StaticAgentGateway:
    def endpoint_for(self, speaker) -> str:
        return "embedded://static"

    async def stream_speech(self, endpoint, payload, fallback_chunks, *, config=None):
        yield {"type": "delta", "task_id": payload["task_id"], "delta": _MULTI_SENTENCE}
        yield {"type": "final", "task_id": payload["task_id"], "content": _MULTI_SENTENCE}


def test_completion_invariant_via_run_agent_speech(monkeypatch) -> None:
    """真实流式管线：合成结束后 expected == |ready| + |skipped| 必成立（不论几句、是否失败）。"""
    monkeypatch.setenv("PHDEBATE_TTS_FORMAL", "1")
    _patch_tts(monkeypatch, _FlakyGateway(), provider="xfyun")
    _use_embedded_mock_agent("spk_aff_2")

    asyncio.run(store.run_agent_speech("spk_aff_2"))

    cs = client.get("/api/matches/match_001").json()["data"]["current_speech"]
    assert cs is not None, "部分成功不应清空发言"
    expected = int(cs["tts_expected_sentences"])
    assert expected >= 1
    ready = store._tts_ready_indices(cs["id"])
    skipped = set(int(i) for i in cs["tts_skipped_sentences"])
    assert ready | skipped == set(range(expected))
    assert ready.isdisjoint(skipped)


def test_tail_skipped_tts_auto_completes_and_next_agent_can_start(monkeypatch) -> None:
    """端到端流式路径：尾部分段 skipped、最终 complete 丢失时，最后一个 played 也能触发收尾并允许下一轮。"""

    class StaticAgentGateway:
        def endpoint_for(self, speaker) -> str:
            return "embedded://static"

        async def stream_speech(self, endpoint, payload, fallback_chunks, *, config=None):
            yield {"type": "delta", "task_id": payload["task_id"], "delta": _MULTI_SENTENCE}
            yield {"type": "final", "task_id": payload["task_id"], "content": _MULTI_SENTENCE}

    monkeypatch.setenv("PHDEBATE_TTS_FORMAL", "1")
    _set_tts_settings(stability_mode="legacy")
    monkeypatch.setenv("PHDEBATE_TTS_SENTENCE_CONCURRENCY", "1")
    monkeypatch.setenv("PHDEBATE_TTS_RETRY_ATTEMPTS", "0")  # 本测试要覆盖尾部分段失败→跳句兜底，关掉重试以免把瞬时失败救活
    _set_tts_settings(stability_mode="legacy", tts_speaking_cps=8)
    monkeypatch.setenv("PHDEBATE_FREE_DEBATE_DECISION_SECONDS", "99")
    _patch_tts(monkeypatch, _FirstOkThenFailGateway(), provider="xfyun")
    _use_embedded_mock_agent("spk_aff_2")
    _use_embedded_mock_agent("spk_neg_1")
    original_gateway = store.agent_gateway
    store.agent_gateway = StaticAgentGateway()
    try:
        asyncio.run(store.run_agent_speech("spk_aff_2"))
        speech = store.snapshot["current_speech"]
        assert speech is not None
        expected = int(speech["tts_expected_sentences"])
        assert expected >= 2
        ready = sorted(store._tts_ready_indices(speech["id"]))
        skipped = set(int(i) for i in speech["tts_skipped_sentences"])
        assert ready
        assert max(skipped) == expected - 1, "本测试需要尾部分段合成失败，覆盖 complete 丢失兜底"

        for idx in ready:
            asyncio.run(store.record_tts_playback_progress(speech["id"], speech["tts_task_id"], idx, "played"))

        assert store.snapshot["current_speech"] is None
        assert store.snapshot["free_debate"]["current_turn_side"] == "negative"
        assert store.snapshot["flow"]["awaiting_host_confirm"] is False
        assert store.events[-1]["type"] == "speech.ended"

        _patch_tts(monkeypatch, _AlwaysOkGateway(), provider="xfyun")
        asyncio.run(store.run_agent_speech("spk_neg_1"))
        next_speech = store.snapshot["current_speech"]
        assert next_speech is not None
        assert next_speech["speaker_id"] == "spk_neg_1"
    finally:
        store.agent_gateway = original_gateway


def test_screen_error_on_audio_backed_sentence_never_skips_or_completes(monkeypatch) -> None:
    """根因加固（线上事故）：一块屏幕（如旧缓存 bundle）对「有音频」的句子连环上报 error/stalled，
    绝不能把这些句子标记为跳过、更不能据此提前收尾整段发言——否则任意异常屏幕都会把发言
    "播一句就结束"。正常屏幕逐句 played 仍能正确收尾。"""
    monkeypatch.setenv("PHDEBATE_TTS_FORMAL", "1")
    _set_tts_settings(stability_mode="legacy", tts_speaking_cps=8)
    monkeypatch.setenv("PHDEBATE_TTS_SENTENCE_CONCURRENCY", "1")
    monkeypatch.setenv("PHDEBATE_FREE_DEBATE_DECISION_SECONDS", "99")
    _patch_tts(monkeypatch, _AlwaysOkGateway(), provider="xfyun")
    _use_embedded_mock_agent("spk_aff_2")
    _use_embedded_mock_agent("spk_neg_1")
    original_gateway = store.agent_gateway
    store.agent_gateway = _StaticAgentGateway()
    try:
        asyncio.run(store.run_agent_speech("spk_aff_2"))
        speech = store.snapshot["current_speech"]
        assert speech is not None
        expected = int(speech["tts_expected_sentences"])
        assert expected >= 2
        ready = sorted(store._tts_ready_indices(speech["id"]))
        assert ready == list(range(expected)), "本测试要求所有句子都有音频"
        sid, task_id = speech["id"], speech["tts_task_id"]

        # 异常屏幕对中间所有「有音频」的句子连环上报 error。
        for idx in range(1, expected):
            asyncio.run(store.record_tts_playback_progress(sid, task_id, idx, "error"))
        assert store.snapshot["current_speech"] is not None, "屏幕报错绝不能结束发言"
        assert store.snapshot["current_speech"]["tts_skipped_sentences"] == [], "有音频的句子不应被屏幕报错拉黑"

        # 正常屏幕逐句播完，最后一段 played 正确触发收尾。
        for idx in range(expected):
            asyncio.run(store.record_tts_playback_progress(sid, task_id, idx, "played"))
        assert store.snapshot["current_speech"] is None, "正常播完应正确收尾"
        assert store.events[-1]["type"] == "speech.ended"
    finally:
        store.agent_gateway = original_gateway


def test_first_segment_gets_lead_silence_prepended(monkeypatch) -> None:
    """首句前置静音：idx0 的归档音频 = 静音 + 合成字节；其余句子保持原样不变。
    根治"开场前几个字被吃掉"（补偿音频输出启动延迟），且不影响后续句子的连贯与字节。"""
    from pathlib import Path as _P

    monkeypatch.setenv("PHDEBATE_TTS_FORMAL", "1")
    _set_tts_settings(stability_mode="legacy", tts_speaking_cps=8)
    monkeypatch.setenv("PHDEBATE_TTS_SENTENCE_CONCURRENCY", "1")
    monkeypatch.setenv("PHDEBATE_TTS_LEAD_SILENCE", "1")
    _patch_tts(monkeypatch, _AlwaysOkGateway(), provider="xfyun")
    _use_embedded_mock_agent("spk_aff_2")
    original_gateway = store.agent_gateway
    store.agent_gateway = _StaticAgentGateway()
    try:
        asyncio.run(store.run_agent_speech("spk_aff_2"))
        speech = store.snapshot["current_speech"]
        assert speech is not None
        silence = (_P(match_store_module.__file__).resolve().parent.parent / "assets" / "silence-lead.mp3").read_bytes()
        assert len(silence) > 0
        asset = next(a for a in store.snapshot["audio_assets"] if a.get("speech_id") == speech["id"])
        by_idx = {int(c["chunk_index"]): c for c in asset["chunks"]}
        assert 0 in by_idx and len(by_idx) >= 2
        idx0_bytes = _P(by_idx[0]["file_path"]).read_bytes()
        assert idx0_bytes == silence + b"mp3-bytes", "首句应被前置静音"
        for i, c in by_idx.items():
            if i == 0:
                continue
            assert _P(c["file_path"]).read_bytes() == b"mp3-bytes", "非首句不应被改动"
    finally:
        store.agent_gateway = original_gateway


def test_completion_invariant_with_gaps(monkeypatch) -> None:
    """多句 + 半数失败：必然出现 skipped 缺口，且不变量仍成立（大屏据此可推进到结束）。"""
    _set_tts_settings(stability_mode="legacy")
    monkeypatch.setenv("PHDEBATE_TTS_SENTENCE_CONCURRENCY", "1")  # 顺序，便于断言确定的缺口
    monkeypatch.setenv("PHDEBATE_TTS_RETRY_ATTEMPTS", "0")  # 本测试要验证失败→跳句缺口，关掉重试以免把瞬时失败救活
    _patch_tts(monkeypatch, _FlakyGateway(), provider="xfyun")
    speech = _seed_running_speech(content_final=_MULTI_SENTENCE)

    snap = asyncio.run(store.resynthesize_speech_tts(speech["id"]))

    cs = snap["current_speech"]
    expected = int(cs["tts_expected_sentences"])
    assert expected >= 4
    ready = store._tts_ready_indices(speech["id"])
    skipped = set(int(i) for i in cs["tts_skipped_sentences"])
    assert ready | skipped == set(range(expected))   # 全覆盖
    assert ready.isdisjoint(skipped)                 # 互斥
    assert skipped, "半数失败应产生跳过的缺口"
    assert ready, "半数成功应产生可播分段"


def test_reconcile_fills_gap_for_unsynthesized_index() -> None:
    speech = _seed_running_speech(tts_skipped_sentences=[2])
    store.snapshot["audio_assets"] = [
        {
            "speech_id": speech["id"],
            "chunks": [
                {"chunk_index": 0, "audio_url": "/api/audio/x0.mp3"},
                {"chunk_index": 3, "audio_url": "/api/audio/x3.mp3"},
            ],
        }
    ]
    store._reconcile_tts_gaps(speech, 5)
    skipped = set(speech["tts_skipped_sentences"])
    ready = {0, 3}
    assert skipped == {1, 2, 4}  # 已有的 2 保留，缺口 1、4 补齐
    assert ready | skipped == {0, 1, 2, 3, 4}
    assert ready.isdisjoint(skipped)


def test_reconcile_noop_when_fully_covered() -> None:
    speech = _seed_running_speech(tts_skipped_sentences=[1])
    store.snapshot["audio_assets"] = [
        {"speech_id": speech["id"], "chunks": [{"chunk_index": 0, "audio_url": "/a"}, {"chunk_index": 2, "audio_url": "/c"}]}
    ]
    store._reconcile_tts_gaps(speech, 3)
    assert sorted(speech["tts_skipped_sentences"]) == [1]


def test_reconcile_zero_expected_does_nothing() -> None:
    speech = _seed_running_speech(tts_skipped_sentences=[])
    store._reconcile_tts_gaps(speech, 0)
    assert speech["tts_skipped_sentences"] == []


def test_tts_unresolved_queue_counts_ready_and_skipped_as_done() -> None:
    speech = _seed_running_speech(
        tts_expected_sentences=4,
        tts_created_sentences=4,
        tts_played_sentences=0,
        tts_skipped_sentences=[1],
    )
    store.snapshot["audio_assets"] = [
        {
            "speech_id": speech["id"],
            "chunks": [
                {"chunk_index": 0, "audio_url": "/api/audio/q0.mp3"},
                {"chunk_index": 3, "audio_url": "/api/audio/q3.mp3"},
            ],
        }
    ]

    assert store._tts_unresolved_sentence_count(speech) == 1
    assert store._tts_remaining_playback_count(speech) == 3
    speech["tts_played_sentences"] = 2
    assert store._tts_remaining_playback_count(speech) == 2


def test_tts_runtime_refresh_recomputes_stale_queue_size() -> None:
    speech = _seed_running_speech(
        tts_expected_sentences=3,
        tts_created_sentences=3,
        tts_ready_sentences=3,
        tts_played_sentences=0,
        tts_skipped_sentences=[1],
    )
    speech["state"] = "thinking"
    store.snapshot["audio_assets"] = [
        {
            "speech_id": speech["id"],
            "chunks": [
                {"chunk_index": 0, "audio_url": "/api/audio/r0.mp3"},
                {"chunk_index": 2, "audio_url": "/api/audio/r2.mp3"},
            ],
        }
    ]
    store.snapshot["speech_service"]["tts"].update(
        {"status": "playing", "speaker_id": speech["speaker_id"], "queue_size": 99, "detail": "stale"}
    )

    store._refresh_tts_runtime_status()

    assert store.snapshot["speech_service"]["tts"]["status"] == "synthesizing"
    assert store.snapshot["speech_service"]["tts"]["queue_size"] == 0


@pytest.mark.parametrize(
    ("played", "skipped", "status", "remaining", "auto_complete"),
    [
        (0, [], "played", 4, False),
        (1, [1], "played", 2, False),
        (1, [1, 2, 3], "skipped", 0, True),
        (2, [2, 3], "stalled", 0, True),
        (3, [3], "error", 0, True),
        (4, [], "played", 0, True),
        (4, [], "playing", 0, False),
        (2, [3], "played", 1, False),
    ],
)
def test_tts_remaining_playback_matrix_for_auto_complete(played, skipped, status, remaining, auto_complete) -> None:
    speech = _seed_running_speech(
        tts_expected_sentences=4,
        tts_created_sentences=4,
        tts_played_sentences=played,
        tts_skipped_sentences=skipped,
    )

    assert store._tts_remaining_playback_count(speech) == remaining
    assert store._should_auto_complete_tts_playback(speech, status) is auto_complete


def test_tts_remaining_prefers_exact_played_indices_over_legacy_high_water() -> None:
    speech = _seed_running_speech(
        tts_expected_sentences=4,
        tts_created_sentences=4,
        tts_played_sentences=4,
        tts_played_sentence_indices=[3],
        tts_skipped_sentences=[],
    )

    assert store._tts_remaining_playback_count(speech) == 3
    assert store._should_auto_complete_tts_playback(speech, "played") is False


def test_legacy_playing_high_water_does_not_count_active_segment_as_played() -> None:
    speech = _seed_running_speech(
        tts_expected_sentences=4,
        tts_created_sentences=4,
        tts_played_sentences=4,
        tts_playing_sentence_idx=3,
        tts_last_playback_status="playing",
        tts_skipped_sentences=[],
    )

    assert store._tts_played_indices(speech) == {0, 1, 2}
    assert store._tts_remaining_playback_count(speech) == 1


def test_grace_task_completes_immediately_when_playback_already_exhausted(monkeypatch) -> None:
    speech = _seed_running_speech(
        content_final="播放状态已经耗尽但 complete 事件丢失。",
        content_partial="播放状态已经耗尽但 complete 事件丢失。",
        tts_expected_sentences=3,
        tts_created_sentences=3,
        tts_played_sentences=3,
        tts_played_sentence_indices=[0, 1, 2],
        tts_skipped_sentences=[],
    )

    async def fail_if_sleep(_seconds: float) -> None:
        raise AssertionError("exhausted playback should complete before grace sleep")

    monkeypatch.setattr(asyncio, "sleep", fail_if_sleep)
    asyncio.run(store._complete_agent_playback_after_grace(speech["id"], speech["tts_task_id"], 3))

    assert store.snapshot["current_speech"] is None
    assert store.snapshot["speech_service"]["tts"]["status"] == "idle"
    assert store.events[-1]["type"] == "speech.ended"


def test_grace_task_does_not_timeout_immediately_before_any_playback_progress(monkeypatch) -> None:
    speech = _seed_running_speech(
        content_final="TTS 已生成但大屏还没开始播放。",
        content_partial="TTS 已生成但大屏还没开始播放。",
        state="thinking",
        started_at=None,
        tts_expected_sentences=3,
        tts_created_sentences=3,
        tts_played_sentences=0,
        tts_played_sentence_indices=[],
        tts_skipped_sentences=[],
    )

    class GraceWouldSleep(Exception):
        pass

    async def stop_at_first_sleep(_seconds: float) -> None:
        raise GraceWouldSleep()

    monkeypatch.setattr(asyncio, "sleep", stop_at_first_sleep)
    with pytest.raises(GraceWouldSleep):
        asyncio.run(store._complete_agent_playback_after_grace(speech["id"], speech["tts_task_id"], 3))

    assert store.snapshot["current_speech"]["id"] == speech["id"]
    assert not any(event["type"] == "speech.ended" for event in store.events)


def test_grace_task_times_out_quickly_when_screen_never_reports_playback(monkeypatch) -> None:
    speech = _seed_running_speech(
        content_final="TTS 已生成但大屏完全没有播放回报。",
        content_partial="TTS 已生成但大屏完全没有播放回报。",
        state="thinking",
        started_at=None,
        tts_expected_sentences=2,
        tts_created_sentences=2,
        tts_played_sentences=0,
        tts_played_sentence_indices=[],
        tts_skipped_sentences=[],
    )
    base = datetime(2026, 6, 21, 9, 0, tzinfo=timezone.utc)
    calls = {"n": 0}

    def fake_utc_now():
        calls["n"] += 1
        return base if calls["n"] == 1 else base + timedelta(seconds=11)

    monkeypatch.setenv("PHDEBATE_TTS_PLAYBACK_START_TIMEOUT_S", "10")
    monkeypatch.setattr(match_store_module, "utc_now", fake_utc_now)

    asyncio.run(store._complete_agent_playback_after_grace(speech["id"], speech["tts_task_id"], 2))

    assert store.snapshot["current_speech"] is None
    assert store.snapshot["speech_service"]["tts"]["status"] == "idle"
    assert store.events[-1]["type"] == "speech.ended"


def test_grace_task_waits_when_recent_playback_heartbeat_exists(monkeypatch) -> None:
    base = datetime(2026, 6, 21, 9, 0, tzinfo=timezone.utc)
    speech = _seed_running_speech(
        content_final="大屏仍在播放并持续 heartbeat。",
        content_partial="大屏仍在播放并持续 heartbeat。",
        state="speaking",
        started_at=(base - timedelta(seconds=30)).isoformat().replace("+00:00", "Z"),
        tts_expected_sentences=4,
        tts_created_sentences=4,
        tts_played_sentences=1,
        tts_played_sentence_indices=[0],
        tts_skipped_sentences=[],
        tts_last_playback_status="playing",
        tts_last_progress_at=base.isoformat().replace("+00:00", "Z"),
    )

    class GraceWouldSleep(Exception):
        pass

    async def stop_at_first_sleep(_seconds: float) -> None:
        raise GraceWouldSleep()

    monkeypatch.setenv("PHDEBATE_TTS_PLAYBACK_IDLE_TIMEOUT_S", "30")
    monkeypatch.setattr(match_store_module, "utc_now", lambda: base + timedelta(seconds=20))
    monkeypatch.setattr(asyncio, "sleep", stop_at_first_sleep)

    with pytest.raises(GraceWouldSleep):
        asyncio.run(store._complete_agent_playback_after_grace(speech["id"], speech["tts_task_id"], 4))

    assert store.snapshot["current_speech"]["id"] == speech["id"]
    assert not any(event["type"] == "speech.ended" for event in store.events)


def test_grace_task_times_out_after_playback_heartbeat_goes_idle(monkeypatch) -> None:
    base = datetime(2026, 6, 21, 9, 0, tzinfo=timezone.utc)
    speech = _seed_running_speech(
        content_final="大屏播放 heartbeat 中断后应自动收尾。",
        content_partial="大屏播放 heartbeat 中断后应自动收尾。",
        state="speaking",
        started_at=(base - timedelta(seconds=60)).isoformat().replace("+00:00", "Z"),
        tts_expected_sentences=4,
        tts_created_sentences=4,
        tts_played_sentences=1,
        tts_played_sentence_indices=[0],
        tts_skipped_sentences=[],
        tts_last_playback_status="playing",
        tts_last_progress_at=base.isoformat().replace("+00:00", "Z"),
    )

    monkeypatch.setenv("PHDEBATE_TTS_PLAYBACK_IDLE_TIMEOUT_S", "30")
    monkeypatch.setattr(match_store_module, "utc_now", lambda: base + timedelta(seconds=31))

    asyncio.run(store._complete_agent_playback_after_grace(speech["id"], speech["tts_task_id"], 4))

    assert store.snapshot["current_speech"] is None
    assert store.snapshot["speech_service"]["tts"]["status"] == "idle"
    assert store.events[-1]["type"] == "speech.ended"


def test_resume_runtime_tasks_rearms_tts_grace_after_restart(monkeypatch) -> None:
    speech = _seed_running_speech(
        content_final="服务重启后仍需恢复 TTS 播放兜底。",
        content_partial="服务重启后仍需恢复 TTS 播放兜底。",
        tts_expected_sentences=2,
        tts_created_sentences=2,
        tts_played_sentences=0,
        tts_played_sentence_indices=[],
        tts_skipped_sentences=[],
    )
    calls = []

    async def scenario() -> None:
        gate = asyncio.Event()

        async def fake_grace(speech_id: str, task_id: str, expected: int) -> None:
            calls.append((speech_id, task_id, expected))
            await gate.wait()

        monkeypatch.setattr(store, "_complete_agent_playback_after_grace", fake_grace)
        await store.resume_runtime_tasks()
        await store.resume_runtime_tasks()
        await asyncio.sleep(0)
        assert calls == [(speech["id"], speech["tts_task_id"], 2)]
        gate.set()
        await asyncio.sleep(0)

    asyncio.run(scenario())


def test_snapshot_refresh_rearms_missing_tts_grace_task(monkeypatch) -> None:
    speech = _seed_running_speech(
        content_final="运行态刷新也要能恢复丢失的 TTS 兜底任务。",
        content_partial="运行态刷新也要能恢复丢失的 TTS 兜底任务。",
        tts_expected_sentences=2,
        tts_created_sentences=2,
        tts_ready_sentences=2,
        tts_played_sentences=0,
        tts_played_sentence_indices=[],
        tts_skipped_sentences=[],
    )
    store._tts_grace_tasks.clear()
    calls = []

    async def scenario() -> None:
        gate = asyncio.Event()

        async def fake_grace(speech_id: str, task_id: str, expected: int) -> None:
            calls.append((speech_id, task_id, expected))
            await gate.wait()

        monkeypatch.setattr(store, "_complete_agent_playback_after_grace", fake_grace)
        await store.get_snapshot()
        await store.get_snapshot()
        await asyncio.sleep(0)
        assert calls == [(speech["id"], speech["tts_task_id"], 2)]
        gate.set()
        await asyncio.sleep(0)

    asyncio.run(scenario())


# --- live 流式默认关 ------------------------------------------------------------

def _stream_selection():
    class StreamGateway:
        def stream_mime_type(self, **_opts) -> str:
            return "audio/mpeg"

        async def synthesize_stream(self, text: str, **_opts):  # pragma: no cover - 仅需存在
            yield {"type": "done", "mime_type": "audio/mpeg", "latency_ms": 1, "chunk_count": 1}

    return SimpleNamespace(provider="alicloud", gateway=StreamGateway(), options={})


def test_live_stream_default_off(monkeypatch) -> None:
    monkeypatch.delenv("PHDEBATE_TTS_LIVE_STREAM", raising=False)
    assert store._tts_live_mime_type(_stream_selection()) == ""


def test_live_stream_explicit_on_still_works(monkeypatch) -> None:
    monkeypatch.setenv("PHDEBATE_TTS_LIVE_STREAM", "1")
    assert store._tts_live_mime_type(_stream_selection()) == "audio/mpeg"


def test_live_stream_off_values(monkeypatch) -> None:
    for value in ("0", "false", "no", "off"):
        monkeypatch.setenv("PHDEBATE_TTS_LIVE_STREAM", value)
        assert store._tts_live_mime_type(_stream_selection()) == ""


# --- 手动救援控制 --------------------------------------------------------------

def test_force_skip_sentence_records_index() -> None:
    speech = _seed_running_speech()
    snap = asyncio.run(store.force_skip_sentence(speech["id"], 2))
    assert 2 in store.snapshot["current_speech"]["tts_skipped_sentences"]
    assert snap["current_speech"]["tts_skipped_sentences"] == [2]


def test_force_skip_sentence_idempotent() -> None:
    speech = _seed_running_speech(tts_skipped_sentences=[2])
    asyncio.run(store.force_skip_sentence(speech["id"], 2))
    assert store.snapshot["current_speech"]["tts_skipped_sentences"].count(2) == 1


def test_force_skip_last_sentence_auto_completes_playback() -> None:
    speech = _seed_running_speech(
        content_final="最后一段被主持人强制跳过后，应直接结束本轮发言。",
        content_partial="最后一段被主持人强制跳过后，应直接结束本轮发言。",
        tts_expected_sentences=2,
        tts_created_sentences=2,
        tts_played_sentences=1,
        tts_skipped_sentences=[],
    )
    snap = asyncio.run(store.force_skip_sentence(speech["id"], 1))

    assert snap["current_speech"] is None
    assert store.events[-2]["type"] == "tts.sentence_ready"
    assert store.events[-2]["payload"]["skipped"] is True
    assert store.events[-1]["type"] == "speech.ended"


def test_force_skip_unknown_speech_raises() -> None:
    _seed_running_speech(speech_id="sp_real")
    from app.services.match_store import MatchStateError

    with pytest.raises(MatchStateError):
        asyncio.run(store.force_skip_sentence("sp_other", 1))


def test_resynthesize_speech_tts_rebuilds_audio(monkeypatch) -> None:
    class OkGateway:
        async def synthesize(self, text: str, **_opts) -> TTSResult:
            return TTSResult(audio=b"mp3", mime_type="audio/mpeg", latency_ms=2, chunk_count=1)

    _patch_tts(monkeypatch, OkGateway(), provider="xfyun")
    speech = _seed_running_speech(
        content_final="谢谢主席。我方认为编程思维比提问思维更重要。理由有三点，论证如下。",
        tts_skipped_sentences=[0, 1],
    )
    # 旧的脏音频，应被清空重建
    store.snapshot["audio_assets"] = [{"speech_id": speech["id"], "chunks": [{"chunk_index": 0, "audio_url": "/stale"}]}]

    snap = asyncio.run(store.resynthesize_speech_tts(speech["id"]))

    cs = snap["current_speech"]
    assert cs["tts_task_id"] != "task_seed"  # 分配了新 task_id → 大屏会 STOP(task_changed) 后重播
    expected = int(cs["tts_expected_sentences"])
    assert expected >= 1
    ready = store._tts_ready_indices(speech["id"])
    skipped = set(int(i) for i in cs["tts_skipped_sentences"])
    assert ready | skipped == set(range(expected))  # 重建后不变量仍成立
    assert ready  # 至少有成功合成的分段


def test_resynthesize_requires_text() -> None:
    _seed_running_speech(content_final="", content_partial="")
    from app.services.match_store import MatchStateError

    with pytest.raises(MatchStateError):
        asyncio.run(store.resynthesize_speech_tts("sp_test"))


def test_sentence_tts_timeout_emits_skip(monkeypatch) -> None:
    class HangingGateway:
        async def synthesize(self, text: str, **_opts) -> TTSResult:
            await asyncio.sleep(60)
            return TTSResult(audio=b"late", mime_type="audio/mpeg", latency_ms=60000, chunk_count=1)

    _patch_tts(monkeypatch, HangingGateway(), provider="xfyun")
    monkeypatch.setattr(store, "_tts_sentence_timeout_seconds", lambda: 0.01)
    speech = _seed_running_speech()
    speaker = next(item for item in store.snapshot["speakers"] if item["id"] == speech["speaker_id"])

    ok = asyncio.run(store._synthesize_sentence_tts_with_timeout("这是一句会超时的语音合成。", 1, speech["tts_task_id"], speech["id"], speaker))

    assert ok is False
    assert 1 in store.snapshot["current_speech"]["tts_skipped_sentences"]
    assert store.events[-1]["type"] == "tts.sentence_ready"
    assert store.events[-1]["payload"]["skipped"] is True
    assert store.events[-1]["payload"]["reason"] == "tts_synthesize_timeout"
