"""Persist authoritative LightTTS stream identity.

Revision ID: 0019_audio_streaming
Revises: 0018_research_exports
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0019_audio_streaming"
down_revision = "0018_research_exports"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("speeches")}
    with op.batch_alter_table("speeches") as batch:
        if "stream_generation" not in columns:
            batch.add_column(sa.Column("stream_generation", sa.String(length=64), nullable=False, server_default=""))
        if "stream_sample_rate" not in columns:
            batch.add_column(sa.Column("stream_sample_rate", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("speeches")}
    with op.batch_alter_table("speeches") as batch:
        if "stream_sample_rate" in columns:
            batch.drop_column("stream_sample_rate")
        if "stream_generation" in columns:
            batch.drop_column("stream_generation")
