import json
import sqlite3
import asyncio
import hashlib
import importlib.util
import io
import re
import zipfile
from pathlib import Path

import pytest
import httpx
from fastapi.testclient import TestClient

from app.main import app
from app.main import FRONTEND_ASSETS, FRONTEND_INDEX
from app.services.agent_gateway import AgentGateway, AgentGatewayError
from app.services.match_store import store
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
def reset_demo_state() -> None:
    client.post("/api/demo/reset")


def test_health() -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["ok"] is True


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


def test_speech_diagnostics_reports_mock_fallback_when_xfyun_missing(monkeypatch) -> None:
    for name in ["XFYUN_APP_ID", "XFYUN_API_KEY", "XFYUN_API_SECRET", "XFYUN_ASR_URL", "XFYUN_TTS_URL"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("PHDEBATE_TTS_FORMAL", raising=False)

    response = client.get("/api/matches/match_001/speech/diagnostics")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["overall_status"] == "mock_fallback"
    assert data["provider"] == "mock"
    assert "XFYUN_APP_ID" in data["asr"]["missing"]
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

    response = client.get("/api/matches/match_001/speech/diagnostics")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["overall_status"] == "ready"
    assert data["provider"] == "xfyun"
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
    monkeypatch.setattr("app.services.match_store.XfyunTTSGateway", FakeGateway)

    response = client.post("/api/matches/match_001/speech/tts/probe", json={"text": "语音合成自检"})

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["result"]["size_bytes"] == len(b"mp3-bytes")
    assert data["result"]["chunk_count"] == 2
    assert Path(data["result"]["file_path"]).read_bytes() == b"mp3-bytes"
    assert data["snapshot"]["speech_service"]["tts"]["latency_ms"] == 123


def test_asr_probe_recognizes_audio_and_updates_status(monkeypatch) -> None:
    class FakeGateway:
        def __init__(self, url: str) -> None:
            self.url = url

        async def recognize(self, audio: bytes, audio_format: str, encoding: str) -> ASRResult:
            assert audio == b"pcm"
            assert audio_format == "audio/L16;rate=16000"
            assert encoding == "raw"
            return ASRResult(text="自检通过", latency_ms=234, chunk_count=1)

    monkeypatch.setattr("app.services.match_store.XfyunASRGateway", FakeGateway)

    response = client.post(
        "/api/matches/match_001/speech/asr/probe",
        json={"audio_base64": "cGNt", "format": "audio/L16;rate=16000", "encoding": "raw"},
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["result"]["text"] == "自检通过"
    assert data["result"]["latency_ms"] == 234
    assert data["snapshot"]["speech_service"]["asr"]["status"] == "ok"


def test_asr_probe_returns_clear_error_when_unconfigured(monkeypatch) -> None:
    for name in ["XFYUN_APP_ID", "XFYUN_API_KEY", "XFYUN_API_SECRET", "XFYUN_ASR_URL"]:
        monkeypatch.delenv(name, raising=False)

    response = client.post("/api/matches/match_001/speech/asr/probe", json={})

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "speech_service_error"
    assert "XFYUN" in body["error"]["message"]


def test_tts_probe_returns_clear_error_when_unconfigured(monkeypatch) -> None:
    for name in ["XFYUN_APP_ID", "XFYUN_API_KEY", "XFYUN_API_SECRET", "XFYUN_TTS_URL"]:
        monkeypatch.delenv(name, raising=False)

    response = client.post("/api/matches/match_001/speech/tts/probe", json={"text": "语音合成自检"})

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "speech_service_error"
    assert "XFYUN" in body["error"]["message"]


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


def test_screen_scene_control_updates_snapshot() -> None:
    response = client.post(
        "/api/matches/match_001/screen/scene",
        json={"scene": "teams"},
    )
    assert response.status_code == 200
    assert response.json()["data"]["match"]["screen_scene"] == "teams"

    response = client.post(
        "/api/matches/match_001/screen/scene",
        json={"scene": "live", "live_mode": "free"},
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["match"]["screen_scene"] == "live"
    assert data["match"]["live_mode"] == "free"
    assert data["system"]["persistence"]["driver"] == "sqlite"


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


def test_speaker_settings_patch_updates_agent_fields_and_rejects_invalid_kind() -> None:
    response = client.patch(
        "/api/matches/match_001/speakers/spk_aff_2",
        json={
            "name": "玄思升级版",
            "model_name": "Qwen-Plus",
            "model_kind": "closed_source",
            "agent_endpoint": "http://127.0.0.1:8100",
            "seat": 4,
        },
    )
    assert response.status_code == 200
    data = response.json()["data"]
    speaker = next(item for item in data["speakers"] if item["id"] == "spk_aff_2")
    agent = next(item for item in data["agent_status"] if item["speaker_id"] == "spk_aff_2")
    assert speaker["name"] == "玄思升级版"
    assert speaker["model_name"] == "Qwen-Plus"
    assert speaker["model_kind"] == "closed_source"
    assert speaker["agent_endpoint"] == "http://127.0.0.1:8100"
    assert speaker["seat"] == 2
    assert agent["name"] == "玄思升级版"
    assert agent["model"] == "Qwen-Plus"

    invalid = client.patch(
        "/api/matches/match_001/speakers/spk_aff_2",
        json={"model_kind": "local"},
    )
    assert invalid.status_code == 409
    assert invalid.json()["error"]["code"] == "invalid_speaker_config"


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
        json={"scene": "teams"},
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
    assert persisted["match"]["screen_scene"] == "teams"

    assert event_row is not None
    assert event_row[0] == "screen.scene_changed"
    assert json.loads(event_row[1])["scene"] == "teams"


def test_audit_logs_can_be_queried_for_admin_actions() -> None:
    action = client.post(
        "/api/matches/match_001/screen/scene",
        json={"scene": "teams"},
    )
    assert action.status_code == 200

    response = client.get("/api/matches/match_001/audit-logs?limit=5")
    assert response.status_code == 200
    items = response.json()["data"]["items"]
    assert items
    assert items[0]["action"] == "screen.scene_changed"
    assert items[0]["actor_type"] == "host"
    assert items[0]["result"] == "success"
    assert items[0]["request"]["scene"] == "teams"


def test_match_export_bundle_contains_core_files() -> None:
    client.post(
        "/api/matches/match_001/screen/scene",
        json={"scene": "teams"},
    )

    response = client.post("/api/matches/match_001/exports")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["download_url"].endswith(f"/{data['export_id']}/download")
    assert Path(data["file_path"]).exists()
    entry_paths = {item["path"] for item in data["entries"]}
    assert {
        "match.json",
        "transcript.json",
        "transcript.csv",
        "events.jsonl",
        "votes.json",
        "audit_logs.jsonl",
        "audio_manifest.json",
    }.issubset(entry_paths)

    download = client.get(data["download_url"])
    assert download.status_code == 200
    with zipfile.ZipFile(io.BytesIO(download.content)) as bundle:
        names = set(bundle.namelist())
        assert "match.json" in names
        assert "transcript.csv" in names
        exported_match = json.loads(bundle.read("match.json"))
        assert exported_match["match"]["id"] == "match_001"


def test_mock_agent_speech_records_final_transcript(monkeypatch) -> None:
    monkeypatch.setenv("PHDEBATE_TTS_FORMAL", "0")

    asyncio.run(store.run_agent_speech("spk_aff_2"))

    response = client.get("/api/matches/match_001")
    assert response.status_code == 200
    data = response.json()["data"]

    assert data["current_speech"] is None
    assert data["recent_transcript"][0]["speaker_id"] == "spk_aff_2"
    assert data["recent_transcript"][0]["source"] == "agent_text"
    assert "可执行" in data["recent_transcript"][0]["text"]
    assert data["free_debate"]["current_turn_side"] == "negative"


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

        async def stream_speech(self, endpoint, payload, fallback_chunks):
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

    data = client.get("/api/matches/match_001").json()["data"]
    assert data["recent_transcript"][0]["text"] == "外部 Agent 流式接入成功。"
    assert next(item for item in data["agent_status"] if item["speaker_id"] == "spk_aff_2")["status"] == "ready"


def test_agent_speech_formal_tts_archives_audio(monkeypatch, tmp_path) -> None:
    class FakeGateway:
        def __init__(self, url: str) -> None:
            self.url = url

        async def synthesize(self, text: str) -> TTSResult:
            assert "可执行" in text
            return TTSResult(audio=b"agent-mp3", mime_type="audio/mpeg", latency_ms=321, chunk_count=1)

    monkeypatch.setenv("PHDEBATE_TTS_FORMAL", "1")
    monkeypatch.setenv("PHDEBATE_AUDIO_DIR", str(tmp_path / "audio"))
    monkeypatch.setattr("app.services.match_store.XfyunTTSGateway", FakeGateway)

    asyncio.run(store.run_agent_speech("spk_aff_2"))

    data = client.get("/api/matches/match_001").json()["data"]
    asset = data["audio_assets"][0]
    assert asset["speaker_id"] == "spk_aff_2"
    assert asset["speech_id"] == data["recent_transcript"][0]["speech_id"]
    assert asset["source"] == "agent_tts"
    assert asset["status"] == "completed"
    assert asset["mime_type"] == "audio/mpeg"
    assert asset["size_bytes"] == len(b"agent-mp3")
    assert Path(asset["chunks"][0]["file_path"]).read_bytes() == b"agent-mp3"
    assert data["speech_service"]["tts"]["status"] == "idle"
    assert data["speech_service"]["tts"]["latency_ms"] == 321


def test_agent_speech_formal_tts_failure_keeps_text_transcript(monkeypatch) -> None:
    class FailingGateway:
        def __init__(self, url: str) -> None:
            self.url = url

        async def synthesize(self, text: str) -> TTSResult:
            raise XfyunGatewayError("tts unavailable", code=500)

    monkeypatch.setenv("PHDEBATE_TTS_FORMAL", "1")
    monkeypatch.setattr("app.services.match_store.XfyunTTSGateway", FailingGateway)

    asyncio.run(store.run_agent_speech("spk_aff_2"))

    data = client.get("/api/matches/match_001").json()["data"]
    assert data["recent_transcript"][0]["speaker_id"] == "spk_aff_2"
    assert "可执行" in data["recent_transcript"][0]["text"]
    assert data["speech_service"]["tts"]["status"] == "failed"
    assert data["speech_service"]["tts"]["degraded_to"] == "text_only"
    assert data["current_speech"] is None


def test_agent_health_check_updates_agent_status() -> None:
    class FakeHealthGateway:
        def endpoint_for(self, speaker):
            return speaker.get("agent_endpoint") or "http://fake-agent"

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


def test_agent_retry_rejects_invalid_phase_speaker() -> None:
    phase = client.post("/api/matches/match_001/phases/phase_aff_constructive_1/start")
    assert phase.status_code == 200

    response = client.post("/api/matches/match_001/agent/spk_aff_2/retry")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "invalid_speaker"


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
        json={"remaining_ms": 60000, "reason": "test_adjust"},
    )
    assert adjusted.status_code == 200
    turn = next(item for item in adjusted.json()["data"]["clocks"] if item["name"] == "turn")
    assert turn["state"] == "running"
    assert 59000 <= turn["remaining_ms"] <= 60000
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


def test_audience_vote_rejects_duplicate_token() -> None:
    payload = {"token": "student-dup", "winner_side": "affirmative", "best_speaker_id": "spk_aff_3"}
    first = client.post("/api/public/matches/match_001/audience-votes", json=payload)
    assert first.status_code == 200

    second = client.post("/api/public/matches/match_001/audience-votes", json=payload)
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "duplicate_vote"


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

    judge = client.post("/api/matches/match_001/votes/publish", json={"scope": "judge"})
    assert judge.status_code == 200
    assert judge.json()["data"]["vote_state"]["judge_published"] is True

    audience = client.post("/api/matches/match_001/votes/publish", json={"scope": "audience"})
    assert audience.status_code == 200
    data = audience.json()["data"]["vote_state"]
    assert data["judge_published"] is True
    assert data["audience_published"] is True


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


def test_audio_chunk_upload_archives_file_and_completes_asset() -> None:
    response = client.post(
        "/api/matches/match_001/speeches/speech_live/audio-chunks",
        data={"speaker_id": "spk_aff_3", "chunk_index": "0", "duration_ms": "500"},
        files={"file": ("chunk.webm", b"audio-bytes", "audio/webm")},
    )
    assert response.status_code == 200
    data = response.json()["data"]
    asset = data["audio_assets"][0]
    assert asset["speech_id"] == "speech_live"
    assert asset["speaker_id"] == "spk_aff_3"
    assert asset["chunk_count"] == 1
    assert asset["size_bytes"] == len(b"audio-bytes")
    assert asset["duration_ms"] == 500
    assert asset["status"] == "recording"
    assert Path(asset["chunks"][0]["file_path"]).exists()

    complete = client.post(
        "/api/matches/match_001/speeches/speech_live/audio/complete",
        json={"speaker_id": "spk_aff_3"},
    )
    assert complete.status_code == 200
    assert complete.json()["data"]["audio_assets"][0]["status"] == "completed"


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
    monkeypatch.setattr("app.services.match_store.XfyunASRGateway", FakeGateway)

    first = client.post(
        "/api/matches/match_001/speeches/speech_live/audio-chunks",
        data={"speaker_id": "spk_aff_3", "chunk_index": "0", "duration_ms": "500"},
        files={"file": ("chunk-0.pcm", b"pcm-audio-0", "audio/L16")},
    )
    assert first.status_code == 200
    first_asset = first.json()["data"]["audio_assets"][0]
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
    data = response.json()["data"]
    asset = data["audio_assets"][0]
    assert asset["mime_type"] == "audio/L16;rate=16000"
    assert data["speech_service"]["asr"]["status"] == "streaming"
    assert data["speech_service"]["asr"]["active_sessions"] == 1
    assert "receiving PCM/L16" in data["speech_service"]["asr"]["detail"]


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
    monkeypatch.setattr("app.services.match_store.XfyunASRGateway", FakeGateway)

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
    monkeypatch.setattr("app.services.match_store.XfyunASRGateway", FakeGateway)

    first = client.post(
        "/api/matches/match_001/speeches/speech_live/audio-chunks",
        data={"speaker_id": "spk_aff_3", "chunk_index": "0", "duration_ms": "500"},
        files={"file": ("chunk-0.pcm", b"pcm-0", "audio/L16;rate=16000")},
    )

    assert first.status_code == 200
    data = first.json()["data"]
    assert data["current_speech"]["content_partial"] == "实时 partial 1"
    assert data["recent_transcript"][0]["is_final"] is False
    assert data["audio_assets"][0]["asr_realtime_status"] == "streaming"

    second = client.post(
        "/api/matches/match_001/speeches/speech_live/audio-chunks",
        data={"speaker_id": "spk_aff_3", "chunk_index": "1", "duration_ms": "500"},
        files={"file": ("chunk-1.pcm", b"pcm-1", "audio/L16;rate=16000")},
    )
    assert second.status_code == 200
    assert second.json()["data"]["current_speech"]["content_partial"] == "实时 partial 2"

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
