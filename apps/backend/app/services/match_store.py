from __future__ import annotations

import asyncio
import base64
import csv
import hashlib
import io
import json
import os
import time
import zipfile
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from fastapi import WebSocket, WebSocketDisconnect

from app.services.agent_gateway import AgentGateway, AgentGatewayError
from app.services.integration_config import integration_config
from app.services.sqlite_repo import SQLiteRepository, project_root
from app.services.xfyun_gateway import XfyunASRGateway, XfyunGatewayError, XfyunTTSGateway


def _select_asr_gateway():
    """Pick the RTASR (极速版) gateway for the iFlytek real-time endpoint, otherwise the
    legacy IAT gateway. References the module-level XfyunASRGateway so tests can monkeypatch it."""
    url = os.getenv("XFYUN_ASR_URL", "").strip()
    from app.services.xfyun_rtasr import XfyunRTASRGateway, is_rtasr_url

    if is_rtasr_url(url):
        return XfyunRTASRGateway(url=url)
    return XfyunASRGateway(url=url)


class MatchStateError(Exception):
    def __init__(self, code: str, message: str, details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat().replace("+00:00", "Z")


def to_iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class MatchStore:
    """In-memory MVP store.

    This gives the project a live vertical slice quickly. The service boundary is
    intentionally narrow so a SQLite implementation can replace the internals
    without changing routes or frontend contracts.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._connections: Set[WebSocket] = set()
        self._asr_streams: Dict[str, Any] = {}
        self.repo = SQLiteRepository()
        self.agent_gateway = AgentGateway()
        loaded = self.repo.load_snapshot()
        if loaded:
            self.snapshot = loaded
            self.seq = int(loaded.get("last_seq", 0))
            self._ensure_runtime_fields()
            self.events = self.repo.load_events(loaded["match"]["id"])
        else:
            self.reset_demo()

    def reset_demo(self) -> None:
        now = utc_now()
        self.seq = 1842
        self.events: List[Dict[str, Any]] = []
        self._asr_streams = {}
        self.repo.clear_match_history("match_001")
        # 需求3：重置 demo 时清理多比赛注册表与非活动槽位，保证起点干净
        try:
            for entry in self._load_registry():
                self.repo.delete_app_state(self._match_slot_key(str(entry.get("id"))))
            self.repo.delete_app_state(self._REGISTRY_KEY)
        except Exception:
            pass
        self.snapshot: Dict[str, Any] = {
            "match": {
                "id": "match_001",
                "title": "中科院计算所第一届人机辩论赛",
                "topic": "AI 时代，我们更应该培养编程思维 / 提问思维",
                "affirmative_position": "更应该培养编程思维",
                "negative_position": "更应该培养提问思维",
                "organizer": "中国科学院计算技术研究所",
                "venue": "现场会场",
                "status": "running",
                "screen_scene": "idle",
                "live_mode": "free",
                "current_phase_id": "phase_free_debate",
                "created_at": to_iso(now),
                "updated_at": to_iso(now),
            },
            "teams": [
                {
                    "id": "team_aff",
                    "side": "affirmative",
                    "name": "智码战队",
                    "position": "编程思维",
                    "description": "主张 AI 时代更应该培养编程思维",
                },
                {
                    "id": "team_neg",
                    "side": "negative",
                    "name": "问道战队",
                    "position": "提问思维",
                    "description": "主张 AI 时代更应该培养提问思维",
                },
            ],
            "speakers": self._demo_speakers(),
            "agent_configs": self._demo_agent_configs(now),
            "phases": self._demo_phases(),
            "clocks": [
                {
                    "id": "clock_aff_total",
                    "phase_id": "phase_free_debate",
                    "name": "affirmative_total",
                    "total_seconds": 240,
                    "remaining_ms": 151000,
                    "state": "running",
                    "deadline_at": to_iso(now + timedelta(milliseconds=151000)),
                },
                {
                    "id": "clock_turn",
                    "phase_id": "phase_free_debate",
                    "name": "turn",
                    "total_seconds": 15,
                    "remaining_ms": 11000,
                    "state": "running",
                    "deadline_at": to_iso(now + timedelta(milliseconds=11000)),
                },
                {
                    "id": "clock_neg_total",
                    "phase_id": "phase_free_debate",
                    "name": "negative_total",
                    "total_seconds": 240,
                    "remaining_ms": 185000,
                    "state": "paused",
                    "deadline_at": None,
                },
            ],
            "current_speech": {
                "id": "speech_live",
                "phase_id": "phase_free_debate",
                "speaker_id": "spk_aff_3",
                "side": "affirmative",
                "turn_index": 14,
                "source": "human_asr",
                "content_final": "",
                "content_partial": "对方辩友说提问思维是起点，但请注意，没有编程思维的结构化拆解，你的问题只能停留在表面。",
                "started_at": to_iso(now - timedelta(seconds=4)),
            },
            "free_debate": {
                "current_turn_side": "affirmative",
                "turn_index": 14,
                "assignment_mode": "teammate_control",
            },
            "flow": self._fresh_flow_state(),
            "audio_output": self._fresh_audio_output_state(),
            "recent_transcript": [
                {
                    "id": "seg_003",
                    "phase_id": "phase_free_debate",
                    "speaker_id": "spk_aff_3",
                    "speaker_label": "正方三辩 · 林晚晴",
                    "source": "human_asr",
                    "is_final": False,
                    "turn_index": 14,
                    "valid": True,
                    "invalid_reason": None,
                    "text": "真正能驱动 AI 解决复杂问题的，恰恰是把大问题拆成可执行步骤的能力……",
                    "created_at": to_iso(now),
                },
                {
                    "id": "seg_002",
                    "phase_id": "phase_free_debate",
                    "speaker_id": "spk_neg_3",
                    "speaker_label": "反方三辩 · 穷理",
                    "source": "agent_text",
                    "is_final": True,
                    "turn_index": 13,
                    "valid": True,
                    "invalid_reason": None,
                    "text": "对方辩友混淆了工具与思维：拆解步骤是 AI 的强项，而决定拆什么、为何拆，恰恰来自好的提问。",
                    "created_at": to_iso(now - timedelta(seconds=20)),
                },
                {
                    "id": "seg_001",
                    "phase_id": "phase_free_debate",
                    "speaker_id": "spk_aff_2",
                    "speaker_label": "正方二辩 · 玄思",
                    "source": "agent_text",
                    "is_final": True,
                    "turn_index": 12,
                    "valid": True,
                    "invalid_reason": None,
                    "text": "提问若不落地为可验证的步骤，就只是空中楼阁。编程思维正是让问题可验证的桥梁。",
                    "created_at": to_iso(now - timedelta(seconds=39)),
                },
            ],
            "speech_revisions": [],
            "audio_assets": [],
            "agent_status": [
                {
                    "speaker_id": "spk_aff_2",
                    "name": "玄思",
                    "model": "Qwen-Max",
                    "status": "ready",
                    "last_heartbeat_seconds": 2,
                    "detail": "平均首字 1.8s",
                },
                {
                    "speaker_id": "spk_aff_4",
                    "name": "深思",
                    "model": "DeepSeek-V3",
                    "status": "ready",
                    "last_heartbeat_seconds": 1,
                    "detail": "已预下发总结任务",
                },
                {
                    "speaker_id": "spk_neg_3",
                    "name": "穷理",
                    "model": "Kimi-K2",
                    "status": "streaming",
                    "last_heartbeat_seconds": 1,
                    "detail": "生成中 · 已等待 2.1s",
                },
                {
                    "speaker_id": "spk_neg_1",
                    "name": "启问",
                    "model": "GLM-4-Plus",
                    "status": "failed",
                    "last_heartbeat_seconds": 12,
                    "detail": "心跳超时 12s",
                },
            ],
            "vote_state": {
                "window_status": "open",
                "audience_count": 137,
                "judge_published": False,
                "audience_published": False,
                "winner_side": "affirmative",
                "best_speaker_id": "spk_neg_2",
                "judge_summary": {
                    "constructive": {"affirmative": 2, "negative": 1},
                    "process": {"affirmative": 1, "negative": 2},
                    "conclusion": {"affirmative": 3, "negative": 0},
                    "computed_winner_side": "affirmative",
                    "winner_side": "affirmative",
                    "best_speaker_id": "spk_neg_2",
                },
                "audience_summary": {
                    "total": 137,
                    "winner": {"affirmative": 83, "negative": 54},
                    "best_speaker": [
                        {"speaker_id": "spk_neg_2", "count": 41},
                        {"speaker_id": "spk_aff_3", "count": 35},
                    ],
                },
                "audience_votes": [],
                "used_audience_tokens": [],
            },
            "speech_service": {
                "asr": {"status": "ok", "latency_ms": 600, "active_sessions": 1, "detail": "demo partial"},
                "tts": {"status": "idle", "latency_ms": 0, "queue_size": 0, "speaker_id": None, "detail": ""},
                "screen": {"status": "connected"},
                "consoles": {"online": 4, "total": 4, "mic_errors": []},
            },
            "system": self._system_info(),
            "last_seq": self.seq,
        }
        self._persist_snapshot()

    def _demo_speakers(self) -> List[Dict[str, Any]]:
        return [
            self._speaker("spk_aff_1", "team_aff", "affirmative", 1, "陈思远", "human", None, None),
            self._speaker("spk_aff_2", "team_aff", "affirmative", 2, "玄思", "agent", "Qwen-Max", "closed_source"),
            self._speaker("spk_aff_3", "team_aff", "affirmative", 3, "林晚晴", "human", None, None),
            self._speaker("spk_aff_4", "team_aff", "affirmative", 4, "深思", "agent", "DeepSeek-V3", "open_source"),
            self._speaker("spk_neg_1", "team_neg", "negative", 1, "启问", "agent", "GLM-4-Plus", "closed_source"),
            self._speaker("spk_neg_2", "team_neg", "negative", 2, "赵亦凡", "human", None, None),
            self._speaker("spk_neg_3", "team_neg", "negative", 3, "穷理", "agent", "Kimi-K2", "open_source"),
            self._speaker("spk_neg_4", "team_neg", "negative", 4, "苏明哲", "human", None, None),
        ]

    def _speaker(
        self,
        speaker_id: str,
        team_id: str,
        side: str,
        seat: int,
        name: str,
        speaker_type: str,
        model_name: Optional[str],
        model_kind: Optional[str],
    ) -> Dict[str, Any]:
        speaker = {
            "id": speaker_id,
            "team_id": team_id,
            "side": side,
            "seat": seat,
            "name": name,
            "speaker_type": speaker_type,
            "model_name": model_name,
            "model_kind": model_kind,
            "status": "online" if speaker_type == "human" else "ready",
            "image_url": "",
            "mic_permission": "unknown" if speaker_type == "human" else None,
            "device_label": None,
            "last_seen_at": None,
        }
        if speaker_type == "agent":
            speaker["agent_config_id"] = self._default_agent_config_id(speaker_id)
            speaker["agent_endpoint"] = self._agent_endpoint_for_speaker(speaker_id)
        return speaker

    def _demo_agent_configs(self, now: datetime) -> List[Dict[str, Any]]:
        configs: List[Dict[str, Any]] = []
        for speaker in self._demo_speakers():
            if speaker.get("speaker_type") != "agent":
                continue
            configs.append(self._agent_config_from_speaker(speaker, to_iso(now)))
        return configs

    def _demo_phases(self) -> List[Dict[str, Any]]:
        rows = [
            ("phase_aff_constructive_1", "aff_constructive_1", "正方一辩立论", "constructive", "affirmative", 1, 180),
            ("phase_neg_constructive_1", "neg_constructive_1", "反方一辩立论", "constructive", "negative", 1, 180),
            ("phase_aff_statement_2", "aff_statement_2", "正方二辩陈词", "statement", "affirmative", 2, 90),
            ("phase_neg_statement_2", "neg_statement_2", "反方二辩陈词", "statement", "negative", 2, 90),
            ("phase_aff_statement_3", "aff_statement_3", "正方三辩陈词", "statement", "affirmative", 3, 90),
            ("phase_neg_statement_3", "neg_statement_3", "反方三辩陈词", "statement", "negative", 3, 90),
            ("phase_free_debate", "free_debate", "自由辩论", "free_debate", "neutral", None, 480),
            ("phase_neg_summary_4", "neg_summary_4", "反方四辩总结", "summary", "negative", 4, 180),
            ("phase_aff_summary_4", "aff_summary_4", "正方四辩总结", "summary", "affirmative", 4, 180),
            ("phase_commentary_vote", "commentary_vote", "点评与评委合票", "commentary", "neutral", None, 1020),
        ]
        phases: List[Dict[str, Any]] = []
        for index, (phase_id, key, name, phase_type, side, seat, duration) in enumerate(rows, start=1):
            status = "completed" if index < 7 else "active" if index == 7 else "pending"
            phases.append(
                {
                    "id": phase_id,
                    "phase_key": key,
                    "name": name,
                    "phase_type": phase_type,
                    "display_order": index,
                    "side": side,
                    "speaker_seat": seat,
                    "duration_seconds": duration,
                    "speaker_selector": "free_debate" if phase_type == "free_debate" else "fixed_seat",
                    "status": status,
                }
            )
        return phases

    @staticmethod
    def _seat_from_speaker_text(text: str) -> Optional[int]:
        mapping = {"一辩": 1, "二辩": 2, "三辩": 3, "四辩": 4}
        for label, seat in mapping.items():
            if label in (text or ""):
                return seat
        return None

    def _phases_from_ruleset(self, ruleset: Dict[str, Any]) -> List[Dict[str, Any]]:
        """把赛制规则的流程节点转换为比赛 phase 结构。"""
        phases: List[Dict[str, Any]] = []
        for index, node in enumerate(ruleset.get("flow", []) or [], start=1):
            phase_type = node.get("phase_type") or "statement"
            side = node.get("side") or "neutral"
            seat = self._seat_from_speaker_text(node.get("speaker", "")) if phase_type != "free_debate" else None
            key = node.get("key") or f"phase_{index}"
            phases.append(
                {
                    "id": f"phase_{index}_{key}",
                    "phase_key": key,
                    "name": node.get("name") or f"环节{index}",
                    "phase_type": phase_type,
                    "display_order": index,
                    "side": side,
                    "speaker_seat": seat,
                    "duration_seconds": int(node.get("duration_seconds") or 180),
                    "speaker_selector": "free_debate" if phase_type == "free_debate" else "fixed_seat",
                    "status": "pending",
                }
            )
        return phases

    async def get_snapshot(self) -> Dict[str, Any]:
        async with self._lock:
            self._refresh_clocks()
            snap = deepcopy(self.snapshot)
            snap["last_seq"] = self.seq
            self._sanitize_snapshot(snap)
            snap["xiaoqi"] = self._xiaoqi_public()
            return snap

    @staticmethod
    def _xiaoqi_public() -> Dict[str, Any]:
        """Minimal 小七 public info (name + image) for the big screen scenes."""
        try:
            from app.services.xiaoqi_store import xiaoqi_store

            cfg = xiaoqi_store.public()
            return {
                "name": cfg.get("name") or "小七",
                "image_url": cfg.get("image_url") or "",
                "enabled": bool(cfg.get("enabled", True)),
            }
        except Exception:
            return {"name": "小七", "image_url": "", "enabled": True}

    async def get_audit_logs(self, limit: int = 30) -> List[Dict[str, Any]]:
        async with self._lock:
            match_id = self.snapshot["match"]["id"]
        return self.repo.load_audit_logs(match_id, limit)

    async def get_data_summary(self) -> Dict[str, Any]:
        async with self._lock:
            self._refresh_clocks()
            snapshot = deepcopy(self.snapshot)
            snapshot["last_seq"] = self.seq
            match_id = snapshot["match"]["id"]

        events = self.repo.load_events(match_id, 10000)
        audit_logs = self.repo.load_audit_logs(match_id, 10000)
        archives = self.repo.load_match_archives(12)
        structured_counts = self.repo.load_structured_counts(match_id)
        agent_requests = self.repo.load_agent_requests(match_id, 10000)
        speech_service_requests = self.repo.load_speech_service_requests(match_id, 10000)
        export_bundles = self.repo.load_export_bundles(match_id, 20)
        latest_export = export_bundles[0] if export_bundles else self._latest_export_from_events(events)
        vote_state = snapshot.get("vote_state", {})
        audio_assets = snapshot.get("audio_assets", [])
        transcript = snapshot.get("recent_transcript", [])
        speakers = snapshot.get("speakers", [])

        archive_items = []
        for archive in archives:
            archived_snapshot = archive.get("snapshot", {})
            archived_vote = archived_snapshot.get("vote_state", {})
            archived_audio = archived_snapshot.get("audio_assets", [])
            archived_transcript = archived_snapshot.get("recent_transcript", [])
            export_bundle = archive.get("export_bundle", {}) or {}
            archive_items.append(
                {
                    "id": archive.get("id"),
                    "archived_match_id": archive.get("archived_match_id"),
                    "new_match_id": archive.get("new_match_id"),
                    "created_at": archive.get("created_at"),
                    "title": archived_snapshot.get("match", {}).get("title", ""),
                    "topic": archived_snapshot.get("match", {}).get("topic", ""),
                    "counts": {
                        "transcript_segments": len(archived_transcript),
                        "audio_assets": len(archived_audio),
                        "audience_votes": int(archived_vote.get("audience_count", archived_vote.get("audience_summary", {}).get("total", 0)) or 0),
                    },
                    "export_bundle": self._compact_export_bundle(export_bundle),
                }
            )

        return {
            "generated_at": iso_now(),
            "match": {
                "id": match_id,
                "title": snapshot["match"].get("title", ""),
                "topic": snapshot["match"].get("topic", ""),
                "status": snapshot["match"].get("status", ""),
                "screen_scene": snapshot["match"].get("screen_scene", ""),
                "current_phase_id": snapshot["match"].get("current_phase_id", ""),
            },
            "persistence": snapshot.get("system", {}).get("persistence", self._system_info().get("persistence", {})),
            "counts": {
                "phases": len(snapshot.get("phases", [])),
                "speakers": len(speakers),
                "human_speakers": len([speaker for speaker in speakers if speaker.get("speaker_type") == "human"]),
                "agent_speakers": len([speaker for speaker in speakers if speaker.get("speaker_type") == "agent"]),
                "agent_configs": len(snapshot.get("agent_configs", [])),
                "transcript_segments": len(transcript),
                "final_transcript_segments": len([segment for segment in transcript if segment.get("is_final")]),
                "speech_revisions": len(snapshot.get("speech_revisions", [])),
                "audio_assets": len(audio_assets),
                "audio_chunks": sum(len(asset.get("chunks", [])) for asset in audio_assets),
                "audience_votes": int(vote_state.get("audience_count", vote_state.get("audience_summary", {}).get("total", 0)) or 0),
                "audience_vote_keys": len(vote_state.get("audience_vote_keys", [])) + len(vote_state.get("used_audience_tokens", [])),
                "agent_requests": len(agent_requests),
                "speech_service_requests": len(speech_service_requests),
                "export_bundles": len(export_bundles),
                "events": len(events),
                "audit_logs": len(audit_logs),
                "archives": len(archives),
            },
            "structured_counts": structured_counts,
            "request_health": {
                "agent_status_counts": self._status_counts(agent_requests),
                "speech_service_status_counts": self._status_counts(speech_service_requests),
                "recent_agent_requests": [self._compact_agent_request(item) for item in agent_requests[:5]],
                "recent_speech_service_requests": [
                    self._compact_speech_service_request(item) for item in speech_service_requests[:5]
                ],
                "failed_agent_requests": [
                    self._compact_agent_request(item)
                    for item in agent_requests
                    if item.get("status") in {"failed", "cancelled"}
                ][:5],
                "failed_speech_service_requests": [
                    self._compact_speech_service_request(item)
                    for item in speech_service_requests
                    if item.get("status") in {"failed", "cancelled"}
                ][:5],
            },
            "event_type_counts": self._event_type_counts(events),
            "recent_events": [self._compact_event(event) for event in reversed(events[-30:])],
            "latest_event": events[-1] if events else None,
            "latest_export": self._compact_export_bundle(latest_export),
            "archives": archive_items,
        }

    async def create_export_bundle(self) -> Dict[str, Any]:
        async with self._lock:
            self._refresh_clocks()
            snapshot = deepcopy(self.snapshot)
            snapshot["last_seq"] = self.seq
            self._sanitize_snapshot(snapshot)
            match_id = snapshot["match"]["id"]
            events = deepcopy(self.events)

        if not events:
            events = self.repo.load_events(match_id, 10000)
        audit_logs = self.repo.load_audit_logs(match_id, 10000)
        structured = self.repo.load_structured_export(match_id)
        structured_summary = {
            "match_id": match_id,
            "generated_at": iso_now(),
            "source": "sqlite_structured_mirror",
            "counts": {name: len(rows) for name, rows in structured.items()},
        }
        agent_requests = structured.get("agent_requests", []) or self._agent_events_for_export(events)
        speech_service_requests = structured.get("speech_service_requests", [])
        export_dir = self.repo.db_path.parent / "exports" / match_id
        export_dir.mkdir(parents=True, exist_ok=True)
        export_id = f"{match_id}_{utc_now().strftime('%Y%m%dT%H%M%SZ')}_{self.seq}"
        zip_path = export_dir / f"{export_id}.zip"
        transcript = [segment for segment in snapshot.get("recent_transcript", []) if segment.get("valid", True)]
        votes = snapshot.get("vote_state", {})
        audio_assets = snapshot.get("audio_assets", [])

        entries: List[Dict[str, Any]] = []
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as bundle:
            self._zip_writestr(bundle, "match.json", snapshot, entries)
            self._zip_writestr(bundle, "transcript.json", transcript, entries)
            self._zip_writestr(bundle, "transcript.csv", self._transcript_csv(transcript), entries, text=True)
            self._zip_writestr(bundle, "phases.json", structured.get("phases", []), entries)
            self._zip_writestr(bundle, "phases.csv", self._rows_csv(structured.get("phases", []), STRUCTURED_CSV_FIELDS["phases"]), entries, text=True)
            self._zip_writestr(bundle, "speeches.json", structured.get("speeches", []), entries)
            self._zip_writestr(bundle, "speeches.csv", self._rows_csv(structured.get("speeches", []), STRUCTURED_CSV_FIELDS["speeches"]), entries, text=True)
            self._zip_writestr(bundle, "transcripts.json", structured.get("transcript_segments", []), entries)
            self._zip_writestr(
                bundle,
                "transcripts.csv",
                self._rows_csv(structured.get("transcript_segments", []), STRUCTURED_CSV_FIELDS["transcript_segments"]),
                entries,
                text=True,
            )
            self._zip_writestr(bundle, "events.jsonl", self._jsonl(events), entries, text=True)
            self._zip_writestr(bundle, "agent_requests.jsonl", self._jsonl(agent_requests), entries, text=True)
            self._zip_writestr(bundle, "speech_service_requests.jsonl", self._jsonl(speech_service_requests), entries, text=True)
            self._zip_writestr(bundle, "votes.json", votes, entries)
            self._zip_writestr(bundle, "audit_logs.jsonl", self._jsonl(audit_logs), entries, text=True)
            self._zip_writestr(bundle, "audio_manifest.json", audio_assets, entries)
            self._zip_writestr(bundle, "structured/summary.json", structured_summary, entries)
            for name, rows in structured.items():
                self._zip_writestr(bundle, f"structured/{name}.json", rows, entries)
            self._zip_audio_assets(bundle, audio_assets, entries)

        created_at = iso_now()
        payload = {
            "export_id": export_id,
            "match_id": match_id,
            "file_path": str(zip_path),
            "download_url": f"/api/matches/{match_id}/exports/{export_id}/download",
            "size_bytes": zip_path.stat().st_size,
            "entries": entries,
            "created_at": created_at,
        }
        self.repo.save_export_bundle(payload)
        await self.emit("export.created", payload, "admin")
        return payload

    async def export_file_path(self, export_id: str, match_id: Optional[str] = None) -> Path:
        if match_id is None or match_id == "current":
            async with self._lock:
                match_id = self.snapshot["match"]["id"]
        safe_match_id = self._safe_path_part(match_id)
        safe_id = self._safe_path_part(export_id)
        path = self.repo.db_path.parent / "exports" / safe_match_id / f"{safe_id}.zip"
        if not path.exists():
            raise MatchStateError("export_not_found", "未找到指定导出文件。", {"export_id": export_id})
        return path

    async def reset_current_match(self, confirm_text: str) -> Dict[str, Any]:
        if confirm_text != "重置比赛":
            raise MatchStateError("invalid_confirmation", "重置比赛需要输入确认文本“重置比赛”。")

        export_bundle = await self.create_export_bundle()
        async with self._lock:
            self._refresh_clocks()
            archived_snapshot = deepcopy(self.snapshot)
            archived_snapshot["last_seq"] = self.seq
            archived_match_id = archived_snapshot["match"]["id"]
            now = utc_now()
            new_match_id = f"match_{now.strftime('%Y%m%d_%H%M%S_%f')}"
            archive_id = f"archive_{archived_match_id}_{now.strftime('%Y%m%dT%H%M%S%fZ')}"
            new_snapshot = self._new_match_snapshot_from_archive(archived_snapshot, new_match_id, now)

            self.repo.save_match_archive(
                archive_id=archive_id,
                archived_match_id=archived_match_id,
                new_match_id=new_match_id,
                snapshot=archived_snapshot,
                export_bundle=export_bundle,
                created_at=to_iso(now),
            )
            self.seq = 0
            self.events = []
            self._asr_streams = {}
            self.snapshot = new_snapshot
            self._persist_snapshot()

        await self.emit(
            "match.reset",
            {
                "archive_id": archive_id,
                "previous_match_id": archived_match_id,
                "new_match_id": new_match_id,
                "export_id": export_bundle.get("export_id"),
                "export_download_url": export_bundle.get("download_url"),
            },
            "admin",
        )
        return await self.get_snapshot()

    # --- 需求 3：多比赛管理（项目化，可增删改查 + 切换） ---

    _REGISTRY_KEY = "match_registry"

    def _match_slot_key(self, match_id: str) -> str:
        return f"match_snapshot:{match_id}"

    def _registry_entry(self, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        match = snapshot["match"]
        return {
            "id": match["id"],
            "title": match.get("title"),
            "topic": match.get("topic"),
            "status": match.get("status"),
            "screen_scene": match.get("screen_scene"),
            "current_phase_id": match.get("current_phase_id"),
            "created_at": match.get("created_at"),
            "updated_at": iso_now(),
        }

    def _load_registry(self) -> List[Dict[str, Any]]:
        data = self.repo.get_app_state(self._REGISTRY_KEY) or {}
        return data.get("matches", [])

    def _save_registry(self, matches: List[Dict[str, Any]]) -> None:
        self.repo.set_app_state(self._REGISTRY_KEY, {"matches": matches}, iso_now())

    def _upsert_registry(self, snapshot: Dict[str, Any]) -> None:
        entry = self._registry_entry(snapshot)
        matches = [m for m in self._load_registry() if m.get("id") != entry["id"]]
        matches.append(entry)
        self._save_registry(matches)

    def _stash_active(self) -> None:
        """Persist the active match to its own slot + refresh its registry entry."""
        self._persist_snapshot()
        self.repo.set_app_state(self._match_slot_key(self.snapshot["match"]["id"]), self.snapshot, iso_now())
        self._upsert_registry(self.snapshot)

    async def list_matches(self) -> Dict[str, Any]:
        async with self._lock:
            self._upsert_registry(self.snapshot)
            matches = self._load_registry()
            active_id = self.snapshot["match"]["id"]
        for entry in matches:
            entry["active"] = entry.get("id") == active_id
        matches.sort(key=lambda m: m.get("created_at") or "", reverse=True)
        return {"matches": matches, "active_match_id": active_id}

    async def create_match(self, fields: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        fields = fields or {}
        async with self._lock:
            self._stash_active()
            now = utc_now()
            new_id = f"match_{now.strftime('%Y%m%d_%H%M%S_%f')}"
            new_snapshot = self._new_match_snapshot_from_archive(deepcopy(self.snapshot), new_id, now)
            title = str(fields.get("title") or "").strip()
            topic = str(fields.get("topic") or "").strip()
            if title:
                new_snapshot["match"]["title"] = title
            if topic:
                new_snapshot["match"]["topic"] = topic
            for key in (
                "affirmative_position",
                "negative_position",
                "organizer",
                "venue",
            ):
                if key in fields:
                    new_snapshot["match"][key] = fields.get(key)
            ruleset_id = str(fields.get("ruleset_id") or "").strip()
            if ruleset_id:
                from app.services.ruleset_store import ruleset_store

                ruleset = ruleset_store.get(ruleset_id)
                if ruleset:
                    new_snapshot["match"]["ruleset_id"] = ruleset_id
                    new_snapshot["match"]["ruleset_name"] = ruleset.get("name", "")
                    phases = self._phases_from_ruleset(ruleset)
                    if phases:
                        new_snapshot["phases"] = phases
                        new_snapshot["clocks"] = []
                        new_snapshot["current_speech"] = None
                        new_snapshot["match"]["current_phase_id"] = phases[0]["id"]
            self.seq = 0
            self.events = []
            self._asr_streams = {}
            self.snapshot = new_snapshot
            self._persist_snapshot()
            self._upsert_registry(self.snapshot)
        await self.emit("match.created", {"match_id": new_id, "title": new_snapshot["match"]["title"]}, "admin")
        return await self.get_snapshot()

    async def switch_match(self, target_id: str) -> Dict[str, Any]:
        async with self._lock:
            active_id = self.snapshot["match"]["id"]
            switched = target_id != active_id
            if switched:
                target = self.repo.get_app_state(self._match_slot_key(target_id))
                if not target:
                    raise MatchStateError("match_not_found", "未找到该比赛，无法切换。", {"match_id": target_id})
                self._stash_active()
                self.snapshot = target
                self.seq = int(target.get("last_seq", 0))
                self._ensure_runtime_fields()
                self.events = self.repo.load_events(target_id)
                self._asr_streams = {}
                self._persist_snapshot()
                # 已成为活动比赛，移除非活动槽位副本
                self.repo.delete_app_state(self._match_slot_key(target_id))
                self._upsert_registry(self.snapshot)
        if switched:
            await self.emit("match.switched", {"match_id": target_id, "previous_match_id": active_id}, "admin")
        return await self.get_snapshot()

    async def delete_match(self, target_id: str) -> Dict[str, Any]:
        async with self._lock:
            if target_id == self.snapshot["match"]["id"]:
                raise MatchStateError("cannot_delete_active", "不能删除当前比赛，请先切换到其它比赛。", {"match_id": target_id})
            matches = self._load_registry()
            if not any(m.get("id") == target_id for m in matches):
                raise MatchStateError("match_not_found", "未找到该比赛。", {"match_id": target_id})
            self._save_registry([m for m in matches if m.get("id") != target_id])
            self.repo.delete_app_state(self._match_slot_key(target_id))
            self.repo.clear_match_history(target_id)
        await self.emit("match.deleted", {"match_id": target_id}, "admin")
        return await self.list_matches()

    async def emit(
        self,
        event_type: str,
        payload: Dict[str, Any],
        actor_type: str = "system",
        actor_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        async with self._lock:
            self.seq += 1
            match_id = self.snapshot["match"]["id"]
            event = {
                "id": f"evt_{match_id}_{self.seq}",
                "type": event_type,
                "match_id": match_id,
                "seq": self.seq,
                "server_time_ms": int(utc_now().timestamp() * 1000),
                "payload": payload,
                "actor_type": actor_type,
                "actor_id": actor_id,
                "created_at": iso_now(),
            }
            self.events.append(event)
            self.events = self.events[-200:]
            self.snapshot["last_seq"] = self.seq
            self.repo.save_event(event)
            self._persist_snapshot()
            self._save_audit_for_event(event)
            await self._broadcast({"type": event_type, **event})
            return event

    async def set_match_status(self, status: str) -> Dict[str, Any]:
        async with self._lock:
            self.snapshot["match"]["status"] = status
            self.snapshot["match"]["updated_at"] = iso_now()
            if status == "paused":
                self._pause_running_clocks()
                self.snapshot["match"]["screen_scene"] = "paused"
            elif status == "running":
                self._resume_paused_clocks()
                if self.snapshot["match"].get("screen_scene") in {"idle", "paused"}:
                    self.snapshot["match"]["screen_scene"] = "live"
            elif status in {"finished", "intervention"}:
                self._pause_running_clocks()
                self._clear_flow_state()
                if status == "finished":
                    self.snapshot["vote_state"]["window_status"] = "closed"
            self._persist_snapshot()
        event_type = {
            "running": "match.resumed",
            "paused": "match.paused",
            "finished": "match.finished",
            "intervention": "match.intervention_started",
        }.get(status, "match.updated")
        return await self.emit(event_type, {"status": status}, "host")

    async def update_match(self, fields: Dict[str, Any]) -> Dict[str, Any]:
        async with self._lock:
            allowed = {
                "title",
                "topic",
                "affirmative_position",
                "negative_position",
                "organizer",
                "venue",
            }
            for key, value in fields.items():
                if key in allowed:
                    self.snapshot["match"][key] = value
            self.snapshot["match"]["updated_at"] = iso_now()
            self._persist_snapshot()
        return await self.emit("match.updated", {"fields": sorted(set(fields) & allowed)}, "admin")

    async def set_audio_output(self, mode: str, reason: str = "manual", actor_type: str = "host") -> Dict[str, Any]:
        next_mode = self._normalize_audio_output_mode(mode)
        async with self._lock:
            audio_output = self.snapshot.setdefault("audio_output", self._fresh_audio_output_state())
            updated_at = iso_now()
            audio_output.update(
                {
                    "mode": next_mode,
                    "label": self._audio_output_label(next_mode),
                    "updated_by": actor_type,
                    "updated_at": updated_at,
                }
            )
            self.snapshot["match"]["updated_at"] = updated_at
            self._persist_snapshot()
        return await self.emit(
            "audio_output.updated",
            {"mode": next_mode, "label": self._audio_output_label(next_mode), "reason": reason},
            actor_type,
        )

    async def update_team(self, team_id: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        async with self._lock:
            team = self._find_team(team_id)
            allowed = {"name", "position", "description"}
            updated = []
            for key, value in fields.items():
                if key in allowed:
                    team[key] = str(value)
                    updated.append(key)
            self.snapshot["match"]["updated_at"] = iso_now()
            self._persist_snapshot()
        return await self.emit("team.updated", {"team_id": team_id, "fields": sorted(set(updated))}, "admin")

    async def update_speaker(self, speaker_id: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        async with self._lock:
            speaker = self._find_speaker(speaker_id)
            allowed = {"name", "speaker_type", "agent_config_id", "image_url"}
            managed_agent_fields = {"model_name", "model_kind", "agent_endpoint"}
            if managed_agent_fields.intersection(fields):
                raise MatchStateError(
                    "agent_fields_managed_by_config",
                    "Agent 模型、模型类型和请求地址请在 Agent 管理中维护；辩手管理只能绑定已有 Agent 配置。",
                    {"fields": sorted(managed_agent_fields.intersection(fields))},
                )
            updated = []

            if "speaker_type" in fields:
                next_type = fields.get("speaker_type")
                if next_type not in {"human", "agent"}:
                    raise MatchStateError("invalid_speaker_config", "辩手类型必须为 human 或 agent。", {"speaker_type": next_type})
                if next_type != speaker["speaker_type"]:
                    target_config_id = str(fields.get("agent_config_id") or speaker.get("agent_config_id") or "").strip()
                    if next_type == "agent":
                        if not target_config_id:
                            raise MatchStateError(
                                "agent_config_required",
                                "Agent 辩手必须绑定 Agent 管理中已有的配置。",
                                {"speaker_id": speaker_id},
                            )
                        self._find_agent_config(target_config_id)
                    speaker["speaker_type"] = next_type
                    updated.append("speaker_type")
                    if next_type == "human":
                        speaker["model_name"] = None
                        speaker["model_kind"] = None
                        speaker.pop("agent_config_id", None)
                        speaker.pop("agent_endpoint", None)
                        speaker["status"] = "online"
                        speaker["mic_permission"] = "unknown"
                    else:
                        speaker["status"] = "ready"
                        speaker["mic_permission"] = None
                        speaker["agent_config_id"] = target_config_id
                        speaker["model_name"] = None
                        speaker["model_kind"] = None
                        speaker["agent_endpoint"] = ""
                        self._apply_agent_config_to_speaker(speaker)

            for key, value in fields.items():
                if key not in allowed:
                    continue
                if key == "speaker_type":
                    continue
                if key == "agent_config_id":
                    if speaker["speaker_type"] != "agent":
                        continue
                    config_id = str(value or "").strip()
                    if not config_id:
                        raise MatchStateError(
                            "agent_config_required",
                            "Agent 辩手必须绑定 Agent 管理中已有的配置。",
                            {"speaker_id": speaker_id},
                        )
                    self._find_agent_config(config_id)
                    speaker["agent_config_id"] = config_id
                    speaker["agent_endpoint"] = ""
                    self._apply_agent_config_to_speaker(speaker)
                    updated.append(key)
                    continue
                speaker[key] = None if value == "" and key in {"model_name", "model_kind"} else value
                updated.append(key)

            if "agent_config_id" in updated and speaker.get("speaker_type") == "agent" and speaker.get("agent_config_id"):
                self._apply_agent_config_to_speaker(speaker)
            if "speaker_type" in updated or "name" in updated or "agent_config_id" in updated:
                self._sync_agent_status_for_speaker(speaker)
            self.snapshot["match"]["updated_at"] = iso_now()
            self._persist_snapshot()
        return await self.emit("speaker.updated", {"speaker_id": speaker_id, "fields": sorted(set(updated))}, "admin")

    async def get_integration_config(self) -> Dict[str, Any]:
        return integration_config.public()

    async def update_integration_config(self, body: Dict[str, Any]) -> Dict[str, Any]:
        config = integration_config.update(body or {})
        changed = [kind for kind in ("asr", "tts") if isinstance((body or {}).get(kind), dict)]
        await self.emit("integration_config.updated", {"sections": changed}, "admin")
        return config

    async def create_agent_config(self, fields: Dict[str, Any]) -> Dict[str, Any]:
        async with self._lock:
            now = iso_now()
            config = self._normalize_agent_config_fields(fields, now=now, create=True)
            config["id"] = self._unique_agent_config_id(str(config["name"]))
            self.snapshot.setdefault("agent_configs", []).append(config)
            self.snapshot["match"]["updated_at"] = now
            self._persist_snapshot()
        return await self.emit("agent_config.created", {"agent_config_id": config["id"], "name": config["name"]}, "admin")

    async def update_agent_config(self, agent_config_id: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        async with self._lock:
            config = self._find_agent_config(agent_config_id)
            updated = self._normalize_agent_config_fields(fields, existing=config, now=iso_now(), create=False)
            changed_fields = []
            for key, value in updated.items():
                if key == "id":
                    continue
                if config.get(key) != value:
                    config[key] = value
                    changed_fields.append(key)
            config["updated_at"] = iso_now()
            changed_fields.append("updated_at")
            for speaker in self.snapshot.get("speakers", []):
                if speaker.get("agent_config_id") == agent_config_id:
                    self._apply_agent_config_to_speaker(speaker)
                    self._sync_agent_status_for_speaker(speaker)
            self.snapshot["match"]["updated_at"] = config["updated_at"]
            self._persist_snapshot()
        return await self.emit(
            "agent_config.updated",
            {"agent_config_id": agent_config_id, "fields": sorted(set(changed_fields))},
            "admin",
        )

    async def delete_agent_config(self, agent_config_id: str) -> Dict[str, Any]:
        async with self._lock:
            self._find_agent_config(agent_config_id)
            bound_speakers = [
                speaker["id"]
                for speaker in self.snapshot.get("speakers", [])
                if speaker.get("agent_config_id") == agent_config_id
            ]
            if bound_speakers:
                raise MatchStateError(
                    "agent_config_in_use",
                    "该 Agent 配置仍绑定辩手，请先在辩手管理中更换绑定。",
                    {"agent_config_id": agent_config_id, "speaker_ids": bound_speakers},
                )
            self.snapshot["agent_configs"] = [
                config for config in self.snapshot.get("agent_configs", []) if config.get("id") != agent_config_id
            ]
            self.snapshot["match"]["updated_at"] = iso_now()
            self._persist_snapshot()
        return await self.emit("agent_config.deleted", {"agent_config_id": agent_config_id}, "admin")

    async def update_speaker_profile(
        self,
        speaker_id: str,
        fields: Dict[str, Any],
        actor_type: str = "speaker",
        actor_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        name = str(fields.get("name", "")).strip()
        if not name:
            raise MatchStateError("invalid_speaker_profile", "姓名不能为空。", {"speaker_id": speaker_id})
        async with self._lock:
            speaker = self._find_speaker(speaker_id)
            speaker["name"] = name
            if speaker["speaker_type"] == "agent":
                self._sync_agent_status_for_speaker(speaker)
            self.snapshot["match"]["updated_at"] = iso_now()
            self._persist_snapshot()
        return await self.emit(
            "speaker.profile_updated",
            {"speaker_id": speaker_id, "fields": ["name"]},
            actor_type,
            actor_id,
        )

    async def update_phase(self, phase_id: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        async with self._lock:
            phase = self._find_phase(phase_id)
            updated: List[str] = []

            if "name" in fields:
                name = str(fields["name"]).strip()
                if not name:
                    raise MatchStateError("invalid_phase_config", "环节名称不能为空。", {"phase_id": phase_id})
                phase["name"] = name
                updated.append("name")

            if "duration_seconds" in fields:
                duration = self._validated_seconds(fields["duration_seconds"], "duration_seconds", 30, 3600)
                phase["duration_seconds"] = duration
                updated.append("duration_seconds")
                if phase["phase_type"] == "free_debate" and "side_total_seconds" not in fields:
                    phase["side_total_seconds"] = max(1, duration // 2)
                    updated.append("side_total_seconds")

            if phase["phase_type"] == "free_debate":
                if "side_total_seconds" in fields:
                    side_total = self._validated_seconds(fields["side_total_seconds"], "side_total_seconds", 30, 1800)
                    phase["side_total_seconds"] = side_total
                    phase["duration_seconds"] = side_total * 2
                    updated.extend(["side_total_seconds", "duration_seconds"])
                if "turn_seconds" in fields:
                    phase["turn_seconds"] = self._validated_seconds(fields["turn_seconds"], "turn_seconds", 5, 120)
                    updated.append("turn_seconds")

            self.snapshot["match"]["updated_at"] = iso_now()
            if phase_id == self.snapshot["match"]["current_phase_id"]:
                self._sync_current_phase_clocks_after_config(phase)
            self._persist_snapshot()

        return await self.emit(
            "phase.config_updated",
            {"phase_id": phase_id, "fields": sorted(set(updated))},
            "admin",
        )

    async def set_screen_scene(self, scene: str, live_mode: Optional[str]) -> Dict[str, Any]:
        normalized_scene = self._normalize_screen_scene(scene)
        async with self._lock:
            if normalized_scene in {"judge_commentary", "judge_result", "audience_result", "xiaoqi_commentary", "xiaoqi_result"}:
                self._ensure_vote_controls_available("set_result_screen_scene")
            if normalized_scene == "judge_commentary":
                self.snapshot["vote_state"]["window_status"] = "open"
            elif normalized_scene == "judge_result":
                self.snapshot["vote_state"]["window_status"] = "closed"
            elif normalized_scene == "audience_result" and not self.snapshot["vote_state"].get("judge_published"):
                raise MatchStateError(
                    "publish_order",
                    "需要先公布评委结果，再切换到学生投票结果。",
                    {"judge_published": False},
                )
            if normalized_scene in {"judge_commentary", "judge_result", "audience_result", "xiaoqi_commentary", "xiaoqi_result"}:
                self._clear_flow_state()
            self.snapshot["match"]["screen_scene"] = normalized_scene
            if live_mode:
                self.snapshot["match"]["live_mode"] = live_mode
            self.snapshot["match"]["updated_at"] = iso_now()
            self._persist_snapshot()
        return await self.emit("screen.scene_changed", {"scene": normalized_scene, "requested_scene": scene, "live_mode": live_mode}, "host")

    async def start_phase(self, phase_id: str) -> Dict[str, Any]:
        async with self._lock:
            self._ensure_match_allows_control("start_phase")
            phase = self._find_phase(phase_id)
            self.snapshot["current_speech"] = None
            self._clear_flow_state()
            for item in self.snapshot["phases"]:
                if item["id"] == phase_id:
                    item["status"] = "active"
                elif item["status"] == "active":
                    item["status"] = "completed"
            self.snapshot["match"]["current_phase_id"] = phase_id
            self.snapshot["match"]["screen_scene"] = "live"
            self.snapshot["match"]["live_mode"] = "free" if phase["phase_type"] == "free_debate" else "single"
            if phase["phase_type"] == "free_debate":
                self.snapshot["free_debate"] = {
                    "current_turn_side": "affirmative",
                    "turn_index": 1,
                    "assignment_mode": "teammate_control",
                }
            self._reset_clocks_for_phase(phase)
            self._persist_snapshot()
        if phase["phase_type"] == "free_debate":
            # 需求 2.md：自由辩论首轮（正方先手）也给 5s 决定窗口。
            self._arm_free_debate_auto_agent("affirmative", 1)
        return await self.emit("phase.started", {"phase_id": phase_id, "name": phase["name"]}, "host")

    async def skip_phase(self, phase_id: str, reason: str) -> Dict[str, Any]:
        async with self._lock:
            self._ensure_match_allows_control("skip_phase")
            phase = self._find_phase(phase_id)
            if phase_id != self.snapshot["match"]["current_phase_id"]:
                raise MatchStateError(
                    "invalid_phase",
                    "只能跳过当前正在进行的环节。",
                    {"phase_id": phase_id, "current_phase_id": self.snapshot["match"]["current_phase_id"]},
                )
            self.snapshot["current_speech"] = None
            self._clear_flow_state()
            self._pause_running_clocks()
            phase["status"] = "skipped"
            next_phase = self._next_phase(phase)
            if next_phase:
                self.snapshot["match"]["current_phase_id"] = next_phase["id"]
                next_phase["status"] = "active"
                self.snapshot["match"]["screen_scene"] = "live"
                self.snapshot["match"]["live_mode"] = "free" if next_phase["phase_type"] == "free_debate" else "single"
                if next_phase["phase_type"] == "free_debate":
                    self.snapshot["free_debate"] = {
                        "current_turn_side": "affirmative",
                        "turn_index": 1,
                        "assignment_mode": "teammate_control",
                    }
                self._reset_clocks_for_phase(next_phase)
            else:
                self.snapshot["match"]["status"] = "finished"
            self._persist_snapshot()
        return await self.emit(
            "phase.skipped",
            {"phase_id": phase_id, "next_phase_id": next_phase["id"] if next_phase else None, "reason": reason},
            "host",
        )

    async def rollback_phase(self, phase_id: str, reason: str) -> Dict[str, Any]:
        async with self._lock:
            self._ensure_match_allows_control("rollback_phase")
            phase = self._find_phase(phase_id)
            target_order = phase["display_order"]
            self.snapshot["current_speech"] = None
            self._clear_flow_state()
            self._pause_running_clocks()
            for item in self.snapshot["phases"]:
                if item["display_order"] < target_order:
                    item["status"] = "completed"
                elif item["id"] == phase_id:
                    item["status"] = "active"
                else:
                    item["status"] = "pending"
            self.snapshot["match"]["current_phase_id"] = phase_id
            self.snapshot["match"]["screen_scene"] = "live"
            self.snapshot["match"]["live_mode"] = "free" if phase["phase_type"] == "free_debate" else "single"
            if phase["phase_type"] == "free_debate":
                self.snapshot["free_debate"] = {
                    "current_turn_side": "affirmative",
                    "turn_index": 1,
                    "assignment_mode": "teammate_control",
                }
            self._reset_clocks_for_phase(phase)
            invalidated = self._invalidate_transcripts_from_order(target_order, "rollback")
            self._persist_snapshot()
        return await self.emit(
            "phase.rolled_back",
            {"phase_id": phase_id, "reason": reason, "invalidated_transcript_ids": invalidated},
            "host",
        )

    async def activate_speaker(self, speaker_id: str) -> Dict[str, Any]:
        async with self._lock:
            self._ensure_match_allows_control("activate_speaker")
            speaker = self._find_speaker(speaker_id)
            self._ensure_speaker_allowed_for_current_phase(speaker)
            phase_id = self.snapshot["match"]["current_phase_id"]
            self._clear_flow_state()
            self.snapshot["current_speech"] = {
                "id": f"speech_{self.seq + 1}",
                "phase_id": phase_id,
                "speaker_id": speaker_id,
                "side": speaker["side"],
                "turn_index": self._current_turn_index(),
                "source": "agent_text" if speaker["speaker_type"] == "agent" else "human_asr",
                "content_final": "",
                "content_partial": "",
                "started_at": None,
            }
            self.snapshot["match"]["live_mode"] = "prep" if speaker["speaker_type"] == "agent" else self.snapshot["match"]["live_mode"]
            self._persist_snapshot()
        return await self.emit(
            "speaker.activated",
            {"speaker_id": speaker_id, "side": speaker["side"], "speaker_type": speaker["speaker_type"]},
            "host",
        )

    async def record_free_debate_skip(self, speaker_id: str) -> Dict[str, Any]:
        """Human debater skips their turn in free debate; if all humans on side skip, auto-trigger agent."""
        import random
        speaker = self._find_speaker(speaker_id)
        if speaker["speaker_type"] != "human":
            raise MatchStateError("not_human_speaker", "只有人类辩手可以跳过发言。", {"speaker_id": speaker_id})
        phase = self._current_phase()
        if phase["phase_type"] != "free_debate":
            raise MatchStateError("not_free_debate", "跳过功能仅在自由辩论阶段可用。", {"phase_type": phase["phase_type"]})
        self._ensure_match_allows_control("record_free_debate_skip")
        side = speaker["side"]
        current_side = self.snapshot["free_debate"]["current_turn_side"]
        if side != current_side:
            raise MatchStateError("wrong_side", "当前不是你方的发言轮次。", {"expected": current_side, "got": side})
        turn_index = int(self.snapshot["free_debate"]["turn_index"])
        turn_key = f"{side}_{turn_index}"
        async with self._lock:
            skip_votes = self.snapshot["free_debate"].setdefault("skip_votes", {})
            turn_votes: list = skip_votes.setdefault(turn_key, [])
            if speaker_id not in turn_votes:
                turn_votes.append(speaker_id)
            all_humans_on_side = [s["id"] for s in self.snapshot["speakers"] if s["side"] == side and s["speaker_type"] == "human"]
            skipped_all = all(uid in turn_votes for uid in all_humans_on_side)
            self._persist_snapshot()
        await self.emit("free_debate.skip_voted", {"speaker_id": speaker_id, "side": side, "turn_key": turn_key, "skip_count": len(turn_votes), "total_humans": len(all_humans_on_side)}, "system")
        if skipped_all:
            agents_on_side = [s for s in self.snapshot["speakers"] if s["side"] == side and s["speaker_type"] == "agent"]
            if agents_on_side:
                async with self._lock:
                    auto_handled = self.snapshot["free_debate"].setdefault("auto_handled", {})
                    if auto_handled.get(turn_key):
                        return await self.get_snapshot()
                    chosen = random.choice(agents_on_side)
                    auto_handled[turn_key] = chosen["id"]
                    self._persist_snapshot()
                await self.emit("free_debate.auto_agent", {"side": side, "turn_index": turn_index, "speaker_id": chosen["id"], "reason": "all_skipped"}, "system")
                try:
                    self.ensure_agent_speaker_for_current_phase(chosen["id"])
                    asyncio.create_task(self.run_agent_speech(chosen["id"]))
                except Exception:
                    pass
        return await self.get_snapshot()

    async def run_agent_speech(self, speaker_id: str) -> None:
        speaker = self._find_speaker(speaker_id)
        if speaker["speaker_type"] != "agent":
            return
        self._ensure_match_allows_control("run_agent_speech")
        self._ensure_speaker_allowed_for_current_phase(speaker)
        config = self._agent_config_for_speaker(speaker)
        if config and not config.get("enabled", True):
            raise MatchStateError(
                "agent_config_disabled",
                "该 Agent 配置已停用，不能触发发言。",
                {"speaker_id": speaker_id, "agent_config_id": config.get("id")},
            )

        task_id = f"task_{self.seq + 1}"
        speech_id = f"speech_{self.seq + 1}"
        endpoint = self.agent_gateway.endpoint_for(speaker)
        payload = self._build_agent_payload(task_id, speech_id, speaker)
        endpoint_label = endpoint or "embedded://mock"
        agent_started_at = iso_now()
        agent_started_time = time.perf_counter()
        self.repo.save_agent_request_started(
            match_id=payload["match_id"],
            task_id=task_id,
            speech_id=speech_id,
            speaker_id=speaker_id,
            endpoint=endpoint_label,
            request=payload,
            started_at=agent_started_at,
            origin="live",
            **self._log_context(),
        )
        await self.emit(
            "agent.task.created",
            {"task_id": task_id, "speaker_id": speaker_id, "agent_config_id": speaker.get("agent_config_id"), "endpoint": endpoint_label},
            "system",
        )

        async with self._lock:
            phase_id = self.snapshot["match"]["current_phase_id"]
            self.snapshot["match"]["live_mode"] = "prep"
            self._clear_flow_state()
            self.snapshot["current_speech"] = {
                "id": speech_id,
                "phase_id": phase_id,
                "speaker_id": speaker_id,
                "side": speaker["side"],
                "turn_index": self._current_turn_index(),
                "source": "agent_text",
                "content_final": "",
                "content_partial": "",
                "started_at": None,
            }
            self._set_agent_status(speaker_id, "streaming", "Agent task sent")
            self._persist_snapshot()

        await self.emit(
            "speaker.activated",
            {"speaker_id": speaker_id, "side": speaker["side"], "speaker_type": "agent"},
            "system",
        )

        full_text = ""
        playback_started = False
        try:
            async for event in self.agent_gateway.stream_speech(endpoint, payload, self._mock_agent_chunks(speaker), config=config):
                event_type = event.get("type")
                if event_type == "delta":
                    delta = event.get("delta", "")
                    if delta and not playback_started:
                        await self._start_agent_playback(task_id, speaker)
                        playback_started = True
                    full_text += delta
                    async with self._lock:
                        if not self.snapshot.get("current_speech"):
                            self.repo.finish_agent_request(
                                match_id=payload["match_id"],
                                task_id=task_id,
                                status="cancelled",
                                response_text=full_text,
                                latency_ms=max(0, int((time.perf_counter() - agent_started_time) * 1000)),
                                completed_at=iso_now(),
                            )
                            return
                        self.snapshot["current_speech"]["content_partial"] = full_text
                        self._persist_snapshot()
                    await self.emit(
                        "agent.speech.delta",
                        {
                            "task_id": task_id,
                            "speech_id": payload["speech_id"],
                            "speaker_id": speaker_id,
                            "delta": delta,
                            "content": full_text,
                        },
                        "agent",
                        speaker_id,
                    )
                    continue
                if event_type == "final":
                    full_text = event.get("content", full_text)
                    break
        except AgentGatewayError as exc:
            self.repo.finish_agent_request(
                match_id=payload["match_id"],
                task_id=task_id,
                status="failed",
                response_text=full_text or None,
                error_code=exc.code,
                error_message=exc.message,
                latency_ms=max(0, int((time.perf_counter() - agent_started_time) * 1000)),
                completed_at=iso_now(),
            )
            await self._fail_agent_task(task_id, speaker_id, exc)
            return

        self.repo.finish_agent_request(
            match_id=payload["match_id"],
            task_id=task_id,
            status="completed",
            response_text=full_text,
            latency_ms=max(0, int((time.perf_counter() - agent_started_time) * 1000)),
            completed_at=iso_now(),
        )

        if not playback_started:
            await self._start_agent_playback(task_id, speaker)

        tts_result = await self._synthesize_agent_tts(task_id, speech_id, speaker, full_text)

        async with self._lock:
            speech = self.snapshot.get("current_speech")
            if speech:
                speech["content_final"] = full_text
                self._upsert_transcript_segment(speech, speaker_id, full_text, True, "agent_text")
            self.snapshot["current_speech"] = None
            self._pause_running_clocks()
            self._advance_free_debate_turn_if_needed(speaker["side"])
            if tts_result and tts_result.get("failed"):
                self.snapshot["speech_service"]["tts"]["queue_size"] = 0
            else:
                self.snapshot["speech_service"]["tts"] = {
                    "status": "idle",
                    "latency_ms": int((tts_result or {}).get("latency_ms") or 0),
                    "queue_size": 0,
                    "speaker_id": None,
                    "detail": self._tts_completion_detail(tts_result),
                }
            self._set_agent_status(speaker_id, "ready", "last task completed")
            self._persist_snapshot()

        await self.emit(
            "agent.speech.final",
            {"task_id": task_id, "speech_id": payload["speech_id"], "speaker_id": speaker_id, "content": full_text},
            "agent",
            speaker_id,
        )
        finished_payload = {"task_id": task_id, "speaker_id": speaker_id}
        if tts_result:
            finished_payload.update(
                {
                    "speech_id": speech_id,
                    "status": "failed" if tts_result.get("failed") else "completed",
                    "audio_asset_id": tts_result.get("audio_asset_id"),
                    "latency_ms": tts_result.get("latency_ms", 0),
                    "degraded_to": tts_result.get("degraded_to"),
                }
            )
        await self.emit("tts.finished", finished_payload, "system")
        await self.emit("speech.ended", {"speaker_id": speaker_id, "side": speaker["side"]}, "agent", speaker_id)

    async def run_mock_agent_speech(self, speaker_id: str) -> None:
        await self.run_agent_speech(speaker_id)

    async def run_agent_command(self, speaker_id: str, command: str, prompt: str = "") -> Dict[str, Any]:
        speaker = self._find_speaker(speaker_id)
        if speaker["speaker_type"] != "agent":
            raise MatchStateError("invalid_speaker", "只有 Agent 辩手可以接收 Agent 命令。", {"speaker_id": speaker_id})
        config = self._agent_config_for_speaker(speaker)
        if config and not config.get("enabled", True):
            raise MatchStateError(
                "agent_config_disabled",
                "该 Agent 配置已停用，不能发送命令。",
                {"speaker_id": speaker_id, "agent_config_id": config.get("id")},
            )

        task_id = f"cmd_{self.seq + 1}"
        endpoint = self.agent_gateway.endpoint_for(speaker)
        phase = self._current_phase()
        match = self.snapshot["match"]
        command_text = str(command or "custom").strip() or "custom"
        prompt_text = str(prompt or "").strip()
        payload = {
            "model_name": (config or {}).get("model_name", speaker.get("model_name") or ""),
            "debater_name": speaker["name"],
            "debate_position": self._seat_label(speaker["seat"]),
            "debate_topic": match["topic"],
            "current_stage": command_text,
            "next_stage": phase.get("name", ""),
            "holder": "正方" if speaker["side"] == "affirmative" else "反方",
            "other_info": {
                "command": command_text,
                "prompt": prompt_text,
                "match_id": match["id"],
                "speaker_id": speaker_id,
                "side": speaker["side"],
                "seat": speaker["seat"],
            },
            "debate_history": self._build_debate_history(),
            "match_id": match["id"],
            "task_id": task_id,
            "speech_id": None,
            "speaker_id": speaker_id,
            "agent_config_id": speaker.get("agent_config_id"),
            "agent_provider_type": (config or {}).get("provider_type"),
            "time_limit_seconds": 60,
            "remaining_seconds": 60,
            "target_chars": 260,
            "output": {"stream": True, "language": "zh-CN"},
        }

        async with self._lock:
            self._set_agent_status(speaker_id, "streaming", f"command {command_text}")
            self._persist_snapshot()
        await self.emit(
            "agent.command.started",
            {"task_id": task_id, "speaker_id": speaker_id, "command": command_text, "endpoint": endpoint or "embedded://mock"},
            "host",
        )

        fallback = self._mock_agent_command_chunks(speaker, command_text)
        content = ""
        try:
            async for event in self.agent_gateway.stream_speech(endpoint, payload, fallback, config=config):
                if event.get("type") == "delta":
                    content += str(event.get("delta") or "")
                elif event.get("type") == "final":
                    content = str(event.get("content") or content)
                    break
        except AgentGatewayError as exc:
            async with self._lock:
                self._set_agent_status(speaker_id, "failed", exc.message)
                self._persist_snapshot()
            await self.emit(
                "agent.command_failed",
                {"task_id": task_id, "speaker_id": speaker_id, "command": command_text, "code": exc.code, "message": exc.message},
                "host",
            )
            raise MatchStateError(exc.code, exc.message, exc.details) from exc

        async with self._lock:
            self._set_agent_status(speaker_id, "ready", f"command {command_text} completed")
            self._persist_snapshot()
        await self.emit(
            "agent.command_finished",
            {"task_id": task_id, "speaker_id": speaker_id, "command": command_text, "content": content},
            "agent",
            speaker_id,
        )
        return {"task_id": task_id, "speaker_id": speaker_id, "command": command_text, "payload": payload, "content": content}

    async def record_manual_agent_input(self, speaker_id: str, content: str, reason: str = "manual_input") -> Dict[str, Any]:
        text = str(content or "").strip()
        if not text:
            raise MatchStateError("invalid_manual_input", "人工代输入内容不能为空。", {"speaker_id": speaker_id})

        async with self._lock:
            status = self.snapshot["match"]["status"]
            if status not in {"running", "paused", "intervention"}:
                raise MatchStateError(
                    "invalid_state",
                    "当前比赛状态不能录入 AI 人工代输入。",
                    {"speaker_id": speaker_id, "status": status},
                )

            speaker = self._find_speaker(speaker_id)
            if speaker["speaker_type"] != "agent":
                raise MatchStateError("invalid_speaker", "只有 AI 辩手支持人工代输入。", {"speaker_id": speaker_id})

            phase = self._current_phase()
            speech = self.snapshot.get("current_speech") or {}
            if speech and speech.get("speaker_id") != speaker_id:
                raise MatchStateError(
                    "speaker_locked",
                    "当前已有其他辩手被指定，请先结束或重新指定发言人。",
                    {"active_speaker_id": speech.get("speaker_id"), "speaker_id": speaker_id},
                )
            if not speech:
                self._ensure_speaker_allowed_for_current_phase(speaker)
                speech = {
                    "id": f"speech_{self.seq + 1}",
                    "phase_id": phase["id"],
                    "speaker_id": speaker_id,
                    "side": speaker["side"],
                    "turn_index": self._current_turn_index(),
                    "source": "manual",
                    "content_final": "",
                    "content_partial": "",
                    "started_at": iso_now(),
                }

            speech.update(
                {
                    "speaker_id": speaker_id,
                    "side": speaker["side"],
                    "source": "manual",
                    "content_partial": text,
                    "content_final": text,
                    "started_at": speech.get("started_at") or iso_now(),
                }
            )
            self._upsert_transcript_segment(speech, speaker_id, text, True, "manual")
            self.snapshot["current_speech"] = None
            self._clear_flow_state()
            self._pause_running_clocks()
            self._advance_free_debate_turn_if_needed(speaker["side"])
            self.snapshot["match"]["live_mode"] = "free" if phase["phase_type"] == "free_debate" else "single"
            self.snapshot["speech_service"]["tts"] = {
                "status": "idle",
                "latency_ms": 0,
                "queue_size": 0,
                "speaker_id": None,
                "detail": "manual input accepted",
            }
            self._set_agent_status(speaker_id, "ready", "manual input accepted")
            payload = {
                "speech_id": speech["id"],
                "speaker_id": speaker_id,
                "side": speaker["side"],
                "content": text,
                "reason": reason,
                "source": "manual",
            }
            self._persist_snapshot()

        await self.emit("agent.manual_input.accepted", payload, "host", speaker_id)
        await self.emit("agent.speech.final", payload, "host", speaker_id)
        await self.emit("speech.ended", {"speaker_id": speaker_id, "side": speaker["side"], "source": "manual"}, "host", speaker_id)
        return payload

    async def start_speaking(self, speaker_id: str) -> Dict[str, Any]:
        async with self._lock:
            self._ensure_match_allows_control("start_speaking")
            speaker = self._find_speaker(speaker_id)
            if speaker["speaker_type"] != "human":
                raise MatchStateError("invalid_speaker", "AI 辩手不能通过人类控制台开始发言。")
            self._ensure_speaker_allowed_for_current_phase(speaker)
            phase_id = self.snapshot["match"]["current_phase_id"]
            speech = self.snapshot.get("current_speech") or {}
            active_speaker_id = speech.get("speaker_id")
            if active_speaker_id and active_speaker_id != speaker_id:
                raise MatchStateError(
                    "speaker_locked",
                    "当前已有其他辩手被指定，请先结束或重新指定发言人。",
                    {"active_speaker_id": active_speaker_id},
                )
            speech.update(
                {
                    "id": speech.get("id") or f"speech_{self.seq + 1}",
                    "phase_id": phase_id,
                    "speaker_id": speaker_id,
                    "side": speaker["side"],
                    "turn_index": self._current_turn_index(),
                    "source": "human_asr" if speaker["speaker_type"] == "human" else "agent_text",
                    "started_at": iso_now(),
                    "state": "speaking",
                    "started_clock_remaining_ms": {
                        clock["name"]: int(clock.get("remaining_ms", 0))
                        for clock in self.snapshot.get("clocks", [])
                    },
                }
            )
            self.snapshot["current_speech"] = speech
            self.snapshot["match"]["live_mode"] = "free" if phase_id == "phase_free_debate" else "single"
            self._clear_flow_state()
            self._start_relevant_clocks(speaker["side"])
            self.snapshot["speech_service"]["asr"] = {
                "status": "streaming",
                "latency_ms": self.snapshot["speech_service"]["asr"].get("latency_ms", 0),
                "active_sessions": 1,
                "detail": "recording",
            }
            self._persist_snapshot()
        return await self.emit("speech.started", {"speaker_id": speaker_id}, "speaker", speaker_id)

    async def pause_speaking(self, speaker_id: str, reason: str = "manual") -> Dict[str, Any]:
        async with self._lock:
            self._ensure_match_allows_control("pause_speaking")
            speaker = self._find_speaker(speaker_id)
            speech = self._active_speech_for_speaker(speaker_id, "pause_speaking")
            if speech.get("state") == "paused":
                payload = {"speaker_id": speaker_id, "speech_id": speech["id"], "already_paused": True, "reason": reason}
            else:
                speech["state"] = "paused"
                speech["paused_at"] = iso_now()
                self._pause_running_clocks()
                if speech.get("source") == "human_asr":
                    self.snapshot["speech_service"]["asr"] = {
                        "status": "paused",
                        "latency_ms": self.snapshot["speech_service"]["asr"].get("latency_ms", 0),
                        "active_sessions": 0,
                        "detail": "speech paused",
                    }
                payload = {"speaker_id": speaker_id, "speech_id": speech["id"], "side": speaker["side"], "reason": reason}
            self._persist_snapshot()
        return await self.emit("speech.paused", payload, "speaker", speaker_id)

    async def resume_speaking(self, speaker_id: str, reason: str = "manual") -> Dict[str, Any]:
        async with self._lock:
            self._ensure_match_allows_control("resume_speaking")
            speaker = self._find_speaker(speaker_id)
            speech = self._active_speech_for_speaker(speaker_id, "resume_speaking")
            if speech.get("state") != "paused":
                raise MatchStateError("speech_not_paused", "当前发言未处于暂停状态。", {"speaker_id": speaker_id})
            speech["state"] = "speaking"
            speech["resumed_at"] = iso_now()
            speech.pop("paused_at", None)
            self._start_relevant_clocks(speaker["side"])
            if speech.get("source") == "human_asr":
                self.snapshot["speech_service"]["asr"] = {
                    "status": "streaming",
                    "latency_ms": self.snapshot["speech_service"]["asr"].get("latency_ms", 0),
                    "active_sessions": 1,
                    "detail": "recording",
                }
            payload = {"speaker_id": speaker_id, "speech_id": speech["id"], "side": speaker["side"], "reason": reason}
            self._persist_snapshot()
        return await self.emit("speech.resumed", payload, "speaker", speaker_id)

    async def stop_speaking(self, speaker_id: str) -> Dict[str, Any]:
        async with self._lock:
            self._ensure_match_allows_control("stop_speaking")
            speaker = self._find_speaker(speaker_id)
            speech = self.snapshot.get("current_speech") or {}
            if not speech:
                raise MatchStateError("no_active_speech", "当前没有正在进行的发言。")
            if speech.get("speaker_id") != speaker_id:
                raise MatchStateError(
                    "invalid_speaker",
                    "只能结束当前发言人的发言。",
                    {"active_speaker_id": speech.get("speaker_id")},
                )
            text = speech.get("content_partial") or "本次发言已结束，正式转写将在后续 ASR 链路中补齐。"
            speech["content_final"] = text
            speech["state"] = "ended"
            speech["ended_at"] = iso_now()
            self._upsert_transcript_segment(speech, speaker_id, text, True, speech.get("source", "human_asr"))
            self.snapshot["current_speech"] = None
            self._pause_running_clocks()
            self._advance_free_debate_turn_if_needed(speaker["side"])
            if speech.get("source") == "human_asr":
                self.snapshot["speech_service"]["asr"] = {
                    "status": "ok",
                    "latency_ms": self.snapshot["speech_service"]["asr"].get("latency_ms", 0),
                    "active_sessions": 0,
                    "detail": "idle",
                }
            self._persist_snapshot()
        return await self.emit("speech.ended", {"speaker_id": speaker_id, "side": speaker["side"]}, "speaker", speaker_id)

    async def reset_current_speech(self, reason: str = "manual_reset") -> Dict[str, Any]:
        async with self._lock:
            self._ensure_match_allows_control("reset_current_speech")
            speech = self.snapshot.get("current_speech") or {}
            if not speech:
                raise MatchStateError("no_active_speech", "当前没有可重置的发言。")
            speaker_id = speech["speaker_id"]
            speaker = self._find_speaker(speaker_id)
            speech_id = speech["id"]
            removed_segments = [
                segment.get("id")
                for segment in self.snapshot.get("recent_transcript", [])
                if segment.get("speech_id") == speech_id or segment.get("id") == speech_id
            ]
            self.snapshot["recent_transcript"] = [
                segment
                for segment in self.snapshot.get("recent_transcript", [])
                if not (segment.get("speech_id") == speech_id or segment.get("id") == speech_id)
            ]
            removed_audio = [
                asset.get("id")
                for asset in self.snapshot.get("audio_assets", [])
                if asset.get("speech_id") == speech_id
            ]
            self.snapshot["audio_assets"] = [
                asset
                for asset in self.snapshot.get("audio_assets", [])
                if asset.get("speech_id") != speech_id
            ]
            self._reset_relevant_clocks_for_speech(speech)
            self.snapshot["current_speech"] = None
            self._clear_flow_state()
            if speech.get("source") == "human_asr":
                self.snapshot["speech_service"]["asr"] = {
                    "status": "ok",
                    "latency_ms": self.snapshot["speech_service"]["asr"].get("latency_ms", 0),
                    "active_sessions": 0,
                    "detail": "speech reset",
                }
            payload = {
                "speech_id": speech_id,
                "speaker_id": speaker_id,
                "side": speaker["side"],
                "reason": reason,
                "removed_segments": removed_segments,
                "removed_audio": removed_audio,
            }
            self._persist_snapshot()
        return await self.emit("speech.reset", payload, "host")

    async def record_asr_partial(self, speaker_id: str, text: str, latency_ms: Optional[int] = None) -> Dict[str, Any]:
        async with self._lock:
            self._ensure_match_allows_control("asr_partial")
            speech = self._active_speech_for_speaker(speaker_id, "asr_partial")
            if speech.get("source") != "human_asr":
                raise MatchStateError("invalid_speech_source", "只有人类发言可以写入 ASR partial。")
            speech["content_partial"] = text
            self._upsert_transcript_segment(speech, speaker_id, text, False, "human_asr")
            self.snapshot["speech_service"]["asr"] = {
                "status": "streaming",
                "latency_ms": latency_ms if latency_ms is not None else self.snapshot["speech_service"]["asr"].get("latency_ms", 0),
                "active_sessions": 1,
                "detail": "partial received",
            }
            payload = {
                "speech_id": speech["id"],
                "speaker_id": speaker_id,
                "text": text,
                "is_final": False,
            }
            self._persist_snapshot()
        return await self.emit("asr.partial", payload, "speech", speaker_id)

    async def record_asr_final(self, speaker_id: str, text: str, latency_ms: Optional[int] = None) -> Dict[str, Any]:
        async with self._lock:
            self._ensure_match_allows_control("asr_final")
            speech = self._active_speech_for_speaker(speaker_id, "asr_final")
            if speech.get("source") != "human_asr":
                raise MatchStateError("invalid_speech_source", "只有人类发言可以写入 ASR final。")
            speech["content_partial"] = text
            speech["content_final"] = text
            self._upsert_transcript_segment(speech, speaker_id, text, True, "human_asr")
            self.snapshot["speech_service"]["asr"] = {
                "status": "ok",
                "latency_ms": latency_ms if latency_ms is not None else self.snapshot["speech_service"]["asr"].get("latency_ms", 0),
                "active_sessions": 1,
                "detail": "final received",
            }
            payload = {
                "speech_id": speech["id"],
                "speaker_id": speaker_id,
                "text": text,
                "is_final": True,
            }
            self._persist_snapshot()
        return await self.emit("asr.final", payload, "speech", speaker_id)

    async def record_asr_failed(self, speaker_id: str, reason: str) -> Dict[str, Any]:
        async with self._lock:
            self._find_speaker(speaker_id)
            speech = self.snapshot.get("current_speech") or {}
            active = speech if speech.get("speaker_id") == speaker_id else None
            if active and active.get("source") == "human_asr":
                active["content_partial"] = "转写不可用，请以现场发言为准。"
            self.snapshot["speech_service"]["asr"] = {
                "status": "failed",
                "latency_ms": self.snapshot["speech_service"]["asr"].get("latency_ms", 0),
                "active_sessions": 1 if active else 0,
                "detail": reason,
            }
            payload = {
                "speech_id": active.get("id") if active else None,
                "speaker_id": speaker_id,
                "reason": reason,
            }
            self._persist_snapshot()
        return await self.emit("asr.failed", payload, "host", speaker_id)

    async def patch_speech(self, speech_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        reason = body.get("reason", "manual_revision")
        new_text = body.get("content_final", body.get("text"))
        new_valid = body.get("valid")
        if new_text is None and new_valid is None:
            raise MatchStateError("invalid_revision", "修订必须包含文本或有效性变更。", {"speech_id": speech_id})

        async with self._lock:
            active = self.snapshot.get("current_speech") if (self.snapshot.get("current_speech") or {}).get("id") == speech_id else None
            segments = [
                segment
                for segment in self.snapshot.get("recent_transcript", [])
                if segment.get("speech_id") == speech_id or segment.get("id") == speech_id
            ]
            if not active and not segments:
                raise MatchStateError("speech_not_found", "未找到要修订的发言或转写段。", {"speech_id": speech_id})

            before_text = self._speech_revision_text(active, segments)
            after_text = new_text if new_text is not None else before_text
            if active:
                if new_text is not None:
                    active["content_partial"] = new_text
                    active["content_final"] = new_text
                if new_valid is not None:
                    active["valid"] = bool(new_valid)
                    active["invalid_reason"] = None if bool(new_valid) else reason

            for segment in segments:
                if new_text is not None:
                    segment["text"] = new_text
                    segment["is_final"] = True
                    segment["updated_at"] = iso_now()
                if new_valid is not None:
                    segment["valid"] = bool(new_valid)
                    segment["invalid_reason"] = None if bool(new_valid) else reason
                    segment["updated_at"] = iso_now()

            revision = {
                "id": f"rev_{self.seq + 1}_{len(self.snapshot.get('speech_revisions', [])) + 1}",
                "speech_id": speech_id,
                "before_text": before_text,
                "after_text": after_text,
                "valid": bool(new_valid) if new_valid is not None else (segments[0].get("valid", True) if segments else active.get("valid", True)),
                "reason": reason,
                "created_at": iso_now(),
                "editor_actor_id": body.get("editor_actor_id", "host"),
            }
            self.snapshot.setdefault("speech_revisions", []).insert(0, revision)
            self.snapshot["speech_revisions"] = self.snapshot["speech_revisions"][:50]
            self._persist_snapshot()

        return await self.emit(
            "speech.revised",
            {
                "speech_id": speech_id,
                "revision": revision,
                "content_final": after_text,
                "valid": revision["valid"],
                "reason": reason,
            },
            "host",
        )

    async def record_audio_chunk(
        self,
        speech_id: str,
        speaker_id: str,
        chunk_index: int,
        content: bytes,
        mime_type: str,
        duration_ms: Optional[int] = None,
    ) -> Dict[str, Any]:
        if chunk_index < 0:
            raise MatchStateError("invalid_audio_chunk", "音频分片序号不能小于 0。", {"chunk_index": chunk_index})
        if not content:
            raise MatchStateError("invalid_audio_chunk", "音频分片内容为空。", {"speech_id": speech_id})

        async with self._lock:
            self._ensure_match_allows_control("audio_chunk")
            speaker = self._find_speaker(speaker_id)
            if speaker["speaker_type"] != "human":
                raise MatchStateError("invalid_speaker", "只有人类辩手控制台可以上传录音分片。", {"speaker_id": speaker_id})
            phase_id, phase_key = self._audio_speech_context(speech_id, speaker_id)
            ext = self._audio_extension(mime_type)
            archive_dir = self._audio_archive_dir(self.snapshot["match"]["id"], phase_key, speech_id)
            archive_dir.mkdir(parents=True, exist_ok=True)
            chunk_path = archive_dir / f"chunk_{chunk_index:05d}.{ext}"
            try:
                chunk_path.write_bytes(content)
            except OSError as exc:
                self.snapshot["speech_service"]["asr"]["detail"] = f"audio archive failed: {exc}"
                self._persist_snapshot()
                raise MatchStateError(
                    "audio_archive_failed",
                    "音频归档写入失败，但比赛状态不会被阻塞。",
                    {"speech_id": speech_id, "reason": str(exc)},
                ) from exc

            asset = self._upsert_audio_asset(
                speech_id=speech_id,
                speaker_id=speaker_id,
                phase_id=phase_id,
                mime_type=mime_type or "application/octet-stream",
                archive_dir=archive_dir,
                chunk_path=chunk_path,
                chunk_index=chunk_index,
                size_bytes=len(content),
                duration_ms=duration_ms,
            )
            pcm_ready = self._asr_supported_audio_mime(mime_type)
            if pcm_ready and (self.snapshot.get("current_speech") or {}).get("id") == speech_id:
                self.snapshot["speech_service"]["asr"] = {
                    "status": "streaming",
                    "latency_ms": self.snapshot["speech_service"]["asr"].get("latency_ms", 0),
                    "active_sessions": 1,
                    "detail": f"receiving PCM/L16 · {asset['chunk_count']} chunks · {asset['size_bytes']} bytes",
                }
            else:
                self.snapshot["speech_service"]["asr"]["detail"] = f"audio archived {asset['chunk_count']} chunks"
            payload = {
                "audio_asset_id": asset["id"],
                "speech_id": speech_id,
                "speaker_id": speaker_id,
                "chunk_index": chunk_index,
                "chunk_count": asset["chunk_count"],
                "size_bytes": asset["size_bytes"],
                "file_path": str(chunk_path),
                "pcm_ready": pcm_ready,
            }
            self._persist_snapshot()

        result = await self.emit("audio.chunk_archived", payload, "speech", speaker_id)
        if payload["pcm_ready"]:
            await self.emit("asr.audio_chunk_received", payload, "speech", speaker_id)
            await self._send_live_asr_chunk(speech_id, speaker_id, content, mime_type)
        return result

    async def complete_audio_archive(self, speech_id: str, speaker_id: Optional[str] = None) -> Dict[str, Any]:
        async with self._lock:
            asset = self._audio_asset_for_speech(speech_id)
            if not asset:
                raise MatchStateError("audio_asset_not_found", "未找到该发言的音频归档。", {"speech_id": speech_id})
            if speaker_id and asset.get("speaker_id") != speaker_id:
                raise MatchStateError(
                    "invalid_speaker",
                    "只能完成本人发言的音频归档。",
                    {"speech_id": speech_id, "speaker_id": speaker_id, "asset_speaker_id": asset.get("speaker_id")},
                )
            asset["status"] = "completed"
            asset["completed_at"] = iso_now()
            asset["updated_at"] = asset["completed_at"]
            payload = {
                "audio_asset_id": asset["id"],
                "speech_id": speech_id,
                "speaker_id": asset["speaker_id"],
                "chunk_count": asset.get("chunk_count", 0),
                "size_bytes": asset.get("size_bytes", 0),
                "file_path": asset.get("file_path"),
            }
            self._persist_snapshot()

        result = await self.emit("audio.archive_completed", payload, "speech", asset["speaker_id"])
        await self._finish_live_asr_stream(speech_id, asset["speaker_id"])
        return result

    async def should_auto_recognize_audio_archive(self, speech_id: str) -> bool:
        if not self._asr_auto_recognize_enabled():
            return False
        async with self._lock:
            asset = self._audio_asset_for_speech(speech_id)
            if asset and asset.get("asr_realtime_status") == "completed":
                return False
            return bool(asset and self._asr_supported_audio_mime(str(asset.get("mime_type") or "")))

    async def auto_recognize_audio_archive(self, speech_id: str) -> None:
        try:
            await self.recognize_audio_archive(speech_id)
        except MatchStateError as exc:
            await self.emit(
                "asr.failed",
                {"speech_id": speech_id, "reason": exc.message, "code": exc.code, "auto_recognize": True},
                "host",
            )

    async def _send_live_asr_chunk(self, speech_id: str, speaker_id: str, content: bytes, mime_type: str) -> None:
        if not self._asr_realtime_enabled() or not self._asr_supported_audio_mime(mime_type):
            return
        session = self._asr_streams.get(speech_id)
        if not session:
            request_id = f"asr_stream_{speech_id}"
            stream_started_at = iso_now()
            gateway = _select_asr_gateway()

            async def on_partial(text: str, latency_ms: int, chunk_count: int) -> None:
                await self._record_live_asr_text(speech_id, speaker_id, text, False, latency_ms, chunk_count)

            async def on_final(text: str, latency_ms: int, chunk_count: int) -> None:
                await self._record_live_asr_text(speech_id, speaker_id, text, True, latency_ms, chunk_count)

            async def on_error(exc: XfyunGatewayError) -> None:
                await self._record_live_asr_failed(speech_id, speaker_id, exc.message, exc.code)

            async with self._lock:
                self.repo.save_speech_service_request_started(
                    match_id=self.snapshot["match"]["id"],
                    request_id=request_id,
                    service="asr",
                    operation="realtime_stream",
                    speech_id=speech_id,
                    speaker_id=speaker_id,
                    request={"speech_id": speech_id, "speaker_id": speaker_id, "mime_type": mime_type},
                    started_at=stream_started_at,
                    origin="live",
                    **self._log_context(),
                )
            try:
                session = await gateway.open_stream(on_partial=on_partial, on_final=on_final, on_error=on_error)
            except XfyunGatewayError as exc:
                await self._record_live_asr_failed(speech_id, speaker_id, exc.message, exc.code)
                return
            self._asr_streams[speech_id] = session
            async with self._lock:
                asset = self._audio_asset_for_speech(speech_id)
                if asset:
                    asset["asr_realtime_status"] = "streaming"
                    asset["asr_realtime_started_at"] = iso_now()
                self.snapshot["speech_service"]["asr"] = {
                    "status": "streaming",
                    "latency_ms": self.snapshot["speech_service"]["asr"].get("latency_ms", 0),
                    "active_sessions": 1,
                    "detail": f"Xunfei realtime ASR stream started · {speech_id}",
                }
                self._persist_snapshot()
            await self.emit("asr.stream_started", {"speech_id": speech_id, "speaker_id": speaker_id}, "speech", speaker_id)
        try:
            await session.send_audio(content)
        except XfyunGatewayError as exc:
            self._asr_streams.pop(speech_id, None)
            await self._record_live_asr_failed(speech_id, speaker_id, exc.message, exc.code)

    async def _finish_live_asr_stream(self, speech_id: str, speaker_id: str) -> None:
        session = self._asr_streams.pop(speech_id, None)
        if not session:
            return
        try:
            result = await session.finish()
        except XfyunGatewayError as exc:
            await self._record_live_asr_failed(speech_id, speaker_id, exc.message, exc.code)
            return
        async with self._lock:
            self.repo.finish_speech_service_request(
                match_id=self.snapshot["match"]["id"],
                request_id=f"asr_stream_{speech_id}",
                status="completed",
                response={"text": result.text, "text_length": len(result.text), "chunk_count": result.chunk_count},
                latency_ms=result.latency_ms,
                completed_at=iso_now(),
            )
            asset = self._audio_asset_for_speech(speech_id)
            if asset:
                asset["asr_realtime_status"] = "completed"
                asset["asr_realtime_finished_at"] = iso_now()
                asset["asr_realtime_text_length"] = len(result.text)
            self._persist_snapshot()

    async def _record_live_asr_text(
        self,
        speech_id: str,
        speaker_id: str,
        text: str,
        is_final: bool,
        latency_ms: int,
        chunk_count: int,
    ) -> None:
        async with self._lock:
            active = self.snapshot.get("current_speech") if (self.snapshot.get("current_speech") or {}).get("id") == speech_id else None
            if active:
                active["content_partial"] = text
                if is_final:
                    active["content_final"] = text
                self._upsert_transcript_segment(active, speaker_id, text, is_final, "human_asr")
            elif is_final:
                self._apply_archived_asr_text(speech_id, text)
            elif not text:
                return
            self.snapshot["speech_service"]["asr"] = {
                "status": "ok" if is_final else "streaming",
                "latency_ms": latency_ms,
                "active_sessions": 0 if is_final else 1,
                "detail": f"realtime ASR {'final' if is_final else 'partial'} · {len(text)} chars · {chunk_count} chunks",
            }
            self._persist_snapshot()
        await self.emit(
            "asr.final" if is_final else "asr.partial",
            {
                "speech_id": speech_id,
                "speaker_id": speaker_id,
                "text": text,
                "is_final": is_final,
                "latency_ms": latency_ms,
                "chunk_count": chunk_count,
            },
            "speech",
            speaker_id,
        )

    async def _record_live_asr_failed(self, speech_id: str, speaker_id: str, reason: str, code: Optional[int] = None) -> None:
        async with self._lock:
            self.repo.finish_speech_service_request(
                match_id=self.snapshot["match"]["id"],
                request_id=f"asr_stream_{speech_id}",
                status="failed",
                error_code=str(code) if code is not None else "asr_stream_error",
                error_message=reason,
                latency_ms=0,
                completed_at=iso_now(),
            )
            asset = self._audio_asset_for_speech(speech_id)
            if asset:
                asset["asr_realtime_status"] = "failed"
                asset["asr_realtime_error"] = reason
            active = self.snapshot.get("current_speech") if (self.snapshot.get("current_speech") or {}).get("id") == speech_id else None
            if active and active.get("source") == "human_asr" and not active.get("content_partial"):
                active["content_partial"] = "转写不可用，请以现场发言为准。"
            self.snapshot["speech_service"]["asr"] = {
                "status": "failed",
                "latency_ms": self.snapshot["speech_service"]["asr"].get("latency_ms", 0),
                "active_sessions": 0,
                "detail": reason,
            }
            self._persist_snapshot()
        await self.emit("asr.failed", {"speech_id": speech_id, "speaker_id": speaker_id, "reason": reason, "code": code}, "speech", speaker_id)

    async def recognize_audio_archive(self, speech_id: str) -> Dict[str, Any]:
        request_id = self._new_speech_service_request_id("asr", "archive_recognition")
        request_started_time = time.perf_counter()
        async with self._lock:
            asset = self._audio_asset_for_speech(speech_id)
            if not asset:
                raise MatchStateError("audio_asset_not_found", "未找到该发言的音频归档。", {"speech_id": speech_id})
            mime_type = str(asset.get("mime_type") or "")
            if not self._asr_supported_audio_mime(mime_type):
                raise MatchStateError(
                    "unsupported_audio_format",
                    "当前归档音频不是讯飞 ASR 可直接识别的 PCM/L16 格式；请使用实时 PCM 流或转码后再识别。",
                    {"speech_id": speech_id, "mime_type": mime_type},
                )
            chunks = sorted(asset.get("chunks") or [], key=lambda item: int(item.get("chunk_index", 0)))
            paths = [Path(item.get("file_path", "")) for item in chunks]
            match_id = self.snapshot["match"]["id"]
            speaker_id = asset.get("speaker_id")
            self.repo.save_speech_service_request_started(
                match_id=match_id,
                request_id=request_id,
                service="asr",
                operation="archive_recognition",
                speech_id=speech_id,
                speaker_id=speaker_id,
                request={
                    "speech_id": speech_id,
                    "speaker_id": speaker_id,
                    "mime_type": mime_type,
                    "chunk_count": len(chunks),
                },
                origin="live",
                **self._log_context(),
                started_at=iso_now(),
            )
            self.snapshot["speech_service"]["asr"] = {
                "status": "recognizing",
                "latency_ms": 0,
                "active_sessions": 1,
                "detail": f"recognizing archived audio {speech_id}",
            }
            self._persist_snapshot()

        content = b"".join(path.read_bytes() for path in paths if path.exists())
        if not content:
            async with self._lock:
                self.snapshot["speech_service"]["asr"] = {
                    "status": "failed",
                    "latency_ms": 0,
                    "active_sessions": 0,
                    "detail": "归档音频为空，无法识别。",
                }
                self.repo.finish_speech_service_request(
                    match_id=self.snapshot["match"]["id"],
                    request_id=request_id,
                    status="failed",
                    error_code="invalid_audio_archive",
                    error_message="归档音频为空，无法识别。",
                    latency_ms=max(0, int((time.perf_counter() - request_started_time) * 1000)),
                    completed_at=iso_now(),
                )
                self._persist_snapshot()
            raise MatchStateError("invalid_audio_archive", "归档音频为空，无法识别。", {"speech_id": speech_id})
        await self.emit("asr.archive_recognition_started", {"speech_id": speech_id, "audio_bytes": len(content)}, "host")

        gateway = _select_asr_gateway()
        try:
            result = await gateway.recognize(content, audio_format="audio/L16;rate=16000", encoding="raw")
        except XfyunGatewayError as exc:
            async with self._lock:
                self.snapshot["speech_service"]["asr"] = {
                    "status": "failed",
                    "latency_ms": 0,
                    "active_sessions": 0,
                    "detail": exc.message,
                }
                self.repo.finish_speech_service_request(
                    match_id=self.snapshot["match"]["id"],
                    request_id=request_id,
                    status="failed",
                    error_code=str(exc.code) if exc.code is not None else "xfyun_asr_error",
                    error_message=exc.message,
                    latency_ms=max(0, int((time.perf_counter() - request_started_time) * 1000)),
                    completed_at=iso_now(),
                )
                self._persist_snapshot()
            await self.emit("asr.failed", {"speech_id": speech_id, "reason": exc.message, "code": exc.code}, "host")
            raise MatchStateError("speech_service_error", f"ASR 归档识别失败：{exc.message}", {"code": exc.code})

        async with self._lock:
            self._apply_archived_asr_text(speech_id, result.text)
            self.snapshot["speech_service"]["asr"] = {
                "status": "ok",
                "latency_ms": result.latency_ms,
                "active_sessions": 0,
                "detail": f"archive ASR ok · {len(result.text)} chars · {result.chunk_count} chunks",
            }
            self.repo.finish_speech_service_request(
                match_id=self.snapshot["match"]["id"],
                request_id=request_id,
                status="completed",
                response={"text": result.text, "text_length": len(result.text), "chunk_count": result.chunk_count, "audio_bytes": len(content)},
                latency_ms=result.latency_ms,
                completed_at=iso_now(),
            )
            self._persist_snapshot()
        payload = {
            "speech_id": speech_id,
            "text": result.text,
            "text_length": len(result.text),
            "latency_ms": result.latency_ms,
            "chunk_count": result.chunk_count,
            "audio_bytes": len(content),
        }
        await self.emit("asr.final", payload, "host")
        return {"result": payload, "snapshot": await self.get_snapshot()}

    async def record_tts_failed(self, speaker_id: str, reason: str, text_only: bool = True) -> Dict[str, Any]:
        async with self._lock:
            speaker = self._find_speaker(speaker_id)
            if speaker["speaker_type"] != "agent":
                raise MatchStateError("invalid_speaker", "只有 AI 辩手会进入 TTS 降级。")
            speech = self.snapshot.get("current_speech") or {}
            active = speech if speech.get("speaker_id") == speaker_id else None
            if active and text_only:
                self.snapshot["match"]["live_mode"] = "free" if active.get("phase_id") == "phase_free_debate" else "single"
            self.snapshot["speech_service"]["tts"] = {
                "status": "failed",
                "latency_ms": self.snapshot["speech_service"]["tts"].get("latency_ms", 0),
                "queue_size": 0,
                "speaker_id": speaker_id,
                "detail": reason,
                "degraded_to": "text_only" if text_only else "manual_reading",
            }
            payload = {
                "speech_id": active.get("id") if active else None,
                "speaker_id": speaker_id,
                "reason": reason,
                "degraded_to": "text_only" if text_only else "manual_reading",
            }
            self._persist_snapshot()
        return await self.emit("tts.failed", payload, "host", speaker_id)

    async def probe_asr(self, audio: bytes, audio_format: str = "audio/L16;rate=16000", encoding: str = "raw") -> Dict[str, Any]:
        content = audio or (b"\0" * 6400)
        request_id = self._new_speech_service_request_id("asr", "probe")
        request_started_time = time.perf_counter()
        async with self._lock:
            self.repo.save_speech_service_request_started(
                match_id=self.snapshot["match"]["id"],
                request_id=request_id,
                service="asr",
                operation="probe",
                request={"audio_bytes": len(content), "format": audio_format, "encoding": encoding},
                started_at=iso_now(),
                origin="test",
                **self._log_context(),
            )
            self.snapshot["speech_service"]["asr"] = {
                "status": "recognizing",
                "latency_ms": 0,
                "active_sessions": 1,
                "detail": "Xunfei ASR probe started",
            }
            self._persist_snapshot()
        await self.emit("asr.probe_started", {"audio_bytes": len(content), "format": audio_format, "encoding": encoding}, "host")

        gateway = _select_asr_gateway()
        try:
            result = await gateway.recognize(content, audio_format=audio_format, encoding=encoding)
        except XfyunGatewayError as exc:
            async with self._lock:
                self.snapshot["speech_service"]["asr"] = {
                    "status": "failed",
                    "latency_ms": 0,
                    "active_sessions": 0,
                    "detail": exc.message,
                }
                self.repo.finish_speech_service_request(
                    match_id=self.snapshot["match"]["id"],
                    request_id=request_id,
                    status="failed",
                    error_code=str(exc.code) if exc.code is not None else "xfyun_asr_error",
                    error_message=exc.message,
                    latency_ms=max(0, int((time.perf_counter() - request_started_time) * 1000)),
                    completed_at=iso_now(),
                )
                self._persist_snapshot()
            await self.emit("asr.failed", {"probe": True, "reason": exc.message, "code": exc.code}, "host")
            raise MatchStateError("speech_service_error", f"ASR 试识别失败：{exc.message}", {"code": exc.code})

        payload = {
            "probe": True,
            "text": result.text,
            "text_length": len(result.text),
            "latency_ms": result.latency_ms,
            "chunk_count": result.chunk_count,
            "audio_bytes": len(content),
        }
        async with self._lock:
            self.snapshot["speech_service"]["asr"] = {
                "status": "ok",
                "latency_ms": result.latency_ms,
                "active_sessions": 0,
                "detail": f"ASR probe ok · {len(result.text)} chars · {result.chunk_count} chunks",
            }
            self.repo.finish_speech_service_request(
                match_id=self.snapshot["match"]["id"],
                request_id=request_id,
                status="completed",
                response=payload,
                latency_ms=result.latency_ms,
                completed_at=iso_now(),
            )
            self._persist_snapshot()
        await self.emit("asr.probe_completed", payload, "host")
        return {"result": payload, "snapshot": await self.get_snapshot()}

    async def probe_tts(self, text: str) -> Dict[str, Any]:
        content = str(text or "").strip() or "人机辩论赛语音合成自检。"
        request_id = self._new_speech_service_request_id("tts", "probe")
        request_started_time = time.perf_counter()
        async with self._lock:
            self.repo.save_speech_service_request_started(
                match_id=self.snapshot["match"]["id"],
                request_id=request_id,
                service="tts",
                operation="probe",
                request={"text": content, "text_length": len(content)},
                started_at=iso_now(),
                origin="test",
                **self._log_context(),
            )
            self.snapshot["speech_service"]["tts"] = {
                "status": "synthesizing",
                "latency_ms": 0,
                "queue_size": 1,
                "speaker_id": None,
                "detail": "Xunfei TTS probe started",
            }
            self._persist_snapshot()
        await self.emit("tts.started", {"probe": True, "text_length": len(content)}, "host")

        gateway = XfyunTTSGateway(url=os.getenv("XFYUN_TTS_URL", "").strip())
        try:
            result = await gateway.synthesize(content)
        except XfyunGatewayError as exc:
            async with self._lock:
                self.snapshot["speech_service"]["tts"] = {
                    "status": "failed",
                    "latency_ms": 0,
                    "queue_size": 0,
                    "speaker_id": None,
                    "detail": exc.message,
                    "degraded_to": "text_only",
                }
                self.repo.finish_speech_service_request(
                    match_id=self.snapshot["match"]["id"],
                    request_id=request_id,
                    status="failed",
                    error_code=str(exc.code) if exc.code is not None else "xfyun_tts_error",
                    error_message=exc.message,
                    latency_ms=max(0, int((time.perf_counter() - request_started_time) * 1000)),
                    completed_at=iso_now(),
                )
                self._persist_snapshot()
            await self.emit("tts.failed", {"probe": True, "reason": exc.message, "code": exc.code}, "host")
            raise MatchStateError("speech_service_error", f"TTS 试合成失败：{exc.message}", {"code": exc.code})

        archive_dir = self.audio_root_path() / "diagnostics"
        archive_dir.mkdir(parents=True, exist_ok=True)
        extension = "mp3" if result.mime_type == "audio/mpeg" else "bin"
        file_path = archive_dir / f"tts_probe_{utc_now().strftime('%Y%m%dT%H%M%SZ')}.{extension}"
        file_path.write_bytes(result.audio)
        payload = {
            "probe": True,
            "mime_type": result.mime_type,
            "size_bytes": len(result.audio),
            "chunk_count": result.chunk_count,
            "latency_ms": result.latency_ms,
            "file_path": str(file_path),
            # 让前端可直接在本机播放试合成音频
            "audio_base64": base64.b64encode(result.audio).decode("ascii"),
        }
        async with self._lock:
            self.snapshot["speech_service"]["tts"] = {
                "status": "idle",
                "latency_ms": result.latency_ms,
                "queue_size": 0,
                "speaker_id": None,
                "detail": f"TTS probe ok · {len(result.audio)} bytes · {file_path}",
            }
            self.repo.finish_speech_service_request(
                match_id=self.snapshot["match"]["id"],
                request_id=request_id,
                status="completed",
                response=payload,
                latency_ms=result.latency_ms,
                completed_at=iso_now(),
            )
            self._persist_snapshot()
        await self.emit("tts.probe_completed", payload, "host")
        await self.emit("tts.finished", {"probe": True, "latency_ms": result.latency_ms}, "system")
        return {"result": payload, "snapshot": await self.get_snapshot()}

    async def pause_clock(self, clock_name: str, reason: str = "manual") -> Dict[str, Any]:
        async with self._lock:
            self._ensure_match_allows_control("pause_clock")
            clock = self._clock_or_error(clock_name)
            self._refresh_clocks()
            if clock["state"] == "running":
                clock["state"] = "paused"
                clock["deadline_at"] = None
            elif clock["state"] not in {"paused", "expired"}:
                clock["state"] = "paused"
                clock["deadline_at"] = None
            payload = self._clock_payload(clock, reason)
            self._persist_snapshot()
        return await self.emit("clock.paused", payload, "host")

    async def resume_clock(self, clock_name: str, reason: str = "manual") -> Dict[str, Any]:
        async with self._lock:
            self._ensure_match_allows_control("resume_clock")
            clock = self._clock_or_error(clock_name)
            self._refresh_clocks()
            if clock["remaining_ms"] <= 0:
                raise MatchStateError("clock_expired", "该时钟剩余时间为 0，不能继续。", {"clock_name": clock_name})
            now = utc_now()
            clock["state"] = "running"
            clock["deadline_at"] = to_iso(now + timedelta(milliseconds=clock["remaining_ms"]))
            clock.pop("expired_notified_at", None)
            payload = self._clock_payload(clock, reason)
            self._persist_snapshot()
        return await self.emit("clock.resumed", payload, "host")

    async def adjust_clock(self, clock_name: str, remaining_ms: int, reason: str = "manual") -> Dict[str, Any]:
        if remaining_ms < 0:
            raise MatchStateError("invalid_clock", "时钟剩余时间不能为负数。", {"clock_name": clock_name, "remaining_ms": remaining_ms})
        async with self._lock:
            self._ensure_match_allows_control("adjust_clock")
            clock = self._clock_or_error(clock_name)
            self._refresh_clocks()
            clock["remaining_ms"] = remaining_ms
            if remaining_ms == 0:
                clock["state"] = "expired"
                clock["deadline_at"] = None
            elif clock["state"] == "running":
                clock["deadline_at"] = to_iso(utc_now() + timedelta(milliseconds=remaining_ms))
                clock.pop("expired_notified_at", None)
            else:
                clock["state"] = "paused"
                clock["deadline_at"] = None
                clock.pop("expired_notified_at", None)
            payload = self._clock_payload(clock, reason)
            self._persist_snapshot()
        return await self.emit("clock.adjusted", payload, "host")

    async def tick_timers(self) -> List[Dict[str, Any]]:
        events: List[tuple[str, Dict[str, Any]]] = []
        flow_payload: Optional[Dict[str, Any]] = None
        async with self._lock:
            if self.snapshot["match"]["status"] != "running":
                return []
            now = utc_now()
            phase = self._current_phase()
            expired_payloads: List[Dict[str, Any]] = []
            for clock in self.snapshot.get("clocks", []):
                if clock["state"] == "running":
                    deadline = parse_iso(clock.get("deadline_at"))
                    if deadline and deadline <= now:
                        clock["remaining_ms"] = 0
                        clock["state"] = "expired"
                        clock["deadline_at"] = None
                if clock["state"] == "expired" and not clock.get("expired_notified_at"):
                    clock["expired_notified_at"] = to_iso(now)
                    expired_payloads.append(self._clock_payload(clock, "timer_loop"))

            timeout_payload: Optional[Dict[str, Any]] = None
            if expired_payloads and self.snapshot.get("current_speech"):
                speech = self.snapshot["current_speech"]
                speaker_id = speech["speaker_id"]
                speaker = self._find_speaker(speaker_id)
                text = speech.get("content_partial") or "时间到，本次发言结束。"
                speech["content_final"] = text
                speech["state"] = "ended"
                speech["ended_at"] = to_iso(now)
                speech["ended_reason"] = "timeout"
                self._upsert_transcript_segment(speech, speaker_id, text, True, speech.get("source", "human_asr"))
                self.snapshot["current_speech"] = None
                self._pause_running_clocks()
                self._advance_free_debate_turn_if_needed(speaker["side"])
                if speech.get("source") == "human_asr":
                    self.snapshot["speech_service"]["asr"] = {
                        "status": "ok",
                        "latency_ms": self.snapshot["speech_service"]["asr"].get("latency_ms", 0),
                        "active_sessions": 0,
                        "detail": "timeout",
                    }
                timeout_payload = {
                    "speech_id": speech["id"],
                    "speaker_id": speaker_id,
                    "side": speaker["side"],
                    "expired_clocks": [item["clock_name"] for item in expired_payloads],
                }

            if not expired_payloads and timeout_payload is None:
                return []
            expired_clock_names = [item["clock_name"] for item in expired_payloads]
            flow_payload = self._set_flow_waiting_for_timeout(
                phase=phase,
                expired_clock_names=expired_clock_names,
                speech_id=(timeout_payload or {}).get("speech_id"),
                speaker_id=(timeout_payload or {}).get("speaker_id"),
                now=now,
            )
            self._persist_snapshot()
            for payload in expired_payloads:
                events.append(("clock.expired", payload))
            if timeout_payload:
                events.append(("speech.timeout", timeout_payload))
                events.append(("speech.ended", {"speaker_id": timeout_payload["speaker_id"], "side": timeout_payload["side"], "reason": "timeout"}))
            if flow_payload:
                events.append(("flow.awaiting_host_confirm", flow_payload))

        emitted = []
        for event_type, payload in events:
            emitted.append(await self.emit(event_type, payload, "system"))
        return emitted

    async def confirm_flow(self, reason: str = "host_confirm") -> Dict[str, Any]:
        async with self._lock:
            self._ensure_match_allows_control("confirm_flow")
            flow = self.snapshot.get("flow") or self._fresh_flow_state()
            if not flow.get("awaiting_host_confirm"):
                payload = {"reason": reason, "already_confirmed": True}
                self._persist_snapshot()
                return payload
            previous = deepcopy(flow)
            if previous.get("next_action") == "free_turn_next":
                turn_clock = self._clock("turn")
                if turn_clock and turn_clock.get("remaining_ms", 0) <= 0:
                    turn_clock["remaining_ms"] = turn_clock["total_seconds"] * 1000
                    turn_clock["state"] = "paused"
                    turn_clock["deadline_at"] = None
                    turn_clock.pop("expired_notified_at", None)
            self._clear_flow_state()
            payload = {
                "reason": reason,
                "next_action": previous.get("next_action"),
                "phase_id": previous.get("phase_id"),
                "speech_id": previous.get("speech_id"),
            }
            self._persist_snapshot()
        await self.emit("flow.confirmed", payload, "host")
        return payload

    async def submit_vote(self, body: Dict[str, Any], audience: bool = False) -> Dict[str, Any]:
        duplicate_error: Optional[MatchStateError] = None
        duplicate_payload: Optional[Dict[str, Any]] = None
        async with self._lock:
            self._ensure_vote_controls_available("submit_audience_vote" if audience else "submit_judge_vote")
            self._validate_vote_body(body, audience=audience)
            if audience:
                if self.snapshot["vote_state"]["window_status"] != "open":
                    raise MatchStateError(
                        "vote_window_closed",
                        "学生投票窗口未开启，暂不能提交投票。",
                        {"window_status": self.snapshot["vote_state"]["window_status"]},
                    )
                vote_keys = self._audience_vote_keys(body)
                existing_keys = set(self.snapshot["vote_state"].get("audience_vote_keys", []))
                legacy_keys = self._legacy_audience_vote_keys(body)
                existing_keys.update(self.snapshot["vote_state"].get("used_audience_tokens", []))
                matched_keys = sorted(existing_keys.intersection(vote_keys) | existing_keys.intersection(legacy_keys))
                if matched_keys:
                    duplicate_payload = {
                        "reason": "duplicate_vote",
                        "vote_key_types": self._vote_key_types_for_log(vote_keys),
                        "matched_key_types": self._vote_key_types_for_log(matched_keys),
                        "winner_side": body["winner_side"],
                        "best_speaker_id": body["best_speaker_id"],
                    }
                    duplicate_error = MatchStateError("duplicate_vote", "你已经投过票，请勿重复提交。", {"vote_key": vote_keys[0]})
                else:
                    self.snapshot["vote_state"].setdefault("audience_vote_keys", []).extend(
                        [key for key in vote_keys if key not in self.snapshot["vote_state"].get("audience_vote_keys", [])]
                    )
                    self.snapshot["vote_state"].setdefault("audience_votes", []).append(
                        {
                            "vote_key": vote_keys[0],
                            "vote_keys": vote_keys,
                            "winner_side": body["winner_side"],
                            "best_speaker_id": body["best_speaker_id"],
                            "created_at": iso_now(),
                        }
                    )
                    self._append_audience_summary(body)
            else:
                judge_summary = self._build_judge_summary(body)
                self.snapshot["vote_state"]["judge_summary"] = judge_summary
                self.snapshot["vote_state"]["winner_side"] = judge_summary["winner_side"]
                self.snapshot["vote_state"]["best_speaker_id"] = judge_summary["best_speaker_id"]
            if duplicate_error is None:
                self._persist_snapshot()
        if duplicate_error is not None:
            await self.emit("vote.duplicate_rejected", duplicate_payload or {"reason": "duplicate_vote"}, "audience")
            raise duplicate_error
        return await self.emit(
            "vote.submitted",
            {"audience": audience, "vote_state": self._public_vote_state()},
            "audience" if audience else "host",
        )

    async def open_audience_votes(self) -> Dict[str, Any]:
        async with self._lock:
            self._ensure_vote_controls_available("open_audience_votes")
            self.snapshot["vote_state"]["window_status"] = "open"
            self._persist_snapshot()
        return await self.emit("vote.window_opened", {"match_id": self.snapshot["match"]["id"]}, "host")

    async def close_audience_votes(self) -> Dict[str, Any]:
        async with self._lock:
            self._ensure_vote_controls_available("close_audience_votes")
            self.snapshot["vote_state"]["window_status"] = "closed"
            self._persist_snapshot()
        return await self.emit("vote.window_closed", {"match_id": self.snapshot["match"]["id"]}, "host")

    async def publish_votes(self, scope: str) -> Dict[str, Any]:
        async with self._lock:
            self._ensure_vote_controls_available("publish_votes")
            if scope not in {"judge", "audience"}:
                raise MatchStateError("invalid_vote_scope", "未知的投票公布范围。", {"scope": scope})
            if scope == "judge":
                if not self.snapshot["vote_state"].get("winner_side") or not self.snapshot["vote_state"].get("best_speaker_id"):
                    raise MatchStateError("missing_votes", "请先录入评委结果。")
                self.snapshot["vote_state"]["judge_published"] = True
                self.snapshot["vote_state"]["window_status"] = "closed"
                self.snapshot["match"]["screen_scene"] = "judge_result"
            if scope == "audience":
                if not self.snapshot["vote_state"]["judge_published"]:
                    raise MatchStateError(
                        "publish_order",
                        "需要先公布评委结果，再公布学生投票结果。",
                        {"judge_published": False},
                    )
                self.snapshot["vote_state"]["audience_published"] = True
                self.snapshot["match"]["screen_scene"] = "audience_result"
            self._persist_snapshot()
        return await self.emit("vote.published", {"scope": scope, "vote_state": self._public_vote_state()}, "host")

    async def record_speaker_heartbeat(self, payload: Dict[str, Any], fallback_speaker_id: Optional[str] = None) -> Dict[str, Any]:
        speaker_id = payload.get("speaker_id") or fallback_speaker_id
        async with self._lock:
            speaker = self._find_speaker(speaker_id)
            if speaker["speaker_type"] != "human":
                raise MatchStateError("invalid_speaker", "只有人类辩手控制台会上报心跳。", {"speaker_id": speaker_id})
            speaker["status"] = "online"
            speaker["mic_permission"] = payload.get("mic_permission", speaker.get("mic_permission", "unknown"))
            speaker["device_label"] = payload.get("device_label", speaker.get("device_label"))
            speaker["last_seen_at"] = iso_now()
            self._recompute_console_status()
            event_payload = {
                "speaker_id": speaker_id,
                "mic_permission": speaker["mic_permission"],
                "device_label": speaker["device_label"],
                "online": self.snapshot["speech_service"]["consoles"]["online"],
            }
            self._persist_snapshot()
        return await self.emit("speaker.heartbeat", event_payload, "speaker", speaker_id)

    async def record_speaker_mic_error(self, payload: Dict[str, Any], fallback_speaker_id: Optional[str] = None) -> Dict[str, Any]:
        speaker_id = payload.get("speaker_id") or fallback_speaker_id
        message = payload.get("message", "Microphone unavailable")
        async with self._lock:
            speaker = self._find_speaker(speaker_id)
            if speaker["speaker_type"] != "human":
                raise MatchStateError("invalid_speaker", "只有人类辩手控制台会上报麦克风异常。", {"speaker_id": speaker_id})
            speaker["status"] = "mic_error"
            speaker["mic_permission"] = payload.get("mic_permission", "denied")
            speaker["device_label"] = payload.get("device_label", speaker.get("device_label"))
            speaker["last_seen_at"] = iso_now()
            speaker["mic_error_message"] = message
            self._recompute_console_status()
            event_payload = {
                "speaker_id": speaker_id,
                "message": message,
                "mic_permission": speaker["mic_permission"],
                "device_label": speaker["device_label"],
            }
            self._persist_snapshot()
        return await self.emit("speaker.mic_error", event_payload, "speaker", speaker_id)

    async def mark_speaker_offline(self, speaker_id: Optional[str]) -> None:
        if not speaker_id:
            return
        try:
            async with self._lock:
                speaker = self._find_speaker(speaker_id)
                if speaker["speaker_type"] != "human":
                    return
                speaker["status"] = "offline"
                speaker["last_seen_at"] = iso_now()
                self._recompute_console_status()
                self._persist_snapshot()
            await self.emit("speaker.offline", {"speaker_id": speaker_id}, "system", speaker_id)
        except Exception:
            return

    def ensure_agent_speaker_for_current_phase(self, speaker_id: str) -> None:
        self._ensure_match_allows_control("agent_speech")
        speaker = self._find_speaker(speaker_id)
        if speaker["speaker_type"] != "agent":
            raise MatchStateError("invalid_speaker", "该辩手不是 AI 辩手，不能触发 Agent 发言。")
        speech = self.snapshot.get("current_speech") or {}
        if speech and speech.get("speaker_id") != speaker_id:
            raise MatchStateError(
                "speaker_locked",
                "当前已有其他辩手正在发言，请先结束当前发言。",
                {"active_speaker_id": speech.get("speaker_id"), "speaker_id": speaker_id},
            )
        config = self._agent_config_for_speaker(speaker)
        if config and not config.get("enabled", True):
            raise MatchStateError(
                "agent_config_disabled",
                "该 Agent 配置已停用，不能触发发言。",
                {"speaker_id": speaker_id, "agent_config_id": config.get("id")},
            )
        self._ensure_speaker_allowed_for_current_phase(speaker)

    async def check_agent_health(self, speaker_id: str) -> Dict[str, Any]:
        speaker = self._find_speaker(speaker_id)
        if speaker["speaker_type"] != "agent":
            raise MatchStateError("invalid_speaker", "该辩手不是 AI 辩手，不能执行 Agent 健康检查。")

        config = self._agent_config_for_speaker(speaker)
        if config and not config.get("enabled", True):
            payload = {
                "speaker_id": speaker_id,
                "agent_config_id": config.get("id"),
                "endpoint": self._agent_runtime_endpoint(config, speaker) or "disabled",
                "ok": False,
                "status": "disabled",
                "model": config.get("model_name") or speaker.get("model_name") or "unknown",
                "latency_ms": 0,
                "checked_at": iso_now(),
            }
            async with self._lock:
                self._update_agent_health_status(speaker_id, "failed", "Agent 配置已停用", payload)
                self._persist_snapshot()
            await self.emit("agent.failed", payload, "admin", speaker_id)
            return payload

        endpoint = self.agent_gateway.endpoint_for(speaker)
        checked_at = iso_now()
        try:
            health = await self.agent_gateway.health(endpoint)
            status = str(health.get("status") or ("ready" if health.get("ok", True) else "unavailable"))
            ok = bool(health.get("ok", True)) and status != "unavailable"
            model = str(health.get("model") or speaker.get("model_name") or "unknown")
            latency_ms = int(health.get("latency_ms") or 0)
            detail = f"health {status} · {latency_ms}ms"
            event_type = "agent.health_checked" if ok else "agent.failed"
            payload = {
                "speaker_id": speaker_id,
                "agent_config_id": speaker.get("agent_config_id"),
                "endpoint": endpoint or "embedded://mock",
                "ok": ok,
                "status": status,
                "model": model,
                "latency_ms": latency_ms,
                "version": health.get("version"),
                "checked_at": checked_at,
            }
        except AgentGatewayError as exc:
            ok = False
            status = "failed"
            model = str(speaker.get("model_name") or "unknown")
            latency_ms = 0
            detail = exc.message
            event_type = "agent.failed"
            payload = {
                "speaker_id": speaker_id,
                "agent_config_id": speaker.get("agent_config_id"),
                "endpoint": endpoint or "embedded://mock",
                "ok": False,
                "status": status,
                "model": model,
                "latency_ms": latency_ms,
                "checked_at": checked_at,
                "code": exc.code,
                "message": exc.message,
                "details": exc.details,
            }

        async with self._lock:
            self._update_agent_health_status(speaker_id, status if ok else "failed", detail, payload)
            self._persist_snapshot()

        await self.emit(event_type, payload, "admin", speaker_id)
        return payload

    async def check_all_agent_health(self) -> List[Dict[str, Any]]:
        async with self._lock:
            speaker_ids = [speaker["id"] for speaker in self.snapshot.get("speakers", []) if speaker.get("speaker_type") == "agent"]
        results = []
        for speaker_id in speaker_ids:
            results.append(await self.check_agent_health(speaker_id))
        return results

    async def test_agent_config(self, config_id: str, custom_payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """一次性连通性 / 调试测试：用已保存的 Agent 配置发起一次发言请求并返回结果。"""
        config = self._find_agent_config(config_id)
        return await self._run_agent_config_test(config, custom_payload)

    async def test_agent_config_inline(self, fields: Dict[str, Any], custom_payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """连通性测试未保存的 Agent 表单（新建页面用）。"""
        try:
            config = self._normalize_agent_config_fields(fields, now=iso_now(), create=True)
        except MatchStateError as exc:
            return {"ok": False, "error_code": exc.code, "error_message": exc.message, "details": exc.details}
        return await self._run_agent_config_test(config, custom_payload)

    async def _run_agent_config_test(self, config: Dict[str, Any], custom_payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        provider = config.get("provider_type", "rest_api")
        endpoint = config.get("endpoint", "").strip() if provider == "rest_api" else ""
        if provider == "rest_api" and not endpoint:
            return {"ok": False, "error_code": "missing_endpoint", "error_message": "REST API agent 未配置接口地址。"}

        topic = self.snapshot["match"].get("topic") or "测试辩题：AI 时代应培养编程思维还是提问思维"
        payload: Dict[str, Any] = custom_payload or {
            "model_name": config.get("model_name", ""),
            "debater_name": "测试辩手",
            "debate_position": "一辩",
            "debate_topic": topic,
            "current_stage": "正方一辩立论",
            "next_stage": "反方一辩立论",
            "holder": "正方",
            "time_limit_seconds": 60,
            "target_chars": 120,
            "other_info": {},
            "debate_history": [],
        }
        # Use a unique task id per test run so each invocation logs a distinct row.
        test_task_id = f"agent_test_{self.seq + 1}"
        payload.setdefault("task_id", test_task_id)
        payload.setdefault("speech_id", "agent_test")
        payload.setdefault("match_id", self.snapshot["match"]["id"])
        log_task_id = payload.get("task_id", test_task_id)

        match_id = self.snapshot["match"]["id"]
        started_at = iso_now()
        self.repo.save_agent_request_started(
            match_id=match_id,
            task_id=log_task_id,
            speech_id=str(payload.get("speech_id") or "agent_test"),
            speaker_id=f"agent_config:{config.get('id', '')}",
            endpoint=endpoint or "openai_sdk",
            request=payload,
            started_at=started_at,
            origin="test",
            **self._log_context(),
        )

        started = time.perf_counter()
        content = ""
        chunks = 0
        try:
            async for event in self.agent_gateway.stream_speech(
                endpoint, payload, ["（连通性测试占位回复）"], config=config
            ):
                if event.get("type") == "delta":
                    content += event.get("delta", "")
                    chunks += 1
                elif event.get("type") == "final" and event.get("content"):
                    content = event["content"]
            latency_ms = int((time.perf_counter() - started) * 1000)
            self.repo.finish_agent_request(
                match_id=match_id,
                task_id=log_task_id,
                status="completed",
                response_text=content,
                latency_ms=latency_ms,
                completed_at=iso_now(),
            )
            return {
                "ok": True,
                "content": content,
                "chunks": chunks,
                "latency_ms": latency_ms,
                "endpoint": endpoint or "openai_sdk",
                "model": config.get("model_id") or config.get("model_name"),
                "request": payload,
            }
        except AgentGatewayError as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            self.repo.finish_agent_request(
                match_id=match_id,
                task_id=log_task_id,
                status="failed",
                response_text=content or None,
                error_code=exc.code,
                error_message=exc.message,
                latency_ms=latency_ms,
                completed_at=iso_now(),
            )
            return {
                "ok": False,
                "error_code": exc.code,
                "error_message": exc.message,
                "details": exc.details,
                "latency_ms": latency_ms,
                "request": payload,
            }

    def log_xiaoqi_command(self, command: str, request_payload: Dict[str, Any], result: Dict[str, Any]) -> None:
        """记录小七命令的完整输入/输出，归类为「小七」类型日志。"""
        match_id = self.snapshot["match"]["id"]
        request_id = self._new_speech_service_request_id("xiaoqi", command or "command")
        now = iso_now()
        sent = bool(result.get("sent"))
        self.repo.save_speech_service_request_started(
            match_id=match_id,
            request_id=request_id,
            service="xiaoqi",
            operation=command or "command",
            request=request_payload,
            started_at=now,
            origin="live",
            **self._log_context(),
        )
        self.repo.finish_speech_service_request(
            match_id=match_id,
            request_id=request_id,
            status="completed" if sent else "failed",
            response=result,
            error_message=None if sent else str(result.get("reason") or "未发送"),
            latency_ms=None,
            completed_at=iso_now(),
        )

    def clear_request_logs(self) -> None:
        """清空当前比赛的 Agent / 语音 / 审计请求日志。"""
        self.repo.clear_request_logs(self.snapshot["match"]["id"])

    def get_request_logs(self, limit: int = 200) -> Dict[str, Any]:
        """API 请求日志：Agent 请求 + 语音服务请求 + 操作审计。"""
        match_id = self.snapshot["match"]["id"]
        return {
            "match_id": match_id,
            "agent_requests": self.repo.load_agent_requests(match_id, limit),
            "speech_service_requests": self.repo.load_speech_service_requests(match_id, limit),
            "audit_logs": self.repo.load_audit_logs(match_id, limit),
        }

    async def websocket(self, websocket: WebSocket, last_seq: int = 0, channel: str = "screen", speaker_id: Optional[str] = None) -> None:
        await websocket.accept()
        self._connections.add(websocket)
        try:
            snapshot = await self.get_snapshot()
            missed = [event for event in self.events if event["seq"] > last_seq]
            try:
                await websocket.send_json(
                    {
                        "type": "snapshot",
                        "match_id": snapshot["match"]["id"],
                        "seq": self.seq,
                        "server_time_ms": int(utc_now().timestamp() * 1000),
                        "payload": {"state": snapshot, "missed_events": missed},
                    }
                )
            except (WebSocketDisconnect, RuntimeError):
                return
            while True:
                try:
                    text = await websocket.receive_text()
                    await self._handle_client_message(text, channel, speaker_id)
                except (WebSocketDisconnect, RuntimeError):
                    break
                except Exception:
                    continue
        finally:
            self._connections.discard(websocket)
            if channel == "speaker":
                await self.mark_speaker_offline(speaker_id)

    async def _handle_client_message(self, text: str, channel: str, speaker_id: Optional[str]) -> None:
        try:
            message = json.loads(text)
        except json.JSONDecodeError:
            return
        message_type = message.get("type")
        payload = message.get("payload") or {}
        if message_type == "speaker.heartbeat" and channel == "speaker":
            await self.record_speaker_heartbeat(payload, speaker_id)
        elif message_type == "speaker.mic_error" and channel == "speaker":
            await self.record_speaker_mic_error(payload, speaker_id)

    async def _broadcast(self, message: Dict[str, Any]) -> None:
        disconnected: List[WebSocket] = []
        for websocket in list(self._connections):
            try:
                await websocket.send_json(message)
            except Exception:
                disconnected.append(websocket)
        for websocket in disconnected:
            self._connections.discard(websocket)

    def _refresh_clocks(self) -> None:
        now = utc_now()
        for clock in self.snapshot["clocks"]:
            if clock["state"] != "running":
                continue
            deadline = parse_iso(clock.get("deadline_at"))
            if not deadline:
                continue
            remaining = max(0, int((deadline - now).total_seconds() * 1000))
            clock["remaining_ms"] = remaining
            if remaining == 0:
                clock["state"] = "expired"
                clock["deadline_at"] = None

    def _persist_snapshot(self) -> None:
        self._ensure_runtime_fields()
        self.snapshot["last_seq"] = self.seq
        self.repo.save_snapshot(self.snapshot, iso_now())

    def _new_match_snapshot_from_archive(self, archived: Dict[str, Any], new_match_id: str, now: datetime) -> Dict[str, Any]:
        phases = deepcopy(archived.get("phases", []))
        phases.sort(key=lambda item: item.get("display_order", 0))
        first_phase_id = phases[0]["id"] if phases else archived["match"].get("current_phase_id")
        for phase in phases:
            phase["status"] = "active" if phase["id"] == first_phase_id else "pending"

        speakers = deepcopy(archived.get("speakers", []))
        for speaker in speakers:
            speaker["status"] = "online" if speaker.get("speaker_type") == "human" else "ready"
            speaker["mic_permission"] = "unknown" if speaker.get("speaker_type") == "human" else None
            speaker["device_label"] = None
            speaker["last_seen_at"] = None
            speaker.pop("mic_error_message", None)

        agent_status = []
        for speaker in speakers:
            if speaker.get("speaker_type") != "agent":
                continue
            agent_status.append(
                {
                    "speaker_id": speaker["id"],
                    "agent_config_id": speaker.get("agent_config_id"),
                    "name": speaker["name"],
                    "model": speaker.get("model_name") or "未配置模型",
                    "status": "ready",
                    "last_heartbeat_seconds": None,
                    "detail": "等待联调",
                    "endpoint": speaker.get("agent_endpoint"),
                }
            )

        match = deepcopy(archived["match"])
        match.update(
            {
                "id": new_match_id,
                "status": "ready",
                "screen_scene": "idle",
                "live_mode": "single",
                "current_phase_id": first_phase_id,
                "created_at": to_iso(now),
                "updated_at": to_iso(now),
            }
        )

        snapshot = {
            "match": match,
            "teams": deepcopy(archived.get("teams", [])),
            "speakers": speakers,
            "agent_configs": deepcopy(archived.get("agent_configs", [])),
            "phases": phases,
            "clocks": [],
            "current_speech": None,
            "free_debate": {
                "current_turn_side": "affirmative",
                "turn_index": 1,
                "assignment_mode": archived.get("free_debate", {}).get("assignment_mode", "teammate_control"),
            },
            "flow": self._fresh_flow_state(),
            "audio_output": deepcopy(archived.get("audio_output") or self._fresh_audio_output_state()),
            "recent_transcript": [],
            "speech_revisions": [],
            "audio_assets": [],
            "agent_status": agent_status,
            "vote_state": self._fresh_vote_state(speakers),
            "speech_service": self._fresh_speech_service(speakers),
            "system": self._system_info(),
            "last_seq": self.seq,
        }

        previous_snapshot = self.snapshot
        self.snapshot = snapshot
        try:
            if phases:
                self._reset_clocks_for_phase(phases[0])
        finally:
            snapshot = self.snapshot
            self.snapshot = previous_snapshot
        return snapshot

    def _fresh_vote_state(self, speakers: List[Dict[str, Any]]) -> Dict[str, Any]:
        first_speaker = next((speaker["id"] for speaker in speakers if speaker.get("side") in {"affirmative", "negative"}), "")
        judge_summary = self._empty_judge_summary()
        judge_summary["best_speaker_id"] = first_speaker
        return {
            "window_status": "closed",
            "audience_count": 0,
            "judge_published": False,
            "audience_published": False,
            "winner_side": "affirmative",
            "best_speaker_id": first_speaker,
            "judge_summary": judge_summary,
            "audience_summary": self._empty_audience_summary(),
            "audience_votes": [],
            "audience_vote_keys": [],
            "used_audience_tokens": [],
        }

    def _fresh_speech_service(self, speakers: List[Dict[str, Any]]) -> Dict[str, Any]:
        human_count = len([speaker for speaker in speakers if speaker.get("speaker_type") == "human"])
        return {
            "asr": {"status": "ok", "latency_ms": 0, "active_sessions": 0, "detail": "idle"},
            "tts": {"status": "idle", "latency_ms": 0, "queue_size": 0, "speaker_id": None, "detail": ""},
            "screen": {"status": "connected"},
            "consoles": {"online": 0, "total": human_count, "mic_errors": []},
        }

    def _fresh_flow_state(self) -> Dict[str, Any]:
        return {
            "awaiting_host_confirm": False,
            "reason": None,
            "message": "",
            "next_action": None,
            "phase_id": None,
            "speech_id": None,
            "speaker_id": None,
            "expired_clocks": [],
            "created_at": None,
        }

    def _fresh_audio_output_state(self) -> Dict[str, Any]:
        return {
            "mode": "host",
            "label": self._audio_output_label("host"),
            "updated_by": "system",
            "updated_at": iso_now(),
        }

    def _normalize_audio_output_mode(self, mode: str) -> str:
        value = str(mode or "").strip()
        if value not in {"host", "admin", "off"}:
            raise MatchStateError(
                "invalid_audio_output",
                "现场声音输出端必须为 host、admin 或 off。",
                {"mode": mode},
            )
        return value

    def _audio_output_label(self, mode: str) -> str:
        if mode == "admin":
            return "技术后台电脑"
        if mode == "off":
            return "关闭浏览器提示音"
        return "主持导播台电脑"

    def _default_agent_config_id(self, speaker_id: str) -> str:
        return f"agent_{speaker_id}"

    def _agent_config_from_speaker(self, speaker: Dict[str, Any], now: str) -> Dict[str, Any]:
        return {
            "id": speaker.get("agent_config_id") or self._default_agent_config_id(str(speaker.get("id") or "")),
            "name": f"{speaker.get('name') or '未命名'} Agent",
            "provider_type": "openai_sdk",
            "request_method": "POST",
            "model_name": speaker.get("model_name") or "qwen3.6-plus",
            "model_id": speaker.get("model_id") or "qwen3.6-plus",
            "model_kind": speaker.get("model_kind") or "closed_source",
            "endpoint": speaker.get("agent_endpoint") or self._agent_endpoint_for_speaker(str(speaker.get("id") or "")),
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "api_key_env": "DASHSCOPE_API_KEY",
            "timeout_ms": getattr(self.agent_gateway, "read_timeout_ms", 30000),
            "enabled": True,
            "created_at": now,
            "updated_at": now,
        }

    def _normalize_agent_config_fields(
        self,
        fields: Dict[str, Any],
        *,
        existing: Optional[Dict[str, Any]] = None,
        now: str,
        create: bool,
    ) -> Dict[str, Any]:
        source = existing or {}
        provider_type = str(fields.get("provider_type", source.get("provider_type", "rest_api")) or "rest_api").strip()
        if provider_type not in {"rest_api", "openai_sdk"}:
            raise MatchStateError("invalid_agent_config", "Agent 类型必须为 rest_api 或 openai_sdk。", {"provider_type": provider_type})

        request_method = str(fields.get("request_method", source.get("request_method", "POST")) or "POST").strip().upper()
        if request_method not in {"GET", "POST", "PUT", "PATCH"}:
            raise MatchStateError("invalid_agent_config", "请求方式必须为 GET、POST、PUT 或 PATCH。", {"request_method": request_method})

        name = str(fields.get("name", source.get("name", "")) or "").strip()
        if not name:
            raise MatchStateError("invalid_agent_config", "Agent 名称不能为空。")

        model_name = str(fields.get("model_name", fields.get("model", source.get("model_name", ""))) or "").strip()
        if not model_name:
            model_name = "未配置模型"

        # model_id is the actual id passed to the OpenAI-compatible API (openai_sdk mode).
        # Defaults to the 需求 2.md test model so demo agents stream out of the box.
        model_id = str(fields.get("model_id", source.get("model_id", "")) or "").strip()
        if not model_id and provider_type == "openai_sdk":
            model_id = "qwen3.6-plus"

        model_kind = fields.get("model_kind", source.get("model_kind", "closed_source"))
        if model_kind not in {"open_source", "closed_source"}:
            raise MatchStateError("invalid_agent_config", "模型类型必须为 open_source 或 closed_source。", {"model_kind": model_kind})

        timeout_raw = fields.get("timeout_ms", source.get("timeout_ms", getattr(self.agent_gateway, "read_timeout_ms", 30000)))
        try:
            timeout_ms = int(timeout_raw)
        except (TypeError, ValueError) as exc:
            raise MatchStateError("invalid_agent_config", "Agent 超时时间必须为数字。", {"timeout_ms": timeout_raw}) from exc
        if timeout_ms < 1000 or timeout_ms > 120000:
            raise MatchStateError("invalid_agent_config", "Agent 超时时间必须在 1000 到 120000 毫秒之间。", {"timeout_ms": timeout_ms})

        enabled = fields.get("enabled", source.get("enabled", True))
        if isinstance(enabled, str):
            enabled = enabled.lower() not in {"false", "0", "no", "off", "停用"}
        else:
            enabled = bool(enabled)

        config = {
            "id": source.get("id", ""),
            "name": name,
            "provider_type": provider_type,
            "request_method": request_method,
            "model_name": model_name,
            "model_id": model_id,
            "model_kind": model_kind,
            "endpoint": str(fields.get("endpoint", source.get("endpoint", "")) or "").strip(),
            "base_url": str(fields.get("base_url", source.get("base_url", "")) or "").strip(),
            "api_key_env": str(fields.get("api_key_env", source.get("api_key_env", "")) or "").strip(),
            "timeout_ms": timeout_ms,
            "enabled": enabled,
            "created_at": source.get("created_at") or now,
            "updated_at": now,
        }
        if "api_key" in fields:
            raise MatchStateError("invalid_agent_config", "不能保存明文 API Key，请填写环境变量名。")
        if create:
            config["created_at"] = now
        return config

    def _unique_agent_config_id(self, name: str) -> str:
        existing_ids = {config.get("id") for config in self.snapshot.get("agent_configs", [])}
        digest = hashlib.sha1(f"{name}:{iso_now()}:{len(existing_ids)}".encode("utf-8")).hexdigest()[:8]
        candidate = f"agent_cfg_{digest}"
        suffix = 1
        while candidate in existing_ids:
            suffix += 1
            candidate = f"agent_cfg_{digest}_{suffix}"
        return candidate

    def _find_agent_config(self, agent_config_id: str) -> Dict[str, Any]:
        for config in self.snapshot.get("agent_configs", []):
            if config.get("id") == agent_config_id:
                return config
        raise MatchStateError("agent_config_not_found", "未找到指定 Agent 配置。", {"agent_config_id": agent_config_id})

    def _agent_config_for_speaker(self, speaker: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        config_id = speaker.get("agent_config_id")
        if not config_id:
            return None
        try:
            return self._find_agent_config(str(config_id))
        except MatchStateError:
            return None

    def _agent_runtime_endpoint(self, config: Optional[Dict[str, Any]], speaker: Dict[str, Any]) -> str:
        if config and config.get("provider_type") == "rest_api":
            return str(config.get("endpoint") or "").strip()
        return str(speaker.get("agent_endpoint") or "").strip()

    def _apply_agent_config_to_speaker(self, speaker: Dict[str, Any]) -> None:
        if speaker.get("speaker_type") != "agent":
            return
        config = self._agent_config_for_speaker(speaker)
        if not config:
            return
        speaker["model_name"] = config.get("model_name") or speaker.get("model_name") or "未配置模型"
        speaker["model_kind"] = config.get("model_kind") or speaker.get("model_kind") or "closed_source"
        speaker["agent_endpoint"] = self._agent_runtime_endpoint(config, speaker)

    def _sync_default_agent_config_from_speaker(self, speaker: Dict[str, Any]) -> None:
        if speaker.get("speaker_type") != "agent":
            return
        if speaker.get("agent_config_id") != self._default_agent_config_id(str(speaker.get("id") or "")):
            return
        try:
            config = self._find_agent_config(str(speaker.get("agent_config_id")))
        except MatchStateError:
            return
        config["name"] = f"{speaker.get('name') or '未命名'} Agent"
        config["model_name"] = speaker.get("model_name") or "未配置模型"
        config["model_kind"] = speaker.get("model_kind") or "closed_source"
        config["endpoint"] = speaker.get("agent_endpoint") or ""
        config["provider_type"] = config.get("provider_type") or "rest_api"
        config["request_method"] = config.get("request_method") or "POST"
        config["updated_at"] = iso_now()

    def _ensure_runtime_fields(self) -> None:
        self.snapshot["system"] = self._system_info()
        flow = self.snapshot.setdefault("flow", self._fresh_flow_state())
        fresh_flow = self._fresh_flow_state()
        for key, value in fresh_flow.items():
            flow.setdefault(key, value)
        self.snapshot.setdefault(
            "free_debate",
            {
                "current_turn_side": "affirmative",
                "turn_index": 1,
                "assignment_mode": "teammate_control",
            },
        )
        audio_output = self.snapshot.setdefault("audio_output", self._fresh_audio_output_state())
        try:
            mode = self._normalize_audio_output_mode(str(audio_output.get("mode", "host")))
        except MatchStateError:
            mode = "host"
        audio_output["mode"] = mode
        audio_output["label"] = self._audio_output_label(mode)
        audio_output.setdefault("updated_at", iso_now())
        self.snapshot.setdefault(
            "vote_state",
            {
                "window_status": "closed",
                "audience_count": 0,
                "judge_published": False,
                "audience_published": False,
                "winner_side": "affirmative",
                "best_speaker_id": "",
                "judge_summary": self._empty_judge_summary(),
                "audience_summary": self._empty_audience_summary(),
                "audience_votes": [],
                "audience_vote_keys": [],
                "used_audience_tokens": [],
            },
        )
        self.snapshot["vote_state"].setdefault("judge_summary", self._empty_judge_summary())
        self.snapshot["vote_state"].setdefault("audience_summary", self._empty_audience_summary())
        self.snapshot["vote_state"].setdefault("audience_votes", [])
        self.snapshot["vote_state"].setdefault("audience_vote_keys", [])
        self.snapshot["vote_state"].setdefault("used_audience_tokens", [])
        audience_summary = self.snapshot["vote_state"]["audience_summary"]
        if not self.snapshot["vote_state"]["audience_votes"] and audience_summary.get("total", 0) == 0 and self.snapshot["vote_state"].get("audience_count", 0) > 0:
            count = int(self.snapshot["vote_state"]["audience_count"])
            winner_side = self.snapshot["vote_state"].get("winner_side", "affirmative")
            audience_summary["total"] = count
            audience_summary.setdefault("winner", {"affirmative": 0, "negative": 0})
            audience_summary["winner"][winner_side] = count
        now = iso_now()
        self.snapshot.setdefault("agent_configs", [])
        config_ids = {config.get("id") for config in self.snapshot["agent_configs"]}
        for speaker in self.snapshot.get("speakers", []):
            if speaker.get("speaker_type") == "human":
                speaker.setdefault("mic_permission", "unknown")
                speaker.setdefault("device_label", None)
                speaker.setdefault("last_seen_at", None)
                speaker.pop("agent_config_id", None)
            else:
                speaker.setdefault("mic_permission", None)
                speaker.setdefault("device_label", None)
                speaker.setdefault("last_seen_at", None)
                speaker.setdefault("agent_config_id", self._default_agent_config_id(str(speaker.get("id") or "")))
                if speaker["agent_config_id"] not in config_ids:
                    config = self._agent_config_from_speaker(speaker, now)
                    self.snapshot["agent_configs"].append(config)
                    config_ids.add(config["id"])
                self._apply_agent_config_to_speaker(speaker)
        normalized_configs = []
        for config in self.snapshot.get("agent_configs", []):
            normalized = self._normalize_agent_config_fields(config, existing=config, now=now, create=False)
            normalized["id"] = str(config.get("id") or self._unique_agent_config_id(normalized["name"]))
            normalized_configs.append(normalized)
        self.snapshot["agent_configs"] = normalized_configs
        current_phase_id = self.snapshot.get("match", {}).get("current_phase_id")
        for segment in self.snapshot.get("recent_transcript", []):
            segment.setdefault("phase_id", current_phase_id)
            segment.setdefault("speech_id", segment.get("id"))
            segment.setdefault("turn_index", None)
            segment.setdefault("valid", True)
            segment.setdefault("invalid_reason", None)
        self.snapshot.setdefault("speech_revisions", [])
        self.snapshot.setdefault("audio_assets", [])
        speech_service = self.snapshot.setdefault("speech_service", {})
        speech_service.setdefault("asr", {})
        speech_service["asr"].setdefault("status", "ok")
        speech_service["asr"].setdefault("latency_ms", 0)
        speech_service["asr"].setdefault("active_sessions", 0)
        speech_service["asr"].setdefault("detail", "")
        speech_service.setdefault("tts", {})
        speech_service["tts"].setdefault("status", "idle")
        speech_service["tts"].setdefault("latency_ms", 0)
        speech_service["tts"].setdefault("queue_size", 0)
        speech_service["tts"].setdefault("speaker_id", None)
        speech_service["tts"].setdefault("detail", "")
        speech_service.setdefault("consoles", {})
        speech_service["consoles"].setdefault("online", 0)
        speech_service["consoles"].setdefault(
            "total",
            len([item for item in self.snapshot.get("speakers", []) if item.get("speaker_type") == "human"]),
        )
        speech_service["consoles"].setdefault("mic_errors", [])

    def _system_info(self) -> Dict[str, Any]:
        return {
            "persistence": {
                "driver": "sqlite",
                "database_path": str(self.repo.db_path),
            }
        }

    def _sanitize_snapshot(self, snapshot: Dict[str, Any]) -> None:
        vote_state = snapshot.get("vote_state", {})
        vote_state.pop("audience_vote_keys", None)
        vote_state.pop("used_audience_tokens", None)
        vote_state.pop("audience_votes", None)
        for config in snapshot.get("agent_configs", []):
            config.pop("api_key", None)
        snapshot["next_speaker"] = self._next_speaker_info(snapshot)
        snapshot["integration_config"] = integration_config.public()

    def _next_speaker_info(self, snapshot: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """需求 2.md：辩手端需展示下一个发言的选手。固定环节解析为下一环节的辩位与辩手，
        自由辩论给出先手方提示。"""
        phases = snapshot.get("phases", [])
        current_id = snapshot.get("match", {}).get("current_phase_id")
        current = next((p for p in phases if p.get("id") == current_id), None)
        order = current.get("display_order") if current else -1
        candidates = [p for p in phases if isinstance(p.get("display_order"), int) and p["display_order"] > order]
        nxt = min(candidates, key=lambda p: p["display_order"]) if candidates else None
        if not nxt:
            return None
        info: Dict[str, Any] = {
            "phase_id": nxt.get("id"),
            "phase_name": nxt.get("name"),
            "phase_type": nxt.get("phase_type"),
        }
        if nxt.get("phase_type") == "free_debate":
            info.update({"side": "affirmative", "label": f"{nxt.get('name')} · 正方先手"})
            return info
        speaker = next(
            (
                s
                for s in snapshot.get("speakers", [])
                if s.get("side") == nxt.get("side") and s.get("seat") == nxt.get("speaker_seat")
            ),
            None,
        )
        if speaker:
            info.update(
                {
                    "speaker_id": speaker.get("id"),
                    "speaker_name": speaker.get("name"),
                    "speaker_type": speaker.get("speaker_type"),
                    "side": speaker.get("side"),
                    "seat": speaker.get("seat"),
                    "label": f"{nxt.get('name')} · {speaker.get('name')}",
                }
            )
        else:
            info["label"] = nxt.get("name")
        return info

    def _empty_judge_summary(self) -> Dict[str, Any]:
        return {
            "constructive": {"affirmative": 0, "negative": 0},
            "process": {"affirmative": 0, "negative": 0},
            "conclusion": {"affirmative": 0, "negative": 0},
            "computed_winner_side": "affirmative",
            "winner_side": "affirmative",
            "best_speaker_id": "",
        }

    def _empty_audience_summary(self) -> Dict[str, Any]:
        return {
            "total": 0,
            "winner": {"affirmative": 0, "negative": 0},
            "best_speaker": [],
        }

    def _public_vote_state(self) -> Dict[str, Any]:
        vote_state = deepcopy(self.snapshot["vote_state"])
        vote_state.pop("audience_vote_keys", None)
        vote_state.pop("used_audience_tokens", None)
        vote_state.pop("audience_votes", None)
        return vote_state

    def _zip_writestr(
        self,
        bundle: zipfile.ZipFile,
        arcname: str,
        content: Any,
        entries: List[Dict[str, Any]],
        text: bool = False,
    ) -> None:
        data = content if text else json.dumps(content, ensure_ascii=False, indent=2)
        encoded = data.encode("utf-8")
        bundle.writestr(arcname, encoded)
        entries.append({"path": arcname, "size_bytes": len(encoded)})

    def _jsonl(self, rows: List[Dict[str, Any]]) -> str:
        return "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + ("\n" if rows else "")

    def _latest_export_from_events(self, events: List[Dict[str, Any]]) -> Dict[str, Any]:
        for event in reversed(events):
            if event.get("type") == "export.created":
                payload = event.get("payload", {})
                return payload if isinstance(payload, dict) else {}
        return {}

    def _compact_export_bundle(self, bundle: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not bundle:
            return None
        entries = bundle.get("entries") or []
        safe_entries = []
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                safe_entries.append(
                    {
                        "path": str(entry.get("path") or ""),
                        "size_bytes": int(entry.get("size_bytes") or 0),
                    }
                )
        return {
            "export_id": bundle.get("export_id", ""),
            "match_id": bundle.get("match_id", ""),
            "download_url": bundle.get("download_url", ""),
            "size_bytes": int(bundle.get("size_bytes") or 0),
            "entry_count": len(safe_entries) if isinstance(entries, list) else int(bundle.get("entry_count") or 0),
            "entries": safe_entries,
            "created_at": bundle.get("created_at", ""),
        }

    def _status_counts(self, rows: List[Dict[str, Any]]) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for row in rows:
            status = str(row.get("status") or "unknown")
            counts[status] = counts.get(status, 0) + 1
        return counts

    def _event_type_counts(self, events: List[Dict[str, Any]]) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for event in events:
            event_type = str(event.get("type") or "unknown")
            counts[event_type] = counts.get(event_type, 0) + 1
        return counts

    def _compact_event(self, event: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": event.get("id", ""),
            "match_id": event.get("match_id", ""),
            "seq": event.get("seq", 0),
            "type": event.get("type", ""),
            "actor_type": event.get("actor_type", ""),
            "actor_id": event.get("actor_id"),
            "created_at": event.get("created_at", ""),
        }

    def _compact_agent_request(self, row: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": row.get("id", ""),
            "task_id": row.get("task_id", ""),
            "speech_id": row.get("speech_id"),
            "speaker_id": row.get("speaker_id", ""),
            "endpoint": row.get("endpoint", ""),
            "status": row.get("status", ""),
            "error_code": row.get("error_code"),
            "error_message": row.get("error_message"),
            "latency_ms": row.get("latency_ms"),
            "started_at": row.get("started_at", ""),
            "completed_at": row.get("completed_at"),
        }

    def _compact_speech_service_request(self, row: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": row.get("id", ""),
            "request_id": row.get("request_id", ""),
            "service": row.get("service", ""),
            "operation": row.get("operation", ""),
            "speech_id": row.get("speech_id"),
            "speaker_id": row.get("speaker_id"),
            "status": row.get("status", ""),
            "error_code": row.get("error_code"),
            "error_message": row.get("error_message"),
            "latency_ms": row.get("latency_ms"),
            "started_at": row.get("started_at", ""),
            "completed_at": row.get("completed_at"),
        }

    def _new_speech_service_request_id(self, service: str, operation: str) -> str:
        service_part = "".join(ch if ch.isalnum() else "_" for ch in service.lower()).strip("_") or "speech"
        operation_part = "".join(ch if ch.isalnum() else "_" for ch in operation.lower()).strip("_") or "request"
        timestamp = utc_now().strftime("%Y%m%dT%H%M%S%fZ")
        return f"{service_part}_{operation_part}_{timestamp}_{self.seq + 1}"

    def _transcript_csv(self, transcript: List[Dict[str, Any]]) -> str:
        output = io.StringIO()
        writer = csv.DictWriter(
            output,
            fieldnames=[
                "created_at",
                "phase_id",
                "speaker_id",
                "speaker_label",
                "source",
                "is_final",
                "turn_index",
                "text",
            ],
        )
        writer.writeheader()
        for segment in transcript:
            writer.writerow(
                {
                    "created_at": segment.get("created_at", ""),
                    "phase_id": segment.get("phase_id", ""),
                    "speaker_id": segment.get("speaker_id", ""),
                    "speaker_label": segment.get("speaker_label", ""),
                    "source": segment.get("source", ""),
                    "is_final": segment.get("is_final", False),
                    "turn_index": segment.get("turn_index", ""),
                    "text": segment.get("text", ""),
                }
            )
        return output.getvalue()

    def _rows_csv(self, rows: List[Dict[str, Any]], fieldnames: List[str]) -> str:
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: self._csv_value(row.get(field)) for field in fieldnames})
        return output.getvalue()

    def _csv_value(self, value: Any) -> Any:
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        if value is None:
            return ""
        return value

    def _agent_events_for_export(self, events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [
            event
            for event in events
            if str(event.get("type") or "").startswith("agent.")
        ]

    def _zip_audio_assets(self, bundle: zipfile.ZipFile, audio_assets: List[Dict[str, Any]], entries: List[Dict[str, Any]]) -> None:
        for asset in audio_assets:
            for chunk in asset.get("chunks", []):
                file_path = Path(chunk.get("file_path", ""))
                if not file_path.exists() or not file_path.is_file():
                    continue
                arcname = f"audio/{self._safe_path_part(asset.get('speech_id', 'speech'))}/{file_path.name}"
                bundle.write(file_path, arcname)
                entries.append({"path": arcname, "size_bytes": file_path.stat().st_size})

    def _audience_vote_keys(self, body: Dict[str, Any]) -> List[str]:
        token = str(body.get("token") or "").strip()
        fingerprint = str(body.get("client_fingerprint") or "").strip()
        request_ip = str(body.get("request_ip") or "").strip()
        request_user_agent = str(body.get("request_user_agent") or "").strip()
        keys: List[str] = []
        if token:
            keys.append(self._audience_hash_key("token", token))
        browser_material = "|".join([request_ip, request_user_agent, fingerprint]).strip("|")
        if browser_material:
            keys.append(self._audience_hash_key("browser", browser_material))
        if not keys:
            raise MatchStateError("invalid_vote", "学生投票必须包含 token 或浏览器指纹。")
        return keys

    def _legacy_audience_vote_keys(self, body: Dict[str, Any]) -> List[str]:
        keys: List[str] = []
        token = str(body.get("token") or "").strip()
        fingerprint = str(body.get("client_fingerprint") or "").strip()
        if token:
            keys.append(f"token:{token}")
        if fingerprint:
            keys.append(f"fingerprint:{fingerprint}")
        return keys

    def _audience_hash_key(self, namespace: str, value: str) -> str:
        match_id = self.snapshot.get("match", {}).get("id", "current")
        digest = hashlib.sha256(f"{match_id}:{namespace}:{value}".encode("utf-8")).hexdigest()
        return f"{namespace}_hash:{digest}"

    def _vote_key_types_for_log(self, keys: List[str]) -> List[str]:
        labels: List[str] = []
        for key in keys:
            label = f"{str(key).split(':', 1)[0]}:*"
            if label not in labels:
                labels.append(label)
        return labels

    def _build_judge_summary(self, body: Dict[str, Any]) -> Dict[str, Any]:
        if "judge_summary" in body:
            return self._normalize_judge_summary(body["judge_summary"])

        summary = self._empty_judge_summary()
        if "items" in body:
            for item in body["items"]:
                vote_type = item.get("vote_type")
                if vote_type in {"constructive", "process", "conclusion"}:
                    side = item.get("target_side")
                    self._validate_side(side)
                    summary[vote_type][side] += 1
                elif vote_type == "winner":
                    side = item.get("target_side")
                    self._validate_side(side)
                    summary["winner_side"] = side
                elif vote_type == "best_speaker":
                    speaker_id = item.get("target_speaker_id")
                    self._find_speaker(speaker_id)
                    summary["best_speaker_id"] = speaker_id
                else:
                    raise MatchStateError("invalid_vote", "未知的评委票类型。", {"vote_type": vote_type})
        else:
            existing = self.snapshot["vote_state"].get("judge_summary", self._empty_judge_summary())
            summary = self._normalize_judge_summary(existing)

        if "winner_side" in body:
            self._validate_side(body["winner_side"])
            summary["winner_side"] = body["winner_side"]
        if "best_speaker_id" in body:
            self._find_speaker(body["best_speaker_id"])
            summary["best_speaker_id"] = body["best_speaker_id"]

        summary["computed_winner_side"] = self._computed_winner_side(summary)
        if not summary.get("winner_side"):
            summary["winner_side"] = summary["computed_winner_side"]
        return summary

    def _normalize_judge_summary(self, value: Dict[str, Any]) -> Dict[str, Any]:
        summary = self._empty_judge_summary()
        for vote_type in ("constructive", "process", "conclusion"):
            row = value.get(vote_type, {})
            summary[vote_type] = {
                "affirmative": int(row.get("affirmative", 0)),
                "negative": int(row.get("negative", 0)),
            }
        winner_side = value.get("winner_side") or value.get("computed_winner_side") or self._computed_winner_side(summary)
        self._validate_side(winner_side)
        summary["computed_winner_side"] = self._computed_winner_side(summary)
        summary["winner_side"] = winner_side
        best_speaker_id = value.get("best_speaker_id") or self.snapshot.get("vote_state", {}).get("best_speaker_id", "")
        if best_speaker_id:
            self._find_speaker(best_speaker_id)
        summary["best_speaker_id"] = best_speaker_id
        return summary

    def _computed_winner_side(self, summary: Dict[str, Any]) -> str:
        aff = 0
        neg = 0
        for vote_type in ("constructive", "process", "conclusion"):
            aff += int(summary.get(vote_type, {}).get("affirmative", 0))
            neg += int(summary.get(vote_type, {}).get("negative", 0))
        return "affirmative" if aff >= neg else "negative"

    def _append_audience_summary(self, body: Dict[str, Any]) -> None:
        summary = self.snapshot["vote_state"].setdefault("audience_summary", self._empty_audience_summary())
        summary.setdefault("winner", {"affirmative": 0, "negative": 0})
        summary.setdefault("best_speaker", [])
        summary["total"] = int(summary.get("total", self.snapshot["vote_state"].get("audience_count", 0))) + 1
        summary["winner"][body["winner_side"]] = int(summary["winner"].get(body["winner_side"], 0)) + 1

        found = False
        for item in summary["best_speaker"]:
            if item["speaker_id"] == body["best_speaker_id"]:
                item["count"] = int(item.get("count", 0)) + 1
                found = True
                break
        if not found:
            summary["best_speaker"].append({"speaker_id": body["best_speaker_id"], "count": 1})
        summary["best_speaker"].sort(key=lambda item: item["count"], reverse=True)
        self.snapshot["vote_state"]["audience_count"] = summary["total"]

    def _recompute_audience_summary(self) -> None:
        votes = self.snapshot.get("vote_state", {}).get("audience_votes", [])
        if not votes:
            return
        summary = self._empty_audience_summary()
        self.snapshot["vote_state"]["audience_summary"] = summary
        self.snapshot["vote_state"]["audience_count"] = 0
        for vote in votes:
            self._append_audience_summary(vote)

    def _recompute_console_status(self) -> None:
        humans = [speaker for speaker in self.snapshot.get("speakers", []) if speaker.get("speaker_type") == "human"]
        online = [speaker for speaker in humans if speaker.get("status") in {"online", "mic_error"}]
        mic_errors = [
            {
                "speaker_id": speaker["id"],
                "name": speaker["name"],
                "mic_permission": speaker.get("mic_permission", "unknown"),
                "message": speaker.get("mic_error_message", "Microphone unavailable"),
                "last_seen_at": speaker.get("last_seen_at"),
            }
            for speaker in humans
            if speaker.get("status") == "mic_error" or speaker.get("mic_permission") == "denied"
        ]
        consoles = self.snapshot.setdefault("speech_service", {}).setdefault("consoles", {})
        consoles["online"] = len(online)
        consoles["total"] = len(humans)
        consoles["mic_errors"] = mic_errors

    def _build_debate_history(self) -> list:
        """Group final+valid transcript segments by phase, return ordered list."""
        phase_by_id = {p["id"]: p["name"] for p in self.snapshot["phases"]}
        ordered_phase_ids = [p["id"] for p in sorted(self.snapshot["phases"], key=lambda x: x["display_order"])]
        groups: Dict[str, list] = {}
        for segment in self.snapshot.get("recent_transcript", []):
            if not segment.get("valid", True) or not segment.get("is_final"):
                continue
            pid = segment.get("phase_id", "")
            if pid not in groups:
                groups[pid] = []
            groups[pid].append({
                "speaker": segment["speaker_label"],
                "content": segment["text"],
            })
        return [
            {"stage": phase_by_id.get(pid, pid), "content": groups[pid]}
            for pid in ordered_phase_ids
            if pid in groups
        ]

    def _build_agent_payload(self, task_id: str, speech_id: str, speaker: Dict[str, Any]) -> Dict[str, Any]:
        phase = self._current_phase()
        match = self.snapshot["match"]
        config = self._agent_config_for_speaker(speaker) or {}
        time_limit = self._free_turn_seconds(phase) if phase["phase_type"] == "free_debate" else phase["duration_seconds"]
        clock = self._clock("turn" if phase["phase_type"] == "free_debate" else "main")
        remaining_seconds = int((clock["remaining_ms"] if clock else time_limit * 1000) / 1000)
        next_phase = self._next_phase(phase)
        holder = "正方" if speaker["side"] == "affirmative" else "反方"
        return {
            # 结构化辩论格式（Agent 接口核心字段）
            "model_name": config.get("model_name", ""),
            "debater_name": speaker["name"],
            "debate_position": self._seat_label(speaker["seat"]),
            "debate_topic": match["topic"],
            "current_stage": phase["name"],
            "next_stage": next_phase["name"] if next_phase else "比赛结束",
            "holder": holder,
            "other_info": {
                "match_id": match["id"],
                "speaker_id": speaker["id"],
                "side": speaker["side"],
                "phase_type": phase["phase_type"],
                "remaining_seconds": remaining_seconds,
                "time_limit_seconds": time_limit,
            },
            "debate_history": self._build_debate_history(),
            # 内部路由字段
            "match_id": match["id"],
            "task_id": task_id,
            "speech_id": speech_id,
            "speaker_id": speaker["id"],
            "agent_config_id": speaker.get("agent_config_id"),
            "agent_provider_type": config.get("provider_type"),
            # 时控字段
            "time_limit_seconds": time_limit,
            "remaining_seconds": remaining_seconds,
            "target_chars": max(40, int(time_limit * 4.5)),
            "output": {"stream": True, "language": "zh-CN"},
        }

    async def _start_agent_playback(self, task_id: str, speaker: Dict[str, Any]) -> None:
        async with self._lock:
            phase_id = self.snapshot["match"]["current_phase_id"]
            self.snapshot["match"]["live_mode"] = "free" if phase_id == "phase_free_debate" else "single"
            if self.snapshot.get("current_speech"):
                self.snapshot["current_speech"]["started_at"] = iso_now()
            self._start_relevant_clocks(speaker["side"])
            self.snapshot["speech_service"]["tts"] = {
                "status": "playing",
                "latency_ms": self.snapshot["speech_service"]["tts"].get("latency_ms", 0) or 420,
                "queue_size": 1,
                "speaker_id": speaker["id"],
                "detail": "TTS playing",
            }
            self._persist_snapshot()
        await self.emit("tts.started", {"task_id": task_id, "speaker_id": speaker["id"]}, "system")
        await self.emit("speech.started", {"speaker_id": speaker["id"], "task_id": task_id}, "agent", speaker["id"])

    async def _synthesize_agent_tts(
        self,
        task_id: str,
        speech_id: str,
        speaker: Dict[str, Any],
        text: str,
    ) -> Optional[Dict[str, Any]]:
        content = str(text or "").strip()
        if not content or not self._tts_formal_enabled():
            return None

        request_id = f"tts_{task_id}"
        request_started_time = time.perf_counter()
        async with self._lock:
            current_speech = self.snapshot.get("current_speech") or {}
            phase_id = current_speech.get("phase_id") or self.snapshot["match"]["current_phase_id"]
            phase_key = self._phase_key_or_default(phase_id)
            match_id = self.snapshot["match"]["id"]
            self.repo.save_speech_service_request_started(
                match_id=match_id,
                request_id=request_id,
                service="tts",
                operation="agent_synthesis",
                speech_id=speech_id,
                speaker_id=speaker["id"],
                origin="live",
                **self._log_context(),
                request={
                    "task_id": task_id,
                    "speech_id": speech_id,
                    "speaker_id": speaker["id"],
                    "text": content,
                    "text_length": len(content),
                },
                started_at=iso_now(),
            )
            self.snapshot["speech_service"]["tts"] = {
                "status": "synthesizing",
                "latency_ms": self.snapshot["speech_service"]["tts"].get("latency_ms", 0),
                "queue_size": 1,
                "speaker_id": speaker["id"],
                "detail": "Xunfei TTS synthesizing official AI speech",
            }
            self._persist_snapshot()

        await self.emit(
            "tts.synthesis_started",
            {"task_id": task_id, "speech_id": speech_id, "speaker_id": speaker["id"], "text_length": len(content)},
            "system",
            speaker["id"],
        )

        gateway = XfyunTTSGateway(url=os.getenv("XFYUN_TTS_URL", "").strip())
        try:
            result = await gateway.synthesize(content)
        except XfyunGatewayError as exc:
            payload = {
                "task_id": task_id,
                "speech_id": speech_id,
                "speaker_id": speaker["id"],
                "reason": exc.message,
                "code": exc.code,
                "failed": True,
                "latency_ms": 0,
                "degraded_to": "text_only",
            }
            async with self._lock:
                self.snapshot["speech_service"]["tts"] = {
                    "status": "failed",
                    "latency_ms": self.snapshot["speech_service"]["tts"].get("latency_ms", 0),
                    "queue_size": 0,
                    "speaker_id": speaker["id"],
                    "detail": exc.message,
                    "degraded_to": "text_only",
                }
                self.repo.finish_speech_service_request(
                    match_id=self.snapshot["match"]["id"],
                    request_id=request_id,
                    status="failed",
                    response={"degraded_to": "text_only"},
                    error_code=str(exc.code) if exc.code is not None else "xfyun_tts_error",
                    error_message=exc.message,
                    latency_ms=max(0, int((time.perf_counter() - request_started_time) * 1000)),
                    completed_at=iso_now(),
                )
                self._persist_snapshot()
            await self.emit("tts.failed", payload, "system", speaker["id"])
            return payload

        archive_dir = self._audio_archive_dir(match_id, phase_key, speech_id)
        archive_dir.mkdir(parents=True, exist_ok=True)
        extension = self._audio_extension(result.mime_type)
        file_path = archive_dir / f"tts_{self._safe_path_part(task_id)}.{extension}"
        file_path.write_bytes(result.audio)

        async with self._lock:
            asset = self._upsert_audio_asset(
                speech_id=speech_id,
                speaker_id=speaker["id"],
                phase_id=phase_id,
                mime_type=result.mime_type,
                archive_dir=archive_dir,
                chunk_path=file_path,
                chunk_index=0,
                size_bytes=len(result.audio),
                duration_ms=None,
            )
            now = iso_now()
            asset.update(
                {
                    "status": "completed",
                    "completed_at": now,
                    "updated_at": now,
                    "source": "agent_tts",
                    "tts_task_id": task_id,
                    "text_length": len(content),
                }
            )
            payload = {
                "task_id": task_id,
                "speech_id": speech_id,
                "speaker_id": speaker["id"],
                "audio_asset_id": asset["id"],
                "mime_type": result.mime_type,
                "size_bytes": len(result.audio),
                "chunk_count": result.chunk_count,
                "latency_ms": result.latency_ms,
                "file_path": str(file_path),
            }
            self.repo.finish_speech_service_request(
                match_id=match_id,
                request_id=request_id,
                status="completed",
                response=payload,
                latency_ms=result.latency_ms,
                completed_at=iso_now(),
            )
            self._persist_snapshot()

        await self.emit("tts.audio_archived", payload, "system", speaker["id"])
        return payload

    def _tts_formal_enabled(self) -> bool:
        raw = os.getenv("PHDEBATE_TTS_FORMAL", "").strip().lower()
        if raw in {"0", "false", "no", "off"}:
            return False
        if raw in {"1", "true", "yes", "on"}:
            return True
        required = ["XFYUN_APP_ID", "XFYUN_API_KEY", "XFYUN_API_SECRET", "XFYUN_TTS_URL"]
        return all(os.getenv(name, "").strip() for name in required)

    def _tts_completion_detail(self, tts_result: Optional[Dict[str, Any]]) -> str:
        if not tts_result or tts_result.get("failed"):
            return ""
        return f"TTS archived · {tts_result.get('size_bytes', 0)} bytes · {tts_result.get('file_path', '')}"

    async def _fail_agent_task(self, task_id: str, speaker_id: str, exc: AgentGatewayError) -> None:
        async with self._lock:
            self._set_agent_status(speaker_id, "failed", exc.message)
            self.snapshot["speech_service"]["tts"] = {
                "status": "failed",
                "latency_ms": self.snapshot["speech_service"]["tts"].get("latency_ms", 0),
                "queue_size": 0,
                "speaker_id": speaker_id,
                "detail": exc.message,
                "degraded_to": "manual_input",
            }
            self._persist_snapshot()
        await self.emit(
            "agent.failed",
            {"task_id": task_id, "speaker_id": speaker_id, "code": exc.code, "message": exc.message, "details": exc.details},
            "agent",
            speaker_id,
        )

    def _set_agent_status(self, speaker_id: str, status: str, detail: str) -> None:
        for item in self.snapshot.get("agent_status", []):
            if item["speaker_id"] == speaker_id:
                item["status"] = status
                item["detail"] = detail
                item["last_heartbeat_seconds"] = 0
                return

    def _update_agent_health_status(self, speaker_id: str, status: str, detail: str, payload: Dict[str, Any]) -> None:
        speaker = self._find_speaker(speaker_id)
        for item in self.snapshot.get("agent_status", []):
            if item["speaker_id"] == speaker_id:
                item.update(
                    {
                        "name": speaker["name"],
                        "agent_config_id": speaker.get("agent_config_id"),
                        "model": payload.get("model") or speaker.get("model_name") or item.get("model", "unknown"),
                        "status": status,
                        "detail": detail,
                        "last_heartbeat_seconds": 0,
                        "endpoint": payload.get("endpoint"),
                        "latency_ms": payload.get("latency_ms", 0),
                        "last_health_at": payload.get("checked_at"),
                        "version": payload.get("version"),
                    }
                )
                return
        self.snapshot.setdefault("agent_status", []).append(
            {
                "speaker_id": speaker_id,
                "agent_config_id": speaker.get("agent_config_id"),
                "name": speaker["name"],
                "model": payload.get("model") or speaker.get("model_name") or "unknown",
                "status": status,
                "last_heartbeat_seconds": 0,
                "detail": detail,
                "endpoint": payload.get("endpoint"),
                "latency_ms": payload.get("latency_ms", 0),
                "last_health_at": payload.get("checked_at"),
                "version": payload.get("version"),
            }
        )

    def _log_context(self) -> Dict[str, Any]:
        """Current 请求时机 (phase + scene) for log classification."""
        match = self.snapshot.get("match", {})
        phase_id = match.get("current_phase_id")
        phase_name = None
        if phase_id:
            phase = next((p for p in self.snapshot.get("phases", []) if p.get("id") == phase_id), None)
            if phase:
                phase_name = phase.get("name")
        return {
            "phase_id": phase_id,
            "phase_name": phase_name,
            "screen_scene": match.get("screen_scene"),
        }

    def _save_audit_for_event(self, event: Dict[str, Any]) -> None:
        if event["actor_type"] not in {"admin", "host"}:
            return
        self.repo.save_audit_log(
            audit_id=f"audit_{event['id']}",
            match_id=event["match_id"],
            actor_type=event["actor_type"],
            actor_id=event.get("actor_id"),
            action=event["type"],
            target_type=None,
            target_id=None,
            request=event["payload"],
            result="success",
            error_message=None,
            created_at=event["created_at"],
            origin="live",
            **self._log_context(),
        )

    def _pause_running_clocks(self) -> None:
        self._refresh_clocks()
        for clock in self.snapshot["clocks"]:
            if clock["state"] == "running":
                clock["state"] = "paused"
                clock["deadline_at"] = None

    def _resume_paused_clocks(self) -> None:
        now = utc_now()
        for clock in self.snapshot["clocks"]:
            if clock["state"] == "paused" and clock["remaining_ms"] > 0:
                clock["state"] = "running"
                clock["deadline_at"] = to_iso(now + timedelta(milliseconds=clock["remaining_ms"]))

    def _ensure_match_allows_control(self, command: str) -> None:
        status = self.snapshot["match"]["status"]
        if status != "running":
            raise MatchStateError(
                "invalid_state",
                "比赛不在进行中，不能执行该操作。",
                {"command": command, "status": status},
            )

    def _ensure_vote_controls_available(self, command: str) -> None:
        status = self.snapshot["match"]["status"]
        if status in {"paused", "intervention", "finished", "archived"}:
            message = (
                "比赛已结束，投票功能已关闭。"
                if status in {"finished", "archived"}
                else "比赛暂停或应急处理中，投票功能暂不可用，请继续比赛后再操作。"
            )
            raise MatchStateError(
                "vote_unavailable",
                message,
                {"command": command, "status": status},
            )

    def _current_phase(self) -> Dict[str, Any]:
        return self._find_phase(self.snapshot["match"]["current_phase_id"])

    def _current_turn_index(self) -> int:
        return int(self.snapshot.get("free_debate", {}).get("turn_index", 0))

    def _ensure_speaker_allowed_for_current_phase(self, speaker: Dict[str, Any]) -> None:
        phase = self._current_phase()
        if phase["phase_type"] == "free_debate":
            expected_side = self.snapshot["free_debate"]["current_turn_side"]
            if speaker["side"] != expected_side:
                raise MatchStateError(
                    "invalid_speaker",
                    f"当前自由辩论轮到{self._side_name(expected_side)}发言。",
                    {"expected_side": expected_side, "speaker_side": speaker["side"]},
                )
            total_clock = self._clock(f"{speaker['side']}_total")
            if total_clock and total_clock["remaining_ms"] <= 0:
                raise MatchStateError(
                    "clock_expired",
                    f"{self._side_name(speaker['side'])}自由辩论总时间已用尽。",
                    {"side": speaker["side"]},
                )
            return

        if phase["side"] != speaker["side"] or phase["speaker_seat"] != speaker["seat"]:
            raise MatchStateError(
                "invalid_speaker",
                "当前环节只允许指定辩位发言。",
                {
                    "phase_id": phase["id"],
                    "expected_side": phase["side"],
                    "expected_seat": phase["speaker_seat"],
                    "speaker_id": speaker["id"],
                },
            )

    def _active_speech_for_speaker(self, speaker_id: str, command: str) -> Dict[str, Any]:
        self._find_speaker(speaker_id)
        speech = self.snapshot.get("current_speech") or {}
        if not speech:
            raise MatchStateError(
                "no_active_speech",
                "当前没有正在进行的发言。",
                {"command": command, "speaker_id": speaker_id},
            )
        if speech.get("speaker_id") != speaker_id:
            raise MatchStateError(
                "invalid_speaker",
                "只能更新当前发言人的语音状态。",
                {"command": command, "active_speaker_id": speech.get("speaker_id"), "speaker_id": speaker_id},
            )
        return speech

    def _audio_speech_context(self, speech_id: str, speaker_id: str) -> tuple[str, str]:
        active = self.snapshot.get("current_speech") or {}
        if active.get("id") == speech_id:
            if active.get("speaker_id") != speaker_id:
                raise MatchStateError(
                    "invalid_speaker",
                    "音频分片只能写入当前发言人。",
                    {"active_speaker_id": active.get("speaker_id"), "speaker_id": speaker_id},
                )
            phase_id = active.get("phase_id", self.snapshot["match"]["current_phase_id"])
            return phase_id, self._phase_key_or_default(phase_id)

        for segment in self.snapshot.get("recent_transcript", []):
            if segment.get("speech_id") == speech_id or segment.get("id") == speech_id:
                if segment.get("speaker_id") != speaker_id:
                    raise MatchStateError(
                        "invalid_speaker",
                        "音频分片只能写入该发言对应的辩手。",
                        {"segment_speaker_id": segment.get("speaker_id"), "speaker_id": speaker_id},
                    )
                phase_id = segment.get("phase_id") or self.snapshot["match"]["current_phase_id"]
                return phase_id, self._phase_key_or_default(phase_id)

        asset = self._audio_asset_for_speech(speech_id)
        if asset:
            if asset.get("speaker_id") != speaker_id:
                raise MatchStateError(
                    "invalid_speaker",
                    "音频分片只能追加到该发言对应的辩手。",
                    {"asset_speaker_id": asset.get("speaker_id"), "speaker_id": speaker_id},
                )
            phase_id = asset.get("phase_id") or self.snapshot["match"]["current_phase_id"]
            return phase_id, self._phase_key_or_default(phase_id)

        raise MatchStateError("speech_not_found", "未找到要归档的发言。", {"speech_id": speech_id})

    def _phase_key_or_default(self, phase_id: str) -> str:
        try:
            return self._find_phase(phase_id)["phase_key"]
        except KeyError:
            return self._safe_path_part(phase_id or "unknown_phase")

    def _audio_archive_dir(self, match_id: str, phase_key: str, speech_id: str) -> Path:
        return (
            self.audio_root_path()
            / self._safe_path_part(match_id)
            / self._safe_path_part(phase_key)
            / self._safe_path_part(speech_id)
        )

    def audio_root_path(self) -> Path:
        raw = os.getenv("PHDEBATE_AUDIO_DIR", "").strip()
        if raw:
            path = Path(raw)
            return path if path.is_absolute() else project_root() / path
        return self.repo.db_path.parent / "audio"

    def image_root_path(self) -> Path:
        raw = os.getenv("PHDEBATE_IMAGE_DIR", "").strip()
        if raw:
            path = Path(raw)
            return path if path.is_absolute() else project_root() / path
        return self.repo.db_path.parent / "images"

    def _image_extension(self, mime_type: str) -> str:
        value = (mime_type or "").lower()
        if "png" in value:
            return "png"
        if "jpeg" in value or "jpg" in value:
            return "jpg"
        if "webp" in value:
            return "webp"
        if "gif" in value:
            return "gif"
        if "svg" in value:
            return "svg"
        return "png"

    async def save_speaker_image(self, speaker_id: str, content: bytes, mime_type: str) -> Dict[str, Any]:
        # Validate the speaker exists before touching disk.
        self._find_speaker(speaker_id)
        ext = self._image_extension(mime_type)
        root = self.image_root_path() / "speakers"
        root.mkdir(parents=True, exist_ok=True)
        filename = f"{speaker_id}.{ext}"
        for old in root.glob(f"{speaker_id}.*"):
            if old.name != filename:
                try:
                    old.unlink()
                except OSError:
                    pass
        (root / filename).write_bytes(content)
        # Cache-busting version so re-uploads refresh on the big screen immediately.
        url = f"/api/files/speaker-images/{filename}?v={self.seq + 1}"
        return await self.update_speaker(speaker_id, {"image_url": url})

    def _audio_extension(self, mime_type: str) -> str:
        value = (mime_type or "").lower()
        if "l16" in value or "pcm" in value or "audio/raw" in value or value == "application/octet-stream":
            return "pcm"
        if "webm" in value:
            return "webm"
        if "ogg" in value:
            return "ogg"
        if "wav" in value:
            return "wav"
        if "mpeg" in value or "mp3" in value:
            return "mp3"
        return "bin"

    def _asr_supported_audio_mime(self, mime_type: str) -> bool:
        value = (mime_type or "").lower()
        return "l16" in value or "pcm" in value or value in {"audio/raw", "application/octet-stream"}

    def _asr_auto_recognize_enabled(self) -> bool:
        raw = os.getenv("PHDEBATE_ASR_AUTO_RECOGNIZE", "").strip().lower()
        if raw in {"0", "false", "no", "off"}:
            return False
        if raw in {"1", "true", "yes", "on"}:
            return True
        required = ["XFYUN_APP_ID", "XFYUN_API_KEY", "XFYUN_API_SECRET", "XFYUN_ASR_URL"]
        return all(os.getenv(name, "").strip() for name in required)

    def _asr_realtime_enabled(self) -> bool:
        raw = os.getenv("PHDEBATE_ASR_REALTIME", "").strip().lower()
        if raw in {"0", "false", "no", "off"}:
            return False
        if raw in {"1", "true", "yes", "on"}:
            return True
        required = ["XFYUN_APP_ID", "XFYUN_API_KEY", "XFYUN_API_SECRET", "XFYUN_ASR_URL"]
        return all(os.getenv(name, "").strip() for name in required)

    def _safe_path_part(self, value: str) -> str:
        cleaned = "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in str(value))
        return cleaned[:96] or "unknown"

    def _audio_asset_for_speech(self, speech_id: str) -> Optional[Dict[str, Any]]:
        for asset in self.snapshot.setdefault("audio_assets", []):
            if asset.get("speech_id") == speech_id:
                return asset
        return None

    def _upsert_audio_asset(
        self,
        *,
        speech_id: str,
        speaker_id: str,
        phase_id: str,
        mime_type: str,
        archive_dir: Path,
        chunk_path: Path,
        chunk_index: int,
        size_bytes: int,
        duration_ms: Optional[int],
    ) -> Dict[str, Any]:
        now = iso_now()
        asset = self._audio_asset_for_speech(speech_id)
        if not asset:
            asset = {
                "id": f"audio_{speech_id}",
                "match_id": self.snapshot["match"]["id"],
                "phase_id": phase_id,
                "speech_id": speech_id,
                "speaker_id": speaker_id,
                "file_path": str(archive_dir),
                "mime_type": mime_type,
                "duration_ms": None,
                "size_bytes": 0,
                "chunk_count": 0,
                "status": "recording",
                "chunks": [],
                "created_at": now,
                "updated_at": now,
            }
            self.snapshot.setdefault("audio_assets", []).insert(0, asset)

        chunks = [chunk for chunk in asset.setdefault("chunks", []) if int(chunk.get("chunk_index", -1)) != chunk_index]
        chunks.append(
            {
                "chunk_index": chunk_index,
                "file_path": str(chunk_path),
                "size_bytes": size_bytes,
                "mime_type": mime_type,
                "duration_ms": duration_ms,
                "created_at": now,
            }
        )
        chunks.sort(key=lambda item: int(item["chunk_index"]))
        total_duration = sum(int(chunk.get("duration_ms") or 0) for chunk in chunks)

        asset.update(
            {
                "phase_id": phase_id,
                "speaker_id": speaker_id,
                "file_path": str(archive_dir),
                "mime_type": mime_type,
                "duration_ms": total_duration if total_duration > 0 else None,
                "size_bytes": sum(int(chunk.get("size_bytes") or 0) for chunk in chunks),
                "chunk_count": len(chunks),
                "status": "recording",
                "chunks": chunks,
                "updated_at": now,
            }
        )
        return asset

    def _speech_revision_text(self, active: Optional[Dict[str, Any]], segments: List[Dict[str, Any]]) -> str:
        if active:
            return active.get("content_final") or active.get("content_partial") or ""
        if segments:
            return segments[0].get("text", "")
        return ""

    def _upsert_transcript_segment(
        self,
        speech: Dict[str, Any],
        speaker_id: str,
        text: str,
        is_final: bool,
        source: str,
    ) -> Dict[str, Any]:
        speech_id = speech.get("id") or f"speech_{self.seq + 1}"
        existing = None
        next_segments = []
        for segment in self.snapshot["recent_transcript"]:
            if segment.get("speech_id") == speech_id:
                existing = segment
            else:
                next_segments.append(segment)

        segment = existing or {
            "id": f"seg_{speech_id}",
            "speech_id": speech_id,
            "phase_id": speech.get("phase_id", self.snapshot["match"]["current_phase_id"]),
            "speaker_id": speaker_id,
            "speaker_label": self.speaker_label(speaker_id),
            "source": source,
            "is_final": is_final,
            "turn_index": speech.get("turn_index", self._current_turn_index()),
            "valid": True,
            "invalid_reason": None,
            "text": text,
            "created_at": iso_now(),
        }
        segment.update(
            {
                "speech_id": speech_id,
                "phase_id": speech.get("phase_id", segment.get("phase_id", self.snapshot["match"]["current_phase_id"])),
                "speaker_id": speaker_id,
                "speaker_label": self.speaker_label(speaker_id),
                "source": source,
                "is_final": is_final,
                "turn_index": speech.get("turn_index", segment.get("turn_index")),
                "valid": segment.get("valid", True),
                "invalid_reason": segment.get("invalid_reason"),
                "text": text,
                "updated_at": iso_now(),
            }
        )
        self.snapshot["recent_transcript"] = [segment, *next_segments][:12]
        return segment

    def _apply_archived_asr_text(self, speech_id: str, text: str) -> None:
        active = self.snapshot.get("current_speech")
        if active and active.get("id") == speech_id:
            active["content_partial"] = text
            active["content_final"] = text
            self._upsert_transcript_segment(active, active["speaker_id"], text, True, "human_asr")
            return

        for segment in self.snapshot.setdefault("recent_transcript", []):
            if segment.get("speech_id") == speech_id or segment.get("id") == speech_id:
                before_text = str(segment.get("text") or "")
                segment["text"] = text
                segment["is_final"] = True
                segment["source"] = "human_asr"
                segment["updated_at"] = iso_now()
                self.snapshot.setdefault("speech_revisions", []).insert(
                    0,
                    {
                        "id": f"rev_{self.seq + 1}_{len(self.snapshot.get('speech_revisions', [])) + 1}",
                        "speech_id": speech_id,
                        "before_text": before_text,
                        "after_text": text,
                        "valid": bool(segment.get("valid", True)),
                        "reason": "archive_asr_recognition",
                        "created_at": iso_now(),
                        "editor_actor_id": "asr",
                    },
                )
                self.snapshot["speech_revisions"] = self.snapshot["speech_revisions"][:50]
                return

        asset = self._audio_asset_for_speech(speech_id) or {}
        speaker_id = asset.get("speaker_id", "unknown")
        phase_id = asset.get("phase_id", self.snapshot["match"]["current_phase_id"])
        try:
            side = self._find_speaker(speaker_id)["side"]
        except KeyError:
            side = "neutral"
        speech = {
            "id": speech_id,
            "phase_id": phase_id,
            "speaker_id": speaker_id,
            "side": side,
            "turn_index": self._current_turn_index(),
        }
        self._upsert_transcript_segment(speech, speaker_id, text, True, "human_asr")

    def _advance_free_debate_turn_if_needed(self, side: str) -> None:
        phase = self._current_phase()
        if phase["phase_type"] != "free_debate":
            return
        old_turn_index = int(self.snapshot["free_debate"]["turn_index"])
        old_turn_key = f"{side}_{old_turn_index}"
        next_side = "negative" if side == "affirmative" else "affirmative"
        new_turn_index = old_turn_index + 1
        self.snapshot["free_debate"]["current_turn_side"] = next_side
        self.snapshot["free_debate"]["turn_index"] = new_turn_index
        # Clear skip votes / auto-handled marker for the completed turn
        skip_votes = self.snapshot["free_debate"].get("skip_votes", {})
        skip_votes.pop(old_turn_key, None)
        auto_handled = self.snapshot["free_debate"].get("auto_handled", {})
        auto_handled.pop(old_turn_key, None)
        turn_clock = self._clock("turn")
        if turn_clock:
            turn_clock["remaining_ms"] = turn_clock["total_seconds"] * 1000
            turn_clock["state"] = "paused"
            turn_clock["deadline_at"] = None
            turn_clock.pop("expired_notified_at", None)
        # 需求 2.md：新一轮开始后给本方 5s 决定窗口，超时则随机 AI 接管。
        self._arm_free_debate_auto_agent(next_side, new_turn_index)

    def _arm_free_debate_auto_agent(self, side: str, turn_index: int) -> None:
        """Schedule a background task: if the side has not started speaking within the
        decision window (and has not all-skipped), a random agent on that side answers."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        asyncio.create_task(self._free_debate_auto_agent_after_delay(side, turn_index))

    async def _free_debate_auto_agent_after_delay(self, side: str, turn_index: int) -> None:
        import random

        await asyncio.sleep(self._free_debate_decision_seconds())
        async with self._lock:
            match = self.snapshot["match"]
            if match.get("status") != "running":
                return
            phase = self._current_phase()
            if phase.get("phase_type") != "free_debate":
                return
            fd = self.snapshot["free_debate"]
            if fd.get("current_turn_side") != side or int(fd.get("turn_index", -1)) != turn_index:
                return
            if self.snapshot.get("current_speech"):
                return
            turn_key = f"{side}_{turn_index}"
            if fd.setdefault("auto_handled", {}).get(turn_key):
                return
            total_clock = self._clock(f"{side}_total")
            if total_clock and int(total_clock.get("remaining_ms", 0)) <= 0:
                return
            agents_on_side = [s for s in self.snapshot["speakers"] if s["side"] == side and s["speaker_type"] == "agent"]
            if not agents_on_side:
                return
            chosen = random.choice(agents_on_side)
            fd["auto_handled"][turn_key] = chosen["id"]
            self._persist_snapshot()
        await self.emit(
            "free_debate.auto_agent",
            {"side": side, "turn_index": turn_index, "speaker_id": chosen["id"], "reason": "decision_timeout"},
            "system",
        )
        try:
            self.ensure_agent_speaker_for_current_phase(chosen["id"])
            await self.run_agent_speech(chosen["id"])
        except Exception:
            pass

    def _clear_flow_state(self) -> None:
        self.snapshot["flow"] = self._fresh_flow_state()

    def _set_flow_waiting_for_timeout(
        self,
        phase: Dict[str, Any],
        expired_clock_names: List[str],
        speech_id: Optional[str],
        speaker_id: Optional[str],
        now: datetime,
    ) -> Dict[str, Any]:
        total_expired = any(name in {"affirmative_total", "negative_total", "main"} for name in expired_clock_names)
        if phase.get("phase_type") == "free_debate" and not total_expired:
            next_action = "free_turn_next"
            message = "单次发言时间到，等待主持确认下一轮。"
        elif self._next_phase(phase):
            next_action = "phase_next"
            message = "本环节时间到，等待主持确认进入下一环节。"
        else:
            next_action = "judge_commentary"
            message = "全部发言时间到，等待主持进入评委点评。"

        flow = self._fresh_flow_state()
        flow.update(
            {
                "awaiting_host_confirm": True,
                "reason": "clock_expired",
                "message": message,
                "next_action": next_action,
                "phase_id": phase.get("id"),
                "speech_id": speech_id,
                "speaker_id": speaker_id,
                "expired_clocks": expired_clock_names,
                "created_at": to_iso(now),
            }
        )
        self.snapshot["flow"] = flow
        return deepcopy(flow)

    def _invalidate_transcripts_from_order(self, target_order: int, reason: str) -> List[str]:
        invalidated: List[str] = []
        for segment in self.snapshot.get("recent_transcript", []):
            if segment.get("valid") is False:
                continue
            phase_id = segment.get("phase_id")
            if not phase_id:
                continue
            try:
                phase_order = self._find_phase(phase_id)["display_order"]
            except KeyError:
                continue
            if phase_order >= target_order:
                segment["valid"] = False
                segment["invalid_reason"] = reason
                invalidated.append(segment["id"])
        return invalidated

    def _validate_vote_body(self, body: Dict[str, Any], audience: bool = False) -> None:
        if audience:
            if "winner_side" not in body or "best_speaker_id" not in body:
                raise MatchStateError("invalid_vote", "学生投票必须包含优胜方和最佳辩手。")
        winner_side = body.get("winner_side")
        if winner_side is not None:
            self._validate_side(winner_side)
        best_speaker_id = body.get("best_speaker_id")
        if best_speaker_id is not None:
            try:
                self._find_speaker(best_speaker_id)
            except KeyError:
                raise MatchStateError("invalid_vote", "投票中的最佳辩手无效。", {"best_speaker_id": best_speaker_id})
        for item in body.get("items", []):
            vote_type = item.get("vote_type")
            if vote_type in {"constructive", "process", "conclusion", "winner"}:
                self._validate_side(item.get("target_side"))
            elif vote_type == "best_speaker":
                try:
                    self._find_speaker(item.get("target_speaker_id"))
                except KeyError:
                    raise MatchStateError("invalid_vote", "评委票中的最佳辩手无效。", {"target_speaker_id": item.get("target_speaker_id")})
            else:
                raise MatchStateError("invalid_vote", "未知的投票类型。", {"vote_type": vote_type})

    def _validate_side(self, side: Any) -> None:
        if side not in {"affirmative", "negative"}:
            raise MatchStateError("invalid_vote", "投票中的优胜方无效。", {"side": side})

    def _normalize_screen_scene(self, scene: str) -> str:
        # Legacy aliases kept for backward compatibility; `opening` and `teams`
        # are now first-class scenes (辩题介绍 / 阵容介绍).
        aliases = {
            "intermission": "judge_commentary",
            "result": "judge_result",
            "thanks": "acknowledgment",
        }
        normalized = aliases.get(scene, scene)
        allowed = {
            "idle", "opening", "teams", "live", "paused",
            "xiaoqi_commentary", "xiaoqi_result",
            "judge_commentary", "judge_result", "audience_result",
            "acknowledgment",
        }
        if normalized not in allowed:
            raise MatchStateError("invalid_screen_scene", "未知的大屏场景。", {"scene": scene})
        return normalized

    def _start_relevant_clocks(self, side: str) -> None:
        now = utc_now()
        phase_id = self.snapshot["match"]["current_phase_id"]
        for clock in self.snapshot["clocks"]:
            should_run = clock["name"] == "main"
            if phase_id == "phase_free_debate":
                should_run = clock["name"] == "turn" or clock["name"] == f"{side}_total"
            if should_run and clock["remaining_ms"] > 0:
                clock["state"] = "running"
                clock["deadline_at"] = to_iso(now + timedelta(milliseconds=clock["remaining_ms"]))
                clock.pop("expired_notified_at", None)
            elif clock["state"] == "running":
                clock["state"] = "paused"
                clock["deadline_at"] = None

    def _reset_relevant_clocks_for_speech(self, speech: Dict[str, Any]) -> None:
        self._refresh_clocks()
        side = speech.get("side")
        phase_id = speech.get("phase_id") or self.snapshot["match"]["current_phase_id"]
        started_remaining = speech.get("started_clock_remaining_ms") or {}
        for clock in self.snapshot["clocks"]:
            if clock.get("phase_id") != phase_id:
                continue
            relevant = clock["name"] == "main"
            if phase_id == "phase_free_debate":
                relevant = clock["name"] in {"turn", f"{side}_total"}
            if not relevant:
                continue
            fallback = clock["total_seconds"] * 1000 if clock["name"] in {"main", "turn"} else int(clock.get("remaining_ms", 0))
            clock["remaining_ms"] = int(started_remaining.get(clock["name"], fallback))
            clock["state"] = "paused"
            clock["deadline_at"] = None
            clock.pop("expired_notified_at", None)

    def _clock(self, name: str) -> Optional[Dict[str, Any]]:
        for clock in self.snapshot["clocks"]:
            if clock["name"] == name:
                return clock
        return None

    def _clock_or_error(self, name: str) -> Dict[str, Any]:
        clock = self._clock(name)
        if not clock:
            raise MatchStateError("clock_not_found", "未找到指定时钟。", {"clock_name": name})
        return clock

    def _clock_payload(self, clock: Dict[str, Any], reason: str) -> Dict[str, Any]:
        return {
            "clock_name": clock["name"],
            "phase_id": clock["phase_id"],
            "state": clock["state"],
            "remaining_ms": clock["remaining_ms"],
            "deadline_at": clock.get("deadline_at"),
            "reason": reason,
        }

    def _reset_clocks_for_phase(self, phase: Dict[str, Any]) -> None:
        if phase["phase_type"] == "free_debate":
            side_total = self._free_side_total_seconds(phase)
            turn_seconds = self._free_turn_seconds(phase)
            self.snapshot["clocks"] = [
                {"id": "clock_aff_total", "phase_id": phase["id"], "name": "affirmative_total", "total_seconds": side_total, "remaining_ms": side_total * 1000, "state": "paused", "deadline_at": None},
                {"id": "clock_turn", "phase_id": phase["id"], "name": "turn", "total_seconds": turn_seconds, "remaining_ms": turn_seconds * 1000, "state": "paused", "deadline_at": None},
                {"id": "clock_neg_total", "phase_id": phase["id"], "name": "negative_total", "total_seconds": side_total, "remaining_ms": side_total * 1000, "state": "paused", "deadline_at": None},
            ]
        else:
            self.snapshot["clocks"] = [
                {"id": "clock_main", "phase_id": phase["id"], "name": "main", "total_seconds": phase["duration_seconds"], "remaining_ms": phase["duration_seconds"] * 1000, "state": "paused", "deadline_at": None}
            ]

    def _sync_current_phase_clocks_after_config(self, phase: Dict[str, Any]) -> None:
        self._refresh_clocks()
        if phase["phase_type"] == "free_debate":
            side_total = self._free_side_total_seconds(phase)
            turn_seconds = self._free_turn_seconds(phase)
            targets = {
                "affirmative_total": side_total,
                "negative_total": side_total,
                "turn": turn_seconds,
            }
        else:
            targets = {"main": int(phase["duration_seconds"])}

        now = utc_now()
        for clock in self.snapshot["clocks"]:
            if clock.get("phase_id") != phase["id"] or clock["name"] not in targets:
                continue
            total_seconds = targets[clock["name"]]
            clock["total_seconds"] = total_seconds
            clock["remaining_ms"] = min(int(clock.get("remaining_ms", 0)), total_seconds * 1000)
            if clock["remaining_ms"] <= 0:
                clock["state"] = "expired"
                clock["deadline_at"] = None
            elif clock["state"] == "running":
                clock["deadline_at"] = to_iso(now + timedelta(milliseconds=clock["remaining_ms"]))

    def _free_side_total_seconds(self, phase: Dict[str, Any]) -> int:
        return int(phase.get("side_total_seconds") or max(1, int(phase["duration_seconds"]) // 2))

    def _free_turn_seconds(self, phase: Dict[str, Any]) -> int:
        # 需求 2.md：自由辩论单次发言不超过 30s。
        return int(phase.get("turn_seconds") or 30)

    def _free_debate_decision_seconds(self, phase: Optional[Dict[str, Any]] = None) -> float:
        # 需求 2.md：人类辩手在本轮有 5s 决定窗口；超时（或全部跳过）则随机 AI 接管。
        # 可用 PHDEBATE_FREE_DEBATE_DECISION_SECONDS 覆盖（测试/现场调参）。
        env_value = os.getenv("PHDEBATE_FREE_DEBATE_DECISION_SECONDS", "").strip()
        if env_value:
            try:
                return max(0.0, float(env_value))
            except ValueError:
                pass
        phase = phase or self._current_phase()
        try:
            return max(0.0, float(phase.get("decision_seconds") or 5))
        except (TypeError, ValueError):
            return 5.0

    def _validated_seconds(self, value: Any, field: str, minimum: int, maximum: int) -> int:
        try:
            seconds = int(value)
        except (TypeError, ValueError) as exc:
            raise MatchStateError("invalid_phase_config", "时长必须为整数秒。", {"field": field, "value": value}) from exc
        if seconds < minimum or seconds > maximum:
            raise MatchStateError(
                "invalid_phase_config",
                f"时长必须在 {minimum} 到 {maximum} 秒之间。",
                {"field": field, "value": seconds, "minimum": minimum, "maximum": maximum},
            )
        return seconds

    def _find_phase(self, phase_id: str) -> Dict[str, Any]:
        for phase in self.snapshot["phases"]:
            if phase["id"] == phase_id:
                return phase
        raise KeyError(f"Unknown phase {phase_id}")

    def _next_phase(self, phase: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        order = phase["display_order"]
        for item in self.snapshot["phases"]:
            if item["display_order"] == order + 1:
                return item
        return None

    def _find_speaker(self, speaker_id: str) -> Dict[str, Any]:
        for speaker in self.snapshot["speakers"]:
            if speaker["id"] == speaker_id:
                return speaker
        raise KeyError(f"Unknown speaker {speaker_id}")

    def _find_team(self, team_id: str) -> Dict[str, Any]:
        for team in self.snapshot["teams"]:
            if team["id"] == team_id:
                return team
        raise KeyError(f"Unknown team {team_id}")

    def _sync_agent_status_for_speaker(self, speaker: Dict[str, Any]) -> None:
        if speaker.get("speaker_type") != "agent":
            self.snapshot["agent_status"] = [
                item for item in self.snapshot.get("agent_status", []) if item.get("speaker_id") != speaker["id"]
            ]
            return
        for item in self.snapshot.get("agent_status", []):
            if item.get("speaker_id") == speaker["id"]:
                item["name"] = speaker["name"]
                item["agent_config_id"] = speaker.get("agent_config_id")
                item["model"] = speaker.get("model_name") or "未配置模型"
                item["endpoint"] = speaker.get("agent_endpoint")
                return
        self.snapshot.setdefault("agent_status", []).append(
            {
                "speaker_id": speaker["id"],
                "agent_config_id": speaker.get("agent_config_id"),
                "name": speaker["name"],
                "model": speaker.get("model_name") or "未配置模型",
                "status": "ready",
                "last_heartbeat_seconds": None,
                "detail": "等待联调",
                "endpoint": speaker.get("agent_endpoint"),
            }
        )

    def _agent_endpoint_for_speaker(self, speaker_id: str) -> str:
        speaker_key = speaker_id.upper().replace("-", "_")
        return (
            os.getenv(f"PHDEBATE_AGENT_ENDPOINT_{speaker_key}", "").strip()
            or os.getenv("PHDEBATE_AGENT_BASE_URL", "").strip()
        )

    def _side_name(self, side: str) -> str:
        return "正方" if side == "affirmative" else "反方" if side == "negative" else "中立"

    def _seat_label(self, seat: int) -> str:
        return ["", "一辩", "二辩", "三辩", "四辩"][seat]

    def speaker_label(self, speaker_id: str) -> str:
        speaker = self._find_speaker(speaker_id)
        side = "正方" if speaker["side"] == "affirmative" else "反方"
        seat = self._seat_label(speaker["seat"])
        return f"{side}{seat} · {speaker['name']}"

    def _mock_agent_chunks(self, speaker: Dict[str, Any]) -> List[str]:
        if speaker["side"] == "affirmative":
            return [
                "提问当然重要，",
                "但真正让 AI 可靠工作的，",
                "是把问题拆成可执行、可验证、可复盘的步骤。"
            ]
        return [
            "对方把编程思维说成万能钥匙，",
            "却忽略了 AI 时代最稀缺的能力，",
            "是提出正确问题并不断校准目标。"
        ]

    def _mock_agent_command_chunks(self, speaker: Dict[str, Any], command: str) -> List[str]:
        if command == "self_intro":
            return [
                f"大家好，我是{speaker.get('name', 'AI 辩手')}，",
                "我会在接下来的辩论中尽量给出清晰、克制且可检验的论证。"
            ]
        if command == "fallback":
            return [
                "当前我给出一段替代回应：",
                "请先抓住对方论证中的关键前提，再回到本方判准进行回应。"
            ]
        return [
            "命令已收到，",
            "我将根据当前辩题和已有发言给出回应。"
        ]


STRUCTURED_CSV_FIELDS = {
    "phases": [
        "match_id",
        "id",
        "phase_key",
        "name",
        "phase_type",
        "display_order",
        "side",
        "speaker_seat",
        "duration_seconds",
        "side_total_seconds",
        "turn_seconds",
        "speaker_selector",
        "status",
        "updated_at",
    ],
    "speeches": [
        "match_id",
        "speech_id",
        "phase_id",
        "speaker_id",
        "side",
        "turn_index",
        "source",
        "state",
        "content_final",
        "content_partial",
        "started_at",
        "paused_at",
        "ended_at",
        "updated_at",
    ],
    "transcript_segments": [
        "match_id",
        "id",
        "speech_id",
        "phase_id",
        "speaker_id",
        "speaker_label",
        "source",
        "is_final",
        "turn_index",
        "valid",
        "invalid_reason",
        "text",
        "created_at",
        "updated_at",
    ],
}


store = MatchStore()
