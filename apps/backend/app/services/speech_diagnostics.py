from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from app.services.integration_config import integration_config
from app.services.xfyun_adapter import credentials_from_env, xfyun_auth_preview


def build_speech_diagnostics(audio_root: Path) -> Dict[str, Any]:
    asr = _component("asr")
    asr["runtime_config"] = _asr_runtime_config()
    tts = _component("tts")
    archive = _audio_archive_status(audio_root)
    overall_status = _overall_status(asr, tts, archive)
    return {
        "checked_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "overall_status": overall_status,
        "provider": {"asr": asr["provider"], "tts": tts["provider"]},
        "asr": asr,
        "tts": tts,
        "audio_archive": archive,
        "realtime_asr": _feature_status("PHDEBATE_ASR_REALTIME", asr, "PCM/L16 分片会进入当前 ASR 服务商长连接", "PCM/L16 分片只归档和补识别"),
        "auto_recognize": _auto_recognize_status(asr),
        "formal_tts": _feature_status("PHDEBATE_TTS_FORMAL", tts, "AI 正式发言会调用当前 TTS 服务商并归档音频", "AI 正式发言仅展示文字/模拟 TTS 状态"),
        "fallbacks": {
            "mock_agent": True,
            "manual_asr_controls": True,
            "text_only_tts": True,
            "audio_recording_without_asr": archive["writable"],
        },
        "next_steps": _next_steps(asr, tts, archive),
    }


def _auto_recognize_status(asr: Dict[str, Any]) -> Dict[str, Any]:
    return _feature_status("PHDEBATE_ASR_AUTO_RECOGNIZE", asr, "PCM/L16 归档完成后会自动补识别", "PCM/L16 归档完成后需主持人手动识别")


def _feature_status(env_name: str, asr: Dict[str, Any], enabled_detail: str, disabled_detail: str) -> Dict[str, Any]:
    raw = os.getenv("PHDEBATE_ASR_AUTO_RECOGNIZE", "").strip().lower()
    if env_name != "PHDEBATE_ASR_AUTO_RECOGNIZE":
        raw = os.getenv(env_name, "").strip().lower()
    explicit_enabled = raw in {"1", "true", "yes", "on"}
    explicit_disabled = raw in {"0", "false", "no", "off"}
    enabled = explicit_enabled or (asr["status"] == "ready" and not explicit_disabled)
    return {
        "enabled": enabled,
        "mode": "explicit_on" if explicit_enabled else "explicit_off" if explicit_disabled else "auto_when_ready",
        "detail": enabled_detail if enabled else disabled_detail,
    }


def _xfyun_component(component: str, url_var: str) -> Dict[str, Any]:
    required = ["XFYUN_APP_ID", "XFYUN_API_KEY", "XFYUN_API_SECRET", url_var]
    configured = [name for name in required if os.getenv(name, "").strip()]
    missing = [name for name in required if name not in configured]
    status = "ready" if not missing else "missing_config"
    url = os.getenv(url_var, "").strip()
    credentials = credentials_from_env()
    auth_preview = xfyun_auth_preview(url) if status == "ready" and credentials else None
    return {
        "component": component,
        "status": status,
        "configured": configured,
        "missing": missing,
        "url": _redact_url(url),
        "auth_ready": auth_preview is not None,
        "auth_preview": auth_preview,
        "detail": "讯飞配置完整" if status == "ready" else f"缺少 {', '.join(missing)}",
    }


def _component(component: str) -> Dict[str, Any]:
    section = integration_config.active_section(component)
    provider = str(section.get("provider") or "alicloud")
    if not section.get("enabled"):
        return {
            "component": component,
            "provider": provider,
            "status": "disabled",
            "configured": [],
            "missing": [],
            "url": _redact_url(str(section.get("endpoint") or "")),
            "auth_ready": False,
            "detail": f"{component.upper()} 未启用",
        }
    if provider == "xfyun":
        return _xfyun_component_from_section(component, section)
    if provider == "alicloud":
        secrets = (section.get("secrets") or {}).get("alicloud") or {}
        api_key_ready = bool(str(secrets.get("api_key") or os.getenv("DASHSCOPE_API_KEY", "")).strip())
        missing = [] if api_key_ready else ["DASHSCOPE_API_KEY / alicloud.api_key"]
        settings = section.get("settings") or {}
        return {
            "component": component,
            "provider": "alicloud",
            "status": "ready" if api_key_ready else "missing_config",
            "configured": ["alicloud.api_key"] if api_key_ready else [],
            "missing": missing,
            "url": _redact_url(str(section.get("endpoint") or "")),
            "auth_ready": api_key_ready,
            "model": settings.get("model"),
            "detail": "阿里云 DashScope 配置完整" if api_key_ready else "缺少阿里云 DashScope API Key",
        }
    if provider == "funasr":
        endpoint = str(section.get("endpoint") or "").strip()
        settings = section.get("settings") or {}
        missing = [] if endpoint else [f"{component}.endpoint"]
        return {
            "component": component,
            "provider": "funasr",
            "status": "ready" if not missing else "missing_config",
            "configured": ["endpoint"] if endpoint else [],
            "missing": missing,
            "url": _redact_url(endpoint),
            "auth_ready": bool(endpoint),
            "model": settings.get("model"),
            "detail": "本机 FunASR streaming 配置完整" if endpoint else "缺少本机 FunASR WebSocket 地址",
        }
    if provider == "local_qwen":
        endpoint = str(section.get("endpoint") or "").strip()
        settings = section.get("settings") or {}
        missing = [] if endpoint else [f"{component}.endpoint"]
        return {
            "component": component,
            "provider": "local_qwen",
            "status": "ready" if not missing else "missing_config",
            "configured": ["endpoint"] if endpoint else [],
            "missing": missing,
            "url": _redact_url(endpoint),
            "auth_ready": bool(endpoint),
            "model": settings.get("model"),
            "detail": "本机 Qwen 语音服务配置完整" if endpoint else "缺少本机 Qwen 服务地址",
        }
    return {
        "component": component,
        "provider": provider,
        "status": "missing_config",
        "configured": [],
        "missing": [f"{component}.provider"],
        "url": _redact_url(str(section.get("endpoint") or "")),
        "auth_ready": False,
        "detail": f"未知语音服务商：{provider}",
    }


def _xfyun_component_from_section(component: str, section: Dict[str, Any]) -> Dict[str, Any]:
    secrets = section.get("secrets") or {}
    secret = secrets.get("xfyun") if isinstance(secrets.get("xfyun"), dict) else secrets
    names = ["app_id", "api_key", "api_secret"]
    configured = [name for name in names if str(secret.get(name) or "").strip()]
    missing = [f"xfyun.{name}" for name in names if name not in configured]
    if not str(section.get("endpoint") or "").strip():
        missing.append(f"{component}.endpoint")
    status = "ready" if not missing else "missing_config"
    url = str(section.get("endpoint") or "")
    auth_preview = xfyun_auth_preview(url) if status == "ready" and credentials_from_env() else None
    return {
        "component": component,
        "provider": "xfyun",
        "status": status,
        "configured": configured,
        "missing": missing,
        "url": _redact_url(url),
        "auth_ready": status == "ready",
        "auth_preview": auth_preview,
        "detail": "讯飞配置完整" if status == "ready" else f"缺少 {', '.join(missing)}",
    }


def _asr_runtime_config() -> Dict[str, float]:
    return {
        "open_timeout_s": _float_env("XFYUN_ASR_OPEN_TIMEOUT_S", 8.0),
        "close_timeout_s": _float_env("XFYUN_ASR_CLOSE_TIMEOUT_S", 3.0),
        "final_timeout_s": _float_env("XFYUN_ASR_FINAL_TIMEOUT_S", 12.0),
    }


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    return float(raw) if raw else default


def _audio_archive_status(audio_root: Path) -> Dict[str, Any]:
    try:
        audio_root.mkdir(parents=True, exist_ok=True)
        probe = audio_root / ".phdebate-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return {
            "status": "ready",
            "root_path": str(audio_root),
            "writable": True,
            "detail": "音频归档目录可写",
        }
    except Exception as exc:  # pragma: no cover - platform-specific filesystem errors
        return {
            "status": "failed",
            "root_path": str(audio_root),
            "writable": False,
            "detail": f"音频归档目录不可写：{exc}",
        }


def _overall_status(asr: Dict[str, Any], tts: Dict[str, Any], archive: Dict[str, Any]) -> str:
    if archive["status"] != "ready":
        return "failed"
    if asr["status"] == "ready" and tts["status"] == "ready":
        return "ready"
    return "mock_fallback"


def _next_steps(asr: Dict[str, Any], tts: Dict[str, Any], archive: Dict[str, Any]) -> List[str]:
    steps: List[str] = []
    if asr["missing"] or tts["missing"]:
        missing = sorted(set(asr["missing"] + tts["missing"]))
        steps.append(f"补齐语音服务配置：{', '.join(missing)}。")
    if archive["status"] != "ready":
        steps.append("修复音频归档目录权限或设置 PHDEBATE_AUDIO_DIR 到可写路径。")
    if not steps:
        steps.append("配置已就绪，下一步用真实麦克风和 TTS 扩声设备做现场彩排。")
    else:
        steps.append("当前仍可使用 mock/人工降级链路完成流程联调。")
    return steps


def _redact_url(value: str) -> str:
    if not value:
        return ""
    if "?" not in value:
        return value
    return value.split("?", 1)[0] + "?..."
