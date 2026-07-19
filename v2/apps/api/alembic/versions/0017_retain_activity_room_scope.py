"""Prevent classroom rooms from losing their activity authorization scope.

Revision ID: 0017_retain_activity_room_scope
Revises: 0016_consent_provenance
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017_retain_activity_room_scope"
down_revision = "0016_consent_provenance"
branch_labels = None
depends_on = None


def _activity_fk_ondelete() -> str | None:
    for foreign_key in sa.inspect(op.get_bind()).get_foreign_keys("rooms"):
        if foreign_key["constrained_columns"] == ["activity_id"]:
            return (foreign_key.get("options") or {}).get("ondelete")
    return None


def _replace_activity_fk(ondelete: str) -> None:
    with op.batch_alter_table("rooms") as batch:
        batch.drop_constraint("fk_rooms_activity_id", type_="foreignkey")
        batch.create_foreign_key(
            "fk_rooms_activity_id",
            "activities",
            ["activity_id"],
            ["id"],
            ondelete=ondelete,
        )


def upgrade() -> None:
    if _activity_fk_ondelete() != "RESTRICT":
        _replace_activity_fk("RESTRICT")


def downgrade() -> None:
    if _activity_fk_ondelete() != "SET NULL":
        _replace_activity_fk("SET NULL")
