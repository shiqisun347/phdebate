"""ASR / TTS 接入配置的运行时存储。

配置落在 gitignored 的 storage/integration.json。前端只拿脱敏视图，密钥
只在服务端内存和运行时配置文件中使用。
"""

from __future__ import annotations

import json
import os
import sys
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.services.sqlite_repo import project_root


FORMAL_DEBATE_TTS_INSTRUCTIONS = (
    "正式、平直、清晰、克制，接近现场辩论正常发言；"
    "不要戏剧化，不要抑扬顿挫，不要夸张情绪，不要拖腔，不要口音化，"
    "不要故意拉长字音，保持稳定音量和自然停顿。"
)
FORMAL_DEBATE_TTS_SPEECH_RATE = 1.4
FORMAL_DEBATE_TTS_VOLUME = 70
FORMAL_DEBATE_TTS_PITCH_RATE = 1.0
FORMAL_DEBATE_TTS_TEMPERATURE = 0.05
FORMAL_DEBATE_TTS_TOP_P = 0.5
FORMAL_DEBATE_TTS_TOP_K = 20
FORMAL_DEBATE_TTS_REPETITION_PENALTY = 1.1
FORMAL_DEBATE_TTS_CHUNK_SIZE = 8
FORMAL_DEBATE_TTS_MAX_NEW_TOKENS = 2048
FORMAL_DEBATE_SCREEN_PLAYBACK_RATE = 1.0

LOCAL_QWEN_STABLE_VOICES = {"aiden", "ryan", "dylan", "sohee"}
LOCAL_QWEN_VOICE_ALIASES = {
    "adien": "aiden",
    "aiden": "aiden",
    "ryan": "ryan",
    "dylan": "dylan",
    "sohee": "sohee",
}


ALICLOUD_ASR_DEFAULTS = {
    "provider": "alicloud",
    "enabled": True,
    "endpoint": "wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model=qwen3-asr-flash-realtime",
    "lang": "zh",
    "settings": {
        "model": "qwen3-asr-flash-realtime",
        "input_audio_format": "pcm",
        "sample_rate": 16000,
        "language": "zh",
        "turn_detection": {"type": "server_vad", "threshold": 0.0, "silence_duration_ms": 400},
    },
}

ALICLOUD_TTS_DEFAULTS = {
    "provider": "alicloud",
    "enabled": True,
    "endpoint": "wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model=qwen3-tts-flash-realtime",
    "voice": "Neil",
    "settings": {
        "model": "qwen3-tts-flash-realtime",
        "response_format": "mp3",
        "sample_rate": 24000,
        "mode": "server_commit",
        "language_type": "Chinese",
        "speech_rate": FORMAL_DEBATE_TTS_SPEECH_RATE,
        "volume": FORMAL_DEBATE_TTS_VOLUME,
        "pitch_rate": FORMAL_DEBATE_TTS_PITCH_RATE,
        "screen_playback_rate": FORMAL_DEBATE_SCREEN_PLAYBACK_RATE,
        "instructions": FORMAL_DEBATE_TTS_INSTRUCTIONS,
    },
}

LOCAL_QWEN_ASR_DEFAULTS = {
    "provider": "local_qwen",
    "enabled": True,
    "endpoint": "http://127.0.0.1:12301",
    "lang": "zh",
    "settings": {
        "model": "Qwen/Qwen3-ASR-1.7B",
        "input_audio_format": "pcm",
        "sample_rate": 16000,
        "language": "zh",
        "final_timeout": 30,
    },
}

FUNASR_ASR_DEFAULTS = {
    "provider": "funasr",
    "enabled": True,
    "endpoint": "ws://127.0.0.1:10095",
    "lang": "zh",
    "settings": {
        "model": "FunAudioLLM/Fun-ASR-Nano-2512",
        "input_audio_format": "pcm",
        "sample_rate": 16000,
        "language": "中文",
        "frame_ms": 100,
        "final_timeout": 8,
        "archive_final_timeout": 90,
    },
}

LOCAL_QWEN_TTS_DEFAULTS = {
    "provider": "local_qwen",
    "enabled": True,
    "endpoint": "http://127.0.0.1:12302",
    "voice": "dylan",
    "settings": {
        "model": "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
        "response_format": "mp3",
        "sample_rate": 24000,
        "speech_rate": FORMAL_DEBATE_TTS_SPEECH_RATE,
        "volume": FORMAL_DEBATE_TTS_VOLUME,
        "pitch_rate": FORMAL_DEBATE_TTS_PITCH_RATE,
        "screen_playback_rate": FORMAL_DEBATE_SCREEN_PLAYBACK_RATE,
        "temperature": FORMAL_DEBATE_TTS_TEMPERATURE,
        "top_p": FORMAL_DEBATE_TTS_TOP_P,
        "top_k": FORMAL_DEBATE_TTS_TOP_K,
        "repetition_penalty": FORMAL_DEBATE_TTS_REPETITION_PENALTY,
        "chunk_size": FORMAL_DEBATE_TTS_CHUNK_SIZE,
        "max_new_tokens": FORMAL_DEBATE_TTS_MAX_NEW_TOKENS,
        "stream": True,
        "language_type": "Chinese",
        "instructions": FORMAL_DEBATE_TTS_INSTRUCTIONS,
    },
}

DEFAULT_VOICE_PRESETS = [
    {
        "id": "voice_alicloud_cherry_host",
        "name": "主持 / 系统播报 · Cherry 芊悦",
        "provider": "alicloud",
        "model": "qwen3-tts-flash-realtime",
        "voice": "Cherry",
        "response_format": "mp3",
        "sample_rate": 24000,
        "mode": "server_commit",
        "language_type": "Chinese",
        "speech_rate": FORMAL_DEBATE_TTS_SPEECH_RATE,
        "volume": FORMAL_DEBATE_TTS_VOLUME,
        "pitch_rate": FORMAL_DEBATE_TTS_PITCH_RATE,
        "instructions": FORMAL_DEBATE_TTS_INSTRUCTIONS,
        "enabled": True,
        "is_default": False,
        "description": "亲切自然，适合主持提示和系统播报。",
    },
    {
        "id": "voice_alicloud_neil_debater",
        "name": "AI 辩手男声 · Neil 阿闻",
        "provider": "alicloud",
        "model": "qwen3-tts-flash-realtime",
        "voice": "Neil",
        "response_format": "mp3",
        "sample_rate": 24000,
        "mode": "server_commit",
        "language_type": "Chinese",
        "speech_rate": FORMAL_DEBATE_TTS_SPEECH_RATE,
        "volume": FORMAL_DEBATE_TTS_VOLUME,
        "pitch_rate": FORMAL_DEBATE_TTS_PITCH_RATE,
        "instructions": FORMAL_DEBATE_TTS_INSTRUCTIONS,
        "enabled": True,
        "is_default": True,
        "description": "平直清晰、字正腔圆，适合正式辩论和立论陈词。",
    },
    {
        "id": "voice_alicloud_ethan_debater",
        "name": "AI 辩手男声 · Ethan 晨煦",
        "provider": "alicloud",
        "model": "qwen3-tts-flash-realtime",
        "voice": "Ethan",
        "response_format": "mp3",
        "sample_rate": 24000,
        "mode": "server_commit",
        "language_type": "Chinese",
        "speech_rate": FORMAL_DEBATE_TTS_SPEECH_RATE,
        "volume": FORMAL_DEBATE_TTS_VOLUME,
        "pitch_rate": FORMAL_DEBATE_TTS_PITCH_RATE,
        "instructions": FORMAL_DEBATE_TTS_INSTRUCTIONS,
        "enabled": True,
        "is_default": False,
        "description": "标准普通话，带部分北方口音；作为备用男声保留。",
    },
    {
        "id": "voice_alicloud_serena_summary",
        "name": "AI 辩手女声 · Serena 苏瑶",
        "provider": "alicloud",
        "model": "qwen3-tts-flash-realtime",
        "voice": "Serena",
        "response_format": "mp3",
        "sample_rate": 24000,
        "mode": "server_commit",
        "language_type": "Chinese",
        "speech_rate": FORMAL_DEBATE_TTS_SPEECH_RATE,
        "volume": FORMAL_DEBATE_TTS_VOLUME,
        "pitch_rate": FORMAL_DEBATE_TTS_PITCH_RATE,
        "instructions": FORMAL_DEBATE_TTS_INSTRUCTIONS,
        "enabled": False,
        "is_default": False,
        "description": "已禁用：现场测试音色不稳定。",
    },
    {
        "id": "voice_local_qwen_dylan_debater",
        "name": "本地 Qwen 男声 · Dylan",
        "provider": "local_qwen",
        "model": "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
        "voice": "dylan",
        "response_format": "mp3",
        "sample_rate": 24000,
        "mode": "server_commit",
        "language_type": "Chinese",
        "speech_rate": FORMAL_DEBATE_TTS_SPEECH_RATE,
        "volume": FORMAL_DEBATE_TTS_VOLUME,
        "pitch_rate": FORMAL_DEBATE_TTS_PITCH_RATE,
        "temperature": FORMAL_DEBATE_TTS_TEMPERATURE,
        "top_p": FORMAL_DEBATE_TTS_TOP_P,
        "instructions": FORMAL_DEBATE_TTS_INSTRUCTIONS,
        "enabled": True,
        "is_default": True,
        "description": "本地 Qwen3-TTS 男声，适合 AI 辩手。",
    },
    {
        "id": "voice_local_qwen_vivian_host",
        "name": "本地 Qwen 女声 · Vivian",
        "provider": "local_qwen",
        "model": "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
        "voice": "vivian",
        "response_format": "mp3",
        "sample_rate": 24000,
        "mode": "server_commit",
        "language_type": "Chinese",
        "speech_rate": FORMAL_DEBATE_TTS_SPEECH_RATE,
        "volume": FORMAL_DEBATE_TTS_VOLUME,
        "pitch_rate": FORMAL_DEBATE_TTS_PITCH_RATE,
        "temperature": FORMAL_DEBATE_TTS_TEMPERATURE,
        "top_p": FORMAL_DEBATE_TTS_TOP_P,
        "instructions": FORMAL_DEBATE_TTS_INSTRUCTIONS,
        "enabled": False,
        "is_default": False,
        "description": "已禁用：现场测试音色不稳定。",
    },
    {
        "id": "voice_local_qwen_aiden_debater",
        "name": "本地 Qwen 男声 · Aiden",
        "provider": "local_qwen",
        "model": "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
        "voice": "aiden",
        "response_format": "mp3",
        "sample_rate": 24000,
        "mode": "server_commit",
        "language_type": "Chinese",
        "speech_rate": FORMAL_DEBATE_TTS_SPEECH_RATE,
        "volume": FORMAL_DEBATE_TTS_VOLUME,
        "pitch_rate": FORMAL_DEBATE_TTS_PITCH_RATE,
        "temperature": FORMAL_DEBATE_TTS_TEMPERATURE,
        "top_p": FORMAL_DEBATE_TTS_TOP_P,
        "instructions": FORMAL_DEBATE_TTS_INSTRUCTIONS,
        "enabled": True,
        "is_default": False,
        "description": "本地 Qwen3-TTS 普通男声候选，优先用于现场辩论。",
    },
    {
        "id": "voice_local_qwen_sohee_debater",
        "name": "本地 Qwen 女声 · Sohee",
        "provider": "local_qwen",
        "model": "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
        "voice": "sohee",
        "response_format": "mp3",
        "sample_rate": 24000,
        "mode": "server_commit",
        "language_type": "Chinese",
        "speech_rate": FORMAL_DEBATE_TTS_SPEECH_RATE,
        "volume": FORMAL_DEBATE_TTS_VOLUME,
        "pitch_rate": FORMAL_DEBATE_TTS_PITCH_RATE,
        "temperature": FORMAL_DEBATE_TTS_TEMPERATURE,
        "top_p": FORMAL_DEBATE_TTS_TOP_P,
        "instructions": FORMAL_DEBATE_TTS_INSTRUCTIONS,
        "enabled": True,
        "is_default": False,
        "description": "本地 Qwen3-TTS 普通女声候选，优先用于现场辩论。",
    },
    {
        "id": "voice_local_qwen_ryan_debater",
        "name": "本地 Qwen 男声 · Ryan",
        "provider": "local_qwen",
        "model": "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
        "voice": "ryan",
        "response_format": "mp3",
        "sample_rate": 24000,
        "mode": "server_commit",
        "language_type": "Chinese",
        "speech_rate": FORMAL_DEBATE_TTS_SPEECH_RATE,
        "volume": FORMAL_DEBATE_TTS_VOLUME,
        "pitch_rate": FORMAL_DEBATE_TTS_PITCH_RATE,
        "temperature": FORMAL_DEBATE_TTS_TEMPERATURE,
        "top_p": FORMAL_DEBATE_TTS_TOP_P,
        "instructions": FORMAL_DEBATE_TTS_INSTRUCTIONS,
        "enabled": True,
        "is_default": False,
        "description": "本地 Qwen3-TTS 男声候选，优先用于现场辩论。",
    },
    {
        "id": "voice_local_qwen_serena_summary",
        "name": "本地 Qwen 女声 · Serena",
        "provider": "local_qwen",
        "model": "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
        "voice": "serena",
        "response_format": "mp3",
        "sample_rate": 24000,
        "mode": "server_commit",
        "language_type": "Chinese",
        "speech_rate": FORMAL_DEBATE_TTS_SPEECH_RATE,
        "volume": FORMAL_DEBATE_TTS_VOLUME,
        "pitch_rate": FORMAL_DEBATE_TTS_PITCH_RATE,
        "temperature": FORMAL_DEBATE_TTS_TEMPERATURE,
        "top_p": FORMAL_DEBATE_TTS_TOP_P,
        "instructions": FORMAL_DEBATE_TTS_INSTRUCTIONS,
        "enabled": False,
        "is_default": False,
        "description": "已禁用：现场测试音色不稳定。",
    },
]

_XFYUN_SECRET_KEYS = ("app_id", "api_key", "api_secret")
_ALICLOUD_SECRET_KEYS = ("api_key", "workspace_id")
_XFYUN_ENV_KEYS = {
    "app_id": "XFYUN_APP_ID",
    "api_key": "XFYUN_API_KEY",
    "api_secret": "XFYUN_API_SECRET",
}

def _under_pytest() -> bool:
    return "pytest" in sys.modules


def _default_path() -> Path:
    raw = os.getenv("PHDEBATE_INTEGRATION_FILE", "").strip()
    if raw:
        path = Path(raw)
        return path if path.is_absolute() else project_root() / path
    return project_root() / "apps" / "backend" / "storage" / "integration.json"


class IntegrationConfigStore:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or _default_path()
        self._lock = threading.Lock()
        self.config = self._seed_from_env()
        self._load_file()
        self._normalize()
        self._apply_to_env()
        self._save_file()

    def _seed_from_env(self) -> Dict[str, Any]:
        xfyun_secrets = {key: os.getenv(env, "").strip() for key, env in _XFYUN_ENV_KEYS.items()}
        alicloud_secrets = {
            "api_key": os.getenv("DASHSCOPE_API_KEY", "").strip(),
            "workspace_id": os.getenv("DASHSCOPE_WORKSPACE_ID", "").strip(),
        }
        asr_url = os.getenv("XFYUN_ASR_URL", "").strip()
        tts_url = os.getenv("XFYUN_TTS_URL", "").strip()
        local_asr_url = os.getenv("PHDEBATE_LOCAL_ASR_BASE_URL", "").strip()
        local_tts_url = os.getenv("PHDEBATE_LOCAL_TTS_BASE_URL", "").strip()
        funasr_url = os.getenv("PHDEBATE_FUNASR_ASR_URL", "").strip()
        config = {
            "asr": {**deepcopy(ALICLOUD_ASR_DEFAULTS), "secrets": {"xfyun": dict(xfyun_secrets), "alicloud": dict(alicloud_secrets)}},
            "tts": {**deepcopy(ALICLOUD_TTS_DEFAULTS), "secrets": {"xfyun": dict(xfyun_secrets), "alicloud": dict(alicloud_secrets)}},
            "voice_presets": deepcopy(DEFAULT_VOICE_PRESETS),
        }
        if local_asr_url:
            config["asr"].update({**deepcopy(LOCAL_QWEN_ASR_DEFAULTS), "endpoint": local_asr_url})
        if funasr_url:
            config["asr"].update({**deepcopy(FUNASR_ASR_DEFAULTS), "endpoint": funasr_url})
        if local_tts_url:
            config["tts"].update({**deepcopy(LOCAL_QWEN_TTS_DEFAULTS), "endpoint": local_tts_url})
        if asr_url:
            config["asr"].update(
                {
                    "enabled": True,
                    "provider": "xfyun",
                    "endpoint": asr_url,
                    "lang": os.getenv("XFYUN_ASR_LANG", "").strip() or "autodialect",
                }
            )
        if tts_url:
            config["tts"].update(
                {
                    "enabled": True,
                    "provider": "xfyun",
                    "endpoint": tts_url,
                    "voice": os.getenv("XFYUN_TTS_VOICE", "").strip() or "x6_lingfeiyi_pro",
                }
            )
        return config

    def _load_file(self) -> None:
        if _under_pytest():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(data, dict):
            self.config = self._merge_config(self.config, data)

    def _save_file(self) -> None:
        if _under_pytest():
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.config, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _merge_config(self, base: Dict[str, Any], data: Dict[str, Any]) -> Dict[str, Any]:
        merged = deepcopy(base)
        for kind in ("asr", "tts"):
            saved = data.get(kind) or {}
            if not isinstance(saved, dict):
                continue
            section = merged[kind]
            for field in ("enabled", "provider", "endpoint", "lang", "voice"):
                if field in saved:
                    section[field] = saved[field]
            if isinstance(saved.get("settings"), dict):
                section.setdefault("settings", {}).update(saved["settings"])
            saved_secrets = saved.get("secrets") or {}
            section["secrets"] = self._merge_secrets(section.get("secrets") or {}, saved_secrets)
        if isinstance(data.get("voice_presets"), list):
            merged["voice_presets"] = [item for item in data["voice_presets"] if isinstance(item, dict)]
        return merged

    def _merge_secrets(self, base: Dict[str, Any], saved: Dict[str, Any]) -> Dict[str, Any]:
        secrets = {
            "xfyun": {key: str((base.get("xfyun") or {}).get(key) or "") for key in _XFYUN_SECRET_KEYS},
            "alicloud": {key: str((base.get("alicloud") or {}).get(key) or "") for key in _ALICLOUD_SECRET_KEYS},
        }
        # Legacy shape: {"app_id": "...", "api_key": "...", "api_secret": "..."}.
        for key in _XFYUN_SECRET_KEYS:
            value = saved.get(key)
            if value:
                secrets["xfyun"][key] = str(value).strip()
        for provider, keys in (("xfyun", _XFYUN_SECRET_KEYS), ("alicloud", _ALICLOUD_SECRET_KEYS)):
            provider_values = saved.get(provider)
            if isinstance(provider_values, dict):
                for key in keys:
                    value = provider_values.get(key)
                    if value:
                        secrets[provider][key] = str(value).strip()
        return secrets

    def _normalize(self) -> None:
        for kind, base_defaults in (("asr", ALICLOUD_ASR_DEFAULTS), ("tts", ALICLOUD_TTS_DEFAULTS)):
            section = self.config.setdefault(kind, {})
            provider = self._normalize_provider(section.get("provider"))
            defaults = self._provider_defaults(kind, provider) or base_defaults
            for key, value in defaults.items():
                if key == "settings":
                    continue
                section.setdefault(key, deepcopy(value))
            section["provider"] = provider
            section["enabled"] = bool(section.get("enabled"))
            section["endpoint"] = str(section.get("endpoint") or "").strip()
            section.setdefault("settings", {})
            for key, value in (defaults.get("settings") or {}).items():
                section["settings"].setdefault(key, deepcopy(value))
            if kind == "tts" and provider in {"alicloud", "local_qwen"}:
                for key, value in self._formal_tts_settings(provider).items():
                    if provider == "local_qwen":
                        section["settings"][key] = deepcopy(value)
                    else:
                        section["settings"].setdefault(key, deepcopy(value))
            section["secrets"] = self._merge_secrets(section.get("secrets") or {}, {})
        presets = [self._normalize_voice_preset(item) for item in self.config.get("voice_presets", []) if isinstance(item, dict)]
        seen = {item["id"] for item in presets}
        for item in DEFAULT_VOICE_PRESETS:
            if item["id"] not in seen:
                presets.append(deepcopy(item))
        for item in presets:
            if item.get("provider") in {"alicloud", "local_qwen"}:
                for key, value in self._formal_tts_settings(str(item.get("provider") or "")).items():
                    if item.get(key) in {None, ""}:
                        item[key] = deepcopy(value)
        self._migrate_alicloud_default_voice(presets)
        self._enforce_local_qwen_voice_whitelist(presets)
        if not any(item.get("is_default") and item.get("enabled") for item in presets):
            for item in presets:
                if item.get("enabled"):
                    item["is_default"] = True
                    break
        self.config["voice_presets"] = presets

    def _enforce_local_qwen_voice_whitelist(self, presets: List[Dict[str, Any]]) -> None:
        formal = self._formal_tts_settings("local_qwen")
        presets[:] = [
            item for item in presets
            if not (
                item.get("provider") == "local_qwen"
                and self._canonical_local_qwen_voice(item.get("voice")) not in LOCAL_QWEN_STABLE_VOICES
            )
        ]
        for item in presets:
            if item.get("provider") != "local_qwen":
                continue
            voice = self._canonical_local_qwen_voice(item.get("voice"))
            item["voice"] = voice
            item["model"] = item.get("model") or LOCAL_QWEN_TTS_DEFAULTS["settings"]["model"]
            for key, value in formal.items():
                item[key] = deepcopy(value)
            item["sample_rate"] = 24000
            item["mode"] = "server_commit"
            item["enabled"] = True
            item["is_default"] = voice == "dylan"
            if not str(item.get("description") or "").strip() or str(item.get("description") or "").startswith("已禁用"):
                item["description"] = "本地 Qwen3-TTS 稳定白名单音色，使用统一正式辩论参数。"

    @staticmethod
    def _canonical_local_qwen_voice(value: Any) -> str:
        voice = str(value or "").strip().lower()
        return LOCAL_QWEN_VOICE_ALIASES.get(voice, voice)

    def _formal_tts_settings(self, provider: str) -> Dict[str, Any]:
        settings: Dict[str, Any] = {
            "speech_rate": FORMAL_DEBATE_TTS_SPEECH_RATE,
            "volume": FORMAL_DEBATE_TTS_VOLUME,
            "pitch_rate": FORMAL_DEBATE_TTS_PITCH_RATE,
            "screen_playback_rate": FORMAL_DEBATE_SCREEN_PLAYBACK_RATE,
            "instructions": FORMAL_DEBATE_TTS_INSTRUCTIONS,
            "response_format": "mp3",
            "stream": True,
            "language_type": "Chinese",
            "chunk_size": FORMAL_DEBATE_TTS_CHUNK_SIZE,
            "max_new_tokens": FORMAL_DEBATE_TTS_MAX_NEW_TOKENS,
            "top_k": FORMAL_DEBATE_TTS_TOP_K,
            "repetition_penalty": FORMAL_DEBATE_TTS_REPETITION_PENALTY,
            "stability_mode": "stable",
            "first_segment_chars": 24,
            "min_segment_chars": 32,
            "max_segment_chars": 72,
            "sentence_concurrency": 1,
            "sentence_timeout_s": 60,
            "loudness_normalize": True,
            "loudness_target": -18,
            "loudness_timeout_s": 12,
            "tts_speaking_cps": 5.4,
            "agent_speech_time_factor": 0.78,
            "agent_max_token_margin": 1.0,
        }
        if provider == "local_qwen":
            settings.update(
                {
                    "temperature": FORMAL_DEBATE_TTS_TEMPERATURE,
                    "top_p": FORMAL_DEBATE_TTS_TOP_P,
                }
            )
        return settings

    def _migrate_alicloud_default_voice(self, presets: List[Dict[str, Any]]) -> None:
        neil = next((item for item in presets if item.get("id") == "voice_alicloud_neil_debater"), None)
        if not neil or not neil.get("enabled"):
            return
        alicloud_defaults = [
            item for item in presets
            if item.get("provider") == "alicloud" and item.get("enabled") and item.get("is_default")
        ]
        built_in_defaults = {"voice_alicloud_ethan_debater", "voice_alicloud_neil_debater"}
        custom_defaults = [item for item in alicloud_defaults if item.get("id") not in built_in_defaults]
        should_migrate = not custom_defaults
        if not should_migrate:
            return
        for item in presets:
            if item.get("provider") == "alicloud":
                item["is_default"] = item.get("id") == "voice_alicloud_neil_debater"
        tts = self.config.get("tts") or {}
        if self._normalize_provider(tts.get("provider")) == "alicloud" and str(tts.get("voice") or "").strip() == "Ethan":
            tts["voice"] = "Neil"

    def _normalize_provider(self, value: Any) -> str:
        provider = str(value or "alicloud").strip().lower()
        return provider if provider in {"xfyun", "alicloud", "local_qwen", "funasr"} else "alicloud"

    def _provider_defaults(self, kind: str, provider: str) -> Optional[Dict[str, Any]]:
        if kind == "asr" and provider == "funasr":
            return FUNASR_ASR_DEFAULTS
        if kind == "asr" and provider == "local_qwen":
            return LOCAL_QWEN_ASR_DEFAULTS
        if kind == "tts" and provider == "local_qwen":
            return LOCAL_QWEN_TTS_DEFAULTS
        if provider == "alicloud":
            return ALICLOUD_ASR_DEFAULTS if kind == "asr" else ALICLOUD_TTS_DEFAULTS
        return None

    def _normalize_voice_preset(self, item: Dict[str, Any]) -> Dict[str, Any]:
        voice = str(item.get("voice") or "Ethan").strip() or "Ethan"
        provider = self._normalize_provider(item.get("provider"))
        model = str(item.get("model") or ALICLOUD_TTS_DEFAULTS["settings"]["model"]).strip()
        return {
            "id": str(item.get("id") or f"voice_{provider}_{voice.lower()}").strip(),
            "name": str(item.get("name") or voice).strip(),
            "provider": provider,
            "model": model,
            "voice": voice,
            "response_format": str(item.get("response_format") or item.get("format") or "mp3").strip(),
            "sample_rate": int(item.get("sample_rate") or 24000),
            "mode": str(item.get("mode") or "server_commit").strip(),
            "language_type": str(item.get("language_type") or "Chinese").strip(),
            "speech_rate": float(item.get("speech_rate") or 1.0),
            "volume": self._normalize_tts_volume(item.get("volume")),
            "pitch_rate": float(item.get("pitch_rate") or 1.0),
            "temperature": float(item.get("temperature") if item.get("temperature") not in {None, ""} else FORMAL_DEBATE_TTS_TEMPERATURE),
            "top_p": float(item.get("top_p") if item.get("top_p") not in {None, ""} else FORMAL_DEBATE_TTS_TOP_P),
            "top_k": int(item.get("top_k") or FORMAL_DEBATE_TTS_TOP_K),
            "repetition_penalty": float(item.get("repetition_penalty") or FORMAL_DEBATE_TTS_REPETITION_PENALTY),
            "chunk_size": int(item.get("chunk_size") or FORMAL_DEBATE_TTS_CHUNK_SIZE),
            "max_new_tokens": int(item.get("max_new_tokens") or FORMAL_DEBATE_TTS_MAX_NEW_TOKENS),
            "stream": bool(item.get("stream", True)),
            "instructions": str(item.get("instructions") or "").strip(),
            "enabled": bool(item.get("enabled", True)),
            "is_default": bool(item.get("is_default", False)),
            "description": str(item.get("description") or "").strip(),
        }

    def _normalize_tts_volume(self, value: Any) -> int:
        if value in {None, ""}:
            return 70
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return 70
        # Migrate the temporary old UI semantics where 1.0 meant "normal".
        if 0 < numeric <= 2:
            return 70 if numeric == 1 else max(0, min(100, int(round(numeric * 50))))
        return max(0, min(100, int(round(numeric))))

    def _apply_to_env(self) -> None:
        asr = self.config["asr"]
        tts = self.config["tts"]
        if asr.get("provider") == "xfyun" and asr.get("enabled"):
            os.environ["XFYUN_ASR_URL"] = asr.get("endpoint", "")
        else:
            os.environ["XFYUN_ASR_URL"] = ""
        if tts.get("provider") == "xfyun" and tts.get("enabled"):
            os.environ["XFYUN_TTS_URL"] = tts.get("endpoint", "")
        else:
            os.environ["XFYUN_TTS_URL"] = ""
        os.environ["XFYUN_ASR_LANG"] = asr.get("lang") or "autodialect"
        os.environ["XFYUN_TTS_VOICE"] = tts.get("voice") or "x6_lingfeiyi_pro"
        for key, env in _XFYUN_ENV_KEYS.items():
            os.environ[env] = (tts["secrets"]["xfyun"].get(key) or asr["secrets"]["xfyun"].get(key) or "").strip()
        alicloud_key = tts["secrets"]["alicloud"].get("api_key") or asr["secrets"]["alicloud"].get("api_key") or ""
        if alicloud_key:
            os.environ["DASHSCOPE_API_KEY"] = alicloud_key
        workspace_id = tts["secrets"]["alicloud"].get("workspace_id") or asr["secrets"]["alicloud"].get("workspace_id") or ""
        if workspace_id:
            os.environ["DASHSCOPE_WORKSPACE_ID"] = workspace_id

    def public(self) -> Dict[str, Any]:
        def view(section: Dict[str, Any]) -> Dict[str, Any]:
            return {
                "enabled": bool(section.get("enabled")),
                "provider": section.get("provider", "alicloud"),
                "endpoint": section.get("endpoint", ""),
                "lang": section.get("lang"),
                "voice": section.get("voice"),
                "settings": deepcopy(section.get("settings") or {}),
                "secrets": self._public_secrets(section.get("secrets") or {}),
            }

        with self._lock:
            return {
                "asr": view(self.config["asr"]),
                "tts": view(self.config["tts"]),
                "voice_presets": deepcopy(self.config.get("voice_presets", [])),
            }

    def _public_secrets(self, secrets: Dict[str, Any]) -> Dict[str, Any]:
        legacy = {
            key: {"configured": bool((secrets.get("xfyun") or {}).get(key)), "redacted": "********" if (secrets.get("xfyun") or {}).get(key) else ""}
            for key in _XFYUN_SECRET_KEYS
        }
        return {
            **legacy,
            "xfyun": legacy,
            "alicloud": {
                key: {
                    "configured": bool((secrets.get("alicloud") or {}).get(key)),
                    "redacted": "********" if (secrets.get("alicloud") or {}).get(key) else "",
                }
                for key in _ALICLOUD_SECRET_KEYS
            },
        }

    def update(self, body: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            for kind in ("asr", "tts"):
                patch = body.get(kind)
                if not isinstance(patch, dict):
                    continue
                target = self.config[kind]
                if "enabled" in patch:
                    target["enabled"] = bool(patch["enabled"])
                if "provider" in patch:
                    target["provider"] = self._normalize_provider(patch.get("provider"))
                for field in ("endpoint", "lang", "voice"):
                    if field in patch and patch[field] is not None:
                        target[field] = str(patch[field]).strip()
                if isinstance(patch.get("settings"), dict):
                    target.setdefault("settings", {}).update(patch["settings"])
                if isinstance(patch.get("secrets"), dict):
                    target["secrets"] = self._update_secrets(target["secrets"], patch["secrets"])
            if isinstance(body.get("voice_presets"), list):
                self.config["voice_presets"] = [
                    self._normalize_voice_preset(item) for item in body["voice_presets"] if isinstance(item, dict)
                ]
            self._normalize()
            self._apply_to_env()
            self._save_file()
        return self.public()

    def _update_secrets(self, current: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
        next_secrets = self._merge_secrets(current, {})
        # Legacy Xunfei fields.
        for key in _XFYUN_SECRET_KEYS:
            value = patch.get(key)
            if value:
                next_secrets["xfyun"][key] = str(value).strip()
        for provider, keys in (("xfyun", _XFYUN_SECRET_KEYS), ("alicloud", _ALICLOUD_SECRET_KEYS)):
            values = patch.get(provider)
            if isinstance(values, dict):
                for key in keys:
                    value = values.get(key)
                    if value:
                        next_secrets[provider][key] = str(value).strip()
        return next_secrets

    def active_section(self, kind: str) -> Dict[str, Any]:
        with self._lock:
            return deepcopy(self.config[kind])

    def voice_presets(self) -> List[Dict[str, Any]]:
        with self._lock:
            return deepcopy(self.config.get("voice_presets", []))

    def voice_preset(self, preset_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            for preset in self.config.get("voice_presets", []):
                if preset.get("id") == preset_id:
                    return deepcopy(preset)
            return None

    def default_voice_preset(self, provider: Optional[str] = None) -> Optional[Dict[str, Any]]:
        with self._lock:
            presets = [
                item for item in self.config.get("voice_presets", [])
                if item.get("enabled") and (provider is None or item.get("provider") == provider)
            ]
            for item in presets:
                if item.get("is_default"):
                    return deepcopy(item)
            return deepcopy(presets[0]) if presets else None


integration_config = IntegrationConfigStore()
