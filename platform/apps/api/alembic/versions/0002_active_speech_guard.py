"""Ensure each room has at most one active speech.

Revision ID: 0002_active_speech_guard
Revises: 0001_initial
"""

from alembic import op
from sqlalchemy import inspect, text

revision = "0002_active_speech_guard"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "playback_started_at" in {item["name"] for item in inspect(op.get_bind()).get_columns("speeches")}:
        return
    op.create_index(
        "uq_speeches_room_active",
        "speeches",
        ["room_id"],
        unique=True,
        postgresql_where=text("status IN ('speaking', 'synthesizing')"),
        sqlite_where=text("status IN ('speaking', 'synthesizing')"),
    )


def downgrade() -> None:
    op.drop_index("uq_speeches_room_active", table_name="speeches")
