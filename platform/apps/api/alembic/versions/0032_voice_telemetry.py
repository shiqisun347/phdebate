"""Add text-only voice phase telemetry.

Revision ID: 0032_voice_telemetry
Revises: 0031_caption_segments
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032_voice_telemetry"
down_revision: str | None = "0031_caption_segments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if "voice_telemetry" in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        "voice_telemetry",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("room_id", sa.String(length=36), nullable=False),
        sa.Column("match_id", sa.String(length=36), nullable=False),
        sa.Column("speech_id", sa.String(length=36), nullable=False),
        sa.Column("generation", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("phases", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("network_summary", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("browser_report_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["match_id"], ["matches.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["room_id"], ["rooms.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["speech_id"], ["speeches.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("speech_id"),
    )
    op.create_index("ix_voice_telemetry_room_id", "voice_telemetry", ["room_id"])
    op.create_index("ix_voice_telemetry_match_id", "voice_telemetry", ["match_id"])
    op.create_index("ix_voice_telemetry_speech_id", "voice_telemetry", ["speech_id"], unique=True)


def downgrade() -> None:
    if "voice_telemetry" in set(sa.inspect(op.get_bind()).get_table_names()):
        op.drop_table("voice_telemetry")
