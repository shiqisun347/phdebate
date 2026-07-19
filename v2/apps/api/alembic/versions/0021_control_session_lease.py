"""Bind seat control leases to authenticated browser sessions.

Revision ID: 0021_control_session_lease
Revises: 0020_test_data_scope
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0021_control_session_lease"
down_revision = "0020_test_data_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("room_seats")}
    if "control_session_id" in columns:
        return
    with op.batch_alter_table("room_seats") as batch:
        batch.add_column(sa.Column("control_session_id", sa.String(length=36), nullable=True))
        batch.create_index("ix_room_seats_control_session_id", ["control_session_id"], unique=False)
        batch.create_foreign_key(
            "fk_room_seats_control_session_id",
            "user_sessions",
            ["control_session_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("room_seats")}
    if "control_session_id" not in columns:
        return
    with op.batch_alter_table("room_seats") as batch:
        if bind.dialect.name != "sqlite":
            batch.drop_constraint("fk_room_seats_control_session_id", type_="foreignkey")
        batch.drop_index("ix_room_seats_control_session_id")
        batch.drop_column("control_session_id")
