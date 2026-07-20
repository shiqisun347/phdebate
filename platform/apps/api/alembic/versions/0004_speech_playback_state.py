"""Track authoritative AI audio playback state.

Revision ID: 0004_speech_playback_state
Revises: 0003_rating_change_idempotency
"""

import sqlalchemy as sa
from alembic import op

revision = "0004_speech_playback_state"
down_revision = "0003_rating_change_idempotency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "playback_started_at" in {item["name"] for item in sa.inspect(op.get_bind()).get_columns("speeches")}:
        return
    op.add_column("speeches", sa.Column("playback_started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("speeches", sa.Column("playback_ends_at", sa.DateTime(timezone=True), nullable=True))
    op.drop_index("uq_speeches_room_active", table_name="speeches")
    op.create_index(
        "uq_speeches_room_active",
        "speeches",
        ["room_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('speaking', 'synthesizing', 'playing')"),
        sqlite_where=sa.text("status IN ('speaking', 'synthesizing', 'playing')"),
    )


def downgrade() -> None:
    op.drop_index("uq_speeches_room_active", table_name="speeches")
    op.create_index(
        "uq_speeches_room_active",
        "speeches",
        ["room_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('speaking', 'synthesizing')"),
        sqlite_where=sa.text("status IN ('speaking', 'synthesizing')"),
    )
    op.drop_column("speeches", "playback_ends_at")
    op.drop_column("speeches", "playback_started_at")
