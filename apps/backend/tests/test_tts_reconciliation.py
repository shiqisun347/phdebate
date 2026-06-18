"""TTS 可靠性不变量测试。

核心保证：一段 AI 发言合成结束后，[0, expected) 中每个分段序号都必然 ready 或 skipped
（expected == |ready| + |skipped| 且不相交）——这样大屏的纯函数对账仅凭快照就能推进到
结束、绝不会等一个永远不来的分段而永久卡死。另外覆盖 live 流式默认关、手动救援控制。
"""

import asyncio
from types import SimpleNamespace

import pytest

from app.main import app
from app.services.match_store import store
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


def test_completion_invariant_with_gaps(monkeypatch) -> None:
    """多句 + 半数失败：必然出现 skipped 缺口，且不变量仍成立（大屏据此可推进到结束）。"""
    monkeypatch.setenv("PHDEBATE_TTS_SENTENCE_CONCURRENCY", "1")  # 顺序，便于断言确定的缺口
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
