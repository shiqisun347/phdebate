from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional


def project_root() -> Path:
    return Path(__file__).resolve().parents[4]


def default_database_path() -> Path:
    return project_root() / "apps" / "backend" / "storage" / "phdebate.sqlite3"


def database_path_from_env() -> Path:
    value = os.getenv("PHDEBATE_DATABASE_URL", "").strip()
    if value.startswith("sqlite:///"):
        raw = value.removeprefix("sqlite:///")
        path = Path(raw)
        return path if path.is_absolute() else project_root() / path
    return default_database_path()


def agent_request_id(match_id: str, task_id: str) -> str:
    return f"{match_id}:{task_id}"


def speech_service_request_id(match_id: str, request_id: str) -> str:
    return f"{match_id}:{request_id}"


LOG_PREVIEW_CHARS = 360


class SQLiteRepository:
    """SQLite persistence for the live MVP slice.

    Snapshots remain the compatibility source, while events, audit logs, and
    structured mirror tables make the current match queryable for operations.
    """

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = db_path or database_path_from_env()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS app_state (
                  key TEXT PRIMARY KEY,
                  value_json TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS events (
                  id TEXT PRIMARY KEY,
                  match_id TEXT NOT NULL,
                  seq INTEGER NOT NULL,
                  type TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  actor_type TEXT NOT NULL,
                  actor_id TEXT,
                  created_at TEXT NOT NULL,
                  UNIQUE (match_id, seq)
                );

                CREATE INDEX IF NOT EXISTS idx_events_match_seq
                  ON events(match_id, seq);

                CREATE TABLE IF NOT EXISTS audit_logs (
                  id TEXT PRIMARY KEY,
                  match_id TEXT,
                  actor_type TEXT NOT NULL,
                  actor_id TEXT,
                  action TEXT NOT NULL,
                  target_type TEXT,
                  target_id TEXT,
                  request_json TEXT NOT NULL DEFAULT '{}',
                  result TEXT NOT NULL,
                  error_message TEXT,
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS matches_archive (
                  id TEXT PRIMARY KEY,
                  archived_match_id TEXT NOT NULL,
                  new_match_id TEXT NOT NULL,
                  snapshot_json TEXT NOT NULL,
                  export_json TEXT NOT NULL DEFAULT '{}',
                  created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_matches_archive_created
                  ON matches_archive(created_at);

                CREATE TABLE IF NOT EXISTS agent_requests (
                  id TEXT PRIMARY KEY,
                  match_id TEXT NOT NULL,
                  task_id TEXT NOT NULL,
                  speech_id TEXT,
                  speaker_id TEXT NOT NULL,
                  endpoint TEXT NOT NULL,
                  status TEXT NOT NULL,
                  request_json TEXT NOT NULL DEFAULT '{}',
                  response_text TEXT,
                  error_code TEXT,
                  error_message TEXT,
                  latency_ms INTEGER,
                  started_at TEXT NOT NULL,
                  completed_at TEXT,
                  updated_at TEXT NOT NULL,
                  UNIQUE (match_id, task_id)
                );

                CREATE INDEX IF NOT EXISTS idx_agent_requests_match_started
                  ON agent_requests(match_id, started_at);

                CREATE TABLE IF NOT EXISTS speech_service_requests (
                  id TEXT PRIMARY KEY,
                  match_id TEXT NOT NULL,
                  request_id TEXT NOT NULL,
                  service TEXT NOT NULL,
                  operation TEXT NOT NULL,
                  speech_id TEXT,
                  speaker_id TEXT,
                  status TEXT NOT NULL,
                  request_json TEXT NOT NULL DEFAULT '{}',
                  response_json TEXT NOT NULL DEFAULT '{}',
                  error_code TEXT,
                  error_message TEXT,
                  latency_ms INTEGER,
                  started_at TEXT NOT NULL,
                  completed_at TEXT,
                  updated_at TEXT NOT NULL,
                  UNIQUE (match_id, request_id)
                );

                CREATE INDEX IF NOT EXISTS idx_speech_service_requests_match_started
                  ON speech_service_requests(match_id, started_at);

                CREATE TABLE IF NOT EXISTS export_bundles (
                  export_id TEXT PRIMARY KEY,
                  match_id TEXT NOT NULL,
                  file_path TEXT NOT NULL,
                  download_url TEXT NOT NULL,
                  size_bytes INTEGER NOT NULL,
                  entry_count INTEGER NOT NULL,
                  entries_json TEXT NOT NULL DEFAULT '[]',
                  created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_export_bundles_match_created
                  ON export_bundles(match_id, created_at);

                CREATE TABLE IF NOT EXISTS structured_matches (
                  id TEXT PRIMARY KEY,
                  title TEXT NOT NULL,
                  topic TEXT NOT NULL,
                  status TEXT NOT NULL,
                  screen_scene TEXT NOT NULL,
                  live_mode TEXT NOT NULL,
                  current_phase_id TEXT,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS structured_phases (
                  match_id TEXT NOT NULL,
                  id TEXT NOT NULL,
                  phase_key TEXT NOT NULL,
                  name TEXT NOT NULL,
                  phase_type TEXT NOT NULL,
                  display_order INTEGER NOT NULL,
                  side TEXT NOT NULL,
                  speaker_seat INTEGER,
                  duration_seconds INTEGER NOT NULL,
                  side_total_seconds INTEGER,
                  turn_seconds INTEGER,
                  speaker_selector TEXT NOT NULL,
                  status TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  PRIMARY KEY (match_id, id)
                );

                CREATE TABLE IF NOT EXISTS structured_slots (
                  match_id TEXT NOT NULL,
                  speaker_id TEXT NOT NULL,
                  team_id TEXT NOT NULL,
                  side TEXT NOT NULL,
                  seat INTEGER NOT NULL,
                  name TEXT NOT NULL,
                  speaker_type TEXT NOT NULL,
                  model_name TEXT,
                  model_kind TEXT,
                  agent_endpoint TEXT,
                  status TEXT,
                  updated_at TEXT NOT NULL,
                  PRIMARY KEY (match_id, speaker_id)
                );

                CREATE TABLE IF NOT EXISTS structured_speeches (
                  match_id TEXT NOT NULL,
                  speech_id TEXT NOT NULL,
                  phase_id TEXT,
                  speaker_id TEXT,
                  side TEXT,
                  turn_index INTEGER,
                  source TEXT,
                  state TEXT,
                  content_final TEXT,
                  content_partial TEXT,
                  started_at TEXT,
                  paused_at TEXT,
                  ended_at TEXT,
                  updated_at TEXT NOT NULL,
                  PRIMARY KEY (match_id, speech_id)
                );

                CREATE TABLE IF NOT EXISTS structured_transcript_segments (
                  match_id TEXT NOT NULL,
                  id TEXT NOT NULL,
                  speech_id TEXT,
                  phase_id TEXT,
                  speaker_id TEXT,
                  speaker_label TEXT,
                  source TEXT,
                  is_final INTEGER NOT NULL,
                  turn_index INTEGER,
                  valid INTEGER NOT NULL,
                  invalid_reason TEXT,
                  text TEXT NOT NULL,
                  created_at TEXT,
                  updated_at TEXT NOT NULL,
                  PRIMARY KEY (match_id, id)
                );

                CREATE TABLE IF NOT EXISTS structured_speech_revisions (
                  match_id TEXT NOT NULL,
                  id TEXT NOT NULL,
                  speech_id TEXT,
                  before_text TEXT,
                  after_text TEXT,
                  valid INTEGER NOT NULL,
                  reason TEXT,
                  editor_actor_id TEXT,
                  created_at TEXT,
                  updated_at TEXT NOT NULL,
                  PRIMARY KEY (match_id, id)
                );

                CREATE TABLE IF NOT EXISTS structured_agent_status (
                  match_id TEXT NOT NULL,
                  speaker_id TEXT NOT NULL,
                  name TEXT NOT NULL,
                  model TEXT,
                  status TEXT NOT NULL,
                  detail TEXT,
                  endpoint TEXT,
                  latency_ms INTEGER,
                  last_health_at TEXT,
                  updated_at TEXT NOT NULL,
                  PRIMARY KEY (match_id, speaker_id)
                );

                CREATE TABLE IF NOT EXISTS structured_agent_configs (
                  match_id TEXT NOT NULL,
                  id TEXT NOT NULL,
                  name TEXT NOT NULL,
                  provider_type TEXT NOT NULL,
                  model_name TEXT,
                  model_kind TEXT,
                  endpoint TEXT,
                  base_url TEXT,
                  api_key_env TEXT,
                  timeout_ms INTEGER,
                  enabled INTEGER NOT NULL,
                  created_at TEXT,
                  updated_at TEXT NOT NULL,
                  PRIMARY KEY (match_id, id)
                );

                CREATE TABLE IF NOT EXISTS structured_audio_assets (
                  match_id TEXT NOT NULL,
                  id TEXT NOT NULL,
                  phase_id TEXT,
                  speech_id TEXT,
                  speaker_id TEXT,
                  file_path TEXT,
                  mime_type TEXT,
                  duration_ms INTEGER,
                  size_bytes INTEGER NOT NULL,
                  chunk_count INTEGER NOT NULL,
                  status TEXT NOT NULL,
                  created_at TEXT,
                  completed_at TEXT,
                  updated_at TEXT NOT NULL,
                  PRIMARY KEY (match_id, id)
                );

                CREATE TABLE IF NOT EXISTS structured_audio_chunks (
                  match_id TEXT NOT NULL,
                  asset_id TEXT NOT NULL,
                  chunk_index INTEGER NOT NULL,
                  speech_id TEXT,
                  file_path TEXT,
                  size_bytes INTEGER NOT NULL,
                  mime_type TEXT,
                  duration_ms INTEGER,
                  created_at TEXT,
                  updated_at TEXT NOT NULL,
                  PRIMARY KEY (match_id, asset_id, chunk_index)
                );

                CREATE TABLE IF NOT EXISTS structured_votes (
                  match_id TEXT PRIMARY KEY,
                  window_status TEXT NOT NULL,
                  audience_count INTEGER NOT NULL,
                  judge_published INTEGER NOT NULL,
                  audience_published INTEGER NOT NULL,
                  winner_side TEXT,
                  best_speaker_id TEXT,
                  vote_state_json TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS structured_runtime_settings (
                  match_id TEXT NOT NULL,
                  key TEXT NOT NULL,
                  label TEXT,
                  value_json TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  PRIMARY KEY (match_id, key)
                );
                """
            )
            self._migrate_log_classification(conn)

    def _migrate_log_classification(self, conn: sqlite3.Connection) -> None:
        """Additively add the multi-level log classification columns (性质/时机).

        Safe on existing prod databases: ALTER TABLE ADD COLUMN only runs for
        columns that are missing. `origin` distinguishes 正式/测试; phase/scene
        capture 请求时机.
        """
        columns = {
            "origin": "TEXT NOT NULL DEFAULT 'live'",
            "phase_id": "TEXT",
            "phase_name": "TEXT",
            "screen_scene": "TEXT",
        }
        for table in ("audit_logs", "agent_requests", "speech_service_requests"):
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for name, decl in columns.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    def load_snapshot(self, key: str = "demo_snapshot") -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute("SELECT value_json FROM app_state WHERE key = ?", (key,)).fetchone()
        if not row:
            return None
        return json.loads(row["value_json"])

    def save_snapshot(
        self, snapshot: Dict[str, Any], updated_at: str, key: str = "demo_snapshot", *, sync_structured: bool = True
    ) -> None:
        # app_state（实时唯一真相，崩溃恢复依据）始终写入——很便宜。结构化镜像表（仅供导出/数据视图）
        # 每次都整段 DELETE+INSERT 本场所有 transcript/audio_chunk，是 O(本场累计) 的开销，放在每个
        # tts.sentence_ready 的落盘里会拖慢首句出声与连贯播放。高频热点用 sync_structured=False 跳过，
        # 在发言结束/阶段切换等关键节点再整体同步——导出仍正确，只是发言进行中略有滞后。
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO app_state (key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                  value_json = excluded.value_json,
                  updated_at = excluded.updated_at
                """,
                (key, json.dumps(snapshot, ensure_ascii=False), updated_at),
            )
            if sync_structured:
                self._sync_structured_snapshot(conn, snapshot, updated_at)

    def get_app_state(self, key: str) -> Optional[Dict[str, Any]]:
        """Raw app_state read (no structured mirroring) — used for the multi-match registry
        and inactive-match snapshot slots."""
        with self.connect() as conn:
            row = conn.execute("SELECT value_json FROM app_state WHERE key = ?", (key,)).fetchone()
        if not row:
            return None
        return json.loads(row["value_json"])

    def set_app_state(self, key: str, value: Dict[str, Any], updated_at: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO app_state (key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                  value_json = excluded.value_json,
                  updated_at = excluded.updated_at
                """,
                (key, json.dumps(value, ensure_ascii=False), updated_at),
            )

    def delete_app_state(self, key: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM app_state WHERE key = ?", (key,))

    def clear_match_history(self, match_id: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM events WHERE match_id = ?", (match_id,))
            conn.execute("DELETE FROM audit_logs WHERE match_id = ?", (match_id,))
            conn.execute("DELETE FROM agent_requests WHERE match_id = ?", (match_id,))
            conn.execute("DELETE FROM speech_service_requests WHERE match_id = ?", (match_id,))
            conn.execute("DELETE FROM export_bundles WHERE match_id = ?", (match_id,))
            conn.execute("DELETE FROM structured_matches WHERE id = ?", (match_id,))
            for table in STRUCTURED_TABLES:
                conn.execute(f"DELETE FROM {table} WHERE match_id = ?", (match_id,))

    def load_events(self, match_id: str, limit: int = 200) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, match_id, seq, type, payload_json, actor_type, actor_id, created_at
                FROM events
                WHERE match_id = ?
                ORDER BY seq DESC
                LIMIT ?
                """,
                (match_id, limit),
            ).fetchall()
        events = []
        for row in reversed(rows):
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            events.append(item)
        return events

    def save_event(self, event: Dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO events
                  (id, match_id, seq, type, payload_json, actor_type, actor_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event["id"],
                    event["match_id"],
                    event["seq"],
                    event["type"],
                    json.dumps(event["payload"], ensure_ascii=False),
                    event["actor_type"],
                    event.get("actor_id"),
                    event["created_at"],
                ),
            )

    def save_audit_log(
        self,
        *,
        audit_id: str,
        match_id: str,
        actor_type: str,
        actor_id: Optional[str],
        action: str,
        target_type: Optional[str],
        target_id: Optional[str],
        request: Dict[str, Any],
        result: str,
        error_message: Optional[str],
        created_at: str,
        origin: str = "live",
        phase_id: Optional[str] = None,
        phase_name: Optional[str] = None,
        screen_scene: Optional[str] = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO audit_logs
                  (id, match_id, actor_type, actor_id, action, target_type, target_id,
                   request_json, result, error_message, created_at, origin, phase_id, phase_name, screen_scene)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    audit_id,
                    match_id,
                    actor_type,
                    actor_id,
                    action,
                    target_type,
                    target_id,
                    json.dumps(request, ensure_ascii=False),
                    result,
                    error_message,
                    created_at,
                    origin,
                    phase_id,
                    phase_name,
                    screen_scene,
                ),
            )

    def load_audit_logs(self, match_id: str, limit: int = 30) -> List[Dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 200))
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, match_id, actor_type, actor_id, action, target_type, target_id,
                       request_json, result, error_message, created_at, origin, phase_id, phase_name, screen_scene
                FROM audit_logs
                WHERE match_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (match_id, safe_limit),
            ).fetchall()

        logs = []
        for row in rows:
            item = dict(row)
            item["request"] = json.loads(item.pop("request_json"))
            logs.append(item)
        return logs

    def load_audit_log_summaries(self, match_id: str, limit: int = 200) -> List[Dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 10000))
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, match_id, actor_type, actor_id, action, target_type, target_id,
                       result, error_message, created_at, origin, phase_id, phase_name, screen_scene,
                       substr(request_json, 1, ?) AS request_preview,
                       length(request_json) AS request_bytes
                FROM audit_logs
                WHERE match_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (LOG_PREVIEW_CHARS, match_id, safe_limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def load_audit_log(self, match_id: str, audit_id: str) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT id, match_id, actor_type, actor_id, action, target_type, target_id,
                       request_json, result, error_message, created_at, origin, phase_id, phase_name, screen_scene
                FROM audit_logs
                WHERE match_id = ? AND id = ?
                LIMIT 1
                """,
                (match_id, audit_id),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["request"] = json.loads(item.pop("request_json"))
        return item

    def save_match_archive(
        self,
        *,
        archive_id: str,
        archived_match_id: str,
        new_match_id: str,
        snapshot: Dict[str, Any],
        export_bundle: Dict[str, Any],
        created_at: str,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO matches_archive
                  (id, archived_match_id, new_match_id, snapshot_json, export_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    archive_id,
                    archived_match_id,
                    new_match_id,
                    json.dumps(snapshot, ensure_ascii=False),
                    json.dumps(export_bundle, ensure_ascii=False),
                    created_at,
                ),
            )

    def load_match_archives(self, limit: int = 20) -> List[Dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 100))
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, archived_match_id, new_match_id, snapshot_json, export_json, created_at
                FROM matches_archive
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
        archives = []
        for row in rows:
            item = dict(row)
            item["snapshot"] = json.loads(item.pop("snapshot_json"))
            item["export_bundle"] = json.loads(item.pop("export_json"))
            archives.append(item)
        return archives

    def save_agent_request_started(
        self,
        *,
        match_id: str,
        task_id: str,
        speech_id: str,
        speaker_id: str,
        endpoint: str,
        request: Dict[str, Any],
        started_at: str,
        origin: str = "live",
        phase_id: Optional[str] = None,
        phase_name: Optional[str] = None,
        screen_scene: Optional[str] = None,
    ) -> None:
        request_id = agent_request_id(match_id, task_id)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO agent_requests
                  (id, match_id, task_id, speech_id, speaker_id, endpoint, status,
                   request_json, response_text, error_code, error_message, latency_ms,
                   started_at, completed_at, updated_at, origin, phase_id, phase_name, screen_scene)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, NULL, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  speech_id = excluded.speech_id,
                  speaker_id = excluded.speaker_id,
                  endpoint = excluded.endpoint,
                  status = excluded.status,
                  request_json = excluded.request_json,
                  response_text = NULL,
                  error_code = NULL,
                  error_message = NULL,
                  latency_ms = NULL,
                  started_at = excluded.started_at,
                  completed_at = NULL,
                  updated_at = excluded.updated_at,
                  origin = excluded.origin,
                  phase_id = excluded.phase_id,
                  phase_name = excluded.phase_name,
                  screen_scene = excluded.screen_scene
                """,
                (
                    request_id,
                    match_id,
                    task_id,
                    speech_id,
                    speaker_id,
                    endpoint,
                    "streaming",
                    json.dumps(request, ensure_ascii=False),
                    started_at,
                    started_at,
                    origin,
                    phase_id,
                    phase_name,
                    screen_scene,
                ),
            )

    def finish_agent_request(
        self,
        *,
        match_id: str,
        task_id: str,
        status: str,
        response_text: Optional[str] = None,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
        latency_ms: Optional[int] = None,
        completed_at: str,
    ) -> None:
        request_id = agent_request_id(match_id, task_id)
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE agent_requests
                SET status = ?,
                    response_text = ?,
                    error_code = ?,
                    error_message = ?,
                    latency_ms = ?,
                    completed_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    response_text,
                    error_code,
                    error_message,
                    latency_ms,
                    completed_at,
                    completed_at,
                    request_id,
                ),
            )

    def clear_request_logs(self, match_id: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM agent_requests WHERE match_id = ?", (match_id,))
            conn.execute("DELETE FROM speech_service_requests WHERE match_id = ?", (match_id,))
            conn.execute("DELETE FROM audit_logs WHERE match_id = ?", (match_id,))

    def load_agent_requests(self, match_id: str, limit: int = 200) -> List[Dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 10000))
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, match_id, task_id, speech_id, speaker_id, endpoint, status,
                       request_json, response_text, error_code, error_message, latency_ms,
                       started_at, completed_at, updated_at, origin, phase_id, phase_name, screen_scene
                FROM agent_requests
                WHERE match_id = ?
                ORDER BY started_at DESC, id DESC
                LIMIT ?
                """,
                (match_id, safe_limit),
            ).fetchall()
        requests = []
        for row in rows:
            item = dict(row)
            item["request"] = json.loads(item.pop("request_json"))
            requests.append(item)
        return requests

    def load_agent_request_summaries(self, match_id: str, limit: int = 200) -> List[Dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 10000))
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, match_id, task_id, speech_id, speaker_id, endpoint, status,
                       error_code, error_message, latency_ms, started_at, completed_at, updated_at,
                       origin, phase_id, phase_name, screen_scene,
                       substr(request_json, 1, ?) AS request_preview,
                       length(request_json) AS request_bytes,
                       substr(COALESCE(response_text, ''), 1, ?) AS response_preview,
                       length(COALESCE(response_text, '')) AS response_bytes
                FROM agent_requests
                WHERE match_id = ?
                ORDER BY started_at DESC, id DESC
                LIMIT ?
                """,
                (LOG_PREVIEW_CHARS, LOG_PREVIEW_CHARS, match_id, safe_limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def load_agent_request(self, match_id: str, request_row_id: str) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT id, match_id, task_id, speech_id, speaker_id, endpoint, status,
                       request_json, response_text, error_code, error_message, latency_ms,
                       started_at, completed_at, updated_at, origin, phase_id, phase_name, screen_scene
                FROM agent_requests
                WHERE match_id = ? AND id = ?
                LIMIT 1
                """,
                (match_id, request_row_id),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["request"] = json.loads(item.pop("request_json"))
        return item

    def save_speech_service_request_started(
        self,
        *,
        match_id: str,
        request_id: str,
        service: str,
        operation: str,
        request: Dict[str, Any],
        started_at: str,
        speech_id: Optional[str] = None,
        speaker_id: Optional[str] = None,
        origin: str = "live",
        phase_id: Optional[str] = None,
        phase_name: Optional[str] = None,
        screen_scene: Optional[str] = None,
    ) -> None:
        row_id = speech_service_request_id(match_id, request_id)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO speech_service_requests
                  (id, match_id, request_id, service, operation, speech_id, speaker_id,
                   status, request_json, response_json, error_code, error_message,
                   latency_ms, started_at, completed_at, updated_at, origin, phase_id, phase_name, screen_scene)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', NULL, NULL, NULL, ?, NULL, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  service = excluded.service,
                  operation = excluded.operation,
                  speech_id = excluded.speech_id,
                  speaker_id = excluded.speaker_id,
                  status = excluded.status,
                  request_json = excluded.request_json,
                  response_json = '{}',
                  error_code = NULL,
                  error_message = NULL,
                  latency_ms = NULL,
                  started_at = excluded.started_at,
                  completed_at = NULL,
                  updated_at = excluded.updated_at,
                  origin = excluded.origin,
                  phase_id = excluded.phase_id,
                  phase_name = excluded.phase_name,
                  screen_scene = excluded.screen_scene
                """,
                (
                    row_id,
                    match_id,
                    request_id,
                    service,
                    operation,
                    speech_id,
                    speaker_id,
                    "running",
                    json.dumps(request, ensure_ascii=False),
                    started_at,
                    started_at,
                    origin,
                    phase_id,
                    phase_name,
                    screen_scene,
                ),
            )

    def finish_speech_service_request(
        self,
        *,
        match_id: str,
        request_id: str,
        status: str,
        response: Optional[Dict[str, Any]] = None,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
        latency_ms: Optional[int] = None,
        completed_at: str,
    ) -> None:
        row_id = speech_service_request_id(match_id, request_id)
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE speech_service_requests
                SET status = ?,
                    response_json = ?,
                    error_code = ?,
                    error_message = ?,
                    latency_ms = ?,
                    completed_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    json.dumps(response or {}, ensure_ascii=False),
                    error_code,
                    error_message,
                    latency_ms,
                    completed_at,
                    completed_at,
                    row_id,
                ),
            )

    def load_speech_service_requests(self, match_id: str, limit: int = 200) -> List[Dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 10000))
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, match_id, request_id, service, operation, speech_id, speaker_id,
                       status, request_json, response_json, error_code, error_message,
                       latency_ms, started_at, completed_at, updated_at, origin, phase_id, phase_name, screen_scene
                FROM speech_service_requests
                WHERE match_id = ?
                ORDER BY started_at DESC, id DESC
                LIMIT ?
                """,
                (match_id, safe_limit),
            ).fetchall()
        requests = []
        for row in rows:
            item = dict(row)
            item["request"] = json.loads(item.pop("request_json"))
            item["response"] = json.loads(item.pop("response_json"))
            requests.append(item)
        return requests

    def load_speech_service_request_summaries(self, match_id: str, limit: int = 200) -> List[Dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 10000))
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, match_id, request_id, service, operation, speech_id, speaker_id,
                       status, error_code, error_message, latency_ms, started_at, completed_at, updated_at,
                       origin, phase_id, phase_name, screen_scene,
                       substr(request_json, 1, ?) AS request_preview,
                       length(request_json) AS request_bytes,
                       substr(COALESCE(response_json, '{}'), 1, ?) AS response_preview,
                       length(COALESCE(response_json, '{}')) AS response_bytes
                FROM speech_service_requests
                WHERE match_id = ?
                ORDER BY started_at DESC, id DESC
                LIMIT ?
                """,
                (LOG_PREVIEW_CHARS, LOG_PREVIEW_CHARS, match_id, safe_limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def load_speech_service_request(self, match_id: str, request_row_id: str) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT id, match_id, request_id, service, operation, speech_id, speaker_id,
                       status, request_json, response_json, error_code, error_message,
                       latency_ms, started_at, completed_at, updated_at, origin, phase_id, phase_name, screen_scene
                FROM speech_service_requests
                WHERE match_id = ? AND id = ?
                LIMIT 1
                """,
                (match_id, request_row_id),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["request"] = json.loads(item.pop("request_json"))
        item["response"] = json.loads(item.pop("response_json"))
        return item

    def save_export_bundle(self, bundle: Dict[str, Any]) -> None:
        entries = bundle.get("entries") if isinstance(bundle.get("entries"), list) else []
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO export_bundles
                  (export_id, match_id, file_path, download_url, size_bytes, entry_count, entries_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(export_id) DO UPDATE SET
                  file_path = excluded.file_path,
                  download_url = excluded.download_url,
                  size_bytes = excluded.size_bytes,
                  entry_count = excluded.entry_count,
                  entries_json = excluded.entries_json,
                  created_at = excluded.created_at
                """,
                (
                    bundle.get("export_id", ""),
                    bundle.get("match_id", ""),
                    bundle.get("file_path", ""),
                    bundle.get("download_url", ""),
                    int(bundle.get("size_bytes") or 0),
                    len(entries),
                    json.dumps(entries, ensure_ascii=False),
                    bundle.get("created_at", ""),
                ),
            )

    def load_export_bundles(self, match_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 1000))
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT export_id, match_id, file_path, download_url, size_bytes, entry_count, entries_json, created_at
                FROM export_bundles
                WHERE match_id = ?
                ORDER BY created_at DESC, export_id DESC
                LIMIT ?
                """,
                (match_id, safe_limit),
            ).fetchall()
        bundles = []
        for row in rows:
            item = dict(row)
            item["entries"] = json.loads(item.pop("entries_json"))
            bundles.append(item)
        return bundles

    def load_structured_counts(self, match_id: str) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        with self.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM structured_matches WHERE id = ?", (match_id,)).fetchone()
            counts["matches"] = int(row["count"] if row else 0)
            for table, label in STRUCTURED_COUNT_TABLES.items():
                row = conn.execute(f"SELECT COUNT(*) AS count FROM {table} WHERE match_id = ?", (match_id,)).fetchone()
                counts[label] = int(row["count"] if row else 0)
            for table, label in STRUCTURED_AUX_COUNT_TABLES.items():
                row = conn.execute(f"SELECT COUNT(*) AS count FROM {table} WHERE match_id = ?", (match_id,)).fetchone()
                counts[label] = int(row["count"] if row else 0)
        return counts

    def load_structured_export(self, match_id: str) -> Dict[str, List[Dict[str, Any]]]:
        with self.connect() as conn:
            match_rows = conn.execute(
                """
                SELECT id, title, topic, status, screen_scene, live_mode, current_phase_id, updated_at
                FROM structured_matches
                WHERE id = ?
                """,
                (match_id,),
            ).fetchall()
            data: Dict[str, List[Dict[str, Any]]] = {
                "matches": [dict(row) for row in match_rows],
            }
            for table, label, order_by in STRUCTURED_EXPORT_TABLES:
                rows = conn.execute(
                    f"SELECT * FROM {table} WHERE match_id = ? ORDER BY {order_by}",
                    (match_id,),
                ).fetchall()
                data[label] = [dict(row) for row in rows]
            agent_rows = conn.execute(
                """
                SELECT id, match_id, task_id, speech_id, speaker_id, endpoint, status,
                       request_json, response_text, error_code, error_message, latency_ms,
                       started_at, completed_at, updated_at
                FROM agent_requests
                WHERE match_id = ?
                ORDER BY started_at, id
                """,
                (match_id,),
            ).fetchall()
            data["agent_requests"] = []
            for row in agent_rows:
                item = dict(row)
                item["request"] = json.loads(item.pop("request_json"))
                data["agent_requests"].append(item)
            speech_service_rows = conn.execute(
                """
                SELECT id, match_id, request_id, service, operation, speech_id, speaker_id,
                       status, request_json, response_json, error_code, error_message,
                       latency_ms, started_at, completed_at, updated_at
                FROM speech_service_requests
                WHERE match_id = ?
                ORDER BY started_at, id
                """,
                (match_id,),
            ).fetchall()
            data["speech_service_requests"] = []
            for row in speech_service_rows:
                item = dict(row)
                item["request"] = json.loads(item.pop("request_json"))
                item["response"] = json.loads(item.pop("response_json"))
                data["speech_service_requests"].append(item)
            export_rows = conn.execute(
                """
                SELECT export_id, match_id, file_path, download_url, size_bytes, entry_count, entries_json, created_at
                FROM export_bundles
                WHERE match_id = ?
                ORDER BY created_at, export_id
                """,
                (match_id,),
            ).fetchall()
            data["export_bundles"] = []
            for row in export_rows:
                item = dict(row)
                item["entries"] = json.loads(item.pop("entries_json"))
                data["export_bundles"].append(item)

        for row in data.get("votes", []):
            raw_vote_state = row.pop("vote_state_json", "{}")
            try:
                vote_state = json.loads(raw_vote_state)
            except (TypeError, json.JSONDecodeError):
                vote_state = {}
            row["vote_state"] = self._public_vote_state_for_export(vote_state)
        for row in data.get("runtime_settings", []):
            raw_value = row.pop("value_json", "{}")
            try:
                row["value"] = json.loads(raw_value)
            except (TypeError, json.JSONDecodeError):
                row["value"] = {}
        return data

    def _public_vote_state_for_export(self, vote_state: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(vote_state, dict):
            return {}
        cleaned = dict(vote_state)
        cleaned.pop("audience_vote_keys", None)
        cleaned.pop("used_audience_tokens", None)
        cleaned.pop("audience_votes", None)
        return cleaned

    def _sync_structured_snapshot(self, conn: sqlite3.Connection, snapshot: Dict[str, Any], updated_at: str) -> None:
        match = snapshot.get("match", {})
        match_id = str(match.get("id") or "")
        if not match_id:
            return

        conn.execute("DELETE FROM structured_matches WHERE id = ?", (match_id,))
        for table in STRUCTURED_TABLES:
            conn.execute(f"DELETE FROM {table} WHERE match_id = ?", (match_id,))

        conn.execute(
            """
            INSERT INTO structured_matches
              (id, title, topic, status, screen_scene, live_mode, current_phase_id, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                match_id,
                str(match.get("title") or ""),
                str(match.get("topic") or ""),
                str(match.get("status") or ""),
                str(match.get("screen_scene") or ""),
                str(match.get("live_mode") or ""),
                match.get("current_phase_id"),
                updated_at,
            ),
        )

        conn.executemany(
            """
            INSERT INTO structured_phases
              (match_id, id, phase_key, name, phase_type, display_order, side, speaker_seat,
               duration_seconds, side_total_seconds, turn_seconds, speaker_selector, status, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    match_id,
                    str(phase.get("id") or ""),
                    str(phase.get("phase_key") or ""),
                    str(phase.get("name") or ""),
                    str(phase.get("phase_type") or ""),
                    int(phase.get("display_order") or 0),
                    str(phase.get("side") or ""),
                    phase.get("speaker_seat"),
                    int(phase.get("duration_seconds") or 0),
                    phase.get("side_total_seconds"),
                    phase.get("turn_seconds"),
                    str(phase.get("speaker_selector") or ""),
                    str(phase.get("status") or ""),
                    updated_at,
                )
                for phase in snapshot.get("phases", [])
            ],
        )

        conn.executemany(
            """
            INSERT INTO structured_slots
              (match_id, speaker_id, team_id, side, seat, name, speaker_type, model_name,
               model_kind, agent_endpoint, status, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    match_id,
                    str(speaker.get("id") or ""),
                    str(speaker.get("team_id") or ""),
                    str(speaker.get("side") or ""),
                    int(speaker.get("seat") or 0),
                    str(speaker.get("name") or ""),
                    str(speaker.get("speaker_type") or ""),
                    speaker.get("model_name"),
                    speaker.get("model_kind"),
                    speaker.get("agent_endpoint"),
                    speaker.get("status"),
                    updated_at,
                )
                for speaker in snapshot.get("speakers", [])
            ],
        )

        speech_rows = self._structured_speech_rows(snapshot, match_id, updated_at)
        conn.executemany(
            """
            INSERT INTO structured_speeches
              (match_id, speech_id, phase_id, speaker_id, side, turn_index, source, state,
               content_final, content_partial, started_at, paused_at, ended_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            speech_rows,
        )

        conn.executemany(
            """
            INSERT INTO structured_transcript_segments
              (match_id, id, speech_id, phase_id, speaker_id, speaker_label, source,
               is_final, turn_index, valid, invalid_reason, text, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    match_id,
                    str(segment.get("id") or segment.get("speech_id") or ""),
                    segment.get("speech_id") or segment.get("id"),
                    segment.get("phase_id"),
                    segment.get("speaker_id"),
                    segment.get("speaker_label"),
                    segment.get("source"),
                    1 if segment.get("is_final") else 0,
                    segment.get("turn_index"),
                    0 if segment.get("valid") is False else 1,
                    segment.get("invalid_reason"),
                    str(segment.get("text") or ""),
                    segment.get("created_at"),
                    updated_at,
                )
                for segment in snapshot.get("recent_transcript", [])
            ],
        )

        conn.executemany(
            """
            INSERT INTO structured_speech_revisions
              (match_id, id, speech_id, before_text, after_text, valid, reason, editor_actor_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    match_id,
                    str(revision.get("id") or ""),
                    revision.get("speech_id"),
                    revision.get("before_text"),
                    revision.get("after_text"),
                    0 if revision.get("valid") is False else 1,
                    revision.get("reason"),
                    revision.get("editor_actor_id"),
                    revision.get("created_at"),
                    updated_at,
                )
                for revision in snapshot.get("speech_revisions", [])
            ],
        )

        conn.executemany(
            """
            INSERT INTO structured_agent_status
              (match_id, speaker_id, name, model, status, detail, endpoint, latency_ms, last_health_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    match_id,
                    str(agent.get("speaker_id") or ""),
                    str(agent.get("name") or ""),
                    agent.get("model"),
                    str(agent.get("status") or ""),
                    agent.get("detail"),
                    agent.get("endpoint"),
                    agent.get("latency_ms"),
                    agent.get("last_health_at"),
                    updated_at,
                )
                for agent in snapshot.get("agent_status", [])
            ],
        )

        conn.executemany(
            """
            INSERT INTO structured_agent_configs
              (match_id, id, name, provider_type, model_name, model_kind, endpoint,
               base_url, api_key_env, timeout_ms, enabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    match_id,
                    str(config.get("id") or ""),
                    str(config.get("name") or ""),
                    str(config.get("provider_type") or ""),
                    config.get("model_name"),
                    config.get("model_kind"),
                    config.get("endpoint"),
                    config.get("base_url"),
                    config.get("api_key_env"),
                    config.get("timeout_ms"),
                    1 if config.get("enabled", True) else 0,
                    config.get("created_at"),
                    updated_at,
                )
                for config in snapshot.get("agent_configs", [])
            ],
        )

        audio_assets = snapshot.get("audio_assets", [])
        conn.executemany(
            """
            INSERT INTO structured_audio_assets
              (match_id, id, phase_id, speech_id, speaker_id, file_path, mime_type, duration_ms,
               size_bytes, chunk_count, status, created_at, completed_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    match_id,
                    str(asset.get("id") or ""),
                    asset.get("phase_id"),
                    asset.get("speech_id"),
                    asset.get("speaker_id"),
                    asset.get("file_path"),
                    asset.get("mime_type"),
                    asset.get("duration_ms"),
                    int(asset.get("size_bytes") or 0),
                    int(asset.get("chunk_count") or 0),
                    str(asset.get("status") or ""),
                    asset.get("created_at"),
                    asset.get("completed_at"),
                    updated_at,
                )
                for asset in audio_assets
            ],
        )

        conn.executemany(
            """
            INSERT INTO structured_audio_chunks
              (match_id, asset_id, chunk_index, speech_id, file_path, size_bytes, mime_type, duration_ms, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    match_id,
                    str(asset.get("id") or ""),
                    int(chunk.get("chunk_index") or 0),
                    asset.get("speech_id"),
                    chunk.get("file_path"),
                    int(chunk.get("size_bytes") or 0),
                    chunk.get("mime_type"),
                    chunk.get("duration_ms"),
                    chunk.get("created_at"),
                    updated_at,
                )
                for asset in audio_assets
                for chunk in asset.get("chunks", [])
            ],
        )

        vote_state = snapshot.get("vote_state", {})
        conn.execute(
            """
            INSERT INTO structured_votes
              (match_id, window_status, audience_count, judge_published, audience_published,
               winner_side, best_speaker_id, vote_state_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                match_id,
                str(vote_state.get("window_status") or ""),
                int(vote_state.get("audience_count", vote_state.get("audience_summary", {}).get("total", 0)) or 0),
                1 if vote_state.get("judge_published") else 0,
                1 if vote_state.get("audience_published") else 0,
                vote_state.get("winner_side"),
                vote_state.get("best_speaker_id"),
                json.dumps(vote_state, ensure_ascii=False),
                updated_at,
            ),
        )

        audio_output = snapshot.get("audio_output", {})
        conn.execute(
            """
            INSERT INTO structured_runtime_settings
              (match_id, key, label, value_json, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                match_id,
                "audio_output",
                str(audio_output.get("label") or ""),
                json.dumps(audio_output, ensure_ascii=False),
                str(audio_output.get("updated_at") or updated_at),
            ),
        )

    def _structured_speech_rows(self, snapshot: Dict[str, Any], match_id: str, updated_at: str) -> List[tuple[Any, ...]]:
        rows_by_id: Dict[str, tuple[Any, ...]] = {}

        def add_speech(speech: Dict[str, Any], fallback_id: str = "") -> None:
            speech_id = str(speech.get("id") or speech.get("speech_id") or fallback_id)
            if not speech_id:
                return
            rows_by_id[speech_id] = (
                match_id,
                speech_id,
                speech.get("phase_id"),
                speech.get("speaker_id"),
                speech.get("side"),
                speech.get("turn_index"),
                speech.get("source"),
                speech.get("state"),
                speech.get("content_final"),
                speech.get("content_partial"),
                speech.get("started_at"),
                speech.get("paused_at"),
                speech.get("ended_at"),
                updated_at,
            )

        current_speech = snapshot.get("current_speech")
        if isinstance(current_speech, dict):
            add_speech(current_speech)

        for segment in snapshot.get("recent_transcript", []):
            speech_id = str(segment.get("speech_id") or segment.get("id") or "")
            if not speech_id or speech_id in rows_by_id:
                continue
            add_speech(
                {
                    "id": speech_id,
                    "phase_id": segment.get("phase_id"),
                    "speaker_id": segment.get("speaker_id"),
                    "side": self._speaker_side(snapshot, segment.get("speaker_id")),
                    "turn_index": segment.get("turn_index"),
                    "source": segment.get("source"),
                    "state": "ended" if segment.get("is_final") else "speaking",
                    "content_final": segment.get("text") if segment.get("is_final") else "",
                    "content_partial": "" if segment.get("is_final") else segment.get("text"),
                    "started_at": segment.get("created_at"),
                    "ended_at": segment.get("created_at") if segment.get("is_final") else None,
                },
                speech_id,
            )

        for asset in snapshot.get("audio_assets", []):
            speech_id = str(asset.get("speech_id") or "")
            if not speech_id or speech_id in rows_by_id:
                continue
            add_speech(
                {
                    "id": speech_id,
                    "phase_id": asset.get("phase_id"),
                    "speaker_id": asset.get("speaker_id"),
                    "side": self._speaker_side(snapshot, asset.get("speaker_id")),
                    "source": "human_asr",
                    "state": "ended" if asset.get("status") == "completed" else "speaking",
                    "started_at": asset.get("created_at"),
                    "ended_at": asset.get("completed_at"),
                },
                speech_id,
            )

        return list(rows_by_id.values())

    def _speaker_side(self, snapshot: Dict[str, Any], speaker_id: Any) -> Optional[str]:
        for speaker in snapshot.get("speakers", []):
            if speaker.get("id") == speaker_id:
                return speaker.get("side")
        return None


STRUCTURED_TABLES = [
    "structured_phases",
    "structured_slots",
    "structured_speeches",
    "structured_transcript_segments",
    "structured_speech_revisions",
    "structured_agent_status",
    "structured_agent_configs",
    "structured_audio_assets",
    "structured_audio_chunks",
    "structured_votes",
    "structured_runtime_settings",
]

STRUCTURED_COUNT_TABLES = {
    "structured_phases": "phases",
    "structured_slots": "slots",
    "structured_speeches": "speeches",
    "structured_transcript_segments": "transcript_segments",
    "structured_speech_revisions": "speech_revisions",
    "structured_agent_status": "agent_status",
    "structured_agent_configs": "agent_configs",
    "structured_audio_assets": "audio_assets",
    "structured_audio_chunks": "audio_chunks",
    "structured_votes": "votes",
    "structured_runtime_settings": "runtime_settings",
}

STRUCTURED_AUX_COUNT_TABLES = {
    "agent_requests": "agent_requests",
    "speech_service_requests": "speech_service_requests",
    "export_bundles": "export_bundles",
}

STRUCTURED_EXPORT_TABLES = [
    ("structured_phases", "phases", "display_order, id"),
    ("structured_slots", "slots", "side, seat, speaker_id"),
    ("structured_speeches", "speeches", "COALESCE(started_at, updated_at), speech_id"),
    ("structured_transcript_segments", "transcript_segments", "COALESCE(created_at, updated_at), id"),
    ("structured_speech_revisions", "speech_revisions", "COALESCE(created_at, updated_at), id"),
    ("structured_agent_status", "agent_status", "speaker_id"),
    ("structured_agent_configs", "agent_configs", "provider_type, name, id"),
    ("structured_audio_assets", "audio_assets", "COALESCE(created_at, updated_at), id"),
    ("structured_audio_chunks", "audio_chunks", "asset_id, chunk_index"),
    ("structured_votes", "votes", "match_id"),
    ("structured_runtime_settings", "runtime_settings", "key"),
]
