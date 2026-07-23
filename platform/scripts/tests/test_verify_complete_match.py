from __future__ import annotations

import importlib.util
import sys
from argparse import Namespace
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "verify_complete_match.py"
SPEC = importlib.util.spec_from_file_location("verify_complete_match_under_test", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def verifier(scenario: str = "1v1-human-ai"):
    result = MODULE.CompleteMatchVerifier(
        base_url="http://127.0.0.1:8000",
        insecure=False,
        scenario=MODULE.SCENARIOS[scenario],
        timeout_seconds=60,
        max_opening_seconds=15,
        free_turns=3,
        full_free_duration=False,
        exercise_pause_resume=True,
        exercise_reconnect=True,
        exercise_ai_reset=False,
        auto_recover=True,
        allow_unclassified_test_data=True,
    )
    result.audit.ai_playback_ids.add("ai-live")
    result.audit.reconnect_duration_seconds = 1.25
    result._pause_exercised = True
    result._reconnect_exercised = True
    return result


def successful_result(scenario: str = "1v1-human-ai") -> dict:
    definition = MODULE.SCENARIOS[scenario]
    events = []
    seq = 1
    for stage_key in definition.expected_stage_keys:
        events.append({"seq": seq, "type": "stage.started", "payload": {"stage": {"key": stage_key}}})
        seq += 1
    for event_type in ("control.pause", "control.resume", "presence.disconnected", "presence.connected"):
        events.append({"seq": seq, "type": event_type, "payload": {}})
        seq += 1
    speeches = []
    for stage_key in definition.expected_stage_keys:
        if stage_key in {"opening", "free_debate", "judging"}:
            continue
        all_human = scenario == "1v1-two-human"
        speeches.append(
            {
                "id": f"speech-{stage_key}",
                "stage_key": stage_key,
                "speaker_type": "human" if all_human or stage_key.startswith("aff") else "ai",
                "status": "completed",
            }
        )
    speeches.extend(
        {
            "id": f"free-{index}",
            "stage_key": "free_debate",
            "speaker_type": "human" if scenario == "1v1-two-human" or index % 2 else "ai",
            "status": "completed",
        }
        for index in range(1, 4)
    )
    if definition.require_ai_speech:
        for speech in speeches:
            if speech["speaker_type"] != "ai":
                continue
            events.append(
                {
                    "seq": seq,
                    "type": "audio.rtc.started",
                    "payload": {
                        "speech_id": speech["id"],
                        "transport": "livekit",
                        "synthesis_mode": "single_session_incremental",
                        "stream_url": None,
                        "audio_url": "",
                    },
                }
            )
            seq += 1
    events.append({"seq": seq, "type": "match.completed", "payload": {}})
    return {
        "room": {
            "status": "completed",
            "is_test_data": True,
            "match_audio_archive_enabled": False,
        },
        "match": {"status": "completed"},
        "scorecard": {"status": "approved", "winner": "aff"},
        "speeches": speeches,
        "events": events,
    }


def args(**overrides) -> Namespace:
    values = {
        "base_url": "http://127.0.0.1:8000",
        "allow_production": False,
        "allow_unclassified_test_data": True,
        "free_turns": 3,
        "timeout_seconds": 300,
        "max_opening_seconds": 15,
    }
    values.update(overrides)
    return Namespace(**values)


def test_remote_mutation_requires_explicit_confirmation_and_classified_data() -> None:
    with pytest.raises(SystemExit, match="--allow-production"):
        MODULE.validate_args(args(base_url="https://debate.example", allow_unclassified_test_data=False))
    with pytest.raises(SystemExit, match="禁止 --allow-unclassified-test-data"):
        MODULE.validate_args(
            args(base_url="https://debate.example", allow_production=True, allow_unclassified_test_data=True)
        )
    MODULE.validate_args(
        args(base_url="https://debate.example", allow_production=True, allow_unclassified_test_data=False)
    )


def test_scenarios_cover_human_ai_two_human_and_mixed_4v4() -> None:
    assert MODULE.SCENARIOS["1v1-human-ai"].human_seats == ("aff_1",)
    assert MODULE.SCENARIOS["1v1-two-human"].human_seats == ("aff_1", "neg_1")
    mixed = MODULE.SCENARIOS["4v4-mixed"]
    assert mixed.competition_slug == "daily-4v4"
    assert mixed.human_seats == ("aff_1", "neg_1", "aff_2", "neg_2")
    assert len(mixed.expected_stage_keys) == 11


def test_state_summary_never_copies_transcript_caption_or_event_payloads() -> None:
    rendered = MODULE.state_summary(
        {
            "status": "running",
            "current_stage": {"key": "free_debate", "kind": "free", "side": "aff"},
            "active_speech": {
                "id": "speech-1",
                "seat_key": "aff_1",
                "speaker_type": "human",
                "status": "speaking",
                "content": "不应进入诊断输出的逐字稿",
            },
            "caption_segments": [{"text": "不应进入诊断输出的字幕"}],
            "recent_events": [{"payload": {"secret": "不应进入诊断输出"}}],
            "free_turn_queue": {"can_request": True, "target_side": "neg"},
        }
    )
    serialized = repr(rendered)
    assert "逐字稿" not in serialized
    assert "字幕" not in serialized
    assert "secret" not in serialized
    assert rendered["active_speech"]["id"] == "speech-1"


def test_result_gate_proves_every_stage_fixed_speech_free_turn_and_real_judge() -> None:
    result = verifier().validate_result(successful_result())

    assert result["ok"] is True
    assert result["status"] == "completed"
    assert result["winner"] == "aff"
    assert result["speech_count"] == 7


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value["events"].pop(2), "未进入完整阶段"),
        (
            lambda value: value["events"].insert(
                -1,
                {
                    "seq": value["events"][-1]["seq"],
                    "type": "provider.failed",
                    "payload": {},
                },
            ),
            "异常事件",
        ),
        (lambda value: value["speeches"].__setitem__(0, {**value["speeches"][0], "status": "failed"}), "未完成发言"),
        (lambda value: value["scorecard"].__setitem__("status", "review_required"), "裁判结果未批准"),
    ],
)
def test_result_gate_rejects_partial_or_recovered_looking_success(mutation, message: str) -> None:
    value = successful_result()
    mutation(value)

    with pytest.raises(MODULE.VerificationFailure, match=message):
        verifier().validate_result(value)


def test_two_human_result_gate_rejects_any_ai_speech() -> None:
    value = successful_result("1v1-two-human")
    value["speeches"][0]["speaker_type"] = "ai"

    with pytest.raises(MODULE.VerificationFailure, match="双真人场景出现"):
        verifier("1v1-two-human").validate_result(value)


def test_short_reconnect_result_gate_rejects_disconnect_timeout_even_if_match_completed() -> None:
    value = successful_result("1v1-two-human")
    value["events"].insert(
        -1,
        {
            "seq": value["events"][-1]["seq"],
            "type": "participant.disconnect_timeout",
            "payload": {"grace_seconds": 60},
        },
    )

    with pytest.raises(MODULE.VerificationFailure, match="异常事件"):
        verifier("1v1-two-human").validate_result(value)


def test_result_gate_rejects_when_required_pause_or_reconnect_was_not_exercised() -> None:
    value = successful_result("1v1-two-human")
    missing_pause = verifier("1v1-two-human")
    missing_pause._pause_exercised = False
    with pytest.raises(MODULE.VerificationFailure, match="未实际执行房主暂停恢复"):
        missing_pause.validate_result(value)

    missing_reconnect = verifier("1v1-two-human")
    missing_reconnect._reconnect_exercised = False
    missing_reconnect.audit.reconnect_duration_seconds = None
    with pytest.raises(MODULE.VerificationFailure, match="未实际执行 60 秒内短断线恢复"):
        missing_reconnect.validate_result(value)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value["room"].__setitem__("match_audio_archive_enabled", True), "音频归档"),
        (lambda value: value["speeches"][0].__setitem__("audio_url", "/media/legacy.wav"), "发言音频"),
        (
            lambda value: next(
                item for item in value["events"] if item["type"] == "audio.rtc.started"
            )["payload"].__setitem__("synthesis_mode", "completed_wav"),
            "单会话真流式",
        ),
        (
            lambda value: next(
                item for item in value["events"] if item["type"] == "audio.rtc.started"
            ).__setitem__("type", "audio.stream.started"),
            "WebRTC 音轨",
        ),
    ],
)
def test_result_gate_rejects_audio_archive_or_retired_playback_paths(mutation, message: str) -> None:
    value = successful_result()
    mutation(value)

    with pytest.raises(MODULE.VerificationFailure, match=message):
        verifier().validate_result(value)
