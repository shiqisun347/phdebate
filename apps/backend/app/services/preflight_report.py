from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List


Status = str


def build_preflight_report(snapshot: Dict[str, Any], diagnostics: Dict[str, Any]) -> Dict[str, Any]:
    sections = [
        _core_section(snapshot),
        _clients_section(snapshot),
        _agent_section(snapshot),
        _speech_section(snapshot, diagnostics),
        _vote_section(snapshot),
        _export_section(snapshot),
        _security_section(),
    ]
    checks = [check for section in sections for check in section["checks"]]
    score = {
        "ok": sum(1 for item in checks if item["status"] == "ok"),
        "warn": sum(1 for item in checks if item["status"] == "warn"),
        "fail": sum(1 for item in checks if item["status"] == "fail"),
        "total": len(checks),
    }
    overall_status = _rollup([section["status"] for section in sections])
    return {
        "checked_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "overall_status": overall_status,
        "summary": _summary(overall_status, score),
        "score": score,
        "sections": sections,
        "next_actions": _next_actions(checks),
    }


def _core_section(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    match = snapshot.get("match", {})
    phases = snapshot.get("phases", [])
    speakers = snapshot.get("speakers", [])
    human_count = len([item for item in speakers if item.get("speaker_type") == "human"])
    agent_count = len([item for item in speakers if item.get("speaker_type") == "agent"])
    persistence = ((snapshot.get("system") or {}).get("persistence") or {})
    checks = [
        _check(
            "match_status",
            "比赛状态",
            "fail" if match.get("status") == "intervention" else "ok" if match.get("status") in {"ready", "running", "paused"} else "warn",
            f"{match.get('status', 'unknown')} · {match.get('screen_scene', '-')}/{match.get('live_mode', '-')}",
            "正式开始前建议处于 ready/running/paused，避免 intervention 或 draft。",
        ),
        _check(
            "format",
            "赛制与辩手",
            "ok" if len(phases) == 10 and human_count == 4 and agent_count == 4 else "warn",
            f"{len(phases)} 个环节，{human_count} 位人类，{agent_count} 位 AI",
            "确认赛制、队伍和辩手配置与现场方案一致。",
        ),
        _check(
            "persistence",
            "持久化",
            "ok" if persistence.get("driver") == "sqlite" else "fail",
            persistence.get("database_path") or "未检测到 SQLite",
            "启用 SQLite 快照、事件和导出，避免刷新后状态丢失。",
        ),
    ]
    return _section("core", "比赛基础", checks)


def _clients_section(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    speech_service = snapshot.get("speech_service", {})
    consoles = speech_service.get("consoles", {})
    total = int(consoles.get("total") or 0)
    online = int(consoles.get("online") or 0)
    mic_errors = consoles.get("mic_errors") or []
    screen = speech_service.get("screen", {})
    checks = [
        _check(
            "screen",
            "大屏连接",
            "ok" if screen.get("status") == "connected" else "warn",
            str(screen.get("status") or "unknown"),
            "确认投影电脑打开大屏页面并保持 WebSocket 在线。",
        ),
        _check(
            "speaker_consoles",
            "辩手端在线",
            "ok" if total and online >= total else "warn" if online else "fail",
            f"{online} / {total} 在线",
            "让四位人类辩手打开自己的控制台页面并保持前台。",
        ),
        _check(
            "microphones",
            "麦克风权限",
            "fail" if mic_errors else "ok" if total and online >= total else "warn",
            "无异常" if not mic_errors else "；".join(f"{item.get('name')}: {item.get('message')}" for item in mic_errors[:3]),
            "逐台检查浏览器麦克风授权和输入设备。",
        ),
    ]
    return _section("clients", "现场页面与设备", checks)


def _agent_section(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    statuses = snapshot.get("agent_status", [])
    failed = [item for item in statuses if item.get("status") == "failed"]
    readyish = [item for item in statuses if item.get("status") in {"ready", "streaming"}]
    stale = [item for item in statuses if int(item.get("last_heartbeat_seconds") or 0) > 10]
    checks = [
        _check(
            "agent_health",
            "AI 辩手健康",
            "fail" if failed else "ok" if readyish and len(readyish) == len(statuses) else "warn",
            f"{len(readyish)} / {len(statuses)} 可用" + (f"，失败：{', '.join(item.get('name', '-') for item in failed)}" if failed else ""),
            "在管理端点击 AI 辩手“全部检查”，失败项可重试或准备人工代输入。",
        ),
        _check(
            "agent_heartbeat",
            "Agent 心跳",
            "warn" if stale else "ok",
            "无超时" if not stale else "；".join(f"{item.get('name')}: {item.get('last_heartbeat_seconds')}s" for item in stale),
            "确认 Agent 服务和局域网可达，必要时切换 mock/人工代输入。",
        ),
    ]
    return _section("agents", "AI Agent", checks)


def _speech_section(snapshot: Dict[str, Any], diagnostics: Dict[str, Any]) -> Dict[str, Any]:
    speech_service = snapshot.get("speech_service", {})
    asr = speech_service.get("asr", {})
    tts = speech_service.get("tts", {})
    archive = diagnostics.get("audio_archive", {})
    realtime = diagnostics.get("realtime_asr", {})
    auto_recognize = diagnostics.get("auto_recognize", {})
    formal_tts = diagnostics.get("formal_tts", {})
    checks = [
        _check(
            "asr_status",
            "ASR 状态",
            "fail" if asr.get("status") == "failed" else "ok",
            f"{asr.get('status', 'unknown')} · {asr.get('latency_ms', 0)}ms · {asr.get('detail', '')}",
            "若 ASR 标红，先用 ASR 自检定位账号、网络或音频格式问题。",
        ),
        _check(
            "tts_status",
            "TTS 状态",
            "fail" if tts.get("status") == "failed" else "ok",
            f"{tts.get('status', 'unknown')} · {tts.get('detail', '')}",
            "运行 TTS 试合成，并确认扩声设备可播放。",
        ),
        _check(
            "speech_diagnostics",
            "讯飞配置",
            "ok" if diagnostics.get("overall_status") == "ready" else "warn" if diagnostics.get("overall_status") == "mock_fallback" else "fail",
            f"{diagnostics.get('overall_status')} · realtime {'on' if realtime.get('enabled') else 'off'} · auto {'on' if auto_recognize.get('enabled') else 'manual'} · TTS {'formal' if formal_tts.get('enabled') else 'text'}",
            "正式上场前补齐讯飞 ASR/TTS 环境变量，或确认降级方案可接受。",
        ),
        _check(
            "audio_archive",
            "音频归档目录",
            "ok" if archive.get("status") == "ready" and archive.get("writable") else "fail",
            archive.get("detail", "unknown"),
            "修复 PHDEBATE_AUDIO_DIR 权限，确保赛后复盘包能包含音频索引。",
        ),
    ]
    return _section("speech", "语音链路", checks)


def _vote_section(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    vote_state = snapshot.get("vote_state", {})
    judge_summary = vote_state.get("judge_summary", {})
    best_speaker_id = vote_state.get("best_speaker_id") or judge_summary.get("best_speaker_id")
    checks = [
        _check(
            "audience_vote",
            "学生投票入口",
            "ok" if vote_state.get("window_status") == "open" or int(vote_state.get("audience_count") or 0) > 0 else "warn",
            f"{vote_state.get('window_status')} · {vote_state.get('audience_count', 0)} 票",
            "评委点评前确认二维码可打开，进入评委点评页会开启学生投票。",
        ),
        _check(
            "judge_vote",
            "评委票录入",
            "ok" if best_speaker_id and vote_state.get("winner_side") else "warn",
            f"winner={vote_state.get('winner_side')} · best={best_speaker_id or '-'}",
            "先公布评委结果进入官方结果页，再公布学生投票结果。",
        ),
    ]
    return _section("votes", "投票与结果", checks)


def _export_section(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    transcript = [item for item in snapshot.get("recent_transcript", []) if item.get("valid") is not False]
    audio_assets = snapshot.get("audio_assets", [])
    checks = [
        _check(
            "transcript",
            "发言记录",
            "ok" if transcript else "warn",
            f"{len(transcript)} 条有效 transcript",
            "彩排至少产生一条 ASR/AI/人工发言，确认导出包可复盘。",
        ),
        _check(
            "audio_assets",
            "音频记录",
            "ok" if any(int(item.get("chunk_count") or 0) > 0 for item in audio_assets) else "warn",
            f"{len(audio_assets)} 条音频归档",
            "至少用一台辩手端完成开始/结束发言，确认音频分片落盘。",
        ),
    ]
    return _section("exports", "复盘导出", checks)


def _security_section() -> Dict[str, Any]:
    env = os.getenv("PHDEBATE_ENV", "development").strip().lower()
    token_file = os.getenv("PHDEBATE_TOKEN_FILE", "").strip()
    admin = os.getenv("PHDEBATE_ADMIN_PASSWORD", "").strip()
    host = os.getenv("PHDEBATE_HOST_PASSWORD", "").strip()
    screen = os.getenv("PHDEBATE_SCREEN_TOKEN", "").strip()
    speaker = os.getenv("PHDEBATE_SPEAKER_TOKEN", "").strip() or os.getenv("PHDEBATE_SPEAKER_TOKENS", "").strip()
    configured = bool(token_file or (admin and host and screen and speaker))
    checks = [
        _check(
            "auth_mode",
            "访问鉴权",
            "ok" if env == "production" and configured else "warn" if env != "production" else "fail",
            f"{env} · {'token configured' if configured else 'token missing'}",
            "正式比赛建议使用 PHDEBATE_ENV=production 和 token/hash token 文件。",
        )
    ]
    return _section("security", "安全与部署", checks)


def _check(identifier: str, label: str, status: Status, detail: str, action: str) -> Dict[str, str]:
    return {
        "id": identifier,
        "label": label,
        "status": status,
        "detail": detail.strip() or "-",
        "action": action,
    }


def _section(identifier: str, label: str, checks: List[Dict[str, str]]) -> Dict[str, Any]:
    return {
        "id": identifier,
        "label": label,
        "status": _rollup([item["status"] for item in checks]),
        "checks": checks,
    }


def _rollup(statuses: List[Status]) -> Status:
    if "fail" in statuses:
        return "fail"
    if "warn" in statuses:
        return "warn"
    return "ok"


def _summary(status: Status, score: Dict[str, int]) -> str:
    if status == "ok":
        return f"赛前体检通过：{score['ok']} / {score['total']} 项就绪。"
    if status == "fail":
        return f"赛前体检发现 {score['fail']} 项阻断问题、{score['warn']} 项提醒。"
    return f"赛前体检有 {score['warn']} 项提醒，建议彩排确认。"


def _next_actions(checks: List[Dict[str, str]]) -> List[str]:
    urgent = [item["action"] for item in checks if item["status"] == "fail"]
    if urgent:
        return urgent[:5]
    warnings = [item["action"] for item in checks if item["status"] == "warn"]
    return warnings[:5] or ["当前体检项均已就绪，继续用真实设备做完整彩排。"]
