"""Cap current free-debate turns at thirty seconds.

Revision ID: 0035_clamp_free_turn_duration
Revises: 0034_retire_ai_takeover
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0035_clamp_free_turn_duration"
down_revision: str | None = "0034_retire_ai_takeover"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MAX_FREE_TURN_SECONDS = 30
ACTIVE_ROOM_STATUSES = ("lobby", "preparing", "running", "paused", "judging")


def _clamped_stages(value: Any) -> tuple[Any, bool]:
    if not isinstance(value, list):
        return value, False
    changed = False
    result: list[Any] = []
    for item in value:
        if not isinstance(item, dict) or item.get("kind") != "free":
            result.append(item)
            continue
        updated = dict(item)
        try:
            duration = int(updated.get("turn_duration", MAX_FREE_TURN_SECONDS))
        except (TypeError, ValueError):
            duration = MAX_FREE_TURN_SECONDS
        bounded = max(1, min(MAX_FREE_TURN_SECONDS, duration))
        if updated.get("turn_duration") != bounded:
            updated["turn_duration"] = bounded
            changed = True
        for remaining_key in (
            "preparing_turn_remaining_seconds",
            "paused_turn_remaining_seconds",
            "human_turn_remaining_seconds",
        ):
            if remaining_key not in updated:
                continue
            try:
                remaining = int(updated[remaining_key])
            except (TypeError, ValueError):
                remaining = bounded
            clamped_remaining = max(0, min(bounded, remaining))
            if updated.get(remaining_key) != clamped_remaining:
                updated[remaining_key] = clamped_remaining
                changed = True
        result.append(updated)
    return result, changed


def _clamp_json_rows(
    table_name: str,
    json_column: str,
    *,
    active_only: bool = False,
    row_ids: set[str] | None = None,
) -> None:
    bind = op.get_bind()
    columns = [sa.column("id", sa.String()), sa.column(json_column, sa.JSON())]
    if active_only:
        columns.append(sa.column("status", sa.String()))
    table = sa.table(table_name, *columns)
    query = sa.select(table.c.id, getattr(table.c, json_column))
    if active_only:
        query = query.where(table.c.status.in_(ACTIVE_ROOM_STATUSES))
    if row_ids is not None:
        if not row_ids:
            return
        query = query.where(table.c.id.in_(row_ids))
    for row_id, stages in bind.execute(query):
        updated, changed = _clamped_stages(stages)
        if changed:
            bind.execute(sa.update(table).where(table.c.id == row_id).values({json_column: updated}))


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "automation_templates" in tables and "competitions" in tables:
        competitions = sa.table(
            "competitions",
            sa.column("automation_template_id", sa.String()),
        )
        bound_template_ids = {
            str(template_id)
            for template_id in bind.execute(sa.select(competitions.c.automation_template_id).distinct()).scalars()
            if template_id
        }
        # Templates are versioned and historical versions are immutable. Only the
        # versions currently selected by competitions should receive the policy fix.
        _clamp_json_rows("automation_templates", "stages", row_ids=bound_template_ids)
    if "rooms" in tables:
        _clamp_json_rows("rooms", "template_snapshot", active_only=True)


def downgrade() -> None:
    # The previous duration cannot be reconstructed safely.
    pass
