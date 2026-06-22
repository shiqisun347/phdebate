from __future__ import annotations

import asyncio
import base64
import csv
import hashlib
import io
import json
import os
import re
import time
import zipfile
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from fastapi import WebSocket, WebSocketDisconnect

from app.services.agent_gateway import AgentGateway, AgentGatewayError
from app.services.integration_config import integration_config
from app.services.speech_gateway import SpeechGatewayError, normalize_tts_text, select_asr_gateway, select_tts_gateway
from app.services.sqlite_repo import SQLiteRepository, project_root
from app.services.tts_live import tts_live_manager
from app.services.xfyun_gateway import TTSResult, XfyunASRGateway, XfyunGatewayError, XfyunTTSGateway


def _select_asr_gateway():
    return select_asr_gateway().gateway


SpeechProviderError = (SpeechGatewayError, XfyunGatewayError)


def _recent_transcript_limit() -> int:
    """Max distinct speeches kept in the in-snapshot transcript (and thus in
    debate_history). Large enough to hold a full debate; bounded for snapshot size."""
    raw = os.getenv("PHDEBATE_RECENT_TRANSCRIPT_LIMIT", "100").strip()
    try:
        value = int(raw)
    except ValueError:
        value = 100
    return max(12, min(500, value))


_RECENT_TRANSCRIPT_LIMIT = _recent_transcript_limit()


def _speech_error_message(exc: Exception) -> str:
    return str(getattr(exc, "message", None) or exc)


def _speech_error_code(exc: Exception, fallback: str) -> str:
    code = getattr(exc, "code", None)
    return str(code) if code is not None else fallback


def _speech_error_provider(exc: Exception, fallback: str = "") -> str:
    return str(getattr(exc, "provider", "") or fallback)


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
        # 每个 WebSocket 连接独占一条有界发送队列 + 一个发送协程：广播只往队列里塞(非阻塞)，
        # 由各自的发送协程串行发出。一个慢/卡的客户端只会撑爆自己的队列被丢弃，绝不阻塞
        # 持有 self._lock 的比赛主流程或其它客户端(彻底消除「一个慢客户端拖死全场」)。
        self._conn_send_queues: Dict[WebSocket, "asyncio.Queue[Dict[str, Any]]"] = {}
        self._conn_senders: Dict[WebSocket, asyncio.Task] = {}
        self._asr_streams: Dict[str, Any] = {}
        self._tts_grace_tasks: Dict[str, asyncio.Task] = {}
        self._tts_request_lock = asyncio.Lock()
        self._last_tts_request_at = 0.0
        # 预取缓存：在空档提前生成 agent 文本 + 归档 TTS，进入该环节时直接促活（见 _prefetch_speech）。
        # 键：自我介绍 `self_intro:{speaker_id}`；固定单人环节 `phase:{phase_id}:{speaker_id}`。
        self._prepared_speeches: Dict[str, Dict[str, Any]] = {}
        self._prefetch_inflight: Set[str] = set()
        self._prefetch_counter = 0
        self.repo = SQLiteRepository()
        self.agent_gateway = AgentGateway()
        loaded = self.repo.load_snapshot()
        if loaded:
            self.snapshot = loaded
            self.seq = int(loaded.get("last_seq", 0))
            self._ensure_runtime_fields()
            self._persist_snapshot()  # persist any migrations applied during normalization
            self.events = self.repo.load_events(loaded["match"]["id"])
        else:
            # 需求：不自动预置 demo 比赛。全新启动=空白起步，操作员须手动「新建比赛」。
            # （reset_demo 仍保留给 /api/demo/reset 与测试使用。）
            self.seq = 0
            self.events = []
            self.snapshot = self._empty_snapshot()
            self._ensure_runtime_fields()
            self._persist_snapshot()

    def reset_demo(self) -> None:
        now = utc_now()
        self.seq = 1842
        self.events: List[Dict[str, Any]] = []
        self._asr_streams = {}
        self._clear_prepared_speeches()
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
                "title_display": "text",
                "title_image_url": "",
                "topic": "AI 时代，我们更应该培养编程思维 / 提问思维",
                "affirmative_position": "更应该培养编程思维",
                "negative_position": "更应该培养提问思维",
                "organizer": "中国科学院计算技术研究所",
                "organizer_display": "image",
                "organizer_image_url": "/assets/logo-full-white.png",
                "venue": "现场会场",
                "status": "running",
                "screen_scene": "idle",
                "live_mode": "free",
                "current_phase_id": "phase_free_debate",
                "created_at": to_iso(now),
                "updated_at": to_iso(now),
            },
            "teams": self._demo_teams(),
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

    def _demo_teams(self) -> List[Dict[str, Any]]:
        return [
            {"id": "team_aff", "side": "affirmative", "name": "智码战队", "position": "编程思维", "description": "主张 AI 时代更应该培养编程思维"},
            {"id": "team_neg", "side": "negative", "name": "问道战队", "position": "提问思维", "description": "主张 AI 时代更应该培养提问思维"},
        ]

    def _empty_snapshot(self) -> Dict[str, Any]:
        """无比赛的"空白起步"状态：合法但没有任何比赛（id=""、status=draft、名单/环节全空）。
        需求：系统不自动预置 demo，操作员必须在「比赛管理」手动「新建比赛」。"""
        now = utc_now()
        return {
            "match": {
                "id": "",
                "title": "",
                "title_display": "text",
                "title_image_url": "",
                "topic": "",
                "affirmative_position": "正方",
                "negative_position": "反方",
                "organizer": "",
                "organizer_display": "text",
                "organizer_image_url": "",
                "venue": "",
                "status": "draft",
                "screen_scene": "idle",
                "live_mode": "single",
                "current_phase_id": "",
                "created_at": to_iso(now),
                "updated_at": to_iso(now),
            },
            "teams": [],
            "speakers": [],
            "agent_configs": [],
            "phases": [],
            "clocks": [],
            "current_speech": None,
            "free_debate": {"current_turn_side": "affirmative", "turn_index": 1, "assignment_mode": "teammate_control"},
            "flow": self._fresh_flow_state(),
            "audio_output": self._fresh_audio_output_state(),
            "recent_transcript": [],
            "speech_revisions": [],
            "audio_assets": [],
            "agent_status": [],
            "vote_state": {
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
            "speech_service": {
                "asr": {"status": "idle", "latency_ms": 0, "active_sessions": 0, "detail": ""},
                "tts": {"status": "idle", "latency_ms": 0, "queue_size": 0, "speaker_id": None, "detail": ""},
                "screen": {"status": "connected"},
                "consoles": {"online": 0, "total": 0, "mic_errors": []},
            },
            "system": self._system_info(),
            "last_seq": 0,
        }

    def _has_real_match(self) -> bool:
        """当前快照是否是一场"真实比赛"（而非空白起步状态）。"""
        return bool((self.snapshot.get("match") or {}).get("id")) and bool(self.snapshot.get("speakers"))

    def _default_template_snapshot(self) -> Dict[str, Any]:
        """新建比赛在"无当前比赛"时使用的默认名单模板（标准 4+4 名单 / 两队 / 默认环节）。
        其余 live 状态由 _new_match_snapshot_from_archive 重置。操作员创建后可在「辩手管理」改名单。"""
        now = utc_now()
        snap = self._empty_snapshot()
        snap["match"]["id"] = "template"
        snap["match"]["status"] = "ready"
        snap["teams"] = self._demo_teams()
        snap["speakers"] = self._demo_speakers()
        snap["agent_configs"] = self._demo_agent_configs(now)
        snap["phases"] = self._demo_phases()
        return snap

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
            self._refresh_tts_runtime_status()
            snap = deepcopy(self.snapshot)
            snap["last_seq"] = self.seq
            self._sanitize_snapshot(snap)
            snap["xiaoqi"] = self._xiaoqi_public()
            return snap

    def _refresh_tts_runtime_status(self) -> None:
        speech = self.snapshot.get("current_speech") or {}
        task_id = speech.get("tts_task_id")
        if speech.get("source") != "agent_text" or speech.get("state") == "ended" or not task_id:
            return
        tts = self.snapshot.get("speech_service", {}).setdefault("tts", {})
        created = int(speech.get("tts_created_sentences") or 0)
        ready = int(speech.get("tts_ready_sentences") or 0)
        played = int(speech.get("tts_played_sentences") or 0)
        if created <= 0 and ready <= 0 and played <= 0:
            return
        status = "playing" if played > 0 or speech.get("state") == "speaking" else "synthesizing"
        if status == "playing":
            queue_size = self._tts_playback_display_queue_size(speech, fallback_total=created or ready or 1)
            last_playback_status = str(speech.get("tts_last_playback_status") or "")
            if last_playback_status in {"stalled", "error", "play_rejected", "failed"}:
                detail = f"screen playback {last_playback_status} at segment {max(1, int(speech.get('tts_playing_sentence_idx') or 0) + 1)}/{speech.get('tts_expected_sentences') or created or ready or '?'}"
            else:
                detail = f"screen playing segment {max(1, int(speech.get('tts_playing_sentence_idx') or 0) + 1)}/{speech.get('tts_expected_sentences') or created or ready or '?'}"
        else:
            queue_size = self._tts_unresolved_sentence_count(speech, fallback_total=created)
            detail = f"TTS archived {ready}/{created or '?'} segments"
        tts.update(
            {
                "status": status,
                "latency_ms": int(tts.get("latency_ms") or 0),
                "queue_size": queue_size,
                "speaker_id": speech.get("speaker_id"),
                "detail": detail,
                "last_progress_at": speech.get("tts_last_progress_at") or iso_now(),
            }
        )
        expected = self._tts_int(speech.get("tts_expected_sentences"), 0)
        if expected > 0:
            self._arm_tts_playback_grace(str(speech.get("id")), str(task_id), expected)

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
            self._clear_prepared_speeches()
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
        if not self._has_real_match():
            return  # 空白起步状态不入注册表/槽位
        self.repo.set_app_state(self._match_slot_key(self.snapshot["match"]["id"]), self.snapshot, iso_now())
        self._upsert_registry(self.snapshot)

    async def list_matches(self) -> Dict[str, Any]:
        async with self._lock:
            if self._has_real_match():
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
            # 有真实当前比赛→沿用其名单模板（基于上一场便利）；空白起步→用默认名单模板。
            template = deepcopy(self.snapshot) if self._has_real_match() else self._default_template_snapshot()
            new_snapshot = self._new_match_snapshot_from_archive(template, new_id, now)
            title = str(fields.get("title") or "").strip()
            topic = str(fields.get("topic") or "").strip()
            if title:
                new_snapshot["match"]["title"] = title
            if topic:
                new_snapshot["match"]["topic"] = topic
            # 新比赛默认用文本标题（不沿用上一场的图片标题）；主办机构图片可继承沿用。
            new_snapshot["match"]["title_display"] = "text"
            new_snapshot["match"]["title_image_url"] = ""
            for key in (
                "affirmative_position",
                "negative_position",
                "organizer",
                "venue",
                "title_display",
                "title_image_url",
                "organizer_display",
                "organizer_image_url",
            ):
                if key in fields:
                    new_snapshot["match"][key] = fields.get(key)
            for field_key, side in (("affirmative_position", "affirmative"), ("negative_position", "negative")):
                value = str(fields.get(field_key) or "").strip()
                if not value:
                    continue
                for team in new_snapshot.get("teams", []):
                    if team.get("side") == side:
                        team["position"] = value
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
            self._clear_prepared_speeches()
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
                self._clear_prepared_speeches()
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
            is_active = target_id == self.snapshot["match"]["id"]
            matches = self._load_registry()
            if not is_active and not any(m.get("id") == target_id for m in matches):
                raise MatchStateError("match_not_found", "未找到该比赛。", {"match_id": target_id})
            remaining = [m for m in matches if m.get("id") != target_id]
            self._save_registry(remaining)
            self.repo.delete_app_state(self._match_slot_key(target_id))
            self.repo.clear_match_history(target_id)
            if is_active:
                self._clear_prepared_speeches()
                # 删除当前比赛：切到最近的其它比赛；没有了就回到"空白起步"（无比赛，须手动新建）。
                remaining.sort(key=lambda m: m.get("created_at") or "", reverse=True)
                nxt = next((m for m in remaining if self.repo.get_app_state(self._match_slot_key(str(m.get("id"))))), None)
                if nxt:
                    target = self.repo.get_app_state(self._match_slot_key(str(nxt["id"])))
                    self.snapshot = target
                    self.seq = int((target or {}).get("last_seq", 0))
                    self._ensure_runtime_fields()
                    self.events = self.repo.load_events(str(nxt["id"]))
                    self._asr_streams = {}
                    self._persist_snapshot()
                    self.repo.delete_app_state(self._match_slot_key(str(nxt["id"])))
                    self._upsert_registry(self.snapshot)
                else:
                    self.seq = 0
                    self.events = []
                    self._asr_streams = {}
                    self.snapshot = self._empty_snapshot()
                    self._ensure_runtime_fields()
                    self._persist_snapshot()
        await self.emit("match.deleted", {"match_id": target_id}, "admin")
        return await self.list_matches()

    async def emit(
        self,
        event_type: str,
        payload: Dict[str, Any],
        actor_type: str = "system",
        actor_id: Optional[str] = None,
        *,
        persist: bool = True,
        sync_structured: bool = True,
    ) -> Dict[str, Any]:
        # persist=False 仅用于高频瞬态事件（如 agent.speech.delta）：跳过整张快照落盘以避免锁拥塞。
        # sync_structured=False 用于高频但需要落盘的事件（如 tts.sentence_ready）：仍写 app_state 实时
        # 快照，但跳过昂贵的结构化镜像同步（在关键节点统一同步）。事件仍记录、仍广播。
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
            if persist:
                self._persist_snapshot(sync_structured=sync_structured)
            self._save_audit_for_event(event)
            self._broadcast({"type": event_type, **event})
            return event

    async def set_match_status(self, status: str) -> Dict[str, Any]:
        async with self._lock:
            self.snapshot["match"]["status"] = status
            self.snapshot["match"]["updated_at"] = iso_now()
            if status == "paused":
                # 记录此刻"正在走"的钟，恢复时只重启这些——避免把"尚未开始的环节钟"也一起起跑。
                self._refresh_clocks()
                self.snapshot["match"]["resume_clock_ids"] = [
                    c["id"] for c in self.snapshot["clocks"] if c.get("state") == "running"
                ]
                self._pause_running_clocks()
                self.snapshot["match"]["screen_scene"] = "paused"
            elif status == "running":
                # 仅恢复暂停前正在走的钟；初次「开始比赛」时没有待恢复的钟 → 不自动起跑，
                # 倒计时要等发言人点「开始发言」（或 AI 首次播报）才开始（_start_relevant_clocks）。
                resume_ids = self.snapshot["match"].pop("resume_clock_ids", None)
                if resume_ids:
                    self._resume_clocks_by_id(resume_ids)
                if self.snapshot["match"].get("screen_scene") in {"idle", "paused"}:
                    self.snapshot["match"]["screen_scene"] = "live"
            elif status in {"finished", "intervention"}:
                if status == "intervention":
                    self._refresh_clocks()
                    self.snapshot["match"]["resume_clock_ids"] = [
                        c["id"] for c in self.snapshot["clocks"] if c.get("state") == "running"
                    ]
                self._pause_running_clocks()
                self._clear_flow_state()
                if status == "finished":
                    self.snapshot["match"].pop("resume_clock_ids", None)
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
                "title_display",
                "title_image_url",
                "topic",
                "affirmative_position",
                "negative_position",
                "organizer",
                "organizer_display",
                "organizer_image_url",
                "venue",
            }
            for key, value in fields.items():
                if key in allowed:
                    if key in {"title_display", "organizer_display"}:
                        value = "image" if str(value) == "image" else "text"
                    self.snapshot["match"][key] = value
            self._sync_team_positions(fields)
            self.snapshot["match"]["updated_at"] = iso_now()
            self._persist_snapshot()
        return await self.emit("match.updated", {"fields": sorted(set(fields) & allowed)}, "admin")

    def _sync_team_positions(self, fields: Dict[str, Any]) -> None:
        """Keep each team's `position`（大屏展示的立场）in sync with the match-level立场."""
        mapping = {"affirmative_position": "affirmative", "negative_position": "negative"}
        for field_key, side in mapping.items():
            if field_key not in fields:
                continue
            value = str(fields.get(field_key) or "").strip()
            if not value:
                continue
            for team in self.snapshot.get("teams", []):
                if team.get("side") == side:
                    team["position"] = value

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
            allowed = {"name", "speaker_type", "agent_config_id", "image_url", "tts_voice_preset_id"}
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
                        speaker.pop("tts_voice_preset_id", None)
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
                if key == "tts_voice_preset_id":
                    if speaker["speaker_type"] != "agent":
                        speaker.pop("tts_voice_preset_id", None)
                        continue
                    preset_id = str(value or "").strip()
                    if preset_id:
                        preset = integration_config.voice_preset(preset_id)
                        provider = str((integration_config.active_section("tts") or {}).get("provider") or "alicloud")
                        if not preset or not preset.get("enabled") or preset.get("provider") != provider:
                            raise MatchStateError(
                                "invalid_tts_voice_preset",
                                "请选择语音引擎页已启用、且匹配当前 TTS 服务商的音色预设。",
                                {"speaker_id": speaker_id, "tts_voice_preset_id": preset_id, "provider": provider},
                            )
                        speaker["tts_voice_preset_id"] = preset_id
                    else:
                        speaker["tts_voice_preset_id"] = None
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
        if isinstance((body or {}).get("voice_presets"), list):
            changed.append("voice_presets")
        if "tts" in changed or "voice_presets" in changed:
            await self._clear_invalid_tts_voice_assignments()
        await self.emit("integration_config.updated", {"sections": changed}, "admin")
        return config

    async def _clear_invalid_tts_voice_assignments(self) -> None:
        provider = str((integration_config.active_section("tts") or {}).get("provider") or "alicloud")
        valid_ids = {
            str(preset.get("id"))
            for preset in integration_config.voice_presets()
            if preset.get("enabled") and preset.get("provider") == provider
        }
        async with self._lock:
            changed = False
            for speaker in self.snapshot.get("speakers", []):
                if speaker.get("speaker_type") != "agent":
                    if speaker.pop("tts_voice_preset_id", None) is not None:
                        changed = True
                    continue
                preset_id = str(speaker.get("tts_voice_preset_id") or "").strip()
                if preset_id and preset_id not in valid_ids:
                    speaker["tts_voice_preset_id"] = None
                    changed = True
            if changed:
                self.snapshot["match"]["updated_at"] = iso_now()
                self._persist_snapshot()

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
            elif normalized_scene == "xiaoqi_result" and not self.snapshot["vote_state"].get("xiaoqi_recorded"):
                raise MatchStateError(
                    "xiaoqi_result_not_recorded",
                    "请先完成「小七结果录入」（获胜方 + 最佳辩手），再切换到小七评判。",
                    {"xiaoqi_recorded": False},
                )
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
        # 进入「辩题介绍」即后台预取全部 agent 自我介绍，等到自我介绍阶段可直接促活播放。
        if normalized_scene == "opening" and self._prefetch_enabled():
            asyncio.create_task(self._prefetch_all_self_intros())
        return await self.emit("screen.scene_changed", {"scene": normalized_scene, "requested_scene": scene, "live_mode": live_mode}, "host")

    def _finalize_current_speech_for_history(self) -> None:
        """Persist the in-progress speech's text as a FINAL transcript segment before it
        is abandoned (e.g. host advances the phase mid-speech). Without this, a speech
        that has already produced text never becomes is_final and is silently dropped
        from the global debate_history sent to later agents."""
        speech = self.snapshot.get("current_speech")
        if not speech:
            return
        speaker_id = speech.get("speaker_id")
        if not speaker_id:
            return
        text = (speech.get("content_final") or speech.get("content_partial") or "").strip()
        if not text:
            return
        source = speech.get("source", "agent_text")
        segment = self._upsert_transcript_segment(speech, speaker_id, text, True, source)
        if speech.get("kind") == "self_intro":
            segment["exclude_from_history"] = True
            segment["kind"] = "self_intro"

    async def start_phase(self, phase_id: str) -> Dict[str, Any]:
        async with self._lock:
            self._ensure_match_allows_control("start_phase")
            phase = self._find_phase(phase_id)
            # 推进环节前，先把上一段（可能仍在播报的）发言定稿进全局历史，避免丢失。
            self._finalize_current_speech_for_history()
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
                "state": "thinking" if speaker["speaker_type"] == "agent" else "ready",
            }
            self.snapshot["match"]["live_mode"] = "prep" if speaker["speaker_type"] == "agent" else self.snapshot["match"]["live_mode"]
            self._persist_snapshot()
        return await self.emit(
            "speaker.activated",
            {"speaker_id": speaker_id, "side": speaker["side"], "speaker_type": speaker["speaker_type"]},
            "host",
        )

    async def record_free_debate_skip(self, speaker_id: str) -> Dict[str, Any]:
        """人类辩手在自由辩论里（预）跳过自己的发言轮。

        需求 5.md：跳过是"预点"——对方发言期间（直到对方说完后 2s 内）本方就能点，预告本方下一轮跳过。
        - 本方就是当前轮 → 跳过当前轮（target=当前 idx）；本方是下一方（对方在发言）→ 预跳过 idx+1。
        - 若本方人类全部投了跳过：当前轮→立即随机 AI 接管；预跳过下一轮→由翻面逻辑在轮到本方时立即接管。
        """
        speaker = self._find_speaker(speaker_id)
        if speaker["speaker_type"] != "human":
            raise MatchStateError("not_human_speaker", "只有人类辩手可以跳过发言。", {"speaker_id": speaker_id})
        phase = self._current_phase()
        if phase["phase_type"] != "free_debate":
            raise MatchStateError("not_free_debate", "跳过功能仅在自由辩论阶段可用。", {"phase_type": phase["phase_type"]})
        self._ensure_match_allows_control("record_free_debate_skip")
        side = speaker["side"]
        fd = self.snapshot["free_debate"]
        current_side = fd["current_turn_side"]
        current_idx = int(fd["turn_index"])
        # 目标轮：本方=当前轮→当前 idx；本方是下一方（对方在发言）→预跳过 idx+1。
        is_current_turn = side == current_side
        target_idx = current_idx if is_current_turn else current_idx + 1
        turn_key = f"{side}_{target_idx}"
        async with self._lock:
            # 该目标轮已被 AI 接管 → 幂等返回（跳过已无意义）。
            if fd.setdefault("auto_handled", {}).get(turn_key):
                return await self.get_snapshot()
            skip_votes = fd.setdefault("skip_votes", {})
            turn_votes: list = skip_votes.setdefault(turn_key, [])
            if speaker_id not in turn_votes:
                turn_votes.append(speaker_id)
            all_humans_on_side = [s["id"] for s in self.snapshot["speakers"] if s["side"] == side and s["speaker_type"] == "human"]
            skipped_all = bool(all_humans_on_side) and all(uid in turn_votes for uid in all_humans_on_side)
            self._persist_snapshot()
        await self.emit(
            "free_debate.skip_voted",
            {"speaker_id": speaker_id, "side": side, "turn_key": turn_key, "skip_count": len(turn_votes), "total_humans": len(all_humans_on_side)},
            "system",
        )
        # 全跳过：当前轮→立即 AI 接管；预跳过下一轮→只记录，轮到本方时由 _advance_free_debate_turn_if_needed 立即接管。
        if skipped_all and is_current_turn:
            await self._trigger_free_debate_auto_agent(side, target_idx, reason="all_skipped")
        return await self.get_snapshot()

    async def run_agent_speech(self, speaker_id: str, mode: str = "speech") -> None:
        is_self_intro = mode == "self_intro"
        speaker = self._find_speaker(speaker_id)
        if speaker["speaker_type"] != "agent":
            return
        # 自我介绍属于赛前动作：比赛通常还是 "ready" 而非 "running"，因此放宽限制，
        # 只要比赛未结束/未归档即可，且不受当前环节轮次限制。
        if is_self_intro:
            status = self.snapshot["match"]["status"]
            if status in {"finished", "archived"}:
                raise MatchStateError(
                    "invalid_state",
                    "比赛已结束，不能进行自我介绍。",
                    {"command": "self_introduction", "status": status},
                )
        else:
            self._ensure_match_allows_control("run_agent_speech")
            self._ensure_speaker_allowed_for_current_phase(speaker)
        config = self._agent_config_for_speaker(speaker)
        if config and not config.get("enabled", True):
            raise MatchStateError(
                "agent_config_disabled",
                "该 Agent 配置已停用，不能触发发言。",
                {"speaker_id": speaker_id, "agent_config_id": config.get("id")},
            )

        # 提前预取命中：直接用缓存促活（音频已归档），不再实时调用 agent/TTS。
        # 未命中/失效/被关闭则返回 None，原样走下面的 live 路径——零行为变化。
        prepared = self._take_prepared_speech(speaker_id, mode)
        if prepared is not None:
            await self._activate_prepared_speech(prepared, speaker, is_self_intro)
            return

        # Finalize any still-in-progress previous speech into history BEFORE building this
        # request's debate_history, so a speech that was interrupted (e.g. by a free-debate
        # turn change) is still seen by the next agent. (Phase advances finalize in
        # start_phase; this covers same-phase / free-debate handovers.)
        async with self._lock:
            current = self.snapshot.get("current_speech")
            if current and current.get("id") and current.get("speaker_id") != speaker_id:
                self._finalize_current_speech_for_history()
                self._persist_snapshot()

        task_id = f"task_{self.seq + 1}"
        speech_id = f"speech_{self.seq + 1}"
        endpoint = self.agent_gateway.endpoint_for(speaker)
        payload = self._build_self_intro_payload(task_id, speech_id, speaker) if is_self_intro else self._build_agent_payload(task_id, speech_id, speaker)
        endpoint_label = endpoint or "embedded://mock"
        agent_started_at = iso_now()
        agent_started_time = time.perf_counter()
        tts_enabled = self._tts_formal_enabled()
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
                "kind": "self_intro" if is_self_intro else "speech",
                "content_final": "",
                "content_partial": "",
                "started_at": None,
                "state": "thinking",
                "tts_task_id": task_id if tts_enabled else None,
                "tts_expected_sentences": None,
                "tts_skipped_sentences": [],
                "tts_played_sentence_indices": [],
            }
            if tts_enabled:
                self.snapshot["speech_service"]["tts"] = {
                    "status": "synthesizing",
                    "latency_ms": self.snapshot["speech_service"]["tts"].get("latency_ms", 0),
                    "queue_size": 0,
                    "speaker_id": speaker_id,
                    "detail": "waiting for first TTS segment",
                    "last_progress_at": iso_now(),
                }
            self._set_agent_status(speaker_id, "streaming", "Agent task sent")
            self._persist_snapshot()

        await self.emit(
            "speaker.activated",
            {"speaker_id": speaker_id, "side": speaker["side"], "speaker_type": "agent"},
            "system",
        )

        full_text = ""
        tts_sent_chars = 0
        tts_sentence_idx = 0
        tts_sentence_tasks: List[asyncio.Task] = []
        tts_semaphore = asyncio.Semaphore(self._tts_sentence_concurrency())

        async def synthesize_sentence(sentence: str, sentence_idx: int) -> bool:
            async with tts_semaphore:
                return await self._synthesize_sentence_tts_with_timeout(sentence, sentence_idx, task_id, speech_id, speaker)

        try:
            async for event in self.agent_gateway.stream_speech(endpoint, payload, self._mock_agent_chunks(speaker), config=config):
                event_type = event.get("type")
                if event_type == "delta":
                    delta = event.get("delta", "")
                    full_text += delta
                    async with self._lock:
                        cs = self.snapshot.get("current_speech")
                        # Stop if this speech was cleared OR replaced by another speech
                        # (e.g. the host advanced the phase / a new speaker took over) —
                        # otherwise this task would corrupt the new current_speech.
                        if not cs or cs.get("id") != speech_id:
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
                        # 实时把回复流写入实时辩论过程（非定稿段），不必等发言/播报结束。
                        self._upsert_transcript_segment(
                            self.snapshot["current_speech"], speaker_id, full_text, False, "agent_text"
                        )
                        # 不在每个 delta 落盘整张快照：delta 是高频事件（流式逐字），而 _persist_snapshot
                        # 要序列化整张 ~157KB 快照并同步结构化表，逐字落盘会把后端锁霸占住，拖慢一切——
                        # 包括真正驱动出声的 tts.sentence_ready 广播（首句"特别慢"的主因）。内存快照已更新，
                        # 实时字幕（getMatch 读内存）照常；定稿/分段就绪等关键节点仍会落盘。
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
                        persist=False,
                    )
                    # Kick off TTS for each newly available segment. Prefer full
                    # sentences, but allow long comma-separated prefixes so the
                    # first audio can start before the agent finishes a paragraph.
                    if tts_enabled:
                        while True:
                            # Only the very first segment may be cut at a comma to keep
                            # time-to-first-audio low; everything after is whole-sentence
                            # so playback stays continuous without mid-sentence breaks.
                            sentence, new_sent_chars = self._next_tts_sentence(
                                full_text, tts_sent_chars, allow_soft_break=tts_sentence_idx == 0
                            )
                            tts_sent_chars = new_sent_chars  # always advance, even for short/empty
                            if not sentence:
                                break
                            tts_sentence_tasks.append(asyncio.create_task(synthesize_sentence(sentence, tts_sentence_idx)))
                            tts_sentence_idx += 1
                            async with self._lock:
                                speech = self.snapshot.get("current_speech")
                                if speech and speech.get("id") == speech_id:
                                    speech["tts_created_sentences"] = tts_sentence_idx
                                    self.snapshot["speech_service"]["tts"].update(
                                        {
                                            "status": "synthesizing",
                                            "queue_size": max(1, self._tts_unresolved_sentence_count(speech, fallback_total=tts_sentence_idx)),
                                            "speaker_id": speaker_id,
                                            "detail": f"TTS segment {tts_sentence_idx} queued",
                                            "last_progress_at": iso_now(),
                                        }
                                    )
                                    self._persist_snapshot()
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

        # 文本生成完成的瞬间就把本段定稿到全局历史（debate_history 依赖 is_final），
        # 不能等到 TTS 合成/播报结束 —— 否则在 TTS 期间推进到下一环节时，下一位 agent
        # 的 debate_history 会缺失这段刚说完的发言。自我介绍仍由 complete 流程处理。
        async with self._lock:
            speech = self.snapshot.get("current_speech")
            if speech and speech.get("id") == speech_id and speech.get("kind") != "self_intro":
                speech["content_final"] = full_text
                speech["agent_final_ready"] = True
                self._upsert_transcript_segment(speech, speaker_id, full_text, True, "agent_text")
                self._persist_snapshot()

        # Fire TTS for any remaining text that didn't end with punctuation
        remaining_tail = full_text[tts_sent_chars:].strip()
        if remaining_tail and tts_enabled:
            tts_sentence_tasks.append(asyncio.create_task(synthesize_sentence(remaining_tail, tts_sentence_idx)))
            tts_sentence_idx += 1
            async with self._lock:
                speech = self.snapshot.get("current_speech")
                if speech and speech.get("id") == speech_id:
                    speech["tts_created_sentences"] = tts_sentence_idx
                    self.snapshot["speech_service"]["tts"].update(
                        {
                            "status": "synthesizing",
                            "queue_size": max(1, self._tts_unresolved_sentence_count(speech, fallback_total=tts_sentence_idx)),
                            "speaker_id": speaker_id,
                            "detail": f"TTS segment {tts_sentence_idx} queued",
                            "last_progress_at": iso_now(),
                        }
                    )
                    self._persist_snapshot()

        # tts_sentence_idx now equals the number of sentence tasks actually created;
        # the screen waits for exactly this many tts.sentence_ready events.
        expected_sentence_count = tts_sentence_idx

        sentence_results: List[bool] = []
        if tts_sentence_tasks:
            raw = await asyncio.gather(*tts_sentence_tasks, return_exceptions=True)
            sentence_results = [bool(r) for r in raw if isinstance(r, bool)]

        all_tts_failed = bool(tts_enabled and tts_sentence_tasks and sentence_results and not any(sentence_results))

        async with self._lock:
            speech = self.snapshot.get("current_speech")
            if speech:
                speech["content_final"] = full_text
                speech["agent_final_ready"] = True
                speech["tts_task_id"] = task_id
                speech["tts_expected_sentences"] = expected_sentence_count
                speech["tts_created_sentences"] = expected_sentence_count
                # 段落已在生成完成时定稿（见上方）；此处仅更新 TTS 计数。
                # 完成对账：保证 [0,expected) 的每个 idx 都已 ready 或 skipped，否则补进
                # tts_skipped_sentences——这样大屏仅凭快照就能走到 nextIdx==expected，永不卡死。
                self._reconcile_tts_gaps(speech, expected_sentence_count)
            current_tts = self.snapshot["speech_service"]["tts"]
            if not all_tts_failed:
                current_tts["status"] = "playing"
                current_tts["queue_size"] = max(0, self._tts_remaining_playback_count(speech, fallback_total=expected_sentence_count)) if speech else 0
                current_tts["speaker_id"] = speaker_id
                current_tts["detail"] = "sentence TTS playback pending"
            current_tts["latency_ms"] = int(current_tts.get("latency_ms", 0) or 0)
            self._persist_snapshot()

        await self.emit(
            "agent.speech.final",
            {"task_id": task_id, "speech_id": payload["speech_id"], "speaker_id": speaker_id, "content": full_text},
            "agent",
            speaker_id,
        )
        finished_payload = {
            "task_id": task_id,
            "speech_id": speech_id,
            "speaker_id": speaker_id,
            "expected_sentence_count": expected_sentence_count,
        }
        await self.emit("tts.finished", finished_payload, "system")
        if all_tts_failed:
            await self.complete_agent_playback(speech_id, task_id, reason="tts_failed")
        elif not tts_enabled or expected_sentence_count == 0:
            await self.start_agent_playback(speech_id, task_id, reason="text_only")
            await self.complete_agent_playback(speech_id, task_id, reason="text_only")
        else:
            self._arm_tts_playback_grace(speech_id, task_id, finished_payload["expected_sentence_count"])

    async def resume_runtime_tasks(self) -> None:
        """Re-arm volatile runtime tasks after process restart.

        SQLite/app_state restores the current speech, but asyncio tasks are process-local.
        If the server restarts while a screen is playing TTS, the playback grace watchdog
        must be reattached or an already-exhausted speech may wait forever.
        """
        async with self._lock:
            speech = self.snapshot.get("current_speech") or {}
            if (
                speech.get("source") == "agent_text"
                and speech.get("state") != "ended"
                and speech.get("tts_task_id")
                and self._tts_int(speech.get("tts_expected_sentences"), 0) > 0
            ):
                speech_id = str(speech.get("id"))
                task_id = str(speech.get("tts_task_id"))
                expected = self._tts_int(speech.get("tts_expected_sentences"), 0)
            else:
                return
        self._arm_tts_playback_grace(speech_id, task_id, expected)

    def _arm_tts_playback_grace(self, speech_id: str, task_id: str, expected_sentence_count: int) -> None:
        key = f"{speech_id}:{task_id}"
        existing = self._tts_grace_tasks.get(key)
        if existing and not existing.done():
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        task = asyncio.create_task(self._complete_agent_playback_after_grace(speech_id, task_id, expected_sentence_count))
        self._tts_grace_tasks[key] = task

        def _forget(done: asyncio.Task) -> None:
            if self._tts_grace_tasks.get(key) is done:
                self._tts_grace_tasks.pop(key, None)

        task.add_done_callback(_forget)

    async def _complete_agent_playback_after_grace(self, speech_id: str, task_id: str, expected_sentence_count: int) -> None:
        # 进度感知的兜底：TTS 已经全部归档后，如果大屏从未开始播放，快速收尾，避免现场
        # 长时间卡在"播放中"；一旦大屏开始播放，则依赖前端真实 currentTime heartbeat 刷新
        # tts_last_progress_at，只有长时间没有任何真实播放进度时才收尾。
        poll_seconds = 3
        grace_started_at = utc_now()
        while True:
            async with self._lock:
                speech = self.snapshot.get("current_speech")
                if not (speech and speech.get("id") == speech_id and speech.get("tts_task_id") == task_id):
                    return  # 已被正常收尾 / 已切换发言
                expected = self._tts_int(speech.get("tts_expected_sentences"), 0)
                playback_exhausted = expected > 0 and self._tts_remaining_playback_count(speech, fallback_total=expected) == 0
                last_playback_status = str(speech.get("tts_last_playback_status") or "")
                playback_statuses = {"playing", "played", "stalled", "error", "play_rejected", "failed", "skipped"}
                last_iso = speech.get("tts_last_progress_at") if last_playback_status in playback_statuses else None
                if not last_iso and speech.get("started_at"):
                    last_iso = speech.get("started_at")
                last_dt = parse_iso(last_iso) if last_iso else None
                idle_anchor = last_dt or grace_started_at
                idle_seconds = (utc_now() - idle_anchor).total_seconds()
                idle_limit = (
                    self._tts_playback_idle_timeout_seconds(expected or expected_sentence_count)
                    if last_dt
                    else self._tts_playback_start_timeout_seconds()
                )
            if playback_exhausted:
                await self.complete_agent_playback(speech_id, task_id, reason="screen_playback_progress_exhausted")
                return
            if idle_seconds >= idle_limit:
                await self.complete_agent_playback(speech_id, task_id, reason="screen_playback_timeout")
                return
            await asyncio.sleep(poll_seconds)

    async def complete_agent_playback(self, speech_id: str, task_id: str, reason: str = "screen_playback_complete") -> Dict[str, Any]:
        ignored_payload: Optional[Dict[str, Any]] = None
        ended_payload: Optional[Dict[str, Any]] = None
        async with self._lock:
            speech = self.snapshot.get("current_speech")
            if not speech or speech.get("id") != speech_id:
                ignored_payload = {"speech_id": speech_id, "task_id": task_id, "ignored": True, "reason": reason}
            elif speech.get("tts_task_id") and speech.get("tts_task_id") != task_id:
                ignored_payload = {"speech_id": speech_id, "task_id": task_id, "ignored": True, "reason": "task_mismatch"}
            if ignored_payload:
                self._persist_snapshot()
            else:
                speaker_id = speech["speaker_id"]
                speaker = self._find_speaker(speaker_id)
                is_self_intro = speech.get("kind") == "self_intro"
                text = speech.get("content_final") or speech.get("content_partial") or ""
                speech["content_final"] = text
                speech["state"] = "ended"
                speech["ended_at"] = iso_now()
                speech["ended_reason"] = reason
                segment = self._upsert_transcript_segment(speech, speaker_id, text, True, speech.get("source", "agent_text"))
                # 自我介绍：记录系统、可在大屏展示，但不进入 agent 历史会话，也不推进辩论流程/计时。
                if is_self_intro:
                    segment["exclude_from_history"] = True
                    segment["kind"] = "self_intro"
                self.snapshot["current_speech"] = None
                if not is_self_intro:
                    self._pause_running_clocks()
                    self._advance_free_debate_turn_if_needed(speaker["side"])
                    self._set_flow_waiting_after_speech_end(speech, speaker, reason)
                self._set_agent_status(speaker_id, "ready", "self introduction completed" if is_self_intro else "last task completed")
                if reason == "tts_failed":
                    self.snapshot["speech_service"]["tts"] = {
                        "status": "failed",
                        "latency_ms": self.snapshot["speech_service"]["tts"].get("latency_ms", 0),
                        "queue_size": 0,
                        "speaker_id": None,
                        "detail": "TTS synthesis failed for all sentences",
                        "degraded_to": "text_only",
                    }
                else:
                    self.snapshot["speech_service"]["tts"] = {
                        "status": "idle",
                        "latency_ms": self.snapshot["speech_service"]["tts"].get("latency_ms", 0),
                        "queue_size": 0,
                        "speaker_id": None,
                        "detail": "browser playback completed",
                    }
                ended_payload = {"speaker_id": speaker_id, "side": speaker["side"], "speech_id": speech_id}
                self._persist_snapshot()

        if ignored_payload:
            await self.emit("tts.playback_complete_ignored", ignored_payload, "screen")
            return await self.get_snapshot()
        await self.emit("speech.ended", ended_payload or {"speech_id": speech_id}, "agent", (ended_payload or {}).get("speaker_id"))
        return await self.get_snapshot()

    async def run_mock_agent_speech(self, speaker_id: str) -> None:
        await self.run_agent_speech(speaker_id)

    # ======================= 预取（提前生成 + 缓存 agent 发言）=======================
    # 设计见 plan：纯优化 + 优雅回退。预取把 agent 文本生成 + 逐句 TTS 归档提前做完并缓存；
    # 进入该环节时 run_agent_speech 命中缓存即"促活"（不再调 agent/TTS），未命中则照旧走 live。

    def _prefetch_enabled(self) -> bool:
        # 默认关闭：预取的"促活"路径不发 tts.sentence_ready，大屏缺少出声快路触发；且未命中缓存时
        # 孤儿预取会与 live 发言并发生成、加倍 TTS 服务商负载导致 live 句子合成失败/不出声。
        # 修好这两点（促活重发 sentence_ready + live 启动时取消同人在途预取）前先关掉，避免影响现场。
        return os.getenv("PHDEBATE_PREFETCH_ENABLED", "0").strip().lower() not in {"0", "false", "no", "off"}

    def _prefetch_concurrency(self) -> int:
        try:
            n = int(os.getenv("PHDEBATE_PREFETCH_CONCURRENCY", "2"))
        except ValueError:
            n = 2
        return max(1, min(n, 8))

    def _clear_prepared_speeches(self) -> None:
        self._prepared_speeches.clear()
        self._prefetch_inflight.clear()

    def _history_fingerprint(self) -> str:
        raw = json.dumps(self._build_debate_history(), ensure_ascii=False, sort_keys=True)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def _prepared_key(self, speaker_id: str, mode: str, phase_id: Optional[str]) -> str:
        if mode == "self_intro":
            return f"self_intro:{speaker_id}"
        return f"phase:{phase_id}:{speaker_id}"

    async def _prefetch_all_self_intros(self) -> None:
        """进入「辩题介绍」场景时调用：限流预取全部 agent 的自我介绍（无 history 依赖，恒有效）。"""
        if not self._prefetch_enabled():
            return
        async with self._lock:
            if self.snapshot.get("current_speech"):
                return  # 有发言进行中则不预取，避免干扰
            if self.snapshot["match"]["status"] in {"finished", "archived"}:
                return
            speaker_ids = [s["id"] for s in self.snapshot.get("speakers", []) if s.get("speaker_type") == "agent"]
        sem = asyncio.Semaphore(self._prefetch_concurrency())

        async def _one(sid: str) -> None:
            async with sem:
                await self._prefetch_speech(sid, "self_intro")

        await asyncio.gather(*(_one(sid) for sid in speaker_ids), return_exceptions=True)

    async def _prefetch_speech(self, speaker_id: str, mode: str, target_phase: Optional[Dict[str, Any]] = None) -> None:
        if not self._prefetch_enabled():
            return
        is_self_intro = mode == "self_intro"
        try:
            speaker = self._find_speaker(speaker_id)
        except KeyError:
            return
        if speaker.get("speaker_type") != "agent":
            return
        config = self._agent_config_for_speaker(speaker)
        if config and not config.get("enabled", True):
            return
        phase_id = None if is_self_intro else (target_phase or {}).get("id")
        key = self._prepared_key(speaker_id, mode, phase_id)
        if key in self._prefetch_inflight:
            return
        existing = self._prepared_speeches.get(key)
        if existing and existing.get("status") in {"ready", "pending"}:
            return
        self._prefetch_inflight.add(key)
        try:
            await self._run_prefetch(speaker, mode, target_phase, key)
        except Exception:  # noqa: BLE001 — 预取失败绝不能影响主流程；命中失败即回退 live。
            entry = self._prepared_speeches.get(key)
            if entry and entry.get("status") == "pending":
                entry["status"] = "failed"
        finally:
            self._prefetch_inflight.discard(key)

    async def _run_prefetch(self, speaker: Dict[str, Any], mode: str, target_phase: Optional[Dict[str, Any]], key: str) -> None:
        is_self_intro = mode == "self_intro"
        speaker_id = speaker["id"]
        self._prefetch_counter += 1
        speech_id = f"speech_prep_{self._prefetch_counter}"
        task_id = f"task_prep_{self._prefetch_counter}"
        if is_self_intro:
            payload = self._build_self_intro_payload(task_id, speech_id, speaker)
            history_fp: Optional[str] = None
        else:
            payload = self._build_agent_payload(task_id, speech_id, speaker, phase_override=target_phase)
            history_fp = self._history_fingerprint()
        endpoint = self.agent_gateway.endpoint_for(speaker)
        config = self._agent_config_for_speaker(speaker)
        tts_enabled = self._tts_formal_enabled()
        # 占位（pending），避免并发重复预取同一键。
        self._prepared_speeches[key] = {
            "speech_id": speech_id,
            "task_id": task_id,
            "speaker_id": speaker_id,
            "kind": "self_intro" if is_self_intro else "speech",
            "phase_id": None if is_self_intro else (target_phase or {}).get("id"),
            "full_text": "",
            "expected_sentence_count": 0,
            "skipped_sentences": [],
            "status": "pending",
            "history_fp": history_fp,
            "created_at": iso_now(),
        }

        full_text = ""
        tts_sent_chars = 0
        tts_sentence_idx = 0
        tts_sentence_tasks: List[asyncio.Task] = []
        tts_semaphore = asyncio.Semaphore(self._tts_sentence_concurrency())

        async def synth(sentence: str, idx: int) -> bool:
            async with tts_semaphore:
                return await self._synthesize_sentence_tts_with_timeout(sentence, idx, task_id, speech_id, speaker)

        try:
            async for event in self.agent_gateway.stream_speech(endpoint, payload, self._mock_agent_chunks(speaker), config=config):
                et = event.get("type")
                if et == "delta":
                    full_text += event.get("delta", "")
                    if tts_enabled:
                        while True:
                            sentence, new_chars = self._next_tts_sentence(
                                full_text, tts_sent_chars, allow_soft_break=tts_sentence_idx == 0
                            )
                            tts_sent_chars = new_chars
                            if not sentence:
                                break
                            tts_sentence_tasks.append(asyncio.create_task(synth(sentence, tts_sentence_idx)))
                            tts_sentence_idx += 1
                elif et == "final":
                    full_text = event.get("content", full_text)
                    break
        except AgentGatewayError:
            entry = self._prepared_speeches.get(key)
            if entry and entry.get("speech_id") == speech_id:
                entry["status"] = "failed"
            return

        remaining_tail = full_text[tts_sent_chars:].strip()
        if remaining_tail and tts_enabled:
            tts_sentence_tasks.append(asyncio.create_task(synth(remaining_tail, tts_sentence_idx)))
            tts_sentence_idx += 1
        expected = tts_sentence_idx
        if tts_sentence_tasks:
            await asyncio.gather(*tts_sentence_tasks, return_exceptions=True)

        # 用与 live 相同口径补齐缺口，得到最终 skipped 集合（[0,expected) 内未归档者）。
        tmp_speech = {"id": speech_id, "tts_skipped_sentences": []}
        self._reconcile_tts_gaps(tmp_speech, expected)
        skipped = list(tmp_speech["tts_skipped_sentences"])

        entry = self._prepared_speeches.get(key)
        if not entry or entry.get("speech_id") != speech_id:
            return  # 被清理/抢占，丢弃本次结果
        entry.update(
            {
                "full_text": full_text,
                "expected_sentence_count": expected,
                "skipped_sentences": skipped,
                "status": "ready",
            }
        )

    def _take_prepared_speech(self, speaker_id: str, mode: str) -> Optional[Dict[str, Any]]:
        """命中且有效则弹出缓存条目；否则返回 None（→ 走 live）。"""
        if not self._prefetch_enabled():
            return None
        is_self_intro = mode == "self_intro"
        phase_id = None if is_self_intro else self.snapshot["match"]["current_phase_id"]
        key = self._prepared_key(speaker_id, mode, phase_id)
        entry = self._prepared_speeches.get(key)
        if not entry or entry.get("status") != "ready":
            return None
        if not is_self_intro and entry.get("history_fp") != self._history_fingerprint():
            self._prepared_speeches.pop(key, None)  # 历史已变，缓存失效
            return None
        return self._prepared_speeches.pop(key)

    async def _activate_prepared_speech(self, prepared: Dict[str, Any], speaker: Dict[str, Any], is_self_intro: bool) -> None:
        """用缓存数据复刻 run_agent_speech 的状态落点（不调 agent/TTS）。音频已在预取时归档。"""
        speaker_id = speaker["id"]
        speech_id = prepared["speech_id"]
        task_id = prepared["task_id"]
        full_text = prepared.get("full_text", "")
        expected = int(prepared.get("expected_sentence_count", 0))
        skipped = list(prepared.get("skipped_sentences", []))
        tts_enabled = self._tts_formal_enabled()
        ready_count = max(0, expected - len(skipped))
        all_tts_failed = bool(tts_enabled and expected > 0 and ready_count == 0)

        # 先把仍在进行的上一段定稿（同 live run_agent_speech 开头）。
        async with self._lock:
            current = self.snapshot.get("current_speech")
            if current and current.get("id") and current.get("speaker_id") != speaker_id:
                self._finalize_current_speech_for_history()
                self._persist_snapshot()

        async with self._lock:
            phase_id = self.snapshot["match"]["current_phase_id"]
            self.snapshot["match"]["live_mode"] = "prep"
            self._clear_flow_state()
            speech = {
                "id": speech_id,
                "phase_id": phase_id,
                "speaker_id": speaker_id,
                "side": speaker["side"],
                "turn_index": self._current_turn_index(),
                "source": "agent_text",
                "kind": "self_intro" if is_self_intro else "speech",
                "content_final": full_text,
                "content_partial": full_text,
                "started_at": None,
                "state": "thinking",
                "tts_task_id": task_id if tts_enabled else None,
                "tts_expected_sentences": expected if tts_enabled else 0,
                "tts_created_sentences": expected,
                "tts_ready_sentences": ready_count,
                "tts_skipped_sentences": skipped,
                "tts_played_sentence_indices": [],
                "agent_final_ready": True,
            }
            self.snapshot["current_speech"] = speech
            # 正式发言：文本即刻定稿入历史（与 live agent.speech.final 处一致）；
            # 自我介绍：仅作非定稿展示，complete 时再排除出历史。
            self._upsert_transcript_segment(speech, speaker_id, full_text, not is_self_intro, "agent_text")
            if tts_enabled and not all_tts_failed:
                self.snapshot["speech_service"]["tts"] = {
                    "status": "playing",
                    "latency_ms": self.snapshot["speech_service"]["tts"].get("latency_ms", 0),
                    "queue_size": max(0, ready_count),
                    "speaker_id": speaker_id,
                    "detail": "prepared TTS playback pending",
                    "last_progress_at": iso_now(),
                }
            self._set_agent_status(speaker_id, "streaming", "prepared speech activated")
            self._persist_snapshot()

        await self.emit(
            "speaker.activated",
            {"speaker_id": speaker_id, "side": speaker["side"], "speaker_type": "agent"},
            "system",
        )
        await self.emit(
            "agent.speech.final",
            {"task_id": task_id, "speech_id": speech_id, "speaker_id": speaker_id, "content": full_text},
            "agent",
            speaker_id,
        )
        finished_payload = {
            "task_id": task_id,
            "speech_id": speech_id,
            "speaker_id": speaker_id,
            "expected_sentence_count": expected,
        }
        await self.emit("tts.finished", finished_payload, "system")
        if all_tts_failed:
            await self.complete_agent_playback(speech_id, task_id, reason="tts_failed")
        elif not tts_enabled or expected == 0:
            await self.start_agent_playback(speech_id, task_id, reason="text_only")
            await self.complete_agent_playback(speech_id, task_id, reason="text_only")
        else:
            self._arm_tts_playback_grace(speech_id, task_id, expected)

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
        cmd_budget = self._agent_output_budget(60, self._speaker_speech_rate(speaker))
        payload = {
            "model_name": (config or {}).get("model_name", speaker.get("model_name") or ""),
            "debater_name": speaker["name"],
            "debate_position": self._seat_label(speaker["seat"]),
            "debate_topic": match["topic"],
            "current_stage": command_text,
            "next_stage": phase.get("name", ""),
            "holder": "正方" if speaker["side"] == "affirmative" else "反方",
            "debate_history": self._build_debate_history(),
            "max_token": cmd_budget["max_token"],
            "other_info": {
                "command": command_text,
                "prompt": prompt_text,
                "match_id": match["id"],
                "speaker_id": speaker_id,
                "side": speaker["side"],
                "seat": speaker["seat"],
                "speech_rate": cmd_budget["speech_rate"],
                "chars_per_second": cmd_budget["chars_per_second"],
                "char_budget": cmd_budget["char_budget"],
            },
            "match_id": match["id"],
            "task_id": task_id,
            "speech_id": None,
            "speaker_id": speaker_id,
            "agent_config_id": speaker.get("agent_config_id"),
            "agent_provider_type": (config or {}).get("provider_type"),
            "time_limit_seconds": 60,
            "remaining_seconds": 60,
            "target_chars": cmd_budget["target_chars"],
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
                    "content_final": speech.get("content_final", ""),
                    "content_partial": speech.get("content_partial", ""),
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
            speech["ended_reason"] = "speaker_stop"
            self._upsert_transcript_segment(speech, speaker_id, text, True, speech.get("source", "human_asr"))
            self.snapshot["current_speech"] = None
            self._pause_running_clocks()
            self._advance_free_debate_turn_if_needed(speaker["side"])
            self._set_flow_waiting_after_speech_end(speech, speaker, "speaker_stop")
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
            # 复位按钮：既能重置进行中的发言，也能对“刚结束/发言完毕”的上一段发言生效。
            if not speech:
                speech = self._last_resettable_speech()
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
            # 复位：丢弃本次发言信息，并把该辩手恢复为可重新开始发言的就绪状态。
            if speaker.get("speaker_type") == "agent":
                self._set_agent_status(speaker_id, "ready", "speech reset")
            self.snapshot["speech_service"]["tts"] = {
                "status": "idle",
                "latency_ms": self.snapshot["speech_service"]["tts"].get("latency_ms", 0),
                "queue_size": 0,
                "speaker_id": None,
                "detail": "speech reset",
            }
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

    def _last_resettable_speech(self) -> Optional[Dict[str, Any]]:
        """Reconstruct a minimal speech for the most-recently-ended turn so the 复位
        button works after a speech has already ended (current_speech is None)."""
        flow = self.snapshot.get("flow") or {}
        target_id = flow.get("speech_id")
        segment = None
        for seg in self.snapshot.get("recent_transcript", []):
            if target_id and seg.get("speech_id") == target_id:
                segment = seg
                break
        if segment is None:
            segment = next((seg for seg in self.snapshot.get("recent_transcript", []) if seg.get("is_final")), None)
        if not segment:
            return None
        speaker_id = segment.get("speaker_id")
        try:
            speaker = self._find_speaker(speaker_id)
        except MatchStateError:
            return None
        return {
            "id": segment.get("speech_id") or segment.get("id"),
            "speaker_id": speaker_id,
            "side": speaker["side"],
            "phase_id": segment.get("phase_id") or self.snapshot["match"]["current_phase_id"],
        }

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
            # stop 后补传/网络重试：该发言音频已归档完成时，迟到分片不能把资产状态打回 recording、
            # 更不能重开一路对已结束发言的实时 ASR。良性忽略（返回当前资产摘要，不报错、不污染状态）。
            existing = self._audio_asset_for_speech(speech_id)
            if existing and existing.get("status") == "completed":
                return {
                    "audio_asset_id": existing["id"],
                    "speech_id": speech_id,
                    "speaker_id": existing.get("speaker_id") or speaker_id,
                    "chunk_index": chunk_index,
                    "chunk_count": existing.get("chunk_count", 0),
                    "size_bytes": existing.get("size_bytes", 0),
                    "file_path": "",
                    "pcm_ready": False,
                    "ignored_after_complete": True,
                }
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

        await self.emit("audio.chunk_archived", payload, "speech", speaker_id)
        if payload["pcm_ready"]:
            await self.emit("asr.audio_chunk_received", payload, "speech", speaker_id)
            await self._send_live_asr_chunk(speech_id, speaker_id, content, mime_type)
        return payload

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
        # ASR 收尾要等服务商返回 final 转写(还叠加发送限速的 send_budget)，慢时可能数秒。给一个短上限：
        # 常态(ASR 很快返回)仍同步拿到 final；一旦超过上限就转后台继续(shield 保护不被取消)，
        # 不让 /audio/complete 长时间挂起——否则人类辩手点「结束发言」后会长时间转圈。
        finish_task = asyncio.create_task(self._finish_live_asr_stream_bg(speech_id, asset["speaker_id"]))
        try:
            await asyncio.wait_for(asyncio.shield(finish_task), timeout=self._asr_finish_grace_seconds())
        except asyncio.TimeoutError:
            pass  # finish_task 仍在后台跑，final 转写就绪后异步写回快照并广播
        return result

    async def _finish_live_asr_stream_bg(self, speech_id: str, speaker_id: str) -> None:
        """ASR 收尾：吞掉一切异常，绝不把未捕获异常抛进 event loop。"""
        try:
            await self._finish_live_asr_stream(speech_id, speaker_id)
        except Exception:  # noqa: BLE001 — 收尾失败不应影响主流程
            pass

    def _asr_finish_grace_seconds(self) -> float:
        raw = os.getenv("PHDEBATE_ASR_FINISH_GRACE_S", "2.5").strip()
        try:
            value = float(raw)
        except ValueError:
            value = 2.5
        return max(0.5, min(15.0, value))

    async def should_auto_recognize_audio_archive(self, speech_id: str) -> bool:
        if not self._asr_auto_recognize_enabled():
            return False
        async with self._lock:
            asset = self._audio_asset_for_speech(speech_id)
            # 始终用"完整、按 chunk_index 顺序"的归档音频做一次批量识别作为权威终稿。
            # 实时流是按 HTTP 分片【到达顺序】喂给 ASR 的，长发言下分片并发上传可能乱序/丢失，
            # 导致实时转写只覆盖一部分内容（现场反馈"只转录一部分"）。归档批量识别按序拼接整段
            # 音频一次性识别，最稳，故不再因"实时流已完成"而跳过。仅 PCM/L16 可直接识别；
            # webm 回退（少数浏览器无 PCM 采集）保持实时结果，不在此重识别。
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
            try:
                selection = select_asr_gateway()
            except SpeechProviderError as exc:
                await self._record_live_asr_failed(speech_id, speaker_id, _speech_error_message(exc), _speech_error_code(exc, "asr_config_error"))
                return
            gateway = selection.gateway

            async def on_partial(text: str, latency_ms: int, chunk_count: int) -> None:
                await self._record_live_asr_text(speech_id, speaker_id, text, False, latency_ms, chunk_count)

            async def on_final(text: str, latency_ms: int, chunk_count: int) -> None:
                await self._record_live_asr_text(speech_id, speaker_id, text, True, latency_ms, chunk_count)

            async def on_error(exc: Exception) -> None:
                await self._record_live_asr_failed(speech_id, speaker_id, _speech_error_message(exc), _speech_error_code(exc, "asr_stream_error"))

            async with self._lock:
                self.repo.save_speech_service_request_started(
                    match_id=self.snapshot["match"]["id"],
                    request_id=request_id,
                    service="asr",
                    operation="realtime_stream",
                    speech_id=speech_id,
                    speaker_id=speaker_id,
                    request={"speech_id": speech_id, "speaker_id": speaker_id, "mime_type": mime_type, "provider": selection.provider},
                    started_at=stream_started_at,
                    origin="live",
                    **self._log_context(),
                )
            try:
                session = await gateway.open_stream(on_partial=on_partial, on_final=on_final, on_error=on_error, **selection.options)
            except SpeechProviderError as exc:
                await self._record_live_asr_failed(speech_id, speaker_id, _speech_error_message(exc), _speech_error_code(exc, "asr_stream_error"))
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
                    "detail": f"{selection.provider} realtime ASR stream started · {speech_id}",
                }
                self._persist_snapshot()
            await self.emit("asr.stream_started", {"speech_id": speech_id, "speaker_id": speaker_id}, "speech", speaker_id)
        try:
            await session.send_audio(content)
        except SpeechProviderError as exc:
            self._asr_streams.pop(speech_id, None)
            await self._record_live_asr_failed(speech_id, speaker_id, _speech_error_message(exc), _speech_error_code(exc, "asr_stream_error"))

    async def _finish_live_asr_stream(self, speech_id: str, speaker_id: str) -> None:
        session = self._asr_streams.pop(speech_id, None)
        if not session:
            return
        try:
            result = await session.finish()
        except SpeechProviderError as exc:
            await self._record_live_asr_failed(speech_id, speaker_id, _speech_error_message(exc), _speech_error_code(exc, "asr_stream_error"))
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

        try:
            selection = select_asr_gateway()
            options = {**selection.options, "audio_format": "audio/L16;rate=16000", "encoding": "raw"}
            result = await selection.gateway.recognize(content, **options)
        except SpeechProviderError as exc:
            message = _speech_error_message(exc)
            code = _speech_error_code(exc, "asr_error")
            async with self._lock:
                self.snapshot["speech_service"]["asr"] = {
                    "status": "failed",
                    "latency_ms": 0,
                    "active_sessions": 0,
                    "detail": message,
                }
                self.repo.finish_speech_service_request(
                    match_id=self.snapshot["match"]["id"],
                    request_id=request_id,
                    status="failed",
                    error_code=code,
                    error_message=message,
                    latency_ms=max(0, int((time.perf_counter() - request_started_time) * 1000)),
                    completed_at=iso_now(),
                )
                self._persist_snapshot()
            await self.emit("asr.failed", {"speech_id": speech_id, "reason": message, "code": code}, "host")
            raise MatchStateError("speech_service_error", f"ASR 归档识别失败：{message}", {"code": code})

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
                "detail": "ASR probe started",
            }
            self._persist_snapshot()
        await self.emit("asr.probe_started", {"audio_bytes": len(content), "format": audio_format, "encoding": encoding}, "host")

        try:
            selection = select_asr_gateway()
            options = {**selection.options, "audio_format": audio_format, "encoding": encoding}
            result = await selection.gateway.recognize(content, **options)
        except SpeechProviderError as exc:
            message = _speech_error_message(exc)
            code = _speech_error_code(exc, "asr_error")
            async with self._lock:
                self.snapshot["speech_service"]["asr"] = {
                    "status": "failed",
                    "latency_ms": 0,
                    "active_sessions": 0,
                    "detail": message,
                }
                self.repo.finish_speech_service_request(
                    match_id=self.snapshot["match"]["id"],
                    request_id=request_id,
                    status="failed",
                    error_code=code,
                    error_message=message,
                    latency_ms=max(0, int((time.perf_counter() - request_started_time) * 1000)),
                    completed_at=iso_now(),
                )
                self._persist_snapshot()
            await self.emit("asr.failed", {"probe": True, "reason": message, "code": code}, "host")
            raise MatchStateError("speech_service_error", f"ASR 试识别失败：{message}", {"code": code})

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

    async def probe_tts(self, text: str, voice_preset_id: str = "") -> Dict[str, Any]:
        content = str(text or "").strip() or "人机辩论赛语音合成自检。"
        request_id = self._new_speech_service_request_id("tts", "probe")
        request_started_time = time.perf_counter()
        selection = select_tts_gateway(voice_preset_id=voice_preset_id)
        async with self._lock:
            self.repo.save_speech_service_request_started(
                match_id=self.snapshot["match"]["id"],
                request_id=request_id,
                service="tts",
                operation="probe",
                request={
                    "text": content,
                    "text_length": len(content),
                    "provider": selection.provider,
                    "voice_preset_id": (selection.preset or {}).get("id"),
                },
                started_at=iso_now(),
                origin="test",
                **self._log_context(),
            )
            self.snapshot["speech_service"]["tts"] = {
                "status": "synthesizing",
                "latency_ms": 0,
                "queue_size": 1,
                "speaker_id": None,
                "detail": f"{selection.provider} TTS probe started",
            }
            self._persist_snapshot()
        await self.emit("tts.started", {"probe": True, "text_length": len(content)}, "host")

        try:
            result = await selection.gateway.synthesize(content, **selection.options)
        except SpeechProviderError as exc:
            message = _speech_error_message(exc)
            code = _speech_error_code(exc, "tts_error")
            async with self._lock:
                self.snapshot["speech_service"]["tts"] = {
                    "status": "failed",
                    "latency_ms": 0,
                    "queue_size": 0,
                    "speaker_id": None,
                    "detail": message,
                    "degraded_to": "text_only",
                }
                self.repo.finish_speech_service_request(
                    match_id=self.snapshot["match"]["id"],
                    request_id=request_id,
                    status="failed",
                    error_code=code,
                    error_message=message,
                    latency_ms=max(0, int((time.perf_counter() - request_started_time) * 1000)),
                    completed_at=iso_now(),
                )
                self._persist_snapshot()
            await self.emit("tts.failed", {"probe": True, "reason": message, "code": code}, "host")
            raise MatchStateError("speech_service_error", f"TTS 试合成失败：{message}", {"code": code})

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
            "provider": selection.provider,
            "voice_preset_id": (selection.preset or {}).get("id"),
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
                text = speech.get("content_final") or speech.get("content_partial") or "时间到，本次发言结束。"
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
                if speech.get("source") == "agent_text" and speech.get("tts_task_id"):
                    self.snapshot["speech_service"]["tts"] = {
                        "status": "idle",
                        "latency_ms": self.snapshot["speech_service"]["tts"].get("latency_ms", 0),
                        "queue_size": 0,
                        "speaker_id": None,
                        "detail": "timeout",
                    }
                timeout_payload = {
                    "speech_id": speech["id"],
                    "task_id": speech.get("tts_task_id"),
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
                events.append(
                    (
                        "speech.ended",
                        {
                            "speech_id": timeout_payload["speech_id"],
                            "task_id": timeout_payload.get("task_id"),
                            "speaker_id": timeout_payload["speaker_id"],
                            "side": timeout_payload["side"],
                            "reason": "timeout",
                        },
                    )
                )
            if flow_payload and flow_payload.get("awaiting_host_confirm"):
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
                # 兼容：新版投票带 ranking（8 人排序），best_speaker_id 取排名第一。
                ranking = [str(s) for s in (body.get("ranking") or [])]
                if ranking and not body.get("best_speaker_id"):
                    body["best_speaker_id"] = ranking[0]
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
                            "ranking": ranking,
                            "created_at": iso_now(),
                        }
                    )
                    self._append_audience_summary(body)
            else:
                judge_summary = self._build_judge_summary(body)
                self.snapshot["vote_state"]["judge_summary"] = judge_summary
                self.snapshot["vote_state"]["winner_side"] = judge_summary["winner_side"]
                self.snapshot["vote_state"]["best_speaker_id"] = judge_summary["best_speaker_id"]
                # 小七结果录入（获胜方 + 最佳辩手）完成标记：大屏切「小七评判」前必须先录入。
                if str(body.get("scope") or "") == "xiaoqi":
                    self.snapshot["vote_state"]["xiaoqi_recorded"] = True
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
        """API 请求日志列表：只返回摘要，完整输入/输出由单条详情接口按需读取。"""
        match_id = self.snapshot["match"]["id"]
        return {
            "match_id": match_id,
            "agent_requests": self.repo.load_agent_request_summaries(match_id, limit),
            "speech_service_requests": self.repo.load_speech_service_request_summaries(match_id, limit),
            "audit_logs": self.repo.load_audit_log_summaries(match_id, limit),
        }

    def get_request_log_detail(self, kind: str, row_id: str) -> Optional[Dict[str, Any]]:
        """完整单条日志详情，用于管理端展开行时按需加载。"""
        match_id = self.snapshot["match"]["id"]
        normalized = str(kind or "").strip().lower()
        if normalized == "agent":
            return self.repo.load_agent_request(match_id, row_id)
        if normalized in {"speech", "xiaoqi"}:
            return self.repo.load_speech_service_request(match_id, row_id)
        if normalized == "audit":
            return self.repo.load_audit_log(match_id, row_id)
        return None

    async def websocket(self, websocket: WebSocket, last_seq: int = 0, channel: str = "screen", speaker_id: Optional[str] = None) -> None:
        await websocket.accept()
        snapshot = await self.get_snapshot()
        queue: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue(maxsize=self._broadcast_queue_maxsize())
        # 在锁内「登记连接 + 把初始快照塞进队列」原子完成：此后任何 emit 都只会把后续事件
        # 排在快照之后(seq 递增)，既不丢事件也不会有事件抢在快照前面。初始快照也走队列，
        # 由唯一的发送协程发出——避免两个协程并发 send_json 同一个连接(会撕裂帧/报错)。
        async with self._lock:
            missed = [event for event in self.events if event["seq"] > last_seq]
            queue.put_nowait(
                {
                    "type": "snapshot",
                    "match_id": snapshot["match"]["id"],
                    "seq": self.seq,
                    "server_time_ms": int(utc_now().timestamp() * 1000),
                    "payload": {"state": snapshot, "missed_events": missed},
                }
            )
            self._connections.add(websocket)
            self._conn_send_queues[websocket] = queue
            self._conn_senders[websocket] = asyncio.create_task(self._connection_sender(websocket, queue))
        try:
            while True:
                try:
                    text = await websocket.receive_text()
                    await self._handle_client_message(text, channel, speaker_id)
                except (WebSocketDisconnect, RuntimeError):
                    break
                except Exception:
                    continue
        finally:
            self._drop_connection(websocket)
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

    def _broadcast_queue_maxsize(self) -> int:
        raw = os.getenv("PHDEBATE_BROADCAST_QUEUE_MAX", "512").strip()
        try:
            value = int(raw)
        except ValueError:
            value = 512
        return max(32, min(8192, value))

    def _broadcast(self, message: Dict[str, Any]) -> None:
        """非阻塞广播：把事件塞进每个连接自己的发送队列(纯内存，O(连接数)，无 await)。
        队列满 = 该客户端消费不过来(网络慢/卡)，直接丢弃该连接——它会用 last_seq 重连并
        全量重同步，绝不拖累其它客户端或持锁的比赛流程。"""
        stale: List[WebSocket] = []
        for websocket, queue in list(self._conn_send_queues.items()):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                stale.append(websocket)
        for websocket in stale:
            self._drop_connection(websocket)

    def _drop_connection(self, websocket: WebSocket) -> None:
        """注销连接并停掉它的发送协程(协程在 finally 里关闭 socket)。同步、可重入、无 await。"""
        self._connections.discard(websocket)
        self._conn_send_queues.pop(websocket, None)
        task = self._conn_senders.pop(websocket, None)
        if task is not None and not task.done():
            task.cancel()

    async def _connection_sender(self, websocket: WebSocket, queue: "asyncio.Queue[Dict[str, Any]]") -> None:
        """单连接发送协程：串行从自己的队列取消息发出，与其它连接、与比赛主流程完全解耦。"""
        try:
            while True:
                message = await queue.get()
                try:
                    await websocket.send_json(message)
                except Exception:
                    break
        except asyncio.CancelledError:
            pass
        finally:
            self._connections.discard(websocket)
            self._conn_send_queues.pop(websocket, None)
            self._conn_senders.pop(websocket, None)
            try:
                await websocket.close()
            except Exception:
                pass

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

    def _persist_snapshot(self, *, sync_structured: bool = True) -> None:
        self._ensure_runtime_fields()
        self.snapshot["last_seq"] = self.seq
        self.repo.save_snapshot(self.snapshot, iso_now(), sync_structured=sync_structured)

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
            "xiaoqi_recorded": False,
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
        endpoint = speaker.get("agent_endpoint") or self._agent_endpoint_for_speaker(str(speaker.get("id") or ""))
        return {
            "id": speaker.get("agent_config_id") or self._default_agent_config_id(str(speaker.get("id") or "")),
            "name": f"{speaker.get('name') or '未命名'} Agent",
            "provider_type": "rest_api",
            "request_method": "POST",
            "model_name": speaker.get("model_name") or "qwen3.6-plus",
            "model_id": speaker.get("model_id") or "qwen3.6-plus",
            "model_kind": speaker.get("model_kind") or "closed_source",
            "endpoint": endpoint or "",
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "api_key_env": "",
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
        # 大屏左上角/右上角的「比赛名称」「主办机构」支持文本或图片两种展示方式。
        match = self.snapshot.setdefault("match", {})
        match.setdefault("title_display", "text")
        match.setdefault("title_image_url", "")
        match.setdefault("organizer_image_url", "/assets/logo-full-white.png")
        match.setdefault("organizer_display", "image" if match.get("organizer_image_url") else "text")
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
        self.snapshot["vote_state"].setdefault("xiaoqi_recorded", False)
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
        # Pass 1: ensure every agent speaker has a config entry (create defaults if missing).
        for speaker in self.snapshot.get("speakers", []):
            if speaker.get("speaker_type") == "human":
                speaker.setdefault("mic_permission", "unknown")
                speaker.setdefault("device_label", None)
                speaker.setdefault("last_seen_at", None)
                speaker.pop("agent_config_id", None)
                speaker.pop("tts_voice_preset_id", None)
            else:
                speaker.setdefault("mic_permission", None)
                speaker.setdefault("device_label", None)
                speaker.setdefault("last_seen_at", None)
                speaker.setdefault("tts_voice_preset_id", None)
                speaker.setdefault("agent_config_id", self._default_agent_config_id(str(speaker.get("id") or "")))
                if speaker["agent_config_id"] not in config_ids:
                    config = self._agent_config_from_speaker(speaker, now)
                    self.snapshot["agent_configs"].append(config)
                    config_ids.add(config["id"])
        # Pass 2: normalize + migrate all configs before syncing back to speakers.
        normalized_configs = []
        for config in self.snapshot.get("agent_configs", []):
            normalized = self._normalize_agent_config_fields(config, existing=config, now=now, create=False)
            normalized["id"] = str(config.get("id") or self._unique_agent_config_id(normalized["name"]))
            # Migrate openai_sdk configs whose API key is not present at runtime to rest_api,
            # so the admin page reflects what will actually execute and agents don't silently fail.
            if normalized.get("provider_type") == "openai_sdk":
                api_key_env = normalized.get("api_key_env", "").strip()
                api_key_available = bool(api_key_env and os.getenv(api_key_env, "").strip())
                if not api_key_available:
                    normalized["provider_type"] = "rest_api"
                    if not normalized.get("endpoint"):
                        normalized["endpoint"] = (
                            os.getenv("PHDEBATE_AGENT_BASE_URL", "").strip() or "http://localhost:8000/api/debate"
                        )
            normalized_configs.append(normalized)
        self.snapshot["agent_configs"] = normalized_configs
        # Pass 3: sync migrated/normalized config fields back to each agent speaker.
        for speaker in self.snapshot.get("speakers", []):
            if speaker.get("speaker_type") == "agent":
                self._apply_agent_config_to_speaker(speaker)
        current_phase_id = self.snapshot.get("match", {}).get("current_phase_id")
        for segment in self.snapshot.get("recent_transcript", []):
            segment.setdefault("phase_id", current_phase_id)
            segment.setdefault("speech_id", segment.get("id"))
            segment.setdefault("turn_index", None)
            segment.setdefault("valid", True)
            segment.setdefault("invalid_reason", None)
        self.snapshot.setdefault("speech_revisions", [])
        self.snapshot.setdefault("audio_assets", [])
        self._ensure_audio_asset_urls()
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

    def _ensure_audio_asset_urls(self) -> None:
        try:
            audio_root = self.audio_root_path()
        except Exception:
            return
        for asset in self.snapshot.get("audio_assets", []):
            for chunk in asset.get("chunks") or []:
                if chunk.get("audio_url"):
                    continue
                raw_path = str(chunk.get("file_path") or "").strip()
                if not raw_path:
                    continue
                try:
                    rel = Path(raw_path).resolve().relative_to(audio_root)
                except (OSError, ValueError):
                    continue
                chunk["audio_url"] = "/api/audio/" + "/".join(rel.parts)

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

        # 辩手排行用 Borda 计分聚合：一票里排第 1 名得 N 分、第 2 名得 N-1 分……依次递减，
        # 跨所有票累加；按总分排序，第一名即"最佳辩手"。兼容旧版只含 best_speaker_id 的票（+1 分）。
        points: Dict[str, int] = {
            str(item["speaker_id"]): int(item.get("count", 0)) for item in summary.get("best_speaker", [])
        }
        ranking = [str(s) for s in (body.get("ranking") or [])]
        if ranking:
            n = len(ranking)
            for idx, sid in enumerate(ranking):
                points[sid] = points.get(sid, 0) + (n - idx)
        elif body.get("best_speaker_id"):
            sid = str(body["best_speaker_id"])
            points[sid] = points.get(sid, 0) + 1
        summary["best_speaker"] = sorted(
            ({"speaker_id": sid, "count": pts} for sid, pts in points.items()),
            key=lambda item: item["count"],
            reverse=True,
        )
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

    def _history_speaker_label(self, segment: Dict[str, Any]) -> str:
        """Side+seat label (e.g. 「正方一辩」) matching the agent request sample.

        Falls back to the stored label (stripping a trailing「 · 姓名」) if the speaker
        is no longer present in the roster.
        """
        speaker_id = segment.get("speaker_id")
        if speaker_id:
            try:
                speaker = self._find_speaker(speaker_id)
                side = "正方" if speaker["side"] == "affirmative" else "反方"
                return f"{side}{self._seat_label(speaker['seat'])}"
            except Exception:
                pass
        label = str(segment.get("speaker_label", ""))
        return label.split(" · ", 1)[0] if " · " in label else label

    def _build_debate_history(self) -> list:
        """Group final+valid transcript segments by phase, return ordered list.

        Shape matches 请求体(1).json: ``[{"stage": ..., "message": [{"speaker", "content"}]}]``.
        Self-introductions (`exclude_from_history`) are recorded/displayed but never sent
        back to the agent as conversation history.
        """
        phase_by_id = {p["id"]: p["name"] for p in self.snapshot["phases"]}
        ordered_phase_ids = [p["id"] for p in sorted(self.snapshot["phases"], key=lambda x: x["display_order"])]
        groups: Dict[str, list] = {}
        # recent_transcript 以"最新在前"存储；按时间正序（最早在前）分组，否则同一环节内多条发言
        # （目前只有自由辩论会出现多条）会被倒序，导致 agent 收到的对话顺序反了——最新一句跑到最前。
        for segment in reversed(self.snapshot.get("recent_transcript", [])):
            if not segment.get("valid", True) or not segment.get("is_final"):
                continue
            if segment.get("exclude_from_history"):
                continue
            pid = segment.get("phase_id", "")
            if pid not in groups:
                groups[pid] = []
            groups[pid].append({
                "speaker": self._history_speaker_label(segment),
                "content": segment["text"],
            })
        return [
            {"stage": phase_by_id.get(pid, pid), "message": groups[pid]}
            for pid in ordered_phase_ids
            if pid in groups
        ]

    def build_match_record(self) -> list:
        """Public accessor for the 小七 `match_record/update` payload.

        Identical shape to the agent's ``debate_history`` (grouped by stage). Pushed
        to 小七 on manual request so it can comment/judge/vote against the full
        transcript of the active match."""
        return self._build_debate_history()

    def _build_agent_payload(
        self, task_id: str, speech_id: str, speaker: Dict[str, Any], phase_override: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        # phase_override：用于"提前预取下一环节发言"——此时当前环节尚未切换，需按目标环节构建
        # 角色/限时等字段。默认 None=当前环节，行为与原来完全一致。预取只用于固定单人环节。
        phase = phase_override or self._current_phase()
        match = self.snapshot["match"]
        config = self._agent_config_for_speaker(speaker) or {}
        time_limit = self._free_turn_seconds(phase) if phase["phase_type"] == "free_debate" else phase["duration_seconds"]
        # 预取目标环节尚未开始，其计时器还没跑，剩余时间即满额限时。
        clock = None if phase_override else self._clock("turn" if phase["phase_type"] == "free_debate" else "main")
        remaining_seconds = int((clock["remaining_ms"] if clock else time_limit * 1000) / 1000)
        next_phase = self._next_phase(phase)
        holder = "正方" if speaker["side"] == "affirmative" else "反方"
        speech_rate = self._speaker_speech_rate(speaker)
        budget = self._agent_output_budget(time_limit, speech_rate)
        return {
            # 结构化辩论格式（Agent 接口核心字段，与 请求体(1).json 对齐）
            "model_name": config.get("model_name", ""),
            "debater_name": speaker["name"],
            "debate_position": self._seat_label(speaker["seat"]),
            "debate_topic": match["topic"],
            "current_stage": phase["name"],
            "next_stage": next_phase["name"] if next_phase else "比赛结束",
            "holder": holder,
            "debate_history": self._build_debate_history(),
            # 输出预算：由本次发言限时 + TTS 语速确定性推导，约束 Agent 回复不超时
            "max_token": budget["max_token"],
            "other_info": {
                "match_id": match["id"],
                "speaker_id": speaker["id"],
                "side": speaker["side"],
                "phase_type": phase["phase_type"],
                "remaining_seconds": remaining_seconds,
                "time_limit_seconds": time_limit,
                "speech_rate": budget["speech_rate"],
                "chars_per_second": budget["chars_per_second"],
                "char_budget": budget["char_budget"],
            },
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
            "target_chars": budget["target_chars"],
            "output": {"stream": True, "language": "zh-CN"},
        }

    def _build_self_intro_payload(self, task_id: str, speech_id: str, speaker: Dict[str, Any]) -> Dict[str, Any]:
        """Pre-match self-introduction request. Not tied to a debate clock, and the
        result is excluded from debate_history (handled at completion)."""
        match = self.snapshot["match"]
        config = self._agent_config_for_speaker(speaker) or {}
        first_phase = next(iter(sorted(self.snapshot["phases"], key=lambda x: x["display_order"])), None)
        time_limit = 40
        speech_rate = self._speaker_speech_rate(speaker)
        budget = self._agent_output_budget(time_limit, speech_rate)
        holder = "正方" if speaker["side"] == "affirmative" else "反方"
        return {
            "model_name": config.get("model_name", ""),
            "debater_name": speaker["name"],
            "debate_position": self._seat_label(speaker["seat"]),
            "debate_topic": match["topic"],
            "current_stage": "自我介绍",
            "next_stage": first_phase["name"] if first_phase else "正式比赛",
            "holder": holder,
            # 自我介绍不需要历史会话作为上下文。
            "debate_history": [],
            "task_type": "self_intro",
            "max_token": budget["max_token"],
            "other_info": {
                "match_id": match["id"],
                "speaker_id": speaker["id"],
                "side": speaker["side"],
                "phase_type": "self_intro",
                "time_limit_seconds": time_limit,
                "speech_rate": budget["speech_rate"],
                "chars_per_second": budget["chars_per_second"],
                "char_budget": budget["char_budget"],
            },
            "match_id": match["id"],
            "task_id": task_id,
            "speech_id": speech_id,
            "speaker_id": speaker["id"],
            "agent_config_id": speaker.get("agent_config_id"),
            "agent_provider_type": config.get("provider_type"),
            "time_limit_seconds": time_limit,
            "remaining_seconds": time_limit,
            "target_chars": budget["target_chars"],
            "output": {"stream": True, "language": "zh-CN"},
        }

    async def start_agent_playback(self, speech_id: str, task_id: str, reason: str = "screen_playback_started") -> Dict[str, Any]:
        ignored_payload: Optional[Dict[str, Any]] = None
        started_payload: Optional[Dict[str, Any]] = None
        async with self._lock:
            speech = self.snapshot.get("current_speech") or {}
            if not speech or speech.get("id") != speech_id:
                ignored_payload = {"speech_id": speech_id, "task_id": task_id, "ignored": True, "reason": reason}
            elif speech.get("tts_task_id") and speech.get("tts_task_id") != task_id:
                ignored_payload = {"speech_id": speech_id, "task_id": task_id, "ignored": True, "reason": "task_mismatch"}
            elif speech.get("state") == "speaking":
                speaker_id = speech.get("speaker_id")
                started_payload = {"speech_id": speech_id, "task_id": task_id, "speaker_id": speaker_id, "already_started": True}
            if ignored_payload:
                self._persist_snapshot()
                awaitable_payload = ignored_payload
            else:
                speaker_id = speech["speaker_id"]
                speaker = self._find_speaker(speaker_id)
                phase_id = speech.get("phase_id") or self.snapshot["match"]["current_phase_id"]
                is_self_intro = speech.get("kind") == "self_intro"
                if not is_self_intro:
                    self.snapshot["match"]["live_mode"] = "free" if phase_id == "phase_free_debate" else "single"
                speech["started_at"] = speech.get("started_at") or iso_now()
                speech["state"] = "speaking"
                if task_id and not speech.get("tts_task_id"):
                    speech["tts_task_id"] = task_id
                # 自我介绍不消耗任何辩论计时。
                if not is_self_intro:
                    self._start_relevant_clocks(speaker["side"])
                self.snapshot["speech_service"]["tts"] = {
                    "status": "playing",
                    "latency_ms": self.snapshot["speech_service"]["tts"].get("latency_ms", 0) or 420,
                    "queue_size": self.snapshot["speech_service"]["tts"].get("queue_size", 1) or 1,
                    "speaker_id": speaker_id,
                    "detail": "TTS playing",
                }
                started_payload = started_payload or {"speech_id": speech_id, "task_id": task_id, "speaker_id": speaker_id, "reason": reason}
                self._persist_snapshot()
                awaitable_payload = started_payload
        if ignored_payload:
            await self.emit("tts.playback_start_ignored", ignored_payload, "screen")
            return await self.get_snapshot()
        await self.emit("tts.started", started_payload or awaitable_payload, "system", (started_payload or {}).get("speaker_id"))
        await self.emit("speech.started", started_payload or awaitable_payload, "agent", (started_payload or {}).get("speaker_id"))
        return await self.get_snapshot()

    async def record_tts_playback_progress(
        self,
        speech_id: str,
        task_id: str,
        sentence_idx: int,
        status: str = "playing",
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "speech_id": speech_id,
            "task_id": task_id,
            "sentence_idx": max(0, int(sentence_idx)),
            "status": status,
        }
        auto_complete_payload: Optional[Dict[str, Any]] = None
        async with self._lock:
            speech = self.snapshot.get("current_speech") or {}
            if speech.get("id") != speech_id:
                payload["ignored"] = True
                payload["reason"] = "speech_mismatch"
            elif speech.get("tts_task_id") and speech.get("tts_task_id") != task_id:
                payload["ignored"] = True
                payload["reason"] = "task_mismatch"
            else:
                expected = int(speech.get("tts_expected_sentences") or speech.get("tts_created_sentences") or 0)
                playing_idx = max(0, int(sentence_idx))
                if not isinstance(speech.get("tts_played_sentence_indices"), list):
                    legacy_played = max(0, int(speech.get("tts_played_sentences") or 0))
                    if status == "playing" and legacy_played > 0 and playing_idx == legacy_played - 1:
                        legacy_played -= 1
                    speech["tts_played_sentence_indices"] = list(range(legacy_played))
                if status in {"stalled", "error", "play_rejected", "failed", "skipped"}:
                    # 屏幕播放失败，只有在「后端确实没产出该段音频」时才允许把它永久标记为跳过。
                    # 若该段已有归档音频，则某一块屏幕一时播不出（旧缓存版本 / 网络抖动 / 解码失败）
                    # 绝不能把它拉黑，更不能据此提前收尾——否则任意一块异常屏幕都会把整场发言
                    # "播一句就结束"（线上实测：旧 bundle 的连环 error 把整段拖到 speech.ended）。
                    # 这类失败只作诊断，不改变权威的 ready/skipped 不变量。
                    if playing_idx not in self._tts_ready_indices(speech_id):
                        skipped = speech.setdefault("tts_skipped_sentences", [])
                        if playing_idx not in {int(i) for i in skipped}:
                            skipped.append(playing_idx)
                            skipped.sort()
                if status == "played":
                    played_indices = speech.setdefault("tts_played_sentence_indices", [])
                    if playing_idx not in {int(i) for i in played_indices}:
                        played_indices.append(playing_idx)
                        played_indices.sort()
                if speech.get("state") == "thinking" and status in {"playing", "played"}:
                    speaker_id = speech.get("speaker_id")
                    speaker = self._find_speaker(speaker_id)
                    phase_id = speech.get("phase_id") or self.snapshot["match"]["current_phase_id"]
                    if speech.get("kind") != "self_intro":
                        self.snapshot["match"]["live_mode"] = "free" if phase_id == "phase_free_debate" else "single"
                        self._start_relevant_clocks(speaker["side"])
                    speech["started_at"] = speech.get("started_at") or iso_now()
                    speech["state"] = "speaking"
                speech["tts_playing_sentence_idx"] = playing_idx
                speech["tts_last_playback_status"] = status
                # 只有「真实播放进度」(playing/played) 才推进高水位、刷新 last_progress_at。否则
                # 异常屏幕持续上报 error/stalled 会不断重置 grace 兜底计时，让发言永远收不了尾。
                if status in {"playing", "played"}:
                    speech["tts_played_sentences"] = max(int(speech.get("tts_played_sentences") or 0), playing_idx + 1)
                    speech["tts_last_progress_at"] = iso_now()
                else:
                    speech.setdefault("tts_last_progress_at", iso_now())
                if status == "played":
                    detail = f"screen played segment {playing_idx + 1}/{expected or '?'}"
                elif status in {"stalled", "error", "play_rejected", "failed"}:
                    detail = f"screen playback {status} at segment {playing_idx + 1}/{expected or '?'}"
                else:
                    detail = f"screen playing segment {playing_idx + 1}/{expected or '?'}"
                self.snapshot["speech_service"]["tts"].update(
                    {
                        "status": "playing",
                        "queue_size": self._tts_playback_display_queue_size(speech, fallback_total=expected),
                        "speaker_id": speech.get("speaker_id"),
                        "detail": detail,
                        "last_progress_at": speech["tts_last_progress_at"],
                    }
                )
                payload.update(
                    {
                        "speaker_id": speech.get("speaker_id"),
                        "played_sentences": speech.get("tts_played_sentences"),
                        "expected_sentence_count": expected or None,
                        "last_progress_at": speech.get("tts_last_progress_at"),
                    }
                )
                if self._should_auto_complete_tts_playback(speech, status):
                    payload["auto_complete"] = True
                    auto_complete_payload = {
                        "speech_id": speech_id,
                        "task_id": task_id,
                        "reason": "screen_playback_progress_exhausted",
                    }
            self._persist_snapshot()
        await self.emit("tts.playback_progress", payload, "screen", payload.get("speaker_id"))
        if auto_complete_payload:
            return await self.complete_agent_playback(
                auto_complete_payload["speech_id"],
                auto_complete_payload["task_id"],
                reason=auto_complete_payload["reason"],
            )
        return await self.get_snapshot()

    async def request_tts_playback_resume(self, speech_id: str, task_id: str = "", reason: str = "host_resume_tts") -> Dict[str, Any]:
        async with self._lock:
            speech = self.snapshot.get("current_speech") or {}
            if speech and speech.get("id") == speech_id and (not task_id or speech.get("tts_task_id") == task_id):
                speech["tts_resume_requested_at"] = iso_now()
                self.snapshot["speech_service"]["tts"].update(
                    {
                        "status": "synthesizing" if speech.get("state") == "thinking" else "playing",
                        "latency_ms": self.snapshot["speech_service"]["tts"].get("latency_ms", 0),
                        "queue_size": max(0, self._tts_remaining_playback_count(speech, fallback_total=1)),
                        "speaker_id": speech.get("speaker_id"),
                        "detail": "playback resume requested",
                        "last_progress_at": speech["tts_resume_requested_at"],
                    }
                )
                self._persist_snapshot()
            payload = {
                "speech_id": speech_id,
                "task_id": task_id or speech.get("tts_task_id"),
                "speaker_id": speech.get("speaker_id"),
                "reason": reason,
            }
        await self.emit("tts.playback_resume_requested", payload, "host", payload.get("speaker_id"))
        return await self.get_snapshot()

    async def request_tts_playback_stop(self, speech_id: str, task_id: str = "", reason: str = "host_stop_tts_audio") -> Dict[str, Any]:
        """Ask the screen to cut the live TTS audio source without ending the speech.

        This is a pure audio control: the speech and the overall match flow keep their
        state, only the projector's sound output is silenced (and can be resumed via
        ``request_tts_playback_resume``).
        """
        async with self._lock:
            speech = self.snapshot.get("current_speech") or {}
            payload = {
                "speech_id": speech_id,
                "task_id": task_id or speech.get("tts_task_id"),
                "speaker_id": speech.get("speaker_id"),
                "reason": reason,
            }
        await self.emit("tts.playback_stop_requested", payload, "host", payload.get("speaker_id"))
        return await self.get_snapshot()

    async def force_skip_sentence(self, speech_id: str, sentence_idx: int, reason: str = "host_force_skip") -> Dict[str, Any]:
        """操作员手动把卡住的某个分段标记为跳过，让大屏对账越过它继续播放。

        纯救援控制：把 idx 记进 tts_skipped_sentences（大屏 reducer 读快照即推进），
        并补发一个 skip 形态的 tts.sentence_ready 触发刷新。"""
        async with self._lock:
            self._ensure_match_allows_control("force_skip_sentence")
            speech = self.snapshot.get("current_speech") or {}
            if not speech or speech.get("id") != speech_id:
                raise MatchStateError("speech_not_found", "未找到要跳过的当前发言。", {"speech_id": speech_id})
            speaker_id = speech.get("speaker_id")
            task_id = speech.get("tts_task_id")
            skipped = speech.setdefault("tts_skipped_sentences", [])
            if int(sentence_idx) not in skipped:
                skipped.append(int(sentence_idx))
                skipped.sort()
            self.snapshot["speech_service"]["tts"].update(
                {
                    "queue_size": self._tts_remaining_playback_count(speech, fallback_total=int(sentence_idx) + 1),
                    "last_progress_at": iso_now(),
                }
            )
            self._persist_snapshot()
            payload = {
                "speech_id": speech_id,
                "task_id": task_id,
                "speaker_id": speaker_id,
                "sentence_idx": int(sentence_idx),
                "audio_url": "",
                "skipped": True,
                "reason": reason,
            }
            auto_complete = self._should_auto_complete_tts_playback(speech, "skipped")
        await self.emit("tts.sentence_ready", payload, "screen", speaker_id)
        if auto_complete:
            return await self.complete_agent_playback(speech_id, task_id, reason="host_force_skip_exhausted")
        return await self.get_snapshot()

    async def resynthesize_speech_tts(self, speech_id: str, reason: str = "host_resynthesize") -> Dict[str, Any]:
        """用已生成的文本对当前 AI 发言重跑 TTS。

        清空旧音频与 TTS 计数、分配新的 task_id、用 content_final 重新分句合成。新的 task_id
        会让大屏对账触发 STOP(task_changed)，随后从头重播新归档——无需新增前端协议。"""
        async with self._lock:
            self._ensure_match_allows_control("resynthesize_tts")
            speech = self.snapshot.get("current_speech")
            if not speech or speech.get("id") != speech_id:
                raise MatchStateError("speech_not_found", "未找到要重新合成的当前发言。", {"speech_id": speech_id})
            if speech.get("source") != "agent_text":
                raise MatchStateError("invalid_speech_source", "只有 AI 发言可以重新合成 TTS。", {"speech_id": speech_id})
            full_text = (speech.get("content_final") or speech.get("content_partial") or "").strip()
            if not full_text:
                raise MatchStateError("empty_speech_text", "该发言尚无可合成的文本。", {"speech_id": speech_id})
            speaker_id = speech.get("speaker_id")
            speaker = self._find_speaker(speaker_id)
            new_task_id = f"task_{self.seq + 1}"
            self.snapshot["audio_assets"] = [
                asset for asset in self.snapshot.get("audio_assets", []) if asset.get("speech_id") != speech_id
            ]
            speech["tts_task_id"] = new_task_id
            speech["tts_expected_sentences"] = None
            speech["tts_created_sentences"] = 0
            speech["tts_ready_sentences"] = 0
            speech["tts_played_sentences"] = 0
            speech["tts_played_sentence_indices"] = []
            speech["tts_skipped_sentences"] = []
            speech.pop("tts_playing_sentence_idx", None)
            speech["tts_last_playback_status"] = "resynthesizing"
            self.snapshot["speech_service"]["tts"].update(
                {
                    "status": "synthesizing",
                    "queue_size": 0,
                    "speaker_id": speaker_id,
                    "detail": "resynthesizing TTS for current speech",
                    "last_progress_at": iso_now(),
                }
            )
            self._persist_snapshot()
        await self.emit(
            "tts.resynthesize_started",
            {"speech_id": speech_id, "task_id": new_task_id, "speaker_id": speaker_id, "reason": reason},
            "screen",
            speaker_id,
        )

        expected = await self._synthesize_text_tts(speech_id, new_task_id, speaker, full_text)

        async with self._lock:
            speech = self.snapshot.get("current_speech")
            if speech and speech.get("id") == speech_id and speech.get("tts_task_id") == new_task_id:
                speech["tts_expected_sentences"] = expected
                speech["tts_created_sentences"] = expected
                self._reconcile_tts_gaps(speech, expected)
                self.snapshot["speech_service"]["tts"].update(
                    {
                        "status": "playing",
                        "queue_size": self._tts_remaining_playback_count(speech, fallback_total=expected),
                        "speaker_id": speaker_id,
                        "detail": "resynthesized TTS playback pending",
                        "last_progress_at": iso_now(),
                    }
                )
                self._persist_snapshot()
        await self.emit(
            "tts.finished",
            {"task_id": new_task_id, "speech_id": speech_id, "speaker_id": speaker_id, "expected_sentence_count": expected},
            "system",
        )
        if expected > 0:
            self._arm_tts_playback_grace(speech_id, new_task_id, expected)
        return await self.get_snapshot()

    async def _synthesize_text_tts(self, speech_id: str, task_id: str, speaker: Dict[str, Any], full_text: str) -> int:
        """对一段完整文本分句并发合成 TTS，返回创建的分段数（expected）。

        复用 run_agent_speech 流式路径里的同一套分句逻辑（_next_tts_sentence）与单句合成
        （_synthesize_sentence_tts），但输入是完整文本——供「重新合成」复用。"""
        semaphore = asyncio.Semaphore(self._tts_sentence_concurrency())

        async def synth(sentence: str, idx: int) -> bool:
            async with semaphore:
                return await self._synthesize_sentence_tts_with_timeout(sentence, idx, task_id, speech_id, speaker)

        tasks: List[asyncio.Task] = []
        sent_chars = 0
        idx = 0
        guard = 0
        while True:
            guard += 1
            if guard > 10000:  # 防御性上限，正常发言远不会触及
                break
            sentence, new_pos = self._next_tts_sentence(full_text, sent_chars, allow_soft_break=(idx == 0))
            if new_pos <= sent_chars and not sentence:
                break  # 没有进展、也没有完整句子 —— 剩余作为 tail 处理
            sent_chars = new_pos
            if sentence:
                tasks.append(asyncio.create_task(synth(sentence, idx)))
                idx += 1
        tail = full_text[sent_chars:].strip()
        if tail:
            tasks.append(asyncio.create_task(synth(tail, idx)))
            idx += 1
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return idx

    async def _start_agent_playback(self, task_id: str, speaker: Dict[str, Any]) -> None:
        async with self._lock:
            phase_id = self.snapshot["match"]["current_phase_id"]
            self.snapshot["match"]["live_mode"] = "free" if phase_id == "phase_free_debate" else "single"
            if self.snapshot.get("current_speech"):
                self.snapshot["current_speech"]["started_at"] = iso_now()
                self.snapshot["current_speech"]["state"] = "speaking"
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
        try:
            selection = select_tts_gateway(speaker=speaker)
        except SpeechProviderError as exc:
            return {
                "task_id": task_id,
                "speech_id": speech_id,
                "speaker_id": speaker["id"],
                "reason": _speech_error_message(exc),
                "code": _speech_error_code(exc, "tts_config_error"),
                "failed": True,
                "latency_ms": 0,
                "degraded_to": "text_only",
            }
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
                    "provider": selection.provider,
                    "voice_preset_id": (selection.preset or {}).get("id"),
                },
                started_at=iso_now(),
            )
            speech = self.snapshot.get("current_speech")
            if speech and speech.get("id") == speech_id and speech.get("tts_task_id") == task_id:
                speech["tts_last_progress_at"] = iso_now()
                self.snapshot["speech_service"]["tts"].update(
                    {
                        "status": "synthesizing",
                        "queue_size": max(1, self._tts_unresolved_sentence_count(speech, fallback_total=sentence_idx + 1)),
                        "speaker_id": speaker_id,
                        "detail": f"synthesizing segment {sentence_idx + 1}",
                        "last_progress_at": speech["tts_last_progress_at"],
                    }
                )
                self._persist_snapshot()
            self.snapshot["speech_service"]["tts"] = {
                "status": "synthesizing",
                "latency_ms": self.snapshot["speech_service"]["tts"].get("latency_ms", 0),
                "queue_size": 1,
                "speaker_id": speaker["id"],
                "detail": f"{selection.provider} TTS synthesizing official AI speech",
            }
            self._persist_snapshot()

        await self.emit(
            "tts.synthesis_started",
            {"task_id": task_id, "speech_id": speech_id, "speaker_id": speaker["id"], "text_length": len(content)},
            "system",
            speaker["id"],
        )

        try:
            result = await selection.gateway.synthesize(content, **selection.options)
        except SpeechProviderError as exc:
            message = _speech_error_message(exc)
            code = _speech_error_code(exc, "tts_error")
            payload = {
                "task_id": task_id,
                "speech_id": speech_id,
                "speaker_id": speaker["id"],
                "reason": message,
                "code": code,
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
                    "detail": message,
                    "degraded_to": "text_only",
                }
                self.repo.finish_speech_service_request(
                    match_id=self.snapshot["match"]["id"],
                    request_id=request_id,
                    status="failed",
                    response={"degraded_to": "text_only"},
                    error_code=code,
                    error_message=message,
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
                "provider": selection.provider,
                "voice_preset_id": (selection.preset or {}).get("id"),
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

        # Compute a browser-accessible URL for this audio file
        try:
            audio_root = project_root() / "apps" / "backend" / "storage" / "audio"
            rel = file_path.relative_to(audio_root)
            parts = rel.parts
            if parts:
                audio_url = "/api/audio/" + "/".join(parts)
            else:
                audio_url = None
        except ValueError:
            audio_url = None

        if audio_url:
            payload["audio_url"] = audio_url

        await self.emit("tts.audio_archived", payload, "screen", speaker["id"])
        return payload

    # --- streaming sentence TTS helpers ---

    _SENTENCE_END_RE = re.compile(r"[。！？!?]+")
    _TTS_SOFT_BREAK_RE = re.compile(r"[，,；;：:]")

    def _next_tts_sentence(self, full_text: str, sent_chars: int, allow_soft_break: bool = True) -> tuple:
        """Return (segment, new_sent_chars) for the next TTS-ready text after sent_chars.

        Full sentences are preferred. When ``allow_soft_break`` is true, a long
        streaming prefix without a sentence end may be cut at a comma/semicolon/colon
        so the very first audio can start before the agent finishes a paragraph.
        Once playback is under way (``allow_soft_break`` false) we only ever cut at a
        real sentence end, so two words inside the same sentence are never synthesized
        as separate, audibly disjoint segments.
        """
        tail = full_text[sent_chars:]
        early_chars = self._tts_early_segment_chars()
        min_sentence_chars = self._tts_min_sentence_chars()
        first_min = self._tts_first_segment_chars()
        stripped_tail = tail.strip()
        m = self._SENTENCE_END_RE.search(tail)
        if m:
            end = m.end()
            sentence = tail[:end].strip()
            new_pos = sent_chars + end
            if len(sentence) < 4:
                return "", new_pos  # advance past short sentence
            if len(sentence) < min_sentence_chars and not allow_soft_break:
                # 播放已开始（非首段）：完整但短的句子整段发出，绝不在句中切。
                return sentence, new_pos
            # 首段（allow_soft_break）：完整句即便短也立刻发声——尽快开口，且是自然句末，不割裂。
            return sentence, new_pos

        # No sentence end yet. Without soft breaks we wait for a full sentence rather
        # than cutting the paragraph mid-way.
        if not allow_soft_break:
            return "", sent_chars

        # 首段尽快出声：在第一个位置 >= first_min 的软停顿（逗号/分号/冒号）处即切出第一声；
        # 都还没有则等满 early_chars 再兜底硬切（极少发生）。切点都在标点处，自然停顿、不割裂。
        window = tail[: min(len(tail), early_chars + 12)]
        first_break = 0
        for match in self._TTS_SOFT_BREAK_RE.finditer(window):
            if match.end() >= first_min:
                first_break = match.end()
                break
        if first_break:
            sentence = tail[:first_break].strip()
            new_pos = sent_chars + first_break
            if len(sentence) < 4:
                return "", new_pos
            return sentence, new_pos

        if len(stripped_tail) < early_chars:
            return "", sent_chars

        hard_end = min(len(tail), early_chars + 12)
        sentence = tail[:hard_end].strip()
        new_pos = sent_chars + hard_end
        if len(sentence) < 4:
            return "", new_pos
        return sentence, new_pos

    async def _synthesize_sentence_tts(
        self,
        text: str,
        sentence_idx: int,
        task_id: str,
        speech_id: str,
        speaker: Dict[str, Any],
    ) -> bool:
        """Synthesize a single sentence's TTS and emit tts.sentence_ready for screen playback.

        Returns True on success, False on failure. Every created sentence index MUST emit
        exactly one tts.sentence_ready event so the screen's ordered audio queue never stalls
        waiting for a missing index. On synthesis/archive failure we still emit the event with
        an empty audio_url plus skipped=True, so the screen advances past it instead of hanging."""
        speaker_id = speaker["id"]
        request_id = f"tts_{task_id}_s{sentence_idx}"

        async def _emit_skip(reason: str) -> None:
            await self._emit_tts_sentence_skip(task_id, speech_id, speaker_id, sentence_idx, reason)

        normalized_text = normalize_tts_text(text)
        if not normalized_text:
            await _emit_skip("empty_sentence")
            return False
        try:
            selection = select_tts_gateway(speaker=speaker)
        except SpeechProviderError:
            await _emit_skip("tts_config_failed")
            return False
        async with self._lock:
            match_id = self.snapshot["match"]["id"]
            phase_id = (self.snapshot.get("current_speech") or {}).get("phase_id") or self.snapshot["match"]["current_phase_id"]
            phase_key = self._phase_key_or_default(phase_id)
            self.repo.save_speech_service_request_started(
                match_id=match_id,
                request_id=request_id,
                service="tts",
                operation="agent_synthesis",
                speech_id=speech_id,
                speaker_id=speaker_id,
                origin="live",
                **self._log_context(),
                request={
                    "task_id": task_id,
                    "speech_id": speech_id,
                    "speaker_id": speaker_id,
                    "sentence_idx": sentence_idx,
                    "text": text,
                    "text_length": len(text),
                    "normalized_text_length": len(normalized_text),
                    "normalized_text_preview": normalized_text[:80],
                    "provider": selection.provider,
                    "voice_preset_id": (selection.preset or {}).get("id"),
                },
                started_at=iso_now(),
            )

        request_started_time = time.perf_counter()
        try:
            live_key = (match_id, speech_id, task_id, sentence_idx)
            live_mime_type = self._tts_live_mime_type(selection)
            if live_mime_type:
                await tts_live_manager.start(
                    live_key,
                    {
                        "match_id": match_id,
                        "task_id": task_id,
                        "speech_id": speech_id,
                        "speaker_id": speaker_id,
                        "sentence_idx": sentence_idx,
                        "mime_type": live_mime_type,
                    },
                )
                async with self._lock:
                    speech = self.snapshot.get("current_speech")
                    if speech and speech.get("id") == speech_id and speech.get("tts_task_id") == task_id:
                        speech["tts_streaming_sentences"] = max(int(speech.get("tts_streaming_sentences") or 0), sentence_idx + 1)
                        self._persist_snapshot()
                await self.emit(
                    "tts.sentence_stream_started",
                    {
                        "task_id": task_id,
                        "speech_id": speech_id,
                        "speaker_id": speaker_id,
                        "sentence_idx": sentence_idx,
                        "mime_type": live_mime_type,
                    },
                    "screen",
                    speaker_id,
                )
                result = await self._stream_tts_live_with_retry(selection, normalized_text, live_key, live_mime_type)
                await tts_live_manager.finish(live_key, {"mime_type": result.mime_type, "latency_ms": result.latency_ms, "chunk_count": result.chunk_count})
            else:
                result = await self._synthesize_tts_with_retry(selection, normalized_text)
        except SpeechProviderError as exc:
            try:
                await tts_live_manager.fail((match_id, speech_id, task_id, sentence_idx), _speech_error_message(exc))
            except Exception:
                pass
            message = _speech_error_message(exc)
            code = _speech_error_code(exc, "tts_error")
            async with self._lock:
                self.repo.finish_speech_service_request(
                    match_id=match_id,
                    request_id=request_id,
                    status="failed",
                    response={"degraded_to": "text_only"},
                    error_code=code,
                    error_message=message,
                    latency_ms=max(0, int((time.perf_counter() - request_started_time) * 1000)),
                    completed_at=iso_now(),
                )
            await _emit_skip("tts_synthesize_failed")
            return False
        except Exception as exc:  # noqa: BLE001 — 任何意外错误都必须降级为跳句，绝不让某句静默死亡卡住大屏 12s
            try:
                await tts_live_manager.fail((match_id, speech_id, task_id, sentence_idx), "TTS 合成内部错误")
            except Exception:
                pass
            try:
                async with self._lock:
                    self.repo.finish_speech_service_request(
                        match_id=match_id,
                        request_id=request_id,
                        status="failed",
                        response={"degraded_to": "text_only"},
                        error_code="tts_internal_error",
                        error_message=f"{type(exc).__name__}: {exc}",
                        latency_ms=max(0, int((time.perf_counter() - request_started_time) * 1000)),
                        completed_at=iso_now(),
                    )
            except Exception:
                pass
            await _emit_skip("tts_internal_error")
            return False

        try:
            archive_dir = self._audio_archive_dir(match_id, phase_key, speech_id)
            archive_dir.mkdir(parents=True, exist_ok=True)
            ext = self._audio_extension(result.mime_type)
            file_path = archive_dir / f"tts_{self._safe_path_part(task_id)}_s{sentence_idx}.{ext}"
            # 首句（idx 0）前置一小段静音：补偿投影机音频输出的启动延迟，避免开场"前几个字被吃掉"。
            # 只对 mp3 首句生效；MP3 帧自描述，拼接静音帧后浏览器正常解码（静音→语音）。
            archived_audio = result.audio
            if sentence_idx == 0 and result.mime_type == "audio/mpeg":
                lead = self._tts_lead_silence_bytes()
                if lead:
                    archived_audio = lead + result.audio
            file_path.write_bytes(archived_audio)
            audio_root = self.audio_root_path()
            rel = file_path.relative_to(audio_root)
            audio_url = "/api/audio/" + "/".join(rel.parts)
        except (OSError, ValueError):
            async with self._lock:
                self.repo.finish_speech_service_request(
                    match_id=match_id,
                    request_id=request_id,
                    status="failed",
                    response={"degraded_to": "text_only"},
                    error_code="archive_error",
                    error_message="Failed to write TTS audio file",
                    latency_ms=max(0, int((time.perf_counter() - request_started_time) * 1000)),
                    completed_at=iso_now(),
                )
            await _emit_skip("tts_archive_failed")
            return False

        latency_ms = result.latency_ms
        async with self._lock:
            asset = self._upsert_audio_asset(
                speech_id=speech_id,
                speaker_id=speaker_id,
                phase_id=phase_id,
                mime_type=result.mime_type,
                archive_dir=archive_dir,
                chunk_path=file_path,
                chunk_index=sentence_idx,
                size_bytes=len(archived_audio),
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
                    "text_length": len(text),
                }
            )
            self.snapshot["speech_service"]["tts"]["latency_ms"] = latency_ms
            speech = self.snapshot.get("current_speech")
            if speech and speech.get("id") == speech_id and speech.get("tts_task_id") == task_id:
                speech["tts_ready_sentences"] = max(int(speech.get("tts_ready_sentences") or 0), sentence_idx + 1)
                created = int(speech.get("tts_created_sentences") or sentence_idx + 1)
                ready = int(speech.get("tts_ready_sentences") or 0)
                self.snapshot["speech_service"]["tts"].update(
                    {
                        "status": "playing" if int(speech.get("tts_played_sentences") or 0) > 0 else "synthesizing",
                        "queue_size": self._tts_unresolved_sentence_count(speech, fallback_total=created),
                        "speaker_id": speaker_id,
                        "detail": f"TTS archived segment {sentence_idx + 1}/{created or '?'}",
                        "last_progress_at": iso_now(),
                    }
                )
            self.repo.finish_speech_service_request(
                match_id=match_id,
                request_id=request_id,
                status="completed",
                response={
                    "task_id": task_id,
                    "speech_id": speech_id,
                    "speaker_id": speaker_id,
                    "sentence_idx": sentence_idx,
                    "audio_asset_id": asset["id"],
                    "mime_type": result.mime_type,
                    "size_bytes": len(result.audio),
                    "chunk_count": result.chunk_count,
                    "latency_ms": latency_ms,
                    "file_path": str(file_path),
                    "provider": selection.provider,
                    "voice_preset_id": (selection.preset or {}).get("id"),
                },
                latency_ms=latency_ms,
                completed_at=iso_now(),
            )
            # 热点：跳过昂贵的结构化镜像同步（只写实时快照），让 sentence_ready 尽快广播去出声；
            # 结构化表在发言结束/阶段切换等节点统一同步。
            self._persist_snapshot(sync_structured=False)

        await self.emit(
            "tts.sentence_ready",
            {
                "task_id": task_id,
                "speech_id": speech_id,
                "speaker_id": speaker_id,
                "sentence_idx": sentence_idx,
                "audio_url": audio_url,
                "mime_type": result.mime_type,
                "size_bytes": len(result.audio),
            },
            "screen",
            speaker_id,
            sync_structured=False,
        )
        return True

    async def _synthesize_tts_with_retry(self, selection: Any, text: str) -> TTSResult:
        attempts = self._tts_retry_attempts()
        for attempt in range(attempts + 1):
            await self._throttle_tts_request_start()
            try:
                return await selection.gateway.synthesize(text, **selection.options)
            except SpeechProviderError as exc:
                if attempt >= attempts or not self._is_retryable_tts_error(exc):
                    raise
                await asyncio.sleep(self._tts_retry_delay_seconds(attempt))
        raise SpeechGatewayError("TTS 合成失败。", code="tts_error")

    async def _stream_tts_live_with_retry(self, selection: Any, text: str, live_key: Any, live_mime_type: str) -> TTSResult:
        """live 流式合成 + 受限重试。长文本一次几十句并发打服务商，瞬时错误(429/连接断)很常见；
        非流式分支有重试，流式分支若不重试就会每句直接 skip → 大屏没声音。

        关键安全约束：**只有在还没向订阅者推出任何音频块时**才重试——一旦推过块再重试，
        大屏会收到重复音频。所以失败发生在首块之前(典型的服务商直接拒绝/429)才重试，
        中途断流则放弃(交由上层跳句)。每次尝试前重新限速。"""
        attempts = self._tts_retry_attempts()
        last_exc: Optional[Exception] = None
        for attempt in range(attempts + 1):
            await self._throttle_tts_request_start()
            audio_parts: List[bytes] = []
            chunk_count = 0
            latency_ms = 0
            result_mime_type = live_mime_type
            published = 0
            try:
                async for event in selection.gateway.synthesize_stream(text, **selection.options):
                    if event["type"] == "chunk":
                        chunk = bytes(event["audio"])
                        chunk_count += 1
                        audio_parts.append(chunk)
                        await tts_live_manager.publish_chunk(live_key, chunk, int(event.get("index") or chunk_count))
                        published += 1
                    elif event["type"] == "done":
                        result_mime_type = event["mime_type"]
                        latency_ms = event["latency_ms"]
                        chunk_count = event["chunk_count"]
                return TTSResult(audio=b"".join(audio_parts), mime_type=result_mime_type, latency_ms=latency_ms, chunk_count=chunk_count)
            except SpeechProviderError as exc:
                last_exc = exc
                # 已推出音频块则不能重试(会重复)；未推出且错误可重试才退避重试。
                if published == 0 and attempt < attempts and self._is_retryable_tts_error(exc):
                    await asyncio.sleep(self._tts_retry_delay_seconds(attempt))
                    continue
                raise
        if last_exc is not None:
            raise last_exc
        raise SpeechGatewayError("TTS 流式合成失败。", code="tts_error")

    # 永久性错误：配置/授权失败、输入为空、服务关闭——重试也不会成功，立即跳句而非无谓拖延。
    _TTS_PERMANENT_ERROR_CODES = frozenset(
        {
            "empty_text",
            "missing_config",
            "missing_api_key",
            "tts_disabled",
            "asr_disabled",
            "unauthenticated",
            "unauthorized",
            "forbidden",
            "401",
            "403",
        }
    )

    async def _throttle_tts_request_start(self) -> None:
        """给 TTS 服务商请求的「启动」加最小节奏间隔。

        长文本会一次裂解出几十句、并发起 TTS 合成；若毫无间隔地齐发，极易触发服务商
        并发上限/限流(429)，导致 live 那句合成失败、大屏不出声。这里用一把独立小锁把相邻
        两次请求启动至少隔开 PHDEBATE_TTS_MIN_REQUEST_INTERVAL_MS，把突发摊平。
        注意：只用 self._tts_request_lock，绝不触碰驱动比赛流程的 self._lock；sleep 会让出
        event loop，不阻塞其它协程。"""
        min_interval = self._tts_min_request_interval_seconds()
        if min_interval <= 0:
            return
        async with self._tts_request_lock:
            now = time.perf_counter()
            wait = self._last_tts_request_at + min_interval - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.perf_counter()
            self._last_tts_request_at = now

    def _tts_min_request_interval_seconds(self) -> float:
        raw = os.getenv("PHDEBATE_TTS_MIN_REQUEST_INTERVAL_MS", "80").strip()
        try:
            value = float(raw)
        except ValueError:
            value = 80.0
        return max(0.0, min(2000.0, value)) / 1000.0

    def _tts_retry_attempts(self) -> int:
        raw = os.getenv("PHDEBATE_TTS_RETRY_ATTEMPTS", "2").strip()
        try:
            value = int(raw)
        except ValueError:
            value = 2
        return max(0, min(5, value))

    def _is_retryable_tts_error(self, exc: Exception) -> bool:
        """瞬时错误(超时/断连/限流)值得重试；永久性配置/输入错误不重试。"""
        code = str(getattr(exc, "code", "") or "").strip().lower()
        return code not in self._TTS_PERMANENT_ERROR_CODES

    def _tts_retry_delay_seconds(self, attempt: int) -> float:
        raw = os.getenv("PHDEBATE_TTS_RETRY_BASE_MS", "250").strip()
        try:
            base = float(raw)
        except ValueError:
            base = 250.0
        base = max(0.0, min(2000.0, base)) / 1000.0
        return min(2.0, base * (2 ** max(0, int(attempt))))

    async def _synthesize_sentence_tts_with_timeout(
        self,
        text: str,
        sentence_idx: int,
        task_id: str,
        speech_id: str,
        speaker: Dict[str, Any],
    ) -> bool:
        started = time.perf_counter()
        try:
            return await asyncio.wait_for(
                self._synthesize_sentence_tts(text, sentence_idx, task_id, speech_id, speaker),
                timeout=self._tts_sentence_timeout_seconds(),
            )
        except asyncio.TimeoutError:
            async with self._lock:
                match_id = self.snapshot["match"]["id"]
                self.repo.finish_speech_service_request(
                    match_id=match_id,
                    request_id=f"tts_{task_id}_s{sentence_idx}",
                    status="failed",
                    response={"degraded_to": "text_only"},
                    error_code="tts_timeout",
                    error_message="TTS sentence synthesis timed out",
                    latency_ms=max(0, int((time.perf_counter() - started) * 1000)),
                    completed_at=iso_now(),
                )
            await self._emit_tts_sentence_skip(task_id, speech_id, speaker["id"], sentence_idx, "tts_synthesize_timeout")
            return False

    async def _emit_tts_sentence_skip(self, task_id: str, speech_id: str, speaker_id: str, sentence_idx: int, reason: str) -> None:
        # Record the skipped index on the speech so the screen can fill the ordered
        # gap deterministically from the snapshot — the realtime layer can drop this
        # event, and a missing index would otherwise stall ordered playback forever.
        try:
            async with self._lock:
                speech = self.snapshot.get("current_speech")
                if speech and speech.get("id") == speech_id and speech.get("tts_task_id") == task_id:
                    skipped = speech.setdefault("tts_skipped_sentences", [])
                    if int(sentence_idx) not in skipped:
                        skipped.append(int(sentence_idx))
                        skipped.sort()
                    self.snapshot["speech_service"]["tts"].update(
                        {
                            "status": "playing" if int(speech.get("tts_played_sentences") or 0) > 0 else "synthesizing",
                            "queue_size": self._tts_unresolved_sentence_count(speech, fallback_total=sentence_idx + 1),
                            "speaker_id": speaker_id,
                            "detail": f"TTS skipped segment {sentence_idx + 1}: {reason}",
                            "last_progress_at": iso_now(),
                        }
                    )
                    self._persist_snapshot(sync_structured=False)
        except Exception:  # noqa: BLE001 — 记录失败也必须把事件发出去，绝不吞掉跳过
            pass
        await self.emit(
            "tts.sentence_ready",
            {
                "task_id": task_id,
                "speech_id": speech_id,
                "speaker_id": speaker_id,
                "sentence_idx": sentence_idx,
                "audio_url": "",
                "skipped": True,
                "reason": reason,
            },
            "screen",
            speaker_id,
            sync_structured=False,
        )

    def _tts_formal_enabled(self) -> bool:
        raw = os.getenv("PHDEBATE_TTS_FORMAL", "").strip().lower()
        if raw in {"0", "false", "no", "off"}:
            return False
        if raw in {"1", "true", "yes", "on"}:
            return True
        return self._speech_section_ready("tts")

    def _tts_sentence_concurrency(self) -> int:
        # Synthesize several sentences in parallel so the next archived file is ready
        # before playback reaches it (keeps the archived-file path from sounding slow).
        raw = os.getenv("PHDEBATE_TTS_SENTENCE_CONCURRENCY", "4").strip()
        try:
            value = int(raw)
        except ValueError:
            value = 4
        return max(1, min(8, value))

    def _tts_sentence_timeout_seconds(self) -> float:
        raw = os.getenv("PHDEBATE_TTS_SENTENCE_TIMEOUT_S", "10").strip()
        try:
            value = float(raw)
        except ValueError:
            value = 10.0
        return max(4.0, min(90.0, value))

    def _tts_playback_start_timeout_seconds(self) -> float:
        raw = os.getenv("PHDEBATE_TTS_PLAYBACK_START_TIMEOUT_S", "25").strip()
        try:
            value = float(raw)
        except ValueError:
            value = 25.0
        return max(8.0, min(180.0, value))

    def _tts_playback_idle_timeout_seconds(self, expected_sentence_count: int = 1) -> float:
        default = max(45.0, min(120.0, float(max(1, int(expected_sentence_count or 1))) * 12.0))
        raw = os.getenv("PHDEBATE_TTS_PLAYBACK_IDLE_TIMEOUT_S", str(int(default))).strip()
        try:
            value = float(raw)
        except ValueError:
            value = default
        return max(20.0, min(300.0, value))

    def _tts_lead_silence_bytes(self) -> bytes:
        """首句前置静音字节。补偿投影机音频输出启动延迟，避免开场前几个字被吃掉。
        通过 PHDEBATE_TTS_LEAD_SILENCE=0 可关闭（开关实时生效，文件字节单独缓存）。"""
        raw = os.getenv("PHDEBATE_TTS_LEAD_SILENCE", "1").strip().lower()
        if raw in {"0", "false", "no", "off"}:
            return b""
        cached = getattr(self, "_lead_silence_cache", None)
        if cached is not None:
            return cached
        try:
            path = Path(__file__).resolve().parent.parent / "assets" / "silence-lead.mp3"
            self._lead_silence_cache = path.read_bytes()
        except OSError:
            self._lead_silence_cache = b""
        return self._lead_silence_cache

    def _tts_ready_indices(self, speech_id: str) -> set:
        """已成功归档（有可播 url）的分段序号集合。"""
        asset = self._audio_asset_for_speech(speech_id)
        ready = set()
        for chunk in (asset or {}).get("chunks", []) if asset else []:
            try:
                idx = int(chunk.get("chunk_index", -1))
            except (TypeError, ValueError):
                continue
            if idx >= 0 and str(chunk.get("audio_url") or ""):
                ready.add(idx)
        return ready

    @staticmethod
    def _tts_int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _tts_skipped_indices(self, speech: Dict[str, Any]) -> set:
        skipped = set()
        for value in speech.get("tts_skipped_sentences") or []:
            idx = self._tts_int(value, -1)
            if idx >= 0:
                skipped.add(idx)
        return skipped

    def _tts_played_indices(self, speech: Dict[str, Any]) -> set:
        raw = speech.get("tts_played_sentence_indices")
        if isinstance(raw, list):
            played = set()
            for value in raw:
                idx = self._tts_int(value, -1)
                if idx >= 0:
                    played.add(idx)
            return played
        legacy_count = max(0, self._tts_int(speech.get("tts_played_sentences"), 0))
        last_status = str(speech.get("tts_last_playback_status") or "")
        playing_idx = self._tts_int(speech.get("tts_playing_sentence_idx"), -1)
        if last_status == "playing" and legacy_count > 0 and playing_idx == legacy_count - 1:
            legacy_count -= 1
        return set(range(legacy_count))

    def _tts_sentence_total(self, speech: Dict[str, Any], fallback_total: int = 0) -> int:
        expected = self._tts_int(speech.get("tts_expected_sentences"), -1)
        if expected > 0:
            return expected
        created = self._tts_int(speech.get("tts_created_sentences"), 0)
        ready = self._tts_ready_indices(speech.get("id")) if speech.get("id") else set()
        skipped = self._tts_skipped_indices(speech)
        high_water = max(ready | skipped, default=-1) + 1
        return max(0, created, high_water, int(fallback_total or 0))

    def _tts_unresolved_sentence_count(self, speech: Dict[str, Any], fallback_total: int = 0) -> int:
        """分段总数中，还没有 ready 也没有 skipped 的数量。用于合成队列口径。"""
        total = self._tts_sentence_total(speech, fallback_total=fallback_total)
        if total <= 0:
            return 0
        ready = self._tts_ready_indices(speech.get("id")) if speech.get("id") else set()
        skipped = self._tts_skipped_indices(speech)
        resolved = {idx for idx in ready | skipped if 0 <= idx < total}
        return max(0, total - len(resolved))

    def _tts_remaining_playback_count(self, speech: Dict[str, Any], fallback_total: int = 0) -> int:
        """还需要大屏处理的分段数量；优先按精确 played/skipped 集合计算，兼容旧高水位快照。"""
        total = self._tts_sentence_total(speech, fallback_total=fallback_total)
        if total <= 0:
            return 0
        resolved = self._tts_played_indices(speech) | self._tts_skipped_indices(speech)
        resolved = {idx for idx in resolved if 0 <= idx < total}
        return max(0, total - len(resolved))

    def _tts_playback_display_queue_size(self, speech: Dict[str, Any], fallback_total: int = 0) -> int:
        """后台展示队列：playing 表示当前段已开始，可按高水位显示；终态判定仍用精确集合。"""
        total = self._tts_sentence_total(speech, fallback_total=fallback_total)
        if total <= 0:
            return 0
        if str(speech.get("tts_last_playback_status") or "playing") == "playing":
            played_high_water = min(total, max(0, self._tts_int(speech.get("tts_played_sentences"), 0)))
            skipped_after_playing = {idx for idx in self._tts_skipped_indices(speech) if played_high_water <= idx < total}
            return max(0, total - played_high_water - len(skipped_after_playing))
        return self._tts_remaining_playback_count(speech, fallback_total=fallback_total)

    def _should_auto_complete_tts_playback(self, speech: Dict[str, Any], status: str) -> bool:
        """当前端已经报告最后一个分段的终态时，后端直接收尾，避免再依赖额外 complete 请求。"""
        if status not in {"played", "stalled", "error", "play_rejected", "failed", "skipped"}:
            return False
        expected = self._tts_int(speech.get("tts_expected_sentences"), 0)
        if expected <= 0:
            return False
        return self._tts_remaining_playback_count(speech, fallback_total=expected) == 0

    def _reconcile_tts_gaps(self, speech: Dict[str, Any], expected: int) -> None:
        """保证 [0,expected) 中每个分段要么 ready、要么 skipped。

        合成任务即便因异常逃逸了 _emit_skip、或 skip 记录丢失，这里也会把遗漏的 idx 补进
        tts_skipped_sentences，使不变量 expected == |ready| + |skipped| 成立——大屏的纯函数
        对账只看快照即可推进到结束，绝不会等一个永远不来的分段而永久卡死。
        """
        if expected <= 0:
            return
        speech_id = speech.get("id")
        ready = self._tts_ready_indices(speech_id) if speech_id else set()
        skipped = speech.setdefault("tts_skipped_sentences", [])
        skipped_set = set(int(i) for i in skipped)
        for idx in range(expected):
            if idx not in ready and idx not in skipped_set:
                skipped.append(idx)
                skipped_set.add(idx)
        skipped.sort()

    def _tts_live_mime_type(self, selection: Any) -> str:
        # 默认关闭 live MSE 流式：投影机浏览器上它会"解码成静音/超慢"，且一旦首块不可解码，
        # 大屏严格队列会卡在该分段永不前进。归档文件路径才是可靠的唯一真相。要试验可在进程
        # 环境设 PHDEBATE_TTS_LIVE_STREAM=1 并重启后端（不需要改码）。
        raw = os.getenv("PHDEBATE_TTS_LIVE_STREAM", "0").strip().lower()
        if raw in {"0", "false", "no", "off"}:
            return ""
        if getattr(selection, "provider", "") != "alicloud":
            return ""
        gateway = getattr(selection, "gateway", None)
        if not hasattr(gateway, "synthesize_stream") or not hasattr(gateway, "stream_mime_type"):
            return ""
        try:
            mime_type = str(gateway.stream_mime_type(**getattr(selection, "options", {}))).strip()
        except Exception:
            return ""
        # MediaSource support for mp3 is mature in the browsers used for the
        # projection screen; PCM/Opus keep using the archived-file fallback.
        return mime_type if mime_type == "audio/mpeg" else ""

    def _tts_early_segment_chars(self) -> int:
        raw = os.getenv("PHDEBATE_TTS_EARLY_SEGMENT_CHARS", "40").strip()
        try:
            value = int(raw)
        except ValueError:
            value = 40
        return max(24, min(110, value))

    def _tts_min_sentence_chars(self) -> int:
        raw = os.getenv("PHDEBATE_TTS_MIN_SENTENCE_CHARS", "18").strip()
        try:
            value = int(raw)
        except ValueError:
            value = 18
        return max(8, min(32, value))

    def _tts_first_segment_chars(self) -> int:
        """首段（idx 0）尽快出声的最小字数：在第一个 >= 该值的自然停顿（句末或逗号）处即切出，
        而不是像后续段那样等满 early_segment_chars。这样首句更早开始合成、且更短=合成更快，
        把"开口很慢"压下来；切点都在标点处（自然停顿，不会把词切断）。"""
        raw = os.getenv("PHDEBATE_TTS_FIRST_SEGMENT_CHARS", "8").strip()
        try:
            value = int(raw)
        except ValueError:
            value = 8
        return max(4, min(40, value))

    def _tts_completion_detail(self, tts_result: Optional[Dict[str, Any]]) -> str:
        if not tts_result or tts_result.get("failed"):
            return ""
        return f"TTS archived · {tts_result.get('size_bytes', 0)} bytes · {tts_result.get('file_path', '')}"

    # --- agent output budget (deterministic max_token from the speech time limit) ---

    def _tts_speaking_cps(self) -> float:
        """Spoken Chinese characters per second at speech_rate=1 (debate pace)."""
        raw = os.getenv("PHDEBATE_TTS_SPEAKING_CPS", "4.5").strip()
        try:
            value = float(raw)
        except ValueError:
            value = 4.5
        return max(2.0, min(8.0, value))

    def _agent_tokens_per_char(self) -> float:
        """Tokens per Chinese character for the agent model's tokenizer.

        Qwen-family tokenizers average ~1.5 Chinese chars per token; 0.75 keeps a
        safety margin so the spoken duration stays within the limit.
        """
        raw = os.getenv("PHDEBATE_AGENT_TOKENS_PER_CHAR", "0.75").strip()
        try:
            value = float(raw)
        except ValueError:
            value = 0.75
        return max(0.4, min(2.0, value))

    def _agent_max_token_margin(self) -> float:
        """Headroom so the model finishes a sentence naturally instead of being
        hard-truncated exactly at the time budget."""
        raw = os.getenv("PHDEBATE_AGENT_MAX_TOKEN_MARGIN", "1.15").strip()
        try:
            value = float(raw)
        except ValueError:
            value = 1.15
        return max(1.0, min(2.0, value))

    def _speaker_speech_rate(self, speaker: Dict[str, Any]) -> float:
        """Resolve the TTS speech rate for a speaker (its preset, else provider default)."""
        provider = str((integration_config.active_section("tts") or {}).get("provider") or "alicloud")
        preset_id = str(speaker.get("tts_voice_preset_id") or "").strip()
        preset = integration_config.voice_preset(preset_id) if preset_id else None
        if not preset:
            preset = integration_config.default_voice_preset(provider)
        try:
            rate = float((preset or {}).get("speech_rate") or 1.0)
        except (TypeError, ValueError):
            rate = 1.0
        return max(0.5, min(2.0, rate))

    def _agent_output_budget(self, time_limit_seconds: int, speech_rate: float) -> Dict[str, Any]:
        """Deterministically derive the char/token budget for one speech.

        char_budget = time_limit × spoken_chars_per_sec × speech_rate
        max_token   = round(char_budget × tokens_per_char × margin), clamped to [64, 4096]
        """
        cps = self._tts_speaking_cps() * speech_rate
        char_budget = max(1.0, float(time_limit_seconds) * cps)
        target_chars = max(40, int(char_budget))
        raw_tokens = char_budget * self._agent_tokens_per_char() * self._agent_max_token_margin()
        max_token = max(64, min(4096, int(round(raw_tokens))))
        return {
            "speech_rate": round(speech_rate, 3),
            "chars_per_second": round(cps, 3),
            "char_budget": target_chars,
            "target_chars": target_chars,
            "max_token": max_token,
        }

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

    def _resume_clocks_by_id(self, clock_ids: List[str]) -> None:
        """只恢复指定 id 的钟（暂停/应急前正在走的那些）。"""
        now = utc_now()
        idset = {str(cid) for cid in clock_ids}
        for clock in self.snapshot["clocks"]:
            if clock.get("id") in idset and clock["state"] == "paused" and clock["remaining_ms"] > 0:
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

    async def save_match_image(self, kind: str, content: bytes, mime_type: str) -> Dict[str, Any]:
        """Store a 比赛名称/主办机构 image and switch that slot to image display mode."""
        if kind not in {"title", "organizer"}:
            raise MatchStateError("invalid_image_kind", "图片类型必须是 title 或 organizer。", {"kind": kind})
        ext = self._image_extension(mime_type)
        match_id = str(self.snapshot["match"]["id"])
        root = self.image_root_path() / "match"
        root.mkdir(parents=True, exist_ok=True)
        filename = f"{match_id}_{kind}.{ext}"
        for old in root.glob(f"{match_id}_{kind}.*"):
            if old.name != filename:
                try:
                    old.unlink()
                except OSError:
                    pass
        (root / filename).write_bytes(content)
        url = f"/api/files/match-images/{filename}?v={self.seq + 1}"
        return await self.update_match({f"{kind}_image_url": url, f"{kind}_display": "image"})

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
        return self._speech_section_ready("asr")

    def _asr_realtime_enabled(self) -> bool:
        raw = os.getenv("PHDEBATE_ASR_REALTIME", "").strip().lower()
        if raw in {"0", "false", "no", "off"}:
            return False
        if raw in {"1", "true", "yes", "on"}:
            return True
        return self._speech_section_ready("asr")

    def _speech_section_ready(self, kind: str) -> bool:
        section = integration_config.active_section(kind)
        if not section.get("enabled"):
            return False
        provider = str(section.get("provider") or "alicloud")
        if provider == "xfyun":
            secrets = section.get("secrets") or {}
            return bool(
                str(section.get("endpoint") or "").strip()
                and str(secrets.get("app_id") or "").strip()
                and str(secrets.get("api_key") or "").strip()
                and str(secrets.get("api_secret") or "").strip()
            )
        if provider == "alicloud":
            secrets = (section.get("secrets") or {}).get("alicloud") or {}
            return bool(str(secrets.get("api_key") or os.getenv("DASHSCOPE_API_KEY", "")).strip())
        return False

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
        audio_url = ""
        try:
            audio_root = self.audio_root_path()
            rel = chunk_path.relative_to(audio_root)
            audio_url = "/api/audio/" + "/".join(rel.parts)
        except ValueError:
            audio_url = ""
        chunks.append(
            {
                "chunk_index": chunk_index,
                "file_path": str(chunk_path),
                "audio_url": audio_url,
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
        # Keep the full debate (newest first). The cap is a safety bound, not a display
        # window — a previous value of 12 dropped earlier speeches from the 实时辩论过程
        # view AND from debate_history sent to agents. One in-progress speech updates a
        # single segment in place, so this counts distinct speeches, not stream deltas.
        self.snapshot["recent_transcript"] = [segment, *next_segments][:_RECENT_TRANSCRIPT_LIMIT]
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
        # 需求 5.md：新一轮本方人类有 2s 决定窗口。但若本方人类在对方发言期间已"全部预跳过"，
        # 则翻面后立即随机 AI 接管（不等 2s）；否则照常给 2s 窗口（超时/全跳过→AI）。
        # 注意：本方预跳过票记在 new_turn_key 下，与刚清掉的 old_turn_key（对方）不同，得以保留。
        new_turn_key = f"{next_side}_{new_turn_index}"
        all_humans = [s["id"] for s in self.snapshot["speakers"] if s["side"] == next_side and s["speaker_type"] == "human"]
        pre_votes = skip_votes.get(new_turn_key, [])
        all_pre_skipped = bool(all_humans) and all(uid in pre_votes for uid in all_humans)
        if all_pre_skipped:
            try:
                asyncio.get_running_loop()
                asyncio.create_task(self._trigger_free_debate_auto_agent(next_side, new_turn_index, "all_pre_skipped"))
            except RuntimeError:
                pass
        else:
            self._arm_free_debate_auto_agent(next_side, new_turn_index)

    async def _trigger_free_debate_auto_agent(self, side: str, turn_index: int, reason: str) -> None:
        """让某一方的一位随机 AI 立即接管当前自由辩论轮（用于"全跳过/全预跳过"立即接管）。
        幂等：通过 auto_handled[turn_key] 防重；仅当该轮确实是当前轮时才接管。"""
        import random

        turn_key = f"{side}_{turn_index}"
        async with self._lock:
            fd = self.snapshot["free_debate"]
            if fd.get("current_turn_side") != side or int(fd.get("turn_index", -1)) != turn_index:
                return
            if self.snapshot.get("current_speech"):
                return
            auto_handled = fd.setdefault("auto_handled", {})
            if auto_handled.get(turn_key):
                return
            agents_on_side = [s for s in self.snapshot["speakers"] if s["side"] == side and s["speaker_type"] == "agent"]
            if not agents_on_side:
                return
            chosen = random.choice(agents_on_side)
            auto_handled[turn_key] = chosen["id"]
            self._persist_snapshot()
        await self.emit(
            "free_debate.auto_agent",
            {"side": side, "turn_index": turn_index, "speaker_id": chosen["id"], "reason": reason},
            "system",
        )
        try:
            self.ensure_agent_speaker_for_current_phase(chosen["id"])
            asyncio.create_task(self.run_agent_speech(chosen["id"]))
        except Exception:
            pass

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

    def _schedule_next_phase_prefetch(self, phase: Dict[str, Any]) -> None:
        """某段发言结束、等待主持进入下一环节时：若下一环节是固定单人 agent 发言，后台预取。
        自由辩论（动态轮次）不预取。失败/无下一环节静默跳过。"""
        if not self._prefetch_enabled():
            return
        nxt = self._next_phase(phase)
        if not nxt or nxt.get("phase_type") == "free_debate":
            return
        designated = next(
            (
                s
                for s in self.snapshot.get("speakers", [])
                if s.get("side") == nxt.get("side")
                and s.get("seat") == nxt.get("speaker_seat")
                and s.get("speaker_type") == "agent"
            ),
            None,
        )
        if not designated:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        asyncio.create_task(self._prefetch_speech(designated["id"], "speech", nxt))

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
            # 自由辩论单轮钟到点也属于"轮内切换"，全自动进入对方 2s 窗口，不需主持确认。
            # 只有某方 total 钟归零（total_expired）才是阶段结束，走下面的 phase_next + 主持确认。
            self._clear_flow_state()
            return deepcopy(self.snapshot["flow"])
        if self._next_phase(phase):
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
        if next_action == "phase_next":
            self._schedule_next_phase_prefetch(phase)
        return deepcopy(flow)

    def _set_flow_waiting_after_speech_end(self, speech: Dict[str, Any], speaker: Dict[str, Any], reason: str) -> Dict[str, Any]:
        phase = self._current_phase()
        if phase.get("phase_type") == "free_debate":
            # 需求 5.md：自由辩论轮内切换全自动——一方说完即进入对方 2s 窗口（人点开始 / 全预跳过 / 超时→AI），
            # 不再 awaiting_host_confirm。轮转与 2s 计时已由 _advance_free_debate_turn_if_needed 安排。
            # 阶段结束（某方 total 钟归零）走另一条 _set_flow_waiting_for_timeout(phase_next)，仍需主持确认。
            self._clear_flow_state()
            return deepcopy(self.snapshot["flow"])
        if self._next_phase(phase):
            next_action = "phase_next"
            message = f"{self.speaker_label(speaker['id'])} 发言完毕，等待主持确认进入下一环节。"
        else:
            next_action = "judge_commentary"
            message = f"{self.speaker_label(speaker['id'])} 发言完毕，等待主持进入评委点评。"

        flow = self._fresh_flow_state()
        flow.update(
            {
                "awaiting_host_confirm": True,
                "reason": reason,
                "message": message,
                "next_action": next_action,
                "phase_id": speech.get("phase_id") or phase.get("id"),
                "speech_id": speech.get("id"),
                "speaker_id": speaker.get("id"),
                "expired_clocks": [],
                "created_at": iso_now(),
            }
        )
        self.snapshot["flow"] = flow
        if next_action == "phase_next":
            self._schedule_next_phase_prefetch(phase)
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
            if "winner_side" not in body:
                raise MatchStateError("invalid_vote", "学生投票必须包含优胜方。")
            ranking = body.get("ranking")
            if ranking is not None:
                if not isinstance(ranking, list) or not ranking:
                    raise MatchStateError("invalid_vote", "辩手排序无效。")
                seen: set = set()
                for sid in ranking:
                    if sid in seen:
                        raise MatchStateError("invalid_vote", "辩手排序中存在重复。", {"speaker_id": sid})
                    seen.add(sid)
                    try:
                        self._find_speaker(sid)
                    except KeyError:
                        raise MatchStateError("invalid_vote", "排序中的辩手无效。", {"speaker_id": sid})
            elif "best_speaker_id" not in body:
                raise MatchStateError("invalid_vote", "学生投票必须包含辩手排序。")
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
            "audience_vote",
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
        # 一方说完后，本方人类有 5s 抢麦窗口（点开始发言），超时（或全部预跳过）则随机 AI 接管。
        # 可用 PHDEBATE_FREE_DEBATE_DECISION_SECONDS 覆盖，或在该环节配置 decision_seconds（测试/现场调参）。
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
