from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.schemas.requests import AutomationStageRequest
from app.services.room_service import free_turn_duration
from pydantic import ValidationError


def _load_migration():
    path = Path(__file__).parents[1] / "alembic" / "versions" / "0035_clamp_free_turn_duration.py"
    spec = importlib.util.spec_from_file_location("migration_0035_clamp_free_turn_duration", path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def test_free_turn_duration_never_exceeds_thirty_seconds() -> None:
    assert free_turn_duration({"turn_duration": 5}) == 5
    assert free_turn_duration({"turn_duration": 30}) == 30
    assert free_turn_duration({"turn_duration": 45}) == 30
    assert free_turn_duration({"turn_duration": "invalid"}) == 30
    assert free_turn_duration(None) == 30


def test_admin_template_rejects_a_free_turn_longer_than_thirty_seconds() -> None:
    valid = AutomationStageRequest(
        key="free",
        name="自由辩论",
        kind="free",
        duration=300,
        side="aff",
        turn_duration=30,
    )
    assert valid.turn_duration == 30
    with pytest.raises(ValidationError):
        AutomationStageRequest(
            key="free",
            name="自由辩论",
            kind="free",
            duration=300,
            side="aff",
            turn_duration=31,
        )


def test_0035_clamps_bound_templates_and_only_active_room_snapshots(tmp_path: Path) -> None:
    migration = _load_migration()
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'free-turn.db'}")
    metadata = sa.MetaData()
    templates = sa.Table(
        "automation_templates",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("stages", sa.JSON, nullable=False),
    )
    competitions = sa.Table(
        "competitions",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("automation_template_id", sa.String(36), nullable=False),
    )
    rooms = sa.Table(
        "rooms",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("template_snapshot", sa.JSON, nullable=False),
    )
    metadata.create_all(engine)
    long_flow = [
        {
            "key": "free",
            "name": "自由辩论",
            "kind": "free",
            "duration": 300,
            "side": "aff",
            "turn_duration": 45,
            "preparing_turn_remaining_seconds": 41,
            "paused_turn_remaining_seconds": 40,
            "human_turn_remaining_seconds": 39,
        },
        {"key": "judge", "name": "裁判", "kind": "judging", "duration": 30},
    ]
    with engine.begin() as connection:
        connection.execute(
            templates.insert(),
            [
                {"id": "current-template", "stages": long_flow},
                {"id": "historical-template", "stages": long_flow},
            ],
        )
        connection.execute(
            competitions.insert(),
            {"id": "competition", "automation_template_id": "current-template"},
        )
        connection.execute(
            rooms.insert(),
            [
                {"id": "active", "status": "paused", "template_snapshot": long_flow},
                {"id": "history", "status": "completed", "template_snapshot": long_flow},
            ],
        )
        context = MigrationContext.configure(connection)
        migration.op = Operations(context)
        migration.upgrade()

        current_template_stages = connection.execute(
            sa.select(templates.c.stages).where(templates.c.id == "current-template")
        ).scalar_one()
        historical_template_stages = connection.execute(
            sa.select(templates.c.stages).where(templates.c.id == "historical-template")
        ).scalar_one()
        active_stages = connection.execute(sa.select(rooms.c.template_snapshot).where(rooms.c.id == "active")).scalar_one()
        history_stages = connection.execute(sa.select(rooms.c.template_snapshot).where(rooms.c.id == "history")).scalar_one()
        assert current_template_stages[0]["turn_duration"] == 30
        assert current_template_stages[0]["preparing_turn_remaining_seconds"] == 30
        assert current_template_stages[0]["paused_turn_remaining_seconds"] == 30
        assert current_template_stages[0]["human_turn_remaining_seconds"] == 30
        assert historical_template_stages[0]["turn_duration"] == 45
        assert active_stages[0]["turn_duration"] == 30
        assert active_stages[0]["preparing_turn_remaining_seconds"] == 30
        assert history_stages[0]["turn_duration"] == 45
