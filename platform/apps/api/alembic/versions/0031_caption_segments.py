"""Add recoverable presentation caption segments.

Revision ID: 0031_caption_segments
Revises: 0030_collab_documents
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031_caption_segments"
down_revision: str | None = "0030_collab_documents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if "caption_segments" in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        "caption_segments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("room_id", sa.String(length=36), nullable=False),
        sa.Column("speech_id", sa.String(length=36), nullable=False),
        sa.Column("seat_key", sa.String(length=24), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("is_final", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("timing_basis", sa.String(length=24), nullable=False, server_default="presentation"),
        sa.Column("presentation_offset_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["room_id"], ["rooms.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["speech_id"], ["speeches.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("speech_id", "source", "ordinal", name="uq_caption_segment_ordinal"),
    )
    op.create_index("ix_caption_segments_room_id", "caption_segments", ["room_id"])
    op.create_index("ix_caption_segments_speech_id", "caption_segments", ["speech_id"])
    op.create_index(
        "ix_caption_segments_room_speech_ordinal",
        "caption_segments",
        ["room_id", "speech_id", "ordinal"],
    )


def downgrade() -> None:
    if "caption_segments" in set(sa.inspect(op.get_bind()).get_table_names()):
        op.drop_table("caption_segments")
