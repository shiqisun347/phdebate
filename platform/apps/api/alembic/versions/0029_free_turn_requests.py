"""Add the authoritative free-debate human request queue.

Revision ID: 0029_free_turn_requests
Revises: 0028_speech_corrections
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029_free_turn_requests"
down_revision: str | None = "0028_speech_corrections"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if "free_turn_requests" in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        "free_turn_requests",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("room_id", sa.String(length=36), nullable=False),
        sa.Column("match_id", sa.String(length=36), nullable=False),
        sa.Column("stage_key", sa.String(length=80), nullable=False),
        sa.Column("turn_seq", sa.Integer(), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("seat_key", sa.String(length=24), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="pending"),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution_reason", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("idempotency_key", sa.String(length=120), nullable=True),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(["match_id"], ["matches.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["room_id"], ["rooms.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("room_id", "match_id", "user_id", "status", "requested_at"):
        op.create_index(f"ix_free_turn_requests_{column}", "free_turn_requests", [column])
    op.create_index(
        "uq_free_turn_request_pending_seat",
        "free_turn_requests",
        ["room_id", "stage_key", "turn_seq", "seat_key"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
        sqlite_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "uq_free_turn_request_idempotency",
        "free_turn_requests",
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
        sqlite_where=sa.text("idempotency_key IS NOT NULL"),
    )


def downgrade() -> None:
    if "free_turn_requests" in set(sa.inspect(op.get_bind()).get_table_names()):
        op.drop_table("free_turn_requests")
