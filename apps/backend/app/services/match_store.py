from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import zipfile
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from fastapi import WebSocket, WebSocketDisconnect

from app.services.agent_gateway import AgentGateway, AgentGatewayError
from app.services.sqlite_repo import SQLiteRepository, project_root
from app.services.xfyun_gateway import XfyunASRGateway, XfyunGatewayError, XfyunTTSGateway


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
                "screen_scene": "live",
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
            "mic_permission": "unknown" if speaker_type == "human" else None,
            "device_label": None,
            "last_seen_at": None,
        }
        if speaker_type == "agent":
            speaker["agent_endpoint"] = self._agent_endpoint_for_speaker(speaker_id)
        return speaker

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

    async def get_snapshot(self) -> Dict[str, Any]:
        async with self._lock:
            self._refresh_clocks()
            snap = deepcopy(self.snapshot)
            snap["last_seq"] = self.seq
            self._sanitize_snapshot(snap)
            return snap

    async def get_audit_logs(self, limit: int = 30) -> List[Dict[str, Any]]:
        async with self._lock:
            match_id = self.snapshot["match"]["id"]
        return self.repo.load_audit_logs(match_id, limit)

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
            self._zip_writestr(bundle, "events.jsonl", self._jsonl(events), entries, text=True)
            self._zip_writestr(bundle, "votes.json", votes, entries)
            self._zip_writestr(bundle, "audit_logs.jsonl", self._jsonl(audit_logs), entries, text=True)
            self._zip_writestr(bundle, "audio_manifest.json", audio_assets, entries)
            self._zip_audio_assets(bundle, audio_assets, entries)

        payload = {
            "export_id": export_id,
            "match_id": match_id,
            "file_path": str(zip_path),
            "download_url": f"/api/matches/{match_id}/exports/{export_id}/download",
            "size_bytes": zip_path.stat().st_size,
            "entries": entries,
            "created_at": iso_now(),
        }
        await self.emit("export.created", payload, "admin")
        return payload

    async def export_file_path(self, export_id: str) -> Path:
        async with self._lock:
            match_id = self.snapshot["match"]["id"]
        safe_id = self._safe_path_part(export_id)
        path = self.repo.db_path.parent / "exports" / match_id / f"{safe_id}.zip"
        if not path.exists():
            raise MatchStateError("export_not_found", "未找到指定导出文件。", {"export_id": export_id})
        return path

    async def emit(
        self,
        event_type: str,
        payload: Dict[str, Any],
        actor_type: str = "system",
        actor_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        async with self._lock:
            self.seq += 1
            event = {
                "id": f"evt_{self.seq}",
                "type": event_type,
                "match_id": self.snapshot["match"]["id"],
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
            elif status == "running":
                self._resume_paused_clocks()
            elif status in {"finished", "intervention"}:
                self._pause_running_clocks()
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
            allowed = {"name", "model_name", "model_kind", "agent_endpoint"}
            updated = []
            for key, value in fields.items():
                if key not in allowed:
                    continue
                if key == "model_kind" and value not in {None, "", "open_source", "closed_source"}:
                    raise MatchStateError("invalid_speaker_config", "AI 模型类型必须为 open_source 或 closed_source。", {"model_kind": value})
                if key in {"model_name", "model_kind", "agent_endpoint"} and speaker["speaker_type"] != "agent":
                    continue
                speaker[key] = None if value == "" and key in {"model_name", "model_kind"} else value
                updated.append(key)

            if "name" in updated or "model_name" in updated:
                self._sync_agent_status_for_speaker(speaker)
            self.snapshot["match"]["updated_at"] = iso_now()
            self._persist_snapshot()
        return await self.emit("speaker.updated", {"speaker_id": speaker_id, "fields": sorted(set(updated))}, "admin")

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
        async with self._lock:
            self.snapshot["match"]["screen_scene"] = scene
            if live_mode:
                self.snapshot["match"]["live_mode"] = live_mode
            self.snapshot["match"]["updated_at"] = iso_now()
            self._persist_snapshot()
        return await self.emit("screen.scene_changed", {"scene": scene, "live_mode": live_mode}, "host")

    async def start_phase(self, phase_id: str) -> Dict[str, Any]:
        async with self._lock:
            self._ensure_match_allows_control("start_phase")
            phase = self._find_phase(phase_id)
            self.snapshot["current_speech"] = None
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

    async def run_agent_speech(self, speaker_id: str) -> None:
        speaker = self._find_speaker(speaker_id)
        if speaker["speaker_type"] != "agent":
            return
        self._ensure_match_allows_control("run_agent_speech")
        self._ensure_speaker_allowed_for_current_phase(speaker)

        task_id = f"task_{self.seq + 1}"
        speech_id = f"speech_{self.seq + 1}"
        endpoint = self.agent_gateway.endpoint_for(speaker)
        payload = self._build_agent_payload(task_id, speech_id, speaker)
        await self.emit(
            "agent.task.created",
            {"task_id": task_id, "speaker_id": speaker_id, "endpoint": endpoint or "embedded://mock"},
            "system",
        )

        async with self._lock:
            phase_id = self.snapshot["match"]["current_phase_id"]
            self.snapshot["match"]["live_mode"] = "prep"
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
            async for event in self.agent_gateway.stream_speech(endpoint, payload, self._mock_agent_chunks(speaker)):
                event_type = event.get("type")
                if event_type == "delta":
                    delta = event.get("delta", "")
                    if delta and not playback_started:
                        await self._start_agent_playback(task_id, speaker)
                        playback_started = True
                    full_text += delta
                    async with self._lock:
                        if not self.snapshot.get("current_speech"):
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
            await self._fail_agent_task(task_id, speaker_id, exc)
            return

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
                }
            )
            self.snapshot["current_speech"] = speech
            self.snapshot["match"]["live_mode"] = "free" if phase_id == "phase_free_debate" else "single"
            self._start_relevant_clocks(speaker["side"])
            self.snapshot["speech_service"]["asr"] = {
                "status": "streaming",
                "latency_ms": self.snapshot["speech_service"]["asr"].get("latency_ms", 0),
                "active_sessions": 1,
                "detail": "recording",
            }
            self._persist_snapshot()
        return await self.emit("speech.started", {"speaker_id": speaker_id}, "speaker", speaker_id)

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
            gateway = XfyunASRGateway(url=os.getenv("XFYUN_ASR_URL", "").strip())

            async def on_partial(text: str, latency_ms: int, chunk_count: int) -> None:
                await self._record_live_asr_text(speech_id, speaker_id, text, False, latency_ms, chunk_count)

            async def on_final(text: str, latency_ms: int, chunk_count: int) -> None:
                await self._record_live_asr_text(speech_id, speaker_id, text, True, latency_ms, chunk_count)

            async def on_error(exc: XfyunGatewayError) -> None:
                await self._record_live_asr_failed(speech_id, speaker_id, exc.message, exc.code)

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
                self._persist_snapshot()
            raise MatchStateError("invalid_audio_archive", "归档音频为空，无法识别。", {"speech_id": speech_id})
        await self.emit("asr.archive_recognition_started", {"speech_id": speech_id, "audio_bytes": len(content)}, "host")

        gateway = XfyunASRGateway(url=os.getenv("XFYUN_ASR_URL", "").strip())
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
        async with self._lock:
            self.snapshot["speech_service"]["asr"] = {
                "status": "recognizing",
                "latency_ms": 0,
                "active_sessions": 1,
                "detail": "Xunfei ASR probe started",
            }
            self._persist_snapshot()
        await self.emit("asr.probe_started", {"audio_bytes": len(content), "format": audio_format, "encoding": encoding}, "host")

        gateway = XfyunASRGateway(url=os.getenv("XFYUN_ASR_URL", "").strip())
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
            self._persist_snapshot()
        await self.emit("asr.probe_completed", payload, "host")
        return {"result": payload, "snapshot": await self.get_snapshot()}

    async def probe_tts(self, text: str) -> Dict[str, Any]:
        content = str(text or "").strip() or "人机辩论赛语音合成自检。"
        async with self._lock:
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
        }
        async with self._lock:
            self.snapshot["speech_service"]["tts"] = {
                "status": "idle",
                "latency_ms": result.latency_ms,
                "queue_size": 0,
                "speaker_id": None,
                "detail": f"TTS probe ok · {len(result.audio)} bytes · {file_path}",
            }
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
            else:
                clock["state"] = "paused"
                clock["deadline_at"] = None
            payload = self._clock_payload(clock, reason)
            self._persist_snapshot()
        return await self.emit("clock.adjusted", payload, "host")

    async def submit_vote(self, body: Dict[str, Any], audience: bool = False) -> Dict[str, Any]:
        async with self._lock:
            self._validate_vote_body(body, audience=audience)
            if audience:
                if self.snapshot["vote_state"]["window_status"] != "open":
                    raise MatchStateError(
                        "vote_window_closed",
                        "学生投票窗口未开启，暂不能提交投票。",
                        {"window_status": self.snapshot["vote_state"]["window_status"]},
                    )
                vote_key = self._audience_vote_key(body)
                if vote_key in set(self.snapshot["vote_state"].get("used_audience_tokens", [])):
                    raise MatchStateError("duplicate_vote", "请勿重复提交投票。", {"vote_key": vote_key})
                self.snapshot["vote_state"].setdefault("used_audience_tokens", []).append(vote_key)
                self.snapshot["vote_state"].setdefault("audience_votes", []).append(
                    {
                        "vote_key": vote_key,
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
            self._persist_snapshot()
        return await self.emit(
            "vote.submitted",
            {"audience": audience, "vote_state": self._public_vote_state()},
            "audience" if audience else "host",
        )

    async def open_audience_votes(self) -> Dict[str, Any]:
        async with self._lock:
            self.snapshot["vote_state"]["window_status"] = "open"
            self._persist_snapshot()
        return await self.emit("vote.window_opened", {"match_id": self.snapshot["match"]["id"]}, "host")

    async def close_audience_votes(self) -> Dict[str, Any]:
        async with self._lock:
            self.snapshot["vote_state"]["window_status"] = "closed"
            self._persist_snapshot()
        return await self.emit("vote.window_closed", {"match_id": self.snapshot["match"]["id"]}, "host")

    async def publish_votes(self, scope: str) -> Dict[str, Any]:
        async with self._lock:
            if scope not in {"judge", "audience"}:
                raise MatchStateError("invalid_vote_scope", "未知的投票公布范围。", {"scope": scope})
            if scope == "judge":
                if not self.snapshot["vote_state"].get("winner_side") or not self.snapshot["vote_state"].get("best_speaker_id"):
                    raise MatchStateError("missing_votes", "请先录入评委结果。")
                self.snapshot["vote_state"]["judge_published"] = True
            if scope == "audience":
                if not self.snapshot["vote_state"]["judge_published"]:
                    raise MatchStateError(
                        "publish_order",
                        "需要先公布评委结果，再公布学生投票结果。",
                        {"judge_published": False},
                    )
                self.snapshot["vote_state"]["audience_published"] = True
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
        self._ensure_speaker_allowed_for_current_phase(speaker)

    async def check_agent_health(self, speaker_id: str) -> Dict[str, Any]:
        speaker = self._find_speaker(speaker_id)
        if speaker["speaker_type"] != "agent":
            raise MatchStateError("invalid_speaker", "该辩手不是 AI 辩手，不能执行 Agent 健康检查。")

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

    async def websocket(self, websocket: WebSocket, last_seq: int = 0, channel: str = "screen", speaker_id: Optional[str] = None) -> None:
        await websocket.accept()
        self._connections.add(websocket)
        try:
            snapshot = await self.get_snapshot()
            missed = [event for event in self.events if event["seq"] > last_seq]
            await websocket.send_json(
                {
                    "type": "snapshot",
                    "match_id": snapshot["match"]["id"],
                    "seq": self.seq,
                    "server_time_ms": int(utc_now().timestamp() * 1000),
                    "payload": {"state": snapshot, "missed_events": missed},
                }
            )
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
        for websocket in self._connections:
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

    def _ensure_runtime_fields(self) -> None:
        self.snapshot["system"] = self._system_info()
        self.snapshot.setdefault(
            "free_debate",
            {
                "current_turn_side": "affirmative",
                "turn_index": 1,
                "assignment_mode": "teammate_control",
            },
        )
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
                "used_audience_tokens": [],
            },
        )
        self.snapshot["vote_state"].setdefault("judge_summary", self._empty_judge_summary())
        self.snapshot["vote_state"].setdefault("audience_summary", self._empty_audience_summary())
        self.snapshot["vote_state"].setdefault("audience_votes", [])
        self.snapshot["vote_state"].setdefault("used_audience_tokens", [])
        audience_summary = self.snapshot["vote_state"]["audience_summary"]
        if not self.snapshot["vote_state"]["audience_votes"] and audience_summary.get("total", 0) == 0 and self.snapshot["vote_state"].get("audience_count", 0) > 0:
            count = int(self.snapshot["vote_state"]["audience_count"])
            winner_side = self.snapshot["vote_state"].get("winner_side", "affirmative")
            audience_summary["total"] = count
            audience_summary.setdefault("winner", {"affirmative": 0, "negative": 0})
            audience_summary["winner"][winner_side] = count
        for speaker in self.snapshot.get("speakers", []):
            if speaker.get("speaker_type") == "human":
                speaker.setdefault("mic_permission", "unknown")
                speaker.setdefault("device_label", None)
                speaker.setdefault("last_seen_at", None)
            else:
                speaker.setdefault("mic_permission", None)
                speaker.setdefault("device_label", None)
                speaker.setdefault("last_seen_at", None)
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
        vote_state.pop("used_audience_tokens", None)
        vote_state.pop("audience_votes", None)

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

    def _zip_audio_assets(self, bundle: zipfile.ZipFile, audio_assets: List[Dict[str, Any]], entries: List[Dict[str, Any]]) -> None:
        for asset in audio_assets:
            for chunk in asset.get("chunks", []):
                file_path = Path(chunk.get("file_path", ""))
                if not file_path.exists() or not file_path.is_file():
                    continue
                arcname = f"audio/{self._safe_path_part(asset.get('speech_id', 'speech'))}/{file_path.name}"
                bundle.write(file_path, arcname)
                entries.append({"path": arcname, "size_bytes": file_path.stat().st_size})

    def _audience_vote_key(self, body: Dict[str, Any]) -> str:
        token = str(body.get("token") or "").strip()
        fingerprint = str(body.get("client_fingerprint") or "").strip()
        if token:
            return f"token:{token}"
        if fingerprint:
            return f"fingerprint:{fingerprint}"
        raise MatchStateError("invalid_vote", "学生投票必须包含 token 或浏览器指纹。")

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

    def _build_agent_payload(self, task_id: str, speech_id: str, speaker: Dict[str, Any]) -> Dict[str, Any]:
        phase = self._current_phase()
        match = self.snapshot["match"]
        time_limit = 15 if phase["phase_type"] == "free_debate" else phase["duration_seconds"]
        clock = self._clock("turn" if phase["phase_type"] == "free_debate" else "main")
        remaining_seconds = int((clock["remaining_ms"] if clock else time_limit * 1000) / 1000)
        transcript_tail = [
            {
                "speaker_label": segment["speaker_label"],
                "source": segment["source"],
                "text": segment["text"],
                "created_at": segment["created_at"],
            }
            for segment in self.snapshot.get("recent_transcript", [])
            if segment.get("valid", True)
        ][:8]
        return {
            "match_id": match["id"],
            "task_id": task_id,
            "speech_id": speech_id,
            "topic": match["topic"],
            "side": speaker["side"],
            "speaker_id": speaker["id"],
            "speaker_name": speaker["name"],
            "speaker_role": self._seat_label(speaker["seat"]),
            "phase": phase["phase_key"],
            "phase_type": phase["phase_type"],
            "turn_index": self._current_turn_index(),
            "time_limit_seconds": time_limit,
            "remaining_seconds": remaining_seconds,
            "target_chars": max(40, int(time_limit * 4.5)),
            "context": {
                "summary": "MVP 自动上下文：请围绕当前辩题与最近发言直接回应。",
                "transcript_tail": transcript_tail,
                "opponent_claims": [],
                "own_claims": [],
                "host_notes": [],
            },
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

        async with self._lock:
            current_speech = self.snapshot.get("current_speech") or {}
            phase_id = current_speech.get("phase_id") or self.snapshot["match"]["current_phase_id"]
            phase_key = self._phase_key_or_default(phase_id)
            match_id = self.snapshot["match"]["id"]
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

    def _save_audit_for_event(self, event: Dict[str, Any]) -> None:
        if event["actor_type"] not in {"admin", "host"}:
            return
        self.repo.save_audit_log(
            audit_id=f"audit_{event['seq']}",
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
        next_side = "negative" if side == "affirmative" else "affirmative"
        self.snapshot["free_debate"]["current_turn_side"] = next_side
        self.snapshot["free_debate"]["turn_index"] = int(self.snapshot["free_debate"]["turn_index"]) + 1
        turn_clock = self._clock("turn")
        if turn_clock:
            turn_clock["remaining_ms"] = turn_clock["total_seconds"] * 1000
            turn_clock["state"] = "paused"
            turn_clock["deadline_at"] = None

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
            elif clock["state"] == "running":
                clock["state"] = "paused"
                clock["deadline_at"] = None

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
        return int(phase.get("turn_seconds") or 15)

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
            return
        for item in self.snapshot.get("agent_status", []):
            if item.get("speaker_id") == speaker["id"]:
                item["name"] = speaker["name"]
                if speaker.get("model_name"):
                    item["model"] = speaker["model_name"]
                return

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


store = MatchStore()
