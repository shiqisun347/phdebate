"""Add auditable human transcript correction requests.

Revision ID: 0028_speech_corrections
Revises: 0027_speech_data_disposition
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028_speech_corrections"
down_revision: str | None = "0027_speech_data_disposition"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "match_participants" not in tables:
        op.create_table(
            "match_participants",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("match_id", sa.String(length=36), nullable=False),
            sa.Column("room_id", sa.String(length=36), nullable=False),
            sa.Column("seat_key", sa.String(length=24), nullable=False),
            sa.Column("user_id", sa.String(length=36), nullable=False),
            sa.Column("display_name", sa.String(length=100), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["match_id"], ["matches.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["room_id"], ["rooms.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("match_id", "seat_key", name="uq_match_participant_seat"),
            sa.UniqueConstraint("match_id", "user_id", name="uq_match_participant_user"),
        )
        op.create_index("ix_match_participants_match_id", "match_participants", ["match_id"])
        op.create_index("ix_match_participants_room_id", "match_participants", ["room_id"])
        op.create_index("ix_match_participants_user_id", "match_participants", ["user_id"])
    if "speech_correction_requests" in tables:
        return
    op.create_table(
        "speech_correction_requests",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("speech_id", sa.String(length=36), nullable=False),
        sa.Column("room_id", sa.String(length=36), nullable=False),
        sa.Column("requester_user_id", sa.String(length=36), nullable=False),
        sa.Column("original_content", sa.Text(), nullable=False),
        sa.Column("proposed_content", sa.Text(), nullable=False),
        sa.Column("original_segments", sa.JSON(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="pending"),
        sa.Column("reviewed_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("review_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("idempotency_key", sa.String(length=120), nullable=True),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["requester_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["reviewed_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["room_id"], ["rooms.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["speech_id"], ["speeches.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_speech_correction_requests_speech_id", "speech_correction_requests", ["speech_id"])
    op.create_index("ix_speech_correction_requests_room_id", "speech_correction_requests", ["room_id"])
    op.create_index("ix_speech_correction_requests_requester_user_id", "speech_correction_requests", ["requester_user_id"])
    op.create_index("ix_speech_correction_requests_status", "speech_correction_requests", ["status"])
    op.create_index("ix_speech_correction_requests_reviewed_by_user_id", "speech_correction_requests", ["reviewed_by_user_id"])
    op.create_index(
        "uq_speech_correction_pending",
        "speech_correction_requests",
        ["speech_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
        sqlite_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "uq_speech_correction_idempotency",
        "speech_correction_requests",
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
        sqlite_where=sa.text("idempotency_key IS NOT NULL"),
    )


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "speech_correction_requests" in tables:
        op.drop_table("speech_correction_requests")
    if "match_participants" in tables:
        op.drop_table("match_participants")
