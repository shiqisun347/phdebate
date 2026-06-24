import json
import sqlite3
import asyncio
import hashlib
import importlib.util
import io
import re
import zipfile
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
import httpx
from fastapi.testclient import TestClient

from app.main import app
from app.main import FRONTEND_ASSETS, FRONTEND_INDEX
from app.services.agent_gateway import AgentGateway, AgentGatewayError
from app.services.fallback_plan import fallback_topic_key, load_fallback_plan, text_for_phase
from app.services.match_store import store
from app.services.ruleset_store import FLOW_TEMPLATE, parse_flow, ruleset_store
from app.services.xfyun_gateway import ASRResult, TTSResult, XfyunGatewayError


client = TestClient(app)


def load_mock_agent_app():
    path = Path(__file__).resolve().parents[2] / "mock_agent" / "app.py"
    spec = importlib.util.spec_from_file_location("phdebate_mock_agent_app", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.app


@pytest.fixture(autouse=True)
def reset_demo_state(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("PHDEBATE_RUNTIME_AUTH_FILE", str(tmp_path / "runtime_auth.json"))
    from app.services.integration_config import integration_config

    integration_config.config = integration_config._seed_from_env()
    integration_config._normalize()
    integration_config._apply_to_env()
    client.post("/api/demo/reset")


def _patch_tts_selection(monkeypatch, gateway, provider: str = "test", preset=None) -> None:
    monkeypatch.setattr(
        "app.services.match_store.select_tts_gateway",
        lambda **_kwargs: SimpleNamespace(gateway=gateway, provider=provider, options={}, preset=preset),
    )


def _patch_asr_selection(monkeypatch, gateway, provider: str = "test") -> None:
    monkeypatch.setattr(
        "app.services.match_store.select_asr_gateway",
        lambda **_kwargs: SimpleNamespace(gateway=gateway, provider=provider, options={}),
    )


def _use_embedded_mock_agent(speaker_id: str = "spk_aff_2") -> None:
    """Demo agents default to the qwen openai_sdk provider, which requires a live
    API key. Tests that exercise the offline embedded-mock speech path switch the
    speaker's agent config to rest_api with no endpoint so stream_speech falls back
    to the deterministic mock chunks."""
    resp = client.patch(
        f"/api/matches/match_001/agents/configs/agent_{speaker_id}",
        json={"provider_type": "rest_api", "endpoint": ""},
    )
    assert resp.status_code == 200, resp.text


def test_health() -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_standard_ruleset_template_includes_third_debater_before_free_debate() -> None:
    names = [item["name"] for item in parse_flow(FLOW_TEMPLATE)["nodes"]]

    assert names == [
        "正方一辩立论",
        "反方一辩立论",
        "正方二辩陈词",
        "反方二辩陈词",
        "正方三辩陈词",
        "反方三辩陈词",
        "自由辩论",
        "反方四辩总结",
        "正方四辩总结",
    ]
    assert "正方三辩陈词" in names
    assert "反方三辩陈词" in names
    assert names.index("正方三辩陈词") < names.index("自由辩论")
    assert names.index("反方三辩陈词") < names.index("自由辩论")


def test_ruleset_phase_conversion_preserves_explicit_phase_ids() -> None:
    phases = store._phases_from_ruleset(
        {
            "flow": [
                {
                    "id": "phase_custom_aff3",
                    "key": "aff_statement_3",
                    "name": "正方三辩陈词",
                    "side": "affirmative",
                    "speaker": "三辩",
                    "duration_seconds": 90,
                    "phase_type": "statement",
                }
            ]
        }
    )

    assert phases[0]["id"] == "phase_custom_aff3"
    assert phases[0]["speaker_seat"] == 3


def test_ruleset_update_applies_times_to_current_match_without_replacing_phase_ids() -> None:
    original = ruleset_store.get("ruleset_standard_4v4")
    assert original is not None
    try:
        created = client.post(
            "/api/matches",
            json={"title": "赛制同步测试", "topic": "测试辩题", "ruleset_id": "ruleset_standard_4v4"},
        )
        assert created.status_code == 200, created.text
        snapshot = client.get("/api/matches/current").json()["data"]
        first_phase_id = snapshot["phases"][0]["id"]
        assert snapshot["phases"][0]["duration_seconds"] == 180
        started_match = client.post("/api/matches/current/start")
        assert started_match.status_code == 200, started_match.text
        started = client.post(f"/api/matches/current/phases/{first_phase_id}/start")
        assert started.status_code == 200, started.text

        flow = deepcopy(original["flow"])
        flow[0]["duration_seconds"] = 120
        free_node = next(item for item in flow if item["phase_type"] == "free_debate")
        free_node["side_total_seconds"] = 90
        free_node["turn_seconds"] = 12
        free_node["duration_seconds"] = 180

        updated = client.patch(
            "/api/admin/rulesets/ruleset_standard_4v4",
            json={**original, "flow": flow},
        )

        assert updated.status_code == 200, updated.text
        applied = updated.json()["data"]["applied_current_match"]
        assert applied["applied"] is True
        data = client.get("/api/matches/current").json()["data"]
        assert data["phases"][0]["id"] == first_phase_id
        assert data["phases"][0]["duration_seconds"] == 120
        free_phase = next(item for item in data["phases"] if item["phase_type"] == "free_debate")
        assert free_phase["duration_seconds"] == 180
        assert free_phase["side_total_seconds"] == 90
        assert free_phase["turn_seconds"] == 12
        clocks = {clock["name"]: clock for clock in data["clocks"]}
        assert clocks["main"]["total_seconds"] == 120
    finally:
        ruleset_store.update("ruleset_standard_4v4", original)


def test_streaming_tts_splits_long_prefix_on_soft_break(monkeypatch) -> None:
    monkeypatch.setenv("PHDEBATE_TTS_EARLY_SEGMENT_CHARS", "48")
    text = "我们首先要明确今天的争议焦点并不是技术本身是否有价值，而是它是否应该成为所有人必须掌握的基础能力，后续论证还在继续生成"

    segment, position = store._next_tts_sentence(text, 0)

    assert segment.endswith("，")
    assert 0 < position < len(text)


def test_streaming_tts_hard_splits_near_default_threshold(monkeypatch) -> None:
    monkeypatch.delenv("PHDEBATE_TTS_EARLY_SEGMENT_CHARS", raising=False)
    text = "我们首先要明确今天的争议焦点并不是技术本身是否有价值，而是它是否应该成为所有人必须掌握的基础能力，后续论证还在继续生成"

    segment, position = store._next_tts_sentence(text, 0)

    assert len(segment) <= 68
    assert position == len(segment)


def test_streaming_tts_first_segment_starts_early(monkeypatch) -> None:
    # 首段尽快出声：完整的短开场句（即便很短）立刻发出，不再为了凑长度而合并——这样首句更早
    # 开始合成、更短=合成更快，把"开口很慢"压下来。切点是自然句末/逗号，不会把词切断。
    monkeypatch.delenv("PHDEBATE_TTS_EARLY_SEGMENT_CHARS", raising=False)
    monkeypatch.delenv("PHDEBATE_TTS_FIRST_SEGMENT_CHARS", raising=False)
    text = "谢谢主席。我们首先要明确今天的争议焦点并不是技术本身是否有价值，而是它是否应该成为所有人必须掌握的基础能力，后续论证还在继续生成。"

    # 第一段立即取到完整的短开场句"谢谢主席。"，不再合并到 40 字。
    segment, position = store._next_tts_sentence(text, 0)
    assert segment == "谢谢主席。"
    assert position == len("谢谢主席。")

    # 还没有句末、仅有逗号时（模拟生成中），首段在第一个 >= first_min 的逗号处即切出
    # （自然停顿，远早于 early_chars）。注意：不含句末标点。
    comma_text = "我们首先要明确今天的争议焦点并不是技术本身是否有价值，而是它是否应该成为所有人必须掌握的基础能力，后续论证还在继续生成"
    seg2, pos2 = store._next_tts_sentence(comma_text, 0)
    assert seg2.endswith("，")
    assert 0 < pos2 < len(comma_text)

    # 后续段（allow_soft_break=False）只在真正句末切，绝不在逗号处把句子切断。
    seg_mid, _ = store._next_tts_sentence(comma_text, 0, allow_soft_break=False)
    assert seg_mid == ""  # 还没遇到句末 → 不切


def test_local_qwen_tts_uses_sequential_stable_synthesis_by_default(monkeypatch) -> None:
    from app.services.integration_config import integration_config

    monkeypatch.delenv("PHDEBATE_TTS_SENTENCE_CONCURRENCY", raising=False)
    integration_config.config["tts"]["provider"] = "local_qwen"
    assert store._tts_sentence_concurrency() == 1

    integration_config.config["tts"]["provider"] = "alicloud"
    assert store._tts_sentence_concurrency() == 1

    integration_config.config["tts"]["provider"] = "local_qwen"
    integration_config.config["tts"]["settings"]["sentence_concurrency"] = 3
    assert store._tts_sentence_concurrency() == 3


def test_tts_speech_profile_is_stable_for_same_speaker(monkeypatch) -> None:
    preset = {
        "id": "voice_local_qwen_dylan_debater",
        "provider": "local_qwen",
        "voice": "dylan",
        "model": "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
        "sample_rate": 24000,
        "speech_rate": 1.0,
    }
    gateway = SimpleNamespace()

    monkeypatch.setattr(
        "app.services.match_store.select_tts_gateway",
        lambda **_kwargs: SimpleNamespace(gateway=gateway, provider="local_qwen", options={}, preset=preset),
    )

    speaker = {"id": "spk_aff_2", "tts_voice_preset_id": "voice_local_qwen_dylan_debater"}
    first = store._select_tts_for_speech("task_a", "speech_a", speaker)
    second = store._select_tts_for_speech("task_a", "speech_a", speaker)
    other_speaker = store._select_tts_for_speech(
        "task_b",
        "speech_b",
        {"id": "spk_neg_2", "tts_voice_preset_id": "voice_local_qwen_dylan_debater"},
    )

    assert first.options["voice"] == "dylan"
    assert first.options["model"] == "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"
    assert first.options["seed"] == second.options["seed"]
    assert first.options["speech_rate"] == 1.4
    assert first.options["volume"] == 70
    assert first.options["pitch_rate"] == 1.0
    assert first.options["temperature"] == 0.05
    assert first.options["top_p"] == 0.5
    assert first.options["strict_payload"] is True
    assert "不要抑扬顿挫" in first.options["instructions"]
    assert first.options["seed"] != other_speaker.options["seed"]


def test_fallback_status_loads_history_and_reports_agent_items(monkeypatch, tmp_path) -> None:
    history = tmp_path / "完整兜底历史.md"
    history.write_text(
        "【正方二辩陈词】\n正方二辩兜底内容。\n\n"
        "【自由辩论】\n【正方二辩】：自由辩论正方二辩。\n【反方二辩】：自由辩论反方二辩。\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PHDEBATE_FALLBACK_HISTORY_FILE", str(history))

    data = store.fallback_status()

    assert data["history_loaded"] is True
    assert data["self_intro_items"]
    assert data["checks"]["self_intro_audio_ready"] is False
    assert any(item["phase_name"] == "正方二辩陈词" and item["text_loaded"] for item in data["agent_phase_items"])
    assert len(data["free_debate_items"]) == 2
    assert data["missing_audio_count"] >= len(data["self_intro_items"])


def test_fallback_topic_specific_histories_are_selected(monkeypatch) -> None:
    monkeypatch.delenv("PHDEBATE_FALLBACK_HISTORY_FILE", raising=False)

    copyright_plan = load_fallback_plan("AI生成内容应该不应该享有版权保护?")
    persona_plan = load_fallback_plan("给AI赋予人格设定是好事还是坏事?")
    programming_plan = load_fallback_plan("AI时代，我们应该培养编程思维/提问思维")

    assert fallback_topic_key("AI生成内容应该不应该享有版权保护?") == "ai_copyright"
    assert fallback_topic_key("给AI赋予人格设定是好事还是坏事?") == "ai_persona"
    assert copyright_plan.topic_key == "ai_copyright"
    assert persona_plan.topic_key == "ai_persona"
    assert programming_plan.topic_key == "programming"
    assert "版权" in copyright_plan.sections["正方一辩立论"].text
    assert "人格设定" in persona_plan.sections["正方一辩立论"].text


def test_fallback_phase_title_synonyms_match_history_sections(monkeypatch) -> None:
    monkeypatch.delenv("PHDEBATE_FALLBACK_HISTORY_FILE", raising=False)
    plan = load_fallback_plan("AI生成内容应该不应该享有版权保护?")

    assert text_for_phase(plan, {"name": "正方一辩开篇立论"}).startswith("尊敬的评委")
    assert text_for_phase(plan, {"name": "反方二辩驳论"}).startswith("谢谢主席")
    assert text_for_phase(plan, {"name": "反方四辩总结陈词"}).startswith("谢谢主席")
    assert text_for_phase(plan, {"name": "正方四辩总结陈词"}).startswith("谢谢主席")


def test_fallback_audio_ids_are_isolated_by_topic(monkeypatch) -> None:
    monkeypatch.delenv("PHDEBATE_FALLBACK_HISTORY_FILE", raising=False)
    original_topic = store.snapshot["match"].get("topic", "")
    try:
        store.snapshot["match"]["topic"] = "AI生成内容应该不应该享有版权保护?"
        copyright_speech_id = store._fallback_speech_id("phase_aff_statement_2", "spk_aff_2")
        copyright_free_id = store._fallback_speech_id("phase_free_debate", "spk_aff_2", kind="free", turn_index=1)
        copyright_intro_id = store._fallback_speech_id("phase_aff_statement_2", "spk_aff_2", kind="self_intro")

        store.snapshot["match"]["topic"] = "给AI赋予人格设定是好事还是坏事?"
        persona_speech_id = store._fallback_speech_id("phase_aff_statement_2", "spk_aff_2")

        store.snapshot["match"]["topic"] = "AI时代，我们应该培养编程思维/提问思维"
        programming_speech_id = store._fallback_speech_id("phase_aff_statement_2", "spk_aff_2")

        assert copyright_speech_id == "fallback_ai_copyright_speech_phase_aff_statement_2_spk_aff_2"
        assert copyright_free_id == "fallback_ai_copyright_free_phase_free_debate_spk_aff_2_1"
        assert copyright_intro_id == "fallback_self_intro_phase_aff_statement_2_spk_aff_2"
        assert persona_speech_id == "fallback_ai_persona_speech_phase_aff_statement_2_spk_aff_2"
        assert programming_speech_id == "fallback_speech_phase_aff_statement_2_spk_aff_2"
    finally:
        store.snapshot["match"]["topic"] = original_topic


def test_fallback_phase_select_fills_missing_history(monkeypatch, tmp_path) -> None:
    history = tmp_path / "完整兜底历史.md"
    history.write_text(
        "【正方一辩立论】\n正方一辩真实缺失时的兜底。\n\n"
        "【反方一辩立论】\n反方一辩真实缺失时的兜底。\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PHDEBATE_FALLBACK_HISTORY_FILE", str(history))
    client.post("/api/matches/match_001/start")

    selected = client.post("/api/matches/match_001/fallback/phases/phase_aff_statement_2/select")

    assert selected.status_code == 200, selected.text
    data = selected.json()["data"]
    fallback_segments = [item for item in data["recent_transcript"] if item.get("source") == "fallback_history"]
    assert {item["phase_id"] for item in fallback_segments} >= {"phase_aff_constructive_1", "phase_neg_constructive_1"}
    assert all(item.get("fallback") is True for item in fallback_segments)


def test_fallback_phase_select_treats_asr_placeholder_as_missing(monkeypatch, tmp_path) -> None:
    history = tmp_path / "完整兜底历史.md"
    history.write_text(
        "【正方一辩立论】\n正方一辩兜底历史。\n\n"
        "【反方一辩立论】\n反方一辩兜底历史。\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PHDEBATE_FALLBACK_HISTORY_FILE", str(history))
    client.post("/api/matches/match_001/start")
    speech = {
        "id": "speech_asr_failed_aff_1",
        "phase_id": "phase_aff_constructive_1",
        "speaker_id": "spk_aff_1",
        "side": "affirmative",
        "turn_index": 0,
    }
    segment = store._upsert_transcript_segment(
        speech,
        "spk_aff_1",
        "本次发言已结束，正式转写将在后续 ASR 链路中补齐。",
        True,
        "human_asr",
    )
    segment["exclude_from_history"] = True
    segment["system_placeholder"] = "asr_pending"

    selected = client.post("/api/matches/match_001/fallback/phases/phase_aff_statement_2/select")

    assert selected.status_code == 200, selected.text
    data = selected.json()["data"]
    fallback_segments = [
        item
        for item in data["recent_transcript"]
        if item.get("phase_id") == "phase_aff_constructive_1" and item.get("source") == "fallback_history"
    ]
    assert fallback_segments
    assert fallback_segments[0]["text"] == "正方一辩兜底历史。"


def test_agent_payload_fills_only_missing_human_history_from_fallback(monkeypatch, tmp_path) -> None:
    history = tmp_path / "完整兜底历史.md"
    history.write_text(
        "【正方一辩立论】\n正方一辩兜底历史。\n\n"
        "【反方一辩立论】\n反方一辩不应自动补入。\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PHDEBATE_FALLBACK_HISTORY_FILE", str(history))
    store.snapshot["recent_transcript"] = []
    placeholder_speech = {
        "id": "speech_asr_failed_aff_1",
        "phase_id": "phase_aff_constructive_1",
        "speaker_id": "spk_aff_1",
        "side": "affirmative",
        "turn_index": 0,
    }
    segment = store._upsert_transcript_segment(
        placeholder_speech,
        "spk_aff_1",
        "转写不可用，请以现场发言为准。",
        True,
        "human_asr",
    )
    segment["exclude_from_history"] = True
    segment["system_placeholder"] = "asr_unavailable"
    phase = next(item for item in store.snapshot["phases"] if item["id"] == "phase_aff_statement_2")
    speaker = next(item for item in store.snapshot["speakers"] if item["id"] == "spk_aff_2")

    payload = store._build_agent_payload("task_payload_test", "speech_payload_test", speaker, phase_override=phase)

    history_payload = payload["debate_history"]
    flat = [(stage["stage"], msg["speaker"], msg["content"]) for stage in history_payload for msg in stage["message"]]
    assert ("正方一辩立论", "正方一辩", "正方一辩兜底历史。") in flat
    assert all(content != "反方一辩不应自动补入。" for _stage, _speaker, content in flat)
    assert all("转写不可用" not in content for _stage, _speaker, content in flat)


def test_fallback_play_uses_existing_audio_without_generation(monkeypatch, tmp_path) -> None:
    history = tmp_path / "完整兜底历史.md"
    history.write_text("【正方二辩陈词】\n正方二辩兜底内容。\n", encoding="utf-8")
    monkeypatch.setenv("PHDEBATE_FALLBACK_HISTORY_FILE", str(history))
    client.post("/api/matches/match_001/start")
    client.post("/api/matches/match_001/fallback/phases/phase_aff_statement_2/select")
    speech_id = "fallback_speech_phase_aff_statement_2_spk_aff_2"
    audio_dir = store.audio_root_path() / "match_001" / "fallback_test" / speech_id
    audio_dir.mkdir(parents=True, exist_ok=True)
    audio_path = audio_dir / "tts.mp3"
    audio_bytes = b"0" * 2048
    audio_path.write_bytes(audio_bytes)
    store._upsert_audio_asset(
        speech_id=speech_id,
        speaker_id="spk_aff_2",
        phase_id="phase_aff_statement_2",
        mime_type="audio/mpeg",
        archive_dir=audio_dir,
        chunk_path=audio_path,
        chunk_index=0,
        size_bytes=len(audio_bytes),
        duration_ms=None,
    )
    asset = store._audio_asset_for_speech(speech_id)
    assert asset is not None
    asset["status"] = "completed"
    asset["fallback_text"] = "正方二辩兜底内容。"

    played = client.post("/api/matches/match_001/fallback/phases/phase_aff_statement_2/speakers/spk_aff_2/play")

    assert played.status_code == 200, played.text
    speech = played.json()["data"]["current_speech"]
    assert speech["id"] == speech_id
    assert speech["source"] == "fallback_history"
    assert speech["tts_expected_sentences"] == 1


def test_fallback_play_force_takes_over_active_speech_and_ignores_late_asr(monkeypatch, tmp_path) -> None:
    history = tmp_path / "完整兜底历史.md"
    history.write_text("【正方二辩陈词】\n正方二辩兜底内容。\n", encoding="utf-8")
    monkeypatch.setenv("PHDEBATE_FALLBACK_HISTORY_FILE", str(history))
    client.post("/api/matches/match_001/start")
    client.post("/api/matches/match_001/phases/phase_aff_constructive_1/start")
    started = client.post("/api/matches/match_001/speakers/spk_aff_1/start-speaking")
    assert started.status_code == 200, started.text
    old_speech_id = started.json()["data"]["current_speech"]["id"]
    store.snapshot["current_speech"]["content_partial"] = "迟到的旧转写。"
    store._upsert_transcript_segment(store.snapshot["current_speech"], "spk_aff_1", "迟到的旧转写。", False, "human_asr")

    class FakeASRSession:
        async def finish(self):
            return ASRResult(text="不应该写回的 ASR 结果", latency_ms=12, chunk_count=1)

    store._asr_streams[old_speech_id] = FakeASRSession()
    store.snapshot["speech_service"]["asr"] = {"status": "streaming", "latency_ms": 12, "active_sessions": 1, "detail": "old stream"}
    store.snapshot["flow"]["awaiting_host_confirm"] = True

    speech_id = "fallback_speech_phase_aff_statement_2_spk_aff_2"
    audio_dir = store.audio_root_path() / "match_001" / "fallback_force_test" / speech_id
    audio_dir.mkdir(parents=True, exist_ok=True)
    audio_path = audio_dir / "tts.mp3"
    audio_bytes = b"0" * 2048
    audio_path.write_bytes(audio_bytes)
    store._upsert_audio_asset(
        speech_id=speech_id,
        speaker_id="spk_aff_2",
        phase_id="phase_aff_statement_2",
        mime_type="audio/mpeg",
        archive_dir=audio_dir,
        chunk_path=audio_path,
        chunk_index=0,
        size_bytes=len(audio_bytes),
        duration_ms=None,
    )
    asset = store._audio_asset_for_speech(speech_id)
    assert asset is not None
    asset["status"] = "completed"
    asset["fallback_text"] = "正方二辩兜底内容。"

    played = client.post("/api/matches/match_001/fallback/phases/phase_aff_statement_2/speakers/spk_aff_2/play")

    assert played.status_code == 200, played.text
    data = played.json()["data"]
    speech = data["current_speech"]
    assert speech["id"] == speech_id
    assert speech["speaker_id"] == "spk_aff_2"
    assert data["match"]["current_phase_id"] == "phase_aff_statement_2"
    assert data["match"]["status"] == "running"
    assert data["flow"]["awaiting_host_confirm"] is False
    assert data["speech_service"]["asr"]["active_sessions"] == 0
    assert data["speech_service"]["tts"]["status"] == "playing"
    assert old_speech_id in store._abandoned_speech_ids
    assert not any(item.get("speech_id") == old_speech_id or item.get("id") == old_speech_id for item in data["recent_transcript"])

    late_chunk = asyncio.run(store.record_audio_chunk(old_speech_id, "spk_aff_1", 0, b"\0" * 3200, "audio/L16;rate=16000"))
    assert late_chunk["ignored_after_takeover"] is True
    asyncio.run(store._record_live_asr_text(old_speech_id, "spk_aff_1", "迟到结果", True, 20, 1))
    assert not any(
        (item.get("speech_id") == old_speech_id or item.get("id") == old_speech_id) and item.get("text") == "迟到结果"
        for item in store.snapshot["recent_transcript"]
    )


def test_fallback_audio_ready_rejects_tiny_fake_package(monkeypatch, tmp_path) -> None:
    history = tmp_path / "完整兜底历史.md"
    history.write_text("【正方二辩陈词】\n正方二辩兜底内容。\n", encoding="utf-8")
    monkeypatch.setenv("PHDEBATE_FALLBACK_HISTORY_FILE", str(history))
    speech_id = "fallback_speech_phase_aff_statement_2_spk_aff_2"
    audio_dir = store.audio_root_path() / "match_001" / "fallback_test" / speech_id
    audio_dir.mkdir(parents=True, exist_ok=True)
    audio_path = audio_dir / "tts.mp3"
    audio_path.write_bytes(b"mp3")
    store._upsert_audio_asset(
        speech_id=speech_id,
        speaker_id="spk_aff_2",
        phase_id="phase_aff_statement_2",
        mime_type="audio/mpeg",
        archive_dir=audio_dir,
        chunk_path=audio_path,
        chunk_index=0,
        size_bytes=3,
        duration_ms=None,
    )
    asset = store._audio_asset_for_speech(speech_id)
    assert asset is not None
    asset["status"] = "completed"
    asset["fallback_text"] = "正方二辩兜底内容。"

    item = next(entry for entry in store._fallback_agent_phase_items() if entry["speech_id"] == speech_id)

    assert item["audio_ready"] is False


def test_self_introduction_endpoint_uses_fixed_fallback_audio(monkeypatch) -> None:
    item = next(entry for entry in store._fallback_self_intro_items() if entry["speaker_id"] == "spk_aff_2")
    assert item["text"].startswith(f"大家好，我是{item['speaker_label']}{item['speaker_name']}，")
    assert item["text"].endswith("我会清晰回应对方观点。")
    speech_id = item["speech_id"]
    task_id = item["task_id"]
    audio_dir = store.audio_root_path() / "match_001" / "fallback_self_intro_test" / speech_id
    audio_dir.mkdir(parents=True, exist_ok=True)
    audio_path = audio_dir / "intro.mp3"
    audio_bytes = b"0" * 2048
    audio_path.write_bytes(audio_bytes)
    store._upsert_audio_asset(
        speech_id=speech_id,
        speaker_id="spk_aff_2",
        phase_id=item["phase_id"],
        mime_type="audio/mpeg",
        archive_dir=audio_dir,
        chunk_path=audio_path,
        chunk_index=0,
        size_bytes=len(audio_bytes),
        duration_ms=None,
    )
    asset = store._audio_asset_for_speech(speech_id)
    assert asset is not None
    asset["status"] = "completed"
    asset["fallback_text"] = item["text"]

    def fail_stream(*_args, **_kwargs):
        raise AssertionError("self-introduction must use fixed fallback audio, not live agent")

    monkeypatch.setattr(store.agent_gateway, "stream_speech", fail_stream)

    response = client.post("/api/matches/match_001/speakers/spk_aff_2/self-introduction")

    assert response.status_code == 200, response.text
    speech = response.json()["data"]["current_speech"]
    assert speech["id"] == speech_id
    assert speech["tts_task_id"] == task_id
    assert speech["kind"] == "self_intro"
    assert speech["source"] == "fallback_history"
    assert speech["content_final"] == item["text"]
    assert speech["tts_expected_sentences"] == 1


def test_fallback_free_debate_restricts_to_numbered_human(monkeypatch, tmp_path) -> None:
    history = tmp_path / "完整兜底历史.md"
    history.write_text(
        "【自由辩论】\n"
        "【正方三辩】：正方三辩按编号发言。\n"
        "【反方二辩】：反方二辩预设音频。\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PHDEBATE_FALLBACK_HISTORY_FILE", str(history))
    client.post("/api/matches/match_001/start")
    asyncio.run(store.start_phase("phase_free_debate"))

    snapshot = asyncio.run(store.start_fallback_free_debate())

    assert snapshot["free_debate"]["fallback_mode"] is True
    assert snapshot["free_debate"]["current_fallback_speaker_id"] == "spk_aff_3"
    wrong = client.post("/api/matches/match_001/speakers/spk_aff_1/start-speaking")
    assert wrong.status_code == 409
    assert wrong.json()["error"]["code"] == "invalid_speaker"
    right = client.post("/api/matches/match_001/speakers/spk_aff_3/start-speaking")
    assert right.status_code == 200, right.text


def test_fallback_free_debate_waits_between_ai_turns(monkeypatch, tmp_path) -> None:
    history = tmp_path / "完整兜底历史.md"
    history.write_text(
        "【自由辩论】\n"
        "【正方二辩】：正方二辩预设音频。\n"
        "【反方一辩】：反方一辩预设音频。\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PHDEBATE_FALLBACK_HISTORY_FILE", str(history))
    monkeypatch.setenv("PHDEBATE_FALLBACK_FREE_AI_GAP_SECONDS", "0.05")
    calls = []

    async def fake_play(phase_id: str, speaker_id: str, *, free_turn_index=None):
        calls.append((asyncio.get_running_loop().time(), phase_id, speaker_id, free_turn_index))
        return await store.get_snapshot()

    monkeypatch.setattr(store, "play_fallback_speech", fake_play)

    async def scenario():
        await store.start_phase("phase_free_debate")
        await store.start_fallback_free_debate()
        first_started_at = calls[-1][0]
        await store._advance_fallback_free_debate_turn("speech_end")
        return first_started_at, calls[-1][0]

    first_started_at, second_started_at = asyncio.run(scenario())

    assert [call[2:] for call in calls] == [
        ("spk_aff_2", 1),
        ("spk_neg_1", 2),
    ]
    assert second_started_at - first_started_at >= 0.045


def test_fallback_phase_select_free_debate_starts_as_normal_mode(monkeypatch, tmp_path) -> None:
    history = tmp_path / "完整兜底历史.md"
    history.write_text(
        "【正方一辩立论】\n正方一辩兜底。\n\n"
        "【自由辩论】\n【正方三辩】：正方三辩编号发言。\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PHDEBATE_FALLBACK_HISTORY_FILE", str(history))
    monkeypatch.setenv("PHDEBATE_FREE_DEBATE_DECISION_SECONDS", "99")
    client.post("/api/matches/match_001/start")

    selected = client.post("/api/matches/match_001/fallback/phases/phase_free_debate/select")

    assert selected.status_code == 200, selected.text
    data = selected.json()["data"]
    assert data["match"]["current_phase_id"] == "phase_free_debate"
    assert data["free_debate"]["assignment_mode"] == "teammate_control"
    assert data["free_debate"]["fallback_mode"] is False


def test_production_auth_requires_read_token_and_keeps_vote_options_public(monkeypatch) -> None:
    monkeypatch.setenv("PHDEBATE_ENV", "production")
    monkeypatch.setenv("PHDEBATE_SCREEN_TOKEN", "screen-secret")

    blocked = client.get("/api/matches/match_001")
    assert blocked.status_code == 401
    assert blocked.json()["error"]["code"] == "unauthorized"

    allowed = client.get("/api/matches/match_001", headers={"Authorization": "Bearer screen-secret"})
    assert allowed.status_code == 200
    assert allowed.json()["data"]["match"]["id"] == "match_001"

    public_options = client.get("/api/public/matches/match_001/vote-options")
    assert public_options.status_code == 200
    data = public_options.json()["data"]
    assert data["match"]["id"] == "match_001"
    assert len(data["teams"]) == 2
    assert len(data["speakers"]) == 8


def test_production_auth_separates_admin_host_and_screen_permissions(monkeypatch) -> None:
    monkeypatch.setenv("PHDEBATE_ENV", "production")
    monkeypatch.setenv("PHDEBATE_ADMIN_PASSWORD", "admin-secret")
    monkeypatch.setenv("PHDEBATE_HOST_PASSWORD", "host-secret")
    monkeypatch.setenv("PHDEBATE_SCREEN_TOKEN", "screen-secret")

    screen_control = client.post("/api/matches/match_001/pause", headers={"Authorization": "Bearer screen-secret"})
    assert screen_control.status_code == 403
    assert screen_control.json()["error"]["code"] == "forbidden"

    host_control = client.post("/api/matches/match_001/pause", headers={"Authorization": "Bearer host-secret"})
    assert host_control.status_code == 200
    assert host_control.json()["data"]["match"]["status"] == "paused"

    host_settings = client.patch(
        "/api/matches/match_001",
        headers={"Authorization": "Bearer host-secret"},
        json={"title": "host should not edit settings"},
    )
    assert host_settings.status_code == 403

    host_activate = client.post(
        "/api/matches/match_001/speakers/spk_aff_2/activate",
        headers={"Authorization": "Bearer host-secret"},
    )
    assert host_activate.status_code == 403
    assert host_activate.json()["error"]["code"] == "forbidden"

    admin_settings = client.patch(
        "/api/matches/match_001",
        headers={"Authorization": "Bearer admin-secret"},
        json={"title": "生产鉴权联调赛"},
    )
    assert admin_settings.status_code == 200
    assert admin_settings.json()["data"]["match"]["title"] == "生产鉴权联调赛"

    export = client.post("/api/matches/match_001/exports", headers={"Authorization": "Bearer host-secret"})
    assert export.status_code == 200
    download_url = f"{export.json()['data']['download_url']}?token=host-secret"
    download = client.get(download_url)
    assert download.status_code == 200
    assert download.headers["content-type"].startswith("application/zip")


def test_production_speaker_token_can_only_control_matching_speaker(monkeypatch) -> None:
    monkeypatch.setenv("PHDEBATE_ENV", "production")
    monkeypatch.setenv("PHDEBATE_SPEAKER_TOKENS", '{"spk_aff_3":"aff-secret","spk_neg_2":"neg-secret"}')

    missing = client.post("/api/matches/match_001/speakers/spk_aff_3/start-speaking")
    assert missing.status_code == 401

    wrong_speaker = client.post(
        "/api/matches/match_001/speakers/spk_aff_3/start-speaking",
        headers={"Authorization": "Bearer neg-secret"},
    )
    assert wrong_speaker.status_code == 403
    assert wrong_speaker.json()["error"]["code"] == "forbidden"

    allowed = client.post(
        "/api/matches/match_001/speakers/spk_aff_3/start-speaking",
        headers={"Authorization": "Bearer aff-secret"},
    )
    assert allowed.status_code == 200
    assert allowed.json()["data"]["current_speech"]["speaker_id"] == "spk_aff_3"


def test_speaker_profile_endpoint_syncs_name_with_speaker_permission(monkeypatch) -> None:
    monkeypatch.setenv("PHDEBATE_ENV", "production")
    monkeypatch.setenv("PHDEBATE_SPEAKER_TOKENS", '{"spk_aff_3":"aff-secret","spk_neg_2":"neg-secret"}')

    wrong = client.patch(
        "/api/matches/match_001/speakers/spk_aff_3/profile",
        headers={"Authorization": "Bearer neg-secret"},
        json={"name": "不应更新"},
    )
    assert wrong.status_code == 403

    updated = client.patch(
        "/api/matches/match_001/speakers/spk_aff_3/profile",
        headers={"Authorization": "Bearer aff-secret"},
        json={"name": "现场姓名"},
    )
    assert updated.status_code == 200
    speaker = next(item for item in updated.json()["data"]["speakers"] if item["id"] == "spk_aff_3")
    assert speaker["name"] == "现场姓名"

    invalid = client.patch(
        "/api/matches/match_001/speakers/spk_aff_3/profile",
        headers={"Authorization": "Bearer aff-secret"},
        json={"name": "   "},
    )
    assert invalid.status_code == 409
    assert invalid.json()["error"]["code"] == "invalid_speaker_profile"


def test_production_auth_accepts_hashed_token_file(monkeypatch, tmp_path) -> None:
    token_file = tmp_path / "tokens.json"
    token_file.write_text(
        json.dumps(
            {
                "version": 1,
                "hash_algorithm": "sha256",
                "admin_hashes": [f"sha256:{_sha256('admin-from-file')}"],
                "host_hashes": [f"sha256:{_sha256('host-from-file')}"],
                "screen_hashes": [f"sha256:{_sha256('screen-from-file')}"],
                "speaker_hashes": {"spk_aff_3": [f"sha256:{_sha256('speaker-from-file')}"]},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PHDEBATE_ENV", "production")
    monkeypatch.setenv("PHDEBATE_TOKEN_FILE", str(token_file))

    blocked = client.get("/api/matches/match_001")
    assert blocked.status_code == 401

    screen_read = client.get("/api/matches/match_001", headers={"Authorization": "Bearer screen-from-file"})
    assert screen_read.status_code == 200

    host_control = client.post("/api/matches/match_001/pause", headers={"Authorization": "Bearer host-from-file"})
    assert host_control.status_code == 200
    resumed = client.post("/api/matches/match_001/resume", headers={"Authorization": "Bearer host-from-file"})
    assert resumed.status_code == 200

    admin_edit = client.patch(
        "/api/matches/match_001",
        headers={"Authorization": "Bearer admin-from-file"},
        json={"title": "哈希口令文件联调赛"},
    )
    assert admin_edit.status_code == 200

    wrong_speaker = client.post(
        "/api/matches/match_001/speakers/spk_neg_2/start-speaking",
        headers={"Authorization": "Bearer speaker-from-file"},
    )
    assert wrong_speaker.status_code == 403

    right_speaker = client.post(
        "/api/matches/match_001/speakers/spk_aff_3/start-speaking",
        headers={"Authorization": "Bearer speaker-from-file"},
    )
    assert right_speaker.status_code == 200


def test_runtime_auth_toggle_persists_hashes_and_enforces_roles() -> None:
    status = client.get("/api/admin/security/auth")
    assert status.status_code == 200
    assert status.json()["data"]["auth_required"] is False

    missing_admin = client.put("/api/admin/security/auth", json={"auth_required": True})
    assert missing_admin.status_code == 409
    assert missing_admin.json()["error"]["code"] == "missing_admin_token"

    enabled = client.put(
        "/api/admin/security/auth",
        json={
            "auth_required": True,
            "tokens": {
                "admin": "runtime-admin",
                "host": "runtime-host",
                "screen": "runtime-screen",
                "speaker_shared": "runtime-speaker",
            },
        },
    )
    assert enabled.status_code == 200
    data = enabled.json()["data"]
    assert data["auth_required"] is True
    assert data["runtime_configured"] is True
    assert data["token_sources"]["admin"]["runtime_count"] == 1

    blocked = client.get("/api/matches/match_001")
    assert blocked.status_code == 401

    admin_read = client.get("/api/admin/security/auth", headers={"Authorization": "Bearer runtime-admin"})
    assert admin_read.status_code == 200

    host_control = client.post("/api/matches/match_001/pause", headers={"Authorization": "Bearer runtime-host"})
    assert host_control.status_code == 200

    host_emergency = client.post("/api/matches/match_001/emergency-stop", headers={"Authorization": "Bearer runtime-host"})
    assert host_emergency.status_code == 403

    admin_emergency = client.post("/api/matches/match_001/emergency-stop", headers={"Authorization": "Bearer runtime-admin"})
    assert admin_emergency.status_code == 200


def test_host_shortcut_endpoints_advance_bell_and_force_stop_current_speech() -> None:
    before = client.get("/api/matches/match_001").json()["data"]
    assert before["audio_output"]["mode"] == "host"
    current_order = next(
        phase["display_order"]
        for phase in before["phases"]
        if phase["id"] == before["match"]["current_phase_id"]
    )

    next_phase = client.post("/api/matches/match_001/phases/next")
    assert next_phase.status_code == 200
    after_next = next_phase.json()["data"]
    assert next(
        phase["display_order"]
        for phase in after_next["phases"]
        if phase["id"] == after_next["match"]["current_phase_id"]
    ) == current_order + 1

    audio_output = client.put("/api/matches/match_001/audio-output", json={"mode": "admin", "reason": "test_admin_output"})
    assert audio_output.status_code == 200
    assert audio_output.json()["data"]["audio_output"]["mode"] == "admin"
    assert audio_output.json()["data"]["audio_output"]["updated_by"] == "host"

    invalid_audio_output = client.put("/api/matches/match_001/audio-output", json={"mode": "console"})
    assert invalid_audio_output.status_code == 409
    assert invalid_audio_output.json()["error"]["code"] == "invalid_audio_output"

    bell = client.post("/api/matches/match_001/bell", json={"kind": "manual", "label": "测试铃"})
    assert bell.status_code == 200
    assert bell.json()["data"]["last_seq"] >= after_next["last_seq"]
    assert bell.json()["data"]["audio_output"]["mode"] == "admin"

    current_phase = next(
        phase
        for phase in after_next["phases"]
        if phase["id"] == after_next["match"]["current_phase_id"]
    )
    speaker = next(
        speaker
        for speaker in after_next["speakers"]
        if (
            current_phase["side"] == "neutral"
            or speaker["side"] == current_phase["side"]
        )
        and (
            current_phase["speaker_seat"] is None
            or speaker["seat"] == current_phase["speaker_seat"]
        )
    )

    start = client.post(f"/api/matches/match_001/speakers/{speaker['id']}/start-speaking")
    assert start.status_code == 200
    assert start.json()["data"]["current_speech"]["speaker_id"] == speaker["id"]

    stopped = client.post("/api/matches/match_001/speeches/current/stop", json={"reason": "test"})
    assert stopped.status_code == 200
    data = stopped.json()["data"]
    assert data["current_speech"] is None
    assert any(segment["speaker_id"] == speaker["id"] and segment["is_final"] for segment in data["recent_transcript"])


def test_speech_diagnostics_reports_mock_fallback_when_xfyun_missing(monkeypatch) -> None:
    for name in ["XFYUN_APP_ID", "XFYUN_API_KEY", "XFYUN_API_SECRET", "XFYUN_ASR_URL", "XFYUN_TTS_URL"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("PHDEBATE_TTS_FORMAL", raising=False)

    response = client.get("/api/matches/match_001/speech/diagnostics")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["overall_status"] == "mock_fallback"
    assert data["provider"] == {"asr": "alicloud", "tts": "alicloud"}
    assert "DASHSCOPE_API_KEY / alicloud.api_key" in data["asr"]["missing"]
    assert data["asr"]["runtime_config"]["open_timeout_s"] == 8.0
    assert data["asr"]["runtime_config"]["final_timeout_s"] == 12.0
    assert data["audio_archive"]["writable"] is True
    assert data["realtime_asr"]["enabled"] is False
    assert data["realtime_asr"]["mode"] == "auto_when_ready"
    assert data["auto_recognize"]["enabled"] is False
    assert data["auto_recognize"]["mode"] == "auto_when_ready"
    assert data["formal_tts"]["enabled"] is False
    assert data["formal_tts"]["mode"] == "auto_when_ready"


def test_speech_diagnostics_reports_ready_when_xfyun_configured(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XFYUN_APP_ID", "appid")
    monkeypatch.setenv("XFYUN_API_KEY", "apikey")
    monkeypatch.setenv("XFYUN_API_SECRET", "secret")
    monkeypatch.setenv("XFYUN_ASR_URL", "wss://iat-api.xfyun.cn/v2/iat?token=secret")
    monkeypatch.setenv("XFYUN_TTS_URL", "wss://tts-api.xfyun.cn/v2/tts")
    monkeypatch.setenv("XFYUN_ASR_OPEN_TIMEOUT_S", "5")
    monkeypatch.setenv("XFYUN_ASR_FINAL_TIMEOUT_S", "9")
    monkeypatch.setenv("PHDEBATE_AUDIO_DIR", str(tmp_path / "audio"))
    client.patch(
        "/api/matches/match_001/integration-config",
        json={
            "asr": {
                "enabled": True,
                "provider": "xfyun",
                "endpoint": "wss://iat-api.xfyun.cn/v2/iat?token=secret",
                "secrets": {"app_id": "appid", "api_key": "apikey", "api_secret": "secret"},
            },
            "tts": {
                "enabled": True,
                "provider": "xfyun",
                "endpoint": "wss://tts-api.xfyun.cn/v2/tts",
                "secrets": {"app_id": "appid", "api_key": "apikey", "api_secret": "secret"},
            },
        },
    )

    response = client.get("/api/matches/match_001/speech/diagnostics")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["overall_status"] == "ready"
    assert data["provider"] == {"asr": "xfyun", "tts": "xfyun"}
    assert data["asr"]["missing"] == []
    assert data["asr"]["url"].endswith("?...")
    assert data["asr"]["auth_ready"] is True
    assert data["asr"]["auth_preview"]["auth_algorithm"] == "hmac-sha256"
    assert data["asr"]["runtime_config"]["open_timeout_s"] == 5.0
    assert data["asr"]["runtime_config"]["final_timeout_s"] == 9.0
    assert data["audio_archive"]["root_path"] == str(tmp_path / "audio")
    assert data["realtime_asr"]["enabled"] is True
    assert data["realtime_asr"]["mode"] == "auto_when_ready"
    assert data["auto_recognize"]["enabled"] is True
    assert data["auto_recognize"]["mode"] == "auto_when_ready"
    assert data["formal_tts"]["enabled"] is True
    assert data["formal_tts"]["mode"] == "auto_when_ready"


def test_preflight_report_summarizes_rehearsal_risks(monkeypatch) -> None:
    for name in ["XFYUN_APP_ID", "XFYUN_API_KEY", "XFYUN_API_SECRET", "XFYUN_ASR_URL", "XFYUN_TTS_URL"]:
        monkeypatch.delenv(name, raising=False)

    response = client.get("/api/matches/match_001/preflight-report")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["overall_status"] in {"warn", "fail"}
    assert data["score"]["total"] >= 10
    section_ids = {section["id"] for section in data["sections"]}
    assert {"core", "clients", "agents", "speech", "votes", "exports", "security"} <= section_ids
    speech_checks = next(section for section in data["sections"] if section["id"] == "speech")["checks"]
    assert any(check["id"] == "speech_diagnostics" and check["status"] == "warn" for check in speech_checks)
    assert data["next_actions"]


def test_preflight_report_flags_missing_production_tokens(monkeypatch) -> None:
    monkeypatch.setenv("PHDEBATE_ENV", "production")
    for name in [
        "PHDEBATE_TOKEN_FILE",
        "PHDEBATE_ADMIN_PASSWORD",
        "PHDEBATE_SCREEN_TOKEN",
        "PHDEBATE_SPEAKER_TOKEN",
        "PHDEBATE_SPEAKER_TOKENS",
    ]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PHDEBATE_HOST_PASSWORD", "host-secret")

    response = client.get("/api/matches/match_001/preflight-report", headers={"Authorization": "Bearer host-secret"})

    assert response.status_code == 200
    security = next(section for section in response.json()["data"]["sections"] if section["id"] == "security")
    auth_check = next(check for check in security["checks"] if check["id"] == "auth_mode")
    assert auth_check["status"] == "fail"
    assert "token missing" in auth_check["detail"]


def test_tts_probe_synthesizes_and_archives_audio(monkeypatch, tmp_path) -> None:
    class FakeGateway:
        def __init__(self, url: str) -> None:
            self.url = url

        async def synthesize(self, text: str) -> TTSResult:
            assert "语音合成" in text
            return TTSResult(audio=b"mp3-bytes", mime_type="audio/mpeg", latency_ms=123, chunk_count=2)

    monkeypatch.setenv("PHDEBATE_AUDIO_DIR", str(tmp_path / "audio"))
    _patch_tts_selection(monkeypatch, FakeGateway("wss://fake-tts"), provider="xfyun")

    response = client.post("/api/matches/match_001/speech/tts/probe", json={"text": "语音合成自检"})

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["result"]["size_bytes"] == len(b"mp3-bytes")
    assert data["result"]["chunk_count"] == 2
    assert Path(data["result"]["file_path"]).read_bytes() == b"mp3-bytes"
    assert data["snapshot"]["speech_service"]["tts"]["latency_ms"] == 123
    requests = store.repo.load_speech_service_requests("match_001", 10)
    assert len(requests) == 1
    assert requests[0]["service"] == "tts"
    assert requests[0]["operation"] == "probe"
    assert requests[0]["status"] == "completed"
    assert requests[0]["request"]["text"] == "语音合成自检"
    assert requests[0]["response"]["size_bytes"] == len(b"mp3-bytes")
    assert requests[0]["latency_ms"] == 123


def test_asr_probe_recognizes_audio_and_updates_status(monkeypatch) -> None:
    class FakeGateway:
        def __init__(self, url: str) -> None:
            self.url = url

        async def recognize(self, audio: bytes, audio_format: str, encoding: str) -> ASRResult:
            assert audio == b"pcm"
            assert audio_format == "audio/L16;rate=16000"
            assert encoding == "raw"
            return ASRResult(text="自检通过", latency_ms=234, chunk_count=1)

    _patch_asr_selection(monkeypatch, FakeGateway("wss://fake-asr"), provider="xfyun")

    response = client.post(
        "/api/matches/match_001/speech/asr/probe",
        json={"audio_base64": "cGNt", "format": "audio/L16;rate=16000", "encoding": "raw"},
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["result"]["text"] == "自检通过"
    assert data["result"]["latency_ms"] == 234
    assert data["snapshot"]["speech_service"]["asr"]["status"] == "ok"
    requests = store.repo.load_speech_service_requests("match_001", 10)
    assert len(requests) == 1
    assert requests[0]["service"] == "asr"
    assert requests[0]["operation"] == "probe"
    assert requests[0]["status"] == "completed"
    assert requests[0]["request"]["audio_bytes"] == len(b"pcm")
    assert requests[0]["response"]["text"] == "自检通过"
    assert requests[0]["latency_ms"] == 234


def test_asr_probe_returns_clear_error_when_unconfigured(monkeypatch) -> None:
    for name in ["XFYUN_APP_ID", "XFYUN_API_KEY", "XFYUN_API_SECRET", "XFYUN_ASR_URL"]:
        monkeypatch.delenv(name, raising=False)

    response = client.post("/api/matches/match_001/speech/asr/probe", json={})

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "speech_service_error"
    assert "DashScope API Key" in body["error"]["message"]
    requests = store.repo.load_speech_service_requests("match_001", 10)
    assert len(requests) == 1
    assert requests[0]["service"] == "asr"
    assert requests[0]["operation"] == "probe"
    assert requests[0]["status"] == "failed"
    assert "DashScope API Key" in requests[0]["error_message"]
    summary = client.get("/api/matches/current/data-summary")
    assert summary.status_code == 200
    health = summary.json()["data"]["request_health"]
    assert health["speech_service_status_counts"]["failed"] == 1
    assert health["failed_speech_service_requests"][0]["service"] == "asr"
    assert health["failed_speech_service_requests"][0]["operation"] == "probe"
    assert "request" not in health["failed_speech_service_requests"][0]


def test_tts_probe_returns_clear_error_when_unconfigured(monkeypatch) -> None:
    for name in ["XFYUN_APP_ID", "XFYUN_API_KEY", "XFYUN_API_SECRET", "XFYUN_TTS_URL"]:
        monkeypatch.delenv(name, raising=False)

    response = client.post("/api/matches/match_001/speech/tts/probe", json={"text": "语音合成自检"})

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "speech_service_error"
    assert "DashScope API Key" in body["error"]["message"]
    requests = store.repo.load_speech_service_requests("match_001", 10)
    assert len(requests) == 1
    assert requests[0]["service"] == "tts"
    assert requests[0]["operation"] == "probe"
    assert requests[0]["status"] == "failed"
    assert "DashScope API Key" in requests[0]["error_message"]
    summary = client.get("/api/matches/current/data-summary")
    assert summary.status_code == 200
    health = summary.json()["data"]["request_health"]
    assert health["speech_service_status_counts"]["failed"] == 1
    assert health["failed_speech_service_requests"][0]["service"] == "tts"
    assert health["failed_speech_service_requests"][0]["operation"] == "probe"


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def test_backend_serves_built_frontend_routes_when_dist_exists() -> None:
    if not FRONTEND_INDEX.exists() or not FRONTEND_ASSETS.exists():
        pytest.skip("frontend dist has not been built")
    response = client.get("/admin?match_id=match_001")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "人机辩论赛控制系统" in response.text

    asset_match = re.search(r'src="(/assets/[^"]+\.js)"', response.text)
    assert asset_match is not None
    asset = client.get(asset_match.group(1))
    assert asset.status_code == 200
    assert "javascript" in asset.headers["content-type"]


def test_demo_match_snapshot_contract() -> None:
    response = client.get("/api/matches/match_001")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["match"]["id"] == "match_001"
    assert data["match"]["status"] in {"running", "paused", "intervention", "finished"}
    assert len(data["phases"]) == 10
    assert len(data["speakers"]) == 8
    assert data["last_seq"] >= 0


def test_current_match_alias_contract_for_official_entrypoints() -> None:
    current = client.get("/api/current-match")
    assert current.status_code == 200
    assert current.json()["data"]["id"] == "match_001"

    snapshot = client.get("/api/matches/current")
    assert snapshot.status_code == 200
    assert snapshot.json()["data"]["match"]["id"] == "match_001"

    vote_options = client.get("/api/public/matches/current/vote-options")
    assert vote_options.status_code == 200
    assert vote_options.json()["data"]["match"]["id"] == "match_001"

    opened = client.post("/api/matches/current/audience-votes/open")
    assert opened.status_code == 200
    assert opened.json()["data"]["vote_url"] == "/vote"

    with client.websocket_connect("/ws/matches/current?channel=screen") as websocket:
        first = websocket.receive_json()
        assert first["type"] == "snapshot"
        assert first["payload"]["state"]["match"]["id"] == "match_001"


def test_current_match_reset_archives_old_match_and_keeps_export_downloadable() -> None:
    wrong = client.post("/api/matches/current/reset", json={"confirm_text": "reset"})
    assert wrong.status_code == 409
    assert wrong.json()["error"]["code"] == "invalid_confirmation"

    reset = client.post("/api/matches/current/reset", json={"confirm_text": "重置比赛"})
    assert reset.status_code == 200
    data = reset.json()["data"]
    assert data["match"]["id"].startswith("match_")
    assert data["match"]["id"] != "match_001"
    assert data["match"]["status"] == "ready"
    assert data["match"]["screen_scene"] == "idle"
    assert data["match"]["current_phase_id"] == "phase_aff_constructive_1"
    assert data["current_speech"] is None
    assert data["recent_transcript"] == []
    assert data["vote_state"]["audience_count"] == 0
    assert data["vote_state"]["window_status"] == "closed"
    assert data["clocks"][0]["name"] == "main"
    assert data["clocks"][0]["state"] == "paused"

    archives = store.repo.load_match_archives(1)
    assert archives
    archive = archives[0]
    assert archive["archived_match_id"] == "match_001"
    assert archive["new_match_id"] == data["match"]["id"]
    export_bundle = archive["export_bundle"]
    assert export_bundle["export_id"]
    assert Path(export_bundle["file_path"]).exists()

    download = client.get(export_bundle["download_url"])
    assert download.status_code == 200
    assert download.headers["content-type"].startswith("application/zip")

    summary = client.get("/api/matches/current/data-summary")
    assert summary.status_code == 200
    summary_data = summary.json()["data"]
    assert summary_data["counts"]["events"] == 1
    assert summary_data["counts"]["audit_logs"] == 1
    assert summary_data["event_type_counts"]["match.reset"] == 1
    assert summary_data["recent_events"][0]["type"] == "match.reset"
    assert summary_data["recent_events"][0]["match_id"] == data["match"]["id"]


def test_start_match_from_ready_goes_live_but_does_not_auto_start_clock() -> None:
    reset = client.post("/api/matches/current/reset", json={"confirm_text": "重置比赛"})
    assert reset.status_code == 200
    reset_data = reset.json()["data"]
    assert reset_data["match"]["status"] == "ready"
    assert reset_data["match"]["screen_scene"] == "idle"
    assert reset_data["clocks"][0]["state"] == "paused"

    started = client.post("/api/matches/current/start")
    assert started.status_code == 200
    data = started.json()["data"]
    assert data["match"]["status"] == "running"
    assert data["match"]["screen_scene"] == "live"
    assert data["clocks"][0]["name"] == "main"
    # 「开始比赛」不再自动起钟：倒计时要等发言人点「开始发言」（或 AI 首次播报）才开始。
    assert data["clocks"][0]["state"] == "paused"
    assert not data["clocks"][0]["deadline_at"]


def test_match_clock_starts_only_when_speaker_starts_speaking() -> None:
    client.post("/api/matches/current/reset", json={"confirm_text": "重置比赛"})
    client.post("/api/matches/current/start")
    # 开始比赛后主钟仍暂停（不自动倒计时）。
    snap = client.get("/api/matches/current").json()["data"]
    main = next(c for c in snap["clocks"] if c["name"] == "main")
    assert main["state"] == "paused" and not main["deadline_at"]
    # 正方一辩（人类）点「开始发言」后，主钟才开始走。
    started = client.post("/api/matches/current/speakers/spk_aff_1/start-speaking")
    assert started.status_code == 200, started.text
    main2 = next(c for c in started.json()["data"]["clocks"] if c["name"] == "main")
    assert main2["state"] == "running" and main2["deadline_at"]


def test_begin_match_alias_supports_host_browser_start_flow() -> None:
    reset = client.post("/api/matches/current/reset", json={"confirm_text": "重置比赛"})
    assert reset.status_code == 200

    started = client.post("/api/matches/current/begin")
    assert started.status_code == 200
    data = started.json()["data"]
    assert data["match"]["status"] == "running"
    assert data["match"]["screen_scene"] == "live"


def test_pause_locks_vote_controls_until_match_resumes() -> None:
    opened = client.post("/api/matches/current/audience-votes/open")
    assert opened.status_code == 200

    paused = client.post("/api/matches/current/pause")
    assert paused.status_code == 200
    assert paused.json()["data"]["match"]["status"] == "paused"

    vote_options = client.get("/api/public/matches/current/vote-options")
    assert vote_options.status_code == 200
    assert vote_options.json()["data"]["match"]["status"] == "paused"

    blocked_audience = client.post(
        "/api/public/matches/current/audience-votes",
        json={
            "token": "paused-vote",
            "winner_side": "affirmative",
            "best_speaker_id": "spk_aff_3",
        },
    )
    assert blocked_audience.status_code == 409
    assert blocked_audience.json()["error"]["code"] == "vote_unavailable"

    for path, body in [
        ("/api/matches/current/audience-votes/open", {}),
        ("/api/matches/current/audience-votes/close", {}),
        ("/api/matches/current/screen/scene", {"scene": "judge_commentary"}),
        (
            "/api/matches/current/votes",
            {
                "judge_summary": {
                    "constructive": {"affirmative": 2, "negative": 1},
                    "process": {"affirmative": 2, "negative": 1},
                    "conclusion": {"affirmative": 2, "negative": 1},
                    "winner_side": "affirmative",
                    "best_speaker_id": "spk_aff_3",
                }
            },
        ),
        ("/api/matches/current/votes/publish", {"scope": "judge"}),
    ]:
        blocked = client.post(path, json=body)
        assert blocked.status_code == 409
        assert blocked.json()["error"]["code"] == "vote_unavailable"

    resumed = client.post("/api/matches/current/resume")
    assert resumed.status_code == 200
    accepted = client.post(
        "/api/public/matches/current/audience-votes",
        json={
            "token": "resumed-vote",
            "winner_side": "affirmative",
            "best_speaker_id": "spk_aff_3",
        },
    )
    assert accepted.status_code == 200


def test_judge_summary_three_aspect_votes_persist() -> None:
    response = client.post(
        "/api/matches/match_001/votes",
        json={
            "judge_summary": {
                "constructive": {"affirmative": 3, "negative": 2},
                "process": {"affirmative": 1, "negative": 4},
                "conclusion": {"affirmative": 2, "negative": 2},
                "winner_side": "negative",
                "best_speaker_id": "spk_aff_3",
            }
        },
    )
    assert response.status_code == 200
    summary = response.json()["data"]["vote_state"]["judge_summary"]
    assert summary["constructive"] == {"affirmative": 3, "negative": 2}
    assert summary["process"] == {"affirmative": 1, "negative": 4}
    assert summary["conclusion"] == {"affirmative": 2, "negative": 2}
    # 三环节合计 正 6 / 反 8 → 自动判定本应反方；显式 winner_side 也为反方。
    assert summary["computed_winner_side"] == "negative"
    assert summary["winner_side"] == "negative"
    assert response.json()["data"]["vote_state"]["winner_side"] == "negative"
    assert response.json()["data"]["vote_state"]["best_speaker_id"] == "spk_aff_3"


def test_screen_scene_control_updates_snapshot() -> None:
    response = client.post(
        "/api/matches/match_001/screen/scene",
        json={"scene": "judge_commentary"},
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["match"]["screen_scene"] == "judge_commentary"
    assert data["vote_state"]["window_status"] == "open"

    response = client.post(
        "/api/matches/match_001/screen/scene",
        json={"scene": "live", "live_mode": "free"},
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["match"]["screen_scene"] == "live"
    assert data["match"]["live_mode"] == "free"
    assert data["system"]["persistence"]["driver"] == "sqlite"


def test_xiaoqi_result_scene_requires_recorded_entry() -> None:
    # 起始：清除小七录入标记 → 切「小七评判」应被拦截。
    store.snapshot["vote_state"]["xiaoqi_recorded"] = False
    blocked = client.post("/api/matches/match_001/screen/scene", json={"scene": "xiaoqi_result"})
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "xiaoqi_result_not_recorded"

    # 小七点评（commentary）不受限制。
    ok = client.post("/api/matches/match_001/screen/scene", json={"scene": "xiaoqi_commentary"})
    assert ok.status_code == 200

    formal = client.post("/api/matches/match_001/votes", json={"winner_side": "negative", "best_speaker_id": "spk_neg_1"})
    assert formal.status_code == 200
    assert formal.json()["data"]["vote_state"]["winner_side"] == "negative"

    # 完成小七结果录入（scope=xiaoqi）后即可切换，且不覆盖正式评委结果。
    recorded = client.post(
        "/api/matches/match_001/votes",
        json={"winner_side": "affirmative", "best_speaker_id": "spk_aff_3", "scope": "xiaoqi"},
    )
    assert recorded.status_code == 200
    vote_state = recorded.json()["data"]["vote_state"]
    assert vote_state["xiaoqi_recorded"] is True
    assert vote_state["xiaoqi_summary"] == {"winner_side": "affirmative", "best_speaker_id": "spk_aff_3"}
    assert vote_state["winner_side"] == "negative"
    assert vote_state["best_speaker_id"] == "spk_neg_1"

    allowed = client.post("/api/matches/match_001/screen/scene", json={"scene": "xiaoqi_result"})
    assert allowed.status_code == 200
    assert allowed.json()["data"]["match"]["screen_scene"] == "xiaoqi_result"

    # 普通评委录入（无 scope）不应置位标记。
    store.snapshot["vote_state"]["xiaoqi_recorded"] = False
    client.post("/api/matches/match_001/votes", json={"winner_side": "negative", "best_speaker_id": "spk_neg_1"})
    assert store.snapshot["vote_state"]["xiaoqi_recorded"] is False


def test_match_settings_patch_updates_basic_fields() -> None:
    response = client.patch(
        "/api/matches/match_001",
        json={
            "title": "现场联调赛",
            "topic": "新的测试辩题",
            "affirmative_position": "正方新立场",
            "negative_position": "反方新立场",
            "organizer": "测试组织",
            "venue": "测试会场",
            "ignored_field": "should not leak",
        },
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["match"]["title"] == "现场联调赛"
    assert data["match"]["topic"] == "新的测试辩题"
    assert data["match"]["affirmative_position"] == "正方新立场"
    assert data["match"]["negative_position"] == "反方新立场"
    assert data["match"]["organizer"] == "测试组织"
    assert data["match"]["venue"] == "测试会场"
    assert "ignored_field" not in data["match"]


def test_team_settings_patch_updates_safe_fields() -> None:
    response = client.patch(
        "/api/matches/match_001/teams/team_aff",
        json={
            "name": "新正方队",
            "position": "新编程立场",
            "description": "新的队伍描述",
            "side": "negative",
        },
    )
    assert response.status_code == 200
    team = next(item for item in response.json()["data"]["teams"] if item["id"] == "team_aff")
    assert team["name"] == "新正方队"
    assert team["position"] == "新编程立场"
    assert team["description"] == "新的队伍描述"
    assert team["side"] == "affirmative"


def test_speaker_settings_patch_uses_agent_configs_instead_of_inline_agent_fields() -> None:
    direct_agent_fields = client.patch(
        "/api/matches/match_001/speakers/spk_aff_2",
        json={
            "name": "玄思升级版",
            "model_name": "Qwen-Plus",
            "model_kind": "closed_source",
            "agent_endpoint": "http://127.0.0.1:8100",
            "seat": 4,
        },
    )
    assert direct_agent_fields.status_code == 409
    assert direct_agent_fields.json()["error"]["code"] == "agent_fields_managed_by_config"
    assert "Agent 管理" in direct_agent_fields.json()["error"]["message"]

    name_only = client.patch(
        "/api/matches/match_001/speakers/spk_aff_2",
        json={"name": "玄思升级版", "seat": 4},
    )
    assert name_only.status_code == 200
    data = name_only.json()["data"]
    speaker = next(item for item in data["speakers"] if item["id"] == "spk_aff_2")
    agent = next(item for item in data["agent_status"] if item["speaker_id"] == "spk_aff_2")
    assert speaker["name"] == "玄思升级版"
    assert speaker["seat"] == 2
    config = next(item for item in data["agent_configs"] if item["id"] == speaker["agent_config_id"])
    assert config["name"] == "玄思 Agent"
    assert config["model_name"] == "Qwen-Max"
    assert agent["name"] == "玄思升级版"
    assert agent["model"] == "Qwen-Max"

    updated_config = client.patch(
        f"/api/matches/match_001/agents/configs/{config['id']}",
        json={"provider_type": "rest_api", "model_name": "Qwen-Plus", "model_kind": "closed_source", "endpoint": "http://127.0.0.1:8100"},
    )
    assert updated_config.status_code == 200
    data = updated_config.json()["data"]
    speaker = next(item for item in data["speakers"] if item["id"] == "spk_aff_2")
    agent = next(item for item in data["agent_status"] if item["speaker_id"] == "spk_aff_2")
    assert speaker["model_name"] == "Qwen-Plus"
    assert speaker["model_kind"] == "closed_source"
    assert speaker["agent_endpoint"] == "http://127.0.0.1:8100"
    assert agent["model"] == "Qwen-Plus"


def test_agent_config_crud_and_speaker_binding() -> None:
    initial = client.get("/api/matches/match_001")
    assert initial.status_code == 200
    data = initial.json()["data"]
    assert len(data["agent_configs"]) == 4
    assert all("api_key" not in item for item in data["agent_configs"])

    created = client.post(
        "/api/matches/match_001/agents/configs",
        json={
            "name": "共享测试 Agent",
            "provider_type": "rest_api",
            "model_name": "Shared-Agent-Model",
            "model_kind": "closed_source",
            "endpoint": "http://127.0.0.1:8199",
            "timeout_ms": 12000,
            "enabled": True,
        },
    )
    assert created.status_code == 200
    data = created.json()["data"]
    config = next(item for item in data["agent_configs"] if item["name"] == "共享测试 Agent")
    assert config["endpoint"] == "http://127.0.0.1:8199"

    bound = client.patch(
        "/api/matches/match_001/speakers/spk_neg_3",
        json={"agent_config_id": config["id"]},
    )
    assert bound.status_code == 200
    data = bound.json()["data"]
    speaker = next(item for item in data["speakers"] if item["id"] == "spk_neg_3")
    agent = next(item for item in data["agent_status"] if item["speaker_id"] == "spk_neg_3")
    assert speaker["agent_config_id"] == config["id"]
    assert speaker["model_name"] == "Shared-Agent-Model"
    assert speaker["agent_endpoint"] == "http://127.0.0.1:8199"
    assert agent["agent_config_id"] == config["id"]

    disabled = client.patch(
        f"/api/matches/match_001/agents/configs/{config['id']}",
        json={"enabled": False},
    )
    assert disabled.status_code == 200
    data = disabled.json()["data"]
    speaker = next(item for item in data["speakers"] if item["id"] == "spk_neg_3")
    assert speaker["model_name"] == "Shared-Agent-Model"
    config = next(item for item in data["agent_configs"] if item["id"] == config["id"])
    assert config["enabled"] is False

    health = client.post("/api/matches/match_001/agent/spk_neg_3/health")
    assert health.status_code == 200
    assert health.json()["data"]["result"]["status"] == "disabled"
    agent = next(item for item in health.json()["data"]["snapshot"]["agent_status"] if item["speaker_id"] == "spk_neg_3")
    assert agent["status"] == "failed"

    delete_bound = client.delete(f"/api/matches/match_001/agents/configs/{config['id']}")
    assert delete_bound.status_code == 409
    assert delete_bound.json()["error"]["code"] == "agent_config_in_use"


def test_speaker_settings_patch_switches_human_and_agent_type() -> None:
    missing_config = client.patch(
        "/api/matches/match_001/speakers/spk_aff_1",
        json={"speaker_type": "agent", "name": "启明"},
    )
    assert missing_config.status_code == 409
    assert missing_config.json()["error"]["code"] == "agent_config_required"

    created = client.post(
        "/api/matches/match_001/agents/configs",
        json={
            "name": "启明 Agent",
            "provider_type": "rest_api",
            "model_name": "GLM-Test",
            "model_kind": "closed_source",
            "endpoint": "http://127.0.0.1:8123",
            "timeout_ms": 12000,
            "enabled": True,
        },
    )
    assert created.status_code == 200
    config = next(item for item in created.json()["data"]["agent_configs"] if item["name"] == "启明 Agent")

    to_agent = client.patch(
        "/api/matches/match_001/speakers/spk_aff_1",
        json={
            "speaker_type": "agent",
            "name": "启明",
            "agent_config_id": config["id"],
        },
    )
    assert to_agent.status_code == 200
    data = to_agent.json()["data"]
    speaker = next(item for item in data["speakers"] if item["id"] == "spk_aff_1")
    agent = next(item for item in data["agent_status"] if item["speaker_id"] == "spk_aff_1")
    assert speaker["speaker_type"] == "agent"
    assert speaker["model_name"] == "GLM-Test"
    assert speaker["model_kind"] == "closed_source"
    assert speaker["agent_endpoint"] == "http://127.0.0.1:8123"
    assert speaker["agent_config_id"] == config["id"]
    assert speaker["mic_permission"] is None
    assert agent["name"] == "启明"
    assert agent["model"] == "GLM-Test"

    to_human = client.patch(
        "/api/matches/match_001/speakers/spk_aff_1",
        json={"speaker_type": "human", "name": "陈思远"},
    )
    assert to_human.status_code == 200
    data = to_human.json()["data"]
    speaker = next(item for item in data["speakers"] if item["id"] == "spk_aff_1")
    assert speaker["speaker_type"] == "human"
    assert speaker["model_name"] is None
    assert speaker["model_kind"] is None
    assert "agent_endpoint" not in speaker
    assert speaker["mic_permission"] == "unknown"
    assert not any(item["speaker_id"] == "spk_aff_1" for item in data["agent_status"])


def test_phase_settings_patch_updates_fixed_phase_clock_when_started() -> None:
    response = client.patch(
        "/api/matches/match_001/phases/phase_aff_constructive_1",
        json={"name": "正方一辩立论（压缩版）", "duration_seconds": 120},
    )
    assert response.status_code == 200
    phase = next(item for item in response.json()["data"]["phases"] if item["id"] == "phase_aff_constructive_1")
    assert phase["name"] == "正方一辩立论（压缩版）"
    assert phase["duration_seconds"] == 120

    started = client.post("/api/matches/match_001/phases/phase_aff_constructive_1/start")
    assert started.status_code == 200
    clock = next(item for item in started.json()["data"]["clocks"] if item["name"] == "main")
    assert clock["total_seconds"] == 120
    assert clock["remaining_ms"] == 120000


def test_phase_settings_patch_updates_free_debate_clock_config() -> None:
    response = client.patch(
        "/api/matches/match_001/phases/phase_free_debate",
        json={"side_total_seconds": 300, "turn_seconds": 20},
    )
    assert response.status_code == 200
    data = response.json()["data"]
    phase = next(item for item in data["phases"] if item["id"] == "phase_free_debate")
    assert phase["duration_seconds"] == 600
    assert phase["side_total_seconds"] == 300
    assert phase["turn_seconds"] == 20

    clocks = {item["name"]: item for item in data["clocks"]}
    assert clocks["affirmative_total"]["total_seconds"] == 300
    assert clocks["negative_total"]["total_seconds"] == 300
    assert clocks["turn"]["total_seconds"] == 20

    invalid = client.patch(
        "/api/matches/match_001/phases/phase_free_debate",
        json={"turn_seconds": 2},
    )
    assert invalid.status_code == 409
    assert invalid.json()["error"]["code"] == "invalid_phase_config"


def test_speaker_start_and_stop_flow() -> None:
    start = client.post("/api/matches/match_001/speakers/spk_aff_3/start-speaking")
    assert start.status_code == 200
    assert start.json()["data"]["current_speech"]["speaker_id"] == "spk_aff_3"

    stop = client.post("/api/matches/match_001/speakers/spk_aff_3/stop-speaking")
    assert stop.status_code == 200
    data = stop.json()["data"]
    assert data["current_speech"] is None
    assert data["recent_transcript"][0]["speaker_id"] == "spk_aff_3"
    assert data["free_debate"]["current_turn_side"] == "negative"
    assert data["free_debate"]["turn_index"] == 15


def test_speaker_pause_resume_and_current_speech_reset_flow(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("PHDEBATE_AUDIO_DIR", str(tmp_path / "audio"))
    phase = client.post("/api/matches/match_001/phases/phase_free_debate/start")
    assert phase.status_code == 200

    start = client.post("/api/matches/match_001/speakers/spk_aff_3/start-speaking")
    assert start.status_code == 200
    start_data = start.json()["data"]
    speech_id = start_data["current_speech"]["id"]
    started_remaining = start_data["current_speech"]["started_clock_remaining_ms"]

    partial = client.post(
        "/api/matches/match_001/speakers/spk_aff_3/asr/partial",
        json={"text": "这段发言准备被重置", "latency_ms": 120},
    )
    assert partial.status_code == 200
    assert any(item["speech_id"] == speech_id for item in partial.json()["data"]["recent_transcript"])

    chunk = client.post(
        f"/api/matches/match_001/speeches/{speech_id}/audio-chunks",
        data={"speaker_id": "spk_aff_3", "chunk_index": "0", "duration_ms": "500"},
        files={"file": ("reset-target.pcm", b"pcm-to-reset", "audio/L16;rate=16000")},
    )
    assert chunk.status_code == 200
    assert chunk.json()["data"]["speech_id"] == speech_id

    adjusted_turn_ms = max(1000, int(started_remaining["turn"]) - 7000)
    adjusted_side_ms = max(1000, int(started_remaining["affirmative_total"]) - 9000)
    turn_adjust = client.post(
        "/api/matches/match_001/clocks/turn/adjust",
        json={"remaining_ms": adjusted_turn_ms, "reason": "unit_elapsed_turn"},
    )
    assert turn_adjust.status_code == 200
    side_adjust = client.post(
        "/api/matches/match_001/clocks/affirmative_total/adjust",
        json={"remaining_ms": adjusted_side_ms, "reason": "unit_elapsed_total"},
    )
    assert side_adjust.status_code == 200

    paused = client.post("/api/matches/match_001/speakers/spk_aff_3/pause-speaking", json={"reason": "unit_pause"})
    assert paused.status_code == 200
    data = paused.json()["data"]
    assert data["current_speech"]["state"] == "paused"
    assert next(item for item in data["clocks"] if item["name"] == "turn")["state"] == "paused"
    paused_turn_remaining = next(item for item in data["clocks"] if item["name"] == "turn")["remaining_ms"]
    assert 0 < paused_turn_remaining <= adjusted_turn_ms

    resumed = client.post("/api/matches/match_001/speakers/spk_aff_3/resume-speaking", json={"reason": "unit_resume"})
    assert resumed.status_code == 200
    data = resumed.json()["data"]
    assert data["current_speech"]["state"] == "speaking"
    assert next(item for item in data["clocks"] if item["name"] == "turn")["state"] == "running"

    reset = client.post("/api/matches/match_001/speeches/current/reset", json={"reason": "unit_reset"})
    assert reset.status_code == 200
    data = reset.json()["data"]
    assert data["current_speech"] is None
    assert not any(item["speech_id"] == speech_id for item in data["recent_transcript"])
    assert not any(item["speech_id"] == speech_id for item in data["audio_assets"])
    turn = next(item for item in data["clocks"] if item["name"] == "turn")
    affirmative_total = next(item for item in data["clocks"] if item["name"] == "affirmative_total")
    assert turn["state"] == "paused"
    assert turn["remaining_ms"] == started_remaining["turn"]
    assert affirmative_total["state"] == "paused"
    assert affirmative_total["remaining_ms"] == started_remaining["affirmative_total"]
    assert data["speech_service"]["asr"]["detail"] == "speech reset"
    logs = client.get("/api/matches/match_001/audit-logs?limit=10").json()["data"]["items"]
    assert any(item["action"] == "speech.reset" for item in logs)


def test_agent_manual_input_writes_transcript_and_advances_turn() -> None:
    phase = client.post("/api/matches/match_001/phases/phase_free_debate/start")
    assert phase.status_code == 200

    response = client.post(
        "/api/matches/match_001/agent/spk_aff_2/manual-input",
        json={"content": "人工代输入：AI 临场卡顿时由主持人代读。", "reason": "agent_timeout"},
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["current_speech"] is None
    assert data["free_debate"]["current_turn_side"] == "negative"
    assert data["recent_transcript"][0]["speaker_id"] == "spk_aff_2"
    assert data["recent_transcript"][0]["source"] == "manual"
    assert "主持人代读" in data["recent_transcript"][0]["text"]
    agent = next(item for item in data["agent_status"] if item["speaker_id"] == "spk_aff_2")
    assert agent["status"] == "ready"
    assert data["speech_service"]["tts"]["status"] == "idle"

    logs = client.get("/api/matches/match_001/audit-logs?limit=5").json()["data"]["items"]
    assert any(item["action"] == "agent.manual_input.accepted" for item in logs)


def test_agent_manual_input_rejects_human_speaker_and_empty_content() -> None:
    phase = client.post("/api/matches/match_001/phases/phase_free_debate/start")
    assert phase.status_code == 200

    empty = client.post("/api/matches/match_001/agent/spk_aff_2/manual-input", json={"content": "   "})
    assert empty.status_code == 409
    assert empty.json()["error"]["code"] == "invalid_manual_input"

    human = client.post("/api/matches/match_001/agent/spk_aff_3/manual-input", json={"content": "not allowed"})
    assert human.status_code == 409
    assert human.json()["error"]["code"] == "invalid_speaker"


def test_sqlite_snapshot_and_events_are_persisted() -> None:
    response = client.post(
        "/api/matches/match_001/screen/scene",
        json={"scene": "judge_commentary"},
    )
    assert response.status_code == 200
    seq = response.json()["data"]["last_seq"]

    db_path = store.repo.db_path
    assert db_path.exists()

    with sqlite3.connect(db_path) as conn:
        state_row = conn.execute(
            "SELECT value_json FROM app_state WHERE key = ?",
            ("demo_snapshot",),
        ).fetchone()
        event_row = conn.execute(
            "SELECT type, payload_json FROM events WHERE seq = ?",
            (seq,),
        ).fetchone()

    assert state_row is not None
    persisted = json.loads(state_row[0])
    assert persisted["match"]["screen_scene"] == "judge_commentary"

    assert event_row is not None
    assert event_row[0] == "screen.scene_changed"
    assert json.loads(event_row[1])["scene"] == "judge_commentary"


def test_audit_logs_can_be_queried_for_admin_actions() -> None:
    action = client.post(
        "/api/matches/match_001/screen/scene",
        json={"scene": "judge_commentary"},
    )
    assert action.status_code == 200

    response = client.get("/api/matches/match_001/audit-logs?limit=5")
    assert response.status_code == 200
    items = response.json()["data"]["items"]
    assert items
    assert items[0]["action"] == "screen.scene_changed"
    assert items[0]["actor_type"] == "host"
    assert items[0]["result"] == "success"
    assert items[0]["request"]["scene"] == "judge_commentary"

    request_logs = client.get("/api/matches/current/logs?limit=10")
    assert request_logs.status_code == 200
    audit_log = next(item for item in request_logs.json()["data"]["audit_logs"] if item["action"] == "screen.scene_changed")
    assert "request" not in audit_log
    assert "judge_commentary" in audit_log["request_preview"]

    detail = client.get(f"/api/matches/current/logs/audit/{audit_log['id']}")
    assert detail.status_code == 200
    assert detail.json()["data"]["request"]["scene"] == "judge_commentary"


def test_match_export_bundle_contains_core_files() -> None:
    client.post(
        "/api/matches/match_001/screen/scene",
        json={"scene": "judge_commentary"},
    )

    response = client.post("/api/matches/match_001/exports")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["download_url"].endswith(f"/{data['export_id']}/download")
    assert Path(data["file_path"]).exists()
    entry_paths = {item["path"] for item in data["entries"]}
    assert {
        "match.json",
        "phases.json",
        "phases.csv",
        "speeches.json",
        "speeches.csv",
        "transcript.json",
        "transcript.csv",
        "transcripts.json",
        "transcripts.csv",
        "events.jsonl",
        "agent_requests.jsonl",
        "speech_service_requests.jsonl",
        "votes.json",
        "audit_logs.jsonl",
        "audio_manifest.json",
        "structured/summary.json",
        "structured/matches.json",
        "structured/phases.json",
        "structured/slots.json",
        "structured/agent_configs.json",
        "structured/speeches.json",
        "structured/transcript_segments.json",
        "structured/votes.json",
        "structured/runtime_settings.json",
    }.issubset(entry_paths)

    download = client.get(data["download_url"])
    assert download.status_code == 200
    with zipfile.ZipFile(io.BytesIO(download.content)) as bundle:
        names = set(bundle.namelist())
        assert "match.json" in names
        assert "transcript.csv" in names
        exported_match = json.loads(bundle.read("match.json"))
        assert exported_match["match"]["id"] == "match_001"
        structured_summary = json.loads(bundle.read("structured/summary.json"))
        assert structured_summary["counts"]["phases"] == 10
        assert structured_summary["counts"]["slots"] == 8
        assert structured_summary["counts"]["agent_configs"] == 4
        assert structured_summary["counts"]["votes"] == 1
        assert structured_summary["counts"]["runtime_settings"] == 1
        assert "speech_service_requests" in structured_summary["counts"]
        speech_service_rows = [
            json.loads(line)
            for line in bundle.read("speech_service_requests.jsonl").decode("utf-8").splitlines()
            if line.strip()
        ]
        assert speech_service_rows == []
        structured_votes = json.loads(bundle.read("structured/votes.json"))
        assert structured_votes
        assert "vote_state_json" not in structured_votes[0]
        assert "audience_vote_keys" not in structured_votes[0]["vote_state"]
        assert "used_audience_tokens" not in structured_votes[0]["vote_state"]
        assert "audience_votes" not in structured_votes[0]["vote_state"]
        runtime_settings = json.loads(bundle.read("structured/runtime_settings.json"))
        assert runtime_settings[0]["key"] == "audio_output"
        assert runtime_settings[0]["value"]["mode"] == "host"
        phases_csv = bundle.read("phases.csv").decode("utf-8")
        speeches_csv = bundle.read("speeches.csv").decode("utf-8")
        transcripts_csv = bundle.read("transcripts.csv").decode("utf-8")
        assert phases_csv.startswith("match_id,id,phase_key")
        assert speeches_csv.startswith("match_id,speech_id,phase_id")
        assert transcripts_csv.startswith("match_id,id,speech_id")


def test_data_summary_reports_current_data_exports_and_archives() -> None:
    created = client.post("/api/matches/current/exports")
    assert created.status_code == 200
    export_id = created.json()["data"]["export_id"]

    summary = client.get("/api/matches/current/data-summary")
    assert summary.status_code == 200
    data = summary.json()["data"]
    assert data["match"]["id"] == "match_001"
    assert data["counts"]["speakers"] == 8
    assert data["counts"]["phases"] == 10
    assert data["counts"]["agent_configs"] == 4
    assert data["counts"]["events"] >= 1
    assert data["counts"]["audit_logs"] >= 1
    assert data["structured_counts"]["matches"] == 1
    assert data["structured_counts"]["phases"] == 10
    assert data["structured_counts"]["slots"] == 8
    assert data["structured_counts"]["agent_configs"] == 4
    assert data["structured_counts"]["transcript_segments"] == data["counts"]["transcript_segments"]
    assert data["structured_counts"]["votes"] == 1
    assert data["structured_counts"]["runtime_settings"] == 1
    assert data["counts"]["speech_service_requests"] == 0
    assert data["structured_counts"]["speech_service_requests"] == 0
    assert data["request_health"]["agent_status_counts"] == {}
    assert data["request_health"]["speech_service_status_counts"] == {}
    assert data["request_health"]["failed_agent_requests"] == []
    assert data["request_health"]["failed_speech_service_requests"] == []
    assert data["event_type_counts"]["export.created"] == 1
    assert data["recent_events"][0]["type"] == "export.created"
    assert data["recent_events"][0]["seq"] >= 1843
    assert "payload" not in data["recent_events"][0]
    assert data["latest_export"]["export_id"] == export_id
    assert data["latest_export"]["entry_count"] >= 6
    assert "file_path" not in data["latest_export"]
    latest_entry_paths = {item["path"] for item in data["latest_export"]["entries"]}
    assert {
        "match.json",
        "phases.json",
        "speeches.json",
        "transcripts.json",
        "votes.json",
        "events.jsonl",
        "audit_logs.jsonl",
        "audio_manifest.json",
        "structured/summary.json",
    }.issubset(latest_entry_paths)
    assert data["persistence"]["driver"] == "sqlite"

    reset = client.post("/api/matches/current/reset", json={"confirm_text": "重置比赛"})
    assert reset.status_code == 200
    archived_summary = client.get("/api/matches/current/data-summary")
    assert archived_summary.status_code == 200
    archived_data = archived_summary.json()["data"]
    assert archived_data["match"]["id"] != "match_001"
    assert archived_data["counts"]["archives"] >= 1
    assert archived_data["structured_counts"]["matches"] == 1
    assert archived_data["structured_counts"]["slots"] == 8
    assert archived_data["structured_counts"]["votes"] == 1
    archive = archived_data["archives"][0]
    assert archive["archived_match_id"] == "match_001"
    assert archive["export_bundle"]["download_url"]
    assert archive["export_bundle"]["entry_count"] >= 6
    archived_entry_paths = {item["path"] for item in archive["export_bundle"]["entries"]}
    assert "match.json" in archived_entry_paths
    assert "audio_manifest.json" in archived_entry_paths


def test_mock_agent_speech_records_final_transcript(monkeypatch) -> None:
    monkeypatch.setenv("PHDEBATE_TTS_FORMAL", "0")
    _use_embedded_mock_agent("spk_aff_2")

    asyncio.run(store.run_agent_speech("spk_aff_2"))

    # TTS disabled → speech waits for screen playback; finalize it manually
    mid = client.get("/api/matches/match_001").json()["data"]
    if mid["current_speech"]:
        asyncio.run(store.complete_agent_playback(mid["current_speech"]["id"], mid["current_speech"].get("tts_task_id") or ""))

    response = client.get("/api/matches/match_001")
    assert response.status_code == 200
    data = response.json()["data"]

    assert data["current_speech"] is None
    assert data["recent_transcript"][0]["speaker_id"] == "spk_aff_2"
    assert data["recent_transcript"][0]["source"] == "agent_text"
    assert "可执行" in data["recent_transcript"][0]["text"]
    assert data["free_debate"]["current_turn_side"] == "negative"


def test_agent_playback_completion_stops_voice_agent(monkeypatch) -> None:
    calls = []

    async def fake_start_voice_agent(payload):
        return {"ok": True, "payload": payload}

    async def fake_stop_voice_agent(payload):
        calls.append(payload)
        return {"ok": True, "payload": payload}

    monkeypatch.setenv("PHDEBATE_TTS_FORMAL", "0")
    monkeypatch.setenv("PHDEBATE_LIVEKIT_ENABLED", "1")
    monkeypatch.setattr("app.services.match_store.start_voice_agent", fake_start_voice_agent)
    monkeypatch.setattr("app.services.match_store.stop_voice_agent", fake_stop_voice_agent)
    _use_embedded_mock_agent("spk_aff_2")

    asyncio.run(store.run_agent_speech("spk_aff_2"))
    mid = client.get("/api/matches/match_001").json()["data"]
    if mid["current_speech"]:
        asyncio.run(store.complete_agent_playback(mid["current_speech"]["id"], mid["current_speech"].get("tts_task_id") or ""))

    assert calls
    assert calls[-1]["match_id"] == "match_001"
    assert calls[-1]["speaker_id"] == "spk_aff_2"
    assert calls[-1]["speech_id"]
    assert any(event["type"] == "voice_agent.stopped" for event in store.events)


def test_xiaoqi_match_record_push(monkeypatch) -> None:
    monkeypatch.setenv("PHDEBATE_TTS_FORMAL", "0")
    _use_embedded_mock_agent("spk_aff_2")
    asyncio.run(store.run_agent_speech("spk_aff_2"))
    mid = client.get("/api/matches/match_001").json()["data"]
    if mid["current_speech"]:
        asyncio.run(
            store.complete_agent_playback(mid["current_speech"]["id"], mid["current_speech"].get("tts_task_id") or "")
        )

    # match_record == debate_history shape: [{stage, message:[{speaker, content}]}]
    record = store.build_match_record()
    assert isinstance(record, list) and record, "finalized speech should appear in match_record"
    assert all(set(stage) == {"stage", "message"} for stage in record)
    first = record[0]
    assert isinstance(first["message"], list) and first["message"]
    assert set(first["message"][0]) == {"speaker", "content"}

    from app.services.xiaoqi_store import xiaoqi_store

    saved = {k: xiaoqi_store.config.get(k) for k in ("match_record_endpoint", "session_id")}
    try:
        # 1) no endpoint configured → not sent, but the full payload is built (default session)
        data = client.post("/api/matches/match_001/xiaoqi/match-record").json()["data"]
        assert data["sent"] is False
        assert data["payload"]["session_id"] == "default"
        assert data["payload"]["match_record"] == record

        # 2) config round-trips through PUT/GET; session_id flows into the payload
        put = client.put(
            "/api/admin/xiaoqi",
            json={
                "match_record_endpoint": "https://aitoys.example/celebration-api/v1/match_record/update",
                "session_id": "evt_demo",
            },
        )
        assert put.status_code == 200
        cfg = client.get("/api/admin/xiaoqi").json()["data"]
        assert cfg["match_record_endpoint"].endswith("/match_record/update")
        assert cfg["session_id"] == "evt_demo"

        # 3) with endpoint configured, the exact wire format is POSTed to 小七
        captured: dict = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={"code": 0, "message": "ok"})

        real_async_client = httpx.AsyncClient

        def fake_async_client(*_args, **kwargs):
            return real_async_client(transport=httpx.MockTransport(handler), timeout=kwargs.get("timeout"))

        monkeypatch.setattr("app.services.xiaoqi_store.httpx.AsyncClient", fake_async_client)

        sent = client.post("/api/matches/match_001/xiaoqi/match-record").json()["data"]
        assert sent["sent"] is True
        assert sent["status_code"] == 200
        assert captured["url"].endswith("/celebration-api/v1/match_record/update")
        assert captured["body"] == {"session_id": "evt_demo", "match_record": record}
    finally:
        xiaoqi_store.config.update(saved)


# ============================ 预取（提前生成 + 缓存）============================

def test_prefetch_self_intro_activation_uses_cache(monkeypatch) -> None:
    monkeypatch.setenv("PHDEBATE_TTS_FORMAL", "0")
    monkeypatch.setenv("PHDEBATE_PREFETCH_ENABLED", "1")
    _use_embedded_mock_agent("spk_aff_2")
    store._clear_prepared_speeches()

    # 1) 预取自我介绍 → 缓存就绪，使用独立 prep speech_id（绝不与 live speech_{seq} 撞）
    asyncio.run(store._prefetch_speech("spk_aff_2", "self_intro"))
    key = "self_intro:spk_aff_2"
    entry = store._prepared_speeches.get(key)
    assert entry and entry["status"] == "ready"
    assert entry["speech_id"].startswith("speech_prep_")
    assert entry["full_text"]

    # 2) 促活：run_agent_speech 命中缓存，绝不再发起 live agent 生成
    called = {"n": 0}
    orig_stream = store.agent_gateway.stream_speech

    def spy(*args, **kwargs):
        called["n"] += 1
        return orig_stream(*args, **kwargs)

    monkeypatch.setattr(store.agent_gateway, "stream_speech", spy)
    asyncio.run(store.run_agent_speech("spk_aff_2", mode="self_intro"))
    assert called["n"] == 0  # 未触发任何 live 生成
    assert key not in store._prepared_speeches  # 缓存已被消费
    store._clear_prepared_speeches()


def test_prefetch_phase_history_fingerprint_invalidation(monkeypatch) -> None:
    monkeypatch.setenv("PHDEBATE_TTS_FORMAL", "0")
    monkeypatch.setenv("PHDEBATE_PREFETCH_ENABLED", "1")
    _use_embedded_mock_agent("spk_aff_2")
    store._clear_prepared_speeches()
    saved_phase = store.snapshot["match"]["current_phase_id"]
    phase = next(p for p in store.snapshot["phases"] if p["id"] == "phase_aff_statement_2")
    key = "phase:phase_aff_statement_2:spk_aff_2"

    try:
        # 预取固定单人环节发言 → 就绪
        asyncio.run(store._prefetch_speech("spk_aff_2", "speech", phase))
        assert store._prepared_speeches[key]["status"] == "ready"

        # 当前环节=目标环节、history 未变 → 命中（返回缓存条目）
        store.snapshot["match"]["current_phase_id"] = "phase_aff_statement_2"
        taken = store._take_prepared_speech("spk_aff_2", "speech")
        assert taken is not None and taken["speech_id"].startswith("speech_prep_")

        # 重新预取后改变 debate_history → fingerprint 不匹配 → 回退 live（None）+ 丢弃失效条目
        asyncio.run(store._prefetch_speech("spk_aff_2", "speech", phase))
        assert store._prepared_speeches[key]["status"] == "ready"
        store.snapshot.setdefault("recent_transcript", []).append(
            {
                "id": "seg_fp_test", "speech_id": "sp_fp_test", "phase_id": "phase_aff_constructive_1",
                "speaker_id": "spk_aff_1", "side": "affirmative", "text": "历史变化测试",
                "valid": True, "is_final": True,
            }
        )
        assert store._take_prepared_speech("spk_aff_2", "speech") is None
        assert key not in store._prepared_speeches
    finally:
        store.snapshot["match"]["current_phase_id"] = saved_phase
        store.snapshot["recent_transcript"] = [
            s for s in store.snapshot.get("recent_transcript", []) if s.get("id") != "seg_fp_test"
        ]
        store._clear_prepared_speeches()


def test_prefetch_schedule_skips_free_debate_but_covers_single_speaker(monkeypatch) -> None:
    monkeypatch.setenv("PHDEBATE_PREFETCH_ENABLED", "1")
    calls: list = []

    async def fake_prefetch(speaker_id, mode, target_phase=None):
        calls.append((speaker_id, mode, (target_phase or {}).get("id")))

    monkeypatch.setattr(store, "_prefetch_speech", fake_prefetch)
    before_free = next(p for p in store.snapshot["phases"] if p["id"] == "phase_neg_statement_3")  # next = free_debate
    before_single = next(p for p in store.snapshot["phases"] if p["id"] == "phase_aff_constructive_1")  # next = neg_constructive_1

    async def run() -> None:
        store._schedule_next_phase_prefetch(before_free)    # 下一是自由辩论 → 不预取
        store._schedule_next_phase_prefetch(before_single)  # 下一是固定单人 agent → 预取
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(run())
    assert all(c[2] != "phase_free_debate" for c in calls)
    assert ("spk_neg_1", "speech", "phase_neg_constructive_1") in calls


def test_prefetch_disabled_falls_back_to_live(monkeypatch) -> None:
    monkeypatch.setenv("PHDEBATE_PREFETCH_ENABLED", "0")
    store._clear_prepared_speeches()
    asyncio.run(store._prefetch_all_self_intros())
    assert store._prepared_speeches == {}
    # 关闭时即便缓存里有条目，take 也返回 None（彻底回退 live）
    store._prepared_speeches["self_intro:spk_aff_2"] = {"status": "ready", "speech_id": "speech_prep_x"}
    assert store._take_prepared_speech("spk_aff_2", "self_intro") is None
    store._clear_prepared_speeches()


def test_debate_history_multi_message_phase_is_chronological() -> None:
    # 回归：自由辩论同一环节多条发言，必须按时间正序（最早在前）进入 debate_history。
    # recent_transcript 以"最新在前"存储——若不翻转，agent 会收到倒序的对话（最新一句跑到最前）。
    saved = store.snapshot.get("recent_transcript")
    try:
        store.snapshot["recent_transcript"] = [
            {"id": "seg_C", "speech_id": "C", "phase_id": "phase_free_debate", "speaker_id": "spk_neg_1",
             "text": "第三句·最新", "valid": True, "is_final": True},
            {"id": "seg_B", "speech_id": "B", "phase_id": "phase_free_debate", "speaker_id": "spk_aff_2",
             "text": "第二句", "valid": True, "is_final": True},
            {"id": "seg_A", "speech_id": "A", "phase_id": "phase_free_debate", "speaker_id": "spk_neg_1",
             "text": "第一句·最早", "valid": True, "is_final": True},
        ]
        history = store._build_debate_history()
        free = next(st for st in history if st["stage"] == "自由辩论")
        assert [m["content"] for m in free["message"]] == ["第一句·最早", "第二句", "第三句·最新"]
    finally:
        store.snapshot["recent_transcript"] = saved


def test_xiaoqi_store_prunes_deprecated_keys(tmp_path, monkeypatch) -> None:
    import app.services.xiaoqi_store as xs

    # 模拟旧版持久化文件：含已废弃的 result_endpoint / result_template。
    f = tmp_path / "xiaoqi.json"
    f.write_text(
        json.dumps(
            {
                "match_record_endpoint": "https://aitoys.example/celebration-api/v1/match_record/update",
                "session_id": "evt_old",
                "result_endpoint": "https://old/result",
                "result_template": {"winner": "{winner}"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(xs, "_under_pytest", lambda: False)
    store_obj = xs.XiaoqiStore(path=f)
    cfg = store_obj.public()
    assert "result_endpoint" not in cfg and "result_template" not in cfg
    assert cfg["match_record_endpoint"].endswith("/match_record/update")
    assert cfg["session_id"] == "evt_old"
    # 持久化文件已被回写清理。
    on_disk = json.loads(f.read_text(encoding="utf-8"))
    assert "result_endpoint" not in on_disk and "result_template" not in on_disk


def test_agent_gateway_parses_sse_delta_and_final() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/speech"
        content = (
            'data: {"type":"delta","task_id":"task_test","delta":"第一句，"}\n\n'
            'data: {"type":"final","task_id":"task_test","content":"第一句，第二句。","usage":{"model":"mock","latency_ms":12}}\n\n'
        )
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=content)

    async def run() -> list[dict]:
        gateway = AgentGateway(transport=httpx.MockTransport(handler))
        return [
            event
            async for event in gateway.stream_speech(
                "http://agent.local",
                {"task_id": "task_test", "output": {"stream": True}},
                [],
            )
        ]

    events = asyncio.run(run())
    assert events[0]["type"] == "delta"
    assert events[0]["delta"] == "第一句，"
    assert events[1]["type"] == "final"
    assert events[1]["content"] == "第一句，第二句。"


def test_agent_gateway_health_accepts_speech_only_explicit_endpoint() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/debate/health"
        return httpx.Response(404, json={"detail": "Not Found"})

    gateway = AgentGateway(transport=httpx.MockTransport(handler))
    result = asyncio.run(gateway.health("http://agent.local/api/debate"))

    assert result["ok"] is True
    assert result["status"] == "speech_only"
    assert result["endpoint"] == "http://agent.local/api/debate"


def test_agent_payload_sends_model_id_not_display_name() -> None:
    speaker = next(item for item in store.snapshot["speakers"] if item["id"] == "spk_aff_2")
    config = next(item for item in store.snapshot["agent_configs"] if item["id"] == speaker["agent_config_id"])
    assert config["model_name"] == "Qwen-Max"
    assert config["model_id"] == "qwen3.6-plus"

    payload = store._build_agent_payload("task_model", "speech_model", speaker)
    intro_payload = store._build_self_intro_payload("task_intro", "speech_intro", speaker)

    assert payload["model_name"] == "qwen3.6-plus"
    assert payload["request_model"] == "qwen3.6-plus"
    assert payload["model_display_name"] == "Qwen-Max"
    assert intro_payload["model_name"] == "qwen3.6-plus"
    assert intro_payload["request_model"] == "qwen3.6-plus"
    assert intro_payload["model_display_name"] == "Qwen-Max"
    assert intro_payload["target_chars"] == 20
    assert intro_payload["max_token"] == 20
    assert intro_payload["other_info"]["char_budget"] == 20


def test_mock_agent_standard_endpoints() -> None:
    mock_client = TestClient(load_mock_agent_app())
    health = mock_client.get("/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ready"

    speech = mock_client.post(
        "/speech",
        json={
            "task_id": "task_mock",
            "side": "affirmative",
            "phase_type": "free_debate",
            "target_chars": 40,
            "output": {"stream": False},
        },
    )
    assert speech.status_code == 200
    body = speech.json()
    assert body["status"] == "completed"
    assert body["task_id"] == "task_mock"
    assert body["content"]

    interrupt = mock_client.post("/interrupt", json={"task_id": "task_mock", "reason": "test"})
    assert interrupt.status_code == 200
    assert interrupt.json()["status"] == "interrupted"


def test_agent_speech_can_use_injected_gateway(monkeypatch) -> None:
    monkeypatch.setenv("PHDEBATE_TTS_FORMAL", "0")

    class FakeGateway:
        def endpoint_for(self, speaker):
            return "http://fake-agent"

        async def stream_speech(self, endpoint, payload, fallback_chunks, *, config=None):
            assert endpoint == "http://fake-agent"
            assert payload["speaker_id"] == "spk_aff_2"
            yield {"type": "delta", "task_id": payload["task_id"], "delta": "外部 Agent "}
            yield {"type": "delta", "task_id": payload["task_id"], "delta": "流式接入成功。"}
            yield {"type": "final", "task_id": payload["task_id"], "content": "外部 Agent 流式接入成功。"}

    original = store.agent_gateway
    store.agent_gateway = FakeGateway()
    try:
        asyncio.run(store.run_agent_speech("spk_aff_2"))
    finally:
        store.agent_gateway = original

    # TTS disabled → speech waits for screen playback; finalize it manually
    mid = client.get("/api/matches/match_001").json()["data"]
    if mid["current_speech"]:
        asyncio.run(store.complete_agent_playback(mid["current_speech"]["id"], mid["current_speech"].get("tts_task_id") or ""))

    data = client.get("/api/matches/match_001").json()["data"]
    assert data["recent_transcript"][0]["text"] == "外部 Agent 流式接入成功。"
    assert next(item for item in data["agent_status"] if item["speaker_id"] == "spk_aff_2")["status"] == "ready"
    agent_requests = store.repo.load_agent_requests("match_001", 10)
    assert len(agent_requests) == 1
    assert agent_requests[0]["task_id"].startswith("task_")
    assert agent_requests[0]["speech_id"] == data["recent_transcript"][0]["speech_id"]
    assert agent_requests[0]["speaker_id"] == "spk_aff_2"
    assert agent_requests[0]["endpoint"] == "http://fake-agent"
    assert agent_requests[0]["status"] == "completed"
    assert agent_requests[0]["response_text"] == "外部 Agent 流式接入成功。"
    assert agent_requests[0]["request"]["speaker_id"] == "spk_aff_2"

    request_logs = client.get("/api/matches/current/logs?limit=10")
    assert request_logs.status_code == 200
    agent_log = request_logs.json()["data"]["agent_requests"][0]
    assert agent_log["status"] == "completed"
    assert agent_log["response_preview"] == "外部 Agent 流式接入成功。"
    assert "request" not in agent_log
    assert "response_text" not in agent_log

    detail = client.get(f"/api/matches/current/logs/agent/{agent_log['id']}")
    assert detail.status_code == 200
    detail_data = detail.json()["data"]
    assert detail_data["request"]["speaker_id"] == "spk_aff_2"
    assert detail_data["response_text"] == "外部 Agent 流式接入成功。"

    export = client.post("/api/matches/current/exports")
    assert export.status_code == 200
    export_data = export.json()["data"]
    export_rows = store.repo.load_export_bundles("match_001", 10)
    assert export_rows[0]["export_id"] == export_data["export_id"]
    assert export_rows[0]["entry_count"] == len(export_data["entries"])

    download = client.get(export_data["download_url"])
    assert download.status_code == 200
    with zipfile.ZipFile(io.BytesIO(download.content)) as bundle:
        rows = [
            json.loads(line)
            for line in bundle.read("agent_requests.jsonl").decode("utf-8").splitlines()
            if line.strip()
        ]
        assert rows[0]["status"] == "completed"
        assert rows[0]["response_text"] == "外部 Agent 流式接入成功。"
        assert rows[0]["request"]["speaker_id"] == "spk_aff_2"

    summary = client.get("/api/matches/current/data-summary")
    assert summary.status_code == 200
    summary_data = summary.json()["data"]
    assert summary_data["counts"]["agent_requests"] == 1
    assert summary_data["counts"]["export_bundles"] == 1
    assert summary_data["structured_counts"]["agent_requests"] == 1
    assert summary_data["structured_counts"]["export_bundles"] == 1
    assert summary_data["latest_export"]["export_id"] == export_data["export_id"]


def test_agent_gateway_failure_records_request_and_allows_manual_fallback(monkeypatch) -> None:
    class FailingGateway:
        def endpoint_for(self, speaker):
            return "http://broken-agent"

        async def stream_speech(self, endpoint, payload, fallback_chunks, *, config=None):
            assert endpoint == "http://broken-agent"
            assert payload["speaker_id"] == "spk_aff_2"
            if False:
                yield {}
            raise AgentGatewayError("agent_timeout", "Agent 请求超时。", {"endpoint": endpoint})

    original = store.agent_gateway
    store.agent_gateway = FailingGateway()
    try:
        asyncio.run(store.run_agent_speech("spk_aff_2"))
    finally:
        store.agent_gateway = original

    failed_snapshot = client.get("/api/matches/match_001").json()["data"]
    failed_agent = next(item for item in failed_snapshot["agent_status"] if item["speaker_id"] == "spk_aff_2")
    assert failed_agent["status"] == "failed"
    assert failed_snapshot["speech_service"]["tts"]["status"] == "failed"
    assert failed_snapshot["speech_service"]["tts"]["degraded_to"] == "manual_input"
    assert failed_snapshot["current_speech"]["speaker_id"] == "spk_aff_2"

    agent_requests = store.repo.load_agent_requests("match_001", 10)
    assert len(agent_requests) == 1
    assert agent_requests[0]["status"] == "failed"
    assert agent_requests[0]["endpoint"] == "http://broken-agent"
    assert agent_requests[0]["error_code"] == "agent_timeout"
    assert agent_requests[0]["error_message"] == "Agent 请求超时。"

    fallback = client.post(
        "/api/matches/match_001/agent/spk_aff_2/manual-input",
        json={"content": "人工接管后完成 AI 发言。", "reason": "agent_failed_fallback"},
    )
    assert fallback.status_code == 200
    recovered = fallback.json()["data"]
    assert recovered["current_speech"] is None
    assert recovered["recent_transcript"][0]["text"] == "人工接管后完成 AI 发言。"
    assert next(item for item in recovered["agent_status"] if item["speaker_id"] == "spk_aff_2")["status"] == "ready"

    export = client.post("/api/matches/current/exports")
    assert export.status_code == 200
    export_data = export.json()["data"]
    with zipfile.ZipFile(io.BytesIO(client.get(export_data["download_url"]).content)) as bundle:
        rows = [
            json.loads(line)
            for line in bundle.read("agent_requests.jsonl").decode("utf-8").splitlines()
            if line.strip()
        ]
        assert rows[0]["status"] == "failed"
        assert rows[0]["error_code"] == "agent_timeout"
        assert rows[0]["error_message"] == "Agent 请求超时。"
        assert rows[0]["request"]["speaker_id"] == "spk_aff_2"

    summary = client.get("/api/matches/current/data-summary")
    assert summary.status_code == 200
    summary_data = summary.json()["data"]
    assert summary_data["counts"]["agent_requests"] == 1
    assert summary_data["structured_counts"]["agent_requests"] == 1
    assert summary_data["latest_export"]["export_id"] == export_data["export_id"]
    health = summary_data["request_health"]
    assert health["agent_status_counts"]["failed"] == 1
    assert health["failed_agent_requests"][0]["speaker_id"] == "spk_aff_2"
    assert health["failed_agent_requests"][0]["error_code"] == "agent_timeout"
    assert "request" not in health["failed_agent_requests"][0]


def test_agent_speech_formal_tts_archives_audio(monkeypatch, tmp_path) -> None:
    class FakeGateway:
        def __init__(self, url: str) -> None:
            self.url = url

        async def synthesize(self, text: str) -> TTSResult:
            # 首段提前切分后，一段发言会被切成多句；每句都应被合成、归档（不再假设整段=一句）。
            return TTSResult(audio=b"agent-mp3", mime_type="audio/mpeg", latency_ms=321, chunk_count=1)

    monkeypatch.setenv("PHDEBATE_TTS_FORMAL", "1")
    monkeypatch.setenv("PHDEBATE_AUDIO_DIR", str(tmp_path / "audio"))
    # 关闭首句前置静音，使本测试可对"合成字节 == 归档字节"做精确断言（静音前缀另有专测）。
    monkeypatch.setenv("PHDEBATE_TTS_LEAD_SILENCE", "0")
    _patch_tts_selection(monkeypatch, FakeGateway("wss://fake-tts"), provider="xfyun")
    _use_embedded_mock_agent("spk_aff_2")

    asyncio.run(store.run_agent_speech("spk_aff_2"))

    # TTS succeeded → speech waits for screen playback; get speech_id before finalization
    mid = client.get("/api/matches/match_001").json()["data"]
    speech_id = mid["current_speech"]["id"] if mid["current_speech"] else None
    task_id = (mid["current_speech"] or {}).get("tts_task_id") or ""

    # Verify audio archived and service request recorded before finalization
    mid_assets = mid["audio_assets"]
    assert len(mid_assets) >= 1
    asset = mid_assets[0]
    assert asset["speaker_id"] == "spk_aff_2"
    assert asset["source"] == "agent_tts"
    assert asset["status"] == "completed"
    assert asset["mime_type"] == "audio/mpeg"
    assert len(asset["chunks"]) >= 1
    assert asset["size_bytes"] == len(b"agent-mp3") * len(asset["chunks"])  # 每句一个分片
    assert asset["chunks"][0]["chunk_index"] == 0
    assert asset["chunks"][0]["audio_url"].startswith("/api/audio/match_001/")
    assert Path(asset["chunks"][0]["file_path"]).read_bytes() == b"agent-mp3"
    requests = store.repo.load_speech_service_requests("match_001", 10)
    assert len(requests) >= 1  # 首段提前切分 → 一段发言可能产生多条合成请求
    assert all(r["service"] == "tts" and r["operation"] == "agent_synthesis" for r in requests)
    assert all(r["speech_id"] == asset["speech_id"] and r["status"] == "completed" for r in requests)
    assert all(r["speaker_id"] == "spk_aff_2" for r in requests)
    assert requests[0]["response"]["size_bytes"] == len(b"agent-mp3")
    assert requests[0]["latency_ms"] == 321

    # Finalize playback (normally done by screen reporting playback complete)
    if speech_id:
        asyncio.run(store.complete_agent_playback(speech_id, task_id))

    data = client.get("/api/matches/match_001").json()["data"]
    assert data["current_speech"] is None
    assert data["audio_assets"][0]["speech_id"] == data["recent_transcript"][0]["speech_id"]
    assert data["speech_service"]["tts"]["status"] == "idle"
    assert data["speech_service"]["tts"]["latency_ms"] == 321


def test_agent_speech_formal_tts_failure_keeps_text_transcript(monkeypatch) -> None:
    class FailingGateway:
        def __init__(self, url: str) -> None:
            self.url = url

        async def synthesize(self, text: str) -> TTSResult:
            raise XfyunGatewayError("tts unavailable", code=500)

    monkeypatch.setenv("PHDEBATE_TTS_FORMAL", "1")
    _patch_tts_selection(monkeypatch, FailingGateway("wss://fake-tts"), provider="xfyun")
    _use_embedded_mock_agent("spk_aff_2")

    asyncio.run(store.run_agent_speech("spk_aff_2"))

    data = client.get("/api/matches/match_001").json()["data"]
    assert data["recent_transcript"][0]["speaker_id"] == "spk_aff_2"
    assert "可执行" in data["recent_transcript"][0]["text"]
    assert data["speech_service"]["tts"]["status"] == "failed"
    assert data["speech_service"]["tts"]["degraded_to"] == "text_only"
    assert data["current_speech"] is None
    requests = store.repo.load_speech_service_requests("match_001", 10)
    assert len(requests) >= 1  # 首段提前切分 → 可能多条；每条都应失败并降级为 text_only
    assert all(r["service"] == "tts" and r["operation"] == "agent_synthesis" for r in requests)
    assert all(r["status"] == "failed" for r in requests)
    assert all(r["error_code"] == "500" and r["error_message"] == "tts unavailable" for r in requests)
    assert all(r["response"]["degraded_to"] == "text_only" for r in requests)


def test_agent_health_check_updates_agent_status() -> None:
    class FakeHealthGateway:
        def endpoint_for(self, speaker):
            return "http://fake-agent"

        async def health(self, endpoint):
            assert endpoint == "http://fake-agent"
            return {"ok": True, "status": "ready", "model": "fake-ready", "version": "test", "latency_ms": 17}

    original = store.agent_gateway
    store.agent_gateway = FakeHealthGateway()
    try:
        response = client.post("/api/matches/match_001/agent/spk_aff_2/health")
    finally:
        store.agent_gateway = original

    assert response.status_code == 200
    data = response.json()["data"]["snapshot"]
    agent = next(item for item in data["agent_status"] if item["speaker_id"] == "spk_aff_2")
    assert agent["status"] == "ready"
    assert agent["model"] == "fake-ready"
    assert agent["latency_ms"] == 17
    assert agent["endpoint"] == "http://fake-agent"
    assert agent["last_health_at"]


def test_agent_health_check_marks_gateway_failure_without_blocking_match() -> None:
    class FailingHealthGateway:
        def endpoint_for(self, speaker):
            return "http://fake-agent"

        async def health(self, endpoint):
            raise AgentGatewayError("agent_unavailable", "Agent 健康检查失败。", {"endpoint": endpoint})

    original = store.agent_gateway
    store.agent_gateway = FailingHealthGateway()
    try:
        response = client.post("/api/matches/match_001/agent/spk_aff_2/health")
    finally:
        store.agent_gateway = original

    assert response.status_code == 200
    data = response.json()["data"]["snapshot"]
    assert data["match"]["status"] == "running"
    agent = next(item for item in data["agent_status"] if item["speaker_id"] == "spk_aff_2")
    assert agent["status"] == "failed"
    assert agent["detail"] == "Agent 健康检查失败。"


def test_fixed_phase_rejects_wrong_speaker() -> None:
    phase = client.post("/api/matches/match_001/phases/phase_aff_constructive_1/start")
    assert phase.status_code == 200

    response = client.post("/api/matches/match_001/speakers/spk_neg_1/activate")
    assert response.status_code == 409
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "invalid_speaker"


def test_free_debate_turn_rejects_wrong_side_after_switch() -> None:
    stop = client.post("/api/matches/match_001/speakers/spk_aff_3/stop-speaking")
    assert stop.status_code == 200
    assert stop.json()["data"]["free_debate"]["current_turn_side"] == "negative"

    wrong_side = client.post("/api/matches/match_001/speakers/spk_aff_3/start-speaking")
    assert wrong_side.status_code == 409
    assert wrong_side.json()["error"]["code"] == "invalid_speaker"

    right_side = client.post("/api/matches/match_001/speakers/spk_neg_2/start-speaking")
    assert right_side.status_code == 200
    assert right_side.json()["data"]["current_speech"]["speaker_id"] == "spk_neg_2"


def test_multi_match_create_list_switch_delete() -> None:
    base = client.get("/api/matches")
    assert base.status_code == 200
    assert any(m["id"] == "match_001" and m["active"] for m in base.json()["data"]["matches"])

    created = client.post("/api/matches", json={"title": "测试场B", "topic": "话题B"})
    assert created.status_code == 200
    new_id = created.json()["data"]["match_id"]
    assert new_id != "match_001"

    listed = client.get("/api/matches").json()["data"]
    assert listed["active_match_id"] == new_id
    ids = {m["id"]: m for m in listed["matches"]}
    assert ids[new_id]["active"] is True and ids[new_id]["title"] == "测试场B"
    assert ids["match_001"]["active"] is False

    switched = client.post("/api/matches/match_001/switch")
    assert switched.status_code == 200
    assert switched.json()["data"]["match"]["id"] == "match_001"

    # 删除当前比赛：自动切到剩下的另一场（manual match control，需求：可清理比赛）
    deleted_active = client.delete("/api/matches/match_001")
    assert deleted_active.status_code == 200
    after = deleted_active.json()["data"]
    assert all(m["id"] != "match_001" for m in after["matches"])
    assert after["active_match_id"] == new_id

    # 删除最后一场 → 回到"空白起步"（无比赛，须手动新建）
    deleted_last = client.delete(f"/api/matches/{new_id}")
    assert deleted_last.status_code == 200
    final = deleted_last.json()["data"]
    assert all(m["id"] != new_id for m in final["matches"])
    assert final["active_match_id"] == ""


def test_fresh_start_is_blank_and_manual_create_works() -> None:
    # 模拟全新启动：进入"空白起步"无比赛状态（不自动预置 demo）。
    async def go_blank():
        async with store._lock:
            store.seq = 0
            store.events = []
            store.snapshot = store._empty_snapshot()
            store._ensure_runtime_fields()
            store._persist_snapshot()

    asyncio.run(go_blank())

    # 无比赛：current 可读、match.id 为空、无名单、列表为空。
    data = client.get("/api/matches/current").json()["data"]
    assert data["match"]["id"] == ""
    assert data["speakers"] == []
    listed = client.get("/api/matches").json()["data"]
    assert listed["active_match_id"] == ""
    assert listed["matches"] == []

    # 手动新建：即便此前无比赛，也能用默认名单模板建出一场并成为 active。
    created = client.post("/api/matches", json={"title": "我的比赛", "topic": "我的辩题"})
    assert created.status_code == 200
    new_id = created.json()["data"]["match_id"]
    assert new_id != ""
    snap = client.get("/api/matches/current").json()["data"]
    assert snap["match"]["id"] == new_id
    assert snap["match"]["title"] == "我的比赛"
    assert len(snap["speakers"]) == 8  # 默认 4+4 名单
    relisted = client.get("/api/matches").json()["data"]
    assert relisted["active_match_id"] == new_id


def test_integration_config_get_patch_toggle_and_redacts_secrets() -> None:
    import os

    saved = {k: os.environ.get(k) for k in ("XFYUN_ASR_URL", "XFYUN_TTS_URL", "XFYUN_API_KEY", "XFYUN_TTS_VOICE", "XFYUN_ASR_LANG")}
    try:
        base = client.get("/api/matches/match_001/integration-config")
        assert base.status_code == 200
        assert set(base.json()["data"].keys()) == {"asr", "tts", "voice_presets"}
        assert len(base.json()["data"]["voice_presets"]) >= 3

        patched = client.patch(
            "/api/matches/match_001/integration-config",
            json={
                "asr": {"enabled": False},
                "tts": {
                    "enabled": True,
                    "provider": "xfyun",
                    "endpoint": "wss://example/tts",
                    "voice": "x6_lingxiaoxuan_pro",
                    "secrets": {"api_key": "SECRET_K"},
                },
            },
        )
        assert patched.status_code == 200
        data = patched.json()["data"]
        assert data["asr"]["enabled"] is False
        assert data["tts"]["enabled"] is True
        assert data["tts"]["provider"] == "xfyun"
        assert data["tts"]["endpoint"] == "wss://example/tts"
        assert data["tts"]["voice"] == "x6_lingxiaoxuan_pro"
        assert data["tts"]["secrets"]["api_key"] == {"configured": True, "redacted": "********"}
        assert "SECRET_K" not in json.dumps(data)  # never echo plaintext

        # disabled ASR clears the env URL so the gateway degrades
        assert os.environ.get("XFYUN_ASR_URL") == ""
        assert os.environ.get("XFYUN_TTS_URL") == "wss://example/tts"

        snap = client.get("/api/matches/match_001").json()["data"]
        assert snap["integration_config"]["tts"]["enabled"] is True
    finally:
        from app.services.integration_config import integration_config

        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        integration_config.config = integration_config._seed_from_env()
        integration_config._apply_to_env()


def test_free_debate_single_turn_defaults_to_15_seconds(monkeypatch) -> None:
    monkeypatch.setenv("PHDEBATE_FREE_DEBATE_DECISION_SECONDS", "99")
    asyncio.run(store.start_phase("phase_free_debate"))
    snapshot = client.get("/api/matches/match_001").json()["data"]
    turn_clock = next(clock for clock in snapshot["clocks"] if clock["name"] == "turn")
    assert turn_clock["total_seconds"] == 15


def test_free_debate_clocks_start_for_ruleset_generated_phase_id(monkeypatch) -> None:
    monkeypatch.setenv("PHDEBATE_FREE_DEBATE_DECISION_SECONDS", "99")
    free_phase = next(item for item in store.snapshot["phases"] if item["id"] == "phase_free_debate")
    free_phase["id"] = "phase_5_phase_5"
    free_phase["phase_key"] = "phase_5"

    started_phase = client.post("/api/matches/match_001/phases/phase_5_phase_5/start")
    assert started_phase.status_code == 200, started_phase.text
    started_speech = client.post("/api/matches/match_001/speakers/spk_aff_1/start-speaking")
    assert started_speech.status_code == 200, started_speech.text

    data = started_speech.json()["data"]
    clocks = {item["name"]: item for item in data["clocks"]}
    assert data["match"]["live_mode"] == "free"
    assert clocks["turn"]["state"] == "running"
    assert clocks["affirmative_total"]["state"] == "running"
    assert clocks["negative_total"]["state"] == "paused"


def test_single_speaker_phase_follows_current_role_assignment() -> None:
    _use_embedded_mock_agent("spk_aff_2")
    converted = client.patch(
        "/api/matches/match_001/speakers/spk_aff_1",
        json={"speaker_type": "agent", "agent_config_id": "agent_spk_aff_2"},
    )
    assert converted.status_code == 200, converted.text
    started = client.post("/api/matches/match_001/start")
    assert started.status_code == 200, started.text
    phase = client.post("/api/matches/match_001/phases/phase_aff_constructive_1/start")
    assert phase.status_code == 200, phase.text

    human_start = client.post("/api/matches/match_001/speakers/spk_aff_1/start-speaking")
    assert human_start.status_code == 409
    assert human_start.json()["error"]["code"] == "invalid_speaker"
    active = store.snapshot.get("current_speech") or {}
    if active.get("speaker_id") != "spk_aff_1":
        store.ensure_agent_speaker_for_current_phase("spk_aff_1")

    client.post("/api/matches/match_001/speeches/current/reset", json={"reason": "test_role_switch"})
    reverted = client.patch("/api/matches/match_001/speakers/spk_aff_1", json={"speaker_type": "human"})
    assert reverted.status_code == 200, reverted.text
    agent_start = client.post("/api/matches/match_001/speakers/spk_aff_1/start-agent-speaking")
    assert agent_start.status_code == 409
    assert agent_start.json()["error"]["code"] == "invalid_speaker"
    human_start = client.post("/api/matches/match_001/speakers/spk_aff_1/start-speaking")
    assert human_start.status_code == 200, human_start.text
    assert human_start.json()["data"]["current_speech"]["speaker_id"] == "spk_aff_1"


def test_free_debate_all_agent_side_auto_handles_without_human_skip(monkeypatch) -> None:
    monkeypatch.setenv("PHDEBATE_FREE_DEBATE_DECISION_SECONDS", "0.02")
    _use_embedded_mock_agent("spk_aff_2")
    _use_embedded_mock_agent("spk_aff_4")
    for speaker_id, config_id in (("spk_aff_1", "agent_spk_aff_2"), ("spk_aff_3", "agent_spk_aff_4")):
        response = client.patch(
            f"/api/matches/match_001/speakers/{speaker_id}",
            json={"speaker_type": "agent", "agent_config_id": config_id},
        )
        assert response.status_code == 200, response.text

    async def scenario():
        await store.start_phase("phase_free_debate")
        await asyncio.sleep(0.08)
        return await store.get_snapshot()

    data = asyncio.run(scenario())
    assert data["free_debate"]["current_turn_side"] == "affirmative"
    assert data["free_debate"].get("auto_handled", {}).get("affirmative_1") in {
        "spk_aff_1",
        "spk_aff_2",
        "spk_aff_3",
        "spk_aff_4",
    }


def test_free_debate_agent_pending_keeps_dual_countdown_mode(monkeypatch) -> None:
    monkeypatch.setenv("PHDEBATE_TTS_FORMAL", "0")

    async def slow_stream(_endpoint, _payload, _fallback, config=None):
        await asyncio.sleep(0.2)
        yield {"type": "final", "content": "自由辩论中，AI 接管发言，继续回应对方观点。"}

    monkeypatch.setattr(store.agent_gateway, "stream_speech", slow_stream)

    async def scenario():
        await store.start_phase("phase_free_debate")
        task = asyncio.create_task(store.run_agent_speech("spk_aff_2"))
        try:
            for _ in range(20):
                snap = await store.get_snapshot()
                if snap.get("current_speech"):
                    return snap
                await asyncio.sleep(0.01)
            return await store.get_snapshot()
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    data = asyncio.run(scenario())
    assert data["match"]["live_mode"] == "free"
    assert data["current_speech"]["speaker_id"] == "spk_aff_2"
    assert data["current_speech"]["state"] == "thinking"
    assert {clock["name"] for clock in data["clocks"]} == {"affirmative_total", "turn", "negative_total"}


def test_free_debate_one_side_total_exhausted_other_side_continues(monkeypatch) -> None:
    monkeypatch.setenv("PHDEBATE_FREE_DEBATE_DECISION_SECONDS", "99")
    phase = client.post("/api/matches/match_001/phases/phase_free_debate/start")
    assert phase.status_code == 200
    start = client.post("/api/matches/match_001/speakers/spk_aff_3/start-speaking")
    assert start.status_code == 200

    adjusted = client.post(
        "/api/matches/match_001/clocks/affirmative_total/adjust",
        json={"remaining_ms": 0, "reason": "unit_total_timeout"},
    )
    assert adjusted.status_code == 200

    emitted = asyncio.run(store.tick_timers())
    assert [event["type"] for event in emitted] == ["clock.expired", "speech.timeout", "speech.ended"]

    data = client.get("/api/matches/match_001").json()["data"]
    assert data["flow"]["awaiting_host_confirm"] is False
    assert data["free_debate"]["current_turn_side"] == "negative"
    assert next(clock for clock in data["clocks"] if clock["name"] == "affirmative_total")["remaining_ms"] == 0
    assert next(clock for clock in data["clocks"] if clock["name"] == "negative_total")["remaining_ms"] > 0

    next_start = client.post("/api/matches/match_001/speakers/spk_neg_2/start-speaking")
    assert next_start.status_code == 200
    assert next_start.json()["data"]["current_speech"]["speaker_id"] == "spk_neg_2"


def test_free_debate_idle_exhausted_current_side_reconciles_to_remaining_side(monkeypatch) -> None:
    monkeypatch.setenv("PHDEBATE_FREE_DEBATE_DECISION_SECONDS", "99")
    asyncio.run(store.start_phase("phase_free_debate"))

    async def seed_idle_state() -> None:
        async with store._lock:
            store.snapshot["current_speech"] = None
            store.snapshot["free_debate"]["current_turn_side"] = "affirmative"
            store.snapshot["free_debate"]["turn_index"] = 7
            for clock in store.snapshot["clocks"]:
                if clock["name"] == "affirmative_total":
                    clock["remaining_ms"] = 0
                    clock["state"] = "paused"
                    clock["deadline_at"] = None
                elif clock["name"] == "negative_total":
                    clock["remaining_ms"] = 3500
                    clock["state"] = "paused"
                    clock["deadline_at"] = None
                elif clock["name"] == "turn":
                    clock["remaining_ms"] = 0
                    clock["state"] = "expired"
                    clock["deadline_at"] = None
                    clock["expired_notified_at"] = "2026-06-17T00:00:00Z"
            store._persist_snapshot()

    asyncio.run(seed_idle_state())
    emitted = asyncio.run(store.tick_timers())

    event_types = [event["type"] for event in emitted]
    assert "free_debate.reconciled" in event_types
    assert emitted[-1]["type"] == "free_debate.reconciled"
    data = client.get("/api/matches/match_001").json()["data"]
    assert data["current_speech"] is None
    assert data["flow"]["awaiting_host_confirm"] is False
    assert data["free_debate"]["current_turn_side"] == "negative"
    assert data["free_debate"]["turn_index"] == 8
    turn = next(clock for clock in data["clocks"] if clock["name"] == "turn")
    assert turn["state"] == "paused"
    assert turn["remaining_ms"] == turn["total_seconds"] * 1000


def test_free_debate_idle_both_sides_exhausted_finishes_phase(monkeypatch) -> None:
    monkeypatch.setenv("PHDEBATE_FREE_DEBATE_DECISION_SECONDS", "99")
    asyncio.run(store.start_phase("phase_free_debate"))

    async def seed_idle_state() -> None:
        async with store._lock:
            store.snapshot["current_speech"] = None
            store.snapshot["free_debate"]["current_turn_side"] = "negative"
            store.snapshot["free_debate"]["turn_index"] = 12
            for clock in store.snapshot["clocks"]:
                if clock["name"] in {"affirmative_total", "negative_total", "turn"}:
                    clock["remaining_ms"] = 0
                    clock["state"] = "paused"
                    clock["deadline_at"] = None
                    clock.pop("expired_notified_at", None)
            store._persist_snapshot()

    asyncio.run(seed_idle_state())
    emitted = asyncio.run(store.tick_timers())

    event_types = [event["type"] for event in emitted]
    assert "free_debate.reconciled" in event_types
    assert "flow.awaiting_host_confirm" in event_types
    data = client.get("/api/matches/match_001").json()["data"]
    assert data["current_speech"] is None
    assert data["flow"]["awaiting_host_confirm"] is True
    assert data["flow"]["next_action"] == "phase_next"
    assert data["flow"]["reason"] == "clock_expired"


def test_free_debate_all_skip_triggers_random_agent(monkeypatch) -> None:
    # Keep the decision timer from firing so we isolate the all-skip path.
    monkeypatch.setenv("PHDEBATE_FREE_DEBATE_DECISION_SECONDS", "99")
    _use_embedded_mock_agent("spk_aff_2")
    _use_embedded_mock_agent("spk_aff_4")

    async def scenario():
        # 处于本方决定窗口（无人在发言）：本方两位人类都跳过 → 立即随机 AI 接管。
        async with store._lock:
            store.snapshot["current_speech"] = None
            store._persist_snapshot()
        await store.record_free_debate_skip("spk_aff_1")
        return await store.record_free_debate_skip("spk_aff_3")

    snapshot = asyncio.run(scenario())
    auto_handled = snapshot["free_debate"].get("auto_handled", {})
    assert auto_handled, "all-skip should mark the turn auto-handled"
    assert any(value in {"spk_aff_2", "spk_aff_4"} for value in auto_handled.values())


def test_free_debate_decision_timeout_triggers_agent(monkeypatch) -> None:
    monkeypatch.setenv("PHDEBATE_FREE_DEBATE_DECISION_SECONDS", "0.05")
    monkeypatch.setenv("PHDEBATE_TTS_FORMAL", "0")
    _use_embedded_mock_agent("spk_aff_2")
    _use_embedded_mock_agent("spk_aff_4")

    async def scenario():
        await store.start_phase("phase_free_debate")  # arms 0.05s decision timer for affirmative turn 1
        await asyncio.sleep(2.0)  # allow timer to fire and the embedded mock agent to finish

    asyncio.run(scenario())
    summary = client.get("/api/matches/match_001/data-summary").json()["data"]
    assert summary["event_type_counts"].get("free_debate.auto_agent", 0) >= 1


def test_free_debate_decision_window_defaults_to_five_seconds(monkeypatch) -> None:
    # 自由辩论给人留 5s 抢麦时间（超时/全预跳过才由随机 AI 接管）。
    monkeypatch.delenv("PHDEBATE_FREE_DEBATE_DECISION_SECONDS", raising=False)
    asyncio.run(store.start_phase("phase_free_debate"))
    assert store._free_debate_decision_seconds() == 5.0


def test_free_debate_speech_end_auto_advances_without_host_confirm(monkeypatch) -> None:
    # 决定窗口设大，隔离掉 auto-agent，专测"轮内结束=全自动、不 awaiting_host_confirm"。
    monkeypatch.setenv("PHDEBATE_FREE_DEBATE_DECISION_SECONDS", "99")

    async def scenario():
        await store.start_phase("phase_free_debate")  # 正方 turn 1
        await store.start_speaking("spk_aff_1")
        await store.stop_speaking("spk_aff_1")

    asyncio.run(scenario())
    data = client.get("/api/matches/match_001").json()["data"]
    assert data["current_speech"] is None
    assert data["free_debate"]["current_turn_side"] == "negative"  # 自动翻面
    assert data["free_debate"]["turn_index"] == 2
    assert data["flow"]["awaiting_host_confirm"] is False  # 不需主持确认


def test_free_debate_pre_skip_records_for_next_turn(monkeypatch) -> None:
    monkeypatch.setenv("PHDEBATE_FREE_DEBATE_DECISION_SECONDS", "99")

    async def scenario():
        await store.start_phase("phase_free_debate")  # 正方 turn 1（对方正在/即将发言）
        # 反方（下一方）在对方轮预点跳过 → 记到 negative_2
        await store.record_free_debate_skip("spk_neg_2")

    asyncio.run(scenario())
    fd = client.get("/api/matches/match_001").json()["data"]["free_debate"]
    assert "spk_neg_2" in fd.get("skip_votes", {}).get("negative_2", [])
    assert "negative_1" not in fd.get("skip_votes", {})  # 没有错记到当前轮


def test_free_debate_all_pre_skip_triggers_ai_immediately(monkeypatch) -> None:
    # 决定窗口设大：若仅靠 2s 计时则永不触发；本测只能由"全预跳过→翻面立即接管"使其发生。
    monkeypatch.setenv("PHDEBATE_FREE_DEBATE_DECISION_SECONDS", "99")
    monkeypatch.setenv("PHDEBATE_TTS_FORMAL", "0")
    _use_embedded_mock_agent("spk_neg_1")
    _use_embedded_mock_agent("spk_neg_3")

    async def scenario():
        await store.start_phase("phase_free_debate")  # 正方 turn 1
        # 对方发言期间，反方两位人类都预点跳过 turn 2
        await store.record_free_debate_skip("spk_neg_2")
        await store.record_free_debate_skip("spk_neg_4")
        # 正方人类发言并结束 → 翻面到反方 turn 2 → 全预跳过 → 立即 AI（不等 2s）
        await store.start_speaking("spk_aff_1")
        await store.stop_speaking("spk_aff_1")
        await asyncio.sleep(0.4)  # 放行"立即接管"的后台任务

    asyncio.run(scenario())
    fd = client.get("/api/matches/match_001").json()["data"]["free_debate"]
    assert fd["current_turn_side"] == "negative"
    assert fd["turn_index"] == 2
    assert fd.get("auto_handled", {}).get("negative_2") in {"spk_neg_1", "spk_neg_3"}


def test_free_debate_human_start_cancels_auto_agent(monkeypatch) -> None:
    monkeypatch.setenv("PHDEBATE_FREE_DEBATE_DECISION_SECONDS", "0.1")
    monkeypatch.setenv("PHDEBATE_TTS_FORMAL", "0")
    _use_embedded_mock_agent("spk_aff_2")
    _use_embedded_mock_agent("spk_aff_4")

    async def scenario():
        await store.start_phase("phase_free_debate")  # 正方 turn 1，0.1s 决定窗口
        await store.start_speaking("spk_aff_1")  # 人类在窗口内开始发言
        await asyncio.sleep(0.5)  # 让 0.1s 决定计时到点

    asyncio.run(scenario())
    data = client.get("/api/matches/match_001").json()["data"]
    assert data["current_speech"]["speaker_id"] == "spk_aff_1"  # 人类在说，AI 未接管
    assert "affirmative_1" not in data["free_debate"].get("auto_handled", {})


def test_agent_retry_rejects_invalid_phase_speaker() -> None:
    phase = client.post("/api/matches/match_001/phases/phase_aff_constructive_1/start")
    assert phase.status_code == 200

    response = client.post("/api/matches/match_001/agent/spk_aff_2/retry")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "invalid_speaker"


def test_agent_speaker_console_can_request_authorized_agent_speech() -> None:
    phase = client.post("/api/matches/match_001/phases/phase_aff_statement_2/start")
    assert phase.status_code == 200

    response = client.post("/api/matches/match_001/speakers/spk_aff_2/start-agent-speaking")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["match"]["id"] == "match_001"
    assert data["last_seq"] > 1842
    summary = client.get("/api/matches/match_001/data-summary")
    assert summary.status_code == 200
    assert summary.json()["data"]["event_type_counts"]["agent.speech.requested"] == 1


def test_force_agent_start_clears_bad_history_audio_and_resets_clock(monkeypatch) -> None:
    monkeypatch.setenv("PHDEBATE_TTS_FORMAL", "0")
    _use_embedded_mock_agent("spk_aff_2")
    phase = client.post("/api/matches/match_001/phases/phase_aff_statement_2/start")
    assert phase.status_code == 200

    async def seed_bad_state() -> None:
        async with store._lock:
            bad_speech = {
                "id": "speech_bad_aff_2",
                "phase_id": "phase_aff_statement_2",
                "speaker_id": "spk_aff_2",
                "side": "affirmative",
                "turn_index": 1,
                "source": "agent_text",
                "content_final": "错误的正方二辩历史。",
                "content_partial": "错误的正方二辩历史。",
                "started_at": "2026-06-17T00:00:00Z",
                "state": "ended",
            }
            store._upsert_transcript_segment(bad_speech, "spk_aff_2", "错误的正方二辩历史。", True, "agent_text")
            store.snapshot.setdefault("audio_assets", []).insert(
                0,
                {
                    "id": "audio_speech_bad_aff_2",
                    "match_id": "match_001",
                    "phase_id": "phase_aff_statement_2",
                    "speech_id": "speech_bad_aff_2",
                    "speaker_id": "spk_aff_2",
                    "file_path": "/tmp/bad",
                    "mime_type": "audio/mpeg",
                    "duration_ms": 1000,
                    "size_bytes": 2048,
                    "chunk_count": 1,
                    "status": "ready",
                    "chunks": [{"chunk_index": 0, "audio_url": "/bad.mp3"}],
                },
            )
            for clock in store.snapshot["clocks"]:
                if clock["name"] == "main":
                    clock["remaining_ms"] = 1234
                    clock["state"] = "expired"
                    clock["deadline_at"] = None
            store.snapshot["flow"].update({"awaiting_host_confirm": True, "speech_id": "speech_bad_aff_2"})
            store._persist_snapshot()

    asyncio.run(seed_bad_state())
    response = client.post(
        "/api/matches/match_001/speakers/spk_aff_2/start-agent-speaking",
        json={"force": True, "reason": "unit_force_restart"},
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert all("错误的正方二辩历史" not in item.get("text", "") for item in data["recent_transcript"])
    assert all(item.get("speech_id") != "speech_bad_aff_2" for item in data["audio_assets"])
    main_clock = next(clock for clock in data["clocks"] if clock["name"] == "main")
    assert main_clock["remaining_ms"] == main_clock["total_seconds"] * 1000
    assert main_clock["state"] == "paused"
    assert data["flow"]["awaiting_host_confirm"] is False
    summary = client.get("/api/matches/match_001/data-summary")
    assert summary.json()["data"]["event_type_counts"]["agent.force_restart"] == 1


def test_agent_speaker_console_rejects_human_and_locked_speech() -> None:
    human = client.post("/api/matches/match_001/speakers/spk_aff_3/start-agent-speaking")
    assert human.status_code == 409
    assert human.json()["error"]["code"] == "invalid_speaker"

    start_human = client.post("/api/matches/match_001/speakers/spk_aff_3/start-speaking")
    assert start_human.status_code == 200
    locked = client.post("/api/matches/match_001/speakers/spk_aff_2/start-agent-speaking")
    assert locked.status_code == 409
    assert locked.json()["error"]["code"] == "speaker_locked"


def test_request_ai_teammate_is_deferred_for_mvp() -> None:
    before = client.get("/api/matches/match_001").json()["data"]
    response = client.post(
        "/api/matches/match_001/speakers/spk_aff_3/request-ai-teammate",
        json={"agent_speaker_id": "spk_aff_2"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "feature_deferred"
    after = client.get("/api/matches/match_001").json()["data"]
    assert after["current_speech"] == before["current_speech"]
    assert after["match"]["live_mode"] == before["match"]["live_mode"]
    assert after["last_seq"] == before["last_seq"]


def test_speaker_websocket_heartbeat_updates_console_status() -> None:
    with client.websocket_connect("/ws/matches/match_001?channel=speaker&speaker_id=spk_aff_3") as websocket:
        snapshot = websocket.receive_json()
        assert snapshot["type"] == "snapshot"
        websocket.send_json(
            {
                "type": "speaker.heartbeat",
                "payload": {
                    "speaker_id": "spk_aff_3",
                    "mic_permission": "granted",
                    "device_label": "Test microphone",
                },
            }
        )
        event = websocket.receive_json()
        assert event["type"] == "speaker.heartbeat"
        data = client.get("/api/matches/match_001").json()["data"]
        speaker = next(item for item in data["speakers"] if item["id"] == "spk_aff_3")
        assert speaker["status"] == "online"
        assert speaker["mic_permission"] == "granted"
        assert speaker["device_label"] == "Test microphone"
        assert data["speech_service"]["consoles"]["online"] == 4


def test_speaker_websocket_mic_error_updates_admin_observability() -> None:
    with client.websocket_connect("/ws/matches/match_001?channel=speaker&speaker_id=spk_aff_3") as websocket:
        websocket.receive_json()
        websocket.send_json(
            {
                "type": "speaker.mic_error",
                "payload": {
                    "speaker_id": "spk_aff_3",
                    "mic_permission": "denied",
                    "device_label": "Test microphone",
                    "message": "permission denied",
                },
            }
        )
        event = websocket.receive_json()
        assert event["type"] == "speaker.mic_error"
        data = client.get("/api/matches/match_001").json()["data"]
        speaker = next(item for item in data["speakers"] if item["id"] == "spk_aff_3")
        assert speaker["status"] == "mic_error"
        assert speaker["mic_permission"] == "denied"
        assert data["speech_service"]["consoles"]["online"] == 4
        assert data["speech_service"]["consoles"]["mic_errors"][0]["speaker_id"] == "spk_aff_3"


def test_clock_pause_resume_and_adjust_flow() -> None:
    paused = client.post(
        "/api/matches/match_001/clocks/turn/pause",
        json={"reason": "test_pause"},
    )
    assert paused.status_code == 200
    data = paused.json()["data"]
    turn = next(item for item in data["clocks"] if item["name"] == "turn")
    assert turn["state"] == "paused"
    assert turn["deadline_at"] is None
    assert 0 <= turn["remaining_ms"] <= 15000

    resumed = client.post(
        "/api/matches/match_001/clocks/turn/resume",
        json={"reason": "test_resume"},
    )
    assert resumed.status_code == 200
    turn = next(item for item in resumed.json()["data"]["clocks"] if item["name"] == "turn")
    assert turn["state"] == "running"
    assert turn["deadline_at"] is not None

    adjusted = client.post(
        "/api/matches/match_001/clocks/turn/adjust",
        json={"remaining_ms": 12000, "reason": "test_adjust"},
    )
    assert adjusted.status_code == 200
    turn = next(item for item in adjusted.json()["data"]["clocks"] if item["name"] == "turn")
    assert turn["state"] == "running"
    assert 11000 <= turn["remaining_ms"] <= 12000
    assert turn["deadline_at"] is not None


def test_clock_adjust_zero_expires_and_resume_rejects() -> None:
    adjusted = client.post(
        "/api/matches/match_001/clocks/turn/adjust",
        json={"remaining_ms": 0, "reason": "test_expire"},
    )
    assert adjusted.status_code == 200
    turn = next(item for item in adjusted.json()["data"]["clocks"] if item["name"] == "turn")
    assert turn["state"] == "expired"
    assert turn["remaining_ms"] == 0

    resumed = client.post("/api/matches/match_001/clocks/turn/resume")
    assert resumed.status_code == 409
    assert resumed.json()["error"]["code"] == "clock_expired"


def test_timer_tick_emits_expiry_and_auto_ends_current_speech() -> None:
    phase = client.post("/api/matches/match_001/phases/phase_free_debate/start")
    assert phase.status_code == 200
    start = client.post("/api/matches/match_001/speakers/spk_aff_3/start-speaking")
    assert start.status_code == 200
    speech_id = start.json()["data"]["current_speech"]["id"]

    adjusted = client.post(
        "/api/matches/match_001/clocks/turn/adjust",
        json={"remaining_ms": 0, "reason": "unit_timeout"},
    )
    assert adjusted.status_code == 200

    emitted = asyncio.run(store.tick_timers())
    # 需求 5.md：自由辩论单轮钟到点属于"轮内切换"，全自动翻面进入对方 2s 窗口——不再等主持确认。
    assert [event["type"] for event in emitted] == [
        "clock.expired",
        "speech.timeout",
        "speech.ended",
    ]

    data = client.get("/api/matches/match_001").json()["data"]
    assert data["current_speech"] is None
    assert data["recent_transcript"][0]["speech_id"] == speech_id
    assert data["free_debate"]["current_turn_side"] == "negative"
    assert data["flow"]["awaiting_host_confirm"] is False  # 自动，无需主持确认

    emitted_again = asyncio.run(store.tick_timers())
    assert emitted_again == []


def test_timer_tick_cuts_off_agent_tts_playback_at_timeout() -> None:
    phase = client.post("/api/matches/match_001/phases/phase_free_debate/start")
    assert phase.status_code == 200

    async def seed_agent_tts_speech() -> None:
        async with store._lock:
            store.snapshot["current_speech"] = {
                "id": "speech_agent_tts",
                "phase_id": "phase_free_debate",
                "speaker_id": "spk_aff_2",
                "side": "affirmative",
                "turn_index": 1,
                "source": "agent_text",
                "content_final": "",
                "content_partial": "AI TTS 正在播放",
                "started_at": "2026-06-17T00:00:00Z",
                "state": "speaking",
                "tts_task_id": "task_agent_tts",
            }
            store._start_relevant_clocks("affirmative")
            store._persist_snapshot()

    asyncio.run(seed_agent_tts_speech())

    adjusted = client.post(
        "/api/matches/match_001/clocks/turn/adjust",
        json={"remaining_ms": 0, "reason": "unit_timeout"},
    )
    assert adjusted.status_code == 200

    emitted = asyncio.run(store.tick_timers())
    # 自由辩论单轮钟到点：自动翻面，不再追加 flow.awaiting_host_confirm 事件。
    assert [event["type"] for event in emitted] == ["clock.expired", "speech.timeout", "speech.ended"]
    assert emitted[1]["payload"]["task_id"] == "task_agent_tts"
    assert emitted[2]["payload"]["reason"] == "timeout"
    assert emitted[2]["payload"]["task_id"] == "task_agent_tts"

    data = client.get("/api/matches/match_001").json()["data"]
    assert data["current_speech"] is None
    assert data["recent_transcript"][0]["speech_id"] == "speech_agent_tts"
    assert data["speech_service"]["tts"]["status"] == "idle"
    assert data["speech_service"]["tts"]["detail"] == "timeout"
    assert data["flow"]["awaiting_host_confirm"] is False  # 自由辩论轮内自动，无需主持确认


def test_tts_playback_progress_updates_current_speech() -> None:
    async def seed_agent_tts_speech() -> None:
        async with store._lock:
            store.snapshot["current_speech"] = {
                "id": "speech_agent_tts_progress",
                "phase_id": "phase_constructive_aff",
                "speaker_id": "spk_aff_2",
                "side": "affirmative",
                "turn_index": 1,
                "source": "agent_text",
                "content_final": "",
                "content_partial": "AI TTS 正在播放",
                "started_at": "2026-06-17T00:00:00Z",
                "state": "speaking",
                "tts_task_id": "task_agent_tts_progress",
                "tts_expected_sentences": 4,
                "tts_created_sentences": 4,
            }
            store._persist_snapshot()

    asyncio.run(seed_agent_tts_speech())

    response = client.post(
        "/api/matches/match_001/speeches/speech_agent_tts_progress/tts/playback-progress",
        json={"task_id": "task_agent_tts_progress", "sentence_idx": 2},
    )

    assert response.status_code == 200
    data = response.json()["data"]
    speech = data["current_speech"]
    assert speech["tts_playing_sentence_idx"] == 2
    assert speech["tts_played_sentences"] == 3
    assert data["speech_service"]["tts"]["queue_size"] == 1


def test_tts_playback_progress_error_records_skipped_sentence() -> None:
    async def seed_agent_tts_speech() -> None:
        async with store._lock:
            store.snapshot["current_speech"] = {
                "id": "speech_agent_tts_error_progress",
                "phase_id": "phase_constructive_aff",
                "speaker_id": "spk_aff_2",
                "side": "affirmative",
                "turn_index": 1,
                "source": "agent_text",
                "content_final": "",
                "content_partial": "AI TTS 播放端卡住",
                "started_at": "2026-06-17T00:00:00Z",
                "state": "speaking",
                "tts_task_id": "task_agent_tts_error_progress",
                "tts_expected_sentences": 4,
                "tts_created_sentences": 4,
                "tts_skipped_sentences": [],
            }
            store._persist_snapshot()

    asyncio.run(seed_agent_tts_speech())

    response = client.post(
        "/api/matches/match_001/speeches/speech_agent_tts_error_progress/tts/playback-progress",
        json={"task_id": "task_agent_tts_error_progress", "sentence_idx": 2, "status": "error"},
    )

    assert response.status_code == 200
    data = response.json()["data"]
    speech = data["current_speech"]
    assert 2 in speech["tts_skipped_sentences"]
    assert speech["tts_last_playback_status"] == "error"
    assert data["speech_service"]["tts"]["detail"] == "screen playback error at segment 3/4"


def test_tts_playback_progress_played_last_segment_auto_completes_speech() -> None:
    async def seed_agent_tts_speech() -> None:
        async with store._lock:
            store.snapshot["current_speech"] = {
                "id": "speech_agent_tts_played_last",
                "phase_id": "phase_constructive_aff",
                "speaker_id": "spk_aff_2",
                "side": "affirmative",
                "turn_index": 1,
                "source": "agent_text",
                "content_final": "AI 发言最后一段已经播完。",
                "content_partial": "AI 发言最后一段已经播完。",
                "started_at": "2026-06-17T00:00:00Z",
                "state": "speaking",
                "tts_task_id": "task_agent_tts_played_last",
                "tts_expected_sentences": 3,
                "tts_created_sentences": 3,
                "tts_played_sentences": 2,
                "tts_skipped_sentences": [],
            }
            store._persist_snapshot()

    asyncio.run(seed_agent_tts_speech())

    response = client.post(
        "/api/matches/match_001/speeches/speech_agent_tts_played_last/tts/playback-progress",
        json={"task_id": "task_agent_tts_played_last", "sentence_idx": 2, "status": "played"},
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["current_speech"] is None
    assert data["speech_service"]["tts"]["status"] == "idle"
    assert store.events[-2]["type"] == "tts.playback_progress"
    assert store.events[-2]["payload"]["auto_complete"] is True
    assert store.events[-1]["type"] == "speech.ended"


def test_tts_playback_progress_out_of_order_played_does_not_auto_complete() -> None:
    async def seed_agent_tts_speech() -> None:
        async with store._lock:
            store.snapshot["current_speech"] = {
                "id": "speech_agent_tts_out_of_order_played",
                "phase_id": "phase_constructive_aff",
                "speaker_id": "spk_aff_2",
                "side": "affirmative",
                "turn_index": 1,
                "source": "agent_text",
                "content_final": "乱序播放进度不能误触发完成。",
                "content_partial": "乱序播放进度不能误触发完成。",
                "started_at": "2026-06-17T00:00:00Z",
                "state": "speaking",
                "tts_task_id": "task_agent_tts_out_of_order_played",
                "tts_expected_sentences": 3,
                "tts_created_sentences": 3,
                "tts_played_sentences": 0,
                "tts_played_sentence_indices": [],
                "tts_skipped_sentences": [],
            }
            store._persist_snapshot()

    asyncio.run(seed_agent_tts_speech())

    response = client.post(
        "/api/matches/match_001/speeches/speech_agent_tts_out_of_order_played/tts/playback-progress",
        json={"task_id": "task_agent_tts_out_of_order_played", "sentence_idx": 2, "status": "played"},
    )

    assert response.status_code == 200
    data = response.json()["data"]
    speech = data["current_speech"]
    assert speech is not None
    assert speech["tts_played_sentences"] == 3  # legacy high-water retained for UI compatibility
    assert speech["tts_played_sentence_indices"] == [2]
    assert data["speech_service"]["tts"]["queue_size"] == 2
    assert store.events[-1]["type"] == "tts.playback_progress"
    assert "auto_complete" not in store.events[-1]["payload"]


def test_tts_playback_progress_legacy_playing_high_water_does_not_auto_complete() -> None:
    async def seed_agent_tts_speech() -> None:
        async with store._lock:
            store.snapshot["current_speech"] = {
                "id": "speech_agent_tts_legacy_playing_highwater",
                "phase_id": "phase_constructive_aff",
                "speaker_id": "spk_aff_2",
                "side": "affirmative",
                "turn_index": 1,
                "source": "agent_text",
                "content_final": "旧快照最后一段只是正在播放，不能被当成完成。",
                "content_partial": "旧快照最后一段只是正在播放，不能被当成完成。",
                "started_at": "2026-06-17T00:00:00Z",
                "state": "speaking",
                "tts_task_id": "task_agent_tts_legacy_playing_highwater",
                "tts_expected_sentences": 3,
                "tts_created_sentences": 3,
                "tts_played_sentences": 3,
                "tts_playing_sentence_idx": 2,
                "tts_last_playback_status": "playing",
                "tts_skipped_sentences": [],
            }
            store._persist_snapshot()

    asyncio.run(seed_agent_tts_speech())

    response = client.post(
        "/api/matches/match_001/speeches/speech_agent_tts_legacy_playing_highwater/tts/playback-progress",
        json={"task_id": "task_agent_tts_legacy_playing_highwater", "sentence_idx": 2, "status": "playing"},
    )

    assert response.status_code == 200
    data = response.json()["data"]
    speech = data["current_speech"]
    assert speech is not None
    assert speech["tts_played_sentence_indices"] == [0, 1]
    assert speech["tts_played_sentences"] == 3
    assert data["speech_service"]["tts"]["queue_size"] == 0
    assert "auto_complete" not in store.events[-1]["payload"]


def test_tts_playback_progress_legacy_out_of_order_error_does_not_auto_complete() -> None:
    async def seed_agent_tts_speech() -> None:
        async with store._lock:
            store.snapshot["current_speech"] = {
                "id": "speech_agent_tts_legacy_out_of_order_error",
                "phase_id": "phase_constructive_aff",
                "speaker_id": "spk_aff_2",
                "side": "affirmative",
                "turn_index": 1,
                "source": "agent_text",
                "content_final": "旧快照乱序错误不能误触发完成。",
                "content_partial": "旧快照乱序错误不能误触发完成。",
                "started_at": "2026-06-17T00:00:00Z",
                "state": "speaking",
                "tts_task_id": "task_agent_tts_legacy_out_of_order_error",
                "tts_expected_sentences": 3,
                "tts_created_sentences": 3,
                "tts_played_sentences": 0,
                "tts_skipped_sentences": [],
            }
            store._persist_snapshot()

    asyncio.run(seed_agent_tts_speech())

    response = client.post(
        "/api/matches/match_001/speeches/speech_agent_tts_legacy_out_of_order_error/tts/playback-progress",
        json={"task_id": "task_agent_tts_legacy_out_of_order_error", "sentence_idx": 2, "status": "error"},
    )

    assert response.status_code == 200
    data = response.json()["data"]
    speech = data["current_speech"]
    assert speech is not None
    assert speech["tts_played_sentence_indices"] == []
    assert speech["tts_skipped_sentences"] == [2]
    assert data["speech_service"]["tts"]["queue_size"] == 2
    assert "auto_complete" not in store.events[-1]["payload"]


def test_tts_playback_progress_legacy_previous_played_then_last_stalled_auto_completes() -> None:
    async def seed_agent_tts_speech() -> None:
        async with store._lock:
            store.snapshot["current_speech"] = {
                "id": "speech_agent_tts_legacy_last_stalled",
                "phase_id": "phase_constructive_aff",
                "speaker_id": "spk_aff_2",
                "side": "affirmative",
                "turn_index": 1,
                "source": "agent_text",
                "content_final": "旧快照最后一段卡住也应自动结束。",
                "content_partial": "旧快照最后一段卡住也应自动结束。",
                "started_at": "2026-06-17T00:00:00Z",
                "state": "speaking",
                "tts_task_id": "task_agent_tts_legacy_last_stalled",
                "tts_expected_sentences": 3,
                "tts_created_sentences": 3,
                "tts_played_sentences": 2,
                "tts_skipped_sentences": [],
            }
            store._persist_snapshot()

    asyncio.run(seed_agent_tts_speech())

    response = client.post(
        "/api/matches/match_001/speeches/speech_agent_tts_legacy_last_stalled/tts/playback-progress",
        json={"task_id": "task_agent_tts_legacy_last_stalled", "sentence_idx": 2, "status": "stalled"},
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["current_speech"] is None
    assert store.events[-2]["payload"]["auto_complete"] is True
    assert store.events[-1]["type"] == "speech.ended"


def test_tts_playback_progress_stalled_last_segment_auto_completes_speech() -> None:
    async def seed_agent_tts_speech() -> None:
        async with store._lock:
            store.snapshot["current_speech"] = {
                "id": "speech_agent_tts_stalled_last",
                "phase_id": "phase_constructive_aff",
                "speaker_id": "spk_aff_2",
                "side": "affirmative",
                "turn_index": 1,
                "source": "agent_text",
                "content_final": "AI 发言最后一段播放端卡住。",
                "content_partial": "AI 发言最后一段播放端卡住。",
                "started_at": "2026-06-17T00:00:00Z",
                "state": "speaking",
                "tts_task_id": "task_agent_tts_stalled_last",
                "tts_expected_sentences": 2,
                "tts_created_sentences": 2,
                "tts_played_sentences": 1,
                "tts_skipped_sentences": [],
            }
            store._persist_snapshot()

    asyncio.run(seed_agent_tts_speech())

    response = client.post(
        "/api/matches/match_001/speeches/speech_agent_tts_stalled_last/tts/playback-progress",
        json={"task_id": "task_agent_tts_stalled_last", "sentence_idx": 1, "status": "stalled"},
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["current_speech"] is None
    ended = next(item for item in store.snapshot["recent_transcript"] if item["speech_id"] == "speech_agent_tts_stalled_last")
    assert ended["text"] == "AI 发言最后一段播放端卡住。"
    assert store.events[-2]["payload"]["auto_complete"] is True
    assert store.events[-1]["type"] == "speech.ended"


def test_tts_playback_resume_emits_event() -> None:
    async def seed_agent_tts_speech() -> None:
        async with store._lock:
            store.snapshot["current_speech"] = {
                "id": "speech_agent_tts_resume",
                "phase_id": "phase_constructive_aff",
                "speaker_id": "spk_aff_2",
                "side": "affirmative",
                "turn_index": 1,
                "source": "agent_text",
                "content_final": "",
                "content_partial": "AI TTS 正在播放",
                "started_at": "2026-06-17T00:00:00Z",
                "state": "speaking",
                "tts_task_id": "task_agent_tts_resume",
            }
            store._persist_snapshot()

    asyncio.run(seed_agent_tts_speech())

    response = client.post(
        "/api/matches/match_001/speeches/speech_agent_tts_resume/tts/playback-resume",
        json={"task_id": "task_agent_tts_resume"},
    )

    assert response.status_code == 200
    assert store.events[-1]["type"] == "tts.playback_resume_requested"
    assert store.events[-1]["payload"]["task_id"] == "task_agent_tts_resume"


def test_tts_playback_stop_emits_event_without_ending_speech() -> None:
    async def seed_agent_tts_speech() -> None:
        async with store._lock:
            store.snapshot["current_speech"] = {
                "id": "speech_agent_tts_stop",
                "phase_id": "phase_constructive_aff",
                "speaker_id": "spk_aff_2",
                "side": "affirmative",
                "turn_index": 1,
                "source": "agent_text",
                "content_final": "",
                "content_partial": "AI TTS 正在播放",
                "started_at": "2026-06-17T00:00:00Z",
                "state": "speaking",
                "tts_task_id": "task_agent_tts_stop",
            }
            store._persist_snapshot()

    asyncio.run(seed_agent_tts_speech())

    response = client.post(
        "/api/matches/match_001/speeches/speech_agent_tts_stop/tts/playback-stop",
        json={"task_id": "task_agent_tts_stop"},
    )

    assert response.status_code == 200
    assert store.events[-1]["type"] == "tts.playback_stop_requested"
    assert store.events[-1]["payload"]["task_id"] == "task_agent_tts_stop"
    # Pure audio control: the speech itself is untouched (not ended).
    assert store.snapshot["current_speech"]["id"] == "speech_agent_tts_stop"
    assert store.snapshot["current_speech"]["state"] == "speaking"


def test_streaming_tts_never_cuts_mid_sentence_after_first_segment() -> None:
    text = "我们首先要明确今天的争议焦点并不是技术本身是否有价值，而是它是否应该成为所有人必须掌握的基础能力，后续论证还在继续生成"

    # Without soft breaks (every segment past the first), a paragraph with no
    # sentence-ending punctuation yet must wait rather than be cut at a comma.
    segment, position = store._next_tts_sentence(text, 0, allow_soft_break=False)

    assert segment == ""
    assert position == 0


def test_agent_output_budget_is_deterministic_and_scales() -> None:
    base = store._agent_output_budget(180, 1.0)
    assert base["max_token"] >= 64
    assert base["target_chars"] >= 40
    assert base["raw_chars_per_second"] == 5.4
    assert base["screen_playback_rate"] == 1.0
    assert base["speech_time_factor"] == 0.78
    # Deterministic: same inputs → same output.
    assert store._agent_output_budget(180, 1.0) == base
    # More time allows more spoken content. The TTS speed parameter is measured
    # separately because Qwen speed is not linear enough to use for budgeting.
    assert store._agent_output_budget(360, 1.0)["max_token"] > base["max_token"]
    faster = store._agent_output_budget(180, 1.5)
    assert faster["speech_rate"] == 1.5
    assert faster["target_chars"] == base["target_chars"]


def test_agent_text_is_clamped_to_output_budget() -> None:
    payload = {"target_chars": 10}
    text = "第一句已经完整。第二句内容会明显超过预算，需要被截断。"

    clipped, clamped = store._clamp_agent_text_to_budget(text, payload)

    assert clamped is True
    assert clipped == "第一句已经完整。"

    hard_text = "这是一段没有任何标点但明显超过限制的长文本"
    clipped, clamped = store._clamp_agent_text_to_budget(hard_text, {"target_chars": 12})
    assert clamped is True
    assert clipped.endswith("。")
    assert len(clipped) <= 12


def test_agent_payload_carries_max_token_and_message_history() -> None:
    speaker = store._find_speaker("spk_aff_2")
    payload = store._build_agent_payload("task_budget", "speech_budget", speaker)

    assert payload["max_token"] >= 64
    assert payload["target_chars"] >= 40
    assert payload["max_len"] == payload["target_chars"]
    assert payload["max_length"] == payload["target_chars"]
    assert payload["other_info"]["speech_rate"] == 1.4
    assert payload["other_info"]["tts_volume"] == 70
    assert payload["other_info"]["speech_time_factor"] == 0.78
    assert payload["other_info"]["raw_chars_per_second"] == 5.4
    assert payload["other_info"]["screen_playback_rate"] == 1.0
    # debate_history matches 请求体(1).json: "message" key, speaker is side+seat only.
    for stage in payload["debate_history"]:
        assert "message" in stage and "content" not in stage
        for msg in stage["message"]:
            assert " · " not in msg["speaker"]


def test_match_update_syncs_positions_to_teams_and_brand_fields() -> None:
    response = client.patch(
        "/api/matches/match_001",
        json={
            "title": "同步测试赛",
            "affirmative_position": "立场甲",
            "negative_position": "立场乙",
            "title_display": "image",
            "organizer_display": "text",
        },
    )
    assert response.status_code == 200
    match = store.snapshot["match"]
    assert match["title"] == "同步测试赛"
    assert match["title_display"] == "image"
    assert match["organizer_display"] == "text"
    teams = {team["side"]: team for team in store.snapshot["teams"]}
    assert teams["affirmative"]["position"] == "立场甲"
    assert teams["negative"]["position"] == "立场乙"


def test_match_image_upload_sets_url_and_image_mode() -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 32
    response = client.post(
        "/api/matches/match_001/image/title",
        files={"file": ("title.png", png, "image/png")},
    )
    assert response.status_code == 200
    match = response.json()["data"]["match"]
    assert match["title_display"] == "image"
    assert match["title_image_url"].startswith("/api/files/match-images/")


def test_self_introduction_recorded_but_excluded_from_history(monkeypatch) -> None:
    monkeypatch.setenv("PHDEBATE_TTS_FORMAL", "0")

    async def set_ready() -> str:
        async with store._lock:
            prev = store.snapshot["match"]["status"]
            store.snapshot["match"]["status"] = "ready"  # 赛前：尚未“开始比赛”
            return prev

    async def restore(prev: str) -> None:
        async with store._lock:
            store.snapshot["match"]["status"] = prev

    prev = asyncio.run(set_ready())
    try:
        # Self-introduction must be allowed before the match is "running".
        asyncio.run(store.run_agent_speech("spk_aff_2", mode="self_intro"))

        seg = next((s for s in store.snapshot["recent_transcript"] if s.get("kind") == "self_intro"), None)
        assert seg is not None, "self-introduction should be recorded to the transcript"
        assert seg.get("exclude_from_history") is True
        assert seg.get("text")

        # The self-intro text must never be sent back as agent conversation history.
        history = store._build_debate_history()
        for stage in history:
            for msg in stage["message"]:
                assert msg["content"] != seg["text"]
    finally:
        asyncio.run(restore(prev))


def test_reset_current_speech_works_after_speech_ended() -> None:
    async def seed() -> None:
        async with store._lock:
            store.snapshot["current_speech"] = None
            store.snapshot["recent_transcript"].insert(0, {
                "id": "seg_reset_ended",
                "speech_id": "speech_reset_ended",
                "phase_id": store.snapshot["match"]["current_phase_id"],
                "speaker_id": "spk_aff_2",
                "speaker_label": "正方二辩 · 测试",
                "source": "agent_text",
                "is_final": True,
                "valid": True,
                "text": "待复位的发言内容",
            })
            flow = store._fresh_flow_state()
            flow.update({"awaiting_host_confirm": True, "speech_id": "speech_reset_ended", "speaker_id": "spk_aff_2"})
            store.snapshot["flow"] = flow
            store._persist_snapshot()

    asyncio.run(seed())
    asyncio.run(store.reset_current_speech("test_reset_ended"))

    assert all(s.get("speech_id") != "speech_reset_ended" for s in store.snapshot["recent_transcript"])
    assert store.snapshot["flow"]["awaiting_host_confirm"] is False
    assert store.snapshot["current_speech"] is None


def test_conversion_autostart_phase_predicate() -> None:
    from app.main import _conversion_autostart_phase

    phase = {"id": "p1", "phase_type": "constructive", "side": "affirmative", "speaker_seat": 1}
    snapshot = {
        "match": {"status": "running", "current_phase_id": "p1"},
        "phases": [phase],
        "current_speech": None,
        "flow": {"awaiting_host_confirm": False},
        "speakers": [],
    }
    updated = {"id": "s1", "speaker_type": "agent", "side": "affirmative", "seat": 1}

    # human → agent at the current turn's seat: auto-start.
    assert _conversion_autostart_phase("human", updated, snapshot) is phase
    # editing an already-agent speaker: no auto-start.
    assert _conversion_autostart_phase("agent", updated, snapshot) is None
    # agent at a different seat (not the current turn): no.
    assert _conversion_autostart_phase("human", {**updated, "seat": 2}, snapshot) is None
    # a speech already in progress: no.
    assert _conversion_autostart_phase("human", updated, {**snapshot, "current_speech": {"id": "x"}}) is None
    # awaiting host confirm (already spoken): no.
    assert _conversion_autostart_phase("human", updated, {**snapshot, "flow": {"awaiting_host_confirm": True}}) is None
    # free debate uses turn-based auto-agent, not this path: no.
    assert _conversion_autostart_phase("human", updated, {**snapshot, "phases": [{**phase, "phase_type": "free_debate"}]}) is None
    # match not running yet: no.
    assert _conversion_autostart_phase("human", updated, {**snapshot, "match": {"status": "ready", "current_phase_id": "p1"}}) is None


def test_skipped_sentence_index_recorded_on_speech() -> None:
    async def scenario() -> None:
        async with store._lock:
            store.snapshot["current_speech"] = {
                "id": "speech_skip_test",
                "phase_id": store.snapshot["match"]["current_phase_id"],
                "speaker_id": "spk_aff_2",
                "side": "affirmative",
                "source": "agent_text",
                "state": "thinking",
                "content_final": "",
                "content_partial": "",
                "tts_task_id": "task_skip_test",
                "tts_skipped_sentences": [],
            }
            store._persist_snapshot()
        speaker = store._find_speaker("spk_aff_2")
        # Empty text triggers the skip path, which must record the index so the screen
        # can fill the ordered gap deterministically (instead of stalling forever).
        ok = await store._synthesize_sentence_tts("   ", 3, "task_skip_test", "speech_skip_test", speaker)
        assert ok is False

    asyncio.run(scenario())
    assert 3 in (store.snapshot["current_speech"] or {}).get("tts_skipped_sentences", [])


def test_transcript_streams_then_finalizes_and_keeps_full_history() -> None:
    from app.services.match_store import _RECENT_TRANSCRIPT_LIMIT

    # Cap raised well past the old 12 so a full debate stays in 实时辩论过程 + debate_history.
    assert _RECENT_TRANSCRIPT_LIMIT >= 100

    async def scenario() -> None:
        async with store._lock:
            speech = {
                "id": "speech_stream_x",
                "phase_id": store.snapshot["match"]["current_phase_id"],
                "speaker_id": "spk_aff_2",
                "turn_index": 1,
            }
            # Streaming: text appears as a non-final ("实时") segment before the speech ends.
            store._upsert_transcript_segment(speech, "spk_aff_2", "实时第一句", False, "agent_text")
            mid = next(s for s in store.snapshot["recent_transcript"] if s["speech_id"] == "speech_stream_x")
            assert mid["is_final"] is False
            assert "实时第一句" in mid["text"]
            # Generation done: the same segment finalizes in place (enters debate_history).
            store._upsert_transcript_segment(speech, "spk_aff_2", "实时第一句，完整定稿。", True, "agent_text")
            done = next(s for s in store.snapshot["recent_transcript"] if s["speech_id"] == "speech_stream_x")
            assert done["is_final"] is True
            assert "完整定稿" in done["text"]
            # Exactly one segment for the speech (streaming updates in place, no duplicate).
            assert sum(1 for s in store.snapshot["recent_transcript"] if s["speech_id"] == "speech_stream_x") == 1
            store._persist_snapshot()

    asyncio.run(scenario())


def test_phase_advance_finalizes_in_progress_speech_into_history() -> None:
    async def scenario() -> str:
        async with store._lock:
            store.snapshot["match"]["status"] = "running"
            store.snapshot["current_speech"] = {
                "id": "speech_inprogress_x",
                "phase_id": store.snapshot["match"]["current_phase_id"],
                "speaker_id": "spk_aff_1",
                "side": "affirmative",
                "source": "agent_text",
                "state": "speaking",
                "content_partial": "正方一辩的立论内容要点。",
                "content_final": "",
                "tts_task_id": "task_inprogress_x",
                "turn_index": 1,
            }
            # Only a non-final ("实时") segment exists, as during an unfinished speech.
            store._upsert_transcript_segment(
                store.snapshot["current_speech"], "spk_aff_1", "正方一辩的立论内容要点。", False, "agent_text"
            )
            store._persist_snapshot()
            cur = store.snapshot["match"]["current_phase_id"]
            nxt = next(
                p for p in sorted(store.snapshot["phases"], key=lambda x: x["display_order"])
                if p["id"] != cur and p.get("phase_type") != "free_debate"
            )
        await store.start_phase(nxt["id"])
        return cur

    asyncio.run(scenario())

    # Advancing the phase mid-speech must finalize that speech into the global history.
    seg = next((s for s in store.snapshot["recent_transcript"] if s.get("speech_id") == "speech_inprogress_x"), None)
    assert seg is not None
    assert seg["is_final"] is True
    history = store._build_debate_history()
    assert any("正方一辩的立论内容要点" in msg["content"] for stage in history for msg in stage["message"])


def test_clock_control_rejects_unknown_and_negative_values() -> None:
    missing = client.post("/api/matches/match_001/clocks/not_real/pause")
    assert missing.status_code == 409
    assert missing.json()["error"]["code"] == "clock_not_found"

    negative = client.post(
        "/api/matches/match_001/clocks/turn/adjust",
        json={"remaining_ms": -1},
    )
    assert negative.status_code == 409
    assert negative.json()["error"]["code"] == "invalid_clock"


def test_skip_current_phase_stops_speech_and_resets_next_phase() -> None:
    response = client.post(
        "/api/matches/match_001/phases/phase_free_debate/skip",
        json={"reason": "test_skip"},
    )
    assert response.status_code == 200
    data = response.json()["data"]

    assert data["current_speech"] is None
    assert data["match"]["current_phase_id"] == "phase_neg_summary_4"
    assert data["match"]["live_mode"] == "single"
    assert next(item for item in data["phases"] if item["id"] == "phase_free_debate")["status"] == "skipped"
    assert next(item for item in data["phases"] if item["id"] == "phase_neg_summary_4")["status"] == "active"
    assert data["clocks"][0]["name"] == "main"
    assert data["clocks"][0]["phase_id"] == "phase_neg_summary_4"
    assert data["clocks"][0]["remaining_ms"] == 180000
    assert data["clocks"][0]["state"] == "paused"


def test_rollback_phase_resets_flow_and_invalidates_later_transcripts() -> None:
    stopped = client.post("/api/matches/match_001/speakers/spk_aff_3/stop-speaking")
    assert stopped.status_code == 200
    segment_id = stopped.json()["data"]["recent_transcript"][0]["id"]

    response = client.post(
        "/api/matches/match_001/phases/phase_aff_statement_3/rollback",
        json={"reason": "test_rollback"},
    )
    assert response.status_code == 200
    data = response.json()["data"]

    assert data["current_speech"] is None
    assert data["match"]["current_phase_id"] == "phase_aff_statement_3"
    assert data["match"]["live_mode"] == "single"
    assert next(item for item in data["phases"] if item["id"] == "phase_aff_statement_3")["status"] == "active"
    assert next(item for item in data["phases"] if item["id"] == "phase_free_debate")["status"] == "pending"
    rolled_back_segment = next(item for item in data["recent_transcript"] if item["id"] == segment_id)
    assert rolled_back_segment["phase_id"] == "phase_free_debate"
    assert rolled_back_segment["valid"] is False
    assert rolled_back_segment["invalid_reason"] == "rollback"


def test_audience_votes_require_open_window() -> None:
    closed = client.post("/api/matches/match_001/audience-votes/close")
    assert closed.status_code == 200
    assert closed.json()["data"]["vote_state"]["window_status"] == "closed"

    blocked = client.post(
        "/api/public/matches/match_001/audience-votes",
        json={"winner_side": "affirmative", "best_speaker_id": "spk_aff_1"},
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "vote_window_closed"

    opened = client.post("/api/matches/match_001/audience-votes/open")
    assert opened.status_code == 200
    response = client.post(
        "/api/public/matches/match_001/audience-votes",
        json={"token": "student-001", "winner_side": "negative", "best_speaker_id": "spk_neg_2"},
    )
    assert response.status_code == 200

    snapshot = client.get("/api/matches/match_001").json()["data"]
    assert snapshot["vote_state"]["window_status"] == "open"
    assert snapshot["vote_state"]["audience_count"] == 138
    assert snapshot["vote_state"]["winner_side"] == "affirmative"
    assert snapshot["vote_state"]["audience_summary"]["winner"]["negative"] == 55
    assert "used_audience_tokens" not in snapshot["vote_state"]
    assert "audience_votes" not in snapshot["vote_state"]


def test_audience_ranking_borda_aggregation() -> None:
    """观众投票排序按 Borda 计分聚合：一票里排第 1 名得 N 分、依次递减；跨票累加、降序排列。"""
    store.snapshot["vote_state"]["audience_summary"] = store._empty_audience_summary()
    store.snapshot["vote_state"]["audience_count"] = 0
    speakers = [s["id"] for s in store.snapshot["speakers"] if s["side"] in {"affirmative", "negative"}]
    n = len(speakers)
    assert n >= 4

    # 票1：完整排序 speakers[0] > … > speakers[-1]
    store._append_audience_summary({"winner_side": "affirmative", "ranking": speakers})
    # 票2：把最后一名提到第一，其余顺延
    rotated = [speakers[-1]] + speakers[:-1]
    store._append_audience_summary({"winner_side": "negative", "ranking": rotated})

    summary = store.snapshot["vote_state"]["audience_summary"]
    assert summary["total"] == 2
    assert summary["winner"]["affirmative"] == 1
    assert summary["winner"]["negative"] == 1

    pts = {item["speaker_id"]: item["count"] for item in summary["best_speaker"]}
    assert pts[speakers[0]] == n + (n - 1)   # 票1第1名(N) + 票2第2名(N-1)
    assert pts[speakers[-1]] == 1 + n        # 票1最后(1) + 票2第1名(N)
    counts = [item["count"] for item in summary["best_speaker"]]
    assert counts == sorted(counts, reverse=True)
    assert len({item["speaker_id"] for item in summary["best_speaker"]}) == len(summary["best_speaker"])


def test_audience_vote_rejects_duplicate_token() -> None:
    payload = {"token": "student-dup", "winner_side": "affirmative", "best_speaker_id": "spk_aff_3"}
    first = client.post("/api/public/matches/match_001/audience-votes", json=payload)
    assert first.status_code == 200

    second = client.post("/api/public/matches/match_001/audience-votes", json=payload)
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "duplicate_vote"


def test_audience_vote_rejects_same_browser_even_after_token_changes() -> None:
    first = client.post(
        "/api/public/matches/match_001/audience-votes",
        headers={"user-agent": "same-browser"},
        json={"token": "student-browser-1", "winner_side": "affirmative", "best_speaker_id": "spk_aff_3"},
    )
    assert first.status_code == 200

    internal_keys = store.snapshot["vote_state"]["audience_vote_keys"]
    assert len(internal_keys) == 2
    assert all(key.startswith(("token_hash:", "browser_hash:")) for key in internal_keys)
    assert not any("student-browser-1" in key for key in internal_keys)

    second = client.post(
        "/api/public/matches/match_001/audience-votes",
        headers={"user-agent": "same-browser"},
        json={"token": "student-browser-2", "winner_side": "negative", "best_speaker_id": "spk_neg_2"},
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "duplicate_vote"
    assert "你已经投过票" in second.json()["error"]["message"]

    public_snapshot = client.get("/api/matches/match_001").json()["data"]
    assert "audience_vote_keys" not in public_snapshot["vote_state"]
    assert "student-browser" not in json.dumps(public_snapshot, ensure_ascii=False)


def test_judge_vote_items_build_summary_and_result_fields() -> None:
    response = client.post(
        "/api/matches/match_001/votes",
        json={
            "voter_type": "judge",
            "voter_id": "judge_01",
            "items": [
                {"vote_type": "constructive", "target_side": "negative"},
                {"vote_type": "process", "target_side": "negative"},
                {"vote_type": "conclusion", "target_side": "affirmative"},
                {"vote_type": "winner", "target_side": "negative"},
                {"vote_type": "best_speaker", "target_speaker_id": "spk_aff_3"},
            ],
        },
    )
    assert response.status_code == 200
    vote_state = response.json()["data"]["vote_state"]
    assert vote_state["judge_summary"]["constructive"]["negative"] == 1
    assert vote_state["judge_summary"]["process"]["negative"] == 1
    assert vote_state["judge_summary"]["conclusion"]["affirmative"] == 1
    assert vote_state["judge_summary"]["computed_winner_side"] == "negative"
    assert vote_state["winner_side"] == "negative"
    assert vote_state["best_speaker_id"] == "spk_aff_3"


def test_vote_publish_order_requires_judge_before_audience() -> None:
    blocked = client.post("/api/matches/match_001/votes/publish", json={"scope": "audience"})
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "publish_order"

    blocked_scene = client.post("/api/matches/match_001/screen/scene", json={"scene": "audience_result"})
    assert blocked_scene.status_code == 409
    assert blocked_scene.json()["error"]["code"] == "publish_order"

    judge = client.post("/api/matches/match_001/votes/publish", json={"scope": "judge"})
    assert judge.status_code == 200
    judge_data = judge.json()["data"]
    assert judge_data["vote_state"]["judge_published"] is True
    assert judge_data["vote_state"]["window_status"] == "closed"
    assert judge_data["match"]["screen_scene"] == "judge_result"

    audience = client.post("/api/matches/match_001/votes/publish", json={"scope": "audience"})
    assert audience.status_code == 200
    data = audience.json()["data"]
    assert data["vote_state"]["judge_published"] is True
    assert data["vote_state"]["audience_published"] is True
    assert data["match"]["screen_scene"] == "audience_result"

    reopened = client.post("/api/matches/match_001/audience-votes/open")
    assert reopened.status_code == 200
    assert reopened.json()["data"]["window_status"] == "open"

    finished = client.post("/api/matches/match_001/finish")
    assert finished.status_code == 200
    finished_data = finished.json()["data"]
    assert finished_data["match"]["status"] == "finished"
    assert finished_data["match"]["screen_scene"] == "audience_result"
    assert finished_data["vote_state"]["window_status"] == "closed"

    blocked_after_finish = client.post(
        "/api/public/matches/match_001/audience-votes",
        json={
            "token": "vote-after-finish",
            "winner_side": "affirmative",
            "best_speaker_id": "spk_aff_3",
        },
    )
    assert blocked_after_finish.status_code == 409
    assert blocked_after_finish.json()["error"]["code"] == "vote_unavailable"


def test_asr_partial_final_updates_live_speech_without_duplicate_segments() -> None:
    partial = client.post(
        "/api/matches/match_001/speakers/spk_aff_3/asr/partial",
        json={"text": "partial text", "latency_ms": 520},
    )
    assert partial.status_code == 200
    data = partial.json()["data"]
    speech_id = data["current_speech"]["id"]
    assert data["current_speech"]["content_partial"] == "partial text"
    assert data["speech_service"]["asr"]["status"] == "streaming"
    assert data["speech_service"]["asr"]["latency_ms"] == 520
    assert data["recent_transcript"][0]["speech_id"] == speech_id
    assert data["recent_transcript"][0]["is_final"] is False

    final = client.post(
        "/api/matches/match_001/speakers/spk_aff_3/asr/final",
        json={"text": "final text", "latency_ms": 660},
    )
    assert final.status_code == 200
    data = final.json()["data"]
    assert data["current_speech"]["content_final"] == "final text"
    assert data["speech_service"]["asr"]["status"] == "ok"
    assert data["recent_transcript"][0]["speech_id"] == speech_id
    assert data["recent_transcript"][0]["is_final"] is True

    stopped = client.post("/api/matches/match_001/speakers/spk_aff_3/stop-speaking")
    assert stopped.status_code == 200
    data = stopped.json()["data"]
    assert data["current_speech"] is None
    assert sum(1 for item in data["recent_transcript"] if item.get("speech_id") == speech_id) == 1
    assert data["recent_transcript"][0]["text"] == "final text"
    assert data["speech_service"]["asr"]["active_sessions"] == 0


def test_asr_pending_placeholder_is_excluded_from_agent_history_and_replaced() -> None:
    active = store.snapshot["current_speech"]
    assert active["speaker_id"] == "spk_aff_3"
    active["content_partial"] = ""
    active["content_final"] = ""
    speech_id = active["id"]
    store.snapshot["recent_transcript"] = [
        segment for segment in store.snapshot["recent_transcript"] if segment.get("speech_id") != speech_id
    ]

    stopped = client.post("/api/matches/match_001/speakers/spk_aff_3/stop-speaking")
    assert stopped.status_code == 200
    data = stopped.json()["data"]
    segment = data["recent_transcript"][0]
    placeholder = "本次发言已结束，正式转写将在后续 ASR 链路中补齐。"
    assert segment["text"] == placeholder
    assert segment["exclude_from_history"] is True

    # Defense in depth for already-existing snapshots written before this fix:
    # even if the marker is absent, the system placeholder must never be sent to agents.
    store.snapshot["recent_transcript"][0].pop("exclude_from_history", None)
    history = store._build_debate_history()
    assert all(msg["content"] != placeholder for stage in history for msg in stage["message"])

    store._apply_archived_asr_text(speech_id, "正方三辩真实转写内容。")
    history = store._build_debate_history()
    assert any("正方三辩真实转写内容。" == msg["content"] for stage in history for msg in stage["message"])


def test_silent_pcm_replaces_pending_placeholder_and_skips_archive_asr(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("PHDEBATE_AUDIO_DIR", str(tmp_path / "audio"))
    monkeypatch.setenv("PHDEBATE_ASR_REALTIME", "1")
    monkeypatch.setenv("PHDEBATE_ASR_AUTO_RECOGNIZE", "1")
    active = store.snapshot["current_speech"]
    active["content_partial"] = ""
    active["content_final"] = ""
    speech_id = active["id"]
    zero_pcm = b"\0" * 16000

    first = client.post(
        f"/api/matches/match_001/speeches/{speech_id}/audio-chunks",
        data={"speaker_id": "spk_aff_3", "chunk_index": "0", "duration_ms": "500"},
        files={"file": ("chunk-0.pcm", zero_pcm, "audio/L16;rate=16000")},
    )
    assert first.status_code == 200
    assert first.json()["data"]["silent"] is True
    assert first.json()["data"]["peak_level"] == 0
    snap = client.get("/api/matches/match_001").json()["data"]
    asset = next(a for a in snap["audio_assets"] if a["speech_id"] == speech_id)
    assert asset["audio_peak_level"] == 0
    assert snap["speech_service"]["asr"]["status"] == "failed"

    stopped = client.post("/api/matches/match_001/speakers/spk_aff_3/stop-speaking")
    assert stopped.status_code == 200
    segment = stopped.json()["data"]["recent_transcript"][0]
    assert segment["text"] == "麦克风输入没有检测到有效声音，请以现场发言为准。"
    assert segment["exclude_from_history"] is True

    complete = client.post(
        f"/api/matches/match_001/speeches/{speech_id}/audio/complete",
        json={"speaker_id": "spk_aff_3"},
    )
    assert complete.status_code == 200
    data = complete.json()["data"]
    assert data["recent_transcript"][0]["text"] == "麦克风输入没有检测到有效声音，请以现场发言为准。"
    assert data["audio_assets"][0]["asr_realtime_status"] == "failed"
    assert data["audio_assets"][0]["asr_realtime_error"] == "microphone input is silent"
    history = store._build_debate_history()
    assert all("麦克风输入没有检测到有效声音" not in msg["content"] for stage in history for msg in stage["message"])


def test_audio_chunk_upload_archives_file_and_completes_asset() -> None:
    response = client.post(
        "/api/matches/match_001/speeches/speech_live/audio-chunks",
        data={"speaker_id": "spk_aff_3", "chunk_index": "0", "duration_ms": "500"},
        files={"file": ("chunk.webm", b"audio-bytes", "audio/webm")},
    )
    assert response.status_code == 200
    # 上传分片接口只回这一片的归档结果(不再回传整张快照，避免长录音逐片拖垮连接)。
    chunk = response.json()["data"]
    assert chunk["speech_id"] == "speech_live"
    assert chunk["speaker_id"] == "spk_aff_3"
    assert chunk["chunk_index"] == 0
    assert chunk["chunk_count"] == 1
    assert chunk["size_bytes"] == len(b"audio-bytes")
    assert Path(chunk["file_path"]).exists()
    # 录音中的完整资产状态(status/duration_ms/分片路径)从快照读取。
    snap = client.get("/api/matches/match_001").json()["data"]
    asset = next(a for a in snap["audio_assets"] if a["speech_id"] == "speech_live")
    assert asset["chunk_count"] == 1
    assert asset["duration_ms"] == 500
    assert asset["status"] == "recording"
    assert Path(asset["chunks"][0]["file_path"]).exists()

    complete = client.post(
        "/api/matches/match_001/speeches/speech_live/audio/complete",
        json={"speaker_id": "spk_aff_3"},
    )
    assert complete.status_code == 200
    assert complete.json()["data"]["audio_assets"][0]["status"] == "completed"


def test_late_audio_chunk_after_complete_is_ignored_not_reverted() -> None:
    """网络抖动/stop 后补传：发言音频已归档完成后，迟到的分片必须被良性忽略——
    既不把资产状态打回 recording，也不重开 ASR，且不向控制台报错（否则现场每次收尾后
    一个迟到分片就把已完成录音弄脏、并触发对已结束发言的识别）。"""
    first = client.post(
        "/api/matches/match_001/speeches/speech_live/audio-chunks",
        data={"speaker_id": "spk_aff_3", "chunk_index": "0", "duration_ms": "500"},
        files={"file": ("c0.webm", b"audio-0", "audio/webm")},
    )
    assert first.status_code == 200
    complete = client.post(
        "/api/matches/match_001/speeches/speech_live/audio/complete",
        json={"speaker_id": "spk_aff_3"},
    )
    assert complete.status_code == 200
    assert complete.json()["data"]["audio_assets"][0]["status"] == "completed"

    # 迟到分片
    late = client.post(
        "/api/matches/match_001/speeches/speech_live/audio-chunks",
        data={"speaker_id": "spk_aff_3", "chunk_index": "1", "duration_ms": "500"},
        files={"file": ("c1.webm", b"audio-1", "audio/webm")},
    )
    assert late.status_code == 200  # 不报错
    assert late.json()["data"].get("ignored_after_complete") is True
    # 资产仍是 completed、chunk_count 没被迟到分片改动。
    snap = client.get("/api/matches/match_001").json()["data"]
    asset = next(a for a in snap["audio_assets"] if a["speech_id"] == "speech_live")
    assert asset["status"] == "completed"
    assert asset["chunk_count"] == 1


def test_archived_pcm_audio_can_be_recognized_and_written_to_transcript(monkeypatch, tmp_path) -> None:
    class FakeGateway:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        async def recognize(self, audio: bytes, audio_format: str, encoding: str) -> ASRResult:
            assert audio == b"pcm-audio-0pcm-audio-1"
            assert audio_format == "audio/L16;rate=16000"
            assert encoding == "raw"
            return ASRResult(text="归档识别文本", latency_ms=345, chunk_count=2)

    monkeypatch.setenv("PHDEBATE_AUDIO_DIR", str(tmp_path / "audio"))
    _patch_asr_selection(monkeypatch, FakeGateway(), provider="xfyun")

    first = client.post(
        "/api/matches/match_001/speeches/speech_live/audio-chunks",
        data={"speaker_id": "spk_aff_3", "chunk_index": "0", "duration_ms": "500"},
        files={"file": ("chunk-0.pcm", b"pcm-audio-0", "audio/L16")},
    )
    assert first.status_code == 200
    assert Path(first.json()["data"]["file_path"]).suffix == ".pcm"
    snap = client.get("/api/matches/match_001").json()["data"]
    first_asset = next(a for a in snap["audio_assets"] if a["speech_id"] == "speech_live")
    assert "l16" in first_asset["mime_type"].lower()
    assert Path(first_asset["chunks"][0]["file_path"]).suffix == ".pcm"
    second = client.post(
        "/api/matches/match_001/speeches/speech_live/audio-chunks",
        data={"speaker_id": "spk_aff_3", "chunk_index": "1", "duration_ms": "500"},
        files={"file": ("chunk-1.pcm", b"pcm-audio-1", "audio/L16")},
    )
    assert second.status_code == 200
    complete = client.post(
        "/api/matches/match_001/speeches/speech_live/audio/complete",
        json={"speaker_id": "spk_aff_3"},
    )
    assert complete.status_code == 200

    response = client.post("/api/matches/match_001/speeches/speech_live/asr/recognize")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["result"]["text"] == "归档识别文本"
    assert data["result"]["audio_bytes"] == len(b"pcm-audio-0pcm-audio-1")
    snapshot = data["snapshot"]
    assert snapshot["current_speech"]["content_final"] == "归档识别文本"
    assert snapshot["recent_transcript"][0]["speech_id"] == "speech_live"
    assert snapshot["recent_transcript"][0]["is_final"] is True
    assert snapshot["speech_service"]["asr"]["status"] == "ok"
    assert snapshot["speech_service"]["asr"]["latency_ms"] == 345


def test_pcm_audio_chunk_updates_live_asr_observability(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PHDEBATE_AUDIO_DIR", str(tmp_path / "audio"))

    response = client.post(
        "/api/matches/match_001/speeches/speech_live/audio-chunks",
        data={"speaker_id": "spk_aff_3", "chunk_index": "0", "duration_ms": "500"},
        files={"file": ("chunk-0.pcm", b"pcm-audio", "audio/L16;rate=16000")},
    )

    assert response.status_code == 200
    assert response.json()["data"]["pcm_ready"] is True
    snap = client.get("/api/matches/match_001").json()["data"]
    asset = next(a for a in snap["audio_assets"] if a["speech_id"] == "speech_live")
    assert asset["mime_type"] == "audio/L16;rate=16000"
    assert snap["speech_service"]["asr"]["status"] == "streaming"
    assert snap["speech_service"]["asr"]["active_sessions"] == 1
    assert "receiving PCM/L16" in snap["speech_service"]["asr"]["detail"]


def test_complete_audio_archive_can_auto_recognize_pcm(monkeypatch, tmp_path) -> None:
    class FakeGateway:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        async def recognize(self, audio: bytes, audio_format: str, encoding: str) -> ASRResult:
            assert audio == b"pcm-audio"
            assert audio_format == "audio/L16;rate=16000"
            assert encoding == "raw"
            return ASRResult(text="自动识别完成", latency_ms=210, chunk_count=1)

    monkeypatch.setenv("PHDEBATE_AUDIO_DIR", str(tmp_path / "audio"))
    _patch_asr_selection(monkeypatch, FakeGateway(), provider="xfyun")

    upload = client.post(
        "/api/matches/match_001/speeches/speech_live/audio-chunks",
        data={"speaker_id": "spk_aff_3", "chunk_index": "0", "duration_ms": "500"},
        files={"file": ("chunk-0.pcm", b"pcm-audio", "audio/L16;rate=16000")},
    )
    assert upload.status_code == 200

    complete = client.post(
        "/api/matches/match_001/speeches/speech_live/audio/complete",
        json={"speaker_id": "spk_aff_3", "auto_recognize": True},
    )

    assert complete.status_code == 200
    data = complete.json()["data"]
    assert data["current_speech"]["content_final"] == "自动识别完成"
    assert data["recent_transcript"][0]["text"] == "自动识别完成"
    assert data["speech_service"]["asr"]["status"] == "ok"
    assert data["speech_service"]["asr"]["latency_ms"] == 210


def test_pcm_chunks_drive_realtime_asr_partial_and_final(monkeypatch, tmp_path) -> None:
    class FakeSession:
        def __init__(self, on_partial, on_final) -> None:
            self.on_partial = on_partial
            self.on_final = on_final
            self.chunks = []

        async def send_audio(self, audio: bytes) -> None:
            self.chunks.append(audio)
            await self.on_partial(f"实时 partial {len(self.chunks)}", 120 + len(self.chunks), len(self.chunks))

        async def finish(self) -> ASRResult:
            await self.on_final("实时 final", 260, len(self.chunks))
            return ASRResult(text="实时 final", latency_ms=260, chunk_count=len(self.chunks))

    class FakeGateway:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        async def open_stream(self, on_partial, on_final, **_kwargs) -> FakeSession:
            return FakeSession(on_partial, on_final)

    monkeypatch.setenv("PHDEBATE_AUDIO_DIR", str(tmp_path / "audio"))
    monkeypatch.setenv("PHDEBATE_ASR_REALTIME", "1")
    _patch_asr_selection(monkeypatch, FakeGateway(), provider="xfyun")

    first = client.post(
        "/api/matches/match_001/speeches/speech_live/audio-chunks",
        data={"speaker_id": "spk_aff_3", "chunk_index": "0", "duration_ms": "500"},
        files={"file": ("chunk-0.pcm", b"pcm-0", "audio/L16;rate=16000")},
    )

    assert first.status_code == 200
    assert first.json()["data"]["pcm_ready"] is True
    snap = client.get("/api/matches/match_001").json()["data"]
    assert snap["current_speech"]["content_partial"] == "实时 partial 1"
    assert snap["recent_transcript"][0]["is_final"] is False
    asset = next(a for a in snap["audio_assets"] if a["speech_id"] == "speech_live")
    assert asset["asr_realtime_status"] == "streaming"

    second = client.post(
        "/api/matches/match_001/speeches/speech_live/audio-chunks",
        data={"speaker_id": "spk_aff_3", "chunk_index": "1", "duration_ms": "500"},
        files={"file": ("chunk-1.pcm", b"pcm-1", "audio/L16;rate=16000")},
    )
    assert second.status_code == 200
    assert client.get("/api/matches/match_001").json()["data"]["current_speech"]["content_partial"] == "实时 partial 2"

    complete = client.post(
        "/api/matches/match_001/speeches/speech_live/audio/complete",
        json={"speaker_id": "spk_aff_3"},
    )

    assert complete.status_code == 200
    data = complete.json()["data"]
    assert data["current_speech"]["content_final"] == "实时 final"
    assert data["recent_transcript"][0]["text"] == "实时 final"
    assert data["recent_transcript"][0]["is_final"] is True
    assert data["audio_assets"][0]["asr_realtime_status"] == "completed"
    assert data["speech_service"]["asr"]["active_sessions"] == 0


def test_archived_webm_audio_recognition_returns_format_error(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PHDEBATE_AUDIO_DIR", str(tmp_path / "audio"))
    upload = client.post(
        "/api/matches/match_001/speeches/speech_live/audio-chunks",
        data={"speaker_id": "spk_aff_3", "chunk_index": "0"},
        files={"file": ("chunk.webm", b"webm-opus", "audio/webm;codecs=opus")},
    )
    assert upload.status_code == 200

    response = client.post("/api/matches/match_001/speeches/speech_live/asr/recognize")

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "unsupported_audio_format"
    assert "PCM/L16" in body["error"]["message"]


def test_audio_chunk_upload_rejects_wrong_speaker() -> None:
    response = client.post(
        "/api/matches/match_001/speeches/speech_live/audio-chunks",
        data={"speaker_id": "spk_neg_2", "chunk_index": "0"},
        files={"file": ("chunk.webm", b"audio-bytes", "audio/webm")},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "invalid_speaker"


def test_patch_speech_revises_text_and_records_revision() -> None:
    final = client.post(
        "/api/matches/match_001/speakers/spk_aff_3/asr/final",
        json={"text": "before revision", "latency_ms": 500},
    )
    assert final.status_code == 200
    speech_id = final.json()["data"]["current_speech"]["id"]

    response = client.patch(
        f"/api/matches/match_001/speeches/{speech_id}",
        json={"content_final": "after revision", "reason": "fix typo"},
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["current_speech"]["content_final"] == "after revision"
    assert data["recent_transcript"][0]["text"] == "after revision"
    assert data["speech_revisions"][0]["speech_id"] == speech_id
    assert data["speech_revisions"][0]["before_text"] == "before revision"
    assert data["speech_revisions"][0]["after_text"] == "after revision"


def test_patch_speech_can_invalidate_and_restore_segment() -> None:
    speech_id = "seg_002"
    invalid = client.patch(
        f"/api/matches/match_001/speeches/{speech_id}",
        json={"valid": False, "reason": "manual invalid"},
    )
    assert invalid.status_code == 200
    segment = next(item for item in invalid.json()["data"]["recent_transcript"] if item["id"] == speech_id)
    assert segment["valid"] is False
    assert segment["invalid_reason"] == "manual invalid"

    restored = client.patch(
        f"/api/matches/match_001/speeches/{speech_id}",
        json={"valid": True, "reason": "restore"},
    )
    assert restored.status_code == 200
    segment = next(item for item in restored.json()["data"]["recent_transcript"] if item["id"] == speech_id)
    assert segment["valid"] is True
    assert segment["invalid_reason"] is None


def test_patch_speech_rejects_missing_speech() -> None:
    response = client.patch(
        "/api/matches/match_001/speeches/not-real",
        json={"content_final": "new"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "speech_not_found"


def test_archive_recognition_authoritative_even_after_realtime_completed(monkeypatch) -> None:
    # 现场反馈「ASR 只转录一部分」：实时流按 HTTP 分片【到达顺序】喂给 ASR，长发言并发上传易乱序/丢失
    # → 终稿不全。修复后即便实时流已 completed，也要用按 chunk_index 顺序拼接的完整归档做一次批量识别
    # 作为权威终稿（仅 PCM/L16）。本用例锁定该判定：completed 不再跳过自动批量识别。
    monkeypatch.setenv("PHDEBATE_ASR_AUTO_RECOGNIZE", "1")
    saved = store.snapshot.get("audio_assets")
    try:
        store.snapshot["audio_assets"] = [
            {
                "id": "aud_asr_t",
                "speech_id": "sp_asr_t",
                "speaker_id": "spk_aff_1",
                "mime_type": "audio/L16;rate=16000",
                "asr_realtime_status": "completed",
                "chunks": [{"chunk_index": 0, "file_path": "/tmp/x.pcm", "audio_url": "/x"}],
            }
        ]
        assert asyncio.run(store.should_auto_recognize_audio_archive("sp_asr_t")) is True
        # webm（非 PCM）仍不在此重识别（保持实时结果）。
        store.snapshot["audio_assets"][0]["mime_type"] = "audio/webm;codecs=opus"
        assert asyncio.run(store.should_auto_recognize_audio_archive("sp_asr_t")) is False
    finally:
        store.snapshot["audio_assets"] = saved


def test_asr_failure_sets_degraded_status_without_stopping_match() -> None:
    response = client.post(
        "/api/matches/match_001/speakers/spk_aff_3/asr/fail",
        json={"reason": "xunfei unavailable"},
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["match"]["status"] == "running"
    assert data["current_speech"]["speaker_id"] == "spk_aff_3"
    assert "转写不可用" in data["current_speech"]["content_partial"]
    assert data["speech_service"]["asr"]["status"] == "failed"
    assert data["speech_service"]["asr"]["detail"] == "xunfei unavailable"


def test_tts_failure_marks_text_only_degradation_for_agent_speech() -> None:
    activated = client.post("/api/matches/match_001/speakers/spk_aff_2/activate")
    assert activated.status_code == 200
    assert activated.json()["data"]["current_speech"]["speaker_id"] == "spk_aff_2"

    response = client.post(
        "/api/matches/match_001/speakers/spk_aff_2/tts/fail",
        json={"reason": "speaker device unavailable", "text_only": True},
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["match"]["status"] == "running"
    assert data["match"]["live_mode"] == "free"
    assert data["speech_service"]["tts"]["status"] == "failed"
    assert data["speech_service"]["tts"]["speaker_id"] == "spk_aff_2"
    assert data["speech_service"]["tts"]["degraded_to"] == "text_only"


def test_broadcast_is_nonblocking_and_drops_stuck_client() -> None:
    """卡死根因加固：emit 的广播必须是同步、非阻塞的——只把事件塞进每个连接自己的队列。
    一个消费不过来(网络慢/卡)的客户端，队列满后被丢弃(它会用 last_seq 重连重同步)，
    绝不阻塞 emit、绝不波及健康客户端。"""

    class DummyWS:
        async def send_json(self, _msg):
            return None

        async def close(self):
            return None

    async def scenario() -> None:
        stuck_ws = DummyWS()
        stuck_q: asyncio.Queue = asyncio.Queue(maxsize=1)
        stuck_q.put_nowait({"type": "backlog"})  # 预填满：模拟慢客户端队列已积压到上限

        healthy_ws = DummyWS()
        healthy_q: asyncio.Queue = asyncio.Queue(maxsize=64)

        store._conn_send_queues[stuck_ws] = stuck_q
        store._conn_send_queues[healthy_ws] = healthy_q
        store._connections.update({stuck_ws, healthy_ws})
        try:
            # 同步调用、立即返回（不是协程，根本不会 await 网络）。
            store._broadcast({"type": "evt", "seq": 1})
            # 卡住的客户端队列已满 → 被丢弃。
            assert stuck_ws not in store._conn_send_queues
            assert stuck_ws not in store._connections
            # 健康客户端照常收到事件。
            assert healthy_q.get_nowait() == {"type": "evt", "seq": 1}
        finally:
            for ws in (stuck_ws, healthy_ws):
                store._conn_send_queues.pop(ws, None)
                store._connections.discard(ws)
                task = store._conn_senders.pop(ws, None)
                if task is not None:
                    task.cancel()

    asyncio.run(scenario())
