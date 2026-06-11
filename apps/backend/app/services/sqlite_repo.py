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


class SQLiteRepository:
    """Small persistence layer for the live MVP slice.

    The normalized tables from docs/02 are still the long-term target. This
    repository gives us durable snapshots and event history immediately, while
    keeping the service API stable for the next migration step.
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
                """
            )

    def load_snapshot(self, key: str = "demo_snapshot") -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute("SELECT value_json FROM app_state WHERE key = ?", (key,)).fetchone()
        if not row:
            return None
        return json.loads(row["value_json"])

    def save_snapshot(self, snapshot: Dict[str, Any], updated_at: str, key: str = "demo_snapshot") -> None:
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

    def clear_match_history(self, match_id: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM events WHERE match_id = ?", (match_id,))
            conn.execute("DELETE FROM audit_logs WHERE match_id = ?", (match_id,))

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
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO audit_logs
                  (id, match_id, actor_type, actor_id, action, target_type, target_id,
                   request_json, result, error_message, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                ),
            )

    def load_audit_logs(self, match_id: str, limit: int = 30) -> List[Dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 200))
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, match_id, actor_type, actor_id, action, target_type, target_id,
                       request_json, result, error_message, created_at
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
