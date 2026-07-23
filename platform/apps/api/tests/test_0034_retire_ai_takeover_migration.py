from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def _load_migration():
    path = Path(__file__).parents[1] / "alembic" / "versions" / "0034_retire_ai_takeover.py"
    spec = importlib.util.spec_from_file_location("migration_0034_retire_ai_takeover", path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def test_0034_normalizes_every_retired_seat_and_expires_pending_requests(tmp_path: Path) -> None:
    migration = _load_migration()
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'retired-takeover.db'}")

    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                CREATE TABLE room_seats (
                    id VARCHAR(36) PRIMARY KEY,
                    occupant_type VARCHAR(16) NOT NULL,
                    user_id VARCHAR(36),
                    display_name VARCHAR(64) NOT NULL,
                    connected BOOLEAN NOT NULL,
                    disconnected_at DATETIME,
                    agent_profile_id VARCHAR(36)
                )
                """
            )
        )
        connection.execute(
            sa.text(
                """
                CREATE TABLE seat_restore_requests (
                    id VARCHAR(36) PRIMARY KEY,
                    status VARCHAR(16) NOT NULL,
                    resolution_reason VARCHAR(300) NOT NULL,
                    resolved_at DATETIME
                )
                """
            )
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO room_seats
                    (id, occupant_type, user_id, display_name, connected, agent_profile_id)
                VALUES
                    ('human-seat', 'ai_substitute', 'user-1', 'AI 接替·张三', 1, 'agent-1'),
                    ('orphan-seat', 'ai_substitute', NULL, 'AI 接替·未知', 1, 'agent-2'),
                    ('real-ai-seat', 'ai', NULL, '乾元', 1, 'agent-3')
                """
            )
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO seat_restore_requests
                    (id, status, resolution_reason)
                VALUES
                    ('pending', 'pending', ''),
                    ('approved', 'approved', '历史已完成')
                """
            )
        )

        context = MigrationContext.configure(connection)
        migration.op = Operations(context)
        migration.upgrade()

        seats = {
            row["id"]: row
            for row in connection.execute(sa.text("SELECT * FROM room_seats ORDER BY id")).mappings()
        }
        assert seats["human-seat"]["occupant_type"] == "human"
        assert seats["human-seat"]["display_name"] == "张三"
        assert seats["human-seat"]["connected"] == 0
        assert seats["human-seat"]["disconnected_at"] is not None
        assert seats["human-seat"]["agent_profile_id"] is None
        assert seats["orphan-seat"]["occupant_type"] == "open"
        assert seats["orphan-seat"]["display_name"] == "待加入"
        assert seats["orphan-seat"]["disconnected_at"] is None
        assert seats["real-ai-seat"]["occupant_type"] == "ai"
        assert seats["real-ai-seat"]["agent_profile_id"] == "agent-3"

        requests = {
            row["id"]: row
            for row in connection.execute(sa.text("SELECT * FROM seat_restore_requests ORDER BY id")).mappings()
        }
        assert requests["pending"]["status"] == "expired"
        assert "60 秒" in requests["pending"]["resolution_reason"]
        assert requests["pending"]["resolved_at"] is not None
        assert requests["approved"]["status"] == "approved"
        assert requests["approved"]["resolution_reason"] == "历史已完成"


def test_0034_is_safe_when_legacy_tables_do_not_exist(tmp_path: Path) -> None:
    migration = _load_migration()
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        migration.op = Operations(context)
        migration.upgrade()
