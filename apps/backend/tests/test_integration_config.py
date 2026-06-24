import json

from app.services import integration_config as ic


def test_local_qwen_tts_runtime_config_migrates_to_stable_four_voice_profile(monkeypatch, tmp_path) -> None:
    config_file = tmp_path / "integration.json"
    config_file.write_text(
        json.dumps(
            {
                "tts": {
                    "provider": "local_qwen",
                    "enabled": True,
                    "endpoint": "http://127.0.0.1:12302",
                    "settings": {
                        "speech_rate": 0.9,
                        "temperature": 0.8,
                        "top_p": 1.0,
                        "min_segment_chars": 120,
                        "max_segment_chars": 220,
                    },
                },
                "voice_presets": [
                    {
                        "id": "legacy_adien",
                        "name": "旧 Adien",
                        "provider": "local_qwen",
                        "model": "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
                        "voice": "adien",
                        "speech_rate": 0.8,
                        "volume": 40,
                        "temperature": 0.9,
                        "enabled": False,
                        "is_default": False,
                    },
                    {
                        "id": "legacy_eric",
                        "name": "旧 Eric",
                        "provider": "local_qwen",
                        "model": "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
                        "voice": "eric",
                        "enabled": True,
                        "is_default": True,
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(ic, "_under_pytest", lambda: False)

    store = ic.IntegrationConfigStore(path=config_file)
    public = store.public()

    settings = public["tts"]["settings"]
    assert settings["speech_rate"] == 1.4
    assert settings["temperature"] == 0.05
    assert settings["top_p"] == 0.5
    assert settings["chunk_size"] == 8
    assert settings["max_new_tokens"] == 2048
    assert settings["min_segment_chars"] == 32
    assert settings["max_segment_chars"] == 72

    local_presets = [item for item in public["voice_presets"] if item["provider"] == "local_qwen"]
    voices = {item["voice"] for item in local_presets}
    assert "eric" not in voices
    assert {"aiden", "ryan", "dylan", "sohee"}.issubset(voices)
    migrated = next(item for item in local_presets if item["id"] == "legacy_adien")
    assert migrated["voice"] == "aiden"
    assert migrated["enabled"] is True
    assert migrated["speech_rate"] == 1.4
    assert migrated["volume"] == 70
    assert migrated["temperature"] == 0.05

    on_disk = json.loads(config_file.read_text(encoding="utf-8"))
    assert on_disk["tts"]["settings"]["speech_rate"] == 1.4
    assert all(item.get("voice") != "eric" for item in on_disk["voice_presets"])
