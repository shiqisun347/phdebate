"""ASR / TTS 接入配置的运行时存储。

需求 2.md：TTS/ASR 的管理放在 admin 页面，可添加 API 设置，并能选择是否启用。

设计：
- 配置（endpoint / 发音人 / 语种 / 是否启用 / 密钥）以本存储为准；
- 任何变更都同步写入 `os.environ`（XFYUN_*），使既有按环境变量读取的网关无需改动即可生效；
- `enabled=False` 时把对应 URL 置空，网关自然降级（与未配置等价）；
- 非空密钥才覆盖，读取只返回脱敏状态；明文密钥落在 gitignored 的存储文件，不进仓库/前端。
"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Optional


def _under_pytest() -> bool:
    return "pytest" in sys.modules

from app.services.sqlite_repo import project_root

_SECRET_KEYS = ("app_id", "api_key", "api_secret")
_ENV_KEYS = {
    "app_id": "XFYUN_APP_ID",
    "api_key": "XFYUN_API_KEY",
    "api_secret": "XFYUN_API_SECRET",
}


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
        self._apply_to_env()

    # --- seeding / persistence ---

    def _seed_from_env(self) -> Dict[str, Any]:
        secrets = {key: os.getenv(env, "").strip() for key, env in _ENV_KEYS.items()}
        asr_url = os.getenv("XFYUN_ASR_URL", "").strip()
        tts_url = os.getenv("XFYUN_TTS_URL", "").strip()
        return {
            "asr": {
                "enabled": bool(asr_url),
                "provider": "xfyun",
                "endpoint": asr_url,
                "lang": os.getenv("XFYUN_ASR_LANG", "").strip() or "autodialect",
                "secrets": dict(secrets),
            },
            "tts": {
                "enabled": bool(tts_url),
                "provider": "xfyun",
                "endpoint": tts_url,
                "voice": os.getenv("XFYUN_TTS_VOICE", "").strip() or "x6_lingfeiyi_pro",
                "secrets": dict(secrets),
            },
        }

    def _load_file(self) -> None:
        if _under_pytest():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for kind in ("asr", "tts"):
            saved = data.get(kind) or {}
            target = self.config[kind]
            for field in ("enabled", "provider", "endpoint", "lang", "voice"):
                if field in saved:
                    target[field] = saved[field]
            for key in _SECRET_KEYS:
                value = (saved.get("secrets") or {}).get(key)
                if value:
                    target["secrets"][key] = value

    def _save_file(self) -> None:
        if _under_pytest():
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.config, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _apply_to_env(self) -> None:
        asr = self.config["asr"]
        tts = self.config["tts"]
        # enabled=False -> 置空 URL，网关降级
        os.environ["XFYUN_ASR_URL"] = asr["endpoint"] if asr.get("enabled") else ""
        os.environ["XFYUN_TTS_URL"] = tts["endpoint"] if tts.get("enabled") else ""
        os.environ["XFYUN_ASR_LANG"] = asr.get("lang") or "autodialect"
        os.environ["XFYUN_TTS_VOICE"] = tts.get("voice") or "x6_lingfeiyi_pro"
        # 密钥（ASR/TTS 共用一套；以非空者为准）
        for key, env in _ENV_KEYS.items():
            value = tts["secrets"].get(key) or asr["secrets"].get(key) or ""
            os.environ[env] = value

    # --- public API ---

    def public(self) -> Dict[str, Any]:
        """脱敏视图，供前端展示与编辑。"""
        def view(section: Dict[str, Any]) -> Dict[str, Any]:
            secrets = section.get("secrets", {})
            return {
                "enabled": bool(section.get("enabled")),
                "provider": section.get("provider", "xfyun"),
                "endpoint": section.get("endpoint", ""),
                "lang": section.get("lang"),
                "voice": section.get("voice"),
                "secrets": {
                    key: {"configured": bool(secrets.get(key)), "redacted": "********" if secrets.get(key) else ""}
                    for key in _SECRET_KEYS
                },
            }

        with self._lock:
            return {"asr": view(self.config["asr"]), "tts": view(self.config["tts"])}

    def update(self, body: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            for kind in ("asr", "tts"):
                patch = body.get(kind)
                if not isinstance(patch, dict):
                    continue
                target = self.config[kind]
                if "enabled" in patch:
                    target["enabled"] = bool(patch["enabled"])
                for field in ("endpoint", "lang", "voice", "provider"):
                    if field in patch and patch[field] is not None:
                        target[field] = str(patch[field]).strip()
                secrets = patch.get("secrets")
                if isinstance(secrets, dict):
                    for key in _SECRET_KEYS:
                        value = secrets.get(key)
                        if value:  # 空值表示不修改
                            target["secrets"][key] = str(value).strip()
            self._apply_to_env()
            self._save_file()
        return self.public()


integration_config = IntegrationConfigStore()
