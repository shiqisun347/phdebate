"""Permanently reserve every issued six-digit room code.

Revision ID: 0010_room_code_reservations
Revises: 0009_season_lifecycle
"""

import sqlalchemy as sa
from alembic import op

revision = "0010_room_code_reservations"
down_revision = "0009_season_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "room_code_reservations" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "room_code_reservations",
        sa.Column("code", sa.String(length=6), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("code"),
    )
    op.create_index(
        "ix_room_code_reservations_created_at",
        "room_code_reservations",
        ["created_at"],
        unique=False,
    )
    op.execute(
        """
        INSERT INTO room_code_reservations (code, created_at)
        SELECT code, created_at
        FROM rooms
        ON CONFLICT (code) DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_index("ix_room_code_reservations_created_at", table_name="room_code_reservations")
    op.drop_table("room_code_reservations")
